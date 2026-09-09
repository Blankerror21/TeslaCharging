"""Tests for charging.py. Run with: python3 -m unittest discover tests"""

import csv
import sys
import tempfile
from collections import defaultdict
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
        self.assertTrue(any("kWh/day, high against" in w for w in warnings))

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


class TestNoCrossContamination(unittest.TestCase):
    """The invariant the original spreadsheet broke.

    Its total columns used =E<n-14>+E<n>. Once the upstairs block ran out of
    rows, that offset reached back into the wall-connector block itself, so a
    2025 month was billed partly for 2024 usage. Pinning the corrected dollar
    figures would only be a snapshot -- they legitimately move when a monitor
    export supersedes a hand-typed reading. The property worth holding forever
    is that a month's cost depends on nothing but its own readings.
    """

    @classmethod
    def setUpClass(cls):
        cls.readings = charging.load_readings()
        cls.rates = charging.load_rates()
        cls.months = charging.by_month(cls.readings, cls.rates)

    def test_each_month_costs_exactly_its_own_readings(self):
        owned = defaultdict(list)
        for r in self.readings:
            owned[billing_month(r)].append(r)
        for key, bucket in self.months.items():
            expected = sum((r.cost(self.rates) for r in owned[key]), Decimal("0"))
            with self.subTest(month=f"{key[0]}-{key[1]:02d}"):
                self.assertEqual(bucket["cost"], expected)
                self.assertAlmostEqual(bucket["kwh"], sum(r.kwh for r in owned[key]), places=6)

    def test_dropping_one_reading_moves_only_its_own_month(self):
        victim = next(r for r in self.readings
                      if r.meter == "wall_connector" and billing_month(r) == (2025, 7))
        without = charging.by_month([r for r in self.readings if r is not victim], self.rates)
        for key, bucket in self.months.items():
            if key == (2025, 7):
                continue
            with self.subTest(month=f"{key[0]}-{key[1]:02d}"):
                self.assertEqual(without[key]["cost"], bucket["cost"])
        # July 2025 holds only this reading, so the month goes away entirely.
        self.assertNotIn((2025, 7), without)

    def test_every_reading_lands_in_exactly_one_year(self):
        counted = sum(len(charging.select(self.readings, year=y)) for y in (2024, 2025, 2026))
        self.assertEqual(counted, len(self.readings))

    def test_the_120v_mining_meter_stopped_in_march_2025(self):
        # The rig was shut off; only the wall connector runs after this.
        latest = max(r.end for r in self.readings if r.meter == "meter_120v")
        self.assertEqual(latest, date(2025, 3, 8))


class TestYearFilterMatchesGrouping(unittest.TestCase):
    """--year must agree with the month a period is billed to."""

    def test_period_straddling_new_year_belongs_to_the_earlier_year(self):
        readings = [reading("2024-12-02", "2025-01-04", kwh=655.9, line=2)]
        self.assertEqual(charging.select(readings, year=2024), readings)
        self.assertEqual(charging.select(readings, year=2025), [])

    def test_real_data_summary_and_year_filter_agree(self):
        readings = charging.load_readings()
        rates = charging.load_rates()
        for year in (2024, 2025, 2026):
            months = charging.by_month(charging.select(readings, year=year), rates)
            self.assertTrue(months, f"no months for {year}")
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


class TestLowOutlier(unittest.TestCase):
    """A partial month recorded as a whole one reads as an unusually low month."""

    def _months(self, *kwh):
        import calendar as _cal
        return [reading(f"2025-{i:02d}-01",
                        f"2025-{i:02d}-{_cal.monthrange(2025, i)[1]}", kwh=v, line=i)
                for i, v in enumerate(kwh, start=1)]

    def test_flags_a_partial_month_recorded_as_whole(self):
        _, warnings = validate(self._months(600, 610, 590, 620, 605, 60), FLAT_RATES)
        self.assertTrue(any("low against a typical" in w for w in warnings))

    def test_still_flags_a_high_outlier(self):
        _, warnings = validate(self._months(600, 610, 590, 620, 605, 6000), FLAT_RATES)
        self.assertTrue(any("high against a typical" in w for w in warnings))

    def test_ordinary_variation_is_not_flagged(self):
        _, warnings = validate(self._months(600, 610, 590, 620, 605, 470), FLAT_RATES)
        self.assertEqual(warnings, [])


