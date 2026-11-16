"""Trend-pullback strategy. Pure functions shared by the live bot and the backtester.

1. Regime (4h): price vs EMA200, the EMA's slope and ADX decide UP / DOWN / RANGE.
   RANGE means no trade. Altcoins also need BTC not to be in the opposite regime.
2. Setup (1h): with the 1h EMA20/EMA50 aligned to the regime, wait for price to pull
   back to the EMA20 while RSI cools off.
3. Trigger: a closed 1h candle in the trend direction that closes beyond the previous
   candle's extreme. Entry is the next candle's open.
4. Stop: beyond the pullback extreme, at least `stop_atr_min` ATR away. Wider than
   `stop_atr_max` ATR = skip the trade.
5. Exit: stop to breakeven at `breakeven_r`, part of the position off at `partial_r`,
   the rest trails `trail_atr` ATR behind the best price. A trade that has not reached
   0.5R after `time_stop_bars` candles is closed.

The strategy only proposes direction and stop. Position size is decided in risk.py.
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from indicators import adx, atr, ema, rsi

INTERVAL_SECONDS = {"1h": 3600, "4h": 14400}
SIGNAL_INTERVAL = "1h"
REGIME_INTERVAL = "4h"
REGIME_EMA = 200
REGIME_SLOPE_BARS = 6          # EMA200 must have moved in the trend direction over this many 4h candles
FAST_EMA = 20
SLOW_EMA = 50
MAX_EXTENSION_ATR = 1.0        # do not chase a trigger candle that closed this far beyond the EMA20
STOP_BUFFER_ATR = 0.1
TIME_STOP_MIN_R = 0.5
FEE_BUFFER = 0.001             # round-trip taker fees, so a breakeven stop does not become a small loss
SIGNAL_HISTORY = 300           # closed 1h candles the live bot loads
REGIME_HISTORY = 500           # closed 4h candles; EMA200 needs a long run-in to settle


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    allow_short: bool = True
    adx_min: float = Field(20.0, ge=10, le=40)
    pullback_lookback: int = Field(6, ge=3, le=20)
    rsi_pullback: float = Field(50.0, ge=30, le=60)
    stop_atr_min: float = Field(1.5, ge=0.8, le=4)
    stop_atr_max: float = Field(3.0, ge=1.5, le=6)
    breakeven_r: float = Field(1.0, ge=0.5, le=3)
    partial_r: float = Field(2.0, ge=1, le=6)
    partial_fraction: float = Field(0.5, ge=0, le=0.9)     # 0 = no partial exit
    trail_atr: float = Field(3.0, ge=1.5, le=8)
    time_stop_bars: int = Field(24, ge=6, le=200)

    @model_validator(mode="after")
    def _check(self):
        if self.stop_atr_max <= self.stop_atr_min:
            raise ValueError("SL tối đa (ATR) phải lớn hơn SL tối thiểu (ATR)")
        if self.partial_r <= self.breakeven_r:
            raise ValueError("Mức chốt một phần (R) phải lớn hơn mức dời SL hòa vốn (R)")
        return self


@dataclass(frozen=True)
class Signal:
    direction: str      # LONG | SHORT
    price: float        # close of the trigger candle
    stop: float
    atr: float
    regime: str
    reason: str


def closed_candles(candles, interval, now_ms, minimum):
    """Reject stale, incomplete, unordered or malformed input before making a signal."""
    duration_ms = INTERVAL_SECONDS[interval] * 1000
    closed = [c for c in candles if c.get("closed")]
    if len(closed) < minimum:
        raise ValueError(f"Chưa đủ {minimum} nến {interval} đã đóng")
    age = now_ms - closed[-1]["close_time_ms"]
    if age < 0 or age > duration_ms + 30_000:
        raise ValueError("Dữ liệu nến đã cũ hoặc lệch thời gian")
    previous = None
    for candle in closed:
        values = [candle[k] for k in ("open", "high", "low", "close")]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Giá nến không hợp lệ")
        if candle["low"] > min(candle["open"], candle["close"]) or candle["high"] < max(candle["open"], candle["close"]):
            raise ValueError("Biên độ nến không hợp lệ")
        if previous is not None and (candle["time"] - previous) * 1000 != duration_ms:
            raise ValueError("Dữ liệu nến bị thiếu hoặc sai thứ tự")
        previous = candle["time"]
    return closed


# ----------------------------- regime -----------------------------

def regime_series(candles: Sequence[Dict], params: StrategyParams) -> List[str]:
    """UP / DOWN / RANGE for every 4h candle. Not enough history counts as RANGE."""
    closes = [c["close"] for c in candles]
    trend, strength = ema(closes, REGIME_EMA), adx(candles)
    out = []
    for i, close in enumerate(closes):
        now = trend[i]
        before = trend[i - REGIME_SLOPE_BARS] if i >= REGIME_SLOPE_BARS else None
        if now is None or before is None or strength[i] is None or strength[i] < params.adx_min:
            out.append("RANGE")
        elif close > now and now > before:
            out.append("UP")
        elif close < now and now < before:
            out.append("DOWN")
        else:
            out.append("RANGE")
    return out


# ----------------------------- entry -----------------------------

def signal_indicators(candles: Sequence[Dict]) -> Dict[str, list]:
    closes = [c["close"] for c in candles]
    return {"ema_fast": ema(closes, FAST_EMA), "ema_slow": ema(closes, SLOW_EMA),
            "rsi": rsi(closes), "atr": atr(candles)}


def evaluate(candles: Sequence[Dict], ind: Dict[str, list], i: int, regime: str, btc_regime: str,
             params: StrategyParams) -> Tuple[Optional[Signal], str]:
    """Signal on closed candle `i`, plus a short note saying why there is or is not one."""
    look = params.pullback_lookback
    first = i - look
    if first < 1 or any(ind[k][j] is None for k in ("ema_fast", "ema_slow", "rsi", "atr") for j in (first, i)):
        return None, "Chưa đủ dữ liệu nến 1h"
    if regime == "RANGE":
        return None, "4h đi ngang hoặc xu hướng yếu: đứng ngoài"
    direction = "LONG" if regime == "UP" else "SHORT"
    if direction == "SHORT" and not params.allow_short:
        return None, "4h giảm, SHORT đang tắt: đứng ngoài"
    if btc_regime == ("DOWN" if direction == "LONG" else "UP"):
        return None, f"Ngược xu hướng BTC ({btc_regime}): đứng ngoài"
    s = 1 if direction == "LONG" else -1
    candle, prev = candles[i], candles[i - 1]
    fast, slow, vol = ind["ema_fast"][i], ind["ema_slow"][i], ind["atr"][i]
    if vol <= 0 or s * (fast - slow) <= 0:
        return None, f"4h {regime} nhưng EMA20/50 khung 1h chưa cùng chiều"

    def extreme(c):     # the side of the candle a pullback reaches
        return c["low"] if s > 0 else c["high"]

    window = range(first, i)
    touched = any(s * (ind["ema_fast"][j] - extreme(candles[j])) >= 0 for j in window)
    rsi_values = [ind["rsi"][j] for j in window]
    cooled = min(rsi_values) <= params.rsi_pullback if s > 0 else max(rsi_values) >= 100 - params.rsi_pullback
    if not (touched and cooled):
        return None, f"4h {regime}: chờ giá hồi về EMA20"
    broke = s * (candle["close"] - (prev["high"] if s > 0 else prev["low"])) > 0
    if not (broke and s * (candle["close"] - candle["open"]) > 0 and s * (candle["close"] - fast) > 0):
        return None, f"4h {regime}: đã hồi, chờ nến xác nhận"
    if s * (candle["close"] - fast) > MAX_EXTENSION_ATR * vol:
        return None, "Nến xác nhận đã chạy quá xa EMA20: không đuổi giá"
    swing = min(extreme(candles[j]) for j in range(first, i + 1)) if s > 0 else \
        max(extreme(candles[j]) for j in range(first, i + 1))
    distance = max(s * (candle["close"] - swing) + STOP_BUFFER_ATR * vol, params.stop_atr_min * vol)
    if distance > params.stop_atr_max * vol:
        return None, f"SL theo đáy/đỉnh hồi xa hơn {params.stop_atr_max:g} ATR: bỏ lệnh"
    reason = f"Pullback thuận xu hướng 4h {regime}, SL cách {distance / vol:.1f} ATR"
    return Signal(direction, candle["close"], candle["close"] - s * distance, vol, regime, reason), reason


# ----------------------------- exit management -----------------------------
# A plan is a plain dict so the bot can persist it as JSON.

def new_plan(direction: str, entry: float, stop: float, params: StrategyParams) -> Dict:
    s = 1 if direction == "LONG" else -1
    r = s * (entry - stop)
    if r <= 0:
        raise ValueError("SL nằm sai phía so với giá vào lệnh")
    return {"direction": direction, "entry": entry, "initial_stop": stop, "stop": stop, "r": r,
            "tp1": entry + s * params.partial_r * r, "best": entry, "bars": 0,
            "stop_kind": "INITIAL", "breakeven_done": False,
            "partial_done": params.partial_fraction <= 0}


def intrabar_event(plan: Dict, open_: float, high: float, low: float) -> Optional[Tuple[str, float]]:
    """What a price move does to the plan. The stop is checked first: when one candle
    touches both the stop and the target, the backtest assumes the stop filled."""
    s = 1 if plan["direction"] == "LONG" else -1
    adverse, favourable = (low, high) if s > 0 else (high, low)
    if s * (adverse - plan["stop"]) <= 0:
        gapped = s * (open_ - plan["stop"]) <= 0
        return "STOP", open_ if gapped else plan["stop"]
    if not plan["partial_done"] and s * (favourable - plan["tp1"]) >= 0:
        return "PARTIAL", plan["tp1"]
    return None


def advance(plan: Dict, candle: Dict, atr_value: Optional[float], params: StrategyParams) -> Optional[str]:
    """Apply one closed 1h candle: ratchet the stop, count bars. Returns "TIME_STOP" or None."""
    s = 1 if plan["direction"] == "LONG" else -1
    plan["bars"] += 1
    best = candle["high"] if s > 0 else candle["low"]
    if s * (best - plan["best"]) > 0:
        plan["best"] = best
    progress = s * (plan["best"] - plan["entry"]) / plan["r"]
    if not plan["breakeven_done"] and progress >= params.breakeven_r:
        plan["breakeven_done"] = True
        level = plan["entry"] * (1 + s * FEE_BUFFER)
        if s * (level - plan["stop"]) > 0:
            plan["stop"], plan["stop_kind"] = level, "BREAKEVEN"
    if atr_value and atr_value > 0:
        trail = plan["best"] - s * params.trail_atr * atr_value
        if s * (trail - plan["stop"]) > 0:
            plan["stop"], plan["stop_kind"] = trail, "TRAIL"
    if plan["bars"] >= params.time_stop_bars and progress < TIME_STOP_MIN_R:
        return "TIME_STOP"
    return None


def stop_reason(plan: Dict) -> str:
    return {"BREAKEVEN": "BREAKEVEN_STOP", "TRAIL": "TRAILING_STOP"}.get(plan.get("stop_kind"), "STOP_LOSS")
