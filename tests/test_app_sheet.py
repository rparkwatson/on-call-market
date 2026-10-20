"""The participant sheet (app/sheet_component.py + app/components/sheet) from the Python
side.  AppTest cannot run the component's JavaScript, so: the args builder, the save path
once the sheet has reported cells (render_sheet is monkeypatched), the Save button before
it has, the plain fallback (?plain=1) and the read-only view after collection closes."""

import json
import os

import pytest
from streamlit.testing.v1 import AppTest

from tests.app_helpers import EXAMPLE_START, ROOT, example_columns, make_example_cycle, temp_database
from app import repo, sheet_component
from app import solver_adapter as sa

APP = str(ROOT / "streamlit_app.py")
SIX_RANKS = {"2026-11-02_M": "1", "2026-11-02_A": "2", "2026-11-02_E": "3",
             "2026-11-03_M": "4", "2026-11-03_A": "5", "2026-11-03_E": "6"}


@pytest.fixture(scope="module")
def Session(tmp_path_factory):
    url, _, Session = temp_database(tmp_path_factory.mktemp("sheet"))
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    yield Session
    if old is None:
        del os.environ["DATABASE_URL"]
    else:
        os.environ["DATABASE_URL"] = old


def _app(**query):
    at = AppTest.from_file(APP, default_timeout=60)
    for k, v in query.items():
        at.query_params[k] = v
    return at


def _person_a(Session, cycle):
    with Session() as s:
        return repo.list_participants(s, cycle["id"])[0]


def _sheet_args(at):
    """The args of the one sheet on the page (its proto carries them as JSON)."""
    sheets = at.get("component_instance")
    assert len(sheets) == 1
    return json.loads(sheets[0].proto.json_args)


def _fake_sheet(value, calls):
    """Stands in for render_sheet: records the call and reports `value` as the typed cells."""
    def render(slots, cells, person, min_ranks, mode="rank", readonly=False, key="sheet"):
        calls.append({"n_slots": len(slots), "cells": dict(cells), "person": person, "min_ranks": min_ranks,
                      "mode": mode, "readonly": readonly, "key": key})
        return None if readonly else dict(value)
    return render


def test_sheet_args_one_entry_per_slot():
    slots = sa.slot_table({"structure": "three", "start_date": EXAMPLE_START, "n_days": 9, "skip_dates": []})
    args = sheet_component.sheet_args(slots, {"2026-11-02_M": "1", "2026-11-04_E": "X", "2026-11-05_M": ""},
                                      "Person A", 6)
    assert len(args["slots"]) == 27
    assert all(tuple(sorted(e)) == tuple(sorted(sheet_component.SLOT_FIELDS)) for e in args["slots"])
    assert args["slots"][0] == {"id": "2026-11-02_M", "date": "2026-11-02", "weekday": "Mon", "short": "Mon Nov 2",
                                "code": "M", "label": "8-12", "time": "08:00-12:00", "hours": 4,
                                "first_of_day": True, "gap_before": False}
    assert args["slots"][1]["first_of_day"] is False
    monday = [e for e in args["slots"] if e["id"] == "2026-11-09_M"][0]
    assert monday["first_of_day"] is True and monday["gap_before"] is True  # the weekend before it
    assert sum(e["gap_before"] for e in args["slots"]) == 1
    assert sum(e["first_of_day"] for e in args["slots"]) == 9
    assert args["cells"] == {"2026-11-02_M": "1", "2026-11-04_E": "X"}  # blanks dropped
    assert (args["person"], args["min_ranks"], args["mode"], args["readonly"]) == ("Person A", 6, "rank", False)
    json.dumps(args)  # what the component call does with them


def test_component_files_speak_the_protocol():
    folder = sheet_component.COMPONENT_DIR
    assert folder.is_dir()
    assert all((folder / name).is_file() for name in sheet_component.COMPONENT_FILES)
    html = (folder / "index.html").read_text(encoding="utf-8")
    assert 'href="sheet.css"' in html and 'src="sheet.js"' in html
    js = (folder / "sheet.js").read_text(encoding="utf-8")
    for message in ("streamlit:componentReady", "streamlit:render", "streamlit:setFrameHeight",
                    "streamlit:setComponentValue"):
        assert message in js
    assert "isStreamlitMessage" in js and "apiVersion: 1" in js


