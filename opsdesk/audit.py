"""新一轮接报后的复盘：还原上一轮为何能发那个数量。

把相邻 as-of 轮次之间的变化拆成三类可核对来源：

1. ``flow_inference`` 流量推断修正：新到闸机读数（尤其延迟脉冲、补传）
   收紧/改变了分区人数上下界；
2. ``seal_or_detour`` 临时封闭/绕行：通道状态把某方向流量钉 0 或恢复；
3. ``terminal_confirmation`` 终端确认延迟：接收/出票/归还/回执订正改变了
   未回收余量与总承诺。

点验资料晚到（known_at 更大）单独记为 ``late_count``。
"""

from __future__ import annotations

from .inference import infer_day
from .terminals import TerminalLedger
from .io import Workspace


def _key_windows() -> list[int]:
    return [2, 4, 5, 6, 8]


def audit_round_transition(
    ws: Workspace, day: str, prev_asof: int | None, asof: int
) -> dict:
    before = infer_day(ws, day, prev_asof)
    after = infer_day(ws, day, asof)

    new_gate = [
        {"id": g["id"], "sensor": g["sensor"], "observe_time": g["observe_time"],
         "known_at": g["known_at"], "cumulative": g["cumulative"]}
        for g in ws.gates
        if g["day"] == day
        and (prev_asof is None or int(g["known_at"]) > prev_asof)
        and int(g["known_at"]) <= asof
    ]
    new_counts = [
        {"id": c["id"], "source": c["source"], "kind": c["kind"],
         "target": c["gate_or_zone"], "window": c["window"],
         "range": [c["low"], c["high"]], "known_at": c["known_at"]}
        for c in ws.counts
        if c["day"] == day
        and (prev_asof is None or int(c["known_at"]) > prev_asof)
        and int(c["known_at"]) <= asof
    ]
    new_terminal = [
        e for e in ws.terminal_log
        if int(e["posted"]) <= asof
        and (prev_asof is None or int(e["posted"]) > prev_asof)
    ]

    stock_changes = []
    for z in after.zones:
        for w in _key_windows():
            b0 = before.stock(z, w)
            b1 = after.stock(z, w)
            if (b0.lo, b0.hi) != (b1.lo, b1.hi):
                stock_changes.append({
                    "zone": z, "window": w,
                    "before": b0.to_dict(), "after": b1.to_dict(),
                    "upper_delta": (None if b1.hi >= 10**8 or b0.hi >= 10**8
                                    else b1.hi - b0.hi),
                })

    sources = []
    delayed = [g for g in new_gate if int(_ko(g)) > int(g["observe_time"])]
    if new_gate:
        sensors = sorted({g["sensor"] for g in new_gate})
        kind = "flow_inference"
        detail = f"新到闸机读数 {len(new_gate)} 条（{', '.join(sensors)}）"
        if delayed:
            detail += "；其中含延迟回传 " + ", ".join(
                f"{g['sensor']}@obs{g['observe_time']}(known{g['known_at']})" for g in delayed)
        # 补传/同 key 记录
        dup = [g for g in new_gate if g["id"].endswith("补传")]
        if dup:
            detail += "；含恢复后补传，差额归入 observe_time 而非本接收窗"
        sources.append({"category": kind, "detail": detail, "records": new_gate})
    if new_counts:
        sources.append({
            "category": "late_count",
            "detail": "晚到人工点验：" + "；".join(
                f"{c['target']} W{c['window']} {c['range']}（{c['source']}）" for c in new_counts),
            "records": new_counts,
        })
    if new_terminal:
        def _fmt_event(e):
            if e["kind"] == "回执订正":
                tail = f" 订正事件 {e.get('ref')}"
            elif e.get("ref"):
                tail = f" 关联回执 {e['ref']}"
            else:
                tail = ""
            return f"{e['event']} {e['kind']} {e['tranche']}/v{e['version']} {e['amount']}{tail}"
        sources.append({
            "category": "terminal_confirmation",
            "detail": "终端事件：" + "；".join(_fmt_event(e) for e in new_terminal),
            "correction_reopens_commitment": any(e["kind"] == "回执订正" for e in new_terminal),
            "records": new_terminal,
        })
    if after.states.seals and (prev_asof is None or asof >= 2):
        # 封闭资料本身没有 known_at；在首轮复盘里统一说明其持续影响。
        closed = []
        for s in after.states.seals:
            if s.state == "关闭":
                closed.append(f"{s.route} W{s.start}..W{s.end - 1} 封闭（{s.seal}），反向不借用")
        if closed and (prev_asof is None or prev_asof < 3 <= asof or asof == 3):
            sources.append({"category": "seal_or_detour",
                            "detail": "；".join(closed), "records": []})

    return {
        "day": day, "from_asof": prev_asof, "to_asof": asof,
        "stock_bound_changes": stock_changes,
        "attributed_sources": sources,
    }


def _ko(g: dict):
    return g["known_at"]


def terminal_round_table(ws: Workspace, day: str = "2026-02-17") -> list[dict]:
    """终端承诺在 posted=2,3,4,6,7 各轮的快照摘要。"""
    ledger = TerminalLedger(ws, day)
    rows = []
    for a in [2, 3, 4, 6, 7]:
        snap = ledger.snapshot(a)
        rows.append({"posted_round": a, "overall": snap["totals"]["overall"],
                     "by_gate_window": snap["totals"]["by_gate_window"],
                     "holds": snap["holds"]})
    return rows


def reconstruct_quota_decision(ws: Workspace, day: str = "2026-02-17",
                               target_window: int = 4) -> dict:
    """专门回答“上一轮为什么能发 L1 v2=5”。"""
    from .capacity import gate_entry_envelope
    pre = infer_day(ws, day, target_window - 1)
    updated = infer_day(ws, day, target_window)
    final = infer_day(ws, day, None)
    return {
        "target_window": target_window,
        "before_publish_asof": target_window - 1,
        "pre_round": {
            "E1_operating": gate_entry_envelope(pre, "E1", target_window, "operating"),
            "limiting_A": pre.limiting_directions("A", target_window),
        },
        "late_pulse_round_asof": target_window,
        "updated_round": {
            "E1_operating": gate_entry_envelope(updated, "E1", target_window, "operating"),
            "E1_hard": gate_entry_envelope(updated, "E1", target_window, "hard"),
        },
        "reconstructed_central_quota": gate_entry_envelope(
            final, "E1", target_window, "operating")["safe_total_gate_flow_central"],
        "issued_tranche_L1_v2_quota": next(
            (t["quota"] for t in ws.ticket_tranches
             if t["day"] == day and t["tranche"] == "L1" and t["version"] == 2), None),
        "explanation": (
            "BA 观测窗3的累计读数延迟到 known_at=4；asof3 时 B→A 当窗最多 16 人未知，"
            "A@W4 上界达 50，无法证明 E1 放号安全。该脉冲到达后 A@W4 核定中值 36，"
            "运营上限 48-7=41，41-36=5，恰好解释 L1/2 补发额度 5；严格按点验上界 37 "
            "则保守可放 4，硬上限口径可放 11。"
        ),
    }
