"""Portfolio backtester. Runs the exact strategy.py and risk.py the live bot runs.

Execution model (deliberately conservative):
- A signal is read on a closed 1h candle; the entry fills at the next candle's open plus
  slippage. It is skipped if that open moved more than 1% from the signal close.
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
    python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 180 --robust
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
from strategy import INTERVAL_SECONDS, REGIME_INTERVAL, SIGNAL_INTERVAL, StrategyParams

BTC = "BTCUSDT"
MAX_DAYS = 365
MAX_SYMBOLS = 15
IN_SAMPLE_FRACTION = 0.7
SIGNAL_WARMUP = 150        # 1h candles loaded before the tested period
REGIME_WARMUP = 500        # 4h candles; EMA200 needs a long run-in
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
    "NEAR_STOP": "Giá mở đã sát SL",
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
    step = INTERVAL_SECONDS[SIGNAL_INTERVAL] * 1000
    end_ms = config.end_ms or int(time.time() * 1000) // step * step      # last fully closed boundary
    start_ms = end_ms - config.days * 86_400_000
    regime_start = start_ms - REGIME_WARMUP * INTERVAL_SECONDS[REGIME_INTERVAL] * 1000
    data = {"start_ms": start_ms, "end_ms": end_ms, "signal": {}, "regime": {}, "funding": {}, "funding_ok": True}
    with requests.Session() as session:
        for sym in config.symbols:
            data["signal"][sym] = fetch_history(sym, SIGNAL_INTERVAL, start_ms - SIGNAL_WARMUP * step, end_ms,
                                                config.source, session)
        for sym in dict.fromkeys([*config.symbols, BTC]):
            data["regime"][sym] = fetch_history(sym, REGIME_INTERVAL, regime_start, end_ms, config.source, session)
        try:
            for sym in config.symbols:
                data["funding"][sym] = fetch_funding(sym, start_ms, end_ms, session)
        except (requests.RequestException, ValueError, KeyError):
            data["funding"], data["funding_ok"] = {}, False
    return data


# ----------------------------- simulation -----------------------------

class _Regime:
    """Regime of the last 4h candle that had closed by a given time (no look-ahead)."""

    def __init__(self, candles, params):
        self.closes = [c["close_time_ms"] for c in candles]
        self.values = strategy.regime_series(candles, params)

    def at(self, time_ms):
        i = bisect_right(self.closes, time_ms) - 1
        return self.values[i] if i >= 0 else "RANGE"


def run_backtest(config: BacktestConfig, data: Dict) -> Dict:
    params, cfg = config.strategy, config.risk
    start_ms, end_ms = data["start_ms"], data["end_ms"]
    step_ms = INTERVAL_SECONDS[SIGNAL_INTERVAL] * 1000
    series = {}
    for sym in config.symbols:
        candles = data["signal"].get(sym) or []
        if len(candles) < 100:
            continue
        series[sym] = {"candles": candles, "ind": strategy.signal_indicators(candles),
                       "index": {c["time"]: i for i, c in enumerate(candles)},
                       "regime": _Regime(data["regime"].get(sym) or [], params),
                       "funding": data["funding"].get(sym, []), "funding_at": 0}
    btc_regime = _Regime(data["regime"].get(BTC) or [], params)
    timeline = sorted({c["time"] for s in series.values() for c in s["candles"] if c["time"] * 1000 >= start_ms})

    state = risk.new_state()
    balance = equity = config.initial_balance
    positions: Dict[str, Dict] = {}
    pending: Dict[str, strategy.Signal] = {}
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
            "symbol": sym, "direction": pos["plan"]["direction"], "entry_time": pos["entry_time"],
            "exit_time": time_ms, "entry_price": pos["plan"]["entry"],
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
        plan = strategy.new_plan(signal.direction, fill, signal.stop, params)
        positions[sym] = {"plan": plan, "quantity": quantity, "initial_quantity": quantity,
                          "entry_time": candle["time"] * 1000, "exit_notional": 0.0, "realized": 0.0,
                          "fees": fee, "funding": 0.0, "mark": fill,
                          "risk_usdt": quantity * plan["r"] + cfg.round_trip_cost * notional}

    def manage(sym, pos, candle, atr_value, feed):
        nonlocal balance, funding_total
        plan = pos["plan"]
        s = 1 if plan["direction"] == "LONG" else -1
        close_ms = candle["close_time_ms"]
        while feed["funding_at"] < len(feed["funding"]) and feed["funding"][feed["funding_at"]][0] <= close_ms:
            when, rate = feed["funding"][feed["funding_at"]]
            feed["funding_at"] += 1
            if when > max(pos["entry_time"], candle["time"] * 1000):
                payment = -s * pos["quantity"] * candle["open"] * rate      # longs pay a positive rate
                balance += payment
                pos["funding"] += payment
                funding_total += payment
        event = strategy.intrabar_event(plan, candle["open"], candle["high"], candle["low"])
        if event and event[0] == "STOP":
            close(sym, pos, pos["quantity"], event[1], strategy.stop_reason(plan), close_ms)
            return
        if event and event[0] == "PARTIAL":
            plan["partial_done"] = True
            close(sym, pos, pos["quantity"] * params.partial_fraction, event[1], "PARTIAL_TP", close_ms)
        if strategy.advance(plan, candle, atr_value, params) == "TIME_STOP":
            close(sym, pos, pos["quantity"], candle["close"], "TIME_STOP", close_ms)
            return
        pos["mark"] = candle["close"]

    for t in timeline:
        for sym, feed in series.items():
            i = feed["index"].get(t)
            if i is None:
                continue
            candle = feed["candles"][i]
            signal = pending.pop(sym, None)
            if signal and sym not in positions:
                enter(sym, signal, candle)
            # Funding settlements before a position existed are not this trade's cost.
            if sym in positions:
                manage(sym, positions[sym], candle, feed["ind"]["atr"][i], feed)
        close_ms = (t + INTERVAL_SECONDS[SIGNAL_INTERVAL]) * 1000 - 1
        equity = balance + sum((1 if p["plan"]["direction"] == "LONG" else -1)
                               * (p["mark"] - p["plan"]["entry"]) * p["quantity"] for p in positions.values())
        risk.roll(state, equity, datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc), cfg)
        curve.append({"time": close_ms, "equity": round(equity, 4)})
        for sym, feed in series.items():
            i = feed["index"].get(t)
            if i is None or sym in positions:
                continue
            exit_ms = last_exit.get(sym, 0)
            if cfg.cooldown_candles and t * 1000 < exit_ms // step_ms * step_ms + cfg.cooldown_candles * step_ms:
                continue
            signal, _ = strategy.evaluate(feed["candles"], feed["ind"], i, feed["regime"].at(close_ms),
                                          btc_regime.at(close_ms), params)
            if signal:
                pending[sym] = signal

    for sym, pos in list(positions.items()):       # mark what is still open at the last close
        close(sym, pos, pos["quantity"], pos["mark"], "END_OF_TEST", end_ms)
    if curve:
        curve[-1]["equity"] = round(balance, 4)

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
        "skipped": [{"code": code, "label": SKIP_LABELS.get(code, code), "count": count}
                    for code, count in skipped.most_common()],
        "equity_curve": points,
        "trades": trades,
        "warnings": warnings,
    }


# ----------------------------- robustness -----------------------------

def robustness(config: BacktestConfig, data: Dict) -> List[Dict]:
    """Re-run with each key parameter nudged down and up. Look for results that stay similar."""
    base = config.strategy
    variants = [("Cấu hình hiện tại", {})]
    for name, label, low, high in (
            ("adx_min", "ADX tối thiểu", base.adx_min - 5, base.adx_min + 5),
            ("stop_atr_min", "SL tối thiểu (ATR)", base.stop_atr_min * 0.8, base.stop_atr_min * 1.2),
            ("trail_atr", "Trailing (ATR)", base.trail_atr * 0.8, base.trail_atr * 1.2),
            ("pullback_lookback", "Số nến tìm nhịp hồi", base.pullback_lookback - 2, base.pullback_lookback + 2)):
        for value in (low, high):
            value = round(value) if name == "pullback_lookback" else round(value, 2)
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
    for row in result["skipped"]:
        print(f"  Bỏ qua {row['count']:>4} tín hiệu: {row['label']}")
    for warning in result["warnings"]:
        print(f"  ! {warning}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest chiến lược pullback thuận xu hướng với mô hình vốn của bot.")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"])
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=None, help="%% vốn rủi ro mỗi lệnh")
    parser.add_argument("--leverage", type=int, default=None)
    parser.add_argument("--no-short", action="store_true")
    parser.add_argument("--source", choices=sorted(KLINES_URL), default="live")
    parser.add_argument("--robust", action="store_true", help="chạy thêm các biến thể tham số")
    args = parser.parse_args(argv)
    risk_changes = {k: v for k, v in (("risk_pct", args.risk), ("leverage", args.leverage)) if v is not None}
    config = BacktestConfig(args.symbols, args.days, args.balance, RiskConfig(**risk_changes),
                            StrategyParams(allow_short=not args.no_short), args.source)
    print(f"Tải dữ liệu {len(config.symbols)} mã, {config.days} ngày...")
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
