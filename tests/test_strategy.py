import unittest

import strategy
from indicators import adx, atr, ema, rsi
from strategy import StrategyParams
from tests.helpers import candles_from_closes, pullback_closes

P = StrategyParams()


def evaluate(closes, regime, btc="RANGE", params=P):
    candles = candles_from_closes(closes)
    return strategy.evaluate(candles, strategy.signal_indicators(candles), len(candles) - 1, regime, btc, params)


class IndicatorTests(unittest.TestCase):
    def test_series_are_aligned_and_blank_until_warm(self):
        candles = candles_from_closes([100 + i for i in range(60)])
        closes = [c["close"] for c in candles]
        for series, first in ((ema(closes, 20), 19), (rsi(closes), 14), (atr(candles), 14), (adx(candles), 27)):
            self.assertEqual(len(series), 60)
            self.assertIsNone(series[first - 1])
            self.assertIsNotNone(series[first])

    def test_values_on_a_straight_trend(self):
        candles = candles_from_closes([100 + i for i in range(60)], wick=0.5)
        self.assertEqual(rsi([c["close"] for c in candles])[-1], 100.0)
        self.assertAlmostEqual(atr(candles)[-1], 2.0)            # 1 body + 2 x 0.5 wick
        self.assertGreater(adx(candles)[-1], 90)                 # one-directional movement only


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


class EntryTests(unittest.TestCase):
    def test_long_after_pullback_in_uptrend(self):
        closes = pullback_closes()
        signal, note = evaluate(closes, "UP")
        self.assertIsNotNone(signal, note)
        self.assertEqual(signal.direction, "LONG")
        self.assertEqual(signal.price, closes[-1])
        swing_low = min(closes[-7:]) - 0.2
        self.assertLess(signal.stop, swing_low)                               # beyond the pullback low
        distance = (signal.price - signal.stop) / signal.atr
        self.assertTrue(P.stop_atr_min <= distance <= P.stop_atr_max, distance)

    def test_short_is_the_mirror_image(self):
        long_signal, _ = evaluate(pullback_closes(), "UP")
        short_signal, note = evaluate(pullback_closes(-1), "DOWN")
        self.assertIsNotNone(short_signal, note)
        self.assertEqual(short_signal.direction, "SHORT")
        self.assertAlmostEqual(short_signal.stop - short_signal.price, long_signal.price - long_signal.stop)

    def test_no_trade_without_a_trend_or_against_bitcoin(self):
        closes = pullback_closes()
        self.assertIsNone(evaluate(closes, "RANGE")[0])
        self.assertIsNone(evaluate(closes, "UP", btc="DOWN")[0])
        self.assertIsNotNone(evaluate(closes, "UP", btc="UP")[0])
        self.assertIsNone(evaluate(closes, "DOWN")[0])                        # 1h EMAs point the other way
        self.assertIsNone(evaluate(pullback_closes(-1), "DOWN", params=StrategyParams(allow_short=False))[0])

    def test_waits_for_pullback_and_confirmation(self):
        closes = pullback_closes()
        signal, note = evaluate(closes[:80], "UP")                            # straight trend, no pullback
        self.assertIsNone(signal)
        self.assertIn("chờ giá hồi", note)
        signal, note = evaluate(closes[:-1], "UP")                            # pulled back, not confirmed
        self.assertIsNone(signal)
        self.assertIn("chờ nến xác nhận", note)

    def test_does_not_chase_an_extended_candle(self):
        closes = pullback_closes()
        closes[-1] += 3.0
        signal, note = evaluate(closes, "UP")
        self.assertIsNone(signal)
        self.assertIn("không đuổi giá", note)

    def test_skips_when_the_structural_stop_is_too_far(self):
        signal, note = evaluate(pullback_closes(), "UP", params=StrategyParams(stop_atr_min=0.8, stop_atr_max=1.5))
        self.assertIsNone(signal)
        self.assertIn("bỏ lệnh", note)


class ExitPlanTests(unittest.TestCase):
    def candle(self, high, low, close=None):
        return {"high": high, "low": low, "close": close if close is not None else (high + low) / 2}

    def test_plan_levels(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, P)
        self.assertEqual((plan["r"], plan["tp1"], plan["stop"]), (2.0, 104.0, 98.0))
        self.assertEqual(strategy.new_plan("SHORT", 100.0, 102.0, P)["tp1"], 96.0)
        with self.assertRaises(ValueError):
            strategy.new_plan("LONG", 100.0, 100.5, P)

    def test_stop_wins_when_one_candle_touches_stop_and_target(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, P)
        self.assertEqual(strategy.intrabar_event(plan, 100, 104.5, 97.5), ("STOP", 98.0))
        self.assertEqual(strategy.intrabar_event(plan, 100, 104.5, 99.0), ("PARTIAL", 104.0))
        self.assertIsNone(strategy.intrabar_event(plan, 100, 103.0, 99.0))

    def test_gap_through_the_stop_fills_at_the_open(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, P)
        self.assertEqual(strategy.intrabar_event(plan, 97.0, 97.5, 96.0), ("STOP", 97.0))
        short = strategy.new_plan("SHORT", 100.0, 102.0, P)
        self.assertEqual(strategy.intrabar_event(short, 103.0, 104.0, 102.5), ("STOP", 103.0))

    def test_breakeven_then_trailing_only_ever_tightens(self):
        plan = strategy.new_plan("LONG", 100.0, 98.0, P)
        strategy.advance(plan, self.candle(101.5, 100.0), 1.0, P)
        self.assertEqual((plan["stop"], plan["stop_kind"]), (98.5, "TRAIL"))      # 101.5 - 3 ATR
        strategy.advance(plan, self.candle(102.2, 101.0), 1.0, P)                 # +1.1R: breakeven beats the trail
        self.assertAlmostEqual(plan["stop"], 100.1)
        self.assertEqual(plan["stop_kind"], "BREAKEVEN")
        strategy.advance(plan, self.candle(106.0, 103.0), 1.0, P)
        self.assertEqual((plan["stop"], plan["stop_kind"]), (103.0, "TRAIL"))
        strategy.advance(plan, self.candle(104.0, 103.5), 2.0, P)                 # wider ATR must not loosen it
        self.assertEqual(plan["stop"], 103.0)
        self.assertEqual(strategy.stop_reason(plan), "TRAILING_STOP")

    def test_short_trails_downwards(self):
        plan = strategy.new_plan("SHORT", 100.0, 102.0, P)
        strategy.advance(plan, self.candle(99.0, 94.0), 1.0, P)
        self.assertEqual(plan["stop"], 97.0)
        self.assertEqual(strategy.intrabar_event(plan, 96.0, 97.2, 95.0), ("STOP", 97.0))

    def test_time_stop_only_for_trades_that_went_nowhere(self):
        params = StrategyParams(time_stop_bars=6)
        stuck = strategy.new_plan("LONG", 100.0, 98.0, params)
        outcomes = [strategy.advance(stuck, self.candle(100.5, 99.5), 1.0, params) for _ in range(6)]
        self.assertEqual(outcomes, [None] * 5 + ["TIME_STOP"])
        moving = strategy.new_plan("LONG", 100.0, 98.0, params)
        outcomes = [strategy.advance(moving, self.candle(101.2, 100.0), 1.0, params) for _ in range(8)]
        self.assertEqual(outcomes, [None] * 8)


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
