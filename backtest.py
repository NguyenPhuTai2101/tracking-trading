"""Portfolio backtester. Runs the exact strategy.py and risk.py the live bot runs.

Execution model (deliberately conservative):
- Each traded setup reads its own timeframe (1h or 4h). A signal is read on a closed candle;
  the entry fills at the next candle's open plus slippage. It is skipped if that open moved
  more than 1% from the signal close. One position per symbol, whatever the setup.
- A position is managed on the candles of the setup that opened it.
- Size comes from risk.plan_entry with the simulated equity, open positions and the same
  circuit breakers, so the daily/weekly limits and the drawdown ladder are part of the test.
- Exits use each candle's high/low. A gap through the stop fills at the open. A candle that
  touches both the stop and the partial target counts as the stop.
- Taker fees on every fill, slippage on every market fill, and historical funding payments.
- Not modelled: liquidation, exchange lot rounding, order book depth.

A backtest is a hypothesis, not proof. The report therefore includes:
- the same settings on the first 70% and the last 30% of the period (by entry time);
- buy & hold over the same period;
- optionally, the result of nudging each key parameter. A strategy that only works at one
  exact setting is fitted to noise.

CLI:
    python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 730 --robust
    python backtest.py --setups THRUST_BREAKOUT TREND_PULLBACK --days 365
"""
import argparse
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

import metrics
import risk
import strategy
from config import settings
from risk import RiskConfig
from strategy import INTERVAL_SECONDS, StrategyParams

BTC = "BTCUSDT"
MAX_DAYS = 1095
MAX_SYMBOLS = 15
IN_SAMPLE_FRACTION = 0.7
SIGNAL_WARMUP = 200        # signal candles loaded before the tested period
REGIME_WARMUP = 500        # regime candles; EMA200 needs a long run-in
MAX_ENTRY_GAP = 0.01
PAGE = 1500

KLINES_URL = {
    # Public market data only: no API keys are sent for backtests.
    "live": "https://fapi.binance.com/fapi/v1/klines",
    "demo": settings.FUTURES_BASE_URL + "/fapi/v1/klines",
}
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

SKIP_LABELS = {
    "CIRCUIT_BREAKER": "Đang nghỉ do giới hạn lỗ ngày/tuần, chuỗi thua hoặc drawdown",
    "MAX_POSITIONS": "Đã đủ số vị thế",
    "SAME_DIRECTION": "Đã đủ vị thế cùng chiều",
    "OPEN_RISK": "Vượt trần tổng rủi ro đang mở",
    "COST": "Phí + trượt giá quá lớn so với 1R",
    "LIQUIDATION": "SL quá gần giá thanh lý",
    "TOO_SMALL": "Khối lượng dưới mức tối thiểu",
    "GAP": "Giá mở lệch quá 1% so với tín hiệu",
    "NEAR_STOP": "Giá mở đã sát SL hoặc đã qua mục tiêu",
}


@dataclass
class BacktestConfig:
    symbols: List[str]
    days: int = 180
    initial_balance: float = 1000.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    source: str = "live"
    end_ms: Optional[int] = None

    def __post_init__(self):
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in self.symbols if s.strip()))
        if not 1 <= len(self.symbols) <= MAX_SYMBOLS:
            raise ValueError(f"Chọn từ 1 đến {MAX_SYMBOLS} mã")
        if not 7 <= self.days <= MAX_DAYS:
            raise ValueError(f"Số ngày phải từ 7 đến {MAX_DAYS}")
        if self.initial_balance <= 0:
            raise ValueError("Vốn ban đầu phải lớn hơn 0")
        if self.source not in KLINES_URL:
            raise ValueError("Nguồn dữ liệu không hợp lệ")


# ----------------------------- data -----------------------------

