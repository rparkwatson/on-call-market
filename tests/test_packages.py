"""Tests for packages.py: the built-in packages are reproduced for 7 x 9, a grid of headcounts and
periods obeys the principles (checked against a brute-force oracle on the small cases), derived
packages really solve with demo preferences, and the command line writes a usable JSON file."""
import itertools
import json
import math
import os
import random
import subprocess
import sys
from datetime import date

import pytest

import oncall_market as om
import packages as pk

STRUCTURES = ("three", "two", "single")
START = date(2026, 11, 2)  # a Monday


# ---------------------------------------------------------------------------
# helpers (independent of packages.py's internals)
# ---------------------------------------------------------------------------

def facts(structure):
    """(categories, half-hours per category, codes per category, late category index, slots per day)."""
    st = om.STRUCTURES[structure]
    by_code = {sh["code"]: sh for sh in st["shifts"]}
    cats = list(st["categories"])
    half = [int(by_code[st["categories"][c][0]]["hours"] * 2) for c in cats]
    codes = [len(st["categories"][c]) for c in cats]
    late = next(i for i, c in enumerate(cats) if st["late"] in st["categories"][c])
    return cats, half, codes, late, len(st["shifts"])


def package_hours(structure, pk_):
    """Hours of one package: exact for fixed counts; total x block length for block structures."""
    cats, half, _, _, _ = facts(structure)
    if len(set(half)) == 1:
        return pk_["total"][0] * half[0] / 2
    return sum(pk_["cats"][c][0] * h for c, h in zip(cats, half)) / 2


