"""Plain functions for every read and write the views need.  Each takes a Session as its
first argument, commits its own writes, and returns plain dicts (never ORM objects), so the
Streamlit views and the tests see the same JSON-friendly data.  No Streamlit imports.

Cycle states: draft -> collecting -> closed -> solved -> published.  "Reopen" goes back to
collecting; a new run drops the state back to closed until an outcome is chosen; unpublish
goes back to solved."""

from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import select

from app.models import Cycle, Edit, Participant, Publication, Run, Submission, utcnow

TOKEN_BYTES = 24        # participant links: 32 URL-safe characters
ADMIN_TOKEN_BYTES = 32  # organiser link: 43 characters


class RepoError(Exception):
    """A request that the current state does not allow (shown to the user without a traceback)."""


def new_token(nbytes=TOKEN_BYTES):
    return secrets.token_urlsafe(nbytes)


def next_monday(today=None):
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7)


def default_min_ranks(structure):
    """The decision for Q7: the largest package size for the structure plus two."""
    from app.solver_adapter import default_min_ranks as _default
    return _default(structure)


# ---------------------------------------------------------------------------
# dict views of the rows
# ---------------------------------------------------------------------------

def _cycle_dict(c):
    return {
        "id": c.id, "name": c.name, "structure": c.structure, "start_date": c.start_date,
        "n_days": c.n_days, "skip_dates": list(c.skip_dates or []), "max_per_day": c.max_per_day,
        "mode": c.mode, "min_ranks": c.min_ranks, "state": c.state,
        "admin_token": c.admin_token, "public_token": c.public_token, "base_url": c.base_url or "",
        "deadline_note": c.deadline_note or "", "packages_json": c.packages_json,
        "created_at": c.created_at,
    }


def _participant_dict(p, sub=None):
    cells = (sub.cells or {}) if sub else None
    return {
        "id": p.id, "cycle_id": p.cycle_id, "name": p.name, "token": p.token, "budget": p.budget,
        "created_at": p.created_at,
        "saved_at": sub.saved_at if sub else None,
        "n_ranked": sum(1 for v in cells.values() if v and v != "X") if cells is not None else None,
        "n_unavailable": sum(1 for v in cells.values() if v == "X") if cells is not None else None,
    }


def _run_dict(r):
    return {
        "id": r.id, "cycle_id": r.cycle_id, "created_at": r.created_at, "seeds": list(r.seeds or []),
        "params": dict(r.params or {}), "candidates": list(r.candidates or []),
        "chosen_index": r.chosen_index, "excluded_names": list(r.excluded_names or []),
        "packages_used": dict(r.packages_used or {}),
    }


def _edit_dict(e):
    return {"id": e.id, "cycle_id": e.cycle_id, "run_id": e.run_id, "created_at": e.created_at,
            "description": e.description, "owner_before": list(e.owner_before or []),
            "owner_after": list(e.owner_after or [])}


def _publication_dict(p):
    return {"id": p.id, "cycle_id": p.cycle_id, "run_id": p.run_id, "published_at": p.published_at,
            "schedule_rows": list(p.schedule_rows or []), "summary_rows": list(p.summary_rows or []),
            "edits_log": list(p.edits_log or [])}


def _get_cycle_row(s, cycle_id):
    c = s.get(Cycle, cycle_id)
    if c is None:
        raise RepoError("This cycle no longer exists.")
    return c


def _require_state(c, *states):
    if c.state not in states:
        raise RepoError("Not allowed while the cycle is '%s' (needs %s)." % (c.state, " or ".join(states)))


# ---------------------------------------------------------------------------
# cycles
# ---------------------------------------------------------------------------

def create_cycle(s, name, *, structure="three", start_date=None, n_days=9, skip_dates=(), max_per_day=1,
                 mode="rank", min_ranks=None, base_url="", deadline_note="", names=()):
    """Create a cycle in the draft state with fresh admin and public tokens."""
    name = (name or "").strip()
    if not name:
        raise RepoError("Give the cycle a name.")
    c = Cycle(name=name, structure=structure, start_date=start_date or next_monday(), n_days=int(n_days),
              skip_dates=[str(d) for d in skip_dates], max_per_day=int(max_per_day), mode=mode,
              min_ranks=int(min_ranks) if min_ranks is not None else default_min_ranks(structure),
              state="draft", admin_token=new_token(ADMIN_TOKEN_BYTES), public_token=new_token(),
              base_url=base_url or "", deadline_note=deadline_note or "")
    s.add(c)
    s.flush()
    for n in names:
        s.add(Participant(cycle_id=c.id, name=n, token=new_token()))
    s.commit()
    return _cycle_dict(c)


