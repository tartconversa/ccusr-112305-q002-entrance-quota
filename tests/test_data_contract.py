import unittest

from scripts.inspect_sources import read_table


class EntranceDataTest(unittest.TestCase):
    def test_history_and_cross_gate_movements(self):
        gates = read_table("gates")
        counts = read_table("counts")
        self.assertGreaterEqual(len({row["day"] for row in gates}), 12)
        self.assertTrue(any(row["kind"] == "区内" for row in counts))
        routes = {row["route"] for row in read_table("route_map")}
        self.assertTrue(all(row["sensor"] in routes for row in gates))
        self.assertTrue(any(row["known_at"] > row["observe_time"] for row in gates))

    def test_reissued_quota_and_late_confirmation_coexist(self):
        tranches = {(row["tranche"], row["version"]): row for row in read_table("ticket_tranches")}
        log = read_table("terminal_log")
        self.assertTrue(all((row["tranche"], row["version"]) in tranches for row in log))
        self.assertEqual(
            {row["kind"] for row in log if row["tranche"] == "L1"},
            {"接收", "接收失败", "出票", "归还", "回执订正"},
        )
        self.assertTrue(any(row["stages"] == "已签到在途" for row in read_table("group_tickets")))
