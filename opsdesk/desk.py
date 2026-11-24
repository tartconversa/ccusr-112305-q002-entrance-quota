"""往复使用的营运台门面：一次装载资料，产出各轮决策报告。

典型用法::

    desk = OperationsDesk()                 # 默认 /workspace/data
    report = desk.full_report()             # 完整 JSON 可序列化报告
    desk.round_report(4)                    # 只看某 as-of 轮次
    desk.backtest()                         # 历史独立点验回测
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .audit import reconstruct_quota_decision, terminal_round_table, audit_round_transition
from .backtest import run_backtest
from .capacity import CapacityBook, compare_gates, gate_entry_envelope, path_bottlenecks
from .groups import GroupBook
from .inference import infer_day
from .io import DEFAULT_DATA_DIR, Workspace, load_workspace
from .terminals import TerminalLedger

MAIN_DAY = "2026-02-17"

ACCOUNTING_RULES = [
    {
        "事项": "边界外进出 O",
        "入账": "E1(O→A)、E2(O→C) 是外部进入，记首入区流入；CO(C→O) 是外部离开，"
                "记 C 流出。O 不设容量、不进 A/B/C 平衡；任何边界残差留在 O 侧。",
    },
    {
        "事项": "同一人折返",
        "入账": "闸机按穿越次数计数、不识别身份。如 A→B(AB) 后再 B→A(BA)，两条方向"
                "分别入账（-A/+B 与 -B/+A），不得轧差成 0；全天净额可能抵消，窗内人数不会。",
    },
    {
        "事项": "跨区移动",
        "入账": "A→B→C 这类链在 AB、BC 各计一次：AB 同时是 A 流出与 B 流入，BC 同时是 "
                "B 流出与 C 流入。两侧引用同一连接序列，故相连两区必然对得上。",
    },
    {
        "事项": "封闭口前排队未穿越",
        "入账": "没有穿越脉冲就不移动分区：排队者仍计在原区（出发区）。承诺/容量预留"
                "口径另行登记为“未来流入”，但不提前计入到达区人数。",
    },
    {
        "事项": "已扫码未走完通道",
        "入账": "已签到在途团按 data/README 在出发区统计，不视为已离场；同时作为刚性"
                "未来流入预留到达区容量。",
    },
    {
        "事项": "缺脉冲",
        "入账": "缺 observe_time 时只得到相邻累计点之间的跨窗总量，按区间/等式传播，"
                "绝不把缺脉冲窗口设为 0。",
    },
    {
        "事项": "恢复后补传",
        "入账": "同一 sensor/epoch/observe_time 以最后 known_at 读数为准；累计差额追溯"
                "到 observe_time 对应窗口，不在 known_at 接收当窗制造人数跳变。",
    },
]


class OperationsDesk:
    def __init__(self, data_dir: str | Path | None = None, day: str = MAIN_DAY):
        self.ws: Workspace = load_workspace(data_dir or DEFAULT_DATA_DIR)
        self.day = day if day in self.ws.days else self.ws.days[-1]
        self.capacity = CapacityBook(self.ws)
        self.terminals = TerminalLedger(self.ws, self.day)
        self.groups = GroupBook(self.ws, self.day)

    # ------------------------------------------------------------------
    def inference(self, asof: int | None = None):
        return infer_day(self.ws, self.day, asof)

    def round_report(self, asof: int) -> dict:
        inf = self.inference(asof)
        gates4 = {g: gate_entry_envelope(inf, g, 4, "operating")
                  for g in inf.network.entrance_gates()}
        gates4_hard = {g: gate_entry_envelope(inf, g, 4, "hard")
                       for g in inf.network.entrance_gates()}
        return {
            "day": self.day, "asof": asof,
            "stock": {z: [r for r in rows] for z, rows in inf.stock_table().items()},
            "gate_comparison_W4": compare_gates(inf, 4),
            "entry_envelopes_W4_operating": gates4,
            "entry_envelopes_W4_hard": gates4_hard,
            "limiting_A_W4": inf.limiting_directions("A", 4),
            "limiting_C_W4": inf.limiting_directions("C", 4),
            "diagnostics": inf.diagnostics(),
            "manual_reconciliation": [asdict(r) for r in inf.reconciliations],
            "terminal_snapshot": self.terminals.snapshot(asof if asof is not None else 99),
        }

    # ------------------------------------------------------------------
    def stop_trace(self) -> dict:
        """晚到计数把某区推向上限时，追到应停窗口、有效票与待回收额度。"""
        inf6 = self.inference(6)
        triggers = []
        for c in self.ws.counts:
            if c["day"] != self.day or int(c["known_at"]) > 6:
                continue
            if c["kind"] != "区内":
                continue
            z = c["gate_or_zone"]
            zl = self.capacity.limits[z]
            if int(c["high"]) >= zl.operating or int(c["low"]) >= zl.operating - 1:
                n6 = inf6.stock(z, 6)
                triggers.append({
                    "count_id": c["id"], "zone": z,
                    "count_range": [c["low"], c["high"]], "known_at": c["known_at"],
                    "inferred_N6": n6.to_dict(),
                    "operating_cap": zl.operating, "hard_cap": zl.hard,
                })
        valid_tickets = {
            "sold_deducted_to_version": self.terminals.version_sales(),
            "must_serve_groups": [g.to_dict() for g in self.groups.groups if g.policy["must_serve"]],
            "note": "已出票/已签到在途均为仍有效承诺，停的是*新*售票，不是这些票",
        }
        return {
            "triggers": triggers,
            "stop_new_issue": [
                {"gate": "E1", "from_window": 6,
                 "action": "立即停止 E1 后续半小时新票签发；L1 不再追加，T-A 下一批额度停发",
                 "why": "A@W6 保守上界 46 已越过运营上限 41，独立收尾 A=48 触硬上限"}
            ],
            "valid_tickets": valid_tickets,
            "terminal_recovery_trace": self.terminals.recovery_trace(),
            "terminal_holds_latest": self.terminals.snapshot(7)["holds"],
        }

    # ------------------------------------------------------------------
    def group_plan(self) -> dict:
        # 用全日资料重建出的最佳容量图景安排各团；W4 的保守/中值口径另行报告。
        inf = self.inference(None)
        # 以硬上限口径安置“必须接待”的在途团；运营口径用于常规份额。
        caps: dict[tuple[str, int], int] = {}
        for w in range(4, 9):
            for g in inf.network.entrance_gates():
                try:
                    hard = gate_entry_envelope(inf, g, w, "hard")
                    caps[(g, w)] = hard["safe_total_gate_flow_conservative"]
                except Exception:
                    caps[(g, w)] = 0
        plans = self.groups.diversion_options(caps)
        claims_W4 = {
            "must_serve_groups": self.groups.committed_people(4),
            "all_groups": self.groups.all_people(4),
            "terminal": self.terminals.snapshot(7)["totals"]["by_gate_window"],
        }
        safe_W4 = {
            g: {"operating": gate_entry_envelope(inf, g, 4, "operating")[
                    "safe_total_gate_flow_conservative"],
                "hard": gate_entry_envelope(inf, g, 4, "hard")[
                    "safe_total_gate_flow_conservative"]}
            for g in inf.network.entrance_gates()
        }
        return {"capacities_used_hard": {f"{g}/W{w}": v for (g, w), v in caps.items()},
                "plans": plans, "claims_W4": claims_W4, "safe_W4": safe_W4}

    # ------------------------------------------------------------------
    def closure_impact(self) -> dict:
        """封闭口侧客流骤降，但替代入口/雪道流量没有同步减少的对照。"""
        inf = self.inference(None)
        blocks = []
        for seal in inf.states.seals:
            if seal.state != "关闭":
                continue
            closed_w = list(range(seal.start, seal.end))
            route_flows = {f"W{w}": inf.flow(seal.route, w).exact_value for w in closed_w}
            blocks.append({
                "seal": seal.seal, "route": seal.route,
                "closed_windows": [f"W{w}" for w in closed_w],
                "flows_during_closure": route_flows,
                "interpretation": "封闭方向闸机增量归零，是通道状态所致，不能解读为需求消失",
            })
        # 入门区旁连接口 AB 封闭 W3-5；替代入口 E2(O→C) 与下游 BC/CO 仍保持流量
        alt = {
            "E2_entries_W1_W8": {f"W{w}": inf.flow("E2", w).exact_value for w in range(1, 9)},
            "E2_during_AB_closure_W3_W5": [inf.flow("E2", w).exact_value for w in (3, 4, 5)],
            "note": "E2 各窗仍有 4-6 人稳定进入，绕行/替代雪道口客流没有随 AB 闸机一起消失；"
                    "团体应优先改引 E2，而不是假设总需求下降",
        }
        one_way = inf.states.one_way_pair("AB", "BA")
        return {"closures": blocks, "alternative_route": alt, "one_way": one_way}

    # ------------------------------------------------------------------
    def response_options(self) -> dict:
        """经理可比较的两类主动作：入口分流 vs 暂缓发售/回收，并留住团体份额。"""
        inf = self.inference(4)
        safe = {g: gate_entry_envelope(inf, g, 4, "hard")
                for g in inf.network.entrance_gates()}
        safe_op = {g: gate_entry_envelope(inf, g, 4, "operating")
                   for g in inf.network.entrance_gates()}
        term = self.terminals.snapshot(7)["totals"]["by_gate_window"]
        group_w4 = {g: self.groups.committed_people(4, g)
                    for g in ("E1", "E2")}
        rows = {}
        for g in ("E1", "E2"):
            t = term.get(f"{g}/W4", {"terminal_commitment": 0, "sold": 0,
                                     "open_remainder": 0})
            claims = group_w4[g] + t["terminal_commitment"]
            rows[g] = {
                "must_serve_group_people_W4": group_w4[g],
                "sold_tickets_version_deducted": t["sold"],
                "terminal_unrecovered_remainder": t["open_remainder"],
                "total_claims_conservative": claims,
                "safe_hard_slots": safe[g]["safe_total_gate_flow_conservative"],
                "safe_operating_slots": safe_op[g]["safe_total_gate_flow_conservative"],
                "hard_gap_claims_minus_slots": claims - safe[g]["safe_total_gate_flow_conservative"],
            }
        total_claims = sum(r["total_claims_conservative"] for r in rows.values())
        total_slots = sum(r["safe_hard_slots"] for r in rows.values())
        plan = self.group_plan()
        diverted = []
        for p in plan["plans"]:
            for a in p["allocations"]:
                if a["change"].startswith("改引"):
                    diverted.append({"ticket": p["ticket"], **a})
        return {
            "window": 4,
            "by_gate": rows,
            "total_conservative_claims": total_claims,
            "total_hard_slots": total_slots,
            "aggregate_gap": total_claims - total_slots,
            "protected_group_share": {
                "W4_must_serve": self.groups.committed_people(4),
                "policy": "先保留在途 G1 与已出票 G2 的承诺份额，再决定散客终端额度",
            },
            "option_divert_entrance": {
                "description": "把可改口的已出票团 G2 部分由 E1 改引 E2，集合时间 W4 不变",
                "diverted_allocations": diverted,
                "effect": "启用 E2 空余硬容量 8，降低 E1 瓶颈；不改变 W4 总缺口，只做口间再平衡",
            },
            "option_hold_sales": {
                "description": "停止 E1 新散客票、停发 T-A 下一批、催收回执与未回收额度，"
                               "G3/G4 走协商改场次",
                "recoverable_terminal_remainder": sum(
                    r["terminal_unrecovered_remainder"] for r in rows.values()),
                "central_quota_cut_L1_14_to_5": 9,
                "negotiable_groups": [g.to_dict() for g in self.groups.groups
                                      if g.policy["may_shift_window"]],
                "effect": "中央口径已由 L1/1=14 压到 L1/2=5；旧版未回收 3 与 T-B 未回收 4 "
                          "在确认回收前仍占总承诺",
            },
            "must_continue_serve": [g.to_dict() for g in self.groups.groups
                                    if g.stages == "已签到在途"],
        }

    # ------------------------------------------------------------------
    def audit(self) -> dict:
        transitions = []
        prev = None
        for a in [3, 4, 6, 7, 9]:
            transitions.append(audit_round_transition(self.ws, self.day, prev, a))
            prev = a
        return {
            "quota_L1_v2_reconstruction": reconstruct_quota_decision(self.ws, self.day, 4),
            "terminal_rounds": terminal_round_table(self.ws, self.day),
            "transitions": transitions,
        }

    def backtest(self) -> dict:
        return run_backtest(self.ws)

    # ------------------------------------------------------------------
    def full_report(self) -> dict:
        inf_full = self.inference(None)
        return {
            "day": self.day,
            "data_dir": str(self.ws.data_dir),
            "accounting_rules": ACCOUNTING_RULES,
            "limits": {z: asdict(self.capacity.limits[z]) for z in self.capacity.limits},
            "network": {e: asdict(r) for e, r in inf_full.network.routes.items()},
            "route_states": inf_full.states.timeline(list(inf_full.network.routes)),
            "closure_impact": self.closure_impact(),
            "occupancy_reconstructed": inf_full.stock_table(),
            "flows_reconstructed": inf_full.flow_table(),
            "cross_zone_match_sample": inf_full.cross_zone_match()[:12],
            "manual_reconciliation_all": [asdict(r) for r in inf_full.reconciliations],
            "diagnostics_full": inf_full.diagnostics(),
            "decision_W4_pre_pulse_asof3": self.round_report(3),
            "decision_W4_asof4": self.round_report(4),
            "path_bottlenecks_W4": {
                g: path_bottlenecks(self.inference(4), g, 4)
                for g in ("E1", "E2")
            },
            "response_options": self.response_options(),
            "groups": {"triage": self.groups.triage(), "plan": self.group_plan()},
            "terminals_latest": self.terminals.snapshot(None),
            "stop_trace": self.stop_trace(),
            "audit": self.audit(),
            "backtest": self.backtest(),
        }
