"""Fee-aware backtester for the bot's strategies.

Replays historical closed candles through the same indicator and entry rules the live
bot uses (indicators.analyze_market_signals + strategy.decide_entry), with the same
TP/SL-as-%-ROE convention, position caps and one position per symbol.

Execution model (deliberately conservative):
- A signal is read on a closed candle; the entry fills at the next candle's open,
  plus slippage, and is skipped if that open moved >1% from the signal close (the
  live bot does the same check).
- Exits are checked on every candle from the entry candle on, using high/low. A gap
  through a level fills at the open. If one candle touches both TP and SL, the SL is
  assumed to fill first.
- Taker fees are charged on entry and exit notional; slippage is charged on every fill.
- Funding, liquidation and exchange lot rounding are not modelled.

CLI example:
    python backtest.py --symbols BTCUSDT ETHUSDT --days 30 --strategy HYBRID_SMART
"""
import argparse
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import requests

from config import settings
from indicators import analyze_market_signals
from strategy import (STRATEGIES, decide_entry, plan_exits, trend_allows, breakeven_levels,
                      stop_too_close_to_liquidation, TREND_HISTORY)
from trading_rules import INTERVAL_SECONDS

WINDOW = 60          # the live bot analyses the last 60 closed candles (limit=61 incl. the open one)
MAX_DAYS = 180
MAX_SYMBOLS = 10

DATA_SOURCES = {
    # Public market data only: no API keys are sent for backtests.
    "live": {"SPOT": "https://api.binance.com/api/v3/klines",
             "FUTURES": "https://fapi.binance.com/fapi/v1/klines"},
    "demo": {"SPOT": settings.SPOT_BASE_URL + "/api/v3/klines",
             "FUTURES": settings.FUTURES_BASE_URL + "/fapi/v1/klines"},
}
DEFAULT_FEE_RATE = {"FUTURES": 0.0005, "SPOT": 0.001}   # Binance taker, no BNB discount


@dataclass
class BacktestConfig:
    symbols: List[str]
    market: str = "FUTURES"
    strategy: str = "HYBRID_SMART"
    interval: str = "15m"
    days: int = 30
    leverage: int = 10
    trade_amount_usdt: float = 20.0
    take_profit_pct: float = 3.0          # % ROE, like the bot config
    stop_loss_pct: float = 1.5            # % ROE
    max_positions: int = 3
    max_total_margin_usdt: Optional[float] = None
    max_open_risk_usdt: Optional[float] = None
    fee_rate: Optional[float] = None      # per side, fraction of notional
    slippage_bps: float = 2.0             # per fill
    cooldown_candles: int = 0             # candles to wait after an exit on the same symbol
    exit_mode: str = "ROE"                # "ROE" (% of margin) or "ATR" (volatility based)
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 3.0
    trend_filter: bool = False            # only trade with EMA200 of the same timeframe
    breakeven_at_r: float = 0.0           # move stop to entry+fees after this many R; 0 = off
    initial_balance: float = 1000.0
    source: str = "live"
    end_ms: Optional[int] = None

    def __post_init__(self):
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in self.symbols if s.strip()))
        if not self.symbols or len(self.symbols) > MAX_SYMBOLS:
            raise ValueError(f"Chọn từ 1 đến {MAX_SYMBOLS} mã")
        if self.market not in ("SPOT", "FUTURES"):
            raise ValueError("Thị trường không hợp lệ")
        if self.strategy not in STRATEGIES:
            raise ValueError("Chiến lược không hợp lệ")
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError("Khung nến không được hỗ trợ")
        if not 1 <= self.days <= MAX_DAYS:
            raise ValueError(f"Số ngày phải từ 1 đến {MAX_DAYS}")
        if self.market == "SPOT":
            self.leverage = 1
        if not 1 <= self.leverage <= 50:
            raise ValueError("Đòn bẩy phải từ 1 đến 50")
        if self.trade_amount_usdt <= 0 or self.initial_balance <= 0:
            raise ValueError("Vốn phải lớn hơn 0")
        if not (0 < self.take_profit_pct < 100 and 0 < self.stop_loss_pct < 100):
            raise ValueError("TP/SL phải trong khoảng 0-100% ROE")
        if not 1 <= self.max_positions <= 10:
            raise ValueError("Số vị thế tối đa phải từ 1 đến 10")
        if self.fee_rate is None:
            self.fee_rate = DEFAULT_FEE_RATE[self.market]
        if not 0 <= self.fee_rate < 0.01 or not 0 <= self.slippage_bps <= 100:
            raise ValueError("Phí hoặc trượt giá không hợp lệ")
        if self.cooldown_candles < 0:
            raise ValueError("Cooldown không hợp lệ")
        if self.exit_mode not in ("ROE", "ATR"):
            raise ValueError("Kiểu TP/SL không hợp lệ")
        if not (0.3 <= self.atr_sl_mult <= 10 and 0.3 <= self.atr_tp_mult <= 20):
            raise ValueError("Hệ số ATR không hợp lệ")
        if self.breakeven_at_r and not 0.5 <= self.breakeven_at_r <= 10:
            raise ValueError("Dời SL hòa vốn: 0 (tắt) hoặc 0.5R-10R")
        if self.source not in DATA_SOURCES:
            raise ValueError("Nguồn dữ liệu không hợp lệ")
        if self.max_total_margin_usdt is None:
            self.max_total_margin_usdt = self.trade_amount_usdt * self.max_positions
        if self.max_open_risk_usdt is None:
            # ATR stops vary per trade, so by default only the margin cap limits exposure.
            self.max_open_risk_usdt = (self.trade_amount_usdt * self.max_positions if self.exit_mode == "ATR"
                                       else self.trade_amount_usdt * self.max_positions * self.stop_loss_pct / 100)


