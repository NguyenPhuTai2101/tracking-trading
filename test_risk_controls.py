import unittest
from unittest.mock import patch

from pydantic import ValidationError

import backtest
import bot_engine
import main
import test_trading_upgrade as regression
from strategy import entry_block_reason, risk_based_margin
from test_backtest_auth import flat, make_candles
from trading_rules import BotConfigRequest


class RiskRuleTests(unittest.TestCase):
    def test_margin_follows_stop_distance_and_includes_fees(self):
        # 3% stop + 0.1% round-trip fees: 1 USDT of risk buys 32.26 USDT of notional.
        self.assertAlmostEqual(risk_based_margin(1, 100, 97, 3, 20), 1 / 0.031 / 3)
        # Twice the stop distance gives roughly half the position.
        self.assertLess(risk_based_margin(1, 100, 94, 3, 20), risk_based_margin(1, 100, 97, 3, 20) * 0.55)
        self.assertAlmostEqual(risk_based_margin(1, 100, 103, 3, 20), 1 / 0.031 / 3)   # SHORT mirrors

    def test_margin_never_exceeds_budget_and_rejects_bad_input(self):
        self.assertEqual(risk_based_margin(100, 100, 99.9, 1, 20, "SPOT"), 20)
        with self.assertRaises(ValueError):
            risk_based_margin(1, 100, 100, 3, 20)
        with self.assertRaises(ValueError):
            risk_based_margin(0, 100, 97, 3, 20)

    def test_entry_block_reason(self):
        self.assertIsNone(entry_block_reason(-5, 9, 0, 0))                 # both limits off
        self.assertIsNone(entry_block_reason(-0.99, 1, 1, 2))
        self.assertIn("lỗ", entry_block_reason(-1, 0, 1, 0))
        self.assertIn("liên tiếp", entry_block_reason(0.5, 2, 1, 2))

    def test_config_accepts_zero_and_rejects_negative(self):
        BotConfigRequest(risk_per_trade_usdt=0, max_daily_loss_usdt=0, max_consecutive_losses=0)
        for field in ("risk_per_trade_usdt", "max_daily_loss_usdt", "max_consecutive_losses"):
            with self.assertRaises(ValidationError):
                BotConfigRequest(**{field: -1})
        request = main.BacktestRequest(symbols=["BTCUSDT"], strategies=["RSI_SCALPING"], risk_per_trade_usdt=1)
        self.assertEqual(request.max_consecutive_losses, 0)


