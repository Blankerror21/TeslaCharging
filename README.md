# TeslaCharging

Tracks kWh used charging at home, and what's owed to the homeowner for it.

Replaces the `Electric_Usage.xlsx` spreadsheet this started as. Readings live
in a CSV that diffs cleanly in git; the totals are computed rather than typed,
so a dragged formula can't quietly bill the wrong month again.

Python 3.9+, standard library only. Nothing to install.

## Usage

```
python3 charging.py summary     # what's owed, by month
python3 charging.py report      # every reading, with its cost
python3 charging.py check       # validate the data
```

`summary` and `report` take `--year 2025`, `--meter wall_connector` (repeatable),
`--since YYYY-MM-DD`, and `--until YYYY-MM-DD`.

### Recording a month

```
python3 charging.py add --month 2026-09 --kwh 610.4
```

That's the whole monthly ritual. `--meter` defaults to `wall_connector`, the
only meter still running; `--month` expands to the first and last day of that
month. For a period that isn't a calendar month, use `--start` and `--end`.

### Catching up on several months

```
python3 charging.py import <<EOF
2025-10  610.4
2025-11  588.1
2025-12  655.2
EOF
```

One `YYYY-MM  kwh  [meter]` line per month, from a file or stdin. Blank lines
and `#` comments are ignored, and it's tolerant about how you paste — tabs,
commas, or thousands separators (`1,610.4`) all work.

Both commands refuse to write a reading that would overlap or duplicate an
existing one for the same meter, so a mistyped date is caught at entry rather
than showing up as a wrong bill later. `import` validates the whole batch
first and writes nothing if any line is bad. `--force` overrides. Editing
`data/readings.csv` by hand is fine too — `check` is the safety net either way.

## Data

**`data/readings.csv`** — one row per meter per billing period.

| column  | meaning                                                    |
| ------- | ---------------------------------------------------------- |
| `start` | first day of the period, `YYYY-MM-DD`                      |
| `end`   | last day, inclusive                                        |
| `meter` | `meter_120v`, `meter_240v`, or `wall_connector` (below)    |
| `kwh`   | kWh used over the period                                   |
| `note`  | free text, optional                                        |

Two periods on the same meter may share a boundary date — one meter read
closes a period and opens the next. Different meters covering the same days
is expected and not an error.

**`data/rates.csv`** — `effective_from,usd_per_kwh,note`, one row per rate
change. A period is billed at the rate in effect on its **end** date, the day
the meter is read. When the rate changes, add a row; don't edit history.

**The three meters:**

- `wall_connector` — the Tesla Wall Connector. The only one still running,
  and the default for `add` and `import`.
- `meter_120v` — the crypto mining rig. Shut off in March 2025 once the power
  cost more than the mining earned; the readings stop there.
- `meter_240v` — recorded over the same months as the mining rig, ending
  March 2025. See the open question below.

To track a new circuit, add its name to `METERS` in `charging.py`.

## How months are assigned

Billing periods don't line up with calendar months. A period is attributed
whole to whichever month holds most of its days — so `11/1 – 12/2` is a
November bill and `3/31 – 4/30` an April one. Going by the start or end date
alone gets one of those two wrong.

## What `check` looks for

Errors (the bill can't be trusted): overlapping or duplicate periods on one
meter, `end` before `start`, negative kWh, unknown meter name, a period with
no rate covering it. Warnings (worth a look): gaps in coverage, zero-kWh
readings, and any period whose kWh/day is more than 3× that meter's own
median — the usual shape of a transposed digit.

`check` exits non-zero if there are errors.

## Migrated from the spreadsheet

`scripts/migrate_from_xlsx.py` produced `data/readings.csv` from the original
`Electric_Usage.xlsx` (it needs `openpyxl`; nothing else here does). It's kept
for provenance. Two things were changed on the way in:

- **Two end-year typos fixed.** Rows 11 and 25 both read `12/x/24 - 1/4/24`,
  which ends before it starts. Corrected to `2025`.
- **One empty row dropped.** Row 35 (`10/1/25 - 10/30/25`) had no kWh recorded
  but its formulas still produced a $133.09 charge. It's simply absent until a
  reading is entered.

Everything else came across as-is, including the February 2025 overlap that
`check` reports — that's real data needing a decision, not something to paper
over.

### The bug that motivated this

In the sheet, the total-cost and total-kWh columns used `=E<n-14>+E<n>`. That
offset was correct while the upstairs "Electric usage" block had rows to point
at, but that block stopped in March 2025 while the wall-connector block kept
going. Past row 31 the reference wrapped back into the wall-connector block
itself:

| Month    | Sheet showed | Actual  | Because it added         |
| -------- | ------------ | ------- | ------------------------ |
| Jul 2025 | $155.61      | $119.39 | 5/26–5/30 **2024**       |
| Aug 2025 | *(blank)*    | $103.05 | formula missing entirely |
| Sep 2025 | $258.80      | $108.34 | July **2024**            |
| Oct 2025 | $133.09      | —       | Aug **2024**; no reading |

`tests/test_charging.py` locks in those four figures, and asserts the months
the sheet got right still come out unchanged.

## What each meter was, and how they combine

All three meters are separate loads, so `summary` adds them together for a
total power bill. `--meter wall_connector` narrows it to car charging alone.

`meter_240v` was ambiguous for a while -- it ran over exactly the same months
as the mining rig, but at 1,273.7 kWh for June 2024 against the wall
connector's 791.9, it could plausibly have been a whole-circuit meter that
already included the car. A yearly "Vehicle" export from the energy monitor
settled it:

| 2024 total            | kWh    | vs. export |
| --------------------- | ------ | ---------- |
| Vehicle (export)      |  5,200 | --         |
| wall_connector        |  5,087 | -113 (2%)  |
| wall_connector + 240v | 15,328 | +10,128    |

The wall connector alone tracks the car's real usage to within 2%, over a year
where our readings don't even start until May 26. Adding `meter_240v` overshoots
threefold, so it was not measuring the car. Combined with its dates, it was the
mining rig's 240v leg.

That 2% also means the car used almost nothing before late May 2024, which is
where the wall connector readings begin.

## Tests

```
python3 -m unittest discover -s tests
```
