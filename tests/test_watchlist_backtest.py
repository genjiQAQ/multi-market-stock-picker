from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from stock_picker import providers
from stock_picker.backtest import run_backtest
from stock_picker.models import QualityLog, StockRecord
from stock_picker.watchlist import apply_watchlist


def scored_record(symbol: str, market: str = "us", rank: int = 1, score: float = 80, result_level: str = "快速评分") -> StockRecord:
    record = StockRecord(
        raw_symbol=symbol,
        yahoo_symbol=symbol,
        market=market,
        name=symbol,
        price=100,
        turnover=10_000_000,
        market_cap=1_000_000_000,
        source="fixture",
        is_tradeable=True,
        result_level=result_level,
        rating="重点跟踪" if score >= 80 else "积极观察",
    )
    record.rank_global = rank
    record.rank_in_market = rank
    record.scores = {
        "final_score": score,
        "momentum_score": score,
        "quality_trend_score": score,
        "liquidity_score": score,
        "risk_control_score": score,
        "data_coverage": 90,
        "quality_coverage": 80,
    }
    return record


def history_rows(days: int = 180, start_price: float = 100) -> list[dict]:
    start = date(2025, 1, 1)
    rows = []
    for idx in range(days):
        close = start_price + idx * 0.5
        rows.append(
            {
                "date": (start + timedelta(days=idx)).isoformat(),
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1_000_000 + idx,
            }
        )
    return rows


