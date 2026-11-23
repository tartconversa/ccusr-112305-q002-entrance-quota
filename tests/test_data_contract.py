"""校验可供分析的资料关系。"""

import unittest

from scripts.inspect_sources import read_table


class SourceContractTest(unittest.TestCase):
    def test_source_observations_and_references(self):
        from collections import defaultdict
        by_block = defaultdict(list)
        by_stratum = defaultdict(set)
        for record in read_table("assignments"):
            by_block[record["block"]].append(record)
            by_stratum[record["stratum"]].add(record["level"])
        self.assertTrue(all(levels == {0,1,2} for levels in by_stratum.values()))
        for rows in by_block.values():
            self.assertEqual(len(rows), 8)
            self.assertEqual(sum(row["invited"] for row in rows), (0,2,5)[rows[0]["level"]])
        original = {row["group"]:row for row in read_table("assignments")}
        classes = {row["class"] for row in read_table("classes")}
        observed = read_table("outcomes")
        self.assertTrue(any(row["actual_class"] != original[row["group"]]["original_class"] for row in observed))
        self.assertTrue(all(row["actual_class"] in classes for row in observed))
        self.assertTrue(all(row["observed_attending"] is None or row["observed_attending"] <= original[row["group"]]["group_size"] for row in observed))
        self.assertEqual({"满员","关班","未回收"} - {row["endpoint"] for row in observed}, set())
        received = read_table("applications")
        self.assertTrue(any(row["paid"] and row["class"] == "P01" for row in received))
        self.assertTrue(any(row["event"] == "减班" and row["target"] == "P01" for row in read_table("updates")))
