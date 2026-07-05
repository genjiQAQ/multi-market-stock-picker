"""Technical indicator calculations without heavy runtime dependencies."""

from __future__ import annotations

import math
from statistics import mean, pstdev

from .models import StockRecord


def _values(record: StockRecord, field: str) -> list[float]:
    values: list[float] = []
    for row in record.history:
        value = row.get(field)
        if isinstance(value, (int, float)) and not math.isnan(float(value)):
            values.append(float(value))
    return values


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return mean(values[-window:])


def pct_return(values: list[float], days: int) -> float | None:
    if len(values) <= days:
        return None
    base = values[-days - 1]
    if base == 0:
        return None
    return (values[-1] / base - 1) * 100


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) <= window:
        return None
    gains: list[float] = []
    losses: list[float] = []
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    for change in changes[-window:]:
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = mean(gains) / avg_loss
    return 100 - (100 / (1 + rs))


def ema(values: list[float], span: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (span + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def macd_hist(values: list[float]) -> float | None:
    if len(values) < 35:
        return None
    macd_line = ema(values, 12)
    signal_values: list[float] = []
    for idx in range(26, len(values) + 1):
        fast = ema(values[:idx], 12)
        slow = ema(values[:idx], 26)
        if fast is not None and slow is not None:
            signal_values.append(fast - slow)
    signal = ema(signal_values, 9)
    if macd_line is None or signal is None:
        return None
    slow_line = ema(values, 26)
    if slow_line is None:
        return None
    return (macd_line - slow_line) - signal


def max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1)
    return worst * 100


def volatility_20d(values: list[float]) -> float | None:
    if len(values) < 21:
        return None
    returns = []
    for idx in range(1, len(values)):
        if values[idx - 1]:
            returns.append(values[idx] / values[idx - 1] - 1)
    if len(returns) < 20:
        return None
    return pstdev(returns[-20:]) * math.sqrt(252) * 100


def atr_pct(record: StockRecord, window: int = 14) -> float | None:
    highs = _values(record, "high")
    lows = _values(record, "low")
    closes = _values(record, "close")
    if len(highs) <= window or len(lows) <= window or len(closes) <= window:
        return None
    true_ranges = []
    for idx in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[idx] - lows[idx],
                abs(highs[idx] - closes[idx - 1]),
                abs(lows[idx] - closes[idx - 1]),
            )
        )
    latest_close = closes[-1]
    if latest_close == 0:
        return None
    return mean(true_ranges[-window:]) / latest_close * 100


def volume_ratio(record: StockRecord, window: int = 20) -> float | None:
    volumes = _values(record, "volume")
    if len(volumes) <= window:
        return None
    avg = mean(volumes[-window - 1 : -1])
    if avg == 0:
        return None
    return volumes[-1] / avg


def compute_indicators(record: StockRecord) -> dict[str, float | None]:
    closes = _values(record, "close")
    if not closes:
        record.indicators = {
            "return_5d": None,
            "return_20d": None,
            "return_60d": None,
            "sma20_ratio": None,
            "sma60_ratio": None,
            "sma120_ratio": None,
            "volume_ratio_20d": None,
            "rsi_14": None,
            "macd_hist": None,
            "atr_pct": None,
            "max_drawdown": None,
            "volatility_20d": None,
        }
        return record.indicators

    latest = closes[-1]

    def ratio(window: int) -> float | None:
        avg = sma(closes, window)
        if avg is None or avg == 0:
            return None
        return latest / avg

    record.indicators = {
        "return_5d": pct_return(closes, 5),
        "return_20d": pct_return(closes, 20),
        "return_60d": pct_return(closes, 60),
        "sma20_ratio": ratio(20),
        "sma60_ratio": ratio(60),
        "sma120_ratio": ratio(120),
        "volume_ratio_20d": volume_ratio(record),
        "rsi_14": rsi(closes),
        "macd_hist": macd_hist(closes),
        "atr_pct": atr_pct(record),
        "max_drawdown": max_drawdown(closes),
        "volatility_20d": volatility_20d(closes),
    }
    if record.price is None:
        record.price = latest
    return record.indicators
