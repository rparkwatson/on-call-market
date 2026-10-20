"""End to end: solve_sheet() on the shipped example, fixed roles, the demo sheets, and the
agreement between the command line's files and solve_sheet()'s data."""
import csv
import json
import random

import pytest

import oncall_market as om
from tests.helpers import check_rules, check_schedule, default_problem

SOLUTION_KEYS = {"structure", "people", "dates", "schedule", "summary", "prices", "package_prices",
                 "cleared", "rounds", "max_regret", "market_roles", "report", "alternatives", "warnings",
                 "prices_supported"}
SUMMARY_KEYS = ["person", "package", "shifts", "hours", "points_received", "pct_of_ideal", "net_price",
                "next_budget_suggestion", "violations", "slots", "responded"]
FIXED_ROLES = {"Person A": "heavy", "Person B": "heavy", "Person C": "heavy", "Person D": "light_evenings",
               "Person E": "light_evenings", "Person F": "light_evenings", "Person G": "light_days"}


class TestExampleEndToEnd:
    def test_is_deterministic_at_a_fixed_seed(self, example_result, example_solution):
        problem, values, avail, result = example_result
        assert [problem.people[i] for i in result["owner"]] == [r["person"] for r in example_solution["schedule"]]
        assert om.solution_dict(problem, values, avail, result) == example_solution

    def test_result_shape_is_as_documented_and_json_serialisable(self, example_solution):
        sol = example_solution
        assert set(sol) == SOLUTION_KEYS
        assert sol["structure"] == "three"
        assert sol["people"] == om.DEFAULT_PEOPLE
        assert sol["dates"] == ["2026-11-%02d" % d for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)]
        assert len(sol["schedule"]) == 27
        assert sol["schedule"][0] == {"slot_id": "2026-11-02_M", "date": "2026-11-02", "weekday": "Mon", "shift": "M",
                                      "time": "08:00-12:00", "hours": 4.0, "person": sol["schedule"][0]["person"],
                                      "price": sol["prices"]["2026-11-02_M"]}
        assert [list(r) for r in sol["summary"]] == [SUMMARY_KEYS] * 7
        assert set(sol["prices"]) == {r["slot_id"] for r in sol["schedule"]}
        assert all(r["price"] == sol["prices"][r["slot_id"]] for r in sol["schedule"])
        assert set(sol["package_prices"]) == {"heavy", "light_evenings", "light_days"}
        assert sol["market_roles"] is True
        assert isinstance(sol["cleared"], bool) and isinstance(sol["rounds"], int)
        assert sol["report"].startswith("SCHEDULE\n")
        assert json.loads(json.dumps(sol)) == sol

    def test_every_slot_has_one_owner_and_packages_hold(self, example_text, example_solution):
        problem, _, _ = om.prepare_sheet(example_text)
        check_schedule(problem, example_solution)
        check_rules(problem, example_solution, example_text)

    def test_hours_are_three_of_14_and_four_of_12(self, example_solution):
        assert sorted(r["hours"] for r in example_solution["summary"]) == [12.0] * 4 + [14.0] * 3
        heavy = [r for r in example_solution["summary"] if r["package"] == "heavy"]
        assert all(r["hours"] == 14.0 and r["shifts"] == 4 for r in heavy)

    def test_regret_is_below_a_tenth_of_a_point(self, example_solution):
        assert 0.0 <= example_solution["max_regret"] < 0.1
        assert example_solution["prices_supported"] is True
        assert "indicative only" not in example_solution["report"]
        if not example_solution["cleared"]:
            assert "maximum regret of %.2f points" % example_solution["max_regret"] in example_solution["report"]

    def test_summary_figures_are_consistent(self, example_solution):
        for row in example_solution["summary"]:
            # damped carry-over: half of the compensation (minus the net price), within 75 to 125
            assert row["next_budget_suggestion"] == pytest.approx(100.0 - 0.5 * row["net_price"], abs=0.051)
            assert 75.0 <= row["next_budget_suggestion"] <= 125.0
            assert 0.0 <= row["pct_of_ideal"] <= 100.0
            assert row["violations"] == 0


class TestCarryOver:
    @pytest.mark.parametrize("net_price, budget", [
        (0.0, 100.0), (-10.0, 105.0), (10.0, 95.0), (21.24, 89.4), (-21.24, 110.6),
        (-50.0, 125.0), (-60.0, 125.0), (50.0, 75.0), (60.0, 75.0),
    ])
    def test_next_budget_is_damped_and_capped(self, net_price, budget):
        assert om.next_budget(net_price) == budget

    def test_other_seeds_also_give_valid_schedules(self, example_text):
        problem, values, avail = om.prepare_sheet(example_text, seed=2)
        result = om.solve(problem, values, avail, seed=2, sa_iters=5000, restarts=1)
        sol = om.solution_dict(problem, values, avail, result)
        check_schedule(problem, sol)
        check_rules(problem, sol, example_text)


