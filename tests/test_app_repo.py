"""Repo functions on a temp SQLite database: create cycle, participants, submissions, state
changes, runs, choices, edits, publication, deletion."""

from datetime import date

import pytest

from app_helpers import make_example_cycle, temp_database
from app import repo
from app.repo import RepoError


@pytest.fixture
def Session(tmp_path):
    return temp_database(tmp_path)[2]


def test_create_cycle_has_tokens_and_defaults(Session):
    with Session() as s:
        c = repo.create_cycle(s, "  November 2026  ", names=["Ana", "Ben"])
        assert c["name"] == "November 2026"
        assert c["state"] == "draft"
        assert c["structure"] == "three" and c["n_days"] == 9 and c["max_per_day"] == 1
        assert c["min_ranks"] == 6  # largest three-shift package (4 shifts) + 2
        assert c["admin_token"] != c["public_token"]
        assert len(c["admin_token"]) >= 40 and len(c["public_token"]) >= 30
        assert c["start_date"].weekday() == 0  # a Monday
        assert repo.get_cycle_by_admin_token(s, c["admin_token"])["id"] == c["id"]
        assert repo.get_cycle_by_public_token(s, c["public_token"])["id"] == c["id"]
        assert repo.get_cycle_by_admin_token(s, "nope") is None
        people = repo.list_participants(s, c["id"])
        assert [p["name"] for p in people] == ["Ana", "Ben"]
        assert people[0]["token"] != people[1]["token"]
        assert people[0]["saved_at"] is None and people[0]["n_ranked"] is None


def test_create_cycle_needs_a_name(Session):
    with Session() as s:
        with pytest.raises(RepoError):
            repo.create_cycle(s, "   ")


