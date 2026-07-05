from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_picker.cli import EXIT_BAD_INPUT, EXIT_NO_CANDIDATES, apply_defaults, parse_args, run, seed_snapshot_history, validate_args
from stock_picker.charts import generate_charts
from stock_picker.indicators import compute_indicators
from stock_picker.models import QualityLog, StockRecord
from stock_picker.narrative import apply_ai_narratives
from stock_picker.reporting import CSV_COLUMNS, write_outputs
from stock_picker.scoring import STYLE_WEIGHTS, score_records


def sample_history(start: float, end: float, days: int = 130) -> list[dict]:
    rows = []
    for idx in range(days):
        close = start + (end - start) * idx / (days - 1)
        rows.append(
            {
                "date": f"2026-01-{idx + 1:02d}",
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000000 + idx * 1000,
            }
        )
    return rows


def sample_record(symbol: str, market: str, price: float, turnover: float, market_cap: float) -> StockRecord:
    record = StockRecord(
        raw_symbol=symbol,
        yahoo_symbol=symbol,
        market=market,
        name=symbol,
        price=price,
        change_pct=2,
        volume=1000000,
        turnover=turnover,
        market_cap=market_cap,
        pe=20,
        pb=3,
        source="fixture",
        is_tradeable=True,
    )
    record.history = sample_history(price * 0.85, price)
    record.fundamentals = {
        "profitMargins": 0.2,
        "returnOnEquity": 0.18,
        "revenueGrowth": 0.15,
        "earningsGrowth": 0.2,
        "operatingCashflow": 100000,
        "totalDebt": 50000,
    }
    return record


