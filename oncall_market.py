#!/usr/bin/env python3
"""
oncall_market.py - preference-ranked, market-cleared on-call scheduling
=======================================================================

Standard library only (Python 3.8+).

Three steps:

  1. python oncall_market.py template --structure three --out preferences.csv
        Writes a blank sign-up sheet: one row per slot, one column per person.
        Each person fills in their column with ranks (1 = favourite slot,
        2 = next, ...), leaves a blank for "available, no preference", and
        writes X for "unavailable - do not schedule me".

  2. python oncall_market.py solve preferences.csv
        Turns the ranks into point weights (everyone gets the same 100 points),
        finds a clearing price for every slot (and every package, if roles are
        left to the market), assigns the shifts, and writes schedule.csv and
        summary.csv.

  python oncall_market.py demo --out preferences_example.csv
        Writes a filled-in example you can solve straight away.

Structures (--structure):
  single  five separate 2-hour blocks a day   (8-10, 10-12, 1-3, 3:30-5:30, 6-8)
  two     two combined shifts a day           (8-12, 1-8)
  three   three combined shifts a day         (8-12, 1-5:30, 6-8)

The fairness system is built in for the 9-weekday / 7-person case: fixed
"packages" (how many shifts of each kind a person takes), at most one shift per
person per day, and no 8 pm finish followed by an 8 am start.  Packages can be
replaced with --packages packages.json (see README.md).
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import itertools
import json
import math
import random
import sys
from datetime import date, timedelta

BUDGET = 100.0  # every person's preferences are scaled to this many points

# Tie handling (README, "How the market works").  Schedules whose total preference points are
# within TIE_TOLERANCE of the best are welfare-equivalent; the final one is chosen by TIE_RULE.
TIE_TOLERANCE = 0.05
TIE_RULE = ("the highest minimum % of ideal, then the smallest spread of net price, then the fewest "
            "late shifts for the person with the most hours, then the first in slot order")
MAX_ALTERNATIVES = 40      # equivalent schedules kept
MAX_TIE_EXPANSIONS = 12    # equivalent schedules whose pair-move neighbours are explored
NOISE = 1e-6               # per-slot tie-break noise added to the point values
NOISE_SEED = 1             # fixed (the default seed), so prices and the search inputs do not depend on the solve seed


class SchedulingError(Exception):
    """A problem with the inputs that the user has to fix (reported without a traceback)."""
UNAVAILABLE_TOKENS = {"x", "xx", "na", "n/a", "no", "-", "unavailable", "out"}
NEG = float("-inf")

# ---------------------------------------------------------------------------
# Shift structures and the built-in fairness packages (9 weekdays, 7 people)
# ---------------------------------------------------------------------------

STRUCTURES = {
    "single": {
        "description": "five separate 2-hour blocks a day",
        "shifts": [
            {"code": "B1", "label": "8-10 am", "time": "08:00-10:00", "hours": 2.0},
            {"code": "B2", "label": "10 am-12", "time": "10:00-12:00", "hours": 2.0},
            {"code": "B3", "label": "1-3 pm", "time": "13:00-15:00", "hours": 2.0},
            {"code": "B4", "label": "3:30-5:30 pm", "time": "15:30-17:30", "hours": 2.0},
            {"code": "B5", "label": "6-8 pm", "time": "18:00-20:00", "hours": 2.0},
        ],
        "categories": {"B1": ["B1"], "B2": ["B2"], "B3": ["B3"], "B4": ["B4"], "B5": ["B5"]},
        "late": "B5",
        "early": ["B1"],
        # heavy7: 7 blocks, one of each slot type plus two extra midday blocks
        # light6: 6 blocks, one of each slot type plus one extra 8 am or evening
        "packages": {
            "heavy7": {"count": 3, "cats": {"B1": [1, 1], "B2": [1, 2], "B3": [1, 2], "B4": [1, 2], "B5": [1, 1]}, "total": [7, 7]},
            "light6": {"count": 4, "cats": {"B1": [1, 2], "B2": [1, 1], "B3": [1, 1], "B4": [1, 1], "B5": [1, 2]}, "total": [6, 6]},
        },
    },
    "two": {
        "description": "two combined shifts a day",
        "shifts": [
            {"code": "M", "label": "8-12", "time": "08:00-12:00", "hours": 4.0},
            {"code": "L", "label": "1-8 pm", "time": "13:00-20:00", "hours": 6.0},
        ],
        "categories": {"M": ["M"], "L": ["L"]},
        "late": "L",
        "early": ["M"],
        "packages": {
            "heavy": {"count": 3, "cats": {"M": [2, 2], "L": [1, 1]}, "total": [3, 3]},
            "light_long": {"count": 3, "cats": {"M": [0, 0], "L": [2, 2]}, "total": [2, 2]},
            "light_mornings": {"count": 1, "cats": {"M": [3, 3], "L": [0, 0]}, "total": [3, 3]},
        },
    },
    "three": {
        "description": "three combined shifts a day",
        "shifts": [
            {"code": "M", "label": "8-12", "time": "08:00-12:00", "hours": 4.0},
            {"code": "A", "label": "1-5:30 pm", "time": "13:00-17:30", "hours": 4.0},
            {"code": "E", "label": "6-8 pm", "time": "18:00-20:00", "hours": 2.0},
        ],
        "categories": {"four": ["M", "A"], "eve": ["E"]},
        "late": "E",
        "early": ["M"],
        "packages": {
            "heavy": {"count": 3, "cats": {"four": [3, 3], "eve": [1, 1]}, "total": [4, 4]},
            "light_evenings": {"count": 3, "cats": {"four": [2, 2], "eve": [2, 2]}, "total": [4, 4]},
            "light_days": {"count": 1, "cats": {"four": [3, 3], "eve": [0, 0]}, "total": [3, 3]},
        },
    },
}

DEFAULT_PEOPLE = ["Person A", "Person B", "Person C", "Person D", "Person E", "Person F", "Person G"]


def weekdays(start, n, skip=()):
    """n weekdays starting at `start`, skipping weekends and any dates in `skip`."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def fmt_date(d):
    return d.strftime("%a %b ") + str(d.day)


# ---------------------------------------------------------------------------
# Problem definition
# ---------------------------------------------------------------------------

