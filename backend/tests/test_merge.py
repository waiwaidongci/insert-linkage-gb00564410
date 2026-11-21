"""两厂清单归并算法的单元测试，只依赖标准库与领域层，可离线运行。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.demo import demo_rows, merge_rows
from src.domain.merge import (
    parse_groups, solve, validate_basis,
)

CUT = {"cut_distance": 0.60}
DEMO_GROUPS = "GA: CP-A01、CP-A02、CP-A03\nGB: CP-B01、CP-B02、CP-B03\nGC: CP-C01、CP-C02"


def row(spec_id, traits, name=None):
    return {"spec_id": spec_id, "name": name or f"内衬{spec_id}", "fit_traits": traits}


class MergeSolverTests(unittest.TestCase):
    def basis(self, old, new, groups=DEMO_GROUPS, params=None):
        return validate_basis(old, new, groups, params or CUT)

    def test_demo_scenario_min_new_groups_and_farthest_pairs(self):
        rows, meta = solve(self.basis(demo_rows(), merge_rows()))
        by_group = {item["group"]: item for item in rows}

        self.assertEqual(meta["new_groups"], 1)
        # 旧组编号与成员固定，且顺序保持签核次序。
        self.assertEqual([item["group"] for item in rows[:3]], ["GA", "GB", "GC"])
        self.assertIn("CP-A04", by_group["GA"]["members"])
        self.assertIn("CP-B04", by_group["GB"]["members"])
        self.assertEqual(by_group["GC"]["added"], 0)
        # 三个互容的异形规格共用一个新组。
        new_group = rows[-1]
        self.assertEqual(new_group["origin"], "另开新组")
        self.assertEqual(new_group["group"], "N01")
        self.assertEqual(new_group["added"], 3)
        # 最远规格对标注正确，且整组不超切线。
        self.assertLessEqual(new_group["farthest_distance"], 0.60)
        self.assertEqual((new_group["farthest_left"], new_group["farthest_right"]),
                         ("CP-D01", "CP-D02"))
        self.assertAlmostEqual(new_group["farthest_distance"], 0.4)

    def test_old_groups_are_fixed_and_never_split(self):
        rows, _ = solve(self.basis(demo_rows(), merge_rows()))
        old_members = {
            "GA": {"CP-A01", "CP-A02", "CP-A03"},
            "GB": {"CP-B01", "CP-B02", "CP-B03"},
            "GC": {"CP-C01", "CP-C02"},
        }
        for item in rows:
            if item["origin"] == "沿用旧组":
                members = set(item["members"].split("、"))
                self.assertTrue(old_members[item["group"]].issubset(members))

    def test_no_pair_exceeds_cut_in_any_group(self):
        basis = self.basis(demo_rows(), merge_rows())
        rows, _ = solve(basis)
        for item in rows:
            members = item["members"].split("、")
            for i in range(len(members)):
                for j in range(i):
                    self.assertLessEqual(
                        basis["pair_distance"](members[j], members[i]), CUT["cut_distance"]
                    )

    def test_conflicting_new_specs_force_separate_new_groups(self):
        # 两条新规格距离 1.0（特征完全不相交），且都进不了旧组，必须各开一组。
        old = [row("O1", "a|b|c"), row("O2", "a|b|c")]
        groups = "G1: O1、O2"
        new = [row("X1", "x|y"), row("X2", "p|q")]
        rows, meta = solve(self.basis(old, new, groups))
        self.assertEqual(meta["new_groups"], 2)
        origins = {item["group"]: item["members"] for item in rows if item["origin"] == "另开新组"}
        self.assertEqual(len(origins), 2)

    def test_new_spec_eligible_for_multiple_old_groups_uses_stable_pick(self):
        # 新规格与两个旧组都兼容时，结果应稳定并入固定次序最靠前的可容纳旧组。
        old = [row("G1a", "a|b"), row("G1b", "a|b"),
               row("G2a", "c|d"), row("G2b", "c|d")]
        groups = "G1: G1a、G1b\nG2: G2a、G2b"
        new = [row("Z1", "a|b|c|d")]  # 与两组距离均不超线
        rows, _ = solve(self.basis(old, new, groups))
        merged = {item["group"]: item["added_members"] for item in rows}
        self.assertEqual(merged["G1"], "Z1")
        self.assertEqual(merged["G2"], "—")

    def test_deterministic_across_repeated_runs(self):
        basis = self.basis(demo_rows(), merge_rows())
        first, _ = solve(basis)
        second, _ = solve(self.basis(demo_rows(), merge_rows()))
        self.assertEqual(first, second)

    def test_singleton_group_farthest_pair_is_self(self):
        old = [row("O1", "a|b|c")]
        new = [row("X1", "x|y|z")]
        rows, _ = solve(self.basis(old, new, "G1: O1"))
        singleton = next(item for item in rows if item["group"] == "N01")
        self.assertEqual(singleton["farthest_left"], "X1")
        self.assertEqual(singleton["farthest_right"], "X1")
        self.assertEqual(singleton["farthest_distance"], 0.0)

    def test_empty_new_batch_is_rejected_upfront(self):
        # 空批次在台账校验阶段即被拒绝，归并要求新批至少含一条规格。
        with self.assertRaises(ValueError):
            self.basis(demo_rows(), [])

    def test_equal_distance_cut_is_allowed(self):
        # 构造距离恰为 0.5 的对，切线 0.5 时必须允许同组。
        old = [row("O1", "a|b")]
        new = [row("X1", "a|c")]  # 交集 1，并集 3 → 距离 2/3? 用切线 0.7 容纳
        rows, _ = solve(self.basis(old, new, "G1: O1", {"cut_distance": 0.7}))
        self.assertIn("X1", rows[0]["members"])

    def test_duplicate_spec_id_across_batches_rejected(self):
        old = [row("O1", "a|b")]
        new = [row("O1", "a|b")]
        with self.assertRaises(ValueError):
            self.basis(old, new, "G1: O1")

    def test_signed_group_member_missing_rejected(self):
        old = [row("O1", "a|b")]
        with self.assertRaises(ValueError):
            self.basis(old, [row("X1", "c")], "G1: O1、O2")

    def test_unsigned_old_spec_rejected(self):
        old = [row("O1", "a|b"), row("O2", "a|b")]
        with self.assertRaises(ValueError):
            self.basis(old, [row("X1", "c")], "G1: O1")

    def test_old_group_exceeding_cut_rejected(self):
        old = [row("O1", "a|b"), row("O2", "x|y")]  # 距离 1.0
        with self.assertRaises(ValueError):
            self.basis(old, [], "G1: O1、O2")

    def test_parse_groups_accepts_full_width_colon_and_commas(self):
        parsed = parse_groups("G1：O1, O2\nG2: O3")
        self.assertEqual(parsed, [("G1", ["O1", "O2"]), ("G2", ["O3"])])

    def test_duplicate_group_code_rejected(self):
        old = [row("O1", "a|b"), row("O2", "c|d")]
        with self.assertRaises(ValueError):
            self.basis(old, [], "G1: O1\nG1: O2")

    def test_member_assignment_stable_under_id_order_swap(self):
        # 交换输入批次的行次序，归并结果必须一致（按编号稳定择一）。
        basis_a = self.basis(demo_rows(), merge_rows())
        basis_b = validate_basis(demo_rows(), list(reversed(merge_rows())), DEMO_GROUPS, CUT)
        self.assertEqual(solve(basis_a)[0], solve(basis_b)[0])

    def test_canonical_winner_among_multiple_zero_new_group_solutions(self):
        # 两条新规格同时兼容两个旧组且彼此兼容：最优为 0 个新组，存在多种并入方案，
        # 稳定择一应把它们都放入固定次序最靠前的旧组 G1。
        old = [row("G1a", "a|b"), row("G2a", "c|d")]
        groups = "G1: G1a\nG2: G2a"
        new = [row("NA", "a|b|c|d"), row("NB", "a|b|c|d")]
        rows, meta = solve(self.basis(old, new, groups, {"cut_distance": 0.7}))
        self.assertEqual(meta["new_groups"], 0)
        self.assertGreater(meta["ties"], 1)
        added = {item["group"]: item["added_members"] for item in rows}
        self.assertEqual(added["G1"], "NA、NB")
        self.assertEqual(added["G2"], "—")

    def test_new_group_labels_follow_smallest_member_order(self):
        # 新组编号 N01、N02… 必须按各组最小规格编号排序，与回溯内部着色顺序无关。
        old = [row("O1", "z1|z2|z3")]
        new = [row("S10", "a|b"), row("S02", "a|c"), row("S03", "b|c"), row("S04", "a|b|c")]
        rows, _ = solve(self.basis(old, new, "G1: O1", {"cut_distance": 0.34}))
        new_rows = [item for item in rows if item["origin"] == "另开新组"]
        self.assertEqual([item["group"] for item in new_rows],
                         [f"N{i:02d}" for i in range(1, len(new_rows) + 1)])
        smallest = [min(item["members"].split("、"), key=str.casefold) for item in new_rows]
        self.assertEqual(smallest, sorted(smallest, key=str.casefold))
        # 旧组编号仍固定在最前。
        self.assertEqual(rows[0]["group"], "G1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
