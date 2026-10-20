"""
streamlit_app.py - the hosted on-call market
============================================

    python -m streamlit run streamlit_app.py

One Streamlit app; the query string picks the view:

    ?a=<admin_token>        the organiser's dashboard for one cycle
    ?p=<participant_token>  a participant's own preferences column
    ?s=<public_token>       the published schedule
    (no parameter)          landing page: create a new cycle

Storage goes through app.repo (SQLAlchemy; Neon Postgres in production, a SQLite file
locally), the solver through app.solver_adapter.  No emails are stored anywhere: people
are identified by a name and an unguessable link.
"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

import oncall_market as om
from app import db, prefs, repo
from app import sheet_component
from app import solver_adapter as sa
from app.repo import RepoError

st.set_page_config(page_title="On-call market", page_icon=":calendar:", layout="wide",
                   initial_sidebar_state="collapsed")

STATE_LABELS = {
    "draft": "draft - add names, then open collection",
    "collecting": "collecting preferences",
    "closed": "collection closed - run the market",
    "solved": "outcome chosen - review, then publish",
    "published": "published",
}
PERSON_COLUMNS = [("person", "Person"), ("package", "Package"), ("hours", "Hours"), ("shifts", "Shifts"),
                  ("late_shifts", "Late shifts"), ("points", "Points"), ("pct_of_ideal", "% of ideal"),
                  ("net_price", "Net price"), ("violations", "Violations")]


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def _secret_database_url():
    """st.secrets["DATABASE_URL"] when a secrets file defines it, else None."""
    try:
        return st.secrets["DATABASE_URL"] if "DATABASE_URL" in st.secrets else None
    except Exception:  # no secrets file at all
        return None


@st.cache_resource(show_spinner=False)
def _session_factory(url):
    return db.connect(url)[1]


def sessions():
    """The sessionmaker for the configured database (tables created on first use)."""
    return _session_factory(db.resolve_database_url(_secret_database_url()))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def current_page_url():
    """This page's address without the query string (empty when unknown, e.g. in tests)."""
    try:
        url = st.context.url or ""
    except Exception:
        return ""
    return url.split("?")[0].split("#")[0].rstrip("/")


def link_base(cycle=None):
    base = (cycle or {}).get("base_url") or ""
    return base.rstrip("/") or current_page_url()


def make_link(base, key, token):
    return "%s/?%s=%s" % (base, key, token) if base else "?%s=%s" % (key, token)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC") if isinstance(dt, datetime) else ""


def long_date(d):
    return d.strftime("%A %d %B").replace(" 0", " ")


def period_label(cycle):
    dates = sa.cycle_dates(cycle)
    return "%s to %s, %d weekdays" % (om.fmt_date(dates[0]), om.fmt_date(dates[-1]), len(dates))


def person_table(rows, extra=()):
    out = []
    for r in rows:
        row = {label: r[key] for key, label in PERSON_COLUMNS}
        for key, label in extra:
            row[label] = r[key]
        out.append(row)
    return out


def show_grid(schedule_rows, structure):
    st.table(sa.grid_table(sa.grid_from_schedule(schedule_rows, structure)))


def show_published(cycle, publication, person_name=None):
    """Decision Q9: the schedule (and the organiser's logged changes), nothing else."""
    show_grid(publication["schedule_rows"], cycle["structure"])
    if person_name:
        mine = [r for r in publication["schedule_rows"] if r["person"] == person_name]
        if mine:
            st.markdown("**Your shifts:** " + "; ".join(
                "%s %s (%s)" % (om.fmt_date(date.fromisoformat(r["date"])), r["shift"], r["time"]) for r in mine))
        else:
            st.markdown("You have no shifts in this cycle.")
    if publication.get("edits_log"):
        with st.expander("Changes the organiser made by hand after the market ran"):
            st.table([{"When": e.get("when", ""), "Change": e["description"]} for e in publication["edits_log"]])
    st.caption("Published %s." % fmt_dt(publication["published_at"]))


def flash():
    message = st.session_state.pop("flash", None)
    if message:
        st.success(message)


# ---------------------------------------------------------------------------
# landing page
# ---------------------------------------------------------------------------

