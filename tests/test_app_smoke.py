"""Streamlit smoke tests with streamlit.testing.v1.AppTest: the landing page creates a cycle,
the participant form renders for a token and saves a column, the public page shows
'not published yet', and the organiser dashboard renders its tabs.  Uses a temp SQLite
file through the DATABASE_URL environment variable."""

import os

import pytest
from streamlit.testing.v1 import AppTest

from tests.app_helpers import ROOT, make_example_cycle, temp_database
from app import repo

APP = str(ROOT / "streamlit_app.py")


@pytest.fixture(scope="module")
def Session(tmp_path_factory):
    url, _, Session = temp_database(tmp_path_factory.mktemp("smoke"))
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


def test_landing_page_creates_a_cycle(Session):
    at = _app().run()
    assert not at.exception
    assert at.title[0].value == "On-call market"
    assert at.button[0].label == "Create a new cycle"
    at.text_input[0].input("Smoke cycle")  # form inputs are sent with the submit click
    at.button[0].click().run()
    assert not at.exception
    assert any("Save this organiser link" in w.value for w in at.warning)
    link = at.code[0].value
    assert "?a=" in link
    token = link.split("?a=")[1]
    with Session() as s:
        cycle = repo.get_cycle_by_admin_token(s, token)
    assert cycle and cycle["name"] == "Smoke cycle" and cycle["state"] == "draft"


def test_participant_form_renders_and_saves(Session):
    cycle = make_example_cycle(Session, name="Smoke participant", submit=[])
    with Session() as s:
        person = repo.list_participants(s, cycle["id"])[0]
    at = _app(p=person["token"]).run()
    assert not at.exception
    assert at.title[0].value == "Smoke participant"
    assert len(at.text_input) == 27  # 9 weekdays x 3 shifts
    assert at.text_input[0].label.startswith("8-12 (08:00-12:00, 4 h)")
    assert at.button[0].label == "Save my preferences"

    # too few ranks: refused with a message, nothing saved
    at.text_input[0].input("1")
    at.button[0].click().run()
    assert not at.exception
    assert any("at least 6 slots" in e.value for e in at.error)
    with Session() as s:
        assert repo.get_submission(s, person["id"]) is None

    # six ranks and one X: saved, then shown with the point preview
    for i in range(6):
        at.text_input[i].input(str(i + 1))
    at.text_input[6].input("x")
    at.text_input[7].input("")
    at.button[0].click().run()
    assert not at.exception
    assert any(s.value.startswith("Saved") for s in at.success)
    with Session() as s:
        saved = repo.get_submission(s, person["id"])["cells"]
    assert saved == {"2026-11-02_M": "1", "2026-11-02_A": "2", "2026-11-02_E": "3", "2026-11-03_M": "4",
                     "2026-11-03_A": "5", "2026-11-03_E": "6", "2026-11-04_M": "X"}
    assert at.text_input[0].value == "1" and at.text_input[6].value == "x"  # entries survive the rerun
    assert any("Worth of each saved rank" in c.value for c in at.caption)


def test_participant_sees_read_only_after_close_and_nothing_before_open(Session):
    cycle = make_example_cycle(Session, name="Smoke closed", submit=["Person A"], close=True)
    with Session() as s:
        people = repo.list_participants(s, cycle["id"])
    at = _app(p=people[0]["token"]).run()
    assert not at.exception
    assert any("Collection has closed" in i.value for i in at.info)
    assert len(at.text_input) == 0
    at = _app(p=people[1]["token"]).run()
    assert any("did not save" in w.value for w in at.warning)
    at = _app(p="not-a-token").run()
    assert any("not valid" in e.value for e in at.error)


def test_public_page_before_publication(Session):
    cycle = make_example_cycle(Session, name="Smoke public", submit=[])
    at = _app(s=cycle["public_token"]).run()
    assert not at.exception
    assert at.title[0].value == "Smoke public"
    assert any("not been published yet" in i.value for i in at.info)
    assert not at.table


def test_admin_dashboard_renders(Session):
    cycle = make_example_cycle(Session, name="Smoke admin", close=True)
    at = _app(a=cycle["admin_token"]).run()
    assert not at.exception
    assert at.title[0].value == "Smoke admin"
    assert [t.label for t in at.tabs] == ["Setup", "Links", "Status", "Run market", "Review and edit", "Publish", "Danger zone"]
    assert any(b.label == "Run the market" for b in at.button)
    assert any("Using the built-in packages" in c.value or "Packages derived automatically" in c.value for c in at.caption)
    assert all(n.label != "Seeds to try" for n in at.number_input)  # one solve finds every equivalent schedule


def test_run_market_lists_the_equivalent_schedules(Session):
    """Run the market on the example cycle (one solve, about 10 s): the market's choice is listed
    first with the two equivalent alternatives, and the solver's warning about Person D shows."""
    cycle = make_example_cycle(Session, name="Smoke run", close=True)
    at = _app(a=cycle["admin_token"]).run()
    next(b for b in at.button if b.label == "Run the market").click().run()
    assert not at.exception
    assert any(h.value.startswith("Run at ") and h.value.endswith(": 3 equivalent schedules") for h in at.subheader)
    assert any("Its own choice is listed first, picked by the highest minimum % of ideal" in c.value for c in at.caption)
    assert sum("Outcome 1 (the market's choice)" in m.value for m in at.markdown) == 1
    assert sum("(equivalent alternative)" in m.value and "equivalent to outcome 1" in m.value for m in at.markdown) == 2
    assert sum(b.label == "Choose this outcome" for b in at.button) == 3
    assert sum(w.value == "Person D ranked 5 slots; the minimum is 6." for w in at.warning) == 3
    with Session() as s:
        run = repo.get_latest_run(s, cycle["id"])
    assert run["seeds"] == [1] and len(run["candidates"]) == 3
