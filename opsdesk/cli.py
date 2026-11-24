"""营运台命令行。

用法示例::

    python -m opsdesk report                 # 主经营日完整文字报告
    python -m opsdesk report --json          # 完整 JSON
    python -m opsdesk round --asof 3         # 只看第 3 轮
    python -m opsdesk backtest               # 历史独立点验回测
    python -m opsdesk report --out reports/desk.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backtest import run_backtest
from .desk import MAIN_DAY, OperationsDesk


def _json_default(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def print_round(desk: OperationsDesk, asof: int) -> None:
    rep = desk.round_report(asof)
    print(f"\n=== {desk.day} 第 {asof} 轮（known_at/posted <= {asof}）===")
    print("分区人数区间（窗口开始时点）：")
    zones = rep["stock"]
    header = "窗口  " + "".join(f"{z:>12}" for z in zones)
    print(header)
    for i, w in enumerate(range(9)):
        cells = []
        for z in zones:
            r = zones[z][i]
            hi = r["high"] if r["high"] is not None else "∞"
            cells.append(f"{str(r['low']) + '-' + str(hi) if not r['exact'] else str(r['low']):>12}")
        print(f"W{w:<3} " + "".join(cells))
    print("\n入口 W4 容量包络：")
    for g, row in rep["entry_envelopes_W4_operating"].items():
        print(
            f"  {g}->{row['first_zone']}: 保守可放(运营) "
            f"{row['safe_total_gate_flow_conservative']:>2}，中值 "
            f"{row['safe_total_gate_flow_central']}，硬上限口径 "
            f"{rep['entry_envelopes_W4_hard'][g]['safe_total_gate_flow_conservative']:>2}"
            f"  数据不足={row['data_insufficient_to_release']}"
        )
    lim = rep["limiting_A_W4"]
    if lim:
        top = lim[0]
        print(f"  限制 A@W4 判断的首要方向: {top['route']}（不确定宽度 {top['width']}；"
              f"{top['reasons'][0]}）")
    d = rep["diagnostics"]
    if d["true_missing_pulses"]:
        print(f"  全日真缺脉冲: {d['true_missing_pulses']}")
    if d["delayed_at_asof"]:
        print(f"  本轮尚未到的延迟读数: {d['delayed_at_asof']}")
    if d["constraint_conflicts"]:
        print(f"  ⚠ 资料冲突: {d['constraint_conflicts']}")
    ts = rep["terminal_snapshot"]["totals"]["overall"]
    print(f"  终端总承诺(保守)={ts['total_commitment_conservative']} "
          f"已售={ts['sold']} 未回收余量={ts['open_remainder']} 无回执敞口={ts['unacked_exposure']}")


def print_backtest(bt: dict) -> None:
    cc = bt["close_coverage"]
    ic = bt["intra_compatibility"]
    print("\n=== 历史独立收尾点验回测 ===")
    print(f"收尾上界覆盖: {cc['covered']}/{cc['checks']} "
          f"({cc['coverage_rate'] * 100:.1f}%)")
    print(f"日内点验相容: {ic['compatible']}/{ic['checks']} "
          f"({ic['compatibility_rate'] * 100:.1f}%)")
    print("\n推定存在容量压力、可能误放行的分区：")
    for p in bt["possible_false_release_zones"]:
        print(f"  {p['day']} {p['zone']} 峰值区间 {p['peak_bounds']} "
              f"收尾={p['independent_close']} -> {p['assessment']}")
    print("\n独立收尾确认的实际越限：")
    for a in bt["actual_cap_breach_zones"]:
        print(f"  {a['day']} {a['zone']} close={a['close']} "
              f"op={a['operating_cap']} hard={a['hard_cap']} — {a['verdict']}")
    wl = bt["pre_round_uncertainty_watchlist"]
    print(f"\nW4/asof3 放号前信息分级：可能误放行 {len(wl['possible_false_release'])} 项，"
          f"不能自证安全 {len(wl['cannot_certify_safety'])} 项，"
          f"缺开场锚点 {len(wl['missing_opening_anchor'])} 项")
    dirs = {}
    for w in wl["cannot_certify_safety"] + wl["possible_false_release"]:
        ld = w.get("limiting_direction") or {}
        dirs[ld.get("route", "?")] = dirs.get(ld.get("route", "?"), 0) + 1
    if dirs:
        print(f"  最常限制判断的方向: {dirs}")


def print_summary(desk: OperationsDesk) -> None:
    print("=" * 72)
    print(f"雪场营运台 · 经营日 {desk.day}")
    print("=" * 72)
    for asof in (3, 4):
        print_round(desk, asof)

    print("\n=== 封闭/单向与绕行对照 ===")
    ci = desk.closure_impact()
    for c in ci["closures"]:
        print(f"  {c['seal']} {c['route']} {c['closed_windows']}: {c['flows_during_closure']}")
    print(f"  替代 E2 封闭窗 W3-5 进入量={ci['alternative_route']['E2_during_AB_closure_W3_W5']}"
          f"（并未随 AB 归零）；单向窗 {ci['one_way']['one_way_toward_backward_windows']} 不可借方向")

    print("\n=== 团体处置（按刚性排序）===")
    for g in desk.groups.triage():
        alts = "/".join(g["alternate_gates"])
        print(f"  {g['ticket']} {g['people']:>2}人 W{g['window']} {g['assigned_gate']}"
              f"（备 {alts or '-'}）{g['stages']} -> {g['action_class']}")
    plan = desk.group_plan()
    print("  安置方案：")
    for p in plan["plans"]:
        alloc = "，".join(f"{a['gate']}/W{a['window']}×{a['people']}({a['change']})"
                         for a in p["allocations"]) or "无"
        print(f"    {p['ticket']}: {alloc}；未安置 {p['unplaced']} — {p['recommendation']}")

    print("\n=== 终端版本承诺（全部事件）===")
    snap = desk.terminals.snapshot(None)
    for v in snap["versions"]:
        print(f"  {v['key']:<7} {v['terminal']} {v['gate']}/W{v['window']} "
              f"quota={v['quota']:<2} {v['receive_status']:<4} "
              f"已售={v['sold_version_deducted']} 确认归还={v['returned_confirmed']} "
              f"未回收={v['open_remainder']} 保守敞口={v['unacked_exposure']}"
              f"{' [当前版]' if v['is_current'] else ' [已替换]'}")
    print("  管控动作：")
    for h in snap["holds"]:
        print(f"   - {h['terminal']}: {h['action']} | {h['reason']}")

    st = desk.stop_trace()
    print("\n=== 分流 vs 暂缓发售（W4 承诺/容量）===")
    opt = desk.response_options()
    for g, r in opt["by_gate"].items():
        print(f"  {g}: 团体刚性 {r['must_serve_group_people_W4']} + 终端承诺 "
              f"{r['sold_tickets_version_deducted']}售/{r['terminal_unrecovered_remainder']}未回收"
              f" = {r['total_claims_conservative']}；硬容量 {r['safe_hard_slots']}，"
              f"缺口 {r['hard_gap_claims_minus_slots']:+d}")
    print(f"  合计保守承诺 {opt['total_conservative_claims']} vs 硬槽位 "
          f"{opt['total_hard_slots']}，总缺口 {opt['aggregate_gap']:+d}")
    for d in opt["option_divert_entrance"]["diverted_allocations"]:
        print(f"  分流: {d['ticket']} {d['gate']}/W{d['window']}×{d['people']}")
    print(f"  暂缓: 可催回未回收 {opt['option_hold_sales']['recoverable_terminal_remainder']}，"
          f"中央已压减 {opt['option_hold_sales']['central_quota_cut_L1_14_to_5']}；"
          f"G3/G4 协商改场次；G1 在途必须继续接待")

    au = desk.audit()
    print("\n=== 轮次复盘（误差来源核对）===")
    for tr in au["transitions"]:
        if not tr["attributed_sources"] and not tr["stock_bound_changes"]:
            continue
        span = f"{tr['from_asof']}->{tr['to_asof']}"
        cats = []
        for s in tr["attributed_sources"]:
            tag = {"flow_inference": "流量推断", "late_count": "晚到点验",
                   "terminal_confirmation": "终端确认", "seal_or_detour": "封闭/绕行"}.get(
                s["category"], s["category"])
            cats.append(tag)
        nchange = len(tr["stock_bound_changes"])
        print(f"  轮 {span}: 来源={cats or '无新资料'}；人数区间变动 {nchange} 项")
        for s in tr["attributed_sources"]:
            if s["category"] in ("flow_inference", "terminal_confirmation", "seal_or_detour"):
                print(f"      · [{s['category']}] {s['detail'][:110]}")

    print("\n=== 晚到计数与停发追踪 ===")
    for t in st["triggers"]:
        print(f"  触发 {t['count_id']}: 点验 {t['count_range']} -> "
              f"N6 推断 {t['inferred_N6']}；运营上限 {t['operating_cap']}，硬上限 {t['hard_cap']}")
    for a in st["stop_new_issue"]:
        print(f"  停发: {a['action']}（{a['why']}）")
    print("  各终端等待回收/对账：")
    for r in st["terminal_recovery_trace"]:
        print(f"   - {r['tranche_version']} {r['terminal']} "
              f"待回收/确认 {r['awaiting_recovery_or_ack']}（{r['kind']}），"
              f"已售有效票 {r['sold_valid_tickets']}")

    print_backtest(desk.backtest())
    print("\n提示: python -m opsdesk report --json --out reports/desk.json 导出完整台账")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="雪场出入口营运台")
    parser.add_argument("command", nargs="?", default="report",
                        choices=["report", "round", "backtest"],
                        help="report=完整报告(默认), round=单轮, backtest=历史回测")
    parser.add_argument("--data", default=None, help="资料目录（默认 /workspace/data）")
    parser.add_argument("--day", default=MAIN_DAY)
    parser.add_argument("--asof", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", default=None, help="把 JSON 报告写入文件")
    args = parser.parse_args(argv)

    desk = OperationsDesk(args.data, args.day)

    if args.command == "backtest":
        payload = run_backtest(desk.ws)
        if args.json:
            text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
            print(text)
        else:
            print_backtest(payload)
        return 0

    if args.command == "round":
        if args.json:
            print(json.dumps(desk.round_report(args.asof), ensure_ascii=False,
                             indent=2, default=_json_default))
        else:
            print_round(desk, args.asof)
        return 0

    # report
    report = desk.full_report()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                  default=_json_default), encoding="utf-8")
        print(f"已写入 {out}（{out.stat().st_size} bytes）")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    elif not args.out:
        print_summary(desk)
    else:
        print_summary(desk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
