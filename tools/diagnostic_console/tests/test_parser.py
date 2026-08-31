"""Parser behaviour on the fixed positional CUR contract."""

import unittest

from bldc_diagnostic_console.constants import DEFAULT_COLUMNS
from bldc_diagnostic_console.parser import TelemetryParser

ROW = "CUR,0.780,0.490,0.150,0,9,23.98,380,2,0"


class ParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = TelemetryParser()

    def test_data_row_without_header(self) -> None:
        """Attaching mid-stream must parse from the compiled-in column order."""
        result = self.parser.parse(ROW)
        self.assertEqual(result.kind, "data")
        self.assertEqual(result.values["RAW_PK_A"], 0.780)
        self.assertEqual(result.values["VDC"], 23.98)
        self.assertEqual(result.values["RPM"], 380)
        self.assertEqual(result.values["STATE"], 2)
        self.assertEqual(result.values["FAULT"], 0)
        self.assertNotIn("TYPE", result.values)

    def test_header_refreshes_column_order(self) -> None:
        header = ",".join(DEFAULT_COLUMNS)
        self.assertEqual(self.parser.parse(header).kind, "header")
        self.assertTrue(self.parser.header_seen)
        self.assertEqual(self.parser.parse(ROW).kind, "data")

    def test_reordered_header_is_honoured(self) -> None:
        self.parser.parse("TYPE,RPM,DUTY")
        result = self.parser.parse("CUR,123,9")
        self.assertEqual(result.values, {"RPM": 123.0, "DUTY": 9.0})

    def test_wrong_field_count_is_invalid(self) -> None:
        result = self.parser.parse("CUR,1,2,3")
        self.assertEqual(result.kind, "invalid")
        self.assertIn("expected", result.reason)

    def test_non_numeric_field_is_invalid(self) -> None:
        result = self.parser.parse("CUR,0.780,0.490,0.150,0,9,oops,380,2,0")
        self.assertEqual(result.kind, "invalid")
        self.assertIn("VDC", result.reason)

    def test_banners_and_blanks_are_other(self) -> None:
        for line in ("", "   ", "BLDC firmware v1.2 booting", "WARN: brownout"):
            self.assertEqual(self.parser.parse(line).kind, "other")

    def test_reset_restores_default_columns(self) -> None:
        self.parser.parse("TYPE,RPM,DUTY")
        self.parser.reset()
        self.assertEqual(self.parser.columns, list(DEFAULT_COLUMNS))
        self.assertFalse(self.parser.header_seen)


if __name__ == "__main__":
    unittest.main()