class EngineRiskTests(unittest.TestCase):
    def setUp(self):
        regression.TradingUpgradeTests.setUp(self)
        self.bot.max_open_risk_usdt = 100

    def trade(self, symbol, exit_quote):
        """Open 2 units at 100 (10x) and close them for `exit_quote`."""
        self.api.get_futures_positions.return_value = []
        self.api.create_futures_order.return_value = regression.fill()
        self.bot._execute_entry(symbol, "LONG", 100, "test")
        self.assertIn(symbol, self.bot.active_positions)
        self.api.get_futures_positions.return_value = [{"symbol": symbol, "positionAmt": 2}]
        self.api.create_futures_order.return_value = regression.fill(quote=str(exit_quote))
        self.assertTrue(self.bot._close_position(symbol, "STOP_LOSS")["success"])
        self.api.get_futures_positions.return_value = []

    def test_risk_sizing_shrinks_margin_for_a_wide_stop(self):
        self.bot.exit_mode = "ATR"
        self.bot.leverage = 3
        self.api.set_leverage.return_value = {"success": True, "leverage": 3}
        self.bot.risk_per_trade_usdt = 1
        self.api.create_futures_order.return_value = regression.fill(qty="0.3225", quote="32.25")
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test", atr=2)      # stop 3% away
        self.assertEqual(self.api.prepare_entry.call_args.args[2], 10.75)
        pos = self.bot.active_positions["BTCUSDT"]
        loss_at_stop = (pos["entry_price"] - pos["target_sl_price"]) * pos["quantity"]
        self.assertAlmostEqual(loss_at_stop, 0.9675, places=3)

    def test_risk_sizing_is_capped_by_trade_amount(self):
        self.bot.risk_per_trade_usdt = 50            # ROE stop is 0.15% away: uncapped margin would be huge
        self.api.create_futures_order.return_value = regression.fill()
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test")
        self.assertEqual(self.api.prepare_entry.call_args.args[2], 20)

    def test_daily_loss_limit_blocks_new_entries_until_next_day(self):
        self.bot.max_daily_loss_usdt = 1
        self.trade("BTCUSDT", 196)                   # -4 gross, about -4.2 with estimated fees
        self.assertAlmostEqual(self.bot.risk_day["pnl"], -4.196)
        self.assertIn("lỗ", self.bot.get_status()["entry_block_reason"])
        calls = self.api.create_futures_order.call_count
        self.bot._execute_entry("ETHUSDT", "LONG", 100, "test")
        self.assertEqual(self.api.create_futures_order.call_count, calls)
        self.assertTrue(self.bot.is_running)         # paused, not stopped: positions stay monitored

        self.bot.risk_day["date"] = "2000-01-01"     # a new day starts from zero
        self.assertIsNone(self.bot.get_status()["entry_block_reason"])
        self.api.create_futures_order.return_value = regression.fill()
        self.bot._execute_entry("ETHUSDT", "LONG", 100, "test")
        self.assertIn("ETHUSDT", self.bot.active_positions)

    def test_losing_streak_blocks_and_a_win_resets_it(self):
        self.bot.max_consecutive_losses = 2
        self.trade("BTCUSDT", 199)
        self.trade("BTCUSDT", 202)                   # win resets the streak
        self.assertEqual(self.bot.risk_day["loss_streak"], 0)
        self.trade("BTCUSDT", 199)
        self.trade("ETHUSDT", 199)
        self.assertEqual(self.bot.risk_day["loss_streak"], 2)
        calls = self.api.create_futures_order.call_count
        self.bot._execute_entry("SOLUSDT", "LONG", 100, "test")
        self.assertEqual(self.api.create_futures_order.call_count, calls)

    def test_scan_is_skipped_and_logged_once_while_blocked(self):
        self.bot.max_daily_loss_usdt = 1
        self.bot._risk_today()["pnl"] = -5
        self.bot._scan_symbol_for_entry("BTCUSDT")
        self.bot._scan_symbol_for_entry("ETHUSDT")
        self.api.get_klines.assert_not_called()
        self.assertEqual(sum("Nghỉ mở lệnh" in entry["message"] for entry in self.bot.logs), 1)

    def test_risk_per_trade_above_open_risk_cap_is_rejected(self):
        self.bot.is_running = False
        self.bot.max_open_risk_usdt = 0.9
        with self.assertRaises(ValueError):
            self.bot.update_config(risk_per_trade_usdt=1)
        self.bot.update_config(risk_per_trade_usdt=1, max_open_risk_usdt=3)
        self.assertEqual(self.bot.risk_per_trade_usdt, 1)

    def test_limits_and_day_result_survive_restart(self):
        self.bot.is_running = False
        self.bot.update_config(risk_per_trade_usdt=1.5, max_daily_loss_usdt=4, max_consecutive_losses=3)
        self.bot._risk_today().update(pnl=-2.5, loss_streak=2)
        self.bot.save_state()
        restored = bot_engine.TradingBotEngine()
        self.assertEqual((restored.risk_per_trade_usdt, restored.max_daily_loss_usdt,
                          restored.max_consecutive_losses), (1.5, 4, 3))
        self.assertEqual(restored.get_status()["risk_day"]["pnl"], -2.5)
        self.assertEqual(restored.get_status()["risk_day"]["loss_streak"], 2)