# ----------------------------- data -----------------------------

def fetch_history(symbol, interval, market, start_ms, end_ms, source="live", session=None):
    """Closed candles with open time in [start_ms, end_ms), paginated."""
    url = DATA_SOURCES[source][market]
    page = 1500 if market == "FUTURES" else 1000
    step = INTERVAL_SECONDS[interval] * 1000
    http = session or requests.Session()
    candles, cursor = [], start_ms
    while cursor < end_ms:
        response = http.get(url, params={"symbol": symbol, "interval": interval, "startTime": cursor,
                                         "endTime": end_ms - 1, "limit": page}, timeout=15)
        if response.status_code in (418, 429):
            raise RuntimeError("Binance đang giới hạn yêu cầu. Thử lại sau.")
        if response.status_code == 400:
            raise ValueError(f"{symbol} không có trên {market}: {response.json().get('msg', '')}")
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        for row in rows:
            close_time = int(row[6])
            if close_time >= end_ms:
                continue
            candles.append({"time": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
                            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                            "close_time_ms": close_time, "closed": True})
        cursor = int(rows[-1][0]) + step
        if len(rows) < page:
            break
    return candles


def load_data(config: BacktestConfig, fetch=fetch_history):
    step = INTERVAL_SECONDS[config.interval] * 1000
    end_ms = config.end_ms or int(time.time() * 1000) // step * step   # last fully closed boundary
    # Fetch an extra warm-up window so the first tested candle has full indicator history.
    warmup = TREND_HISTORY if config.trend_filter else WINDOW
    start_ms = end_ms - config.days * 86_400_000 - warmup * step
    with requests.Session() as session:
        return {sym: fetch(sym, config.interval, config.market, start_ms, end_ms, config.source, session)
                for sym in config.symbols}


# ----------------------------- simulation -----------------------------

@dataclass
class _Position:
    symbol: str
    direction: str
    entry_time: int
    entry_price: float
    quantity: float
    margin: float
    take_profit: float
    stop_loss: float
    entry_fee: float
    reason: str
    breakeven_arm: Optional[float] = None
    breakeven_stop: Optional[float] = None
    breakeven_active: bool = False


