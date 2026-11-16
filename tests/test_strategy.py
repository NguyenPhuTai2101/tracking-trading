import unittest

from pydantic import ValidationError

import strategy
from indicators import adx, atr, bollinger, ema, percent_rank, rolling_extreme, rsi
from strategy import StrategyParams
from tests.helpers import candles_from_closes, pullback_closes

P = StrategyParams()
TREND = strategy.trend_exit(P)


def evaluate(setup, candles, regime, btc="RANGE", params=P):
    return strategy.evaluate(setup, candles, strategy.signal_indicators(candles, params), len(candles) - 1,
                             regime, btc, params)


def pullback(closes, regime, btc="RANGE", params=P):
    return evaluate("TREND_PULLBACK", candles_from_closes(closes), regime, btc, params)


class IndicatorTests(unittest.TestCase):
    def test_series_are_aligned_and_blank_until_warm(self):
        candles = candles_from_closes([100 + i for i in range(60)])
        closes = [c["close"] for c in candles]
        for series, first in ((ema(closes, 20), 19), (rsi(closes), 14), (atr(candles), 14), (adx(candles), 27),
                              (bollinger(closes)[0], 19)):
            self.assertEqual(len(series), 60)
            self.assertIsNone(series[first - 1])
            self.assertIsNotNone(series[first])

    def test_values_on_a_straight_trend(self):
        candles = candles_from_closes([100 + i for i in range(60)], wick=0.5)
        self.assertEqual(rsi([c["close"] for c in candles])[-1], 100.0)
        self.assertAlmostEqual(atr(candles)[-1], 2.0)            # 1 body + 2 x 0.5 wick
        self.assertGreater(adx(candles)[-1], 90)                 # one-directional movement only

    def test_rolling_extreme_looks_only_at_earlier_values(self):
        values = [1, 3, 2, 5, 4, 1]
        self.assertEqual(rolling_extreme(values, 2, max), [None, None, 3, 3, 5, 5])
        self.assertEqual(rolling_extreme(values, 2, min), [None, None, 1, 2, 2, 4])

    def test_bollinger_and_percent_rank(self):
        middle, upper, lower = bollinger([10.0] * 19 + [20.0], 20, 2.0)
        self.assertAlmostEqual(middle[-1], 10.5)
        self.assertAlmostEqual(upper[-1] - middle[-1], middle[-1] - lower[-1])
        self.assertEqual(percent_rank([1, 2, 3, 2, 5], 3), [None, None, 2 / 3, 0.0, 2 / 3])


class RegimeTests(unittest.TestCase):
    def regime(self, closes):
        return strategy.regime_series(candles_from_closes(closes, step=4 * 3600), P)[-1]

    def test_up_down_and_range(self):
        self.assertEqual(self.regime([100 + 0.5 * i for i in range(320)]), "UP")
        self.assertEqual(self.regime([300 - 0.5 * i for i in range(320)]), "DOWN")
        self.assertEqual(self.regime([100 + (0, 1, 0, -1)[i % 4] for i in range(320)]), "RANGE")   # chop

    def test_not_enough_history_is_range(self):
        self.assertEqual(self.regime([100 + 0.5 * i for i in range(150)]), "RANGE")

    def test_price_back_under_a_rising_ema_is_not_an_uptrend(self):
        closes = [100 + 0.5 * i for i in range(320)]
        self.assertEqual(self.regime(closes + [closes[-1] - 60]), "RANGE")


