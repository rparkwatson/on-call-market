"""Bridge between the stored cycle data and oncall_market.  No Streamlit imports.

The solver is reached only through oncall_market's library entry point: prepare_sheet,
solve_sheet and solution_dict (the solve_sheet dict is the one solver interface, see its
docstring), plus schedule_csv_text and summary_csv_text for the downloads.

Packages come from packages.derive_packages(structure, n_people, days, max_per_day=1) for
the responders.  When it cannot derive a set for this size, get_packages() reports why and
the organiser can paste a packages JSON instead (the tests also exercise the built-in
packages for 7 people over 9 weekdays with the module forced absent).

Non-responders (decision Q6) are excluded: the problem is built for the responders only.
"""

from __future__ import annotations

import copy
import csv
import importlib
import inspect
import io
import json
from collections import Counter
from datetime import date, timedelta

import oncall_market as om
from oncall_market import prepare_sheet, solve_sheet, solution_dict, schedule_csv_text, summary_csv_text  # noqa: F401

DEFAULT_SEEDS = (1,)       # run_market solves once; the tuple stays for callers that still pass seeds
EQUIVALENT_POINTS = 0.05   # outcomes whose total points are within this are shown as equivalent
MAX_ALTERNATIVES = 24      # candidates kept per run: the market's choice plus the first 23 alternatives
REGRET_OK = 0.1            # README: below this the prices are as good as exact clearing prices

STRUCTURE_LABELS = {
    "three": "Three shifts a day: 8-12, 1-5:30, 6-8",
    "two": "Two shifts a day: 8-12, 1-8",
    "single": "Five 2-hour blocks a day: 8-10, 10-12, 1-3, 3:30-5:30, 6-8",
}


# ---------------------------------------------------------------------------
# calendar and sheet helpers (no Problem needed, so they work before packages exist)
# ---------------------------------------------------------------------------

def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def cycle_dates(cycle):
    skip = {as_date(d) for d in (cycle.get("skip_dates") or [])}
    return om.weekdays(as_date(cycle["start_date"]), int(cycle["n_days"]), skip)


def default_min_ranks(structure):
    """Decision Q7: the largest package size for the structure plus two."""
    largest = max(int(pk["total"][1]) for pk in om.STRUCTURES[structure]["packages"].values())
    return largest + 2


def slot_table(cycle):
    """One dict per slot in schedule order, with the same ids om.Problem gives them."""
    st = om.STRUCTURES[cycle["structure"]]
    dates = cycle_dates(cycle)
    slots = []
    for di, d in enumerate(dates):
        gap = di > 0 and (dates[di] - dates[di - 1]).days > 1
        for k, sh in enumerate(st["shifts"]):
            slots.append({
                "id": "%s_%s" % (d.isoformat(), sh["code"]), "day": di, "date": d,
                "weekday": d.strftime("%a"), "short": om.fmt_date(d), "code": sh["code"],
                "label": sh["label"], "time": sh["time"], "hours": sh["hours"],
                "first_of_day": k == 0, "gap_before": gap and k == 0,
            })
    return slots


def slot_label(slot):
    """'Wed Nov 4 8-12 (M)' - used in edit descriptions and selectors."""
    return "%s %s (%s)" % (om.fmt_date(slot["date"]), slot["label"], slot["code"])


class _Sheet:
    """The two attributes om.preferences_text reads, without om.Problem's package check."""

    def __init__(self, people, slots):
        self.people = list(people)
        self.slots = slots


def sheet_text(cycle, people, submissions, header_lines=om.TEMPLATE_HEADER):
    """The preferences CSV (CLI format) for these people, via om.preferences_text."""
    slots = slot_table(cycle)
    cells = [[str((submissions.get(p) or {}).get(s["id"], "") or "") for s in slots] for p in people]
    return om.preferences_text(_Sheet(people, slots), cells, header_lines)