@dataclass
class BacktestResult:
    config: dict
    summary: dict
    per_symbol: Dict[str, dict]
    exit_reasons: Dict[str, int]
    trades: List[dict] = field(default_factory=list)
    equity_curve: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def run_backtest(config: BacktestConfig, data: Dict[str, List[dict]]) -> BacktestResult:
    step = INTERVAL_SECONDS[config.interval] * 1000
    slip = config.slippage_bps / 10_000
    warnings = []
    series = {}
    for sym in config.symbols:
        candles = sorted(data.get(sym) or [], key=lambda c: c["time"])
        if len(candles) <= WINDOW + 1:
            warnings.append(f"{sym}: không đủ dữ liệu ({len(candles)} nến), bỏ qua")
            continue
        series[sym] = {"candles": candles, "index": {c["time"]: i for i, c in enumerate(candles)}}
    timeline = sorted({c["time"] for s in series.values() for c in s["candles"]})

    open_positions: Dict[str, _Position] = {}
    pending: Dict[str, tuple] = {}         # symbol -> (direction, reason, signal_close, atr)
    cooldown_until: Dict[str, int] = {}
    trades, equity_curve = [], []
    ambiguous = 0
    gap_skips = 0
    liquidation_skips = 0
    trend_skips = 0
    history = TREND_HISTORY if config.trend_filter else WINDOW

    def close(pos, candle, raw_price, reason):
        sign = 1 if pos.direction == "LONG" else -1
        fill = raw_price * (1 - sign * slip)
        gross = sign * (fill - pos.entry_price) * pos.quantity
        exit_fee = fill * pos.quantity * config.fee_rate
        net = gross - pos.entry_fee - exit_fee
        trades.append({
            "symbol": pos.symbol, "direction": pos.direction, "reason": pos.reason, "exit_reason": reason,
            "entry_time": pos.entry_time, "exit_time": candle["close_time_ms"] // 1000,
            "entry_price": pos.entry_price, "exit_price": fill, "quantity": pos.quantity,
            "gross_pnl": gross, "fees": pos.entry_fee + exit_fee, "net_pnl": net,
            "roe_pct": net / pos.margin * 100,
        })
        del open_positions[pos.symbol]
        cooldown_until[pos.symbol] = candle["time"] * 1000 + config.cooldown_candles * step

    for t in timeline:
        # 1) Fill entries signalled on the previous candle at this candle's open.
        for sym, (direction, reason, signal_close, atr) in list(pending.items()):
            del pending[sym]
            i = series[sym]["index"].get(t)
            if i is None:
                continue
            candle = series[sym]["candles"][i]
            if abs(candle["open"] / signal_close - 1) > 0.01:
                gap_skips += 1
                continue
            sign = 1 if direction == "LONG" else -1
            price = candle["open"] * (1 + sign * slip)
            tp, sl = plan_exits(price, direction, exit_mode=config.exit_mode, tp_pct=config.take_profit_pct,
                                sl_pct=config.stop_loss_pct, leverage=config.leverage, atr=atr,
                                atr_sl_mult=config.atr_sl_mult, atr_tp_mult=config.atr_tp_mult)
            if stop_too_close_to_liquidation(price, sl, config.leverage):
                liquidation_skips += 1
                continue
            used_margin = sum(p.margin for p in open_positions.values())
            open_risk = sum(abs(p.entry_price - p.stop_loss) * p.quantity for p in open_positions.values())
            new_risk = config.trade_amount_usdt * config.leverage * abs(price - sl) / price
            if (len(open_positions) >= config.max_positions
                    or used_margin + config.trade_amount_usdt > config.max_total_margin_usdt + 1e-9
                    or open_risk + new_risk > config.max_open_risk_usdt + 1e-9):
                continue
            qty = config.trade_amount_usdt * config.leverage / price
            be = breakeven_levels(price, sl, direction, config.breakeven_at_r, config.market)
            open_positions[sym] = _Position(sym, direction, t, price, qty, config.trade_amount_usdt, tp, sl,
                                            price * qty * config.fee_rate, reason,
                                            be[0] if be else None, be[1] if be else None)

        # 2) Exits on this candle (including the entry candle itself).
        for sym, pos in list(open_positions.items()):
            i = series[sym]["index"].get(t)
            if i is None:
                continue
            c = series[sym]["candles"][i]
            long = pos.direction == "LONG"
            stop = pos.breakeven_stop if pos.breakeven_active else pos.stop_loss
            hit_sl = c["low"] <= stop if long else c["high"] >= stop
            hit_tp = c["high"] >= pos.take_profit if long else c["low"] <= pos.take_profit
            if hit_sl and hit_tp:
                ambiguous += 1
            if hit_sl:
                gapped = c["open"] <= stop if long else c["open"] >= stop
                close(pos, c, c["open"] if gapped else stop,
                      "BREAKEVEN_STOP" if pos.breakeven_active else "STOP_LOSS")
            elif hit_tp:
                gapped = c["open"] >= pos.take_profit if long else c["open"] <= pos.take_profit
                close(pos, c, c["open"] if gapped else pos.take_profit, "TAKE_PROFIT")
            elif pos.breakeven_arm and (c["high"] >= pos.breakeven_arm if long else c["low"] <= pos.breakeven_arm):
                pos.breakeven_active = True   # takes effect from the next candle (conservative)

        # 3) Signals on this closed candle, for symbols without a position.
        for sym, s in series.items():
            i = s["index"].get(t)
            if i is None or i < history - 1 or i + 1 >= len(s["candles"]):
                continue
            if sym in open_positions or t * 1000 < cooldown_until.get(sym, 0):
                continue
            window = s["candles"][i - WINDOW + 1:i + 1]
            if (window[-1]["time"] - window[0]["time"]) * 1000 != (WINDOW - 1) * step:
                continue   # gap in exchange data; the live bot would refuse this window too
            analysis = analyze_market_signals(window)
            decision = decide_entry(config.strategy, analysis, allow_short=config.market == "FUTURES")
            if decision and config.trend_filter:
                closes = [c["close"] for c in s["candles"][i - TREND_HISTORY + 1:i + 1]]
                if not trend_allows(decision[0], closes):
                    trend_skips += 1
                    decision = None
            if decision:
                pending[sym] = (decision[0], decision[1], window[-1]["close"], analysis.get("atr"))


    # Close anything still open at the last close so the result reflects all exposure.
    for sym, pos in list(open_positions.items()):
        last = series[sym]["candles"][-1]
        close(pos, last, last["close"], "END_OF_TEST")
    equity = config.initial_balance
    for trade in sorted(trades, key=lambda tr: tr["exit_time"]):
        equity += trade["net_pnl"]
        equity_curve.append({"time": trade["exit_time"], "equity": round(equity, 4)})

    summary = _summarise(trades, config)
    summary.update(ambiguous_candles=ambiguous, gap_skipped_entries=gap_skips,
                   liquidation_skipped_entries=liquidation_skips, trend_filtered_signals=trend_skips,
                   candles_tested=max(0, len(timeline) - history),
                   period_start=timeline[history - 1] if len(timeline) >= history else None,
                   period_end=timeline[-1] if timeline else None)
    if ambiguous:
        warnings.append(f"{ambiguous} nến chạm cả TP và SL; đã giả định SL khớp trước (thận trọng).")
    per_symbol = {sym: _summarise([t for t in trades if t["symbol"] == sym], config, brief=True)
                  for sym in series}
    reasons: Dict[str, int] = {}
    for trade in trades:
        reasons[trade["exit_reason"]] = reasons.get(trade["exit_reason"], 0) + 1
    return BacktestResult(asdict(config), summary, per_symbol, reasons, trades, equity_curve, warnings)