def landing_view(Session):
    st.title("On-call market")
    flash()
    st.write("Each person ranks the slots they want through a personal link. The organiser runs the market, "
             "which prices every slot so that the schedule is the one nobody could improve on at those prices, "
             "and publishes the result.")
    with st.form("create_cycle"):
        name = st.text_input("Cycle name", placeholder="e.g. On-call 2 to 12 November 2026", max_chars=200)
        submitted = st.form_submit_button("Create a new cycle", type="primary")
    if submitted:
        try:
            with Session() as s:
                cycle = repo.create_cycle(s, name, base_url=current_page_url())
        except RepoError as e:
            st.error(str(e))
        else:
            st.session_state["new_cycle"] = {"name": cycle["name"],
                                             "admin": make_link(current_page_url(), "a", cycle["admin_token"])}
    new = st.session_state.get("new_cycle")
    if new:
        st.success("Cycle '%s' created." % new["name"])
        st.warning("Save this organiser link now. It is the only way to manage the cycle and it is not shown again.")
        st.code(new["admin"], language=None)
        st.link_button("Open the organiser dashboard", new["admin"])
    st.caption("There are no accounts: anyone with this address can create a cycle, and whoever has a cycle's "
               "organiser link manages it. Participants are identified by name and personal link only; "
               "no email addresses are stored.")


# ---------------------------------------------------------------------------
# organiser dashboard
# ---------------------------------------------------------------------------

def admin_view(Session, token):
    with Session() as s:
        cycle = repo.get_cycle_by_admin_token(s, token)
    if cycle is None:
        st.error("This organiser link is not valid. The cycle may have been deleted.")
        return
    st.title(cycle["name"])
    st.caption("Organiser dashboard. State: %s. %s. %s." % (
        STATE_LABELS[cycle["state"]], sa.STRUCTURE_LABELS[cycle["structure"]], period_label(cycle)))
    flash()
    with Session() as s:
        run = repo.get_latest_run(s, cycle["id"])
    tabs = st.tabs(["Setup", "Links", "Status", "Run market", "Review and edit", "Publish", "Danger zone"])
    with tabs[0]:
        setup_tab(Session, cycle)
    with tabs[1]:
        links_tab(Session, cycle)
    with tabs[2]:
        status_tab(Session, cycle)
    with tabs[3]:
        run_tab(Session, cycle, run)
    chosen = load_chosen(Session, cycle, run)
    with tabs[4]:
        review_tab(Session, cycle, run, chosen)
    with tabs[5]:
        publish_tab(Session, cycle, run, chosen)
    with tabs[6]:
        danger_tab(Session, cycle)


