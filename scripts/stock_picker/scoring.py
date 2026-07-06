"""Deterministic factor scoring and ranking."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from .indicators import compute_indicators
from .models import QualityLog, StockRecord


STYLE_WEIGHTS = {
    "balanced": {
        "momentum_score": 0.40,
        "quality_trend_score": 0.40,
        "liquidity_score": 0.10,
        "risk_control_score": 0.10,
    },
    "momentum": {
        "momentum_score": 0.60,
        "quality_trend_score": 0.15,
        "liquidity_score": 0.15,
        "risk_control_score": 0.10,
    },
    "quality-trend": {
        "momentum_score": 0.25,
        "quality_trend_score": 0.50,
        "liquidity_score": 0.10,
        "risk_control_score": 0.15,
    },
}

RATING_THRESHOLDS = (
    (80, "重点跟踪"),
    (70, "积极观察"),
    (60, "需进一步验证"),
    (50, "中性观察"),
    (0, "暂不纳入"),
)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def percentile_scores(records: list[StockRecord], getter, *, reverse: bool = False) -> dict[int, float]:
    values = []
    for idx, record in enumerate(records):
        value = getter(record)
        if value is not None:
            values.append((idx, float(value)))
    if not values:
        return {}
    values.sort(key=lambda item: item[1], reverse=reverse)
    if len(values) == 1:
        return {values[0][0]: 50.0}
    scores = {}
    for rank, (idx, _value) in enumerate(values):
        score = rank / (len(values) - 1) * 100
        if not reverse:
            score = 100 - score
        scores[idx] = score
    return scores


def _available_average(values: list[float | None]) -> tuple[float, float]:
    available = [float(value) for value in values if value is not None]
    if not values:
        return 50.0, 0.0
    coverage = len(available) / len(values)
    if not available:
        return 50.0, 0.0
    return mean(available), coverage


def reasonable_pe_score(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if 8 <= value <= 35:
        return 85
    if 35 < value <= 60:
        return 65
    if 3 <= value < 8:
        return 60
    return 35


def reasonable_pb_score(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if 0.8 <= value <= 6:
        return 80
    if 6 < value <= 12:
        return 60
    return 40


def score_records(records: list[StockRecord], style: str, quality: QualityLog) -> list[StockRecord]:
    tradable = [record for record in records if record.is_tradeable]
    if not quality.run_mode:
        quality.run_mode = "full"
    for record in tradable:
        if not record.run_mode:
            record.run_mode = quality.run_mode
    for record in tradable:
        compute_indicators(record)

    by_market: dict[str, list[StockRecord]] = defaultdict(list)
    for record in tradable:
        by_market[record.market].append(record)

    for market_records in by_market.values():
        apply_market_scores(market_records, style, quality)

    rank_records(tradable)
    quality.result_level = aggregate_result_level(tradable, quality.run_mode)
    return records


def apply_market_scores(records: list[StockRecord], style: str, quality: QualityLog) -> None:
    percentile_maps = {
        "return_5d": percentile_scores(records, lambda r: r.indicators.get("return_5d")),
        "return_20d": percentile_scores(records, lambda r: r.indicators.get("return_20d")),
        "return_60d": percentile_scores(records, lambda r: r.indicators.get("return_60d")),
        "volume_ratio_20d": percentile_scores(records, lambda r: r.indicators.get("volume_ratio_20d")),
        "macd_hist": percentile_scores(records, lambda r: r.indicators.get("macd_hist")),
        "turnover": percentile_scores(records, lambda r: r.turnover),
        "volume": percentile_scores(records, lambda r: r.volume),
        "market_cap": percentile_scores(records, lambda r: r.market_cap),
        "max_drawdown": percentile_scores(records, lambda r: r.indicators.get("max_drawdown"), reverse=True),
        "volatility_20d": percentile_scores(records, lambda r: r.indicators.get("volatility_20d"), reverse=True),
        "atr_pct": percentile_scores(records, lambda r: r.indicators.get("atr_pct"), reverse=True),
    }

    for idx, record in enumerate(records):
        ind = record.indicators
        trend_score, trend_cov = _available_average(
            [
                percentile_maps["return_5d"].get(idx),
                percentile_maps["return_20d"].get(idx),
                percentile_maps["return_60d"].get(idx),
            ]
        )
        ma_score, ma_cov = _available_average(
            [
                ratio_score(ind.get("sma20_ratio")),
                ratio_score(ind.get("sma60_ratio")),
                ratio_score(ind.get("sma120_ratio")),
            ]
        )
        rsi_value = ind.get("rsi_14")
        rsi_score = clamp(100 - abs(float(rsi_value) - 58) * 2) if rsi_value is not None else None
        momentum_score, momentum_cov = _weighted_available(
            [
                (trend_score, 0.30, trend_cov),
                (ma_score, 0.20, ma_cov),
                (percentile_maps["volume_ratio_20d"].get(idx), 0.15, 1.0 if percentile_maps["volume_ratio_20d"].get(idx) is not None else 0.0),
                (rsi_score, 0.10, 1.0 if rsi_score is not None else 0.0),
                (percentile_maps["macd_hist"].get(idx), 0.10, 1.0 if percentile_maps["macd_hist"].get(idx) is not None else 0.0),
                (percentile_maps["max_drawdown"].get(idx), 0.10, 1.0 if percentile_maps["max_drawdown"].get(idx) is not None else 0.0),
                (percentile_maps["volatility_20d"].get(idx), 0.05, 1.0 if percentile_maps["volatility_20d"].get(idx) is not None else 0.0),
            ]
        )

        valuation_score, valuation_cov = _available_average([reasonable_pe_score(record.pe), reasonable_pb_score(record.pb)])
        fundamentals = record.fundamentals
        profitability_score, profitability_cov = _available_average(
            [
                scaled_optional(fundamentals.get("profitMargins"), 0, 0.35),
                scaled_optional(fundamentals.get("returnOnEquity"), 0, 0.35),
            ]
        )
        growth_score, growth_cov = _available_average(
            [
                scaled_optional(fundamentals.get("revenueGrowth"), -0.2, 0.5),
                scaled_optional(fundamentals.get("earningsGrowth"), -0.2, 0.5),
            ]
        )
        cashflow_score = 70 if _positive(fundamentals.get("operatingCashflow")) else None
        leverage_score = 70 if fundamentals.get("totalDebt") is not None else None
        trend_stability, stability_cov = _available_average([ma_score, percentile_maps["max_drawdown"].get(idx)])
        quality_trend_score, quality_cov = _weighted_available(
            [
                (valuation_score, 0.20, valuation_cov),
                (percentile_maps["market_cap"].get(idx), 0.10, 1.0 if percentile_maps["market_cap"].get(idx) is not None else 0.0),
                (profitability_score, 0.20, profitability_cov),
                (growth_score, 0.15, growth_cov),
                (cashflow_score, 0.10, 1.0 if cashflow_score is not None else 0.0),
                (leverage_score, 0.10, 1.0 if leverage_score is not None else 0.0),
                (trend_stability, 0.15, stability_cov),
            ]
        )
        if quality_cov < 0.35:
            quality_trend_score = min(quality_trend_score, 65.0)
        elif quality_cov < 0.50:
            quality_trend_score = min(quality_trend_score, 75.0)
        liquidity_score, liquidity_cov = _available_average(
            [
                percentile_maps["turnover"].get(idx),
                percentile_maps["volume"].get(idx),
                percentile_maps["market_cap"].get(idx),
            ]
        )
        risk_control_score, risk_cov = _available_average(
            [
                percentile_maps["max_drawdown"].get(idx),
                percentile_maps["volatility_20d"].get(idx),
                percentile_maps["atr_pct"].get(idx),
                100 - min(abs(record.change_pct or 0) * 5, 100) if record.change_pct is not None else None,
            ]
        )

        weights = STYLE_WEIGHTS[style]
        final_score, component_cov = _weighted_available(
            [
                (momentum_score, weights["momentum_score"], momentum_cov),
                (quality_trend_score, weights["quality_trend_score"], quality_cov),
                (liquidity_score, weights["liquidity_score"], liquidity_cov),
                (risk_control_score, weights["risk_control_score"], risk_cov),
            ]
        )
        data_coverage = clamp(mean([momentum_cov, liquidity_cov, risk_cov, component_cov]) * 100)
        quality_coverage = clamp(quality_cov * 100)
        if fundamentals.get("_synthetic_history"):
            data_coverage = min(data_coverage, 45.0)
            momentum_score = min(momentum_score, 55.0)
            risk_control_score = min(risk_control_score, 55.0)
            final_score = min(final_score, 58.0)
        if quality_coverage < 35:
            final_score = min(final_score, 69.99)

        record.scores = {
            "momentum_score": round(clamp(momentum_score), 2),
            "quality_trend_score": round(clamp(quality_trend_score), 2),
            "liquidity_score": round(clamp(liquidity_score), 2),
            "risk_control_score": round(clamp(risk_control_score), 2),
            "final_score": round(clamp(final_score), 2),
            "data_coverage": round(data_coverage, 2),
            "quality_coverage": round(quality_coverage, 2),
        }
        record.result_level = result_level_for(record, quality.run_mode, data_coverage, quality_coverage)
        record.rating = rating_for(final_score, quality_coverage)
        if record.result_level == "低覆盖率结果":
            record.rating = "需进一步验证"
        record.reason = build_reason(record)
        record.risk = build_risk(record)
        record.watch_condition = build_watch_condition(record)
        record.invalidation = build_invalidation(record)
        for field_name, value in record.to_row().items():
            if field_name in {
                "rank_global",
                "rank_in_market",
                "exclude_reason",
                "watch_status",
                "previous_rank",
                "rank_change",
                "previous_score",
                "score_change",
                "watch_runs",
                "last_seen_at",
            }:
                continue
            if value is None or value == "":
                quality.add_missing(field_name)


def _weighted_available(items: list[tuple[float | None, float, float]]) -> tuple[float, float]:
    available = [(float(value), weight, coverage) for value, weight, coverage in items if value is not None and coverage > 0]
    if not available:
        return 50.0, 0.0
    weight_sum = sum(weight for _value, weight, _coverage in available)
    score = sum(value * weight for value, weight, _coverage in available) / weight_sum
    coverage = sum(weight * coverage for _value, weight, coverage in available) / sum(weight for _value, weight, _coverage in items)
    return score, coverage


def ratio_score(value: float | None) -> float | None:
    if value is None:
        return None
    return clamp(50 + (value - 1) * 250)


def scaled_optional(value, low: float, high: float) -> float | None:
    try:
        if value is None:
            return None
        raw = float(value)
    except Exception:
        return None
    return clamp((raw - low) / (high - low) * 100)


def _positive(value) -> bool:
    try:
        return value is not None and float(value) > 0
    except Exception:
        return False


def rating_for(score: float, quality_coverage: float | None = None) -> str:
    if quality_coverage is not None and quality_coverage < 35 and score >= 70:
        return "需进一步验证"
    for threshold, rating in RATING_THRESHOLDS:
        if score >= threshold:
            return rating
    return "暂不纳入"


def result_level_for(record: StockRecord, run_mode: str, data_coverage: float, quality_coverage: float) -> str:
    source = record.source or ""
    directory_only = source.startswith("NasdaqTrader:") and record.price is None
    no_quote_fields = record.price is None and record.turnover is None and record.volume is None
    failed_enrichment = record.enrichment_status in {"failed", "timeout", "skipped_provider_breaker"}
    if directory_only or no_quote_fields:
        return "低覆盖率结果"
    if run_mode == "snapshot-only":
        return "快照初筛"
    if failed_enrichment or data_coverage < 40 or quality_coverage < 20:
        return "低覆盖率结果"
    if run_mode == "fast":
        return "快速评分"
    return "完整评分"


def aggregate_result_level(records: list[StockRecord], run_mode: str) -> str:
    ranked = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    if not ranked:
        if run_mode == "snapshot-only":
            return "快照初筛"
        if run_mode == "full":
            return "完整评分"
        return "快速评分"
    ranked.sort(key=lambda record: record.rank_global or 10**9)
    return ranked[0].result_level or {
        "snapshot-only": "快照初筛",
        "full": "完整评分",
    }.get(run_mode, "快速评分")


def build_reason(record: StockRecord) -> str:
    parts = [
        f"综合得分 {record.scores.get('final_score', 0):.1f}",
        _rank_phrase(record),
        _turnover_phrase(record),
        _change_phrase(record),
        _liquidity_phrase(record),
    ]
    if record.scores.get("data_coverage", 100) < 40:
        parts.append("本轮历史K线覆盖不足，排名主要来自快照成交活跃度和当日波动约束")
    return "；".join(part for part in parts if part) + "。"


def build_risk(record: StockRecord) -> str:
    drawdown = record.indicators.get("max_drawdown")
    volatility = record.indicators.get("volatility_20d")
    parts = []
    if drawdown is not None:
        parts.append(f"历史最大回撤约 {drawdown:.1f}%")
    if volatility is not None:
        parts.append(f"20日年化波动约 {volatility:.1f}%")
    if drawdown is None and volatility is None and record.scores.get("data_coverage", 100) < 40:
        parts.append("K线历史缺失，均线、回撤和持续放量无法在本轮确认")
    if record.change_pct is not None and abs(record.change_pct) >= 5:
        parts.append(f"当日涨跌幅 {_format_pct(record.change_pct)}，短线情绪波动偏高")
    elif record.change_pct is not None and record.change_pct < -1:
        parts.append(f"当日回落 {_format_pct(record.change_pct)}，需要复核高成交额下的承接力度")
    if record.scores.get("quality_coverage", 0) < 50:
        missing = []
        if record.pe is None:
            missing.append("PE")
        if record.pb is None:
            missing.append("PB")
        if record.market_cap is None:
            missing.append("总市值")
        if missing:
            parts.append(f"估值字段缺失（{','.join(missing)}）")
        else:
            parts.append("基本面覆盖不足")
    return "；".join(parts) if parts else "数据覆盖有限，需复核流动性和波动。"


def build_watch_condition(record: StockRecord) -> str:
    parts = []
    if record.turnover is not None:
        parts.append(f"后续成交额能否维持在约{_format_amount(record.turnover * 0.7, record.currency)}以上（本次约{_format_amount(record.turnover, record.currency)}）")
    else:
        parts.append("后续成交额是否继续处在同市场前列")
    if record.price is not None:
        parts.append(f"价格围绕 {_format_price(record.price)} 的承接是否稳定")
    if record.change_pct is not None and record.change_pct >= 5:
        parts.append(f"当日涨幅 {_format_pct(record.change_pct)} 后是否避免快速回吐")
    elif record.change_pct is not None and record.change_pct < 0:
        parts.append(f"当日 {_format_pct(record.change_pct)} 回落后是否出现缩量企稳")
    else:
        parts.append("涨跌幅温和时是否仍能保持成交活跃")
    if record.scores.get("data_coverage", 100) < 40:
        parts.append("待K线接口恢复后补看20/60日均线和近20日成交量均值")
    return "；".join(parts) + "。"


def build_invalidation(record: StockRecord) -> str:
    parts = []
    if record.turnover is not None:
        parts.append(f"成交额明显跌破约{_format_amount(record.turnover * 0.5, record.currency)}且排名不再靠前")
    else:
        parts.append("成交活跃度退出同市场前列")
    if record.price is not None:
        if record.change_pct is not None and record.change_pct >= 5:
            parts.append(f"价格回落到 {_format_price(record.price * 0.95)} 附近仍无承接")
        elif record.change_pct is not None and record.change_pct < 0:
            parts.append(f"价格继续弱于 {_format_price(record.price)} 且放量下行")
        else:
            parts.append(f"价格持续弱于 {_format_price(record.price)} 且成交额同步萎缩")
    if record.scores.get("quality_coverage", 0) < 35:
        parts.append("补齐PE/PB/市值或历史K线后评分跌出60分区间")
    else:
        parts.append("回撤扩大或基本面字段转弱")
    return "；".join(parts) + "。"


def _rank_phrase(record: StockRecord) -> str:
    if record.rank_in_market:
        return f"市场内排名第 {record.rank_in_market}"
    return ""


def _turnover_phrase(record: StockRecord) -> str:
    if record.turnover is None:
        return "成交额缺失，需补充验证流动性"
    return f"本次成交额约 {_format_amount(record.turnover, record.currency)}"


def _change_phrase(record: StockRecord) -> str:
    if record.change_pct is None:
        return "当日涨跌幅缺失"
    change = _format_pct(record.change_pct)
    if record.change_pct >= 5:
        return f"当日上涨 {change}，活跃度强但需要防止单日脉冲"
    if record.change_pct >= 1:
        return f"当日上涨 {change}，价格动能偏强"
    if record.change_pct >= 0:
        return f"当日小幅上涨 {change}，不是单靠大涨入选"
    if record.change_pct > -1:
        return f"当日小幅回落 {change}，仍保持较高成交"
    return f"当日回落 {change}，入选更多来自成交额和流动性"


def _liquidity_phrase(record: StockRecord) -> str:
    liquidity = record.scores.get("liquidity_score")
    risk_control = record.scores.get("risk_control_score")
    parts = []
    if liquidity is not None:
        if liquidity >= 85:
            parts.append(f"流动性分 {liquidity:.1f}，处于候选池前段")
        elif liquidity >= 65:
            parts.append(f"流动性分 {liquidity:.1f}，成交活跃度中上")
        else:
            parts.append(f"流动性分 {liquidity:.1f}，成交优势不算突出")
    if risk_control is not None:
        if risk_control >= 90:
            parts.append(f"风险控制分 {risk_control:.1f}，当日涨跌幅惩罚较低")
        elif risk_control >= 70:
            parts.append(f"风险控制分 {risk_control:.1f}，短线波动仍需跟踪")
        else:
            parts.append(f"风险控制分 {risk_control:.1f}，当日波动对评分有拖累")
    return "，".join(parts)


def _format_pct(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:+.2f}%"


def _format_amount(value: float | None, currency: str = "") -> str:
    if value is None:
        return "未知"
    suffix = {"CNY": "元", "HKD": "港元", "USD": "美元"}.get(currency, currency or "")
    abs_value = abs(value)
    if abs_value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿{suffix}"
    if abs_value >= 10_000:
        return f"{value / 10_000:.2f}万{suffix}"
    return f"{value:.0f}{suffix}"


def _format_price(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def rank_records(records: list[StockRecord]) -> None:
    by_market: dict[str, list[StockRecord]] = defaultdict(list)
    for record in records:
        if record.is_tradeable and "final_score" in record.scores:
            by_market[record.market].append(record)
    for market_records in by_market.values():
        market_records.sort(key=lambda item: item.scores.get("final_score", 0), reverse=True)
        for idx, record in enumerate(market_records, start=1):
            record.rank_in_market = idx
    all_records = [record for records_for_market in by_market.values() for record in records_for_market]
    global_percentile = _market_percentile_global(all_records)
    all_records.sort(key=lambda item: (global_percentile.get(id(item), 0), item.scores.get("final_score", 0)), reverse=True)
    for idx, record in enumerate(all_records, start=1):
        record.rank_global = idx


def _market_percentile_global(records: list[StockRecord]) -> dict[int, float]:
    by_market: dict[str, list[StockRecord]] = defaultdict(list)
    for record in records:
        by_market[record.market].append(record)
    result: dict[int, float] = {}
    for market_records in by_market.values():
        market_records.sort(key=lambda item: item.scores.get("final_score", 0), reverse=True)
        if len(market_records) == 1:
            result[id(market_records[0])] = 100.0
            continue
        for idx, record in enumerate(market_records):
            result[id(record)] = 100 - idx / (len(market_records) - 1) * 100
    return result
