from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_picker.models import QualityLog
from stock_picker.universe import merge_universes, parse_symbols, parse_symbols_file, records_from_presets


class UniverseTests(unittest.TestCase):
    def test_parse_symbols_and_dedupe_merge_append(self) -> None:
        base = parse_symbols("AAPL,MSFT")
        custom = parse_symbols("MSFT,NVDA")
        merged = merge_universes(base, custom, "append")
        self.assertEqual([record.yahoo_symbol for record in merged], ["AAPL", "MSFT", "NVDA"])

    def test_merge_intersect_and_exclude(self) -> None:
        base = parse_symbols("AAPL,MSFT,NVDA")
        custom = parse_symbols("MSFT")
        self.assertEqual([record.yahoo_symbol for record in merge_universes(base, custom, "intersect")], ["MSFT"])
        self.assertEqual([record.yahoo_symbol for record in merge_universes(base, custom, "exclude")], ["AAPL", "NVDA"])

    def test_parse_symbols_file_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "symbols.csv"
            path.write_text("symbol,name,market\n700,Tencent,hk\nBRK.B,Berkshire,us\n", encoding="utf-8")
            records = parse_symbols_file(str(path))
        self.assertEqual(records[0].yahoo_symbol, "0700.HK")
        self.assertEqual(records[1].yahoo_symbol, "BRK-B")

    def test_presets_exist(self) -> None:
        records = records_from_presets("all")
        self.assertTrue(any(record.yahoo_symbol == "AAPL" for record in records))
        self.assertTrue(any(record.yahoo_symbol == "0700.HK" for record in records))


if __name__ == "__main__":
    unittest.main()
