from __future__ import annotations

import unittest

from stock_picker.normalization import normalize_custom_symbol, normalize_snapshot_row, normalize_symbol


class NormalizationTests(unittest.TestCase):
    def test_a_share_suffixes(self) -> None:
        self.assertEqual(normalize_symbol("600519", "a-share")[:3], ("600519", "600519.SS", "a-share"))
        self.assertEqual(normalize_symbol("300750", "a-share")[:3], ("300750", "300750.SZ", "a-share"))

    def test_a_share_bj_unsupported(self) -> None:
        raw, yahoo, market, reason = normalize_symbol("832000", "a-share")
        self.assertEqual(raw, "832000")
        self.assertEqual(yahoo, "")
        self.assertEqual(market, "a-share")
        self.assertEqual(reason, "bj_unsupported")

    def test_hk_leading_zero(self) -> None:
        self.assertEqual(normalize_symbol("700", "hk")[:3], ("0700", "0700.HK", "hk"))
        self.assertEqual(normalize_symbol("9988.HK", "hk")[:3], ("9988", "9988.HK", "hk"))

    def test_us_prefix_and_class(self) -> None:
        self.assertEqual(normalize_symbol("105.AAPL", "us")[:3], ("AAPL", "AAPL", "us"))
        self.assertEqual(normalize_symbol("BRK.B", "us")[:3], ("BRK-B", "BRK-B", "us"))

    def test_a_share_em_volume_lot_conversion(self) -> None:
        record = normalize_snapshot_row(
            {"代码": "600519", "名称": "贵州茅台", "最新价": 1500, "成交量": 10, "成交额": 100000000},
            "a-share",
            {"min_turnover_by_market": {"a-share": 1}, "min_price_by_market": {"a-share": 1}},
            "AKShare:stock_zh_a_spot_em",
        )
        self.assertEqual(record.volume, 1000)
        self.assertEqual(record.yahoo_symbol, "600519.SS")
        self.assertTrue(record.is_tradeable)

    def test_a_share_legacy_akshare_volume_preserved(self) -> None:
        record = normalize_snapshot_row(
            {"代码": "600519", "名称": "贵州茅台", "最新价": 1500, "成交量": 10, "成交额": 100000000},
            "a-share",
            {"min_turnover_by_market": {"a-share": 1}, "min_price_by_market": {"a-share": 1}},
            "AKShare:stock_zh_a_spot",
        )
        self.assertEqual(record.volume, 10)

    def test_eastmoney_a_share_volume_lot_converted(self) -> None:
        record = normalize_snapshot_row(
            {"代码": "600519", "名称": "贵州茅台", "最新价": 1500, "成交量": 10, "成交额": 100000000},
            "a-share",
            {"min_turnover_by_market": {"a-share": 1}, "min_price_by_market": {"a-share": 1}},
            "Eastmoney:qt_clist",
        )
        self.assertEqual(record.volume, 1000)

    def test_a_share_new_stock_prefix_filtered(self) -> None:
        record = normalize_snapshot_row(
            {"代码": "001399", "名称": "C惠科", "最新价": 46.28, "成交量": 1000, "成交额": 100000000},
            "a-share",
            {"min_turnover_by_market": {"a-share": 1}, "min_price_by_market": {"a-share": 1}},
            "fixture",
        )
        self.assertEqual(record.exclude_reason, "new_stock_prefix_c_or_n")
        self.assertFalse(record.is_tradeable)

    def test_custom_infers_markets(self) -> None:
        self.assertEqual(normalize_custom_symbol("0700.HK").market, "hk")
        self.assertEqual(normalize_custom_symbol("AAPL").market, "us")

    def test_us_spac_name_filtered(self) -> None:
        record = normalize_snapshot_row(
            {"symbol": "AACB", "name": "Artius II Acquisition Corp. - Class A Ordinary Shares"},
            "us",
            {"min_turnover_by_market": {"us": 1}, "min_price_by_market": {"us": 1}},
            "NasdaqTrader:SymbolDirectory",
        )
        self.assertEqual(record.exclude_reason, "us_non_common")
        self.assertFalse(record.is_tradeable)


if __name__ == "__main__":
    unittest.main()
