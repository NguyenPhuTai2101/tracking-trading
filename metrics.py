"""Performance figures for the dashboard, computed from the bot's recorded fills.

The bot stores one record per closing fill, with PnL before fees. Here fills are grouped back
into trades (one per position) and an estimated round-trip taker fee is subtracted, so the
win rate and profit factor describe whole trades after costs. Funding is not included.
"""
import calendar
from datetime import datetime, timedelta

from strategy import BREAKEVEN_FEE_BUFFER

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MIN_TRADES_FOR_STATS = 30


def _parse(text):
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _net(fill):
    notional = float(fill.get("exit_price", 0)) * float(fill.get("quantity", 0))
    fee = notional * BREAKEVEN_FEE_BUFFER.get(fill.get("market_type"), 0.001)
    return float(fill.get("pnl_usdt", 0)), fee


def _bucket(rows, key):
    out = {}
    for row in rows:
        item = out.setdefault(row[key], {key: row[key], "trades": 0, "wins": 0, "net_pnl": 0.0})
        item["trades"] += 1
        item["wins"] += row["net"] > 0
        item["net_pnl"] += row["net"]
    for item in out.values():
        item["winrate_pct"] = round(item["wins"] / item["trades"] * 100, 1)
        item["net_pnl"] = round(item["net_pnl"], 4)
    return sorted(out.values(), key=lambda item: item["net_pnl"], reverse=True)


def summarise_fills(fills, days=0, now=None, open_keys=()):
    """fills: the engine's closed_trades. days=0 keeps everything.

    open_keys: (market, symbol, entry_time) of positions that are only partly closed; their
    realised PnL counts, but they are not scored as a win or a loss yet.
    """
    now = now or datetime.now()
    since = now - timedelta(days=days) if days else None
    rows = []
    for fill in fills:
        closed_at = _parse(fill.get("exit_time"))
        if closed_at is None or (since and closed_at < since):
            continue
        gross, fee = _net(fill)
        rows.append({"at": closed_at, "gross": gross, "fee": fee, "net": gross - fee, "fill": fill})
    rows.sort(key=lambda row: row["at"])

    # Equity curve and daily totals use every fill, in the order it was realised.
    equity, daily, running, peak, max_dd = [], {}, 0.0, 0.0, 0.0
    for row in rows:
        running += row["net"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        # The chart shows wall-clock time of the server, matching the trade log.
        stamp = calendar.timegm(row["at"].timetuple())
        if equity and equity[-1]["time"] >= stamp:
            stamp = equity[-1]["time"] + 1
        equity.append({"time": stamp, "value": round(running, 4)})
        day = daily.setdefault(row["at"].strftime("%Y-%m-%d"), {"pnl": 0.0, "fills": 0})
        day["pnl"] += row["net"]
        day["fills"] += 1

    # One trade per position: partial closes of the same entry are merged.
    trades, index = [], {}
    open_keys = set(open_keys)
    for row in rows:
        fill = row["fill"]
        key = (fill.get("market_type"), fill.get("symbol"), fill.get("entry_time"))
        if key in open_keys:
            continue
        trade = index.get(key)
        if trade is None:
            trade = index[key] = {"market": key[0], "symbol": key[1], "side": fill.get("position_side", "LONG"),
                                  "net": 0.0, "gross": 0.0}
            trades.append(trade)
        trade["net"] += row["net"]
        trade["gross"] += row["gross"]
        trade["reason"] = fill.get("reason") or "UNKNOWN"
        trade["exit_time"] = fill.get("exit_time")

    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [-t["net"] for t in trades if t["net"] <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    streak = 0
    for trade in reversed(trades):
        sign = 1 if trade["net"] > 0 else -1
        if streak and (streak > 0) != (sign > 0):
            break
        streak += sign
    gross = sum(row["gross"] for row in rows)
    fees = sum(row["fee"] for row in rows)
    return {
        "days": days,
        "kpis": {
            "net_pnl": round(gross - fees, 4), "gross_pnl": round(gross, 4), "fees_est": round(fees, 4),
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "winrate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "breakeven_winrate_pct": round(avg_loss / (avg_win + avg_loss) * 100, 1) if wins and losses else None,
            "profit_factor": round(sum(wins) / sum(losses), 2) if sum(losses) > 0 else None,
            "expectancy": round(sum(t["net"] for t in trades) / len(trades), 4) if trades else None,
            "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
            "best_trade": round(max((t["net"] for t in trades), default=0.0), 4),
            "worst_trade": round(min((t["net"] for t in trades), default=0.0), 4),
            "max_drawdown": round(max_dd, 4),
            "streak": streak,
            "enough_trades": len(trades) >= MIN_TRADES_FOR_STATS,
        },
        "equity": equity,
        "daily": [{"date": date, "pnl": round(day["pnl"], 4), "fills": day["fills"]}
                  for date, day in sorted(daily.items())],
        "by_symbol": _bucket(trades, "symbol"),
        "by_reason": _bucket(trades, "reason"),
        "by_side": _bucket(trades, "side"),
    }


def live_snapshot(status):
    """Current exposure and today's limits, from TradingBotEngine.get_status()."""
    positions = status.get("active_positions", [])
    day = status.get("risk_day") or {}
    return {
        "market": status["market_type"], "is_running": status["is_running"],
        "entries_enabled": status.get("entries_enabled", False),
        "entry_block_reason": status.get("entry_block_reason"),
        "stop_reason": None if status["is_running"] else status.get("stop_reason"),
        "strategy": status["strategy"], "interval": status["interval"], "leverage": status["leverage"],
        "positions": len(positions), "max_positions": status["max_positions"],
        "unrealized_pnl": round(sum(float(p.get("current_pnl_usdt") or 0) for p in positions), 4),
        "used_margin": round(sum(float(p.get("cost_usdt") or 0) for p in positions), 4),
        "max_total_margin_usdt": status["max_total_margin_usdt"],
        "open_risk": round(sum(abs(p["entry_price"] - p["target_sl_price"]) * p["quantity"]
                               for p in positions if p.get("target_sl_price")), 4),
        "max_open_risk_usdt": status["max_open_risk_usdt"],
        "risk_per_trade_usdt": status.get("risk_per_trade_usdt", 0),
        "day_date": day.get("date"), "day_pnl": round(float(day.get("pnl", 0)), 4),
        "loss_streak": int(day.get("loss_streak", 0)),
        "max_daily_loss_usdt": status.get("max_daily_loss_usdt", 0),
        "max_consecutive_losses": status.get("max_consecutive_losses", 0),
    }
