#!/usr/bin/env python3
"""Track home charging usage and what's owed to the homeowner.

Readings live in data/readings.csv, one row per meter per billing period.
Rates live in data/rates.csv, effective-dated so a rate change doesn't
require touching historical data.

  python3 charging.py summary          what's owed, by month
  python3 charging.py report           every reading, with cost
  python3 charging.py check            validate the data
  python3 charging.py add ...          append a reading
  python3 charging.py import f.txt     backfill several months at once

Standard library only.
"""

import argparse
import calendar
import csv
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
READINGS_CSV = DATA_DIR / "readings.csv"
RATES_CSV = DATA_DIR / "rates.csv"

# Every meter that can appear in readings.csv. Adding one here is the only
# step needed to start tracking a new circuit.
#
#   meter_120v      the crypto mining rig, shut off in March 2025
#   meter_240v      recorded alongside it over the same months
#   wall_connector  the Tesla Wall Connector -- the only live meter
METERS = ("meter_120v", "meter_240v", "wall_connector")

CENT = Decimal("0.01")


# The meter still in use. Mining is shut off, so this is what a bare
# `add` or `import` means unless told otherwise.
DEFAULT_METER = "wall_connector"


class DataError(Exception):
    """Raised when readings.csv or rates.csv cannot be interpreted at all."""


def month_bounds(text):
    """'2025-10' -> (2025-10-01, 2025-10-31)."""
    try:
        year, month = (int(part) for part in text.strip().split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, TypeError, calendar.IllegalMonthError):
        raise DataError(f"{text!r} is not a YYYY-MM month") from None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


class Reading:
    __slots__ = ("start", "end", "meter", "kwh", "note", "line")

    def __init__(self, start, end, meter, kwh, note="", line=0):
        self.start = start
        self.end = end
        self.meter = meter
        self.kwh = kwh
        self.note = note
        self.line = line

    @property
    def days(self):
        """Length of the billing period, counting both endpoints."""
        return (self.end - self.start).days + 1

    def cost(self, rates):
        """Cost of this reading, rounded to whole cents.

        The rate in effect on the period's end date applies to the whole
        period -- that is the date the meter is read and the bill is cut.
        """
        rate = rate_on(rates, self.end)
        return (Decimal(str(self.kwh)) * rate).quantize(CENT, rounding=ROUND_HALF_UP)

    def __repr__(self):
        return f"Reading({self.start} {self.end} {self.meter} {self.kwh})"


class Rate:
    __slots__ = ("effective_from", "usd_per_kwh", "note")

    def __init__(self, effective_from, usd_per_kwh, note=""):
        self.effective_from = effective_from
        self.usd_per_kwh = usd_per_kwh
        self.note = note


def parse_date(text, field, line):
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise DataError(f"line {line}: {field} {text!r} is not YYYY-MM-DD") from None


