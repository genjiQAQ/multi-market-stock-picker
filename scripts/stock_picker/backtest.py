"""Cache-history based rolling backtests for picker scoring."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from . import providers
from .models import SCHEMA_VERSION, QualityLog, StockRecord
from .scoring import score_records


BACKTEST_COLUMNS = [
    "evaluation_date",
    "market",
    "yahoo_symbol",
    "raw_symbol",
    "name",
    "rank_global",
    "rank_in_market",
    "final_score",
    "rating",
    "result_level",
    "industry",
    "entry_price",
    "exit_price",
    "forward_return_pct",
    "max_drawdown_pct",
    "hold_days",
    "style",
    "run_mode",
]


def generated_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_backtest(
    *,
    records: list[StockRecord],
    output_dir: Path,
    style: str,
    run_mode: str,
    top_n: int,
    start: str | None,
    end: str | None,
    window_days: int,
    hold_days: int,
    frequency: str,
    config: dict[str, Any],
) -> dict[str, str]:
    generated = generated_at()
    cache_quality = QualityLog(run_mode=run_mode)
    histories, missing_history = _load_cached_histories(records, cache_quality)
    parsed_start = _parse_date(start) if start else None
    parsed_end = _parse_date(end) if end else None
    evaluation_dates = _evaluation_dates(histories, parsed_start, parsed_end, frequency)
    results: list[dict[str, Any]] = []
    insufficient_history: list[dict[str, Any]] = []
    skipped_periods: list[str] = []

    for evaluation_date in evaluation_dates:
        period_records: list[StockRecord] = []
        period_context: dict[str, tuple[float, float, float | None]] = {}
        for base_record in records:
            symbol = base_record.yahoo_symbol or base_record.raw_symbol
            rows = histories.get(symbol)
            if not rows:
                continue
            prepared = _record_for_evaluation(base_record, rows, evaluation_date, window_days, hold_days)
            if prepared is None:
                insufficient_history.append({"symbol": symbol, "market": base_record.market, "evaluation_date": evaluation_date.isoformat()})
                continue
            period_record, entry_price, exit_price, drawdown = prepared
            period_records.append(period_record)
            period_context[symbol] = (entry_price, exit_price, drawdown)
        if not period_records:
            skipped_periods.append(evaluation_date.isoformat())
            continue

        period_quality = QualityLog(run_mode=run_mode)
        score_records(period_records, style, period_quality)
        top_records = sorted(
            [record for record in period_records if record.is_tradeable and "final_score" in record.scores],
            key=lambda record: record.rank_global or 10**9,
        )[:top_n]
        if not top_records:
            skipped_periods.append(evaluation_date.isoformat())
            continue
        for record in top_records:
            symbol = record.yahoo_symbol or record.raw_symbol
            entry_price, exit_price, drawdown = period_context[symbol]
            forward_return = (exit_price / entry_price - 1) * 100 if entry_price else None
            results.append(
                {
                    "evaluation_date": evaluation_date.isoformat(),
                    "market": record.market,
                    "yahoo_symbol": record.yahoo_symbol,
                    "raw_symbol": record.raw_symbol,
                    "name": record.name,
                    "rank_global": record.rank_global,
                    "rank_in_market": record.rank_in_market,
                    "final_score": record.scores.get("final_score"),
                    "rating": record.rating,
                    "result_level": record.result_level,
                    "industry": record.industry,
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "forward_return_pct": round(forward_return, 4) if forward_return is not None else None,
                    "max_drawdown_pct": round(drawdown, 4) if drawdown is not None else None,
                    "hold_days": hold_days,
                    "style": style,
                    "run_mode": run_mode,
                }
            )

    summary = _summary(results)
    quality_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "backtest_mode": True,
        "cache_used": cache_quality.cache_used,
        "warnings": cache_quality.warnings,
        "missing_history": missing_history,
        "insufficient_history": insufficient_history,
        "skipped_periods": skipped_periods,
        "period_count": summary["period_count"],
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "style": style,
        "run_mode": run_mode,
        "top_n": top_n,
        "config": {
            "backtest_start": start,
            "backtest_end": end,
            "backtest_window_days": window_days,
            "backtest_hold_days": hold_days,
            "backtest_frequency": frequency,
            "history_source": "cache:history",
            "min_price_by_market": config.get("min_price_by_market"),
        },
        "summary": summary,
        "market_breakdown": _group_breakdown(results, "market"),
        "industry_breakdown": _group_breakdown(results, "industry"),
        "results": results,
    }

    results_csv = output_dir / "backtest_results.csv"
    results_json = output_dir / "backtest_results.json"
    quality_json = output_dir / "backtest_quality.json"
    report_md = output_dir / "backtest_report.md"
    _write_results_csv(results_csv, results)
    results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    quality_json.write_text(json.dumps(quality_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report_md.write_text(_build_report(generated, style, run_mode, top_n, summary, results, quality_payload), encoding="utf-8")
    return {
        "backtest_report": str(report_md),
        "backtest_results_csv": str(results_csv),
        "backtest_results_json": str(results_json),
        "backtest_quality": str(quality_json),
    }


def _load_cached_histories(records: list[StockRecord], quality: QualityLog) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    histories: dict[str, list[dict[str, Any]]] = {}
    missing: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        symbol = record.yahoo_symbol or record.raw_symbol
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        cached = providers.read_cache("history", symbol, quality, ttl_hours=None)
        rows = _normalize_history_rows(cached or [])
        if not rows:
            missing.append({"symbol": symbol, "market": record.market})
            continue
        histories[symbol] = rows
    return histories, missing


def _normalize_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_date = _row_date(row)
        close = _float(row.get("close"))
        if item_date is None or close is None:
            continue
        normalized.append(
            {
                "date": item_date,
                "open": _float(row.get("open")) or close,
                "high": _float(row.get("high")) or close,
                "low": _float(row.get("low")) or close,
                "close": close,
                "volume": _float(row.get("volume")) or 0.0,
                "turnover": _float(row.get("turnover")),
            }
        )
    normalized.sort(key=lambda row: row["date"])
    return normalized


def _evaluation_dates(
    histories: dict[str, list[dict[str, Any]]],
    start: date | None,
    end: date | None,
    frequency: str,
) -> list[date]:
    all_dates = sorted({row["date"] for rows in histories.values() for row in rows})
    filtered = [item for item in all_dates if (start is None or item >= start) and (end is None or item <= end)]
    if frequency == "daily":
        return filtered
    selected: list[date] = []
    seen: set[tuple[int, int] | tuple[int, int, int]] = set()
    for item in filtered:
        if frequency == "monthly":
            key: tuple[int, int] | tuple[int, int, int] = (item.year, item.month)
        else:
            iso = item.isocalendar()
            key = (iso.year, iso.week, 0)
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
    return selected


def _record_for_evaluation(
    base_record: StockRecord,
    rows: list[dict[str, Any]],
    evaluation_date: date,
    window_days: int,
    hold_days: int,
) -> tuple[StockRecord, float, float, float | None] | None:
    prior = [row for row in rows if row["date"] <= evaluation_date]
    future = [row for row in rows if row["date"] > evaluation_date]
    if len(prior) < window_days or len(future) < hold_days:
        return None
    window = prior[-window_days:]
    entry = window[-1]
    future_window = future[:hold_days]
    exit_row = future_window[-1]
    entry_price = float(entry["close"])
    exit_price = float(exit_row["close"])
    period_rows = [
        {
            "date": row["date"].isoformat(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "turnover": row.get("turnover"),
        }
        for row in window
    ]
    record = replace(
        base_record,
        price=entry_price,
        volume=entry.get("volume"),
        turnover=entry.get("turnover") or (entry_price * float(entry.get("volume") or 0)),
        history=period_rows,
        scores={},
        indicators={},
        rank_global=None,
        rank_in_market=None,
        rating="",
        reason="",
        risk="",
        watch_condition="",
        invalidation="",
        is_tradeable=True,
        exclude_reason="",
        run_mode="backtest",
    )
    drawdown = _max_drawdown(entry_price, [row["close"] for row in future_window])
    return record, entry_price, exit_price, drawdown


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["forward_return_pct"]) for row in results if row.get("forward_return_pct") is not None]
    drawdowns = [float(row["max_drawdown_pct"]) for row in results if row.get("max_drawdown_pct") is not None]
    scores = [float(row["final_score"]) for row in results if row.get("final_score") is not None]
    periods = {row["evaluation_date"] for row in results}
    low_coverage_count = len([row for row in results if row.get("result_level") == "低覆盖率结果"])
    industry_values = [row.get("industry") or "未知" for row in results]
    most_common_industry = max((industry_values.count(item), item) for item in set(industry_values))[1] if industry_values else ""
    industry_concentration = industry_values.count(most_common_industry) / len(industry_values) * 100 if industry_values else 0.0
    return {
        "period_count": len(periods),
        "selection_count": len(results),
        "avg_forward_return": round(mean(returns), 4) if returns else None,
        "median_forward_return": round(median(returns), 4) if returns else None,
        "win_rate": round(len([value for value in returns if value > 0]) / len(returns) * 100, 4) if returns else None,
        "max_drawdown": round(min(drawdowns), 4) if drawdowns else None,
        "avg_topn_score": round(mean(scores), 4) if scores else None,
        "low_coverage_ratio": round(low_coverage_count / len(results) * 100, 4) if results else None,
        "industry_concentration": round(industry_concentration, 4),
        "dominant_industry": most_common_industry,
    }


def _group_breakdown(results: list[dict[str, Any]], field_name: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        key = str(row.get(field_name) or "未知")
        grouped.setdefault(key, []).append(row)
    return {key: _summary(rows) for key, rows in sorted(grouped.items())}


def _write_results_csv(path: Path, results: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=BACKTEST_COLUMNS)
        writer.writeheader()
        for row in results:
            writer.writerow({column: row.get(column) for column in BACKTEST_COLUMNS})


def _build_report(
    generated: str,
    style: str,
    run_mode: str,
    top_n: int,
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    quality: dict[str, Any],
) -> str:
    lines = [
        "# 选股模型回测验证",
        "",
        f"- 生成时间：{generated}",
        f"- 风格：{style}",
        f"- 运行模式：{run_mode}",
        f"- TopN：{top_n}",
        f"- 评估期数：{summary.get('period_count')}",
        f"- 入选样本数：{summary.get('selection_count')}",
        "",
        "## 核心指标",
        "",
        f"- 平均后续收益：{summary.get('avg_forward_return')}%",
        f"- 后续收益中位数：{summary.get('median_forward_return')}%",
        f"- 胜率：{summary.get('win_rate')}%",
        f"- 最大回撤：{summary.get('max_drawdown')}%",
        f"- 平均 TopN 得分：{summary.get('avg_topn_score')}",
        f"- 低覆盖率占比：{summary.get('low_coverage_ratio')}%",
        f"- 行业集中度：{summary.get('industry_concentration')}%（{summary.get('dominant_industry') or '未知'}）",
        "",
        "## 样本预览",
        "",
    ]
    for row in results[:20]:
        lines.append(
            f"- {row.get('evaluation_date')} {row.get('market')} / {row.get('yahoo_symbol')}："
            f"排名 {row.get('rank_global')}，后续收益 {row.get('forward_return_pct')}%，最大回撤 {row.get('max_drawdown_pct')}%。"
        )
    if not results:
        lines.append("没有足够缓存历史生成回测样本。")
    lines.extend(
        [
            "",
            "## 数据质量",
            "",
            f"- 缺失历史：{len(quality.get('missing_history') or [])}",
            f"- 历史不足：{len(quality.get('insufficient_history') or [])}",
            f"- 跳过评估期：{len(quality.get('skipped_periods') or [])}",
            "",
            "本报告仅用于研究辅助和模型验证，不构成投资建议。",
        ]
    )
    return "\n".join(lines) + "\n"


def _row_date(row: dict[str, Any]) -> date | None:
    raw = row.get("date")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.startswith("snapshot-"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _max_drawdown(entry_price: float, closes: list[float]) -> float | None:
    if not closes or entry_price <= 0:
        return None
    peak = entry_price
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak:
            worst = min(worst, close / peak - 1)
    return worst * 100