def preferences_csv_text(cycle, people, submissions, excluded=()):
    """preferences.csv for download: the CLI format plus a note on who was excluded."""
    header = list(om.TEMPLATE_HEADER)
    if excluded:
        header.append("# Excluded from this cycle (no submission by the deadline): " + ", ".join(excluded))
    return sheet_text(cycle, people, submissions, header)


def split_responders(participants, submissions):
    """(responders, excluded names): a responder is a participant with a saved submission."""
    responders = [p for p in participants if p["name"] in submissions]
    excluded = [p["name"] for p in participants if p["name"] not in submissions]
    return responders, excluded


def budgets_of(responders):
    """{name: budget} for anyone whose budget is not the default, or None."""
    out = {p["name"]: float(p["budget"]) for p in responders if abs(float(p["budget"]) - om.BUDGET) > 1e-9}
    return out or None


# ---------------------------------------------------------------------------
# packages
# ---------------------------------------------------------------------------

PASTE_PACKAGES = ("Paste a packages JSON below (the shape shown under 'Changing the system' in README.md) "
                  "and save it.")


def _derive_packages_function():
    """packages.derive_packages(structure, n_people, days, max_per_day=1); None only when the
    module is absent (the tests force that to reach the built-in packages)."""
    try:
        module = importlib.import_module("packages")
    except ImportError:
        return None
    return getattr(module, "derive_packages", None)


def _check_packages(structure, dates, people, packages, max_per_day):
    """None if om.Problem accepts the packages for this cycle, else the error message."""
    try:
        om.Problem(structure, dates, people, packages, max_per_day=max_per_day)
    except om.SchedulingError as e:
        return str(e)
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        return "the packages are not in the expected shape (%s)" % e
    return None


def _no_packages(message):
    return {"packages": None, "source": None, "message": message}


def get_packages(structure, responders, dates, max_per_day=1, override_json=None):
    """The packages to solve with, for the RESPONDERS.

    Returns {"packages": dict or None, "source": str or None, "message": str}.
    Order: a pasted override; packages.derive_packages (when it raises, its message plus an
    invitation to paste a packages JSON); without the module, the built-in packages when there
    are exactly 7 responders and 9 weekdays, otherwise None with the same invitation."""
    names = [p["name"] if isinstance(p, dict) else str(p) for p in responders]
    n, days = len(names), len(dates)
    if override_json:
        try:
            packages = json.loads(override_json) if isinstance(override_json, str) else dict(override_json)
        except ValueError as e:
            return _no_packages("The pasted packages JSON could not be read: %s" % e)
        err = _check_packages(structure, dates, names, packages, max_per_day)
        if err:
            return _no_packages("The pasted packages do not fit this cycle: %s" % err)
        return {"packages": packages, "source": "organiser override",
                "message": "Using the packages pasted by the organiser."}
    derive = _derive_packages_function()
    if derive is not None:
        try:
            packages = derive(structure, n, days, max_per_day=max_per_day)
        except om.SchedulingError as e:
            return _no_packages("packages.py could not derive packages for %d people over %d weekdays: %s %s"
                                % (n, days, str(e).rstrip(".") + ".", PASTE_PACKAGES))
        err = _check_packages(structure, dates, names, packages, max_per_day)
        if err:
            return _no_packages("packages.py returned packages that do not fit this cycle: %s %s" % (err, PASTE_PACKAGES))
        return {"packages": packages, "source": "packages.py",
                "message": "Packages derived automatically for %d people over %d weekdays." % (n, days)}
    if n == 7 and days == 9:
        packages = copy.deepcopy(om.STRUCTURES[structure]["packages"])
        err = _check_packages(structure, dates, names, packages, max_per_day)
        if err:
            return _no_packages("The built-in packages do not fit this cycle: %s" % err)
        return {"packages": packages, "source": "built-in",
                "message": "Using the built-in packages for 7 people over 9 weekdays."}
    return _no_packages(
        "Automatic packages are not available yet for %d people over %d weekdays: the built-in packages "
        "need exactly 7 people and 9 weekdays. %s" % (n, days, PASTE_PACKAGES))


