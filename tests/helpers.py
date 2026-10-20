"""Builders and checks shared by the tests (not a test module)."""
from datetime import date

import oncall_market as om

START = date(2026, 11, 2)  # a Monday: the first day the command line defaults to


def default_problem(structure="three", skip=(), people=om.DEFAULT_PEOPLE, days=9, max_per_day=1, packages=None):
    """The built-in problem: 9 weekdays from 2 Nov 2026 for 7 people."""
    return om.Problem(structure, om.weekdays(START, days, set(skip)), list(people), packages, max_per_day=max_per_day)


def rows_for(problem, cells_by_person):
    """Slot rows as parse_preferences returns them, from {person: {slot_id: cell}} (missing cells blank)."""
    return [{"id": s["id"], "date": s["date"], "code": s["code"],
             "cells": [cells_by_person.get(p, {}).get(s["id"], "") for p in problem.people]}
            for s in problem.slots]


def slot_index(problem, *slot_ids):
    index = {s["id"]: j for j, s in enumerate(problem.slots)}
    return [index[sid] for sid in slot_ids]


def check_schedule(problem, solution):
    """Every slot has exactly one owner, every person's package counts hold, the summary agrees
    with the schedule, and the solver reports no violations."""
    assert [r["slot_id"] for r in solution["schedule"]] == [s["id"] for s in problem.slots]
    owners = {r["slot_id"]: r["person"] for r in solution["schedule"]}
    assert set(owners.values()) <= set(problem.people)
    package_of = {r["person"]: r["package"] for r in solution["summary"]}
    assert list(package_of) == list(problem.people)
    for name, pk in problem.packages.items():
        assert sum(1 for p in package_of.values() if p == name) == pk["count"], name
    for row in solution["summary"]:
        pk = problem.packages[row["package"]]
        mine = [j for j, s in enumerate(problem.slots) if owners[s["id"]] == row["person"]]
        counts = [0] * len(problem.cats)
        for j in mine:
            counts[problem.slots[j]["cat"]] += 1
        assert all(pk["min"][c] <= counts[c] <= pk["max"][c] for c in range(len(problem.cats))), (row["person"], counts)
        assert pk["total"][0] <= len(mine) <= pk["total"][1], (row["person"], len(mine))
        assert row["shifts"] == len(mine)
        assert row["hours"] == sum(problem.slots[j]["hours"] for j in mine)
        assert row["slots"].split() == [problem.slots[j]["id"] for j in mine]
        assert row["violations"] == 0, row


def check_rules(problem, solution, text):
    """Independently of the solver's own violations(): nobody gets a slot they marked unavailable,
    nobody has more than max_per_day shifts a day, and no late shift is followed by an early
    shift on the next calendar day."""
    people, rows = om.parse_preferences(text)
    unavailable = {(people[i], r["id"]) for r in rows
                   for i, c in enumerate(r["cells"]) if c.lower() in om.UNAVAILABLE_TOKENS}
    assigned = {(r["person"], r["slot_id"]) for r in solution["schedule"]}
    assert not (assigned & unavailable)
    by_person_day = {}
    for r in solution["schedule"]:
        by_person_day.setdefault((r["person"], r["date"]), []).append(r["shift"])
    assert max(len(codes) for codes in by_person_day.values()) <= problem.max_per_day
    dates = solution["dates"]
    for (person, d), codes in by_person_day.items():
        di = dates.index(d)
        if problem.late in codes and di + 1 < len(dates) and problem.consecutive[di + 1]:
            next_codes = set(by_person_day.get((person, dates[di + 1]), []))
            assert not (next_codes & problem.early), (person, d)
