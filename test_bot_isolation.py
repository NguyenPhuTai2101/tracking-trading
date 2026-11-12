import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import bot_engine
import main
import test_trading_upgrade as regression
from bot_manager import TradingBotManager


class BotIsolationTests(unittest.TestCase):
    def setUp(self):
        regression.TradingUpgradeTests.setUp(self)
        self.manager = TradingBotManager(Path(bot_engine.STATE_FILE).parent)
        self.spot = self.manager.engines["SPOT"]
        self.futures = self.manager.engines["FUTURES"]
        for engine in self.manager.engines.values():
            engine.supervise = Mock()
        self.api.get_account_info.return_value = {"success": True, "balances": [{"asset": "USDT", "free": 1000}]}
        self.api.create_order.return_value = regression.fill(qty="0.2", quote="20")
        self.api.create_futures_order.return_value = regression.fill()

    def open_both(self):
        for engine in (self.spot, self.futures):
            engine.is_running = engine.entries_enabled = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(e._execute_entry, "BTCUSDT", "LONG", 100, "isolation")
                     for e in (self.spot, self.futures)]
            for task in tasks:
                task.result()

    def test_configuration_and_state_files_are_independent(self):
        self.futures.is_running = True
        self.spot.update_config(amount=15, interval="5m", strategy="RSI_SCALPING", sl=2, tp=4)
        self.assertEqual(self.futures.trade_amount_usdt, 20)
        self.assertEqual(self.futures.interval, "15m")
        self.assertEqual(self.futures.strategy, "HYBRID_SMART")
        self.assertEqual(self.spot.leverage, 1)
        self.assertIn("PEPEUSDT", self.spot.watchlist_symbols)
        self.assertIn("1000PEPEUSDT", self.futures.watchlist_symbols)
        restored = bot_engine.TradingBotEngine("SPOT", self.spot.state_file)
        self.assertEqual(restored.trade_amount_usdt, 15)
        self.assertNotEqual(self.spot.state_file, self.futures.state_file)

    def test_bots_cannot_change_market_or_add_spot_leverage(self):
        with self.assertRaises(ValueError):
            self.spot.update_config(market_type="FUTURES")
        with self.assertRaises(ValueError):
            self.futures.update_config(market_type="SPOT")
        with self.assertRaises(ValueError):
            self.spot.update_config(leverage=10)
        with self.assertRaises(ValueError):
            self.spot._execute_entry("BTCUSDT", "SHORT", 100, "invalid")

    def test_same_symbol_can_be_managed_by_both_markets(self):
        self.open_both()
        self.assertEqual(self.spot.active_positions["BTCUSDT"]["quantity"], .2)
        self.assertEqual(self.futures.active_positions["BTCUSDT"]["quantity"], 2)
        self.assertFalse(self.spot.protective_orders)
        self.assertEqual(len(self.futures.protective_orders), 1)
        self.api.create_order.assert_called_once()
        self.api.create_futures_order.assert_called_once()

    def test_stop_and_close_all_affect_only_selected_market(self):
        self.open_both()
        self.spot.stop()
        self.assertTrue(self.futures.is_running)
        self.api.load_symbol_filters.return_value = {"baseAsset": "BTC"}
        self.api.get_asset_balance.return_value = 1
        self.api.create_order.return_value = regression.fill(qty="0.2", quote="21")
        result = self.spot.close_all_positions()
        self.assertTrue(result["success"])
        self.assertFalse(self.spot.active_positions)
        self.assertTrue(self.futures.active_positions)
        self.assertTrue(self.futures.protective_orders)
        self.assertTrue(self.futures.is_running)
        self.assertEqual(self.spot.total_pnl_usdt, 1)
        self.assertEqual(self.futures.total_pnl_usdt, 0)
        self.api.cancel_futures_stop.assert_not_called()

    def test_manual_guard_is_scoped_to_market(self):
        self.futures.is_running = True
        with self.spot.manual_action("SPOT", "BTCUSDT"):
            pass
        with self.assertRaises(ValueError):
            with self.futures.manual_action("FUTURES", "BTCUSDT"):
                self.fail("Must block manual interference")

    def test_api_requires_market_and_routes_all_controls(self):
        with patch.object(main, "bot_manager", self.manager):
            client = TestClient(main.app)
            for path in ("start", "stop", "config", "close-position", "close-all"):
                self.assertEqual(client.post(f"/api/bot/{path}", json={}).status_code, 422)
            self.assertEqual(client.get("/api/bot/status?market=INVALID").status_code, 422)
            response = client.post("/api/bot/config?market=SPOT", json={"trade_amount_usdt": 35, "interval": "5m"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.futures.trade_amount_usdt, 20)
            self.assertEqual(client.post("/api/bot/config?market=SPOT", json={"market_type": "FUTURES"}).status_code, 422)
            client.post("/api/bot/start?market=FUTURES")
            client.post("/api/bot/start?market=SPOT")
            client.post("/api/bot/stop?market=SPOT")
            self.assertTrue(self.futures.is_running)
            self.assertFalse(self.spot.is_running)
            data = client.get("/api/bots/status").json()["bots"]
            self.assertEqual(data["SPOT"]["trade_amount_usdt"], 35)
            self.assertEqual(data["FUTURES"]["trade_amount_usdt"], 20)
            self.assertEqual(client.get("/api/bot/status?market=SPOT").json()["market_type"], "SPOT")

    def test_legacy_migration_preserves_open_fills_stops_and_original(self):
        regression.TradingUpgradeTests.open_position(self)
        self.bot.interval = "1m"
        self.bot.last_entry_candle = {"FUTURES:BTCUSDT:1m": 1234}
        self.bot.total_pnl_usdt = 12.5
        self.bot.save_state()
        original = Path(self.bot.state_file).read_bytes()
        self.manager.legacy_file.write_bytes(original)
        self.manager.acquire_locks()
        try:
            self.manager.load()
            self.assertEqual(self.futures.active_positions, self.bot.active_positions)
            self.assertEqual(self.futures.protective_orders, self.bot.protective_orders)
            self.assertEqual(self.futures.interval, "1m")
            self.assertEqual(self.futures.last_entry_candle, self.bot.last_entry_candle)
            self.assertEqual(self.futures.total_pnl_usdt, 12.5)
            self.assertFalse(self.spot.active_positions)
            self.assertFalse(self.spot.is_running)
            self.assertFalse(self.futures.is_running)
            self.spot.update_config(amount=35)
            self.manager.load()
            self.assertEqual(self.spot.trade_amount_usdt, 35)
            self.assertEqual(self.manager.legacy_file.read_bytes(), original)
            self.api.create_futures_order.assert_called_once()
            self.api.create_futures_stop.assert_called_once()  # Migration cannot repost stops.
        finally:
            self.manager.release_locks()

    def test_legacy_spot_configuration_migrates_to_spot_only(self):
        self.bot.market_type = "SPOT"
        self.bot.watchlist_symbols = ["BTCUSDT", "PEPEUSDT"]
        self.bot.trade_amount_usdt = 65
        self.bot.save_state()
        self.manager.legacy_file.write_bytes(Path(self.bot.state_file).read_bytes())
        self.manager.acquire_locks()
        try:
            self.manager.load()
        finally:
            self.manager.release_locks()
        self.assertEqual(self.spot.trade_amount_usdt, 65)
        self.assertEqual(self.spot.leverage, 1)
        self.assertEqual(self.futures.trade_amount_usdt, 20)

    def test_second_manager_and_legacy_process_cannot_share_writer_lock(self):
        self.manager.acquire_locks()
        try:
            with self.assertRaises(RuntimeError):
                TradingBotManager(self.manager.legacy_file.parent).acquire_locks()
            legacy = bot_engine.TradingBotEngine(state_file=self.manager.legacy_file)
            with self.assertRaises(RuntimeError):
                legacy.acquire_process_lock()
        finally:
            self.manager.release_locks()

    def test_wrong_market_state_is_blocked(self):
        self.spot.save_state()
        path = Path(self.spot.state_file)
        data = json.loads(path.read_text())
        data["market_type"] = "FUTURES"
        path.write_text(json.dumps(data))
        restored = bot_engine.TradingBotEngine("SPOT", path)
        self.assertTrue(restored.state_error)
        self.assertFalse(restored.start()["success"])


if __name__ == "__main__":
    unittest.main()