def package_descriptions(structure, packages):
    """Table rows (package, people, contents, hours) describing a package set."""
    st = om.STRUCTURES[structure]
    cats = st["categories"]
    hours_of = {sh["code"]: sh["hours"] for sh in st["shifts"]}
    rows = []
    for name, pk in packages.items():
        parts, lo_h, hi_h = [], 0.0, 0.0
        for cat, (lo, hi) in pk["cats"].items():
            if int(hi) == 0:
                continue
            codes = "/".join(cats[cat])
            parts.append(("%d" % lo if lo == hi else "%d-%d" % (lo, hi)) + " x " + codes)
            lo_h += int(lo) * hours_of[cats[cat][0]]
            hi_h += int(hi) * hours_of[cats[cat][0]]
        hours = "%g" % lo_h if lo_h == hi_h else "%g-%g" % (lo_h, hi_h)
        rows.append({"package": name, "people": int(pk["count"]), "contents": ", ".join(parts), "hours": hours})
    return rows


# ---------------------------------------------------------------------------
# running the market
# ---------------------------------------------------------------------------

class _ZeroRng:
    """Stands in for random.Random so that om.preference_values adds no tie-break noise."""

    def uniform(self, _a, _b):
        return 0.0


def exact_point_values(problem, text, mode, budgets):
    """Point values without the tiny tie-break noise, for comparing outcomes fairly."""
    _, slot_rows = om.parse_preferences(text)
    values, _ = om.preference_values(problem, slot_rows, mode, _ZeroRng(), budgets)
    return values


def status_line(solution):
    """How the market ended, in one line: the clearing status, then every warning (indicative
    prices, a rule violation, the solver's own warnings about min_ranks) each introduced by
    " WARNING: " so that the dashboard can show them apart."""
    supported = solution.get("prices_supported", True) is not False
    if solution["cleared"]:
        status = ("The market cleared exactly in %d rounds: everyone holds the bundle they would pick for "
                  "themselves at these prices." % solution["rounds"])
    elif supported:
        status = ("Prices support this assignment with a maximum regret of %.2f points "
                  "(0 = exact equilibrium; under %.1f is as good as exact)." % (solution["max_regret"], REGRET_OK))
    else:
        status = ("The prices leave a maximum regret of %.2f points (0 = exact equilibrium; under %.1f is as "
                  "good as exact). WARNING: Prices are indicative only: the regret exceeds %g points."
                  % (solution["max_regret"], REGRET_OK, om.REGRET_SUPPORTED))
    if any(r["violations"] for r in solution["summary"]):
        status += (" WARNING: the best schedule found still breaks a rule - usually the X's leave a day "
                   "short-handed. Allow 2 shifts per day or ask for fewer X's.")
    for warning in solution.get("warnings") or []:
        status += " WARNING: " + warning
    return status


def candidate_from_solution(solution, seed):
    """A JSON-serialisable candidate outcome: the solve_sheet dict plus the owner list, the
    per-person rows the organiser compares, the solver's warnings and a status line."""
    people = solution["people"]
    index = {p: i for i, p in enumerate(people)}
    owner = [index[r["person"]] for r in solution["schedule"]]
    late_code = om.STRUCTURES[solution["structure"]]["late"]
    late = Counter(r["person"] for r in solution["schedule"] if r["shift"] == late_code)
    person_rows = [{
        "person": row["person"], "package": row["package"], "hours": row["hours"], "shifts": row["shifts"],
        "late_shifts": late.get(row["person"], 0), "points": row["points_received"],
        "pct_of_ideal": row["pct_of_ideal"], "net_price": row["net_price"], "violations": row["violations"],
    } for row in solution["summary"]]
    return {
        "seeds": [seed], "people": list(people), "structure": solution["structure"], "dates": solution["dates"],
        "owner": owner, "roles": {row["person"]: row["package"] for row in solution["summary"]},
        "schedule": solution["schedule"], "summary": solution["summary"], "person_rows": person_rows,
        "prices": solution["prices"], "package_prices": solution["package_prices"],
        "cleared": solution["cleared"], "rounds": solution["rounds"], "max_regret": solution["max_regret"],
        "prices_supported": solution.get("prices_supported", True) is not False,
        "market_roles": solution["market_roles"], "report": solution["report"],
        "warnings": list(solution.get("warnings") or []),
        "status": status_line(solution), "violations": sum(r["violations"] for r in solution["summary"]),
    }