class TrendPullbackTests(unittest.TestCase):
    def test_long_after_pullback_in_uptrend(self):
        closes = pullback_closes()
        signal, note = pullback(closes, "UP")
        self.assertIsNotNone(signal, note)
        self.assertEqual((signal.direction, signal.setup, signal.price), ("LONG", "TREND_PULLBACK", closes[-1]))
        self.assertLess(signal.stop, min(closes[-7:]) - 0.2)                  # beyond the pullback low
        distance = (signal.price - signal.stop) / signal.atr
        self.assertTrue(P.stop_atr_min <= distance <= P.stop_atr_max, distance)
        self.assertEqual(signal.exit, TREND)

    def test_short_is_the_mirror_image(self):
        long_signal, _ = pullback(pullback_closes(), "UP")
        short_signal, note = pullback(pullback_closes(-1), "DOWN")
        self.assertIsNotNone(short_signal, note)
        self.assertEqual(short_signal.direction, "SHORT")
        self.assertAlmostEqual(short_signal.stop - short_signal.price, long_signal.price - long_signal.stop)

    def test_no_trade_without_a_trend_or_against_bitcoin(self):
        closes = pullback_closes()
        self.assertIsNone(pullback(closes, "RANGE")[0])
        self.assertIsNone(pullback(closes, "UP", btc="DOWN")[0])
        self.assertIsNotNone(pullback(closes, "UP", btc="UP")[0])
        self.assertIsNone(pullback(closes, "DOWN")[0])                        # 1h EMAs point the other way
        self.assertIsNone(pullback(pullback_closes(-1), "DOWN", params=StrategyParams(allow_short=False))[0])

    def test_waits_for_pullback_and_confirmation(self):
        closes = pullback_closes()
        signal, note = pullback(closes[:80], "UP")                            # straight trend, no pullback
        self.assertIsNone(signal)
        self.assertIn("chờ giá hồi", note)
        signal, note = pullback(closes[:-1], "UP")                            # pulled back, not confirmed
        self.assertIsNone(signal)
        self.assertIn("chờ nến xác nhận", note)

    def test_does_not_chase_or_take_a_stop_that_is_too_far(self):
        closes = pullback_closes()
        closes[-1] += 3.0
        self.assertIn("không đuổi giá", pullback(closes, "UP")[1])
        tight = StrategyParams(stop_atr_min=0.8, stop_atr_max=1.5)
        self.assertIn("bỏ lệnh", pullback(pullback_closes(), "UP", params=tight)[1])


def thrust_candles(sign=1):
    """A quiet range, then one candle that closes far beyond it."""
    closes = [100 + (0, 0.4, 0, -0.4)[i % 4] for i in range(160)] + [100 + sign * 4.0]
    return candles_from_closes(closes)


class ThrustBreakoutTests(unittest.TestCase):
    def test_close_beyond_range_and_band_is_a_breakout(self):
        signal, note = evaluate("THRUST_BREAKOUT", thrust_candles(), "RANGE")
        self.assertIsNotNone(signal, note)
        self.assertEqual((signal.direction, signal.setup), ("LONG", "THRUST_BREAKOUT"))
        self.assertAlmostEqual(signal.price - signal.stop, P.breakout_stop_atr * signal.atr)
        self.assertEqual(signal.exit, {"trail_atr": P.breakout_trail_atr})    # no breakeven, no partial
        short, _ = evaluate("THRUST_BREAKOUT", thrust_candles(-1), "DOWN")
        self.assertEqual(short.direction, "SHORT")

    def test_not_against_the_daily_regime_and_not_inside_the_range(self):
        self.assertIsNone(evaluate("THRUST_BREAKOUT", thrust_candles(), "DOWN")[0])
        self.assertIsNotNone(evaluate("THRUST_BREAKOUT", thrust_candles(), "UP")[0])
        self.assertIsNone(evaluate("THRUST_BREAKOUT", thrust_candles()[:-1], "UP")[0])
        no_short = StrategyParams(allow_short=False)
        self.assertIsNone(evaluate("THRUST_BREAKOUT", thrust_candles(-1), "DOWN", params=no_short)[0])

    def test_lookback_is_configurable(self):
        candles = candles_from_closes([100.0] * 60 + [103.0] + [100.0] * 30 + [102.0])
        self.assertIsNotNone(evaluate("THRUST_BREAKOUT", candles, "UP")[0])                      # above the last 20
        wide = StrategyParams(breakout_lookback=40)
        self.assertIsNone(evaluate("THRUST_BREAKOUT", candles, "UP", params=wide)[0])           # 103 is in the last 40


