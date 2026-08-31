"""Campaign statistics, I-MR limits, and the trial CSV."""

import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bldc_diagnostic_console.verification.stats import (
    D4_N2,
    E2_N2,
    imr,
    moving_ranges,
    summarize,
)
from bldc_diagnostic_console.verification.trial import (
    TRIAL_CSV_COLUMNS,
    TrialLog,
    TrialRecord,
    write_trials_csv,
)

T0 = datetime(2026, 8, 31, 14, 0, 0)


def _trial(index, delay_ms, passed=True, auto_run=False, fault="COMM"):
    observed = None if delay_ms is None else T0 + timedelta(milliseconds=delay_ms)
    return TrialRecord(
        index=index, test_type="supervisor SIGKILL", injection_time=T0,
        fault_observed_time=observed, delay_ms=delay_ms,
        final_state="FAULT", final_fault=fault,
        auto_run_detected=auto_run, passed=passed,
    )


class SummaryTest(unittest.TestCase):
    def test_empty(self) -> None:
        s = summarize([])
        self.assertEqual(s.n, 0)
        self.assertIsNone(s.mean)

    def test_single_trial_has_no_spread(self) -> None:
        s = summarize([500.0])
        self.assertEqual((s.n, s.mean, s.median, s.minimum, s.maximum), (1, 500.0, 500.0, 500.0, 500.0))
        self.assertIsNone(s.stdev)  # blank, not a misleading 0

    def test_known_values(self) -> None:
        s = summarize([float(v) for v in range(1, 11)])
        self.assertEqual(s.n, 10)
        self.assertAlmostEqual(s.mean, 5.5)
        self.assertAlmostEqual(s.median, 5.5)
        self.assertAlmostEqual(s.stdev, 3.0276503541, places=6)  # n-1 form
        self.assertAlmostEqual(s.p95, 9.55)  # linear interpolation


class ImrTest(unittest.TestCase):
    #  values 10 12 11 13 12 30  ->  MR 2 1 2 1 18, MRbar 4.8, xbar 14.6667
    VALUES = [10.0, 12.0, 11.0, 13.0, 12.0, 30.0]

    def test_needs_two_points(self) -> None:
        self.assertIsNone(imr([]))
        self.assertIsNone(imr([500.0]))

    def test_moving_ranges(self) -> None:
        self.assertEqual(moving_ranges(self.VALUES), [2.0, 1.0, 2.0, 1.0, 18.0])

    def test_limits_come_from_mean_moving_range(self) -> None:
        """Not mean +/- 3s: the estimator is MRbar/d2, i.e. the E2 factor."""
        result = imr(self.VALUES)
        i = result.individuals
        self.assertAlmostEqual(i.center, 88 / 6)
        self.assertAlmostEqual(i.ucl, 88 / 6 + E2_N2 * 4.8, places=9)
        self.assertAlmostEqual(i.lcl, 88 / 6 - E2_N2 * 4.8, places=9)
        # A plain 3-sigma chart would have used s = 7.4 and put UCL near 36,
        # missing the outlier entirely.
        self.assertLess(i.ucl, 30.0)

    def test_moving_range_chart_limits(self) -> None:
        mr = imr(self.VALUES).moving_range
        self.assertAlmostEqual(mr.center, 4.8)
        self.assertAlmostEqual(mr.ucl, D4_N2 * 4.8, places=9)
        self.assertEqual(mr.lcl, 0.0)  # no lower limit at subgroup size 2

    def test_out_of_control_points_flagged(self) -> None:
        result = imr(self.VALUES)
        self.assertEqual(result.individuals.out_of_control, [5])   # the 30
        self.assertEqual(result.moving_range.out_of_control, [4])  # the jump of 18

    def test_stable_campaign_flags_nothing(self) -> None:
        result = imr([520.0, 535.0, 528.0, 541.0, 519.0, 533.0])
        self.assertEqual(result.individuals.out_of_control, [])
        self.assertEqual(result.moving_range.out_of_control, [])


class TrialLogTest(unittest.TestCase):
    def test_delays_exclude_undetected_trials(self) -> None:
        log = TrialLog()
        log.add(_trial(1, 520.0))
        log.add(_trial(2, None, passed=False, fault="NONE"))
        log.add(_trial(3, 540.0))
        self.assertEqual(log.delays(), [520.0, 540.0])
        self.assertAlmostEqual(log.pass_rate(), 2 / 3)
        self.assertEqual(log.next_index(), 4)

    def test_pass_rate_of_empty_log(self) -> None:
        self.assertIsNone(TrialLog().pass_rate())

    def test_auto_run_trial_is_a_failure(self) -> None:
        trial = _trial(1, 480.0, passed=False, auto_run=True)
        self.assertEqual(trial.result, "FAIL")
        self.assertIn("YES", trial.as_row())

    def test_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trials.csv")
            written = write_trials_csv(path, [_trial(1, 520.4), _trial(2, None, passed=False)])
            self.assertEqual(written, 2)
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], TRIAL_CSV_COLUMNS)
        # The delay column names itself, so an exported campaign cannot be
        # mistaken for a PWM-off latency measurement.
        self.assertIn("PC-observed Fault Transition Delay [ms]", rows[0])
        self.assertEqual(rows[1][4], "520.4")
        self.assertEqual(rows[2][4], "")       # no delay when never observed
        self.assertEqual(rows[1][8], "PASS")
        self.assertEqual(rows[2][8], "FAIL")


if __name__ == "__main__":
    unittest.main()
