"""Shared fixtures.  The solves of the shipped example take about 7 s each, so they are
session-scoped and shared by the end-to-end, command-line and GUI tests."""
import contextlib
import io
import os

import pytest

import oncall_market as om

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PATH = os.path.join(ROOT, "preferences_example.csv")


@pytest.fixture(scope="session")
def example_path():
    return EXAMPLE_PATH


@pytest.fixture(scope="session")
def example_text():
    with open(EXAMPLE_PATH, encoding="utf-8-sig") as f:
        return f.read()


@pytest.fixture(scope="session")
def example_solution(example_text):
    """solve_sheet() on the shipped example with every default (seed 1)."""
    return om.solve_sheet(example_text)


@pytest.fixture(scope="session")
def example_result(example_text):
    """prepare_sheet() + solve() on the example with every default: (problem, values, avail, result).
    A second, independent run of the solver on the same input as example_solution."""
    problem, values, avail = om.prepare_sheet(example_text)
    return problem, values, avail, om.solve(problem, values, avail)


@pytest.fixture(scope="session")
def example_cli(example_path, tmp_path_factory):
    """`solve preferences_example.csv --out-dir <tmp> --quiet` through main(); returns (out_dir, stdout)."""
    out_dir = tmp_path_factory.mktemp("cli")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        om.main(["solve", example_path, "--out-dir", str(out_dir), "--quiet"])
    return out_dir, buf.getvalue()
