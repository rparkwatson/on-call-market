#!/usr/bin/env python3
"""
packages.py - fairness packages for any headcount and cycle length
==================================================================

Standard library only.  oncall_market.py ships "packages" (how many shifts of
each category a person takes) for 7 people over 9 weekdays only; this module
derives them for any headcount, period and shift structure:

    from packages import derive_packages, describe
    packages = derive_packages("three", 6, 9)      # the packages JSON shape
    print(describe(packages, "three"))             # a table like the README's

    python packages.py --structure three --people 6 --days 9 [--start 2026-11-02]
                       [--skip 2026-11-11 ...] [--max-per-day 2] [--out packages.json]

Principles (docs/packages.md has the algorithm and worked examples):

  P1  Hours at the floor: everyone works either the floor or the floor plus
      one granule (the greatest common divisor of the shift lengths, 2 h for
      the built-in structures), and as few people as possible take the
      higher load.
  P2  Evenings balanced: late shifts are spread as evenly as possible, and
      the higher-hours package carries fewer late shifts than the
      lower-hours package that carries the evenings.
  P3  At most max_per_day shifts per person per day, so nobody takes more
      than days x max_per_day shifts.
  P4  No late shift followed by an early shift the next day: the solver
      enforces it per schedule; the packages only have to leave it possible.

Raises oncall_market.SchedulingError, naming the headcounts that would work,
when no split satisfying the principles exists.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date

import oncall_market as om

SchedulingError = om.SchedulingError
VALIDATION_START = date(2026, 11, 2)  # a Monday; only used to make dates when `days` is a number


# ---------------------------------------------------------------------------
# Structure facts and the hours arithmetic
# ---------------------------------------------------------------------------

def _facts(structure):
    """Categories of a structure: codes, labels, length in half-hours, which are late and early."""
    if structure not in om.STRUCTURES:
        raise SchedulingError("Unknown structure %r (expected one of %s)" % (structure, ", ".join(om.STRUCTURES)))
    st = om.STRUCTURES[structure]
    by_code = {sh["code"]: sh for sh in st["shifts"]}
    cats = list(st["categories"])
    codes = [list(st["categories"][c]) for c in cats]
    half = []
    for cat, cs in zip(cats, codes):
        lengths = {int(round(by_code[c]["hours"] * 2)) for c in cs}
        if len(lengths) != 1:
            raise SchedulingError("Category %r mixes shifts of different lengths" % cat)
        half.append(lengths.pop())
    return {
        "cats": cats, "codes": codes, "half": half,
        "labels": [[by_code[c]["label"] for c in cs] for cs in codes],
        "late": next(i for i, cs in enumerate(codes) if st["late"] in cs),
        "early": [i for i, cs in enumerate(codes) if set(cs) & set(st["early"])],
        "n_early": len(st["early"]), "per_day": len(st["shifts"]), "builtin": st["packages"],
    }


def _gcd_all(values):
    g = 0
    for v in values:
        g = math.gcd(g, v)
    return g


def _loads(total_half, n, granule):
    """Half-hours of the lower and the higher load, and how many people take the higher one (P1)."""
    lo = granule * (total_half // (granule * n))
    k = (total_half - n * lo) // granule
    return lo, lo + granule if k else lo, k


def _shapes(half, caps, total_cap, target):
    """Every count tuple with sum(count * half) == target, count[i] <= caps[i], sum(count) <= total_cap."""
    out = []

    def rec(i, counts, left, used):
        if i == len(half):
            if left == 0:
                out.append(tuple(counts))
            return
        for c in range(min(caps[i], left // half[i], total_cap - used) + 1):
            counts.append(c)
            rec(i + 1, counts, left - c * half[i], used + c)
            counts.pop()

    rec(0, [], target, 0)
    return out


def _dates(days):
    """`days` as a list of dates: a number means that many weekdays from VALIDATION_START."""
    if isinstance(days, int):
        if days < 1:
            raise SchedulingError("Need at least one day")
        return om.weekdays(VALIDATION_START, days)
    return [d if isinstance(d, date) else date.fromisoformat(str(d)) for d in days]


# ---------------------------------------------------------------------------
# Structures with mixed shift lengths: choose a multiset of exact shapes
# ---------------------------------------------------------------------------

def _key(chosen, late):
    """Objective of a multiset [(shape, level, people)], smaller is better: the maximum late count;
    the higher-load people with at least as many late shifts as the lower-load package that carries
    the most (P2); the sum of squared late counts (evenness); the number of packages; a fixed order."""
    max_late = max(c[late] for c, _, _ in chosen)
    lo_max = max([c[late] for c, lvl, _ in chosen if lvl == "lo"], default=0)
    p2 = sum(m for c, lvl, m in chosen if lvl == "hi" and c[late] >= lo_max)
    even = sum(m * c[late] ** 2 for c, _, m in chosen)
    return (max_late, p2, even, len(chosen), sorted((lvl != "hi", c, m) for c, lvl, m in chosen))


def _search(hi_shapes, lo_shapes, k, n_lo, supply, late):
    """Best multiset by _key: k people on hi_shapes, n_lo on lo_shapes, category totals == supply."""
    C = len(supply)
    items = [(s, "hi") for s in hi_shapes] + [(s, "lo") for s in lo_shapes]
    bounds = {}
    for lvl, shapes in (("hi", hi_shapes), ("lo", lo_shapes)):
        bounds[lvl] = ([min((s[c] for s in shapes), default=0) for c in range(C)],
                       [max((s[c] for s in shapes), default=0) for c in range(C)])
    best = [None]

    def possible(left, remaining):
        for c in range(C):
            least = sum(remaining[l] * bounds[l][0][c] for l in remaining)
            most = sum(remaining[l] * bounds[l][1][c] for l in remaining)
            if not least <= left[c] <= most:
                return False
        return True

    def rec(idx, left, remaining, chosen):
        if not possible(left, remaining):
            return
        if idx == len(items):
            if not any(remaining.values()) and not any(left):
                key = _key(chosen, late)
                if best[0] is None or key < best[0][0]:
                    best[0] = (key, list(chosen))
            return
        shape, lvl = items[idx]
        most = remaining[lvl]
        for c in range(C):
            if shape[c]:
                most = min(most, left[c] // shape[c])
        for m in range(most, -1, -1):
            if m:
                chosen.append((shape, lvl, m))
            rec(idx + 1, [left[c] - m * shape[c] for c in range(C)], {**remaining, lvl: remaining[lvl] - m}, chosen)
            if m:
                chosen.pop()

    rec(0, list(supply), {"hi": k, "lo": n_lo}, [])
    return best[0][1] if best[0] else None


def _generic_packages(f, n, D, mpd):
    """Packages for a structure whose shifts differ in length (three, two); None if no fair split exists."""
    supply = [D * len(cs) for cs in f["codes"]]
    caps = [D * min(mpd, len(cs)) for cs in f["codes"]]
    lo, hi, k = _loads(sum(s * h for s, h in zip(supply, f["half"])), n, _gcd_all(f["half"]))
    hi_shapes = _shapes(f["half"], caps, D * mpd, hi) if k else []
    lo_shapes = _shapes(f["half"], caps, D * mpd, lo)
    late = f["late"]
    for bound in range(max((s[late] for s in hi_shapes + lo_shapes), default=0) + 1):
        chosen = _search([s for s in hi_shapes if s[late] <= bound], [s for s in lo_shapes if s[late] <= bound],
                         k, n - k, supply, late)
        if chosen:
            break
    else:
        return None
    chosen.sort(key=lambda item: (item[1] != "hi", -item[0][late]))  # higher load first, then more evenings
    packages = {}
    for shape, lvl, m in chosen:
        hours = sum(c * h for c, h in zip(shape, f["half"])) / 2
        name = _builtin_name(f, shape, lvl if k else None) or "%gh_%d%s" % (hours, shape[late], f["cats"][late])
        packages[name] = {"count": m, "cats": {cat: [c, c] for cat, c in zip(f["cats"], shape)},
                          "total": [sum(shape), sum(shape)]}
    return packages


def _builtin_name(f, shape, level):
    """The built-in package's name when a derived package has the same contents and the same role
    (the heavier or the lighter load); None otherwise, or when everyone has the same hours."""
    if level is None:
        return None
    built = f["builtin"]
    hours = {name: sum(pk["cats"].get(cat, [0, 0])[0] * h for cat, h in zip(f["cats"], f["half"]))
             for name, pk in built.items()}
    for name, pk in built.items():
        same = all(list(pk["cats"].get(cat, [0, 0])) == [c, c] for cat, c in zip(f["cats"], shape))
        same = same and list(pk.get("total", [sum(shape)] * 2)) == [sum(shape)] * 2
        if same and ("hi" if hours[name] == max(hours.values()) else "lo") == level:
            return name
    return None


# ---------------------------------------------------------------------------
# Block structures (every category is one shift and all are the same length): ranges
# ---------------------------------------------------------------------------

def _is_block(f):
    return all(len(cs) == 1 for cs in f["codes"]) and len(set(f["half"])) == 1


def _block_extras(f, k, n_lo, r_hi, extra):
    """How many of the k higher-load people take an extra block of each type (each takes r_hi extras,
    `extra` of each type are needed overall): as few as possible on the late block, then on the early
    blocks, and the rest spread evenly over the middle of the day."""
    T = len(f["cats"])
    lo_cap, hi_cap = max(0, extra - n_lo), min(k, extra)   # keeps the lower load's share within 0..n_lo
    x = [lo_cap] * T
    left = k * r_hi - lo_cap * T
    middle = [i for i in range(T) if i != f["late"] and i not in f["early"]]
    take = min(left, (hi_cap - lo_cap) * len(middle))
    q, rem = divmod(take, len(middle)) if middle else (0, 0)
    for j, i in enumerate(middle):
        x[i] += q + (1 if j < rem else 0)
    left -= take
    for i in list(f["early"]) + [f["late"]]:
        add = min(left, hi_cap - lo_cap)
        x[i] += add
        left -= add
    return x if left == 0 else None


def _block_packages(f, n, D, mpd):
    """Packages for a block structure (single): everyone gets the floor of each block type, and the
    extra blocks are ranges; None if the higher load does not fit in days x max_per_day."""
    T = len(f["cats"])
    t_lo, k = divmod(T * D, n)                      # k people take t_lo + 1 blocks
    t_hi = t_lo + (1 if k else 0)
    if t_hi > D * mpd:
        return None
    groups = [("hi", k, t_hi), ("lo", n - k, t_lo)] if k else [("lo", n, t_lo)]
    base = {lvl: t // T for lvl, _, t in groups}
    extra = D - sum(m * base[lvl] for lvl, m, _ in groups)   # extra blocks of each type to place
    if k:
        x_hi = _block_extras(f, k, n - k, t_hi % T, extra)
        if x_hi is None:
            return None
        x = {"hi": x_hi, "lo": [extra - v for v in x_hi]}
    else:
        x = {"lo": [extra] * T}
    packages = {}
    for lvl, m, t in groups:
        name = ("heavy%d" if lvl == "hi" else "light%d" if k else "all%d") % t
        cats = {cat: [base[lvl] + (1 if x[lvl][i] == m else 0), base[lvl] + (1 if x[lvl][i] > 0 else 0)]
                for i, cat in enumerate(f["cats"])}
        packages[name] = {"count": m, "cats": cats, "total": [t, t]}
    return packages


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _derive(f, n, D, mpd):
    """The packages, or None when the principles cannot be met."""
    if f["per_day"] > n * mpd:
        return None
    if n * mpd == f["per_day"] and (n - 1) * mpd < f["n_early"]:
        return None   # whoever takes the late shift would have to take an early one next morning (P4)
    return _block_packages(f, n, D, mpd) if _is_block(f) else _generic_packages(f, n, D, mpd)


def _why_not(f, structure, n, D, mpd):
    """An actionable message for a case _derive refused."""
    per_day = f["per_day"]
    if per_day > n * mpd:
        return ("The %s structure has %d shifts a day but %d people at %d shift(s) a day can cover at most %d: "
                "use at least %d people or max_per_day %d."
                % (structure, per_day, n, mpd, n * mpd, math.ceil(per_day / mpd), math.ceil(per_day / n)))
    if n * mpd == per_day and (n - 1) * mpd < f["n_early"]:
        return ("With %d people at %d shift(s) a day everyone works every shift every day, so whoever takes the "
                "late shift would have to take an early shift the next morning: use more people." % (n, mpd))
    supply = ", ".join("%d x %s" % (D * len(cs), "/".join(labels)) for cs, labels in zip(f["codes"], f["labels"]))
    lo, hi, k = _loads(sum(D * len(cs) * h for cs, h in zip(f["codes"], f["half"])), n, _gcd_all(f["half"]))
    hours = "%g hours each" % (lo / 2) if not k else "%g or %g hours each" % (lo / 2, hi / 2)
    smaller = next((m for m in range(n - 1, 0, -1) if _derive(f, m, D, mpd)), None)
    larger = next((m for m in range(n + 1, n + 8) if _derive(f, m, D, mpd)), None)
    works = [str(m) for m in (smaller, larger) if m]
    hints = []
    if works:
        hints.append("Headcounts that work: %s." % " or ".join(works))
    if _derive(f, n, D, mpd + 1):
        hints.append("max_per_day %d would work for %d people." % (mpd + 1, n))
    return ("No fair split for %d people over %d days with the %s structure: everyone would work %s, and the "
            "shifts (%s) cannot be divided that way. %s" % (n, D, structure, hours, supply, " ".join(hints))).rstrip()


def derive_packages(structure, n_people, days, max_per_day=1):
    """Fair packages for n_people over `days` (a number of days or a list of dates) in `structure`.

    Returns a dict in the packages JSON shape {name: {"count", "cats": {category: [min, max]},
    "total": [min, max]}}, validated by oncall_market.Problem.  Deterministic.  Raises
    SchedulingError with the headcounts that would work when no fair split exists."""
    f = _facts(structure)
    n, mpd = int(n_people), max(1, int(max_per_day))
    if n < 1:
        raise SchedulingError("Need at least one person")
    dates = _dates(days)
    packages = _derive(f, n, len(dates), mpd)
    if packages is None:
        raise SchedulingError(_why_not(f, structure, n, len(dates), mpd))
    om.Problem(structure, dates, ["Person %d" % (i + 1) for i in range(n)], packages, max_per_day=mpd)
    return packages


# ---------------------------------------------------------------------------
# Presentation and command line
# ---------------------------------------------------------------------------

def describe(packages, structure):
    """A table like the README's: package, count, contents, hours (plain text, markdown-shaped)."""
    f = _facts(structure)
    uniform = len(set(f["half"])) == 1
    rows = [("Package", "Count", "Contents", "Hours")]
    for name, pk in packages.items():
        parts, lo_half, hi_half = [], 0, 0
        for i, cat in enumerate(f["cats"]):
            lo, hi = (int(v) for v in pk["cats"].get(cat, [0, 0]))
            label = f["labels"][i]
            label = label[0] if len(label) == 1 else "(" + " or ".join(label) + ")"
            if hi:
                parts.append("%s x %s" % ("%d" % lo if lo == hi else "%d-%d" % (lo, hi), label))
            lo_half += lo * f["half"][i]
            hi_half += hi * f["half"][i]
        tmin, tmax = (int(v) for v in pk.get("total", [0, 0]))
        if uniform and tmin == tmax:
            lo_half = hi_half = tmin * f["half"][0]
        hours = "%g" % (lo_half / 2) if lo_half == hi_half else "%g-%g" % (lo_half / 2, hi_half / 2)
        rows.append((str(name), str(pk["count"]), " + ".join(parts) or "nothing", hours))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    line = lambda r: "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"
    rule = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(rows[0]), rule] + [line(r) for r in rows[1:]])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Derive the fairness packages for a headcount and period.")
    ap.add_argument("--structure", default="three", choices=list(om.STRUCTURES), help="shift structure (default three)")
    ap.add_argument("--people", type=int, required=True, help="number of people taking part")
    ap.add_argument("--days", type=int, required=True, help="number of weekdays in the cycle")
    ap.add_argument("--start", default="2026-11-02", help="first day (YYYY-MM-DD)")
    ap.add_argument("--skip", nargs="*", default=[], help="dates to leave out, e.g. holidays (YYYY-MM-DD)")
    ap.add_argument("--max-per-day", type=int, default=1, help="shifts per person per day (default 1)")
    ap.add_argument("--out", help="write the packages JSON here (for oncall_market.py --packages)")
    args = ap.parse_args(argv)
    try:
        dates = om.weekdays(date.fromisoformat(args.start), args.days, {date.fromisoformat(s) for s in args.skip})
        packages = derive_packages(args.structure, args.people, dates, max_per_day=args.max_per_day)
    except (SchedulingError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(describe(packages, args.structure))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(packages, fh, indent=2)
            fh.write("\n")
        print("Wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
