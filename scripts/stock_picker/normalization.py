"""Ticker normalization and row filtering."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from .models import StockRecord


A_SHARE_SH_PREFIXES = ("600", "601", "603", "605", "688")
A_SHARE_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
A_SHARE_BJ_PREFIXES = ("430", "8", "920")
US_NON_COMMON_PATTERNS = (
    "warrant",
    "unit",
    "right",
    "preferred",
    "preference",
    "depositary",
    "pink",
    "otc",
    "acquisition corp",
    "blank check",
    "spac",
)
HK_NON_COMMON_PATTERNS = ("权证", "牛熊", "债券", "基金", "etf", "reit", "warrant")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_symbol(value: str | int | float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip().upper()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def infer_market(symbol: str, default: str | None = None) -> str:
    text = clean_symbol(symbol)
    if text.endswith((".SS", ".SZ")) or re.fullmatch(r"\d{6}", text):
        return "a-share"
    if text.endswith(".HK") or re.fullmatch(r"\d{1,5}", text):
        return "hk"
    return default or "us"


def normalize_a_share_symbol(symbol: str) -> tuple[str, str, str]:
    raw = re.sub(r"\D", "", clean_symbol(symbol))[-6:].zfill(6)
    if raw.startswith(A_SHARE_SH_PREFIXES):
        return raw, f"{raw}.SS", ""
    if raw.startswith(A_SHARE_SZ_PREFIXES):
        return raw, f"{raw}.SZ", ""
    if raw.startswith(A_SHARE_BJ_PREFIXES):
        return raw, "", "bj_unsupported"
    return raw, "", "a_share_prefix_unsupported"


def normalize_hk_symbol(symbol: str) -> tuple[str, str, str]:
    text = clean_symbol(symbol).replace(".HK", "")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return text, "", "hk_symbol_invalid"
    raw = digits[-5:] if len(digits) > 5 else digits
    yahoo_raw = raw.zfill(4)
    return yahoo_raw, f"{yahoo_raw}.HK", ""


def normalize_us_symbol(symbol: str) -> tuple[str, str, str]:
    text = clean_symbol(symbol)
    if "." in text:
        prefix, suffix = text.split(".", 1)
        if prefix.isdigit() and suffix:
            text = suffix
        elif len(prefix) <= 5 and suffix:
            text = f"{prefix}-{suffix}"
    text = text.replace("/", "-")
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", text):
        return text, "", "us_symbol_invalid"
    return text, text, ""


def normalize_symbol(symbol: str, market: str | None = None) -> tuple[str, str, str, str]:
    resolved_market = market or infer_market(symbol)
    if resolved_market == "a-share":
        raw, yahoo, reason = normalize_a_share_symbol(symbol)
    elif resolved_market == "hk":
        raw, yahoo, reason = normalize_hk_symbol(symbol)
    elif resolved_market == "us":
        raw, yahoo, reason = normalize_us_symbol(symbol)
    else:
        raw, yahoo, reason = clean_symbol(symbol), "", "market_unsupported"
    return raw, yahoo, resolved_market, reason


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def first_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def detect_exclude_reason(record: StockRecord, config: dict[str, Any]) -> str:
    if record.exclude_reason:
        return record.exclude_reason
    name = (record.name or "").lower()
    symbol = record.raw_symbol.upper()
    min_turnover = config.get("min_turnover_by_market", {}).get(record.market)
    min_price = config.get("min_price_by_market", {}).get(record.market)
    source = record.source or ""
    user_or_preset = source == "custom" or source.startswith("preset:") or source.startswith("NasdaqTrader:")

    if record.price is None and not user_or_preset:
        return "missing_price"
    if min_price is not None and record.price is not None and record.price < float(min_price):
        return "price_too_low"
    if (
        min_turnover is not None
        and not user_or_preset
        and (record.turnover is None or record.turnover < float(min_turnover))
    ):
        return "turnover_too_low"

    if record.market == "a-share":
        a_rules = config.get("exclude_rules", {}).get("a-share", {})
        if re.match(r"^[cn]", record.name or "", re.IGNORECASE):
            return "new_stock_prefix_c_or_n"
        if "st" in name or "退" in record.name:
            return "a_share_st_or_delisted"
        if record.yahoo_symbol == "":
            return "a_share_unsupported"
        if a_rules.get("exclude_near_limit_up", True):
            max_change = to_float(a_rules.get("max_change_pct", 9.5))
            if max_change is not None and record.change_pct is not None and record.change_pct >= max_change:
                return "a_share_near_limit_up"
        max_amplitude = to_float(a_rules.get("max_amplitude_pct", 12.0))
        if max_amplitude is not None and record.amplitude_pct is not None and record.amplitude_pct >= max_amplitude:
            return "a_share_intraday_amplitude_too_high"
    elif record.market == "hk":
        if any(pattern in name for pattern in HK_NON_COMMON_PATTERNS):
            return "hk_non_common"
    elif record.market == "us":
        if any(pattern in name for pattern in US_NON_COMMON_PATTERNS):
            return "us_non_common"
        if re.search(r"(W|WS|WT|U|R)$", symbol) and len(symbol) > 3:
            return "us_non_common"

    return ""


def normalize_snapshot_row(row: dict[str, Any], market: str, config: dict[str, Any], source: str) -> StockRecord:
    symbol = first_value(row, ("代码", "symbol", "Symbol", "code", "raw_symbol", "yahoo_symbol")) or ""
    name = first_value(row, ("名称", "name", "Name", "简称")) or ""
    raw, yahoo, resolved_market, symbol_reason = normalize_symbol(str(symbol), market)
    currency = {"a-share": "CNY", "hk": "HKD", "us": "USD"}.get(resolved_market, "")

    volume = to_float(first_value(row, ("成交量", "volume", "Volume")))
    if resolved_market == "a-share" and volume is not None and a_share_volume_is_lots(source):
        volume *= 100
    price = to_float(first_value(row, ("最新价", "现价", "price", "Price", "lastPrice")))
    high = to_float(first_value(row, ("最高", "high", "High", "dayHigh")))
    low = to_float(first_value(row, ("最低", "low", "Low", "dayLow")))
    amplitude_pct = to_float(first_value(row, ("振幅", "amplitude_pct", "Amplitude", "amplitude")))
    close_position = close_position_from_snapshot(price, high, low)

    record = StockRecord(
        raw_symbol=raw,
        yahoo_symbol=yahoo,
        market=resolved_market,
        name=str(name).strip(),
        price=price,
        change_pct=to_float(first_value(row, ("涨跌幅", "change_pct", "Change Percent", "pctChange"))),
        volume=volume,
        turnover=to_float(first_value(row, ("成交额", "turnover", "Turnover", "amount"))),
        market_cap=to_float(first_value(row, ("总市值", "市值", "market_cap", "Market Cap", "marketCap"))),
        pe=to_float(first_value(row, ("市盈率", "市盈率-动态", "pe", "PE", "trailingPE"))),
        pb=to_float(first_value(row, ("市净率", "pb", "PB", "priceToBook"))),
        high=high,
        low=low,
        amplitude_pct=amplitude_pct,
        close_position=close_position,
        currency=currency,
        source=source,
        source_time=now_iso(),
        data_delay="delayed" if resolved_market == "hk" else "",
        exclude_reason=symbol_reason,
    )
    record.exclude_reason = detect_exclude_reason(record, config)
    record.is_tradeable = not bool(record.exclude_reason)
    return record


def a_share_volume_is_lots(source: str) -> bool:
    return source.startswith(("Eastmoney:", "cache:Eastmoney:", "AKShare:stock_zh_a_spot_em", "cache:AKShare:stock_zh_a_spot_em"))


def close_position_from_snapshot(price: float | None, high: float | None, low: float | None) -> float | None:
    if price is None or high is None or low is None or high <= low:
        return None
    return max(0.0, min(1.0, (price - low) / (high - low)))


def normalize_custom_symbol(symbol: str, market: str | None = None, name: str = "") -> StockRecord:
    raw, yahoo, resolved_market, reason = normalize_symbol(symbol, market)
    return StockRecord(
        raw_symbol=raw,
        yahoo_symbol=yahoo,
        market=resolved_market,
        name=name,
        currency={"a-share": "CNY", "hk": "HKD", "us": "USD"}.get(resolved_market, ""),
        source="custom",
        source_time=now_iso(),
        exclude_reason=reason,
        is_tradeable=not bool(reason),
    )
