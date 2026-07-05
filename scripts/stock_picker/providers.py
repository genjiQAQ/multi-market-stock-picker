"""External provider adapters with lazy imports and clear fallbacks."""

from __future__ import annotations

import json
import signal
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import QualityLog, StockRecord
from .normalization import normalize_snapshot_row


SKILL_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = SKILL_DIR / ".cache"


AKSHARE_FUNCTIONS = {
    "a-share": "stock_zh_a_spot_em",
    "hk": "stock_hk_spot_em",
    "us": "stock_us_spot_em",
}

A_SHARE_SNAPSHOT_FALLBACKS = (
    "stock_zh_a_spot_em",
    "stock_zh_a_spot",
)

EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
NASDAQ_OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
}


class SymbolEnrichmentTimeout(TimeoutError):
    """Raised when one symbol exceeds its configured enrichment budget."""


def _cache_path(kind: str, key: str) -> Path:
    safe_key = key.replace("/", "_").replace(".", "_")
    return CACHE_DIR / kind / f"{safe_key}.json"


def _cache_ttl_hours(config: dict[str, Any], kind: str) -> float | None:
    ttl_config = config.get("cache_ttl_hours", {})
    if not isinstance(ttl_config, dict) or kind not in ttl_config:
        return None
    try:
        return float(ttl_config[kind])
    except Exception:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_cache_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_cache(kind: str, key: str, quality: QualityLog, ttl_hours: float | None = None) -> Any | None:
    path = _cache_path(kind, key)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    cache_id = f"{kind}:{key}"
    if isinstance(raw, dict) and "payload" in raw and "fetched_at" in raw:
        fetched_at = _parse_cache_time(raw.get("fetched_at"))
        if ttl_hours is not None and fetched_at is not None:
            age_hours = (_utc_now() - fetched_at).total_seconds() / 3600
            if age_hours > ttl_hours:
                quality.cache_expired.append(
                    {
                        "kind": kind,
                        "key": key,
                        "path": str(path),
                        "age_hours": round(age_hours, 2),
                        "ttl_hours": ttl_hours,
                    }
                )
                return None
        elif ttl_hours is not None and fetched_at is None:
            quality.warnings.append(f"缓存时间无法解析，按可用缓存读取：{kind}:{key}")
        quality.cache_used[cache_id] = True
        return raw.get("payload")

    quality.cache_used[cache_id] = True
    quality.warnings.append(f"使用旧版 {kind} 缓存格式，建议下次联网刷新：{key}")
    return raw


def write_cache(kind: str, key: str, payload: Any, *, source: str | None = None) -> None:
    path = _cache_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "fetched_at": _utc_now().isoformat(timespec="seconds"),
        "payload": payload,
    }
    if source:
        envelope["source"] = source
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def fetch_market_snapshot(
    market: str,
    config: dict,
    quality: QualityLog,
    *,
    no_cache: bool = False,
    live: bool = False,
) -> list[StockRecord]:
    cache_key = f"snapshot_{market}"
    if not no_cache:
        cached = read_cache("market_snapshots", cache_key, quality, _cache_ttl_hours(config, "market_snapshots"))
        if cached:
            if isinstance(cached, dict) and "rows" in cached:
                cached_rows = cached.get("rows") or []
                cached_source = f"cache:{cached.get('source') or 'unknown'}"
            else:
                cached_rows = cached
                cached_source = "cache:unknown"
                quality.warnings.append("使用旧版快照缓存，原始数据源未知，成交量单位可能需要复核。")
            return [normalize_snapshot_row(row, market, config, cached_source) for row in cached_rows]

    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Missing Python package: akshare. Run scripts/run_stock_picker.py --setup") from exc

    rows: list[dict[str, Any]] = []
    source = ""
    failures: list[str] = []
    if market == "a-share":
        for func_name in A_SHARE_SNAPSHOT_FALLBACKS:
            try:
                rows = _call_akshare_snapshot(ak, func_name)
                source = f"AKShare:{func_name}"
                break
            except Exception as exc:
                failures.append(f"{func_name}: {exc}")
        if not rows:
            try:
                rows = fetch_eastmoney_a_share_snapshot(config)
                source = "Eastmoney:qt_clist"
            except Exception as exc:
                failures.append(f"Eastmoney:qt_clist: {exc}")
        for failure in failures:
            quality.warnings.append(f"A股快照源回退记录：{failure}")
    else:
        func_name = AKSHARE_FUNCTIONS[market]
        try:
            rows = _call_akshare_snapshot(ak, func_name)
            source = f"AKShare:{func_name}"
        except Exception as exc:
            if market != "us":
                raise
            failures.append(f"{func_name}: {exc}")
            try:
                rows = fetch_nasdaq_trader_us_directory(config)
                source = "NasdaqTrader:SymbolDirectory"
            except Exception as fallback_exc:
                failures.append(f"NasdaqTrader:SymbolDirectory: {fallback_exc}")
                raise RuntimeError("; ".join(failures)) from fallback_exc
            for failure in failures:
                quality.warnings.append(f"美股快照源回退记录：{failure}")

    if not rows:
        raise RuntimeError("; ".join(failures) if failures else f"empty snapshot for {market}")
    quality.sources[market] = source
    if not no_cache:
        write_cache("market_snapshots", cache_key, {"source": source, "rows": rows}, source=source)
    return [normalize_snapshot_row(row, market, config, source) for row in rows]


