import unittest
from unittest.mock import patch

import backtest
import bot_engine
import test_trading_upgrade as regression
from strategy import breakeven_levels, plan_exits, stop_too_close_to_liquidation, trend_allows
from test_backtest_auth import flat, make_candles


class StrategyRefinementTests(unittest.TestCase):
    def test_atr_exits_ignore_leverage(self):
        self.assertEqual(plan_exits(100, "LONG", exit_mode="ATR", atr=2, atr_sl_mult=1.5, atr_tp_mult=3,
                                    leverage=20), (106, 97))
        self.assertEqual(plan_exits(100, "SHORT", exit_mode="ATR", atr=2, leverage=3), (94, 103))
        with self.assertRaises(ValueError):
            plan_exits(100, "LONG", exit_mode="ATR", atr=None)

    def test_liquidation_guard(self):
        self.assertTrue(stop_too_close_to_liquidation(100, 95, 20))     # 5% stop vs ~5% liquidation
        self.assertFalse(stop_too_close_to_liquidation(100, 97, 3))
        self.assertFalse(stop_too_close_to_liquidation(100, 50, 1))     # spot never liquidates

    def test_trend_filter_follows_ema200(self):
        rising = [100 + i * 0.1 for i in range(260)]
        self.assertTrue(trend_allows("LONG", rising))
        self.assertFalse(trend_allows("SHORT", rising))
        self.assertFalse(trend_allows("LONG", rising[:100]))           # not enough history

    def test_breakeven_levels(self):
        arm, stop = breakeven_levels(100, 98, "LONG", 1, "FUTURES")
        self.assertAlmostEqual(arm, 102)
        self.assertAlmostEqual(stop, 100.1)
        self.assertIsNone(breakeven_levels(100, 98, "LONG", 0, "FUTURES"))
        self.assertIsNone(breakeven_levels(100, 99.95, "LONG", 1, "FUTURES"))   # target inside fees


class EngineRunIntentTests(unittest.TestCase):
    def setUp(self):
        regression.TradingUpgradeTests.setUp(self)

    def test_running_bot_resumes_after_restart(self):
        self.bot.is_running = False
        self.assertTrue(self.bot.start()["success"])
        self.assertTrue(self.bot.auto_run)
        restored = bot_engine.TradingBotEngine()
        restored.supervise = lambda: None
        self.assertFalse(restored.is_running)
        self.assertTrue(restored.resume_pending)
        restored._maybe_resume()
        self.assertTrue(restored.is_running)
        self.assertFalse(restored.resume_pending)

    def test_user_stop_is_not_resumed_and_reason_is_kept(self):
        self.bot.stop()
        self.assertFalse(self.bot.auto_run)
        self.assertIn("bấm dừng", self.bot.stop_reason)
        restored = bot_engine.TradingBotEngine()
        self.assertFalse(restored.resume_pending)
        self.assertIn("bấm dừng", restored.get_status()["stop_reason"])

    def test_resume_gives_up_after_repeated_failures(self):
        self.bot.is_running = False
        self.bot.resume_pending = True
        self.bot.supervise = lambda: None
        self.api.has_credentials = False
        for _ in range(10):
            self.bot.next_resume_at = 0
            self.bot._maybe_resume()
        self.assertFalse(self.bot.resume_pending)
        self.assertIn("Không tự chạy lại", self.bot.stop_reason)

    def test_logs_survive_restart(self):
        self.bot.add_log("ERROR", "điều cần nhớ")
        self.bot.save_state()
        restored = bot_engine.TradingBotEngine()
        self.assertTrue(any("điều cần nhớ" in entry["message"] for entry in restored.logs))


