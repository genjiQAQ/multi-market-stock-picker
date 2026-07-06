# Output Schema

Read this reference before changing files, JSON keys, CSV columns, report wording, or exit codes.

## Files

Every run should write:

- `screening_report.md`
- `screening_results.csv`
- `screening_results.json`
- `top10_candidates.csv`
- `score_distribution.png`
- `top_candidates.png`
- `data_quality.json`

When there are no valid candidates, still write `screening_report.md`, `screening_results.csv`, `screening_results.json`, and `data_quality.json`. Charts may be omitted only if there are no numeric scores to render.

## CSV Columns

`screening_results.csv` and `top10_candidates.csv` must use UTF-8 and include:

```text
rank_global
rank_in_market
market
raw_symbol
yahoo_symbol
name
price
change_pct
turnover
market_cap
pe
pb
run_mode
result_level
industry
concepts
enrichment_status
momentum_score
quality_trend_score
liquidity_score
risk_control_score
final_score
data_coverage
quality_coverage
rating
watch_status
previous_rank
rank_change
previous_score
score_change
watch_runs
last_seen_at
reason
risk
watch_condition
invalidation
source
source_time
exclude_reason
```

## screening_results.json

Required top-level keys:

```text
schema_version
generated_at
market
style
top_n
max_candidates
run_mode
result_level
config
top10_global
top_per_market
results
```

Use `schema_version: "1.0"`.

## data_quality.json

Required top-level keys:

```text
schema_version
generated_at
sources
source_failures
symbol_failures
excluded_counts
missing_field_counts
cache_used
cache_expired
provider_breakers
run_mode
result_level
watchlist_state_path
watchlist_changes_count
backtest_mode
warnings
```

`excluded_counts` maps exclude reason to count. `missing_field_counts` maps normalized field names to missing counts.

## Report Structure

`screening_report.md` must contain:

- `# 多市场选股研究候选`
- `## 数据摘要`
- `## Top10 研究候选`
- `## 分市场结果`
- `## 数据质量和缺失字段`
- `## 免责声明`

Each Top10 row or bullet must include:

- ranking
- market
- ticker
- name
- final score
- rating
- result level
- reason
- risk
- watch condition
- invalidation

Allowed ratings:

- `重点跟踪`
- `积极观察`
- `需进一步验证`
- `中性观察`
- `暂不纳入`

Required disclaimer:

```text
本报告仅用于研究辅助，不构成投资建议。
```

## Watchlist Outputs

When `--watchlist` is enabled, write:

```text
watchlist_state.json
watchlist_changes.csv
watchlist_report.md
```

Allowed watch statuses:

- `新进入`
- `继续跟踪`
- `重点延续`
- `降级观察`
- `移出观察池`

## Backtest Outputs

When `--backtest` is enabled, write:

```text
backtest_report.md
backtest_results.csv
backtest_results.json
backtest_quality.json
```

Backtest mode does not need to write the normal screening report files.

## Exit Codes

- `0`: success with at least one candidate.
- `2`: successful run but no qualified candidate.
- `3`: all requested data sources unavailable.
- `4`: invalid CLI input or symbol file error.
- `5`: output generation failure.