def get_cycle(s, cycle_id):
    c = s.get(Cycle, cycle_id)
    return _cycle_dict(c) if c else None


def get_cycle_by_admin_token(s, token):
    c = s.scalar(select(Cycle).where(Cycle.admin_token == token))
    return _cycle_dict(c) if c else None


def get_cycle_by_public_token(s, token):
    c = s.scalar(select(Cycle).where(Cycle.public_token == token))
    return _cycle_dict(c) if c else None


SETUP_FIELDS = ("name", "structure", "start_date", "n_days", "skip_dates", "max_per_day", "mode", "min_ranks")
NOTE_FIELDS = ("base_url", "deadline_note", "packages_json")


def update_cycle_setup(s, cycle_id, **fields):
    """Change the period settings; only while the cycle is a draft."""
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "draft")
    for k, v in fields.items():
        if k not in SETUP_FIELDS:
            raise RepoError("Unknown setup field %r." % k)
        if k == "skip_dates":
            v = [str(d) for d in v]
        setattr(c, k, v)
    s.commit()
    return _cycle_dict(c)


def update_cycle_notes(s, cycle_id, **fields):
    """Change the fields that are harmless in any state: base_url, deadline_note, packages_json."""
    c = _get_cycle_row(s, cycle_id)
    for k, v in fields.items():
        if k not in NOTE_FIELDS:
            raise RepoError("Unknown field %r." % k)
        setattr(c, k, v)
    s.commit()
    return _cycle_dict(c)


def delete_cycle(s, cycle_id):
    c = _get_cycle_row(s, cycle_id)
    s.delete(c)
    s.commit()


# ---------------------------------------------------------------------------
# participants and submissions
# ---------------------------------------------------------------------------

def set_participants(s, cycle_id, names):
    """Make the participant list equal to `names` (draft only).  People who keep their name
    keep their token; new names get a token; removed names are deleted."""
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "draft")
    clean = []
    for n in names:
        n = " ".join(str(n).split())
        if n and n not in clean:
            clean.append(n)
    existing = {p.name: p for p in c.participants}
    for name, p in existing.items():
        if name not in clean:
            s.delete(p)
    for name in clean:
        if name not in existing:
            s.add(Participant(cycle_id=c.id, name=name, token=new_token()))
    s.commit()
    return list_participants(s, cycle_id)


def list_participants(s, cycle_id):
    rows = s.execute(
        select(Participant, Submission)
        .outerjoin(Submission, Submission.participant_id == Participant.id)
        .where(Participant.cycle_id == cycle_id)
        .order_by(Participant.id)).all()
    return [_participant_dict(p, sub) for p, sub in rows]


def get_participant_by_token(s, token):
    """(participant dict, cycle dict) for a personal link, or None."""
    p = s.scalar(select(Participant).where(Participant.token == token))
    if p is None:
        return None
    sub = s.get(Submission, p.id)
    return _participant_dict(p, sub), _cycle_dict(s.get(Cycle, p.cycle_id))


def set_budget(s, participant_id, budget):
    p = s.get(Participant, participant_id)
    if p is None:
        raise RepoError("Unknown participant.")
    p.budget = float(budget)
    s.commit()


def regenerate_token(s, participant_id):
    p = s.get(Participant, participant_id)
    if p is None:
        raise RepoError("Unknown participant.")
    p.token = new_token()
    s.commit()
    return p.token


def get_submission(s, participant_id):
    """{"cells": {...}, "saved_at": datetime} or None."""
    sub = s.get(Submission, participant_id)
    if sub is None:
        return None
    return {"cells": dict(sub.cells or {}), "saved_at": sub.saved_at}


def upsert_submission(s, participant_id, cells):
    """Save a participant's column (only while collecting).  cells: {slot_id: "3" | "X"}."""
    p = s.get(Participant, participant_id)
    if p is None:
        raise RepoError("Unknown participant.")
    c = s.get(Cycle, p.cycle_id)
    _require_state(c, "collecting")
    clean = {k: str(v) for k, v in cells.items() if str(v).strip()}
    sub = s.get(Submission, participant_id)
    if sub is None:
        sub = Submission(participant_id=participant_id, cells=clean, saved_at=utcnow())
        s.add(sub)
    else:
        sub.cells = clean
        sub.saved_at = utcnow()
    s.commit()
    return {"cells": clean, "saved_at": sub.saved_at}


def clear_submission(s, participant_id):
    sub = s.get(Submission, participant_id)
    if sub is not None:
        s.delete(sub)
        s.commit()