def expected_loads(structure, n, days):
    """P1 arithmetic: (lower load, higher load, people on the higher load) in hours."""
    _, half, codes, _, _ = facts(structure)
    g = 0
    for h in half:
        g = math.gcd(g, h)
    total = days * sum(c * h for c, h in zip(codes, half))
    lo = g * (total // (g * n))
    k = (total - n * lo) // g
    return lo / 2, (lo + g) / 2 if k else lo / 2, k


def late_range(structure, pk_):
    cats, _, _, late, _ = facts(structure)
    lo, hi = pk_["cats"][cats[late]]
    return lo, hi


def demo_inputs(structure, n, days, max_per_day, seed=1):
    """A Problem with derived packages plus demo preferences: (problem, values, avail)."""
    packages = pk.derive_packages(structure, n, days, max_per_day=max_per_day)
    problem = om.Problem(structure, om.weekdays(START, days), ["P%d" % i for i in range(n)], packages,
                         max_per_day=max_per_day)
    cells = om.demo_cells(problem, random.Random(seed))
    rows = [{"id": s["id"], "date": s["date"], "code": s["code"], "cells": [cells[i][j] for i in range(n)]}
            for j, s in enumerate(problem.slots)]
    values, avail = om.preference_values(problem, rows, "rank", random.Random(seed))
    return problem, values, avail


# ---------------------------------------------------------------------------
# (a) 7 people x 9 weekdays reproduces the built-in packages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("structure", STRUCTURES)
def test_seven_by_nine_equals_builtin(structure):
    got = pk.derive_packages(structure, 7, 9)
    builtin = om.STRUCTURES[structure]["packages"]
    assert got == builtin                      # counts, ranges, totals and names
    assert list(got) == list(builtin)          # and the same order


def test_dates_list_and_max_per_day_two_give_the_same_builtin():
    dates = om.weekdays(START, 9)
    builtin = om.STRUCTURES["three"]["packages"]
    assert pk.derive_packages("three", 7, dates) == builtin
    assert pk.derive_packages("three", 7, [d.isoformat() for d in dates]) == builtin
    assert pk.derive_packages("three", 7, 9, max_per_day=2) == builtin


def test_describe_matches_the_readme_table():
    text = pk.describe(om.STRUCTURES["three"]["packages"], "three")
    assert "| heavy" in text and "3 x (8-12 or 1-5:30 pm) + 1 x 6-8 pm" in text and "| 14 " in text
    assert "| light_days" in text and "3 x (8-12 or 1-5:30 pm)" in text and "| 12 " in text
    single = pk.describe(om.STRUCTURES["single"]["packages"], "single")
    assert "1-2 x 10 am-12" in single and "| 14 " in single and "| 12 " in single


def test_worked_examples_are_deterministic_and_sensible():
    six = pk.derive_packages("three", 6, 9)
    assert six == pk.derive_packages("three", 6, 9)
    assert {name: p["count"] for name, p in six.items()} == {"16h_2eve": 3, "14h_1eve": 3}
    ten = pk.derive_packages("three", 7, 10)
    assert {name: p["count"] for name, p in ten.items()} == {"16h_2eve": 1, "14h_3eve": 1, "14h_1eve": 5}
    eight = pk.derive_packages("three", 8, 9)
    assert {name: p["count"] for name, p in eight.items()} == {"12h_2eve": 3, "12h_0eve": 2, "10h_1eve": 3}


# ---------------------------------------------------------------------------
# (b) the grid: n 3..12, days 5..15, every structure, max_per_day 1 and 2
# ---------------------------------------------------------------------------

GRID = [(n, days) for n in range(3, 13) for days in range(5, 16)]


def key_of(hi_people, lo_people, late):
    """The documented objective of a package set given as shapes per person: (maximum late count,
    higher-load people with at least as many late shifts as the lower-load package that carries
    the most, sum of squared late counts, number of distinct packages)."""
    lates_hi = [p[late] for p in hi_people]
    lates_lo = [p[late] for p in lo_people]
    lo_max = max(lates_lo, default=0)
    return (max(lates_hi + lates_lo), sum(1 for l in lates_hi if l >= lo_max),
            sum(l * l for l in lates_hi + lates_lo), len(set(hi_people)) + len(set(lo_people)))


def oracle_key(structure, n, days, max_per_day):
    """Brute force over every multiset of exact shapes at the two loads (small cases only)."""
    cats, half, codes, late, _ = facts(structure)
    lo, hi, k = expected_loads(structure, n, days)
    supply = [days * c for c in codes]
    caps = [days * min(max_per_day, c) for c in codes]

    def shapes(hours):
        return [s for s in itertools.product(*(range(c + 1) for c in caps))
                if sum(a * h for a, h in zip(s, half)) == hours * 2 and sum(s) <= days * max_per_day]

    best = None
    for hs in itertools.combinations_with_replacement(shapes(hi) if k else [], k):
        for ls in itertools.combinations_with_replacement(shapes(lo), n - k):
            people = list(hs) + list(ls)
            if all(sum(p[c] for p in people) == supply[c] for c in range(len(cats))):
                key = key_of(hs, ls, late)
                if best is None or key < best:
                    best = key
    return best


def result_key(structure, n, days, packages):
    cats, _, _, late, _ = facts(structure)
    _, hi, k = expected_loads(structure, n, days)
    hi_people, lo_people = [], []
    for p in packages.values():
        shape = tuple(p["cats"][c][0] for c in cats)
        (hi_people if k and package_hours(structure, p) == hi else lo_people).extend([shape] * p["count"])
    return key_of(hi_people, lo_people, late)


@pytest.mark.parametrize("structure,max_per_day", [(s, m) for s in STRUCTURES for m in (1, 2)])
def test_grid_obeys_the_principles(structure, max_per_day):
    cats, half, codes, late, per_day = facts(structure)
    feasible, infeasible = 0, 0
    for n, days in GRID:
        try:
            packages = pk.derive_packages(structure, n, days, max_per_day=max_per_day)
        except om.SchedulingError as e:
            infeasible += 1
            assert "people" in str(e) and ("max_per_day" in str(e) or "Headcounts that work" in str(e)), (n, days, e)
            continue
        feasible += 1
        case = (structure, n, days, max_per_day)
        om.Problem(structure, om.weekdays(START, days), ["P%d" % i for i in range(n)], packages, max_per_day=max_per_day)
        assert sum(p["count"] for p in packages.values()) == n, case
        assert all(p["total"][0] == p["total"][1] for p in packages.values()), case
        assert sum(p["count"] * p["total"][0] for p in packages.values()) == days * per_day, case  # the totals check
        for p in packages.values():                                                      # P3
            assert p["total"][1] <= days * max_per_day, case
            assert all(p["cats"][c][1] <= days * min(max_per_day, k) for c, k in zip(cats, codes)), case
        lo, hi, k = expected_loads(structure, n, days)                                   # P1
        hours = {name: package_hours(structure, p) for name, p in packages.items()}
        assert set(hours.values()) <= {lo, hi}, (case, hours)
        assert sum(p["count"] for name, p in packages.items() if k and hours[name] == hi) == k, (case, hours)
        lates = [late_range(structure, p) for p in packages.values()]                   # P2
        if len(set(half)) == 1:   # block structure: everyone has the floor of the late block or one more
            assert max(h for _, h in lates) - min(l for l, _ in lates) <= 1, (case, lates)
        else:
            assert all(l == h for l, h in lates), case
            assert max(h for _, h in lates) <= math.ceil(days * codes[late] / n) + 1, (case, lates)
            if n <= 8 and days <= 10:
                assert result_key(structure, n, days, packages) == oracle_key(structure, n, days, max_per_day), case
    assert feasible > 0
    if structure == "single":
        assert infeasible == (22 if max_per_day == 1 else 0)   # fewer than 5 people cannot cover 5 blocks a day


def test_single_extras_go_midday_for_the_heavy_load_and_to_the_edges_for_the_light():
    six = pk.derive_packages("single", 6, 9)
    assert six["heavy8"]["cats"] == {"B1": [1, 1], "B2": [2, 2], "B3": [2, 2], "B4": [2, 2], "B5": [1, 1]}
    assert six["light7"]["cats"] == {"B1": [2, 2], "B2": [1, 1], "B3": [1, 1], "B4": [1, 1], "B5": [2, 2]}
    # 8 x 9: five extra blocks for five heavier people, one of each type, so the heavier load has to
    # open its evening range too: the one-of-each-block rule leaves the lighter load no room for it.
    eight = pk.derive_packages("single", 8, 9)
    assert eight["heavy6"]["cats"] == {c: [1, 2] for c in ("B1", "B2", "B3", "B4", "B5")}
    assert eight["light5"]["cats"] == {c: [1, 1] for c in ("B1", "B2", "B3", "B4", "B5")}
    assert list(pk.derive_packages("single", 5, 8, max_per_day=2)) == ["all8"]


def test_infeasible_cases_say_what_would_work():
    with pytest.raises(om.SchedulingError, match="use at least 5 people or max_per_day 2"):
        pk.derive_packages("single", 4, 9)
    with pytest.raises(om.SchedulingError, match="Headcounts that work: 5 or 10"):
        pk.derive_packages("two", 6, 5)
    with pytest.raises(om.SchedulingError, match="late shift would have to take an early shift"):
        pk.derive_packages("three", 1, 5, max_per_day=3)
    with pytest.raises(om.SchedulingError, match="Unknown structure"):
        pk.derive_packages("four", 7, 9)


def test_streamlit_adapter_picks_the_module_up():
    sa = pytest.importorskip("app.solver_adapter")
    got = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], om.weekdays(START, 9))
    assert got["source"] == "packages.py" and got["packages"] == pk.derive_packages("three", 6, 9)