class ScoringReportingTests(unittest.TestCase):
    def test_compute_indicators(self) -> None:
        record = sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000)
        indicators = compute_indicators(record)
        self.assertIn("return_20d", indicators)
        self.assertIsNotNone(indicators["rsi_14"])

    def test_style_weights_are_fixed(self) -> None:
        self.assertEqual(STYLE_WEIGHTS["balanced"]["momentum_score"], 0.40)
        self.assertEqual(STYLE_WEIGHTS["momentum"]["momentum_score"], 0.60)
        self.assertEqual(STYLE_WEIGHTS["quality-trend"]["quality_trend_score"], 0.50)

    def test_score_records_and_cross_market_ranks(self) -> None:
        records = [
            sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000),
            sample_record("MSFT", "us", 200, 20_000_000, 2_000_000_000),
            sample_record("0700.HK", "hk", 400, 15_000_000, 800_000_000),
        ]
        quality = QualityLog()
        score_records(records, "balanced", quality)
        self.assertTrue(all("final_score" in record.scores for record in records))
        self.assertTrue(all(record.rank_in_market is not None for record in records))
        self.assertEqual(sorted(record.rank_global for record in records), [1, 2, 3])

    def test_low_quality_coverage_caps_final_score(self) -> None:
        record = sample_record("603629", "a-share", 100, 10_000_000, 1_000_000_000)
        record.market_cap = None
        record.pe = None
        record.pb = None
        record.fundamentals = {}
        quality = QualityLog()
        score_records([record], "balanced", quality)
        self.assertLessEqual(record.scores["final_score"], 69.99)
        self.assertLess(record.scores["quality_coverage"], 35)
        self.assertNotIn(record.rating, {"重点跟踪", "积极观察"})

    def test_low_coverage_narratives_use_stock_specific_snapshot_fields(self) -> None:
        records = [
            StockRecord(
                raw_symbol="300475",
                yahoo_symbol="300475.SZ",
                market="a-share",
                name="香农芯创",
                price=264.25,
                change_pct=0.843,
                volume=1000000,
                turnover=8_547_856_783,
                currency="CNY",
                source="fixture",
            ),
            StockRecord(
                raw_symbol="002916",
                yahoo_symbol="002916.SZ",
                market="a-share",
                name="深南电路",
                price=454.48,
                change_pct=7.417,
                volume=900000,
                turnover=7_950_826_255,
                currency="CNY",
                source="fixture",
            ),
        ]
        quality = QualityLog()
        score_records(records, "balanced", quality)
        self.assertNotEqual(records[0].reason, records[1].reason)
        self.assertIn("85.48亿元", records[0].reason)
        self.assertIn("+7.42%", records[1].reason)
        self.assertIn("待K线接口恢复", records[0].watch_condition)
        self.assertIn("PE,PB,总市值", records[0].risk)
        self.assertNotIn("观察成交额延续和价格相对均线结构", records[0].watch_condition)

    def test_ai_narrative_updates_top_records_from_json_response(self) -> None:
        records = [
            sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000),
            sample_record("MSFT", "us", 200, 20_000_000, 2_000_000_000),
        ]
        quality = QualityLog()
        score_records(records, "balanced", quality)

        def fake_requester(payload, base_url, api_key, timeout_seconds):
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(base_url, "https://example.test/v1")
            self.assertEqual(api_key, "test-key")
            self.assertEqual(timeout_seconds, 7)
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "narratives": [
                                        {
                                            "yahoo_symbol": records[0].yahoo_symbol,
                                            "reason": "AI基于排名、成交额和综合得分描述进入研究候选的原因。",
                                            "risk": "AI提示估值和历史波动仍需继续复核。",
                                            "watch_condition": "AI要求后续确认成交额延续和均线结构。",
                                            "invalidation": "AI说明如果评分回落且成交活跃度下降则移出观察池。",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        previous_second_reason = records[1].reason
        applied = apply_ai_narratives(
            records,
            quality,
            top_n=1,
            config={"ai_narrative": {"timeout_seconds": 7}},
            api_key="test-key",
            model="test-model",
            base_url="https://example.test/v1",
            requester=fake_requester,
        )
        self.assertTrue(applied)
        self.assertTrue(records[0].reason.startswith("AI基于排名"))
        self.assertEqual(records[1].reason, previous_second_reason)
        self.assertTrue(any("AI文案已生成" in warning for warning in quality.warnings))

    def test_ai_narrative_rejects_forbidden_trade_terms(self) -> None:
        record = sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000)
        quality = QualityLog()
        score_records([record], "balanced", quality)
        previous_reason = record.reason

        def fake_requester(_payload, _base_url, _api_key, _timeout_seconds):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "narratives": [
                                        {
                                            "yahoo_symbol": record.yahoo_symbol,
                                            "reason": "建议买入，后续继续观察。",
                                            "risk": "风险有限。",
                                            "watch_condition": "继续跟踪。",
                                            "invalidation": "弱于预期。",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        applied = apply_ai_narratives(
            [record],
            quality,
            top_n=1,
            config={},
            api_key="test-key",
            model="test-model",
            requester=fake_requester,
        )
        self.assertFalse(applied)
        self.assertEqual(record.reason, previous_reason)
        self.assertTrue(any("未通过校验" in warning for warning in quality.warnings))

    def test_synthetic_history_only_for_custom_or_preset(self) -> None:
        custom = StockRecord("AAPL", "AAPL", "us", price=100, change_pct=2, source="custom")
        live = StockRecord("MSFT", "MSFT", "us", price=100, change_pct=2, source="AKShare:stock_us_spot_em")
        quality = QualityLog()
        seed_snapshot_history([custom, live], quality)
        self.assertTrue(custom.history)
        self.assertFalse(live.history)
        self.assertTrue(custom.fundamentals["_synthetic_history"])

    def test_write_outputs_schema(self) -> None:
        records = [sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000)]
        quality = QualityLog()
        score_records(records, "balanced", quality)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            write_outputs(
                output_dir=output_dir,
                records=records,
                market="us",
                style="balanced",
                top_n=10,
                max_candidates=200,
                config={"output_root": "desktop"},
                quality=quality,
                chart_paths={},
            )
            payload = json.loads((output_dir / "screening_results.json").read_text(encoding="utf-8"))
            data_quality = json.loads((output_dir / "data_quality.json").read_text(encoding="utf-8"))
            csv_header = (output_dir / "screening_results.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertIn("top10_global", payload)
        self.assertIn("missing_field_counts", data_quality)
        self.assertEqual(csv_header, CSV_COLUMNS)

    def test_generate_charts_nonempty(self) -> None:
        records = [sample_record("AAPL", "us", 100, 10_000_000, 1_000_000_000)]
        quality = QualityLog()
        score_records(records, "balanced", quality)
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_charts(records, Path(tmpdir), 10)
            for path in paths.values():
                self.assertTrue(Path(path).exists())
                self.assertGreater(Path(path).stat().st_size, 20)

    def test_bad_custom_input_validation(self) -> None:
        args = parse_args(["--universe", "custom"])
        args = apply_defaults(args, {"default_market": "all", "default_style": "balanced", "default_top_n": 10, "max_candidates": 200})
        with self.assertRaises(ValueError):
            validate_args(args)

    def test_run_empty_custom_returns_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "symbols.txt"
            path.write_text("# empty\n", encoding="utf-8")
            args = parse_args(["--universe", "custom", "--symbols-file", str(path), "--out-dir", tmpdir])
            args = apply_defaults(args, {"default_market": "all", "default_style": "balanced", "default_top_n": 10, "max_candidates": 200})
            code, paths = run(args, {"output_root": "desktop"})
        self.assertEqual(code, EXIT_NO_CANDIDATES)
        self.assertIn("report", paths)


if __name__ == "__main__":
    unittest.main()
