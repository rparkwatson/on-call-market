"""The solver adapter: sheet building, the three package branches, running the market on the
example data (one solve, one candidate per welfare-equivalent schedule) and checking it
against the CLI path, the solver's warnings, the exclusion rule, edits and the CSV texts."""

import copy
import random
import sys
import types
from datetime import date

import pytest

from tests.app_helpers import EXAMPLE_CSV, example_columns, make_example_cycle, temp_database
import oncall_market as om
from app import repo
from app import solver_adapter as sa


def _fake_packages_module():
    """A stand-in for packages.py: packages for 6 or 7 people over 9 weekdays in the three-shift structure."""
    def derive_packages(structure, n_people, days, max_per_day=1):
        assert structure == "three" and days == 9
        if n_people == 7:
            return copy.deepcopy(om.STRUCTURES["three"]["packages"])
        if n_people == 6:  # 90 h over 6 people: 3 x 14 h + 3 x 16 h; evenings 3 x 1 + 3 x 2 = 9
            return {"a": {"count": 3, "cats": {"four": [3, 3], "eve": [1, 1]}, "total": [4, 4]},
                    "b": {"count": 3, "cats": {"four": [3, 3], "eve": [2, 2]}, "total": [5, 5]}}
        raise om.SchedulingError("no packages for %d people" % n_people)
    module = types.ModuleType("packages")
    module.derive_packages = derive_packages
    return module


@pytest.fixture
def no_packages_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "packages", None)  # makes `import packages` fail


@pytest.fixture
def fake_packages_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "packages", _fake_packages_module())


def _example_cycle_dict():
    return {"structure": "three", "start_date": date(2026, 11, 2), "n_days": 9, "skip_dates": [],
            "max_per_day": 1, "mode": "rank"}


# ---------------------------------------------------------------------------
# sheet helpers
# ---------------------------------------------------------------------------

def test_slot_table_matches_problem_slots():
    cycle = _example_cycle_dict()
    cycle["skip_dates"] = ["2026-11-11"]
    slots = sa.slot_table(cycle)
    problem = om.Problem("three", sa.cycle_dates(cycle), om.DEFAULT_PEOPLE)
    assert [s["id"] for s in slots] == [s["id"] for s in problem.slots]
    assert len(slots) == 27 and slots[0]["first_of_day"] and not slots[1]["first_of_day"]
    assert [s["gap_before"] for s in slots].count(True) == 2  # after the weekend and after the skipped Wednesday
    assert slots[0]["short"] == "Mon Nov 2" and slots[2]["hours"] == 2.0


def test_sheet_text_round_trips_the_example():
    people, cells = example_columns()
    text = sa.sheet_text(_example_cycle_dict(), people, cells)
    assert text == EXAMPLE_CSV.read_text(encoding="utf-8").replace(
        "# EXAMPLE preferences (made up) - ranks 1, 2, 3 ... and X = unavailable.", om.TEMPLATE_HEADER[0])
    parsed_people, slot_rows = om.parse_preferences(text)
    assert parsed_people == people
    assert slot_rows[1]["cells"] == ["6", "8", "2", "", "X", "", "9"]
    csv_text = sa.preferences_csv_text(_example_cycle_dict(), people[:6], cells, excluded=["Person G"])
    assert "# Excluded from this cycle (no submission by the deadline): Person G" in csv_text
    assert om.parse_preferences(csv_text)[0] == people[:6]


def test_default_min_ranks():
    assert sa.default_min_ranks("three") == 6
    assert sa.default_min_ranks("two") == 5
    assert sa.default_min_ranks("single") == 9


# ---------------------------------------------------------------------------
# packages: override, packages.py, built-in, nothing
# ---------------------------------------------------------------------------

def test_get_packages_builtin_when_no_module_and_seven_by_nine(no_packages_module):
    dates = sa.cycle_dates(_example_cycle_dict())
    got = sa.get_packages("three", om.DEFAULT_PEOPLE, dates)
    assert got["source"] == "built-in" and got["packages"] == om.STRUCTURES["three"]["packages"]
    rows = sa.package_descriptions("three", got["packages"])
    assert [r["package"] for r in rows] == ["heavy", "light_evenings", "light_days"]
    assert [r["hours"] for r in rows] == ["14", "12", "12"] and rows[0]["contents"] == "3 x M/A, 1 x E"


def test_get_packages_message_when_no_module_and_other_size(no_packages_module):
    dates = sa.cycle_dates(_example_cycle_dict())
    got = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], dates)
    assert got["packages"] is None and got["source"] is None
    assert "not available yet for 6 people over 9 weekdays" in got["message"]
    assert "packages JSON" in got["message"]