class FadeSetupTests(unittest.TestCase):
    def reversion_candles(self):
        # A range, a slide that closes below the lower band, then a candle that closes back inside.
        closes = [100 + (0, 0.6, 0, -0.6)[i % 4] for i in range(60)] + [98.4, 98.2, 97.2, 97.0, 96.2, 95.6, 96.8]
        return candles_from_closes(closes, wick=0.1)

    def test_range_reversion_targets_the_mean(self):
        candles = self.reversion_candles()
        signal, note = evaluate("RANGE_REVERSION", candles, "RANGE")
        self.assertIsNotNone(signal, note)
        self.assertEqual((signal.direction, signal.setup), ("LONG", "RANGE_REVERSION"))
        ind = strategy.signal_indicators(candles)
        self.assertEqual(signal.exit, {"target": ind["bb_mid"][-1], "max_bars": strategy.FADE_MAX_BARS})
        self.assertLess(signal.stop, min(c["low"] for c in candles[-3:]))
        self.assertGreaterEqual(signal.exit["target"] - signal.price, signal.price - signal.stop)   # at least 1:1
        self.assertIsNone(evaluate("RANGE_REVERSION", candles, "UP")[0])                             # only in a range
        self.assertIsNone(evaluate("RANGE_REVERSION", candles[:-1], "RANGE")[0])                     # not back inside yet

    def sweep_candles(self):
        candles = candles_from_closes([100 + (0, 0.5, 0, -0.5)[i % 4] for i in range(80)], wick=0.3)
        last = candles[-1]
        candles.append({"time": last["time"] + 3600, "open": 100.6, "high": 102.0, "low": 100.2, "close": 100.4,
                        "volume": 1.0, "close_time_ms": last["close_time_ms"] + 3_600_000, "closed": True})
        return candles

    def test_failed_breakout_fades_the_sweep_with_a_2r_target(self):
        candles = self.sweep_candles()
        signal, note = evaluate("FAILED_BREAKOUT", candles, "RANGE")
        self.assertIsNotNone(signal, note)
        self.assertEqual((signal.direction, signal.setup), ("SHORT", "FAILED_BREAKOUT"))
        self.assertGreater(signal.stop, 102.0)                                                       # above the sweep high
        self.assertAlmostEqual(signal.price - signal.exit["target"], 2 * (signal.stop - signal.price))
        self.assertIsNone(evaluate("FAILED_BREAKOUT", candles, "UP")[0])                             # never against the trend
        self.assertIsNotNone(evaluate("FAILED_BREAKOUT", candles, "DOWN")[0])


class MarketStateTests(unittest.TestCase):
    def test_tags_describe_squeeze_shock_and_extremes(self):
        quiet = candles_from_closes([100 + (0, 3, 0, -3)[i % 4] for i in range(150)] + [100.0] * 30, wick=0.05)
        ind = strategy.signal_indicators(quiet)
        self.assertIn("SQUEEZE", strategy.market_tags(ind, len(quiet) - 1))
        shocked = candles_from_closes([100 + (0, 0.2, 0, -0.2)[i % 4] for i in range(150)] + [106, 112, 118, 124, 130, 136])
        ind = strategy.signal_indicators(shocked)
        tags = strategy.market_tags(ind, len(shocked) - 1)
        self.assertIn("SHOCK", tags)
        self.assertIn("OVERBOUGHT", tags)
        self.assertNotIn("SQUEEZE", tags)

    def test_setup_list_is_validated_and_ordered(self):
        self.assertEqual(P.setups, ["THRUST_BREAKOUT"])
        params = StrategyParams(setups=["TREND_PULLBACK", "THRUST_BREAKOUT", "TREND_PULLBACK"])
        self.assertEqual(params.setups, ["THRUST_BREAKOUT", "TREND_PULLBACK"])
        for bad in (["NOPE"], []):
            with self.assertRaises(ValidationError):
                StrategyParams(setups=bad)
        self.assertEqual({s.interval for s in strategy.SETUPS.values()}, {"1h", "4h"})


