# Scoring Model

Read this reference before changing factor calculations, weights, ranking behavior, or missing-data handling.

## Score Direction

All scores are on a 0-100 scale and higher is better:

- `momentum_score`: stronger short-term price/volume structure.
- `quality_trend_score`: better quality, valuation coverage, and medium-term trend stability.
- `liquidity_score`: better tradability.
- `risk_control_score`: more controlled volatility/drawdown risk.
- `final_score`: weighted composite.

`risk_control_score` is not a risk amount. Higher means risk is more controlled.

## Styles And Weights

`balanced`:

- `momentum_score`: 40%
- `quality_trend_score`: 40%
- `liquidity_score`: 10%
- `risk_control_score`: 10%

`momentum`:

- `momentum_score`: 60%
- `quality_trend_score`: 15%
- `liquidity_score`: 15%
- `risk_control_score`: 10%

`quality-trend`:

- `momentum_score`: 25%
- `quality_trend_score`: 50%
- `liquidity_score`: 10%
- `risk_control_score`: 15%

If a score component cannot be computed, reweight the available components proportionally and lower `data_coverage`.

## Normalization

- Use percentile ranking inside each market for cross-sectional snapshot factors.
- Clamp all component scores to `[0, 100]`.
- For positive factors, higher raw value means higher score.
- For negative factors, lower raw value means higher score.
- For single-row custom universes, use neutral percentile values where cross-sectional comparisons are impossible.

## Momentum Factors

Compute from history when available:

- 5-day return.
- 20-day return.
- 60-day return.
- Price relative to SMA 20, 60, and 120.
- Latest volume versus 20-day average volume.
- RSI 14, favoring constructive but not extremely overbought levels.
- MACD minus signal.
- ATR as a percentage of close.
- Maximum drawdown over available history.
- 20-day annualized volatility.

Suggested deterministic formula:

```text
trend = average(percentile(return_5d), percentile(return_20d), percentile(return_60d))
ma_structure = average(score(close/sma20), score(close/sma60), score(close/sma120))
volume_confirm = percentile(volume_ratio_20d)
rsi_score = 100 - abs(rsi_14 - 58) * 2, clamped to 0-100
macd_score = percentile(macd_histogram)
drawdown_penalty = percentile(max_drawdown, negative factor)
volatility_penalty = percentile(volatility_20d, negative factor)
momentum_score = weighted average:
  trend 30%, ma_structure 20%, volume_confirm 15%, rsi_score 10%,
  macd_score 10%, drawdown_penalty 10%, volatility_penalty 5%
```

When history is unavailable for live/auto provider rows, do not synthesize a full technical history. For preset/custom offline rows only, a snapshot-derived history may be used for rough screening, must be marked, and must cap `data_coverage`, momentum, risk-control, and final scores.

## Quality-Trend Factors

Use available snapshot and yfinance fields:

- Market capitalization.
- PE availability and reasonableness.
- PB availability and reasonableness.
- Profitability fields when available, such as margins or return metrics.
- Growth fields when available, such as revenue or earnings growth.
- Cash flow fields when available.
- Leverage/debt fields when available.
- Trend stability from historical drawdown and SMA structure.

Suggested deterministic formula:

```text
valuation_score = average(reasonable_pe_score, reasonable_pb_score)
size_score = percentile(market_cap)
profitability_score = percentile(available profitability metrics)
growth_score = percentile(available growth metrics)
cashflow_score = percentile(available cashflow metrics)
leverage_score = percentile(leverage metrics, negative factor)
trend_stability = average(sma_structure_score, drawdown_control_score)
quality_trend_score = weighted average of available groups:
  valuation 20%, size 10%, profitability 20%, growth 15%,
  cashflow 10%, leverage 10%, trend_stability 15%
```

Do not punish a stock with a zero for unavailable fundamentals. Reweight available groups and lower `quality_coverage`. When `quality_coverage` is very low, cap `quality_trend_score` and final score so a candidate cannot reach top ratings on trend stability alone.

## Liquidity Score

Use turnover first, then volume and market cap:

```text
liquidity_score = average(percentile(turnover), percentile(volume), percentile(market_cap where available))
```

If only one liquidity field exists, use it and lower `data_coverage`.

## Risk Control Score

Use:

- Maximum drawdown, lower is better.
- 20-day annualized volatility, lower is better.
- ATR percentage, lower is better.
- Extreme one-day change penalty, lower absolute value is better.

If history is unavailable, use snapshot volatility proxies only and lower `data_coverage`.

## Ratings

Map `final_score` to research labels:

- `>= 80`: `重点跟踪`
- `>= 70`: `积极观察`
- `>= 60`: `需进一步验证`
- `>= 50`: `中性观察`
- `< 50`: `暂不纳入`

Do not use deterministic buy/sell wording.

If `quality_coverage < 35`, cap the final score below the `积极观察` threshold even when technical trend factors are strong.

## Result Levels

- `完整评分`: full mode with usable history, liquidity, and valuation coverage.
- `快速评分`: fast mode after enriching only the rough-screened TopN records.
- `快照初筛`: snapshot-only mode, with no history or fundamentals enrichment.
- `低覆盖率结果`: quote/history/fundamental fields are too sparse or enrichment failed. The rating must not exceed `需进一步验证`.

## Cross-Market Ranking

For `--market all`:

- Rank inside each market first.
- Compute `rank_global` by market-internal `final_score` percentile, not raw mixed-market score.
- Output both `top10_global` and `top_per_market`.
- State that cross-market ranking is for research triage only.
