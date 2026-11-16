"""Signal research with train / validation / holdout discipline.

Runs the project's real backtester (same fills, fees, slippage, funding) with the capital
model's caps and breakers lifted, so what is measured is the setup itself: R per trade, net
of costs. Three disjoint data sets:

    train       2023-11 -> 2025-12, the 10 watchlist coins      explore here as much as you like
    validation  2025-12 -> 2026-11, the 10 watchlist coins      check what train suggested
    holdout     2021-01 -> 2023-11, all 20 coins  (H1: an earlier market cycle)
                2023-11 -> 2026-11, 10 other coins (H2: coins never looked at)
                                                                the final check, for a rule fixed beforehand

    python research.py fetch            # ~5 min, public data, cached in research_data.pkl
    python research.py fetch-holdout    # ~15 min, cached in research_holdout.pkl
    python research.py train
    python research.py validate
    python research.py holdout

Every look at validation or holdout spends it. Decide the rule on train (the middle of a
plateau, not the best cell), write it down, then look. A rule changed after seeing the
holdout result has no out-of-sample evidence left.

Findings so far are summarised in README.md ("Nghiên cứu tín hiệu").
"""
import math
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import backtest
import strategy
from engine import DEFAULT_WATCHLIST
from risk import RiskConfig
from strategy import SETUPS, Setup, Signal, StrategyParams

HERE = Path(__file__).resolve().parent
CACHE, HOLDOUT_CACHE = HERE / "research_data.pkl", HERE / "research_holdout.pkl"
DAY = 86_400_000
TRAIN_FRACTION = 0.7
HOLDOUT_COINS = ["LTCUSDT", "DOTUSDT", "TRXUSDT", "BCHUSDT", "NEARUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT",
                 "XLMUSDT", "AAVEUSDT"]
HOLDOUT_START = int(datetime(2020, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
EARLY_START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


# ----------------------------- data -----------------------------

def _download(symbols, start, end, path):
    data = {"start_ms": start, "end_ms": end, "1h": {}, "4h": {}, "1d": {}, "funding": {}}
    real_get = requests.Session.get

    def paced(self, *args, **kwargs):          # stay well under Binance's request-weight limit
        time.sleep(0.45)
        return real_get(self, *args, **kwargs)

    requests.Session.get = paced
    try:
        with requests.Session() as session:
            for sym in symbols:
                data["1h"][sym] = backtest.fetch_history(sym, "1h", start - 30 * DAY, end, "live", session)
                data["4h"][sym] = backtest.fetch_history(sym, "4h", start - 150 * DAY, end, "live", session)
                data["1d"][sym] = backtest.fetch_history(sym, "1d", start - 500 * DAY, end, "live", session)
                data["funding"][sym] = backtest.fetch_funding(sym, start, end, session)
                print(f"{sym}: {len(data['1h'][sym])} nến 1h, {len(data['4h'][sym])} nến 4h", flush=True)
    finally:
        requests.Session.get = real_get
    path.write_bytes(pickle.dumps(data))
    print(f"Đã lưu {path.name}")


def fetch():
    end = int(time.time() * 1000) // DAY * DAY
    _download(DEFAULT_WATCHLIST, end - 3 * 365 * DAY, end, CACHE)


def fetch_holdout():
    _download([*DEFAULT_WATCHLIST, *HOLDOUT_COINS], HOLDOUT_START, load(CACHE)["end_ms"], HOLDOUT_CACHE)


def load(path):
    if not path.exists():
        sys.exit(f"Chưa có {path.name}. Chạy: python research.py {'fetch' if path == CACHE else 'fetch-holdout'}")
    return pickle.loads(path.read_bytes())


# ----------------------------- harness -----------------------------

def free_risk(**changes):
    """The capital model with every cap and breaker lifted: each signal is taken, sized at 1R."""
    values = RiskConfig().model_dump()
    values.update(risk_pct=0.25, max_positions=99, max_same_direction=99, max_open_risk_pct=1e9,
                  max_gross_exposure_pct=1e9, max_position_notional_pct=1e9, daily_loss_pct=1e9,
                  weekly_loss_pct=1e9, max_consecutive_losses=10 ** 9, dd_reduce_pct=1e9, dd_halt_pct=1e9)
    values.update(changes)
    return RiskConfig.model_construct(**values)


def register(key, interval, regime_interval, rule):
    """Add an experimental rule next to the bot's own setups, for research runs only."""
    SETUPS[key] = Setup(key, key, interval, regime_interval, "experimental", "")
    strategy._RULES[key] = rule
    return key


def run(data, window, setup, symbols, params=None, real_risk=False):
    """Backtest one setup on one window. Candles after the window's end are cut off, never seen."""
    start, end = window
    info = SETUPS[setup]

    def cut(rows):
        return [c for c in rows if c["close_time_ms"] < end]

    symbols = [s for s in symbols if len(cut(data[info.interval].get(s, []))) > 400]
    feed = {"start_ms": start, "end_ms": end, "funding_ok": True,
            "candles": {info.interval: {s: cut(data[info.interval][s]) for s in symbols},
                        info.regime_interval: {s: cut(data[info.regime_interval][s])
                                               for s in dict.fromkeys([*symbols, "BTCUSDT"])}},
            "funding": {s: [f for f in data["funding"][s] if f[0] < end] for s in symbols}}
    params = (params or StrategyParams()).model_copy(update={"setups": [setup]})
    risk_cfg = RiskConfig() if real_risk else free_risk(leverage=5 if info.interval == "1h" else 3)
    config = backtest.BacktestConfig(symbols[:1], 365, 10_000.0, risk_cfg, params)
    config.symbols = symbols                    # research may run more coins than the UI allows
    result = backtest.run_backtest(config, feed)
    result["window"] = window
    return result


def stats(trades):
    rs = [t["r_multiple"] for t in trades if t["reason"] != "END_OF_TEST"]
    n = len(rs)
    if n < 2:
        return {"n": n, "mean": 0.0, "t": 0.0, "win": 0.0, "total": sum(rs)}
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1))
    return {"n": n, "mean": mean, "t": mean / (sd / math.sqrt(n)) if sd else 0.0,
            "win": sum(r > 0 for r in rs) / n * 100, "total": sum(rs)}


