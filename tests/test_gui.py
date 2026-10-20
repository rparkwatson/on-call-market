"""oncall_gui helpers and its HTTP API, served in-process on an ephemeral port."""
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

import oncall_gui as gui
import oncall_market as om

SETUP = {"structure": "three", "start": "2026-11-02", "days": 9, "skip": "",
         "people": ", ".join(om.DEFAULT_PEOPLE), "max_per_day": 1}
FIXED_ROLES = {"Person A": "heavy", "Person B": "heavy", "Person C": "heavy", "Person D": "light_evenings",
               "Person E": "light_evenings", "Person F": "light_evenings", "Person G": "light_days"}


class TestMakeProblem:
    def test_builds_the_default_problem(self):
        p = gui.make_problem(SETUP)
        assert p.structure == "three"
        assert p.people == om.DEFAULT_PEOPLE
        assert p.dates[0] == date(2026, 11, 2) and len(p.dates) == 9
        assert p.max_per_day == 1

    def test_people_may_be_a_list_and_are_stripped(self):
        p = gui.make_problem(dict(SETUP, people=[" Ann ", "Bob", "Cy", "Di", "Ed", "Flo", "Gus "], max_per_day="2"))
        assert p.people == ["Ann", "Bob", "Cy", "Di", "Ed", "Flo", "Gus"]
        assert p.max_per_day == 2

    def test_skip_accepts_commas_and_spaces(self):
        p = gui.make_problem(dict(SETUP, skip="2026-11-11 2026-11-12,2026-11-13"))
        assert [d.day for d in p.dates] == [2, 3, 4, 5, 6, 9, 10, 16, 17]

    @pytest.mark.parametrize("field, value, message", [
        ("start", "2026-13-01", "First day must be a date like 2026-11-02."),
        ("start", "", "First day must be a date like 2026-11-02."),
        ("days", "abc", "Weekdays to cover must be a whole number."),
        ("days", 0, "Weekdays to cover must be between 1 and 60."),
        ("days", 61, "Weekdays to cover must be between 1 and 60."),
        ("skip", "2026-11-11, nope", "Dates to skip must look like 2026-11-11 (got 'nope')."),
        ("people", "Ann, Ann, Bob", "Each person needs a different name."),
        ("people", " , ", "Enter at least one name under People."),
        ("structure", "four", "Unknown shift structure 'four'."),
        ("max_per_day", "x", "Shifts per day must be a whole number."),
    ])
    def test_validation_messages(self, field, value, message):
        with pytest.raises(om.SchedulingError) as exc:
            gui.make_problem(dict(SETUP, **{field: value}))
        assert str(exc.value) == message

    def test_headcount_errors_come_from_the_solver(self):
        with pytest.raises(om.SchedulingError) as exc:
            gui.make_problem(dict(SETUP, people="Ann, Bob"))
        assert str(exc.value).startswith("The packages are for 7 people but there are 2.")


class TestCellHelpers:
    def test_slot_rows_and_matrix(self):
        p = gui.make_problem(SETUP)
        cells = {"2026-11-02_M": {"Person A": " 3 ", "Person G": "X"}, "2026-11-03_E": {"Person B": 1}}
        rows = gui.slot_rows_from_cells(p, cells)
        assert [r["id"] for r in rows] == [s["id"] for s in p.slots]
        assert rows[0]["cells"] == ["3", "", "", "", "", "", "X"]
        assert rows[5]["cells"] == ["", "1", "", "", "", "", ""]
        matrix = gui.cells_matrix(p, cells)
        assert matrix[0][0] == "3" and matrix[6][0] == "X" and matrix[1][5] == "1"
        assert gui.slot_rows_from_cells(p, "not a dict")[0]["cells"] == [""] * 7

    def test_sheet_payload(self):
        payload = gui.sheet_payload(gui.make_problem(dict(SETUP, skip="2026-11-11")))
        assert payload["description"] == "three combined shifts a day"
        assert payload["people"] == om.DEFAULT_PEOPLE
        assert [sh["code"] for sh in payload["shifts"]] == ["M", "A", "E"]
        assert payload["slots"][0] == {"id": "2026-11-02_M", "date": "2026-11-02", "weekday": "Mon", "short": "Mon Nov 2",
                                       "day": 0, "code": "M", "label": "8-12", "time": "08:00-12:00", "hours": 4.0,
                                       "gap_before": False}
        # gap_before marks only the first slot of a day that follows a gap: one "no coverage" row per gap
        gaps = [s["id"] for s in payload["slots"] if s["gap_before"]]
        assert gaps == ["2026-11-09_M", "2026-11-12_M"]
        assert payload["packages"] == [
            {"name": "heavy", "count": 3, "contents": "3 x M/A, 1 x E", "hours": 14.0},
            {"name": "light_evenings", "count": 3, "contents": "2 x M/A, 2 x E", "hours": 12.0},
            {"name": "light_days", "count": 1, "contents": "3 x M/A", "hours": 12.0},
        ]


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(base, path, body=None, raw=None):
    """Returns (status, parsed JSON or text, headers)."""
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req) as r:
            status, payload, headers = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, payload, headers = e.code, e.read(), e.headers
    ctype = headers.get("Content-Type", "")
    if ctype.startswith("application/json"):
        return status, json.loads(payload.decode("utf-8")), headers
    if ctype.startswith("text/"):
        return status, payload.decode("utf-8"), headers
    return status, payload, headers