class TestParseExport(unittest.TestCase):
    def test_kwh_export(self):
        parsed = charging.parse_export([
            "Date time,Vehicle (kWh)",
            "2025-10-01T00:00:00-07:00,470.4",
            "2025-11-01T00:00:00-08:00,517.0",
        ])
        self.assertEqual(parsed, {(2025, 10): 470.4, (2025, 11): 517.0})

    def test_mwh_is_scaled_to_kwh(self):
        parsed = charging.parse_export([
            "Date time,Vehicle (MWh)",
            "2025-10-01T00:00:00-07:00,5.2",
        ])
        self.assertEqual(parsed, {(2025, 10): 5200.0})

    def test_rejects_a_yearly_export(self):
        # Yearly rows are also dated the 1st, but of January only -- the give
        # away is that they can't fill a monthly tracker. A daily export is
        # what the day check really catches.
        with self.assertRaises(DataError) as caught:
            charging.parse_export([
                "Date time,Vehicle (kWh)",
                "2025-01-15T00:00:00-08:00,470.4",
            ])
        self.assertIn("isn't the first of a month", str(caught.exception))

    def test_rejects_an_unknown_unit(self):
        with self.assertRaises(DataError):
            charging.parse_export(["Date time,Vehicle (therms)", "2025-10-01,470.4"])

    def test_rejects_empty_and_headerless(self):
        with self.assertRaises(DataError):
            charging.parse_export([])
        with self.assertRaises(DataError):
            charging.parse_export(["Date time,Vehicle (kWh)"])

    def test_blank_rows_are_skipped(self):
        parsed = charging.parse_export([
            "Date time,Vehicle (kWh)", "2025-10-01,470.4", ",", "",
        ])
        self.assertEqual(len(parsed), 1)


class TestPlanSync(unittest.TestCase):
    TODAY = date(2026, 9, 9)

    def test_adds_months_the_tracker_lacks(self):
        adds, updates, unchanged, skipped, _ = charging.plan_sync(
            {(2026, 1): 289.4}, [], "wall_connector", today=self.TODAY)
        self.assertEqual(len(adds), 1)
        self.assertEqual((adds[0].start, adds[0].end), (date(2026, 1, 1), date(2026, 1, 31)))
        self.assertEqual((updates, unchanged, skipped), ([], [], []))

    def test_skips_a_month_still_in_progress(self):
        adds, _, _, skipped, _ = charging.plan_sync(
            {(2026, 9): 157.7}, [], "wall_connector", today=self.TODAY)
        self.assertEqual(adds, [])
        self.assertEqual(skipped, [((2026, 9), 157.7)])

    def test_matching_month_is_left_alone(self):
        existing = [reading("2026-01-01", "2026-01-31", kwh=289.4, line=2)]
        adds, updates, unchanged, _, _ = charging.plan_sync(
            {(2026, 1): 289.4}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual((adds, updates), ([], []))
        self.assertEqual(len(unchanged), 1)

    def test_differing_value_becomes_an_update(self):
        existing = [reading("2025-08-01", "2025-08-31", kwh=572.5, line=2)]
        _, updates, _, _, _ = charging.plan_sync(
            {(2025, 8): 583.9}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual(len(updates), 1)
        was, now = updates[0]
        self.assertEqual((was.kwh, now.kwh), (572.5, 583.9))

    def test_loose_dates_are_normalised_even_when_the_value_matches(self):
        # The sheet's "1/4/25 - 2/4/25" held January's figure exactly.
        existing = [reading("2025-01-04", "2025-02-04", kwh=648.2, line=2)]
        _, updates, unchanged, _, _ = charging.plan_sync(
            {(2025, 1): 648.2}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual(unchanged, [])
        was, now = updates[0]
        self.assertEqual((now.start, now.end), (date(2025, 1, 1), date(2025, 1, 31)))
        self.assertEqual(now.kwh, was.kwh)

    def test_other_meters_are_untouched(self):
        existing = [reading("2025-08-01", "2025-08-31", meter="meter_240v", kwh=1.0, line=2)]
        adds, updates, _, _, _ = charging.plan_sync(
            {(2025, 8): 583.9}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual(len(adds), 1)
        self.assertEqual(updates, [])

    def test_ambiguous_month_is_refused(self):
        existing = [
            reading("2025-08-01", "2025-08-15", kwh=300.0, line=2),
            reading("2025-08-16", "2025-08-31", kwh=283.9, line=3),
        ]
        with self.assertRaises(DataError):
            charging.plan_sync({(2025, 8): 583.9}, existing, "wall_connector",
                               today=self.TODAY)


class TestSyncedData(unittest.TestCase):
    """The tracker now reflects the monitor exports."""

    @classmethod
    def setUpClass(cls):
        cls.readings = charging.load_readings()
        cls.rates = charging.load_rates()
        cls.months = charging.by_month(cls.readings, cls.rates)

    def test_no_errors(self):
        errors, _ = validate(self.readings, self.rates)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_wall_connector_months_are_whole_calendar_months(self):
        import calendar as _cal
        for r in (r for r in self.readings if r.meter == "wall_connector"):
            if r.start < date(2024, 6, 1):
                continue  # the connector was installed partway through May 2024
            with self.subTest(period=f"{r.start}..{r.end}"):
                self.assertEqual(r.start.day, 1)
                self.assertEqual(r.end.day, _cal.monthrange(r.end.year, r.end.month)[1])
                self.assertEqual((r.start.year, r.start.month), (r.end.year, r.end.month))

    def test_september_2026_is_not_recorded_while_in_progress(self):
        self.assertNotIn((2026, 9), self.months)

    def test_coverage_runs_unbroken_to_august_2026(self):
        ordered = sorted(self.months)
        self.assertEqual(ordered[0], (2024, 4))
        self.assertEqual(ordered[-1], (2026, 8))
        for earlier, later in zip(ordered, ordered[1:]):
            expected = (earlier[0] + 1, 1) if earlier[1] == 12 else (earlier[0], earlier[1] + 1)
            self.assertEqual(later, expected, f"gap after {earlier}")


class TestEstimatedReadings(unittest.TestCase):
    TODAY = date(2026, 9, 9)

    def test_an_estimate_is_held_against_the_export_that_missed_it(self):
        existing = [Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector",
                            635.0, estimated=True, line=2)]
        adds, updates, _, _, held = charging.plan_sync(
            {(2026, 1): 289.4}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual((adds, updates), ([], []))
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][1], 289.4)

    def test_replace_estimates_takes_the_export(self):
        existing = [Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector",
                            635.0, estimated=True, line=2)]
        _, updates, _, _, held = charging.plan_sync(
            {(2026, 1): 289.4}, existing, "wall_connector", today=self.TODAY,
            replace_estimates=True)
        self.assertEqual(held, [])
        self.assertEqual(len(updates), 1)
        self.assertFalse(updates[0][1].estimated, "a synced figure is metered, not estimated")

    def test_a_backfilled_export_still_needs_the_flag(self):
        # Even a higher figure is held -- taking it is an explicit choice.
        existing = [Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector",
                            635.0, estimated=True, line=2)]
        _, updates, _, _, held = charging.plan_sync(
            {(2026, 1): 648.0}, existing, "wall_connector", today=self.TODAY)
        self.assertEqual(updates, [])
        self.assertEqual(len(held), 1)

    def test_check_keeps_estimates_visible(self):
        estimate = Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector",
                           635.0, estimated=True, line=2)
        _, warnings = validate([estimate], FLAT_RATES)
        self.assertTrue(any("estimated, not metered" in w for w in warnings))

    def test_estimated_survives_a_write_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readings.csv"
            charging.write_readings([
                Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector", 635.0,
                        note="outage", estimated=True),
                Reading(date(2026, 2, 1), date(2026, 2, 28), "wall_connector", 607.9),
            ], path)
            loaded = charging.load_readings(path)
        self.assertTrue(loaded[0].estimated)
        self.assertEqual(loaded[0].note, "outage")
        self.assertFalse(loaded[1].estimated)

    def test_old_files_without_the_column_still_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readings.csv"
            path.write_text("start,end,meter,kwh,note\n"
                            "2026-01-01,2026-01-31,wall_connector,635,\n")
            loaded = charging.load_readings(path)
        self.assertFalse(loaded[0].estimated)