def load_readings(path=READINGS_CSV):
    if not path.exists():
        raise DataError(f"{path} not found")
    readings = []
    with path.open(newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                kwh = float(row["kwh"])
            except (KeyError, TypeError, ValueError):
                raise DataError(f"line {line}: kwh {row.get('kwh')!r} is not a number") from None
            readings.append(
                Reading(
                    start=parse_date(row["start"], "start", line),
                    end=parse_date(row["end"], "end", line),
                    meter=(row["meter"] or "").strip(),
                    kwh=kwh,
                    note=(row.get("note") or "").strip(),
                    line=line,
                )
            )
    readings.sort(key=lambda r: (r.end, r.start, r.meter))
    return readings


def load_rates(path=RATES_CSV):
    if not path.exists():
        raise DataError(f"{path} not found")
    rates = []
    with path.open(newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                usd = Decimal(row["usd_per_kwh"].strip())
            except (KeyError, AttributeError, ArithmeticError):
                raise DataError(
                    f"line {line}: usd_per_kwh {row.get('usd_per_kwh')!r} is not a number"
                ) from None
            rates.append(
                Rate(
                    effective_from=parse_date(row["effective_from"], "effective_from", line),
                    usd_per_kwh=usd,
                    note=(row.get("note") or "").strip(),
                )
            )
    if not rates:
        raise DataError(f"{path} contains no rates")
    rates.sort(key=lambda r: r.effective_from)
    return rates


def rate_on(rates, day):
    """The rate in effect on `day` -- the latest one that started on or before it."""
    chosen = None
    for rate in rates:
        if rate.effective_from <= day:
            chosen = rate
        else:
            break
    if chosen is None:
        raise DataError(
            f"no rate covers {day}; earliest rate starts {rates[0].effective_from}"
        )
    return chosen.usd_per_kwh


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def select(readings, year=None, meters=None, since=None, until=None):
    """Filter readings.

    --year matches the billing month the period is attributed to, so it agrees
    with `summary` for periods that straddle New Year. --since/--until match
    the period's end date, the day the meter was read.
    """
    chosen = readings
    if year is not None:
        chosen = [r for r in chosen if billing_month(r)[0] == year]
    if meters:
        chosen = [r for r in chosen if r.meter in meters]
    if since is not None:
        chosen = [r for r in chosen if r.end >= since]
    if until is not None:
        chosen = [r for r in chosen if r.end <= until]
    return chosen


def billing_month(reading):
    """The calendar month a billing period belongs to.

    Periods rarely line up with calendar months -- "11/1 - 12/2" is a
    November bill, "3/31 - 4/30" an April one -- so a period is attributed
    whole to whichever month holds most of its days, earliest month winning
    a tie. Attributing by start or end date alone gets one of those two
    cases wrong.
    """
    days = defaultdict(int)
    day = reading.start
    while day <= reading.end:
        days[(day.year, day.month)] += 1
        day += timedelta(days=1)
    if not days:
        return (reading.start.year, reading.start.month)
    return max(sorted(days), key=lambda month: days[month])


def by_month(readings, rates):
    """Group readings by billing month. See billing_month for the rule."""
    months = defaultdict(lambda: {"kwh": 0.0, "cost": Decimal("0"), "meters": defaultdict(float)})
    for reading in readings:
        bucket = months[billing_month(reading)]
        bucket["kwh"] += reading.kwh
        bucket["cost"] += reading.cost(rates)
        bucket["meters"][reading.meter] += reading.kwh
    return dict(sorted(months.items()))


def totals(readings, rates):
    kwh = sum(r.kwh for r in readings)
    cost = sum((r.cost(rates) for r in readings), Decimal("0"))
    return kwh, cost


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

OUTLIER_FACTOR = 3.0


def median(values):
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def validate(readings, rates):
    """Return (errors, warnings) as lists of human-readable strings.

    Errors mean the data cannot be trusted to produce a correct bill.
    Warnings mean something looks off but the arithmetic still holds.
    """
    errors, warnings = [], []

    def at(reading):
        """Where a reading came from -- a file line, or the one being added."""
        return f"line {reading.line}" if reading.line else "the new reading"

    for reading in readings:
        where = f"{at(reading)} ({reading.start}..{reading.end} {reading.meter})"
        if reading.meter not in METERS:
            errors.append(f"{where}: unknown meter {reading.meter!r}; expected one of {', '.join(METERS)}")
        if reading.end < reading.start:
            errors.append(f"{where}: end date is before start date")
        if reading.kwh < 0:
            errors.append(f"{where}: negative kWh ({reading.kwh})")
        elif reading.kwh == 0:
            warnings.append(f"{where}: zero kWh recorded")
        try:
            rate_on(rates, reading.end)
        except DataError as exc:
            errors.append(f"{where}: {exc}")

    per_meter = defaultdict(list)
    for reading in readings:
        per_meter[reading.meter].append(reading)

    for meter, group in sorted(per_meter.items()):
        group = sorted(group, key=lambda r: (r.start, r.end))

        seen = {}
        for reading in group:
            key = (reading.start, reading.end)
            if key in seen:
                errors.append(
                    f"{meter}: duplicate period {reading.start}..{reading.end} "
                    f"on {seen[key]} and {at(reading)}"
                )
            else:
                seen[key] = at(reading)

        for previous, current in zip(group, group[1:]):
            # A shared boundary date is normal: one read closes a period and
            # opens the next. Only a genuine overlap is an error.
            if current.start < previous.end:
                overlap = (previous.end - current.start).days
                errors.append(
                    f"{meter}: periods overlap by {overlap} day(s) -- "
                    f"{previous.start}..{previous.end} ({at(previous)}) and "
                    f"{current.start}..{current.end} ({at(current)})"
                )
            elif current.start > previous.end + timedelta(days=1):
                missing = (current.start - previous.end).days - 1
                warnings.append(
                    f"{meter}: {missing} day(s) unaccounted between "
                    f"{previous.end} ({at(previous)}) and "
                    f"{current.start} ({at(current)})"
                )

        # Flag a reading wildly out of line with the meter's own history --
        # the usual sign of a transposed digit or a missed period.
        rates_per_day = [r.kwh / r.days for r in group if r.days > 0 and r.kwh > 0]
        if len(rates_per_day) >= 4:
            typical = median(rates_per_day)
            if typical > 0:
                for reading in group:
                    if reading.days <= 0 or reading.kwh <= 0:
                        continue
                    daily = reading.kwh / reading.days
                    if daily > typical * OUTLIER_FACTOR:
                        warnings.append(
                            f"{meter}: {reading.start}..{reading.end} ({at(reading)}) "
                            f"averages {daily:.1f} kWh/day vs a typical {typical:.1f}"
                        )

    return errors, warnings


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def money(amount):
    return f"${amount:,.2f}"


def render_table(headers, rows, aligns=None):
    if not rows:
        return ""
    aligns = aligns or ["left"] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def line(cells):
        parts = []
        for index, cell in enumerate(cells):
            text = str(cell)
            parts.append(text.ljust(widths[index]) if aligns[index] == "left" else text.rjust(widths[index]))
        return "  ".join(parts).rstrip()

    out = [line(headers), "  ".join("-" * width for width in widths)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def cmd_summary(args, readings, rates):
    chosen = select(readings, args.year, args.meters, args.since, args.until)
    if not chosen:
        print("No readings match that filter.")
        return 0

    months = by_month(chosen, rates)
    meters = [m for m in METERS if any(m in b["meters"] for b in months.values())]

    headers = ["Month"] + [m.replace("_", " ") for m in meters] + ["Total kWh", "Owed"]
    aligns = ["left"] + ["right"] * (len(meters) + 2)
    rows = []
    for (year, month), bucket in months.items():
        row = [f"{MONTH_NAMES[month - 1]} {year}"]
        for meter in meters:
            value = bucket["meters"].get(meter)
            row.append(f"{value:,.1f}" if value else "-")
        row.append(f"{bucket['kwh']:,.1f}")
        row.append(money(bucket["cost"]))
        rows.append(row)

    print(render_table(headers, rows, aligns))

    kwh, cost = totals(chosen, rates)
    print()
    print(f"{len(months)} month(s), {kwh:,.1f} kWh, {money(cost)} owed")
    if len(meters) > 1:
        print()
        print("By meter:")
        for meter in meters:
            group = [r for r in chosen if r.meter == meter]
            meter_kwh, meter_cost = totals(group, rates)
            print(f"  {meter:<16} {meter_kwh:>10,.1f} kWh   {money(meter_cost):>12}")
    return 0


def cmd_report(args, readings, rates):
    chosen = select(readings, args.year, args.meters, args.since, args.until)
    if not chosen:
        print("No readings match that filter.")
        return 0

    headers = ["Start", "End", "Days", "Meter", "kWh", "Rate", "Cost"]
    aligns = ["left", "left", "right", "left", "right", "right", "right"]
    rows = [
        [
            r.start.isoformat(),
            r.end.isoformat(),
            r.days,
            r.meter,
            f"{r.kwh:,.2f}",
            f"${rate_on(rates, r.end):.4f}".rstrip("0").rstrip("."),
            money(r.cost(rates)),
        ]
        for r in chosen
    ]
    print(render_table(headers, rows, aligns))

    kwh, cost = totals(chosen, rates)
    print()
    print(f"{len(chosen)} reading(s), {kwh:,.1f} kWh, {money(cost)} owed")
    return 0


def cmd_check(args, readings, rates):
    errors, warnings = validate(readings, rates)
    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"ERROR {error}")
    print()
    print(f"{len(readings)} reading(s): {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def append_readings(new, readings, rates, force=False):
    """Validate `new` against existing readings, then append them together.

    Nothing is written unless the whole batch is clean, so a bad line in a
    backfill can't leave the file half-updated.
    """
    before, _ = validate(readings, rates)
    after, _ = validate(readings + new, rates)
    introduced = [e for e in after if e not in before]
    if introduced and not force:
        return introduced

    with READINGS_CSV.open("a", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        for reading in new:
            writer.writerow([
                reading.start.isoformat(), reading.end.isoformat(),
                reading.meter, f"{reading.kwh:g}", reading.note,
            ])
    return []


def resolve_period(args):
    """Period from either --month or --start/--end."""
    if args.month:
        if args.start or args.end:
            raise DataError("use --month or --start/--end, not both")
        return month_bounds(args.month)
    if not (args.start and args.end):
        raise DataError("give --month YYYY-MM, or both --start and --end")
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as exc:
        raise DataError(str(exc)) from None
    if end < start:
        raise DataError(f"end {end} is before start {start}")
    return start, end


def cmd_add(args, readings, rates):
    try:
        start, end = resolve_period(args)
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    new = Reading(start, end, args.meter, args.kwh, args.note or "")
    introduced = append_readings([new], readings, rates, args.force)
    if introduced:
        print("Refusing to add -- this reading would break the data:", file=sys.stderr)
        for error in introduced:
            print(f"  ERROR {error}", file=sys.stderr)
        print("Re-run with --force to add it anyway.", file=sys.stderr)
        return 1

    print(f"Added {start}..{end} {args.meter} {args.kwh:g} kWh = {money(new.cost(rates))}")
    return 0


def parse_backfill(lines, default_meter=DEFAULT_METER):
    """Parse `YYYY-MM  kwh  [meter]` lines. Blank lines and # comments ignored."""
    parsed = []
    for number, raw in enumerate(lines, start=1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        # Fields are whitespace-separated. A line with no whitespace at all
        # is treated as comma-separated, so a row pasted from a CSV export
        # works too. Any comma left inside a field is a thousands separator.
        fields = text.split()
        if len(fields) == 1 and "," in fields[0]:
            fields = fields[0].split(",")
        fields = [field for field in (f.replace(",", "").strip() for f in fields) if field]
        if len(fields) not in (2, 3):
            raise DataError(f"line {number}: expected 'YYYY-MM kwh [meter]', got {text!r}")
        start, end = month_bounds(fields[0])
        try:
            kwh = float(fields[1])
        except ValueError:
            raise DataError(f"line {number}: {fields[1]!r} is not a number") from None
        meter = fields[2] if len(fields) == 3 else default_meter
        if meter not in METERS:
            raise DataError(f"line {number}: unknown meter {meter!r}")
        parsed.append(Reading(start, end, meter, kwh))
    return parsed


def cmd_import(args, readings, rates):
    """Backfill several whole months at once, from a file or stdin."""
    source = open(args.file) if args.file else sys.stdin
    try:
        lines = source.readlines()
    finally:
        if args.file:
            source.close()

    try:
        new = parse_backfill(lines, args.meter)
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not new:
        print("Nothing to import.")
        return 0

    introduced = append_readings(new, readings, rates, args.force)
    if introduced:
        print(f"Refusing to import {len(new)} reading(s) -- nothing was written:", file=sys.stderr)
        for error in introduced:
            print(f"  ERROR {error}", file=sys.stderr)
        print("Fix the input, or re-run with --force.", file=sys.stderr)
        return 1

    total = sum((r.cost(rates) for r in new), Decimal("0"))
    for reading in new:
        print(f"  {reading.start}..{reading.end}  {reading.meter:<15} "
              f"{reading.kwh:>8,.1f} kWh  {money(reading.cost(rates)):>9}")
    print(f"\nImported {len(new)} reading(s), {money(total)} added.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_filters(parser):
    parser.add_argument("--year", type=int, help="only periods ending in this year")
    parser.add_argument(
        "--meter", dest="meters", action="append", choices=METERS,
        help="only this meter (repeatable)",
    )
    parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="only periods ending on or after this date")
    parser.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="only periods ending on or before this date")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="charging.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary", help="what's owed, by month")
    add_filters(summary)
    summary.set_defaults(func=cmd_summary)

    report = sub.add_parser("report", help="every reading, with cost")
    add_filters(report)
    report.set_defaults(func=cmd_report)

    check = sub.add_parser("check", help="validate the data")
    check.set_defaults(func=cmd_check)

    add = sub.add_parser("add", help="append one reading")
    add.add_argument("--month", metavar="YYYY-MM", help="a whole calendar month")
    add.add_argument("--start", metavar="YYYY-MM-DD", help="or an explicit period")
    add.add_argument("--end", metavar="YYYY-MM-DD")
    add.add_argument("--meter", choices=METERS, default=DEFAULT_METER)
    add.add_argument("--kwh", required=True, type=float)
    add.add_argument("--note", default="")
    add.add_argument("--force", action="store_true", help="add even if it breaks validation")
    add.set_defaults(func=cmd_add)

    imp = sub.add_parser(
        "import", help="backfill whole months from a file or stdin",
        description="Reads 'YYYY-MM  kwh  [meter]' lines. Blank lines and "
                    "# comments are ignored. Nothing is written unless every "
                    "line is valid.",
    )
    imp.add_argument("file", nargs="?", help="input file (default: stdin)")
    imp.add_argument("--meter", choices=METERS, default=DEFAULT_METER,
                     help="meter for lines that don't name one")
    imp.add_argument("--force", action="store_true", help="import even if it breaks validation")
    imp.set_defaults(func=cmd_import)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        readings = load_readings()
        rates = load_rates()
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args, readings, rates)
    except BrokenPipeError:
        # Something downstream (`| head`) stopped reading. Point stdout at
        # devnull so the interpreter's own flush on exit doesn't raise again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
