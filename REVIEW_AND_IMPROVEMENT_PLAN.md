# On-call market scheduler: review and improvement plan

Date: 2026-10-19.
Reviewed: `README.md`, `oncall_market.py` (1,115 lines), `oncall_gui.py` (705 lines),
`preferences_example.csv`, both screenshots. Ran the CLI and the GUI on Python 3.14.7.

## 1. What the tool is for

The documented objective: turn a fixed group's ranked slot preferences into a fair on-call
schedule using a market. Every person gets the same 100 points, spread over the slots they
ranked. Every slot, and every workload package, gets a clearing price. The intended outcome is
one where nobody could do better by switching bundles at those prices (an envy-free allocation
from equal budgets). Fairness in hours is guaranteed separately by fixed packages derived for
7 people over 9 weekdays: three people take 14 h, four take 12 h, evenings are balanced, one
shift per person per day, and no 8 pm finish before an 8 am start.

Scope today: an organiser builds a CSV sheet (or opens a local browser page), gathers each
person's column by hand, runs the solver, and gets `schedule.csv`, `summary.csv`, and a report
with prices, satisfaction (% of ideal), and net price (who paid, who was compensated).

## 2. How it was evaluated

- Read all code and documentation.
- CLI: solved the shipped example; generated templates for 6 people and for 10 days; generated
  and solved demo files for the two-shift and five-block structures.
- GUI: started the local server, imported the example through its API, typed ranks and X's
  into the sheet, solved, and read the results page.
- A probe script (kept in the session scratchpad, not in this folder) ran: six seeds on the
  same input; the other two structures; a blank column; a single-slot ranker; everyone ranking
  the same slot first; a 130-point budget for one person; fixed roles; a 14-person / 18-weekday
  problem with doubled packages.

## 3. Findings

### 3.1 What works well

- Solves the intended case correctly and quickly. Hard constraints (availability, one shift per
  day, late-then-early, package counts) held in every run; zero violations were observed.
- The mechanism is coherent and the report is honest: equal budgets, prices that explain who
  was compensated, a measured "regret" when exact clearing fails, and a carry-over suggestion
  for the next cycle. Budget carry-over works as designed.
- Input validation and error messages are good (short-handed days, too many X's, wrong package
  counts).
- Zero dependencies, CSV in and out. The GUI mirrors the CLI and merges per-person CSVs by
  column, which is a workable manual collection flow.
- It scales further than the README suggests once packages are supplied.

Measured on this machine:

| Scenario | Cleared exactly | Final regret (points) | Time |
|---|---|---|---|
| Example, three shifts, 7 x 9 | no | 0.03 | 7.1 s |
| Demo, two shifts, 7 x 9 | no | 0.01 | 4.7 s |
| Demo, five blocks, 7 x 9 | no | 5.66 | 22.7 s |
| Fixed roles, three shifts | no | 0.00 | 1.3 s |
| 14 people x 18 weekdays, doubled packages | no | 0.09 | 12.1 s |

### 3.2 Where it falls short of the objective

Ordered by impact.

**F1. The schedule comes from the fallback search, not the market, and ties are broken by a
hidden random seed.**
Prices never cleared exactly in any run. The assignment is then produced by simulated
annealing that maximises total points, and prices are fitted afterwards to justify it. On the
example, seeds 1 and 2 give every person identical satisfaction, yet seed 1 puts Person D on
the 14-hour package (with an unranked Friday evening) and seed 2 puts Person F on it instead.
Person D's net price barely moves (+21.2 against +21.0). Two extra hours of work are allocated
by the seed and are effectively unpriced. That is hard to defend to a group that will see the
numbers. Fix: detect welfare-equivalent alternatives, break ties by an explicit rule (history,
rotation, fewest hours last cycle, or organiser choice), and show the alternatives.

**F2. Only one group size and period work out of the box.**
Packages are hard-coded for 7 people over 9 weekdays. Six people, or ten days, fails with an
error that tells the user to write a packages JSON by hand. The GUI has no way to supply one,
so it cannot handle a holiday-shortened cycle, a new member, or someone on leave. The
`packages_example.json` the README refers to is not in the folder. Fix: derive packages
automatically from people, days, and structure using the same fairness principles.

