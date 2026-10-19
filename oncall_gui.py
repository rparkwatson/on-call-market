#!/usr/bin/env python3
"""
oncall_gui.py - a simple window for the on-call market scheduler
================================================================

    python oncall_gui.py            (opens http://127.0.0.1:8765 in your browser)
    python oncall_gui.py --port 9000 --no-browser

Runs a small local web page (standard library only; oncall_market.py must be in
the same folder).  Nothing leaves your machine: the page only talks to this
script on 127.0.0.1.

The page walks through the same three steps as the command-line tool:
set up the period, fill in the preferences sheet (ranks, or X for unavailable),
solve.  It can load and download the same preferences.csv the command-line tool
uses, and download schedule.csv and summary.csv.
"""

import argparse
import json
import random
import re
import socket
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import oncall_market as om

STRUCTURE_LABELS = [
    ("three", "Three shifts a day: 8-12, 1-5:30, 6-8"),
    ("two", "Two shifts a day: 8-12, 1-8"),
    ("single", "Five 2-hour blocks a day: 8-10, 10-12, 1-3, 3:30-5:30, 6-8"),
]

LAST = {}  # last solve, kept for the download links


# ---------------------------------------------------------------------------
# Helpers that turn the page's JSON into library calls
# ---------------------------------------------------------------------------

def make_problem(setup):
    structure = setup.get("structure", "three")
    if structure not in om.STRUCTURES:
        raise om.SchedulingError("Unknown shift structure %r." % structure)
    try:
        start = date.fromisoformat(str(setup.get("start", "")).strip())
    except ValueError:
        raise om.SchedulingError("First day must be a date like 2026-11-02.")
    try:
        days = int(setup.get("days", 9))
    except (TypeError, ValueError):
        raise om.SchedulingError("Weekdays to cover must be a whole number.")
    if not 1 <= days <= 60:
        raise om.SchedulingError("Weekdays to cover must be between 1 and 60.")
    skip = set()
    for token in re.split(r"[,\s]+", str(setup.get("skip", ""))):
        if token:
            try:
                skip.add(date.fromisoformat(token))
            except ValueError:
                raise om.SchedulingError("Dates to skip must look like 2026-11-11 (got %r)." % token)
    people_raw = setup.get("people", "")
    if isinstance(people_raw, list):
        people = [str(p).strip() for p in people_raw]
    else:
        people = [p.strip() for p in str(people_raw).split(",")]
    people = [p for p in people if p]
    if not people:
        raise om.SchedulingError("Enter at least one name under People.")
    if len(set(people)) != len(people):
        raise om.SchedulingError("Each person needs a different name.")
    try:
        max_per_day = int(setup.get("max_per_day", 1))
    except (TypeError, ValueError):
        raise om.SchedulingError("Shifts per day must be a whole number.")
    return om.Problem(structure, om.weekdays(start, days, skip), people, max_per_day=max_per_day)


def slot_rows_from_cells(problem, cells):
    rows = []
    for s in problem.slots:
        per = cells.get(s["id"], {}) if isinstance(cells, dict) else {}
        rows.append({"id": s["id"], "date": s["date"], "code": s["code"],
                     "cells": [str(per.get(p, "") or "").strip() for p in problem.people]})
    return rows


def cells_matrix(problem, cells):
    rows = slot_rows_from_cells(problem, cells)
    return [[rows[j]["cells"][i] for j in range(len(problem.slots))] for i in range(len(problem.people))]


def package_descriptions(problem):
    out = []
    for name, pk in problem.packages.items():
        parts = []
        for ci, cat in enumerate(problem.cats):
            lo, hi = pk["min"][ci], pk["max"][ci]
            if hi == 0:
                continue
            codes = "/".join(problem.st["categories"][cat])
            parts.append(("%d" % lo if lo == hi else "%d-%d" % (lo, hi)) + " x " + codes)
        hours = sum(pk["min"][ci] * problem.st["shifts"][[sh["code"] for sh in problem.st["shifts"]].index(
            problem.st["categories"][cat][0])]["hours"] for ci, cat in enumerate(problem.cats))
        out.append({"name": name, "count": pk["count"], "contents": ", ".join(parts), "hours": hours})
    return out


