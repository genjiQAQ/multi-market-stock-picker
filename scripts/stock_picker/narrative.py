"""Optional AI-generated narrative fields for report candidates."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.request import Request, urlopen

from .models import QualityLog, StockRecord


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
NARRATIVE_FIELDS = ("reason", "risk", "watch_condition", "invalidation")
FORBIDDEN_TERMS = (
    "买入",
    "卖出",
    "建仓",
    "加仓",
    "减仓",
    "清仓",
    "满仓",
    "必涨",
    "稳赚",
    "保证收益",
)

Requester = Callable[[dict[str, Any], str, str, int], dict[str, Any]]


def apply_ai_narratives(
    records: list[StockRecord],
    quality: QualityLog,
    *,
    top_n: int,
    config: dict[str, Any],
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    requester: Requester | None = None,
) -> bool:
    """Generate narrative fields for top candidates through an optional LLM call."""

    candidates = _top_records(records, top_n)
    if not candidates:
        return False

    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_api_key:
        quality.warnings.append("AI文案未启用：缺少 OPENAI_API_KEY。")
        return False

    ai_config = config.get("ai_narrative", {}) if isinstance(config.get("ai_narrative"), dict) else {}
    resolved_model = model or os.getenv("OPENAI_MODEL") or ai_config.get("model") or DEFAULT_MODEL
    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or ai_config.get("base_url") or DEFAULT_BASE_URL
    resolved_timeout = int(timeout_seconds or ai_config.get("timeout_seconds") or config.get("request_timeout_seconds") or 30)

    payload = build_chat_payload(candidates, model=resolved_model)
    try:
        response = (requester or request_chat_completion)(payload, resolved_base_url, resolved_api_key, resolved_timeout)
        narratives = parse_narrative_response(response)
    except Exception as exc:
        quality.warnings.append(f"AI文案生成失败，已使用规则回退文案：{exc}")
        return False

    by_symbol = {str(item.get("yahoo_symbol") or ""): item for item in narratives}
    applied = 0
    rejected = 0
    for record in candidates:
        item = by_symbol.get(record.yahoo_symbol)
        if not item:
            rejected += 1
            continue
        if not validate_narrative(item):
            rejected += 1
            continue
        for field in NARRATIVE_FIELDS:
            setattr(record, field, clean_text(str(item[field])))
        applied += 1

    if applied:
        quality.warnings.append(f"AI文案已生成：{applied} 个候选，模型 {resolved_model}。")
    if rejected:
        quality.warnings.append(f"AI文案有 {rejected} 个候选未通过校验，保留规则回退文案。")
    return applied > 0


def build_chat_payload(records: list[StockRecord], *, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0.35,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是股票研究报告撰写助手，只能基于用户提供的结构化事实生成中文研究候选说明。"
                    "不要补充未提供的价格、K线、估值、新闻或基本面事实。"
                    "不要给买入、卖出、仓位、收益保证或交易指令。"
                    "输出必须是严格 JSON 对象。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(build_narrative_prompt(records), ensure_ascii=False, separators=(",", ":")),
            },
        ],
    }


def build_narrative_prompt(records: list[StockRecord]) -> dict[str, Any]:
    return {
        "task": "为每个候选生成 reason、risk、watch_condition、invalidation 四个字段。",
        "language": "zh-CN",
        "output_schema": {
            "narratives": [
                {
                    "yahoo_symbol": "string",
                    "reason": "80-180字，解释为什么进入研究候选，必须引用排名、成交额、涨跌幅、核心分数或数据覆盖限制中的至少两项。",
                    "risk": "40-140字，说明主要风险；如果K线或估值缺失，必须点明。",
                    "watch_condition": "40-140字，描述继续跟踪所需的数据或市场行为确认。",
                    "invalidation": "40-140字，描述移出观察池的失效条件，不得写交易指令。",
                }
            ]
        },
        "rules": [
            "只使用 candidates 中的事实。",
            "如果 data_coverage 低于 40，必须说明历史K线/均线/回撤验证不足。",
            "如果 quality_coverage 低于 35，必须说明估值或基本面字段覆盖不足。",
            "不要使用买入、卖出、建仓、加仓、减仓、清仓、满仓、必涨、稳赚、保证收益等词。",
            "所有结论都必须表述为研究候选或观察条件，不构成投资建议。",
        ],
        "candidates": [record_to_ai_input(record) for record in records],
    }


def record_to_ai_input(record: StockRecord) -> dict[str, Any]:
    missing_fields = []
    if record.pe is None:
        missing_fields.append("pe")
    if record.pb is None:
        missing_fields.append("pb")
    if record.market_cap is None:
        missing_fields.append("market_cap")
    if not record.history:
        missing_fields.append("history")
    return {
        "raw_symbol": record.raw_symbol,
        "yahoo_symbol": record.yahoo_symbol,
        "name": record.name,
        "market": record.market,
        "rank_global": record.rank_global,
        "rank_in_market": record.rank_in_market,
        "rating": record.rating,
        "price": record.price,
        "change_pct": record.change_pct,
        "turnover": record.turnover,
        "market_cap": record.market_cap,
        "pe": record.pe,
        "pb": record.pb,
        "currency": record.currency,
        "source": record.source,
        "source_time": record.source_time,
        "scores": {
            "momentum_score": record.scores.get("momentum_score"),
            "quality_trend_score": record.scores.get("quality_trend_score"),
            "liquidity_score": record.scores.get("liquidity_score"),
            "risk_control_score": record.scores.get("risk_control_score"),
            "final_score": record.scores.get("final_score"),
            "data_coverage": record.scores.get("data_coverage"),
            "quality_coverage": record.scores.get("quality_coverage"),
        },
        "indicators": {
            "return_20d": record.indicators.get("return_20d"),
            "return_60d": record.indicators.get("return_60d"),
            "sma20_ratio": record.indicators.get("sma20_ratio"),
            "sma60_ratio": record.indicators.get("sma60_ratio"),
            "max_drawdown": record.indicators.get("max_drawdown"),
            "volatility_20d": record.indicators.get("volatility_20d"),
        },
        "missing_fields": missing_fields,
    }


def request_chat_completion(payload: dict[str, Any], base_url: str, api_key: str, timeout_seconds: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_narrative_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = (((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise ValueError("empty AI response content")
    parsed = json.loads(_strip_json_fence(content))
    narratives = parsed.get("narratives")
    if not isinstance(narratives, list):
        raise ValueError("AI response missing narratives list")
    return [item for item in narratives if isinstance(item, dict)]


def validate_narrative(item: dict[str, Any]) -> bool:
    if not item.get("yahoo_symbol"):
        return False
    for field in NARRATIVE_FIELDS:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
        if contains_forbidden_term(value):
            return False
    return True


def contains_forbidden_term(value: str) -> bool:
    return any(term in value for term in FORBIDDEN_TERMS)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\n", " ")).strip()


def _strip_json_fence(value: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    return match.group(1).strip() if match else value


def _top_records(records: list[StockRecord], top_n: int) -> list[StockRecord]:
    candidates = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    candidates.sort(key=lambda record: record.rank_global or 10**9)
    return candidates[:top_n]
