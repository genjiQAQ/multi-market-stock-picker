# Watchlist Tracking

Read this reference before changing persistent candidate tracking, watchlist output files, or watch status rules.

## Purpose

Watchlist mode compares the current TopN research candidates with previous runs. It answers what changed since the last screen without turning the output into trading instructions.

## CLI

- `--watchlist`: enable persistent tracking for the current normal screening run.
- `--watchlist-name`: namespace for independent strategy/market watchlists; defaults to `default`.
- `--watchlist-state-dir`: persistent state directory; defaults to `~/Desktop/stock-picker-output/_state/watchlists/`.
- `--watchlist-lookback-runs`: missing-run threshold before a stale candidate becomes `移出观察池`; defaults to `5`.

## State

The persistent file is `<watchlist-state-dir>/<watchlist-name>.json`. Each entry is keyed by:

```text
market:yahoo_symbol
```

The state records first seen time, last seen time, last rank, last score, last rating, last result level, watch run count, missed run count, and a short history of recent statuses.

## Status Rules

- `新进入`: current TopN record was not in previous state.
- `继续跟踪`: current TopN record remains acceptable without a major score/rank drop.
- `重点延续`: record has appeared repeatedly and score/rank are stable.
- `降级观察`: record dropped out of TopN, score fell materially, rating is weak, or result level is `低覆盖率结果`.
- `移出观察池`: record is absent for `watchlist-lookback-runs` consecutive runs or prior rating was `暂不纳入`.

Watchlist status must only use available screening fields. Do not fabricate previous score, price, or rank.

## Outputs

- `watchlist_state.json`: copy of current persistent state in the run output directory.
- `watchlist_changes.csv`: current change rows.
- `watchlist_report.md`: Chinese summary of watchlist changes.
- Regular `screening_report.md` includes `## 候选跟踪变化` when changes exist.

## Safety

Watchlist output is research triage only. It must not use buy/sell wording or imply that continued tracking is a trading instruction.