class EngineEntryExitTests(unittest.TestCase):
    def setUp(self):
        regression.TradingUpgradeTests.setUp(self)
        self.bot.leverage = 3
        self.api.set_leverage.return_value = {"success": True, "leverage": 3}
        self.bot.max_open_risk_usdt = 100

    def test_atr_mode_sets_exchange_stop_from_atr(self):
        self.bot.exit_mode = "ATR"
        self.api.create_futures_order.return_value = regression.fill(qty="0.6", quote="60")
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test", atr=2)
        pos = self.bot.active_positions["BTCUSDT"]
        self.assertAlmostEqual(pos["target_sl_price"], 97)
        self.assertAlmostEqual(pos["target_tp_price"], 106)
        stop = next(iter(self.stops.values()))
        self.assertAlmostEqual(float(stop["triggerPrice"]), 97)

    def test_atr_stop_beyond_liquidation_skips_entry(self):
        self.bot.exit_mode = "ATR"
        self.bot.leverage = 20
        self.api.set_leverage.return_value = {"success": True, "leverage": 20}
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test", atr=4)    # 6% stop at 20x
        self.api.create_futures_order.assert_not_called()

    def test_breakeven_stop_closes_at_entry_after_one_r(self):
        self.bot.exit_mode = "ATR"
        self.bot.breakeven_at_r = 1
        self.api.create_futures_order.return_value = regression.fill(qty="0.6", quote="60")
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test", atr=2)
        self.api.get_ticker_price.return_value = {"price": "103.5"}     # +1R reached (risk = 3)
        self.bot._monitor_position("BTCUSDT")
        pos = self.bot.active_positions["BTCUSDT"]
        self.assertTrue(pos["breakeven_active"])
        self.api.get_futures_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": 0.6}]
        self.api.create_futures_order.return_value = regression.fill(qty="0.6", quote="60.03")
        self.api.get_ticker_price.return_value = {"price": "100.05"}    # back under entry + fees
        self.bot._monitor_position("BTCUSDT")
        self.assertNotIn("BTCUSDT", self.bot.active_positions)
        self.assertEqual(self.bot.closed_trades[-1]["reason"], "BREAKEVEN_STOP")
        self.assertIn("BTCUSDT", self.bot.last_exit_ms)

    def test_cooldown_skips_signal_right_after_exit(self):
        self.bot.cooldown_candles = 2
        candles = regression.candles()
        self.api.get_klines.return_value = candles
        self.bot.last_exit_ms["BTCUSDT"] = candles[-1]["time"] * 1000 + 1
        with patch.object(bot_engine, "analyze_market_signals") as analyze:
            self.bot._scan_symbol_for_entry("BTCUSDT")
            analyze.assert_not_called()
        self.bot.last_exit_ms["BTCUSDT"] = candles[-1]["time"] * 1000 - 3 * 900_000
        self.bot.last_entry_candle.clear()
        with patch.object(bot_engine, "analyze_market_signals", return_value={"action": "NEUTRAL", "net_score": 0,
                                                                               "rsi": 50, "ema_cross": "NONE",
                                                                               "ema9": 1, "ema21": 1}) as analyze:
            self.bot._scan_symbol_for_entry("BTCUSDT")
            analyze.assert_called_once()

    def test_trend_filter_blocks_counter_trend_signal(self):
        self.bot.trend_filter = True
        falling = regression.candles()
        self.api.get_klines.return_value = falling
        signal = {"action": "STRONG_BUY", "net_score": 60, "rsi": 25, "ema_cross": "NONE", "ema9": 1, "ema21": 1, "atr": 1}
        with patch.object(bot_engine, "analyze_market_signals", return_value=signal), \
                patch.object(bot_engine, "trend_allows", return_value=False) as trend:
            self.bot._scan_symbol_for_entry("BTCUSDT")
            trend.assert_called_once()
        self.api.create_futures_order.assert_not_called()
        self.api.get_klines.assert_called_with("BTCUSDT", interval="15m", limit=261, market="FUTURES")


class BacktestRefinementTests(unittest.TestCase):
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
        params = dict(symbols=["BTCUSDT"], leverage=3, trade_amount_usdt=20, fee_rate=0.0005, slippage_bps=0)
        params.update(overrides)
        return backtest.run_backtest(backtest.BacktestConfig(**params), {"BTCUSDT": candles})

    def test_atr_take_profit(self):
        w = backtest.WINDOW
        prices = flat(w) + [(100, 100, 100, 100), (100, 106.5, 99.5, 106)] + flat(2, 106)
        result = self.run_bt(prices, {w - 1: ("LONG", "t")}, exit_mode="ATR")
        self.assertEqual(result.trades[0]["exit_reason"], "TAKE_PROFIT")
        self.assertAlmostEqual(result.trades[0]["exit_price"], 106)

    def test_breakeven_stop_after_one_r(self):
        w = backtest.WINDOW
        # Entry 100, stop 97, arm at 103; next candle falls back through 100.1.
        prices = flat(w) + [(100, 103.2, 99.9, 103), (103, 103, 99, 99.5)] + flat(2, 99.5)
        result = self.run_bt(prices, {w - 1: ("LONG", "t")}, exit_mode="ATR", breakeven_at_r=1)
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "BREAKEVEN_STOP")
        self.assertAlmostEqual(trade["exit_price"], 100.1)

    def test_liquidation_guard_skips_entry(self):
        w = backtest.WINDOW
        result = self.run_bt(flat(w + 3), {w - 1: ("LONG", "t")}, exit_mode="ATR", leverage=20, atr_sl_mult=3)
        self.assertEqual(result.summary["trades"], 0)
        self.assertEqual(result.summary["liquidation_skipped_entries"], 1)

    def test_trend_filter_needs_history_and_blocks_counter_trend(self):
        hist = backtest.TREND_HISTORY
        falling = [(200 - i * 0.1,) * 4 for i in range(hist + 5)]
        result = self.run_bt(falling, {hist + 1: ("LONG", "t")}, trend_filter=True)
        self.assertEqual(result.summary["trades"], 0)
        self.assertEqual(result.summary["trend_filtered_signals"], 1)


if __name__ == "__main__":
    unittest.main()