# ---------------------------------------------------------------------------
# (c) real solves: derived packages are schedulable with demo preferences
# ---------------------------------------------------------------------------

SOLVE_CASES = [  # about 40 s in all; the five-block solves are the slow ones
    ("three", 5, 6, 1), ("three", 6, 9, 1), ("three", 7, 10, 1), ("three", 8, 9, 1), ("three", 10, 9, 1),
    ("three", 12, 9, 1), ("three", 4, 9, 2), ("three", 9, 12, 1),
    ("two", 4, 5, 1), ("two", 6, 9, 1), ("two", 8, 9, 1), ("two", 5, 5, 1), ("two", 10, 5, 1), ("two", 4, 9, 2),
    ("single", 9, 9, 1), ("single", 10, 9, 1), ("single", 12, 9, 1),
]
SLOW_CASES = [("single", 7, 9, 1), ("single", 8, 9, 1), ("single", 6, 9, 2), ("single", 5, 8, 2)]  # 15-30 s each


def solve_and_check(structure, n, days, max_per_day):
    problem, values, avail = demo_inputs(structure, n, days, max_per_day)
    result = om.solve(problem, values, avail, sa_iters=4000, restarts=1)
    owner = result["owner"]
    assert None not in owner
    assert sorted(result["roles"].count(name) for name in problem.packages) == \
        sorted(p["count"] for p in problem.packages.values())
    for i, role in enumerate(result["roles"]):
        mine = [j for j in range(len(owner)) if owner[j] == i]
        assert problem.violations(i, mine, avail) == 0, (problem.people[i], role)
        counts = [0] * len(problem.cats)
        for j in mine:
            counts[problem.slots[j]["cat"]] += 1
        p = problem.packages[role]
        assert all(p["min"][c] <= counts[c] <= p["max"][c] for c in range(len(counts))), (role, counts)
        assert p["total"][0] <= len(mine) <= p["total"][1], (role, len(mine))


