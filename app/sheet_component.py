"""The participant's preference sheet as a Streamlit custom component with no build step.

app/components/sheet/ holds index.html, sheet.css and sheet.js: the one-column version of
the sheet in oncall_gui.py's PAGE string, talking the component protocol by hand.  This
module is the Python side: the declare_component call and the args builder.  Validation
and saving are not here; they stay in app.prefs and app.repo.

The component receives, as args: slots (one JSON-friendly dict per app.solver_adapter
.slot_table() row), cells (the saved {slot_id: "3" | "X"}), person, min_ranks, mode and
readonly.  It reports its value, the current cells, after every change; Streamlit returns
that value (or None until the browser has loaded the sheet) from render_sheet()."""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

COMPONENT_NAME = "oncall_sheet"
COMPONENT_DIR = Path(__file__).resolve().parent / "components" / "sheet"
COMPONENT_FILES = ("index.html", "sheet.css", "sheet.js")
SLOT_FIELDS = ("id", "date", "weekday", "short", "code", "label", "time", "hours", "first_of_day", "gap_before")

_component = None


def component():
    """The declared component.  Declared on first use rather than at import time, because
    Streamlit only registers a component when a script run is in progress."""
    global _component
    if _component is None:
        _component = components.declare_component(COMPONENT_NAME, path=str(COMPONENT_DIR))
    return _component


def slot_entry(slot):
    """The JSON-friendly form of one slot_table() row (dates become ISO strings)."""
    return {
        "id": slot["id"], "date": slot["date"].isoformat(), "weekday": slot["weekday"], "short": slot["short"],
        "code": slot["code"], "label": slot["label"], "time": slot["time"], "hours": slot["hours"],
        "first_of_day": bool(slot["first_of_day"]), "gap_before": bool(slot["gap_before"]),
    }


def sheet_args(slots, cells, person, min_ranks, mode="rank", readonly=False):
    """Everything the component needs, as plain JSON-serialisable values."""
    return {
        "slots": [slot_entry(s) for s in slots],
        "cells": {str(k): str(v) for k, v in (cells or {}).items() if str(v or "").strip()},
        "person": str(person), "min_ranks": int(min_ranks), "mode": mode, "readonly": bool(readonly),
    }


def render_sheet(slots, cells, person, min_ranks, mode="rank", readonly=False, key="sheet"):
    """Show the sheet for one person.  Returns the cells the sheet last reported
    ({slot_id: text}), or None until the browser has loaded it (and always in readonly mode,
    where nothing is reported)."""
    args = sheet_args(slots, cells, person, min_ranks, mode, readonly)
    value = component()(key=key, default=None, **args)
    if readonly or not isinstance(value, dict):
        return None
    return {str(k): str(v) for k, v in value.items()}
