"""Rules for the participant form: cleaning cells, the minimum-rank check (decision Q7) and
the point preview that uses the same linear rule as om.preference_values.  No Streamlit."""

from __future__ import annotations

import oncall_market as om


def normalise_cell(raw, mode="rank"):
    """"" for a blank, "X" for any unavailable token, "3" for a rank (or "2.5" for points).
    Raises ValueError with a readable message for anything else."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.lower() in om.UNAVAILABLE_TOKENS:
        return "X"
    try:
        number = float(text)
    except ValueError:
        raise ValueError("'%s' is not a rank number or X" % text)
    if mode == "rank":
        if number != int(number) or number < 1:
            raise ValueError("'%s' is not a whole number from 1 upwards" % text)
        return str(int(number))
    if number < 0:
        raise ValueError("points cannot be negative")
    return "%g" % number


def count_ranked(cells):
    return sum(1 for v in cells.values() if v and v != "X")


def validate_cells(raw_cells, min_ranks, mode="rank"):
    """(clean cells, errors).  raw_cells is {slot_id: text as typed}; clean cells omit blanks.
    errors is a list of (slot_id or None, message); an empty list means the column can be saved."""
    clean, errors = {}, []
    for slot_id, raw in raw_cells.items():
        try:
            value = normalise_cell(raw, mode)
        except ValueError as e:
            errors.append((slot_id, str(e)))
            continue
        if value:
            clean[slot_id] = value
    n_ranked = count_ranked(clean)
    if n_ranked < int(min_ranks):
        word = "rank" if mode == "rank" else "give points to"
        errors.append((None, "Please %s at least %d slots (you did %d). X does not count." % (word, int(min_ranks), n_ranked)))
    return clean, errors


def point_values(cells, mode="rank", budget=om.BUDGET):
    """What each entry is worth, by the rule in om.preference_values: in rank mode the distinct
    ranks get values R, R-1, ..., 1 (top rank highest), in points mode the numbers are used as
    they are; then the person's values are scaled so they add up to `budget`.

    Returns [(entry, points, how_many_slots)] sorted best first; empty if nothing is ranked."""
    entries = [v for v in cells.values() if v and v != "X"]
    if not entries:
        return []
    if mode == "rank":
        numbers = [int(v) for v in entries]
        distinct = sorted(set(numbers))
        R = len(distinct)
        value = {r: float(R - k) for k, r in enumerate(distinct)}
    else:
        numbers = [float(v) for v in entries]
        distinct = sorted(set(numbers), reverse=True)
        value = {v: max(0.0, v) for v in distinct}
    total = sum(value[n] for n in numbers)
    if total <= 0:
        return []
    out = []
    for r in distinct:
        n = numbers.count(r)
        out.append((r, value[r] * budget / total, n))
    return out