@pytest.mark.parametrize("structure,n,days,max_per_day", SOLVE_CASES)
def test_derived_packages_really_solve(structure, n, days, max_per_day):
    solve_and_check(structure, n, days, max_per_day)


@pytest.mark.slow
@pytest.mark.parametrize("structure,n,days,max_per_day", SLOW_CASES)
def test_derived_single_packages_really_solve_slow(structure, n, days, max_per_day):
    solve_and_check(structure, n, days, max_per_day)


# ---------------------------------------------------------------------------
# (d) the command line
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_cli_writes_json_that_problem_accepts(tmp_path, capsys):
    out = tmp_path / "packages.json"
    assert pk.main(["--structure", "two", "--people", "6", "--days", "9", "--out", str(out)]) == 0
    assert "| 16h_2L" in capsys.readouterr().out
    packages = json.loads(out.read_text(encoding="utf-8"))
    om.Problem("two", om.weekdays(START, 9), ["P%d" % i for i in range(6)], packages)
    assert packages == pk.derive_packages("two", 6, 9)


def test_cli_as_a_script_with_start_skip_and_max_per_day(tmp_path):
    out = tmp_path / "p.json"
    run = subprocess.run([sys.executable, "packages.py", "--structure", "three", "--people", "8", "--days", "9",
                          "--start", "2026-11-02", "--skip", "2026-11-11", "--max-per-day", "2", "--out", str(out)],
                         capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 0 and "Wrote" in run.stdout and "| Package" in run.stdout, run.stderr
    packages = json.loads(out.read_text(encoding="utf-8"))
    dates = om.weekdays(START, 9, {date(2026, 11, 11)})
    om.Problem("three", dates, ["P%d" % i for i in range(8)], packages, max_per_day=2)
    bad = subprocess.run([sys.executable, "packages.py", "--structure", "single", "--people", "4", "--days", "9"],
                         capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 1 and "use at least 5 people or max_per_day 2" in bad.stderr


# ---------------------------------------------------------------------------
# the shipped example file and the documentation
# ---------------------------------------------------------------------------

def test_packages_example_json_is_the_builtin_three_shift_set():
    with open(os.path.join(ROOT, "packages_example.json"), encoding="utf-8") as f:
        example = json.load(f)
    assert example == om.STRUCTURES["three"]["packages"] == pk.derive_packages("three", 7, 9)


def test_docs_tables_are_what_describe_prints():
    with open(os.path.join(ROOT, "docs", "packages.md"), encoding="utf-8") as f:
        text = f.read()
    for n, days in ((6, 9), (7, 10), (8, 9)):
        assert pk.describe(pk.derive_packages("three", n, days), "three") in text, (n, days)
