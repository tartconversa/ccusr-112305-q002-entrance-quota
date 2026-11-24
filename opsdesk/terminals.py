"""半小时限额版本与区域票务终端台账。

口径（对应用户口述与 data/README）：

- 每个 ``(tranche, version)`` 是一次签发版本；``replaces`` 指向被替换的旧版；
- 实际售票（出票）必须扣到具体版本，不能只扣限额名称；
- 旧版限额即使被新版替换，已经下发到终端且*未对账回收*的余量仍可能出票，
  在确认回收前继续计入“总承诺”；
- 回执订正通过 ``ref`` 覆盖原事件的有效金额（最后订正为准）；
- 接收失败是确定的“未收到”，可停发该终端下一批；完全没有接收回执时不能
  假定终端没有出票，按保守口径把整版限额列为未证实敞口。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .io import Workspace


@dataclass
class VersionState:
    tranche: str
    version: int
    day: str
    window: int
    gate: str
    terminal: str
    quota: int
    replaces: str | None
    current_version: int
    receive_status: str = "无回执"
    received: int = 0
    receive_failed: int = 0
    sold: int = 0
    cancelled: int = 0
    returned_confirmed: int = 0
    open_remainder: int = 0
    terminal_commitment: int = 0
    unacked_exposure: int = 0
    stop_next_batch: bool = False
    events: list[dict] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.version == self.current_version

    def to_dict(self) -> dict:
        return {
            "tranche": self.tranche,
            "version": self.version,
            "key": f"{self.tranche}/{self.version}",
            "day": self.day,
            "window": self.window,
            "gate": self.gate,
            "terminal": self.terminal,
            "quota": self.quota,
            "replaces": self.replaces,
            "is_current": self.version == self.current_version,
            "receive_status": self.receive_status,
            "received": self.received,
            "receive_failed_events": self.receive_failed,
            "sold_version_deducted": self.sold,
            "cancelled": self.cancelled,
            "returned_confirmed": self.returned_confirmed,
            "open_remainder": self.open_remainder,
            "terminal_commitment_sold_plus_open": self.terminal_commitment,
            "unacked_exposure": self.unacked_exposure,
            "stop_next_batch": self.stop_next_batch,
        }


class TerminalLedger:
    def __init__(self, ws: Workspace, day: str = "2026-02-17"):
        self.ws = ws
        self.day = day
        self.tranches = [t for t in ws.ticket_tranches if t["day"] == day]
        self.events = [e for e in ws.terminal_log if self._event_day(e) == day]
        self.current_versions: dict[str, int] = defaultdict(int)
        for t in self.tranches:
            self.current_versions[t["tranche"]] = max(
                self.current_versions[t["tranche"]], int(t["version"])
            )

    def _event_day(self, e: dict) -> str:
        # terminal_log 本身没有 day 字段：资料只覆盖主经营日；若有则用之。
        return e.get("day", self.day)

    # ------------------------------------------------------------------
    def snapshot(self, asof_posted: int | None = None) -> dict:
        """重放到 posted <= asof_posted 的事件状态。None 表示全部事件。"""
        events = [
            e for e in self.events
            if asof_posted is None or int(e["posted"]) <= asof_posted
        ]
        corrections: dict[str, dict] = {}
        for e in events:
            if e["kind"] == "回执订正" and e.get("ref"):
                old = corrections.get(e["ref"])
                if old is None or int(e["posted"]) >= int(old["posted"]):
                    corrections[e["ref"]] = e

        states: dict[tuple, VersionState] = {}
        for t in self.tranches:
            key = (t["tranche"], int(t["version"]), t["terminal"])
            states[key] = VersionState(
                tranche=t["tranche"], version=int(t["version"]), day=t["day"],
                window=int(t["window"]), gate=t["gate"], terminal=t["terminal"],
                quota=int(t["quota"]), replaces=t.get("replaces"),
                current_version=self.current_versions[t["tranche"]],
            )

        for e in sorted(events, key=lambda x: (int(x["posted"]), x["event"])):
            key = (e["tranche"], int(e["version"]), e["terminal"])
            if key not in states:
                # 事件必须能扣到具体签发版本（数据契约也校验此外键）。
                raise KeyError(f"终端事件 {e['event']} 找不到对应限额版本 {key}")
            st = states[key]
            st.events.append(e)
            kind, amount = e["kind"], int(e["amount"])
            if kind == "接收":
                st.receive_status = "已接收"
                st.received += amount
            elif kind == "接收失败":
                st.receive_failed += 1
                if st.receive_status == "无回执":
                    st.receive_status = "接收失败"
                st.stop_next_batch = True
            elif kind == "出票":
                st.sold += amount
            elif kind in ("撤销", "退票撤销"):
                st.cancelled += amount
            elif kind == "归还":
                corrected = corrections.get(e["event"])
                effective = int(corrected["amount"]) if corrected else amount
                st.returned_confirmed += effective

        result_states = []
        for st in states.values():
            # 若归还事件之后才有订正，上面的循环可能按未订正金额计入；
            # 用订正表整体重算一次，保证“最后订正为准”。
            returned = 0
            for e in st.events:
                if e["kind"] == "归还":
                    corr = corrections.get(e["event"])
                    returned += int(corr["amount"]) if corr else int(e["amount"])
            st.returned_confirmed = returned
            st.open_remainder = max(0, st.received - st.sold - st.cancelled - st.returned_confirmed)
            # 已确认售票 + 仍在终端未对账回收、可能继续出票的余量
            st.terminal_commitment = st.sold + st.open_remainder
            if st.receive_status == "无回执":
                # 完全没有接收回执：不能假定终端没出票，整版额度保守计为敞口。
                st.unacked_exposure = st.quota
            elif st.receive_status == "接收失败":
                st.unacked_exposure = 0
            else:
                st.unacked_exposure = 0
            result_states.append(st)

        totals = self._totals(result_states)
        return {
            "asof_posted": asof_posted,
            "versions": [s.to_dict() for s in sorted(
                result_states, key=lambda s: (s.window, s.gate, s.tranche, s.version))],
            "totals": totals,
            "holds": self._holds(result_states),
        }

    @staticmethod
    def _totals(states: list[VersionState]) -> dict:
        by_gate_window: dict[str, dict] = defaultdict(
            lambda: {"sold": 0, "open_remainder": 0, "terminal_commitment": 0,
                     "unacked_exposure": 0, "central_current_quota": 0,
                     "central_superseded_quota": 0})
        total = {"sold": 0, "open_remainder": 0, "terminal_commitment": 0,
                 "unacked_exposure": 0, "total_commitment_conservative": 0}
        for st in states:
            k = f"{st.gate}/W{st.window}"
            bucket = by_gate_window[k]
            bucket["sold"] += st.sold
            bucket["open_remainder"] += st.open_remainder
            bucket["terminal_commitment"] += st.terminal_commitment
            bucket["unacked_exposure"] += st.unacked_exposure
            if st.is_current:
                bucket["central_current_quota"] += st.quota
            else:
                bucket["central_superseded_quota"] += st.quota
            total["sold"] += st.sold
            total["open_remainder"] += st.open_remainder
            total["terminal_commitment"] += st.terminal_commitment
            total["unacked_exposure"] += st.unacked_exposure
        # 总承诺：已出票 + 未回收终端余量 + 无回执整版敞口
        total["total_commitment_conservative"] = (
            total["terminal_commitment"] + total["unacked_exposure"]
        )
        return {"overall": total, "by_gate_window": dict(by_gate_window)}

    @staticmethod
    def _holds(states: list[VersionState]) -> list[dict]:
        holds = []
        for st in states:
            if st.stop_next_batch:
                holds.append({
                    "terminal": st.terminal,
                    "action": "停发该终端下一批额度，直至接收链路确认恢复",
                    "reason": f"{st.tranche}/{st.version} 接收失败",
                    "unreconciled_remainder": st.open_remainder,
                })
            if st.receive_status == "无回执":
                holds.append({
                    "terminal": st.terminal,
                    "action": "立即索取接收/出票回执；无回执期间按整版额度保守计入承诺",
                    "reason": f"{st.tranche}/{st.version} 无任何接收回执",
                    "unacked_exposure": st.unacked_exposure,
                })
        return holds

    # ------------------------------------------------------------------
    def version_sales(self) -> dict[str, int]:
        """按具体签发版本汇总实际出票（扣到版本）。"""
        sales: dict[str, int] = defaultdict(int)
        for e in self.events:
            if e["kind"] == "出票":
                sales[f"{e['tranche']}/v{e['version']}@{e['terminal']}"] += int(e["amount"])
        return dict(sales)

    def recovery_trace(self, asof_posted: int | None = None) -> list[dict]:
        """各终端等待回收/对账的额度，供超员追踪。"""
        snap = self.snapshot(asof_posted)
        rows = []
        for v in snap["versions"]:
            if v["open_remainder"] > 0 or v["unacked_exposure"] > 0:
                rows.append({
                    "tranche_version": f"{v['tranche']}/{v['version']}",
                    "gate": v["gate"], "window": v["window"],
                    "terminal": v["terminal"],
                    "sold_valid_tickets": v["sold_version_deducted"],
                    "awaiting_recovery_or_ack": max(v["open_remainder"], v["unacked_exposure"])
                    if v["receive_status"] == "无回执" else v["open_remainder"],
                    "kind": "无回执整版敞口" if v["receive_status"] == "无回执"
                    else "已下发未回收余量",
                })
        return rows