class WatchlistTests(unittest.TestCase):
    def test_watchlist_first_and_second_run_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "out"
            state_dir = Path(tmpdir) / "state"
            output_dir.mkdir()
            quality = QualityLog()
            first_records = [scored_record("AAPL", rank=1, score=82), scored_record("MSFT", rank=2, score=76)]
            apply_watchlist(
                records=first_records,
                output_dir=output_dir,
                top_n=2,
                watchlist_name="test",
                state_dir=state_dir,
                lookback_runs=5,
                quality=quality,
            )
            self.assertEqual(first_records[0].watch_status, "新进入")
            self.assertEqual(first_records[1].watch_status, "新进入")
            self.assertTrue((output_dir / "watchlist_state.json").exists())
            self.assertTrue((output_dir / "watchlist_changes.csv").exists())

            second_output = Path(tmpdir) / "out2"
            second_output.mkdir()
            second_quality = QualityLog()
            second_records = [scored_record("AAPL", rank=1, score=80), scored_record("MSFT", rank=3, score=62)]
            apply_watchlist(
                records=second_records,
                output_dir=second_output,
                top_n=1,
                watchlist_name="test",
                state_dir=state_dir,
                lookback_runs=5,
                quality=second_quality,
            )
            self.assertEqual(second_records[0].watch_status, "继续跟踪")
            self.assertEqual(second_records[1].watch_status, "降级观察")
            self.assertEqual(second_records[0].previous_rank, 1)
            self.assertEqual(second_records[0].score_change, -2)

    def test_watchlist_market_symbol_key_is_isolated_and_low_coverage_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            records = [
                scored_record("ABC", "us", rank=1, score=82),
                scored_record("ABC", "hk", rank=2, score=81),
            ]
            apply_watchlist(
                records=records,
                output_dir=output_dir,
                top_n=2,
                watchlist_name="markets",
                state_dir=state_dir,
                lookback_runs=5,
                quality=QualityLog(),
            )
            state = json.loads((state_dir / "markets.json").read_text(encoding="utf-8"))
            self.assertIn("us:ABC", state["entries"])
            self.assertIn("hk:ABC", state["entries"])

            state["entries"]["us:ABC"]["watch_runs"] = 3
            state["entries"]["us:ABC"]["last_score"] = 82
            state["entries"]["us:ABC"]["last_rank"] = 1
            (state_dir / "markets.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            low_coverage = [scored_record("ABC", "us", rank=1, score=83, result_level="低覆盖率结果")]
            second_output = Path(tmpdir) / "out2"
            second_output.mkdir()
            apply_watchlist(
                records=low_coverage,
                output_dir=second_output,
                top_n=1,
                watchlist_name="markets",
                state_dir=state_dir,
                lookback_runs=5,
                quality=QualityLog(),
            )
            self.assertEqual(low_coverage[0].watch_status, "降级观察")

    def test_watchlist_missing_candidate_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            state = {
                "schema_version": "1.0",
                "watchlist_name": "remove",
                "run_count": 1,
                "entries": {
                    "us:MSFT": {
                        "market": "us",
                        "raw_symbol": "MSFT",
                        "yahoo_symbol": "MSFT",
                        "name": "MSFT",
                        "last_rank": 2,
                        "last_score": 75,
                        "last_rating": "积极观察",
                        "last_result_level": "快速评分",
                        "watch_runs": 1,
                        "missed_runs": 1,
                    }
                },
            }
            (state_dir / "remove.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            quality = QualityLog()
            apply_watchlist(
                records=[scored_record("AAPL", rank=1, score=82)],
                output_dir=output_dir,
                top_n=1,
                watchlist_name="remove",
                state_dir=state_dir,
                lookback_runs=2,
                quality=quality,
            )
            statuses = {row["yahoo_symbol"]: row["watch_status"] for row in quality.watchlist_changes}
            self.assertEqual(statuses["MSFT"], "移出观察池")

    def test_watchlist_does_not_persist_new_non_top_candidates_and_rank_drop_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            first_output = Path(tmpdir) / "out"
            first_output.mkdir()
            apply_watchlist(
                records=[scored_record("AAPL", rank=1, score=82), scored_record("MSFT", rank=2, score=79)],
                output_dir=first_output,
                top_n=1,
                watchlist_name="top-only",
                state_dir=state_dir,
                lookback_runs=5,
                quality=QualityLog(),
            )
            state = json.loads((state_dir / "top-only.json").read_text(encoding="utf-8"))
            self.assertIn("us:AAPL", state["entries"])
            self.assertNotIn("us:MSFT", state["entries"])

            second_output = Path(tmpdir) / "out2"
            second_output.mkdir()
            second_records = [scored_record("MSFT", rank=1, score=80), scored_record("AAPL", rank=6, score=82)]
            apply_watchlist(
                records=second_records,
                output_dir=second_output,
                top_n=10,
                watchlist_name="top-only",
                state_dir=state_dir,
                lookback_runs=5,
                quality=QualityLog(),
            )
            self.assertEqual(second_records[0].watch_status, "新进入")
            self.assertEqual(second_records[1].watch_status, "降级观察")


class BacktestTests(unittest.TestCase):
    def test_backtest_generates_results_from_cached_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            with patch.object(providers, "CACHE_DIR", cache_dir):
                providers.write_cache("history", "AAPL", history_rows(), source="fixture")
                providers.write_cache("history", "MSFT", history_rows(start_price=80), source="fixture")
                paths = run_backtest(
                    records=[StockRecord("AAPL", "AAPL", "us", name="AAPL"), StockRecord("MSFT", "MSFT", "us", name="MSFT")],
                    output_dir=output_dir,
                    style="balanced",
                    run_mode="fast",
                    top_n=1,
                    start="2025-03-15",
                    end="2025-04-30",
                    window_days=40,
                    hold_days=5,
                    frequency="weekly",
                    config={},
                )
            payload = json.loads((output_dir / "backtest_results.json").read_text(encoding="utf-8"))
            quality = json.loads((output_dir / "backtest_quality.json").read_text(encoding="utf-8"))
            self.assertIn("backtest_report", paths)
            self.assertGreater(payload["summary"]["period_count"], 0)
            self.assertGreater(payload["summary"]["selection_count"], 0)
            self.assertIsNotNone(payload["summary"]["avg_forward_return"])
            self.assertTrue(quality["backtest_mode"])

    def test_backtest_records_missing_history_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            with patch.object(providers, "CACHE_DIR", Path(tmpdir) / "cache"):
                run_backtest(
                    records=[StockRecord("AAPL", "AAPL", "us", name="AAPL")],
                    output_dir=output_dir,
                    style="balanced",
                    run_mode="fast",
                    top_n=1,
                    start=None,
                    end=None,
                    window_days=40,
                    hold_days=5,
                    frequency="weekly",
                    config={},
                )
            quality = json.loads((output_dir / "backtest_quality.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["missing_history"][0]["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main()
