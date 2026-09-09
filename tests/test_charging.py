"""Tests for charging.py. Run with: python3 -m unittest discover tests"""

import csv
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import charging
from charging import DataError, Reading, billing_month, rate_on, validate


def reading(start, end, meter="wall_connector", kwh=100.0, line=0):
    return Reading(date.fromisoformat(start), date.fromisoformat(end), meter, kwh, line=line)


def write_csv(directory, name, header, rows):
    path = Path(directory) / name
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


FLAT_RATES = [charging.Rate(date(2024, 1, 1), Decimal("0.18"))]


class TestRates(unittest.TestCase):
    def test_picks_latest_rate_at_or_before_the_date(self):
        rates = [
            charging.Rate(date(2024, 1, 1), Decimal("0.18")),
            charging.Rate(date(2025, 6, 1), Decimal("0.21")),
        ]
        self.assertEqual(rate_on(rates, date(2024, 1, 1)), Decimal("0.18"))
        self.assertEqual(rate_on(rates, date(2025, 5, 31)), Decimal("0.18"))
        self.assertEqual(rate_on(rates, date(2025, 6, 1)), Decimal("0.21"))
        self.assertEqual(rate_on(rates, date(2030, 1, 1)), Decimal("0.21"))

    def test_date_before_every_rate_is_an_error(self):
        with self.assertRaises(DataError):
            rate_on(FLAT_RATES, date(2023, 12, 31))

    def test_rate_applies_from_the_period_end_date(self):
        rates = [
            charging.Rate(date(2024, 1, 1), Decimal("0.18")),
            charging.Rate(date(2025, 6, 15), Decimal("0.20")),
        ]
        spanning = reading("2025-06-01", "2025-06-30", kwh=100.0)
        self.assertEqual(spanning.cost(rates), Decimal("20.00"))


class TestCost(unittest.TestCase):
    def test_rounds_to_whole_cents(self):
        self.assertEqual(reading("2025-01-01", "2025-01-31", kwh=663.3).cost(FLAT_RATES),
                         Decimal("119.39"))

    def test_rounds_half_up_not_to_even(self):
        # 0.25 kWh * 0.18 = 0.045 -> 0.05, not banker's-rounded to 0.04.
        self.assertEqual(reading("2025-01-01", "2025-01-31", kwh=0.25).cost(FLAT_RATES),
                         Decimal("0.05"))

    def test_days_counts_both_endpoints(self):
        self.assertEqual(reading("2025-01-01", "2025-01-31").days, 31)
        self.assertEqual(reading("2025-01-01", "2025-01-01").days, 1)


class TestBillingMonth(unittest.TestCase):
    def test_period_starting_on_the_last_day_of_a_month(self):
        # "3/31/24 - 4/30/24" is an April bill, not a March one.
        self.assertEqual(billing_month(reading("2024-03-31", "2024-04-30")), (2024, 4))

    def test_period_spilling_into_the_next_month(self):
        # "11/1/24 - 12/2/24" is a November bill, not a December one.
        self.assertEqual(billing_month(reading("2024-11-01", "2024-12-02")), (2024, 11))

    def test_period_inside_one_month(self):
        self.assertEqual(billing_month(reading("2024-05-26", "2024-05-30")), (2024, 5))

    def test_year_boundary(self):
        self.assertEqual(billing_month(reading("2024-12-03", "2025-01-04")), (2024, 12))

    def test_tie_goes_to_the_earlier_month(self):
        # 15 days in Jan, 15 in Feb.
        self.assertEqual(billing_month(reading("2025-01-17", "2025-02-15")), (2025, 1))

    def test_single_day_period(self):
        self.assertEqual(billing_month(reading("2025-07-04", "2025-07-04")), (2025, 7))


