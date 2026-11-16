"""Indicator series. Every function returns a list aligned with its input; None = not enough history."""
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