def test_get_packages_uses_packages_module_for_responders(fake_packages_module):
    dates = sa.cycle_dates(_example_cycle_dict())
    got = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], dates)
    assert got["source"] == "packages.py" and set(got["packages"]) == {"a", "b"}
    got8 = sa.get_packages("three", om.DEFAULT_PEOPLE + ["Person H"], dates)
    assert got8["packages"] is None and "could not derive" in got8["message"]


def test_get_packages_override_json(no_packages_module):
    dates = sa.cycle_dates(_example_cycle_dict())
    override = ('{"a": {"count": 3, "cats": {"four": [3, 3], "eve": [1, 1]}, "total": [4, 4]},'
                ' "b": {"count": 3, "cats": {"four": [3, 3], "eve": [2, 2]}, "total": [5, 5]}}')
    got = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], dates, override_json=override)
    assert got["source"] == "organiser override" and got["packages"]["b"]["count"] == 3
    bad = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], dates, override_json=override.replace('"count": 3, "cats": {"four": [3, 3], "eve": [2', '"count": 2, "cats": {"four": [3, 3], "eve": [2'))
    assert bad["packages"] is None and "do not fit" in bad["message"]
    unreadable = sa.get_packages("three", om.DEFAULT_PEOPLE[:6], dates, override_json="{not json")
    assert unreadable["packages"] is None and "could not be read" in unreadable["message"]


# ---------------------------------------------------------------------------
# running the market on the example (one solve, shared by several checks)
# ---------------------------------------------------------------------------

# Person D's example column ranks 5 slots, below the app's default minimum of 6 for the
# three-shift structure; the form would refuse it, the helper loads it through the repo, and
# the solver reports it (never an error).
EXAMPLE_WARNING = "Person D ranked 5 slots; the minimum is 6."


@pytest.fixture(scope="module")
def example_run(tmp_path_factory):
    """Cycle with all seven example columns, closed, solved once through the adapter (default
    seeds).  The solve_sheet dict the run was built from is kept as "solution"."""
    _, _, Session = temp_database(tmp_path_factory.mktemp("adapter"))
    cycle = make_example_cycle(Session, close=True)
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    packages = sa.get_packages(cycle["structure"], participants, sa.cycle_dates(cycle))["packages"]
    seen, real_solve_sheet = [], sa.solve_sheet

    def recording_solve_sheet(text, **kwargs):
        seen.append(real_solve_sheet(text, **kwargs))
        return seen[-1]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "solve_sheet", recording_solve_sheet)
        outcome = sa.run_market(cycle, participants, submissions, packages)
    assert len(seen) == 1  # one solve, however many candidates come out of it
    return {"Session": Session, "cycle": cycle, "participants": participants, "submissions": submissions,
            "packages": packages, "outcome": outcome, "solution": seen[0]}


def test_run_market_example_is_fair_and_supported(example_run):
    outcome = example_run["outcome"]
    assert outcome["excluded"] == [] and outcome["responders"] == om.DEFAULT_PEOPLE
    assert len(outcome["candidates"]) == 3  # see test_run_market_lists_the_equivalent_schedules
    cand = outcome["candidates"][0]
    assert cand["seeds"] == [1] and cand["equivalent_to_best"] and cand["label"] == "Outcome 1 (the market's choice)"
    assert cand["prices_supported"] is True and cand["warnings"] == [EXAMPLE_WARNING]
    assert cand["status"].endswith(" WARNING: " + EXAMPLE_WARNING) and "indicative" not in cand["status"]
    assert cand["max_regret"] < 0.1
    assert cand["violations"] == 0
    assert sorted(r["hours"] for r in cand["person_rows"]) == [12, 12, 12, 12, 14, 14, 14]
    assert sorted(cand["owner"]) == sorted(list(range(7)) * 27)[:27] or len(cand["owner"]) == 27
    assert set(cand["owner"]) == set(range(7))
    assert all(r["late_shifts"] == {"heavy": 1, "light_evenings": 2, "light_days": 0}[r["package"]] for r in cand["person_rows"])
    assert abs(sum(r["points"] for r in cand["person_rows"]) - cand["total_points"]) < 0.5
    assert "SCHEDULE" in cand["report"] and "regret" in cand["status"]
    assert len(cand["schedule"]) == 27 and set(cand["prices"]) == {r["slot_id"] for r in cand["schedule"]}


