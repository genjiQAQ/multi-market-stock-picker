# Data Sources

Read this reference before changing market providers, ticker normalization, filters, or fallback behavior.

## Provider Roles

- AKShare is the default auto-universe provider for A-share, Hong Kong, and US market snapshots.
- Nasdaq Trader Symbol Directory is the US auto-universe fallback when AKShare's US snapshot fails. It provides a broad listed-security directory, not live quote or valuation fields.
- yfinance is the default historical price and optional fundamentals provider.
- Presets and custom files are local fallbacks and user-controlled universes.
- Never require paid market-data API keys for the default path.

## AKShare Snapshot Interfaces

Use these interfaces when AKShare is installed and the run uses `--universe auto` or `auto+custom`:

| Market | Preferred AKShare function | Purpose | Notes |
|---|---|---|---|
| `a-share` | `stock_zh_a_spot_em()` | A-share snapshot and rough universe | Fallback to `stock_zh_a_spot()` and then Eastmoney clist when the preferred endpoint fails. Convert volume in lots to shares when needed. |
| `hk` | `stock_hk_spot_em()` | Hong Kong stock snapshot and rough universe | Treat as delayed data when provider indicates delay. |
| `us` | `stock_us_spot_em()` | US stock snapshot and rough universe | Clean AKShare-prefixed codes before yfinance use. |

AKShare field names may change. Provider adapters must map available columns into the internal schema and record missing columns in `data_quality.json`.

## Nasdaq Trader US Directory Fallback

When `--market us --universe auto` cannot fetch `stock_us_spot_em()`, use these official Nasdaq Trader Symbol Directory text files as the next fallback:

- `https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`
- `https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt`

The fallback must:

- Parse pipe-delimited text and stop before `File Creation Time`.
- Skip rows with `Test Issue=Y`, `ETF=Y`, or `NextShares=Y`.
- Normalize `Symbol` / `ACT Symbol` into Yahoo-compatible US tickers.
- Mark the source as `NasdaqTrader:SymbolDirectory`.
- Treat missing price, turnover, PE, PB, and market cap as expected directory-only gaps until enrichment runs.
- Directory-only rows must not outrank quoted US rows during rough screening. If quote/history enrichment also fails, keep them as low-coverage research candidates only.

This fallback is suitable for building a broad US stock universe. It is not a replacement for live quotes, turnover, historical K-line data, or fundamentals.

## Internal Snapshot Schema

Every market snapshot row must be normalized to:

```text
raw_symbol
yahoo_symbol
market
name
price
change_pct
volume
turnover
market_cap
pe
pb
high
low
amplitude_pct
close_position
currency
source
source_time
data_delay
is_tradeable
exclude_reason
```

Use `None` for unavailable numeric fields and record missing-field counts. Do not fabricate values.

## Units And Currency

- A-share volume from Chinese quote providers is often reported in lots. Convert to shares when the source column is clearly lots; otherwise preserve the numeric value and record a warning. Current defaults treat Eastmoney clist and `stock_zh_a_spot_em()` as lots, while legacy `stock_zh_a_spot()` is preserved.
- Turnover stays in the source currency and must be paired with `currency`.
- Default currencies: A-share `CNY`, Hong Kong `HKD`, US `USD`.
- `source_time` must be generated at normalization time when the provider does not supply a timestamp.

## Ticker Normalization

A-share:

- `600`, `601`, `603`, `605`, `688` prefixes map to `.SS`.
- `000`, `001`, `002`, `003`, `300`, `301` prefixes map to `.SZ`.
- Beijing Stock Exchange symbols are excluded in v1 with `exclude_reason=bj_unsupported`.
- Preserve six-digit raw symbols.

Hong Kong:

- Preserve leading zeroes and normalize to four digits before appending `.HK`.
- Examples: `700 -> 0700.HK`, `0700 -> 0700.HK`, `9988 -> 9988.HK`.

US:

- Remove AKShare numeric prefixes such as `105.AAPL -> AAPL`.
- Convert Yahoo class notation with a dash: `BRK.B -> BRK-B`.
- Preserve plain US tickers already compatible with yfinance.

## Filters

A-share exclusions:

- ST names.
- Delisted or退 symbols/names.
- Suspended or missing price rows.
- Beijing Stock Exchange rows in v1.
- New-share name prefixes such as `N` / `C` when detectable.
- Near-limit-up and high intraday-amplitude rows when configured.
- Rows below `min_turnover_by_market.a-share` or `min_price_by_market.a-share`.

Hong Kong exclusions:

- Missing price rows.
- Low turnover rows below `min_turnover_by_market.hk`.
- Penny-stock rows below `min_price_by_market.hk`.
- Non-common stock rows when detectable from name or code.

US exclusions:

- Warrants, units, rights, preferreds, OTC, and pink-sheet names or tickers when detectable.
- Rows below `min_turnover_by_market.us` or `min_price_by_market.us`.
- Missing price rows.

## History And Fundamentals

- Fetch A-share history with Eastmoney daily K-line first, then use yfinance for non-A-share history when available.
- Enrich A-share industry/concept metadata from AKShare individual-stock information when available. Missing industry or concept metadata must not block scoring.
- If history is missing or yfinance is unavailable, keep the snapshot row but lower data coverage.
- Fetch fundamentals from yfinance `Ticker.info` when available.
- A/H fundamentals may be incomplete. Missing fundamentals must lower `quality_coverage`; they must not automatically disqualify a candidate unless required fields for ranking are absent.

## Fallbacks

Fallback order:

1. Auto AKShare snapshot for requested markets.
2. For A-share, fallback from `stock_zh_a_spot_em()` to `stock_zh_a_spot()`, then Eastmoney clist.
3. Preset universe for failed markets when requested by `preset` or `preset+custom`.
4. Custom symbols when provided.

Single ticker failures do not fail the run. Single market failures do not fail other markets. If every requested market/source fails and there are no usable custom or preset rows, return exit code `3`.
