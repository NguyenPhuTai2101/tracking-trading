import unittest
from unittest.mock import patch

import backtest
import strategy
from risk import RiskConfig
from strategy import Signal, StrategyParams
from tests.helpers import HOUR, candles_from_closes

SYM = "ETHUSDT"
WARMUP = 20
NO_SLIPPAGE = RiskConfig(slippage_rate=0)
PULLBACK = StrategyParams(setups=["TREND_PULLBACK"])            # a 1h setup: the simulation ticks hourly


class BacktestTests(unittest.TestCase):
    """Signals are injected at chosen candle indices; the entry rules themselves are tested in test_strategy."""

    def setUp(self):
        # Wicks of 0.5 give an ATR of 1.0, so a 2-point stop is 2 ATR: inside the 3 ATR trail.
        self.candles = candles_from_closes([100.0] * 220, wick=0.5)
        self.signals = {}

        def fake_evaluate(setup, candles, ind, i, regime, btc_regime, params):
            return self.signals.get((setup, candles[i]["time"])), ""

        patcher = patch.object(strategy, "evaluate", fake_evaluate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def long(self, at, stop=98.0, exit=None):
        rules = exit or strategy.trend_exit(PULLBACK)
        self.signals[("TREND_PULLBACK", self.candles[at]["time"])] = Signal("LONG", 100.0, stop, 1.0, "UP", "test", exit=rules)

    def run_bt(self, risk=NO_SLIPPAGE, funding=None):
        data = {"start_ms": self.candles[WARMUP]["time"] * 1000, "end_ms": self.candles[-1]["close_time_ms"] + 1,
                "candles": {"1h": {SYM: self.candles}}, "funding": {SYM: funding or []}, "funding_ok": True}
        config = backtest.BacktestConfig([SYM], days=8, risk=risk, strategy=PULLBACK)
        return backtest.run_backtest(config, data)

    def test_stop_out_costs_one_r_and_fees_are_charged_both_sides(self):
        self.long(50)
        self.candles[52]["low"] = 97.0
        result = self.run_bt()
        (trade,) = result["trades"]
        quantity = 7.5 / (0.02 + 0.001) / 100             # 0.75% of 1000 over a 2% stop + 0.1% fees
        self.assertEqual(trade["entry_time"], self.candles[51]["time"] * 1000)     # next candle's open
        self.assertEqual((trade["entry_price"], trade["exit_price"], trade["reason"]), (100.0, 98.0, "STOP_LOSS"))
        self.assertEqual(trade["setup"], "TREND_PULLBACK")
        self.assertAlmostEqual(trade["quantity"], quantity)
        self.assertAlmostEqual(trade["fees"], quantity * (100 + 98) * 0.0005, places=5)
        self.assertAlmostEqual(trade["net_pnl"], -2 * quantity - trade["fees"], places=5)
        self.assertAlmostEqual(trade["r_multiple"], -1.0, delta=0.01)
        self.assertAlmostEqual(result["summary"]["final_equity"], round(1000 + trade["net_pnl"], 2))

    def test_slippage_makes_every_fill_worse(self):
        self.long(50)
        self.candles[52]["low"] = 97.0
        (trade,) = self.run_bt(risk=RiskConfig(slippage_rate=0.001))["trades"]
        self.assertAlmostEqual(trade["entry_price"], 100.1)
        self.assertAlmostEqual(trade["exit_price"], 98.0 * 0.999)
        # 1R already budgets for slippage, so the stop-out is still about -1R, not worse.
        self.assertAlmostEqual(trade["r_multiple"], -1.0, delta=0.06)

    def test_gap_through_stop_fills_at_the_open(self):
        self.long(50)
        self.candles[52].update(open=96.0, high=96.5, low=95.5, close=96.0)
        (trade,) = self.run_bt()["trades"]
        self.assertEqual(trade["exit_price"], 96.0)
        self.assertLess(trade["r_multiple"], -1.9)

    def test_winner_takes_partial_at_2r_then_trails(self):
        self.long(50)
        self.candles[52].update(high=104.5, close=104.0)                # +2R touched: half comes off at 104
        self.candles[53].update(open=104.0, high=104.2, low=99.0, close=99.5)
        result = self.run_bt()
        (trade,) = result["trades"]
        self.assertEqual(trade["reason"], "TRAILING_STOP")
        self.assertGreater(trade["exit_price"], 102.0)                  # average of 104 and the trailed stop
        self.assertLess(trade["exit_price"], 104.0)
        self.assertGreater(trade["r_multiple"], 0.8)
        self.assertEqual(result["summary"]["wins"], 1)

    def test_target_exit_closes_the_whole_position_at_the_target(self):
        self.long(50, exit=strategy.target_exit(103.0, max_bars=10))
        self.candles[53]["high"] = 103.4
        (trade,) = self.run_bt()["trades"]
        self.assertEqual((trade["reason"], trade["exit_price"], trade["bars"]), ("TAKE_PROFIT", 103.0, 2))
        self.assertAlmostEqual(trade["r_multiple"], 1.38, delta=0.02)      # +3 on a 2-point stop, less fees

    def test_target_trade_times_out_at_max_bars(self):
        self.long(50, exit=strategy.target_exit(103.0, max_bars=4))
        (trade,) = self.run_bt()["trades"]
        self.assertEqual((trade["reason"], trade["bars"], trade["exit_price"]), ("TIME_STOP", 4, 100.0))

    def test_entry_skipped_when_the_open_runs_away_or_sits_on_the_stop(self):
        self.long(50)
        self.candles[51].update(open=101.5, high=101.7, close=101.5)    # +1.5% from the signal close
        self.long(60, stop=99.2)
        self.candles[61].update(open=99.5, low=99.3, close=99.5)        # within 1%, but most of the stop distance is gone
        result = self.run_bt()
        self.assertEqual(result["trades"], [])
        self.assertEqual({row["code"]: row["count"] for row in result["skipped"]}, {"GAP": 1, "NEAR_STOP": 1})

    def test_daily_loss_limit_blocks_the_day_and_size_follows_equity(self):
        for at in (50, 56, 80):         # 50 and 56 fall on one UTC day, 80 on the next
            self.long(at)
        self.candles[52]["low"] = 97.0
        self.candles[82]["low"] = 97.0
        result = self.run_bt(risk=RiskConfig(slippage_rate=0, daily_loss_pct=0.5))
        first, second = result["trades"]
        self.assertEqual({row["code"]: row["count"] for row in result["skipped"]}, {"CIRCUIT_BREAKER": 1})
        self.assertEqual(second["entry_time"], self.candles[81]["time"] * 1000)
        equity_after_first = 1000 + first["net_pnl"]
        self.assertAlmostEqual(second["risk_usdt"], equity_after_first * 0.0075, places=4)

    def test_cooldown_skips_signals_right_after_an_exit(self):
        self.long(50)
        self.candles[52]["low"] = 97.0
        self.long(53)                   # one candle after the stop-out: inside the 3-candle cooldown
        self.assertEqual(len(self.run_bt()["trades"]), 1)

    def test_funding_is_paid_only_while_the_position_is_open(self):
        self.long(50, exit=strategy.trail_exit(8.0))                     # nothing closes it before the end
        funding = [(self.candles[40]["time"] * 1000 + 1, 0.01),          # before the entry: ignored
                   (self.candles[55]["time"] * 1000 + 1, 0.001)]
        result = self.run_bt(funding=funding)
        (trade,) = result["trades"]
        self.assertEqual(trade["reason"], "END_OF_TEST")
        self.assertAlmostEqual(trade["funding"], -trade["quantity"] * 100 * 0.001, places=5)
        self.assertAlmostEqual(result["summary"]["funding"], trade["funding"], places=4)

    def test_report_splits_the_period_and_warns_on_a_small_sample(self):
        self.long(50)
        self.candles[52]["low"] = 97.0
        self.long(190)
        self.candles[192]["low"] = 97.0
        result = self.run_bt()
        self.assertEqual((result["in_sample"]["trades"], result["out_of_sample"]["trades"]), (1, 1))
        self.assertTrue(result["summary"]["low_sample"])
        self.assertTrue(any("chưa đủ để kết luận" in w for w in result["warnings"]))
        self.assertEqual(result["buy_hold"]["average_pct"], 0.0)
        self.assertAlmostEqual(result["equity_curve"][-1]["equity"], result["summary"]["final_equity"], places=2)
        self.assertEqual(result["by_setup"][0]["setup"], "TREND_PULLBACK")

    def test_config_validation(self):
        for bad in (dict(symbols=[]), dict(symbols=["A"], days=3), dict(symbols=["A"], initial_balance=0),
                    dict(symbols=[f"S{i}USDT" for i in range(16)])):
            with self.assertRaises(ValueError, msg=bad):
                backtest.BacktestConfig(**bad)


class MultiTimeframeTests(unittest.TestCase):
    """A 4h setup next to a 1h setup: each is read and managed on its own candles."""

    def setUp(self):
        start = 1_700_000_000 // (4 * HOUR) * (4 * HOUR)
        self.hourly = candles_from_closes([100.0] * 480, wick=0.5, start=start)
        self.four = candles_from_closes([100.0] * 120, wick=0.5, start=start, step=4 * HOUR)
        self.calls, self.signals = [], {}

        def fake_evaluate(setup, candles, ind, i, regime, btc_regime, params):
            self.calls.append((setup, candles[i]["close_time_ms"] - candles[i]["time"] * 1000 + 1))
            return self.signals.get((setup, candles[i]["time"])), ""

        patcher = patch.object(strategy, "evaluate", fake_evaluate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_bt(self, setups):
        data = {"start_ms": self.hourly[40]["time"] * 1000, "end_ms": self.hourly[-1]["close_time_ms"] + 1,
                "candles": {"1h": {SYM: self.hourly}, "4h": {SYM: self.four}}, "funding": {}, "funding_ok": True}
        config = backtest.BacktestConfig([SYM], days=8, risk=NO_SLIPPAGE, strategy=StrategyParams(setups=setups))
        return backtest.run_backtest(config, data)

    def test_each_setup_is_evaluated_only_on_its_own_candles(self):
        self.run_bt(["THRUST_BREAKOUT", "TREND_PULLBACK"])
        widths = {setup: {width for s, width in self.calls if s == setup} for setup in ("THRUST_BREAKOUT", "TREND_PULLBACK")}
        self.assertEqual(widths, {"THRUST_BREAKOUT": {4 * HOUR * 1000}, "TREND_PULLBACK": {HOUR * 1000}})
        hourly = sum(s == "TREND_PULLBACK" for s, _ in self.calls)
        self.assertAlmostEqual(hourly / sum(s == "THRUST_BREAKOUT" for s, _ in self.calls), 4, delta=0.2)

    def test_4h_position_is_entered_at_the_next_open_and_managed_on_4h_candles(self):
        self.signals[("THRUST_BREAKOUT", self.four[60]["time"])] = Signal(
            "LONG", 100.0, 98.0, 1.0, "UP", "test", "THRUST_BREAKOUT", strategy.trail_exit(4.0))
        self.four[62]["low"] = 97.0                                   # the second 4h candle after the signal hits the stop
        result = self.run_bt(["THRUST_BREAKOUT", "TREND_PULLBACK"])
        (trade,) = result["trades"]
        self.assertEqual(trade["setup"], "THRUST_BREAKOUT")
        self.assertEqual(trade["entry_time"], self.four[61]["time"] * 1000)       # open of the next 4h candle
        self.assertEqual(trade["exit_time"], self.four[62]["close_time_ms"])
        self.assertEqual((trade["reason"], trade["bars"]), ("STOP_LOSS", 1))      # one full 4h candle was held

    def test_the_first_setup_in_priority_order_wins_a_tie(self):
        t = self.four[60]["time"] + 3 * HOUR                              # the 1h candle closing with that 4h candle
        self.signals[("THRUST_BREAKOUT", self.four[60]["time"])] = Signal(
            "LONG", 100.0, 98.0, 1.0, "UP", "a", "THRUST_BREAKOUT", strategy.trail_exit(8.0))
        self.signals[("TREND_PULLBACK", t)] = Signal("SHORT", 100.0, 102.0, 1.0, "DOWN", "b", exit=strategy.trail_exit(8.0))
        trades = self.run_bt(["THRUST_BREAKOUT", "TREND_PULLBACK"])["trades"]
        self.assertEqual([(x["setup"], x["direction"]) for x in trades], [("THRUST_BREAKOUT", "LONG")])


class RegimeLookupTests(unittest.TestCase):
    def test_only_candles_closed_by_then_are_used(self):
        candles = candles_from_closes([100 + 0.5 * i for i in range(320)], step=4 * HOUR)
        lookup = backtest._Regime(candles, StrategyParams())
        self.assertEqual(lookup.at(candles[-1]["close_time_ms"]), "UP")
        self.assertEqual(lookup.at(candles[100]["close_time_ms"]), "RANGE")     # EMA200 not formed yet
        self.assertEqual(lookup.at(candles[0]["close_time_ms"] - 1), "RANGE")


if __name__ == "__main__":
    unittest.main()