def test_save_goes_through_validate_cells_and_upsert(Session, monkeypatch):
    cycle = make_example_cycle(Session, name="Sheet save", submit=[])
    person = _person_a(Session, cycle)
    calls = []
    typed = dict(SIX_RANKS, **{"2026-11-04_M": "x", "not-a-slot": "9"})
    monkeypatch.setattr(sheet_component, "render_sheet", _fake_sheet(typed, calls))
    at = _app(p=person["token"]).run()
    assert not at.exception
    assert calls[0] == {"n_slots": 27, "cells": {}, "person": "Person A", "min_ranks": 6, "mode": "rank",
                        "readonly": False, "key": "sheet"}
    assert len(at.text_input) == 0
    assert at.button[0].label == "Save my preferences" and not at.button[0].disabled
    at.button[0].click().run()
    assert not at.exception
    assert any(s.value.startswith("Saved") for s in at.success)
    with Session() as s:
        saved = repo.get_submission(s, person["id"])["cells"]
    assert saved == dict(SIX_RANKS, **{"2026-11-04_M": "X"})  # x cleaned to X, the unknown key dropped
    assert calls[-1]["cells"] == saved  # the saved cells are what the sheet is shown next
    assert any("Worth of each saved rank" in c.value for c in at.caption)


def test_too_few_ranks_is_refused_with_nothing_saved(Session, monkeypatch):
    cycle = make_example_cycle(Session, name="Sheet refuse", submit=[])
    person = _person_a(Session, cycle)
    monkeypatch.setattr(sheet_component, "render_sheet", _fake_sheet({"2026-11-02_M": "1", "2026-11-02_A": "0"}, []))
    at = _app(p=person["token"]).run()
    at.button[0].click().run()
    assert not at.exception
    assert any("at least 6 slots" in e.value for e in at.error)
    assert any(e.value.startswith("Mon Nov 2 1-5:30 pm: '0' is not a whole number") for e in at.error)
    assert any("Nothing was saved" in i.value for i in at.info)
    with Session() as s:
        assert repo.get_submission(s, person["id"]) is None


def test_save_is_disabled_until_the_sheet_reports(Session):
    cycle = make_example_cycle(Session, name="Sheet loading", submit=["Person A"])
    person = _person_a(Session, cycle)
    at = _app(p=person["token"]).run()  # the real component; AppTest never runs its JavaScript
    assert not at.exception
    args = _sheet_args(at)
    assert len(args["slots"]) == 27 and args["person"] == "Person A" and args["readonly"] is False
    assert args["cells"] == example_columns()[1]["Person A"]
    assert at.button[0].label == "Save my preferences" and at.button[0].disabled
    assert any("Loading the sheet" in c.value for c in at.caption)
    assert len(at.text_input) == 0


def test_plain_fallback_renders_and_saves(Session):
    cycle = make_example_cycle(Session, name="Sheet plain", submit=[])
    person = _person_a(Session, cycle)
    at = _app(p=person["token"], plain="1").run()
    assert not at.exception
    assert len(at.text_input) == 27 and not at.get("component_instance")
    for i, v in enumerate(["1", "2", "3", "4", "5", "6", "x"]):
        at.text_input[i].input(v)
    at.button[0].click().run()
    assert not at.exception
    assert any(s.value.startswith("Saved") for s in at.success)
    with Session() as s:
        assert repo.get_submission(s, person["id"])["cells"] == dict(SIX_RANKS, **{"2026-11-04_M": "X"})


def test_readonly_view_after_close(Session):
    cycle = make_example_cycle(Session, name="Sheet closed", submit=["Person A"], close=True)
    person = _person_a(Session, cycle)
    at = _app(p=person["token"]).run()
    assert not at.exception
    assert any("Collection has closed" in i.value for i in at.info)
    args = _sheet_args(at)
    assert args["readonly"] is True and args["key"] == "sheet_readonly"
    assert args["cells"] == example_columns()[1]["Person A"]
    assert len(at.text_input) == 0 and not any(b.label == "Save my preferences" for b in at.button)
    assert any("Worth of each saved rank" in c.value for c in at.caption)
    at = _app(p=person["token"], plain="1").run()
    assert not at.exception
    assert not at.get("component_instance") and len(at.table) == 2  # the entries, then the point preview