def _summarise(trades, config, brief=False):
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    net = gross - fees
    out = {"trades": len(trades), "wins": len(wins), "losses": len(losses),
           "winrate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
           "gross_pnl": round(gross, 4), "fees": round(fees, 4), "net_pnl": round(net, 4)}
    if brief:
        return out
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    gross_loss = -sum(losses)
    equity, peak, max_dd = config.initial_balance, config.initial_balance, 0.0
    for trade in sorted(trades, key=lambda t: t["exit_time"]):
        equity += trade["net_pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    # Fixed-size theory: with these TP/SL and fees, the winrate needed to break even.
    notional = config.trade_amount_usdt * config.leverage
    round_trip_cost = notional * (2 * config.fee_rate + 2 * config.slippage_bps / 10_000)
    tp_usdt = config.trade_amount_usdt * config.take_profit_pct / 100 - round_trip_cost
    sl_usdt = config.trade_amount_usdt * config.stop_loss_pct / 100 + round_trip_cost
    if config.exit_mode == "ATR" or config.breakeven_at_r:
        # Distances vary per trade: use the realised average win/loss instead of the formula.
        breakeven_wr = round(avg_loss / (avg_win + avg_loss) * 100, 2) if avg_win + avg_loss > 0 else 0.0
    else:
        breakeven_wr = round(sl_usdt / (tp_usdt + sl_usdt) * 100, 2) if tp_usdt > 0 else 100.0
    out.update({
        "return_pct": round(net / config.initial_balance * 100, 3),
        "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
        "expectancy": round(net / len(trades), 4) if trades else 0.0,
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd / config.initial_balance * 100, 3),
        "fees_pct_of_gross_abs": round(fees / sum(abs(t["gross_pnl"]) for t in trades) * 100, 2)
        if trades and any(t["gross_pnl"] for t in trades) else 0.0,
        "breakeven_winrate_pct": breakeven_wr,
        "net_tp_usdt": round(tp_usdt, 4), "net_sl_usdt": round(-sl_usdt, 4),
    })
    return out


def backtest(config: BacktestConfig, fetch=fetch_history) -> BacktestResult:
    return run_backtest(config, load_data(config, fetch))


# ----------------------------- CLI -----------------------------

