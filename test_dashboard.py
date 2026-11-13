import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import bot_engine
import main
import metrics
import test_trading_upgrade as regression

NOW = datetime(2026, 11, 13, 12, 0, 0)


def fill(symbol, exit_time, pnl, *, entry_time="2026-11-10 09:00:00", side="LONG", reason="TAKE_PROFIT",
         market="FUTURES", exit_price=100.0, quantity=1.0):
    return {"market_type": market, "position_side": side, "leverage": 3, "symbol": symbol,
            "entry_time": entry_time, "exit_time": exit_time, "entry_price": 100.0, "exit_price": exit_price,
            "quantity": quantity, "pnl_usdt": pnl, "pnl_pct": 0.0, "reason": reason}


class SummariseFillsTests(unittest.TestCase):
    def test_fees_are_subtracted_and_trades_are_scored_after_costs(self):
        fills = [fill("BTCUSDT", "2026-11-12 10:00:00", 2.0),                                   # +1.9 net
                 fill("ETHUSDT", "2026-11-12 11:00:00", -1.0, reason="STOP_LOSS", side="SHORT"),  # -1.1 net
                 fill("SOLUSDT", "2026-11-13 09:00:00", 0.05)]                                   # -0.05: fees eat it
        k = metrics.summarise_fills(fills, now=NOW)["kpis"]
        self.assertAlmostEqual(k["gross_pnl"], 1.05)
        self.assertAlmostEqual(k["fees_est"], 0.3)            # 0.1% round trip on 100 USDT, three times
        self.assertAlmostEqual(k["net_pnl"], 0.75)
        self.assertEqual((k["trades"], k["wins"], k["losses"]), (3, 1, 2))
        self.assertAlmostEqual(k["profit_factor"], round(1.9 / 1.15, 2))
        self.assertAlmostEqual(k["expectancy"], 0.25)
        self.assertEqual(k["streak"], -2)
        self.assertFalse(k["enough_trades"])

    def test_partial_closes_of_one_position_count_as_one_trade(self):
        fills = [fill("BTCUSDT", "2026-11-12 10:00:00", -0.5, quantity=0.5),
                 fill("BTCUSDT", "2026-11-12 10:05:00", 1.5, quantity=0.5),
                 fill("BTCUSDT", "2026-11-12 15:00:00", 1.0, entry_time="2026-11-12 14:00:00")]
        result = metrics.summarise_fills(fills, now=NOW)
        self.assertEqual(result["kpis"]["trades"], 2)
        self.assertEqual(result["kpis"]["wins"], 2)            # the first trade nets +0.9 overall
        self.assertEqual(len(result["equity"]), 3)             # the curve still shows every fill
        self.assertEqual(result["by_symbol"][0]["trades"], 2)

    def test_partly_open_position_is_not_scored_yet(self):
        fills = [fill("BTCUSDT", "2026-11-12 10:00:00", -2.0, quantity=0.5)]
        result = metrics.summarise_fills(fills, now=NOW,
                                         open_keys=[("FUTURES", "BTCUSDT", "2026-11-10 09:00:00")])
        self.assertEqual(result["kpis"]["trades"], 0)
        self.assertIsNone(result["kpis"]["winrate_pct"])
        self.assertAlmostEqual(result["kpis"]["net_pnl"], -2.05)

    def test_period_filter_daily_totals_and_drawdown(self):
        fills = [fill("BTCUSDT", "2026-10-01 10:00:00", 50.0, entry_time="2026-10-01 09:00:00"),
                 fill("BTCUSDT", "2026-11-11 10:00:00", 3.1, entry_time="2026-11-11 09:00:00"),
                 fill("ETHUSDT", "2026-11-12 10:00:00", -1.9, entry_time="2026-11-12 09:00:00", reason="STOP_LOSS"),
                 fill("ETHUSDT", "2026-11-12 18:00:00", -0.9, entry_time="2026-11-12 17:00:00", reason="STOP_LOSS")]
        recent = metrics.summarise_fills(fills, days=7, now=NOW)
        self.assertEqual(recent["kpis"]["trades"], 3)
        self.assertEqual(recent["daily"], [{"date": "2026-11-11", "pnl": 3.0, "fills": 1},
                                           {"date": "2026-11-12", "pnl": -3.0, "fills": 2}])
        self.assertAlmostEqual(recent["kpis"]["max_drawdown"], 3.0)
        self.assertEqual([p["value"] for p in recent["equity"]], [3.0, 1.0, 0.0])
        self.assertEqual(recent["by_reason"][0]["reason"], "TAKE_PROFIT")
        self.assertEqual(metrics.summarise_fills(fills, days=0, now=NOW)["kpis"]["trades"], 4)

    def test_empty_history_and_bad_timestamps(self):
        result = metrics.summarise_fills([fill("BTCUSDT", "not a date", 5.0)], now=NOW)
        self.assertEqual(result["kpis"]["trades"], 0)
        self.assertEqual(result["kpis"]["net_pnl"], 0)
        self.assertIsNone(result["kpis"]["profit_factor"])
        self.assertEqual(result["equity"], [])

    def test_equity_times_strictly_increase(self):
        fills = [fill("BTCUSDT", "2026-11-12 10:00:00", 1.0, entry_time=f"2026-11-12 0{i}:00:00") for i in range(3)]
        times = [p["time"] for p in metrics.summarise_fills(fills, now=NOW)["equity"]]
        self.assertEqual(times, sorted(set(times)))


