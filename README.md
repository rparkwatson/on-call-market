# On-call market scheduler

A single-file Python tool (`oncall_market.py`, standard library only, Python 3.8+) that turns
people's ranked slot preferences into a fair on-call schedule using a market: every slot gets a
clearing price, and the assignment is the one where nobody could do better by swapping at those
prices. The fairness system developed for the 9-weekday / 7-person rotation (packages, one shift
per day, no 8 pm finish before an 8 am start) is built in.

## Quick start

### Point-and-click

```bash
python oncall_gui.py
```

This opens a page in your browser (served by Python on your own machine; nothing is sent
anywhere). It walks through the same three steps as the command-line tool: set up the period,
fill in the sheet, solve. On the sheet, type a rank in a cell, or X if you can't do that slot;
Tab and Enter move down your own column, and "Click to rank" fills in the next number with each
click. Load CSV and Download CSV use the same `preferences.csv` format as the command line, so
people can fill in their own copy of the sheet and the organiser can load the files one after
another: a file whose period matches the sheet replaces just the columns it contains and keeps
everyone else's entries. The results page shows the schedule, each person's summary and the
clearing prices, with download links for `schedule.csv` and `summary.csv`. Stop the program with
Ctrl+C. (`--port 9000` picks a different port; `--no-browser` just prints the address.)

### Command line

```bash
# 1. Make the sign-up sheet (defaults: three-shift structure, Nov 2–12 2026, Person A–G)
python oncall_market.py template --out preferences.csv

# 2. People fill in their column (see "Filling in the sheet"), then:
python oncall_market.py solve preferences.csv
```

To see it work immediately:

```bash
python oncall_market.py demo --out preferences_example.csv
python oncall_market.py solve preferences_example.csv
```

`solve` prints the schedule, each person's summary, and the clearing prices, and writes
`schedule.csv` and `summary.csv`.

### Options worth knowing

| Flag | Meaning |
|---|---|
| `--structure three` | `single` (five 2-h blocks), `two` (8–12 / 1–8), `three` (8–12 / 1–5:30 / 6–8). Default `three`; `solve` infers it from the file. |
| `--start 2026-11-02 --days 9` | First day and number of weekdays (template/demo). Weekends are skipped automatically. |
| `--skip 2026-11-11` | Leave out specific dates (holidays). |
| `--people "Ana,Ben,Cy,..."` | Names for the columns (default Person A–G). |
| `--mode points` | Cells are points (higher = want more) instead of ranks. |
| `--roles market` / `--roles fixed --roles-map "Ana=heavy,Ben=light_evenings,..."` | Let the market decide who takes which package (default), or fix it. |
| `--max-per-day 2` | Allow someone two shifts on the same day (default 1). |
| `--budgets budgets.csv` | Give some people more than 100 points (see "Carrying fairness across cycles"). |
| `--packages packages.json` | Replace the built-in packages (see "Changing the system"). |
| `--seed 1` | Change the search seed. Ties between equally good schedules are broken by a fixed rule (see "How the market works"), so the schedule normally does not change with the seed. |

## Filling in the sheet

The sheet is a CSV with one row per slot and one column per person. Open it in Excel or
Google Sheets, fill in your column, and save it back as CSV.

- Write **1** next to the slot you want most, **2** next to the next, and so on. Only the order
  matters; gaps between numbers do not. You can rank as many or as few slots as you like.
- Leave a cell **blank** if you are available but have no preference.
- Write **X** if you are **unavailable**. You will never be scheduled in an X slot.

Strategy note: your rankings are converted to 100 points spread over the slots you ranked, so
ranking only a few slots concentrates your points on them. Ranking everything spreads your
points thinly but tells the market more about you. Both are fine; mark X only when you truly
cannot do a slot, because X's are hard constraints and too many of them can make the schedule
impossible (the program will tell you which day is short-handed).

## How the market works

1. **Ranks become points.** Each person's ranked slots get values in rank order (top slot
   highest), scaled so that everyone's values add up to 100 points. Equal budgets are what make
   the outcome fair: nobody's preferences count for more than anyone else's.
2. **Packages define the load.** Each person ends up with a package (for the three-shift
   structure: `heavy` = three 4-hour shifts + one evening, 14 h; `light_evenings` = two 4-hour
   shifts + two evenings, 12 h; `light_days` = three 4-hour shifts, 12 h). The package counts
   (3 / 3 / 1) are exactly the fair split derived earlier.
3. **Prices clear the market.** Every slot (and every package) starts at price 0. Each round,
   every person picks the bundle that maximises *their points minus prices*, subject to their
   package, their X's, one shift per day, and no late shift followed by an early one the next
   calendar day. Slots that more than one person wants get more expensive; slots nobody wants
   get cheaper, going negative if necessary. A negative price means the slot *pays* you points to
   take it. Prices stop moving when every slot has exactly one taker.
