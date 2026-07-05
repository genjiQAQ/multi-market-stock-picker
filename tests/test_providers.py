from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from stock_picker.models import QualityLog, StockRecord
from stock_picker import providers


class _FakeFrame:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        self.assert_orient(orient)
        return self.rows

    @staticmethod
    def assert_orient(orient: str) -> None:
        if orient != "records":
            raise AssertionError(orient)


class ProviderTests(unittest.TestCase):
    def test_a_share_snapshot_falls_back_to_legacy_akshare(self) -> None:
        fake_ak = types.SimpleNamespace()

        def fail_em():
            raise RuntimeError("remote disconnected")

        def ok_legacy():
            return _FakeFrame(
                [
                    {
                        "代码": "600519",
                        "名称": "贵州茅台",
                        "最新价": 1500,
                        "成交量": 10,
                        "成交额": 100000000,
                    }
                ]
            )

        fake_ak.stock_zh_a_spot_em = fail_em
        fake_ak.stock_zh_a_spot = ok_legacy
        quality = QualityLog()
        config = {"min_turnover_by_market": {"a-share": 1}, "min_price_by_market": {"a-share": 1}}
        with patch.dict("sys.modules", {"akshare": fake_ak}):
            records = providers.fetch_market_snapshot("a-share", config, quality, no_cache=True)
        self.assertEqual(records[0].yahoo_symbol, "600519.SS")
        self.assertEqual(records[0].volume, 10)
        self.assertEqual(quality.sources["a-share"], "AKShare:stock_zh_a_spot")
        self.assertTrue(any("stock_zh_a_spot_em" in warning for warning in quality.warnings))

    def test_eastmoney_a_share_history_parser(self) -> None:
        payload = {
            "data": {
                "klines": [
                    "2026-01-02,10.0,10.5,10.8,9.9,100000,1050000,1.0,5.0,0.5,1.2",
                    "2026-01-05,10.5,11.0,11.2,10.4,120000,1300000,1.0,4.76,0.5,1.3",
                ]
            }
        }
        record = StockRecord("600519", "600519.SS", "a-share")
        with patch.object(providers, "_http_json", return_value=payload):
            rows = providers.fetch_eastmoney_a_share_history(record, {"history_period": "9mo"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-01-02")
        self.assertEqual(rows[1]["close"], 11.0)

    def test_a_share_fundamentals_use_snapshot_fields(self) -> None:
        record = StockRecord("600519", "600519.SS", "a-share", market_cap=1_000_000, pe=20, pb=3)
        payload = providers.fetch_fundamentals(record, {}, QualityLog(), no_cache=True)
        self.assertEqual(payload["marketCap"], 1_000_000)
        self.assertEqual(payload["trailingPE"], 20)
        self.assertEqual(payload["priceToBook"], 3)

    def test_nasdaq_trader_directory_parser_skips_etfs_and_test_issues(self) -> None:
        listed_text = "\n".join(
            [
                "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares",
                "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
                "AAA|Example ETF|G|N|N|100|Y|N",
                "TEST|Test Company|G|Y|N|100|N|N",
                "File Creation Time: 0705202600:00|||||||",
            ]
        )
        other_text = "\n".join(
            [
                "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
                "MSFT|Microsoft Corporation Common Stock|N|MSFT|N|100|N|MSFT",
                "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY",
                "File Creation Time: 0705202600:00|||||||",
            ]
        )

        def fake_text(url: str, config: dict) -> str:
            if url == providers.NASDAQ_LISTED_URL:
                return listed_text
            if url == providers.NASDAQ_OTHER_LISTED_URL:
                return other_text
            raise AssertionError(url)

        with patch.object(providers, "_http_text", side_effect=fake_text):
            rows = providers.fetch_nasdaq_trader_us_directory({})

        self.assertEqual([row["symbol"] for row in rows], ["AAPL", "MSFT"])
        self.assertEqual(rows[0]["name"], "Apple Inc. - Common Stock")

    def test_ssl_context_uses_certifi_when_available(self) -> None:
        fake_certifi = types.SimpleNamespace(where=lambda: "/tmp/cacert.pem")
        with patch.dict("sys.modules", {"certifi": fake_certifi}):
            with patch.object(providers.ssl, "create_default_context", return_value="context") as create_context:
                context = providers._ssl_context()
        self.assertEqual(context, "context")
        create_context.assert_called_once_with(cafile="/tmp/cacert.pem")

    def test_http_json_prefers_requests(self) -> None:
        response = types.SimpleNamespace(
            text='{"ok": true}',
            raise_for_status=lambda: None,
            json=lambda: {"ok": True},
        )
        fake_requests = types.SimpleNamespace(get=lambda *args, **kwargs: response)
        with patch.dict("sys.modules", {"requests": fake_requests}):
            payload = providers._http_json("https://example.test", {"a": 1}, retries=1)
        self.assertEqual(payload, {"ok": True})


if __name__ == "__main__":
    unittest.main()
