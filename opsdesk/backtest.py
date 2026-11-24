"""用独立收尾点验重跑历史时段，检验推定上界覆盖与误放行风险。

两类覆盖：

1. *收尾覆盖*：只用闸机+日内点验+人工截面推断 N(z,8)，与独立收尾点验逐区
   比较，统计推定区间是否覆盖真实收尾人数；
2. *日内覆盖*：纯闸机/约束推断对各次日内点验区间的覆盖。

误放行观察：在每个历史日的放号时点（默认目标窗口 4、asof=3，即 BA 延迟
脉冲尚未到达）计算入口容量包络，若某区保守上界已触/超容量、或当前信息
不足以证明放号安全，则记为“可能误放行”的分区。
"""

from __future__ import annotations

from .bounds import Bounds
from .capacity import CapacityBook, gate_entry_envelope
from .inference import infer_day
from .io import Workspace


def evaluate_close_coverage(ws: Workspace) -> dict:
    rows = []
    covered = total = 0
    for c in ws.counts:
        if c["source"] != "独立收尾点验":
            continue
        day, z, value = c["day"], c["gate_or_zone"], int(c["low"])
        inf = infer_day(ws, day, None)
        b = inf.stock(z, 8)
        hit = b.contains(value)
        total += 1
        covered += int(hit)
        central = (b.lo + b.hi) // 2 if b.hi < 10**8 else None
        rows.append({
            "day": day, "zone": z, "independent_close": value,
            "inferred_low": b.lo, "inferred_high": None if b.hi >= 10**8 else b.hi,
            "central": central, "covered": hit,
            "central_abs_error": None if central is None else abs(central - value),
        })
    return {
        "checks": total, "covered": covered,
        "coverage_rate": round(covered / total, 4) if total else None,
        "rows": sorted(rows, key=lambda r: (r["day"], r["zone"])),
    }


def evaluate_intra_coverage(ws: Workspace) -> dict:
    """推断区间与日内点验区间是否相交（资料口径下两者都应能同时成立）。"""
    rows = []
    hit = total = 0
    for c in ws.counts:
        if c["source"] != "日内点验" or int(c["window"]) == 0:
            continue
        day, z, w = c["day"], c["gate_or_zone"], int(c["window"])
        inf = infer_day(ws, day, None)
        b = inf.stock(z, w)
        overlap = b.intersection(Bounds(
            int(c["low"]), max(0, int(c["high"]))))
        ok = overlap is not None
        total += 1
        hit += int(ok)
        rows.append({"day": day, "zone": z, "window": w,
                     "check": [c["low"], c["high"]],
                     "inferred": b.to_dict(), "compatible": ok})
    return {"checks": total, "compatible": hit,
            "compatibility_rate": round(hit / total, 4) if total else None,
            "rows": rows}


def false_release_watchlist(
    ws: Workspace, target_window: int = 4, asof: int = 3
) -> dict:
    """逐历史日重跑放号前判断，分级列出风险。

    返回：
    - ``possible_false_release``：有开场锚点、但保守上界已触/超容量的分区，
      继续按中值放号就可能误放行；
    - ``cannot_certify``：缺测/延迟方向使当前轮无法证明放号安全（流程信号，
      不一定真的超员），并指出限制判断的方向；
    - ``unanchored``：该分区连开场点验都缺，开场净流无界，应先补点验。
    """
    book = CapacityBook(ws)
    possible, certify, unanchored = [], [], []
    w0 = {
        (c["day"], c["gate_or_zone"])
        for c in ws.counts
        if c["source"] == "日内点验" and c["window"] == 0
    }
    for day in ws.days:
        inf = infer_day(ws, day, asof)
        for gate in inf.network.entrance_gates():
            op = gate_entry_envelope(inf, gate, target_window, "operating")
            hard = gate_entry_envelope(inf, gate, target_window, "hard")
            z = op["first_zone"]
            zl = book.limits[z]
            n = op["start_stock_bounds"]
            lim = inf.limiting_directions(z, target_window)
            item = {
                "day": day, "target_window": target_window, "asof": asof,
                "gate": gate, "zone": z,
                "stock_bounds_at_window_start": n,
                "operating_cap": zl.operating, "hard_cap": zl.hard,
                "safe_operating_quota": op["safe_total_gate_flow_conservative"],
                "safe_hard_quota": hard["safe_total_gate_flow_conservative"],
                "limiting_direction": _lim_brief(lim[0] if lim else None),
                "actual_close": _close_value(ws, day, z),
            }
            if (day, z) not in w0:
                item["reason"] = "缺 w0 开场点验，开场净流无界"
                unanchored.append(item)
                continue
            if n["high"] is not None and n["high"] >= zl.operating:
                item["reason"] = (
                    f"保守上界 {n['high']} 已达/超运营上限 {zl.operating}"
                    + (f"，距硬上限仅 {max(0, zl.hard - n['high'])}" if n["high"] < zl.hard else "，已触硬上限")
                )
                possible.append(item)
            elif op["data_insufficient_to_release"]:
                item["reason"] = "延迟/缺测方向的当窗流入未知，无法证明放号安全"
                certify.append(item)
    return {
        "possible_false_release": possible,
        "cannot_certify_safety": certify,
        "missing_opening_anchor": unanchored,
    }


