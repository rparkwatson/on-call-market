"""Shared helpers for the hosted-app tests (tests/test_app_*.py): a temp SQLite database and
a cycle loaded with the seven example columns from preferences_example.csv."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oncall_market as om  # noqa: E402
from app import db, repo  # noqa: E402

EXAMPLE_CSV = ROOT / "preferences_example.csv"
EXAMPLE_START = date(2026, 11, 2)
EXAMPLE_DAYS = 9


def temp_database(tmp_path, name="test.db"):
    """(url, engine, SessionLocal) for a fresh SQLite file under tmp_path."""
    url = "sqlite:///" + (Path(tmp_path) / name).as_posix()
    engine, Session = db.connect(url)
    return url, engine, Session


def example_columns():
    """(people, {name: {slot_id: cell}}) from preferences_example.csv, blanks omitted."""
    people, slot_rows = om.read_preferences(str(EXAMPLE_CSV))
    cells = {p: {} for p in people}
    for r in slot_rows:
        for p, c in zip(people, r["cells"]):
            if c:
                cells[p][r["id"]] = c
    return people, cells


def make_example_cycle(Session, name="Example cycle", names=None, submit=None, close=False):
    """A cycle with the example's period (three shifts, 2 Nov 2026, 9 weekdays) and people.
    Submissions are loaded for the names in `submit` (default: everyone in `names`).
    Returns the cycle dict (state collecting, or closed when close=True)."""
    people, cells = example_columns()
    names = list(names or people)
    with Session() as s:
        cycle = repo.create_cycle(s, name, structure="three", start_date=EXAMPLE_START, n_days=EXAMPLE_DAYS,
                                  names=names)
        repo.open_collection(s, cycle["id"])
        by_name = {p["name"]: p for p in repo.list_participants(s, cycle["id"])}
        for n in (names if submit is None else submit):
            repo.upsert_submission(s, by_name[n]["id"], cells[n])
        if close:
            repo.close_collection(s, cycle["id"])
        return repo.get_cycle(s, cycle["id"])