def test_run_market_lists_the_equivalent_schedules(example_run):
    """The solver reports three welfare-equivalent schedules for the example (the heavy package
    moves between people, everyone's % of ideal is the same).  The market's choice comes first
    and the alternatives are valued at its prices."""
    cands = example_run["outcome"]["candidates"]
    solution = example_run["solution"]
    assert len(cands) == 3 == len(solution["alternatives"])
    assert [c["label"] for c in cands] == ["Outcome 1 (the market's choice)", "Outcome 2 (equivalent alternative)",
                                           "Outcome 3 (equivalent alternative)"]
    chosen = next(a for a in solution["alternatives"] if a["chosen"])
    assert [cands[0]["people"][i] for i in cands[0]["owner"]] == chosen["owner"] == [r["person"] for r in solution["schedule"]]
    assert all(c["equivalent_to_best"] and c["seeds"] == [1] for c in cands)
    totals = [c["total_points"] for c in cands]
    assert max(totals) - min(totals) <= sa.EQUIVALENT_POINTS
    pct = [[r["pct_of_ideal"] for r in c["person_rows"]] for c in cands]
    assert pct[0] == pct[1] == pct[2]
    heavy = [frozenset(p for p, pk in c["roles"].items() if pk == "heavy") for c in cands]
    assert len(set(heavy)) == 3 and all(len(h) == 3 for h in heavy)
    for alt, c in zip((a for a in solution["alternatives"] if not a["chosen"]), cands[1:]):
        assert [c["people"][i] for i in c["owner"]] == alt["owner"] and c["roles"] == alt["roles"]
        assert c["owner"] != cands[0]["owner"]
        assert c["prices"] == cands[0]["prices"] and c["package_prices"] == cands[0]["package_prices"]
        assert (c["cleared"], c["rounds"], c["max_regret"]) == (cands[0]["cleared"], cands[0]["rounds"], cands[0]["max_regret"])
        assert c["warnings"] == [EXAMPLE_WARNING] and c["prices_supported"] is True
        assert c["status"].endswith(" WARNING: " + EXAMPLE_WARNING)
        assert {r["person"]: r["hours"] for r in c["person_rows"]} == alt["hours"]
        assert len(c["schedule"]) == 27 and len(c["summary"]) == 7 and "SCHEDULE" in c["report"]
    assert "Ties: 3 equivalent schedules" in cands[0]["report"]


def test_run_market_without_alternatives_has_one_candidate(example_run, monkeypatch):
    """When the solver reports no alternative (or none at all), the solution is the only
    candidate; extra seeds are accepted but only the first is used."""
    cycle, participants, submissions, packages = (example_run[k] for k in ("cycle", "participants", "submissions", "packages"))
    solution = example_run["solution"]
    only_chosen = [a for a in solution["alternatives"] if a["chosen"]]
    for faked in (dict(solution, alternatives=only_chosen), dict(solution, alternatives=[]),
                  {k: v for k, v in solution.items() if k != "alternatives"}):
        monkeypatch.setattr(sa, "solve_sheet", lambda text, _s=faked, **kwargs: _s)
        outcome = sa.run_market(cycle, participants, submissions, packages, seeds=(1, 2, 3))
        assert len(outcome["candidates"]) == 1
        cand = outcome["candidates"][0]
        assert cand["label"] == "Outcome 1 (the market's choice)" and cand["equivalent_to_best"]
        assert cand["seeds"] == [1] and cand["owner"] == example_run["outcome"]["candidates"][0]["owner"]


def test_status_line_wording():
    base = {"cleared": False, "rounds": 800, "max_regret": 0.69, "summary": [{"violations": 0}], "warnings": []}
    assert sa.status_line(dict(base, prices_supported=True)).startswith(
        "Prices support this assignment with a maximum regret of 0.69 points")
    line = sa.status_line(dict(base, prices_supported=False, warnings=["Person A ranked 2 slots; the minimum is 6."]))
    assert line.startswith("The prices leave a maximum regret of 0.69 points")
    assert line.split(" WARNING: ")[1:] == ["Prices are indicative only: the regret exceeds 0.5 points.",
                                           "Person A ranked 2 slots; the minimum is 6."]
    cleared = sa.status_line({"cleared": True, "rounds": 12, "max_regret": 0.0, "summary": [], "prices_supported": True})
    assert cleared.startswith("The market cleared exactly in 12 rounds") and "WARNING" not in cleared