def parse_skip_dates(text):
    out = []
    for token in text.replace(";", ",").replace("\n", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(date.fromisoformat(token))
        except ValueError:
            raise RepoError("Dates to skip must look like 2026-11-11 (got '%s')." % token)
    return out


def setup_tab(Session, cycle):
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
    editable = cycle["state"] == "draft"
    if not editable:
        st.info("The period and the names were locked when collection opened. The deadline note and the app "
                "address can still be changed. For a different period or group, create a new cycle.")
    with st.form("setup"):
        name = st.text_input("Cycle name", cycle["name"], max_chars=200, disabled=not editable)
        structure = st.selectbox("Shift structure", list(sa.STRUCTURE_LABELS), format_func=sa.STRUCTURE_LABELS.get,
                                 index=list(sa.STRUCTURE_LABELS).index(cycle["structure"]), disabled=not editable)
        c1, c2, c3 = st.columns(3)
        start = c1.date_input("First day", cycle["start_date"], disabled=not editable)
        n_days = c2.number_input("Weekdays to cover", 1, 40, int(cycle["n_days"]), disabled=not editable)
        max_per_day = c3.number_input("Shifts one person may take on the same day", 1, 3, int(cycle["max_per_day"]),
                                      disabled=not editable)
        skip = st.text_input("Dates to skip (holidays), separated by commas", ", ".join(cycle["skip_dates"]),
                             placeholder="2026-11-11, 2026-11-26", disabled=not editable)
        min_ranks = st.number_input("Minimum number of slots each person must rank", 1, 200, int(cycle["min_ranks"]),
                                    disabled=not editable,
                                    help="Default: the largest package size for the structure plus two.")
        names = st.text_area("Names, one per line", "\n".join(p["name"] for p in participants), height=220,
                             disabled=not editable, help="Up to 10 people. Names must be different from each other.")
        deadline_note = st.text_area("Deadline note shown to participants", cycle["deadline_note"], height=80,
                                     placeholder="Please save your preferences by Friday 30 October, 5 pm.")
        base_url = st.text_input("App address used to build the links", cycle["base_url"] or current_page_url(),
                                 help="The address of this app as people will open it, without the ?a=... part.")
        saved = st.form_submit_button("Save settings")
    if saved:
        try:
            with Session() as s:
                if editable:
                    skip_dates = parse_skip_dates(skip)
                    name_list = [n.strip() for n in names.splitlines() if n.strip()]
                    if len(set(name_list)) != len(name_list):
                        raise RepoError("Each person needs a different name.")
                    if not name.strip():
                        raise RepoError("Give the cycle a name.")
                    repo.update_cycle_setup(s, cycle["id"], name=name.strip(), structure=structure, start_date=start,
                                            n_days=int(n_days), skip_dates=skip_dates, max_per_day=int(max_per_day),
                                            min_ranks=int(min_ranks))
                    repo.set_participants(s, cycle["id"], name_list)
                repo.update_cycle_notes(s, cycle["id"], base_url=base_url.strip().rstrip("/"),
                                        deadline_note=deadline_note.strip())
        except RepoError as e:
            st.error(str(e))
        else:
            st.session_state["flash"] = "Settings saved."
            st.rerun()
    if editable:
        n = len(participants)
        if n > 10:
            st.warning("%d names: the app was designed for up to 10 people; larger groups are untested." % n)
        st.write("When the names and the period are right, open collection: the personal links become active "
                 "and the settings above lock.")
        if st.button("Open collection", type="primary", disabled=n == 0):
            try:
                with Session() as s:
                    repo.open_collection(s, cycle["id"])
            except RepoError as e:
                st.error(str(e))
            else:
                st.session_state["flash"] = "Collection is open. Send everyone their link (Links tab)."
                st.rerun()


def links_tab(Session, cycle):
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
    base = link_base(cycle)
    if not base:
        st.warning("Enter the app address under Setup so that the links below are complete.")
    if cycle["state"] == "draft":
        st.info("The personal links work once collection is open.")
    if not participants:
        st.write("No names yet. Add them under Setup.")
    else:
        st.table([{"Name": p["name"], "Personal link": make_link(base, "p", p["token"])} for p in participants])
        st.text_area("All links, one per line (copy and paste them into your messages)",
                     "\n".join("%s: %s" % (p["name"], make_link(base, "p", p["token"])) for p in participants),
                     height=min(400, 40 + 26 * len(participants)))
    st.write("Public schedule link (share it after publishing; it shows the schedule and nothing else):")
    st.code(make_link(base, "s", cycle["public_token"]), language=None)
    st.write("Your organiser link (keep it to yourself; anyone who has it can manage this cycle):")
    st.code(make_link(base, "a", cycle["admin_token"]), language=None)


def status_tab(Session, cycle):
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
    saved = [p for p in participants if p["saved_at"]]
    st.write("**%d of %d** have saved their preferences." % (len(saved), len(participants)))
    if participants:
        header = st.columns([3, 3, 3, 2, 2])
        for col, text in zip(header, ["Name", "Saved", "Entries", "", ""]):
            col.markdown("**%s**" % text)
        for p in participants:
            cols = st.columns([3, 3, 3, 2, 2])
            cols[0].write(p["name"])
            cols[1].write(fmt_dt(p["saved_at"]) if p["saved_at"] else "not yet")
            cols[2].write("%d ranked, %d X" % (p["n_ranked"], p["n_unavailable"]) if p["saved_at"] else "")
            with cols[3].popover("Clear", disabled=not p["saved_at"]):
                st.write("Delete %s's saved entries? They can fill the form in again while collection is open." % p["name"])
                if st.button("Yes, clear", key="clear_%d" % p["id"]):
                    with Session() as s:
                        repo.clear_submission(s, p["id"])
                    st.session_state["flash"] = "Cleared %s's entries." % p["name"]
                    st.rerun()
            with cols[4].popover("New link"):
                st.write("Give %s a new personal link? The old one stops working immediately." % p["name"])
                if st.button("Yes, regenerate", key="regen_%d" % p["id"]):
                    with Session() as s:
                        repo.regenerate_token(s, p["id"])
                    st.session_state["flash"] = "New link for %s (see the Links tab)." % p["name"]
                    st.rerun()
    st.divider()
    if cycle["state"] == "collecting":
        st.write("At the deadline, close collection. People who have not saved by then are excluded from "
                 "this cycle (their links show their entries read-only).")
        if st.button("Close collection", type="primary"):
            with Session() as s:
                repo.close_collection(s, cycle["id"])
            st.session_state["flash"] = "Collection closed. Run the market in the next tab."
            st.rerun()
    elif cycle["state"] in ("closed", "solved"):
        st.write("Collection is closed. Reopening lets everyone edit their entries again; run the market "
                 "again afterwards.")
        if st.button("Reopen collection"):
            with Session() as s:
                repo.reopen_collection(s, cycle["id"])
            st.session_state["flash"] = "Collection reopened."
            st.rerun()
    elif cycle["state"] == "published":
        st.write("The schedule is published. Unpublish it (Publish tab) before reopening collection.")


def run_tab(Session, cycle, run):
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
    responders, excluded = sa.split_responders(participants, submissions)
    st.markdown("**Responders (%d):** %s" % (len(responders), ", ".join(p["name"] for p in responders) or "nobody yet"))
    if excluded:
        st.warning("**Excluded, no submission (%d):** %s. They are left out of this cycle and the packages are "
                   "derived for the responders only." % (len(excluded), ", ".join(excluded)))
    if cycle["state"] in ("draft", "collecting"):
        st.info("Close collection (Status tab) before running the market.")
        return
    if cycle["state"] == "published":
        st.info("The schedule is published. Unpublish it (Publish tab) to run the market again.")
        if run:
            show_candidates(Session, cycle, run, participants, locked=True)
        return

    dates = sa.cycle_dates(cycle)
    packages = sa.get_packages(cycle["structure"], responders, dates, int(cycle["max_per_day"]), cycle["packages_json"])
    st.subheader("Packages")
    if packages["packages"]:
        st.caption(packages["message"])
        st.table(sa.package_descriptions(cycle["structure"], packages["packages"]))
    else:
        st.error(packages["message"])
    with st.expander("Packages JSON override", expanded=packages["packages"] is None):
        st.caption("Only needed when automatic packages are not available. `count` is how many people get the "
                   "package; `cats` gives [min, max] shifts per category; `total` gives [min, max] shifts overall. "
                   "Categories: four and eve (three shifts), M and L (two shifts), B1 to B5 (five blocks).")
        text = st.text_area("Packages JSON", cycle["packages_json"] or "", height=160, key="pkg_json")
        c1, c2 = st.columns(2)
        if c1.button("Save packages JSON", disabled=not text.strip()):
            check = sa.get_packages(cycle["structure"], responders, dates, int(cycle["max_per_day"]), text)
            if check["packages"] is None:
                st.error(check["message"])
            else:
                with Session() as s:
                    repo.update_cycle_notes(s, cycle["id"], packages_json=text)
                st.session_state["flash"] = "Packages saved."
                st.rerun()
        if c2.button("Clear override", disabled=not cycle["packages_json"]):
            with Session() as s:
                repo.update_cycle_notes(s, cycle["id"], packages_json=None)
            st.rerun()

    st.subheader("Run")
    st.caption("One solve finds the clearing prices, the schedule and every schedule that is equally good. "
               "It takes about 10 to 20 seconds on a laptop and up to a minute or two on the free host; "
               "stay on this page until it finishes.")
    if st.button("Run the market", type="primary", disabled=packages["packages"] is None or not responders):
        with st.spinner("Finding clearing prices, assigning shifts and looking for equivalent schedules ..."):
            try:
                outcome = sa.run_market(cycle, participants, submissions, packages["packages"])
            except om.SchedulingError as e:
                st.error(str(e))
                return
        params = {"structure": cycle["structure"], "start_date": cycle["start_date"].isoformat(),
                  "n_days": cycle["n_days"], "skip_dates": cycle["skip_dates"], "max_per_day": cycle["max_per_day"],
                  "mode": cycle["mode"], "min_ranks": cycle["min_ranks"], "people": outcome["responders"],
                  "budgets": outcome["budgets"]}
        with Session() as s:
            repo.create_run(s, cycle["id"], seeds=outcome["candidates"][0]["seeds"], params=params,
                            candidates=outcome["candidates"], excluded_names=outcome["excluded"],
                            packages_used={"source": packages["source"], "packages": packages["packages"]})
        n = len(outcome["candidates"])
        st.session_state["flash"] = ("Run finished: one schedule, no equivalent alternative. Choose it below."
                                     if n == 1 else "Run finished: %d equivalent schedules. Choose one below." % n)
        st.rerun()
    if run:
        show_candidates(Session, cycle, run, participants, locked=False)


def show_candidates(Session, cycle, run, participants, locked):
    cands = run["candidates"]
    n = len(cands)
    st.subheader("Run at %s: %s" % (fmt_dt(run["created_at"]),
                                    "one schedule" if n == 1 else "%d equivalent schedules" % n))
    newest = max((p["saved_at"] for p in participants if p["saved_at"]), default=None)
    if newest and newest > run["created_at"]:
        st.warning("Preferences were saved after this run. Run the market again before choosing.")
    if run["excluded_names"]:
        st.caption("Excluded from this run: " + ", ".join(run["excluded_names"]))
    if n == 1:
        st.caption("The market found no other schedule with the same total of preference points (within %.2f), "
                   "so its choice is the only outcome." % sa.EQUIVALENT_POINTS)
    else:
        rule = (sa.tie_rule_from_report(cands[0].get("report"))
                or "ties broken in favour of the person who was otherwise worst off")
        st.caption("The market found %d schedules that satisfy the same total of preference points (within %.2f): "
                   "only who gets which equivalent bundle differs, typically who takes the heavier package. "
                   "Its own choice is listed first, picked by %s. The choice between them is yours."
                   % (n, sa.EQUIVALENT_POINTS, rule))
    columns = st.columns(len(cands)) if len(cands) <= 3 else [st.container(border=True) for _ in cands]
    for k, cand in enumerate(cands):
        chosen = run["chosen_index"] == k
        with columns[k]:
            flags = []
            if k > 0 and cand["equivalent_to_best"]:
                flags.append("equivalent to outcome 1")
            if chosen:
                flags.append("CHOSEN")
            st.markdown("**%s**%s" % (cand["label"], (" - " + ", ".join(flags)) if flags else ""))
            st.caption("Total preference points %.1f. Maximum regret %.2f." % (cand["total_points"], cand["max_regret"]))
            if cand["violations"]:
                st.warning("%d constraint violation(s): see the Violations column." % cand["violations"])
            show_grid(cand["schedule"], cand["structure"])
            st.dataframe(person_table(cand["person_rows"]), hide_index=True)
            # the status line carries the solver's warnings (too few ranks, a rule violation,
            # indicative prices) after " WARNING: "; each is shown as its own warning box
            status, *notes = cand["status"].split(" WARNING: ")
            st.caption(status)
            for note in notes:
                st.warning(note)
            if st.button("Chosen" if chosen else "Choose this outcome", key="choose_%d_%d" % (run["id"], k),
                         type="secondary" if chosen else "primary", disabled=chosen or locked):
                with Session() as s:
                    repo.choose_candidate(s, run["id"], k)
                st.session_state["flash"] = "%s chosen. Review it in the next tab, then publish." % cand["label"]
                st.rerun()
            with st.expander("Prices and full report"):
                if cand["package_prices"]:
                    st.write("Package prices: " + ", ".join("%s %+.1f" % kv for kv in cand["package_prices"].items()))
                st.code(cand["report"], language=None)


def load_chosen(Session, cycle, run):
    """Everything the review and publish tabs need for the chosen outcome, or None."""
    if not run or run["chosen_index"] is None:
        return None
    cand = run["candidates"][run["chosen_index"]]
    with Session() as s:
        participants = repo.list_participants(s, cycle["id"])
        submissions = repo.submissions_by_name(s, cycle["id"])
        edits = repo.list_edits(s, run["id"])
        owner = repo.current_owner(s, run["id"])
    by_name = {p["name"]: p for p in participants}
    responders = [by_name.get(n, {"name": n, "budget": om.BUDGET}) for n in cand["people"]]
    packages = (run.get("packages_used") or {}).get("packages")
    try:
        ctx = sa.build_context(cycle, responders, submissions, packages, seed=cand["seeds"][0])
        rows, solution = sa.person_rows(ctx, cand, owner)
    except om.SchedulingError as e:
        st.error("The chosen outcome no longer matches the saved preferences: %s. Run the market again." % e)
        return None
    return {"cand": cand, "ctx": ctx, "owner": owner, "rows": rows, "solution": solution, "edits": edits,
            "submissions": submissions}


def review_tab(Session, cycle, run, chosen):
    if chosen is None:
        st.info("Run the market and choose an outcome first.")
        return
    cand, ctx, owner, rows = chosen["cand"], chosen["ctx"], chosen["owner"], chosen["rows"]
    locked = cycle["state"] == "published"
    st.markdown("**%s**, with %d change%s by hand." % (cand["label"], len(chosen["edits"]), "" if len(chosen["edits"]) == 1 else "s"))
    show_grid(chosen["solution"]["schedule"], cycle["structure"])
    problems = [r for r in rows if r["violations"] or not r["package_ok"]]
    if problems:
        st.warning("Check: " + "; ".join(
            "%s %s" % (r["person"], " and ".join(
                (["breaks a rule (%d)" % r["violations"]] if r["violations"] else []) +
                ([] if r["package_ok"] else ["no longer matches the %s package" % r["package"]])))
            for r in problems))
    st.dataframe(person_table(rows, extra=[("package_ok", "Package fits")]), hide_index=True)
    st.caption("Points, % of ideal and net price are recomputed at the run's prices after every change.")

    if locked:
        st.info("The schedule is published. Unpublish it (Publish tab) to make further changes.")
    else:
        st.subheader("Change the schedule by hand")
        st.caption("Every change is logged and shown with the published schedule.")
        m = len(ctx.slots)

        def slot_text(j):
            return "%s - %s" % (sa.slot_label(ctx.slots[j]), ctx.people[owner[j]])

        c1, c2, c3 = st.columns([4, 4, 2])
        a = c1.selectbox("Swap this slot", range(m), format_func=slot_text, key="swap_a")
        b = c2.selectbox("with this slot", range(m), format_func=slot_text, key="swap_b", index=min(1, m - 1))
        c3.markdown("<br>", unsafe_allow_html=True)
        if c3.button("Swap"):
            apply_edit(Session, run, owner, lambda: sa.swap_slots(ctx, owner, a, b))
        c1, c2, c3 = st.columns([4, 4, 2])
        j = c1.selectbox("Assign this slot", range(m), format_func=slot_text, key="assign_slot")
        who = c2.selectbox("to", range(len(ctx.people)), format_func=lambda i: ctx.people[i], key="assign_who")
        c3.markdown("<br>", unsafe_allow_html=True)
        if c3.button("Assign"):
            apply_edit(Session, run, owner, lambda: sa.assign_slot(ctx, owner, j, who))

    if chosen["edits"]:
        st.subheader("Changes so far")
        st.table([{"When": fmt_dt(e["created_at"]), "Change": e["description"]} for e in chosen["edits"]])
        if not locked and st.button("Undo the last change"):
            last = chosen["edits"][-1]
            with Session() as s:
                repo.add_edit(s, run["id"], "Undid: " + last["description"], last["owner_after"], last["owner_before"])
            st.rerun()


def apply_edit(Session, run, owner, make):
    try:
        new_owner, description = make()
        with Session() as s:
            repo.add_edit(s, run["id"], description, owner, new_owner)
    except (om.SchedulingError, RepoError) as e:
        st.error(str(e))
        return
    st.session_state["flash"] = description
    st.rerun()


def publish_tab(Session, cycle, run, chosen):
    if chosen is None:
        st.info("Run the market and choose an outcome first.")
        return
    cand, solution, edits = chosen["cand"], chosen["solution"], chosen["edits"]
    with Session() as s:
        publication = repo.get_publication(s, cycle["id"])
    public_link = make_link(link_base(cycle), "s", cycle["public_token"])
    if cycle["state"] == "published" and publication:
        st.success("Published %s. Participants' links and the public link now show the schedule." % fmt_dt(publication["published_at"]))
        st.code(public_link, language=None)
        if st.button("Unpublish"):
            with Session() as s:
                repo.unpublish(s, cycle["id"])
            st.session_state["flash"] = "Unpublished. The links show 'not published yet' again."
            st.rerun()
    else:
        st.write("Publishing shows the schedule below to every participant (through their own link) and at the "
                 "public link. Prices, points and summaries stay here.")
        show_grid(solution["schedule"], cycle["structure"])
        if st.button("Publish the schedule", type="primary"):
            log = [{"when": fmt_dt(e["created_at"]), "description": e["description"]} for e in edits]
            with Session() as s:
                repo.publish(s, cycle["id"], run["id"], solution["schedule"], solution["summary"], log)
            st.session_state["flash"] = "Published."
            st.rerun()
        st.write("Public link (works after publishing):")
        st.code(public_link, language=None)

    st.subheader("Downloads")
    st.caption("Keep these as the record of the cycle; the free database is not backed up for you.")
    stem = "".join(ch if ch.isalnum() else "_" for ch in cycle["name"]).strip("_") or "cycle"
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("schedule.csv", sa.schedule_csv_text(solution), "%s_schedule.csv" % stem, "text/csv")
    c2.download_button("summary.csv", sa.summary_csv_text(solution), "%s_summary.csv" % stem, "text/csv")
    c3.download_button("preferences.csv",
                       sa.preferences_csv_text(cycle, cand["people"], chosen["submissions"], run["excluded_names"]),
                       "%s_preferences.csv" % stem, "text/csv")
    c4.download_button("edits.csv", sa.edits_csv_text(edits), "%s_edits.csv" % stem, "text/csv")


def danger_tab(Session, cycle):
    st.warning("Deleting removes this cycle with its names, links, preferences, runs and publication. "
               "There is no undo. Download the CSVs first.")
    typed = st.text_input("Type the cycle name to confirm", key="delete_name")
    sure = st.checkbox("I understand this cannot be undone", key="delete_sure")
    if st.button("Delete this cycle", type="primary", disabled=not (sure and typed.strip() == cycle["name"])):
        with Session() as s:
            repo.delete_cycle(s, cycle["id"])
        st.session_state["flash"] = "Cycle '%s' deleted." % cycle["name"]
        st.query_params.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# participant form
# ---------------------------------------------------------------------------

def participant_view(Session, token):
    with Session() as s:
        found = repo.get_participant_by_token(s, token)
    if found is None:
        st.error("This link is not valid. Ask the organiser for a new one.")
        return
    participant, cycle = found
    st.title(cycle["name"])
    st.write("Hello **%s**." % participant["name"])
    state = cycle["state"]
    if cycle["deadline_note"] and state in ("draft", "collecting"):
        st.info(cycle["deadline_note"])
    if state == "draft":
        st.info("The organiser has not opened collection yet. Come back later.")
        return
    if state == "published":
        with Session() as s:
            publication = repo.get_publication(s, cycle["id"])
        if publication:
            st.subheader("The schedule")
            show_published(cycle, publication, participant["name"])
            return
    with Session() as s:
        saved = repo.get_submission(s, participant["id"])
    slots = sa.slot_table(cycle)
    plain = wants_plain_form()
    if state == "collecting":
        if plain:
            participant_form_plain(Session, participant, cycle, slots, saved)
        else:
            participant_form(Session, participant, cycle, slots, saved)
    else:
        st.info("Collection has closed. The schedule will appear here once the organiser publishes it.")
        if saved:
            st.caption("Your saved entries (%s):" % fmt_dt(saved["saved_at"]))
            show_saved(slots, saved["cells"], participant, cycle, plain)
        else:
            st.warning("You did not save any preferences, so you are not part of this cycle.")


def wants_plain_form():
    """?plain=1 asks for the plain form (one text box per slot) instead of the sheet."""
    return str(st.query_params.get("plain", "")).strip().lower() in ("1", "true", "yes")


def participant_link(participant, plain=False):
    """The participant's own link, relative to this page: ?p=<token>[&plain=1]."""
    return "?p=%s%s" % (participant["token"], "&plain=1" if plain else "")


def show_saved(slots, cells, participant, cycle, plain=False):
    """The saved column read-only: the sheet with the saved colours (a plain table with
    ?plain=1), then what each rank is worth."""
    if plain:
        rows = [{"Day": s["short"], "Shift": "%s (%s)" % (s["label"], s["code"]), "Entry": cells.get(s["id"], "")}
                for s in slots]
        st.table(rows)
    else:
        sheet_component.render_sheet(slots, cells, participant["name"], cycle["min_ranks"], cycle["mode"],
                                     readonly=True, key="sheet_readonly")
    show_point_preview(cells, participant, cycle)


def show_point_preview(cells, participant, cycle):
    if not cells:
        return
    preview = prefs.point_values(cells, cycle["mode"], participant["budget"])
    if preview:
        st.caption("Worth of each saved rank (your %g points spread over the slots you ranked):" % participant["budget"])
        st.table([{"Rank": r, "Points per slot": round(p, 1), "Slots": n} for r, p, n in preview])


def participant_intro(saved, cycle, sheet):
    """The status line and the instructions shared by the sheet and the plain form."""
    flash()
    if saved:
        st.success("Saved %s. You can change your entries until collection closes." % fmt_dt(saved["saved_at"]))
    else:
        st.warning("Nothing saved yet. If you have not saved by the deadline you are left out of this cycle.")
    st.markdown(
        "Write **1** next to the slot you want most, **2** next to the next, and so on. Rank at least **%d** slots; "
        "ties and gaps between numbers are fine, only the order matters. Write **X** where you cannot work. "
        "Leave a slot blank if you are available but have no preference.%s"
        % (cycle["min_ranks"], " Tab or Enter moves down the column." if sheet else ""))


def save_cells(Session, participant, cycle, slots, raw):
    """Validate the typed column ({slot_id: text}) and save it, with the same messages
    whichever form it came from.  Reruns the page after a successful save."""
    clean, errors = prefs.validate_cells(raw, cycle["min_ranks"], cycle["mode"])
    if errors:
        by_id = {s["id"]: s for s in slots}
        for slot_id, message in errors:
            prefix = ("%s %s: " % (by_id[slot_id]["short"], by_id[slot_id]["label"])) if slot_id else ""
            st.error(prefix + message)
        st.info("Nothing was saved; fix the entries above and save again.")
        return
    try:
        with Session() as s:
            repo.upsert_submission(s, participant["id"], clean)
    except RepoError as e:
        st.error(str(e))
    else:
        st.session_state["flash"] = "Saved. Thank you!"
        st.rerun()


def participant_form(Session, participant, cycle, slots, saved):
    """The sheet (app/components/sheet) with the Save button under it.  The button stays
    disabled until the sheet has reported its cells, so a click always saves what is shown."""
    cells = saved["cells"] if saved else {}
    participant_intro(saved, cycle, sheet=True)
    value = sheet_component.render_sheet(slots, cells, participant["name"], cycle["min_ranks"], cycle["mode"],
                                         key="sheet")
    ready = value is not None
    if not ready:
        st.caption("Loading the sheet ... If it does not appear, use the [plain form](%s)."
                   % participant_link(participant, plain=True))
    if st.button("Save my preferences", type="primary", disabled=not ready, key="save_sheet"):
        raw = {slot["id"]: value.get(slot["id"], "") for slot in slots}
        save_cells(Session, participant, cycle, slots, raw)
    show_point_preview(cells, participant, cycle)
    if ready:
        st.caption("Trouble with the sheet? Use the [plain form](%s)." % participant_link(participant, plain=True))


def participant_form_plain(Session, participant, cycle, slots, saved):
    """The fallback form: one text box per slot (?plain=1).  Same validation and saving."""
    cells = saved["cells"] if saved else {}
    participant_intro(saved, cycle, sheet=False)
    with st.form("prefs", border=False):
        for slot in slots:
            if slot["first_of_day"]:
                st.markdown("##### %s" % long_date(slot["date"]))
            st.text_input("%s (%s, %g h)" % (slot["label"], slot["time"], slot["hours"]),
                          value=cells.get(slot["id"], ""), key="cell_" + slot["id"], max_chars=5, placeholder="rank or X")
        submitted = st.form_submit_button("Save my preferences", type="primary")
    if submitted:
        raw = {slot["id"]: st.session_state.get("cell_" + slot["id"], "") for slot in slots}
        save_cells(Session, participant, cycle, slots, raw)
    show_point_preview(cells, participant, cycle)
    st.caption("Prefer the sheet? [Open it](%s)." % participant_link(participant))


# ---------------------------------------------------------------------------
# public schedule
# ---------------------------------------------------------------------------

def public_view(Session, token):
    with Session() as s:
        cycle = repo.get_cycle_by_public_token(s, token)
        publication = repo.get_publication(s, cycle["id"]) if cycle else None
    if cycle is None:
        st.error("This link is not valid.")
        return
    st.title(cycle["name"])
    if cycle["state"] != "published" or publication is None:
        st.info("The schedule has not been published yet.")
        return
    show_published(cycle, publication)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main():
    try:
        Session = sessions()
    except Exception as exc:  # the database is unreachable or refuses the credentials
        url = db.resolve_database_url(_secret_database_url())
        st.error("Could not connect to the database. " + db.describe_connection_error(exc, url))
        st.caption("Check the DATABASE_URL secret (the app's Settings > Secrets): the host, user and "
                   "password must match Neon's Connect panel and the whole string must be inside double "
                   "quotes. The full error is in the app logs under Manage app.")
        st.stop()
    params = st.query_params
    if params.get("a"):
        admin_view(Session, params["a"])
    elif params.get("p"):
        participant_view(Session, params["p"])
    elif params.get("s"):
        public_view(Session, params["s"])
    else:
        landing_view(Session)


main()
