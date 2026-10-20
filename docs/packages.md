# Automatic packages (`packages.py`)

`oncall_market.py` ships fairness packages for exactly 7 people over 9 weekdays. `packages.py`
derives them for any headcount, period and structure, so the hosted app can build packages for
whoever responded:

```bash
python packages.py --structure three --people 6 --days 9 --out packages.json
```

```python
from packages import derive_packages, describe
packages = derive_packages("three", 6, 9)        # days: a number or a list of dates
print(describe(packages, "three"))               # the table shown below
```

The result is a dict in the packages JSON shape (`count`, `cats` with `[min, max]` per category,
`total`), validated by `oncall_market.Problem`, and the same every time for the same inputs.
When no fair split exists, `derive_packages` raises `SchedulingError` with the headcounts that
would work, or the `max_per_day` that would.

## The principles

- **P1 Hours at the floor.** Everyone works either the floor or the floor plus one granule (the
  greatest common divisor of the shift lengths, 2 h for the built-in structures), with as few
  people as possible on the higher load. For 7 people over 9 days that is 12 h for four and 14 h
  for three.
- **P2 Evenings balanced.** Late shifts are spread as evenly as possible, and the higher-hours
  package carries fewer late shifts than the lower-hours package that carries the evenings
  (`heavy` has one evening, `light_evenings` two).
- **P3 One shift a day.** At most `max_per_day` shifts per person per day, so nobody can have more
  than `days x max_per_day` shifts, nor more of a category than its daily slots allow.
- **P4 No late shift before an early one.** The solver enforces this per schedule; the packages
  only have to leave it possible.

## The algorithm

1. **Loads.** Total hours are divided by the headcount and rounded down to a multiple of the
   granule: the lower load. The remainder says how many people must take one granule more.
   Both loads and that headcount are fixed by arithmetic; the search never changes them.
2. **Shapes.** Every combination of per-category shift counts whose hours equal one of the two
   loads, with each count at most `days x min(max_per_day, slots of that category per day)` and
   the total at most `days x max_per_day` (P3). For the three-shift structure at 14 h these are
   3 four-hour + 1 evening, 2 + 3, 1 + 5 and 0 + 7.
3. **Multiset.** A multiset of shapes, one per person, with the right number on the higher load
   and category totals equal to the slot supply. Candidates are searched with a rising cap on the
   late-shift count, so the first cap that admits any solution gives the smallest possible maximum
   of late shifts. Among those, the best by this order: fewest higher-load people with at least as
   many late shifts as the lower-load package that carries the most (P2); most even late counts
   (smallest sum of squares); fewest distinct packages; then a fixed order of the shapes.
4. **Block structures** (`single`: every category is one two-hour block). Everyone gets the floor
   of each block type and the leftover blocks become ranges `[floor, floor + 1]`. The heavier load
   takes its extras in the middle of the day and the lighter load at the edges (8 am or 6 pm),
   which minimises the heavier load's evenings and mornings; when the middle blocks cannot hold
   the extras, the remainder spills to the early block, then the late one. The late count is
   always the floor or one more, so P2 holds automatically.
5. **Names.** A derived package takes the built-in name when its contents and role (heavier or
   lighter load) match a built-in package, so 7 x 9 gives `heavy`, `light_evenings`, `light_days`.
   Otherwise the name says the hours and late shifts (`14h_1eve`, `16h_2L`), or the block count
   (`heavy8`, `light7`, `all9` when everyone has the same load).
6. **Checks.** The result is passed through `oncall_market.Problem`, and the tests also verify
   that package totals add up to the slot supply and that samples really solve.

Infeasible inputs raise with a reason: too few people for the slots a day (`use at least 5 people
or max_per_day 2`), or no two-level split of the supply (`Headcounts that work: 5 or 10`). The
two-shift structure (4 h and 6 h shifts) is the awkward one: 60 of the 220 headcount/period pairs
between 3 and 12 people and 5 and 15 days have no fair split, for example 5 people over 7 days,
where everyone would work 14 h and the only 14 h shape (2 mornings + 1 afternoon) needs 10
mornings out of 7.

## Worked examples (three-shift structure)

Tables as printed by `describe()`. 90 hours over 9 days; `four` is a 4-hour shift (8-12 or
1-5:30 pm), `eve` the 2-hour evening.

**6 people x 9 days** (`python packages.py --people 6 --days 9`): 15 h each is not a multiple of
2 h, so three people work 16 h and three 14 h. Nine evenings over six people force someone to take
two; with the maximum at two evenings the only split gives the two-evening shapes to the 16 h
people. Here the two halves of P2 conflict and the even spread wins: the heavier package carries
more evenings, not fewer, and the market prices it accordingly.

| Package  | Count | Contents                             | Hours |
|----------|-------|--------------------------------------|-------|
| 16h_2eve | 3     | 3 x (8-12 or 1-5:30 pm) + 2 x 6-8 pm | 16    |
| 14h_1eve | 3     | 3 x (8-12 or 1-5:30 pm) + 1 x 6-8 pm | 14    |

**7 people x 10 days**: 100 h, so one person works 16 h and six work 14 h. Ten evenings cannot
be spread with a maximum of two (the 14 h shapes with at most two evenings carry one each), so
the maximum is three. Two splits achieve it: `4 x four` for the 16 h person with two people on
`2 four + 3 eve`, or the table below. Both satisfy P2's trade-off (the 16 h package has fewer
evenings than the package that carries the most); the table wins on evenness (late counts
1,1,1,1,1,2,3 against 0,1,1,1,1,3,3).

| Package  | Count | Contents                             | Hours |
|----------|-------|--------------------------------------|-------|
| 16h_2eve | 1     | 3 x (8-12 or 1-5:30 pm) + 2 x 6-8 pm | 16    |
| 14h_3eve | 1     | 2 x (8-12 or 1-5:30 pm) + 3 x 6-8 pm | 14    |
| 14h_1eve | 5     | 3 x (8-12 or 1-5:30 pm) + 1 x 6-8 pm | 14    |

**8 people x 9 days**: 11.25 h each, so five people work 12 h and three 10 h. The split is the
same pattern as the built-in 7 x 9 one shifted down a level: the lighter package has one evening,
the heavier ones two or none.

| Package  | Count | Contents                             | Hours |
|----------|-------|--------------------------------------|-------|
| 12h_2eve | 3     | 2 x (8-12 or 1-5:30 pm) + 2 x 6-8 pm | 12    |
| 12h_0eve | 2     | 3 x (8-12 or 1-5:30 pm)              | 12    |
| 10h_1eve | 3     | 2 x (8-12 or 1-5:30 pm) + 1 x 6-8 pm | 10    |

## Limits

- P4 is not modelled. With two people on the two-shift structure at one shift a day, whoever
  takes the afternoon must take it again the next day, so the fair split is unschedulable within
  a week; the solver reports the violation. Three or more people always leave room.
- Block-structure ranges leave the exact split to the market, as the built-ins do; the tests
  solve samples (`-m slow` for the 15-30 s five-block ones) to confirm the ranges are achievable.
- `days` only matters as a count; holidays passed as `--skip` change the dates, not the packages.