def test_run_market_matches_the_cli_path(example_run):
    """Same seed through om.read_preferences / Problem / preference_values / solve gives the same owners."""
    people, slot_rows = om.read_preferences(str(EXAMPLE_CSV))
    problem = om.Problem("three", sorted({r["date"] for r in slot_rows}), people)
    values, avail = om.preference_values(problem, slot_rows, "rank", random.Random(1))
    result = om.solve(problem, values, avail, seed=1)
    cand = example_run["outcome"]["candidates"][0]
    assert cand["owner"] == result["owner"]
    assert cand["roles"] == dict(zip(people, result["roles"]))
    _, rows, _ = om.build_report(problem, values, avail, result)
    assert cand["summary"] == rows


def test_csv_texts_match_write_outputs(example_run, tmp_path):
    people, slot_rows = om.read_preferences(str(EXAMPLE_CSV))
    problem = om.Problem("three", sorted({r["date"] for r in slot_rows}), people)
    values, avail = om.preference_values(problem, slot_rows, "rank", random.Random(1))
    cand = example_run["outcome"]["candidates"][0]
    result = sa.result_for_owner(cand, cand["owner"])
    sched, summ = om.write_outputs(problem, values, avail, result, str(tmp_path))
    solution = sa.solution_dict(problem, values, avail, result)
    assert sa.schedule_csv_text(solution) == open(sched, encoding="utf-8", newline="").read()
    assert sa.summary_csv_text(solution) == open(summ, encoding="utf-8", newline="").read()
    assert sa.schedule_csv_text(solution).splitlines()[0] == "slot_id,date,weekday,shift,time,hours,person,clearing_price"


def test_context_edits_and_person_rows(example_run):
    cycle, participants, submissions, packages = (example_run[k] for k in ("cycle", "participants", "submissions", "packages"))
    cand = example_run["outcome"]["candidates"][0]
    ctx = sa.build_context(cycle, participants, submissions, packages, seed=1)
    assert ctx.people == cand["people"]
    rows, solution = sa.person_rows(ctx, cand, cand["owner"])
    assert [r["hours"] for r in rows] == [r["hours"] for r in cand["person_rows"]]
    assert all(r["package_ok"] for r in rows) and all(r["violations"] == 0 for r in rows)
    assert solution["summary"] == cand["summary"]  # unedited: identical to the run

    # swap two four-hour slots held by different people: hours unchanged, packages still fit
    owner = cand["owner"]
    j = next(j for j, s in enumerate(ctx.slots) if s["code"] == "M")
    k = next(k for k, s in enumerate(ctx.slots) if s["code"] == "A" and owner[k] != owner[j])
    new_owner, text = sa.swap_slots(ctx, owner, j, k)
    assert text.startswith("Swapped Mon Nov 2 8-12 (M): ") and " -> " in text
    assert new_owner[j] == owner[k] and new_owner[k] == owner[j] and sum(a != b for a, b in zip(new_owner, owner)) == 2
    rows2, _ = sa.person_rows(ctx, cand, new_owner)
    assert [r["hours"] for r in rows2] == [r["hours"] for r in rows]
    assert all(r["package_ok"] for r in rows2)
    with pytest.raises(om.SchedulingError):
        sa.swap_slots(ctx, owner, j, j)

    # assigning a slot to someone else moves 4 hours and breaks the package counts
    target = (owner[j] + 1) % 7
    new_owner, text = sa.assign_slot(ctx, owner, j, target)
    assert text == "Assigned Mon Nov 2 8-12 (M) to %s (was %s)" % (ctx.people[target], ctx.people[owner[j]])
    rows3, _ = sa.person_rows(ctx, cand, new_owner)
    assert rows3[target]["hours"] == rows[target]["hours"] + 4 and rows3[owner[j]]["hours"] == rows[owner[j]]["hours"] - 4
    assert not rows3[target]["package_ok"] and not rows3[owner[j]]["package_ok"]
    with pytest.raises(om.SchedulingError):
        sa.assign_slot(ctx, owner, j, owner[j])


def test_grid_from_schedule(example_run):
    cand = example_run["outcome"]["candidates"][0]
    grid = sa.grid_from_schedule(cand["schedule"], "three")
    assert grid["columns"] == ["Date", "8-12", "1-5:30 pm", "6-8 pm"]
    assert len(grid["rows"]) == 9 and grid["rows"][0][0] == "Mon Nov 2"  # weekend gap is not a coverage gap
    assert all(cell in cand["people"] for row in grid["rows"] for cell in row[1:])
    table = sa.grid_table(grid)
    assert table[0]["Date"] == "Mon Nov 2" and set(table[0]) == set(grid["columns"])
    # a skipped weekday shows as a 'no coverage' row
    skipped = [r for r in cand["schedule"] if r["date"] != "2026-11-11"]
    grid2 = sa.grid_from_schedule(skipped, "three")
    assert len(grid2["rows"]) == 9 and grid2["rows"][7][0] == "no coverage Wed Nov 11"


