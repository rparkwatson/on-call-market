"""Non-responders (F4) and the min_ranks warnings (F3): flagged and reported, never an error."""
import csv
import io
import json

import pytest

import oncall_market as om
from tests.helpers import check_schedule, default_problem, rows_for

FAST = dict(iters=100, sa_iters=10000, restarts=1)


def blank_column(text, person):
    """The example sheet with one person's column emptied (no ranks, no X's)."""
    people, _ = om.parse_preferences(text)
    i = people.index(person)
    out = []
    for line in text.splitlines():
        if not line.startswith("#") and not line.startswith("slot_id"):
            cells = line.split(",")
            cells[6 + i] = ""
            line = ",".join(cells)
        out.append(line)
    return "\n".join(out) + "\n"


def rank_counts(text):
    people, rows = om.parse_preferences(text)
    return {p: sum(1 for r in rows if r["cells"][i] not in ("", "X")) for i, p in enumerate(people)}


@pytest.fixture(scope="module")
def blank_g(example_text):
    """Person G's column emptied, solved with min_ranks=10 (several people rank fewer)."""
    text = blank_column(example_text, "Person G")
    return text, om.solve_sheet(text, min_ranks=10, **FAST)


class TestNonResponder:
    def test_is_flagged_with_null_metrics(self, blank_g):
        text, sol = blank_g
        rows = {r["person"]: r for r in sol["summary"]}
        g = rows["Person G"]
        assert g["responded"] is False
        assert g["pct_of_ideal"] is None and g["net_price"] is None and g["next_budget_suggestion"] is None
        assert g["points_received"] == 0.0 and g["shifts"] >= 3
        for p, r in rows.items():
            if p != "Person G":
                assert r["responded"] is True
                assert isinstance(r["pct_of_ideal"], float) and isinstance(r["net_price"], float)
        assert json.loads(json.dumps(sol)) == sol
        check_schedule(default_problem(), sol)

    def test_text_report_says_so(self, blank_g):
        _, sol = blank_g
        line = next(ln for ln in sol["report"].splitlines() if ln.startswith("  Person G "))
        assert "pts (n/a of ideal)  net price    n/a" in line
        lines = sol["report"].splitlines()
        assert lines[lines.index(line) + 2].strip() == "no preferences submitted"
        assert "WARNINGS" in sol["report"]
        assert "  - Person G submitted no preferences (no ranks and no X's); their metrics are not meaningful." in sol["report"]

    def test_csv_writes_n_a(self, blank_g):
        _, sol = blank_g
        rows = list(csv.DictReader(io.StringIO(om.summary_csv_text(sol))))
        g = next(r for r in rows if r["person"] == "Person G")
        assert (g["pct_of_ideal"], g["net_price"], g["next_budget_suggestion"], g["responded"]) == ("n/a", "n/a", "n/a", "False")
        assert list(rows[0].keys())[-1] == "responded"
        assert all(r["responded"] == "True" for r in rows if r["person"] != "Person G")

    def test_non_responder_is_left_out_of_the_tie_break_minimum(self, blank_g):
        text, sol = blank_g
        problem, values, avail = om.prepare_sheet(text)
        assert problem.responded == [True] * 6 + [False]
        ideal = om.ideal_points(problem, values, avail, True, None)
        chosen = next(a for a in sol["alternatives"] if a["chosen"])
        index = {p: i for i, p in enumerate(problem.people)}
        owner = [index[p] for p in chosen["owner"]]
        roles = [chosen["roles"][p] for p in problem.people]
        prices = [sol["prices"][s["id"]] for s in problem.slots]
        key = om.tie_break_key(problem, values, roles, owner, prices, sol["package_prices"], True, ideal, problem.responded)
        assert -key[0] == pytest.approx(min(r["pct_of_ideal"] for r in sol["summary"] if r["responded"]), abs=0.06)


class TestMinRanks:
    def test_warnings_name_everyone_below_the_minimum(self, blank_g, example_text):
        text, sol = blank_g
        counts = rank_counts(example_text)
        expect = []
        for p, n in counts.items():  # sheet order
            if p == "Person G":
                expect.append("Person G submitted no preferences (no ranks and no X's); their metrics are not meaningful.")
            elif n < 10:
                expect.append("%s ranked %d slot%s; the minimum is 10." % (p, n, "" if n == 1 else "s"))
        assert sol["warnings"] == expect
        assert len(expect) >= 2
        problem, _, _ = om.prepare_sheet(text, min_ranks=10)
        assert problem.warnings == sol["warnings"]

    def test_no_warnings_when_everyone_responded_and_no_minimum(self, example_solution):
        assert example_solution["warnings"] == []
        assert all(r["responded"] is True for r in example_solution["summary"])
        assert "WARNINGS" not in example_solution["report"]
        assert "n/a" not in example_solution["report"]
        assert "n/a" not in om.summary_csv_text(example_solution)

    def test_x_only_column_counts_as_a_response_with_no_ranks(self):
        problem = default_problem()
        rows = rows_for(problem, {"Person A": {"2026-11-02_M": "X"}, "Person B": {"2026-11-02_M": "0", "2026-11-03_M": "2"}})
        responded, warnings = om.response_check(problem.people, rows, "rank", min_ranks=1)
        assert responded == [True, True] + [False] * 5
        assert warnings[0] == "Person A ranked 0 slots; the minimum is 1."
        assert warnings[1:] == ["%s submitted no preferences (no ranks and no X's); their metrics are not meaningful." % p
                                for p in om.DEFAULT_PEOPLE[2:]]
        responded, warnings = om.response_check(problem.people, rows, "points", min_ranks=2)
        assert responded[1] is True and warnings[1] == "Person B ranked 1 slot; the minimum is 2."  # a 0-point cell is no rank

    def test_command_line_option(self, example_path, tmp_path, capsys):
        om.main(["solve", example_path, "--out-dir", str(tmp_path), "--quiet", "--min-ranks", "25",
                 "--iters", "50", "--sa-iters", "2000", "--restarts", "1"])
        out = capsys.readouterr().out
        assert "WARNINGS" in out and "; the minimum is 25." in out
        assert (tmp_path / "summary.csv").read_text(encoding="utf-8").splitlines()[0].endswith(",responded")
