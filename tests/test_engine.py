import os
import tempfile
import time
import unittest

from engine import BotConfig, TradingEngine
from journal import Journal
from risk import RiskConfig
from strategy import Signal
from tests.helpers import HOUR, FakeExchange, candles_from_closes

SYM = "ETHUSDT"
LONG = Signal("LONG", 100.0, 98.0, 1.0, "UP", "test")
QTY = 3.504        # 0.75% of 1000 over a 2% stop + 0.14% costs = 350.47 USDT, floored to the lot size


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exchange = FakeExchange()
        self.exchange.price[SYM] = 100.0
        self.engine = self.new_engine()
        self.assertTrue(self.engine.start()["success"])

    def new_engine(self):
        journal = Journal(os.path.join(self.tmp.name, "trading.db"))
        self.addCleanup(journal.close)
        engine = TradingEngine(self.exchange, journal)
        engine.supervise = lambda: None          # tests drive _tick() by hand
        return engine

    def enter(self):
        self.engine._execute_entry(SYM, LONG)
        return self.engine.positions[SYM]

    def trades(self):
        return self.engine.journal.trades()

    # ---------------- entry ----------------

    def test_entry_is_sized_by_risk_and_protected_on_the_exchange(self):
        pos = self.enter()
        self.assertEqual(pos["quantity"], QTY)
        self.assertAlmostEqual(pos["risk_usdt"], 7.5, delta=0.02)
        (stop,) = self.exchange.live_stops(SYM)
        self.assertEqual((stop["side"], float(stop["quantity"]), float(stop["triggerPrice"])), ("SELL", QTY, 98.0))
        self.assertTrue(self.engine._protection_ready())
        status = self.engine.get_status()
        self.assertTrue(status["positions"][0]["stop_on_exchange"])
        self.assertAlmostEqual(status["risk"]["open_risk_pct"], QTY * 2 / 1000 * 100, places=2)

    def test_order_is_journalled_before_it_is_sent(self):
        seen = []
        self.exchange.on_order = lambda: seen.append(self.engine.journal.load_state()["pending_orders"])
        self.enter()
        self.assertEqual(seen[0][SYM]["kind"], "entry")
        self.assertEqual(self.engine.pending_orders, {})

    def test_unknown_order_result_is_looked_up_not_resent(self):
        self.exchange.order_result = {"success": False, "unknown": True, "client_order_id": "?"}
        self.engine._execute_entry(SYM, LONG)
        self.assertNotIn(SYM, self.engine.positions)
        self.assertIn(SYM, self.engine.pending_orders)
        self.engine._execute_entry(SYM, LONG)            # a second signal must not stack another order
        self.engine._tick()
        self.assertEqual(self.engine.positions[SYM]["quantity"], QTY)
        self.assertEqual(len(self.exchange.sent), 1)
        self.assertEqual(len(self.exchange.live_stops(SYM)), 1)

    def test_risk_model_can_veto_a_signal(self):
        self.exchange.equity = 975.0                     # -2.5% today: past the 2% daily limit
        self.engine._execute_entry(SYM, LONG)
        self.assertEqual(self.exchange.sent, [])
        self.assertIsNotNone(self.engine.get_status()["risk"]["block_reason"])
        self.assertTrue(any(e["level"] == "SKIP" for e in self.engine.journal.events()))

    def test_entry_skipped_when_price_ran_to_the_stop(self):
        self.exchange.price[SYM] = 98.6
        self.engine._execute_entry(SYM, LONG)
        self.assertEqual(self.exchange.sent, [])

    # ---------------- exits ----------------

    def test_stop_hit_closes_the_position_and_records_about_minus_one_r(self):
        self.enter()
        self.exchange.price[SYM] = 97.9
        self.engine._tick()
        self.assertEqual(self.engine.positions, {})
        self.assertEqual(self.exchange.position[SYM], 0)
        self.assertEqual(self.exchange.live_stops(SYM), [])
        (trade,) = self.trades()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["r_multiple"], -1.0, delta=0.08)
        self.assertEqual(self.engine.risk_state["loss_streak"], 1)

    def test_stop_fired_on_the_exchange_is_reconciled(self):
        self.enter()
        (stop,) = self.exchange.live_stops(SYM)
        self.exchange.trigger_stop(stop["clientAlgoId"], 97.95)
        self.engine._tick()
        self.assertEqual(self.engine.positions, {})
        self.assertEqual(self.engine.protective_orders, {})
        (trade,) = self.trades()
        self.assertEqual((trade["reason"], trade["exit_price"]), ("STOP_LOSS", 97.95))
        self.assertEqual(len(self.exchange.sent), 1)     # the bot sent no close order of its own

    def test_partial_exit_at_2r_resizes_the_exchange_stop(self):
        pos = self.enter()
        self.exchange.price[SYM] = 104.0
        self.engine._tick()
        self.assertEqual(pos["quantity"], QTY / 2)
        self.assertTrue(pos["plan"]["partial_done"])
        self.engine._tick()                              # old stop cancelled, new one for what is left
        (stop,) = self.exchange.live_stops(SYM)
        self.assertEqual(float(stop["quantity"]), QTY / 2)
        self.engine._tick()
        self.assertEqual(len(self.exchange.sent), 2)     # entry + exactly one partial close
        self.assertTrue(self.engine._protection_ready())
        self.assertEqual(self.trades(), [])              # a trade is recorded only when fully closed

    def closed_candles_after_entry(self, last_close, last_high):
        start = int(time.time()) - 19 * HOUR - HOUR // 2     # the last candle closes after the entry
        candles = candles_from_closes([100.0] * 19 + [last_close], start=start)
        candles[-1]["high"] = last_high
        return candles

    def test_closed_candles_trail_the_stop_on_the_exchange_too(self):
        pos = self.enter()
        self.exchange.klines[(SYM, "1h")] = self.closed_candles_after_entry(102.5, 103.0)
        self.exchange.price[SYM] = 102.5
        self.engine._tick()
        new_stop = pos["plan"]["stop"]
        self.assertGreater(new_stop, 100.0)              # past breakeven after +1.5R
        self.assertEqual(pos["plan"]["stop_kind"], "TRAIL")
        self.engine._tick()
        (stop,) = self.exchange.live_stops(SYM)
        self.assertEqual(float(stop["triggerPrice"]), new_stop)
        self.assertEqual(self.engine.get_status()["positions"][0]["open_risk_usdt"], 0)

        self.exchange.price[SYM] = new_stop - 0.05
        self.engine._tick()
        (trade,) = self.trades()
        self.assertEqual(trade["reason"], "TRAILING_STOP")
        self.assertGreater(trade["net_pnl"], 0)
        self.assertEqual(trade["bars"], 1)

    def test_manual_close_and_close_all(self):
        self.enter()
        self.assertTrue(self.engine.close_position(SYM)["success"])
        self.assertEqual(self.trades()[0]["reason"], "MANUAL_CLOSE")
        with self.assertRaises(ValueError):
            self.engine.close_position(SYM)
        self.enter()
        result = self.engine.close_all_positions()
        self.assertTrue(result["success"])
        self.assertFalse(self.engine.is_running)
        self.assertEqual(self.exchange.position[SYM], 0)

    # ---------------- safety ----------------

    def test_position_the_bot_does_not_know_blocks_start_until_closed(self):
        self.engine.stop()
        self.exchange.position["LINKUSDT"] = 5.0
        self.exchange.price["LINKUSDT"] = 10.0
        result = self.engine.start()
        self.assertFalse(result["success"])
        self.assertIn("LINKUSDT", result["message"])
        self.assertIn("LINKUSDT", self.engine.get_status()["position_mismatches"])
        self.assertTrue(self.engine.close_untracked_positions(["LINKUSDT"])["success"])
        self.assertTrue(self.engine.start()["success"])

    def test_drawdown_halt_stops_the_bot_until_acknowledged(self):
        self.exchange.equity = 795.0                     # -20.5% from the 1000 peak
        self.engine._refresh_account(force=True)
        self.assertFalse(self.engine.is_running)
        self.assertIn("Drawdown", self.engine.stop_reason)
        self.assertFalse(self.engine.start()["success"])
        self.engine.acknowledge_drawdown()
        self.assertTrue(self.engine.start()["success"])
        self.assertEqual(self.engine.risk_state["peak_equity"], 795.0)

    def test_state_survives_a_restart(self):
        self.enter()
        restarted = self.new_engine()
        self.assertFalse(restarted.state_error)
        self.assertEqual(restarted.positions[SYM]["quantity"], QTY)
        self.assertEqual(list(restarted.protective_orders), list(self.engine.protective_orders))
        self.assertTrue(restarted.resume_pending)        # it was running: it resumes by itself
        restarted._maybe_resume()
        self.assertTrue(restarted.is_running)

    def test_config_changes_need_a_stopped_bot_and_no_leverage_change_with_positions(self):
        new = BotConfig(risk=RiskConfig(risk_pct=0.5))
        with self.assertRaises(ValueError):
            self.engine.update_config(new)
        self.enter()
        self.engine.stop()
        self.engine.update_config(new)
        self.assertEqual(self.new_engine().config.risk.risk_pct, 0.5)
        with self.assertRaises(ValueError):
            self.engine.update_config(BotConfig(risk=RiskConfig(leverage=3)))

    def test_stopped_bot_still_manages_open_positions(self):
        self.enter()
        self.engine.stop()
        self.exchange.price[SYM] = 97.9
        self.engine._tick()
        self.assertEqual(self.engine.positions, {})
        self.engine._execute_entry(SYM, LONG)            # but opens nothing new
        self.assertEqual(len(self.exchange.sent), 2)


if __name__ == "__main__":
    unittest.main()