class TestReplace(unittest.TestCase):
    """Revising a month in place, for when a figure turns out to be wrong."""

    class Args:
        def __init__(self, **kw):
            defaults = dict(month=None, start=None, end=None, meter="wall_connector",
                            kwh=0.0, note="", estimated=False, replace=False, force=False)
            defaults.update(kw)
            for key, value in defaults.items():
                setattr(self, key, value)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "readings.csv"
        charging.write_readings([
            Reading(date(2026, 1, 1), date(2026, 1, 31), "wall_connector", 289.4),
            Reading(date(2026, 2, 1), date(2026, 2, 28), "wall_connector", 607.9),
        ], self.path)
        real = charging.READINGS_CSV
        charging.READINGS_CSV = self.path
        self.addCleanup(setattr, charging, "READINGS_CSV", real)
        self.readings = charging.load_readings(self.path)

    def test_replaces_in_place_without_duplicating(self):
        code = charging.cmd_add(
            self.Args(month="2026-01", kwh=635.3, estimated=True, replace=True),
            self.readings, FLAT_RATES)
        self.assertEqual(code, 0)
        after = charging.load_readings(self.path)
        self.assertEqual(len(after), 2)
        january = next(r for r in after if billing_month(r) == (2026, 1))
        self.assertAlmostEqual(january.kwh, 635.3)
        self.assertTrue(january.estimated)

    def test_leaves_other_months_alone(self):
        charging.cmd_add(self.Args(month="2026-01", kwh=635.3, replace=True),
                         self.readings, FLAT_RATES)
        after = charging.load_readings(self.path)
        february = next(r for r in after if billing_month(r) == (2026, 2))
        self.assertAlmostEqual(february.kwh, 607.9)
        self.assertFalse(february.estimated)

    def test_replacing_a_month_with_no_reading_is_refused(self):
        code = charging.cmd_add(self.Args(month="2026-05", kwh=600.0, replace=True),
                                self.readings, FLAT_RATES)
        self.assertEqual(code, 2)
        self.assertEqual(len(charging.load_readings(self.path)), 2)

    def test_without_replace_a_duplicate_month_is_refused(self):
        code = charging.cmd_add(self.Args(month="2026-01", kwh=635.3),
                                self.readings, FLAT_RATES)
        self.assertEqual(code, 1)
        self.assertEqual(len(charging.load_readings(self.path)), 2)
