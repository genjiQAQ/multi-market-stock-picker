"""Shared data structures for the multi-market stock picker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MARKETS = ("a-share", "us", "hk")
SCHEMA_VERSION = "1.0"


@dataclass
class QualityLog:
    sources: dict[str, str] = field(default_factory=dict)
    source_failures: list[dict[str, str]] = field(default_factory=list)
    symbol_failures: list[dict[str, str]] = field(default_factory=list)
    excluded_counts: dict[str, int] = field(default_factory=dict)
    missing_field_counts: dict[str, int] = field(default_factory=dict)
    cache_used: dict[str, bool] = field(default_factory=dict)
    cache_expired: list[dict[str, Any]] = field(default_factory=list)
    provider_breakers: list[dict[str, Any]] = field(default_factory=list)
    run_mode: str = ""
    result_level: str = ""
    watchlist_state_path: str = ""
    watchlist_changes_count: int = 0
    watchlist_changes: list[dict[str, Any]] = field(default_factory=list)
    backtest_mode: bool = False
    warnings: list[str] = field(default_factory=list)

    def add_source_failure(self, market: str, source: str, error: Exception | str) -> None:
        self.source_failures.append({"market": market, "source": source, "error": str(error)})

    def add_symbol_failure(self, symbol: str, market: str, error: Exception | str) -> None:
        self.symbol_failures.append({"symbol": symbol, "market": market, "error": str(error)})

    def add_excluded(self, reason: str | None) -> None:
        key = reason or "unknown"
        self.excluded_counts[key] = self.excluded_counts.get(key, 0) + 1

    def add_missing(self, field_name: str) -> None:
        self.missing_field_counts[field_name] = self.missing_field_counts.get(field_name, 0) + 1

    def to_json(self, generated_at: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "sources": self.sources,
            "source_failures": self.source_failures,
            "symbol_failures": self.symbol_failures,
            "excluded_counts": self.excluded_counts,
            "missing_field_counts": self.missing_field_counts,
            "cache_used": self.cache_used,
            "cache_expired": self.cache_expired,
            "provider_breakers": self.provider_breakers,
            "run_mode": self.run_mode,
            "result_level": self.result_level,
            "watchlist_state_path": self.watchlist_state_path,
            "watchlist_changes_count": self.watchlist_changes_count,
            "backtest_mode": self.backtest_mode,
            "warnings": self.warnings,
        }


@dataclass
class StockRecord:
    raw_symbol: str
    yahoo_symbol: str
    market: str
    name: str = ""
    price: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    turnover: float | None = None
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    high: float | None = None
    low: float | None = None
    amplitude_pct: float | None = None
    close_position: float | None = None
    currency: str = ""
    source: str = ""
    source_time: str = ""
    data_delay: str = ""
    run_mode: str = ""
    result_level: str = ""
    industry: str = ""
    concepts: list[str] = field(default_factory=list)
    enrichment_status: str = ""
    is_tradeable: bool = True
    exclude_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    fundamentals: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, float | None] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    rank_in_market: int | None = None
    rank_global: int | None = None
    rating: str = ""
    reason: str = ""
    risk: str = ""
    watch_condition: str = ""
    invalidation: str = ""
    watch_status: str = ""
    previous_rank: int | None = None
    rank_change: int | None = None
    previous_score: float | None = None
    score_change: float | None = None
    watch_runs: int = 0
    last_seen_at: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "rank_global": self.rank_global,
            "rank_in_market": self.rank_in_market,
            "market": self.market,
            "raw_symbol": self.raw_symbol,
            "yahoo_symbol": self.yahoo_symbol,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "turnover": self.turnover,
            "market_cap": self.market_cap,
            "pe": self.pe,
            "pb": self.pb,
            "run_mode": self.run_mode,
            "result_level": self.result_level,
            "industry": self.industry,
            "concepts": ";".join(self.concepts) if isinstance(self.concepts, list) else self.concepts,
            "enrichment_status": self.enrichment_status,
            "momentum_score": self.scores.get("momentum_score"),
            "quality_trend_score": self.scores.get("quality_trend_score"),
            "liquidity_score": self.scores.get("liquidity_score"),
            "risk_control_score": self.scores.get("risk_control_score"),
            "final_score": self.scores.get("final_score"),
            "data_coverage": self.scores.get("data_coverage"),
            "quality_coverage": self.scores.get("quality_coverage"),
            "rating": self.rating,
            "watch_status": self.watch_status,
            "previous_rank": self.previous_rank,
            "rank_change": self.rank_change,
            "previous_score": self.previous_score,
            "score_change": self.score_change,
            "watch_runs": self.watch_runs,
            "last_seen_at": self.last_seen_at,
            "reason": self.reason,
            "risk": self.risk,
            "watch_condition": self.watch_condition,
            "invalidation": self.invalidation,
            "source": self.source,
            "source_time": self.source_time,
            "exclude_reason": self.exclude_reason,
        }
