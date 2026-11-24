"""分区人数与口间流量的区间推断。

变量设计（关键）：

- ``F[e,w]``：连接 e 在窗口 w 的穿越人数（w>=1 可由闸机累计差校验）；
- ``P[e,w]``：``F[e,1]+...+F[e,w]`` 的累计穿越量，P[e,0]=0。闸机累计读数
  C 满足 P[e,t]=C(t)-C(0)，因此可见读数能把 P 直接钉成单点，避免逐窗区间
  求和丢掉“跨窗总量守恒”的相关性；
- ``G[z]``：窗口 0 的区域净流入（计数器当日从新底数起算，F[e,0] 没有前值
  可差），由 F[e,0] 的带符号和定义，并被 w2 等区内点验区间收紧；
- ``N[z,w]``：窗口 w 开始时区内人数，满足
  ``N[z,w] = N[z,0] + G[z] + Σ sign·P[e,w-1]``。

所有关系组成线性区间约束，用 :class:`opsdesk.constraints.PropagationEngine`
收紧到不动点。输出的每个上界都不会低估仍可能成立的真实人数。

独立收尾点验只用于回测核对，*不*进入推断（data/README 口径）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bounds import Bounds, INF
from .closures import RouteStates
from .constraints import Constraint, PropagationEngine
from .network import ZONES, Network
from .sensors import GateLedger
from .timegrid import MAX_WINDOW
from .io import Workspace

NKEY = lambda z, w: ("N", z, w)
FKEY = lambda e, w: ("F", e, w)
PKEY = lambda e, w: ("P", e, w)
GKEY = lambda z: ("G", z)


@dataclass
class Reconciliation:
    route: str
    window: int
    gate_flow: dict | None
    manual: dict | None
    status: str
    detail: str


@dataclass
class DayInference:
    workspace: Workspace
    day: str
    asof: int | None
    network: Network
    states: RouteStates
    ledger: GateLedger
    engine: PropagationEngine
    zones: tuple[str, ...] = ZONES
    max_window: int = MAX_WINDOW
    reconciliations: list[Reconciliation] = field(default_factory=list)

    # ---------- 结果读取 ----------
    def stock(self, zone: str, window: int) -> Bounds:
        return self.engine.bounds[NKEY(zone, window)]

    def flow(self, route: str, window: int) -> Bounds:
        return self.engine.bounds[FKEY(route, window)]

    def cumulative(self, route: str, window: int) -> Bounds:
        return self.engine.bounds[PKEY(route, window)]

    def opening_net(self, zone: str) -> Bounds:
        return self.engine.bounds[GKEY(zone)]

    def stock_table(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for z in self.zones:
            rows = []
            for w in range(self.max_window + 1):
                b = self.stock(z, w)
                rows.append(
                    {
                        "window": w,
                        "clock": _clock(w),
                        "low": b.lo,
                        "high": _hi(b),
                        "central": central_value(b),
                        "exact": b.pinned,
                    }
                )
            out[z] = rows
        return out

    def flow_table(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for e in self.network.routes:
            rows = []
            for w in range(self.max_window + 1):
                b = self.flow(e, w)
                rows.append(
                    {
                        "window": w,
                        "low": max(0, b.lo),
                        "high": _hi(b),
                        "central": central_value(b),
                        "exact": b.pinned,
                    }
                )
            out[e] = rows
        return out

    def limiting_directions(self, zone: str, window: int) -> list[dict]:
        """找出对 N(zone, window) 区间宽度贡献最大的连接方向。

        N(z,W) = N(z,0) + G[z] + Σ sign·P[e,W-1]，因此直接考察 G 与各
        P 变量的区间宽度，并标注不确定来源（缺脉冲 / asof 延迟 / 封闭）。
        """
        target = self.stock(zone, window)
        total_width = max(1, target.width)
        items = []
        g = self.opening_net(zone)
        if g.width > 0:
            items.append(
                {
                    "route": "(开场窗口0净流)",
                    "variable": list(GKEY(zone)),
                    "width": g.width,
                    "bounds": g.to_dict(),
                    "share_of_width": round(g.width / total_width, 3),
                    "reasons": ["窗口0无累计前值，由区内点验区间约束"],
                }
            )
        if window >= 1:
            for route, sign in self.network.balance_terms(zone):
                p = self.cumulative(route, window - 1)
                width = p.width
                if width <= 0:
                    continue
                items.append(
                    {
                        "route": route,
                        "signed": sign,
                        "variable": list(PKEY(route, window - 1)),
                        "width": width,
                        "bounds": p.to_dict(),
                        "share_of_width": round(width / total_width, 3),
                        "reasons": self._uncertainty_reasons(route, window - 1),
                    }
                )
        items.sort(key=lambda x: x["width"], reverse=True)
        return items

    def _uncertainty_reasons(self, route: str, upto_window: int) -> list[str]:
        reasons: list[str] = []
        true_missing = set(self.ledger.true_missing_pulses(route))
        if any(w <= upto_window for w in true_missing):
            reasons.append("缺脉冲形成跨窗总量，不能按零处理")
        if self.asof is not None:
            view = self.ledger.view(self.asof)
            pending_t = sorted(
                p["observe_time"]
                for p in view.pending
                if p["sensor"] == route and p["observe_time"] <= upto_window
            )
            if pending_t:
                reasons.append(f"读数延迟回传 observe={pending_t}（当前轮不可见）")
        if self.states.closed_windows(route):
            reasons.append("方向临时封闭，流量按 0")
        if not reasons:
            reasons.append("点验区间本身有宽度")
        return reasons

    def diagnostics(self) -> dict:
        view = self.ledger.view(self.asof)
        missing = {
            e: self.ledger.true_missing_pulses(e)
            for e in self.network.routes
            if self.ledger.true_missing_pulses(e)
        }
        delayed: dict[str, list[int]] = {}
        if self.asof is not None:
            for p in view.pending:
                # 只把“被覆盖窗口 observe_time 已到、回执却晚于 asof”算作延迟方向；
                # observe_time > asof 的记录属于尚未发生窗口的正常预报，不算缺测；
                # 同一 observe_time 已有可见读数时，待处理的只是重复补传，也不算缺方向。
                if p["observe_time"] > self.asof:
                    continue
                if p["observe_time"] in view.current.get(p["sensor"], {}):
                    continue
                delayed.setdefault(p["sensor"], []).append(p["observe_time"])
            for k in delayed:
                delayed[k] = sorted(set(delayed[k]))
        return {
            "true_missing_pulses": missing,
            "delayed_at_asof": delayed,
            "backfills": view.backfills,
            "constraint_conflicts": self.engine.conflicts,
            "propagation_iterations": self.engine.iterations,
        }

    def cross_zone_match(self) -> list[dict]:
        """相连两区必须对得上：同一条有向连接同时是出发区流出与到达区流入。

        推断引擎两边引用同一个 F/P 变量，因此天然逐窗相等；这里把对账结果
        显式列出，供营运台核对“相连两区的流量对得上”。
        """
        out = []
        for route, r in self.network.routes.items():
            for w in range(1, self.max_window + 1):
                b = self.flow(route, w)
                out.append(
                    {
                        "route": route,
                        "from": r.src,
                        "to": r.dst,
                        "window": w,
                        "origin_out_variable": list(FKEY(route, w)),
                        "destination_in_variable": list(FKEY(route, w)),
                        "matched": True,
                        "flow": b.to_dict(),
                    }
                )
        return out


def _hi(b: Bounds):
    return None if b.hi >= INF else b.hi


def _clock(w: int) -> str:
    from .timegrid import window_label

    return window_label(w)


def central_value(b: Bounds) -> int | None:
    if b.hi >= INF:
        return None
    return (b.lo + b.hi) // 2


def build_engine(
    ws: Workspace,
    day: str,
    asof: int | None = None,
    *,
    use_manual: bool = True,
    use_intra_checks: bool = True,
    pins: dict | None = None,
) -> tuple[PropagationEngine, Network, RouteStates, GateLedger]:
    """构造并传播约束引擎。pins 用于容量试算（把某变量钉成候选值/区间）。"""
    ws.require_day(day)
    network = Network(ws.route_map)
    states = RouteStates(ws.closures, day)
    ledger = GateLedger(ws.gates, day)
    view = ledger.view(asof)
    eng = PropagationEngine()

    # ---- 变量初始域 ----
    for z in ZONES:
        eng.var(GKEY(z), Bounds(-INF, INF))
        for w in range(MAX_WINDOW + 1):
            eng.var(NKEY(z, w), Bounds(0, INF))
    for route, r in network.routes.items():
        eng.var(PKEY(route, 0), Bounds.exact(0))
        for w in range(MAX_WINDOW + 1):
            if w >= 1:
                eng.var(PKEY(route, w), Bounds(0, INF))
            base = Bounds(0, r.capacity)
            if states.is_closed(route, w):
                base = Bounds.exact(0)
            eng.var(FKEY(route, w), base)

    # ---- F 与 P 的定义：P[w] = P[w-1] + F[w]，且 0<=F<=capacity 已在初始域 ----
    for route in network.routes:
        for w in range(1, MAX_WINDOW + 1):
            eng.add(
                Constraint(
                    {PKEY(route, w): 1, PKEY(route, w - 1): -1, FKEY(route, w): -1},
                    0, 0, f"cumulative {route} W{w}",
                )
            )
        # G[z] = Σ sign F[e,0]
    for z in ZONES:
        terms = {GKEY(z): 1}
        for route, sign in network.balance_terms(z):
            terms[FKEY(route, 0)] = -sign
        eng.add(Constraint(terms, 0, 0, f"opening net {z}"))

    # ---- 累计平衡式：N[z,w] - G[z] - Σ sign P[e,w-1] = N[z,0] ----
    # N[z,0] 作为变量保留（由 w0 点验钉住，缺则非负自由），写成等式：
    # N[z,w] - N[z,0] - G[z] - Σ sign P = 0
    for z in ZONES:
        for w in range(1, MAX_WINDOW + 1):
            # N[z,w] - N[z,0] - G[z] - Σ sign P[e,w-1] = 0
            terms = {NKEY(z, w): 1, NKEY(z, 0): -1, GKEY(z): -1}
            for route, sign in network.balance_terms(z):
                terms[PKEY(route, w - 1)] = -sign
            eng.add(Constraint(terms, 0, 0, f"stock {z} W{w}"))

    # ---- 闸机累计读数 -> P 差分等式 ----
    for route in network.routes:
        series = view.current.get(route, {})
        by_epoch: dict[str, list[tuple[int, int]]] = {}
        for t, reading in series.items():
            by_epoch.setdefault(reading.epoch, []).append((t, reading.cumulative))
        for epoch, pts in by_epoch.items():
            pts.sort()
            for (p, cp), (q, cq) in zip(pts, pts[1:]):
                diff = cq - cp
                if diff < 0:
                    eng.conflicts.append(
                        {"sensor": route, "epoch": epoch, "between": [p, q],
                         "detail": f"累计读数倒退 {cp}->{cq}，可能计数器重置，禁止跨段求差"}
                    )
                    continue
                # P[q] - P[p] = Cq - Cp（同一底数，绝对底数消去）
                eng.add(
                    Constraint(
                        {PKEY(route, q): 1, PKEY(route, p): -1},
                        diff, diff, f"gate {route} C{q}-C{p}={diff}",
                    )
                )

    # ---- 人工点验 ----
    for c in ws.counts:
        if c["day"] != day:
            continue
        if asof is not None and int(c["known_at"]) > asof:
            continue
        kind = c["kind"]
        target = c["gate_or_zone"]
        w = int(c["window"])
        rng = Bounds(int(c["low"]), int(c["high"]))
        if kind == "过口" and use_manual and target in network.routes:
            eng.tighten(FKEY(target, w), rng, f"manual-section {c['id']}")
        elif kind == "区内" and use_intra_checks and c["source"] == "日内点验" and target in ZONES:
            eng.tighten(NKEY(target, w), rng.clip_nonnegative(), f"zone-check {c['id']}")
        # 独立收尾点验刻意不进入推断。

    if pins:
        for key, bounds in pins.items():
            if not isinstance(bounds, Bounds):
                bounds = Bounds(int(bounds), int(bounds))
            eng.tighten(key, bounds, "what-if pin")

    eng.propagate()
    return eng, network, states, ledger


def infer_day(
    ws: Workspace,
    day: str,
    asof: int | None = None,
    *,
    use_manual: bool = True,
    use_intra_checks: bool = True,
) -> DayInference:
    eng, network, states, ledger = build_engine(
        ws, day, asof, use_manual=use_manual, use_intra_checks=use_intra_checks
    )
    inf = DayInference(
        workspace=ws, day=day, asof=asof, network=network,
        states=states, ledger=ledger, engine=eng,
    )
    inf.reconciliations = reconcile_manual(inf)
    return inf


def reconcile_manual(inf: DayInference) -> list[Reconciliation]:
    """把人工截面区间、闸机推断流量放在一起对账（只列 asof 可见资料）。"""
    out: list[Reconciliation] = []
    for c in inf.workspace.counts:
        if c["day"] != inf.day or c["kind"] != "过口":
            continue
        if inf.asof is not None and int(c["known_at"]) > inf.asof:
            continue
        route, w = c["gate_or_zone"], int(c["window"])
        if route not in inf.network.routes:
            continue
        mb = Bounds(int(c["low"]), int(c["high"]))
        gb = inf.flow(route, w)
        overlap = gb.intersection(mb)
        if overlap is None:
            status = "冲突"
            detail = f"推断 {gb} 与人工区间 {mb} 不相交"
        elif gb.pinned and mb.contains(gb.lo):
            status = "一致"
            detail = f"闸机增量 {gb.lo} 落在人工区间 {mb.lo}-{mb.hi}"
        else:
            status = "区间相容"
            detail = f"推断 {gb} 与人工区间 {mb} 重叠于 {overlap}"
        out.append(
            Reconciliation(
                route=route, window=w,
                gate_flow=gb.to_dict(), manual=mb.to_dict(),
                status=status, detail=detail,
            )
        )
    out.sort(key=lambda r: (r.window, r.route))
    return out