class Problem:
    def __init__(self, structure, dates, people, packages_override=None, max_per_day=1):
        st = copy.deepcopy(STRUCTURES[structure])
        if packages_override:
            st["packages"] = packages_override
        self.structure = structure
        self.max_per_day = max(1, int(max_per_day))
        self.st = st
        self.dates = list(dates)
        self.people = list(people)
        self.shifts = st["shifts"]
        self.cats = list(st["categories"].keys())
        self.cat_of_code = {}
        for ci, (cat, codes) in enumerate(st["categories"].items()):
            for code in codes:
                self.cat_of_code[code] = ci
        self.late = st["late"]
        self.early = set(st["early"])

        self.slots = []
        self.slots_by_day = [[] for _ in self.dates]
        for di, d in enumerate(self.dates):
            for sh in self.shifts:
                j = len(self.slots)
                self.slots.append({
                    "id": "%s_%s" % (d.isoformat(), sh["code"]),
                    "day": di, "date": d, "code": sh["code"],
                    "cat": self.cat_of_code[sh["code"]],
                    "hours": sh["hours"], "label": sh["label"], "time": sh["time"],
                })
                self.slots_by_day[di].append(j)
        # consecutive[d] is True when day d follows day d-1 on the calendar (no weekend/holiday between)
        self.consecutive = [False] + [
            (self.dates[d] - self.dates[d - 1]).days == 1 for d in range(1, len(self.dates))
        ]

        self.packages = {}
        for name, pk in st["packages"].items():
            mins = [0] * len(self.cats)
            maxs = [0] * len(self.cats)
            for cat, (lo, hi) in pk["cats"].items():
                if cat not in self.cats:
                    raise SchedulingError("Package %s refers to unknown category %r" % (name, cat))
                ci = self.cats.index(cat)
                mins[ci], maxs[ci] = int(lo), int(hi)
            total = pk.get("total", [sum(mins), sum(maxs)])
            self.packages[name] = {"count": int(pk["count"]), "min": mins, "max": maxs,
                                   "total": (int(total[0]), int(total[1]))}
        self.supply = [0] * len(self.cats)
        for s in self.slots:
            self.supply[s["cat"]] += 1
        self.validate()

    def validate(self):
        n = len(self.people)
        total_people = sum(p["count"] for p in self.packages.values())
        if total_people != n:
            raise SchedulingError(
                "The packages are for %d people but there are %d. The built-in packages assume 7 people and "
                "9 weekdays; for other sizes supply your own packages (see README.md)." % (total_people, n))
        for ci, cat in enumerate(self.cats):
            lo = sum(p["count"] * p["min"][ci] for p in self.packages.values())
            hi = sum(p["count"] * p["max"][ci] for p in self.packages.values())
            if not (lo <= self.supply[ci] <= hi):
                raise SchedulingError(
                    "Category %r has %d slots but the packages call for between %d and %d. The built-in packages "
                    "assume 9 weekdays; for other periods supply your own packages (see README.md)." % (cat, self.supply[ci], lo, hi))

    # -- a person's best response to prices -------------------------------------------
    def _package_tables(self, pkg):
        """Per-package state encoding (cached)."""
        if not hasattr(self, "_ptab"):
            self._ptab = {}
        if pkg not in self._ptab:
            pk = self.packages[pkg]
            maxs = pk["max"]
            C = len(self.cats)
            mult = [1] * C
            for c in range(1, C):
                mult[c] = mult[c - 1] * (maxs[c - 1] + 1)
            S = mult[-1] * (maxs[-1] + 1)
            decoded = [tuple((s // mult[c]) % (maxs[c] + 1) for c in range(C)) for s in range(S)]
            self._ptab[pkg] = (mult, S, decoded, {})
        return self._ptab[pkg]

    def _day_options(self, i, pkg, d, avail_i):
        """Ways person i can spend day d under package pkg (cached; independent of prices)."""
        if not hasattr(self, "_dopt"):
            self._dopt = {}
        key = (i, pkg, d)
        if key in self._dopt:
            return self._dopt[key]
        mult, S, decoded, valid_cache = self._package_tables(pkg)
        maxs = self.packages[pkg]["max"]
        C = len(self.cats)
        today = [j for j in self.slots_by_day[d] if avail_i[j]]
        options = [((), 0, False, False, range(S))]
        for size in range(1, min(self.max_per_day, len(today)) + 1):
            for combo in itertools.combinations(today, size):
                delta = [0] * C
                inc = 0
                for j in combo:
                    c = self.slots[j]["cat"]
                    delta[c] += 1
                    inc += mult[c]
                delta = tuple(delta)
                if any(delta[c] > maxs[c] for c in range(C)):
                    continue
                if delta not in valid_cache:
                    valid_cache[delta] = [s for s in range(S)
                                          if all(decoded[s][c] + delta[c] <= maxs[c] for c in range(C))]
                codes = {self.slots[j]["code"] for j in combo}
                options.append((combo, inc, self.late in codes, bool(codes & self.early), valid_cache[delta]))
        self._dopt[key] = options
        return options

    def best_response(self, i, pkg, prices, values, avail):
        """Best bundle for person i under package pkg at the given prices.

        Maximises sum(value - price) subject to: package counts, at most
        max_per_day shifts per day, availability, and no late shift followed by
        an early shift the next calendar day.  Returns (value, [slot indices])
        or (None, None) if nothing feasible exists.
        """
        pk = self.packages[pkg]
        mins = pk["min"]
        tmin, tmax = pk["total"]
        C = len(self.cats)
        mult, S, decoded, _ = self._package_tables(pkg)
        vals_i = values[i]
        val = [[NEG] * S, [NEG] * S]
        val[0][0] = 0.0
        backs = []
        for d in range(len(self.dates)):
            options = self._day_options(i, pkg, d, avail[i])
            nval = [[NEG] * S, [NEG] * S]
            nback = [[None] * S, [None] * S]
            consec = self.consecutive[d]
            for f in (0, 1):
                vf = val[f]
                for combo, inc, has_late, has_early, valid in options:
                    if f and consec and has_early:
                        continue
                    gain = 0.0
                    for j in combo:
                        gain += vals_i[j] - prices[j]
                    nv = nval[1 if has_late else 0]
                    nb = nback[1 if has_late else 0]
                    for s in valid:
                        v = vf[s]
                        if v == NEG:
                            continue
                        v2 = v + gain
                        s2 = s + inc
                        if v2 > nv[s2]:
                            nv[s2] = v2
                            nb[s2] = (f, s, combo)
            val = nval
            backs.append(nback)
        best, bf, bs = NEG, None, None
        for f in (0, 1):
            for s in range(S):
                v = val[f][s]
                if v == NEG:
                    continue
                counts = decoded[s]
                if any(counts[c] < mins[c] for c in range(C)):
                    continue
                if not (tmin <= sum(counts) <= tmax):
                    continue
                if v > best:
                    best, bf, bs = v, f, s
        if bf is None:
            return None, None
        bundle = []
        f, s = bf, bs
        for d in range(len(self.dates) - 1, -1, -1):
            pf, ps, combo = backs[d][f][s]
            bundle.extend(combo)
            f, s = pf, ps
        bundle.sort()
        return best, bundle

    # -- constraint violations of an assigned set of slots --------------------------
    def violations(self, i, slots_i, avail):
        by_day = {}
        v = 0
        for j in slots_i:
            if not avail[i][j]:
                v += 1
            sl = self.slots[j]
            by_day.setdefault(sl["day"], []).append(sl["code"])
        for d, codes in by_day.items():
            v += max(0, len(codes) - self.max_per_day)
            if self.late in codes and (d + 1) in by_day and self.consecutive[d + 1]:
                if any(c in self.early for c in by_day[d + 1]):
                    v += 1
        return v


# ---------------------------------------------------------------------------
# Preferences file
# ---------------------------------------------------------------------------

FIXED_COLUMNS = ["slot_id", "date", "weekday", "shift", "time", "hours"]

TEMPLATE_HEADER = [
    "# ON-CALL PREFERENCES - one column per person, one row per slot.",
    "# In your column write your rank for each slot: 1 = the slot you want most, 2 = next, and so on.",
    "# Rank as many or as few slots as you like; only the order matters, not the gaps between numbers.",
    "# Leave a cell blank if you are available but have no preference.",
    "# Write X if you are UNAVAILABLE for that slot - you will never be scheduled there.",
    "# (If the organiser runs the solver with --mode points, write points instead of ranks; higher = want more.)",
]


def preferences_text(problem, cells=None, header_lines=TEMPLATE_HEADER):
    """The preferences sheet as CSV text (comment lines, header, one row per slot)."""
    buf = io.StringIO()
    for line in header_lines:
        buf.write(line + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIXED_COLUMNS + problem.people)
    for j, s in enumerate(problem.slots):
        row = [s["id"], s["date"].isoformat(), s["date"].strftime("%a"), s["code"], s["time"], s["hours"]]
        row += [cells[i][j] if cells else "" for i in range(len(problem.people))]
        w.writerow(row)
    return buf.getvalue()


def write_preferences(path, problem, cells=None, header_lines=TEMPLATE_HEADER):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(preferences_text(problem, cells, header_lines))


def parse_preferences(text):
    """Parse preferences CSV text.  Returns (people, slot_rows)."""
    lines = []
    for line in text.splitlines(True):
        if line.lstrip().lstrip('"').startswith("#") or not line.strip():
            continue
        lines.append(line)
    rows = list(csv.reader(lines))
    if not rows:
        raise SchedulingError("Preferences file is empty.")
    header = [h.strip() for h in rows[0]]
    if header[:6] != FIXED_COLUMNS:
        raise SchedulingError("Unexpected header; expected the first six columns to be %s" % ", ".join(FIXED_COLUMNS))
    people = [h for h in header[6:] if h]
    if not people:
        raise SchedulingError("No person columns found in the preferences file.")
    slot_rows = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        r = [c.strip() for c in r]
        r += [""] * (6 + len(people) - len(r))
        try:
            d = date.fromisoformat(r[1])
        except ValueError:
            raise SchedulingError("Bad date %r in row %s" % (r[1], r[0]))
        slot_rows.append({"id": r[0], "date": d, "code": r[3], "cells": r[6:6 + len(people)]})
    return people, slot_rows


def read_preferences(path):
    with open(path, encoding="utf-8-sig") as f:
        return parse_preferences(f.read())


def infer_structure(slot_rows):
    codes = {r["code"] for r in slot_rows}
    for name, st in STRUCTURES.items():
        if codes == {sh["code"] for sh in st["shifts"]}:
            return name
    raise SchedulingError("Could not infer the shift structure from codes %s; pass --structure." % sorted(codes))


def preference_values(problem, slot_rows, mode, rng, budgets=None):
    """Convert cells to point values (each person scaled to their budget, default BUDGET)
    and availability."""
    n, m = len(problem.people), len(problem.slots)
    index = {s["id"]: j for j, s in enumerate(problem.slots)}
    values = [[0.0] * m for _ in range(n)]
    avail = [[True] * m for _ in range(n)]
    raw = [dict() for _ in range(n)]
    for r in slot_rows:
        j = index.get(r["id"])
        if j is None:
            raise SchedulingError("Slot %s in the preferences file is not part of the schedule." % r["id"])
        for i, c in enumerate(r["cells"]):
            if c == "":
                continue
            if c.lower() in UNAVAILABLE_TOKENS:
                avail[i][j] = False
                continue
            try:
                raw[i][j] = float(c)
            except ValueError:
                raise SchedulingError("%s: cannot read %r for slot %s (use a number, a blank, or X)." % (problem.people[i], c, r["id"]))
    for i in range(n):
        if mode == "rank":
            distinct = sorted(set(raw[i].values()))
            pos = {v: k for k, v in enumerate(distinct)}  # 0 = best
            R = len(distinct)
            for j, v in raw[i].items():
                values[i][j] = float(R - pos[v])
        else:
            for j, v in raw[i].items():
                values[i][j] = max(0.0, v)
        budget = (budgets or {}).get(problem.people[i], BUDGET)
        tot = sum(values[i][j] for j in raw[i])
        if tot > 0:
            for j in raw[i]:
                values[i][j] *= budget / tot
        for j in range(m):  # tiny deterministic tie-break noise
            values[i][j] += rng.uniform(0.0, NOISE)
    return values, avail


def response_check(people, slot_rows, mode, min_ranks=None):
    """(responded, warnings).  responded[i] is False for a column with no ranks and no X's (a
    non-responder: the solver flags them, the app excludes them).  warnings are plain sentences
    for the report: every non-responder and, when min_ranks is given, everyone who ranked fewer
    slots (in points mode a cell counts as a rank when its points are above zero)."""
    n = len(people)
    ranks, xs = [0] * n, [0] * n
    for r in slot_rows:
        for i, c in enumerate(r["cells"][:n]):
            if c == "":
                continue
            if c.lower() in UNAVAILABLE_TOKENS:
                xs[i] += 1
                continue
            try:
                v = float(c)
            except ValueError:
                continue  # preference_values reports unreadable cells
            if mode == "rank" or v > 0:
                ranks[i] += 1
    responded = [ranks[i] > 0 or xs[i] > 0 for i in range(n)]
    warnings = []
    for i, p in enumerate(people):
        if not responded[i]:
            warnings.append("%s submitted no preferences (no ranks and no X's); their metrics are not meaningful." % p)
        elif min_ranks is not None and ranks[i] < min_ranks:
            warnings.append("%s ranked %d slot%s; the minimum is %d." % (p, ranks[i], "" if ranks[i] == 1 else "s", min_ranks))
    return responded, warnings


def check_feasibility(problem, values, avail):
    """Catch the obvious impossibilities before running the market."""
    n = len(problem.people)
    problems = []
    for j, s in enumerate(problem.slots):
        if not any(avail[i][j] for i in range(n)):
            problems.append("nobody is available for %s %s" % (fmt_date(s["date"]), s["code"]))
    for d, dt in enumerate(problem.dates):
        need = len(problem.slots_by_day[d])
        capacity = sum(min(problem.max_per_day, sum(1 for j in problem.slots_by_day[d] if avail[i][j])) for i in range(n))
        if capacity < need:
            problems.append("%s has %d slots but the people available that day can cover only %d "
                            "(at most %d shift(s) per person per day; try --max-per-day %d or fewer X's)"
                            % (fmt_date(dt), need, capacity, problem.max_per_day, problem.max_per_day + 1))
    zero = [0.0] * len(problem.slots)
    for i in range(n):
        if all(problem.best_response(i, k, zero, values, avail)[0] is None for k in problem.packages):
            problems.append("%s has marked too many slots X to fit any package" % problem.people[i])
    if problems:
        raise SchedulingError("Cannot build a schedule:\n  - " + "\n  - ".join(problems))


# ---------------------------------------------------------------------------
# Market: tatonnement on slot prices (and package prices)
# ---------------------------------------------------------------------------

def tatonnement(problem, values, avail, roles, iters, step0, decay, log=None):
    """Adjust prices until each slot is demanded by exactly one person.

    Each round every person picks the bundle (and, if roles are not fixed, the
    package) that maximises value minus price.  Over-demanded slots get more
    expensive, unwanted slots get cheaper (prices may go negative: that slot
    pays you).  Returns (prices, package_prices, choices, cleared, rounds).
    """
    n, m = len(problem.people), len(problem.slots)
    pkgs = list(problem.packages.keys())
    supply = {k: problem.packages[k]["count"] for k in pkgs}
    p = [0.0] * m
    pi = {k: 0.0 for k in pkgs}
    best = None
    for t in range(iters):
        demand = [0] * m
        pdem = {k: 0 for k in pkgs}
        choices = []
        dual = 0.0
        for i in range(n):
            cands = [roles[i]] if roles else pkgs
            bk, bv, bb = None, NEG, None
            for k in cands:
                v, b = problem.best_response(i, k, p, values, avail)
                if v is None:
                    continue
                surplus = v - pi[k]
                if surplus > bv:
                    bk, bv, bb = k, surplus, b
            if bk is None:
                raise SchedulingError("%s has no feasible set of shifts for package(s) %s given their unavailable slots."
                                 % (problem.people[i], ", ".join(cands)))
            choices.append((bk, bb))
            dual += bv
            for j in bb:
                demand[j] += 1
            pdem[bk] += 1
        dual += sum(p) + sum(supply[k] * pi[k] for k in pkgs)
        excess = [demand[j] - 1 for j in range(m)]
        pexcess = {k: pdem[k] - supply[k] for k in pkgs}
        if best is None or dual < best[0]:
            best = (dual, list(p), dict(pi), list(choices))
        if all(e == 0 for e in excess) and all(e == 0 for e in pexcess.values()):
            if log:
                log("  market cleared exactly after %d rounds" % (t + 1))
            return p, pi, choices, True, t + 1
        step = step0 / (1.0 + t / decay)
        for j in range(m):
            p[j] += step * excess[j]
        if not roles:
            for k in pkgs:
                pi[k] += step * pexcess[k]
        if log and (t + 1) % 100 == 0:
            over = sum(1 for e in excess if e > 0)
            under = sum(1 for e in excess if e < 0)
            log("  round %d: %d slots over-demanded, %d unclaimed" % (t + 1, over, under))
    if log:
        log("  no exact clearing in %d rounds; using the best price vector found" % iters)
    return best[1], best[2], best[3], False, iters


def candidate_roles(problem, values, avail, prices, pkg_prices, top=5):
    """The `top` ways of handing out packages, ranked by total surplus at the final prices."""
    n = len(problem.people)
    pkgs = list(problem.packages.keys())
    surplus = []
    for i in range(n):
        row = {}
        for k in pkgs:
            v, _ = problem.best_response(i, k, prices, values, avail)
            if v is not None:
                row[k] = v - pkg_prices[k]
        surplus.append(row)
    remaining = {k: problem.packages[k]["count"] for k in pkgs}
    found = []

    def rec(i, current, total):
        if i == n:
            found.append((total, list(current)))
            return
        for k in pkgs:
            if remaining[k] > 0 and k in surplus[i]:
                remaining[k] -= 1
                current.append(k)
                rec(i + 1, current, total + surplus[i][k])
                current.pop()
                remaining[k] += 1

    rec(0, [], 0.0)
    if not found:
        raise SchedulingError("No way to hand out the packages that respects everyone's unavailable slots.")
    found.sort(key=lambda x: -x[0])
    return [r for _, r in found[:top]]


def support_prices(problem, values, avail, roles, owner, prices, pkg_prices, market_roles, iters, step0, decay):
    """Nudge prices so that the final assignment is each person's own best choice.

    Raises the price of anything a person would rather have and lowers the price
    of what they hold but would drop, until no one can do better by switching.
    Returns (prices, package_prices, max_regret).
    """
    n, m = len(problem.people), len(problem.slots)
    pkgs = list(problem.packages.keys())
    mine = [set() for _ in range(n)]
    for j, i in enumerate(owner):
        mine[i].add(j)
    p, pi = list(prices), dict(pkg_prices)
    best = (float("inf"), list(p), dict(pi))
    for t in range(iters):
        grad = [0] * m
        pgrad = {k: 0 for k in pkgs}
        max_reg = 0.0
        for i in range(n):
            own = roles[i]
            held = sum(values[i][j] - p[j] for j in mine[i]) - (pi[own] if market_roles else 0.0)
            bk, bv, bb = own, NEG, None
            for k in ([own] if not market_roles else pkgs):
                v, b = problem.best_response(i, k, p, values, avail)
                if v is None:
                    continue
                s = v - (pi[k] if market_roles else 0.0)
                if s > bv:
                    bk, bv, bb = k, s, b
            reg = bv - held
            if reg > max_reg:
                max_reg = reg
            if reg > 1e-6:
                want = set(bb)
                for j in want - mine[i]:
                    grad[j] += 1
                for j in mine[i] - want:
                    grad[j] -= 1
                if bk != own:
                    pgrad[bk] += 1
                    pgrad[own] -= 1
        if max_reg < best[0]:
            best = (max_reg, list(p), dict(pi))
        if max_reg < 0.01:
            break
        step = step0 / (1.0 + t / decay)
        for j in range(m):
            p[j] += step * grad[j]
        if market_roles:
            for k in pkgs:
                pi[k] += step * pgrad[k]
    return best[1], best[2], best[0]


# ---------------------------------------------------------------------------
# Assignment: simulated annealing over feasible exact covers
# ---------------------------------------------------------------------------

def initial_counts(problem, roles, rng):
    n, C = len(problem.people), len(problem.cats)
    for _ in range(500):
        counts = [list(problem.packages[roles[i]]["min"]) for i in range(n)]
        ok = True
        for c in range(C):
            need = problem.supply[c] - sum(counts[i][c] for i in range(n))
            while need > 0:
                cands = [i for i in range(n)
                         if counts[i][c] < problem.packages[roles[i]]["max"][c]
                         and sum(counts[i]) < problem.packages[roles[i]]["total"][1]]
                if not cands:
                    ok = False
                    break
                i = rng.choice(cands)
                counts[i][c] += 1
                need -= 1
            if not ok or need < 0:
                ok = False
                break
        if ok and all(problem.packages[roles[i]]["total"][0] <= sum(counts[i]) <= problem.packages[roles[i]]["total"][1]
                      for i in range(n)):
            return counts
    raise SchedulingError("Could not find a split of the slots that satisfies the packages.")


def anneal(problem, values, avail, roles, iters, rng, t0=8.0, t1=0.02, penalty=1000.0):
    n, m, C = len(problem.people), len(problem.slots), len(problem.cats)
    counts = initial_counts(problem, roles, rng)
    owner = [None] * m
    by_cat = [[] for _ in range(C)]
    for j, s in enumerate(problem.slots):
        by_cat[s["cat"]].append(j)
    for c in range(C):
        pool = list(by_cat[c])
        rng.shuffle(pool)
        k = 0
        for i in range(n):
            for _ in range(counts[i][c]):
                owner[pool[k]] = i
                k += 1
    mine = [set() for _ in range(n)]
    for j in range(m):
        mine[owner[j]].add(j)

    def score(i):
        return sum(values[i][j] for j in mine[i]) - penalty * problem.violations(i, mine[i], avail)

    cur = sum(score(i) for i in range(n))
    best_val, best_owner = cur, list(owner)
    for it in range(iters):
        T = t0 * (t1 / t0) ** (it / max(1, iters - 1))
        j = rng.randrange(m)
        a = owner[j]
        c = problem.slots[j]["cat"]
        if rng.random() < 0.7:
            pool = by_cat[c]
            k = pool[rng.randrange(len(pool))]
            b = owner[k]
            if b == a:
                continue
            before = score(a) + score(b)
            mine[a].discard(j); mine[a].add(k); mine[b].discard(k); mine[b].add(j)
            owner[j], owner[k] = b, a
            delta = score(a) + score(b) - before
            if delta >= 0 or rng.random() < math.exp(delta / T):
                cur += delta
            else:
                mine[a].discard(k); mine[a].add(j); mine[b].discard(j); mine[b].add(k)
                owner[j], owner[k] = a, b
        else:
            b = rng.randrange(n)
            if b == a:
                continue
            pa, pb = problem.packages[roles[a]], problem.packages[roles[b]]
            if counts[a][c] - 1 < pa["min"][c] or sum(counts[a]) - 1 < pa["total"][0]:
                continue
            if counts[b][c] + 1 > pb["max"][c] or sum(counts[b]) + 1 > pb["total"][1]:
                continue
            before = score(a) + score(b)
            mine[a].discard(j); mine[b].add(j); owner[j] = b
            delta = score(a) + score(b) - before
            if delta >= 0 or rng.random() < math.exp(delta / T):
                cur += delta
                counts[a][c] -= 1
                counts[b][c] += 1
            else:
                mine[b].discard(j); mine[a].add(j); owner[j] = a
        if cur > best_val + 1e-9:
            best_val, best_owner = cur, list(owner)
    return best_owner, best_val


def pair_polish(problem, values, avail, roles, owner, penalty=1000.0, max_sweeps=6):
    """Exact re-optimisation of every pair of people: pool their slots and redistribute
    them optimally between the two.  Escapes the local optima that single swaps cannot."""
    n, C = len(problem.people), len(problem.cats)
    mine = [set() for _ in range(n)]
    for j, i in enumerate(owner):
        mine[i].add(j)

    def score(i, slots_i):
        return sum(values[i][j] for j in slots_i) - penalty * problem.violations(i, slots_i, avail)

    def fits(i, slots_i):
        pk = problem.packages[roles[i]]
        counts = [0] * C
        for j in slots_i:
            counts[problem.slots[j]["cat"]] += 1
        return all(pk["min"][c] <= counts[c] <= pk["max"][c] for c in range(C)) and \
            pk["total"][0] <= len(slots_i) <= pk["total"][1]

    improved_any = False
    for _ in range(max_sweeps):
        improved = False
        for a in range(n):
            for b in range(a + 1, n):
                pool = sorted(mine[a] | mine[b])
                base = score(a, mine[a]) + score(b, mine[b])
                best, best_split = base + 1e-9, None
                ta = problem.packages[roles[a]]["total"]
                for size in range(ta[0], min(ta[1], len(pool)) + 1):
                    for sa in itertools.combinations(pool, size):
                        sa = set(sa)
                        if not fits(a, sa):
                            continue
                        sb = set(pool) - sa
                        if not fits(b, sb):
                            continue
                        v = score(a, sa) + score(b, sb)
                        if v > best:
                            best, best_split = v, (sa, sb)
                if best_split is not None:
                    mine[a], mine[b] = best_split
                    improved = improved_any = True
        if not improved:
            break
    new_owner = list(owner)
    for i in range(n):
        for j in mine[i]:
            new_owner[j] = i
    total = sum(score(i, mine[i]) for i in range(n))
    return new_owner, total, improved_any


# ---------------------------------------------------------------------------
# Welfare-equivalent alternatives and the tie-break rule (finding F1)
# ---------------------------------------------------------------------------

def bundle_fits(problem, pkg, slots_i):
    """True when the slots satisfy the package's per-category and total counts."""
    pk = problem.packages[pkg]
    counts = [0] * len(problem.cats)
    for j in slots_i:
        counts[problem.slots[j]["cat"]] += 1
    return all(pk["min"][c] <= counts[c] <= pk["max"][c] for c in range(len(problem.cats))) and \
        pk["total"][0] <= len(slots_i) <= pk["total"][1]


def bundle_score(problem, values, avail, i, slots_i, penalty=1000.0):
    """Points person i collects from slots_i, less the penalty for every broken rule."""
    return sum(values[i][j] for j in slots_i) - penalty * problem.violations(i, slots_i, avail)


def pair_alternatives(problem, values, avail, roles, owner, threshold, market_roles, penalty=1000.0):
    """Assignments scoring at least `threshold` that differ from (roles, owner) for one pair of
    people: their pooled slots are re-split, with their packages kept or, when the market hands
    out the packages, swapped.  Yields (total, roles, owner)."""
    n = len(problem.people)
    mine = [[] for _ in range(n)]
    for j, i in enumerate(owner):
        mine[i].append(j)
    scores = [bundle_score(problem, values, avail, i, mine[i], penalty) for i in range(n)]
    total = sum(scores)
    for a in range(n):
        for b in range(a + 1, n):
            pool = sorted(mine[a] + mine[b])
            rest = total - scores[a] - scores[b]
            need = threshold - rest - 1e-9
            arrangements = [(roles[a], roles[b])]
            if market_roles and roles[a] != roles[b]:
                arrangements.append((roles[b], roles[a]))
            for ra, rb in arrangements:
                ta = problem.packages[ra]["total"]
                for size in range(ta[0], min(ta[1], len(pool)) + 1):
                    for sa in itertools.combinations(pool, size):
                        if not bundle_fits(problem, ra, sa):
                            continue
                        chosen = set(sa)
                        sb = [j for j in pool if j not in chosen]
                        if not bundle_fits(problem, rb, sb):
                            continue
                        v = bundle_score(problem, values, avail, a, sa, penalty) + \
                            bundle_score(problem, values, avail, b, sb, penalty)
                        if v < need or (ra == roles[a] and list(sa) == mine[a]):
                            continue
                        new_roles = list(roles)
                        new_roles[a], new_roles[b] = ra, rb
                        new_owner = list(owner)
                        for j in sa:
                            new_owner[j] = a
                        for j in sb:
                            new_owner[j] = b
                        yield rest + v, new_roles, new_owner


def equivalent_assignments(problem, values, avail, found, market_roles, tol=TIE_TOLERANCE,
                           limit=MAX_ALTERNATIVES, expansions=MAX_TIE_EXPANSIONS):
    """The welfare-equivalent assignments: every distinct (roles, owner) in `found`
    [(total, roles, owner), ...] whose total is within tol of the best, plus the pair-move
    neighbours of the first `expansions` of them (at most `limit` in all).  Best first."""
    pool, order = {}, []

    def add(total, roles, owner):
        key = (tuple(roles), tuple(owner))
        if key in pool:
            return False
        pool[key] = (total, list(roles), list(owner))
        order.append(key)
        return True

    for total, roles, owner in sorted(found, key=lambda x: -x[0]):
        add(total, roles, owner)
    best = max(t for t, _, _ in pool.values())
    expanded = qi = 0
    while qi < len(order) and expanded < expansions and len(pool) < limit:
        total, roles, owner = pool[order[qi]]
        qi += 1
        if best - total > tol:
            continue
        expanded += 1
        for total2, roles2, owner2 in pair_alternatives(problem, values, avail, roles, owner, best - tol, market_roles):
            if add(total2, roles2, owner2):
                best = max(best, total2)
            if len(pool) >= limit:
                break
    ties = [t for t in pool.values() if best - t[0] <= tol]
    ties.sort(key=lambda t: -t[0])
    return ties


def ideal_points(problem, values, avail, market_roles, roles):
    """The best each person could score with nobody else in the way (within their package, or
    any package when the market hands them out)."""
    zero = [0.0] * len(problem.slots)
    pkgs = list(problem.packages.keys())
    return [max((problem.best_response(i, k, zero, values, avail)[0] or 0.0)
                for k in (pkgs if market_roles else [roles[i]]))
            for i in range(len(problem.people))]


def tie_break_key(problem, values, roles, owner, prices, pkg_prices, market_roles, ideal, counted=None):
    """Sort key implementing TIE_RULE (smaller is better).  `counted` marks the people whose
    % of ideal is meaningful (non-responders are left out of the minimum)."""
    n = len(problem.people)
    mine = [[] for _ in range(n)]
    for j, i in enumerate(owner):
        mine[i].append(j)
    pct, net, hours, late = [], [], [], []
    for i in range(n):
        pts = sum(values[i][j] for j in mine[i])
        pct.append(pts / ideal[i] * 100.0 if ideal[i] > 1e-9 else 100.0)
        net.append(sum(prices[j] for j in mine[i]) + (pkg_prices[roles[i]] if market_roles else 0.0))
        hours.append(sum(problem.slots[j]["hours"] for j in mine[i]))
        late.append(sum(1 for j in mine[i] if problem.slots[j]["code"] == problem.late))
    counted = [i for i in range(n) if counted is None or counted[i]] or list(range(n))
    most = max(hours)
    return (-round(min(pct[i] for i in counted), 2), round(max(net) - min(net), 2),
            max(late[i] for i in range(n) if abs(hours[i] - most) < 1e-9), tuple(owner))


def describe_alternative(problem, roles, owner, total, chosen):
    """A plain-dict summary of one assignment: who owns each slot (indices), packages, points, hours."""
    hours = {p: 0.0 for p in problem.people}
    late = {p: 0 for p in problem.people}
    for j, i in enumerate(owner):
        hours[problem.people[i]] += problem.slots[j]["hours"]
        if problem.slots[j]["code"] == problem.late:
            late[problem.people[i]] += 1
    return {"owner": list(owner), "roles": list(roles), "total_points": round(total, 3),
            "hours": hours, "late_shifts": late, "chosen": bool(chosen)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def solve(problem, values, avail, market_roles=True, roles=None, iters=400, step=3.0, decay=40.0,
          sa_iters=40000, restarts=4, seed=1, log=None):
    """Run the whole pipeline.  Returns a dict with roles, owner (slot -> person index),
    prices, pkg_prices, cleared, rounds, max_regret, market_roles and alternatives (the
    welfare-equivalent assignments, see describe_alternative; the chosen one is flagged).

    The seed drives only the assignment search (the annealing restarts): the point values,
    the clearing prices and the tie-break rule do not depend on it."""
    log = log or (lambda s: None)
    if not market_roles:
        validate_roles(problem, roles)
    check_feasibility(problem, values, avail)
    log("Finding clearing prices ...")
    prices, pkg_prices, choices, cleared, rounds = tatonnement(
        problem, values, avail, None if market_roles else roles, iters, step, decay, log)
    if cleared:
        roles = [k for k, _ in choices]
        owner = [None] * len(problem.slots)
        for i, (_, bundle) in enumerate(choices):
            for j in bundle:
                owner[j] = i
        max_regret = 0.0
        total = sum(values[owner[j]][j] for j in range(len(problem.slots)))
        alternatives = [describe_alternative(problem, roles, owner, total, True)]
    else:
        log("Assigning shifts (welfare-maximising search) ...")
        candidates = candidate_roles(problem, values, avail, prices, pkg_prices, top=5) if market_roles else [roles]
        found = []
        for cand in candidates:
            for r in range(restarts):
                o, v = anneal(problem, values, avail, cand, sa_iters, random.Random(seed + 17 * r))
                o, v, _ = pair_polish(problem, values, avail, cand, o)
                found.append((v, list(cand), o))
        log("Looking for welfare-equivalent alternatives ...")
        ties = equivalent_assignments(problem, values, avail, found, market_roles)
        ideal = ideal_points(problem, values, avail, market_roles, ties[0][1])
        counted = getattr(problem, "responded", None)
        ties.sort(key=lambda t: tie_break_key(problem, values, t[1], t[2], prices, pkg_prices, market_roles,
                                              ideal, counted))
        _, roles, owner = ties[0]
        alternatives = [describe_alternative(problem, r, o, v, k == 0) for k, (v, r, o) in enumerate(ties)]
        if len(ties) > 1:
            log("  %d equivalent schedules (total points within %g); chosen by %s" % (len(ties), TIE_TOLERANCE, TIE_RULE))
        log("Refining prices to support the assignment ...")
        prices, pkg_prices, max_regret = support_prices(
            problem, values, avail, roles, owner, prices, pkg_prices, market_roles,
            iters=2 * iters, step0=step / 10.0, decay=5.0 * decay)
    return {"roles": roles, "owner": owner, "prices": prices, "pkg_prices": pkg_prices,
            "cleared": cleared, "rounds": rounds, "max_regret": max_regret, "market_roles": market_roles,
            "alternatives": alternatives}


def ties_lines(problem, result):
    """The report's account of welfare-equivalent schedules and how the final one was chosen."""
    alts = result.get("alternatives")
    if not alts:
        return []
    if len(alts) == 1:
        return ["  Ties: none (no other schedule found within %g points of the best)." % TIE_TOLERANCE]
    chosen = next(a for a in alts if a["chosen"])
    out = ["  Ties: %d equivalent schedules (total points within %g); chosen by %s."
           % (len(alts), TIE_TOLERANCE, TIE_RULE)]
    for k, alt in enumerate(a for a in alts if not a["chosen"]):
        moved = sum(1 for j in range(len(alt["owner"])) if alt["owner"][j] != chosen["owner"][j])
        changed = ["%s %s %g h" % (p, alt["roles"][i], alt["hours"][p]) for i, p in enumerate(problem.people)
                   if alt["roles"][i] != chosen["roles"][i] or alt["hours"][p] != chosen["hours"][p]]
        out.append("    alternative %d (%.1f pts): %s hands; %s"
                   % (k + 2, alt["total_points"], "1 slot changes" if moved == 1 else "%d slots change" % moved,
                      ", ".join(changed) if changed else "same packages and hours"))
    return out


def build_report(problem, values, avail, result):
    """Human-readable report.  Returns (text, summary_rows, schedule_rows)."""
    roles, owner, prices, pkg_prices = result["roles"], result["owner"], result["prices"], result["pkg_prices"]
    cleared, rounds, max_regret, market_roles = result["cleared"], result["rounds"], result["max_regret"], result["market_roles"]
    n, m = len(problem.people), len(problem.slots)
    mine = [[] for _ in range(n)]
    for j, i in enumerate(owner):
        mine[i].append(j)
    for i in range(n):
        mine[i].sort()
    out = []

    out.append("SCHEDULE")
    labels = [sh["label"] for sh in problem.shifts]
    width = max([10, max(len(p) for p in problem.people) + 2] + [len(lab) + 2 for lab in labels])
    out.append("  %-12s" % "Date" + "".join("%-*s" % (width, lab) for lab in labels))
    schedule_rows = []
    for d, dt in enumerate(problem.dates):
        if d > 0 and (dt - problem.dates[d - 1]).days > 1:
            first, last = problem.dates[d - 1] + timedelta(days=1), dt - timedelta(days=1)
            gap = fmt_date(first) if first == last else "%s - %s" % (fmt_date(first), fmt_date(last))
            out.append("  %-12s(no coverage %s)" % ("", gap))
            schedule_rows.append(("no coverage %s" % gap, [""] * len(labels)))
        cells = [problem.people[owner[j]] for j in problem.slots_by_day[d]]
        out.append("  %-12s" % fmt_date(dt) + "".join("%-*s" % (width, c) for c in cells))
        schedule_rows.append((fmt_date(dt), cells))

    out.append("")
    out.append("PEOPLE  (points = how much of their own 100 points landed on the slots they got;")
    out.append("         ideal = the best they could have done with no one else in the way)")
    rows = []
    total_pts = 0.0
    ideals = ideal_points(problem, values, avail, market_roles, roles)
    responded = getattr(problem, "responded", None) or [True] * n
    for i in range(n):
        hours = sum(problem.slots[j]["hours"] for j in mine[i])
        pts = sum(values[i][j] for j in mine[i])
        total_pts += pts
        paid = sum(prices[j] for j in mine[i]) + (pkg_prices[roles[i]] if market_roles else 0.0)
        ideal = ideals[i]
        # No ranks (ideal is tie-break noise only) means % of ideal is meaningless; a non-responder's
        # net price is meaningless too (F4).  Both are reported as n/a.
        share = (pts / ideal * 100.0) if ideal > 1e-3 and responded[i] else None
        if not responded[i]:
            paid = None
        viol = problem.violations(i, mine[i], avail)
        shifts = ", ".join("%s %s" % (fmt_date(problem.slots[j]["date"]), problem.slots[j]["code"]) for j in mine[i])
        out.append("  %-*s %-16s %4.1f h  %d shifts  %5.1f pts (%s of ideal)  net price %s%s"
                   % (width, problem.people[i], roles[i], hours, len(mine[i]), pts,
                      "n/a" if share is None else "%3.0f%%" % share, "   n/a" if paid is None else "%+6.1f" % paid,
                      ("  CONSTRAINT VIOLATIONS: %d" % viol) if viol else ""))
        out.append("  %-*s %s" % (width, "", shifts))
        if not responded[i]:
            out.append("  %-*s no preferences submitted" % (width, ""))
        rows.append({"person": problem.people[i], "package": roles[i], "shifts": len(mine[i]), "hours": hours,
                     "points_received": round(pts, 1),
                     "pct_of_ideal": None if share is None else round(share, 1),
                     "net_price": None if paid is None else round(paid, 1),
                     "next_budget_suggestion": None if paid is None else round(BUDGET - paid, 1),
                     "violations": viol, "slots": " ".join(problem.slots[j]["id"] for j in mine[i]),
                     "responded": bool(responded[i])})
    out.append("  total preference points satisfied: %.1f" % total_pts)
    out.extend(ties_lines(problem, result))
    if any(r["violations"] for r in rows):
        out.append("")
        out.append("  WARNING: the best schedule found still breaks a rule (see CONSTRAINT VIOLATIONS above).")
        out.append("  Usually this means the X's leave a day short-handed; allow 2 shifts per day or ask for fewer X's.")
    warnings = list(getattr(problem, "warnings", None) or [])
    if warnings:
        out.append("")
        out.append("WARNINGS")
        out.extend("  - " + w for w in warnings)

    out.append("")
    out.append("CLEARING PRICES (points; positive = sought after, negative = that slot pays you to take it)")
    for d, dt in enumerate(problem.dates):
        cells = ["%s %+6.1f" % (problem.slots[j]["code"], prices[j]) for j in problem.slots_by_day[d]]
        out.append("  %-12s" % fmt_date(dt) + "   ".join(cells))
    if market_roles:
        out.append("  packages:    " + "   ".join("%s %+.1f" % (k, v) for k, v in pkg_prices.items()))
    out.append("")
    if cleared:
        out.append("Market status: cleared exactly in %d rounds - every person holds the bundle they would pick for" % rounds)
        out.append("themselves at these prices, and every slot has exactly one taker.")
    else:
        out.append("Market status: prices from %d rounds support the assignment with a maximum regret of %.2f points"
                   % (rounds, max_regret))
        out.append("(regret = how much better any one person could do by switching bundles at these prices; 0 = exact equilibrium).")
    return "\n".join(out), rows, schedule_rows


def write_outputs(problem, values, avail, result, out_dir):
    """Write schedule.csv and summary.csv from a solve() result; returns their paths.

    Kept for existing callers: the files are produced by write_solution() from the
    plain dict that solution_dict() builds, so every front end writes the same bytes.
    """
    return write_solution(solution_dict(problem, values, avail, result), out_dir)


def validate_roles(problem, roles):
    """Check a fixed package-per-person list against the package counts."""
    if roles is None or len(roles) != len(problem.people) or any(r is None for r in roles):
        raise SchedulingError("Every person needs a package when roles are fixed.")
    for r in roles:
        if r not in problem.packages:
            raise SchedulingError("Unknown package %r (choose from %s)" % (r, ", ".join(problem.packages)))
    counts = {k: roles.count(k) for k in problem.packages}
    bad = ["%s (needs %d, has %d)" % (k, problem.packages[k]["count"], counts[k])
           for k in problem.packages if counts[k] != problem.packages[k]["count"]]
    if bad:
        raise SchedulingError("The fixed packages give the wrong number of people to: " + ", ".join(bad))


# ---------------------------------------------------------------------------
# Library entry point: the command line, the local GUI and the hosted app all go through here
# ---------------------------------------------------------------------------

def prepare_sheet(text, *, structure=None, mode="rank", packages=None, max_per_day=1, budgets=None, seed=1,
                  min_ranks=None):
    """Turn preferences CSV text into solver inputs.  Returns (problem, values, avail).

    The first half of solve_sheet(); cmd_solve calls it directly so that it can print the
    problem size and parse --roles-map before solving.  Arguments are as for solve_sheet().
    The problem carries two extra attributes read by the report: problem.responded (one flag
    per person, False for a column with no ranks and no X's) and problem.warnings (see
    response_check); both are harmless to callers that build a Problem by hand.
    """
    if mode not in ("rank", "points"):
        raise SchedulingError("mode must be 'rank' or 'points', not %r." % (mode,))
    people, slot_rows = parse_preferences(text)
    structure = structure or infer_structure(slot_rows)
    if structure not in STRUCTURES:
        raise SchedulingError("Unknown structure %r (choose from %s)." % (structure, ", ".join(STRUCTURES)))
    dates = sorted({r["date"] for r in slot_rows})
    problem = Problem(structure, dates, people, packages, max_per_day=max_per_day)
    if budgets:
        unknown = sorted(set(budgets) - set(people))
        if unknown:
            raise SchedulingError("Budgets name people not in the preferences file: %s" % ", ".join(unknown))
    # The tie-break noise uses a fixed seed: the clearing prices and the search inputs are then the
    # same for every solve seed, and the seed only drives the assignment search (see solve()).
    values, avail = preference_values(problem, slot_rows, mode, random.Random(NOISE_SEED), budgets)
    problem.responded, problem.warnings = response_check(
        people, slot_rows, mode, None if min_ranks is None else int(min_ranks))
    return problem, values, avail


def roles_in_order(problem, roles):
    """A {person: package} mapping as the list solve() takes (sheet order; None where a person is missing)."""
    unknown = sorted(set(roles) - set(problem.people))
    if unknown:
        raise SchedulingError("roles name people not in the preferences file: %s" % ", ".join(unknown))
    return [roles.get(p) or None for p in problem.people]


def solution_dict(problem, values, avail, result):
    """The plain, JSON-serialisable form of a solve() result (the shape is documented in solve_sheet)."""
    text, rows, _ = build_report(problem, values, avail, result)
    owner, prices = result["owner"], result["prices"]
    schedule = [{"slot_id": s["id"], "date": s["date"].isoformat(), "weekday": s["date"].strftime("%a"),
                 "shift": s["code"], "time": s["time"], "hours": s["hours"],
                 "person": problem.people[owner[j]], "price": prices[j]}
                for j, s in enumerate(problem.slots)]
    return {
        "structure": problem.structure,
        "people": list(problem.people),
        "dates": [d.isoformat() for d in problem.dates],
        "schedule": schedule,
        "summary": rows,
        "prices": {s["id"]: prices[j] for j, s in enumerate(problem.slots)},
        "package_prices": dict(result["pkg_prices"]) if result["market_roles"] else None,
        "cleared": bool(result["cleared"]),
        "rounds": int(result["rounds"]),
        "max_regret": float(result["max_regret"]),
        "market_roles": bool(result["market_roles"]),
        "report": text,
        "warnings": list(getattr(problem, "warnings", None) or []),
        "alternatives": [
            {"owner": [problem.people[i] for i in alt["owner"]],
             "roles": {problem.people[i]: alt["roles"][i] for i in range(len(problem.people))},
             "total_points": alt["total_points"], "hours": dict(alt["hours"]),
             "late_shifts": dict(alt["late_shifts"]), "chosen": alt["chosen"]}
            for alt in result.get("alternatives", [])],
    }


def solve_sheet(text, *, structure=None, mode="rank", roles=None, packages=None, max_per_day=1, budgets=None,
                seed=1, iters=400, step=3.0, decay=40.0, sa_iters=40000, restarts=4, log=None, min_ranks=None):
    """Solve a preferences sheet end to end and return a plain dict.

    This is the one library entry point: the command line (cmd_solve), the local GUI
    (oncall_gui.run_solve) and the hosted app all go through the same path
    (prepare_sheet -> solve -> solution_dict).  The defaults are the `solve` command's defaults.

    Arguments
      text         preferences CSV text in the format `template` writes: comment lines starting
                   with '#', the header slot_id,date,weekday,shift,time,hours,<one column per
                   person>, then one row per slot.  Cells hold a rank (or points), nothing
                   (available, no preference) or an unavailable token such as X.
      structure    'single', 'two' or 'three'; inferred from the shift codes when None.
      mode         'rank' (1 = favourite) or 'points' (higher = want more).
      roles        None lets the market hand out the packages and price them; a
                   {person: package} mapping fixes them (every person must be named).
      packages     None for the built-in packages, or a dict in the --packages JSON format.
      max_per_day  shifts one person may take on the same day.
      budgets      optional {person: points}; anyone not named gets BUDGET (100 points).
      seed, iters, step, decay, sa_iters, restarts
                   the `solve` command's options of the same names.
      log          optional callable that receives progress lines (the CLI passes print).
      min_ranks    optional minimum number of ranked slots per person; anyone below it is named
                   in `warnings` (never an error: the app enforces the minimum, the solver reports).

    Returns a dict with exactly these keys (all values are JSON-serialisable):
      structure       'single' | 'two' | 'three'
      people          [name, ...] in sheet order
      dates           ['YYYY-MM-DD', ...] the covered days in order (weekends/holidays absent)
      schedule        [{slot_id, date, weekday, shift, time, hours, person, price}, ...] one
                      entry per slot in date order (shifts in structure order within a day).
                      price is the slot's clearing price, unrounded; schedule.csv rounds it
                      to 2 decimals as clearing_price.
      summary         one row per person in sheet order, the rows of summary.csv:
                      {person, package, shifts, hours, points_received, pct_of_ideal,
                       net_price, next_budget_suggestion, violations, slots, responded}
                      where slots is a space-separated string of slot ids and the point
                      and price figures are rounded to 1 decimal.  responded is False for
                      a column with no ranks and no X's; that person's pct_of_ideal,
                      net_price and next_budget_suggestion are None ("n/a" in the text
                      report and in summary.csv), as is pct_of_ideal for anyone who
                      ranked nothing.
      prices          {slot_id: price} (unrounded)
      package_prices  {package: price}, or None when roles were fixed
      cleared         True when the market cleared exactly
      rounds          price-adjustment rounds used
      max_regret      the most anyone could gain by switching bundles at the final prices
                      (0.0 for an exact clearing)
      market_roles    False when roles were fixed
      report          the text report the `solve` command prints
      warnings        [sentence, ...] about non-responders and, with min_ranks, people who
                      ranked too few slots; [] when there is nothing to say
      alternatives    the welfare-equivalent schedules (total preference points within
                      TIE_TOLERANCE of the best), best-ranked first, each
                      {owner: [person per slot in schedule order], roles: {person: package},
                       total_points, hours: {person: h}, late_shifts: {person: n}, chosen}.
                      Exactly one has chosen True: the schedule above, picked by TIE_RULE.
                      A single entry means no equivalent alternative was found.

    The seed drives only the assignment search: the point values (and so the clearing prices)
    are the same for every seed, and welfare-equivalent schedules are ranked by TIE_RULE, so
    different seeds normally give the same schedule.

    Raises SchedulingError for anything the user has to fix (bad sheet, impossible X's,
    packages that do not add up, a wrong roles mapping).
    """
    problem, values, avail = prepare_sheet(text, structure=structure, mode=mode, packages=packages,
                                           max_per_day=max_per_day, budgets=budgets, seed=seed, min_ranks=min_ranks)
    market_roles = roles is None
    role_list = None if market_roles else roles_in_order(problem, roles)
    result = solve(problem, values, avail, market_roles, role_list, iters, step, decay, sa_iters, restarts, seed, log)
    return solution_dict(problem, values, avail, result)


def schedule_csv_text(solution):
    """schedule.csv as text, from a solve_sheet() result."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["slot_id", "date", "weekday", "shift", "time", "hours", "person", "clearing_price"])
    for r in solution["schedule"]:
        w.writerow([r["slot_id"], r["date"], r["weekday"], r["shift"], r["time"], r["hours"], r["person"],
                    round(r["price"], 2)])
    return buf.getvalue()


def summary_csv_text(solution):
    """summary.csv as text, from a solve_sheet() result."""
    rows = solution["summary"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows([{k: ("n/a" if v is None else v) for k, v in r.items()} for r in rows])
    return buf.getvalue()


def write_solution(solution, out_dir):
    """Write schedule.csv and summary.csv from a solve_sheet() result into out_dir; returns their paths."""
    out_dir = out_dir.rstrip("/\\") or "."
    sched = out_dir + "/schedule.csv"
    summ = out_dir + "/summary.csv"
    with open(sched, "w", newline="", encoding="utf-8") as f:
        f.write(schedule_csv_text(solution))
    with open(summ, "w", newline="", encoding="utf-8") as f:
        f.write(summary_csv_text(solution))
    return sched, summ


# ---------------------------------------------------------------------------
# Demo preferences
# ---------------------------------------------------------------------------

def demo_cells(problem, rng):
    """Plausible made-up preferences: everyone has a taste for early/late and for
    certain days, ranks a different number of slots, and has a few X's."""
    n, m = len(problem.people), len(problem.slots)
    nsh = len(problem.shifts)
    pos = {sh["code"]: k / max(1, nsh - 1) for k, sh in enumerate(problem.shifts)}  # 0 = earliest, 1 = latest
    ndays = len(problem.dates)
    profiles = []
    for i in range(n):
        taste = rng.choice(["early", "late", "mid", "flat"])
        days = rng.choice(["week1", "week2", "midweek", "flat"])
        nrank = rng.choice([5, 8, 10, 12, m])
        profiles.append((taste, days, nrank))
    cells = [[""] * m for _ in range(n)]
    off_days = list(range(ndays))
    rng.shuffle(off_days)
    for i, (taste, days, nrank) in enumerate(profiles):
        scores = []
        for j, s in enumerate(problem.slots):
            x = pos[s["code"]]
            t = {"early": 1 - x, "late": x, "mid": 1 - abs(x - 0.5) * 2, "flat": 0.5}[taste] * 6
            d = s["day"] / max(1, ndays - 1)
            dd = {"week1": 1 - d, "week2": d, "midweek": 1 - abs(d - 0.5) * 2, "flat": 0.5}[days] * 4
            scores.append(t + dd + rng.uniform(0, 3))
        order = sorted(range(m), key=lambda j: -scores[j])
        # unavailability: one whole day off, plus a couple of random slots
        off_day = off_days[i % ndays]
        unavailable = {j for j in range(m) if problem.slots[j]["day"] == off_day}
        unavailable |= set(rng.sample(range(m), rng.choice([0, 1, 2])))
        rank = 1
        for j in order:
            if j in unavailable:
                cells[i][j] = "X"
            elif rank <= nrank:
                cells[i][j] = str(rank)
                rank += 1
    return cells


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_roles_map(text, people, packages):
    roles = [None] * len(people)
    for part in text.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SchedulingError("--roles-map entries look like 'Person A=heavy'")
        name, pkg = [x.strip() for x in part.split("=", 1)]
        if name not in people:
            raise SchedulingError("--roles-map: unknown person %r" % name)
        if pkg not in packages:
            raise SchedulingError("--roles-map: unknown package %r (choose from %s)" % (pkg, ", ".join(packages)))
        roles[people.index(name)] = pkg
    missing = [people[i] for i in range(len(people)) if roles[i] is None]
    if missing:
        raise SchedulingError("--roles-map is missing: %s" % ", ".join(missing))
    return roles


def build_problem_from_args(args, people):
    skip = {date.fromisoformat(s) for s in (args.skip or [])}
    dates = weekdays(date.fromisoformat(args.start), args.days, skip)
    packages = None
    if getattr(args, "packages", None):
        with open(args.packages, encoding="utf-8") as f:
            packages = json.load(f)
    return Problem(args.structure, dates, people, packages)


def cmd_template(args):
    people = [p.strip() for p in args.people.split(",")] if args.people else DEFAULT_PEOPLE
    problem = build_problem_from_args(args, people)
    write_preferences(args.out, problem)
    print("Wrote %s: %d slots x %d people (%s, %s)." % (args.out, len(problem.slots), len(people),
                                                        problem.st["description"], args.structure))


def cmd_demo(args):
    people = [p.strip() for p in args.people.split(",")] if args.people else DEFAULT_PEOPLE
    problem = build_problem_from_args(args, people)
    rng = random.Random(args.seed)
    cells = demo_cells(problem, rng)
    write_preferences(args.out, problem, cells,
                      ["# EXAMPLE preferences (made up) - ranks 1, 2, 3 ... and X = unavailable."] + TEMPLATE_HEADER[1:])
    print("Wrote %s with example rankings for %d people." % (args.out, len(people)))


def cmd_solve(args):
    with open(args.preferences, encoding="utf-8-sig") as f:
        text = f.read()
    packages = None
    if args.packages:
        with open(args.packages, encoding="utf-8") as f:
            packages = json.load(f)
    budgets = None
    if args.budgets:
        budgets = {}
        with open(args.budgets, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                budgets[row["person"].strip()] = float(row["budget"])
    # Same path as solve_sheet(): prepare_sheet -> solve -> solution_dict, split here only so the
    # problem size can be printed and --roles-map parsed (with its own messages) before solving.
    problem, values, avail = prepare_sheet(text, structure=args.structure, mode=args.mode, packages=packages,
                                           max_per_day=args.max_per_day, budgets=budgets, seed=args.seed,
                                           min_ranks=args.min_ranks)

    market_roles = args.roles == "market"
    roles = None if market_roles else parse_roles_map(args.roles_map or "", problem.people, problem.packages)

    print("Structure: %s (%s); %d slots over %d days; %d people; %s mode; roles decided by %s."
          % (problem.structure, problem.st["description"], len(problem.slots), len(problem.dates),
             len(problem.people), args.mode, "the market" if market_roles else "the organiser"))
    log = (lambda s: None) if args.quiet else print
    result = solve(problem, values, avail, market_roles, roles, args.iters, args.step, args.decay,
                   args.sa_iters, args.restarts, args.seed, log)
    solution = solution_dict(problem, values, avail, result)
    print()
    print(solution["report"])
    sched, summ = write_solution(solution, args.out_dir)
    print("Wrote %s and %s" % (sched, summ))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Preference-ranked, market-cleared on-call scheduling.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_calendar_args(p):
        p.add_argument("--structure", choices=list(STRUCTURES), default="three")
        p.add_argument("--start", default="2026-11-02", help="first day (YYYY-MM-DD)")
        p.add_argument("--days", type=int, default=9, help="number of weekdays to cover")
        p.add_argument("--skip", nargs="*", help="dates to leave out, e.g. holidays (YYYY-MM-DD)")
        p.add_argument("--people", help="comma-separated names (default: Person A ... Person G)")
        p.add_argument("--packages", help="JSON file overriding the built-in packages")

    t = sub.add_parser("template", help="write a blank preferences sheet")
    add_calendar_args(t)
    t.add_argument("--out", default="preferences.csv")
    t.set_defaults(func=cmd_template)

    d = sub.add_parser("demo", help="write a filled-in example preferences sheet")
    add_calendar_args(d)
    d.add_argument("--out", default="preferences_example.csv")
    d.add_argument("--seed", type=int, default=1)
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("solve", help="compute clearing prices and assign shifts")
    s.add_argument("preferences", help="filled-in preferences CSV")
    s.add_argument("--structure", choices=list(STRUCTURES), help="(inferred from the file if omitted)")
    s.add_argument("--mode", choices=["rank", "points"], default="rank",
                   help="cells are ranks (1 = best) or points (higher = better)")
    s.add_argument("--roles", choices=["market", "fixed"], default="market",
                   help="let the market decide who takes which package, or fix it with --roles-map")
    s.add_argument("--roles-map", help='e.g. "Person A=heavy,Person B=light_evenings,..."')
    s.add_argument("--packages", help="JSON file overriding the built-in packages")
    s.add_argument("--max-per-day", type=int, default=1, help="shifts one person may take on the same day (default 1)")
    s.add_argument("--budgets", help="CSV (person,budget) giving some people more than 100 points, e.g. to carry over last cycle's compensation")
    s.add_argument("--min-ranks", type=int, help="list anyone who ranked fewer slots than this under WARNINGS in the report")
    s.add_argument("--iters", type=int, default=400, help="maximum price-adjustment rounds")
    s.add_argument("--step", type=float, default=3.0, help="initial price step (points)")
    s.add_argument("--decay", type=float, default=40.0, help="rounds after which the step has halved")
    s.add_argument("--sa-iters", type=int, default=40000, help="annealing iterations per restart (fallback)")
    s.add_argument("--restarts", type=int, default=4)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--out-dir", default=".")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_solve)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except SchedulingError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