class TestFixedRoles:
    def test_fixed_roles_are_honoured(self, example_text):
        sol = om.solve_sheet(example_text, roles=FIXED_ROLES)
        assert sol["market_roles"] is False
        assert sol["package_prices"] is None
        assert {r["person"]: r["package"] for r in sol["summary"]} == FIXED_ROLES
        problem, _, _ = om.prepare_sheet(example_text)
        check_schedule(problem, sol)
        check_rules(problem, sol, example_text)
        assert "packages:" not in sol["report"]

    def test_validate_roles_rejects_wrong_counts(self):
        problem = default_problem()
        with pytest.raises(om.SchedulingError) as exc:
            om.validate_roles(problem, ["heavy"] * 4 + ["light_evenings"] * 3)
        assert str(exc.value) == ("The fixed packages give the wrong number of people to: "
                                  "heavy (needs 3, has 4), light_days (needs 1, has 0)")

    def test_validate_roles_rejects_unknown_packages(self):
        problem = default_problem()
        with pytest.raises(om.SchedulingError) as exc:
            om.validate_roles(problem, ["nope"] + ["heavy"] * 2 + ["light_evenings"] * 3 + ["light_days"])
        assert str(exc.value) == "Unknown package 'nope' (choose from heavy, light_evenings, light_days)"

    def test_validate_roles_requires_everyone(self):
        problem = default_problem()
        for roles in (None, ["heavy"] * 6, ["heavy"] * 6 + [None]):
            with pytest.raises(om.SchedulingError) as exc:
                om.validate_roles(problem, roles)
            assert str(exc.value) == "Every person needs a package when roles are fixed."

    def test_solve_sheet_roles_mapping_errors(self, example_text):
        with pytest.raises(om.SchedulingError) as exc:
            om.solve_sheet(example_text, roles=dict(FIXED_ROLES, Nobody="heavy"))
        assert str(exc.value) == "roles name people not in the preferences file: Nobody"
        short = {p: k for p, k in FIXED_ROLES.items() if p != "Person G"}
        with pytest.raises(om.SchedulingError) as exc:
            om.solve_sheet(example_text, roles=short)
        assert str(exc.value) == "Every person needs a package when roles are fixed."
        with pytest.raises(om.SchedulingError) as exc:
            om.solve_sheet(example_text, roles=dict(FIXED_ROLES, **{"Person G": "heavy"}))
        assert str(exc.value).startswith("The fixed packages give the wrong number of people to: heavy (needs 3, has 4)")


class TestSolveSheetInputs:
    def test_structure_can_be_given_or_inferred(self, example_text):
        problem, _, _ = om.prepare_sheet(example_text)
        assert problem.structure == "three"
        problem, _, _ = om.prepare_sheet(example_text, structure="three")
        assert problem.structure == "three"
        with pytest.raises(om.SchedulingError) as exc:
            om.prepare_sheet(example_text, structure="four")
        assert str(exc.value) == "Unknown structure 'four' (choose from single, two, three)."

    def test_mode_is_checked(self, example_text):
        with pytest.raises(om.SchedulingError) as exc:
            om.solve_sheet(example_text, mode="weird")
        assert str(exc.value) == "mode must be 'rank' or 'points', not 'weird'."

    def test_budgets_must_name_people_in_the_sheet(self, example_text):
        with pytest.raises(om.SchedulingError) as exc:
            om.solve_sheet(example_text, budgets={"Person A": 130.0, "Zed": 90.0})
        assert str(exc.value) == "Budgets name people not in the preferences file: Zed"
        problem, values, _ = om.prepare_sheet(example_text, budgets={"Person A": 130.0})
        ranked = [j for j, v in enumerate(values[0]) if v >= 1e-3]
        assert sum(values[0][j] for j in ranked) == pytest.approx(130.0, abs=0.03)

    def test_packages_override_and_max_per_day_reach_the_problem(self, example_text):
        packages = {"all": {"count": 7, "cats": {"four": [2, 3], "eve": [1, 2]}}}
        problem, _, _ = om.prepare_sheet(example_text, packages=packages, max_per_day=2)
        assert list(problem.packages) == ["all"]
        assert problem.max_per_day == 2

    def test_iteration_counts_must_be_at_least_one(self, example_text):
        problem, values, avail = om.prepare_sheet(example_text)
        for kwargs, name in ((dict(iters=0), "iters"), (dict(sa_iters=0), "sa_iters"), (dict(restarts=-1), "restarts")):
            with pytest.raises(om.SchedulingError) as exc:
                om.solve(problem, values, avail, **kwargs)
            assert str(exc.value) == "%s must be at least 1 (got %s)." % (name, kwargs[name])

    def test_log_receives_progress_lines(self, example_text):
        lines = []
        problem, values, avail = om.prepare_sheet(example_text)
        om.solve(problem, values, avail, roles=[FIXED_ROLES[p] for p in problem.people], market_roles=False,
                 sa_iters=2000, restarts=1, log=lines.append)
        assert lines[0] == "Finding clearing prices ..."
        assert any("round" in line or "cleared" in line for line in lines)