def _call_akshare_snapshot(ak: Any, func_name: str) -> list[dict[str, Any]]:
    func = getattr(ak, func_name)
    data = func()
    try:
        return data.to_dict(orient="records")
    except AttributeError:
        return list(data)


def fetch_nasdaq_trader_us_directory(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(_parse_nasdaq_symbol_directory(_http_text(NASDAQ_LISTED_URL, config), listed=True))
    rows.extend(_parse_nasdaq_symbol_directory(_http_text(NASDAQ_OTHER_LISTED_URL, config), listed=False))
    return rows


def _parse_nasdaq_symbol_directory(text: str, *, listed: bool) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    headers = lines[0].split("|")
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        values = line.split("|")
        if len(values) != len(headers):
            continue
        item = dict(zip(headers, values, strict=False))
        symbol = item.get("Symbol") if listed else item.get("ACT Symbol")
        name = item.get("Security Name") or ""
        if not symbol:
            continue
        if item.get("Test Issue") == "Y" or item.get("ETF") == "Y" or item.get("NextShares") == "Y":
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": "Q" if listed else item.get("Exchange"),
            }
        )
    return rows


def fetch_eastmoney_a_share_snapshot(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    rows: list[dict[str, Any]] = []
    page = 1
    page_size = 80
    total: int | None = None
    while True:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f6",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f5,f6,f7,f15,f16,f20,f9,f23",
        }
        payload = _http_json(
            EASTMONEY_CLIST_URL,
            params,
            retries=_provider_max_retries(config),
            timeout=_provider_timeout_seconds(config),
        )
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        if total is None:
            total = int(data.get("total") or 0)
        if not diff:
            break
        rows.extend(_normalize_eastmoney_snapshot_row(item) for item in diff)
        if total and len(rows) >= total:
            break
        page += 1
        time.sleep(0.05)
    return rows


def _normalize_eastmoney_snapshot_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "代码": item.get("f12"),
        "名称": item.get("f14"),
        "最新价": item.get("f2"),
        "涨跌幅": item.get("f3"),
        "成交量": item.get("f5"),
        "成交额": item.get("f6"),
        "振幅": item.get("f7"),
        "最高": item.get("f15"),
        "最低": item.get("f16"),
        "总市值": item.get("f20"),
        "市盈率-动态": item.get("f9"),
        "市净率": item.get("f23"),
    }