def _result_at_prices(source, roles, owner):
    """An om.solve()-shaped result: these roles (one per person, sheet order) and owners (person
    index per slot) valued at the prices, rounds, regret and clearing status of `source`, a
    solve_sheet dict or a candidate.  Used for the equivalent alternatives of a run and for
    hand edits, so that every schedule is compared at the same prices."""
    result = {"roles": list(roles), "owner": list(owner),
              "prices": [source["prices"][r["slot_id"]] for r in source["schedule"]],
              "pkg_prices": dict(source["package_prices"] or {}),
              "cleared": source["cleared"], "rounds": source["rounds"],
              "max_regret": source["max_regret"], "market_roles": bool(source["market_roles"])}
    if source.get("prices_supported") is not None:
        result["prices_supported"] = bool(source["prices_supported"])
    return result


def alternative_result(solution, alt):
    """The result for one entry of solution["alternatives"], at the solution's prices."""
    people = solution["people"]
    index = {p: i for i, p in enumerate(people)}
    return _result_at_prices(solution, [alt["roles"][p] for p in people], [index[p] for p in alt["owner"]])


def _with_min_ranks(function, kwargs, min_ranks):
    """kwargs plus min_ranks when the cycle has one and `function` takes that keyword."""
    if min_ranks is None:
        return kwargs
    params = inspect.signature(function).parameters
    if "min_ranks" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return dict(kwargs, min_ranks=int(min_ranks))
    return kwargs


