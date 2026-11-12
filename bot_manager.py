"""Two independent market workers sharing only the Binance HTTP client."""
import json
import os
from copy import deepcopy
from pathlib import Path

from bot_engine import STATE_FILE, TradingBotEngine


class TradingBotManager:
    def __init__(self, state_dir=None):
        directory = Path(state_dir) if state_dir else Path(STATE_FILE).parent
        self.legacy_file = directory / "bot_state.json"
        # Also excludes an older single-bot process while migrating/restarting.
        self.guard = TradingBotEngine(state_file=self.legacy_file, autoload=False)
        self.engines = {
            market: TradingBotEngine(market, directory / f"bot_state_{market.lower()}.json", autoload=False)
            for market in ("SPOT", "FUTURES")
        }

    def acquire_locks(self):
        try:
            self.guard.acquire_process_lock()
            for engine in self.engines.values():
                engine.acquire_process_lock()
        except Exception:
            self.release_locks()
            raise

    def release_locks(self):
        for engine in self.engines.values():
            engine.release_process_lock()
        self.guard.release_process_lock()

    def load(self):
        """Call with process locks held. Retain the original legacy file as backup."""
        if self.legacy_file.exists() and any(not Path(e.state_file).exists() for e in self.engines.values()):
            legacy = TradingBotEngine(state_file=self.legacy_file)
            if legacy.state_error:
                raise RuntimeError("Không chuyển đổi được file trạng thái bot cũ")
            original = json.loads(self.legacy_file.read_text(encoding="utf-8"))
            for market, engine in self.engines.items():
                if Path(engine.state_file).exists():
                    continue  # Never overwrite configuration or fills from a previous launch.
                data = deepcopy(original) if market == legacy.market_type else engine.get_status()
                data["market_type"] = market
                if market == "SPOT":
                    data["leverage"] = 1
                data["active_positions"] = {s: deepcopy(p) for s, p in legacy.active_positions.items()
                                            if p["market_type"] == market}
                data["pending_orders"] = {s: deepcopy(o) for s, o in legacy.pending_orders.items()
                                          if o["market"] == market}
                data["protective_orders"] = {k: deepcopy(o) for k, o in legacy.protective_orders.items()
                                             if o["market"] == market}
                data["last_entry_candle"] = {k: v for k, v in legacy.last_entry_candle.items()
                                             if k.startswith(market + ":")}
                data["close_all_requested"] = legacy.close_all_requested and bool(
                    data["active_positions"] or data["pending_orders"] or data["protective_orders"])
                # Historical aggregate counts cannot be reconstructed from retained partial fills.
                # Preserve them on the legacy owner and label any mixed-market legacy history.
                if market == legacy.market_type and any(t.get("market_type") != market for t in legacy.closed_trades):
                    data["history_scope"] = "legacy_combined"
                temp_path = engine.state_file + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, allow_nan=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, engine.state_file)
        for engine in self.engines.values():
            engine.load_state()
            if engine.state_error:
                raise RuntimeError(f"Trạng thái bot {engine.fixed_market} không hợp lệ")
            if not Path(engine.state_file).exists() and not engine.save_state():
                raise RuntimeError(f"Không lưu được trạng thái bot {engine.fixed_market}")

    def startup(self):
        self.acquire_locks()
        try:
            self.load()
            for engine in self.engines.values():
                engine.supervise()
        except Exception:
            self.shutdown()
            raise

    def shutdown(self):
        # Pause both immediately before waiting for any in-flight request to finish.
        for engine in self.engines.values():
            with engine.lock:
                engine.is_running = False
                engine.stop_event.set()
        for engine in self.engines.values():
            engine.shutdown()
        self.guard.release_process_lock()


bot_manager = TradingBotManager()
