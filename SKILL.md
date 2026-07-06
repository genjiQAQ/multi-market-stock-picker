---
name: multi-market-stock-picker
description: Use when selecting or screening stocks across A-share, US, and Hong Kong markets, including A股选股, 美股选股, 港股选股, 股票池筛选, 自动选股, 批量股票分析, 短线强势股, 中线趋势股, Top10 候选, 可购买股票研究候选, multi-market stock picking, stock screener, ranking, factor scoring, and watchlist generation.
---

# Multi-Market Stock Picker

Use this skill to build a research candidate list from A-share, US, and Hong Kong stocks. It creates a market universe from public data or user-provided symbols, ranks candidates with momentum and quality-trend signals, and writes a Chinese Top10 screening report with CSV/JSON data and PNG charts.

This skill is independent from `$stock-data-analysis`. Do not import, modify, or rely on files from that skill. If a user wants a deep single-stock report after screening, tell them to run `$stock-data-analysis` separately for the selected ticker.

This skill provides research assistance only. It does not provide financial advice, trading instructions, guaranteed returns, or unconditional buy/sell orders.

## Quick Start

First-time setup creates a skill-local virtual environment declared in `config/defaults.json`:

```bash
python3 ~/.codex/skills/multi-market-stock-picker/scripts/run_stock_picker.py --setup
```

Run the default balanced picker:

```bash
python3 ~/.codex/skills/multi-market-stock-picker/scripts/run_stock_picker.py \
  --market all \
  --universe auto \
  --style balanced \
  --top-n 10
```

Run with AI-generated candidate narratives when `OPENAI_API_KEY` is available:

```bash
python3 ~/.codex/skills/multi-market-stock-picker/scripts/run_stock_picker.py \
  --market a-share \
  --universe auto \
  --style balanced \
  --top-n 10 \
  --ai-narrative
```

Run on a custom list:

```bash
python3 ~/.codex/skills/multi-market-stock-picker/scripts/run_stock_picker.py \
  --market all \
  --universe custom \
  --custom-mode only \
  --symbols AAPL,MSFT,0700.HK,600519.SS \
  --style balanced
```

## Workflow

1. Read `references/data-sources.md` before changing providers, ticker conversion, market filters, or auto-universe behavior.
2. Read `references/scoring-model.md` before changing scoring weights, factor formulas, missing-data rules, or cross-market ranking.
3. Read `references/output-schema.md` before changing CSV/JSON/report fields or exit codes.
4. Read `references/watchlist-spec.md` before changing persistent candidate tracking.
5. Read `references/backtest-spec.md` before changing backtest behavior, metrics, or schemas.
6. Prefer the bundled runner so the caller project does not need its own finance Python stack.
7. Use actual fetched, cached, or user-provided data only; do not invent missing prices, fundamentals, volumes, histories, or rankings.

## Inputs

- `--market`: `a-share`, `us`, `hk`, or `all`.
- `--universe`: `auto`, `preset`, `custom`, `auto+custom`, or `preset+custom`.
- `--custom-mode`: `only`, `append`, `intersect`, or `exclude`.
- `--symbols`: comma-separated tickers such as `AAPL,MSFT,0700.HK,600519.SS`.
- `--symbols-file`: CSV/TXT symbol file. CSV may contain `symbol,name,market`; TXT may contain one symbol per line.
- `--style`: `momentum`, `quality-trend`, or `balanced`.
- `--run-mode`: `snapshot-only`, `fast`, or `full`; defaults to `fast`. `snapshot-only` skips history/fundamental enrichment, `fast` enriches only the rough-screened TopN, and `full` enriches up to `--max-candidates`.
- `--top-n`: number of research candidates to highlight; defaults to `10`.
- `--max-candidates`: maximum rough-screened candidates to enrich with history/fundamentals; defaults to `200`.
- `--out-dir`: output directory. If omitted, reports save to `~/Desktop/stock-picker-output/<market>/<timestamp>/` on macOS/Linux and `%USERPROFILE%\Desktop\stock-picker-output\<market>\<timestamp>\` on Windows.
- `--no-cache`: skip cached market snapshots, histories, and fundamentals.
- `--live`: allow live optional integration checks and enhanced real-time provider calls.
- `--watchlist`: update persistent candidate tracking and write watchlist artifacts.
- `--watchlist-name`, `--watchlist-state-dir`, `--watchlist-lookback-runs`: optional watchlist namespace, state directory, and stale-run threshold.
- `--backtest`: run cache-history based backtest instead of normal screening.
- `--backtest-start`, `--backtest-end`, `--backtest-window-days`, `--backtest-hold-days`, `--backtest-frequency`, `--backtest-top-n`: optional backtest controls.
- `--ai-narrative`: use `OPENAI_API_KEY` to generate per-candidate `reason`, `risk`, `watch_condition`, and `invalidation` text from structured scoring inputs. If unavailable or invalid, the runner records a warning and keeps deterministic fallback text.
- `--ai-model`, `--ai-base-url`, `--ai-narrative-limit`: optional AI narrative model, OpenAI-compatible endpoint, and maximum number of top candidates to rewrite.

Run `scripts/run_stock_picker.py --help` when unsure about flags.

## Outputs

Expected artifacts:

- `screening_report.md`
- `screening_results.csv`
- `screening_results.json`
- `top10_candidates.csv`
- `score_distribution.png`
- `top_candidates.png`
- `data_quality.json`

When `--watchlist` is enabled, also write:

- `watchlist_state.json`
- `watchlist_changes.csv`
- `watchlist_report.md`

When `--backtest` is enabled, write:

- `backtest_report.md`
- `backtest_results.csv`
- `backtest_results.json`
- `backtest_quality.json`

For `--market all`, write both `top10_global` and `top_per_market` into JSON and explain in the report that cross-market scores are research rankings, not direct investment priority across currencies, sessions, data delays, and valuation coverage.

## Analysis Rules

- Frame conclusions as research candidates: `重点跟踪`, `积极观察`, `需进一步验证`, `中性观察`, or `暂不纳入`.
- Do not use deterministic buy wording, guaranteed return language, unconditional position sizing, or direct trading commands.
- All scores are better when higher. `risk_control_score` means risk is more controlled when higher.
- Missing data must lower `data_coverage` or `quality_coverage` and be recorded in `data_quality.json`; do not fabricate or silently ignore it.
- Single ticker failures must not stop the whole run. Single market failures must not stop other markets.
- Chinese reports must include: `本报告仅用于研究辅助，不构成投资建议。`

## Configuration

- `config/defaults.json`: runtime defaults, cache TTLs, filters, thresholds, provider retry pacing, venv, and requirements file.
- `config/presets.json`: static preset symbol pools only; do not store dynamic quote data here.
- `requirements.txt`: runtime dependencies owned by this skill.
- `requirements-dev.txt`: validation/test dependencies for this skill.

## Validation

Use offline tests by default:

```bash
PYTHONPATH=~/.codex/skills/multi-market-stock-picker/scripts \
python3 -m unittest discover -s ~/.codex/skills/multi-market-stock-picker/tests
```

Live data tests, if added, must require explicit opt-in:

```bash
RUN_LIVE_MARKET_TESTS=1 pytest
```

Validate skill structure:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  ~/.codex/skills/multi-market-stock-picker
```

## Disclaimer

This skill is for research support only. It is not investment advice, trading instruction, or a guarantee of future returns. Investment decisions require independent judgment and, when appropriate, a qualified financial professional.
