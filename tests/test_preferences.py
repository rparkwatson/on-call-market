"""CSV parsing and the rank-to-points conversion."""
import random
from datetime import date

import pytest

import oncall_market as om
from tests.helpers import default_problem, rows_for, slot_index

HEADER = ",".join(om.FIXED_COLUMNS)


def sheet(rows, people=("Ann", "Bob"), comments=()):
    """A small preferences CSV.  rows: (slot_id, date, code, *cells)."""
    lines = list(comments) + [HEADER + "," + ",".join(people)]
    for slot_id, d, code, *cells in rows:
        lines.append(",".join([slot_id, d, "Mon", code, "08:00-12:00", "4.0"] + list(cells)))
    return "\n".join(lines) + "\n"


class TestParsePreferences:
    def test_example_parses(self, example_text):
        people, rows = om.parse_preferences(example_text)
        assert people == om.DEFAULT_PEOPLE
        assert len(rows) == 27
        assert rows[0] == {"id": "2026-11-02_M", "date": date(2026, 11, 2), "code": "M",
                           "cells": ["", "3", "", "", "21", "4", ""]}
        assert rows[3]["cells"] == ["", "1", "X", "X", "19", "X", ""]
        assert {len(r["cells"]) for r in rows} == {7}

    def test_round_trip_reproduces_the_example(self, example_text):
        people, rows = om.parse_preferences(example_text)
        problem = om.Problem("three", sorted({r["date"] for r in rows}), people)
        cells = [[r["cells"][i] for r in rows] for i in range(len(people))]
        comments = [line for line in example_text.splitlines() if line.startswith("#")]
        text = om.preferences_text(problem, cells, comments)
        assert text.splitlines() == example_text.splitlines()
        assert om.parse_preferences(text) == (people, rows)

    def test_blank_template_round_trip(self):
        problem = default_problem()
        text = om.preferences_text(problem)
        assert text.splitlines()[:6] == om.TEMPLATE_HEADER
        people, rows = om.parse_preferences(text)
        assert people == om.DEFAULT_PEOPLE
        assert [r["id"] for r in rows] == [s["id"] for s in problem.slots]
        assert all(r["cells"] == [""] * 7 for r in rows)

    def test_read_preferences_accepts_a_byte_order_mark(self, tmp_path):
        path = tmp_path / "prefs.csv"
        path.write_bytes(b"\xef\xbb\xbf" + sheet([("2026-11-02_M", "2026-11-02", "M", "1", "X")]).encode("utf-8"))
        people, rows = om.read_preferences(str(path))
        assert people == ["Ann", "Bob"]
        assert rows[0]["cells"] == ["1", "X"]

    def test_comment_and_blank_lines_are_skipped(self):
        text = sheet([("2026-11-02_M", "2026-11-02", "M", "1", "X"),
                      ("2026-11-02_A", "2026-11-02", "A", "", "2")],
                     comments=["# a comment", "", "   "])
        text = text.replace("\n2026-11-02_A", '\n\n# another comment\n"# quoted by a spreadsheet",,,\n2026-11-02_A')
        people, rows = om.parse_preferences(text)
        assert people == ["Ann", "Bob"]
        assert [r["id"] for r in rows] == ["2026-11-02_M", "2026-11-02_A"]
        assert rows[0]["cells"] == ["1", "X"]
        assert rows[1]["cells"] == ["", "2"]

    def test_cells_are_stripped_and_short_rows_padded(self):
        text = sheet([("2026-11-02_M", "2026-11-02", "M", " 1 ", " x ")])
        text += "2026-11-02_A,2026-11-02,Mon,A,13:00-17:30,4.0\n"
        people, rows = om.parse_preferences(text)
        assert rows[0]["cells"] == ["1", "x"]
        assert rows[1]["cells"] == ["", ""]

    def test_header_whitespace_and_trailing_blank_columns_are_tolerated(self):
        text = HEADER + ", Ann ,Bob,,\n2026-11-02_M,2026-11-02,Mon,M,08:00-12:00,4.0,1,2,,\n"
        people, rows = om.parse_preferences(text)
        assert people == ["Ann", "Bob"]
        assert rows[0]["cells"] == ["1", "2"]

    @pytest.mark.parametrize("header", [
        "slot,date,weekday,shift,time,hours,Ann",
        "date,slot_id,weekday,shift,time,hours,Ann",
        "Ann,Bob",
    ])
    def test_bad_header(self, header):
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences(header + "\n2026-11-02_M,2026-11-02,Mon,M,08:00-12:00,4.0,1\n")
        assert str(exc.value) == ("Unexpected header; expected the first six columns to be "
                                  "slot_id, date, weekday, shift, time, hours")

    def test_bad_date(self):
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences(sheet([("2026-11-02_M", "2026-13-02", "M", "1", "")]))
        assert str(exc.value) == "Bad date '2026-13-02' in row 2026-11-02_M"

    def test_empty_file_and_no_people(self):
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences("# only a comment\n\n")
        assert str(exc.value) == "Preferences file is empty."
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences(HEADER + "\n")
        assert str(exc.value) == "No person columns found in the preferences file."

    def test_blank_middle_header_column_is_an_error_naming_the_column(self):
        # used to shift every later person's cells silently (found by T1)
        text = HEADER + ",Ann,,Bob\n2026-11-02_M,2026-11-02,Mon,M,08:00-12:00,4.0,1,,2\n"
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences(text)
        assert str(exc.value) == ("Header column 8 (between 'Ann' and 'Bob') is blank: every person column needs a name, "
                                  "and blank columns are only allowed at the end of the header.")
        with pytest.raises(om.SchedulingError) as exc:
            om.parse_preferences(HEADER + ",,,Bob\n")
        assert str(exc.value).startswith("Header column 7 (between 'hours' and 'Bob') is blank")