class TestDemo:
    def test_demo_at_seed_1_is_the_shipped_example(self, example_text):
        problem = default_problem("three")
        cells = om.demo_cells(problem, random.Random(1))
        header = ["# EXAMPLE preferences (made up) - ranks 1, 2, 3 ... and X = unavailable."] + om.TEMPLATE_HEADER[1:]
        assert om.preferences_text(problem, cells, header).splitlines() == example_text.splitlines()

    def test_demo_cells_have_ranks_and_x_for_everyone(self):
        problem = default_problem("two")
        cells = om.demo_cells(problem, random.Random(3))
        for column in cells:
            ranks = sorted(int(c) for c in column if c not in ("", "X"))
            assert ranks == list(range(1, len(ranks) + 1))
            assert ranks and "X" in column

    def demo_solution(self, structure):
        problem = default_problem(structure)
        text = om.preferences_text(problem, om.demo_cells(problem, random.Random(1)))
        sol = om.solve_sheet(text)
        assert sol["structure"] == structure
        check_schedule(problem, sol)
        check_rules(problem, sol, text)
        return sol

    def test_demo_three_shifts_is_covered_by_the_example_tests(self, example_solution):
        check_schedule(default_problem("three"), example_solution)

    def test_demo_two_shifts_solves_without_violations(self):
        sol = self.demo_solution("two")
        assert sorted(r["hours"] for r in sol["summary"]) == [12.0] * 4 + [14.0] * 3

    @pytest.mark.slow
    def test_demo_five_blocks_solves_without_violations(self):
        sol = self.demo_solution("single")
        assert sorted(r["hours"] for r in sol["summary"]) == [12.0] * 4 + [14.0] * 3
        # the five-block prices cannot be made to support the schedule (README): say so honestly
        assert sol["prices_supported"] == (sol["cleared"] or sol["max_regret"] <= om.REGRET_SUPPORTED)
        assert ("indicative only" in sol["report"]) == (not sol["prices_supported"])


class TestEntryPointMatchesCommandLine:
    def read_csv(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_schedule_csv_matches_solve_sheet(self, example_cli, example_solution):
        out_dir, _ = example_cli
        rows = self.read_csv(out_dir / "schedule.csv")
        expect = [{"slot_id": r["slot_id"], "date": r["date"], "weekday": r["weekday"], "shift": r["shift"],
                   "time": r["time"], "hours": str(r["hours"]), "person": r["person"],
                   "clearing_price": str(round(r["price"], 2))} for r in example_solution["schedule"]]
        assert rows == expect

    def test_summary_csv_matches_solve_sheet(self, example_cli, example_solution):
        out_dir, _ = example_cli
        rows = self.read_csv(out_dir / "summary.csv")
        assert rows == [{k: str(v) for k, v in r.items()} for r in example_solution["summary"]]

    def test_report_on_stdout_is_the_solution_report(self, example_cli, example_solution):
        _, stdout = example_cli
        assert example_solution["report"] in stdout

    def test_write_solution_reproduces_the_files_byte_for_byte(self, example_cli, example_solution, tmp_path):
        out_dir, _ = example_cli
        sched, summ = om.write_solution(json.loads(json.dumps(example_solution)), str(tmp_path))
        assert open(sched, "rb").read() == (out_dir / "schedule.csv").read_bytes()
        assert open(summ, "rb").read() == (out_dir / "summary.csv").read_bytes()
        assert om.schedule_csv_text(example_solution).encode("utf-8") == (out_dir / "schedule.csv").read_bytes()
        assert om.summary_csv_text(example_solution).encode("utf-8") == (out_dir / "summary.csv").read_bytes()

    def test_write_outputs_still_works_for_old_callers(self, example_result, example_cli, tmp_path):
        out_dir, _ = example_cli
        problem, values, avail, result = example_result
        sched, summ = om.write_outputs(problem, values, avail, result, str(tmp_path))
        assert open(sched, "rb").read() == (out_dir / "schedule.csv").read_bytes()
        assert open(summ, "rb").read() == (out_dir / "summary.csv").read_bytes()
