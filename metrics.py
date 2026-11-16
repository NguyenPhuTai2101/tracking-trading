"""Performance figures shared by the live dashboard and the backtester.

A trade is one closed position: net of estimated fees, measured in USDT and in R
(multiples of the amount that was at risk when it was opened).
"""
from typing import Dict, List, Sequence

MIN_TRADES_FOR_STATS = 30    # below this, win rate and expectancy are mostly noise


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
