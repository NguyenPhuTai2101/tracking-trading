import os
import tempfile
import time
import unittest
from unittest.mock import patch

import engine as engine_module
import strategy
from engine import BotConfig, TradingEngine
from journal import Journal
from risk import RiskConfig
from strategy import Signal, StrategyParams
from tests.helpers import HOUR, FakeExchange, candles_from_closes, recent_candles

SYM = "ETHUSDT"
LONG = Signal("LONG", 100.0, 98.0, 1.0, "UP", "test", "TREND_PULLBACK", strategy.trend_exit(StrategyParams()))
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

    # ---------------- settling against the exchange ----------------

    def settle(self):
        self.engine.next_settle_at = 0.0
        with patch.object(engine_module, "SETTLE_DELAY_MS", 0):
            self.engine._settle_trades()
        return self.trades()[0]

    def stopped_out_trade(self):
        self.enter()
        self.exchange.price[SYM] = 97.9
        self.engine._tick()
        (trade,) = self.trades()
        return trade

    def test_closed_trade_starts_estimated_then_takes_real_fees_and_funding(self):
        estimate = self.stopped_out_trade()
        self.assertEqual(estimate["settled"], 0)
        self.assertAlmostEqual(estimate["fees"], QTY * (100 + 97.9) * 0.0005, places=6)     # assumed 0.05% a side
        self.assertEqual(estimate["signal_price"], 100.0)
        self.exchange.funding = [
            {"symbol": SYM, "income": "-0.21", "time": estimate["entry_time"] + 1},
            {"symbol": SYM, "income": "-9.99", "time": estimate["entry_time"] - 60_000},      # before the trade
            {"symbol": "SOLUSDT", "income": "-5", "time": estimate["entry_time"] + 1}]        # another symbol
        trade = self.settle()
        self.assertEqual(trade["settled"], 1)
        self.assertAlmostEqual(trade["fees"], QTY * (100 + 97.9) * 0.0004, places=6)        # what the exchange charged
        self.assertAlmostEqual(trade["funding"], -0.21)
        self.assertAlmostEqual(trade["gross_pnl"], -2.1 * QTY, places=6)
        self.assertAlmostEqual(trade["net_pnl"], trade["gross_pnl"] - trade["fees"] - 0.21, places=6)
        self.assertAlmostEqual(trade["r_multiple"], trade["net_pnl"] / trade["risk_usdt"], places=3)
        self.assertNotEqual(trade["net_pnl"], estimate["net_pnl"])
        self.assertEqual(self.engine.journal.unsettled_trades(0), [])

    def test_trade_keeps_its_estimate_until_the_exchange_has_every_fill(self):
        estimate = self.stopped_out_trade()
        self.exchange.history_ready = False                         # lookup fails
        self.assertEqual(self.settle()["settled"], 0)
        self.exchange.history_ready = True
        closing = self.exchange.fills.pop(2)                        # the closing fill is not booked yet
        self.assertEqual(self.settle()["net_pnl"], estimate["net_pnl"])
        self.exchange.fills[2] = closing
        self.assertEqual(self.settle()["settled"], 1)

    def test_fills_of_partial_exit_and_exchange_stop_are_all_counted(self):
        self.enter()
        self.exchange.price[SYM] = 104.0
        self.engine._tick()                                         # half off at 2R
        self.engine._tick()                                         # stop resized
        (stop,) = self.exchange.live_stops(SYM)
        self.exchange.trigger_stop(stop["clientAlgoId"], 100.5)     # the rest leaves via the exchange stop
        self.engine._tick()
        trade = self.settle()
        self.assertEqual(trade["settled"], 1)
        self.assertEqual(sorted(strategy_ids(trade)), [1, 2, 777])
        self.assertAlmostEqual(trade["gross_pnl"], QTY / 2 * 4.0 + QTY / 2 * 0.5, places=6)
        self.assertAlmostEqual(trade["fees"], (QTY * 100 + QTY / 2 * 104.0 + QTY / 2 * 100.5) * 0.0004, places=6)

    def test_commission_paid_in_another_asset_keeps_the_fee_estimate(self):
        self.exchange.fee_asset = "BNB"
        estimate = self.stopped_out_trade()
        trade = self.settle()
        self.assertEqual(trade["settled"], 2)
        self.assertEqual(trade["fees"], estimate["fees"])
        self.assertAlmostEqual(trade["gross_pnl"], -2.1 * QTY, places=6)

    # ---------------- cases ----------------

    def scan_with(self, firing):
        """Run one scan of SYM with live-looking candles; `firing` maps setup -> Signal."""
        for interval, step in (("1h", HOUR), ("4h", 4 * HOUR), ("1d", 24 * HOUR)):
            for sym in (SYM, "BTCUSDT"):
                self.exchange.klines[(sym, interval)] = recent_candles(120, step)

        def fake(setup, candles, ind, i, regime, btc_regime, params):
            return firing.get(setup), f"note {setup}"

        with patch.object(strategy, "evaluate", fake):
            self.engine._scan_symbol(SYM)
        return {case["setup"]: case for case in self.engine.get_status()["scan_board"][0]["cases"]}

    def test_only_enabled_setups_trade_the_rest_are_observed(self):
        fade = Signal("SHORT", 100.0, 101.5, 1.0, "RANGE", "sweep", "FAILED_BREAKOUT", strategy.target_exit(97.0))
        cases = self.scan_with({"FAILED_BREAKOUT": fade})
        self.assertEqual(self.exchange.sent, [])                         # recognised, not traded
        self.assertEqual(set(cases), set(strategy.SETUPS))               # every case was looked for
        self.assertEqual((cases["FAILED_BREAKOUT"]["signal"], cases["FAILED_BREAKOUT"]["traded"]), ("SHORT", False))
        self.assertIn("chỉ quan sát", cases["FAILED_BREAKOUT"]["note"])
        self.assertTrue(cases["THRUST_BREAKOUT"]["traded"])
        self.assertTrue(any(e["level"] == "CASE" for e in self.engine.journal.events()))

    def test_enabled_4h_setup_opens_a_position_managed_on_4h_candles(self):
        thrust = Signal("LONG", 100.0, 98.0, 1.0, "UP", "thrust", "THRUST_BREAKOUT", strategy.trail_exit(4.0))
        cases = self.scan_with({"THRUST_BREAKOUT": thrust})
        self.assertEqual(cases["THRUST_BREAKOUT"]["signal"], "LONG")
        pos = self.engine.positions[SYM]
        self.assertEqual((pos["setup"], pos["interval"], pos["quantity"]), ("THRUST_BREAKOUT", "4h", QTY))
        self.assertTrue(pos["plan"]["partial_done"])                     # trail-only exit: no partial at 2R
        self.exchange.price[SYM] = 104.5
        self.engine._tick()
        self.assertEqual(len(self.exchange.sent), 1)                     # still just the entry
        self.scan_with({"THRUST_BREAKOUT": thrust})                      # the same candle is never traded twice
        self.assertEqual(len(self.exchange.sent), 1)

    def test_target_setup_takes_profit_at_the_target(self):
        self.engine.stop()
        self.engine.update_config(BotConfig(strategy=StrategyParams(setups=["RANGE_REVERSION"])))
        self.assertTrue(self.engine.start()["success"])
        fade = Signal("LONG", 100.0, 98.0, 1.0, "RANGE", "revert", "RANGE_REVERSION", strategy.target_exit(102.5))
        self.scan_with({"RANGE_REVERSION": fade})
        self.assertIn(SYM, self.engine.positions)
        self.exchange.price[SYM] = 102.6
        self.engine._tick()
        (trade,) = self.trades()
        self.assertEqual((trade["reason"], trade["setup"]), ("TAKE_PROFIT", "RANGE_REVERSION"))
        self.assertGreater(trade["r_multiple"], 1.0)

    def test_stopped_bot_still_manages_open_positions(self):
        self.enter()
        self.engine.stop()
        self.exchange.price[SYM] = 97.9
        self.engine._tick()
        self.assertEqual(self.engine.positions, {})
        self.engine._execute_entry(SYM, LONG)            # but opens nothing new
        self.assertEqual(len(self.exchange.sent), 2)


def strategy_ids(trade):
    import json
    return json.loads(trade["order_ids"])


if __name__ == "__main__":
    unittest.main()
