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
            values[i][j] += rng.uniform(0.0, 1e-3)
    return values, avail


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
# Reporting
# ---------------------------------------------------------------------------

def solve(problem, values, avail, market_roles=True, roles=None, iters=400, step=3.0, decay=40.0,
          sa_iters=40000, restarts=4, seed=1, log=None):
    """Run the whole pipeline.  Returns a dict with roles, owner (slot -> person index),
    prices, pkg_prices, cleared, rounds and max_regret."""
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
    else:
        log("Assigning shifts (welfare-maximising search) ...")
        candidates = candidate_roles(problem, values, avail, prices, pkg_prices, top=5) if market_roles else [roles]
        best_owner, best_roles, best_val = None, None, NEG
        for cand in candidates:
            for r in range(restarts):
                o, v = anneal(problem, values, avail, cand, sa_iters, random.Random(seed + 17 * r))
                o, v, _ = pair_polish(problem, values, avail, cand, o)
                if v > best_val:
                    best_owner, best_roles, best_val = o, cand, v
        owner, roles = best_owner, best_roles
        log("Refining prices to support the assignment ...")
        prices, pkg_prices, max_regret = support_prices(
            problem, values, avail, roles, owner, prices, pkg_prices, market_roles,
            iters=2 * iters, step0=step / 10.0, decay=5.0 * decay)
    return {"roles": roles, "owner": owner, "prices": prices, "pkg_prices": pkg_prices,
            "cleared": cleared, "rounds": rounds, "max_regret": max_regret, "market_roles": market_roles}


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
    zero = [0.0] * m
    pkgs = list(problem.packages.keys())
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
    for i in range(n):
        hours = sum(problem.slots[j]["hours"] for j in mine[i])
        pts = sum(values[i][j] for j in mine[i])
        total_pts += pts
        paid = sum(prices[j] for j in mine[i]) + (pkg_prices[roles[i]] if market_roles else 0.0)
        ideal = max((problem.best_response(i, k, zero, values, avail)[0] or 0.0)
                    for k in (pkgs if market_roles else [roles[i]]))
        share = (pts / ideal * 100.0) if ideal > 1e-9 else 100.0
        viol = problem.violations(i, mine[i], avail)
        shifts = ", ".join("%s %s" % (fmt_date(problem.slots[j]["date"]), problem.slots[j]["code"]) for j in mine[i])
        out.append("  %-*s %-16s %4.1f h  %d shifts  %5.1f pts (%3.0f%% of ideal)  net price %+6.1f%s"
                   % (width, problem.people[i], roles[i], hours, len(mine[i]), pts, share, paid,
                      ("  CONSTRAINT VIOLATIONS: %d" % viol) if viol else ""))
        out.append("  %-*s %s" % (width, "", shifts))
        rows.append({"person": problem.people[i], "package": roles[i], "shifts": len(mine[i]), "hours": hours,
                     "points_received": round(pts, 1), "pct_of_ideal": round(share, 1), "net_price": round(paid, 1),
                     "next_budget_suggestion": round(BUDGET - paid, 1),
                     "violations": viol, "slots": " ".join(problem.slots[j]["id"] for j in mine[i])})
    out.append("  total preference points satisfied: %.1f" % total_pts)
    if any(r["violations"] for r in rows):
        out.append("")
        out.append("  WARNING: the best schedule found still breaks a rule (see CONSTRAINT VIOLATIONS above).")
        out.append("  Usually this means the X's leave a day short-handed; allow 2 shifts per day or ask for fewer X's.")

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
    """Write schedule.csv and summary.csv into out_dir; returns their paths."""
    _, rows, _ = build_report(problem, values, avail, result)
    owner, prices = result["owner"], result["prices"]
    out_dir = out_dir.rstrip("/\\") or "."
    sched = out_dir + "/schedule.csv"
    summ = out_dir + "/summary.csv"
    with open(sched, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slot_id", "date", "weekday", "shift", "time", "hours", "person", "clearing_price"])
        for j, s in enumerate(problem.slots):
            w.writerow([s["id"], s["date"].isoformat(), s["date"].strftime("%a"), s["code"], s["time"], s["hours"],
                        problem.people[owner[j]], round(prices[j], 2)])
    with open(summ, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return sched, summ


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
    people, slot_rows = read_preferences(args.preferences)
    structure = args.structure or infer_structure(slot_rows)
    dates = sorted({r["date"] for r in slot_rows})
    packages = None
    if args.packages:
        with open(args.packages, encoding="utf-8") as f:
            packages = json.load(f)
    problem = Problem(structure, dates, people, packages, max_per_day=args.max_per_day)
    rng = random.Random(args.seed)
    budgets = None
    if args.budgets:
        budgets = {}
        with open(args.budgets, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                budgets[row["person"].strip()] = float(row["budget"])
        unknown = sorted(set(budgets) - set(people))
        if unknown:
            raise SchedulingError("--budgets names people not in the preferences file: %s" % ", ".join(unknown))
    values, avail = preference_values(problem, slot_rows, args.mode, rng, budgets)

    market_roles = args.roles == "market"
    roles = None if market_roles else parse_roles_map(args.roles_map or "", people, problem.packages)

    print("Structure: %s (%s); %d slots over %d days; %d people; %s mode; roles decided by %s."
          % (structure, problem.st["description"], len(problem.slots), len(dates), len(people), args.mode,
             "the market" if market_roles else "the organiser"))
    log = (lambda s: None) if args.quiet else print
    result = solve(problem, values, avail, market_roles, roles, args.iters, args.step, args.decay,
                   args.sa_iters, args.restarts, args.seed, log)
    text, _, _ = build_report(problem, values, avail, result)
    print()
    print(text)
    sched, summ = write_outputs(problem, values, avail, result, args.out_dir)
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