def fetch_history(symbol, interval, start_ms, end_ms, source="live", session=None):
    """Closed candles with open time in [start_ms, end_ms), paginated."""
    step = INTERVAL_SECONDS[interval] * 1000
    http = session or requests.Session()
    candles, cursor = [], start_ms
    while cursor < end_ms:
        response = http.get(KLINES_URL[source], params={"symbol": symbol, "interval": interval, "startTime": cursor,
                                                        "endTime": end_ms - 1, "limit": PAGE}, timeout=15)
        if response.status_code in (418, 429):
            raise RuntimeError("Binance đang giới hạn yêu cầu. Thử lại sau.")
        if response.status_code == 400:
            raise ValueError(f"{symbol} không có trên Futures: {response.json().get('msg', '')}")
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        for row in rows:
            if int(row[6]) < end_ms:
                candles.append({"time": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
                                "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                                "close_time_ms": int(row[6]), "closed": True})
        cursor = int(rows[-1][0]) + step
        if len(rows) < PAGE:
            break
        time.sleep(0.2)                 # long histories: stay under the request-weight limit
    return candles


def fetch_funding(symbol, start_ms, end_ms, session=None):
    """[(time_ms, rate)] funding settlements in [start_ms, end_ms)."""
    http = session or requests.Session()
    out, cursor = [], start_ms
    while cursor < end_ms:
        response = http.get(FUNDING_URL, params={"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1,
                                                 "limit": 1000}, timeout=15)
        response.raise_for_status()
        rows = response.json()
        out += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        if len(rows) < 1000:
            break
        cursor = int(rows[-1]["fundingTime"]) + 1
    return out


def load_data(config: BacktestConfig):
    """Candles for every timeframe the traded setups read, plus funding. Public data, no API keys."""
    setups = [strategy.SETUPS[key] for key in config.strategy.setups]
    regime_tfs = {s.regime_interval for s in setups}
    tfs = {s.interval for s in setups} | regime_tfs
    step = min(INTERVAL_SECONDS[s.interval] for s in setups) * 1000
    end_ms = config.end_ms or int(time.time() * 1000) // step * step      # last fully closed boundary
    start_ms = end_ms - config.days * 86_400_000
    data = {"start_ms": start_ms, "end_ms": end_ms, "candles": {}, "funding": {}, "funding_ok": True}
    with requests.Session() as session:
        for tf in sorted(tfs, key=INTERVAL_SECONDS.get):
            warmup = (REGIME_WARMUP if tf in regime_tfs else SIGNAL_WARMUP) * INTERVAL_SECONDS[tf] * 1000
            symbols = dict.fromkeys([*config.symbols, BTC]) if tf in regime_tfs else config.symbols
            data["candles"][tf] = {sym: fetch_history(sym, tf, start_ms - warmup, end_ms, config.source, session)
                                   for sym in symbols}
        try:
            for sym in config.symbols:
                data["funding"][sym] = fetch_funding(sym, start_ms, end_ms, session)
        except (requests.RequestException, ValueError, KeyError):
            data["funding"], data["funding_ok"] = {}, False
    return data


# ----------------------------- simulation -----------------------------

class _Regime:
    """Regime of the last candle that had closed by a given time (no look-ahead)."""

    def __init__(self, candles, params):
        self.closes = [c["close_time_ms"] for c in candles]
        self.values = strategy.regime_series(candles, params)

    def at(self, time_ms):
        i = bisect_right(self.closes, time_ms) - 1
        return self.values[i] if i >= 0 else "RANGE"


def run_backtest(config: BacktestConfig, data: Dict) -> Dict:
    params, cfg = config.strategy, config.risk
    start_ms, end_ms = data["start_ms"], data["end_ms"]
    setups = [strategy.SETUPS[key] for key in params.setups]
    seconds = INTERVAL_SECONDS
    base = min((s.interval for s in setups), key=seconds.get)       # the clock the simulation ticks on
    base_step = seconds[base]
    # One feed per (timeframe, symbol): candles, their indicators, and a lookup by open time.
    feeds: Dict[str, Dict[str, Dict]] = {}
    for tf in {s.interval for s in setups}:
        feeds[tf] = {}
        for sym in config.symbols:
            candles = data["candles"].get(tf, {}).get(sym) or []
            if len(candles) >= 100:
                feeds[tf][sym] = {"candles": candles, "ind": strategy.signal_indicators(candles, params),
                                  "index": {c["time"]: i for i, c in enumerate(candles)}}
    symbols = [sym for sym in config.symbols if sym in feeds[base]]
    regimes = {}

    def regime(sym, tf, time_ms):
        key = (sym, tf)
        if key not in regimes:
            regimes[key] = _Regime(data["candles"].get(tf, {}).get(sym) or [], params)
        return regimes[key].at(time_ms)

    funding = {sym: {"rows": data["funding"].get(sym, []), "at": 0} for sym in symbols}
    timeline = sorted({c["time"] for sym in symbols for c in feeds[base][sym]["candles"] if c["time"] * 1000 >= start_ms})

    state = risk.new_state()
    balance = equity = config.initial_balance
    positions: Dict[str, Dict] = {}
    pending: Dict[str, tuple] = {}             # symbol -> (signal, open time of the candle it must fill on)
    last_exit: Dict[str, int] = {}
    trades, curve, skipped = [], [], Counter()
    funding_total = 0.0

    def views():
        return [{"symbol": s, "direction": p["plan"]["direction"], "quantity": p["quantity"],
                 "entry_price": p["plan"]["entry"], "stop_price": p["plan"]["stop"]} for s, p in positions.items()]

    def close(sym, pos, quantity, price, reason, time_ms):
        nonlocal balance
        s = 1 if pos["plan"]["direction"] == "LONG" else -1
        fill = price * (1 - s * cfg.slippage_rate)
        pnl = s * (fill - pos["plan"]["entry"]) * quantity
        fee = fill * quantity * cfg.fee_rate
        balance += pnl - fee
        pos["realized"] += pnl
        pos["fees"] += fee
        pos["exit_notional"] += fill * quantity
        pos["quantity"] -= quantity
        if pos["quantity"] > pos["initial_quantity"] * 1e-9:
            return
        net = pos["realized"] - pos["fees"] + pos["funding"]
        trades.append({
            "symbol": sym, "direction": pos["plan"]["direction"], "setup": pos["setup"],
            "entry_time": pos["entry_time"], "exit_time": time_ms, "entry_price": pos["plan"]["entry"],
            "exit_price": pos["exit_notional"] / pos["initial_quantity"], "quantity": pos["initial_quantity"],
            "gross_pnl": round(pos["realized"], 6), "fees": round(pos["fees"], 6),
            "funding": round(pos["funding"], 6), "net_pnl": round(net, 6),
            "risk_usdt": round(pos["risk_usdt"], 6), "r_multiple": round(net / pos["risk_usdt"], 4),
            "reason": reason, "bars": pos["plan"]["bars"]})
        risk.record_trade(state, net, cfg)
        last_exit[sym] = time_ms
        del positions[sym]

    def enter(sym, signal, candle):
        nonlocal balance
        s = 1 if signal.direction == "LONG" else -1
        if abs(candle["open"] / signal.price - 1) > MAX_ENTRY_GAP:
            skipped["GAP"] += 1
            return
        fill = candle["open"] * (1 + s * cfg.slippage_rate)
        if s * (fill - signal.stop) < 0.5 * s * (signal.price - signal.stop):
            skipped["NEAR_STOP"] += 1
            return
        try:
            plan = strategy.new_plan(signal.direction, fill, signal.stop, signal.exit)
        except ValueError:                      # the open already passed the target
            skipped["NEAR_STOP"] += 1
            return
        used_margin = sum(p["quantity"] * p["plan"]["entry"] for p in positions.values()) / cfg.leverage
        decision = risk.plan_entry(symbol=sym, direction=signal.direction, entry=signal.price, stop=signal.stop,
                                   equity=equity, available=equity - used_margin, positions=views(),
                                   state=state, cfg=cfg)
        if not decision.ok:
            skipped[decision.code] += 1
            return
        quantity = decision.notional / candle["open"]
        notional = quantity * fill
        fee = notional * cfg.fee_rate
        balance -= fee
        positions[sym] = {"plan": plan, "setup": signal.setup, "interval": strategy.SETUPS[signal.setup].interval,
                          "quantity": quantity, "initial_quantity": quantity,
                          "entry_time": candle["time"] * 1000, "exit_notional": 0.0, "realized": 0.0,
                          "fees": fee, "funding": 0.0, "mark": fill,
                          "risk_usdt": quantity * plan["r"] + cfg.round_trip_cost * notional}

    def manage(sym, pos, candle, atr_value):
        """Apply one closed candle of the position's own timeframe."""
        nonlocal balance, funding_total
        plan = pos["plan"]
        s = 1 if plan["direction"] == "LONG" else -1
        close_ms = candle["close_time_ms"]
        feed = funding[sym]
        while feed["at"] < len(feed["rows"]) and feed["rows"][feed["at"]][0] <= close_ms:
            when, rate = feed["rows"][feed["at"]]
            feed["at"] += 1
            # Settlements before the position existed are not this trade's cost.
            if when > max(pos["entry_time"], candle["time"] * 1000):
                payment = -s * pos["quantity"] * candle["open"] * rate      # longs pay a positive rate
                balance += payment
                pos["funding"] += payment
                funding_total += payment
        event = strategy.intrabar_event(plan, candle["open"], candle["high"], candle["low"])
        if event and event[0] == "STOP":
            close(sym, pos, pos["quantity"], event[1], strategy.stop_reason(plan), close_ms)
            return
        if event and event[0] == "TARGET":
            close(sym, pos, pos["quantity"], event[1], "TAKE_PROFIT", close_ms)
            return
        if event and event[0] == "PARTIAL":
            plan["partial_done"] = True
            close(sym, pos, pos["quantity"] * plan["exit"]["partial_fraction"], event[1], "PARTIAL_TP", close_ms)
        if strategy.advance(plan, candle, atr_value) == "TIME_STOP":
            close(sym, pos, pos["quantity"], candle["close"], "TIME_STOP", close_ms)

    def closing(tf, sym, close_s):
        """(feed, index) of the `tf` candle that closes at second `close_s`, if there is one."""
        feed = feeds[tf].get(sym)
        i = feed["index"].get(close_s - seconds[tf]) if feed and close_s % seconds[tf] == 0 else None
        return (feed, i) if i is not None else (None, None)

    for t in timeline:
        close_s = t + base_step
        close_ms = close_s * 1000 - 1
        for sym in symbols:
            i = feeds[base][sym]["index"].get(t)
            if i is None:
                continue
            candle = feeds[base][sym]["candles"][i]
            signal, fill_at = pending.pop(sym, (None, None))
            if signal and fill_at == t and sym not in positions:
                enter(sym, signal, candle)
            if sym in positions:
                feed, j = closing(positions[sym]["interval"], sym, close_s)
                if feed:
                    manage(sym, positions[sym], feed["candles"][j], feed["ind"]["atr"][j])
            if sym in positions:
                positions[sym]["mark"] = candle["close"]
        equity = balance + sum((1 if p["plan"]["direction"] == "LONG" else -1)
                               * (p["mark"] - p["plan"]["entry"]) * p["quantity"] for p in positions.values())
        risk.roll(state, equity, datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc), cfg)
        curve.append({"time": close_ms, "equity": round(equity, 4)})
        for sym in symbols:
            if sym in positions or sym in pending:
                continue
            for setup in setups:                    # in priority order; the first one that fires wins
                feed, j = closing(setup.interval, sym, close_s)
                if not feed:
                    continue
                step_ms = seconds[setup.interval] * 1000
                opened_ms = (close_s - seconds[setup.interval]) * 1000
                cooling_until = last_exit.get(sym, 0) // step_ms * step_ms + cfg.cooldown_candles * step_ms
                if cfg.cooldown_candles and opened_ms < cooling_until:
                    continue
                signal, _ = strategy.evaluate(setup.key, feed["candles"], feed["ind"], j,
                                              regime(sym, setup.regime_interval, close_ms),
                                              regime(BTC, setup.regime_interval, close_ms), params)
                if signal:
                    pending[sym] = (signal, close_s)        # fills at the open of the next base candle
                    break

    for sym, pos in list(positions.items()):       # mark what is still open at the last close
        close(sym, pos, pos["quantity"], pos["mark"], "END_OF_TEST", end_ms)
    if curve:
        curve[-1]["equity"] = round(balance, 4)

    series = {sym: feeds[base][sym] for sym in symbols}
    return _report(config, data, series, trades, curve, skipped, balance, funding_total, state)


def _report(config, data, series, trades, curve, skipped, balance, funding_total, state):
    start_ms, end_ms = data["start_ms"], data["end_ms"]
    split_ms = start_ms + int((end_ms - start_ms) * IN_SAMPLE_FRACTION)
    summary = metrics.summarise(trades)
    summary.update({
        "initial_balance": config.initial_balance,
        "final_equity": round(balance, 2),
        "return_pct": round((balance / config.initial_balance - 1) * 100, 2),
        "max_drawdown_pct": metrics.max_drawdown_pct([p["equity"] for p in curve]),
        "funding": round(funding_total, 4),
        "trades_per_week": round(len(trades) / max(config.days / 7, 1), 2),
        "halted_by_drawdown": state["halted"],
    })
    hold = {}
    for sym, feed in series.items():
        window = [c for c in feed["candles"] if c["time"] * 1000 >= start_ms]
        if window:
            hold[sym] = round((window[-1]["close"] / window[0]["open"] - 1) * 100, 2)
    in_sample = metrics.summarise([t for t in trades if t["entry_time"] < split_ms])
    out_sample = metrics.summarise([t for t in trades if t["entry_time"] >= split_ms])

    warnings = []
    if summary["low_sample"]:
        warnings.append(f"Chỉ có {summary['trades']} lệnh (dưới {metrics.MIN_TRADES_FOR_STATS}): chưa đủ để kết luận. "
                        "Thêm mã hoặc kéo dài giai đoạn.")
    if summary["trades"] and (summary["expectancy_r"] or 0) <= 0:
        warnings.append("Kỳ vọng mỗi lệnh ≤ 0R sau phí: cấu hình này không có lợi thế trong giai đoạn đã thử.")
    if in_sample["net_pnl"] > 0 >= out_sample["net_pnl"] and out_sample["trades"]:
        warnings.append("Có lãi ở 70% đầu nhưng không có lãi ở 30% cuối: kết quả có thể chỉ là khớp quá khứ.")
    if state["halted"]:
        warnings.append(f"Drawdown chạm mức dừng {config.risk.dd_halt_pct:g}%: bot đã ngừng mở lệnh từ thời điểm đó.")
    if not data.get("funding_ok", True):
        warnings.append("Không tải được lịch sử funding: kết quả chưa trừ funding.")
    missing = [s for s in config.symbols if s not in series]
    if missing:
        warnings.append(f"Không đủ dữ liệu nến cho: {', '.join(missing)}.")

    stride = max(1, -(-len(curve) // 500))
    points = curve[::stride]
    if curve and points[-1] is not curve[-1]:
        points.append(curve[-1])
    return {
        "config": {"symbols": config.symbols, "days": config.days, "source": config.source,
                   "risk": config.risk.model_dump(), "strategy": config.strategy.model_dump()},
        "period": {"start": start_ms, "end": end_ms, "split": split_ms},
        "summary": summary,
        "in_sample": in_sample,
        "out_of_sample": out_sample,
        "buy_hold": {"per_symbol": hold,
                     "average_pct": round(sum(hold.values()) / len(hold), 2) if hold else None},
        "per_symbol": metrics.group(trades, "symbol"),
        "by_reason": metrics.group(trades, "reason"),
        "by_setup": metrics.group(trades, "setup"),
        "skipped": [{"code": code, "label": SKIP_LABELS.get(code, code), "count": count}
                    for code, count in skipped.most_common()],
        "equity_curve": points,
        "trades": trades,
        "warnings": warnings,
    }


# ----------------------------- robustness -----------------------------

def robustness(config: BacktestConfig, data: Dict) -> List[Dict]:
    """Re-run with each key parameter of the traded setups nudged down and up.
    Look for results that stay similar: an edge that needs one exact setting is noise."""
    base = config.strategy
    knobs = []
    if "THRUST_BREAKOUT" in base.setups:
        knobs += [("breakout_lookback", "Số nến phá vỡ", base.breakout_lookback // 2, base.breakout_lookback * 2),
                  ("breakout_stop_atr", "SL phá vỡ (ATR)", base.breakout_stop_atr * 0.75, base.breakout_stop_atr * 1.5),
                  ("breakout_trail_atr", "Trailing phá vỡ (ATR)", base.breakout_trail_atr - 1, base.breakout_trail_atr + 1)]
    if "TREND_PULLBACK" in base.setups:
        knobs += [("stop_atr_min", "SL pullback tối thiểu (ATR)", base.stop_atr_min * 0.8, base.stop_atr_min * 1.2),
                  ("trail_atr", "Trailing pullback (ATR)", base.trail_atr * 0.8, base.trail_atr * 1.2),
                  ("pullback_lookback", "Số nến tìm nhịp hồi", base.pullback_lookback - 2, base.pullback_lookback + 2)]
    knobs.append(("adx_min", "ADX tối thiểu", base.adx_min - 5, base.adx_min + 5))
    variants = [("Cấu hình hiện tại", {})]
    for name, label, low, high in knobs:
        for value in (low, high):
            value = round(value) if isinstance(getattr(base, name), int) else round(value, 2)
            variants.append((f"{label} = {value:g}", {name: value}))
    rows = []
    for label, change in variants:
        try:
            params = StrategyParams(**{**base.model_dump(), **change})
        except ValueError:
            continue                    # nudged outside the allowed range
        result = run_backtest(BacktestConfig(config.symbols, config.days, config.initial_balance, config.risk,
                                             params, config.source, config.end_ms), data)
        s = result["summary"]
        rows.append({"label": label, "trades": s["trades"], "return_pct": s["return_pct"],
                     "max_drawdown_pct": s["max_drawdown_pct"], "expectancy_r": s["expectancy_r"],
                     "profit_factor": s["profit_factor"]})
    return rows


# ----------------------------- CLI -----------------------------

def _print_report(result):
    s, i, o = result["summary"], result["in_sample"], result["out_of_sample"]

    def line(name, m):
        print(f"  {name:<14} lệnh {m['trades']:>4} | thắng {m['win_rate_pct'] or 0:>5}% | "
              f"kỳ vọng {m['expectancy_r'] or 0:>+6.2f}R | PF {m['profit_factor'] or 0:>5} | net {m['net_pnl']:>+10.2f}")

    print(f"\nVốn {s['initial_balance']:.0f} → {s['final_equity']:.2f} USDT ({s['return_pct']:+.2f}%), "
          f"drawdown tối đa {s['max_drawdown_pct']}%, phí {s['fees']:.2f}, funding {s['funding']:+.2f}")
    line("Toàn giai đoạn", s)
    line("70% đầu", i)
    line("30% cuối", o)
    print(f"  Mua & giữ (trung bình các mã): {result['buy_hold']['average_pct']}%")
    for row in result["by_setup"]:
        print(f"  {strategy.SETUPS[row['setup']].label:<30} lệnh {row['trades']:>4} | R trung bình {row['avg_r']:>+5.2f} "
              f"| net {row['net_pnl']:>+9.2f}")
    for row in result["skipped"]:
        print(f"  Bỏ qua {row['count']:>4} tín hiệu: {row['label']}")
    for warning in result["warnings"]:
        print(f"  ! {warning}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest các setup của bot với đúng mô hình vốn của bot.")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"])
    parser.add_argument("--setups", nargs="+", default=None, choices=sorted(strategy.SETUPS),
                        help="setup được vào lệnh (mặc định: như cấu hình mặc định của bot)")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=None, help="%% vốn rủi ro mỗi lệnh")
    parser.add_argument("--leverage", type=int, default=None)
    parser.add_argument("--no-short", action="store_true")
    parser.add_argument("--source", choices=sorted(KLINES_URL), default="live")
    parser.add_argument("--robust", action="store_true", help="chạy thêm các biến thể tham số")
    args = parser.parse_args(argv)
    risk_changes = {k: v for k, v in (("risk_pct", args.risk), ("leverage", args.leverage)) if v is not None}
    strategy_changes = {"allow_short": not args.no_short, **({"setups": args.setups} if args.setups else {})}
    config = BacktestConfig(args.symbols, args.days, args.balance, RiskConfig(**risk_changes),
                            StrategyParams(**strategy_changes), args.source)
    print(f"Tải dữ liệu {len(config.symbols)} mã, {config.days} ngày, setup: {', '.join(config.strategy.setups)}...")
    data = load_data(config)
    _print_report(run_backtest(config, data))
    if args.robust:
        print("\nĐộ nhạy tham số:")
        for row in robustness(config, data):
            print(f"  {row['label']:<28} lệnh {row['trades']:>4} | {row['return_pct']:>+7.2f}% | "
                  f"DD {row['max_drawdown_pct']:>5}% | kỳ vọng {row['expectancy_r'] or 0:>+5.2f}R")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
