"""The dynamic programme in Problem.best_response against brute-force enumeration on tiny problems."""
import random
from datetime import date

import pytest

import oncall_market as om

MON, TUE, WED, THU, FRI, MON2 = (date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9))

TWO_PACKAGES = {"a": {"count": 1, "cats": {"M": [1, 1], "L": [0, 1]}, "total": [1, 2]},
                "b": {"count": 2, "cats": {"M": [0, 2], "L": [1, 2]}, "total": [2, 3]}}
THREE_PACKAGES = {"a": {"count": 1, "cats": {"four": [1, 3], "eve": [0, 2]}, "total": [2, 3]},
                  "b": {"count": 1, "cats": {"four": [2, 6], "eve": [1, 3]}, "total": [3, 6]}}
SINGLE_PACKAGES = {"a": {"count": 1, "cats": {"B1": [1, 1], "B2": [0, 1], "B3": [0, 1], "B4": [1, 2], "B5": [0, 1]},
                         "total": [2, 4]},
                   "b": {"count": 1, "cats": {"B1": [0, 2], "B2": [1, 2], "B3": [1, 2], "B4": [0, 2], "B5": [1, 2]},
                         "total": [3, 6]}}
LOOSE_PACKAGES = {"a": {"count": 1, "cats": {"four": [0, 4], "eve": [0, 3]}, "total": [1, 5]},
                  "b": {"count": 1, "cats": {"four": [2, 6], "eve": [0, 3]}, "total": [2, 6]}}

# (structure, dates, max_per_day, packages); the package counts fix the number of people
SCENARIOS = {
    "two shifts, three consecutive days": ("two", [MON, TUE, WED], 1, TWO_PACKAGES),
    "two shifts, two a day allowed": ("two", [MON, TUE, WED], 2, TWO_PACKAGES),
    "three shifts across a weekend": ("three", [THU, FRI, MON2], 1, THREE_PACKAGES),
    "three shifts with a skipped day, two a day": ("three", [MON, TUE, THU], 2, THREE_PACKAGES),
    "five blocks over two days, two a day": ("single", [MON, TUE], 2, SINGLE_PACKAGES),
    "three shifts, three a day allowed": ("three", [MON, TUE, WED], 3, LOOSE_PACKAGES),
}


def build(structure, dates, max_per_day, packages):
    n = sum(pk["count"] for pk in packages.values())
    people = ["P%d" % i for i in range(n)]
    return om.Problem(structure, dates, people, packages, max_per_day=max_per_day)


def feasible(problem, i, pkg, chosen, avail):
    """The rules best_response must respect, written out independently."""
    pk = problem.packages[pkg]
    if any(not avail[i][j] for j in chosen):
        return False
    counts = [0] * len(problem.cats)
    by_day = {}
    for j in chosen:
        s = problem.slots[j]
        counts[s["cat"]] += 1
        by_day.setdefault(s["day"], set()).add(s["code"])
    if any(not (pk["min"][c] <= counts[c] <= pk["max"][c]) for c in range(len(problem.cats))):
        return False
    if not (pk["total"][0] <= len(chosen) <= pk["total"][1]):
        return False
    if any(len(codes) > problem.max_per_day for codes in by_day.values()):
        return False
    for d, codes in by_day.items():
        if problem.late in codes and (d + 1) in by_day and problem.consecutive[d + 1] and (by_day[d + 1] & problem.early):
            return False
    return True


def brute_force(problem, i, pkg, prices, values, avail):
    m = len(problem.slots)
    best, best_set = None, None
    for mask in range(1 << m):
        chosen = [j for j in range(m) if mask >> j & 1]
        if not feasible(problem, i, pkg, chosen, avail):
            continue
        v = sum(values[i][j] - prices[j] for j in chosen)
        if best is None or v > best:
            best, best_set = v, chosen
    return best, best_set


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_dynamic_programme_matches_brute_force(name):
    structure, dates, max_per_day, packages = SCENARIOS[name]
    checked = 0
    for trial in range(6):
        rng = random.Random(1000 * len(name) + trial)
        # A fresh Problem per availability pattern (not required since the day-option cache became
        # availability-aware, but it keeps the trials independent).
        problem = build(structure, dates, max_per_day, packages)
        n, m = len(problem.people), len(problem.slots)
        avail = [[rng.random() > 0.25 for _ in range(m)] for _ in range(n)]
        values = [[rng.uniform(0.0, 30.0) for _ in range(m)] for _ in range(n)]
        for _ in range(3):
            prices = [rng.uniform(-15.0, 15.0) for _ in range(m)]
            for i in range(n):
                for pkg in problem.packages:
                    expect_v, expect_set = brute_force(problem, i, pkg, prices, values, avail)
                    got_v, got_set = problem.best_response(i, pkg, prices, values, avail)
                    if expect_v is None:
                        assert (got_v, got_set) == (None, None), (name, trial, i, pkg)
                        continue
                    assert got_v == pytest.approx(expect_v, abs=1e-9), (name, trial, i, pkg, got_set, expect_set)
                    assert got_set == sorted(got_set)
                    assert feasible(problem, i, pkg, got_set, avail), (name, trial, i, pkg, got_set)
                    assert sum(values[i][j] - prices[j] for j in got_set) == pytest.approx(got_v, abs=1e-9)
                    assert problem.violations(i, got_set, avail) == 0
                    checked += 1
    assert checked > 0


