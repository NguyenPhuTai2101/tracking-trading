"""Market cases the bot recognises. Pure functions shared by the live bot and the backtester.

A *setup* is one case: a rule that says "this situation is happening now" and proposes a
direction, a stop and how to exit. Each setup has its own signal and regime timeframe.
The bot recognises every setup below; it only *trades* the ones listed in
`StrategyParams.setups`. The others are reported as observations.

  THRUST_BREAKOUT  4h / regime 1d   close beyond the 20-candle extreme and the Bollinger band,
                                    not against the daily regime. Exit: ATR trailing stop.
  TREND_PULLBACK   1h / regime 4h   pullback to the EMA20 inside a trend, then a confirmation
                                    candle. Exit: breakeven, partial, trailing stop.
  RANGE_REVERSION  1h / regime 4h   in a ranging market, a close outside the Bollinger band
                                    followed by a close back inside. Exit: target at the mean.
  FAILED_BREAKOUT  1h / regime 4h   price sweeps the 48-candle extreme and closes back inside.
                                    Exit: 2R target.

What the research says about each is in `Setup.evidence` and in the README. Only
THRUST_BREAKOUT is traded by default.

The strategy only proposes. Position size is decided in risk.py.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from indicators import adx, atr, bollinger, ema, percent_rank, rolling_extreme, rsi, sma

INTERVAL_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
REGIME_EMA = 200
REGIME_SLOPE_BARS = 6          # EMA200 must have moved in the trend direction over this many candles
FAST_EMA = 20
SLOW_EMA = 50
MAX_EXTENSION_ATR = 1.0        # pullback: do not chase a trigger candle this far beyond the EMA20
STOP_BUFFER_ATR = 0.1
TIME_STOP_MIN_R = 0.5
FEE_BUFFER = 0.001             # round-trip taker fees, so a breakeven stop does not become a small loss
SIGNAL_HISTORY = 300           # closed signal candles the live bot loads
REGIME_HISTORY = 500           # closed regime candles; EMA200 needs a long run-in to settle
SWEEP_LOOKBACK = 48            # failed breakout: the extreme that gets swept
SQUEEZE_RANK = 0.2             # Bollinger width in the lowest 20% of the last 120 candles
SHOCK_ATR_MULT = 2.0           # ATR above twice its own 100-candle average
FADE_MAX_BARS = 24


@dataclass(frozen=True)
class Setup:
    key: str
    label: str
    interval: str           # candles the signal is read on
    regime_interval: str    # candles the trend regime is read on
    case: str               # the situation, in one line
    evidence: str           # what the research found
    expected_r: float = 0.0     # research: mean R per trade, net of costs
    r_sd: float = 1.0           # research: standard deviation of R per trade


SETUPS: Dict[str, Setup] = {s.key: s for s in (
    Setup("THRUST_BREAKOUT", "Phá vỡ có lực", "4h", "1d",
          "Nến 4h đóng vượt đỉnh/đáy 20 nến và vượt dải Bollinger, không ngược xu hướng ngày.",
          "Dương ở cả 4 mẫu dữ liệu độc lập (+0,07 đến +0,25R mỗi lệnh; t = 2,7 trên dữ liệu chưa từng dùng). "
          "Lãi dồn vào những năm thị trường có xu hướng mạnh; các năm khác gần hòa vốn.", 0.10, 2.1),
    Setup("TREND_PULLBACK", "Nhịp hồi trong xu hướng", "1h", "4h",
          "Xu hướng 4h rõ, giá hồi về EMA20 khung 1h rồi có nến xác nhận.",
          "Không có lợi thế: −0,055R trên train, khoảng 0R ngoài mẫu.", -0.04, 1.2),
    Setup("RANGE_REVERSION", "Đảo chiều trong vùng đi ngang", "1h", "4h",
          "Thị trường đi ngang, giá đóng ngoài dải Bollinger rồi đóng trở lại bên trong.",
          "Âm trên train (−0,22R, t = −4,9), khoảng 0R trên giai đoạn kiểm định. Chỉ nên quan sát.", -0.16, 1.1),
    Setup("FAILED_BREAKOUT", "Phá vỡ giả", "1h", "4h",
          "Giá quét qua đỉnh/đáy 48 nến rồi đóng ngược trở lại trong vùng.",
          "Âm trên train (−0,13R, t = −4,0), khoảng 0R trên giai đoạn kiểm định. Chỉ nên quan sát.", -0.10, 1.2),
)}
DEFAULT_SETUPS = ["THRUST_BREAKOUT"]


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    setups: List[str] = Field(default_factory=lambda: list(DEFAULT_SETUPS), min_length=1)   # traded setups
    allow_short: bool = True
    adx_min: float = Field(20.0, ge=10, le=40)                 # regime: trend strength needed
    # THRUST_BREAKOUT
    breakout_lookback: int = Field(20, ge=10, le=100)
    breakout_stop_atr: float = Field(2.0, ge=1, le=4)
    breakout_trail_atr: float = Field(4.0, ge=2, le=8)
    # TREND_PULLBACK
    pullback_lookback: int = Field(6, ge=3, le=20)
    rsi_pullback: float = Field(50.0, ge=30, le=60)
    stop_atr_min: float = Field(1.5, ge=0.8, le=4)
    stop_atr_max: float = Field(3.0, ge=1.5, le=6)
    breakeven_r: float = Field(1.0, ge=0.5, le=3)
    partial_r: float = Field(2.0, ge=1, le=6)
    partial_fraction: float = Field(0.5, ge=0, le=0.9)         # 0 = no partial exit
    trail_atr: float = Field(3.0, ge=1.5, le=8)
    time_stop_bars: int = Field(24, ge=6, le=200)

    @field_validator("setups")
    @classmethod
    def _known_setups(cls, setups):
        unknown = [s for s in setups if s not in SETUPS]
        if unknown:
            raise ValueError(f"Setup không tồn tại: {', '.join(unknown)}")
        return [s for s in SETUPS if s in setups]              # unique, in priority order

    @model_validator(mode="after")
    def _check(self):
        if self.stop_atr_max <= self.stop_atr_min:
            raise ValueError("SL tối đa (ATR) phải lớn hơn SL tối thiểu (ATR)")
        if self.partial_r <= self.breakeven_r:
            raise ValueError("Mức chốt một phần (R) phải lớn hơn mức dời SL hòa vốn (R)")
        return self


@dataclass(frozen=True)
class Signal:
    direction: str          # LONG | SHORT
    price: float            # close of the trigger candle
    stop: float
    atr: float
    regime: str
    reason: str
    setup: str = "TREND_PULLBACK"
    exit: Dict = field(default_factory=dict)    # how the position is managed, see new_plan()


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


# ----------------------------- regime & market state -----------------------------

def regime_series(candles: Sequence[Dict], params: StrategyParams) -> List[str]:
    """UP / DOWN / RANGE for every candle. Not enough history counts as RANGE."""
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


def signal_indicators(candles: Sequence[Dict], params: Optional[StrategyParams] = None) -> Dict[str, list]:
    params = params or StrategyParams()
    closes = [c["close"] for c in candles]
    highs, lows = [c["high"] for c in candles], [c["low"] for c in candles]
    middle, upper, lower = bollinger(closes)
    width = [None if m is None or m <= 0 else (u - l) / m for m, u, l in zip(middle, upper, lower)]
    volatility = atr(candles)
    return {
        "ema_fast": ema(closes, FAST_EMA), "ema_slow": ema(closes, SLOW_EMA), "rsi": rsi(closes), "atr": volatility,
        "bb_mid": middle, "bb_up": upper, "bb_lo": lower, "bb_rank": percent_rank(width, 120),
        "atr_avg": sma(volatility, 100),
        "high_n": rolling_extreme(highs, params.breakout_lookback, max),
        "low_n": rolling_extreme(lows, params.breakout_lookback, min),
        "sweep_high": rolling_extreme(highs, SWEEP_LOOKBACK, max),
        "sweep_low": rolling_extreme(lows, SWEEP_LOOKBACK, min),
    }


def market_tags(ind: Dict[str, list], i: int) -> List[str]:
    """Conditions worth showing next to the regime. They describe the market; they are not signals."""
    tags = []
    rank, vol, average, strength = ind["bb_rank"][i], ind["atr"][i], ind["atr_avg"][i], ind["rsi"][i]
    if rank is not None and rank <= SQUEEZE_RANK:
        tags.append("SQUEEZE")
    if vol and average and vol > SHOCK_ATR_MULT * average:
        tags.append("SHOCK")
    if strength is not None and strength >= 70:
        tags.append("OVERBOUGHT")
    if strength is not None and strength <= 30:
        tags.append("OVERSOLD")
    return tags


# ----------------------------- exits -----------------------------
# An exit profile is a plain dict so it can be journalled with the order. Keys:
#   breakeven_r, partial_r, partial_fraction, trail_atr, time_stop_bars   (None/0 = off)
#   target, max_bars                                                      (fixed-target trades)

def trend_exit(params: StrategyParams) -> Dict:
    return {"breakeven_r": params.breakeven_r, "partial_r": params.partial_r,
            "partial_fraction": params.partial_fraction, "trail_atr": params.trail_atr,
            "time_stop_bars": params.time_stop_bars}


def trail_exit(trail_atr: float) -> Dict:
    return {"trail_atr": trail_atr}


def target_exit(target: float, max_bars: int = FADE_MAX_BARS) -> Dict:
    return {"target": target, "max_bars": max_bars}


# ----------------------------- setups -----------------------------

def _thrust_breakout(candles, ind, i, regime, btc_regime, p):
    vol, upper, lower = ind["atr"][i], ind["bb_up"][i], ind["bb_lo"][i]
    high_n, low_n = ind["high_n"][i], ind["low_n"][i]
    if high_n is None or upper is None or not vol or vol <= 0:
        return None, "Chưa đủ dữ liệu nến"
    close = candles[i]["close"]
    if close > upper and close > high_n:
        direction = "LONG"
    elif close < lower and close < low_n:
        direction = "SHORT"
    else:
        return None, f"Chưa vượt đỉnh/đáy {p.breakout_lookback} nến"
    if regime == ("DOWN" if direction == "LONG" else "UP"):
        return None, f"Phá vỡ {direction} ngược xu hướng ngày ({regime}): bỏ"
    if direction == "SHORT" and not p.allow_short:
        return None, "Phá vỡ xuống, SHORT đang tắt"
    s = 1 if direction == "LONG" else -1
    reason = f"Đóng vượt {'đỉnh' if s > 0 else 'đáy'} {p.breakout_lookback} nến và dải Bollinger"
    return Signal(direction, close, close - s * p.breakout_stop_atr * vol, vol, regime, reason,
                  "THRUST_BREAKOUT", trail_exit(p.breakout_trail_atr)), reason


def _trend_pullback(candles, ind, i, regime, btc_regime, p):
    look = p.pullback_lookback
    first = i - look
    if first < 1 or any(ind[k][j] is None for k in ("ema_fast", "ema_slow", "rsi", "atr") for j in (first, i)):
        return None, "Chưa đủ dữ liệu nến"
    if regime == "RANGE":
        return None, "4h đi ngang hoặc xu hướng yếu"
    direction = "LONG" if regime == "UP" else "SHORT"
    if direction == "SHORT" and not p.allow_short:
        return None, "4h giảm, SHORT đang tắt"
    if btc_regime == ("DOWN" if direction == "LONG" else "UP"):
        return None, f"Ngược xu hướng BTC ({btc_regime})"
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
    cooled = min(rsi_values) <= p.rsi_pullback if s > 0 else max(rsi_values) >= 100 - p.rsi_pullback
    if not (touched and cooled):
        return None, f"4h {regime}: chờ giá hồi về EMA20"
    broke = s * (candle["close"] - (prev["high"] if s > 0 else prev["low"])) > 0
    if not (broke and s * (candle["close"] - candle["open"]) > 0 and s * (candle["close"] - fast) > 0):
        return None, f"4h {regime}: đã hồi, chờ nến xác nhận"
    if s * (candle["close"] - fast) > MAX_EXTENSION_ATR * vol:
        return None, "Nến xác nhận đã chạy quá xa EMA20: không đuổi giá"
    swing = min(extreme(candles[j]) for j in range(first, i + 1)) if s > 0 else \
        max(extreme(candles[j]) for j in range(first, i + 1))
    distance = max(s * (candle["close"] - swing) + STOP_BUFFER_ATR * vol, p.stop_atr_min * vol)
    if distance > p.stop_atr_max * vol:
        return None, f"SL theo đáy/đỉnh hồi xa hơn {p.stop_atr_max:g} ATR: bỏ lệnh"
    reason = f"Pullback thuận xu hướng 4h {regime}, SL cách {distance / vol:.1f} ATR"
    return Signal(direction, candle["close"], candle["close"] - s * distance, vol, regime, reason,
                  "TREND_PULLBACK", trend_exit(p)), reason


def _range_reversion(candles, ind, i, regime, btc_regime, p):
    vol = ind["atr"][i]
    if i < 3 or ind["bb_lo"][i - 1] is None or ind["rsi"][i - 2] is None or not vol:
        return None, "Chưa đủ dữ liệu nến"
    if regime != "RANGE":
        return None, f"4h {regime}: không phải vùng đi ngang"
    candle, prev = candles[i], candles[i - 1]
    for s, direction in ((1, "LONG"), (-1, "SHORT")):
        if direction == "SHORT" and not p.allow_short:
            continue
        band_before, band = (ind["bb_lo"][i - 1], ind["bb_lo"][i]) if s > 0 else (ind["bb_up"][i - 1], ind["bb_up"][i])
        strengths = ind["rsi"][i - 2:i + 1]
        washed = min(strengths) <= 30 if s > 0 else max(strengths) >= 70
        back_inside = s * (candle["close"] - band) > 0 and s * (candle["close"] - candle["open"]) > 0
        if not (s * (band_before - prev["close"]) > 0 and back_inside and washed):
            continue
        last = candles[i - 2:i + 1]
        swing = min(c["low"] for c in last) if s > 0 else max(c["high"] for c in last)
        distance = max(s * (candle["close"] - swing) + STOP_BUFFER_ATR * vol, vol)
        target = ind["bb_mid"][i]
        if distance > 2.5 * vol or s * (target - candle["close"]) < distance:
            return None, "Quay lại trong dải nhưng mục tiêu quá gần so với SL"
        reason = "Đi ngang: đóng ngoài dải Bollinger rồi quay lại, mục tiêu đường giữa"
        return Signal(direction, candle["close"], candle["close"] - s * distance, vol, regime, reason,
                      "RANGE_REVERSION", target_exit(target)), reason
    return None, "Đi ngang: giá chưa ra ngoài dải Bollinger"


def _failed_breakout(candles, ind, i, regime, btc_regime, p):
    vol, high_n, low_n = ind["atr"][i], ind["sweep_high"][i], ind["sweep_low"][i]
    if high_n is None or not vol:
        return None, "Chưa đủ dữ liệu nến"
    candle = candles[i]
    span = candle["high"] - candle["low"]
    if span <= 0:
        return None, "Chưa có cú quét đỉnh/đáy"
    close, open_ = candle["close"], candle["open"]
    if candle["high"] > high_n and close < high_n and close < open_ and candle["high"] - close >= 0.5 * span:
        s, direction, extreme, against = -1, "SHORT", candle["high"], "UP"
    elif candle["low"] < low_n and close > low_n and close > open_ and close - candle["low"] >= 0.5 * span:
        s, direction, extreme, against = 1, "LONG", candle["low"], "DOWN"
    else:
        return None, f"Chưa có cú quét đỉnh/đáy {SWEEP_LOOKBACK} nến"
    if regime == against:
        return None, f"Quét {'đỉnh' if s < 0 else 'đáy'} nhưng ngược xu hướng 4h ({regime}): bỏ"
    if direction == "SHORT" and not p.allow_short:
        return None, "Quét đỉnh, SHORT đang tắt"
    distance = s * (close - extreme) + STOP_BUFFER_ATR * vol
    if not 0.8 * vol <= distance <= 2.5 * vol:
        return None, "Cú quét quá nhỏ hoặc quá lớn so với ATR"
    reason = f"Quét {'đỉnh' if s < 0 else 'đáy'} {SWEEP_LOOKBACK} nến rồi đóng ngược lại, mục tiêu 2R"
    return Signal(direction, close, close - s * distance, vol, regime, reason,
                  "FAILED_BREAKOUT", target_exit(close + s * 2 * distance)), reason


_RULES = {"THRUST_BREAKOUT": _thrust_breakout, "TREND_PULLBACK": _trend_pullback,
          "RANGE_REVERSION": _range_reversion, "FAILED_BREAKOUT": _failed_breakout}


def evaluate(setup: str, candles: Sequence[Dict], ind: Dict[str, list], i: int, regime: str, btc_regime: str,
             params: StrategyParams) -> Tuple[Optional[Signal], str]:
    """Does `setup` fire on closed candle `i`? Returns the signal (or None) and a note saying why."""
    return _RULES[setup](candles, ind, i, regime, btc_regime, params)


# ----------------------------- exit management -----------------------------
# A plan is a plain dict so the bot can persist it as JSON.

def new_plan(direction: str, entry: float, stop: float, exit: Dict) -> Dict:
    """Two kinds of plan. Trend plans let a winner run: optional breakeven and partial exit,
    then an ATR trailing stop. Target plans (mean-reversion setups) keep the stop where it is
    and leave whole at the target, at the stop, or after `max_bars` candles."""
    s = 1 if direction == "LONG" else -1
    r = s * (entry - stop)
    if r <= 0:
        raise ValueError("SL nằm sai phía so với giá vào lệnh")
    target = exit.get("target")
    if target is not None and s * (target - entry) <= 0:
        raise ValueError("Mục tiêu nằm sai phía so với giá vào lệnh")
    partial = bool(exit.get("partial_fraction")) and exit.get("partial_r") and target is None
    return {"direction": direction, "entry": entry, "initial_stop": stop, "stop": stop, "r": r,
            "tp1": entry + s * exit["partial_r"] * r if partial else None, "best": entry, "bars": 0,
            "stop_kind": "INITIAL", "breakeven_done": False, "partial_done": not partial, "exit": dict(exit)}


def intrabar_event(plan: Dict, open_: float, high: float, low: float) -> Optional[Tuple[str, float]]:
    """What a price move does to the plan. The stop is checked first: when one candle
    touches both the stop and a target, the backtest assumes the stop filled."""
    s = 1 if plan["direction"] == "LONG" else -1
    adverse, favourable = (low, high) if s > 0 else (high, low)
    if s * (adverse - plan["stop"]) <= 0:
        gapped = s * (open_ - plan["stop"]) <= 0
        return "STOP", open_ if gapped else plan["stop"]
    target = plan["exit"].get("target")
    if target is not None and s * (favourable - target) >= 0:
        return "TARGET", target
    if not plan["partial_done"] and s * (favourable - plan["tp1"]) >= 0:
        return "PARTIAL", plan["tp1"]
    return None


def advance(plan: Dict, candle: Dict, atr_value: Optional[float]) -> Optional[str]:
    """Apply one closed signal candle: ratchet the stop, count bars. Returns "TIME_STOP" or None."""
    s = 1 if plan["direction"] == "LONG" else -1
    rules = plan["exit"]
    plan["bars"] += 1
    best = candle["high"] if s > 0 else candle["low"]
    if s * (best - plan["best"]) > 0:
        plan["best"] = best
    if rules.get("max_bars") and plan["bars"] >= rules["max_bars"]:
        return "TIME_STOP"
    progress = s * (plan["best"] - plan["entry"]) / plan["r"]
    if rules.get("breakeven_r") and not plan["breakeven_done"] and progress >= rules["breakeven_r"]:
        plan["breakeven_done"] = True
        level = plan["entry"] * (1 + s * FEE_BUFFER)
        if s * (level - plan["stop"]) > 0:
            plan["stop"], plan["stop_kind"] = level, "BREAKEVEN"
    if rules.get("trail_atr") and atr_value and atr_value > 0:
        trail = plan["best"] - s * rules["trail_atr"] * atr_value
        if s * (trail - plan["stop"]) > 0:
            plan["stop"], plan["stop_kind"] = trail, "TRAIL"
    if rules.get("time_stop_bars") and plan["bars"] >= rules["time_stop_bars"] and progress < TIME_STOP_MIN_R:
        return "TIME_STOP"
    return None


def stop_reason(plan: Dict) -> str:
    return {"BREAKEVEN": "BREAKEVEN_STOP", "TRAIL": "TRAILING_STOP"}.get(plan.get("stop_kind"), "STOP_LOSS")