class TestApi:
    def test_index_serves_the_page(self, server):
        status, body, headers = request(server, "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "<title>On-call market scheduler</title>" in body

    def test_unknown_paths(self, server):
        assert request(server, "/nope")[0] == 404
        assert request(server, "/api/nope", {})[:2] == (404, {"error": "Unknown action."})

    def test_unreadable_body(self, server):
        status, body, _ = request(server, "/api/sheet", raw=b"not json")
        assert (status, body) == (400, {"error": "The page sent something this program could not read."})

    def test_sheet(self, server):
        status, body, _ = request(server, "/api/sheet", {"setup": SETUP})
        assert status == 200
        assert body == gui.sheet_payload(gui.make_problem(SETUP))
        status, body, _ = request(server, "/api/sheet", {"setup": dict(SETUP, days="x")})
        assert (status, body) == (400, {"error": "Weekdays to cover must be a whole number."})

    def test_import_round_trip(self, server, example_text):
        status, body, _ = request(server, "/api/import", {"text": example_text})
        assert status == 200
        assert body["setup"] == {"structure": "three", "start": "2026-11-02", "days": 9, "skip": "",
                                 "people": ", ".join(om.DEFAULT_PEOPLE)}
        people, rows = om.parse_preferences(example_text)
        expect = {r["id"]: {p: c for p, c in zip(people, r["cells"]) if c} for r in rows}
        assert body["cells"] == expect
        assert body["cells"]["2026-11-02_M"] == {"Person B": "3", "Person E": "21", "Person F": "4"}
        assert body["cells"]["2026-11-03_M"] == {"Person B": "1", "Person C": "X", "Person D": "X", "Person E": "19",
                                                 "Person F": "X"}
        # and back out through /api/export: the same cells in the template format
        status, text, headers = request(server, "/api/export", {"setup": body["setup"], "cells": body["cells"]})
        assert status == 200
        assert headers["Content-Type"].startswith("text/csv")
        assert headers["Content-Disposition"] == "attachment; filename=preferences.csv"
        assert text.splitlines()[:6] == om.TEMPLATE_HEADER
        assert om.parse_preferences(text) == (people, rows)

    def test_import_detects_skipped_days(self, server):
        problem = gui.make_problem(dict(SETUP, skip="2026-11-11", people="Ann,Bob,Cy,Di,Ed,Flo,Gus"))
        status, body, _ = request(server, "/api/import", {"text": om.preferences_text(problem)})
        assert status == 200
        assert body["setup"] == {"structure": "three", "start": "2026-11-02", "days": 9, "skip": "2026-11-11",
                                 "people": "Ann, Bob, Cy, Di, Ed, Flo, Gus"}
        assert all(v == {} for v in body["cells"].values())

    def test_import_rejects_a_bad_sheet(self, server):
        status, body, _ = request(server, "/api/import", {"text": "garbage\n"})
        assert status == 400
        assert body["error"].startswith("Unexpected header")

    def test_download_before_any_solve(self, server):
        gui.LAST.clear()
        status, body, _ = request(server, "/api/download/schedule.csv")
        assert (status, body) == (404, "Solve first, then download.")

    def test_solve_errors(self, server, example_text):
        cells = request(server, "/api/import", {"text": example_text})[1]["cells"]
        status, body, _ = request(server, "/api/solve", {"setup": SETUP, "cells": cells, "roles_mode": "fixed",
                                                         "roles": {"Person A": "heavy"}})
        assert (status, body["error"]) == (400, "Pick a package for: " + ", ".join(om.DEFAULT_PEOPLE[1:]))
        status, body, _ = request(server, "/api/solve", {"setup": SETUP, "cells": cells, "roles_mode": "fixed",
                                                         "roles": {p: "nope" for p in om.DEFAULT_PEOPLE}})
        assert (status, body["error"]) == (400, "Unknown package 'nope' (choose from heavy, light_evenings, light_days)")
        status, body, _ = request(server, "/api/solve", {"setup": SETUP, "cells": cells, "mode": "weird"})
        assert (status, body["error"]) == (400, "Cells must contain ranks or points.")
        status, body, _ = request(server, "/api/solve", {"setup": SETUP, "cells": {"2026-11-02_M": {"Person A": "abc"}}})
        assert (status, body["error"]) == (400, "Person A: cannot read 'abc' for slot 2026-11-02_M "
                                                "(use a number, a blank, or X).")
        status, body, _ = request(server, "/api/solve", {"setup": dict(SETUP, people="Ann, Bob"), "cells": {}})
        assert status == 400 and body["error"].startswith("The packages are for 7 people but there are 2.")

    def test_solve_matches_the_command_line(self, server, example_text, example_solution, example_cli):
        cells = request(server, "/api/import", {"text": example_text})[1]["cells"]
        status, res, _ = request(server, "/api/solve", {"setup": SETUP, "mode": "rank", "roles_mode": "market",
                                                        "roles": {}, "cells": cells})
        assert status == 200
        sol = example_solution
        assert res["people"] == sol["summary"]
        assert res["report"] == sol["report"]
        assert res["cleared"] == sol["cleared"]
        assert res["pkg_prices"] == {k: round(v, 1) for k, v in sol["package_prices"].items()}
        assert res["schedule"]["columns"] == ["Date", "8-12", "1-5:30 pm", "6-8 pm"]
        people_by_slot = {r["slot_id"]: r["person"] for r in sol["schedule"]}
        assert res["schedule"]["rows"] == [
            ["Mon Nov 2"] + [people_by_slot["2026-11-02_" + c] for c in "MAE"],
            ["Tue Nov 3"] + [people_by_slot["2026-11-03_" + c] for c in "MAE"],
            ["Wed Nov 4"] + [people_by_slot["2026-11-04_" + c] for c in "MAE"],
            ["Thu Nov 5"] + [people_by_slot["2026-11-05_" + c] for c in "MAE"],
            ["Fri Nov 6"] + [people_by_slot["2026-11-06_" + c] for c in "MAE"],
            ["no coverage Sat Nov 7 - Sun Nov 8", "", "", ""],
            ["Mon Nov 9"] + [people_by_slot["2026-11-09_" + c] for c in "MAE"],
            ["Tue Nov 10"] + [people_by_slot["2026-11-10_" + c] for c in "MAE"],
            ["Wed Nov 11"] + [people_by_slot["2026-11-11_" + c] for c in "MAE"],
            ["Thu Nov 12"] + [people_by_slot["2026-11-12_" + c] for c in "MAE"],
        ]
        assert res["prices"]["columns"] == ["Date", "M", "A", "E"]
        assert res["prices"]["rows"][0] == ["Mon Nov 2"] + [round(sol["prices"]["2026-11-02_" + c], 1) for c in "MAE"]
        assert len(res["prices"]["rows"]) == 9
        if sol["cleared"]:
            assert res["status"].startswith("The market cleared exactly")
        else:
            assert res["status"].startswith("Prices support this assignment with a maximum regret of %.2f points"
                                            % sol["max_regret"])
        # the download links serve exactly the command line's files
        out_dir, _ = example_cli
        for name in ("schedule.csv", "summary.csv"):
            status, body, headers = request(server, "/api/download/" + name)
            assert status == 200
            assert headers["Content-Disposition"] == "attachment; filename=" + name
            assert body == (out_dir / name).read_bytes().decode("utf-8")

    def test_run_solve_with_fixed_roles(self, example_text):
        cells = {}
        people, rows = om.parse_preferences(example_text)
        for r in rows:
            cells[r["id"]] = {p: c for p, c in zip(people, r["cells"]) if c}
        res = gui.run_solve({"setup": SETUP, "mode": "rank", "roles_mode": "fixed", "roles": FIXED_ROLES, "cells": cells})
        assert res["pkg_prices"] is None
        assert {r["person"]: r["package"] for r in res["people"]} == FIXED_ROLES
        assert all(r["violations"] == 0 for r in res["people"])
        assert gui.LAST["solution"]["market_roles"] is False