class BacktestRiskTests(unittest.TestCase):
    def setUp(self):
        self.signal_at = {}
        patches = [patch.object(backtest, "analyze_market_signals",
                                lambda window: {"index": window[-1]["i"], "atr": 2.0}),
                   patch.object(backtest, "decide_entry",
                                lambda strategy, analysis, allow_short: self.signal_at.get(analysis["index"]))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_bt(self, prices, signals, **overrides):
        candles = make_candles(prices)
        for i, c in enumerate(candles):
            c["i"] = i
        self.signal_at = signals
        params = dict(symbols=["BTCUSDT"], leverage=3, trade_amount_usdt=20, fee_rate=0.0005, slippage_bps=0,
                      exit_mode="ATR")
        params.update(overrides)
        return backtest.run_backtest(backtest.BacktestConfig(**params), {"BTCUSDT": candles})

    def losing_prices(self, tail=6):
        # Entry at 100 on candle WINDOW, stop at 97 hit on the same candle.
        return flat(backtest.WINDOW) + [(100, 100, 96, 96.5)] + flat(tail, 96.5)

    def test_stop_out_loses_the_configured_risk(self):
        w = backtest.WINDOW
        result = self.run_bt(self.losing_prices(), {w - 1: ("LONG", "t")}, risk_per_trade_usdt=1)
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["quantity"], 1 / 0.031 / 100)
        self.assertAlmostEqual(trade["net_pnl"], -1, places=2)

    def test_trade_below_exchange_minimum_is_skipped(self):
        w = backtest.WINDOW
        result = self.run_bt(self.losing_prices(), {w - 1: ("LONG", "t")}, risk_per_trade_usdt=0.1)
        self.assertEqual(result.summary["trades"], 0)
        self.assertEqual(result.summary["min_notional_skipped_entries"], 1)

    def test_losing_streak_limit_stops_reentry_the_same_day(self):
        w = backtest.WINDOW
        signals = {i: ("LONG", "t") for i in range(w - 1, w + 4)}
        free = self.run_bt(self.losing_prices(), signals)
        guarded = self.run_bt(self.losing_prices(), signals, max_consecutive_losses=1)
        self.assertEqual(free.summary["trades"], 2)
        self.assertEqual(guarded.summary["trades"], 1)
        self.assertGreaterEqual(guarded.summary["daily_limit_skipped_entries"], 1)
        limited = self.run_bt(self.losing_prices(), signals, max_daily_loss_usdt=0.5)
        self.assertEqual(limited.summary["trades"], 1)

    def test_daily_limit_resets_on_the_next_utc_day(self):
        w = backtest.WINDOW
        prices = self.losing_prices(tail=100)        # 100 x 15m candles: crosses midnight UTC
        signals = {w - 1: ("LONG", "t"), w + 90: ("LONG", "t")}
        result = self.run_bt(prices, signals, max_consecutive_losses=1)
        self.assertEqual(result.summary["trades"], 2)

    def test_report_has_split_baseline_and_small_sample_warning(self):
        w = backtest.WINDOW
        result = self.run_bt(self.losing_prices(), {w - 1: ("LONG", "t")})
        s = result.summary
        self.assertEqual(s["buy_hold_return_pct"], -3.5)
        self.assertEqual(result.per_symbol["BTCUSDT"]["buy_hold_pct"], -3.5)
        self.assertEqual(s["first_part"]["trades"] + s["last_part"]["trades"], s["trades"])
        self.assertLess(s["period_start"], s["split_time"])
        self.assertTrue(any("mẫu quá nhỏ" in warning for warning in result.warnings))

    def test_unstable_result_is_flagged(self):
        w = backtest.WINDOW
        # Win early (TP 106), lose late (stop 97 from 100).
        prices = (flat(w) + [(100, 107, 99.5, 106)] + flat(20, 100)
                  + [(100, 100, 96, 96.5)] + flat(3, 96.5))
        result = self.run_bt(prices, {w - 1: ("LONG", "t"), w + 19: ("LONG", "t")})
        self.assertGreater(result.summary["first_part"]["net_pnl"], 0)
        self.assertLess(result.summary["last_part"]["net_pnl"], 0)
        self.assertTrue(any("không ổn định" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
