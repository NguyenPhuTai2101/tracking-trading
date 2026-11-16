"""Performance figures shared by the live dashboard and the backtester.

A trade is one closed position, measured in USDT and in R (multiples of the amount that
was at risk when it was opened). Live trades start with estimated fees and are later
settled against the exchange (real commission and funding); `settled` tells which.
"""
import math
from typing import Dict, List, Sequence

MIN_TRADES_FOR_STATS = 30    # below this, win rate and expectancy are mostly noise
MIN_TRADES_TO_JUDGE = 20     # below this, a setup's live result is not compared with research


def summarise(trades: Sequence[Dict]) -> Dict:
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gain = sum(t["net_pnl"] for t in wins)
    loss = -sum(t["net_pnl"] for t in losses)
    streak = worst_streak = 0
    for trade in trades:
        streak = streak + 1 if trade["net_pnl"] < 0 else 0
        worst_streak = max(worst_streak, streak)
    count = len(trades)

    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 3) if values else None

    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / count * 100, 1) if count else None,
        "net_pnl": round(sum(t["net_pnl"] for t in trades), 4),
        "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 4),
        "fees": round(sum(t["fees"] for t in trades), 4),
        "funding": round(sum(t.get("funding") or 0 for t in trades), 4),
        "estimated_trades": sum(t.get("settled") == 0 for t in trades),     # still carrying estimated fees
        "profit_factor": round(gain / loss, 2) if loss > 0 else None,
        "expectancy_r": mean(t["r_multiple"] for t in trades),
        "avg_win_r": mean(t["r_multiple"] for t in wins),
        "avg_loss_r": mean(t["r_multiple"] for t in losses),
        "avg_bars": mean(t["bars"] for t in trades),
        "max_consecutive_losses": worst_streak,
        "low_sample": count < MIN_TRADES_FOR_STATS,
    }


def group(trades: Sequence[Dict], key: str) -> List[Dict]:
    buckets: Dict[str, List[Dict]] = {}
    for trade in trades:
        buckets.setdefault(trade[key], []).append(trade)
    rows = [{key: name, "trades": len(items), "wins": sum(t["net_pnl"] > 0 for t in items),
             "net_pnl": round(sum(t["net_pnl"] for t in items), 4),
             "avg_r": round(sum(t["r_multiple"] for t in items) / len(items), 3)}
            for name, items in buckets.items()]
    return sorted(rows, key=lambda row: row["net_pnl"], reverse=True)


def max_drawdown_pct(equity: Sequence[float]) -> float:
    peak, worst = 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 2)


def execution_quality(trades: Sequence[Dict], assumed_fee_rate: float, assumed_slippage_rate: float) -> Dict:
    """What live trading actually cost, next to what the backtest assumes.

    Fee rate and funding use only trades settled against the exchange. Entry slippage is the
    fill price against the signal candle's close, signed so that positive = worse than the signal.
    """
    settled = [t for t in trades if t.get("settled") == 1]
    reconciled = [t for t in trades if t.get("settled") in (1, 2)]
    turnover = sum(t["quantity"] * (t["entry_price"] + t["exit_price"]) for t in settled)
    slips = [(1 if t["direction"] == "LONG" else -1) * (t["entry_price"] / t["signal_price"] - 1)
             for t in trades if t.get("signal_price")]
    risk = sum(t["risk_usdt"] for t in reconciled)
    return {
        "settled_trades": len(reconciled),
        "estimated_trades": sum(t.get("settled") == 0 for t in trades),
        "fee_rate_pct": round(sum(t["fees"] for t in settled) / turnover * 100, 4) if turnover else None,
        "assumed_fee_rate_pct": round(assumed_fee_rate * 100, 4),
        "entry_slippage_pct": round(sum(slips) / len(slips) * 100, 4) if slips else None,
        "assumed_slippage_pct": round(assumed_slippage_rate * 100, 4),
        "slippage_samples": len(slips),
        "funding_usdt": round(sum(t.get("funding") or 0 for t in reconciled), 4),
        # Funding as a share of the amount at risk: what it adds to (or takes from) the average trade, in R.
        "funding_r_per_trade": round(sum(t.get("funding") or 0 for t in reconciled) / risk, 4) if risk else None,
    }


def versus_expectation(trades: Sequence[Dict], setups: Dict) -> List[Dict]:
    """Per setup: the live mean R next to the range research says to expect for that many trades.

    The range is expected_r +/- 2 standard errors. It is wide for small samples on purpose: a
    result inside it says nothing is visibly wrong, not that the setup works.
    """
    rows = []
    for key, setup in setups.items():
        rs = [t["r_multiple"] for t in trades if t.get("setup") == key]
        if not rs:
            continue
        mean = sum(rs) / len(rs)
        margin = 2 * setup.r_sd / math.sqrt(len(rs))
        verdict = ("TOO_FEW" if len(rs) < MIN_TRADES_TO_JUDGE else
                   "BELOW" if mean < setup.expected_r - margin else
                   "ABOVE" if mean > setup.expected_r + margin else "WITHIN")
        rows.append({"setup": key, "trades": len(rs), "mean_r": round(mean, 3), "expected_r": setup.expected_r,
                     "low_r": round(setup.expected_r - margin, 3), "high_r": round(setup.expected_r + margin, 3),
                     "verdict": verdict})
    return rows
