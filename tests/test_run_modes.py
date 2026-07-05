from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_picker import providers
from stock_picker.cli import (
    EXIT_SUCCESS,
    apply_defaults,
    parse_args,
    rough_screen,
    run,
    select_records_for_enrichment,
    trim_to_enrichment_scope,
    validate_args,
)
from stock_picker.models import QualityLog, StockRecord
from stock_picker.normalization import normalize_snapshot_row
from stock_picker.scoring import score_records


def _record(symbol: str, market: str = "us", turnover: float = 1_000_000, price: float | None = 10) -> StockRecord:
    return StockRecord(
        raw_symbol=symbol,
        yahoo_symbol=symbol,
        market=market,
        name=symbol,
        price=price,
        volume=100_000,
        turnover=turnover,
        market_cap=1_000_000_000,
        source="fixture",
        is_tradeable=True,
    )


class RunModeTests(unittest.TestCase):
    def test_run_mode_default_and_validation(self) -> None:
        args = parse_args([])
        args = apply_defaults(
            args,
            {
                "default_market": "us",
                "default_universe": "preset",
                "default_style": "balanced",
                "default_run_mode": "fast",
                "default_top_n": 10,
                "max_candidates": 200,
            },
        )
        self.assertEqual(args.run_mode, "fast")
        args.run_mode = "invalid"
        with self.assertRaises(ValueError):
            validate_args(args)

    def test_snapshot_only_run_does_not_call_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            args = parse_args(
                [
                    "--market",
                    "us",
                    "--universe",
                    "custom",
                    "--symbols",
                    "AAPL",
                    "--run-mode",
                    "snapshot-only",
                    "--out-dir",
                    tmpdir,
                ]
            )
            config = {
                "output_root": "desktop",
                "default_style": "balanced",
                "default_top_n": 10,
                "max_candidates": 200,
                "default_run_mode": "fast",
            }
            args = apply_defaults(args, config)
            with patch("stock_picker.cli.providers.enrich_records") as enrich_records:
                code, paths = run(args, config)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("report", paths)
        enrich_records.assert_not_called()

    def test_fast_and_full_select_expected_enrichment_scope(self) -> None:
        records = rough_screen([_record(f"SYM{i}", turnover=i) for i in range(50)], 40)
        fast_targets = select_records_for_enrichment(records, "fast", {"fast_enrich_candidates": 30}, 40)
        full_targets = select_records_for_enrichment(records, "full", {"fast_enrich_candidates": 30}, 40)
        snapshot_targets = select_records_for_enrichment(records, "snapshot-only", {"fast_enrich_candidates": 30}, 40)

        self.assertEqual(len(fast_targets), 30)
        self.assertEqual(len(full_targets), 40)
        self.assertEqual(snapshot_targets, [])
        self.assertEqual(fast_targets[0].raw_symbol, "SYM49")
        self.assertEqual(full_targets[-1].raw_symbol, "SYM10")
        self.assertEqual(trim_to_enrichment_scope(records, fast_targets, "fast"), fast_targets)

    def test_a_share_filters_limit_up_amplitude_st_bj_and_new_stock(self) -> None:
        config = {
            "min_turnover_by_market": {"a-share": 1},
            "min_price_by_market": {"a-share": 1},
            "exclude_rules": {"a-share": {"max_change_pct": 9.5, "max_amplitude_pct": 12.0}},
        }
        limit_up = normalize_snapshot_row(
            {"代码": "600519", "名称": "贵州茅台", "最新价": 100, "涨跌幅": 9.8, "成交额": 100_000_000},
            "a-share",
            config,
            "fixture",
        )
        amplitude = normalize_snapshot_row(
            {"代码": "600000", "名称": "浦发银行", "最新价": 10, "振幅": 13, "成交额": 100_000_000},
            "a-share",
            config,
            "fixture",
        )
        st = normalize_snapshot_row(
            {"代码": "000001", "名称": "ST平安", "最新价": 10, "成交额": 100_000_000},
            "a-share",
            config,
            "fixture",
        )
        bj = normalize_snapshot_row(
            {"代码": "832000", "名称": "北交测试", "最新价": 10, "成交额": 100_000_000},
            "a-share",
            config,
            "fixture",
        )
        new_stock = normalize_snapshot_row(
            {"代码": "001399", "名称": "N惠科", "最新价": 10, "成交额": 100_000_000},
            "a-share",
            config,
            "fixture",
        )

        self.assertEqual(limit_up.exclude_reason, "a_share_near_limit_up")
        self.assertEqual(amplitude.exclude_reason, "a_share_intraday_amplitude_too_high")
        self.assertEqual(st.exclude_reason, "a_share_st_or_delisted")
        self.assertEqual(bj.exclude_reason, "bj_unsupported")
        self.assertEqual(new_stock.exclude_reason, "new_stock_prefix_c_or_n")

    def test_nasdaq_directory_without_quote_is_demoted_and_low_coverage(self) -> None:
        config = {"min_turnover_by_market": {"us": 1}, "min_price_by_market": {"us": 1}}
        directory = normalize_snapshot_row(
            {"symbol": "ZZZ", "name": "ZZZ Common Stock"},
            "us",
            config,
            "NasdaqTrader:SymbolDirectory",
        )
        priced = _record("AAPL", turnover=1)
        selected = rough_screen([directory, priced], 2)
        self.assertIs(selected[0], priced)
        self.assertTrue(directory.is_tradeable)

        quality = QualityLog(run_mode="full")
        score_records([directory], "balanced", quality)
        self.assertEqual(directory.result_level, "低覆盖率结果")
        self.assertEqual(directory.rating, "需进一步验证")