class TestRules:
    """Hand-built cases for each rule, where the best bundle is known."""

    def one_person(self, structure, dates, cats, total, max_per_day=1):
        """Person 0 ("Solo") holds package p; a second person with a package wide enough to absorb
        every slot keeps Problem.validate() happy without affecting Solo's best response."""
        packages = {"p": {"count": 1, "cats": cats, "total": total}}
        probe = om.Problem(structure, dates, ["Solo"], {"all": {"count": 1, "cats": {
            c: [0, len(dates) * len(codes)] for c, codes in om.STRUCTURES[structure]["categories"].items()}}})
        packages["rest"] = {"count": 1, "cats": {c: [0, probe.supply[ci]] for ci, c in enumerate(probe.cats)},
                            "total": [0, len(probe.slots)]}
        return om.Problem(structure, dates, ["Solo", "Filler"], packages, max_per_day=max_per_day)

    def test_late_then_early_is_blocked_only_on_consecutive_calendar_days(self):
        cats, total = {"M": [1, 1], "L": [1, 1]}, [2, 2]
        for dates, allowed in (([MON, TUE], False), ([FRI, MON2], True), ([MON, WED], True)):
            p = self.one_person("two", dates, cats, total)
            values = [[1.0, 10.0, 10.0, 1.0]]  # M d1, L d1, M d2, L d2: the tempting pair is L d1 + M d2
            avail = [[True] * 4]
            v, bundle = p.best_response(0, "p", [0.0] * 4, values, avail)
            if allowed:
                assert (v, bundle) == (20.0, [1, 2])
            else:
                assert (v, bundle) == (2.0, [0, 3])

    def test_late_and_early_on_the_same_day_are_allowed_when_two_shifts_fit(self):
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [1, 1]}, [2, 2], max_per_day=2)
        values = [[10.0, 10.0, 1.0, 1.0]]
        v, bundle = p.best_response(0, "p", [0.0] * 4, values, [[True] * 4])
        assert (v, bundle) == (20.0, [0, 1])

    def test_unavailable_slots_are_never_chosen(self):
        values = [[100.0, 100.0, 1.0, 1.0]]
        avail = [[False, False, True, True]]
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [0, 1]}, [1, 2])
        assert p.best_response(0, "p", [0.0] * 4, values, avail) == (1.0, [2])  # one shift a day
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [0, 1]}, [1, 2], max_per_day=2)
        assert p.best_response(0, "p", [0.0] * 4, values, avail) == (2.0, [2, 3])

    def test_negative_surplus_slots_are_taken_only_when_the_package_needs_them(self):
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [0, 1]}, [1, 2])
        values = [[5.0, 1.0, 5.0, 1.0]]
        prices = [0.0, 3.0, 2.0, 3.0]
        v, bundle = p.best_response(0, "p", prices, values, [[True] * 4])
        assert bundle == [0]  # one M is required; the L shifts would each lose 2 points
        assert v == 5.0
        # the same Problem with a different availability matrix (the day-option cache follows it)
        v, bundle = p.best_response(0, "p", prices, values, [[False, True, True, True]])
        assert bundle == [2]
        assert v == 3.0

    def test_day_options_follow_a_changed_availability_matrix(self):
        """One Problem instance may be reused with a different availability matrix: the day-option
        cache is keyed on the slots the person is available for that day (T1 found it keyed on the
        person only, which served stale options)."""
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [0, 1]}, [1, 2])
        values = [[5.0, 1.0, 5.0, 1.0]]
        assert p.best_response(0, "p", [0.0] * 4, values, [[True] * 4]) == (6.0, [0, 3])
        assert p.best_response(0, "p", [0.0] * 4, values, [[False, True, True, True]]) == (5.0, [2])
        assert p.best_response(0, "p", [0.0] * 4, values, [[True] * 4]) == (6.0, [0, 3])
        assert p.best_response(0, "p", [0.0] * 4, values, [[False] * 4]) == (None, None)

    def test_package_minimums_force_a_loss_making_slot(self):
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [1, 1]}, [2, 2])
        prices = [0.0, 50.0, 60.0, 40.0]
        v, bundle = p.best_response(0, "p", prices, [[0.0] * 4], [[True] * 4])
        assert bundle == [0, 3]
        assert v == -40.0

    def test_infeasible_returns_none(self):
        p = self.one_person("two", [MON, TUE], {"M": [1, 1], "L": [1, 1]}, [2, 2])
        assert p.best_response(0, "p", [0.0] * 4, [[1.0] * 4], [[False] * 4]) == (None, None)
        p = self.one_person("two", [MON, TUE], {"M": [2, 2], "L": [2, 2]}, [4, 4])
        assert p.best_response(0, "p", [0.0] * 4, [[1.0] * 4], [[True] * 4]) == (None, None)  # one shift a day
        p = self.one_person("two", [MON, TUE], {"M": [2, 2], "L": [2, 2]}, [4, 4], max_per_day=2)
        assert p.best_response(0, "p", [0.0] * 4, [[1.0] * 4], [[True] * 4]) == (None, None)  # L Mon then M Tue
        p = self.one_person("two", [FRI, MON2], {"M": [2, 2], "L": [2, 2]}, [4, 4], max_per_day=2)
        assert p.best_response(0, "p", [0.0] * 4, [[1.0] * 4], [[True] * 4]) == (4.0, [0, 1, 2, 3])
