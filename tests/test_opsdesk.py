"""opsdesk 营运台核心口径与场景测试。

运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opsdesk import load_workspace, infer_day  # noqa: E402
from opsdesk.backtest import run_backtest  # noqa: E402
from opsdesk.capacity import gate_entry_envelope  # noqa: E402
from opsdesk.desk import OperationsDesk  # noqa: E402
from opsdesk.terminals import TerminalLedger  # noqa: E402

DATA = ROOT / "data"
DAY = "2026-02-17"


class OpsDeskCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = load_workspace(DATA)
        cls.desk = OperationsDesk(DATA, DAY)

    # ---------- 闸机流量口径 ----------
    def test_flow_alignment_with_all_manual_counts(self):
        """f(w)=cum(w)-cum(w-1) 与全部 120 条人工截面区间相容。"""
        statuses = {}
        for day in self.ws.days:
            inf = infer_day(self.ws, day)
            for r in inf.reconciliations:
                self.assertNotEqual(r.status, "冲突", r.detail)
                statuses[r.status] = statuses.get(r.status, 0) + 1
        self.assertEqual(statuses.get("一致"), 110)
        self.assertEqual(statuses.get("区间相容"), 10)

    def test_day17_exact_gate_increments(self):
        inf = infer_day(self.ws, DAY)
        # 抽查由累计读数直接钉死的窗口增量
        self.assertEqual(inf.flow("E1", 4).exact_value, 4)
        self.assertEqual(inf.flow("E1", 5).exact_value, 5)
        self.assertEqual(inf.flow("BA", 4).exact_value, 0)
        self.assertEqual(inf.flow("CO", 4).exact_value, 7)
        # AB 封闭窗 3,4,5 全部为 0，6 恢复为 6
        for w in (3, 4, 5):
            self.assertEqual(inf.flow("AB", w).exact_value, 0)
        self.assertEqual(inf.flow("AB", 6).exact_value, 6)

    # ---------- 缺脉冲 / 补传 ----------
    def test_missing_pulse_is_span_not_zero(self):
        led = infer_day(self.ws, DAY).ledger
        self.assertEqual(led.true_missing_pulses("AB"), [2])
        self.assertEqual(led.true_missing_pulses("CO"), [2])
        inf = infer_day(self.ws, DAY)
        # AB: C3-C1=6 = f2+f3，且封闭令 f3=0 -> f2=6（不能把 W2 设 0）
        self.assertEqual(inf.flow("AB", 2).exact_value, 6)
        # CO: f2+f3=19 的拆分无唯一解，两窗都保留宽度且绝不含“只能为 0”
        f2, f3 = inf.flow("CO", 2), inf.flow("CO", 3)
        self.assertEqual((f2.lo, f2.hi), (0, 19))
        self.assertEqual((f3.lo, f3.hi), (0, 19))
        # 跨窗总量等式仍在：用累计 P3-P1=19 钉住
        self.assertEqual(inf.cumulative("CO", 3).lo - inf.cumulative("CO", 1).lo, 19)

    def test_backfill_does_not_change_occupancy(self):
        dup = infer_day(self.ws, DAY).ledger.duplicate_keys("AB")
        ab4 = [d for d in dup if d["observe_time"] == 4]
        self.assertEqual(len(ab4), 1)
        self.assertFalse(ab4[0]["revises_value"])  # 补传累计值一致
        full = infer_day(self.ws, DAY)
        self.assertEqual(full.stock("A", 8).to_dict(),
                         {"low": 47, "high": 49, "exact": False})
        diag = full.diagnostics()
        self.assertTrue(any(b["sensor"] == "AB" for b in diag["backfills"]))

    # ---------- as-of 延迟方向 ----------
    def test_delayed_ba_is_limiting_direction(self):
        pre = infer_day(self.ws, DAY, 3)
        a4 = pre.stock("A", 4)
        self.assertGreaterEqual(a4.hi, 48)       # 信息不足，上界可能已超硬上限
        lim = pre.limiting_directions("A", 4)
        self.assertEqual(lim[0]["route"], "BA")  # B→A 延迟脉冲最限制判断
        self.assertEqual(lim[0]["width"], 16)
        diag = pre.diagnostics()
        self.assertIn(3, diag["delayed_at_asof"]["BA"])

        updated = infer_day(self.ws, DAY, 4)
        self.assertEqual((updated.stock("A", 4).lo, updated.stock("A", 4).hi), (35, 37))

    # ---------- 推断覆盖 ----------
    def test_close_and_intra_coverage(self):
        bt = run_backtest(self.ws)
        self.assertEqual(bt["close_coverage"]["covered"], 36)
        self.assertEqual(bt["close_coverage"]["coverage_rate"], 1.0)
        self.assertEqual(bt["intra_compatibility"]["compatible"], 72)
        self.assertEqual(bt["intra_compatibility"]["compatibility_rate"], 1.0)

    def test_day17_reconstructed_matches_close(self):
        inf = infer_day(self.ws, DAY)
        self.assertTrue(inf.stock("A", 8).contains(48))
        self.assertTrue(inf.stock("B", 8).contains(1))
        self.assertTrue(inf.stock("C", 8).contains(17))
        self.assertEqual((inf.stock("A", 8).lo + inf.stock("A", 8).hi) // 2, 48)

    # ---------- 记账规则 / 相连两区对得上 ----------
    def test_cross_zone_uses_single_edge_truth(self):
        inf = infer_day(self.ws, DAY)
        checks = inf.cross_zone_match()
        self.assertTrue(all(c["matched"] for c in checks))
        ab = [c for c in checks if c["route"] == "AB" and c["window"] == 6][0]
        self.assertEqual(ab["flow"]["low"], 6)
        # 区域平衡恒等式在钉死窗口上成立：A W4 中值 36
        self.assertEqual((inf.stock("A", 4).lo + inf.stock("A", 4).hi) // 2, 36)

    def test_one_way_closure_does_not_allow_borrowing(self):
        inf = infer_day(self.ws, DAY)
        ow = inf.states.one_way_pair("AB", "BA")
        self.assertEqual(ow["forward_closed"], [3, 4, 5])
        self.assertEqual(ow["one_way_toward_backward_windows"], [3, 4, 5])
        # AB 封闭时 A 不能借 BA 方向的容量疏散：W4 流出下界为 0
        env = gate_entry_envelope(inf, "E1", 4, "operating")
        self.assertEqual(env["outflow_low_credited"], 0)

    # ---------- 容量与 L1v2=5 ----------
    def test_l1_v2_quota_explained_by_operating_headroom(self):
        inf = infer_day(self.ws, DAY, 4)
        op = gate_entry_envelope(inf, "E1", 4, "operating")
        self.assertEqual(op["safe_total_gate_flow_central"], 5)
        self.assertEqual(op["safe_total_gate_flow_conservative"], 4)
        hard = gate_entry_envelope(inf, "E1", 4, "hard")
        self.assertEqual(hard["safe_total_gate_flow_conservative"], 11)
        self.assertEqual(op["capacity"], 41)       # 48 - reserved 7
        # E2 受入口通道容量 18 约束，且 C 区容量充足
        e2 = gate_entry_envelope(inf, "E2", 4, "operating")
        self.assertEqual(e2["safe_total_gate_flow_conservative"], 18)

    def test_pre_pulse_round_cannot_certify_e1(self):
        pre = infer_day(self.ws, DAY, 3)
        op = gate_entry_envelope(pre, "E1", 4, "operating")
        self.assertTrue(op["data_insufficient_to_release"])
        self.assertEqual(op["safe_total_gate_flow_conservative"], 0)

    # ---------- 终端版本台账 ----------
    def test_terminal_version_ledger_rounds(self):
        tl = TerminalLedger(self.ws, DAY)
        s4 = tl.snapshot(4)["totals"]["by_gate_window"]["E1/W4"]
        self.assertEqual(s4["sold"], 7)
        self.assertEqual(s4["open_remainder"], 7)
        self.assertEqual(s4["terminal_commitment"], 14)
        # asof6：J5 归还 5 -> 未回收 2
        s6 = tl.snapshot(6)["totals"]["by_gate_window"]["E1/W4"]
        self.assertEqual(s6["open_remainder"], 2)
        # asof7：回执订正把归还改为 4 -> 未回收回到 3（订正使 1 个名额重新挂账）
        s7e1 = tl.snapshot(7)["totals"]["by_gate_window"]["E1/W4"]
        s7e2 = tl.snapshot(7)["totals"]["by_gate_window"]["E2/W4"]
        self.assertEqual(s7e1["open_remainder"], 3)
        self.assertEqual(s7e1["terminal_commitment"], 10)
        self.assertEqual(s7e2["sold"], 6)
        self.assertEqual(s7e2["open_remainder"], 4)
        # L3 无回执：整版 13 保守计为敞口
        l3 = [v for v in tl.snapshot(None)["versions"] if v["key"] == "L3/1"][0]
        self.assertEqual(l3["receive_status"], "无回执")
        self.assertEqual(l3["unacked_exposure"], 13)
        # 出票扣到具体版本
        self.assertEqual(tl.version_sales()["L1/v1@T-A"], 7)
        self.assertEqual(tl.version_sales()["L2/v1@T-B"], 6)
        # T-A 因 L1/2 接收失败被建议停发下一批
        self.assertTrue(any(h["terminal"] == "T-A" for h in tl.snapshot(7)["holds"]))

    # ---------- 团体 ----------
    def test_group_triage_and_diversion(self):
        triage = {g["ticket"]: g for g in self.desk.groups.triage()}
        self.assertTrue(triage["G1"]["must_serve"])
        self.assertFalse(triage["G1"]["may_divert_gate"])
        self.assertTrue(triage["G4"]["may_shift_window"])
        plan = {p["ticket"]: p for p in self.desk.group_plan()["plans"]}
        # 在途 G1 留在 E1/W4 且 9 人全部可接待（硬上限口径 11）
        self.assertEqual(plan["G1"]["unplaced"], 0)
        self.assertEqual(plan["G1"]["allocations"][0]["gate"], "E1")
        # G2 改引 E2 时集合时间 W4 保留
        g2_alt = [a for a in plan["G2"]["allocations"] if a["gate"] == "E2"]
        self.assertTrue(g2_alt and g2_alt[0]["window"] == 4)
        self.assertIn("集合时间 W4 不变", g2_alt[0]["change"])

    # ---------- 回测结论 ----------
    def test_only_day17_A_is_actual_false_release_zone(self):
        bt = run_backtest(self.ws)
        breaches = bt["actual_cap_breach_zones"]
        self.assertEqual(len(breaches), 1)
        self.assertEqual((breaches[0]["day"], breaches[0]["zone"]), (DAY, "A"))
        fr = bt["possible_false_release_zones"]
        self.assertTrue(any(p["day"] == DAY and p["zone"] == "A" for p in fr))

    # ---------- 门面 / JSON ----------
    def test_full_report_is_json_serializable(self):
        rep = self.desk.full_report()
        text = json.dumps(rep, ensure_ascii=False, default=str)
        self.assertIn("accounting_rules", rep)
        self.assertIn("backtest", rep)
        self.assertGreater(len(text), 10000)

    def test_accounting_rules_cover_required_subjects(self):
        subjects = " ".join(r["事项"] for r in self.desk.full_report()["accounting_rules"])
        for token in ("边界外", "折返", "跨区", "排队", "补传", "缺脉冲"):
            self.assertIn(token, subjects)

    def test_response_options_quantify_gap_and_protect_groups(self):
        opt = self.desk.response_options()
        # E1 承担 20 人刚性团体 + 10 终端承诺，对 11 个硬槽位有 19 缺口
        e1 = opt["by_gate"]["E1"]
        self.assertEqual(e1["must_serve_group_people_W4"], 20)
        self.assertEqual(e1["total_claims_conservative"], 30)
        self.assertEqual(e1["hard_gap_claims_minus_slots"], 19)
        # E2 有 8 个空余硬槽位可承接分流
        e2 = opt["by_gate"]["E2"]
        self.assertEqual(e2["safe_hard_slots"] - e2["total_claims_conservative"], 8)
        self.assertEqual(opt["aggregate_gap"], 11)
        self.assertTrue(opt["must_continue_serve"][0]["ticket"] == "G1")

    def test_path_bottleneck_points_to_saturated_A_for_E1(self):
        from opsdesk.capacity import path_bottlenecks
        inf = infer_day(self.ws, DAY, 4)
        pb = path_bottlenecks(inf, "E1", 4)
        self.assertIsNotNone(pb["limiting_saturated_zone"])
        self.assertEqual(pb["limiting_saturated_zone"]["zone"], "A")
        # AB 封闭时可先行离开名额为 0，不能借 BA 方向
        self.assertEqual(pb["departure_share"]["guaranteed_departure_slots"], 0)
        pb2 = path_bottlenecks(inf, "E2", 4)
        self.assertIsNone(pb2["limiting_saturated_zone"])

    def test_stop_trace_reaches_windows_valid_tickets_and_recovery(self):
        st = self.desk.stop_trace()
        self.assertEqual(st["triggers"][0]["count_id"], "2026-02-17-A-5")
        trace = {r["tranche_version"]: r for r in st["terminal_recovery_trace"]}
        self.assertEqual(trace["L1/1"]["awaiting_recovery_or_ack"], 3)
        self.assertEqual(trace["L2/1"]["awaiting_recovery_or_ack"], 4)
        self.assertEqual(trace["L3/1"]["kind"], "无回执整版敞口")
        self.assertIn("G1", json.dumps(st["valid_tickets"], ensure_ascii=False))

    def test_cli_round_smoke(self):
        from opsdesk.cli import main as cli_main
        self.assertEqual(cli_main(["round", "--asof", "4", "--data", str(DATA)]), 0)
        self.assertEqual(cli_main(["backtest", "--data", str(DATA)]), 0)

    def test_versioned_callback_book(self):
        from opsdesk.callbacks import CallbackBook
        book = CallbackBook.from_terminal_log(self.ws.terminal_log)
        # 日志中的 7+6 张出票已扣到版本
        summary = {r["key"]: r for r in book.summary()}
        self.assertEqual(summary["L1/v1"]["sold"], 7)
        book.register("到场", "L1", 1, 4, in_channel=2)
        row = {r["key"]: r for r in book.summary()}["L1/v1"]
        self.assertEqual(row["arrived_or_scanned"], 4)
        self.assertEqual(row["not_yet_arrived"], 3)
        self.assertEqual(row["scanned_in_channel_counted_at_origin"], 2)


if __name__ == "__main__":
    unittest.main()