class ExitPlanTests(unittest.TestCase):
    def candle(self, high, low, close=None):
        return {"high": high, "low": low, "close": close if close is not None else (high + low) / 2}

    def test_plan_levels(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, TREND)
        self.assertEqual((plan["r"], plan["tp1"], plan["stop"]), (2.0, 104.0, 98.0))
        self.assertEqual(strategy.new_plan("SHORT", 100.0, 102.0, TREND)["tp1"], 96.0)
        with self.assertRaises(ValueError):
            strategy.new_plan("LONG", 100.0, 100.5, TREND)

    def test_stop_wins_when_one_candle_touches_stop_and_target(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, TREND)
        self.assertEqual(strategy.intrabar_event(plan, 100, 104.5, 97.5), ("STOP", 98.0))
        self.assertEqual(strategy.intrabar_event(plan, 100, 104.5, 99.0), ("PARTIAL", 104.0))
        self.assertIsNone(strategy.intrabar_event(plan, 100, 103.0, 99.0))

    def test_gap_through_the_stop_fills_at_the_open(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, TREND)
        self.assertEqual(strategy.intrabar_event(plan, 97.0, 97.5, 96.0), ("STOP", 97.0))
        short = strategy.new_plan("SHORT", 100.0, 102.0, TREND)
        self.assertEqual(strategy.intrabar_event(short, 103.0, 104.0, 102.5), ("STOP", 103.0))

    def test_breakeven_then_trailing_only_ever_tightens(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, TREND)
        strategy.advance(plan, self.candle(101.5, 100.0), 1.0)
        self.assertEqual((plan["stop"], plan["stop_kind"]), (98.5, "TRAIL"))      # 101.5 - 3 ATR
        strategy.advance(plan, self.candle(102.2, 101.0), 1.0)                    # +1.1R: breakeven beats the trail
        self.assertAlmostEqual(plan["stop"], 100.1)
        self.assertEqual(plan["stop_kind"], "BREAKEVEN")
        strategy.advance(plan, self.candle(106.0, 103.0), 1.0)
        self.assertEqual((plan["stop"], plan["stop_kind"]), (103.0, "TRAIL"))
        strategy.advance(plan, self.candle(104.0, 103.5), 2.0)                    # wider ATR must not loosen it
        self.assertEqual(plan["stop"], 103.0)
        self.assertEqual(strategy.stop_reason(plan), "TRAILING_STOP")

    def test_short_trails_downwards(self):
        plan = strategy.new_plan("SHORT", 100.0, 102.0, TREND)
        strategy.advance(plan, self.candle(99.0, 94.0), 1.0)
        self.assertEqual(plan["stop"], 97.0)
        self.assertEqual(strategy.intrabar_event(plan, 96.0, 97.2, 95.0), ("STOP", 97.0))

    def test_time_stop_only_for_trades_that_went_nowhere(self):
        rules = strategy.trend_exit(StrategyParams(time_stop_bars=6))
        stuck = strategy.new_plan("LONG", 100.0, 98.0, rules)
        outcomes = [strategy.advance(stuck, self.candle(100.5, 99.5), 1.0) for _ in range(6)]
        self.assertEqual(outcomes, [None] * 5 + ["TIME_STOP"])
        moving = strategy.new_plan("LONG", 100.0, 98.0, rules)
        outcomes = [strategy.advance(moving, self.candle(101.2, 100.0), 1.0) for _ in range(8)]
        self.assertEqual(outcomes, [None] * 8)

    def test_trail_only_plan_has_no_breakeven_partial_or_time_stop(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, strategy.trail_exit(4.0))
        self.assertTrue(plan["partial_done"])
        self.assertIsNone(strategy.intrabar_event(plan, 100, 110.0, 99.0))        # no partial target to hit
        for _ in range(300):
            self.assertIsNone(strategy.advance(plan, self.candle(102.5, 100.0), 1.0))
        self.assertEqual(plan["stop"], 98.5)                                      # 102.5 - 4 ATR; breakeven never applied

    def test_target_plan_leaves_at_target_or_after_max_bars_and_never_moves_the_stop(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, strategy.target_exit(103.0, max_bars=3))
        self.assertEqual(strategy.intrabar_event(plan, 100, 103.5, 99.0), ("TARGET", 103.0))
        self.assertEqual(strategy.intrabar_event(plan, 100, 103.5, 97.0), ("STOP", 98.0))
        outcomes = [strategy.advance(plan, self.candle(102.9, 100.0), 0.2) for _ in range(3)]
        self.assertEqual(outcomes, [None, None, "TIME_STOP"])
        self.assertEqual((plan["stop"], plan["stop_kind"]), (98.0, "INITIAL"))
        with self.assertRaises(ValueError):
            strategy.new_plan("LONG", 100.0, 98.0, strategy.target_exit(99.5))    # target behind the entry


class ClosedCandleTests(unittest.TestCase):
    def test_rejects_stale_gapped_or_unfinished_data(self):
        candles = candles_from_closes([100 + i for i in range(90)])
        now = candles[-1]["close_time_ms"] + 5_000
        self.assertEqual(len(strategy.closed_candles(candles, "1h", now, 80)), 90)
        with self.assertRaises(ValueError):
            strategy.closed_candles(candles, "1h", now + 2 * 3_600_000, 80)       # stale
        with self.assertRaises(ValueError):
            strategy.closed_candles(candles[:40] + candles[41:], "1h", now, 80)   # a missing candle
        with self.assertRaises(ValueError):
            strategy.closed_candles(candles[:50], "1h", candles[49]["close_time_ms"] + 1, 80)
        candles[-1]["closed"] = False
        self.assertEqual(len(strategy.closed_candles(candles, "1h", candles[-2]["close_time_ms"] + 5_000, 80)), 89)


if __name__ == "__main__":
    unittest.main()
