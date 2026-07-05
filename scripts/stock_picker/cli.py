"""Command-line entrypoint for multi-market stock picking."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import providers
from .charts import generate_charts
from .models import MARKETS, QualityLog, StockRecord
from .narrative import apply_ai_narratives
from .normalization import detect_exclude_reason
from .reporting import ensure_output_dir, write_outputs
from .scoring import score_records
from .universe import build_universe


SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = SKILL_DIR / "config" / "defaults.json"


EXIT_SUCCESS = 0
EXIT_NO_CANDIDATES = 2
EXIT_DATA_UNAVAILABLE = 3
EXIT_BAD_INPUT = 4
EXIT_OUTPUT_FAILED = 5
RUN_MODES = ("snapshot-only", "fast", "full")


def load_config(path: str | Path) -> dict:
    config_path = Path(path).expanduser()
    return json.loads(config_path.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Screen A-share, Hong Kong, and US stocks into Top10 research candidates.",
    )
    parser.add_argument("--market", choices=[*MARKETS, "all"], default=None, help="Market to screen")
    parser.add_argument(
        "--universe",
        choices=["auto", "preset", "custom", "auto+custom", "preset+custom"],
        default=None,
        help="Universe source",
    )
    parser.add_argument(
        "--custom-mode",
        choices=["only", "append", "intersect", "exclude"],
        default=None,
        help="How custom symbols participate when using +custom universe",
    )
    parser.add_argument("--symbols", help="Comma-separated symbols such as AAPL,MSFT,0700.HK,600519.SS")
    parser.add_argument("--symbols-file", help="CSV/TXT symbols file")
    parser.add_argument("--style", choices=["momentum", "quality-trend", "balanced"], default=None, help="Scoring style")
    parser.add_argument("--run-mode", choices=RUN_MODES, default=None, help="Depth mode: snapshot-only, fast, or full")
    parser.add_argument("--top-n", type=int, default=None, help="Number of candidates to highlight")
    parser.add_argument("--max-candidates", type=int, default=None, help="Maximum candidates to enrich after rough screening")
    parser.add_argument("--out-dir", help="Output directory")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config JSON path")
    parser.add_argument("--no-cache", action="store_true", help="Skip cached provider data")
    parser.add_argument("--live", action="store_true", help="Allow live enhanced provider calls")
    parser.add_argument("--ai-narrative", action="store_true", help="Use OPENAI_API_KEY to generate candidate narrative fields")
    parser.add_argument("--ai-model", default=None, help="Model used when --ai-narrative is enabled")
    parser.add_argument("--ai-base-url", default=None, help="OpenAI-compatible base URL used when --ai-narrative is enabled")
    parser.add_argument("--ai-narrative-limit", type=int, default=None, help="Maximum top candidates to rewrite with AI narrative")
    return parser.parse_args(argv)


def apply_defaults(args: argparse.Namespace, config: dict) -> argparse.Namespace:
    args.market = args.market or config.get("default_market", "all")
    args.universe = args.universe or config.get("default_universe", "auto")
    args.custom_mode = args.custom_mode or config.get("default_custom_mode", "append")
    args.style = args.style or config.get("default_style", "balanced")
    args.run_mode = args.run_mode or config.get("default_run_mode", "fast")
    args.top_n = args.top_n or int(config.get("default_top_n", 10))
    args.max_candidates = args.max_candidates or int(config.get("max_candidates", 200))
    ai_config = config.get("ai_narrative", {}) if isinstance(config.get("ai_narrative"), dict) else {}
    args.ai_model = args.ai_model or ai_config.get("model")
    args.ai_base_url = args.ai_base_url or ai_config.get("base_url")
    args.ai_narrative_limit = args.ai_narrative_limit or int(ai_config.get("limit", args.top_n))
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.top_n <= 0:
        raise ValueError("--top-n must be positive")
    if args.max_candidates <= 0:
        raise ValueError("--max-candidates must be positive")
    if args.run_mode not in RUN_MODES:
        raise ValueError(f"--run-mode must be one of: {', '.join(RUN_MODES)}")
    if args.ai_narrative_limit <= 0:
        raise ValueError("--ai-narrative-limit must be positive")
    if args.universe == "custom" and not (args.symbols or args.symbols_file):
        raise ValueError("--universe custom requires --symbols or --symbols-file")
    if "+custom" in args.universe and not (args.symbols or args.symbols_file):
        raise ValueError(f"--universe {args.universe} requires --symbols or --symbols-file")


def apply_filters(records: list[StockRecord], config: dict, quality: QualityLog) -> list[StockRecord]:
    filtered: list[StockRecord] = []
    for record in records:
        record.exclude_reason = detect_exclude_reason(record, config)
        record.is_tradeable = not bool(record.exclude_reason)
        if not record.is_tradeable:
            quality.add_excluded(record.exclude_reason)
        filtered.append(record)
    return filtered


def rough_screen(records: list[StockRecord], max_candidates: int) -> list[StockRecord]:
    tradable = [record for record in records if record.is_tradeable]
    tradable.sort(key=_rough_sort_key, reverse=True)
    return tradable[:max_candidates] + [record for record in records if not record.is_tradeable]


def _rough_sort_key(record: StockRecord) -> tuple[float, ...]:
    has_quote = 1.0 if record.price is not None else 0.0
    if record.market == "a-share":
        close_position = record.close_position if record.close_position is not None else 0.5
        amplitude_penalty = -(record.amplitude_pct or 0.0)
        return (
            record.turnover or 0.0,
            close_position,
            amplitude_penalty,
            record.market_cap or 0.0,
            record.change_pct or 0.0,
        )
    if record.market == "us":
        return (
            has_quote,
            record.turnover or 0.0,
            record.market_cap or 0.0,
            record.volume or 0.0,
            record.change_pct or 0.0,
        )
    return (
        record.turnover or 0.0,
        record.market_cap or 0.0,
        has_quote,
        record.change_pct or 0.0,
    )


def select_records_for_enrichment(
    records: list[StockRecord],
    run_mode: str,
    config: dict,
    max_candidates: int,
) -> list[StockRecord]:
    tradable = [record for record in records if record.is_tradeable]
    if run_mode == "snapshot-only":
        return []
    if run_mode == "fast":
        limit = min(max_candidates, int(config.get("fast_enrich_candidates", 30)))
    else:
        limit = max_candidates
    return tradable[: max(0, limit)]


def trim_to_enrichment_scope(records: list[StockRecord], targets: list[StockRecord], run_mode: str) -> list[StockRecord]:
    if run_mode == "snapshot-only":
        return records
    target_ids = {id(record) for record in targets}
    return [record for record in records if not record.is_tradeable or id(record) in target_ids]


def stamp_run_mode(records: list[StockRecord], run_mode: str) -> None:
    for record in records:
        record.run_mode = run_mode
        if run_mode == "snapshot-only" and record.is_tradeable:
            record.enrichment_status = "skipped_snapshot_only"


def seed_snapshot_history(records: list[StockRecord], quality: QualityLog) -> None:
    """Provide minimal synthetic history from snapshots for offline preset/custom scoring.

    The values are derived only from known snapshot fields and neutral defaults. This keeps
    custom/preset runs usable without inventing external data.
    """
    seeded = 0
    for record in records:
        if record.history or record.price is None:
            continue
        source = record.source or ""
        if not (source == "custom" or source.startswith("preset:")):
            continue
        base = record.price
        change = (record.change_pct or 0) / 100
        start = base / (1 + change) if change > -0.95 else base
        history = []
        for idx in range(130):
            progress = idx / 129
            close = start + (base - start) * progress
            history.append(
                {
                    "date": f"snapshot-{idx:03d}",
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": record.volume or 0,
                }
            )
        record.history = history
        record.fundamentals["_synthetic_history"] = True
        seeded += 1
    if seeded:
        quality.warnings.append(f"为 {seeded} 个 preset/custom 标的使用快照派生历史，仅用于离线粗筛。")


def run(args: argparse.Namespace, config: dict) -> tuple[int, dict[str, str]]:
    quality = QualityLog()
    quality.run_mode = args.run_mode
    records = build_universe(
        market=args.market,
        universe=args.universe,
        custom_mode=args.custom_mode,
        symbols=args.symbols,
        symbols_file=args.symbols_file,
        config=config,
        providers_module=providers,
        quality=quality,
        no_cache=args.no_cache,
        live=args.live,
    )
    records = apply_filters(records, config, quality)
    if not records:
        output_dir = ensure_output_dir(args.out_dir, args.market)
        paths = write_outputs(
            output_dir=output_dir,
            records=[],
            market=args.market,
            style=args.style,
            top_n=args.top_n,
            max_candidates=args.max_candidates,
            config=config,
            quality=quality,
            chart_paths={},
        )
        return EXIT_DATA_UNAVAILABLE if quality.source_failures else EXIT_NO_CANDIDATES, paths

    records = rough_screen(records, args.max_candidates)
    stamp_run_mode(records, args.run_mode)
    enrich_targets = select_records_for_enrichment(records, args.run_mode, config, args.max_candidates)
    records = trim_to_enrichment_scope(records, enrich_targets, args.run_mode)
    if args.run_mode == "snapshot-only":
        quality.result_level = "快照初筛"
    else:
        try:
            providers.enrich_records(enrich_targets, config, quality, no_cache=args.no_cache, run_mode=args.run_mode)
        except RuntimeError as exc:
            quality.warnings.append(str(exc))
    seed_snapshot_history(records, quality)
    score_records(records, args.style, quality)
    if args.ai_narrative:
        apply_ai_narratives(
            records,
            quality,
            top_n=min(args.top_n, args.ai_narrative_limit),
            config=config,
            model=args.ai_model,
            base_url=args.ai_base_url,
        )

    output_dir = ensure_output_dir(args.out_dir, args.market)
    try:
        chart_paths = generate_charts(records, output_dir, args.top_n)
        paths = write_outputs(
            output_dir=output_dir,
            records=records,
            market=args.market,
            style=args.style,
            top_n=args.top_n,
            max_candidates=args.max_candidates,
            config=config,
            quality=quality,
            chart_paths=chart_paths,
        )
    except Exception as exc:
        print(f"output generation failed: {exc}", file=sys.stderr)
        return EXIT_OUTPUT_FAILED, {}

    candidates = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    if not candidates:
        return EXIT_NO_CANDIDATES, paths
    return EXIT_SUCCESS, paths


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        config = load_config(args.config)
        args = apply_defaults(args, config)
        validate_args(args)
    except Exception as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    code, paths = run(args, config)
    if paths:
        print(json.dumps(paths, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
