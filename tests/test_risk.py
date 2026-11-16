import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

import risk
from risk import RiskConfig


def at(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)     # 2026-11-16 is a Monday


def position(symbol, direction="LONG", quantity=3.75, entry=100.0, stop=98.0):
    return {"symbol": symbol, "direction": direction, "quantity": quantity, "entry_price": entry, "stop_price": stop}


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig()
        self.state = risk.new_state()
        risk.roll(self.state, 1000, at(16), self.cfg)

    def plan(self, **overrides):
        args = dict(symbol="ETHUSDT", direction="LONG", entry=100.0, stop=98.0, equity=1000.0, available=1000.0,
                    positions=[], state=self.state, cfg=self.cfg)
        args.update(overrides)
        return risk.plan_entry(**args)

    def test_stop_out_loses_the_configured_share_of_equity_including_costs(self):
        decision = self.plan()
        self.assertTrue(decision.ok)
        # 0.75% of 1000 = 7.5 USDT at risk over a 2% stop plus 0.14% round-trip costs.
        self.assertAlmostEqual(decision.notional, 7.5 / (0.02 + 0.0014), places=6)
        self.assertAlmostEqual(decision.risk_usdt, 7.5, places=6)

    def test_wider_stop_means_smaller_position_same_risk(self):
        tight, wide = self.plan(stop=98.0), self.plan(stop=96.0)
        self.assertLess(wide.notional, tight.notional)
        self.assertAlmostEqual(wide.risk_usdt, tight.risk_usdt, places=6)

    def test_short_is_sized_like_long(self):
        self.assertAlmostEqual(self.plan(direction="SHORT", stop=102.0).notional, self.plan().notional, places=6)

    def test_stop_on_wrong_side_is_refused(self):
        self.assertEqual(self.plan(stop=101.0).code, "BAD_STOP")
        self.assertEqual(self.plan(direction="SHORT", stop=99.0).code, "BAD_STOP")

    def test_stop_too_tight_for_costs_is_refused(self):
        # 0.14% costs over a 0.5% stop = 0.28R, above the 0.15R cap.
        self.assertEqual(self.plan(stop=99.5).code, "COST")

    def test_stop_near_liquidation_is_refused(self):
        cfg = RiskConfig(leverage=10)
        self.assertEqual(self.plan(stop=91.0, cfg=cfg).code, "LIQUIDATION")

    def test_position_and_margin_caps_only_shrink_the_trade(self):
        capped = self.plan(cfg=RiskConfig(max_position_notional_pct=20))
        self.assertAlmostEqual(capped.notional, 200.0)
        self.assertLess(capped.risk_usdt, 7.5)
        starved = self.plan(available=10.0)
        self.assertAlmostEqual(starved.notional, 10.0 * risk.MARGIN_BUFFER * 5)
        self.assertEqual(self.plan(available=0.5).code, "TOO_SMALL")


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig(max_positions=10, max_same_direction=10)
        self.state = risk.new_state()
        risk.roll(self.state, 1000, at(16), self.cfg)

    def plan(self, positions, cfg=None, direction="LONG"):
        return risk.plan_entry(symbol="NEWUSDT", direction=direction, entry=100.0,
                               stop=98.0 if direction == "LONG" else 102.0, equity=1000.0, available=1000.0,
                               positions=positions, state=self.state, cfg=cfg or self.cfg)

    def test_open_risk_cap_counts_every_open_stop(self):
        three = [position(f"S{i}USDT") for i in range(3)]          # 3 x 7.5 = 22.5 of the 30 allowed
        self.assertTrue(self.plan(three).ok)
        self.assertEqual(self.plan(three + [position("S3USDT")]).code, "OPEN_RISK")

    def test_a_stop_past_entry_frees_its_risk(self):
        locked = [position(f"S{i}USDT", stop=100.5) for i in range(4)]
        self.assertEqual(risk.open_risk(locked), 0)
        self.assertTrue(self.plan(locked).ok)

    def test_position_count_and_same_direction_caps(self):
        cfg = RiskConfig(max_positions=4, max_same_direction=2, max_open_risk_pct=8)
        longs = [position("AUSDT"), position("BUSDT")]
        self.assertEqual(self.plan(longs, cfg).code, "SAME_DIRECTION")
        self.assertTrue(self.plan(longs, cfg, direction="SHORT").ok)
        full = longs + [position("CUSDT", "SHORT", stop=102), position("DUSDT", "SHORT", stop=102)]
        self.assertEqual(self.plan(full, cfg).code, "MAX_POSITIONS")

    def test_same_symbol_is_never_stacked(self):
        decision = risk.plan_entry(symbol="AUSDT", direction="LONG", entry=100.0, stop=98.0, equity=1000.0,
                                   available=1000.0, positions=[position("AUSDT")], state=self.state, cfg=self.cfg)
        self.assertEqual(decision.code, "DUPLICATE")


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig()
        self.state = risk.new_state()
        risk.roll(self.state, 1000, at(16), self.cfg)

    def blocked(self, equity):
        return risk.plan_entry(symbol="ETHUSDT", direction="LONG", entry=100.0, stop=98.0, equity=equity,
                               available=equity, positions=[], state=self.state, cfg=self.cfg).code

    def test_daily_loss_pauses_until_the_next_utc_day(self):
        self.assertEqual(risk.roll(self.state, 985, at(16, 13), self.cfg), [])
        self.assertEqual(len(risk.roll(self.state, 979, at(16, 14), self.cfg)), 1)
        self.assertEqual(self.blocked(979), "CIRCUIT_BREAKER")
        risk.roll(self.state, 990, at(16, 20), self.cfg)             # recovering does not lift the pause
        self.assertEqual(self.blocked(990), "CIRCUIT_BREAKER")
        risk.roll(self.state, 990, at(17, 0), self.cfg)
        self.assertEqual(self.blocked(990), "")

    def test_weekly_loss_pauses_until_monday(self):
        for day, equity in ((16, 1000), (17, 985), (18, 970), (19, 955)):
            risk.roll(self.state, equity, at(day), self.cfg)         # -1.5% a day: no daily trip
        self.assertIsNone(self.state["day_block"])
        risk.roll(self.state, 949, at(20), self.cfg)                 # -5.1% on the week
        self.assertIsNotNone(self.state["week_block"])
        risk.roll(self.state, 949, at(22), self.cfg)                 # Sunday: new day, same week
        self.assertEqual(self.blocked(949), "CIRCUIT_BREAKER")
        risk.roll(self.state, 949, at(23), self.cfg)                 # Monday
        self.assertEqual(self.blocked(949), "")

    def test_losing_streak_pauses_for_the_day_and_a_win_resets_it(self):
        for _ in range(3):
            self.assertIsNone(risk.record_trade(self.state, -5, self.cfg))
        risk.record_trade(self.state, 4, self.cfg)
        self.assertEqual(self.state["loss_streak"], 0)
        for _ in range(3):
            risk.record_trade(self.state, -5, self.cfg)
        self.assertIsNotNone(risk.record_trade(self.state, -5, self.cfg))
        self.assertEqual(self.blocked(980), "CIRCUIT_BREAKER")
        risk.roll(self.state, 980, at(17), self.cfg)
        self.assertEqual(self.state["loss_streak"], 0)
        self.assertEqual(self.blocked(980), "")

    def test_drawdown_ladder_halves_risk_then_halts_until_acknowledged(self):
        risk.roll(self.state, 1100, at(17), self.cfg)                # new equity peak
        self.assertEqual(risk.risk_scale(self.state, 1000, self.cfg), 1.0)
        self.assertEqual(risk.risk_scale(self.state, 985, self.cfg), 0.5)      # -10.5% from 1100
        # Walk down slowly so the daily/weekly limits are not what blocks.
        state = self.state
        state.update(day_start_equity=985, week_start_equity=985)
        decision = risk.plan_entry(symbol="ETHUSDT", direction="LONG", entry=100.0, stop=98.0, equity=985.0,
                                   available=985.0, positions=[], state=state, cfg=self.cfg)
        self.assertAlmostEqual(decision.risk_usdt, 985 * 0.0075 * 0.5, places=6)
        events = risk.roll(state, 879, at(30), self.cfg)             # -20.1% from the peak
        self.assertTrue(state["halted"])
        self.assertTrue(any("Drawdown" in e for e in events))
        risk.roll(state, 879, datetime(2026, 12, 7, tzinfo=timezone.utc), self.cfg)   # time does not lift a halt
        self.assertEqual(self.blocked(879), "CIRCUIT_BREAKER")
        risk.acknowledge_halt(state, 879)
        self.assertEqual(self.blocked(879), "")
        self.assertEqual(risk.risk_scale(state, 879, self.cfg), 1.0)


class ConfigTests(unittest.TestCase):
    def test_limits_cannot_be_disabled_or_made_reckless(self):
        for bad in (dict(risk_pct=0), dict(risk_pct=5), dict(daily_loss_pct=0), dict(leverage=50),
                    dict(dd_halt_pct=5, dd_reduce_pct=10), dict(weekly_loss_pct=1, daily_loss_pct=2),
                    dict(max_open_risk_pct=0.5, risk_pct=1)):
            with self.assertRaises(ValidationError, msg=bad):
                RiskConfig(**bad)


if __name__ == "__main__":
    unittest.main()