**F3. Ranking breadth is rewarded and punished in ways people will learn to exploit.**
Ranks become linear points scaled to 100, so ranking one slot puts all 100 points on it. In
the probe, a person who ranked a single slot got it, scored 100% of ideal, and was paid 8.5
points for the rest of their bundle. A person who ranked all 23 of their available slots scored
29 points. The README's "strategy note" acknowledges this. Over repeated cycles the dominant
strategy is to rank only your must-haves. Options: an explicit point-spending interface, a
steeper or capped rank curve, or a required minimum number of ranks. This is a design decision
for you (question Q7).

**F4. Non-responders silently get the worst deal.**
A blank column (no ranks, no X) produced a person with 0 points, placed on the heavy package,
with a reported "% of ideal" of 79.9 that is pure tie-break noise. In the GUI test, two blank
people showed 67.9% and 66.7%. Fix: treat non-submission as an explicit state, show N/A, and
let the organiser choose the policy (assign as flexible, exclude, or block the solve).

**F5. The five-block structure is not supported by the market in practice.**
Final regret was 5.7 points against the README's 0.1 threshold for "as good as exact", and the
solve took 23 s. The prices shown there do not support the assignment. Either improve the
solver for that case or label that structure "assignment only, prices indicative".

**F6. There is no way to collect preferences from people.**
Collection means emailing CSVs around or the organiser typing every column. The GUI is
single-user and local-only, keeps the last result in a global variable, and has no persistence,
accounts, deadlines, or submission tracking. This is the feature request.

**F7. The GUI lags the CLI.**
No budgets (carry-over), no packages override, a fixed seed, no iteration controls. Downloads
depend on whatever was solved last in memory.

**F8. No automated tests.**
The dynamic programme in `best_response`, the constraint logic, and both price loops are
untested. Any refactor for a hosted version needs a safety net first.

**F9. The carry-over magnitude is unexamined.**
Giving one person 130 points moved three other people by 14 to 20 percentage points of ideal.
The suggested next budget of 100 minus net price may overcorrect. A damped carry (for example
half) and a cap should be considered and documented.

**F10. Smaller items.**
No per-person explanation of "why did I get this"; no calendar (ICS) export; weekends and
holidays cannot be covered; partial-day availability cannot be expressed; "% of ideal" is not
comparable between people who ranked different numbers of slots.

## 4. Improvement plan

Phased so the hosted version is built on a solver that is tested and general.

### Phase 0: make the core trustworthy (prerequisite)

- 0.1 Test suite (pytest): CSV parsing and round trip; rank-to-points; `best_response` checked
  against brute force on tiny problems; constraint checks; feasibility errors; end-to-end on the
  example at a fixed seed; package validation. Target: runs in under a minute.
- 0.2 Library entry point: one `solve_sheet(...)` function returning a plain, JSON-serialisable
  result, so the CLI, the GUI, and the web app all call the same code. No behaviour change.
- 0.3 Automatic packages: from people, days, and structure, produce the fair package set (hours
  at the mathematical floor, evenings balanced, one shift per day). Must reproduce the current
  3/3/1 split for 7 x 9 as a test. Restore `packages_example.json`.
- 0.4 Tie handling (F1): enumerate welfare-equivalent package assignments and schedules, report
  them, and apply a documented tie-break rule with an organiser override.
- 0.5 Metrics and edge cases (F3, F4, F9): a "responded" flag and N/A metrics for
  non-responders; the chosen ranking rule; damped and capped carry-over.
- 0.6 Five-block structure (F5): more rounds with adaptive steps, or label as indicative.

### Phase 1: hosted web application (MVP)

Decided on 2026-10-19 (see section 7): Streamlit Community Cloud (free) with a free Neon
Postgres database, deployed from a public GitHub repository. This replaces the FastAPI proposal
in the first draft.

- One Streamlit app (`streamlit_app.py` plus an `app/` package). The link's query parameter
  selects the view: participant form, organiser dashboard, or published schedule.
- Storage through SQLAlchemy: Neon Postgres in production (`DATABASE_URL` from Streamlit
  secrets), a SQLite file for local development and tests. Tables: cycles, participants (name,
  token, budget, responded), submissions (cells, saved at), runs (parameters, seed, result,
  chosen), edits (what changed, when), publications.
