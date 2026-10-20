"""Command-line smoke tests through main([...])."""
import csv

import pytest

import oncall_market as om


def fails(argv):
    with pytest.raises(SystemExit) as exc:
        om.main(argv)
    assert exc.value.code == 1


class TestTemplate:
    def test_writes_a_blank_sheet_with_the_documented_header(self, tmp_path, capsys):
        out = tmp_path / "preferences.csv"
        om.main(["template", "--out", str(out)])
        text = out.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[:6] == om.TEMPLATE_HEADER
        assert lines[6] == ",".join(om.FIXED_COLUMNS + om.DEFAULT_PEOPLE)
        assert lines[7] == "2026-11-02_M,2026-11-02,Mon,M,08:00-12:00,4.0,,,,,,,"
        assert len(lines) == 7 + 27
        people, rows = om.parse_preferences(text)
        assert people == om.DEFAULT_PEOPLE
        assert all(r["cells"] == [""] * 7 for r in rows)
        assert capsys.readouterr().out == "Wrote %s: 27 slots x 7 people (three combined shifts a day, three).\n" % out

    def test_calendar_options(self, tmp_path):
        out = tmp_path / "t.csv"
        om.main(["template", "--out", str(out), "--structure", "two", "--start", "2026-11-04", "--days", "9",
                 "--skip", "2026-11-11", "2026-11-12", "--people", "Ann, Bob,Cy,Di,Ed,Flo,Gus"])
        people, rows = om.parse_preferences(out.read_text(encoding="utf-8"))
        assert people == ["Ann", "Bob", "Cy", "Di", "Ed", "Flo", "Gus"]
        assert om.infer_structure(rows) == "two"
        dates = sorted({r["date"].isoformat() for r in rows})
        assert dates == ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-13",
                         "2026-11-16", "2026-11-17", "2026-11-18"]

    def test_wrong_headcount_exits_with_the_packages_message(self, tmp_path, capsys):
        fails(["template", "--out", str(tmp_path / "t.csv"), "--people", "A,B,C,D,E,F"])
        err = capsys.readouterr().err
        assert err.startswith("The packages are for 7 people but there are 6.")
        assert not (tmp_path / "t.csv").exists()


class TestDemo:
    def test_writes_a_filled_sheet(self, tmp_path, capsys):
        out = tmp_path / "demo.csv"
        om.main(["demo", "--out", str(out), "--structure", "two"])
        text = out.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == "# EXAMPLE preferences (made up) - ranks 1, 2, 3 ... and X = unavailable."
        assert lines[1:6] == om.TEMPLATE_HEADER[1:]
        people, rows = om.parse_preferences(text)
        assert people == om.DEFAULT_PEOPLE
        assert om.infer_structure(rows) == "two"
        cells = {c for r in rows for c in r["cells"]}
        assert "X" in cells and "1" in cells
        assert capsys.readouterr().out == "Wrote %s with example rankings for 7 people.\n" % out

    def test_seed_changes_the_cells(self, tmp_path):
        a, b = tmp_path / "a.csv", tmp_path / "b.csv"
        om.main(["demo", "--out", str(a), "--seed", "1"])
        om.main(["demo", "--out", str(b), "--seed", "2"])
        assert a.read_text(encoding="utf-8") != b.read_text(encoding="utf-8")


class TestSolve:
    def test_writes_the_two_csvs_with_the_documented_columns(self, example_cli):
        out_dir, stdout = example_cli
        with open(out_dir / "schedule.csv", newline="", encoding="utf-8") as f:
            sched = list(csv.reader(f))
        with open(out_dir / "summary.csv", newline="", encoding="utf-8") as f:
            summ = list(csv.reader(f))
        assert sched[0] == ["slot_id", "date", "weekday", "shift", "time", "hours", "person", "clearing_price"]
        assert len(sched) == 1 + 27
        assert sched[1][:6] == ["2026-11-02_M", "2026-11-02", "Mon", "M", "08:00-12:00", "4.0"]
        assert sched[1][6] in om.DEFAULT_PEOPLE
        float(sched[1][7])
        assert summ[0] == ["person", "package", "shifts", "hours", "points_received", "pct_of_ideal", "net_price",
                           "next_budget_suggestion", "violations", "slots"]
        assert [r[0] for r in summ[1:]] == om.DEFAULT_PEOPLE

    def test_quiet_stdout_has_the_summary_line_report_and_file_names(self, example_cli):
        out_dir, stdout = example_cli
        lines = stdout.splitlines()
        assert lines[0] == ("Structure: three (three combined shifts a day); 27 slots over 9 days; 7 people; "
                            "rank mode; roles decided by the market.")
        assert lines[1] == ""
        assert lines[2] == "SCHEDULE"
        assert "CLEARING PRICES" in stdout
        assert lines[-1] == "Wrote %s/schedule.csv and %s/summary.csv" % (out_dir, out_dir)
        assert "Finding clearing prices" not in stdout

    def test_bad_sheet_exits_1(self, tmp_path, capsys):
        bad = tmp_path / "bad.csv"
        bad.write_text("slot,date\n1,2\n", encoding="utf-8")
        fails(["solve", str(bad)])
        assert capsys.readouterr().err.startswith("Unexpected header; expected the first six columns to be")

    def test_roles_fixed_needs_a_complete_valid_map(self, example_path, capsys):
        fails(["solve", example_path, "--roles", "fixed"])
        assert capsys.readouterr().err.strip() == "--roles-map is missing: " + ", ".join(om.DEFAULT_PEOPLE)
        fails(["solve", example_path, "--roles", "fixed", "--roles-map", "Person A=nope"])
        assert capsys.readouterr().err.strip() == ("--roles-map: unknown package 'nope' "
                                                   "(choose from heavy, light_evenings, light_days)")
        fails(["solve", example_path, "--roles", "fixed", "--roles-map", "Nobody=heavy"])
        assert capsys.readouterr().err.strip() == "--roles-map: unknown person 'Nobody'"
        fails(["solve", example_path, "--roles", "fixed", "--roles-map", "garbage"])
        assert capsys.readouterr().err.strip() == "--roles-map entries look like 'Person A=heavy'"

    def test_budgets_file_must_name_people_in_the_sheet(self, example_path, tmp_path, capsys):
        budgets = tmp_path / "budgets.csv"
        budgets.write_text("person,budget\nPerson A,130\nZed,90\n", encoding="utf-8")
        fails(["solve", example_path, "--budgets", str(budgets)])
        assert capsys.readouterr().err.strip() == "Budgets name people not in the preferences file: Zed"

    def test_packages_file_is_validated(self, example_path, tmp_path, capsys):
        packages = tmp_path / "packages.json"
        packages.write_text('{"heavy": {"count": 6, "cats": {"four": [3, 3], "eve": [1, 1]}}}', encoding="utf-8")
        fails(["solve", example_path, "--packages", str(packages)])
        assert capsys.readouterr().err.startswith("The packages are for 6 people but there are 7.")

    def test_infeasible_sheet_reports_the_short_handed_day(self, example_text, tmp_path, capsys):
        people, rows = om.parse_preferences(example_text)
        lines = example_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("2026-11-12_"):
                lines[i] = ",".join(line.split(",")[:6] + ["X"] * 7)
        sheet = tmp_path / "short.csv"
        sheet.write_text("\n".join(lines) + "\n", encoding="utf-8")
        fails(["solve", str(sheet)])
        err = capsys.readouterr().err
        assert "nobody is available for Thu Nov 12 M" in err
        assert "Thu Nov 12 has 3 slots but the people available that day can cover only 0" in err
