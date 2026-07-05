"""Universe construction from auto providers, presets, and custom inputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import MARKETS, QualityLog, StockRecord
from .normalization import normalize_custom_symbol


SKILL_DIR = Path(__file__).resolve().parents[2]
PRESETS_PATH = SKILL_DIR / "config" / "presets.json"


def requested_markets(market: str) -> list[str]:
    if market == "all":
        return list(MARKETS)
    return [market]


def load_presets(path: Path = PRESETS_PATH) -> dict[str, list[dict[str, str]]]:
    return json.loads(path.read_text(encoding="utf-8"))


def preset_names_for_market(market: str) -> list[str]:
    if market == "all":
        return ["all_core"]
    if market == "a-share":
        return ["a_share_core"]
    if market == "hk":
        return ["hk_core"]
    if market == "us":
        return ["us_large_cap", "us_tech"]
    return []


def records_from_presets(market: str, preset_path: Path = PRESETS_PATH) -> list[StockRecord]:
    presets = load_presets(preset_path)
    records: list[StockRecord] = []
    seen: set[tuple[str, str]] = set()
    for preset_name in preset_names_for_market(market):
        for item in presets.get(preset_name, []):
            item_market = item.get("market") or market
            if market != "all" and item_market != market:
                continue
            record = normalize_custom_symbol(item["symbol"], item_market, item.get("name", ""))
            record.source = f"preset:{preset_name}"
            key = (record.market, record.yahoo_symbol or record.raw_symbol)
            if key not in seen:
                seen.add(key)
                records.append(record)
    return records


def parse_symbols(symbols: str | None) -> list[StockRecord]:
    if not symbols:
        return []
    records = []
    for symbol in symbols.split(","):
        symbol = symbol.strip()
        if symbol:
            records.append(normalize_custom_symbol(symbol))
    return records


def parse_symbols_file(path: str | None) -> list[StockRecord]:
    if not path:
        return []
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"symbols file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return []

    records: list[StockRecord] = []
    if "," in lines[0]:
        reader = csv.DictReader(lines)
        if reader.fieldnames:
            for row in reader:
                symbol = row.get("symbol") or row.get("ticker") or row.get("raw_symbol") or row.get("yahoo_symbol")
                if symbol:
                    records.append(normalize_custom_symbol(symbol, row.get("market") or None, row.get("name") or ""))
        else:
            for line in lines:
                parts = [part.strip() for part in line.split(",")]
                if parts and parts[0]:
                    records.append(normalize_custom_symbol(parts[0], parts[2] if len(parts) > 2 else None, parts[1] if len(parts) > 1 else ""))
    else:
        for line in lines:
            records.append(normalize_custom_symbol(line.strip()))
    return records


def dedupe_records(records: list[StockRecord]) -> list[StockRecord]:
    deduped: list[StockRecord] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.market, record.yahoo_symbol or record.raw_symbol)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def merge_universes(base: list[StockRecord], custom: list[StockRecord], mode: str) -> list[StockRecord]:
    base = dedupe_records(base)
    custom = dedupe_records(custom)
    custom_keys = {(record.market, record.yahoo_symbol or record.raw_symbol) for record in custom}
    if mode == "only":
        return custom
    if mode == "append":
        return dedupe_records(base + custom)
    if mode == "intersect":
        return [record for record in base if (record.market, record.yahoo_symbol or record.raw_symbol) in custom_keys]
    if mode == "exclude":
        return [record for record in base if (record.market, record.yahoo_symbol or record.raw_symbol) not in custom_keys]
    raise ValueError(f"unsupported custom mode: {mode}")


def filter_by_requested_market(records: list[StockRecord], market: str) -> list[StockRecord]:
    markets = set(requested_markets(market))
    return [record for record in records if record.market in markets]


def build_universe(
    *,
    market: str,
    universe: str,
    custom_mode: str,
    symbols: str | None,
    symbols_file: str | None,
    config: dict,
    providers_module,
    quality: QualityLog,
    no_cache: bool = False,
    live: bool = False,
) -> list[StockRecord]:
    custom_records = filter_by_requested_market(parse_symbols(symbols) + parse_symbols_file(symbols_file), market)
    base_records: list[StockRecord] = []

    if "auto" in universe:
        for requested_market in requested_markets(market):
            try:
                fetched = providers_module.fetch_market_snapshot(requested_market, config, quality, no_cache=no_cache, live=live)
                base_records.extend(fetched)
            except Exception as exc:
                quality.add_source_failure(requested_market, "akshare", exc)

    if "preset" in universe:
        base_records.extend(records_from_presets(market))

    if universe == "custom":
        return dedupe_records(custom_records)
    if universe in {"auto", "preset"}:
        return dedupe_records(base_records)
    if universe in {"auto+custom", "preset+custom"}:
        return merge_universes(base_records, custom_records, custom_mode)
    raise ValueError(f"unsupported universe: {universe}")
