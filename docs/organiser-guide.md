# Organiser guide

One cycle = one scheduling period. You create it, send everyone a personal link, close
collection at the deadline, run the market, review the result, publish it and download the
records. Everything happens on your organiser link (`?a=...`), which has seven tabs.

## Create a cycle

1. Open the app's address. Enter a name (for example "On-call 2 to 12 November 2026") and
   click **Create a new cycle**.
2. **Save the organiser link that appears.** It is shown once and it is the only way to
   manage the cycle. Anyone who has it can manage the cycle, so keep it to yourself.

## Setup tab

Fill in the period and the people, then **Save settings**:

- Shift structure: three shifts a day (8-12, 1-5:30, 6-8) is the default; two shifts and
  five 2-hour blocks are also available.
- First day and number of weekdays. Weekends are skipped automatically; list holidays under
  "Dates to skip".
- Shifts one person may take on the same day (normally 1).
- Minimum number of slots each person must rank. The default is the largest package size
  plus two (6 for the three-shift structure). This stops people ranking only one or two
  must-have slots.
- Names, one per line (up to 10). Names must differ from each other.
- The deadline note is shown to participants on their form.
- The app address is used to build the links; it is prefilled and normally needs no change.

When everything is right, click **Open collection**. The settings lock, the personal links
become active and the cycle is "collecting". Only the deadline note and the app address can
be changed afterwards; for a different period or group, create a new cycle.

## Links tab

Copy each person's personal link and send it by hand (email, chat, whatever you use). The
box "All links, one per line" has them all for pasting. The public schedule link shows the
published schedule and nothing else; share it after publishing.

## Status tab

Shows who has saved and when, with how many slots they ranked and marked X.

- **Clear** deletes someone's saved entries (they can fill the form in again while
  collection is open).
- **New link** gives someone a fresh personal link; the old one stops working at once. Use
  it when a link was forwarded or lost.
- **Close collection** at the deadline. People who have not saved by then are excluded from
  the cycle. **Reopen collection** lets everyone edit again; run the market again afterwards.

## Run market tab

The tab lists the responders and anyone excluded, and the packages that will be used (how
many shifts of each kind each person takes, and the hours). Packages are derived for the
responders only. If the app says automatic packages are not available for this number of
people and days, paste a packages JSON in the override box (the format is under "Changing
the system" in README.md) and save it.

Click **Run the market**. One solve takes about 10 to 20 seconds on a laptop and up to a
minute or two on the free host; stay on the page. Several schedules often satisfy the same
total of preference points; the run keeps up to 24 of them, marked **equivalent**. The tab
lists the first six (and the chosen one); tick **Show all** below the list to see the rest,
and the caption above the list says when the solver found more than the 24 kept. The
only difference between them is who gets which equivalent bundle (typically who takes the
heavier package). The market's own choice comes first, picked by a fixed rule: the highest
minimum % of ideal, then the smallest spread of net price, then the fewest late shifts for
the person with the most hours. The choice between them is yours. For each outcome you see
the schedule, a table per person (package, hours, late shifts, points, % of ideal, net
price, violations), the market status line and any warnings (for example someone who ranked
fewer slots than the minimum). Click **Choose this outcome**.

What the numbers mean:

- Points: how much of that person's 100 points landed on the slots they got.
- % of ideal: those points as a share of the best they could have got with nobody else in
  the way. The fairest single satisfaction measure.
- Net price: points paid for the bundle (positive: they got popular slots; negative: they
  were compensated for taking unpopular ones).
- Maximum regret: how much better anyone could do by switching bundles at the final prices;
  below 0.1 is as good as an exact clearing. Above 0.5 the prices are indicative only and
  the outcome says so (the schedule itself is still the best one found).

## Review and edit tab

The chosen schedule as a grid with the per-person table. You can change it by hand:

- **Swap** the people on two slots.
- **Assign** a slot to someone else.

After every change the table is recomputed: hours, late shifts, points, % of ideal, net
price, rule violations (an X slot, two shifts a day, a late shift before an early one) and
whether each person's slots still match their package. A warning lists anyone affected.
Every change is written to the log with a readable description, shown below the controls,
on the published schedule and in `edits.csv`. **Undo the last change** adds a reversing
entry rather than deleting one, so the log stays complete.

## Publish tab

**Publish the schedule** makes the grid visible to every participant through their own link
and at the public link. Participants see the schedule and the log of hand changes only;
prices, points and summaries stay on your dashboard. **Unpublish** takes it down again (for
example to make another change), then publish again.

Download and keep the four files after each cycle:

- `schedule.csv`: one row per slot with the person and the clearing price.
- `summary.csv`: one row per person with package, hours, points, % of ideal, net price and
  the suggested next budget.
- `preferences.csv`: everyone's column in the command-line tool's format.
- `edits.csv`: the hand changes.

## Danger zone tab

Deletes the cycle with all its data after you type its name and tick the confirmation.
Download the CSVs first.

## Common situations

- Someone did not save by the deadline: they are excluded from this cycle (decision taken
  on 2026-10-19). If you want them in, reopen collection, ask them to save, close again and
  run the market again.
- Someone saved after you ran the market (because you reopened): the Run market tab warns
  that preferences changed; run again before choosing.
- Fewer or more than 7 people, or a period other than 9 weekdays: packages are derived for
  that size automatically. If the Run market tab says they could not be, paste a packages JSON.
- A participant lost their link: Status tab, **New link**, send the new one.
- You lost the organiser link: there is no recovery. Create a new cycle (the participants
  will need new links).
