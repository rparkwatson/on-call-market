"""The assignment search: demand_start() builds a package-consistent starting point from the
market's demand, anneal() accepts it, and the result is reported as supported or indicative."""
import random

import pytest

import oncall_market as om
from tests.helpers import default_problem


def counts_of(problem, owner):
    counts = [[0] * len(problem.cats) for _ in problem.people]
    for j, i in enumerate(owner):
        counts[i][problem.slots[j]["cat"]] += 1
    return counts


def fits(problem, roles, counts):
    for i, cnt in enumerate(counts):
        pk = problem.packages[roles[i]]
        if not all(pk["min"][c] <= cnt[c] <= pk["max"][c] for c in range(len(problem.cats))):
            return False
        if not pk["total"][0] <= sum(cnt) <= pk["total"][1]:
            return False
    return True


class TestDemandStart:
    @pytest.mark.parametrize("structure", ["three", "two", "single"])
    def test_start_meets_the_supply_and_the_packages(self, structure):
        problem = default_problem(structure)
        text = om.preferences_text(problem, om.demo_cells(problem, random.Random(1)))
        problem, values, avail = om.prepare_sheet(text)
        prices, pkg_prices, _, _, _ = om.tatonnement(problem, values, avail, None, 30, 3.0, 40.0)
        roles = om.candidate_roles(problem, values, avail, prices, pkg_prices, top=1)[0]
        start = om.demand_start(problem, values, avail, roles, prices, random.Random(5))
        assert start is not None
        counts, owner = start
        assert [sum(counts[i][c] for i in range(len(problem.people))) for c in range(len(problem.cats))] == problem.supply
        assert fits(problem, roles, counts)
        assert None not in owner and counts_of(problem, owner) == counts
        # the demanded slots are honoured where the counts allow it
        demanded = 0
        for i in range(len(problem.people)):
            _, bundle = problem.best_response(i, roles[i], prices, values, avail)
            demanded += sum(1 for j in bundle if owner[j] == i)
        assert demanded >= len(problem.slots) // 2

    def test_anneal_from_a_start_keeps_the_packages(self):
        problem = default_problem("three")
        text = om.preferences_text(problem, om.demo_cells(problem, random.Random(2)))
        problem, values, avail = om.prepare_sheet(text)
        prices, pkg_prices, _, _, _ = om.tatonnement(problem, values, avail, None, 30, 3.0, 40.0)
        roles = om.candidate_roles(problem, values, avail, prices, pkg_prices, top=1)[0]
        rng = random.Random(7)
        start = om.demand_start(problem, values, avail, roles, prices, rng)
        owner, total = om.anneal(problem, values, avail, roles, 3000, rng, t0=om.DEMAND_START_T0, start=start)
        assert fits(problem, roles, counts_of(problem, owner))
        assert total == pytest.approx(sum(values[owner[j]][j] for j in range(len(owner))) - 1000.0 * sum(
            problem.violations(i, [j for j in range(len(owner)) if owner[j] == i], avail) for i in range(len(problem.people))))
        # the random start still works and consumes the rng the old way
        owner2, _ = om.anneal(problem, values, avail, roles, 3000, random.Random(7))
        assert fits(problem, roles, counts_of(problem, owner2))


class TestPricesSupported:
    def test_recomputed_for_results_without_the_key(self):
        assert om.prices_supported({"cleared": True, "max_regret": 0.0}) is True
        assert om.prices_supported({"cleared": False, "max_regret": 0.5}) is True
        assert om.prices_supported({"cleared": False, "max_regret": 0.51}) is False
        assert om.prices_supported({"cleared": False, "max_regret": 9.0, "prices_supported": True}) is True

    def test_report_wording_follows_the_flag(self, example_text):
        problem, values, avail = om.prepare_sheet(example_text)
        result = om.solve(problem, values, avail, iters=30, sa_iters=2000, restarts=1)
        result["max_regret"], result["prices_supported"] = 3.0, False
        sol = om.solution_dict(problem, values, avail, result)
        assert sol["prices_supported"] is False
        assert "Market status: the prices are indicative only - after 30 rounds they leave a maximum regret of 3.00 points" in sol["report"]
        assert "No price vector was found" in sol["report"]
        result["prices_supported"] = True
        sol = om.solution_dict(problem, values, avail, result)
        assert "indicative only" not in sol["report"]
        assert "support the assignment with a maximum regret of 3.00 points" in sol["report"]
