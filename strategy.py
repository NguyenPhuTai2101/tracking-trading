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
