"""分区—连接口有向网络。

O 表示外部（不做容量分区），A 练习地、B 坡道、C 等候区。route_map 给出
*有向* 连接与每窗口正常通过上限；AB 与 BA 是两条独立方向，不能互相借用容量。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

OUTSIDE = "O"
ZONES = ("A", "B", "C")


@dataclass(frozen=True)
class Route:
    route: str
    src: str
    dst: str
    capacity: int

    @property
    def gate(self) -> str:
        """入口类连接（O→分区）通常把 route 编号直接当作闸机/入口号。"""
        return self.route


class Network:
    def __init__(self, route_rows: list[dict]):
        self.routes: dict[str, Route] = {}
        for row in route_rows:
            route = Route(
                route=row["route"],
                src=row["from_zone"],
                dst=row["to_zone"],
                capacity=int(row["slot_capacity"]),
            )
            if route.route in self.routes:
                raise ValueError(f"连接 {route.route} 重复")
            self.routes[route.route] = route
        self.out_edges: dict[str, list[str]] = defaultdict(list)
        self.in_edges: dict[str, list[str]] = defaultdict(list)
        for e, r in self.routes.items():
            self.out_edges[r.src].append(e)
            self.in_edges[r.dst].append(e)

    def __contains__(self, route: str) -> bool:
        return route in self.routes

    def edge(self, route: str) -> Route:
        return self.routes[route]

    def balance_terms(self, zone: str) -> list[tuple[str, int]]:
        """区域平衡式中各连接的符号：流入 +1，流出 -1。"""
        terms: list[tuple[str, int]] = []
        for e in self.in_edges.get(zone, []):
            terms.append((e, +1))
        for e in self.out_edges.get(zone, []):
            terms.append((e, -1))
        return terms

    def entrance_gates(self) -> list[str]:
        return [e for e, r in self.routes.items() if r.src == OUTSIDE]

    def exit_gates(self) -> list[str]:
        return [e for e, r in self.routes.items() if r.dst == OUTSIDE]

    def paths_from(self, gate: str) -> list[list[str]]:
        """从某入口连接出发的全部简单有向路径（边编号序列）。"""
        root = self.routes[gate]
        results: list[list[str]] = []

        def walk(zone: str, edges: list[str], seen_zones: set[str]) -> None:
            progressed = False
            for e in self.out_edges.get(zone, []):
                nxt = self.routes[e].dst
                if nxt in seen_zones:
                    continue
                progressed = True
                walk(nxt, edges + [e], seen_zones | {nxt})
            if not progressed:
                results.append(edges)

        walk(root.dst, [gate], {root.src, root.dst})
        return results

    def downstream_zones(self, gate: str) -> list[str]:
        """该入口之后可能经过的分区（含首入区），按拓扑出现顺序去重。"""
        root = self.routes[gate]
        ordered = [root.dst]
        seen = {root.dst, root.src}
        changed = True
        while changed:
            changed = False
            for z in list(ordered):
                for e in self.out_edges.get(z, []):
                    nxt = self.routes[e].dst
                    if nxt not in seen:
                        seen.add(nxt)
                        ordered.append(nxt)
                        changed = True
        return ordered
