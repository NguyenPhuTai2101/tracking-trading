"""Futures bot engine (USDT-M, one-way mode).

strategy.py recognises market cases (setups), risk.py approves and sizes a trade, this module
executes it and keeps the bot's books equal to the exchange:

- Every setup is scanned on its own timeframe. Setups listed in the config are traded; the
  rest are only reported, so their behaviour can be watched before any money follows them.

- Every order is journalled before it is sent. An unknown result is looked up by client id,
  never resent.
- Every filled position gets a reduce-only STOP_MARKET on the exchange (mark price), so the
  stop survives the app going down. When the plan moves the stop, the exchange stop is
  cancelled and re-created at the new level; while that happens the monitor loop enforces
  the stop itself.
- Positions on the exchange that the bot does not know about block new entries.
- Events worth knowing about without opening the app (trades, breakers, halts, the bot losing
  the exchange, a daily summary) go to the notifier. Sending never blocks or fails the loop.
- A closed trade is first booked with estimated fees. Shortly after, its fills are read back
  from the exchange and the estimate is replaced by the real commission and funding.
"""
import json
import logging
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from math import isclose, isfinite
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

import risk
import strategy
from indicators import atr
from risk import RiskConfig
from strategy import REGIME_HISTORY, SETUPS, SIGNAL_HISTORY, StrategyParams

MARKET = "FUTURES"
BTC = "BTCUSDT"
DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
                     "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT"]
SYMBOL_PATTERN = r"^[A-Z0-9]{2,30}USDT$"
ACCOUNT_REFRESH_SEC = 10
EQUITY_SNAPSHOT_SEC = 300
MIN_STOP_GAP = 0.0015      # an exchange stop closer than this to the mark price would trigger at once
SETTLE_INTERVAL_SEC = 60
SETTLE_DELAY_MS = 20_000              # give the exchange time to book the fills before reading them back
SETTLE_WINDOW_MS = 3 * 86_400_000     # stop retrying after this long; the trade keeps its estimate
USD_ASSETS = {"USDT", "USDC", "BUSD", "FDUSD", "BFUSD"}
HEALTH_ALERT_SEC = 300                # the exchange unreachable for this long is worth a notification
# Which notification switch each kind of log line belongs to. Levels not listed are never sent.
NOTIFY_CATEGORY = {"SUCCESS": "trades", "RISK": "risk", "ERROR": "risk", "ALERT": "health", "DAILY": "daily",
                   "SIGNAL": "signals", "CASE": "signals", "WARN": "warnings"}

log = logging.getLogger(__name__)


