"""连接口状态（临时封闭 / 单向 / 恢复）。

seal 的 start/end 都是当地时段编号，开始包含、结束排除，即封闭窗口为
``start <= w < end``。封闭记录是*有方向*的：AB 封闭不影响 BA，两区临时改成
单向时，反方向的空位与疏散能力不能借用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .timegrid import MAX_WINDOW


@dataclass(frozen=True)
class Seal:
    seal: str
    day: str
    route: str
    start: int
    end: int
    announced: int | None
    state: str

    def covers(self, window: int) -> bool:
        return self.start <= window < self.end


class RouteStates:
    def __init__(self, closure_rows: list[dict], day: str):
        self.day = day
        self.seals = [
            Seal(
                seal=row["seal"],
                day=row["day"],
                route=row["route"],
                start=int(row["start"]),
                end=int(row["end"]),
                announced=row.get("announced"),
                state=row["state"],
            )
            for row in closure_rows
            if row["day"] == day
        ]

    def is_closed(self, route: str, window: int) -> bool:
        """该方向在该窗口是否处于关闭状态。

        只有 state 为“关闭”的记录施加封闭；“开放/恢复”记录用于留痕与说明，
        关闭区间本身按 [start, end) 生效，end 之后自然恢复。
        """
        return any(s.route == route and s.state == "关闭" and s.covers(window) for s in self.seals)

    def closed_windows(self, route: str, max_window: int = MAX_WINDOW) -> list[int]:
        return [w for w in range(max_window + 1) if self.is_closed(route, w)]

    def one_way_pair(self, forward: str, backward: str) -> dict:
        """返回一对反向连接的单向状态说明。"""
        return {
            "forward": forward,
            "forward_closed": self.closed_windows(forward),
            "backward": backward,
            "backward_closed": self.closed_windows(backward),
            "one_way_toward_backward_windows": [
                w
                for w in range(MAX_WINDOW + 1)
                if self.is_closed(forward, w) and not self.is_closed(backward, w)
            ],
        }

    def timeline(self, routes: list[str]) -> list[dict]:
        out = []
        for route in routes:
            out.append(
                {
                    "route": route,
                    "closed_windows": self.closed_windows(route),
                    "seals": [s.__dict__ for s in self.seals if s.route == route],
                }
            )
        return out
