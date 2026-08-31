"""Capture CSV export — the shape downstream pandas/Excel work depends on."""

import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bldc_diagnostic_console.constants import VALUE_COLUMNS
from bldc_diagnostic_console.csv_writer import write_csv, write_raw_log
from bldc_diagnostic_console.data_store import DataStore

T0 = datetime(2026, 8, 31, 12, 0, 0)


def _store(rows: int = 3) -> DataStore:
    store = DataStore()
    for i in range(rows):
        store.add_row(
            {"RAW_PK_A": 0.5 + i, "RPM": 100 * i, "STATE": 2, "FAULT": 0},
            wall=T0 + timedelta(milliseconds=500 * i),
        )
    return store


class CsvWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "out.csv")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _read(self) -> list[list[str]]:
        with open(self.path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def test_header_and_row_count(self) -> None:
        written = write_csv(self.path, _store(3))
        rows = self._read()
        self.assertEqual(written, 3)
        self.assertEqual(len(rows), 4)  # header + 3
        self.assertEqual(rows[0][:2], ["t_iso", "t_elapsed_s"])
        for name in VALUE_COLUMNS:
            self.assertIn(name, rows[0])

    def test_elapsed_and_iso_timestamps(self) -> None:
        write_csv(self.path, _store(3))
        rows = self._read()
        self.assertEqual([r[1] for r in rows[1:]], ["0.000", "0.500", "1.000"])
        self.assertTrue(rows[1][0].startswith("2026-08-31T12:00:00"))

    def test_missing_samples_are_blank_not_nan(self) -> None:
        """A column that appears late is padded; blanks must not read as 0."""
        store = _store(2)
        store.add_row({"RAW_PK_A": 1.0, "LATE_COL": 7.0}, wall=T0)
        write_csv(self.path, store)
        rows = self._read()
        column = rows[0].index("LATE_COL")
        self.assertEqual([rows[1][column], rows[2][column]], ["", ""])
        self.assertEqual(rows[3][column], "7")

    def test_raw_log_is_verbatim(self) -> None:
        path = str(Path(self.tmp.name) / "raw.log")
        lines = ["CUR,1,2", "garbage ~~", "CUR,3,4"]
        self.assertEqual(write_raw_log(path, lines), 3)
        self.assertEqual(Path(path).read_text().splitlines(), lines)


class DelayLabelTest(unittest.TestCase):
    """The table header is abbreviated; the recorded definition is not."""

    def test_the_csv_column_keeps_the_full_name(self) -> None:
        from bldc_diagnostic_console.verification.trial import (
            DELAY_COLUMN,
            TRIAL_CSV_COLUMNS,
        )

        self.assertEqual(DELAY_COLUMN, "PC-observed Fault Transition Delay [ms]")
        self.assertIn(DELAY_COLUMN, TRIAL_CSV_COLUMNS)

    def test_the_table_header_is_the_short_form(self) -> None:
        from bldc_diagnostic_console.ui.verification_panel import TABLE_COLUMNS

        self.assertEqual(TABLE_COLUMNS[2], "PC-observed COMM Delay [ms]")

    def test_neither_form_calls_it_a_pwm_off_latency(self) -> None:
        """It spans the SSH round trip and a telemetry frame; it is not that."""
        from bldc_diagnostic_console.verification.trial import (
            DELAY_LABEL,
            DELAY_SHORT_LABEL,
        )

        for label in (DELAY_LABEL, DELAY_SHORT_LABEL):
            self.assertIn("PC-observed", label)
            self.assertNotIn("latency", label.lower())


class DataStoreTest(unittest.TestCase):
    def test_fault_edges_only(self) -> None:
        store = DataStore()
        for fault in (0, 0, 3, 3, 0, 1):
            store.add_row({"FAULT": fault}, wall=T0)
        self.assertEqual([code for _t, code in store.fault_events], [3, 1])

    def test_elapsed_at_maps_wall_clock_to_axis(self) -> None:
        store = DataStore()
        self.assertIsNone(store.elapsed_at(T0))  # no origin before the first row
        store.add_row({"RPM": 0}, wall=T0)
        self.assertAlmostEqual(store.elapsed_at(T0 + timedelta(seconds=2.5)), 2.5)


if __name__ == "__main__":
    unittest.main()