class DashboardEndpointTests(unittest.TestCase):
    def setUp(self):
        regression.TradingUpgradeTests.setUp(self)
        self.bot.max_open_risk_usdt = 100
        self.client = TestClient(main.app)
        engines = {"FUTURES": self.bot, "SPOT": bot_engine.TradingBotEngine("SPOT", self.bot.state_file + ".spot")}
        patcher = patch.object(main.bot_manager, "engines", engines)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dashboard_reports_closed_trade_and_live_risk(self):
        self.bot.max_daily_loss_usdt = 10
        self.api.create_futures_order.return_value = regression.fill()
        self.bot._execute_entry("BTCUSDT", "LONG", 100, "test")
        self.api.get_futures_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": 2}]
        self.api.create_futures_order.return_value = regression.fill(quote="196")
        self.assertTrue(self.bot._close_position("BTCUSDT", "STOP_LOSS")["success"])
        self.api.get_futures_positions.return_value = []
        self.api.create_futures_order.return_value = regression.fill()
        self.bot._execute_entry("ETHUSDT", "LONG", 100, "test")

        data = self.client.get("/api/dashboard?market=FUTURES&days=7").json()
        self.assertEqual(data["kpis"]["trades"], 1)
        self.assertAlmostEqual(data["kpis"]["net_pnl"], -4.196)
        self.assertEqual(data["by_reason"][0]["reason"], "STOP_LOSS")
        live = data["bots"][0]
        self.assertEqual((live["market"], live["positions"]), ("FUTURES", 1))
        self.assertAlmostEqual(live["used_margin"], 20)
        self.assertAlmostEqual(live["day_pnl"], -4.196)
        self.assertEqual(live["max_daily_loss_usdt"], 10)

        both = self.client.get("/api/dashboard").json()
        self.assertEqual([b["market"] for b in both["bots"]], ["FUTURES", "SPOT"])
        self.assertEqual(self.client.get("/api/dashboard?market=SPOT").json()["kpis"]["trades"], 0)
        self.assertEqual(self.client.get("/api/dashboard?market=XYZ").status_code, 422)

    def test_history_survives_restart_beyond_fifty_fills(self):
        self.bot.closed_trades = [fill("BTCUSDT", "2026-11-12 10:00:00", 1.0, entry_time=f"t{i}") for i in range(120)]
        self.bot.save_state()
        self.assertEqual(len(bot_engine.TradingBotEngine().closed_trades), 120)


if __name__ == "__main__":
    unittest.main()