class TestValidate(unittest.TestCase):
    def test_clean_data_passes(self):
        readings = [
            reading("2025-01-01", "2025-01-31", line=2),
            reading("2025-02-01", "2025-02-28", line=3),
        ]
        errors, warnings = validate(readings, FLAT_RATES)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_shared_boundary_date_is_not_an_overlap(self):
        # One meter read closes a period and opens the next.
        readings = [
            reading("2025-01-01", "2025-02-01", line=2),
            reading("2025-02-01", "2025-03-01", line=3),
        ]
        errors, _ = validate(readings, FLAT_RATES)
        self.assertEqual(errors, [])

    def test_real_overlap_is_an_error(self):
        readings = [
            reading("2025-01-04", "2025-02-04", line=2),
            reading("2025-02-01", "2025-03-01", line=3),
        ]
        errors, _ = validate(readings, FLAT_RATES)
        self.assertEqual(len(errors), 1)
        self.assertIn("overlap by 3 day(s)", errors[0])

    def test_gap_is_a_warning_not_an_error(self):
        readings = [
            reading("2024-08-01", "2024-08-18", line=2),
            reading("2024-09-02", "2024-09-30", line=3),
        ]
        errors, warnings = validate(readings, FLAT_RATES)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("14 day(s) unaccounted", warnings[0])

    def test_consecutive_months_leave_no_gap(self):
        readings = [
            reading("2025-06-01", "2025-06-30", line=2),
            reading("2025-07-01", "2025-07-31", line=3),
        ]
        _, warnings = validate(readings, FLAT_RATES)
        self.assertEqual(warnings, [])

    def test_different_meters_may_cover_the_same_days(self):
        readings = [
            reading("2025-01-01", "2025-01-31", meter="meter_120v", line=2),
            reading("2025-01-01", "2025-01-31", meter="meter_240v", line=3),
            reading("2025-01-01", "2025-01-31", meter="wall_connector", line=4),
        ]
        errors, warnings = validate(readings, FLAT_RATES)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_duplicate_period_on_one_meter(self):
        readings = [
            reading("2025-01-01", "2025-01-31", line=2),
            reading("2025-01-01", "2025-01-31", line=3),
        ]
        errors, _ = validate(readings, FLAT_RATES)
        self.assertTrue(any("duplicate period" in e for e in errors))

    def test_unknown_meter(self):
        errors, _ = validate([reading("2025-01-01", "2025-01-31", meter="typo")], FLAT_RATES)
        self.assertTrue(any("unknown meter" in e for e in errors))

    def test_end_before_start(self):
        errors, _ = validate([reading("2025-02-01", "2025-01-01")], FLAT_RATES)
        self.assertTrue(any("end date is before start date" in e for e in errors))

    def test_negative_kwh_is_an_error_zero_is_a_warning(self):
        errors, _ = validate([reading("2025-01-01", "2025-01-31", kwh=-5.0)], FLAT_RATES)
        self.assertTrue(any("negative kWh" in e for e in errors))
        _, warnings = validate([reading("2025-01-01", "2025-01-31", kwh=0.0)], FLAT_RATES)
        self.assertTrue(any("zero kWh" in e for e in warnings))

    def test_period_with_no_covering_rate(self):
        errors, _ = validate([reading("2023-01-01", "2023-01-31")], FLAT_RATES)
        self.assertTrue(any("no rate covers" in e for e in errors))

    def test_outlier_flags_a_transposed_digit(self):
        readings = [reading(f"2025-{m:02d}-01", f"2025-{m:02d}-28", kwh=600.0, line=m)
                    for m in range(1, 6)]
        readings.append(reading("2025-06-01", "2025-06-28", kwh=6000.0, line=6))
        _, warnings = validate(readings, FLAT_RATES)
        self.assertTrue(any("kWh/day vs a typical" in w for w in warnings))

    def test_outlier_needs_enough_history_to_judge(self):
        readings = [
            reading("2025-01-01", "2025-01-31", kwh=600.0, line=2),
            reading("2025-02-01", "2025-02-28", kwh=6000.0, line=3),
        ]
        _, warnings = validate(readings, FLAT_RATES)
        self.assertFalse(any("kWh/day" in w for w in warnings))


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.readings = [
            reading("2024-12-01", "2024-12-31", meter="meter_120v"),
            reading("2025-01-01", "2025-01-31", meter="wall_connector"),
            reading("2025-06-01", "2025-06-30", meter="wall_connector"),
        ]

    def test_year(self):
        self.assertEqual(len(charging.select(self.readings, year=2025)), 2)

    def test_meter(self):
        self.assertEqual(len(charging.select(self.readings, meters=["meter_120v"])), 1)

    def test_since_and_until(self):
        chosen = charging.select(self.readings, since=date(2025, 1, 1), until=date(2025, 2, 1))
        self.assertEqual(len(chosen), 1)