def test_tie_rule_from_report(example_run):
    cands = example_run["outcome"]["candidates"]
    assert sa.tie_rule_from_report(cands[0]["report"]) == om.TIE_RULE
    assert sa.tie_rule_from_report(cands[1]["report"]) is None  # an alternative's report has no Ties line
    assert sa.tie_rule_from_report("") is None and sa.tie_rule_from_report(None) is None
    assert sa.tie_rule_from_report("  Ties: none (no other schedule found within 0.05 points of the best).") is None


def test_edits_csv_text():
    from datetime import datetime
    text = sa.edits_csv_text([{"created_at": datetime(2026, 10, 30, 9, 5, 0), "description": "Swapped a: X -> Y"}])
    assert text.splitlines() == ["when_utc,description", "2026-10-30 09:05:00,Swapped a: X -> Y"]


# ---------------------------------------------------------------------------
# exclusion rule (decision Q6)
# ---------------------------------------------------------------------------

def test_non_responder_is_excluded_and_needs_packages_module(tmp_path, no_packages_module):
    _, _, Session = temp_database(tmp_path)
    people, _ = example_columns()
    cycle = make_example_cycle(Session, submit=people[:6], close=True)
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    responders, excluded = sa.split_responders(participants, submissions)
    assert [p["name"] for p in responders] == people[:6] and excluded == ["Person G"]
    got = sa.get_packages(cycle["structure"], responders, sa.cycle_dates(cycle))
    assert got["packages"] is None and "not available yet for 6 people" in got["message"]


def test_non_responder_excluded_solves_with_packages_module(tmp_path, fake_packages_module):
    _, _, Session = temp_database(tmp_path)
    people, _ = example_columns()
    cycle = make_example_cycle(Session, submit=people[:6], close=True)
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    responders, excluded = sa.split_responders(participants, submissions)
    got = sa.get_packages(cycle["structure"], responders, sa.cycle_dates(cycle))
    assert got["source"] == "packages.py"
    outcome = sa.run_market(cycle, participants, submissions, got["packages"], seeds=(1,))
    assert outcome["excluded"] == ["Person G"] and outcome["responders"] == people[:6]
    cand = outcome["candidates"][0]
    assert cand["people"] == people[:6] and "Person G" not in cand["roles"]
    assert sorted(r["hours"] for r in cand["person_rows"]) == [14, 14, 14, 16, 16, 16]
    assert cand["violations"] == 0


def test_run_market_warns_when_someone_ranked_too_few(tmp_path):
    """The form refuses fewer than min_ranks ranks; a column written through the repo bypasses
    that, and the solver names the person in warnings, repeated in every candidate's status."""
    _, _, Session = temp_database(tmp_path)
    people, cells = example_columns()
    cycle = make_example_cycle(Session, submit=people[:6])
    few = {slot_id: str(k + 1) for k, slot_id in enumerate(list(cells["Person G"])[:3])}
    with Session() as s:
        g = next(p for p in repo.list_participants(s, cycle["id"]) if p["name"] == "Person G")
        repo.upsert_submission(s, g["id"], few)
        repo.close_collection(s, cycle["id"])
        cycle = repo.get_cycle(s, cycle["id"])
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    assert cycle["min_ranks"] == 6 and submissions["Person G"] == few
    packages = sa.get_packages(cycle["structure"], participants, sa.cycle_dates(cycle))["packages"]
    outcome = sa.run_market(cycle, participants, submissions, packages)
    assert outcome["excluded"] == [] and outcome["responders"] == people
    expected = [EXAMPLE_WARNING, "Person G ranked 3 slots; the minimum is 6."]  # sheet order
    for cand in outcome["candidates"]:
        assert cand["warnings"] == expected
        assert cand["status"].endswith(" WARNING: " + expected[0] + " WARNING: " + expected[1])
        assert "WARNINGS" in cand["report"]


def test_run_market_with_nobody_raises(tmp_path):
    _, _, Session = temp_database(tmp_path)
    cycle = make_example_cycle(Session, submit=[], close=True)
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
    with pytest.raises(om.SchedulingError):
        sa.run_market(cycle, participants, {}, om.STRUCTURES["three"]["packages"], seeds=(1,))