def run_market(cycle, participants, submissions, packages, seeds=DEFAULT_SEEDS, log=None):
    """Solve once for the responders and return one candidate per welfare-equivalent schedule.

    cycle: a repo cycle dict; participants: repo participant dicts (name, budget);
    submissions: {name: cells}; packages: the dict get_packages() returned.
    seeds: kept for compatibility; only the first seed is used.  The point values and the
    clearing prices do not depend on the seed, and one solve reports every equivalent
    schedule it found in solution["alternatives"], so re-solving with other seeds adds nothing.
    Returns {"candidates": [the market's choice first, then the equivalent alternatives in the
             solver's order], "n_alternatives_found": int, "responders": [names],
             "excluded": [names], "packages": packages, "budgets": {...}, "text": preferences CSV text}.
    At most MAX_ALTERNATIVES candidates are kept (the market's choice plus the first
    MAX_ALTERNATIVES - 1 alternatives in the solver's order); "n_alternatives_found" is how many
    equivalent schedules the solve reported, the market's choice included, so that the
    dashboard can say when some were dropped.
    Every candidate is valued at the same prices (the market's); its "warnings" and
    "prices_supported" come from the solver and are repeated in its status line.
    Raises om.SchedulingError for anything the organiser has to fix."""
    responders, excluded = split_responders(participants, submissions)
    if not responders:
        raise om.SchedulingError("Nobody has submitted preferences, so there is nothing to solve.")
    people = [p["name"] for p in responders]
    budgets = budgets_of(responders)
    text = sheet_text(cycle, people, submissions)
    seed = int(seeds[0]) if seeds else 1
    kwargs = dict(structure=cycle["structure"], mode=cycle["mode"], packages=packages,
                  max_per_day=int(cycle["max_per_day"]), budgets=budgets, seed=seed)
    min_ranks = cycle.get("min_ranks")
    problem, values, avail = prepare_sheet(text, **_with_min_ranks(prepare_sheet, kwargs, min_ranks))
    exact = exact_point_values(problem, text, cycle["mode"], budgets)
    if log:
        log("Solving (seed %d) ..." % seed)
    solution = solve_sheet(text, log=log, **_with_min_ranks(solve_sheet, kwargs, min_ranks))
    chosen = ([r["person"] for r in solution["schedule"]], {r["person"]: r["package"] for r in solution["summary"]})
    alternatives = [alt for alt in solution.get("alternatives") or []
                    if not (alt.get("chosen") or (list(alt["owner"]), dict(alt["roles"])) == chosen)]
    n_found = 1 + len(alternatives)
    solutions = [solution] + [solution_dict(problem, values, avail, alternative_result(solution, alt))
                              for alt in alternatives[:MAX_ALTERNATIVES - 1]]
    candidates = []
    for k, sol in enumerate(solutions):
        cand = candidate_from_solution(sol, seed)
        cand["total_points"] = round(sum(exact[cand["owner"][j]][j] for j in range(len(cand["owner"]))), 3)
        cand["label"] = "Outcome 1 (the market's choice)" if k == 0 else "Outcome %d (equivalent alternative)" % (k + 1)
        candidates.append(cand)
    best = max(c["total_points"] for c in candidates)
    for cand in candidates:
        cand["equivalent_to_best"] = (best - cand["total_points"]) <= EQUIVALENT_POINTS
    return {"candidates": candidates, "n_alternatives_found": n_found, "responders": people, "excluded": excluded,
            "packages": packages, "budgets": budgets or {}, "text": text}


# ---------------------------------------------------------------------------
# review and edit a chosen outcome
# ---------------------------------------------------------------------------

class Context:
    """The problem, point values and availability behind a candidate, rebuilt from the
    stored data (the point values do not depend on the seed, so they are the run's)."""

    def __init__(self, cycle, responders, submissions, packages, seed=1):
        self.people = [p["name"] for p in responders]
        self.budgets = budgets_of(responders)
        self.mode = cycle["mode"]
        self.text = sheet_text(cycle, self.people, submissions)
        self.problem, self.values, self.avail = prepare_sheet(
            self.text, structure=cycle["structure"], mode=cycle["mode"], packages=packages,
            max_per_day=int(cycle["max_per_day"]), budgets=self.budgets, seed=int(seed))
        self.slots = self.problem.slots


def build_context(cycle, responders, submissions, packages, seed=1):
    return Context(cycle, responders, submissions, packages, seed)


def result_for_owner(candidate, owner):
    """An om.solve()-shaped result for the candidate's prices with a (possibly edited) owner list."""
    return _result_at_prices(candidate, [candidate["roles"][p] for p in candidate["people"]], owner)


def solution_for_owner(ctx, candidate, owner):
    """The solve_sheet-shaped dict for an edited owner list (schedule, summary rows, report)."""
    return solution_dict(ctx.problem, ctx.values, ctx.avail, result_for_owner(candidate, owner))


def person_rows(ctx, candidate, owner):
    """(rows, solution): per-person package, hours, shifts, late shifts, points, % of ideal,
    net price, violations and whether the slots still fit the package, after edits."""
    solution = solution_for_owner(ctx, candidate, owner)
    problem = ctx.problem
    late = Counter(r["person"] for r in solution["schedule"] if r["shift"] == problem.late)
    rows = []
    for i, row in enumerate(solution["summary"]):
        pk = problem.packages[row["package"]]
        mine = [j for j, o in enumerate(owner) if o == i]
        counts = [0] * len(problem.cats)
        for j in mine:
            counts[problem.slots[j]["cat"]] += 1
        fits = all(pk["min"][c] <= counts[c] <= pk["max"][c] for c in range(len(problem.cats))) \
            and pk["total"][0] <= len(mine) <= pk["total"][1]
        rows.append({
            "person": row["person"], "package": row["package"], "hours": row["hours"], "shifts": row["shifts"],
            "late_shifts": late.get(row["person"], 0), "points": row["points_received"],
            "pct_of_ideal": row["pct_of_ideal"], "net_price": row["net_price"],
            "violations": row["violations"], "package_ok": fits,
        })
    return rows, solution


