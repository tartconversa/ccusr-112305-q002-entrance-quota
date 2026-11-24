"""分区上限 + 通道消化能力约束下的放号测算。

安全放号量的直接平衡式（入口 g 把人送进首入区 z，窗口 w 进入，决定 N(z,w+1)）：

    N(z,w+1) = N(z,w) + q_g + 其他流入(w) - 流出(w)

保守口径取 N(z,w) 上界、其他流入上界、流出下界，要求 N(z,w+1) 上界不超过
所选容量上限，于是

    q_g <= cap - N(z,w)^high - Σ其他流入^high + Σ流出^low

封闭方向的流量域是 [0,0]，单向改造时反方向容量不会出现在 z 的流出/流入侧
被“借”用。后续路径经过已饱和分区时，再用该饱和区*可确认先行离开*的下界
收窄入口名额（只能共享先腾出的位置）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .bounds import Bounds, INF
from .inference import DayInference, FKEY
from .io import Workspace
from .network import OUTSIDE


@dataclass
class ZoneLimit:
    zone: str
    hard: int
    reserved: int

    @property
    def operating(self) -> int:
        return self.hard - self.reserved


class CapacityBook:
    def __init__(self, ws: Workspace):
        self.limits = {
            r["zone"]: ZoneLimit(r["zone"], int(r["max_people"]), int(r["reserved"]))
            for r in ws.occupancy_limits
        }

    def cap(self, zone: str, basis: str = "operating") -> int:
        zl = self.limits[zone]
        return zl.operating if basis == "operating" else zl.hard

    def zone_state(self, zone: str, high: int) -> str:
        zl = self.limits[zone]
        if high > zl.hard:
            return "超过硬上限"
        if high == zl.hard:
            return "触及硬上限"
        if high > zl.operating:
            return "超过运营保留口径"
        if high >= zl.operating - 2:
            return "贴近运营上限"
        return "正常"


def _central(b: Bounds):
    if b.hi >= INF:
        return None
    return (b.lo + b.hi) // 2


def gate_entry_envelope(
    inf: DayInference, gate: str, window: int, basis: str = "operating"
) -> dict:
    """计算某入口在某窗口可承受的*总穿越量*上下/中值与风险标志。"""
    book = CapacityBook(inf.workspace)
    route = inf.network.edge(gate)
    if route.src != OUTSIDE:
        raise ValueError(f"{gate} 不是外部入口连接")
    zone = route.dst
    cap = book.cap(zone, basis)
    zl = book.limits[zone]

    n0 = inf.stock(zone, window)
    other_in_hi = 0
    other_in_central = 0
    out_lo = 0
    out_central = 0
    components = []
    for e, sign in inf.network.balance_terms(zone):
        if e == gate:
            continue
        fb = inf.flow(e, window)
        cen = _central(fb)
        if sign == +1:
            other_in_hi += fb.hi if fb.hi < INF else route_cap_safe(inf, e)
            other_in_central += cen if cen is not None else inf.network.edge(e).capacity // 2
            components.append({"route": e, "role": "inflow", "high": fb.hi, "central": cen})
        else:
            out_lo += fb.lo
            out_central += cen if cen is not None else 0
            components.append({"route": e, "role": "outflow", "low": fb.lo, "central": cen})

    conservative_q = cap - n0.hi - other_in_hi + out_lo
    central_n = _central(n0)
    central_q = None if central_n is None else cap - central_n - other_in_central + out_central
    safe_total = max(0, min(route.capacity, conservative_q))
    central_total = None if central_q is None else max(0, min(route.capacity, central_q))

    baseline_hi = n0.hi + other_in_hi - out_lo  # q=0 时 N(w+1) 上界
    data_insufficient = n0.hi >= INF or baseline_hi > cap and conservative_q < 0
    baseline_over = baseline_hi > cap

    observed = inf.flow(gate, window)
    return {
        "gate": gate,
        "window": window,
        "first_zone": zone,
        "basis": basis,
        "capacity": cap,
        "hard_capacity": zl.hard,
        "operating_capacity": zl.operating,
        "reserved": zl.reserved,
        "start_stock_bounds": n0.to_dict(),
        "next_stock_baseline_high_q0": None if baseline_hi >= INF else baseline_hi,
        "other_inflow_high": other_in_hi,
        "outflow_low_credited": out_lo,
        "safe_total_gate_flow_conservative": safe_total,
        "safe_total_gate_flow_central": central_total,
        "gate_slot_capacity": route.capacity,
        "observed_flow_bounds": observed.to_dict(),
        "observed_flow": observed.exact_value,
        "baseline_already_over_cap": baseline_over,
        "data_insufficient_to_release": bool(data_insufficient),
        "deficit_to_cap": max(0, baseline_hi - cap) if baseline_hi < INF else None,
        "components": components,
        "zone_state_at_start_hi": book.zone_state(zone, n0.hi if n0.hi < INF else zl.hard + 1),
        "note": (
            "当前信息下现状上界已超过容量，不能支持继续发票，先等缺测方向回执"
            if data_insufficient
            else "保守口径可放总穿越量；中值口径用于解释已签发额度"
        ),
    }


def route_cap_safe(inf: DayInference, route: str) -> int:
    return inf.network.edge(route).capacity


def path_bottlenecks(inf: DayInference, gate: str, window: int, basis: str = "operating") -> dict:
    """沿入口下游路径逐区/逐通道给出瓶颈与“可先行离开名额”。"""
    book = CapacityBook(inf.workspace)
    zones = inf.network.downstream_zones(gate)
    zone_rows = []
    limiting = None
    for z in zones:
        cap = book.cap(z, basis)
        for w in range(window + 1, min(MAX_W := inf.max_window, window + 3) + 1):
            b = inf.stock(z, w)
            saturated = b.lo >= cap or (b.hi >= cap and b.width <= 2)
            row = {
                "zone": z, "window": w, "stock_bounds": b.to_dict(),
                "capacity": cap, "upper_breached": (b.hi if b.hi < INF else INF) > cap,
                "looks_saturated": saturated,
                "state": book.zone_state(z, min(b.hi, cap + 1)),
            }
            zone_rows.append(row)
            if row["looks_saturated"] and limiting is None:
                limiting = row
    # 饱和区在进入当窗可确认的最低流出（允许先行离开的名额）
    departure_share = None
    if limiting is not None:
        z = limiting["zone"]
        w = max(window, 0)
        total_out_lo = 0
        edges = []
        for e, sign in inf.network.balance_terms(z):
            if sign == -1:
                fb = inf.flow(e, w)
                total_out_lo += fb.lo
                r = inf.network.edge(e)
                edges.append({"route": e, "low": fb.lo, "capacity": r.capacity,
                              "closed": inf.states.is_closed(e, w)})
        departure_share = {"zone": z, "window": w,
                           "guaranteed_departure_slots": total_out_lo, "edges": edges}
    return {"gate": gate, "basis": basis, "zone_checks": zone_rows,
            "limiting_saturated_zone": limiting, "departure_share": departure_share}


def compare_gates(inf: DayInference, window: int, gates: list[str] | None = None) -> list[dict]:
    gates = gates or inf.network.entrance_gates()
    rows = []
    for g in gates:
        op = gate_entry_envelope(inf, g, window, "operating")
        hard = gate_entry_envelope(inf, g, window, "hard")
        rows.append({
            "gate": g, "zone": op["first_zone"],
            "safe_operating": op["safe_total_gate_flow_conservative"],
            "central_operating": op["safe_total_gate_flow_central"],
            "safe_hard": hard["safe_total_gate_flow_conservative"],
            "data_insufficient": op["data_insufficient_to_release"],
            "start_stock": op["start_stock_bounds"],
            "state": op["zone_state_at_start_hi"],
        })
    return rows
