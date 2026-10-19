"""Participant-form rules: cell cleaning, the minimum-rank check and the point preview."""

import random
from datetime import date

import pytest

from app_helpers import ROOT  # noqa: F401  (puts the repo root on sys.path)
import oncall_market as om
from app import prefs


@pytest.mark.parametrize("raw, expected", [
    ("", ""), ("   ", ""), ("x", "X"), ("X", "X"), (" n/a ", "X"), ("out", "X"),
    ("3", "3"), (" 12 ", "12"), ("3.0", "3"),
])
def test_normalise_cell_rank_mode(raw, expected):
    assert prefs.normalise_cell(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "2.5", "abc", "1st"])
def test_normalise_cell_rejects_bad_ranks(raw):
    with pytest.raises(ValueError):
        prefs.normalise_cell(raw)


def test_normalise_cell_points_mode():
    assert prefs.normalise_cell("2.5", mode="points") == "2.5"
    assert prefs.normalise_cell("0", mode="points") == "0"
    with pytest.raises(ValueError):
        prefs.normalise_cell("-1", mode="points")


def test_validate_cells_minimum_ranks():
    raw = {"a": "1", "b": "2", "c": "X", "d": "", "e": " 2 "}
    clean, errors = prefs.validate_cells(raw, min_ranks=3)
    assert clean == {"a": "1", "b": "2", "c": "X", "e": "2"}
    assert errors == []
    clean, errors = prefs.validate_cells(raw, min_ranks=4)
    assert [e[0] for e in errors] == [None]
    assert "at least 4 slots (you did 3). X does not count." in errors[0][1]
    clean, errors = prefs.validate_cells({"a": "1", "b": "oops", "c": "-2"}, min_ranks=1)
    assert clean == {"a": "1"}
    assert [e[0] for e in errors] == ["b", "c"]


def test_point_values_rank_rule():
    # ranks 1, 2, 2, 3: distinct values 3, 2, 1 -> weights 3, 2, 2, 1 (total 8)
    got = prefs.point_values({"a": "1", "b": "2", "c": "2", "d": "3", "e": "X"})
    assert [(r, round(p, 2), n) for r, p, n in got] == [(1, 37.5, 1), (2, 25.0, 2), (3, 12.5, 1)]
    assert prefs.point_values({"a": "X"}) == []
    assert prefs.point_values({}) == []
    # gaps between ranks do not matter, only the order
    assert [round(p, 3) for _, p, _ in prefs.point_values({"a": "1", "b": "7"})] == \
        [round(p, 3) for _, p, _ in prefs.point_values({"a": "1", "b": "2"})]
    assert [round(p, 1) for _, p, _ in prefs.point_values({"a": "1", "b": "2"}, budget=120)] == [80.0, 40.0]


def test_point_values_points_mode():
    got = prefs.point_values({"a": "10", "b": "5", "c": "5"}, mode="points")
    assert [(v, round(p, 2), n) for v, p, n in got] == [(10.0, 50.0, 1), (5.0, 25.0, 2)]


def test_point_preview_matches_preference_values():
    """The preview uses the same rule as om.preference_values (up to its 0.001 tie-break noise)."""
    people = ["Someone"]
    problem = om.Problem("three", om.weekdays(date(2026, 11, 2), 9), people,
                         packages_override={"solo": {"count": 1, "cats": {"four": [18, 18], "eve": [9, 9]}, "total": [27, 27]}})
    cells = {"2026-11-02_M": "1", "2026-11-02_A": "3", "2026-11-02_E": "3", "2026-11-03_M": "X", "2026-11-03_A": "9"}
    slot_rows = [{"id": s["id"], "date": s["date"], "code": s["code"], "cells": [cells.get(s["id"], "")]}
                 for s in problem.slots]
    values, avail = om.preference_values(problem, slot_rows, "rank", random.Random(1))
    preview = {rank: pts for rank, pts, _ in prefs.point_values(cells)}
    index = {s["id"]: j for j, s in enumerate(problem.slots)}
    for slot_id, cell in cells.items():
        if cell == "X":
            assert not avail[0][index[slot_id]]
        else:
            assert abs(values[0][index[slot_id]] - preview[int(cell)]) < 2e-3
    assert abs(sum(preview[int(c)] for c in cells.values() if c != "X") - 100.0) < 1e-9