def swap_slots(ctx, owner, j, k):
    """Swap the people on slots j and k.  Returns (new owner list, description)."""
    if j == k or owner[j] == owner[k]:
        raise om.SchedulingError("Pick two slots held by different people.")
    new = list(owner)
    new[j], new[k] = owner[k], owner[j]
    a, b = ctx.people[owner[j]], ctx.people[owner[k]]
    description = "Swapped %s: %s -> %s; %s: %s -> %s" % (
        slot_label(ctx.slots[j]), a, b, slot_label(ctx.slots[k]), b, a)
    return new, description


def assign_slot(ctx, owner, j, person_index):
    """Give slot j to another responder.  Returns (new owner list, description)."""
    if owner[j] == person_index:
        raise om.SchedulingError("%s already has that slot." % ctx.people[person_index])
    new = list(owner)
    new[j] = person_index
    description = "Assigned %s to %s (was %s)" % (slot_label(ctx.slots[j]), ctx.people[person_index], ctx.people[owner[j]])
    return new, description


# ---------------------------------------------------------------------------
# presentation helpers (pure)
# ---------------------------------------------------------------------------

def grid_from_schedule(schedule_rows, structure):
    """{"columns": ["Date", shift labels...], "rows": [[date, names...], ...]}, with a
    'no coverage' row where the schedule skips days (weekends are not shown)."""
    st = om.STRUCTURES[structure]
    labels = [sh["label"] for sh in st["shifts"]]
    codes = [sh["code"] for sh in st["shifts"]]
    by_date = {}
    for r in schedule_rows:
        by_date.setdefault(r["date"], {})[r["shift"]] = r["person"]
    rows, prev = [], None
    for iso in sorted(by_date):
        d = date.fromisoformat(iso)
        if prev is not None and (d - prev).days > 1 and any(x.weekday() < 5 for x in _between(prev, d)):
            first, last = prev + timedelta(days=1), d - timedelta(days=1)
            gap = om.fmt_date(first) if first == last else "%s - %s" % (om.fmt_date(first), om.fmt_date(last))
            rows.append(["no coverage %s" % gap] + [""] * len(labels))
        rows.append([om.fmt_date(d)] + [by_date[iso].get(code, "") for code in codes])
        prev = d
    return {"columns": ["Date"] + labels, "rows": rows}


def _between(a, b):
    d = a + timedelta(days=1)
    while d < b:
        yield d
        d += timedelta(days=1)


def grid_table(grid):
    """The grid as a list of dicts (what st.table takes)."""
    return [dict(zip(grid["columns"], row)) for row in grid["rows"]]


def tie_rule_from_report(report):
    """The tie-break rule as the solution report states it ("Ties: N equivalent schedules
    (...); chosen by <rule>."), or None when the report has no such line."""
    for line in (report or "").splitlines():
        line = line.strip()
        if line.startswith("Ties:") and "; chosen by " in line:
            return line.split("; chosen by ", 1)[1].rstrip(".")
    return None


def edits_csv_text(edits):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["when_utc", "description"])
    for e in edits:
        when = e.get("created_at") or e.get("when") or ""
        w.writerow([when.strftime("%Y-%m-%d %H:%M:%S") if hasattr(when, "strftime") else str(when), e["description"]])
    return buf.getvalue()
