"""Report and structured output generation."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, QualityLog, StockRecord


CSV_COLUMNS = [
    "rank_global",
    "rank_in_market",
    "market",
    "raw_symbol",
    "yahoo_symbol",
    "name",
    "price",
    "change_pct",
    "turnover",
    "market_cap",
    "pe",
    "pb",
    "run_mode",
    "result_level",
    "industry",
    "concepts",
    "enrichment_status",
    "momentum_score",
    "quality_trend_score",
    "liquidity_score",
    "risk_control_score",
    "final_score",
    "data_coverage",
    "quality_coverage",
    "rating",
    "reason",
    "risk",
    "watch_condition",
    "invalidation",
    "source",
    "source_time",
    "exclude_reason",
]


def generated_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_output_dir(market: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.name == "nt":
        root = Path.home() / "Desktop" / "stock-picker-output"
    else:
        root = Path.home() / "Desktop" / "stock-picker-output"
    return root / market / timestamp


def ensure_output_dir(path: str | None, market: str) -> Path:
    output_dir = Path(path).expanduser() if path else default_output_dir(market)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def result_rows(records: list[StockRecord]) -> list[dict[str, Any]]:
    rows = [record.to_row() for record in records if record.is_tradeable and "final_score" in record.scores]
    rows.sort(key=lambda row: (row.get("rank_global") or 10**9, -(row.get("final_score") or 0)))
    return rows


def top_global(records: list[StockRecord], top_n: int) -> list[StockRecord]:
    candidates = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    candidates.sort(key=lambda record: record.rank_global or 10**9)
    return candidates[:top_n]


def top_per_market(records: list[StockRecord], top_n: int) -> dict[str, list[StockRecord]]:
    grouped: dict[str, list[StockRecord]] = {}
    for record in records:
        if record.is_tradeable and "final_score" in record.scores:
            grouped.setdefault(record.market, []).append(record)
    for market_records in grouped.values():
        market_records.sort(key=lambda record: record.rank_in_market or 10**9)
    return {market: market_records[:top_n] for market, market_records in sorted(grouped.items())}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in CSV_COLUMNS})


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_results_payload(
    *,
    records: list[StockRecord],
    market: str,
    style: str,
    top_n: int,
    max_candidates: int,
    config: dict,
    generated: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "market": market,
        "style": style,
        "run_mode": config.get("_run_mode"),
        "result_level": config.get("_result_level"),
        "top_n": top_n,
        "max_candidates": max_candidates,
        "config": {
            "output_root": config.get("output_root"),
            "min_turnover_by_market": config.get("min_turnover_by_market"),
            "min_price_by_market": config.get("min_price_by_market"),
        },
        "top10_global": [record.to_row() for record in top_global(records, top_n)],
        "top_per_market": {
            item_market: [record.to_row() for record in market_records]
            for item_market, market_records in top_per_market(records, top_n).items()
        },
        "results": result_rows(records),
    }


def build_markdown_report(
    *,
    records: list[StockRecord],
    market: str,
    style: str,
    top_n: int,
    quality: QualityLog,
    generated: str,
) -> str:
    top_records = top_global(records, top_n)
    lines = [
        "# 多市场选股研究候选",
        "",
        f"- 生成时间：{generated}",
        f"- 市场：{market}",
        f"- 风格：{style}",
        f"- 运行模式：{quality.run_mode or '未知'}",
        f"- 结果等级：{quality.result_level or '未知'}",
        f"- 候选数量：{len(top_records)}",
        "",
        "## 数据摘要",
        "",
        f"- 可评分标的：{len([r for r in records if r.is_tradeable and 'final_score' in r.scores])}",
        f"- 被过滤标的：{sum(quality.excluded_counts.values())}",
        f"- 数据源失败：{len(quality.source_failures)}",
        f"- 单标的失败：{len(quality.symbol_failures)}",
        "",
        "## Top10 研究候选",
        "",
    ]
    if not top_records:
        lines.extend(["无合格候选。", ""])
    for record in top_records:
        lines.extend(
            [
                f"### {record.rank_global}. {record.name or record.yahoo_symbol} ({record.market} / {record.yahoo_symbol})",
                "",
                f"- 综合得分：{record.scores.get('final_score')}",
                f"- 评级：{record.rating}",
                f"- 结果等级：{record.result_level or quality.result_level or '未知'}",
                f"- 行业/概念：{_industry_concepts_text(record)}",
                f"- 入选理由：{record.reason}",
                f"- 主要风险：{record.risk}",
                f"- 观察条件：{record.watch_condition}",
                f"- 失效条件：{record.invalidation}",
                "",
            ]
        )

    lines.extend(["## 分市场结果", ""])
    for item_market, market_records in top_per_market(records, top_n).items():
        lines.append(f"### {item_market}")
        if not market_records:
            lines.append("")
            continue
        for record in market_records:
            lines.append(
                f"- {record.rank_in_market}. {record.name or record.yahoo_symbol} "
                f"({record.yahoo_symbol})：{record.scores.get('final_score')} / {record.rating}"
            )
        lines.append("")

    if market == "all":
        lines.extend(
            [
                "跨市场排名基于各市场内部百分位合并，仅用于研究排序；不同市场存在交易时段、币种、估值字段覆盖率、流动性和数据延迟差异，不代表直接投资优先级。",
                "",
            ]
        )

    lines.extend(
        [
            "## 数据质量和缺失字段",
            "",
            f"- 数据源：{json.dumps(quality.sources, ensure_ascii=False)}",
            f"- 数据源失败：{json.dumps(quality.source_failures, ensure_ascii=False)}",
            f"- 单标的失败：{json.dumps(quality.symbol_failures, ensure_ascii=False)}",
            f"- 过滤统计：{json.dumps(quality.excluded_counts, ensure_ascii=False)}",
            f"- 缺失字段：{json.dumps(quality.missing_field_counts, ensure_ascii=False)}",
            f"- 警告：{json.dumps(quality.warnings, ensure_ascii=False)}",
            f"- 缓存过期：{json.dumps(quality.cache_expired, ensure_ascii=False)}",
            f"- Provider 熔断：{json.dumps(quality.provider_breakers, ensure_ascii=False)}",
            "",
            "## 免责声明",
            "",
            "本报告仅用于研究辅助，不构成投资建议。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(
    *,
    output_dir: Path,
    records: list[StockRecord],
    market: str,
    style: str,
    top_n: int,
    max_candidates: int,
    config: dict,
    quality: QualityLog,
    chart_paths: dict[str, str] | None = None,
) -> dict[str, str]:
    generated = generated_at()
    config["_run_mode"] = quality.run_mode
    config["_result_level"] = quality.result_level
    rows = result_rows(records)
    top_rows = [record.to_row() for record in top_global(records, top_n)]
    write_csv(output_dir / "screening_results.csv", rows)
    write_csv(output_dir / "top10_candidates.csv", top_rows)
    write_json(
        output_dir / "screening_results.json",
        build_results_payload(
            records=records,
            market=market,
            style=style,
            top_n=top_n,
            max_candidates=max_candidates,
            config=config,
            generated=generated,
        ),
    )
    write_json(output_dir / "data_quality.json", quality.to_json(generated))
    (output_dir / "screening_report.md").write_text(
        build_markdown_report(
            records=records,
            market=market,
            style=style,
            top_n=top_n,
            quality=quality,
            generated=generated,
        ),
        encoding="utf-8",
    )
    return {
        "report": str(output_dir / "screening_report.md"),
        "results_csv": str(output_dir / "screening_results.csv"),
        "results_json": str(output_dir / "screening_results.json"),
        "top10_csv": str(output_dir / "top10_candidates.csv"),
        "data_quality": str(output_dir / "data_quality.json"),
        **(chart_paths or {}),
    }


def _industry_concepts_text(record: StockRecord) -> str:
    parts = []
    if record.industry:
        parts.append(record.industry)
    if record.concepts:
        parts.append("、".join(record.concepts[:5]))
    return " / ".join(parts) if parts else "缺失"