class TestLoading(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, "readings.csv", ["start", "end", "meter", "kwh", "note"],
                             [["2025-01-01", "2025-01-31", "wall_connector", "600.5", "hi"]])
            loaded = charging.load_readings(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].kwh, 600.5)
        self.assertEqual(loaded[0].note, "hi")

    def test_blank_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readings.csv"
            path.write_text("start,end,meter,kwh,note\n"
                            "2025-01-01,2025-01-31,wall_connector,600,\n"
                            ",,,,\n")
            self.assertEqual(len(charging.load_readings(path)), 1)

    def test_bad_date_is_reported_with_its_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, "readings.csv", ["start", "end", "meter", "kwh", "note"],
                             [["01/01/2025", "2025-01-31", "wall_connector", "600", ""]])
            with self.assertRaises(DataError) as caught:
                charging.load_readings(path)
        self.assertIn("line 2", str(caught.exception))

    def test_bad_kwh_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, "readings.csv", ["start", "end", "meter", "kwh", "note"],
                             [["2025-01-01", "2025-01-31", "wall_connector", "six hundred", ""]])
            with self.assertRaises(DataError):
                charging.load_readings(path)

    def test_empty_rates_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, "rates.csv", ["effective_from", "usd_per_kwh", "note"], [])
            with self.assertRaises(DataError):
                charging.load_rates(path)

    def test_missing_file_is_an_error(self):
        with self.assertRaises(DataError):
            charging.load_readings(Path("/nonexistent/readings.csv"))


class TestRealData(unittest.TestCase):
    """Locks in the figures the original spreadsheet's formulas got wrong.

    Rows 32-35 of the sheet referenced cells 14 rows up, which fell inside
    the wall-connector block itself once the upstairs block ran out, adding
    2024 costs onto 2025 months.
    """

    @classmethod
    def setUpClass(cls):
        cls.readings = charging.load_readings()
        cls.rates = charging.load_rates()
        cls.months = charging.by_month(cls.readings, cls.rates)

    def test_july_2025_excludes_the_may_2024_reading(self):
        # Sheet showed $155.61 -- $36.22 of that was 5/26-5/30/2024.
        self.assertEqual(self.months[(2025, 7)]["cost"], Decimal("119.39"))

    def test_september_2025_excludes_the_july_2024_reading(self):
        # Sheet showed $258.80 -- $150.46 of that was July 2024.
        self.assertEqual(self.months[(2025, 9)]["cost"], Decimal("108.34"))

    def test_august_2025_is_present(self):
        # The sheet had no total-cost formula in this row at all.
        self.assertEqual(self.months[(2025, 8)]["cost"], Decimal("103.05"))

    def test_the_120v_mining_meter_stopped_in_march_2025(self):
        # The rig was shut off; only the wall connector runs after this.
        latest = max(r.end for r in self.readings if r.meter == "meter_120v")
        self.assertEqual(latest, date(2025, 3, 8))

    def test_months_the_sheet_got_right_are_unchanged(self):
        for (year, month), expected in {
            (2024, 5): "502.58", (2024, 6): "479.00", (2024, 7): "532.56",
            (2024, 11): "323.62", (2025, 1): "256.31", (2025, 2): "101.97",
            (2025, 6): "111.08",
        }.items():
            with self.subTest(month=f"{year}-{month:02d}"):
                self.assertEqual(self.months[(year, month)]["cost"], Decimal(expected))

    def test_period_straddling_new_year_belongs_to_the_earlier_year(self):
        readings = [reading("2024-12-02", "2025-01-04", kwh=655.9, line=2)]
        self.assertEqual(charging.select(readings, year=2024), readings)
        self.assertEqual(charging.select(readings, year=2025), [])

    def test_real_data_summary_and_year_filter_agree(self):
        readings = charging.load_readings()
        rates = charging.load_rates()
        for year in (2024, 2025):
            months = charging.by_month(charging.select(readings, year=year), rates)
            self.assertTrue(months)
            self.assertTrue(all(m[0] == year for m in months), f"{year}: {sorted(months)}")


