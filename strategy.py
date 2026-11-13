"""Entry rules shared by the live bot and the backtester, so both trade the same logic."""
from typing import Any, Dict, Optional, Tuple

STRATEGIES = ("RSI_SCALPING", "EMA_CROSSOVER", "HYBRID_SMART")


def decide_entry(strategy: str, analysis: Dict[str, Any], allow_short: bool) -> Optional[Tuple[str, str]]:
    """Return (direction, reason) when the strategy wants to enter, else None."""
    if strategy == "RSI_SCALPING":
        if analysis["rsi"] <= 32:
            return "LONG", f"RSI Quá Bán ({analysis['rsi']:.1f} <= 32)"
        if allow_short and analysis["rsi"] >= 68:
            return "SHORT", f"RSI Quá Mua ({analysis['rsi']:.1f} >= 68) - Vào vị thế SHORT"

    elif strategy == "EMA_CROSSOVER":
        if analysis["ema_cross"] == "BULLISH_CROSS" or (
                analysis["ema9"] > analysis["ema21"] and analysis["net_score"] >= 20):
            return "LONG", "EMA Golden Cross (EMA9 > EMA21)"
        if allow_short and (analysis["ema_cross"] == "BEARISH_CROSS" or (
                analysis["ema9"] < analysis["ema21"] and analysis["net_score"] <= -20)):
            return "SHORT", "EMA Death Cross (EMA9 < EMA21) - Vào vị thế SHORT"

    elif strategy == "HYBRID_SMART":
        if analysis["action"] in ("STRONG_BUY", "BUY") and analysis["net_score"] >= 25:
            return "LONG", f"Thuật toán AI Tổng Hợp: Điểm +{analysis['net_score']} ({analysis['action']})"
        if allow_short and analysis["action"] in ("STRONG_SELL", "SELL") and analysis["net_score"] <= -25:
            return "SHORT", (f"Thuật toán AI Tổng Hợp: Điểm {analysis['net_score']} "
                             f"({analysis['action']}) - Vào vị thế SHORT")
    return None


def exit_prices(entry_price: float, direction: str, tp_pct: float, sl_pct: float, leverage: int):
    """TP/SL are configured as % ROE; convert to price levels exactly like the live bot."""
    sign = 1 if direction == "LONG" else -1
    take_profit = entry_price * (1 + sign * tp_pct / leverage / 100)
    stop_loss = entry_price * (1 - sign * sl_pct / leverage / 100)
    return take_profit, stop_loss


# ---------------- entry/exit refinements shared by bot and backtest ----------------

TREND_EMA_PERIOD = 200
TREND_HISTORY = 260          # closed candles needed for a settled EMA200
# Round-trip taker fees, so a breakeven exit does not turn into a small loss.
BREAKEVEN_FEE_BUFFER = {"FUTURES": 0.001, "SPOT": 0.002}


def plan_exits(entry_price, direction, *, exit_mode="ROE", tp_pct=3.0, sl_pct=1.5, leverage=1,
               atr=None, atr_sl_mult=1.5, atr_tp_mult=3.0):
    """Return (take_profit, stop_loss) prices for either exit mode.

    ROE: distances are a % of margin (shrink as leverage grows).
    ATR: distances follow the market's recent volatility, independent of leverage.
    """
    if exit_mode == "ATR":
        if not atr or atr <= 0:
            raise ValueError("Thiếu ATR để đặt TP/SL theo biến động")
        sign = 1 if direction == "LONG" else -1
        return entry_price + sign * atr * atr_tp_mult, entry_price - sign * atr * atr_sl_mult
    return exit_prices(entry_price, direction, tp_pct, sl_pct, leverage)


def risk_based_margin(risk_usdt, entry_price, stop_loss, leverage, max_margin, market="FUTURES"):
    """Margin for a position that loses about `risk_usdt` (fees included) if the stop is hit.

    Fixed-risk sizing: a wide stop gets a small position and a tight stop a larger one, so
    every trade risks the same amount. Never exceeds `max_margin` (the per-trade budget).
    """
    distance = abs(entry_price - stop_loss) / entry_price
    if risk_usdt <= 0 or distance <= 0:
        raise ValueError("Không tính được khối lượng theo rủi ro")
    notional = risk_usdt / (distance + BREAKEVEN_FEE_BUFFER.get(market, 0.001))
    return min(notional / leverage, max_margin)


def entry_block_reason(day_pnl, loss_streak, max_daily_loss_usdt, max_consecutive_losses):
    """Daily circuit breaker: why new entries are paused for the rest of the day, else None."""
    if max_daily_loss_usdt and day_pnl <= -max_daily_loss_usdt:
        return f"Đã lỗ {-day_pnl:.2f} USDT hôm nay (giới hạn {max_daily_loss_usdt:g}). Nghỉ mở lệnh đến ngày mai."
    if max_consecutive_losses and loss_streak >= max_consecutive_losses:
        return f"Đã thua {loss_streak} lệnh liên tiếp hôm nay (giới hạn {max_consecutive_losses}). Nghỉ mở lệnh đến ngày mai."
    return None


def stop_too_close_to_liquidation(entry_price, stop_loss, leverage):
    """Isolated liquidation sits roughly 1/leverage away; keep the stop well inside it."""
    return leverage > 1 and abs(entry_price - stop_loss) / entry_price >= 0.8 / leverage


def trend_allows(direction, closes, period=TREND_EMA_PERIOD):
    """Trade only with the slower trend: LONG above EMA(period), SHORT below it."""
    if len(closes) < period + 20:
        return False
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema += (price - ema) * k
    return closes[-1] > ema if direction == "LONG" else closes[-1] < ema


def breakeven_levels(entry_price, initial_stop, direction, r_multiple, market):
    """(arm_price, stop_price): once price reaches entry + r_multiple * risk, the stop moves to
    entry plus fees. Returns None when the feature is off."""
    if not r_multiple or r_multiple <= 0:
        return None
    sign = 1 if direction == "LONG" else -1
    risk = abs(entry_price - initial_stop)
    arm = entry_price + sign * risk * r_multiple
    stop = entry_price * (1 + sign * BREAKEVEN_FEE_BUFFER.get(market, 0.001))
    if sign * (arm - stop) <= 0:
        return None   # target too close to cover fees; moving the stop would exit at once
    return arm, stop
