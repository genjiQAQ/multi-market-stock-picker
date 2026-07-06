# Backtest Validation

Read this reference before changing backtest behavior, metrics, or output schemas.

## Purpose

Backtest mode validates the current scoring model with cached historical K-line data. It is a model diagnostic, not a trading system or performance promise.

## CLI

- `--backtest`: run cache-history based backtest instead of normal screening.
- `--backtest-start` / `--backtest-end`: optional ISO date bounds.
- `--backtest-window-days`: historical rows used for each evaluation point; defaults to `120`.
- `--backtest-hold-days`: forward rows used to evaluate later return and drawdown; defaults to `20`.
- `--backtest-frequency`: `daily`, `weekly`, or `monthly`; defaults to `weekly`.
- `--backtest-top-n`: TopN selected at each evaluation point; defaults to `--top-n`.

## Data Contract

V1 uses only cached `history` data. It does not reconstruct historical all-market snapshots and does not fabricate unavailable rows.

If a symbol has no cache, unparsable dates, insufficient lookback rows, or insufficient forward rows, write the issue into `backtest_quality.json` and skip that symbol/evaluation pair.

## Evaluation Flow

1. Build the requested universe from existing CLI inputs.
2. Load cached history for each normalized Yahoo-compatible ticker.
3. Build evaluation dates from cached trading dates and requested frequency.
4. For each date, score each symbol with only history available up to that date.
5. Select TopN and measure forward return plus max drawdown over `hold-days`.
6. Summarize overall, by market, and by industry.

## Outputs

- `backtest_report.md`
- `backtest_results.csv`
- `backtest_results.json`
- `backtest_quality.json`

Core metrics:

- `period_count`
- `selection_count`
- `avg_forward_return`
- `median_forward_return`
- `win_rate`
- `max_drawdown`
- `avg_topn_score`
- `low_coverage_ratio`
- `industry_concentration`
- `market_breakdown`

## Safety

Backtest results are for model validation only. Reports must state that past performance does not constitute investment advice or a future return guarantee.