def sheet_payload(problem):
    return {
        "description": problem.st["description"],
        "people": problem.people,
        "shifts": [{"code": sh["code"], "label": sh["label"], "time": sh["time"], "hours": sh["hours"]} for sh in problem.shifts],
        "slots": [{"id": s["id"], "date": s["date"].isoformat(), "weekday": s["date"].strftime("%a"), "short": om.fmt_date(s["date"]),
                   "day": s["day"], "code": s["code"], "label": s["label"], "time": s["time"], "hours": s["hours"],
                   "gap_before": s["day"] > 0 and (problem.dates[s["day"]] - problem.dates[s["day"] - 1]).days > 1}
                  for s in problem.slots],
        "packages": package_descriptions(problem),
    }


def run_solve(body):
    problem = make_problem(body.get("setup", {}))
    mode = body.get("mode", "rank")
    if mode not in ("rank", "points"):
        raise om.SchedulingError("Cells must contain ranks or points.")
    market_roles = body.get("roles_mode", "market") != "fixed"
    roles = None
    if not market_roles:
        chosen = body.get("roles", {}) or {}
        roles = [chosen.get(p) or None for p in problem.people]
        missing = [p for p, r in zip(problem.people, roles) if r is None]
        if missing:
            raise om.SchedulingError("Pick a package for: " + ", ".join(missing))
    slot_rows = slot_rows_from_cells(problem, body.get("cells", {}))
    values, avail = om.preference_values(problem, slot_rows, mode, random.Random(1))
    result = om.solve(problem, values, avail, market_roles, roles)
    text, summary_rows, schedule_rows = om.build_report(problem, values, avail, result)
    LAST.update({"problem": problem, "values": values, "avail": avail, "result": result})

    prices = result["prices"]
    price_rows = []
    for d, dt in enumerate(problem.dates):
        price_rows.append([om.fmt_date(dt)] + [round(prices[j], 1) for j in problem.slots_by_day[d]])
    if result["cleared"]:
        status = ("The market cleared exactly in %d rounds: everyone holds the bundle they would pick for "
                  "themselves at these prices." % result["rounds"])
    else:
        status = ("Prices support this assignment with a maximum regret of %.2f points "
                  "(0 = exact equilibrium; under 0.1 is as good as exact)." % result["max_regret"])
    if any(r["violations"] for r in summary_rows):
        status += (" WARNING: the best schedule found still breaks a rule - usually the X's leave a day "
                   "short-handed. Allow 2 shifts per day or ask for fewer X's.")
    return {
        "status": status,
        "cleared": result["cleared"],
        "schedule": {"columns": ["Date"] + [sh["label"] for sh in problem.shifts],
                     "rows": [[label] + cells for label, cells in schedule_rows]},
        "people": summary_rows,
        "prices": {"columns": ["Date"] + [sh["code"] for sh in problem.shifts], "rows": price_rows},
        "pkg_prices": {k: round(v, 1) for k, v in result["pkg_prices"].items()} if result["market_roles"] else None,
        "report": text,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise om.SchedulingError("The page sent something this program could not read.")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/download/schedule.csv" or self.path == "/api/download/summary.csv":
            if "result" not in LAST:
                self._send(404, "Solve first, then download.", "text/plain; charset=utf-8")
                return
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                sched, summ = om.write_outputs(LAST["problem"], LAST["values"], LAST["avail"], LAST["result"], tmp)
                path = sched if self.path.endswith("schedule.csv") else summ
                with open(path, "rb") as f:
                    data = f.read()
            name = self.path.rsplit("/", 1)[-1]
            self._send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": "attachment; filename=%s" % name})
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/sheet":
                problem = make_problem(body.get("setup", {}))
                self._json(200, sheet_payload(problem))
            elif self.path == "/api/solve":
                self._json(200, run_solve(body))
            elif self.path == "/api/export":
                problem = make_problem(body.get("setup", {}))
                text = om.preferences_text(problem, cells_matrix(problem, body.get("cells", {})))
                self._send(200, text, "text/csv; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=preferences.csv"})
            elif self.path == "/api/import":
                people, slot_rows = om.parse_preferences(str(body.get("text", "")))
                structure = om.infer_structure(slot_rows)
                dates = sorted({r["date"] for r in slot_rows})
                all_weekdays = om.weekdays(dates[0], 400)
                skip = [d.isoformat() for d in all_weekdays if dates[0] <= d <= dates[-1] and d not in set(dates)]
                cells = {}
                for r in slot_rows:
                    cells[r["id"]] = {p: c for p, c in zip(people, r["cells"]) if c}
                self._json(200, {"setup": {"structure": structure, "start": dates[0].isoformat(), "days": len(dates),
                                           "skip": ", ".join(skip), "people": ", ".join(people)},
                                 "cells": cells})
            else:
                self._json(404, {"error": "Unknown action."})
        except om.SchedulingError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # keep the page alive, show what happened
            self._json(500, {"error": "Unexpected error: %r" % e})


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>On-call market scheduler</title>
<style>
  :root {
    --ink: #1d2733; --muted: #5c6670; --line: #d6d9dc; --paper: #ffffff; --wash: #f3f4f2;
    --accent: #0f6e56; --accent-ink: #ffffff; --x: #e9eaea; --mark: #e1f5ee;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink); background: var(--paper); }
  header, main { max-width: 1280px; margin: 0 auto; padding: 0 24px; }
  header { padding-top: 28px; }
  h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px; }
  .lede { margin: 0 0 8px; color: var(--muted); max-width: 70ch; }
  section { border-top: 1px solid var(--line); padding: 20px 0 28px; }
  h2 { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
  h3 { font-size: 15px; font-weight: 600; margin: 20px 0 8px; }
  p { max-width: 80ch; }
  .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px 20px; max-width: 980px; }
  .form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; color: var(--muted); }
  .form-grid .wide { grid-column: 1 / -1; }
  input, select, button { font: inherit; }
  input[type=text], input[type=date], input[type=number], select {
    padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); background: var(--paper); }
  input:focus, select:focus, button:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  button { padding: 7px 14px; border: 1px solid var(--line); border-radius: 6px; background: var(--wash); color: var(--ink); cursor: pointer; }
  button:hover { border-color: var(--muted); }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; margin: 12px 0; }
  .toolbar label { font-size: 14px; }
  .file { position: relative; overflow: hidden; display: inline-block; }
  .file input { position: absolute; inset: 0; opacity: 0; pointer-events: none; }
  .msg { color: #9b1c1c; margin-left: 10px; }
  .ok { color: var(--accent); }
  .packages { margin: 14px 0 4px; font-size: 14px; color: var(--muted); }
  .packages b { color: var(--ink); font-weight: 600; }
  #roles-box { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 16px; margin: 10px 0 4px; max-width: 980px; }
  #roles-box label { display: flex; flex-direction: column; gap: 3px; font-size: 14px; color: var(--muted); }
  .table-wrap { overflow: auto; max-height: 68vh; border: 1px solid var(--line); border-radius: 6px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { padding: 5px 6px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
  th.person { white-space: normal; max-width: 110px; vertical-align: top; }
  th .clear { display: block; }
  th { position: sticky; top: 0; background: var(--wash); font-weight: 600; z-index: 1; }
  th .clear { font-size: 12px; font-weight: 400; color: var(--muted); background: none; border: none; padding: 0; cursor: pointer; }
  th .clear:hover { color: var(--ink); }
  td.dim { color: var(--muted); }
  tr.gap td { background: var(--wash); color: var(--muted); font-size: 13px; padding: 3px 8px; }
  tr.dayend td { border-bottom: 2px solid var(--line); }
  td.cell input { width: 48px; text-align: center; padding: 4px 2px; }
  td.time { font-size: 12.5px; }
  td.cell input.rank { background: var(--mark); border-color: #9fe1cb; }
  td.cell input.x { background: var(--x); color: var(--muted); }
  #results .table-wrap { display: inline-block; max-width: 100%; max-height: none; vertical-align: top; }
  #results table { width: auto; }
  #results td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #results td.neg { color: #993c1d; }
  #results td.pos { color: var(--accent); }
  pre { background: var(--wash); padding: 12px; border-radius: 6px; overflow: auto; font-size: 13px; }
  details summary { cursor: pointer; color: var(--muted); }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
</style>
</head>
<body>
<header>
  <h1>On-call market scheduler</h1>
  <p class="lede">Rank the slots you want, mark the ones you can't do, and let the market price and assign the rest.</p>
</header>
<main>
  <section id="setup">
    <h2>1. Set up the period</h2>
    <div class="form-grid">
      <label>Shift structure
        <select id="structure"></select></label>
      <label>First day
        <input type="date" id="start" value="2026-11-02"></label>
      <label>Weekdays to cover
        <input type="number" id="days" value="9" min="1" max="60"></label>
      <label>Dates to skip (holidays)
        <input type="text" id="skip" placeholder="2026-11-11, 2026-11-26"></label>
      <label class="wide">People, separated by commas
        <input type="text" id="people" value="Person A, Person B, Person C, Person D, Person E, Person F, Person G"></label>
      <label>Shifts one person may take on the same day
        <select id="maxperday"><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
      <label>Cells will contain
        <select id="mode"><option value="rank">ranks (1 = the slot you want most)</option><option value="points">points (higher = want more)</option></select></label>
      <label>Who takes which package
        <select id="rolesmode"><option value="market">the market decides and prices it</option><option value="fixed">I assign the packages</option></select></label>
    </div>
    <div class="packages" id="packages-info"></div>
    <div id="roles-box" hidden></div>
    <div class="toolbar">
      <button id="build" class="primary">Build sheet</button>
      <span id="setup-msg" class="msg"></span>
    </div>
  </section>

  <section id="prefs" hidden>
    <h2>2. Fill in preferences</h2>
    <p id="instructions"></p>
    <div class="toolbar">
      <span class="file"><button type="button" id="load-btn">Load CSV</button><input type="file" id="load" accept=".csv,text/csv" aria-label="Load CSV"></span>
      <button id="export">Download CSV</button>
      <button id="clear">Clear all</button>
      <label><input type="checkbox" id="clickrank"> Click to rank (each click on an empty cell fills in the next number)</label>
      <button id="solve" class="primary">Solve</button>
      <span id="prefs-msg" class="msg"></span>
    </div>
    <div class="table-wrap"><table id="sheet"></table></div>
  </section>

  <section id="results" hidden>
    <h2>3. Results</h2>
    <p id="status"></p>
    <h3>Schedule</h3>
    <div class="table-wrap"><table id="schedule"></table></div>
    <h3>People</h3>
    <div class="table-wrap"><table id="people-table"></table></div>
    <h3>Clearing prices (points; positive = sought after, negative = the slot pays you to take it)</h3>
    <div class="table-wrap"><table id="prices"></table></div>
    <p id="pkg-prices"></p>
    <p><a href="/api/download/schedule.csv">Download schedule.csv</a> &nbsp;&middot;&nbsp; <a href="/api/download/summary.csv">Download summary.csv</a></p>
    <details><summary>Full text report</summary><pre id="report"></pre></details>
  </section>
</main>

<script>
(function () {
  "use strict";
  var STRUCTURES = __STRUCTURES__;
  var state = { sheet: null, cells: {} };   // cells[slotId][person] = "3" | "X" | ""

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); }); }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) {
        var ct = r.headers.get("Content-Type") || "";
        if (ct.indexOf("application/json") === 0) {
          return r.json().then(function (j) { if (!r.ok) { throw new Error(j.error || "Request failed"); } return j; });
        }
        if (!r.ok) { return r.text().then(function (t) { throw new Error(t || "Request failed"); }); }
        return r.text();
      });
  }
  function setup() {
    return {
      structure: $("structure").value, start: $("start").value, days: $("days").value, skip: $("skip").value,
      people: $("people").value, max_per_day: $("maxperday").value
    };
  }
  function people() { return state.sheet ? state.sheet.people : []; }
  function getCell(slotId, person) { return (state.cells[slotId] || {})[person] || ""; }
  function setCell(slotId, person, v) {
    if (!state.cells[slotId]) { state.cells[slotId] = {}; }
    if (v) { state.cells[slotId][person] = v; } else { delete state.cells[slotId][person]; }
  }
  function cleanValue(v) {
    v = (v || "").trim();
    if (/^x+$/i.test(v)) { return "X"; }
    v = v.replace(/[^0-9.]/g, "");
    if ($("mode").value === "rank") { v = v.replace(/\..*/, ""); }
    return v;
  }
  function styleInput(inp) {
    var v = inp.value;
    inp.classList.toggle("x", v === "X");
    inp.classList.toggle("rank", v !== "" && v !== "X");
  }

  // ---- setup ------------------------------------------------------------
  STRUCTURES.forEach(function (s) { $("structure").appendChild(el("option", { value: s[0] }, s[1])); });
  $("rolesmode").addEventListener("change", renderRoles);

  function describePackages(pk) {
    var box = $("packages-info");
    box.textContent = "";
    if (!pk || !pk.length) { return; }
    box.appendChild(document.createTextNode("Packages for this structure: "));
    pk.forEach(function (p, i) {
      var b = el("b", null, p.name);
      box.appendChild(b);
      box.appendChild(document.createTextNode(" (" + p.count + (p.count === 1 ? " person" : " people") + ": " + p.contents + ", " + p.hours + " h)" + (i < pk.length - 1 ? "; " : ".")));
    });
  }
  function renderRoles() {
    var box = $("roles-box");
    var fixed = $("rolesmode").value === "fixed";
    box.hidden = !fixed || !state.sheet;
    if (box.hidden) { return; }
    var current = {};
    box.querySelectorAll("select").forEach(function (s) { current[s.dataset.person] = s.value; });
    box.textContent = "";
    people().forEach(function (p) {
      var lab = el("label", null, p);
      var sel = el("select", { "data-person": p });
      sel.appendChild(el("option", { value: "" }, "choose a package"));
      state.sheet.packages.forEach(function (pk) { sel.appendChild(el("option", { value: pk.name }, pk.name + " (" + pk.count + " of these)")); });
      sel.value = current[p] || "";
      lab.appendChild(sel);
      box.appendChild(lab);
    });
  }
  function roles() {
    var out = {};
    $("roles-box").querySelectorAll("select").forEach(function (s) { if (s.value) { out[s.dataset.person] = s.value; } });
    return out;
  }

  $("build").addEventListener("click", function () { buildSheet().catch(function (e) { $("setup-msg").textContent = e.message; }); });

  function buildSheet() {
    $("setup-msg").textContent = "";
    return post("/api/sheet", { setup: setup() }).then(function (sheet) {
      state.sheet = sheet;
      // drop cells for people who no longer exist
      Object.keys(state.cells).forEach(function (sid) {
        Object.keys(state.cells[sid]).forEach(function (p) { if (sheet.people.indexOf(p) < 0) { delete state.cells[sid][p]; } });
      });
      describePackages(sheet.packages);
      renderRoles();
      renderSheet();
      $("prefs").hidden = false;
      $("instructions").textContent = ($("mode").value === "rank"
        ? "In your column, write 1 next to the slot you want most, 2 next to the next, and so on - rank as many or as few as you like; only the order matters. "
        : "In your column, write how many points you would spend on each slot (higher = want more); your points are scaled to 100. ")
        + "Leave a cell blank if you are available but have no preference. Type X if you cannot do that slot - you will never be scheduled there. Tab moves down your own column.";
      $("prefs").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  // ---- sheet ------------------------------------------------------------
  function renderSheet() {
    var sheet = state.sheet, table = $("sheet");
    table.textContent = "";
    var thead = el("thead"), hr = el("tr");
    ["Date", "Shift", "Time"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    sheet.people.forEach(function (p) {
      var th = el("th", { "class": "person" }, p);
      var clr = el("button", { "class": "clear", type: "button", title: "Clear this column" }, "clear");
      clr.addEventListener("click", function () { clearColumn(p); });
      th.appendChild(clr);
      hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tbody = el("tbody");
    var nSlots = sheet.slots.length;
    var inputs = {};
    sheet.slots.forEach(function (s, si) {
      if (s.gap_before) {
        var gap = el("tr", { "class": "gap" });
        var gtd = el("td", { colspan: 3 + sheet.people.length }, "no coverage (weekend or skipped day)");
        gap.appendChild(gtd); tbody.appendChild(gap);
      }
      var first = si === 0 || sheet.slots[si - 1].day !== s.day;
      var last = si === nSlots - 1 || sheet.slots[si + 1].day !== s.day;
      var tr = el("tr", last ? { "class": "dayend" } : null);
      tr.appendChild(el("td", first ? null : { "class": "dim" }, first ? s.short : ""));
      tr.appendChild(el("td", null, s.label + " (" + s.code + ")"));
      tr.appendChild(el("td", { "class": "dim time" }, s.time + ", " + s.hours + " h"));
      sheet.people.forEach(function (p, pi) {
        var td = el("td", { "class": "cell" });
        var inp = el("input", { type: "text", maxlength: "5", "data-slot": s.id, "data-person": p,
                                tabindex: String(pi * nSlots + si + 1), "aria-label": p + ", " + s.weekday + " " + s.date + " " + s.code });
        inp.value = getCell(s.id, p);
        styleInput(inp);
        inp.addEventListener("input", function () {
          var v = cleanValue(inp.value);
          if (inp.value !== v) { inp.value = v; }
          setCell(s.id, p, v); styleInput(inp);
        });
        inp.addEventListener("click", function () {
          if ($("clickrank").checked && inp.value === "" && $("mode").value === "rank") {
            inp.value = String(nextRank(p)); setCell(s.id, p, inp.value); styleInput(inp);
          }
        });
        inp.addEventListener("keydown", function (ev) {
          var step = (ev.key === "ArrowDown" || ev.key === "Enter") ? 1 : (ev.key === "ArrowUp" ? -1 : 0);
          if (!step) { return; }
          var target = inputs[p + "\u0000" + (si + step)];
          if (target) { ev.preventDefault(); target.focus(); target.select(); }
        });
        inputs[p + "\u0000" + si] = inp;
        td.appendChild(inp); tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }
  function nextRank(person) {
    var best = 0;
    Object.keys(state.cells).forEach(function (sid) {
      var v = state.cells[sid][person];
      if (v && v !== "X") { best = Math.max(best, parseInt(v, 10) || 0); }
    });
    return best + 1;
  }
  function clearColumn(person) {
    Object.keys(state.cells).forEach(function (sid) { delete state.cells[sid][person]; });
    $("sheet").querySelectorAll("input[data-person]").forEach(function (inp) {
      if (inp.dataset.person === person) { inp.value = ""; styleInput(inp); }
    });
  }
  $("clear").addEventListener("click", function () {
    if (!confirm("Clear every cell on the sheet?")) { return; }
    state.cells = {};
    $("sheet").querySelectorAll("input[data-person]").forEach(function (inp) { inp.value = ""; styleInput(inp); });
  });
  $("mode").addEventListener("change", function () { if (state.sheet) { buildSheet().catch(function (e) { $("setup-msg").textContent = e.message; }); } });

  // ---- load / download --------------------------------------------------
  $("load-btn").addEventListener("click", function () { $("load").click(); });
  var loadNote = "";
  $("load").addEventListener("change", function () {
    var f = $("load").files[0];
    if (!f) { return; }
    var reader = new FileReader();
    reader.onload = function () {
      post("/api/import", { text: reader.result }).then(function (res) {
        var norm = function (s) { return String(s || "").split(/[,\s]+/).filter(Boolean).sort().join(","); };
        var filePeople = res.setup.people.split(",").map(function (p) { return p.trim(); }).filter(Boolean);
        var samePeriod = state.sheet && res.setup.structure === $("structure").value && res.setup.start === $("start").value
          && String(res.setup.days) === String($("days").value) && norm(res.setup.skip) === norm($("skip").value);
        if (samePeriod) {
          // same period: replace only the columns that are in the file, keep everyone else's entries
          var current = $("people").value.split(",").map(function (p) { return p.trim(); }).filter(Boolean);
          filePeople.forEach(function (p) {
            Object.keys(state.cells).forEach(function (sid) { delete state.cells[sid][p]; });
            if (current.indexOf(p) < 0) { current.push(p); }
          });
          Object.keys(res.cells).forEach(function (sid) {
            Object.keys(res.cells[sid]).forEach(function (p) { setCell(sid, p, res.cells[sid][p]); });
          });
          $("people").value = current.join(", ");
          loadNote = "Loaded " + f.name + ": columns for " + filePeople.join(", ") + " replaced; everyone else kept.";
        } else {
          $("structure").value = res.setup.structure; $("start").value = res.setup.start; $("days").value = res.setup.days;
          $("skip").value = res.setup.skip; $("people").value = res.setup.people;
          state.cells = res.cells;
          loadNote = "Loaded " + f.name + ".";
        }
        return buildSheet();
      }).then(function () { $("prefs-msg").textContent = loadNote; $("prefs-msg").className = "msg ok"; })
        .catch(function (e) { $("prefs-msg").textContent = e.message; $("prefs-msg").className = "msg"; });
      $("load").value = "";
    };
    reader.readAsText(f);
  });
  $("export").addEventListener("click", function () {
    post("/api/export", { setup: setup(), cells: state.cells }).then(function (text) {
      var blob = new Blob([text], { type: "text/csv" });
      var a = el("a", { href: URL.createObjectURL(blob), download: "preferences.csv" });
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
    }).catch(function (e) { $("prefs-msg").textContent = e.message; $("prefs-msg").className = "msg"; });
  });

  // ---- solve ------------------------------------------------------------
  $("solve").addEventListener("click", function () {
    var btn = $("solve"), msg = $("prefs-msg");
    btn.disabled = true;
    msg.className = "msg ok";
    msg.innerHTML = '<span class="spinner"></span>Finding clearing prices and assigning shifts ... this takes a few seconds (up to half a minute for the five-block structure).';
    post("/api/solve", { setup: setup(), mode: $("mode").value, roles_mode: $("rolesmode").value, roles: roles(), cells: state.cells })
      .then(function (res) { msg.textContent = ""; renderResults(res); })
      .catch(function (e) { msg.className = "msg"; msg.textContent = e.message; })
      .then(function () { btn.disabled = false; });
  });

  function fillTable(table, columns, rows, numericFrom) {
    table.textContent = "";
    var thead = el("thead"), tr = el("tr");
    columns.forEach(function (c) { tr.appendChild(el("th", null, c)); });
    thead.appendChild(tr); table.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (r) {
      var tr2 = el("tr");
      r.forEach(function (v, i) {
        var td = el("td");
        if (numericFrom !== undefined && i >= numericFrom && typeof v === "number") {
          td.className = "num " + (v > 0.05 ? "pos" : (v < -0.05 ? "neg" : ""));
          td.textContent = (v > 0 ? "+" : "") + v.toFixed(1);
        } else { td.textContent = v === null || v === undefined ? "" : String(v); }
        tr2.appendChild(td);
      });
      tbody.appendChild(tr2);
    });
    table.appendChild(tbody);
  }
  function renderResults(res) {
    $("results").hidden = false;
    $("status").textContent = res.status;
    fillTable($("schedule"), res.schedule.columns, res.schedule.rows);
    fillTable($("people-table"), ["Person", "Package", "Shifts", "Hours", "Points", "% of ideal", "Net price", "Slots"],
      res.people.map(function (p) { return [p.person, p.package, p.shifts, p.hours, p.points_received, p.pct_of_ideal, p.net_price, p.slots.split(" ").join(", ")]; }));
    fillTable($("prices"), res.prices.columns, res.prices.rows, 1);
    $("pkg-prices").textContent = res.pkg_prices
      ? "Package prices: " + Object.keys(res.pkg_prices).map(function (k) { var v = res.pkg_prices[k]; return k + " " + (v > 0 ? "+" : "") + v.toFixed(1); }).join(", ") + "."
      : "";
    $("report").textContent = res.report;
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  }
})();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__STRUCTURES__", json.dumps(STRUCTURE_LABELS))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Browser window for the on-call market scheduler.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args(argv)
    port = args.port
    server = None
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if server is None:
        print("Could not find a free port near %d." % args.port, file=sys.stderr)
        sys.exit(1)
    url = "http://127.0.0.1:%d/" % port
    print("On-call market scheduler running at %s  (press Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