class TestInferStructure:
    @pytest.mark.parametrize("structure", ["single", "two", "three"])
    def test_infers_each_structure(self, structure):
        problem = default_problem(structure)
        people, rows = om.parse_preferences(om.preferences_text(problem))
        assert om.infer_structure(rows) == structure

    def test_unknown_codes(self):
        with pytest.raises(om.SchedulingError) as exc:
            om.infer_structure([{"id": "x", "date": date(2026, 11, 2), "code": "Z", "cells": []}])
        assert str(exc.value) == "Could not infer the shift structure from codes ['Z']; pass --structure."


class TestPreferenceValues:
    A, B = "Person A", "Person B"
    M1, A1, E1 = "2026-11-02_M", "2026-11-02_A", "2026-11-02_E"

    def values(self, cells_by_person, mode="rank", budgets=None, seed=1, structure="three"):
        problem = default_problem(structure)
        values, avail = om.preference_values(problem, rows_for(problem, cells_by_person), mode, random.Random(seed), budgets)
        return problem, values, avail

    def test_ranks_become_linear_points_scaled_to_100(self):
        problem, values, avail = self.values({self.A: {self.M1: "1", self.A1: "2", self.E1: "3"}})
        m, a, e = slot_index(problem, self.M1, self.A1, self.E1)
        assert values[0][m] == pytest.approx(50.0, abs=1e-3)
        assert values[0][a] == pytest.approx(100.0 / 3, abs=1e-3)
        assert values[0][e] == pytest.approx(100.0 / 6, abs=1e-3)
        assert values[0][m] > values[0][a] > values[0][e]
        assert sum(values[0][j] for j in (m, a, e)) == pytest.approx(100.0, abs=1e-2)
        assert all(0 <= values[0][j] < 1e-3 for j in range(len(problem.slots)) if j not in (m, a, e))
        assert all(avail[0])

    def test_only_the_order_of_ranks_matters(self):
        _, v1, _ = self.values({self.A: {self.M1: "1", self.A1: "2", self.E1: "3"}})
        _, v2, _ = self.values({self.A: {self.M1: "1", self.A1: "5", self.E1: "20"}})
        _, v3, _ = self.values({self.A: {self.M1: "2.5", self.A1: "7", self.E1: "100"}})
        assert v1 == v2 == v3  # same seed, so even the tie-break noise is identical

    def test_equal_ranks_share_a_value(self):
        problem, values, _ = self.values({self.A: {self.M1: "1", self.A1: "1", self.E1: "2"}})
        m, a, e = slot_index(problem, self.M1, self.A1, self.E1)
        assert values[0][m] == pytest.approx(40.0, abs=1e-3)
        assert values[0][a] == pytest.approx(40.0, abs=1e-3)
        assert values[0][e] == pytest.approx(20.0, abs=1e-3)

    def test_points_mode_scales_and_clamps_negatives_to_zero(self):
        problem, values, avail = self.values({self.A: {self.M1: "10", self.A1: "-5", self.E1: "30"}}, mode="points")
        m, a, e = slot_index(problem, self.M1, self.A1, self.E1)
        assert values[0][m] == pytest.approx(25.0, abs=1e-3)
        assert values[0][a] == pytest.approx(0.0, abs=1e-3)
        assert values[0][e] == pytest.approx(75.0, abs=1e-3)
        assert all(avail[0])

    def test_points_mode_all_zero_is_left_unscaled(self):
        problem, values, _ = self.values({self.A: {self.M1: "0", self.A1: "0"}}, mode="points")
        assert all(0 <= v < 1e-3 for v in values[0])

    def test_blank_column_gives_only_tie_break_noise_and_full_availability(self):
        problem, values, avail = self.values({})
        for i in range(len(problem.people)):
            assert all(0 <= v < 1e-3 for v in values[i])
            assert all(avail[i])

    def test_budget_scales_that_persons_total(self):
        cells = {self.A: {self.M1: "1", self.A1: "2"}, self.B: {self.M1: "1", self.A1: "2"}}
        problem, values, _ = self.values(cells, budgets={self.A: 130.0})
        m, a = slot_index(problem, self.M1, self.A1)
        assert values[0][m] + values[0][a] == pytest.approx(130.0, abs=1e-2)
        assert values[1][m] + values[1][a] == pytest.approx(100.0, abs=1e-2)
        assert values[0][m] == pytest.approx(130.0 * 2 / 3, abs=1e-3)

    @pytest.mark.parametrize("token", sorted(om.UNAVAILABLE_TOKENS))
    @pytest.mark.parametrize("case", [str.lower, str.upper, str.capitalize])
    def test_every_unavailable_token_in_any_case(self, token, case):
        problem, values, avail = self.values({self.A: {self.M1: case(token), self.A1: "1"}})
        m, a = slot_index(problem, self.M1, self.A1)
        assert avail[0][m] is False
        assert values[0][m] < 1e-3
        assert avail[0][a] is True
        assert values[0][a] == pytest.approx(100.0, abs=1e-3)

    def test_unreadable_cell(self):
        with pytest.raises(om.SchedulingError) as exc:
            self.values({self.A: {self.M1: "abc"}})
        assert str(exc.value) == "Person A: cannot read 'abc' for slot 2026-11-02_M (use a number, a blank, or X)."

    def test_slot_not_in_the_schedule(self):
        problem = default_problem()
        rows = rows_for(problem, {}) + [{"id": "2026-11-02_Z", "date": date(2026, 11, 2), "code": "Z", "cells": [""] * 7}]
        with pytest.raises(om.SchedulingError) as exc:
            om.preference_values(problem, rows, "rank", random.Random(1))
        assert str(exc.value) == "Slot 2026-11-02_Z in the preferences file is not part of the schedule."

    def test_noise_is_deterministic_per_seed(self):
        _, v1, _ = self.values({self.A: {self.M1: "1"}}, seed=1)
        _, v2, _ = self.values({self.A: {self.M1: "1"}}, seed=1)
        _, v3, _ = self.values({self.A: {self.M1: "1"}}, seed=2)
        assert v1 == v2
        assert v1 != v3
