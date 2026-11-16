"""Signal research with train/test discipline.

Runs the project's real backtester (same fills, fees, slippage, funding) over three years of
public data, with the capital model's breakers out of the way, so what is measured is the
signal itself: R per trade, net of costs.

    python research.py fetch     # ~5 minutes, public data only, cached in research_data.pkl
    python research.py train     # first 70% of the period: explore here as much as you like
    python research.py test      # last 30%: only for a candidate chosen beforehand on train

Every look at the test period spends it. Pick ONE candidate from the train results (the middle
of a plateau, not the best cell), write it into CANDIDATE below, and only then run `test`.
If you change the candidate after seeing the test result, the test result means nothing.

Findings so far are summarised in README.md ("Nghiên cứu tín hiệu").
"""
import math
import pickle
import sys
import time
from pathlib import Path

import requests

import backtest
import strategy
from engine import DEFAULT_WATCHLIST
from risk import RiskConfig
from strategy import Signal, StrategyParams

CACHE = Path(__file__).resolve().with_name("research_data.pkl")
DAY = 86_400_000
YEARS = 3
TRAIN_FRACTION = 0.7
strategy.INTERVAL_SECONDS.setdefault("1d", 86400)
_REAL_EVALUATE = strategy.evaluate


# ----------------------------- data -----------------------------

def fetch():
    end = int(time.time() * 1000) // DAY * DAY
    start = end - YEARS * 365 * DAY
    data = {"start_ms": start, "end_ms": end, "1h": {}, "4h": {}, "1d": {}, "funding": {}}
    real_get = requests.Session.get

    def paced(self, *args, **kwargs):          # stay well under Binance's request-weight limit
        time.sleep(0.45)
        return real_get(self, *args, **kwargs)

    requests.Session.get = paced
    try:
        with requests.Session() as session:
            for sym in DEFAULT_WATCHLIST:
                data["1h"][sym] = backtest.fetch_history(sym, "1h", start - 30 * DAY, end, "live", session)
                data["4h"][sym] = backtest.fetch_history(sym, "4h", start - 150 * DAY, end, "live", session)
                data["1d"][sym] = backtest.fetch_history(sym, "1d", start - 500 * DAY, end, "live", session)
                data["funding"][sym] = backtest.fetch_funding(sym, start, end, session)
                print(f"{sym}: {len(data['1h'][sym])} nến 1h", flush=True)
    finally:
        requests.Session.get = real_get
    CACHE.write_bytes(pickle.dumps(data))
    print(f"Đã lưu {CACHE.name}")


def load():
    if not CACHE.exists():
        sys.exit("Chưa có dữ liệu. Chạy: python research.py fetch")
    data = pickle.loads(CACHE.read_bytes())
    split = data["start_ms"] + int((data["end_ms"] - data["start_ms"]) * TRAIN_FRACTION)
    data["periods"] = {"train": (data["start_ms"], split), "test": (split, data["end_ms"])}
    return data


# ----------------------------- harness -----------------------------

def free_risk(**changes):
    """The capital model with every cap and breaker lifted: each signal is taken, sized at 1R."""
    values = RiskConfig().model_dump()
    values.update(risk_pct=0.25, max_positions=99, max_same_direction=99, max_open_risk_pct=1e9,
                  max_gross_exposure_pct=1e9, max_position_notional_pct=1e9, daily_loss_pct=1e9,
                  weekly_loss_pct=1e9, max_consecutive_losses=10 ** 9, dd_reduce_pct=1e9, dd_halt_pct=1e9)
    values.update(changes)
    return RiskConfig.model_construct(**values)


def params(**changes):
    """Strategy parameters outside the validated ranges, e.g. breakeven_r=99 for 'never'."""
    return StrategyParams.model_construct(**{**StrategyParams().model_dump(), **changes})


PLAIN_EXIT = dict(breakeven_r=99, partial_fraction=0, time_stop_bars=10 ** 9)   # stop + ATR trail only


def run(data, period, evaluate=None, p=None, signal_tf="1h", regime_tf="4h", risk_cfg=None):
    """Backtest one rule on one period. Candles after the period's end are cut off, never seen."""
    start, end = data["periods"][period]
    symbols = list(data["1h"])

    def cut(rows):
        return [c for c in rows if c["close_time_ms"] < end]

    feed = {"start_ms": start, "end_ms": end, "funding_ok": True,
            "signal": {s: cut(data[signal_tf][s]) for s in symbols},
            "regime": {s: cut(data[regime_tf][s]) for s in dict.fromkeys([*symbols, "BTCUSDT"])},
            "funding": {s: [f for f in data["funding"][s] if f[0] < end] for s in symbols}}
    backtest.SIGNAL_INTERVAL, strategy.evaluate = signal_tf, evaluate or _REAL_EVALUATE
    try:
        config = backtest.BacktestConfig(symbols, 365, 10_000.0, risk_cfg or free_risk(), p or StrategyParams())
        result = backtest.run_backtest(config, feed)
    finally:
        backtest.SIGNAL_INTERVAL, strategy.evaluate = "1h", _REAL_EVALUATE
    result["window"] = (start, end)
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


def row(label, result):
    """One line: expectancy, its t-statistic, and each half of the period (an edge should show in both)."""
    trades = result["trades"]
    middle = sum(result["window"]) // 2
    a = stats(trades)
    h1 = stats([t for t in trades if t["entry_time"] < middle])
    h2 = stats([t for t in trades if t["entry_time"] >= middle])
    print(f"{label:<38} n={a['n']:>5} thắng={a['win']:>4.1f}% E={a['mean']:>+6.3f}R t={a['t']:>+5.2f} "
          f"tổng={a['total']:>+7.1f}R | nửa đầu {h1['mean']:>+6.3f}R, nửa sau {h2['mean']:>+6.3f}R", flush=True)
    return a


