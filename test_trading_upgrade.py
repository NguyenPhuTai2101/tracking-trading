import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

import bot_engine
import main
from binance_client import BinanceDemoClient
from indicators import analyze_market_signals
from trading_rules import BotConfigRequest, validate_closed_candles


def candles(price=100.0):
    duration = 900_000
    boundary = int(time.time() * 1000) // duration * duration
    return [dict(time=(boundary - (60 - i) * duration) // 1000,
                 close_time_ms=boundary - (59 - i) * duration - 1,
                 open=price, high=price, low=price, close=price, closed=True)
            for i in range(60)]


def fill(status="FILLED", qty="2", quote="200"):
    return {"success": True, "data": {"orderId": 123, "status": status,
                                     "executedQty": qty, "cumQuote": quote}}


class TradingUpgradeTests(unittest.TestCase):
    def setUp(self):
        # An unmocked network call must fail, never touch a real trading account.
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_patch = patch.object(bot_engine, "STATE_FILE", str(Path(directory.name) / "state.json"))
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.api = Mock()
        self.api.has_credentials = True
        self.api.validate_symbol.return_value = None
        self.api.get_futures_positions.return_value = []
        self.api.set_leverage.return_value = {"success": True, "leverage": 10}
        self.api.get_futures_account_info.return_value = {"success": True, "availableBalance": 5000}
        self.api.prepare_entry.side_effect = lambda market, sym, budget, lev, price: (budget * lev / price, price)
        self.api._get_timestamp.side_effect = lambda: int(time.time() * 1000)
        self.stops = {}
        def create_stop(sym, side, quantity, trigger_price, key):
            data = dict(algoId=321, clientAlgoId=key, symbol=sym, side=side,
                        quantity=str(quantity), triggerPrice=str(trigger_price), reduceOnly=True,
                        orderType="STOP_MARKET", workingType="MARK_PRICE", algoStatus="NEW")
            self.stops[key] = data
            return {"success": True, "data": data.copy()}
        def query_stop(key):
            return {"success": True, "data": self.stops[key].copy()}
        def cancel_stop(key):
            self.stops[key]["algoStatus"] = "CANCELED"
            return query_stop(key)
        self.api.create_futures_stop.side_effect = create_stop
        self.api.query_futures_stop.side_effect = query_stop
        self.api.cancel_futures_stop.side_effect = cancel_stop
        api_patch = patch.object(bot_engine, "binance_client", self.api)
        api_patch.start()
        self.addCleanup(api_patch.stop)
        self.bot = bot_engine.TradingBotEngine()
        self.bot.is_running = self.bot.entries_enabled = True
        self.bot.supervise = Mock()

    def open_position(self, result=None):
        self.api.create_futures_order.return_value = result or fill()
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test")

    def test_partial_entry_immediately_managed_and_survives_restart(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["quantity"], 1)
        order_id = self.bot.pending_orders["BTCUSDT"]["id"]
        restored = bot_engine.TradingBotEngine()
        self.assertEqual(restored.pending_orders["BTCUSDT"]["id"], order_id)
        self.api.query_order.return_value = fill("FILLED", "2", "202")
        restored._reconcile_orders()
        self.assertEqual(restored.active_positions["BTCUSDT"]["quantity"], 2)
        self.assertEqual(restored.active_positions["BTCUSDT"]["entry_price"], 101)
        self.assertFalse(restored.pending_orders)
        self.assertEqual(len(restored.protective_orders), 2)
        self.assertTrue(restored._protection_ready())

    def test_futures_stop_covers_only_filled_quantity(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        args = self.api.create_futures_stop.call_args.args
        self.assertEqual(args[:3], ("BTCUSDT", "SELL", 1))
        self.assertLess(args[3], 100)
        self.assertTrue(self.bot.protective_orders)
        self.api.query_order.return_value = fill("PARTIALLY_FILLED", "1", "100")
        self.bot._reconcile_orders()
        self.api.create_futures_stop.assert_called_once()

    def test_stop_timeout_is_reconciled_without_resubmission(self):
        self.api.create_futures_stop.side_effect = None
        self.api.create_futures_stop.return_value = {"success": False, "unknown": True}
        self.open_position()
        self.assertFalse(self.bot._protection_ready())
        restored = bot_engine.TradingBotEngine()
        self.api.query_futures_stop.side_effect = None
        self.api.query_futures_stop.return_value = {"success": False, "unknown": True}
        restored._reconcile_protections()
        restored._ensure_futures_protection("BTCUSDT")
        self.api.create_futures_stop.assert_called_once()
        self.assertEqual(len(restored.protective_orders), 1)

    def test_rejected_stop_pauses_entries_and_requests_close(self):
        self.api.create_futures_stop.side_effect = None
        self.api.create_futures_stop.return_value = {"success": False, "error": "rejected"}
        self.open_position()
        self.assertFalse(self.bot.is_running)
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["exit_requested"], "PROTECTION_FAILED")

    def test_exchange_stop_fill_reconciles_after_restart(self):
        self.open_position()
        key = next(iter(self.stops))
        self.stops[key].update(algoStatus="FINISHED", actualOrderId="456")
        self.api.query_order.return_value = fill("FILLED", "2", "198")
        restored = bot_engine.TradingBotEngine()
        restored._reconcile_protections()
        restored._reconcile_protections()
        self.assertFalse(restored.active_positions)
        self.assertFalse(restored.protective_orders)
        self.assertEqual(restored.total_pnl_usdt, -2)
        self.assertEqual(restored.loss_count, 1)
        self.api.create_futures_order.assert_called_once()  # Entry only; no duplicate exit.
        self.api.query_order.assert_called_once_with("FUTURES", "BTCUSDT", order_id="456")

    def test_cancel_stop_race_with_fill_does_not_send_close(self):
        self.open_position()
        self.api.cancel_futures_stop.side_effect = lambda key: {
            "success": True, "data": dict(self.stops[key], algoStatus="FINISHED", actualOrderId="456")}
        self.api.query_order.return_value = fill("FILLED", "2", "198")
        result = self.bot._close_position("BTCUSDT")
        self.assertTrue(result["success"])
        self.api.create_futures_order.assert_called_once()
        self.assertFalse(self.bot.protective_orders)

    def test_ambiguous_stop_cancel_blocks_market_close(self):
        self.open_position()
        self.api.cancel_futures_stop.side_effect = None
        self.api.cancel_futures_stop.return_value = {"success": False, "unknown": True}
        self.assertTrue(self.bot._close_position("BTCUSDT")["pending"])
        self.api.create_futures_order.assert_called_once()
        self.assertFalse(self.bot._protection_ready())

    def test_stop_fill_while_entry_pending_does_not_erase_entry(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        key = next(iter(self.stops))
        self.stops[key].update(algoStatus="FINISHED", actualOrderId="456")
        self.api.query_order.return_value = fill("FILLED", "1", "99")
        self.bot._reconcile_protections()
        self.assertTrue(self.bot.pending_orders["BTCUSDT"]["cancel_requested"])
        self.assertFalse(self.bot.active_positions)
        self.api.cancel_by_client_id.return_value = fill("CANCELED", "2", "200")
        self.bot._reconcile_orders()
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["quantity"], 1)
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["exit_requested"], "EXCHANGE_STOP_LOSS")

    def test_stop_api_timeout_queries_same_algo_id(self):
        client = BinanceDemoClient("key", "secret")
        filters = dict(stepSize="0.001", minQty="0.001", maxQty="100", tickSize="0.1")
        with patch.object(client, "sync_time", return_value=True), \
                patch.object(client, "load_futures_symbol_filters", return_value=filters), \
                patch.object(client, "_request", side_effect=requests.Timeout) as request:
            result = client.create_futures_stop("BTCUSDT", "SELL", 2, 99.81, "stop-id")
        self.assertTrue(result["unknown"])
        self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "GET"])
        body = request.call_args_list[0].kwargs["data"]
        self.assertIn("triggerPrice=99.9", body)
        self.assertIn("reduceOnly=true", body)
        self.assertIn("workingType=MARK_PRICE", body)
        self.assertIn("clientAlgoId=stop-id", request.call_args_list[1].args[1])

    def test_cancel_ack_with_stale_new_status_waits_for_confirmation(self):
        self.open_position()
        self.api.cancel_futures_stop.side_effect = lambda key: {"success": True, "data": self.stops[key].copy()}
        self.assertTrue(self.bot._close_position("BTCUSDT")["pending"])
        self.api.create_futures_order.assert_called_once()
        self.assertTrue(next(iter(self.bot.protective_orders.values()))["cancel_requested"])

    def test_short_stop_buys_to_reduce_position(self):
        self.api.create_futures_order.return_value = fill()
        self.bot._execute_entry("BTCUSDT", "SHORT", 100, "test")
        args = self.api.create_futures_stop.call_args.args
        self.assertEqual(args[:3], ("BTCUSDT", "BUY", 2))
        self.assertGreater(args[3], 100)
        self.assertEqual(self.bot.get_status()["active_position"]["stop_protection"], "exchange")

    def test_spot_entry_does_not_create_futures_protection(self):
        self.bot.market_type = "SPOT"
        self.api.get_account_info.return_value = {"success": True, "balances": [{"asset": "USDT", "free": 1000}]}
        self.api.create_order.return_value = fill(qty="0.2", quote="20")
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test")
        self.api.create_futures_stop.assert_not_called()
        self.assertEqual(self.bot.get_status()["active_position"]["stop_protection"], "local")

    def test_unchanged_stop_query_does_not_rewrite_state(self):
        self.open_position()
        with patch.object(self.bot, "save_state", wraps=self.bot.save_state) as save:
            self.bot._reconcile_protections()
        save.assert_not_called()

    def test_weakened_exchange_stop_is_rejected(self):
        self.open_position()
        self.stops[next(iter(self.stops))]["triggerPrice"] = "90"
        self.bot._reconcile_protections()
        self.assertFalse(self.bot.is_running)
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["exit_requested"], "PROTECTION_FAILED")

    def test_repeated_partial_response_is_not_counted_twice(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        self.api.query_order.return_value = fill("PARTIALLY_FILLED", "1", "100")
        self.bot._reconcile_orders()
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["quantity"], 1)

    def test_partial_close_updates_remaining_before_terminal(self):
        self.open_position()
        self.api.get_futures_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": 2}]
        self.api.create_futures_order.return_value = fill("PARTIALLY_FILLED", "1", "105")
        self.assertTrue(self.bot._close_position("BTCUSDT")["pending"])
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["quantity"], 1)
        self.assertEqual(self.bot.total_pnl_usdt, 5)
        self.assertEqual(self.bot.win_count, 0)
        self.api.query_order.return_value = fill("EXPIRED", "1", "105")
        self.bot._reconcile_orders()
        self.assertEqual(self.bot.active_positions["BTCUSDT"]["quantity"], 1)
        self.assertEqual(self.bot.total_pnl_usdt, 5)

    def test_unknown_order_blocks_duplicate_entry(self):
        self.open_position({"success": False, "unknown": True})
        self.bot._execute_entry("ETHUSDT", "LONG", 100, "test")
        self.api.create_futures_order.assert_called_once()
        self.assertTrue(self.bot.pending_orders)

    def test_failed_leverage_does_not_place_order(self):
        self.api.set_leverage.return_value = {"success": False, "error": "rejected"}
        self.open_position()
        self.api.create_futures_order.assert_not_called()

    def test_small_budget_never_forced_to_55(self):
        self.bot.trade_amount_usdt = 5
        self.bot.leverage = 2
        self.api.set_leverage.return_value = {"success": True, "leverage": 2}
        self.open_position(fill(qty="0.1", quote="10"))
        self.assertEqual(self.api.create_futures_order.call_args.kwargs["quantity"], 0.1)

    def test_exchange_minimum_rejected_before_leverage_change(self):
        self.api.prepare_entry.side_effect = ValueError("Below minimum")
        with self.assertRaises(ValueError):
            self.open_position()
        self.api.set_leverage.assert_not_called()
        self.api.create_futures_order.assert_not_called()

    def test_total_margin_cap_blocks_new_entry(self):
        self.bot.max_total_margin_usdt = 10
        with self.assertRaises(ValueError):
            self.open_position()
        self.api.create_futures_order.assert_not_called()

    def test_total_risk_cap_blocks_new_entry(self):
        self.bot.max_open_risk_usdt = 0.1
        with self.assertRaises(ValueError):
            self.open_position()
        self.api.create_futures_order.assert_not_called()

    def test_stop_keeps_monitoring(self):
        self.open_position()
        self.bot.stop()
        self.assertFalse(self.bot.is_running)
        self.assertFalse(self.bot.stop_event.is_set())
        self.api.get_ticker_price.return_value = {"price": "100"}
        self.bot._monitor_position("BTCUSDT")
        self.api.get_ticker_price.assert_called_once()

    def test_pause_during_leverage_request_prevents_submission(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            release.wait(2)
            return {"success": True, "leverage": 10}
        self.api.set_leverage.side_effect = delayed
        worker = threading.Thread(target=self.open_position)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.bot.stop()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.api.create_futures_order.assert_not_called()

    def test_close_all_pauses_and_cancels_partial_entry(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        self.api.cancel_by_client_id.return_value = fill("CANCELED", "1", "100")
        self.api.get_futures_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": 1}]
        self.api.create_futures_order.return_value = fill("FILLED", "1", "100")
        result = self.bot.close_all_positions()
        self.assertTrue(result["success"])
        self.assertFalse(self.bot.is_running)
        self.assertFalse(self.bot.active_positions)
        self.api.cancel_by_client_id.assert_called_once()

    def test_stop_loss_tracks_partial_position_and_requests_cancellation(self):
        self.open_position(fill("PARTIALLY_FILLED", "1", "100"))
        self.api.get_ticker_price.return_value = {"price": "90"}
        self.bot._monitor_position("BTCUSDT")
        self.assertTrue(self.bot.pending_orders["BTCUSDT"]["cancel_requested"])

    def test_manual_action_blocked_on_bot_symbol(self):
        self.open_position()
        with self.assertRaises(ValueError):
            with self.bot.manual_action("FUTURES", "BTCUSDT"):
                self.fail("Must not reach exchange")

    def test_external_increase_allows_reduce_only_bot_quantity(self):
        self.open_position()
        self.api.get_futures_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": 3}]
        self.api.create_futures_order.return_value = fill("FILLED", "2", "200")
        self.assertTrue(self.bot._close_position("BTCUSDT")["success"])
        self.assertEqual(self.api.create_futures_order.call_args.kwargs["quantity"], 2)
        self.assertTrue(self.api.create_futures_order.call_args.kwargs["reduce_only"])

    def test_cannot_change_market_with_position(self):
        self.open_position()
        self.bot.stop()
        with self.assertRaises(ValueError):
            self.bot.update_config(market_type="SPOT")

    def test_invalid_strategy_and_infinity_rejected(self):
        with self.assertRaises(ValueError):
            BotConfigRequest(strategy="INVALID")
        with self.assertRaises(ValueError):
            BotConfigRequest(trade_amount_usdt=float("inf"))
        with self.assertRaises(ValueError):
            self.bot.update_config(amount=-1)

    def test_stale_and_missing_candles_rejected(self):
        stale = candles()
        for candle in stale:
            candle["time"] -= 86400
            candle["close_time_ms"] -= 86400000
        with self.assertRaises(ValueError):
            validate_closed_candles(stale, "15m", int(time.time() * 1000))
        missing = candles()
        del missing[20]
        with self.assertRaises(ValueError):
            validate_closed_candles(missing, "15m", int(time.time() * 1000))

    def test_flat_and_insufficient_candles(self):
        data = candles(1e-6)
        self.assertEqual(analyze_market_signals(data[:34])["action"], "WAIT")
        signal = main.closed_candle_signals("PEPEUSDT", data)
        self.assertEqual(signal["action"], "NEUTRAL")
        self.assertEqual(signal["rsi"], 50)

    def test_timeout_queries_same_id_never_reposts(self):
        client = BinanceDemoClient("key", "secret")
        with patch.object(client, "sync_time", return_value=True), \
                patch.object(client, "format_futures_quantity", return_value="2"), \
                patch.object(client, "_request", side_effect=requests.Timeout) as request:
            result = client.create_futures_order("BTCUSDT", "BUY", quantity=2, client_order_id="test-id")
        self.assertTrue(result["unknown"])
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET"])
        self.assertIn("newClientOrderId=test-id", request.call_args_list[0].kwargs["data"])
        self.assertIn("origClientOrderId=test-id", request.call_args_list[1].args[1])

    def test_cache_is_shared_but_isolated_from_spot(self):
        client = BinanceDemoClient("key", "secret")
        response = SimpleNamespace(status_code=200, json=lambda: [
            [0, "100", "100", "100", "100", "1", int(time.time() * 1000) + 60000]])
        with patch.object(client, "_request", return_value=response) as request:
            client.get_klines("BTCUSDT", limit=61, market="FUTURES")
            client.get_klines("BTCUSDT", limit=100, market="FUTURES")
            client.get_klines("BTCUSDT", limit=100, market="SPOT")
        self.assertEqual(request.call_count, 2)

    def test_entry_minimum_filter_uses_actual_rounded_notional(self):
        client = BinanceDemoClient("key", "secret")
        filters = dict(stepSize="0.001", minQty="0.001", maxQty="100", minNotional="50")
        with patch.object(client, "_load_filters", return_value=filters), \
                patch.object(client, "get_ticker_price", return_value={"price": "100"}):
            with self.assertRaises(ValueError):
                client.prepare_entry("FUTURES", "BTCUSDT", 5, 2, 100)
            quantity, price = client.prepare_entry("FUTURES", "BTCUSDT", 20, 10, 100)
            self.assertLessEqual(quantity * price, 200)

    def test_api_rejects_bad_symbol_and_invalid_configuration(self):
        with patch.dict(main.bot_manager.engines, {"FUTURES": self.bot}), patch.object(main, "binance_client", self.api):
            client = TestClient(main.app)
            response = client.post("/api/bot/config?market=FUTURES", json={"strategy": "INVALID"})
            self.assertEqual(response.status_code, 422)
            self.api.validate_symbol.side_effect = ValueError("Mã không tồn tại")
            response = client.get("/api/market/klines?symbol=PEPEUSDT&market=FUTURES")
            self.assertEqual(response.status_code, 422)

    def test_api_valid_signals_use_market_and_closed_candles(self):
        with patch.dict(main.bot_manager.engines, {"FUTURES": self.bot}), patch.object(main, "binance_client", self.api):
            self.api.get_klines.return_value = candles()
            client = TestClient(main.app)
            response = client.get("/api/market/klines?symbol=BTCUSDT&market=FUTURES&include_signals=true")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["signals"]["action"], "NEUTRAL")
            self.assertEqual(self.api.get_klines.call_args.kwargs["market"], "FUTURES")

    def test_single_writer_lock_rejects_second_engine(self):
        second = bot_engine.TradingBotEngine()
        self.bot.acquire_process_lock()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire_process_lock()
        finally:
            self.bot.release_process_lock()
        second.acquire_process_lock()
        second.release_process_lock()

    def test_candle_signal_id_persists_across_restart(self):
        self.api.get_klines.return_value = candles()
        self.bot._scan_symbol_for_entry("BTCUSDT")
        restored = bot_engine.TradingBotEngine()
        self.assertTrue(restored.last_entry_candle)
        self.assertEqual(restored.last_entry_candle, self.bot.last_entry_candle)

    def test_risk_monitor_runs_even_when_account_reconciliation_fails(self):
        self.open_position()
        self.api.get_futures_positions.side_effect = RuntimeError("temporarily unavailable")
        self.api.get_ticker_price.return_value = {"price": "100"}
        self.bot._bot_tick()
        self.assertFalse(self.bot.entries_enabled)
        self.api.get_ticker_price.assert_called_once()

    def test_invalid_saved_config_blocks_start(self):
        self.bot.save_state()
        path = Path(bot_engine.STATE_FILE)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["leverage"] = -1
        path.write_text(json.dumps(data), encoding="utf-8")
        restored = bot_engine.TradingBotEngine()
        self.assertTrue(restored.state_error)
        self.assertFalse(restored.start()["success"])

    def test_rate_limit_prevents_immediate_followup(self):
        client = BinanceDemoClient("key", "secret")
        session = Mock()
        session.request.return_value = SimpleNamespace(status_code=429, headers={"Retry-After": "10"})
        client.http_local.session = session
        client._request("GET", "https://demo-api.binance.com/api/v3/time")
        with self.assertRaises(RuntimeError):
            client._request("GET", "https://demo-api.binance.com/api/v3/time")
        session.request.assert_called_once()

    def test_persistence_failure_prevents_sending(self):
        with patch.object(self.bot, "save_state", return_value=False):
            self.open_position()
        self.api.create_futures_order.assert_not_called()
        self.assertTrue(self.bot.state_error)


if __name__ == "__main__":
    unittest.main()
