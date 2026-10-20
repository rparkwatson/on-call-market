"""Problem construction and validation, weekdays(), violations() and check_feasibility()."""
import random
from datetime import date

import pytest

import oncall_market as om
from tests.helpers import START, default_problem, rows_for, slot_index

NOV = lambda d: date(2026, 11, d)  # noqa: E731


class TestWeekdays:
    def test_skips_weekends(self):
        days = om.weekdays(START, 9)
        assert days == [NOV(d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)]
        assert all(d.weekday() < 5 for d in days)

    def test_skips_given_dates(self):
        days = om.weekdays(START, 9, {NOV(11)})
        assert days == [NOV(d) for d in (2, 3, 4, 5, 6, 9, 10, 12, 13)]

    def test_start_on_a_weekend_moves_to_monday(self):
        assert om.weekdays(date(2026, 10, 31), 2) == [NOV(2), NOV(3)]

    def test_fmt_date(self):
        assert om.fmt_date(NOV(2)) == "Mon Nov 2"
        assert om.fmt_date(NOV(12)) == "Thu Nov 12"


class TestProblem:
    @pytest.mark.parametrize("structure, n_slots, supply", [
        ("three", 27, {"four": 18, "eve": 9}),
        ("two", 18, {"M": 9, "L": 9}),
        ("single", 45, {"B1": 9, "B2": 9, "B3": 9, "B4": 9, "B5": 9}),
    ])
    def test_builtin_packages_validate_for_7_people_over_9_weekdays(self, structure, n_slots, supply):
        p = default_problem(structure)
        assert len(p.slots) == n_slots
        assert dict(zip(p.cats, p.supply)) == supply
        assert sum(pk["count"] for pk in p.packages.values()) == 7
        for ci in range(len(p.cats)):
            lo = sum(pk["count"] * pk["min"][ci] for pk in p.packages.values())
            hi = sum(pk["count"] * pk["max"][ci] for pk in p.packages.values())
            assert lo <= p.supply[ci] <= hi

    def test_slots_are_in_date_then_shift_order(self):
        p = default_problem()
        assert [s["id"] for s in p.slots[:4]] == ["2026-11-02_M", "2026-11-02_A", "2026-11-02_E", "2026-11-03_M"]
        assert p.slots[0] == {"id": "2026-11-02_M", "day": 0, "date": NOV(2), "code": "M", "cat": 0,
                              "hours": 4.0, "label": "8-12", "time": "08:00-12:00"}
        assert p.cat_of_code == {"M": 0, "A": 0, "E": 1}
        assert p.slots_by_day[1] == [3, 4, 5]
        assert p.late == "E" and p.early == {"M"}

    @pytest.mark.parametrize("structure", ["three", "two", "single"])
    def test_six_people_are_rejected(self, structure):
        with pytest.raises(om.SchedulingError) as exc:
            default_problem(structure, people=om.DEFAULT_PEOPLE[:6])
        assert "packages" in str(exc.value)
        assert str(exc.value).startswith("The packages are for 7 people but there are 6.")

    @pytest.mark.parametrize("structure, category", [("three", "four"), ("two", "M")])
    def test_ten_days_are_rejected(self, structure, category):
        with pytest.raises(om.SchedulingError) as exc:
            default_problem(structure, days=10)
        assert "packages" in str(exc.value)
        assert str(exc.value).startswith("Category %r has" % category)

    @pytest.mark.parametrize("days, slots", [(10, 50), (8, 40)])
    def test_wrong_number_of_days_is_rejected_for_single_blocks_too(self, days, slots):
        # the per-category ranges are wide enough to hide this; the package totals are not (found by T1)
        with pytest.raises(om.SchedulingError) as exc:
            default_problem("single", days=days)
        assert str(exc.value).startswith(
            "There are %d slots but the packages add up to between 45 and 45 shifts in total." % slots)

    def test_unknown_category_in_a_package_override(self):
        packages = {"p": {"count": 7, "cats": {"nope": [1, 1]}}}
        with pytest.raises(om.SchedulingError) as exc:
            default_problem(packages=packages)
        assert str(exc.value) == "Package p refers to unknown category 'nope'"

    def test_package_total_defaults_to_the_category_sums(self):
        packages = {"all": {"count": 7, "cats": {"four": [2, 3], "eve": [1, 2]}}}
        p = default_problem(packages=packages)
        assert p.packages == {"all": {"count": 7, "min": [2, 1], "max": [3, 2], "total": (3, 5)}}

    def test_readme_packages_json_equals_the_builtin_three_shift_packages(self):
        readme = {
            "heavy": {"count": 3, "cats": {"four": [3, 3], "eve": [1, 1]}, "total": [4, 4]},
            "light_evenings": {"count": 3, "cats": {"four": [2, 2], "eve": [2, 2]}, "total": [4, 4]},
            "light_days": {"count": 1, "cats": {"four": [3, 3], "eve": [0, 0]}, "total": [3, 3]},
        }
        assert default_problem(packages=readme).packages == default_problem().packages

    def test_consecutive_is_false_across_a_weekend_or_a_skipped_day(self):
        assert default_problem().consecutive == [False, True, True, True, True, False, True, True, True]
        p = default_problem(skip=[NOV(11)])
        assert [d.day for d in p.dates] == [2, 3, 4, 5, 6, 9, 10, 12, 13]
        assert p.consecutive == [False, True, True, True, True, False, True, False, True]

    def test_max_per_day_is_at_least_one(self):
        assert default_problem(max_per_day=0).max_per_day == 1
        assert default_problem(max_per_day=2).max_per_day == 2