def row(label, trades, window=None):
    """One line: expectancy, its t-statistic, long/short, and each half of the window when given."""
    a = stats(trades)
    long_, short = (stats([t for t in trades if t["direction"] == d]) for d in ("LONG", "SHORT"))
    line = (f"{label:<40} n={a['n']:>5} thắng={a['win']:>4.1f}% E={a['mean']:>+6.3f}R t={a['t']:>+5.2f} "
            f"| LONG {long_['mean']:+.3f}R SHORT {short['mean']:+.3f}R")
    if window:
        middle = sum(window) // 2
        h1 = stats([t for t in trades if t["entry_time"] < middle])
        h2 = stats([t for t in trades if t["entry_time"] >= middle])
        line += f" | nửa đầu {h1['mean']:+.3f}R, nửa sau {h2['mean']:+.3f}R"
    print(line, flush=True)
    return a


# ----------------------------- experimental rules -----------------------------

def donchian(n, stop_atr=2.0):
    """Plain Donchian breakout: close beyond the extreme of the previous n candles. No filter."""
    def rule(candles, ind, i, regime, btc_regime, p):
        vol = ind["atr"][i]
        if i < n + 1 or not vol:
            return None, ""
        candle, window = candles[i], candles[i - n:i]
        s = 1 if candle["close"] > max(c["high"] for c in window) else \
            -1 if candle["close"] < min(c["low"] for c in window) else 0
        if not s:
            return None, ""
        return Signal("LONG" if s > 0 else "SHORT", candle["close"], candle["close"] - s * stop_atr * vol, vol,
                      regime, "donchian", f"DONCHIAN{n}", strategy.trail_exit(4.0)), ""
    return rule


def squeeze_breakout(candles, ind, i, regime, btc_regime, p):
    """The bot's thrust breakout, but only right after a volatility squeeze."""
    if i < 6 or ind["bb_rank"][i - 6] is None or min(ind["bb_rank"][i - 6:i]) > strategy.SQUEEZE_RANK:
        return None, ""
    signal, note = strategy._thrust_breakout(candles, ind, i, regime, btc_regime, p)
    return (Signal(**{**vars(signal), "setup": "SQUEEZE_BREAKOUT"}) if signal else None), note