def submissions_by_name(s, cycle_id):
    """{name: cells} for everyone in the cycle who has saved."""
    rows = s.execute(
        select(Participant.name, Submission.cells)
        .join(Submission, Submission.participant_id == Participant.id)
        .where(Participant.cycle_id == cycle_id)
        .order_by(Participant.id)).all()
    return {name: dict(cells or {}) for name, cells in rows}


# ---------------------------------------------------------------------------
# state changes
# ---------------------------------------------------------------------------

def open_collection(s, cycle_id):
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "draft")
    if not c.participants:
        raise RepoError("Add at least one name before opening collection.")
    c.state = "collecting"
    s.commit()
    return _cycle_dict(c)


def close_collection(s, cycle_id):
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "collecting")
    c.state = "closed"
    s.commit()
    return _cycle_dict(c)


def reopen_collection(s, cycle_id):
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "closed", "solved")
    c.state = "collecting"
    s.commit()
    return _cycle_dict(c)


# ---------------------------------------------------------------------------
# runs, choices, edits
# ---------------------------------------------------------------------------

def create_run(s, cycle_id, *, seeds, params, candidates, excluded_names, packages_used):
    """Store a market run.  The state goes (back) to closed until an outcome is chosen."""
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "closed", "solved")
    r = Run(cycle_id=cycle_id, seeds=list(seeds), params=params, candidates=candidates,
            excluded_names=list(excluded_names), packages_used=packages_used)
    s.add(r)
    c.state = "closed"
    s.commit()
    return _run_dict(r)


def get_run(s, run_id):
    r = s.get(Run, run_id)
    return _run_dict(r) if r else None


def get_latest_run(s, cycle_id):
    r = s.scalar(select(Run).where(Run.cycle_id == cycle_id).order_by(Run.id.desc()).limit(1))
    return _run_dict(r) if r else None


def choose_candidate(s, run_id, index):
    r = s.get(Run, run_id)
    if r is None:
        raise RepoError("Unknown run.")
    if not 0 <= int(index) < len(r.candidates or []):
        raise RepoError("No outcome number %s in this run." % index)
    c = _get_cycle_row(s, r.cycle_id)
    _require_state(c, "closed", "solved")
    if r.chosen_index != int(index):
        # choosing a different outcome discards the edits made on the previous one
        for e in list(r.edits):
            s.delete(e)
    r.chosen_index = int(index)
    c.state = "solved"
    s.commit()
    return _run_dict(r)


def add_edit(s, run_id, description, owner_before, owner_after):
    r = s.get(Run, run_id)
    if r is None:
        raise RepoError("Unknown run.")
    if r.chosen_index is None:
        raise RepoError("Choose an outcome before editing it.")
    c = _get_cycle_row(s, r.cycle_id)
    _require_state(c, "solved")
    e = Edit(cycle_id=r.cycle_id, run_id=run_id, description=description,
             owner_before=list(owner_before), owner_after=list(owner_after))
    s.add(e)
    s.commit()
    return _edit_dict(e)


def list_edits(s, run_id):
    rows = s.scalars(select(Edit).where(Edit.run_id == run_id).order_by(Edit.id)).all()
    return [_edit_dict(e) for e in rows]


def current_owner(s, run_id):
    """The chosen outcome's owner list with every logged edit applied, or None."""
    r = s.get(Run, run_id)
    if r is None or r.chosen_index is None:
        return None
    last = s.scalar(select(Edit).where(Edit.run_id == run_id).order_by(Edit.id.desc()).limit(1))
    if last is not None:
        return list(last.owner_after)
    return list(r.candidates[r.chosen_index]["owner"])


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------

def publish(s, cycle_id, run_id, schedule_rows, summary_rows, edits_log):
    """Publish (replacing any earlier publication of this cycle)."""
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "solved", "published")
    for old in list(c.publications):
        s.delete(old)
    p = Publication(cycle_id=cycle_id, run_id=run_id, schedule_rows=schedule_rows,
                    summary_rows=summary_rows, edits_log=edits_log)
    s.add(p)
    c.state = "published"
    s.commit()
    return _publication_dict(p)


def unpublish(s, cycle_id):
    c = _get_cycle_row(s, cycle_id)
    _require_state(c, "published")
    for old in list(c.publications):
        s.delete(old)
    c.state = "solved"
    s.commit()
    return _cycle_dict(c)


def get_publication(s, cycle_id):
    p = s.scalar(select(Publication).where(Publication.cycle_id == cycle_id)
                 .order_by(Publication.id.desc()).limit(1))
    return _publication_dict(p) if p else None