def _lim_brief(lim: dict | None) -> dict | None:
    if not lim:
        return None
    return {"route": lim.get("route"), "width": lim.get("width"),
            "reason": (lim.get("reasons") or [None])[0]}


def historical_decision_reruns(ws: Workspace, target_window: int = 4) -> list[dict]:
    """对拥有完整三区收尾点验的日期，重跑 asof=3/4 两轮放号判断并对回收尾。"""
    full_days = sorted({
        c["day"] for c in ws.counts if c["source"] == "独立收尾点验"
    })
    full_days = [
        d for d in full_days
        if len({c["gate_or_zone"] for c in ws.counts
                if c["day"] == d and c["source"] == "独立收尾点验"}) == 3
    ]
    book = CapacityBook(ws)
    rows = []
    for day in full_days:
        rounds = {}
        for asof in (3, 4):
            inf = infer_day(ws, day, asof)
            rounds[f"asof{asof}"] = {
                g: gate_entry_envelope(inf, g, target_window, "operating")
                for g in inf.network.entrance_gates()
            }
        final = infer_day(ws, day, None)
        rows.append({
            "day": day,
            "rounds": {
                k: {g: {"stock": v["start_stock_bounds"],
                        "central_safe": v["safe_total_gate_flow_central"],
                        "conservative_safe": v["safe_total_gate_flow_conservative"],
                        "insufficient": v["data_insufficient_to_release"]}
                     for g, v in r.items()}
                for k, r in rounds.items()
            },
            "close_N8": {z: final.stock(z, 8).to_dict() for z in "ABC"},
            "independent_close": {
                c["gate_or_zone"]: c["low"] for c in ws.counts
                if c["day"] == day and c["source"] == "独立收尾点验"
            },
            "caps": {z: {"operating": book.limits[z].operating, "hard": book.limits[z].hard}
                     for z in "ABC"},
        })
    return rows


def _close_value(ws: Workspace, day: str, zone: str):
    for c in ws.counts:
        if c["source"] == "独立收尾点验" and c["day"] == day and c["gate_or_zone"] == zone:
            return int(c["low"])
    return None


def peak_pressure_report(ws: Workspace) -> list[dict]:
    """各历史日重建出的分区峰值与容量压力，直接回答“可能误放行的分区”。"""
    book = CapacityBook(ws)
    rows = []
    for day in ws.days:
        inf = infer_day(ws, day, None)
        for z in "ABC":
            series = [inf.stock(z, w) for w in range(9)]
            peak_w = max(range(9), key=lambda w: series[w].lo)
            b = series[peak_w]
            zl = book.limits[z]
            close = _close_value(ws, day, z)
            risk = None
            if b.hi >= zl.hard or (close is not None and close >= zl.hard):
                risk = "触及硬上限：继续放号即误放行"
            elif b.lo > zl.operating or (close is not None and close > zl.operating):
                risk = "整体越过运营上限：按运营口径可能误放行"
            elif b.hi > zl.operating:
                risk = "上界越过运营上限：存在误放行可能，需保守放号"
            if risk:
                rows.append({
                    "day": day, "zone": z, "peak_window": peak_w,
                    "peak_bounds": b.to_dict(),
                    "operating_cap": zl.operating, "hard_cap": zl.hard,
                    "independent_close": close, "assessment": risk,
                })
    return rows


def run_backtest(ws: Workspace) -> dict:
    close_cov = evaluate_close_coverage(ws)
    intra_cov = evaluate_intra_coverage(ws)
    watch = false_release_watchlist(ws)
    reruns = historical_decision_reruns(ws)
    peak = peak_pressure_report(ws)
    book = CapacityBook(ws)
    # 用独立收尾值判定“实际误放行”：收尾达到/超过硬上限或越过运营上限
    actual_flags = []
    for row in close_cov["rows"]:
        zl = book.limits[row["zone"]]
        v = row["independent_close"]
        if v >= zl.hard or v > zl.operating:
            actual_flags.append({
                "day": row["day"], "zone": row["zone"], "close": v,
                "hard_cap": zl.hard, "operating_cap": zl.operating,
                "verdict": "收尾达到硬上限，继续放号即误放行" if v >= zl.hard
                else "收尾越过运营保留上限，按运营口径属于误放行",
            })
    return {
        "close_coverage": {k: v for k, v in close_cov.items() if k != "rows"},
        "close_coverage_rows": close_cov["rows"],
        "intra_compatibility": {k: v for k, v in intra_cov.items() if k != "rows"},
        "peak_pressure": peak,
        "possible_false_release_zones": [
            {"day": p["day"], "zone": p["zone"], "assessment": p["assessment"],
             "peak_bounds": p["peak_bounds"], "independent_close": p["independent_close"]}
            for p in peak
        ],
        "pre_round_uncertainty_watchlist": watch,
        "historical_decision_reruns": reruns,
        "actual_cap_breach_zones": actual_flags,
    }
