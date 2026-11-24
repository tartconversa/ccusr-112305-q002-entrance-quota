"""预售团体的承诺分级与改引入口。

阶段（stages）对应容量不足时的三类处置：

- ``已签到在途``：已扫码、尚未走完入口通道，按口径仍计在出发区，必须继续接待；
- ``已出票``：有效票，等待扫码，按预计到场预留容量，不能当成已离场；
- ``已付待发`` / ``可协商``：可协商改场次/暂缓的对象。

团体有指定入口与接待窗口；改发入口时必须保留其有效集合（接待）时间，即
默认仍落在原窗口，只有协商改场次后才移动 window。
"""

from __future__ import annotations

from dataclasses import dataclass

from .io import Workspace

# 处置优先级与容量刚性
STAGE_POLICY = {
    "已签到在途": {
        "rank": 0,
        "must_serve": True,
        "may_divert_gate": False,   # 人已在原入口通道/出发区，原则上在原口继续接待
        "may_shift_window": False,
        "label": "已经在途，须继续接待",
    },
    "已出票": {
        "rank": 1,
        "must_serve": True,
        "may_divert_gate": True,    # 票仍有效，可改引 alternate_gates，集合时间不变
        "may_shift_window": False,  # 除非另行协商
        "label": "有效票，保留承诺份额，可改口但保留集合时间",
    },
    "已付待发": {
        "rank": 2,
        "must_serve": False,
        "may_divert_gate": True,
        "may_shift_window": True,
        "label": "已付款未出票，可停发/协商改场次",
    },
    "可协商": {
        "rank": 3,
        "must_serve": False,
        "may_divert_gate": True,
        "may_shift_window": True,
        "label": "可协商改场次，优先用于暂缓发售方案",
    },
}


@dataclass
class Group:
    ticket: str
    day: str
    people: int
    window: int
    assigned_gate: str
    stages: str
    alternate_gates: list[str]

    @property
    def policy(self) -> dict:
        return STAGE_POLICY.get(self.stages, STAGE_POLICY["可协商"])

    def to_dict(self) -> dict:
        p = self.policy
        return {
            "ticket": self.ticket,
            "people": self.people,
            "window": self.window,
            "assigned_gate": self.assigned_gate,
            "stages": self.stages,
            "alternate_gates": self.alternate_gates,
            "action_class": p["label"],
            "must_serve": p["must_serve"],
            "may_divert_gate": p["may_divert_gate"],
            "may_shift_window": p["may_shift_window"],
            "assembly_time_preserved": True,
        }


class GroupBook:
    def __init__(self, ws: Workspace, day: str = "2026-02-17"):
        self.groups = [
            Group(
                ticket=r["ticket"], day=r["day"], people=int(r["people"]),
                window=int(r["window"]), assigned_gate=r["assigned_gate"],
                stages=r["stages"], alternate_gates=list(r.get("alternate_gates", [])),
            )
            for r in ws.group_tickets if r["day"] == day
        ]

    def by_window_gate(self) -> dict[tuple[int, str], list[Group]]:
        out: dict[tuple[int, str], list[Group]] = {}
        for g in self.groups:
            out.setdefault((g.window, g.assigned_gate), []).append(g)
        return out

    def committed_people(self, window: int, gate: str | None = None) -> int:
        """刚性承诺人数：在途 + 已出票（其余列为可协商，不计入必须保留份额）。"""
        return sum(
            g.people for g in self.groups
            if g.window == window and (gate is None or g.assigned_gate == gate)
            if g.policy["must_serve"]
        )

    def all_people(self, window: int, gate: str | None = None) -> int:
        return sum(
            g.people for g in self.groups
            if g.window == window and (gate is None or g.assigned_gate == gate)
        )

    def triage(self) -> list[dict]:
        return [g.to_dict() for g in sorted(self.groups, key=lambda g: g.policy["rank"])]

    def diversion_options(self, capacities: dict[tuple[str, int], int]) -> list[dict]:
        """给定各 (gate,window) 可容纳改引人数，给出逐团改引/暂缓建议。

        capacities: {(gate, window): 可分配名额}。在不移动集合时间的前提下，
        先尝试原口，再尝试 alternate_gates 同窗口；仍放不下且可协商窗口时，
        才建议改场次。
        """
        remaining = dict(capacities)
        plans = []
        for g in sorted(self.groups, key=lambda x: x.policy["rank"]):
            need = g.people
            alloc: list[dict] = []
            # 1) 原入口同窗口
            key = (g.assigned_gate, g.window)
            take = min(need, remaining.get(key, 0))
            if take:
                alloc.append({"gate": g.assigned_gate, "window": g.window, "people": take,
                              "change": "原口"})
                remaining[key] -= take
                need -= take
            # 2) 备用入口，保留集合时间（同窗口）
            if need and g.policy["may_divert_gate"]:
                for alt in g.alternate_gates:
                    key = (alt, g.window)
                    take = min(need, remaining.get(key, 0))
                    if take:
                        alloc.append({"gate": alt, "window": g.window, "people": take,
                                      "change": f"改引 {alt}，集合时间 W{g.window} 不变"})
                        remaining[key] -= take
                        need -= take
                    if not need:
                        break
            # 3) 可协商团才建议改场次
            shift = None
            if need and g.policy["may_shift_window"]:
                for (gate, w), cap in sorted(remaining.items(), key=lambda kv: kv[0][1]):
                    if w <= g.window:
                        continue
                    take = min(need, cap)
                    if take:
                        alloc.append({"gate": gate, "window": w, "people": take,
                                      "change": f"协商改场次 W{w}"})
                        remaining[(gate, w)] -= take
                        need -= take
                        shift = w
                    if not need:
                        break
            if need:
                recommendation = "容量不足：须暂缓新售票并继续协商，未安置刚性份额需升级值班经理" \
                    if g.policy["must_serve"] else "建议暂缓发售/协商改场次"
            else:
                recommendation = "可安置"
            plans.append({
                "ticket": g.ticket, "people": g.people, "stages": g.stages,
                "assembly_window": g.window, "allocations": alloc,
                "unplaced": need, "recommendation": recommendation,
                "shifted_to_window": shift,
            })
        return plans
