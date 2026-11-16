"""Indicator series. Every function returns a list aligned with its input; None = not enough history."""
import math
from collections import deque
from typing import Dict, List, Optional, Sequence

Series = List[Optional[float]]


def ema(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    value = sum(values[:period]) / period
    out[period - 1] = value
    k = 2.0 / (period + 1)
    for i in range(period, len(values)):
        value += (values[i] - value) * k
        out[i] = value
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    gains, losses = gains / period, losses / period
    for i in range(period, len(closes)):
        if i > period:
            delta = closes[i] - closes[i - 1]
            gains = (gains * (period - 1) + max(delta, 0.0)) / period
            losses = (losses * (period - 1) + max(-delta, 0.0)) / period
        out[i] = 100.0 if losses == 0 and gains > 0 else 50.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    return out


def _true_ranges(candles: Sequence[Dict]) -> List[float]:
    return [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
            for p, c in zip(candles, candles[1:])]


def atr(candles: Sequence[Dict], period: int = 14) -> Series:
    """Wilder's average true range."""
    out: Series = [None] * len(candles)
    ranges = _true_ranges(candles)
    if len(ranges) < period:
        return out
    value = sum(ranges[:period]) / period
    out[period] = value
    for i in range(period, len(ranges)):
        value = (value * (period - 1) + ranges[i]) / period
        out[i + 1] = value
    return out


def adx(candles: Sequence[Dict], period: int = 14) -> Series:
    """Wilder's ADX: trend strength regardless of direction (above ~20 = trending)."""
    out: Series = [None] * len(candles)
    ranges = _true_ranges(candles)
    if len(ranges) < 2 * period - 1:
        return out
    plus, minus = [], []
    for p, c in zip(candles, candles[1:]):
        up, down = c["high"] - p["high"], p["low"] - c["low"]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    s_tr, s_plus, s_minus = sum(ranges[:period]), sum(plus[:period]), sum(minus[:period])
    value, seen = 0.0, 0
    for k in range(period - 1, len(ranges)):
        if k >= period:
            s_tr += ranges[k] - s_tr / period
            s_plus += plus[k] - s_plus / period
            s_minus += minus[k] - s_minus / period
        pdi = 100.0 * s_plus / s_tr if s_tr else 0.0
        ndi = 100.0 * s_minus / s_tr if s_tr else 0.0
        dx = 100.0 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0
        seen += 1
        if seen <= period:
            value += dx / period
            if seen == period:
                out[k + 1] = value
        else:
            value = (value * (period - 1) + dx) / period
            out[k + 1] = value
    return out


def bollinger(closes: Sequence[float], period: int = 20, width: float = 2.0):
    """(middle, upper, lower) bands: a moving average and `width` standard deviations around it."""
    n = len(closes)
    middle: Series = [None] * n
    upper: Series = [None] * n
    lower: Series = [None] * n
    total = squares = 0.0
    for i, close in enumerate(closes):
        total += close
        squares += close * close
        if i >= period:
            total -= closes[i - period]
            squares -= closes[i - period] ** 2
        if i >= period - 1:
            mean = total / period
            deviation = math.sqrt(max(squares / period - mean * mean, 0.0))
            middle[i], upper[i], lower[i] = mean, mean + width * deviation, mean - width * deviation
    return middle, upper, lower


def rolling_extreme(values: Sequence[float], n: int, pick) -> Series:
    """out[i] = max (pick=max) or min (pick=min) of the n values BEFORE i; None until n values exist."""
    out: Series = [None] * len(values)
    window = deque()                       # indices, best candidate first
    for i, value in enumerate(values):
        if i >= n:
            while window[0] < i - n:
                window.popleft()
            out[i] = values[window[0]]
        while window and (value >= values[window[-1]] if pick is max else value <= values[window[-1]]):
            window.pop()
        window.append(i)
    return out


def percent_rank(values: Sequence[Optional[float]], n: int) -> Series:
    """Share of the last n values that are below the current one (0 = lowest in the window)."""
    out: Series = [None] * len(values)
    for i in range(len(values)):
        window = values[i - n + 1:i + 1] if i >= n - 1 else None
        if window and window[0] is not None:
            out[i] = sum(v < values[i] for v in window) / n
    return out


def sma(values: Sequence[Optional[float]], n: int) -> Series:
    out: Series = [None] * len(values)
    total, count = 0.0, 0
    for i, value in enumerate(values):
        if value is not None:
            total += value
            count += 1
        if i >= n and values[i - n] is not None:
            total -= values[i - n]
            count -= 1
        if count == n:
            out[i] = total / n
    return out
