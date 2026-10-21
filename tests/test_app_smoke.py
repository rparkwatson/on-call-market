"""Streamlit smoke tests with streamlit.testing.v1.AppTest: the landing page creates a cycle,
the participant form renders for a token and saves a column, the public page shows
'not published yet', and the organiser dashboard renders its tabs.  Uses a temp SQLite
file through the DATABASE_URL environment variable."""

import os

import pytest
from streamlit.testing.v1 import AppTest

from tests.app_helpers import ROOT, make_example_cycle, temp_database
import oncall_market as om
from app import repo
from app import solver_adapter as sa

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
    """The plain form (?plain=1): one text box per slot.  The default page shows the sheet
    component instead; tests/test_app_sheet.py covers it."""
    cycle = make_example_cycle(Session, name="Smoke participant", submit=[])
    with Session() as s:
        person = repo.list_participants(s, cycle["id"])[0]
    at = _app(p=person["token"], plain="1").run()
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
    assert run["seeds"] == [1] and len(run["candidates"]) == 3 == run["params"]["n_alternatives_found"]


def _fabricated_run(Session, cycle, n_candidates, n_found):
    """A stored run of n_candidates copies of one hand-built outcome (no solve: the responders
    in a round-robin over the slots, valued at zero prices), recorded as n_found equivalent
    schedules found.  Realistic enough for the Run market and Review tabs to render it."""
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    packages = om.STRUCTURES[cycle["structure"]]["packages"]
    ctx = sa.build_context(cycle, participants, submissions, packages)
    roles = [name for name, pk in packages.items() for _ in range(int(pk["count"]))][:len(ctx.people)]
    owner = [j % len(ctx.people) for j in range(len(ctx.slots))]
    result = {"roles": roles, "owner": owner, "prices": [0.0] * len(ctx.slots),
              "pkg_prices": {name: 0.0 for name in packages},
              "cleared": False, "rounds": 0, "max_regret": 0.0, "market_roles": True}
    base = sa.candidate_from_solution(om.solution_dict(ctx.problem, ctx.values, ctx.avail, result), 1)
    base.update(total_points=50.0, equivalent_to_best=True)
    candidates = [dict(base, label="Outcome 1 (the market's choice)" if k == 0
                       else "Outcome %d (equivalent alternative)" % (k + 1)) for k in range(n_candidates)]
    with Session() as s:
        return repo.create_run(s, cycle["id"], seeds=[1], params={"n_alternatives_found": n_found},
                               candidates=candidates, excluded_names=[],
                               packages_used={"source": "built-in", "packages": packages})


def _choose_buttons(at):
    return [b for b in at.button if b.label == "Choose this outcome"]


def test_run_market_tab_lists_six_outcomes_until_show_all(Session):
    """A run that kept 24 of the 40 equivalent schedules the solver found (the live case):
    the tab lists six with the capped caption and a 'Show all' box; ticking it lists all 24;
    a chosen outcome beyond the six stays listed when the box is cleared again."""
    cycle = make_example_cycle(Session, name="Smoke cap", close=True)
    run = _fabricated_run(Session, cycle, n_candidates=24, n_found=40)
    at = _app(a=cycle["admin_token"]).run()
    assert not at.exception
    assert any(h.value.endswith(": 40 equivalent schedules") for h in at.subheader)
    assert any(c.value.startswith("The market found 40 schedules that satisfy") for c in at.caption)
    assert any(c.value == "The market found 40 equivalent schedules; 24 are kept. The first 6 are listed; the 18 "
               "others are equally good. Tick 'Show all 24 outcomes' below the list to see them." for c in at.caption)
    assert len(_choose_buttons(at)) == 6
    assert sum("Outcome 6 (equivalent alternative)" in m.value for m in at.markdown) == 1
    assert not any("Outcome 7 (equivalent alternative)" in m.value for m in at.markdown)
    box = next(c for c in at.checkbox if c.label == "Show all 24 outcomes")
    assert box.value is False
    box.check().run()
    assert not at.exception
    assert len(_choose_buttons(at)) == 24
    assert any(c.value == "The market found 40 equivalent schedules; 24 are kept. All of them are listed."
               for c in at.caption)
    # choose the eighth outcome, then clear the box: the chosen one is listed with the first six
    _choose_buttons(at)[7].click().run()
    assert not at.exception
    assert any("Outcome 8 (equivalent alternative) chosen" in s.value for s in at.success)
    next(c for c in at.checkbox if c.label == "Show all 24 outcomes").uncheck().run()
    assert not at.exception
    assert len(_choose_buttons(at)) == 6 and sum(b.label == "Chosen" for b in at.button) == 1
    assert sum("Outcome 8 (equivalent alternative)" in m.value and "CHOSEN" in m.value for m in at.markdown) == 1
    assert any(c.value == "The market found 40 equivalent schedules; 24 are kept. The first 6 and the chosen one "
               "are listed; the 17 others are equally good. Tick 'Show all 24 outcomes' below the list to see them."
               for c in at.caption)
    with Session() as s:
        assert repo.get_run(s, run["id"])["chosen_index"] == 7


def test_run_market_tab_caption_when_only_the_listing_is_capped(Session):
    """Eight equivalent schedules, all kept: six are listed and the caption says so without
    mentioning the solver's cap."""
    cycle = make_example_cycle(Session, name="Smoke eight", close=True)
    _fabricated_run(Session, cycle, n_candidates=8, n_found=8)
    at = _app(a=cycle["admin_token"]).run()
    assert not at.exception
    assert any(h.value.endswith(": 8 equivalent schedules") for h in at.subheader)
    assert any(c.value == "The first 6 are listed; the 2 others are equally good. Tick 'Show all 8 outcomes' "
               "below the list to see them." for c in at.caption)
    assert not any("are kept" in c.value for c in at.caption)
    assert len(_choose_buttons(at)) == 6 and any(c.label == "Show all 8 outcomes" for c in at.checkbox)