- Solver called in-process with a spinner (7 to 25 s). Several seeds per run so that
  welfare-equivalent alternatives are shown to the organiser. Packages derived automatically for
  the responders (T2), with the built-in packages as the fallback for 7 people over 9 weekdays.
- Deployment: `requirements.txt`, `.streamlit/config.toml`, secrets set in the Streamlit
  dashboard, code compatible with Python 3.11 to 3.13 (Streamlit Cloud may not offer 3.14).
  The app sleeps after idle time on the free tier; a visitor may have to click to wake it.
- No emails stored anywhere. Tokens are unguessable; the organiser's admin token is separate
  from participant tokens; the public repository contains code only.

Organiser flow: create cycle -> add names -> copy links and send them by hand -> watch
submissions -> close collection at the deadline -> run the market (non-responders excluded) ->
review (ties, warnings, manual swaps with a log) -> publish -> download CSVs.

Participant flow: open personal link -> see own column only -> rank at least the minimum
number of slots, mark X where unavailable, save (editable until collection closes) -> after
publishing, see the schedule only.

### Phase 2: fairness over time and organiser tooling

History across cycles, automatic budgets and heavy-package rotation, a fairness dashboard
(hours, evenings, net price over time), manual swaps before publishing, swap requests after
publishing priced at the clearing prices, email reminders.

### Phase 3: generalisation

Custom shift structures defined in the UI, weekend and holiday coverage, partial availability,
multiple skills or roles.

## 5. Proposed delegation

Each task becomes its own session with a written brief and a fixed file scope, so tasks run in
parallel without touching the same files.

| # | Task | Files | Depends on |
|---|---|---|---|
| T1 | Test suite and library entry point (0.1, 0.2) | `tests/`, `oncall_market.py` (entry point only) | none |
| T2 | Automatic packages and `packages_example.json` (0.3) | new `packages.py`, README | T1 |
| T3 | Tie handling, metrics, edge cases, five-block (0.4 to 0.6) | `oncall_market.py` | T1 |
| T4 | Streamlit app: storage, participant form, organiser dashboard, publish, CSV export, deploy files and guide (Phase 1) | new `streamlit_app.py`, `app/`, `tests/test_app*.py`, `requirements.txt`, `.streamlit/config.toml`, `docs/deploy.md` | none; adapter seam for T1 and T2 |
| T5 | Deployment steps that need your accounts: GitHub push, Neon database, Streamlit Cloud app and secrets | manual, following `docs/deploy.md` | T4 |
| T6 | Documentation: README, organiser guide, participant guide | `README.md`, `docs/` | all |

Recommendation: put this folder under git first, so each session works in an isolated worktree
and every change arrives as a reviewable diff.

## 6. Open questions

All of these were answered on 2026-10-19; the answers are in section 7, which takes precedence
over any default proposed below.

Answers to Q1 to Q4 change the architecture; the rest change details. Where a default is
proposed, nothing proceeds on it until you confirm or correct it.

**Q1. Group and period.** Is the real rotation always 7 people over 9 weekdays with the
three-shift structure (8-12, 1-5:30, 6-8), or do headcount, length, holidays, and structure
vary from cycle to cycle? Which of the three structures is actually used? What is the largest
group this should ever handle?

**Q2. Hosting and data.** Where should the web version run: a cloud host you pay for (Render,
Fly.io, Azure, AWS), a server your institution provides, or a machine you control? Are there
rules on where names, emails, and availability data may be stored? Is a domain with HTTPS
available?

**Q3. Sign-in.** How should participants and the organiser identify themselves?
(a) per-cycle personal links emailed by the organiser, no accounts;
(b) institutional single sign-on;
(c) organiser password plus participant links.
Proposed default: (c).

**Q4. Stack.** Keep everything in Python (FastAPI, SQLite, light JavaScript), or is a specific
stack required by whoever will maintain or host it? Does "standard library only" still matter
once it is hosted?

**Q5. Context and roles.** Who is the group and what is being covered? One organiser or
several? Should the organiser be able to edit the solved schedule by hand before publishing?
Proposed default: yes, with each change logged.