def _print(result: BacktestResult):
    s, c = result.summary, result.config
    exits = (f"ATR TP {c['atr_tp_mult']}x / SL {c['atr_sl_mult']}x" if c["exit_mode"] == "ATR"
             else f"TP {c['take_profit_pct']}% / SL {c['stop_loss_pct']}% ROE")
    extras = "".join([" | EMA200" if c["trend_filter"] else "",
                      f" | hòa vốn {c['breakeven_at_r']}R" if c["breakeven_at_r"] else "",
                      f" | nghỉ {c['cooldown_candles']} nến" if c["cooldown_candles"] else ""])
    print(f"\n== {c['strategy']} | {c['market']} {c['leverage']}x | {c['interval']} | {c['days']} ngày | "
          f"{exits}{extras} | phí {c['fee_rate'] * 100:.3f}%/chiều, "
          f"trượt {c['slippage_bps']} bps")
    print(f"Lệnh: {s['trades']}  Thắng: {s['wins']}  Thua: {s['losses']}  Winrate: {s['winrate_pct']}% "
          f"(cần ≥ {s['breakeven_winrate_pct']}% để hòa vốn)")
    print(f"PnL trước phí: {s['gross_pnl']:+.2f}  Phí+trượt: -{s['fees']:.2f}  PnL ròng: {s['net_pnl']:+.2f} USDT "
          f"({s['return_pct']:+.2f}% trên {c['initial_balance']:g})")
    pf = s["profit_factor"]
    print(f"Profit factor: {pf if pf is not None else '-'}  Kỳ vọng/lệnh: {s['expectancy']:+.4f}  "
          f"Max drawdown: {s['max_drawdown']:.2f} ({s['max_drawdown_pct']:.2f}%)")
    print("Theo mã:")
    for sym, row in result.per_symbol.items():
        print(f"  {sym:<14} {row['trades']:>4} lệnh  winrate {row['winrate_pct']:>6}%  ròng {row['net_pnl']:+.2f}")
    print("Lý do thoát:", result.exit_reasons)
    for warning in result.warnings:
        print("! " + warning)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description="Backtest chiến lược bot có tính phí")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--market", choices=["SPOT", "FUTURES"], default="FUTURES")
    parser.add_argument("--strategy", choices=STRATEGIES, nargs="+", default=["HYBRID_SMART"],
                        help="Có thể truyền nhiều chiến lược để so sánh")
    parser.add_argument("--interval", default="15m", choices=list(INTERVAL_SECONDS))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--leverage", type=int, default=10)
    parser.add_argument("--amount", type=float, default=20.0, help="Ký quỹ mỗi lệnh (USDT)")
    parser.add_argument("--tp", type=float, default=3.0, help="Chốt lời theo %% ROE")
    parser.add_argument("--sl", type=float, default=1.5, help="Cắt lỗ theo %% ROE")
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--fee", type=float, default=None, help="Phí mỗi chiều, %% (mặc định 0.05 Futures, 0.1 Spot)")
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--cooldown", type=int, default=0, help="Số nến chờ sau khi thoát lệnh")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--exit-mode", choices=["ROE", "ATR"], default="ROE")
    parser.add_argument("--atr-sl", type=float, default=1.5, help="SL = hệ số x ATR (chế độ ATR)")
    parser.add_argument("--atr-tp", type=float, default=3.0, help="TP = hệ số x ATR (chế độ ATR)")
    parser.add_argument("--trend-filter", action="store_true", help="Chỉ vào lệnh cùng chiều EMA200")
    parser.add_argument("--breakeven", type=float, default=0.0, help="Dời SL về hòa vốn khi lãi N x R (0 = tắt)")
    parser.add_argument("--source", choices=list(DATA_SOURCES), default="live")
    args = parser.parse_args(argv)

    base = dict(symbols=args.symbols, market=args.market, interval=args.interval, days=args.days,
                leverage=args.leverage, trade_amount_usdt=args.amount, take_profit_pct=args.tp,
                stop_loss_pct=args.sl, max_positions=args.max_positions,
                fee_rate=None if args.fee is None else args.fee / 100, slippage_bps=args.slippage_bps,
                cooldown_candles=args.cooldown, initial_balance=args.balance, source=args.source,
                exit_mode=args.exit_mode, atr_sl_mult=args.atr_sl, atr_tp_mult=args.atr_tp,
                trend_filter=args.trend_filter, breakeven_at_r=args.breakeven)
    data = load_data(BacktestConfig(strategy=args.strategy[0], **base))   # fetch once, reuse per strategy
    for strategy in args.strategy:
        _print(run_backtest(BacktestConfig(strategy=strategy, **base), data))


if __name__ == "__main__":
    main()