class ProviderResilienceTests(unittest.TestCase):
    def test_cache_ttl_new_old_and_expired_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(providers, "CACHE_DIR", Path(tmpdir)):
                providers.write_cache("history", "AAPL", [{"close": 1}], source="fixture")
                quality = QualityLog()
                self.assertEqual(providers.read_cache("history", "AAPL", quality, ttl_hours=24), [{"close": 1}])
                self.assertTrue(quality.cache_used["history:AAPL"])

                old_path = providers._cache_path("history", "MSFT")
                old_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.write_text('[{"close": 2}]', encoding="utf-8")
                old_quality = QualityLog()
                self.assertEqual(providers.read_cache("history", "MSFT", old_quality, ttl_hours=24), [{"close": 2}])
                self.assertTrue(any("旧版 history 缓存格式" in warning for warning in old_quality.warnings))

                expired_path = providers._cache_path("history", "NVDA")
                expired_path.write_text(
                    '{"fetched_at": "2020-01-01T00:00:00+00:00", "payload": [{"close": 3}]}',
                    encoding="utf-8",
                )
                expired_quality = QualityLog()
                self.assertIsNone(providers.read_cache("history", "NVDA", expired_quality, ttl_hours=1))
                self.assertEqual(expired_quality.cache_expired[0]["kind"], "history")

    def test_provider_breaker_skips_after_consecutive_failures(self) -> None:
        records = [_record(f"SYM{i}") for i in range(3)]
        quality = QualityLog()
        config = {
            "symbol_timeout_seconds": 0,
            "provider_failure_breaker_threshold": 2,
            "provider_failure_cooldown_seconds": 300,
        }
        with patch.object(providers, "_enrich_record_once", side_effect=RuntimeError("boom")):
            providers.enrich_records(records, config, quality, no_cache=True, run_mode="full")

        self.assertEqual([record.enrichment_status for record in records], ["failed", "failed", "skipped_provider_breaker"])
        self.assertEqual(quality.provider_breakers[0]["provider"], "yfinance")

    def test_symbol_timeout_interrupts_slow_enrichment(self) -> None:
        if not providers._can_use_signal_timeout():
            self.skipTest("signal timeout is unavailable")
        record = _record("AAPL")
        quality = QualityLog()

        def slow_enrich(*_args, **_kwargs):
            time.sleep(2)
            return record

        started = time.monotonic()
        with patch.object(providers, "_enrich_record_once", side_effect=slow_enrich):
            providers.enrich_records(
                [record],
                {
                    "symbol_timeout_seconds": 0.1,
                    "provider_failure_breaker_threshold": 1,
                    "provider_failure_cooldown_seconds": 300,
                },
                quality,
                no_cache=True,
                run_mode="full",
            )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(record.enrichment_status, "timeout")
        self.assertEqual(quality.provider_breakers[0]["provider"], "yfinance")


if __name__ == "__main__":
    unittest.main()