class TestViolations:
    def count(self, problem, slot_ids, unavailable=()):
        avail = [[True] * len(problem.slots) for _ in problem.people]
        for j in slot_index(problem, *unavailable):
            avail[0][j] = False
        return problem.violations(0, slot_index(problem, *slot_ids), avail)

    def test_clean_bundle(self):
        assert self.count(default_problem(), ["2026-11-02_M", "2026-11-03_A", "2026-11-04_E"]) == 0

    def test_unavailable_slots_count_one_each(self):
        p = default_problem()
        assert self.count(p, ["2026-11-02_M", "2026-11-03_A"], unavailable=["2026-11-02_M"]) == 1
        assert self.count(p, ["2026-11-02_M", "2026-11-03_A"], unavailable=["2026-11-02_M", "2026-11-03_A"]) == 2

    def test_days_over_max_per_day(self):
        p = default_problem()
        assert self.count(p, ["2026-11-02_M", "2026-11-02_A"]) == 1
        assert self.count(p, ["2026-11-02_M", "2026-11-02_A", "2026-11-02_E"]) == 2
        p2 = default_problem(max_per_day=2)
        assert self.count(p2, ["2026-11-02_M", "2026-11-02_A"]) == 0
        assert self.count(p2, ["2026-11-02_M", "2026-11-02_A", "2026-11-02_E"]) == 1

    def test_late_then_early_on_consecutive_days(self):
        p = default_problem()
        assert self.count(p, ["2026-11-02_E", "2026-11-03_M"]) == 1
        assert self.count(p, ["2026-11-02_E", "2026-11-03_A"]) == 0
        assert self.count(p, ["2026-11-02_M", "2026-11-03_E"]) == 0

    def test_a_weekend_breaks_consecutiveness(self):
        assert self.count(default_problem(), ["2026-11-06_E", "2026-11-09_M"]) == 0

    def test_a_skipped_day_breaks_consecutiveness(self):
        assert self.count(default_problem(), ["2026-11-10_E", "2026-11-11_M"]) == 1
        assert self.count(default_problem(skip=[NOV(11)]), ["2026-11-10_E", "2026-11-12_M"]) == 0

    def test_violations_add_up(self):
        p = default_problem()
        bundle = ["2026-11-02_E", "2026-11-03_M", "2026-11-03_A", "2026-11-05_M"]
        assert self.count(p, bundle, unavailable=["2026-11-05_M"]) == 3


class TestCheckFeasibility:
    def run(self, cells_by_person, structure="three", max_per_day=1):
        problem = default_problem(structure, max_per_day=max_per_day)
        values, avail = om.preference_values(problem, rows_for(problem, cells_by_person), "rank", random.Random(1))
        om.check_feasibility(problem, values, avail)

    def test_example_is_feasible(self, example_text):
        problem, values, avail = om.prepare_sheet(example_text)
        om.check_feasibility(problem, values, avail)

    def test_nobody_available_for_a_slot(self):
        cells = {p: {"2026-11-02_M": "X"} for p in om.DEFAULT_PEOPLE}
        with pytest.raises(om.SchedulingError) as exc:
            self.run(cells)
        assert str(exc.value) == "Cannot build a schedule:\n  - nobody is available for Mon Nov 2 M"

    def test_short_handed_day(self):
        monday = {"2026-11-02_M": "X", "2026-11-02_A": "X", "2026-11-02_E": "X"}
        cells = {p: dict(monday) for p in om.DEFAULT_PEOPLE[:5]}
        with pytest.raises(om.SchedulingError) as exc:
            self.run(cells)
        assert str(exc.value) == ("Cannot build a schedule:\n  - Mon Nov 2 has 3 slots but the people available that "
                                  "day can cover only 2 (at most 1 shift(s) per person per day; try --max-per-day 2 "
                                  "or fewer X's)")

    def test_short_handed_day_is_fine_with_two_shifts_per_person(self):
        monday = {"2026-11-02_M": "X", "2026-11-02_A": "X", "2026-11-02_E": "X"}
        cells = {p: dict(monday) for p in om.DEFAULT_PEOPLE[:5]}
        self.run(cells, max_per_day=2)

    def test_too_many_x_to_fit_any_package(self):
        problem = default_problem()
        cells = {"Person A": {s["id"]: "X" for s in problem.slots if s["code"] in ("M", "A")}}
        with pytest.raises(om.SchedulingError) as exc:
            self.run(cells)
        assert str(exc.value) == "Cannot build a schedule:\n  - Person A has marked too many slots X to fit any package"

    def test_every_problem_is_listed(self):
        problem = default_problem()
        cells = {"Person A": {s["id"]: "X" for s in problem.slots if s["code"] in ("M", "A")}}
        for p in om.DEFAULT_PEOPLE:
            cells.setdefault(p, {})["2026-11-12_E"] = "X"
        with pytest.raises(om.SchedulingError) as exc:
            self.run(cells)
        lines = str(exc.value).splitlines()
        assert lines[0] == "Cannot build a schedule:"
        assert lines[1] == "  - nobody is available for Thu Nov 12 E"
        assert lines[2] == "  - Person A has marked too many slots X to fit any package"
        assert len(lines) == 3