class NotifyConfig(BaseModel):
    """What is sent to Telegram. The credentials themselves live in the environment, not here."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    trades: bool = True        # position opened, partly closed, closed (with the result), bot started/resumed
    risk: bool = True          # circuit breakers, drawdown halt, the bot stopping itself, exchange-stop failures
    health: bool = True        # the exchange unreachable for 5 minutes, and when it is back
    daily: bool = True         # one summary at 00:00 UTC
    signals: bool = False      # every recognised case, traded or only observed
    warnings: bool = False     # recurring problems such as a symbol that cannot be scanned


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watchlist: List[str] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST), min_length=1, max_length=20)
    scan_interval_sec: int = Field(20, ge=5, le=600)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyParams = Field(default_factory=StrategyParams)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @field_validator("watchlist")
    @classmethod
    def _symbols(cls, symbols):
        import re
        normalized = list(dict.fromkeys(s.strip().upper() for s in symbols))
        if any(not re.fullmatch(SYMBOL_PATTERN, s) for s in normalized):
            raise ValueError("Watchlist chứa mã không hợp lệ (cần dạng XXXUSDT)")
        return normalized


def _now_ms():
    return int(time.time() * 1000)


def _sign(direction):
    return 1 if direction == "LONG" else -1


class TradingEngine:
    def __init__(self, client, journal, autoload=True, notifier=None):
        self.client = client
        self.journal = journal
        self.notifier = notifier
        self.unhealthy_since: Optional[float] = None
        self.health_alerted = False
        self.lock = threading.RLock()
        self.order_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self._process_lock_file = None

        self.config = BotConfig()
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.protective_orders: Dict[str, Dict[str, Any]] = {}
        self.last_signal_candle: Dict[str, int] = {}
        self.last_exit_ms: Dict[str, int] = {}
        self.risk_state = risk.new_state()
        self.unsaved_trades: List[Dict[str, Any]] = []

        self.is_running = False
        self.entries_enabled = False
        self.close_all_requested = False
        self.state_error = False
        self.last_error = None
        self.position_mismatches: Dict[str, Dict[str, Any]] = {}
        self.protection_failures: List[float] = []
        # Run intent survives restarts: a redeploy or crash resumes a bot the user left running.
        self.auto_run = False
        self.resume_pending = False
        self.resume_attempts = 0
        self.next_resume_at = 0.0
        self.stop_reason = None
        self.stopped_at = None

        self.account: Optional[Dict[str, float]] = None
        self.account_checked = 0.0
        self.equity_logged = 0.0
        self.next_candle_check_ms: Dict[str, int] = {}
        self.next_advance_ms: Dict[str, int] = {}
        self.regime_cache: Dict[str, Any] = {}
        self.scan_board: Dict[str, Dict[str, Any]] = {}
        self.warned: Dict[str, float] = {}
        self.next_settle_at = 0.0
        if autoload:
            self.load_state()

    # ------------------------------------------------------------------ persistence

    def add_log(self, level: str, message: str):
        log.info("[%s] %s", level, message)
        try:
            self.journal.add_event(level, message)
        except Exception as exc:     # the log must never take the bot down
            log.error("Không ghi được nhật ký: %s", exc)
        category = NOTIFY_CATEGORY.get(level)
        if self.notifier and category and getattr(self.config.notify, category):
            self.notifier.send(message)

    def _watch_health(self, healthy: bool, detail: str = ""):
        """Tell the user once when the bot has been blind for a while, and once when it sees again."""
        if healthy:
            if self.health_alerted:
                self.add_log("ALERT", "✅ Bot đã kết nối lại với sàn và giám sát bình thường.")
            self.unhealthy_since, self.health_alerted = None, False
            return
        now = time.monotonic()
        self.unhealthy_since = self.unhealthy_since or now
        if not self.health_alerted and now - self.unhealthy_since >= HEALTH_ALERT_SEC:
            self.health_alerted = True
            open_positions = len(self.positions)
            self.add_log("ALERT", f"⚠ Bot không đọc được sàn hơn {HEALTH_ALERT_SEC // 60} phút{': ' + detail if detail else ''}. "
                                  f"Đang có {open_positions} vị thế; SL đã đặt trên sàn vẫn có hiệu lực, "
                                  "nhưng trailing và chốt lời đang tạm ngừng.")

    def _warn(self, key: str, message: str, every: float = 300):
        """Log a recurring problem at most once per `every` seconds."""
        now = time.monotonic()
        if now - self.warned.get(key, -every) >= every:
            self.warned[key] = now
            self.add_log("WARN", message)

    def save_state(self) -> bool:
        with self.lock:
            data = {
                "config": self.config.model_dump(),
                "positions": self.positions,
                "pending_orders": self.pending_orders,
                "protective_orders": self.protective_orders,
                "last_signal_candle": self.last_signal_candle,
                "last_exit_ms": self.last_exit_ms,
                "risk_state": self.risk_state,
                "close_all_requested": self.close_all_requested,
                "auto_run": self.auto_run,
                "stop_reason": self.stop_reason,
                "stopped_at": self.stopped_at,
            }
            try:
                self.journal.commit(data, self.unsaved_trades)
                self.unsaved_trades = []
                return True
            except Exception as exc:
                log.error("Không lưu được trạng thái bot: %s", exc)
                return False

    def _persist(self) -> bool:
        """Save after an order event. Trading without a durable record is not allowed."""
        if self.save_state():
            return True
        self.state_error = True
        self.is_running = self.entries_enabled = self.auto_run = False
        self.last_error = "Không lưu được trạng thái giao dịch"
        self.stop_reason = "Không lưu được trạng thái giao dịch (ổ đĩa/volume)."
        return False

    def load_state(self):
        try:
            data = self.journal.load_state()
            if not data:
                return
            self.config = BotConfig(**data["config"])
            self.positions = data.get("positions", {})
            self.pending_orders = data.get("pending_orders", {})
            self.protective_orders = data.get("protective_orders", {})
            self.last_signal_candle = data.get("last_signal_candle", {})
            self.last_exit_ms = data.get("last_exit_ms", {})
            self.risk_state = {**risk.new_state(), **data.get("risk_state", {})}
            self.close_all_requested = data.get("close_all_requested", False)
            self.auto_run = bool(data.get("auto_run", False))
            self.resume_pending = self.auto_run
            self.stop_reason = data.get("stop_reason")
            self.stopped_at = data.get("stopped_at")
            for sym, pos in self.positions.items():
                # Positions saved before setups existed were all trend pullbacks on 1h.
                pos.setdefault("setup", "TREND_PULLBACK")
                pos.setdefault("interval", SETUPS[pos["setup"]].interval)
                pos.setdefault("order_ids", [])
                pos["plan"].setdefault("exit", strategy.trend_exit(self.config.strategy))
                numbers = [float(pos["quantity"]), float(pos["entry_price"]), float(pos["plan"]["stop"])]
                if (pos["symbol"] != sym or pos["direction"] not in ("LONG", "SHORT")
                        or any(not isfinite(n) or n <= 0 for n in numbers)):
                    raise ValueError("Dữ liệu vị thế đã lưu không hợp lệ")
        except Exception as exc:
            self.state_error = True
            log.error("Không đọc được trạng thái bot: %s", exc)

    def acquire_process_lock(self):
        handle = open(self.journal.path + ".lock", "a+b")
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Đã có tiến trình khác quản lý trạng thái bot. Chạy một worker.")
        self._process_lock_file = handle

    def release_process_lock(self):
        if self._process_lock_file:
            self._process_lock_file.close()
            self._process_lock_file = None

    # ------------------------------------------------------------------ control

    def update_config(self, new: BotConfig):
        with self.order_lock:
            with self.lock:
                if self.state_error:
                    raise RuntimeError("Không đọc hoặc lưu được trạng thái bot.")
                if self.is_running:
                    raise ValueError("Dừng bot trước khi sửa cấu hình.")
                busy = self.positions or self.pending_orders or self.protective_orders
                if new.risk.leverage != self.config.risk.leverage and busy:
                    raise ValueError("Chỉ đổi đòn bẩy khi đã hết vị thế và lệnh chờ.")
            for sym in new.watchlist:
                self.client.validate_symbol(MARKET, sym)
            with self.lock:
                previous, self.config = self.config, new
                if not self.save_state():
                    self.config = previous
                    raise RuntimeError("Không lưu được cấu hình bot.")
                self.regime_cache.clear()
                self.next_candle_check_ms.clear()
                self.scan_board = {s: v for s, v in self.scan_board.items() if s in new.watchlist}
                self.entries_enabled = False
        self.add_log("CONFIG", f"Đã lưu cấu hình: rủi ro {new.risk.risk_pct:g}%/lệnh, "
                               f"đòn bẩy {new.risk.leverage}x, {len(new.watchlist)} mã.")

    def supervise(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
            self.thread.start()
            self.scan_thread.start()

    def start(self):
        with self.order_lock:
            with self.lock:
                if self.is_running:
                    return {"success": False, "message": "Bot đang chạy."}
                if self.state_error or self.close_all_requested:
                    return {"success": False, "message": "Trạng thái chưa sẵn sàng hoặc đang đóng toàn bộ."}
                if not self.client.has_credentials:
                    return {"success": False, "message": "Cần cấu hình khóa Binance Demo."}
                symbols = list(self.config.watchlist)
            try:
                for sym in symbols:
                    self.client.validate_symbol(MARKET, sym)
                if not self._refresh_account(force=True):
                    raise RuntimeError("Không đọc được vốn tài khoản Futures.")
                if self.risk_state["halted"]:
                    raise ValueError(f"{self.risk_state['halt_reason']}. Bấm 'Xác nhận tiếp tục' sau khi đã xem lại.")
                self._reconcile_orders()
                self._reconcile_protections()
                for sym in list(self.positions):
                    self._ensure_protection(sym)
                if not self._protection_ready():
                    raise ValueError("Còn vị thế hoặc lệnh cắt lỗ chưa xác nhận bảo vệ.")
                if not self._check_positions():
                    detail = ", ".join(
                        f"{sym} (sàn: {d.get('exchange_quantity', 0):g}, bot: {d.get('bot_quantity', 0):g})"
                        if "error" not in d else d["error"] for sym, d in self.position_mismatches.items())
                    raise ValueError(f"Vị thế Futures trên sàn chưa khớp dữ liệu bot: {detail or 'chưa xác định'}. "
                                     "Đóng vị thế ngoài bot ở bảng điều khiển hoặc trên Binance rồi chạy lại.")
            except (ValueError, RuntimeError) as exc:
                return {"success": False, "message": str(exc)}
            with self.lock:
                if self.pending_orders:
                    return {"success": False, "message": "Còn lệnh đang chờ xác nhận."}
                self.is_running = self.entries_enabled = self.auto_run = True
                self.last_error = None
                self.resume_pending = False
                self.resume_attempts = 0
                self.stop_reason = self.stopped_at = None
                self.protection_failures = []
                self.save_state()
            self.supervise()
        self.add_log("SUCCESS", "Bot bắt đầu quét tín hiệu.")
        return {"success": True, "message": "Đã bật mở lệnh tự động."}

    def stop(self):
        # The flag is set first so a scan in progress cannot submit after the pause.
        self._halt("Bạn đã bấm dừng.", by_user=True)
        self.supervise()
        return {"success": True, "message": "Đã dừng mở lệnh mới. Bot vẫn quản lý SL của vị thế đang mở."}

    def _halt(self, reason, by_user=False):
        """Stop opening trades and remember why, so the UI can explain it after a restart."""
        with self.lock:
            was_running = self.is_running or self.resume_pending
            self.is_running = self.entries_enabled = self.auto_run = self.resume_pending = False
            if was_running or by_user:
                self.stop_reason = reason
                self.stopped_at = _now_ms()
            self.save_state()
        if was_running:
            self.add_log("INFO" if by_user else "ERROR", f"Bot dừng mở lệnh: {reason}")

    def _maybe_resume(self):
        """After a restart, resume a bot that was running, with the same checks as a manual start."""
        with self.lock:
            if not self.resume_pending or self.is_running or time.monotonic() < self.next_resume_at:
                return
            self.resume_attempts += 1
            attempt = self.resume_attempts
            self.next_resume_at = time.monotonic() + 30
        result = self.start()
        if result.get("success"):
            self.add_log("SUCCESS", "Đã tự chạy lại bot sau khi máy chủ khởi động lại.")
        elif attempt >= 10:
            self._halt(f"Không tự chạy lại được sau khi khởi động lại: {result.get('message')}")
        else:
            self.add_log("WARN", f"Tự chạy lại lần {attempt} chưa được: {result.get('message')}")

    def acknowledge_drawdown(self):
        """The user reviewed the drawdown halt: restart the equity high-water mark from now."""
        account = self._refresh_account(force=True)
        if not account:
            raise RuntimeError("Không đọc được vốn tài khoản Futures.")
        with self.lock:
            if not self.risk_state["halted"]:
                raise ValueError("Bot không ở trạng thái dừng do drawdown.")
            risk.acknowledge_halt(self.risk_state, account["equity"])
            if not self.save_state():
                raise RuntimeError("Không lưu được trạng thái bot.")
        self.add_log("CONFIG", f"Đã xác nhận drawdown. Đỉnh vốn tính lại từ {account['equity']:.2f} USDT.")
        return {"success": True, "message": "Đã đặt lại đỉnh vốn. Có thể chạy lại bot."}

    def shutdown(self):
        with self.lock:
            self.is_running = False
            self.stop_event.set()
        for thread in (self.thread, self.scan_thread):
            if thread:
                thread.join()
        if self.notifier:
            self.notifier.close()
        self.release_process_lock()

    # ------------------------------------------------------------------ loops

    def _run_loop(self):
        while not self.stop_event.is_set():
            try:
                self._maybe_resume()
                self._tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.entries_enabled = False
                self._warn("tick", f"Lỗi giám sát: {exc}", every=60)
                self._watch_health(False, str(exc))
            self.stop_event.wait(2)

    def _scan_loop(self):
        while not self.stop_event.is_set():
            if self.is_running and self.entries_enabled:
                for sym in list(self.config.watchlist):
                    if self.stop_event.is_set() or not self.is_running:
                        break
                    try:
                        self._scan_symbol(sym)
                    except Exception as exc:
                        self._warn(f"scan:{sym}", f"Bỏ qua {sym}: {exc}")
            self.stop_event.wait(self.config.scan_interval_sec)

    def _tick(self):
        self._reconcile_orders()
        self._reconcile_protections()
        for sym in list(self.positions):
            self._ensure_protection(sym)
        account = self._refresh_account()
        if self.client.has_credentials:
            self._watch_health(account is not None, "không đọc được tài khoản Futures")
        try:
            self._settle_trades()
        except Exception as exc:        # bookkeeping must never interrupt position monitoring
            self._warn("settle", f"Chưa đối soát được phí/funding với sàn: {exc}")
        with self.lock:
            symbols = list(self.positions)
            needs_check = self.is_running or bool(symbols)
        if needs_check:
            try:
                self.entries_enabled = self._check_positions() and self._protection_ready()
            except (RuntimeError, ValueError) as exc:
                self.entries_enabled = False
                self.position_mismatches = {"account": {"error": str(exc)}}
                self._warn("positions", f"Đối soát Futures thất bại: {exc}")
        for sym in symbols:
            try:
                self._monitor_position(sym)
            except Exception as exc:
                self.entries_enabled = False
                self._warn(f"monitor:{sym}", f"Lỗi theo dõi {sym}: {exc}", every=60)
        if self.close_all_requested:
            with self.order_lock:
                for sym in list(self.positions):
                    result = self._close_position(sym, "MANUAL_CLOSE_ALL")
                    if not result["success"] and not result.get("pending"):
                        self._warn(f"closeall:{sym}", f"{sym}: {result.get('error', 'Đang đóng vị thế')}", every=30)
                with self.lock:
                    if not self.positions and not self.pending_orders and not self.protective_orders:
                        self.close_all_requested = False
                        self.save_state()

    # ------------------------------------------------------------------ account & risk

    def _position_views(self):
        return [{"symbol": sym, "direction": p["direction"], "quantity": p["quantity"],
                 "entry_price": p["entry_price"], "stop_price": p["plan"]["stop"]}
                for sym, p in self.positions.items()]

    def _refresh_account(self, force=False):
        """Read account equity, feed it to the risk state and snapshot it for the dashboard."""
        if not self.client.has_credentials:
            return None
        now = time.monotonic()
        with self.lock:
            if not force and self.account and now - self.account_checked < ACCOUNT_REFRESH_SEC:
                return self.account
        info = self.client.get_futures_account_info()
        if not info.get("success"):
            self._warn("account", f"Không đọc được tài khoản Futures: {info.get('error', 'không rõ')}")
            return None
        equity = float(info["totalMarginBalance"])
        with self.lock:
            self.account = {"equity": equity, "available": float(info["availableBalance"]),
                            "wallet": float(info["totalWalletBalance"]),
                            "unrealized": float(info["totalUnrealizedProfit"]), "time": _now_ms()}
            self.account_checked = now
            before = deepcopy(self.risk_state)
            events = risk.roll(self.risk_state, equity, datetime.now(timezone.utc), self.config.risk)
            # The peak moves on almost every read; only breaker and day/week changes need saving now.
            changed = {k: v for k, v in self.risk_state.items() if k != "peak_equity"} != \
                      {k: v for k, v in before.items() if k != "peak_equity"}
            snapshot_due = equity > 0 and now - self.equity_logged >= EQUITY_SNAPSHOT_SEC
            if snapshot_due:
                self.equity_logged = now
            if changed or snapshot_due:
                self.save_state()
            halted, account = self.risk_state["halted"], self.account
        if snapshot_due:
            try:
                self.journal.add_equity(equity)
            except Exception as exc:
                log.error("Không ghi được đường vốn: %s", exc)
        for message in events:
            self.add_log("RISK", f"⏸ {message}")
        if before["day"] and before["day"] != self.risk_state["day"]:
            self._daily_summary(before, equity)
        if halted and (self.is_running or self.resume_pending):
            self._halt(self.risk_state["halt_reason"])
        return account

    def _daily_summary(self, yesterday, equity):
        """One message when the UTC day rolls over: what the day did and what is still open."""
        day_start = datetime.strptime(yesterday["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ms = int(day_start.timestamp() * 1000)
        trades = [t for t in self.journal.trades(start_ms) if t["exit_time"] < start_ms + 86_400_000]
        base = yesterday.get("day_start_equity") or equity
        change = (equity / base - 1) * 100 if base else 0.0
        with self.lock:
            open_lines = [f"  {sym} {p['direction']} {p.get('r_now', 0):+.2f}R" for sym, p in self.positions.items()]
            state = "đang chạy" if self.is_running else "đang dừng"
        lines = [f"📊 Tổng kết {yesterday['day']} (UTC)",
                 f"Vốn: {equity:,.2f} USDT ({change:+.2f}% trong ngày)",
                 f"Lệnh đã đóng: {len(trades)}, ròng {sum(t['net_pnl'] for t in trades):+.2f} USDT"
                 + (f" ({sum(t['r_multiple'] for t in trades):+.2f}R)" if trades else ""),
                 f"Vị thế đang mở: {len(open_lines)}" + ("" if not open_lines else "\n" + "\n".join(open_lines)),
                 f"Bot {state}."]
        self.add_log("DAILY", "\n".join(lines))

    def _settle_trades(self):
        """Replace the estimated fees of recently closed trades with what the exchange booked."""
        now = time.monotonic()
        if not self.client.has_credentials or now < self.next_settle_at:
            return
        self.next_settle_at = now + SETTLE_INTERVAL_SEC
        now_ms = _now_ms()
        for trade in self.journal.unsettled_trades(now_ms - SETTLE_WINDOW_MS):
            if now_ms - trade["exit_time"] < SETTLE_DELAY_MS:
                continue
            actual = self._actual_result(trade)
            if not actual:
                continue                    # not booked yet or lookup failed: try again next round
            self.journal.settle_trade(trade["position_id"], **actual)
            self.add_log("INFO", f"Đối soát {trade['symbol']}: phí {'thật' if actual['settled'] == 1 else 'ước tính'} "
                                 f"{actual['fees']:.4f}, funding {actual['funding']:+.4f}, ròng {actual['net_pnl']:+.2f} USDT "
                                 f"({actual['r_multiple']:+.2f}R; ước tính ban đầu {trade['net_pnl']:+.2f}).")

    def _actual_result(self, trade):
        """What the exchange booked for a closed trade, or None while that cannot be established."""
        order_ids = json.loads(trade["order_ids"] or "[]")
        if not order_ids:
            return None
        fills = []
        for order_id in order_ids:
            rows = self.client.get_futures_order_fills(trade["symbol"], order_id)
            if rows is None:
                return None
            fills += rows
        # Every unit was bought once and sold once; anything else means fills are still missing.
        if not isclose(sum(float(f["qty"]) for f in fills), 2 * trade["quantity"], rel_tol=1e-6):
            return None
        payments = self.client.get_futures_funding(trade["symbol"], trade["entry_time"], trade["exit_time"])
        if payments is None:
            return None
        in_usd = all(f.get("commissionAsset") in USD_ASSETS for f in fills)
        fees = sum(float(f["commission"]) for f in fills) if in_usd else trade["fees"]
        gross = sum(float(f["realizedPnl"]) for f in fills)
        funding = sum(float(p["income"]) for p in payments)
        net = gross - fees + funding
        return {"gross_pnl": round(gross, 8), "fees": round(fees, 8), "funding": round(funding, 8),
                "net_pnl": round(net, 8),
                "r_multiple": round(net / trade["risk_usdt"], 4) if trade["risk_usdt"] > 0 else 0.0,
                "settled": 1 if in_usd else 2}

    # ------------------------------------------------------------------ scanning & entry

    def _regime(self, sym, interval, now_ms):
        cached = self.regime_cache.get((sym, interval))
        if cached and now_ms < cached[1]:
            return cached[0]
        candles = self.client.get_klines(sym, interval=interval, limit=REGIME_HISTORY + 1, market=MARKET)
        closed = strategy.closed_candles(candles, interval, now_ms, minimum=1)
        value = strategy.regime_series(closed, self.config.strategy)[-1]
        valid_until = closed[-1]["close_time_ms"] + strategy.INTERVAL_SECONDS[interval] * 1000
        self.regime_cache[(sym, interval)] = (value, valid_until)
        return value

    def _can_enter(self, sym):
        return (self.is_running and self.entries_enabled and not self.close_all_requested
                and not self.stop_event.is_set() and not self.pending_orders
                and sym not in self.positions and self._protection_ready())

    def _scan_symbol(self, sym):
        """Look for every known case on its own timeframe. Traded setups may open a position;
        the others are only reported."""
        with self.lock:
            if not self.is_running or not self.entries_enabled:
                return
            params, cooldown = self.config.strategy, self.config.risk.cooldown_candles
            last_exit = self.last_exit_ms.get(sym, 0)
        now_ms = self.client._get_timestamp()
        for interval in sorted({s.interval for s in SETUPS.values()}, key=strategy.INTERVAL_SECONDS.get):
            self._scan_interval(sym, interval, now_ms, params, cooldown, last_exit)

    def _scan_interval(self, sym, interval, now_ms, params, cooldown, last_exit):
        key = f"{sym}:{interval}"
        if now_ms < self.next_candle_check_ms.get(key, 0):
            return
        candles = self.client.get_klines(sym, interval=interval, limit=SIGNAL_HISTORY + 1, market=MARKET)
        if candles and not candles[-1]["closed"]:
            self.next_candle_check_ms[key] = candles[-1]["close_time_ms"] + 1
        closed = strategy.closed_candles(candles, interval, now_ms, minimum=80)
        close_time = closed[-1]["close_time_ms"]
        if self.last_signal_candle.get(key, 0) >= close_time:
            return
        ind, last = strategy.signal_indicators(closed, params), len(closed) - 1
        step_ms = strategy.INTERVAL_SECONDS[interval] * 1000
        cooling = bool(cooldown) and closed[-1]["time"] * 1000 < last_exit // step_ms * step_ms + cooldown * step_ms
        cases, chosen, observed = {}, None, []
        for setup in SETUPS.values():
            if setup.interval != interval:
                continue
            regime = self._regime(sym, setup.regime_interval, now_ms)
            signal, note = strategy.evaluate(setup.key, closed, ind, last, regime,
                                             self._regime(BTC, setup.regime_interval, now_ms), params)
            traded = setup.key in params.setups
            if signal and traded and not cooling and chosen is None:
                chosen = signal
            elif signal:
                why = "chỉ quan sát" if not traded else f"đang nghỉ {cooldown} nến sau lệnh vừa đóng" if cooling \
                    else "đã có tín hiệu khác ưu tiên hơn"
                observed.append((setup, signal, why))
                note = f"{note} ({why})"
            cases[setup.key] = {"setup": setup.key, "interval": interval, "traded": traded, "regime": regime,
                                "note": note, "signal": signal.direction if signal else None, "time": close_time}
        with self.lock:
            self.last_signal_candle[key] = close_time
            board = self.scan_board.setdefault(sym, {"symbol": sym, "cases": {}, "tags": {}})
            board["cases"].update(cases)
            board["tags"][interval] = strategy.market_tags(ind, last)
            board.update(price=closed[-1]["close"], time=close_time)
            if not self.save_state():
                self.state_error = True
        if self.state_error:
            self._halt("Không lưu được trạng thái bot (ổ đĩa/volume).")
            return
        for setup, signal, why in observed:
            self.add_log("CASE", f"👁 {sym} {interval}: «{setup.label}» {signal.direction} @ {signal.price:g} ({why}).")
        if chosen:
            self.add_log("SIGNAL", f"🚀 {sym} {interval}: «{SETUPS[chosen.setup].label}» {chosen.direction} @ "
                                   f"{chosen.price:g}, SL {chosen.stop:g}. {chosen.reason}")
            self._execute_entry(sym, chosen)

    def _skip(self, sym, signal, reason):
        self.add_log("SKIP", f"Bỏ tín hiệu {sym} {signal.direction}: {reason}")
        with self.lock:
            case = self.scan_board.get(sym, {}).get("cases", {}).get(signal.setup)
            if case:
                case["note"] = f"Có tín hiệu nhưng mô hình vốn chặn: {reason}"

    def _execute_entry(self, sym, signal):
        s = _sign(signal.direction)
        with self.order_lock:
            with self.lock:
                if not self._can_enter(sym):
                    return
                cfg = self.config.risk
            account = self._refresh_account(force=True)
            if not account:
                raise RuntimeError("Không xác nhận được vốn tài khoản trước khi mở lệnh")
            with self.lock:
                decision = risk.plan_entry(
                    symbol=sym, direction=signal.direction, entry=signal.price, stop=signal.stop,
                    equity=account["equity"], available=account["available"],
                    positions=self._position_views(), state=self.risk_state, cfg=cfg)
            if not decision.ok:
                self._skip(sym, signal, decision.reason)
                return
            if not self._check_positions(sym):
                self._skip(sym, signal, "vị thế trên sàn khác dữ liệu của bot")
                return
            leverage = self.client.set_leverage(sym, cfg.leverage)
            if not leverage.get("success") or int(leverage.get("leverage", cfg.leverage)) != cfg.leverage:
                self._skip(sym, signal, f"không xác nhận được đòn bẩy ({leverage.get('error')})")
                return
            try:
                qty, price = self.client.prepare_entry(MARKET, sym, decision.notional / cfg.leverage,
                                                       cfg.leverage, signal.price)
            except ValueError as exc:
                self._skip(sym, signal, str(exc))
                return
            if s * (price - signal.stop) < 0.5 * s * (signal.price - signal.stop):
                self._skip(sym, signal, "giá đã chạy quá sát SL so với lúc có tín hiệu")
                return
            order = {"id": uuid4().hex, "market": MARKET, "kind": "entry", "direction": signal.direction,
                     "leverage": cfg.leverage, "reason": signal.reason, "regime": signal.regime,
                     "stop_price": signal.stop, "signal_price": signal.price, "setup": signal.setup,
                     "interval": SETUPS[signal.setup].interval, "exit": dict(signal.exit)}
            with self.lock:
                if not self.is_running or self.close_all_requested or self.stop_event.is_set():
                    return
            scale = "" if decision.scale == 1 else f" (rủi ro ×{decision.scale:g} do drawdown)"
            self.add_log("INFO", f"{sym}: vốn {account['equity']:.2f} USDT, rủi ro lệnh ≈ {decision.risk_usdt:.2f} USDT"
                                 f"{scale}, giá trị vị thế ≈ {qty * price:.2f} USDT.")
            result = self._submit_order(sym, order, qty)
            if not result["success"]:
                self.add_log("WARN", f"{sym}: {result['error']}")

    # ------------------------------------------------------------------ position management

    def _monitor_position(self, sym):
        with self.lock:
            pos = self.positions.get(sym)
        if not pos:
            return
        if pos.get("exit_requested"):
            self._request_exit(sym, pos, pos["exit_requested"])
            return
        ticker = self.client.get_ticker_price(sym, market=MARKET, mark=True)
        price = float(ticker.get("price", 0)) if ticker else 0.0
        if price <= 0:
            raise RuntimeError(f"Không lấy được giá giám sát {sym}")
        plan, s = pos["plan"], _sign(pos["direction"])
        with self.lock:
            pos["current_price"] = price
            pos["pnl_usdt"] = round(s * (price - pos["entry_price"]) * pos["quantity"], 4)
            pos["r_now"] = round(s * (price - pos["entry_price"]) / plan["r"], 2)
        if sym in self.pending_orders:
            return                      # an order for this position is still being reconciled
        if pos.get("partial_requested"):
            self._close_position(sym, "PARTIAL_TP", expected_position=pos, quantity=pos["partial_requested"])
            return
        event = strategy.intrabar_event(plan, price, price, price)
        if event and event[0] == "STOP":
            reason = strategy.stop_reason(plan)
            self.add_log("SL", f"🛑 {sym} {pos['direction']}: giá {price:g} chạm SL {plan['stop']:g} "
                               f"({reason}, {pos['r_now']:+.2f}R)")
            self._request_exit(sym, pos, reason)
            return
        if event and event[0] == "TARGET":
            self.add_log("TP", f"🎯 {sym} {pos['direction']}: giá {price:g} chạm mục tiêu {event[1]:g} "
                               f"({pos['r_now']:+.2f}R)")
            self._request_exit(sym, pos, "TAKE_PROFIT")
            return
        if event and event[0] == "PARTIAL":
            self._take_partial(sym, pos, price)
            return
        self._advance_position(sym, pos, price)
        if pos.get("stop_sync_pending"):
            self._sync_exchange_stop(sym, pos, price)

    def _advance_position(self, sym, pos, price):
        """Feed newly closed candles of the setup's timeframe to the exit plan: breakeven, trailing stop, time stop."""
        now_ms = self.client._get_timestamp()
        if now_ms < self.next_advance_ms.get(sym, 0):
            return
        candles = self.client.get_klines(sym, interval=pos["interval"], limit=100, market=MARKET)
        if not candles:
            return
        self.next_advance_ms[sym] = candles[-1]["close_time_ms"] + 1 if not candles[-1]["closed"] else now_ms + 5000
        closed = [c for c in candles if c["closed"]]
        volatility = atr(closed)
        outcome, advanced = None, False
        with self.lock:
            plan, before = pos["plan"], pos["plan"]["stop"]
            seen = max(pos.get("last_candle_ms", 0), pos["entry_time_ms"])
            for i, candle in enumerate(closed):
                if candle["close_time_ms"] <= seen:
                    continue
                outcome = strategy.advance(plan, candle, volatility[i])
                pos["last_candle_ms"] = seen = candle["close_time_ms"]
                advanced = True
                if outcome:
                    break
            moved = plan["stop"] != before
            if moved:
                pos["stop_sync_pending"] = True
            if advanced:
                self.save_state()
        if moved:
            kind = "hòa vốn" if plan["stop_kind"] == "BREAKEVEN" else "trailing"
            self.add_log("INFO", f"{sym}: dời SL ({kind}) {before:g} → {plan['stop']:g}.")
        if outcome == "TIME_STOP":
            self.add_log("EXIT", f"⏱ {sym}: hết thời gian giữ lệnh sau {plan['bars']} nến {pos['interval']}, đóng lệnh.")
            self._request_exit(sym, pos, "TIME_STOP")

    def _sync_exchange_stop(self, sym, pos, price):
        """Move the exchange stop to the plan's stop: cancel it here, _ensure_protection re-creates it."""
        stop, s = pos["plan"]["stop"], _sign(pos["direction"])
        if s * (price - stop) / price < MIN_STOP_GAP:
            return      # too close to trigger price; the monitor loop enforces the stop meanwhile
        with self.lock:
            for order in self.protective_orders.values():
                if order["symbol"] == sym and not isclose(order["trigger_price"], stop, rel_tol=1e-12):
                    order["cancel_requested"] = True
            pos.pop("stop_sync_pending", None)
            self._persist()

    def _take_partial(self, sym, pos, price):
        plan = pos["plan"]
        fraction = plan["exit"].get("partial_fraction") or 0
        try:
            qty = float(self.client.format_futures_quantity(sym, pos["quantity"] * fraction, market_order=True))
            rest = float(self.client.format_futures_quantity(sym, pos["quantity"] - qty, market_order=True))
            minimum = float(self.client.load_futures_symbol_filters(sym).get("minNotional", 0))
            if min(qty, rest) * price < minimum:
                raise ValueError("dưới mức tối thiểu")
        except (ValueError, RuntimeError):
            with self.lock:
                plan["partial_done"] = True
                self.save_state()
            self.add_log("INFO", f"{sym}: vị thế quá nhỏ để chốt một phần; giữ nguyên và trailing.")
            return
        with self.lock:
            pos["partial_requested"] = qty
            if not self._persist():
                return
        self.add_log("TP", f"🎯 {sym} {pos['direction']}: đạt {plan['exit'].get('partial_r', 0):g}R tại {price:g}, "
                           f"chốt {fraction * 100:g}% vị thế.")
        result = self._close_position(sym, "PARTIAL_TP", expected_position=pos, quantity=qty)
        if not result["success"] and not result.get("pending"):
            with self.lock:
                plan["partial_done"] = True
                pos.pop("partial_requested", None)
                self.save_state()
            self.add_log("WARN", f"{sym}: không chốt được một phần ({result.get('error')}); tiếp tục trailing.")

    def _request_exit(self, sym, pos, reason):
        with self.order_lock:
            if sym in self.pending_orders:
                with self.lock:
                    self.pending_orders[sym]["cancel_requested"] = True
                    self.pending_orders[sym]["exit_reason"] = reason
                    pos["exit_requested"] = reason
                    self.save_state()
                return
            result = self._close_position(sym, reason, expected_position=pos)
            if not result["success"] and not result.get("pending"):
                self._warn(f"exit:{sym}", f"{sym}: {result.get('error')}", every=30)

    def _close_position(self, sym, reason="MANUAL_CLOSE", expected_position=None, quantity=None):
        """Market-close a position (or `quantity` of it). A full close first cancels its exchange stop."""
        partial = quantity is not None
        with self.order_lock:
            with self.lock:
                pos = self.positions.get(sym)
                if sym in self.pending_orders:
                    return {"success": False, "pending": True, "error": "Lệnh trước đang chờ đối soát."}
                if expected_position is not None and pos is not expected_position:
                    return {"success": False, "error": "Vị thế đã thay đổi."}
                if not pos or pos["quantity"] <= 0:
                    return {"success": False, "error": "Không có khối lượng đã khớp để đóng."}
                if not partial:
                    pos["exit_requested"] = reason
                    if not self._persist():
                        return {"success": False, "error": "Không lưu được yêu cầu đóng vị thế."}
            if not partial:
                self._reconcile_protections(cancel_symbol=sym)
            with self.lock:
                if not partial and any(o["symbol"] == sym for o in self.protective_orders.values()):
                    return {"success": False, "pending": True,
                            "error": "Đang xác nhận hủy/khớp lệnh cắt lỗ trên sàn."}
                pos = self.positions.get(sym)
                if not pos:
                    return {"success": True, "remaining_quantity": 0}
                order = {"id": uuid4().hex, "market": MARKET, "kind": "close", "direction": pos["direction"],
                         "reason": reason, "partial": partial}
                qty = quantity if partial else pos["quantity"]
            try:
                rows = self.client.get_futures_positions(sym, strict=True)
                actual = next((p for p in rows if p["symbol"] == sym and p.get("positionSide", "BOTH") == "BOTH"), None)
                signed = float(actual["positionAmt"]) if actual else 0.0
            except (RuntimeError, ValueError):
                return {"success": False, "error": "Không xác nhận được vị thế trên sàn."}
            if signed * _sign(order["direction"]) <= 0:
                return {"success": False, "error": "Vị thế trên sàn đã đóng hoặc đổi chiều. Cần đối soát thủ công."}
            return self._submit_order(sym, order, min(qty, abs(signed)))

    def close_position(self, sym):
        """Manual close from the UI."""
        with self.lock:
            pos = self.positions.get(sym)
        if not pos:
            raise ValueError(f"Bot không có vị thế {sym}.")
        self.add_log("EXIT", f"Bạn yêu cầu đóng {sym}.")
        return self._close_position(sym, "MANUAL_CLOSE", expected_position=pos)

    def close_all_positions(self):
        self._halt("Bạn đã bấm đóng toàn bộ.", by_user=True)
        with self.lock:
            self.close_all_requested = True
        with self.order_lock:
            if not self.save_state():
                return {"success": False, "message": "Không lưu được yêu cầu đóng toàn bộ."}
            self._reconcile_orders()
            self._reconcile_protections()
            for sym in list(self.positions):
                self._close_position(sym, "MANUAL_CLOSE_ALL")
            # Positions the bot does not track are never touched here: they may be manual
            # trades. close_untracked_positions needs an explicit symbol list instead.
            with self.lock:
                complete = not self.positions and not self.pending_orders and not self.protective_orders
                if complete:
                    self.close_all_requested = False
                    self.save_state()
        self.supervise()
        return {"success": complete, "pending": not complete,
                "message": "Đã đóng toàn bộ và dừng mở lệnh mới." if complete
                else "Đã dừng mở lệnh mới. Bot đang tiếp tục hủy lệnh chờ và đóng vị thế."}

    def close_untracked_positions(self, symbols):
        """Close exchange positions the bot does not manage, only for symbols the user named."""
        symbols = list(dict.fromkeys(s.strip().upper() for s in symbols or [] if s and s.strip()))
        if not symbols:
            raise ValueError("Chọn ít nhất một mã cần đóng.")
        results = []
        with self.order_lock:
            with self.lock:
                if self.is_running:
                    raise ValueError("Dừng bot trước khi đóng vị thế ngoài bot.")
                if self.state_error:
                    raise RuntimeError("Không đọc hoặc lưu được trạng thái bot.")
                managed = set(self.positions) | set(self.pending_orders) | {
                    o["symbol"] for o in self.protective_orders.values()}
            for sym in symbols:
                if sym in managed:
                    results.append({"symbol": sym, "success": False, "error": "Mã do bot quản lý."})
                    continue
                try:
                    rows = [p for p in self.client.get_futures_positions(sym, strict=True) if p["symbol"] == sym]
                except (RuntimeError, ValueError) as exc:
                    results.append({"symbol": sym, "success": False, "error": f"Không xác nhận được vị thế: {exc}"})
                    continue
                if any(p.get("positionSide", "BOTH") != "BOTH" for p in rows):
                    results.append({"symbol": sym, "success": False, "error": "Hedge mode không được hỗ trợ."})
                    continue
                amount = sum(float(p["positionAmt"]) for p in rows)
                if not amount:
                    results.append({"symbol": sym, "success": True, "closed_quantity": 0})
                    continue
                side = "SELL" if amount > 0 else "BUY"
                result = self.client.create_futures_order(sym, side, quantity=abs(amount), reduce_only=True,
                                                          client_order_id="untracked_" + uuid4().hex[:20])
                data = result.get("data") or {}
                if result.get("success") and data.get("status") == "FILLED":
                    results.append({"symbol": sym, "success": True, "closed_quantity": abs(amount)})
                    self.add_log("SUCCESS", f"Đã đóng vị thế ngoài bot {sym}: {side} {abs(amount):g}")
                else:
                    error = ("Lệnh đóng chưa rõ kết quả. Kiểm tra trên Binance trước khi thử lại."
                             if result.get("unknown") else
                             result.get("error") or f"Lệnh đóng chưa khớp hết ({data.get('status', 'không rõ')})")
                    results.append({"symbol": sym, "success": False, "error": error})
                    self.add_log("WARN", f"Đóng vị thế ngoài bot {sym} thất bại: {error}")
            try:
                self._check_positions()
            except (RuntimeError, ValueError) as exc:
                self._warn("positions", f"Đối soát Futures thất bại: {exc}")
        return {"success": all(r["success"] for r in results), "results": results}

    # ------------------------------------------------------------------ orders & fills

    def _submit_order(self, sym, order, quantity):
        with self.lock:
            if self.state_error:
                return {"success": False, "error": "Trạng thái bot chưa sẵn sàng."}
            self.pending_orders[sym] = order
            if not self.save_state():      # journal first: an order that is not recorded is never sent
                self.state_error = True
                self.is_running = self.entries_enabled = self.auto_run = False
                self.stop_reason = "Không lưu được lệnh trước khi gửi (ổ đĩa/volume)."
                del self.pending_orders[sym]
                return {"success": False, "error": "Không lưu được lệnh trước khi gửi."}
        entry = order["kind"] == "entry"
        side = "BUY" if (order["direction"] == "LONG") == entry else "SELL"
        result = self.client.create_futures_order(sym, side, quantity=quantity, reduce_only=not entry,
                                                  client_order_id=order["id"])
        outcome = self._handle_order_result(sym, order, result)
        self._ensure_protection(sym)
        return outcome

    def _reconcile_orders(self):
        with self.order_lock:
            for sym, order in list(self.pending_orders.items()):
                if self.close_all_requested or order.get("cancel_requested"):
                    result = self.client.cancel_by_client_id(order["market"], sym, order["id"])
                else:
                    result = self.client.query_order(order["market"], sym, order["id"])
                outcome = self._handle_order_result(sym, order, result)
                self._ensure_protection(sym)
                if not outcome.get("success") and not outcome.get("pending"):
                    self.add_log("WARN", f"{sym}: {outcome.get('error', 'Đối soát lệnh thất bại')}")

    def _handle_order_result(self, sym, order, result, storage=None, storage_key=None):
        storage = self.pending_orders if storage is None else storage
        storage_key = sym if storage_key is None else storage_key
        data = result.get("data") or {}
        status = data.get("status")
        terminal = status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
        if result.get("unknown"):
            return {"success": False, "pending": True, "error": "Lệnh đang chờ xác nhận từ Binance."}
        if not result.get("success"):
            # A rejected submission is definitive. Lookup failures come back as unknown instead.
            with self.lock:
                storage.pop(storage_key, None)
                self._persist()
            return {"success": False, "error": result.get("error", "Lệnh bị từ chối")}
        try:
            qty = Decimal(str(data.get("executedQty", 0)))
            quote = Decimal(str(data.get("cumQuote", data.get("cummulativeQuoteQty", 0))))
            if qty > 0 and quote <= 0:
                quote = qty * Decimal(str(data.get("avgPrice", 0)))
            applied_qty = Decimal(str(order.get("applied_qty", 0)))
            applied_quote = Decimal(str(order.get("applied_quote", 0)))
            if not qty.is_finite() or not quote.is_finite() or qty < applied_qty or quote < applied_quote:
                raise ValueError("Dữ liệu khớp lệnh giảm hoặc không hợp lệ")
            delta_qty, delta_quote = qty - applied_qty, quote - applied_quote
            if delta_qty > 0 and delta_quote <= 0:
                raise ValueError("Thiếu giá trị khớp lệnh")
            if status == "FILLED" and qty <= 0:
                raise ValueError("Thiếu khối lượng khớp")
        except (ValueError, TypeError, ArithmeticError):
            return {"success": False, "pending": True, "error": "Dữ liệu khớp lệnh chưa đầy đủ."}

        with self.lock:
            if delta_qty > 0:
                if order["kind"] == "entry":
                    self._record_entry(sym, order, float(delta_qty), float(delta_quote), data.get("orderId"))
                else:
                    pos = self.positions.get(sym)
                    if not pos or (float(delta_qty) > pos["quantity"]
                                   and not isclose(float(delta_qty), pos["quantity"])):
                        return {"success": False, "pending": True, "error": "Khối lượng khớp khác dữ liệu vị thế."}
                    self._record_exit(sym, pos, order, float(delta_qty), float(delta_quote), data.get("orderId"))
                order["applied_qty"] = str(qty)
                order["applied_quote"] = str(quote)
            order["exchange_status"] = status
            if terminal:
                storage.pop(storage_key, None)
                pos = self.positions.get(sym)
                if pos and order.get("partial"):
                    # Filled or not, this partial exit is settled; the plan must not ask again.
                    pos.pop("partial_requested", None)
                    pos["plan"]["partial_done"] = True
            if not self._persist():
                return {"success": False, "error": "Không lưu được kết quả khớp lệnh. Đã dừng mở mới."}
            remaining = self.positions.get(sym, {}).get("quantity", 0)
        if not terminal:
            return {"success": False, "pending": True, "error": "Đã cập nhật phần khớp; đang chờ phần còn lại."}
        if qty <= 0:
            return {"success": False, "error": f"Lệnh {status} không khớp."}
        return {"success": True, "remaining_quantity": remaining if order["kind"] == "close" else 0}

    @staticmethod
    def _note_order(pos, order_id):
        """Remember which exchange orders built and closed the position, to read their fills back later."""
        if order_id is not None and order_id not in pos["order_ids"]:
            pos["order_ids"].append(order_id)

    def _record_entry(self, sym, order, qty, quote, order_id=None):
        existing = self.positions.get(sym)
        if existing:                                   # a further partial fill of the same entry order
            qty += existing["quantity"]
            quote += existing["notional_usdt"]
        price = quote / qty
        pos = existing or {
            "position_id": order["id"], "symbol": sym, "direction": order["direction"],
            "leverage": order["leverage"], "entry_time_ms": _now_ms(), "last_candle_ms": 0,
            "realized_pnl": 0.0, "exit_notional": 0.0, "reason": order["reason"], "regime": order.get("regime"),
            "setup": order["setup"], "interval": order["interval"],
            "order_ids": [], "signal_price": order.get("signal_price"),
        }
        self._note_order(pos, order_id)
        try:
            plan = strategy.new_plan(order["direction"], price, order["stop_price"], order["exit"])
        except ValueError:
            # Filled beyond the stop or the target: there is no valid trade, close it at once.
            plan = {"direction": order["direction"], "entry": price, "initial_stop": order["stop_price"],
                    "stop": order["stop_price"], "r": max(abs(price - order["stop_price"]), price * 1e-4),
                    "tp1": None, "best": price, "bars": 0, "stop_kind": "INITIAL",
                    "breakeven_done": True, "partial_done": True, "exit": {}}
            pos["exit_requested"] = "INVALID_STOP"
        cost = self.config.risk.round_trip_cost
        pos.update({
            "entry_price": price, "quantity": qty, "initial_quantity": qty,
            "notional_usdt": quote, "entry_notional": quote, "plan": plan,
            # 1R in USDT: the loss at the stop including estimated fees and slippage.
            "risk_usdt": qty * plan["r"] + cost * quote,
            "current_price": price, "pnl_usdt": 0.0, "r_now": 0.0,
        })
        if order.get("cancel_requested"):
            pos["exit_requested"] = order.get("exit_reason", "CANCEL_ENTRY")
        self.positions[sym] = pos
        self.add_log("SUCCESS", f"✅ Mở {order['direction']} {sym} ({SETUPS[order['setup']].label}): {qty:g} @ {price:g}, SL {plan['stop']:g}, "
                                f"1R = {pos['risk_usdt']:.2f} USDT")

    def _record_exit(self, sym, pos, order, qty, quote, order_id=None):
        self._note_order(pos, order_id)
        price = quote / qty
        s = _sign(pos["direction"])
        pnl = s * (price - pos["entry_price"]) * qty
        pos["realized_pnl"] += pnl
        pos["exit_notional"] += quote
        reason = order["reason"]
        if reason == "EXCHANGE_STOP_LOSS":
            reason = strategy.stop_reason(pos["plan"])
        if not isclose(qty, pos["quantity"], rel_tol=1e-9):
            fraction = (pos["quantity"] - qty) / pos["quantity"]
            pos["quantity"] = float(Decimal(str(pos["quantity"])) - Decimal(str(qty)))
            pos["notional_usdt"] *= fraction
            if order.get("partial"):
                # The exchange stop still covers the old size: replace it for what is left.
                for stop in self.protective_orders.values():
                    if stop["symbol"] == sym:
                        stop["cancel_requested"] = True
            self.add_log("SUCCESS", f"Đóng một phần {sym}: {qty:g} @ {price:g} ({pnl:+.2f} USDT), còn {pos['quantity']:g}")
            return
        cfg = self.config.risk
        fees = cfg.fee_rate * (pos["entry_notional"] + pos["exit_notional"])
        net = pos["realized_pnl"] - fees
        now = _now_ms()
        self.unsaved_trades.append({
            "position_id": pos["position_id"], "symbol": sym, "direction": pos["direction"],
            "entry_time": pos["entry_time_ms"], "exit_time": now,
            "entry_price": pos["entry_price"], "exit_price": pos["exit_notional"] / pos["initial_quantity"],
            "quantity": pos["initial_quantity"], "gross_pnl": round(pos["realized_pnl"], 8),
            "fees": round(fees, 8), "net_pnl": round(net, 8), "risk_usdt": round(pos["risk_usdt"], 8),
            "r_multiple": round(net / pos["risk_usdt"], 4) if pos["risk_usdt"] > 0 else 0.0,
            "reason": reason, "bars": pos["plan"]["bars"], "setup": pos["setup"],
            "funding": 0.0, "settled": 0, "order_ids": json.dumps(pos["order_ids"]),
            "signal_price": pos.get("signal_price"),
        })
        tripped = risk.record_trade(self.risk_state, net, cfg)
        self.last_exit_ms[sym] = now
        del self.positions[sym]
        self.next_advance_ms.pop(sym, None)
        self.add_log("SUCCESS", f"Đóng {pos['direction']} {sym} @ {price:g}: {net:+.2f} USDT sau phí ước tính "
                                f"({net / pos['risk_usdt']:+.2f}R, {reason})")
        if tripped:
            self.add_log("RISK", f"⏸ {tripped}")

    # ------------------------------------------------------------------ exchange stops

    def _check_positions(self, symbol=None):
        """True when exchange positions equal the bot's books (for one symbol or the whole account)."""
        rows = self.client.get_futures_positions(symbol=symbol, strict=True)
        with self.lock:
            expected = {sym: pos["quantity"] * _sign(pos["direction"]) for sym, pos in self.positions.items()
                        if (symbol is None or sym == symbol) and sym not in self.pending_orders}
            pending = set(self.pending_orders)
        actual = {}
        for row in rows:
            sym = row["symbol"]
            if sym in pending:
                continue
            if row.get("positionSide", "BOTH") != "BOTH":
                with self.lock:
                    self.position_mismatches = {sym: {"error": "Tài khoản đang ở Hedge mode; bot chỉ hỗ trợ One-way."}}
                return False
            if float(row["positionAmt"]):
                actual[sym] = actual.get(sym, 0.0) + float(row["positionAmt"])
        differences = {sym: {"bot_quantity": expected.get(sym, 0), "exchange_quantity": actual.get(sym, 0)}
                       for sym in expected.keys() | actual.keys()
                       if not isclose(expected.get(sym, 0), actual.get(sym, 0), rel_tol=1e-9, abs_tol=1e-12)}
        with self.lock:
            if symbol is None:
                self.position_mismatches = differences
            else:
                self.position_mismatches.pop(symbol, None)
                self.position_mismatches.update(differences)
        return not differences

    def _protection_ready(self):
        """Every position is fully covered by confirmed exchange stops and nothing is in flux."""
        with self.lock:
            for order in self.protective_orders.values():
                pos = self.positions.get(order["symbol"])
                if (not pos or order["position_id"] != pos["position_id"]
                        or order.get("algo_status") != "NEW" or order.get("cancel_requested")):
                    return False
            for sym, pos in self.positions.items():
                covered = sum(float(o["quantity"]) for o in self.protective_orders.values() if o["symbol"] == sym)
                if pos.get("exit_requested") or not isclose(covered, pos["quantity"], rel_tol=1e-9, abs_tol=1e-12):
                    return False
            return True

    def _protection_failure(self, sym, message, halt_now=False):
        """An exchange stop could not be placed: close that position, stop adding exposure.
        One failure can be a fast candle; a second within the hour is systematic and halts the bot."""
        now = time.time()
        with self.lock:
            self.entries_enabled = False
            self.protection_failures = [t for t in self.protection_failures if now - t < 3600] + [now]
            repeated = halt_now or len(self.protection_failures) >= 2
            pos = self.positions.get(sym)
            if pos:
                pos["exit_requested"] = "PROTECTION_FAILED"
            pending = self.pending_orders.get(sym)
            if pending and pending["kind"] == "entry":
                pending.update(cancel_requested=True, exit_reason="PROTECTION_FAILED")
            self.last_error = message
            self._persist()
        self.add_log("ERROR", f"{sym}: {message}. Đang đóng vị thế chưa có SL trên sàn.")
        if repeated:
            self._halt(f"Lệnh SL trên sàn không đúng yêu cầu ({sym}: {message})." if halt_now else
                       f"Đặt SL trên sàn thất bại 2 lần trong 1 giờ ({sym}: {message}).")

    def _ensure_protection(self, sym):
        """Create an exchange stop for any part of the position that has none."""
        with self.order_lock:
            with self.lock:
                pos = self.positions.get(sym)
                if not pos or pos.get("exit_requested") or self.close_all_requested or self.state_error:
                    return
                orders = [o for o in self.protective_orders.values() if o["symbol"] == sym]
                if any(o.get("algo_status") != "NEW" or o.get("cancel_requested")
                       or o["position_id"] != pos["position_id"] for o in orders):
                    self.entries_enabled = False
                    return                 # wait for the stop being cancelled/confirmed to settle first
                covered = sum((Decimal(str(o["quantity"])) for o in orders), Decimal(0))
                missing = Decimal(str(pos["quantity"])) - covered
                if missing <= 0 or isclose(float(covered), pos["quantity"], rel_tol=1e-9, abs_tol=1e-12):
                    return
                order = {"id": "sl_" + uuid4().hex, "symbol": sym, "position_id": pos["position_id"],
                         "market": MARKET, "kind": "close", "reason": "EXCHANGE_STOP_LOSS",
                         "quantity": str(missing), "trigger_price": pos["plan"]["stop"],
                         "direction": pos["direction"], "algo_status": "SUBMITTING"}
                self.protective_orders[order["id"]] = order
                if not self._persist():
                    return
            result = self.client.create_futures_stop(
                sym, "SELL" if order["direction"] == "LONG" else "BUY",
                float(missing), order["trigger_price"], order["id"])
            self._handle_protection_result(order, result)

    def _reconcile_protections(self, cancel_symbol=None):
        with self.order_lock:
            for order in list(self.protective_orders.values()):
                sym = order["symbol"]
                with self.lock:
                    pos = self.positions.get(sym)
                    cancel = (order.get("cancel_requested") or sym == cancel_symbol or self.close_all_requested
                              or not pos or pos["position_id"] != order["position_id"] or pos.get("exit_requested"))
                    if cancel and not order.get("cancel_requested"):
                        order["cancel_requested"] = True
                        if not self._persist():
                            continue
                result = (self.client.cancel_futures_stop(order["id"]) if cancel
                          else self.client.query_futures_stop(order["id"]))
                self._handle_protection_result(order, result)

    def _handle_protection_result(self, order, result):
        sym, key = order["symbol"], order["id"]
        with self.lock:
            if result.get("unknown"):
                self.entries_enabled = False
                if order.get("algo_status") != "UNKNOWN":
                    order["algo_status"] = "UNKNOWN"
                    self._persist()
                return
            if not result.get("success"):
                self.protective_orders.pop(key, None)
                self._protection_failure(sym, result.get("error", "Lệnh cắt lỗ bị từ chối"))
                return
            data = result.get("data") or {}
            status = data.get("algoStatus", "UNKNOWN")
            previous = (order.get("algo_status"), order.get("algo_id"), order.get("exchange_trigger_price"))
            order["algo_status"] = status
            order["algo_id"] = data.get("algoId")
            order["exchange_trigger_price"] = data.get("triggerPrice")
            actual_id = data.get("actualOrderId")
            triggered = bool(actual_id) and str(actual_id) != "0"
            pos = self.positions.get(sym)
            same_position = bool(pos) and pos["position_id"] == order["position_id"]
            if triggered:
                if same_position:
                    pos["exit_requested"] = "EXCHANGE_STOP_LOSS"
                pending = self.pending_orders.get(sym)
                if pending and pending["kind"] == "entry":
                    pending.update(cancel_requested=True, exit_reason="EXCHANGE_STOP_LOSS")
            changed = previous != (status, order["algo_id"], order["exchange_trigger_price"])
            if (changed or triggered) and not self._persist():
                return
        if triggered:
            # The algo order firing is not a fill. Apply only the executions of the order it created.
            execution = self.client.query_order(MARKET, sym, order_id=actual_id)
            if not same_position and Decimal(str(order.get("applied_qty", 0))) == 0:
                self.entries_enabled = False
                self.last_error = f"Cần đối soát lệnh cắt lỗ {sym} của vị thế cũ"
                return
            self._handle_order_result(sym, order, execution, self.protective_orders, key)
            return
        if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            with self.lock:
                self.protective_orders.pop(key, None)
                self._persist()
            if same_position and not order.get("cancel_requested"):
                self._protection_failure(sym, "Lệnh cắt lỗ trên sàn không còn hoạt động")
        elif status == "NEW":
            try:
                trigger = float(data["triggerPrice"])
                valid = (data.get("symbol") == sym and data.get("clientAlgoId") == key
                         and data.get("reduceOnly") in (True, "true")
                         and data.get("workingType") == "MARK_PRICE"
                         and data.get("orderType") == "STOP_MARKET"
                         and data.get("side") == ("SELL" if order["direction"] == "LONG" else "BUY")
                         and isclose(float(data["quantity"]), float(order["quantity"]), rel_tol=1e-9)
                         and isfinite(trigger) and trigger > 0
                         # Rounding may only tighten the stop, never widen it.
                         and (trigger >= order["trigger_price"] if order["direction"] == "LONG"
                              else trigger <= order["trigger_price"]))
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                order["cancel_requested"] = True
                self._protection_failure(sym, "Thông tin lệnh cắt lỗ chưa khớp yêu cầu bảo vệ", halt_now=True)
        else:
            self.entries_enabled = False

    # ------------------------------------------------------------------ status

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            positions = deepcopy(list(self.positions.values()))
            for pos in positions:
                confirmed = [o for o in self.protective_orders.values()
                             if o["symbol"] == pos["symbol"] and o["position_id"] == pos["position_id"]
                             and o.get("algo_status") == "NEW" and not o.get("cancel_requested")]
                covered = sum(float(o["quantity"]) for o in confirmed)
                pos["stop_on_exchange"] = (not pos.get("exit_requested")
                                           and isclose(covered, pos["quantity"], rel_tol=1e-9))
                pos["exchange_stop_prices"] = [o.get("exchange_trigger_price") for o in confirmed]
                pos["open_risk_usdt"] = round(risk.position_risk(
                    {"direction": pos["direction"], "quantity": pos["quantity"],
                     "entry_price": pos["entry_price"], "stop_price": pos["plan"]["stop"]}), 4)
            account = dict(self.account) if self.account else None
            return {
                "is_running": self.is_running,
                "auto_run": self.auto_run,
                "resume_pending": self.resume_pending,
                "stop_reason": self.stop_reason,
                "stopped_at": self.stopped_at,
                "monitoring": bool(self.thread and self.thread.is_alive()),
                "entries_enabled": self.entries_enabled,
                "close_all_requested": self.close_all_requested,
                "last_error": self.last_error,
                "state_error": self.state_error,
                "has_credentials": self.client.has_credentials,
                "position_mismatches": dict(self.position_mismatches),
                "config": self.config.model_dump(),
                "account": account,
                "risk": risk.snapshot(self.risk_state, account["equity"] if account else None,
                                      self._position_views(), self.config.risk),
                "positions": positions,
                "pending_orders": deepcopy(self.pending_orders),
                "protection_ready": self._protection_ready(),
                "scan_board": [{**self.scan_board[s], "cases": [self.scan_board[s]["cases"][k] for k in SETUPS
                                                              if k in self.scan_board[s]["cases"]]}
                               for s in self.config.watchlist if s in self.scan_board],
                "pnl_basis": "exchange_fees_and_funding_once_settled_else_estimated_fees",
            }