def portfolio(label, result):
    s = result["summary"]
    print(f"{label:<38} lệnh={s['trades']:>4} E={s['expectancy_r'] or 0:+.3f}R "
          f"lợi nhuận={(s['final_equity'] / s['initial_balance'] - 1) * 100:+6.1f}% DD={s['max_drawdown_pct']}% "
          f"dừng do DD={'có' if s['halted_by_drawdown'] else 'không'}", flush=True)


# ----------------------------- rules under study -----------------------------

def breakout(n, stop_atr=2.0, use_regime=False):
    """Donchian breakout: close beyond the extreme of the previous n candles."""
    def evaluate(candles, ind, i, regime, btc_regime, p):
        vol = ind["atr"][i]
        if i < n + 1 or not vol or vol <= 0:
            return None, ""
        candle, window = candles[i], candles[i - n:i]
        if candle["close"] > max(c["high"] for c in window):
            direction = "LONG"
        elif candle["close"] < min(c["low"] for c in window):
            direction = "SHORT"
        else:
            return None, ""
        if use_regime and regime != ("UP" if direction == "LONG" else "DOWN"):
            return None, ""
        s = 1 if direction == "LONG" else -1
        return Signal(direction, candle["close"], candle["close"] - s * stop_atr * vol, vol, regime, f"breakout {n}"), ""
    return evaluate


def regime_only(stop_atr=2.0):
    """Control: trade with the regime whenever flat, with no setup at all."""
    def evaluate(candles, ind, i, regime, btc_regime, p):
        vol = ind["atr"][i]
        if not vol or regime == "RANGE" or btc_regime == ("DOWN" if regime == "UP" else "UP"):
            return None, ""
        s = 1 if regime == "UP" else -1
        close = candles[i]["close"]
        return Signal("LONG" if s > 0 else "SHORT", close, close - s * stop_atr * vol, vol, regime, "regime"), ""
    return evaluate


# The one rule allowed to touch the test period. Chosen on train on 2026-11-16: the middle of the
# 4h-breakout plateau (all 24 neighbouring cells were positive on train).
CANDIDATE = dict(evaluate=breakout(40), p=params(trail_atr=4, **PLAIN_EXIT), signal_tf="4h", regime_tf="1d")
SLOW_RISK = dict(leverage=3)        # 4h stops are wide; keep them inside the liquidation buffer


def train(data):
    print("== Chiến lược hiện tại (pullback 1h) và các biến thể thoát lệnh ==")
    row("mặc định", run(data, "train"))
    row("bỏ hòa vốn + chốt một phần", run(data, "train", p=params(**PLAIN_EXIT)))
    row("như trên, trailing 5 ATR", run(data, "train", p=params(trail_atr=5, **PLAIN_EXIT)))
    row("đối chứng: theo xu hướng, không setup", run(data, "train", evaluate=regime_only(), p=params(trail_atr=4, **PLAIN_EXIT)))
    print("\n== Breakout 1h, lọc xu hướng 4h ==")
    for n in (24, 96):
        row(f"breakout {n} nến", run(data, "train", evaluate=breakout(n, use_regime=True), p=params(trail_atr=4, **PLAIN_EXIT)))
    print("\n== Breakout 4h, không lọc: lưới N x SL x trailing ==")
    slow = free_risk(**SLOW_RISK)
    positive = total = 0
    for n in (20, 40, 80):
        for stop in (2.0, 3.0):
            for trail in (3, 4, 5, 6):
                result = run(data, "train", evaluate=breakout(n, stop), p=params(trail_atr=trail, **PLAIN_EXIT),
                             signal_tf="4h", regime_tf="1d", risk_cfg=slow)
                positive += row(f"N={n} SL={stop:g} ATR trailing={trail} ATR", result)["mean"] > 0
                total += 1
    print(f"Số ô dương: {positive}/{total}")
    print("\n== Ứng viên qua mô hình vốn thật (RiskConfig mặc định) ==")
    portfolio("ứng viên (breakout 40 x 4h)", run(data, "train", risk_cfg=RiskConfig(), **CANDIDATE))
    portfolio("pullback 1h mặc định", run(data, "train", risk_cfg=RiskConfig()))


def test(data):
    print("== NGOÀI MẪU: chỉ ứng viên đã chọn trước ==")
    row("ứng viên (breakout 40 x 4h)", run(data, "test", risk_cfg=free_risk(**SLOW_RISK), **CANDIDATE))
    row("pullback 1h mặc định", run(data, "test"))
    print("\n== Qua mô hình vốn thật ==")
    portfolio("ứng viên (breakout 40 x 4h)", run(data, "test", risk_cfg=RiskConfig(), **CANDIDATE))
    portfolio("pullback 1h mặc định", run(data, "test", risk_cfg=RiskConfig()))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "fetch":
        fetch()
    elif command in ("train", "test"):
        loaded = load()
        for name, (a, b) in loaded["periods"].items():
            print(f"{name}: {time.strftime('%Y-%m-%d', time.gmtime(a / 1000))} -> {time.strftime('%Y-%m-%d', time.gmtime(b / 1000))}")
        print()
        (train if command == "train" else test)(loaded)
    else:
        sys.exit(__doc__)
