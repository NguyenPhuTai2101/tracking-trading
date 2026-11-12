import time
import json
import logging
import os
import threading
from uuid import uuid4
from math import isfinite, isclose
from typing import Dict, Any, List, Optional
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from copy import deepcopy
from contextlib import contextmanager
from trading_rules import BotConfigRequest, validate_closed_candles
from config import settings
from binance_client import binance_client
from indicators import analyze_market_signals
from strategy import decide_entry, exit_prices

STATE_FILE = str(Path(__file__).resolve().with_name("bot_state.json"))

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "NEARUSDT", "1000PEPEUSDT"]

class TradingBotEngine:
    def __init__(self, market_type=None, state_file=None, autoload=True):
        if market_type not in (None, "SPOT", "FUTURES"):
            raise ValueError("Thị trường bot không hợp lệ")
        self.fixed_market = market_type
        self.state_file = str(state_file or STATE_FILE)
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.last_error = None
        self.close_all_requested = False
        self.position_mismatches = {}
        self._process_lock_file = None
        self.lock = threading.RLock()
        self.order_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.protective_orders: Dict[str, Dict[str, Any]] = {}
        self.last_entry_candle = {}
        self.next_candle_check_ms = {}
        self.entries_enabled = False
        self.state_error = False
        
        # Bot Configurations
        self.market_type = market_type or "FUTURES"
        self.leverage = 1 if market_type == "SPOT" else 10
        self.symbol = settings.DEFAULT_SYMBOL
        self.watchlist_symbols: List[str] = list(DEFAULT_WATCHLIST)
        if market_type == "SPOT":
            self.watchlist_symbols = ["PEPEUSDT" if s == "1000PEPEUSDT" else s for s in self.watchlist_symbols]
        self.max_positions: int = 3
        self.max_total_margin_usdt = 60.0
        self.max_open_risk_usdt = 0.9
        self.interval = settings.DEFAULT_INTERVAL
        self.strategy = "HYBRID_SMART" # RSI_SCALPING, EMA_CROSSOVER, HYBRID_SMART
        self.trade_amount_usdt = settings.DEFAULT_TRADE_AMOUNT_USDT
        self.stop_loss_pct = settings.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = settings.DEFAULT_TAKE_PROFIT_PCT
        self.scan_interval_sec = settings.DEFAULT_SCAN_INTERVAL_SEC
        
        # State & Metrics
        # active_positions is a map of symbol -> position dict
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.closed_trades: List[Dict[str, Any]] = []
        self.total_pnl_usdt = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.last_signal: Optional[Dict[str, Any]] = None
        self.last_check_time = None
        self.history_scope = "market"
        
        if autoload:
            self.load_state()

    @property
    def active_position(self) -> Optional[Dict[str, Any]]:
        """Backward compatibility for single-position callers"""
        with self.lock:
            if self.symbol in self.active_positions:
                return self.active_positions[self.symbol]
            if self.active_positions:
                return next(iter(self.active_positions.values()))
            return None

    def add_log(self, level: str, message: str):
        now = datetime.now().strftime("%H:%M:%S")
        entry = {"time": now, "level": level, "message": message}
        with self.lock:
            self.logs.insert(0, entry)
            if len(self.logs) > 200:
                self.logs.pop()
        logging.getLogger(__name__).info("[%s] %s", level, message)

    def save_state(self):
        with self.lock:
            data = {
                "market_type": self.market_type,
                "leverage": self.leverage,
                "symbol": self.symbol,
                "watchlist_symbols": self.watchlist_symbols,
                "max_positions": self.max_positions,
                "strategy": self.strategy,
                "trade_amount_usdt": self.trade_amount_usdt,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
                "scan_interval_sec": self.scan_interval_sec,
                "interval": self.interval,
                "max_total_margin_usdt": self.max_total_margin_usdt,
                "max_open_risk_usdt": self.max_open_risk_usdt,
                "last_entry_candle": self.last_entry_candle,
                "close_all_requested": self.close_all_requested,
                "active_positions": self.active_positions,
                "pending_orders": self.pending_orders,
                "protective_orders": self.protective_orders,
                "closed_trades": self.closed_trades[-50:],
                "total_pnl_usdt": self.total_pnl_usdt,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "history_scope": self.history_scope,
            }
            try:
                with open(f"{self.state_file}.tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(f"{self.state_file}.tmp", self.state_file)
                return True
            except Exception as e:
                logging.getLogger(__name__).error("Error saving bot state: %s", e)
                return False

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.market_type = data.get("market_type", self.market_type)
                    if self.fixed_market and self.market_type != self.fixed_market:
                        raise ValueError("File trạng thái thuộc bot khác")
                    self.leverage = data.get("leverage", self.leverage)
                    self.symbol = data.get("symbol", self.symbol)
                    self.watchlist_symbols = data.get("watchlist_symbols", self.watchlist_symbols)
                    self.max_positions = data.get("max_positions", self.max_positions)
                    self.strategy = data.get("strategy", self.strategy)
                    self.trade_amount_usdt = data.get("trade_amount_usdt", self.trade_amount_usdt)
                    self.stop_loss_pct = data.get("stop_loss_pct", self.stop_loss_pct)
                    self.take_profit_pct = data.get("take_profit_pct", self.take_profit_pct)
                    self.scan_interval_sec = data.get("scan_interval_sec", self.scan_interval_sec)
                    self.interval = data.get("interval", self.interval)
                    self.max_total_margin_usdt = data.get("max_total_margin_usdt", self.trade_amount_usdt * self.max_positions)
                    self.max_open_risk_usdt = data.get("max_open_risk_usdt", self.trade_amount_usdt * self.max_positions * self.stop_loss_pct / 100)
                    self.last_entry_candle = data.get("last_entry_candle", {})
                    self.close_all_requested = data.get("close_all_requested", False)
                    if self.market_type == "FUTURES":
                        self.watchlist_symbols = ["1000PEPEUSDT" if s == "PEPEUSDT" else s for s in self.watchlist_symbols]
                    
                    # Migration: if active_positions exists use it, otherwise check active_position
                    if "active_positions" in data and isinstance(data["active_positions"], dict):
                        self.active_positions = data["active_positions"]
                    elif "active_position" in data and data["active_position"]:
                        pos = data["active_position"]
                        sym = pos.get("symbol", self.symbol)
                        self.active_positions = {sym: pos}
                    else:
                        self.active_positions = {}

                    self.pending_orders = data.get("pending_orders", {})
                    self.protective_orders = data.get("protective_orders", {})
                    self.closed_trades = data.get("closed_trades", [])
                    self.total_pnl_usdt = data.get("total_pnl_usdt", 0.0)
                    self.win_count = data.get("win_count", 0)
                    self.loss_count = data.get("loss_count", 0)
                    self.history_scope = data.get("history_scope", "market")
                    if self.fixed_market == "SPOT" and self.leverage != 1:
                        raise ValueError("Bot Spot chỉ dùng 1x")
                    BotConfigRequest(
                        market_type=self.market_type, leverage=self.leverage, symbol=self.symbol,
                        watchlist_symbols=self.watchlist_symbols, max_positions=self.max_positions,
                        strategy=self.strategy, trade_amount_usdt=self.trade_amount_usdt,
                        stop_loss_pct=self.stop_loss_pct, take_profit_pct=self.take_profit_pct,
                        scan_interval_sec=self.scan_interval_sec, interval=self.interval,
                        max_total_margin_usdt=self.max_total_margin_usdt, max_open_risk_usdt=self.max_open_risk_usdt)
                    for sym, pos in self.active_positions.items():
                        if self.fixed_market and pos.get("market_type") != self.fixed_market:
                            raise ValueError("Vị thế thuộc bot khác")
                        pos.setdefault("position_id", uuid4().hex)
                        if (pos.get("symbol") != sym or pos.get("market_type") not in ("SPOT", "FUTURES")
                                or pos.get("position_side") not in ("LONG", "SHORT")
                                or not isfinite(float(pos.get("quantity", 0))) or float(pos.get("quantity", 0)) <= 0
                                or not isfinite(float(pos.get("entry_price", 0))) or float(pos.get("entry_price", 0)) <= 0):
                            raise ValueError("Dữ liệu vị thế đã lưu không hợp lệ")
                    for order in [*self.pending_orders.values(), *self.protective_orders.values()]:
                        if self.fixed_market and order.get("market") != self.fixed_market:
                            raise ValueError("Lệnh chờ thuộc bot khác")
            except Exception as e:
                self.state_error = True
                logging.getLogger(__name__).error("Error loading bot state: %s", e)

    def update_config(self, market_type=None, leverage=None, symbol=None, strategy=None,
                      amount=None, sl=None, tp=None, interval_sec=None, watchlist_symbols=None,
                      max_positions=None, interval=None, max_total_margin_usdt=None, max_open_risk_usdt=None):
        request = BotConfigRequest(
            market_type=market_type, leverage=leverage, symbol=symbol, strategy=strategy,
            trade_amount_usdt=amount, stop_loss_pct=sl, take_profit_pct=tp,
            scan_interval_sec=interval_sec, watchlist_symbols=watchlist_symbols,
            max_positions=max_positions, interval=interval,
            max_total_margin_usdt=max_total_margin_usdt, max_open_risk_usdt=max_open_risk_usdt)
        updates = request.model_dump(exclude_none=True)
        if self.fixed_market and updates.get("market_type", self.fixed_market) != self.fixed_market:
            raise ValueError("Mỗi bot có thị trường cố định. Chọn bot tương ứng để cấu hình.")
        if self.fixed_market == "SPOT" and updates.get("leverage", 1) != 1:
            raise ValueError("Bot Spot không dùng đòn bẩy, chỉ hỗ trợ 1x.")
        with self.order_lock:
            with self.lock:
                if self.state_error:
                    raise RuntimeError("Không đọc hoặc lưu được trạng thái bot.")
                if self.is_running:
                    raise ValueError("Tạm dừng mở lệnh mới trước khi sửa cấu hình bot.")
                changing_market = updates.get("market_type", self.market_type) != self.market_type
                changing_leverage = updates.get("leverage", self.leverage) != self.leverage
                if (changing_market or changing_leverage) and (self.active_positions or self.pending_orders or self.protective_orders):
                    raise ValueError("Chỉ đổi thị trường hoặc đòn bẩy khi đã hết vị thế và lệnh chờ.")
                market = updates.get("market_type", self.market_type)
                watchlist = updates.get("watchlist_symbols", self.watchlist_symbols)
            for sym in watchlist:
                binance_client.validate_symbol(market, sym)
            with self.lock:
                previous = {key: getattr(self, key) for key in updates}
                for key, value in updates.items():
                    setattr(self, key, value)
                if not self.save_state():
                    for key, value in previous.items():
                        setattr(self, key, value)
                    raise RuntimeError("Không lưu được cấu hình bot.")
                self.next_candle_check_ms.clear()
                self.entries_enabled = False
        self.add_log("CONFIG", f"Đã lưu bot {market}, {self.leverage}x, khung {self.interval}.")
        return True

    @contextmanager
    def manual_action(self, market, symbol):
        if self.fixed_market and market != self.fixed_market:
            raise ValueError("Thao tác gửi nhầm bot")
        # Serialize the check and the exchange mutation with bot entry/exit.
        with self.order_lock:
            with self.lock:
                managed = (symbol in self.active_positions or symbol in self.pending_orders
                           or any(o["symbol"] == symbol for o in self.protective_orders.values()))
                watched = self.is_running and self.market_type == market and symbol in self.watchlist_symbols
                if managed or watched:
                    raise ValueError("Mã này đang do bot quản lý. Dùng nút đóng vị thế của bot.")
            yield

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
                    return {"success": False, "message": "Bot đang mở lệnh tự động."}
                if self.state_error or self.close_all_requested:
                    return {"success": False, "message": "Trạng thái chưa sẵn sàng hoặc đang đóng toàn bộ."}
                if not binance_client.has_credentials:
                    return {"success": False, "message": "Cần cấu hình khóa Binance Demo."}
                market, symbols = self.market_type, list(self.watchlist_symbols)
            try:
                for sym in symbols:
                    binance_client.validate_symbol(market, sym)
                self._reconcile_orders()
                self._reconcile_protections()
                for sym in list(self.active_positions):
                    self._ensure_futures_protection(sym)
                if not self._protection_ready():
                    raise ValueError("Còn vị thế hoặc lệnh cắt lỗ chưa xác nhận bảo vệ.")
                if market == "FUTURES" and not self._check_futures_positions():
                    mismatches = [f"{sym} (sàn: {d.get('exchange_quantity', 0)}, bot: {d.get('bot_quantity', 0)})" for sym, d in self.position_mismatches.items()]
                    detail = ", ".join(mismatches) if mismatches else "chưa xác định"
                    raise ValueError(f"Vị thế Futures trên sàn chưa khớp dữ liệu bot: {detail}. Vị thế bot không quản lý: dùng nút 'Đóng vị thế ngoài bot' (sẽ đóng trên sàn) hoặc tự xử lý trên Binance.")
            except (ValueError, RuntimeError) as exc:
                return {"success": False, "message": str(exc)}
            with self.lock:
                if self.pending_orders:
                    return {"success": False, "message": "Còn lệnh đang chờ xác nhận."}
                self.is_running = True
                self.entries_enabled = True
                self.last_error = None
            self.supervise()
        return {"success": True, "message": "Đã bật mở lệnh tự động."}

    def stop(self):
        # Immediate flag prevents a scan in progress from submitting after the pause.
        with self.lock:
            self.is_running = False
        self.supervise()
        return {"success": True, "message": "Đã dừng mở lệnh mới. Bot tiếp tục theo dõi TP/SL và lệnh chờ."}

    def shutdown(self):
        with self.lock:
            self.is_running = False
            self.stop_event.set()
        for thread in (self.thread, self.scan_thread):
            if thread:
                thread.join()
        self.release_process_lock()

    def acquire_process_lock(self):
        path = self.state_file + ".lock"
        handle = open(path, "a+b")
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

    def _run_loop(self):
        while not self.stop_event.is_set():
            try:
                self._bot_tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.entries_enabled = False
                self.add_log("ERROR", f"Lỗi giám sát: {exc}")
            self.stop_event.wait(2)

    def _scan_loop(self):
        while not self.stop_event.is_set():
            if self.is_running and self.entries_enabled:
                with self.lock:
                    symbols = list(self.watchlist_symbols)
                for sym in symbols:
                    if self.stop_event.is_set() or not self.is_running:
                        break
                    try:
                        self._scan_symbol_for_entry(sym)
                    except Exception as exc:
                        self.add_log("WARN", f"Bỏ qua {sym}: {exc}")
            self.stop_event.wait(self.scan_interval_sec)

    def _reconcile_orders(self):
        with self.order_lock:
            for sym, order in list(self.pending_orders.items()):
                if self.close_all_requested or order.get("cancel_requested"):
                    result = binance_client.cancel_by_client_id(order["market"], sym, order["id"])
                else:
                    result = binance_client.query_order(order["market"], sym, order["id"])
                outcome = self._handle_order_result(sym, order, result)
                self._ensure_futures_protection(sym)
                if not outcome.get("success") and not outcome.get("pending"):
                    self.add_log("WARN", outcome.get("error", "Đối soát lệnh thất bại"))

    def _check_futures_positions(self, symbol=None):
        positions = binance_client.get_futures_positions(symbol=symbol, strict=True)
        with self.lock:
            expected = {
                sym: pos["quantity"] * (1 if pos["position_side"] == "LONG" else -1)
                for sym, pos in self.active_positions.items()
                if pos["market_type"] == "FUTURES" and (symbol is None or sym == symbol)
                and sym not in self.pending_orders
            }
            pending = set(self.pending_orders)
        actual = {}
        for pos in positions:
            sym = pos["symbol"]
            if sym in pending:
                continue
            if pos.get("positionSide", "BOTH") != "BOTH":
                with self.lock:
                    self.position_mismatches = {sym: {"error": "Tài khoản đang ở Hedge mode; bot hỗ trợ One-way."}}
                return False
            if not float(pos["positionAmt"]):
                continue
            if sym in actual:
                return False
            actual[sym] = float(pos["positionAmt"])
        differences = {
            sym: {"bot_quantity": expected.get(sym, 0), "exchange_quantity": actual.get(sym, 0)}
            for sym in expected.keys() | actual.keys()
            if not isclose(expected.get(sym, 0), actual.get(sym, 0), rel_tol=1e-9, abs_tol=1e-12)
        }
        with self.lock:
            if symbol is None:
                self.position_mismatches = differences
            else:
                self.position_mismatches.pop(symbol, None)
                self.position_mismatches.update(differences)
        return not differences

    def _protection_ready(self):
        with self.lock:
            for order in self.protective_orders.values():
                pos = self.active_positions.get(order["symbol"])
                if (not pos or order["position_id"] != pos["position_id"]
                        or order.get("algo_status") != "NEW" or order.get("cancel_requested")):
                    return False
            for sym, pos in self.active_positions.items():
                if pos["market_type"] != "FUTURES":
                    continue
                covered = sum(float(o["quantity"]) for o in self.protective_orders.values()
                              if o["symbol"] == sym and o["position_id"] == pos["position_id"])
                if pos.get("exit_requested") or not isclose(covered, pos["quantity"], rel_tol=1e-9, abs_tol=1e-12):
                    return False
            return True

    def _protection_failure(self, sym, message):
        # Stop adding exposure and request an orderly close of the unprotected fill.
        with self.lock:
            self.is_running = self.entries_enabled = False
            pos = self.active_positions.get(sym)
            if pos:
                pos["exit_requested"] = "PROTECTION_FAILED"
            pending = self.pending_orders.get(sym)
            if pending and pending["kind"] == "entry":
                pending.update(cancel_requested=True, exit_reason="PROTECTION_FAILED")
            self.last_error = message
            self._persist_order_state()
        self.add_log("ERROR", f"{sym}: {message}. Đã dừng mở mới và yêu cầu đóng vị thế.")

    def _ensure_futures_protection(self, sym):
        with self.order_lock:
            with self.lock:
                pos = self.active_positions.get(sym)
                if (not pos or pos["market_type"] != "FUTURES" or pos.get("exit_requested")
                        or self.close_all_requested or self.state_error):
                    return
                orders = [o for o in self.protective_orders.values() if o["symbol"] == sym]
                if any(o.get("algo_status") != "NEW" or o.get("cancel_requested")
                       or o["position_id"] != pos["position_id"] for o in orders):
                    self.entries_enabled = False
                    return
                covered = sum((Decimal(str(o["quantity"])) for o in orders), Decimal(0))
                missing = Decimal(str(pos["quantity"])) - covered
                if missing <= 0 or isclose(float(covered), pos["quantity"], rel_tol=1e-9, abs_tol=1e-12):
                    return
                # Each newly filled portion gets its own stop; existing coverage stays live.
                order = {
                    "id": "sl_" + uuid4().hex, "symbol": sym, "position_id": pos["position_id"],
                    "market": "FUTURES", "kind": "close", "reason": "EXCHANGE_STOP_LOSS",
                    "quantity": str(missing), "trigger_price": pos.get("target_sl_price"),
                    "direction": pos["position_side"], "algo_status": "SUBMITTING",
                }
                if not order["trigger_price"] or order["trigger_price"] <= 0:
                    self._protection_failure(sym, "Thiếu giá cắt lỗ")
                    return
                self.protective_orders[order["id"]] = order
                if not self._persist_order_state():
                    return
            result = binance_client.create_futures_stop(
                sym, "SELL" if order["direction"] == "LONG" else "BUY",
                float(missing), order["trigger_price"], order["id"])
            self._handle_protection_result(order, result)

    def _reconcile_protections(self, cancel_symbol=None):
        with self.order_lock:
            for order in list(self.protective_orders.values()):
                sym = order["symbol"]
                with self.lock:
                    pos = self.active_positions.get(sym)
                    cancel = (order.get("cancel_requested") or sym == cancel_symbol or self.close_all_requested
                              or not pos or pos["position_id"] != order["position_id"] or pos.get("exit_requested"))
                    if cancel and not order.get("cancel_requested"):
                        order["cancel_requested"] = True
                        if not self._persist_order_state():
                            continue
                result = (binance_client.cancel_futures_stop(order["id"]) if cancel
                          else binance_client.query_futures_stop(order["id"]))
                self._handle_protection_result(order, result)

    def _handle_protection_result(self, order, result):
        sym, key = order["symbol"], order["id"]
        with self.lock:
            if result.get("unknown"):
                self.entries_enabled = False
                if order.get("algo_status") != "UNKNOWN":
                    order["algo_status"] = "UNKNOWN"
                    self._persist_order_state()
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
            pos = self.active_positions.get(sym)
            same_position = pos and pos["position_id"] == order["position_id"]
            if actual_id and str(actual_id) != "0":
                if same_position:
                    pos["exit_requested"] = "EXCHANGE_STOP_LOSS"
                pending = self.pending_orders.get(sym)
                if pending and pending["kind"] == "entry":
                    pending.update(cancel_requested=True, exit_reason="EXCHANGE_STOP_LOSS")
            changed = previous != (status, order["algo_id"], order["exchange_trigger_price"])
            if (changed or actual_id) and not self._persist_order_state():
                return
        if actual_id and str(actual_id) != "0":
            # Algo acceptance/trigger is not a fill. Apply only the actual order's executions.
            execution = binance_client.query_order("FUTURES", sym, order_id=actual_id)
            if not same_position and Decimal(str(order.get("applied_qty", 0))) == 0:
                self.entries_enabled = False
                self.last_error = f"Cần đối soát lệnh cắt lỗ {sym} của vị thế cũ"
                return
            self._handle_order_result(sym, order, execution, self.protective_orders, key)
            return
        if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            with self.lock:
                self.protective_orders.pop(key, None)
                self._persist_order_state()
            if same_position and not order.get("cancel_requested"):
                self._protection_failure(sym, "Lệnh cắt lỗ trên sàn không còn hoạt động")
        elif status == "NEW":
            try:
                valid = (data.get("symbol") == sym and data.get("clientAlgoId") == key
                         and data.get("reduceOnly") in (True, "true")
                         and data.get("workingType") == "MARK_PRICE"
                         and data.get("orderType") == "STOP_MARKET"
                         and data.get("side") == ("SELL" if order["direction"] == "LONG" else "BUY")
                         and isclose(float(data["quantity"]), float(order["quantity"]), rel_tol=1e-9)
                         and isfinite(float(data["triggerPrice"])) and float(data["triggerPrice"]) > 0
                         and ((float(data["triggerPrice"]) >= order["trigger_price"])
                              if order["direction"] == "LONG" else
                              (float(data["triggerPrice"]) <= order["trigger_price"])))
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                order["cancel_requested"] = True
                self._protection_failure(sym, "Thông tin lệnh cắt lỗ chưa khớp yêu cầu bảo vệ")
        else:
            self.entries_enabled = False

    def _bot_tick(self):
        self.last_check_time = datetime.now().strftime("%H:%M:%S")
        self._reconcile_orders()
        self._reconcile_protections()
        for sym in list(self.active_positions):
            self._ensure_futures_protection(sym)
        with self.lock:
            positions = list(self.active_positions)
            needs_check = self.is_running or bool(positions)
            futures = self.market_type == "FUTURES" or any(
                p["market_type"] == "FUTURES" for p in self.active_positions.values())
        if needs_check and futures:
            try:
                self.entries_enabled = self._check_futures_positions() and self._protection_ready()
            except (RuntimeError, ValueError) as exc:
                self.entries_enabled = False
                self.position_mismatches = {"account": {"error": str(exc)}}
                self.add_log("WARN", f"Đối soát Futures thất bại: {exc}")
        elif needs_check:
            self.entries_enabled = True
        for sym in positions:
            try:
                self._monitor_position(sym)
            except Exception as exc:
                self.entries_enabled = False
                self.add_log("ERROR", f"Lỗi theo dõi {sym}: {exc}")
        if self.close_all_requested:
            with self.order_lock:
                for sym in list(self.active_positions):
                    result = self._close_position(sym, "MANUAL_FORCE_CLOSE_ALL")
                    if not result["success"]:
                        self.add_log("WARN", result.get("error", "Đang đóng vị thế"))
                with self.lock:
                    if not self.active_positions and not self.pending_orders and not self.protective_orders:
                        self.close_all_requested = False
                        self.save_state()

    def _monitor_position(self, sym: str):
        with self.lock:
            pos = self.active_positions.get(sym)
        if not pos or pos["entry_price"] <= 0:
            return

        if pos.get("exit_requested"):
            self._request_exit(sym, pos, pos["exit_requested"])
            return
        # Fetch latest price
        ticker = binance_client.get_ticker_price(sym, market=pos["market_type"], mark=True)
        if not ticker:
            raise RuntimeError(f"Không lấy được giá giám sát {sym}")
        current_price = float(ticker.get("price", 0))
        if current_price <= 0:
            return

        entry_price = pos["entry_price"]
        qty = pos["quantity"]
        side = pos.get("position_side", "LONG")
        lev = pos.get("leverage", 1) if pos.get("market_type") == "FUTURES" else 1

        # Calculate PnL
        if side == "LONG":
            raw_pct = ((current_price - entry_price) / entry_price) * 100.0
            pnl_usdt = (current_price - entry_price) * qty
        else: # SHORT
            raw_pct = ((entry_price - current_price) / entry_price) * 100.0
            pnl_usdt = (entry_price - current_price) * qty

        pnl_pct = raw_pct * lev
        
        with self.lock:
            pos["current_price"] = current_price
            pos["current_pnl_pct"] = round(pnl_pct, 2)
            pos["current_pnl_usdt"] = round(pnl_usdt, 2)

        # Check Take Profit
        target_tp = pos.get("target_tp_price")
        target_sl = pos.get("target_sl_price")
        hit_tp = (current_price >= target_tp if side == "LONG" else current_price <= target_tp) if target_tp else pnl_pct >= self.take_profit_pct
        hit_sl = (current_price <= target_sl if side == "LONG" else current_price >= target_sl) if target_sl else pnl_pct <= -self.stop_loss_pct
        if hit_tp:
            self.add_log("TP", f"🎯 CHỐT LỜI {sym} [{side}]: Giá ${current_price:,.4f} đạt +{pnl_pct:.2f}% (Lãi: +${pnl_usdt:.2f})")
            self._request_exit(sym, pos, "TAKE_PROFIT")
            return

        # Check Stop Loss
        if hit_sl:
            self.add_log("SL", f"🛑 CẮT LỖ {sym} [{side}]: Giá ${current_price:,.4f} chạm -{abs(pnl_pct):.2f}% (Lỗ: ${pnl_usdt:.2f})")
            self._request_exit(sym, pos, "STOP_LOSS")
            return

        self.add_log("MONITOR", f"Đang giữ {sym} [{pos['market_type']} {side} {lev}x] | Giá: ${current_price:,.4f} | PnL: {pnl_pct:+.2f}% (${pnl_usdt:+.2f})")

    def _request_exit(self, sym, pos, reason):
        with self.order_lock:
            if sym in self.pending_orders:
                self.pending_orders[sym]["cancel_requested"] = True
                self.pending_orders[sym]["exit_reason"] = reason
                pos["exit_requested"] = reason
                self.save_state()
                return
            result = self._close_position(sym, reason, expected_position=pos)
            if not result["success"]:
                self.add_log("WARN", f"{sym}: {result.get('error')}")

    def _scan_symbol_for_entry(self, sym: str):
        with self.lock:
            if (not self.is_running or not self.entries_enabled or self.pending_orders
                    or not self._protection_ready()
                    or sym in self.active_positions or len(self.active_positions) >= self.max_positions):
                return
            market, interval = self.market_type, self.interval
        candle_key = f"{market}:{sym}:{interval}"
        now_ms = binance_client._get_timestamp()
        if now_ms < self.next_candle_check_ms.get(candle_key, 0):
            return
        candles = binance_client.get_klines(sym, interval=interval, limit=61, market=market)
        if candles and not candles[-1]["closed"]:
            self.next_candle_check_ms[candle_key] = candles[-1]["close_time_ms"] + 1
        candles = validate_closed_candles(candles, interval, now_ms)
        close_time = candles[-1]["close_time_ms"]
        if self.last_entry_candle.get(candle_key, 0) >= close_time:
            return

        current_price = candles[-1]["close"]
        analysis = analyze_market_signals(candles)
        with self.lock:
            self.last_entry_candle[candle_key] = close_time
            if not self.save_state():
                self.state_error = True
                self.is_running = False
                return
        
        # Save last signal if it's the currently viewed symbol
        if sym == self.symbol:
            self.last_signal = analysis

        decision = decide_entry(self.strategy, analysis, allow_short=(market == "FUTURES"))
        if decision:
            direction, reason = decision
            self.add_log("SIGNAL", f"🚀 TÍN HIỆU {sym} [{direction}]: {reason} tại giá ${current_price:,.4f}")
            self._execute_entry(sym, direction, current_price, reason, market=market)
        else:
            self.add_log("SCAN", f"Quét {sym} | Giá: ${current_price:,.4f} | RSI: {analysis['rsi']} | Điểm: {analysis['net_score']} | Tín hiệu: {analysis['action']}")

    def _execute_entry(self, sym, direction, current_price, trigger_reason, market=None):
        with self.order_lock:
            with self.lock:
                market = market or self.market_type
                if (self.fixed_market and market != self.fixed_market) or (market == "SPOT" and direction != "LONG"):
                    raise ValueError("Lệnh không thuộc thị trường của bot")
                if (not self.is_running or self.close_all_requested or self.stop_event.is_set()
                        or market != self.market_type or sym in self.active_positions
                        or self.pending_orders or not self.entries_enabled
                        or not self._protection_ready()
                        or len(self.active_positions) >= self.max_positions):
                    return
                leverage = self.leverage if market == "FUTURES" else 1
                amount = self.trade_amount_usdt
                order = {
                    "id": uuid4().hex, "market": market, "kind": "entry", "direction": direction,
                    "leverage": leverage, "reason": trigger_reason,
                    "tp_pct": self.take_profit_pct, "sl_pct": self.stop_loss_pct,
                }
            if not isfinite(current_price) or current_price <= 0:
                return
            with self.lock:
                used_margin = sum(p["cost_usdt"] for p in self.active_positions.values())
                open_risk = sum(abs(p["entry_price"] - p["target_sl_price"]) * p["quantity"]
                                for p in self.active_positions.values() if p.get("target_sl_price"))
                if used_margin + amount > self.max_total_margin_usdt + 1e-9:
                    raise ValueError("Đã đạt trần tổng vốn của bot")
                if open_risk + amount * self.stop_loss_pct / 100 > self.max_open_risk_usdt + 1e-9:
                    raise ValueError("Đã đạt trần tổng lỗ dự kiến tại SL")
            qty, current_price = binance_client.prepare_entry(market, sym, amount, leverage, current_price)
            account = binance_client.get_futures_account_info() if market == "FUTURES" else binance_client.get_account_info()
            if not account.get("success"):
                raise RuntimeError("Không xác nhận được số dư trước khi mở lệnh")
            available = account.get("availableBalance", 0) if market == "FUTURES" else next(
                (b["free"] for b in account.get("balances", []) if b["asset"] == "USDT"), 0)
            if float(available) < amount:
                raise ValueError("Không đủ số dư khả dụng")
            if market == "FUTURES":
                if not self._check_futures_positions(sym):
                    self.add_log("WARN", f"Vị thế Futures {sym} khác dữ liệu của bot.")
                    return
                result = binance_client.set_leverage(sym, leverage)
                if not result.get("success") or result.get("leverage", leverage) != leverage:
                    self.add_log("ERROR", f"Không xác nhận được đòn bẩy {sym}: {result.get('error')}")
                    return
            with self.lock:
                if not self.is_running or self.close_all_requested or self.stop_event.is_set() or market != self.market_type:
                    return
            result = self._submit_bot_order(sym, order, quantity=qty, quote=amount)
            if not result["success"]:
                self.add_log("WARN", f"{sym}: {result['error']}")

    def _close_position(self, sym, exit_reason="MANUAL_CLOSE", expected_position=None):
        with self.order_lock:
            with self.lock:
                pos = self.active_positions.get(sym)
                if sym in self.pending_orders:
                    return {"success": False, "error": "Lệnh trước đang chờ đối soát."}
                if expected_position is not None and pos is not expected_position:
                    return {"success": False, "error": "Vị thế đã thay đổi."}
                if not pos or pos["quantity"] <= 0:
                    return {"success": False, "error": "Không có khối lượng đã khớp để đóng."}
                pos["exit_requested"] = exit_reason
                if not self._persist_order_state():
                    return {"success": False, "error": "Không lưu được yêu cầu đóng vị thế."}
            self._reconcile_protections(cancel_symbol=sym)
            with self.lock:
                if any(o["symbol"] == sym for o in self.protective_orders.values()):
                    return {"success": False, "pending": True, "error": "Đang xác nhận hủy/khớp lệnh cắt lỗ trên sàn."}
                pos = self.active_positions.get(sym)
                if not pos:
                    return {"success": True, "remaining_quantity": 0}
                order = {
                    "id": uuid4().hex, "market": pos["market_type"], "kind": "close",
                    "direction": pos["position_side"], "reason": exit_reason,
                }
                qty = pos["quantity"]
            if order["market"] == "SPOT":
                filters = binance_client.load_symbol_filters(sym)
                base_asset = filters.get("baseAsset")
                if not base_asset:
                    return {"success": False, "error": "Không xác định được tài sản Spot."}
                qty = min(qty, binance_client.get_asset_balance(base_asset))
                if qty <= 0:
                    return {"success": False, "error": "Không xác nhận được số dư Spot khả dụng."}
            else:
                try:
                    positions = binance_client.get_futures_positions(sym, strict=True)
                    actual = next((p for p in positions if p["symbol"] == sym and p.get("positionSide", "BOTH") == "BOTH"), None)
                    signed_qty = float(actual["positionAmt"]) if actual else 0
                    direction = 1 if order["direction"] == "LONG" else -1
                    if signed_qty * direction <= 0:
                        return {"success": False, "error": "Vị thế trên sàn đã đóng hoặc đổi chiều. Cần đối soát lịch sử khớp."}
                    qty = min(qty, abs(signed_qty))
                except (RuntimeError, ValueError):
                    return {"success": False, "error": "Không xác nhận được vị thế trên sàn."}
            return self._submit_bot_order(sym, order, quantity=qty)

    def _submit_bot_order(self, sym, order, quantity, quote=None):
        with self.lock:
            if self.state_error:
                return {"success": False, "error": "Trạng thái bot chưa sẵn sàng."}
            self.pending_orders[sym] = order
            if not self.save_state():
                self.state_error = True
                self.is_running = False
                del self.pending_orders[sym]
                return {"success": False, "error": "Không lưu được lệnh trước khi gửi."}
        entry = order["kind"] == "entry"
        buy = (order["direction"] == "LONG") == entry
        side = "BUY" if buy else "SELL"
        if order["market"] == "FUTURES":
            result = binance_client.create_futures_order(
                sym, side, quantity=quantity, reduce_only=not entry, client_order_id=order["id"]
            )
        else:
            result = binance_client.create_order(
                sym, side, quantity=None if entry else quantity, quote_order_qty=quote,
                client_order_id=order["id"]
            )
        outcome = self._handle_order_result(sym, order, result)
        self._ensure_futures_protection(sym)
        return outcome

    def _handle_order_result(self, sym, order, result, storage=None, storage_key=None):
        storage = self.pending_orders if storage is None else storage
        storage_key = sym if storage_key is None else storage_key
        data = result.get("data") or {}
        status = data.get("status")
        terminal = status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
        if result.get("unknown"):
            return {"success": False, "pending": True, "error": "Lệnh đang chờ xác nhận từ Binance."}
        if not result.get("success"):
            # A locally rejected submission is definitive. Lookup failures return unknown.
            with self.lock:
                storage.pop(storage_key, None)
                self._persist_order_state()
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

        trade = None
        with self.lock:
            if delta_qty > 0:
                if order["kind"] == "entry":
                    self._record_entry(sym, order, data, float(delta_qty), float(delta_quote))
                else:
                    pos = self.active_positions.get(sym)
                    if not pos or (float(delta_qty) > pos["quantity"] and not isclose(float(delta_qty), pos["quantity"])):
                        return {"success": False, "pending": True, "error": "Khối lượng khớp khác dữ liệu vị thế."}
                    trade = self._record_exit(sym, pos, order, float(delta_qty), float(delta_quote))
                order["applied_qty"] = str(qty)
                order["applied_quote"] = str(quote)
            order["exchange_status"] = status
            if terminal:
                storage.pop(storage_key, None)
            if not self._persist_order_state():
                return {"success": False, "error": "Không lưu được kết quả khớp lệnh. Đã dừng mở mới."}
            remaining = self.active_positions.get(sym, {}).get("quantity", 0)
        if not terminal:
            return {"success": False, "pending": True, "error": "Đã cập nhật phần khớp; đang chờ phần còn lại."}
        if qty <= 0:
            return {"success": False, "error": f"Lệnh {status} không khớp."}
        return {"success": True, "trade": trade, "remaining_quantity": remaining if order["kind"] == "close" else 0}

    def _persist_order_state(self):
        if self.save_state():
            return True
        self.state_error = True
        self.is_running = False
        self.last_error = "Không lưu được trạng thái giao dịch"
        return False

    def _record_entry(self, sym, order, data, qty, quote):
        existing = self.active_positions.get(sym)
        entry_time = existing["entry_time"] if existing else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing:
            qty += existing["quantity"]
            quote += existing["notional_usdt"]
        price = quote / qty
        lev = order["leverage"]
        target_tp, target_sl = exit_prices(price, order["direction"], order["tp_pct"], order["sl_pct"], lev)
        pos = existing if existing is not None else {}
        pos.update({
            "position_id": pos.get("position_id", order["id"]),
            "market_type": order["market"], "position_side": order["direction"], "leverage": lev,
            "symbol": sym, "entry_price": price, "current_price": price, "quantity": qty,
            "cost_usdt": quote / lev, "notional_usdt": quote,
            "entry_time": entry_time, "realized_pnl_usdt": pos.get("realized_pnl_usdt", 0),
            "target_tp_price": target_tp,
            "target_sl_price": target_sl,
            "current_pnl_pct": 0.0, "current_pnl_usdt": 0.0,
            "trigger_reason": order["reason"], "order_id": data.get("orderId"),
        })
        if order.get("cancel_requested"):
            pos["exit_requested"] = order.get("exit_reason", "CANCEL_ENTRY")
        self.active_positions[sym] = pos
        self.add_log("SUCCESS", f"Đã cập nhật khớp lệnh {sym}: {qty:g} @ {price:g}")

    def _record_exit(self, sym, pos, order, qty, quote):
        price = quote / qty
        entry_price = pos["entry_price"]
        direction = 1 if pos["position_side"] == "LONG" else -1
        pnl = direction * (price - entry_price) * qty
        trade = {
            "market_type": pos["market_type"], "position_side": pos["position_side"],
            "leverage": pos["leverage"], "symbol": sym, "entry_time": pos["entry_time"],
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": entry_price, "exit_price": price, "quantity": qty,
            "pnl_usdt": round(pnl, 8),
            "pnl_pct": round(direction * (price / entry_price - 1) * 100 * pos["leverage"], 2),
            "reason": order["reason"],
        }
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-500:]
        self.total_pnl_usdt = round(self.total_pnl_usdt + pnl, 8)
        pos["realized_pnl_usdt"] = pos.get("realized_pnl_usdt", 0) + pnl
        if isclose(qty, pos["quantity"], rel_tol=1e-9):
            if pos["realized_pnl_usdt"] > 0:
                self.win_count += 1
            elif pos["realized_pnl_usdt"] < 0:
                self.loss_count += 1
            del self.active_positions[sym]
        else:
            remaining_fraction = (pos["quantity"] - qty) / pos["quantity"]
            pos["quantity"] = float(Decimal(str(pos["quantity"])) - Decimal(str(qty)))
            pos["cost_usdt"] *= remaining_fraction
            pos["notional_usdt"] *= remaining_fraction
        self.add_log("SUCCESS", f"Khớp lệnh đóng {sym}: {qty} @ {price:g}")
        return trade

    def close_all_positions(self, exit_reason="MANUAL_CLOSE_ALL"):
        with self.lock:
            self.is_running = False
            self.close_all_requested = True
        with self.order_lock:
            if not self.save_state():
                return {"success": False, "error": "Không lưu được yêu cầu đóng toàn bộ."}
            self._reconcile_orders()
            self._reconcile_protections()
            results = []
            for sym in list(self.active_positions):
                results.append({"symbol": sym, "result": self._close_position(sym, exit_reason)})
            # Positions the bot does not track are never touched here: they may be manual
            # trades. Use close_untracked_positions with an explicit symbol list instead.
            with self.lock:
                complete = not self.active_positions and not self.pending_orders and not self.protective_orders
                if complete:
                    self.close_all_requested = False
                    self.save_state()
        self.supervise()
        return {"success": complete, "pending": not complete,
                "count": sum(r["result"].get("success", False) for r in results),
                "results": results, "message": "Đã đóng toàn bộ và dừng mở lệnh mới." if complete
                else "Đã dừng mở lệnh mới. Bot đang tiếp tục hủy lệnh chờ và đóng vị thế.",
                "error": None if complete else "Đang tiếp tục đóng các vị thế còn lại."}

    def close_untracked_positions(self, symbols):
        """Close exchange positions the bot does not manage, only for symbols the user named."""
        symbols = list(dict.fromkeys(s.strip().upper() for s in symbols or [] if s and s.strip()))
        if not symbols:
            raise ValueError("Chọn ít nhất một mã cần đóng.")
        if self.market_type != "FUTURES":
            raise ValueError("Chỉ bot Futures có vị thế ngoài bot để đóng.")
        results = []
        with self.order_lock:
            with self.lock:
                if self.is_running:
                    raise ValueError("Dừng bot trước khi đóng vị thế ngoài bot.")
                if self.state_error:
                    raise RuntimeError("Không đọc hoặc lưu được trạng thái bot.")
                managed = set(self.active_positions) | set(self.pending_orders) | {
                    o["symbol"] for o in self.protective_orders.values()}
            for sym in symbols:
                if sym in managed:
                    results.append({"symbol": sym, "success": False,
                                    "error": "Mã do bot quản lý. Dùng nút đóng vị thế của bot."})
                    continue
                try:
                    positions = binance_client.get_futures_positions(sym, strict=True)
                except (RuntimeError, ValueError) as exc:
                    results.append({"symbol": sym, "success": False, "error": f"Không xác nhận được vị thế: {exc}"})
                    continue
                if any(p.get("positionSide", "BOTH") != "BOTH" for p in positions if p["symbol"] == sym):
                    results.append({"symbol": sym, "success": False, "error": "Hedge mode không được hỗ trợ."})
                    continue
                amt = sum(float(p["positionAmt"]) for p in positions if p["symbol"] == sym)
                if not amt:
                    results.append({"symbol": sym, "success": True, "closed_quantity": 0,
                                    "message": "Không còn vị thế trên sàn."})
                    continue
                side = "SELL" if amt > 0 else "BUY"
                result = binance_client.create_futures_order(
                    sym, side, quantity=abs(amt), reduce_only=True,
                    client_order_id="untracked_" + uuid4().hex[:20])
                data = result.get("data") or {}
                if result.get("success") and data.get("status") == "FILLED":
                    results.append({"symbol": sym, "success": True, "closed_quantity": abs(amt)})
                    self.add_log("SUCCESS", f"Đã đóng vị thế ngoài bot {sym}: {side} {abs(amt):g}")
                else:
                    error = result.get("error") or f"Lệnh đóng chưa khớp hết ({data.get('status', 'không rõ')})"
                    if result.get("unknown"):
                        error = "Lệnh đóng chưa rõ kết quả. Kiểm tra trên Binance trước khi thử lại."
                    results.append({"symbol": sym, "success": False, "error": error})
                    self.add_log("WARN", f"Đóng vị thế ngoài bot {sym} thất bại: {error}")
            try:
                self._check_futures_positions()
            except (RuntimeError, ValueError) as exc:
                self.add_log("WARN", f"Đối soát Futures thất bại: {exc}")
        return {"success": all(r["success"] for r in results), "results": results}

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            total_trades = self.win_count + self.loss_count
            winrate = round((self.win_count / total_trades) * 100.0, 1) if total_trades > 0 else 0.0
            pos_list = deepcopy(list(self.active_positions.values()))
            for pos in pos_list:
                stops = [o for o in self.protective_orders.values() if o["symbol"] == pos["symbol"]
                         and o["position_id"] == pos["position_id"]]
                confirmed = [o for o in stops if o.get("algo_status") == "NEW" and not o.get("cancel_requested")]
                covered = sum(float(o["quantity"]) for o in confirmed)
                pos["stop_protection"] = "local" if pos["market_type"] == "SPOT" else (
                    "exchange" if not pos.get("exit_requested") and isclose(covered, pos["quantity"], rel_tol=1e-9)
                    else "pending")
                pos["exchange_stop_prices"] = [o.get("exchange_trigger_price") for o in confirmed]
            single_pos = pos_list[0] if pos_list else None

            return {
                "is_running": self.is_running,
                "monitoring": bool(self.thread and self.thread.is_alive()),
                "close_all_requested": self.close_all_requested,
                "last_error": self.last_error,
                "state_error": self.state_error,
                "entries_enabled": self.entries_enabled,
                "position_mismatches": dict(self.position_mismatches),
                "interval": self.interval,
                "max_total_margin_usdt": self.max_total_margin_usdt,
                "max_open_risk_usdt": self.max_open_risk_usdt,
                "pnl_basis": "gross_before_fees_and_funding",
                "history_scope": self.history_scope,
                "market_type": self.market_type,
                "leverage": self.leverage,
                "symbol": self.symbol,
                "watchlist_symbols": self.watchlist_symbols,
                "max_positions": self.max_positions,
                "strategy": self.strategy,
                "trade_amount_usdt": self.trade_amount_usdt,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
                "scan_interval_sec": self.scan_interval_sec,
                "active_position": single_pos, # backward compat
                "active_positions": pos_list,  # multi-position list
                "active_positions_count": len(pos_list),
                "pending_orders": {sym: order.copy() for sym, order in self.pending_orders.items()},
                "protective_orders": deepcopy(self.protective_orders),
                "futures_protection_ready": self._protection_ready(),
                "total_pnl_usdt": self.total_pnl_usdt,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "total_trades": total_trades,
                "winrate": winrate,
                "last_signal": self.last_signal,
                "last_check_time": self.last_check_time,
                "recent_logs": self.logs[:40],
                "closed_trades": deepcopy(list(reversed(self.closed_trades[-15:])))
            }