4. **Assignment.** If the prices clear exactly, everyone simply gets what they chose. When an
   exact clearing does not exist (it needn't, because bundles are all-or-nothing), the program
   finds the assignment that satisfies the most total preference points and then adjusts the
   prices so that this assignment is each person's own best choice. The report shows the
   remaining **regret** — how much better anyone could do by switching at the final prices.
   Values below about 0.1 points mean the prices are, for practical purposes, true clearing
   prices.
5. **Ties.** Several schedules often satisfy the same total of preference points (for example
   two people who are equally happy either way, one of whom must take the 14-hour package). The
   program collects every schedule it finds whose total is within 0.05 points of the best, lists
   them in the report ("Ties: 3 equivalent schedules ...") and in `solve_sheet`'s `alternatives`,
   and picks one by a fixed rule: the highest minimum "% of ideal" across people, then the
   smallest spread of net price, then the fewest late shifts for the person with the most hours,
   then the first in slot order. The rule does not depend on the random seed, so the same sheet
   gives the same schedule whatever `--seed` is used; the seed only steers the search.

What the numbers in the output mean:

- **pts** — how many of that person's 100 points landed on the slots they got.
- **% of ideal** — those points as a share of the best that person could have scored if nobody
  else existed (within their package). This is the fairest single satisfaction measure.
- **net price** — points paid for the bundle (positive: they got popular slots; negative: they
  were compensated for taking unpopular ones).
- **package prices** — the market's valuation of the packages themselves; a negative price on
  `heavy` means people had to be paid to take the 14-hour package.

## The fairness system that is built in

- Hours are at the mathematical floor for 7 people over 9 days: 12 h for four people, 14 h for
  three (2-hour blocks can never split 90 hours seven ways evenly).
- Packages balance count against evenings: the 14-hour package has one evening, the 12-hour
  packages have two evenings or none.
- One shift per person per day (`--max-per-day` relaxes this).
- No 6–8 pm (or 1–8 pm, or 6–8 pm block) followed by an 8 am start on the next calendar day.
  Friday evening before Monday morning is fine.
- Who takes which package is decided by the market (and priced) unless you fix it with
  `--roles fixed`, e.g. to rotate the heavy package by hand.

Built-in packages per structure:

| Structure | Package | Count | Contents | Hours |
|---|---|---|---|---|
| three | heavy | 3 | 3 × (8–12 or 1–5:30) + 1 × 6–8 | 14 |
| three | light_evenings | 3 | 2 × (8–12 or 1–5:30) + 2 × 6–8 | 12 |
| three | light_days | 1 | 3 × (8–12 or 1–5:30) | 12 |
| two | heavy | 3 | 2 × 8–12 + 1 × 1–8 | 14 |
| two | light_long | 3 | 2 × 1–8 | 12 |
| two | light_mornings | 1 | 3 × 8–12 | 12 |
| single | heavy7 | 3 | one of each block + two extra midday blocks | 14 |
| single | light6 | 4 | one of each block + one extra 8 am or 6 pm block | 12 |

## Carrying fairness across cycles

If this rotation repeats, the simplest fix for a one-off imbalance is to run the next cycle with
`--roles fixed` and give the heavy package to different people. The market offers a finer tool:
`summary.csv` has a `next_budget_suggestion` column equal to `100 − net price`. Someone who was
compensated this cycle (negative net price) gets a larger budget next cycle, so their preferences
carry more weight; someone who got the popular slots gets a smaller one. Save those numbers to a
file with columns `person,budget` and pass it with `--budgets`.

## Changing the system

**Different dates or headcount.** `template`/`demo` take `--start`, `--days`, `--skip` and
`--people`. The built-in packages assume 9 days and 7 people; with other numbers the program
will tell you the packages no longer add up, and you provide your own:

```json
{
  "heavy":          {"count": 3, "cats": {"four": [3, 3], "eve": [1, 1]}, "total": [4, 4]},
  "light_evenings": {"count": 3, "cats": {"four": [2, 2], "eve": [2, 2]}, "total": [4, 4]},
  "light_days":     {"count": 1, "cats": {"four": [3, 3], "eve": [0, 0]}, "total": [3, 3]}
}
```

(`packages_example.json` is this file.) `count` is how many people get the package; `cats`
gives `[min, max]` shifts per category; `total` gives `[min, max]` shifts overall. Categories
are `four` and `eve` for the three-shift structure, `M` and `L` for two, `B1`–`B5` for single.
The counts must add up to the number of people, and the category totals must match the number
of slots. Pass the file with `--packages`.

**Different shift times or categories.** Edit the `STRUCTURES` dictionary at the top of
`oncall_market.py`: each structure lists its shifts (code, label, time, hours), how codes group
into categories, which shift is the "late" one and which are "early", and its packages.

## Running the tests

The automated tests live in `tests/` and need only `pytest` (the scheduler itself still has no
dependencies):

```bash
python -m pip install pytest
```

```bash
python -m pytest -q
```

The default run takes about a minute. It covers CSV parsing and round trips, the rank-to-points
conversion, package validation, the constraint and feasibility checks, `best_response` against
brute force on tiny problems, a full solve of `preferences_example.csv` at seed 1 (same schedule
every run, regret below 0.1), the command line, and the GUI's helpers and API. Solves of the
five-block structure take about 25 s each and are marked `slow`: they are skipped by default and
run with `python -m pytest -q -m slow` (`-m "slow or not slow"` runs everything).

## Files

- `oncall_market.py` — the scheduler (library and command line). `solve_sheet(text, ...)` is the
  library entry point: it takes the preferences CSV as text and returns a plain dict (schedule,
  summary, prices, report); the command line and the browser window both go through it.
- `oncall_gui.py` — the browser window; needs `oncall_market.py` in the same folder.
- `tests/`, `pytest.ini` — the automated tests (see "Running the tests").
- `screenshot_sheet.png`, `screenshot_results.png` — what the window looks like.
- `preferences_example.csv` — a filled-in example (three-shift structure).
- `packages_example.json` — the default three-shift packages, ready to edit.
- `schedule.csv`, `summary.csv` — written by `solve`.
