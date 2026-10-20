"""Welfare-equivalent schedules and the seed-independent tie-break rule (finding F1)."""
import random
from datetime import date

import pytest

import oncall_market as om
from tests.helpers import check_rules, check_schedule

FRI, MON2 = date(2026, 11, 6), date(2026, 11, 9)   # not consecutive, so late-then-early never bites

# Two people, four slots (Fri M, Fri L, Mon M, Mon L), one 3-slot package and one 1-slot package.
PACKAGES = {"big": {"count": 1, "cats": {"M": [1, 2], "L": [1, 2]}, "total": [3, 3]},
            "small": {"count": 1, "cats": {"M": [0, 1], "L": [0, 1]}, "total": [1, 1]}}
# Xena: Fri M (66.7 pts), Fri L (33.3).  Yuri: Mon L (50), Fri L (33.3), Fri M (16.7).
# Either "Xena big {Fri M, Fri L, Mon M} + Yuri small {Mon L}" (Xena 100%, Yuri 50%) or
# "Yuri big {Mon L, Fri L, Mon M} + Xena small {Fri M}" (Xena 66.7%, Yuri 83.3%) scores 150 points.
CELLS = [["1", "2", "", ""], ["3", "2", "", "1"]]


def tiny_problem():
    return om.Problem("two", [FRI, MON2], ["Xena", "Yuri"], PACKAGES, max_per_day=2)


def tiny_text():
    return om.preferences_text(tiny_problem(), CELLS)


class TestHandBuiltTie:
    def solve(self):
        return om.solve_sheet(tiny_text(), packages=PACKAGES, max_per_day=2, iters=60, sa_iters=3000, restarts=2)

    def test_lighter_package_goes_to_the_person_with_the_lower_share_of_ideal(self):
        sol = self.solve()
        assert not sol["cleared"]
        assert {r["person"]: r["package"] for r in sol["summary"]} == {"Xena": "small", "Yuri": "big"}
        assert {r["person"]: r["pct_of_ideal"] for r in sol["summary"]} == {"Xena": 66.7, "Yuri": 83.3}
        assert [r["person"] for r in sol["schedule"]] == ["Xena", "Yuri", "Yuri", "Yuri"]
        check_schedule(tiny_problem(), sol)
        check_rules(tiny_problem(), sol, tiny_text())

    def test_both_equivalent_schedules_are_reported(self):
        sol = self.solve()
        alts = sol["alternatives"]
        assert len(alts) == 2
        assert [a["chosen"] for a in alts] == [True, False]
        assert alts[0]["roles"] == {"Xena": "small", "Yuri": "big"}
        assert alts[0]["owner"] == [r["person"] for r in sol["schedule"]]
        assert alts[1]["roles"] == {"Xena": "big", "Yuri": "small"}
        assert alts[1]["owner"] == ["Xena", "Xena", "Xena", "Yuri"]
        assert alts[0]["total_points"] == pytest.approx(alts[1]["total_points"], abs=om.TIE_TOLERANCE)
        assert alts[0]["hours"] == {"Xena": 4.0, "Yuri": 16.0}
        assert alts[0]["late_shifts"] == {"Xena": 0, "Yuri": 2}
        assert alts[1]["hours"] == {"Xena": 14.0, "Yuri": 6.0}
        assert ("  Ties: 2 equivalent schedules (total points within 0.05); chosen by " + om.TIE_RULE + "."
                ) in sol["report"]
        assert "    alternative 2 (150.0 pts): 2 slots change hands; Xena big 14 h, Yuri small 6 h" in sol["report"]

    def test_enumeration_finds_the_role_swap_from_either_side(self):
        problem, values, avail = om.prepare_sheet(tiny_text(), packages=PACKAGES, max_per_day=2)
        owner_a, roles_a = [0, 0, 0, 1], ["big", "small"]
        total_a = sum(values[owner_a[j]][j] for j in range(4))
        ties = om.equivalent_assignments(problem, values, avail, [(total_a, roles_a, owner_a)], True)
        assert sorted((r, o) for _, r, o in ties) == [(["big", "small"], [0, 0, 0, 1]), (["small", "big"], [0, 1, 1, 1])]
        assert all(abs(t - total_a) <= om.TIE_TOLERANCE for t, _, _ in ties)
        # with fixed roles the packages cannot swap, so there is no equivalent alternative
        assert len(om.equivalent_assignments(problem, values, avail, [(total_a, roles_a, owner_a)], False)) == 1

    def test_tie_break_key_components(self):
        problem, values, avail = om.prepare_sheet(tiny_text(), packages=PACKAGES, max_per_day=2)
        ideal = om.ideal_points(problem, values, avail, True, None)
        assert ideal == pytest.approx([100.0, 100.0], abs=1e-3)
        prices, pkg_prices = [1.0, 2.0, 3.0, 4.0], {"big": 5.0, "small": 0.0}
        key_b = om.tie_break_key(problem, values, ["small", "big"], [0, 1, 1, 1], prices, pkg_prices, True, ideal)
        key_a = om.tie_break_key(problem, values, ["big", "small"], [0, 0, 0, 1], prices, pkg_prices, True, ideal)
        assert key_b == (-66.67, 13.0, 2, (0, 1, 1, 1))   # Xena pays 1, Yuri 2+3+4+5; Yuri has most hours, 2 late
        assert key_a == (-50.0, 7.0, 1, (0, 0, 0, 1))
        assert key_b < key_a


class TestExampleIsSeedStable:
    def test_seeds_1_and_2_give_the_same_schedule_prices_and_summary(self, example_text, example_solution):
        sol2 = om.solve_sheet(example_text, seed=2)
        assert [r["person"] for r in sol2["schedule"]] == [r["person"] for r in example_solution["schedule"]]
        assert sol2["summary"] == example_solution["summary"]
        assert sol2["prices"] == example_solution["prices"]
        assert sol2["alternatives"] == example_solution["alternatives"]

    def test_example_has_equivalent_alternatives_and_exactly_one_is_chosen(self, example_solution):
        alts = example_solution["alternatives"]
        assert len(alts) >= 2
        assert sum(a["chosen"] for a in alts) == 1
        chosen = next(a for a in alts if a["chosen"])
        assert chosen["owner"] == [r["person"] for r in example_solution["schedule"]]
        assert chosen["roles"] == {r["person"]: r["package"] for r in example_solution["summary"]}
        best = max(a["total_points"] for a in alts)
        assert all(best - a["total_points"] <= om.TIE_TOLERANCE for a in alts)
        assert "  Ties: %d equivalent schedules" % len(alts) in example_solution["report"]
        assert om.TIE_RULE in example_solution["report"]

    def test_point_values_do_not_depend_on_the_seed(self, example_text):
        _, v1, a1 = om.prepare_sheet(example_text, seed=1)
        _, v2, a2 = om.prepare_sheet(example_text, seed=2)
        assert v1 == v2 and a1 == a2
        assert all(0.0 <= v - int(v) < 1e-3 for row in v1 for v in row if v < 1e-3)

    def test_fixed_roles_report_the_single_schedule(self, example_text):
        roles = {"Person A": "heavy", "Person B": "heavy", "Person C": "heavy", "Person D": "light_evenings",
                 "Person E": "light_evenings", "Person F": "light_evenings", "Person G": "light_days"}
        sol = om.solve_sheet(example_text, roles=roles, sa_iters=3000, restarts=1)
        assert sum(a["chosen"] for a in sol["alternatives"]) == 1
        assert all(a["roles"] == roles for a in sol["alternatives"])
        assert "  Ties:" in sol["report"]