**Q6. Non-responders.** If someone has not submitted by the deadline: assign them as fully
available with no preferences (today's behaviour), exclude them from the cycle, or block the
solve until everyone has submitted? Proposed default: the organiser chooses per cycle, with
"assign as flexible" pre-selected.

**Q7. Ranking rules.** Keep "rank as many or as few as you like" with the known incentive to
rank few; require a minimum number of ranks; or switch to spending 100 points explicitly?
Proposed default: keep ranks, require at least the largest package size plus two, and show each
person the point values their ranks produce.

**Q8. Fairness over time.** Should the hosted version carry net prices into next cycle's
budgets automatically, and rotate the heavy package? Proposed default: carry half of the net
price, capped at plus or minus 25 points, visible to everyone; break ties by last cycle's hours.

**Q9. Visibility.** Should participants see only the final schedule and their own summary, or
also everyone's prices, packages, and satisfaction? Proposed default: everyone sees the
schedule and the clearing prices; personal summaries are private; the organiser sees all.

**Q10. Package derivation.** Was the 3/3/1 split derived by hand? Are the stated principles
(hours at the mathematical floor, evenings balanced, at most one shift a day, no late-then-early)
the complete list, or are there other rules (maximum consecutive days, minimum rest, who may
take evenings)?

**Q11. Notifications and calendar.** Should the app email invitations and reminders, or will
the organiser send links by hand? Is an ICS calendar export wanted? Proposed default: email via
an SMTP account you provide; ICS yes.

**Q12. Coverage beyond weekdays.** Will weekend or holiday coverage, or partial-day
availability (for example "available after 10 am"), ever be needed? Proposed default: not in
the MVP.

**Q13. Timeline.** The defaults in the code point to 2 to 12 November 2026. Is that the next
real cycle? That sets what must be in the MVP.

**Q14. Repository.** May I initialise a git repository in this folder so delegated sessions
work in isolated worktrees and every change is reviewable?

## 7. Decisions (answers received 2026-10-19)

| Question | Decision |
|---|---|
| Q1 group and period | Headcount and cycle length vary from cycle to cycle, so packages must be derived automatically. Largest group: 10 people. Which of the three structures is in use was not captured; the three-shift structure stays the default and all three remain supported. |
| Q2 hosting and data | No emails stored; the organiser sends personal links by hand. Host: Streamlit Community Cloud (free) with a free Neon Postgres database. Code in a public GitHub repository. |
| Q3 sign-in | Personal links only, no accounts. The organiser uses a separate secret admin link. |
| Q4 stack | Streamlit (Python) for the whole app; SQLAlchemy to Postgres in production and SQLite locally. The solver stays standard-library only. Replaces the FastAPI proposal. |
| Q5 roles | The organiser may edit the solved schedule by hand before publishing; every change is logged and shown with the schedule. Who the group is and whether there is more than one organiser was not asked; the admin link can simply be shared. |
| Q6 non-responders | Always excluded from the cycle; packages are re-derived for the responders only. |
| Q7 ranking rule | Free ranking with a minimum count equal to the largest package size plus two; the form shows the point value each rank produces. |
| Q8 fairness over time | Damped carry-over: next budget = 100 plus half of last cycle's compensation, capped at plus or minus 25; the heavy package rotates using history. After the MVP; the MVP stores what it needs (net price per person per cycle, an editable budget). |
| Q9 visibility | Participants see the published schedule only. Prices, summaries and budgets are organiser-only. |
| Q10 package rules | The four principles (hours at the floor, evenings balanced, at most one shift a day, no late-then-early) are the complete list. |
| Q11 notifications | The app sends no email. ICS export comes after the MVP (the MVP scope chosen under Q13 is links, form, solve and publish, CSV export). |
| Q12 coverage | Weekdays only, whole-slot availability. No weekend, holiday or partial-day coverage in the MVP. |
| Q13 timeline | 2 to 12 November 2026 is the next real cycle and the hosted MVP must be ready for it, so collection should open by about 27 October 2026. |
| Q14 repository | Git initialised on 2026-10-19 (branch `main`). T1 and T2 started the same day in their own worktrees. |

Still open: which shift structure the group actually uses; the GitHub repository name and the
go-ahead to push (pushing publishes the code).