def test_set_participants_keeps_tokens_of_kept_names(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x", names=["Ana", "Ben", "Cy"])
        before = {p["name"]: p["token"] for p in repo.list_participants(s, c["id"])}
        after = repo.set_participants(s, c["id"], ["Ben", " Dee ", "Ben", ""])
        assert [p["name"] for p in after] == ["Ben", "Dee"]
        assert {p["name"]: p["token"] for p in after}["Ben"] == before["Ben"]
        assert repo.get_participant_by_token(s, before["Ana"]) is None


def test_setup_is_locked_after_opening(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x", names=["Ana"])
        repo.update_cycle_setup(s, c["id"], n_days=10, skip_dates=[date(2026, 11, 11)], min_ranks=7)
        c = repo.get_cycle(s, c["id"])
        assert c["n_days"] == 10 and c["skip_dates"] == ["2026-11-11"] and c["min_ranks"] == 7
        repo.open_collection(s, c["id"])
        with pytest.raises(RepoError):
            repo.update_cycle_setup(s, c["id"], n_days=9)
        with pytest.raises(RepoError):
            repo.set_participants(s, c["id"], ["Ana", "Ben"])
        # notes stay editable in any state
        c = repo.update_cycle_notes(s, c["id"], base_url="https://x.streamlit.app", deadline_note="by Friday")
        assert c["base_url"] == "https://x.streamlit.app" and c["deadline_note"] == "by Friday"


def test_open_requires_participants(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x")
        with pytest.raises(RepoError):
            repo.open_collection(s, c["id"])


def test_submissions_upsert_clear_regenerate(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x", names=["Ana", "Ben"])
        ana = repo.list_participants(s, c["id"])[0]
        with pytest.raises(RepoError):  # not collecting yet
            repo.upsert_submission(s, ana["id"], {"2026-11-02_M": "1"})
        repo.open_collection(s, c["id"])
        saved = repo.upsert_submission(s, ana["id"], {"2026-11-02_M": "1", "2026-11-02_A": "X", "2026-11-02_E": ""})
        assert saved["cells"] == {"2026-11-02_M": "1", "2026-11-02_A": "X"}
        p, cyc = repo.get_participant_by_token(s, ana["token"])
        assert p["n_ranked"] == 1 and p["n_unavailable"] == 1 and p["saved_at"] is not None
        assert cyc["id"] == c["id"]
        # upsert replaces the whole column
        repo.upsert_submission(s, ana["id"], {"2026-11-03_M": "2"})
        assert repo.get_submission(s, ana["id"])["cells"] == {"2026-11-03_M": "2"}
        assert repo.submissions_by_name(s, c["id"]) == {"Ana": {"2026-11-03_M": "2"}}
        repo.clear_submission(s, ana["id"])
        assert repo.get_submission(s, ana["id"]) is None
        new_token = repo.regenerate_token(s, ana["id"])
        assert new_token != ana["token"]
        assert repo.get_participant_by_token(s, ana["token"]) is None
        assert repo.get_participant_by_token(s, new_token)[0]["id"] == ana["id"]
        repo.set_budget(s, ana["id"], 112.5)
        assert repo.list_participants(s, c["id"])[0]["budget"] == 112.5


def test_close_and_reopen(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x", names=["Ana"])
        with pytest.raises(RepoError):
            repo.close_collection(s, c["id"])
        repo.open_collection(s, c["id"])
        assert repo.close_collection(s, c["id"])["state"] == "closed"
        assert repo.reopen_collection(s, c["id"])["state"] == "collecting"


def _fake_candidates():
    return [
        {"owner": [0, 1, 0, 1], "people": ["Ana", "Ben"], "total_points": 50.0, "label": "Outcome 1"},
        {"owner": [1, 0, 1, 0], "people": ["Ana", "Ben"], "total_points": 49.0, "label": "Outcome 2"},
    ]


def test_run_choose_edit_publish_unpublish_delete(Session):
    with Session() as s:
        c = repo.create_cycle(s, "x", names=["Ana", "Ben", "Cy"])
        repo.open_collection(s, c["id"])
        ana = repo.list_participants(s, c["id"])[0]
        repo.upsert_submission(s, ana["id"], {"2026-11-02_M": "1"})
        with pytest.raises(RepoError):  # must be closed first
            repo.create_run(s, c["id"], seeds=[1], params={}, candidates=_fake_candidates(),
                            excluded_names=["Ben", "Cy"], packages_used={})
        repo.close_collection(s, c["id"])
        run = repo.create_run(s, c["id"], seeds=[1, 2], params={"structure": "three"},
                              candidates=_fake_candidates(), excluded_names=["Ben", "Cy"], packages_used={"source": "built-in"})
        assert run["chosen_index"] is None and run["excluded_names"] == ["Ben", "Cy"]
        assert repo.get_latest_run(s, c["id"])["id"] == run["id"]
        assert repo.current_owner(s, run["id"]) is None
        with pytest.raises(RepoError):
            repo.add_edit(s, run["id"], "too early", [0], [1])
        with pytest.raises(RepoError):
            repo.choose_candidate(s, run["id"], 5)

        run = repo.choose_candidate(s, run["id"], 1)
        assert run["chosen_index"] == 1
        assert repo.get_cycle(s, c["id"])["state"] == "solved"
        assert repo.current_owner(s, run["id"]) == [1, 0, 1, 0]

        e = repo.add_edit(s, run["id"], "Swapped Mon Nov 2 8-12 (M): Ben -> Ana", [1, 0, 1, 0], [0, 0, 1, 1])
        assert e["run_id"] == run["id"] and e["created_at"] is not None
        assert repo.current_owner(s, run["id"]) == [0, 0, 1, 1]
        assert [x["description"] for x in repo.list_edits(s, run["id"])] == ["Swapped Mon Nov 2 8-12 (M): Ben -> Ana"]

        # choosing another outcome discards the edits made on the previous one
        repo.choose_candidate(s, run["id"], 0)
        assert repo.list_edits(s, run["id"]) == []
        assert repo.current_owner(s, run["id"]) == [0, 1, 0, 1]
        repo.add_edit(s, run["id"], "Assigned Tue Nov 3 8-12 (M) to Ana (was Ben)", [0, 1, 0, 1], [0, 0, 0, 1])

        pub = repo.publish(s, c["id"], run["id"], schedule_rows=[{"slot_id": "a", "person": "Ana"}],
                           summary_rows=[{"person": "Ana", "hours": 12}], edits_log=[{"description": "x"}])
        assert repo.get_cycle(s, c["id"])["state"] == "published"
        assert repo.get_publication(s, c["id"])["id"] == pub["id"]
        assert repo.get_publication(s, c["id"])["schedule_rows"][0]["person"] == "Ana"
        with pytest.raises(RepoError):  # no new runs while published
            repo.create_run(s, c["id"], seeds=[1], params={}, candidates=_fake_candidates(),
                            excluded_names=[], packages_used={})
        # publishing again replaces the earlier publication
        pub2 = repo.publish(s, c["id"], run["id"], schedule_rows=[], summary_rows=[], edits_log=[])
        assert pub2["id"] != pub["id"] and repo.get_publication(s, c["id"])["id"] == pub2["id"]

        assert repo.unpublish(s, c["id"])["state"] == "solved"
        assert repo.get_publication(s, c["id"]) is None

        # a fresh run after reopening drops the choice until the organiser picks again
        repo.reopen_collection(s, c["id"])
        repo.close_collection(s, c["id"])
        run2 = repo.create_run(s, c["id"], seeds=[1], params={}, candidates=_fake_candidates(),
                               excluded_names=[], packages_used={})
        assert repo.get_cycle(s, c["id"])["state"] == "closed"
        assert repo.get_latest_run(s, c["id"])["id"] == run2["id"]

        tokens = [p["token"] for p in repo.list_participants(s, c["id"])]
        repo.delete_cycle(s, c["id"])
        assert repo.get_cycle(s, c["id"]) is None
        assert all(repo.get_participant_by_token(s, t) is None for t in tokens)
        assert repo.get_submission(s, ana["id"]) is None
        assert repo.get_run(s, run["id"]) is None and repo.list_edits(s, run["id"]) == []


def test_example_cycle_loads_seven_columns(Session):
    cycle = make_example_cycle(Session, close=True)
    with Session() as s:
        people = repo.list_participants(s, cycle["id"])
        assert len(people) == 7 and all(p["saved_at"] is not None for p in people)
        subs = repo.submissions_by_name(s, cycle["id"])
        assert subs["Person A"]["2026-11-02_E"] == "1" and subs["Person E"]["2026-11-02_A"] == "X"
        assert cycle["state"] == "closed"
