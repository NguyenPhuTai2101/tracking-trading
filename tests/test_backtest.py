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


class BacktestTests(unittest.TestCase):
    """Signals are injected at chosen candle indices; the entry rules themselves are tested in test_strategy."""

    def setUp(self):
        # Wicks of 0.5 give an ATR of 1.0, so a 2-point stop is 2 ATR: inside the 3 ATR trail.
        self.candles = candles_from_closes([100.0] * 220, wick=0.5)
        self.signals = {}
        index = {c["time"]: i for i, c in enumerate(self.candles)}

        def fake_evaluate(candles, ind, i, regime, btc_regime, params):
            return self.signals.get(index[candles[i]["time"]]), ""

        patcher = patch.object(strategy, "evaluate", fake_evaluate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def long(self, at, stop=98.0):
        self.signals[at] = Signal("LONG", 100.0, stop, 1.0, "UP", "test")

    def run_bt(self, risk=NO_SLIPPAGE, funding=None, params=None):
        data = {"start_ms": self.candles[WARMUP]["time"] * 1000,
                "end_ms": self.candles[-1]["close_time_ms"] + 1,
                "signal": {SYM: self.candles}, "regime": {}, "funding": {SYM: funding or []}, "funding_ok": True}
        config = backtest.BacktestConfig([SYM], days=8, risk=risk, strategy=params or StrategyParams())
        return backtest.run_backtest(config, data)

    def test_stop_out_costs_one_r_and_fees_are_charged_both_sides(self):
        self.long(50)
        self.candles[52]["low"] = 97.0
        result = self.run_bt()
        (trade,) = result["trades"]
        quantity = 7.5 / (0.02 + 0.001) / 100             # 0.75% of 1000 over a 2% stop + 0.1% fees
        self.assertEqual(trade["entry_time"], self.candles[51]["time"] * 1000)     # next candle's open
        self.assertEqual((trade["entry_price"], trade["exit_price"], trade["reason"]), (100.0, 98.0, "STOP_LOSS"))
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
        self.long(50)
        funding = [(self.candles[40]["time"] * 1000 + 1, 0.01),          # before the entry: ignored
                   (self.candles[55]["time"] * 1000 + 1, 0.001)]
        result = self.run_bt(funding=funding, params=StrategyParams(time_stop_bars=200))
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

    def test_config_validation(self):
        for bad in (dict(symbols=[]), dict(symbols=["A"], days=3), dict(symbols=["A"], initial_balance=0),
                    dict(symbols=[f"S{i}USDT" for i in range(16)])):
            with self.assertRaises(ValueError, msg=bad):
                backtest.BacktestConfig(**bad)


class RegimeLookupTests(unittest.TestCase):
    def test_only_candles_closed_by_then_are_used(self):
        candles = candles_from_closes([100 + 0.5 * i for i in range(320)], step=4 * HOUR)
        lookup = backtest._Regime(candles, StrategyParams())
        self.assertEqual(lookup.at(candles[-1]["close_time_ms"]), "UP")
        self.assertEqual(lookup.at(candles[100]["close_time_ms"]), "RANGE")     # EMA200 not formed yet
        self.assertEqual(lookup.at(candles[0]["close_time_ms"] - 1), "RANGE")


if __name__ == "__main__":
    unittest.main()