def regime_only(candles, ind, i, regime, btc_regime, p):
    """Control: trade with the regime whenever flat, with no setup at all."""
    vol = ind["atr"][i]
    if not vol or regime == "RANGE" or btc_regime == ("DOWN" if regime == "UP" else "UP"):
        return None, ""
    s = 1 if regime == "UP" else -1
    close = candles[i]["close"]
    return Signal("LONG" if s > 0 else "SHORT", close, close - s * 2.0 * vol, vol, regime, "regime",
                  "REGIME_ONLY", strategy.trail_exit(4.0)), ""


register("DONCHIAN40", "4h", "1d", donchian(40))
register("SQUEEZE_BREAKOUT", "4h", "1d", squeeze_breakout)
register("REGIME_ONLY", "1h", "4h", regime_only)

RULES = [("Phá vỡ có lực 4h (bot)", "THRUST_BREAKOUT"),
         ("  + chỉ sau nén biến động", "SQUEEZE_BREAKOUT"),
         ("Donchian 40 nến 4h, không lọc", "DONCHIAN40"),
         ("Nhịp hồi trong xu hướng 1h (bot)", "TREND_PULLBACK"),
         ("  đối chứng: theo xu hướng, không setup", "REGIME_ONLY"),
         ("Đảo chiều trong vùng đi ngang 1h (bot)", "RANGE_REVERSION"),
         ("Phá vỡ giả 1h (bot)", "FAILED_BREAKOUT")]


def windows():
    data = load(CACHE)
    split = data["start_ms"] + int((data["end_ms"] - data["start_ms"]) * TRAIN_FRACTION)
    return data, (data["start_ms"], split), (split, data["end_ms"])


def train():
    data, window, _ = windows()
    for label, setup in RULES:
        row(label, run(data, window, setup, DEFAULT_WATCHLIST)["trades"], window)
    print("\n== Phá vỡ có lực 4h: đổi từng tham số quanh giá trị mặc định ==")
    for change in ({"breakout_lookback": 10}, {"breakout_lookback": 40}, {"breakout_stop_atr": 1.5},
                   {"breakout_stop_atr": 3.0}, {"breakout_trail_atr": 3.0}, {"breakout_trail_atr": 5.0}):
        params = StrategyParams(**change)
        row(f"  {change}", run(data, window, "THRUST_BREAKOUT", DEFAULT_WATCHLIST, params)["trades"], window)


def validate():
    data, _, window = windows()
    for label, setup in RULES:
        row(label, run(data, window, setup, DEFAULT_WATCHLIST)["trades"], window)
    print("\n== Qua mô hình vốn thật (RiskConfig mặc định) ==")
    for label, setup in RULES[:1] + RULES[3:4]:
        s = run(data, window, setup, DEFAULT_WATCHLIST, real_risk=True)["summary"]
        print(f"{label:<40} lệnh={s['trades']:>4} lợi nhuận={(s['final_equity'] / s['initial_balance'] - 1) * 100:+6.1f}% "
              f"DD={s['max_drawdown_pct']}%")


def holdout():
    core, held = load(CACHE), load(HOLDOUT_CACHE)
    early, late = (EARLY_START, core["start_ms"]), (core["start_ms"], held["end_ms"])
    for label, setup in RULES[:2]:
        print(f"== {label.strip()} ==")
        h1 = run(held, early, setup, [*DEFAULT_WATCHLIST, *HOLDOUT_COINS])["trades"]
        h2 = run(held, late, setup, HOLDOUT_COINS)["trades"]
        row("H1: 2021-01 -> 2023-11, 20 coin", h1)
        row("H2: 2023-11 -> 2026-11, 10 coin mới", h2)
        row("H1 + H2", h1 + h2)
        years = {}
        for trade in h1 + h2:
            if trade["reason"] != "END_OF_TEST":
                years.setdefault(datetime.fromtimestamp(trade["entry_time"] / 1000, timezone.utc).year, []).append(trade["r_multiple"])
        print("   theo năm:", {year: f"{sum(rs) / len(rs):+.2f}R (n={len(rs)})" for year, rs in sorted(years.items())})


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    commands = {"fetch": fetch, "fetch-holdout": fetch_holdout, "train": train, "validate": validate, "holdout": holdout}
    command = commands.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not command:
        sys.exit(__doc__)
    command()