def fetch_history(record: StockRecord, config: dict, quality: QualityLog, *, no_cache: bool = False) -> list[dict[str, Any]]:
    symbol = record.yahoo_symbol
    if not symbol:
        return []
    if not no_cache:
        cached = read_cache("history", symbol, quality, _cache_ttl_hours(config, "history"))
        if cached is not None:
            return cached
    if record.market == "a-share":
        rows = fetch_eastmoney_a_share_history(record, config)
        if not rows:
            quality.add_symbol_failure(symbol, record.market, "empty Eastmoney history")
            return []
        if not no_cache:
            write_cache("history", symbol, rows, source="Eastmoney:kline")
        return rows

    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        quality.add_symbol_failure(symbol, record.market, "Missing Python package: yfinance")
        return []

    history = None
    last_error: Exception | None = None
    for attempt in range(_provider_max_retries(config)):
        try:
            _sleep_before_provider_request(config)
            history = yf.Ticker(symbol).history(period=config.get("history_period", "9mo"), auto_adjust=False)
            break
        except Exception as exc:
            if isinstance(exc, SymbolEnrichmentTimeout):
                raise
            last_error = exc
            if attempt < _provider_max_retries(config) - 1:
                _sleep_after_provider_failure(config, attempt)
    if history is None:
        quality.add_symbol_failure(symbol, record.market, last_error or "history request failed")
        return []
    if history is None or history.empty:
        quality.add_symbol_failure(symbol, record.market, "empty history")
        return []
    rows = []
    for idx, row in history.reset_index().iterrows():
        rows.append(
            {
                "date": str(row.get("Date") or row.get("Datetime") or idx),
                "open": _clean_number(row.get("Open")),
                "high": _clean_number(row.get("High")),
                "low": _clean_number(row.get("Low")),
                "close": _clean_number(row.get("Close")),
                "volume": _clean_number(row.get("Volume")),
            }
        )
    if not no_cache:
        write_cache("history", symbol, rows, source="yfinance")
    return rows


def fetch_eastmoney_a_share_history(record: StockRecord, config: dict) -> list[dict[str, Any]]:
    params = {
        "secid": _eastmoney_secid(record.raw_symbol),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 1,
        "beg": _history_start_date(config),
        "end": datetime.now().strftime("%Y%m%d"),
    }
    payload = _http_json(
        EASTMONEY_KLINE_URL,
        params,
        retries=_provider_max_retries(config),
        timeout=_provider_timeout_seconds(config),
    )
    klines = ((payload.get("data") or {}).get("klines") or [])
    rows: list[dict[str, Any]] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": _clean_number(parts[1]),
                "close": _clean_number(parts[2]),
                "high": _clean_number(parts[3]),
                "low": _clean_number(parts[4]),
                "volume": _clean_number(parts[5]),
                "turnover": _clean_number(parts[6]),
            }
        )
    return rows


def _history_start_date(config: dict) -> str:
    period = str(config.get("history_period", "9mo"))
    if period.endswith("mo") and period[:-2].isdigit():
        days = int(period[:-2]) * 31
    elif period.endswith("y") and period[:-1].isdigit():
        days = int(period[:-1]) * 366
    else:
        days = 280
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _eastmoney_secid(raw_symbol: str) -> str:
    if raw_symbol.startswith(("600", "601", "603", "605", "688")):
        return f"1.{raw_symbol}"
    return f"0.{raw_symbol}"


def _http_json(url: str, params: dict[str, Any], *, retries: int = 3, timeout: int = 20) -> dict[str, Any]:
    requests_error: Exception | None = None
    try:
        import requests  # type: ignore
    except Exception:
        requests = None
    if requests is not None:
        for attempt in range(retries):
            try:
                response = requests.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=timeout)
                response.raise_for_status()
                text = response.text.strip()
                if not text:
                    raise RuntimeError("empty response")
                return response.json()
            except Exception as exc:
                if isinstance(exc, SymbolEnrichmentTimeout):
                    raise
                requests_error = exc
                time.sleep(0.25 * (attempt + 1))

    query = urlencode(params)
    request = Request(f"{url}?{query}", headers=EASTMONEY_HEADERS)
    context = _ssl_context()
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout, context=context) as response:
                text = response.read().decode("utf-8").strip()
            if not text:
                raise RuntimeError("empty response")
            return json.loads(text)
        except Exception as exc:
            if isinstance(exc, SymbolEnrichmentTimeout):
                raise
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    if requests_error is not None and last_error is not None:
        raise RuntimeError(f"{requests_error}; urllib fallback: {last_error}")
    raise RuntimeError(str(last_error))


