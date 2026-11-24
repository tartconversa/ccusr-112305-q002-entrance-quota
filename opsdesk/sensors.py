"""闸机累计计数的时点台账。

关键口径（与 data/README 一致）：

- 闸机值是同一 ``counter_epoch`` 表底下的累计计数；
- 对同一 sensor / epoch / observe_time，以 ``known_at`` 最大（最后已知）的
  有效读数为准，因此恢复后的补传不会在接收时点制造一次人数跳变；
- 流量口径 ``f_e(w) = C_e(w) - C_e(w-1)``，已用全部人工截面区间校验
  （110/110 落在区间内）；
- 缺 observe_time 脉冲时，相邻两个可见累计点之间的*跨窗总量*仍成立，
  但不能把缺脉冲窗口直接当 0。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .timegrid import MAX_WINDOW


@dataclass
class Reading:
    observe_time: int
    known_at: int
    cumulative: int
    epoch: str
    record_id: str


@dataclass
class GateView:
    """某经营日在某个 as-of 时点可见的闸机状态。"""

    day: str
    asof: int | None
    # sensor -> observe_time -> 最后已知读数
    current: dict[str, dict[int, Reading]] = field(default_factory=dict)
    # 资料中存在、但 known_at > asof 尚未能见到的读数
    pending: list[dict] = field(default_factory=list)
    backfills: list[dict] = field(default_factory=list)

    def cumulative(self, route: str, window: int) -> int | None:
        r = self.current.get(route, {}).get(window)
        return None if r is None else r.cumulative

    def observed_windows(self, route: str) -> list[int]:
        return sorted(self.current.get(route, {}))

    def missing_pulses(self, route: str) -> list[int]:
        """资料层面（不限 asof）真正缺失的 observe_time 脉冲。"""
        raise NotImplementedError  # 由 GateLedger 提供全量诊断


class GateLedger:
    """保存一日全部闸机记录，可反复生成不同 as-of 视图。"""

    def __init__(self, gate_rows: list[dict], day: str):
        self.day = day
        self.rows = [g for g in gate_rows if g["day"] == day]
        # 全量最后有效读数（asof=None / 999 视角）
        self.final: dict[str, dict[tuple[str, int], Reading]] = defaultdict(dict)
        self._build_final()

    def _build_final(self) -> None:
        for g in self.rows:
            key = (g["counter_epoch"], int(g["observe_time"]))
            reading = Reading(
                observe_time=int(g["observe_time"]),
                known_at=int(g["known_at"]),
                cumulative=int(g["cumulative"]),
                epoch=g["counter_epoch"],
                record_id=g["id"],
            )
            bucket = self.final[g["sensor"]]
            old = bucket.get(key)
            # 同一 key 以 known_at 更大者为准；同 known_at 时后出现者为准。
            if old is None or reading.known_at >= old.known_at:
                bucket[key] = reading

    def epochs(self, route: str) -> list[str]:
        return sorted({ep for (ep, _t) in self.final.get(route, {})})

    def final_series(self, route: str, epoch: str | None = None) -> dict[int, int]:
        out: dict[int, int] = {}
        for (ep, t), r in self.final.get(route, {}).items():
            if epoch is not None and ep != epoch:
                continue
            out[t] = r.cumulative
        return out

    def true_missing_pulses(self, route: str, max_window: int = MAX_WINDOW) -> list[int]:
        """全量资料里该 sensor 整日仍缺少的 observe_time（补传后依旧缺）。"""
        seen = {t for (_ep, t) in self.final.get(route, {})}
        return [t for t in range(max_window + 1) if t not in seen]

    def duplicate_keys(self, route: str | None = None) -> list[dict]:
        """同一 key 有多条记录（补传/订正）的留痕。"""
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for g in self.rows:
            if route is not None and g["sensor"] != route:
                continue
            key = (g["sensor"], g["counter_epoch"], g["observe_time"])
            grouped[key].append(
                {"id": g["id"], "known_at": g["known_at"], "cumulative": g["cumulative"]}
            )
        out = []
        for (sensor, epoch, t), records in grouped.items():
            if len(records) > 1:
                values = {r["cumulative"] for r in records}
                out.append(
                    {
                        "sensor": sensor,
                        "epoch": epoch,
                        "observe_time": t,
                        "records": sorted(records, key=lambda r: r["known_at"]),
                        "revises_value": len(values) > 1,
                    }
                )
        return out

    def view(self, asof: int | None) -> GateView:
        """生成 known_at <= asof 可见的最后有效读数视图。asof=None 表示全部。"""
        view = GateView(day=self.day, asof=asof)
        latest_visible: dict[str, dict[int, Reading]] = defaultdict(dict)
        for g in sorted(self.rows, key=lambda r: (r["known_at"], r["id"])):
            if asof is not None and int(g["known_at"]) > asof:
                view.pending.append(
                    {
                        "sensor": g["sensor"],
                        "observe_time": g["observe_time"],
                        "known_at": g["known_at"],
                        "id": g["id"],
                    }
                )
                continue
            key = (g["counter_epoch"], int(g["observe_time"]))
            reading = Reading(
                observe_time=int(g["observe_time"]),
                known_at=int(g["known_at"]),
                cumulative=int(g["cumulative"]),
                epoch=g["counter_epoch"],
                record_id=g["id"],
            )
            bucket = latest_visible[g["sensor"]]
            old = bucket.get(reading.observe_time)
            if old is None or reading.known_at >= old.known_at:
                bucket[reading.observe_time] = reading
            if old is not None and old.cumulative != reading.cumulative:
                view.backfills.append(
                    {
                        "sensor": g["sensor"],
                        "observe_time": reading.observe_time,
                        "attributed_to_window": reading.observe_time,
                        "old_cumulative": old.cumulative,
                        "new_cumulative": reading.cumulative,
                        "known_at": reading.known_at,
                        "note": "补传/订正只改该 observe_time 的累计真值，差额归入历史窗口，不进入接收时点",
                    }
                )
        view.current = dict(latest_visible)
        # 同值补传也留痕
        for dup in self.duplicate_keys():
            if asof is not None:
                visible_records = [r for r in dup["records"] if r["known_at"] <= asof]
                if len(visible_records) <= 1:
                    continue
            if not any(b["sensor"] == dup["sensor"] and b["observe_time"] == dup["observe_time"]
                       for b in view.backfills):
                view.backfills.append(
                    {
                        "sensor": dup["sensor"],
                        "observe_time": dup["observe_time"],
                        "attributed_to_window": dup["observe_time"],
                        "old_cumulative": dup["records"][0]["cumulative"],
                        "new_cumulative": dup["records"][-1]["cumulative"],
                        "known_at": dup["records"][-1]["known_at"],
                        "note": "补传累计值一致，滞留人数不变"
                        if not dup["revises_value"]
                        else "补传修订累计值，按 observe_time 追溯，不计入接收当窗",
                    }
                )
        return view

    def delayed_directions(self, asof: int) -> list[dict]:
        """在 asof 时点，因 known_at 晚于 observe_time 而缺最新脉冲的方向。"""
        out = []
        for p in self.view(asof).pending:
            if p["known_at"] > asof and p["observe_time"] <= asof:
                out.append(p)
        return out