class TestMonthBounds(unittest.TestCase):
    def test_expands_a_month_to_its_first_and_last_day(self):
        self.assertEqual(charging.month_bounds("2025-10"), (date(2025, 10, 1), date(2025, 10, 31)))
        self.assertEqual(charging.month_bounds("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_leap_february(self):
        self.assertEqual(charging.month_bounds("2024-02")[1], date(2024, 2, 29))

    def test_rejects_junk(self):
        for bad in ("2025-13", "2025", "October", "2025-1-1", ""):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                charging.month_bounds(bad)


class TestParseBackfill(unittest.TestCase):
    def test_reads_month_and_kwh(self):
        parsed = charging.parse_backfill(["2025-10 610.4", "2025-11  580.2"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].start, date(2025, 10, 1))
        self.assertEqual(parsed[0].end, date(2025, 10, 31))
        self.assertEqual(parsed[0].kwh, 610.4)
        self.assertEqual(parsed[0].meter, "wall_connector")

    def test_ignores_blanks_and_comments(self):
        lines = ["# Oct-Nov", "", "2025-10 610.4", "   ", "2025-11 580.2  # guessed", "\n"]
        self.assertEqual(len(charging.parse_backfill(lines)), 2)

    def test_keeps_thousands_separators_in_one_field(self):
        # Pasted straight out of a spreadsheet.
        parsed = charging.parse_backfill(["2025-10 1,610.4"])
        self.assertEqual(parsed[0].kwh, 1610.4)

    def test_accepts_commas_as_delimiters(self):
        parsed = charging.parse_backfill(["2025-10,610.4,meter_240v"])
        self.assertEqual(parsed[0].kwh, 610.4)
        self.assertEqual(parsed[0].meter, "meter_240v")

    def test_both_at_once(self):
        parsed = charging.parse_backfill(["2025-10, 1,610.4, meter_240v"])
        self.assertEqual(parsed[0].kwh, 1610.4)
        self.assertEqual(parsed[0].meter, "meter_240v")

    def test_accepts_tab_separated_paste(self):
        parsed = charging.parse_backfill(["2025-10\t610.4"])
        self.assertEqual(parsed[0].kwh, 610.4)

    def test_explicit_meter(self):
        parsed = charging.parse_backfill(["2025-10 610.4 meter_240v"])
        self.assertEqual(parsed[0].meter, "meter_240v")

    def test_default_meter_is_overridable(self):
        parsed = charging.parse_backfill(["2025-10 610.4"], default_meter="meter_120v")
        self.assertEqual(parsed[0].meter, "meter_120v")

    def test_reports_the_offending_line_number(self):
        with self.assertRaises(DataError) as caught:
            charging.parse_backfill(["2025-10 610.4", "", "2025-11 lots"])
        self.assertIn("line 3", str(caught.exception))

    def test_rejects_an_unknown_meter(self):
        with self.assertRaises(DataError):
            charging.parse_backfill(["2025-10 610.4 solar"])

    def test_rejects_a_wrong_field_count(self):
        with self.assertRaises(DataError):
            charging.parse_backfill(["2025-10"])
        with self.assertRaises(DataError):
            charging.parse_backfill(["2025-10 1 2 3"])


class TestResolvePeriod(unittest.TestCase):
    class Args:
        def __init__(self, month=None, start=None, end=None):
            self.month, self.start, self.end = month, start, end

    def test_month(self):
        self.assertEqual(charging.resolve_period(self.Args(month="2025-10")),
                         (date(2025, 10, 1), date(2025, 10, 31)))

    def test_explicit_period(self):
        self.assertEqual(charging.resolve_period(self.Args(start="2025-10-03", end="2025-11-02")),
                         (date(2025, 10, 3), date(2025, 11, 2)))

    def test_month_and_explicit_together_is_an_error(self):
        with self.assertRaises(DataError):
            charging.resolve_period(self.Args(month="2025-10", start="2025-10-01"))

    def test_half_a_period_is_an_error(self):
        with self.assertRaises(DataError):
            charging.resolve_period(self.Args(start="2025-10-01"))

    def test_backwards_period_is_an_error(self):
        with self.assertRaises(DataError):
            charging.resolve_period(self.Args(start="2025-11-01", end="2025-10-01"))


class TestAppendReadings(unittest.TestCase):
    """A batch must be all-or-nothing, so a bad line can't half-update the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "readings.csv"
        self.path.write_text("start,end,meter,kwh,note\n"
                             "2025-01-01,2025-01-31,wall_connector,600,\n")
        self._real = charging.READINGS_CSV
        charging.READINGS_CSV = self.path
        self.addCleanup(setattr, charging, "READINGS_CSV", self._real)
        self.existing = charging.load_readings(self.path)

    def test_a_clean_batch_is_written(self):
        new = charging.parse_backfill(["2025-02 580", "2025-03 590"])
        self.assertEqual(charging.append_readings(new, self.existing, FLAT_RATES), [])
        self.assertEqual(len(charging.load_readings(self.path)), 3)

    def test_one_bad_row_writes_nothing(self):
        new = charging.parse_backfill(["2025-02 580", "2025-01 999"])  # Jan already exists
        introduced = charging.append_readings(new, self.existing, FLAT_RATES)
        self.assertTrue(introduced)
        self.assertEqual(len(charging.load_readings(self.path)), 1)

    def test_force_writes_anyway(self):
        new = charging.parse_backfill(["2025-01 999"])
        self.assertEqual(charging.append_readings(new, self.existing, FLAT_RATES, force=True), [])
        self.assertEqual(len(charging.load_readings(self.path)), 2)

    def test_a_batch_conflicting_with_itself_is_caught(self):
        new = charging.parse_backfill(["2025-02 580", "2025-02 590"])
        self.assertTrue(charging.append_readings(new, self.existing, FLAT_RATES))
        self.assertEqual(len(charging.load_readings(self.path)), 1)