def _http_text(url: str, config: dict[str, Any]) -> str:
    timeout = int(config.get("request_timeout_seconds", 30))
    retries = _provider_max_retries(config)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/plain,*/*",
    }
    requests_error: Exception | None = None
    try:
        import requests  # type: ignore
    except Exception:
        requests = None
    if requests is not None:
        for attempt in range(retries):
            try:
                _sleep_before_provider_request(config)
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                text = response.text.strip()
                if not text:
                    raise RuntimeError("empty response")
                return text
            except Exception as exc:
                if isinstance(exc, SymbolEnrichmentTimeout):
                    raise
                requests_error = exc
                if attempt < retries - 1:
                    _sleep_after_provider_failure(config, attempt)

    request = Request(url, headers=headers)
    context = _ssl_context()
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            _sleep_before_provider_request(config)
            with urlopen(request, timeout=timeout, context=context) as response:
                text = response.read().decode("utf-8").strip()
            if not text:
                raise RuntimeError("empty response")
            return text
        except Exception as exc:
            if isinstance(exc, SymbolEnrichmentTimeout):
                raise
            last_error = exc
            if attempt < retries - 1:
                _sleep_after_provider_failure(config, attempt)
    if requests_error is not None and last_error is not None:
        raise RuntimeError(f"{requests_error}; urllib fallback: {last_error}")
    raise RuntimeError(str(last_error))


def _ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore
    except Exception:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def fetch_fundamentals(record: StockRecord, config: dict, quality: QualityLog, *, no_cache: bool = False) -> dict[str, Any]:
    symbol = record.yahoo_symbol
    if not symbol:
        return {}
    if not no_cache:
        cached = read_cache("fundamentals", symbol, quality, _cache_ttl_hours(config, "fundamentals"))
        if cached is not None:
            return cached
    if record.market == "a-share":
        payload = {
            key: value
            for key, value in {
                "marketCap": record.market_cap,
                "trailingPE": record.pe,
                "priceToBook": record.pb,
            }.items()
            if value is not None
        }
        payload.update(fetch_a_share_industry_concepts(record, quality))
        if not no_cache:
            write_cache("fundamentals", symbol, payload, source="AKShare:stock_individual_info_em")
        return payload
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        quality.add_symbol_failure(symbol, record.market, "Missing Python package: yfinance")
        return {}

    info: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(_provider_max_retries(config)):
        try:
            _sleep_before_provider_request(config)
            info = yf.Ticker(symbol).info or {}
            break
        except Exception as exc:
            if isinstance(exc, SymbolEnrichmentTimeout):
                raise
            last_error = exc
            if attempt < _provider_max_retries(config) - 1:
                _sleep_after_provider_failure(config, attempt)
    if info is None:
        quality.add_symbol_failure(symbol, record.market, last_error or "fundamentals request failed")
        return {}
    payload = {
        "marketCap": info.get("marketCap"),
        "trailingPE": info.get("trailingPE"),
        "priceToBook": info.get("priceToBook"),
        "profitMargins": info.get("profitMargins"),
        "revenueGrowth": info.get("revenueGrowth"),
        "earningsGrowth": info.get("earningsGrowth"),
        "operatingCashflow": info.get("operatingCashflow"),
        "totalDebt": info.get("totalDebt"),
        "returnOnEquity": info.get("returnOnEquity"),
    }
    if not no_cache:
        write_cache("fundamentals", symbol, payload, source="yfinance")
    return payload


def fetch_a_share_industry_concepts(record: StockRecord, quality: QualityLog) -> dict[str, Any]:
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return {}
    try:
        data = ak.stock_individual_info_em(symbol=record.raw_symbol)
        try:
            rows = data.to_dict(orient="records")
        except AttributeError:
            rows = list(data)
    except Exception as exc:
        if isinstance(exc, SymbolEnrichmentTimeout):
            raise
        quality.warnings.append(f"A股行业/概念字段获取失败：{record.raw_symbol}: {exc}")
        return {}

    industry = ""
    concepts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("item") or row.get("项目") or row.get("指标") or row.get("Item") or "")
        value = row.get("value")
        if value is None:
            value = row.get("值") or row.get("Value")
        text = str(value or "").strip()
        if not text:
            continue
        if not industry and ("行业" in key or key.lower() == "industry"):
            industry = text
        if "概念" in key or "板块" in key or key.lower() in {"concept", "concepts"}:
            concepts.extend(_split_concepts(text))
    payload: dict[str, Any] = {}
    if industry:
        payload["industry"] = industry
    if concepts:
        payload["concepts"] = sorted(set(concepts))
    return payload


def _split_concepts(value: str) -> list[str]:
    parts = []
    for item in re_split_concepts(value):
        text = item.strip()
        if text:
            parts.append(text)
    return parts


def re_split_concepts(value: str) -> list[str]:
    separators = [";", "；", ",", "，", "|", "/", "、"]
    parts = [value]
    for separator in separators:
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts
    return parts


def enrich_records(
    records: list[StockRecord],
    config: dict,
    quality: QualityLog,
    *,
    no_cache: bool = False,
    run_mode: str | None = None,
) -> list[StockRecord]:
    if run_mode == "snapshot-only":
        for record in records:
            if record.is_tradeable:
                record.enrichment_status = "skipped_snapshot_only"
        return records

    timeout_seconds = _symbol_timeout_seconds(config)
    threshold = _provider_breaker_threshold(config)
    cooldown_seconds = _provider_breaker_cooldown_seconds(config)
    breaker_state: dict[str, dict[str, Any]] = {}

    for record in records:
        if not record.is_tradeable:
            continue
        provider = _provider_name_for_record(record)
        state = breaker_state.setdefault(provider, {"consecutive_failures": 0, "opened_until": 0.0, "reported": False})
        now = time.time()
        if state["opened_until"] and now < state["opened_until"]:
            record.enrichment_status = "skipped_provider_breaker"
            continue

        try:
            enriched = _run_enrichment_with_timeout(record, config, quality, no_cache=no_cache, timeout_seconds=timeout_seconds)
            _copy_enriched_fields(record, enriched)
            record.enrichment_status = _enrichment_status(record)
            if record.enrichment_status == "failed":
                _record_provider_failure(provider, state, threshold, cooldown_seconds, quality)
            else:
                state["consecutive_failures"] = 0
        except (FutureTimeoutError, SymbolEnrichmentTimeout):
            record.enrichment_status = "timeout"
            quality.add_symbol_failure(record.yahoo_symbol or record.raw_symbol, record.market, f"{provider} enrichment timeout after {timeout_seconds}s")
            _record_provider_failure(provider, state, threshold, cooldown_seconds, quality)
        except Exception as exc:
            record.enrichment_status = "failed"
            quality.add_symbol_failure(record.yahoo_symbol or record.raw_symbol, record.market, exc)
            _record_provider_failure(provider, state, threshold, cooldown_seconds, quality)
    return records


def _run_enrichment_with_timeout(
    record: StockRecord,
    config: dict,
    quality: QualityLog,
    *,
    no_cache: bool,
    timeout_seconds: float,
) -> StockRecord:
    if timeout_seconds <= 0:
        return _enrich_record_once(record, config, quality, no_cache=no_cache)
    if _can_use_signal_timeout():
        previous_handler = signal.getsignal(signal.SIGALRM)

        def _raise_timeout(_signum, _frame) -> None:
            raise SymbolEnrichmentTimeout()

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return _enrich_record_once(record, config, quality, no_cache=no_cache)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_enrich_record_once, record, config, quality, no_cache=no_cache)
    try:
        return future.result(timeout=timeout_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _can_use_signal_timeout() -> bool:
    return (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
    )


def _enrich_record_once(record: StockRecord, config: dict, quality: QualityLog, *, no_cache: bool = False) -> StockRecord:
    enriched = _copy_record_for_enrichment(record)
    enriched.history = fetch_history(enriched, config, quality, no_cache=no_cache)
    if enriched.price is None and enriched.history:
        latest = enriched.history[-1]
        previous = enriched.history[-2] if len(enriched.history) > 1 else None
        enriched.price = _clean_number(latest.get("close"))
        if enriched.volume is None:
            enriched.volume = _clean_number(latest.get("volume"))
        if enriched.turnover is None and enriched.price is not None and enriched.volume is not None:
            enriched.turnover = enriched.price * enriched.volume
        if enriched.change_pct is None and previous and previous.get("close"):
            previous_close = _clean_number(previous.get("close"))
            if previous_close:
                enriched.change_pct = ((enriched.price or 0) / previous_close - 1) * 100

    enriched.fundamentals = fetch_fundamentals(enriched, config, quality, no_cache=no_cache)
    _apply_fundamentals(enriched)
    return enriched


def _copy_record_for_enrichment(record: StockRecord) -> StockRecord:
    return replace(
        record,
        history=list(record.history),
        fundamentals=dict(record.fundamentals),
        indicators=dict(record.indicators),
        scores=dict(record.scores),
        concepts=list(record.concepts),
    )


def _copy_enriched_fields(target: StockRecord, source: StockRecord) -> None:
    target.price = source.price
    target.change_pct = source.change_pct
    target.volume = source.volume
    target.turnover = source.turnover
    target.market_cap = source.market_cap
    target.pe = source.pe
    target.pb = source.pb
    target.history = source.history
    target.fundamentals = source.fundamentals
    target.industry = source.industry
    target.concepts = source.concepts


def _apply_fundamentals(record: StockRecord) -> None:
    market_cap = _clean_number(record.fundamentals.get("marketCap"))
    trailing_pe = _clean_number(record.fundamentals.get("trailingPE"))
    price_to_book = _clean_number(record.fundamentals.get("priceToBook"))
    if record.market_cap is None and market_cap is not None:
        record.market_cap = market_cap
    if record.pe is None and trailing_pe is not None:
        record.pe = trailing_pe
    if record.pb is None and price_to_book is not None:
        record.pb = price_to_book
    if record.fundamentals.get("industry"):
        record.industry = str(record.fundamentals["industry"])
    concepts = record.fundamentals.get("concepts")
    if isinstance(concepts, list):
        record.concepts = [str(item) for item in concepts if str(item).strip()]
    elif isinstance(concepts, str) and concepts.strip():
        record.concepts = _split_concepts(concepts)


def _enrichment_status(record: StockRecord) -> str:
    has_history = bool(record.history)
    has_fundamentals = bool(record.fundamentals)
    if has_history and has_fundamentals:
        return "complete"
    if has_history or has_fundamentals:
        return "partial"
    return "failed"


def _provider_name_for_record(record: StockRecord) -> str:
    if record.market == "a-share":
        return "eastmoney_a_share"
    return "yfinance"


def _record_provider_failure(
    provider: str,
    state: dict[str, Any],
    threshold: int,
    cooldown_seconds: float,
    quality: QualityLog,
) -> None:
    state["consecutive_failures"] += 1
    if state["consecutive_failures"] < threshold:
        return
    state["opened_until"] = time.time() + cooldown_seconds
    if state.get("reported"):
        return
    quality.provider_breakers.append(
        {
            "provider": provider,
            "threshold": threshold,
            "cooldown_seconds": cooldown_seconds,
            "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    state["reported"] = True


def _symbol_timeout_seconds(config: dict[str, Any]) -> float:
    try:
        return max(0.0, float(config.get("symbol_timeout_seconds", 20)))
    except Exception:
        return 20.0


def _provider_breaker_threshold(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get("provider_failure_breaker_threshold", 8)))
    except Exception:
        return 8


def _provider_breaker_cooldown_seconds(config: dict[str, Any]) -> float:
    try:
        return max(0.0, float(config.get("provider_failure_cooldown_seconds", 300)))
    except Exception:
        return 300.0


def _clean_number(value: Any) -> float | None:
    try:
        if value != value:
            return None
        return float(value)
    except Exception:
        return None


def _provider_max_retries(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get("max_retries", 4)))
    except Exception:
        return 4


def _provider_timeout_seconds(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get("request_timeout_seconds", 30)))
    except Exception:
        return 30


def _sleep_before_provider_request(config: dict[str, Any]) -> None:
    try:
        interval = float(config.get("provider_request_interval_seconds", 0))
    except Exception:
        interval = 0
    if interval > 0:
        time.sleep(interval)


def _sleep_after_provider_failure(config: dict[str, Any], attempt: int) -> None:
    try:
        base = float(config.get("provider_retry_base_delay_seconds", 2.0))
    except Exception:
        base = 2.0
    try:
        max_delay = float(config.get("provider_retry_max_delay_seconds", 30.0))
    except Exception:
        max_delay = 30.0
    delay = min(max_delay, base * (2 ** attempt))
    if delay > 0:
        time.sleep(delay)
