import os
import sqlite3
import tempfile
import unittest

import metrics
from journal import Journal
from strategy import SETUPS


def trade(r, setup="THRUST_BREAKOUT", settled=1, **more):
    row = {"symbol": "ETHUSDT", "direction": "LONG", "setup": setup, "entry_price": 100.0, "exit_price": 100.0 + 2 * r,
           "quantity": 5.0, "signal_price": 100.0, "gross_pnl": 10.0 * r, "fees": 0.4, "funding": -0.1,
           "net_pnl": 10.0 * r - 0.5, "risk_usdt": 10.0, "r_multiple": r - 0.05, "reason": "x", "bars": 3,
           "settled": settled}
    row.update(more)
    return row


class SummaryTests(unittest.TestCase):
    def test_summary_counts_funding_and_estimated_trades(self):
        summary = metrics.summarise([trade(2), trade(-1), trade(-1, settled=0)])
        self.assertEqual((summary["trades"], summary["wins"], summary["losses"]), (3, 1, 2))
        self.assertAlmostEqual(summary["funding"], -0.3)
        self.assertEqual(summary["estimated_trades"], 1)
        self.assertEqual(summary["max_consecutive_losses"], 2)

    def test_backtest_trades_without_a_settled_flag_are_not_called_estimates(self):
        row = trade(1)
        del row["settled"]
        self.assertEqual(metrics.summarise([row])["estimated_trades"], 0)


class ExecutionQualityTests(unittest.TestCase):
    def test_real_fee_rate_and_funding_use_only_settled_trades(self):
        trades = [trade(1, fees=0.8, exit_price=100.0), trade(-1, fees=5.0, settled=0, exit_price=100.0)]
        quality = metrics.execution_quality(trades, 0.0005, 0.0002)
        self.assertEqual((quality["settled_trades"], quality["estimated_trades"]), (1, 1))
        self.assertAlmostEqual(quality["fee_rate_pct"], 0.8 / (5 * 200) * 100)       # 0.08% of turnover
        self.assertEqual(quality["assumed_fee_rate_pct"], 0.05)
        self.assertAlmostEqual(quality["funding_usdt"], -0.1)
        self.assertAlmostEqual(quality["funding_r_per_trade"], -0.01)              # -0.1 on 10 at risk

    def test_entry_slippage_is_signed_against_the_trade(self):
        trades = [trade(1, entry_price=100.1),                                      # long filled higher: worse
                  trade(1, direction="SHORT", entry_price=100.1),                   # short filled higher: better
                  trade(1, entry_price=100.3), trade(1, signal_price=None)]
        quality = metrics.execution_quality(trades, 0.0005, 0.0002)
        self.assertEqual(quality["slippage_samples"], 3)
        self.assertAlmostEqual(quality["entry_slippage_pct"], (0.1 - 0.1 + 0.3) / 3, places=4)

    def test_nothing_to_report_without_trades(self):
        quality = metrics.execution_quality([], 0.0005, 0.0002)
        self.assertIsNone(quality["fee_rate_pct"])
        self.assertIsNone(quality["entry_slippage_pct"])
        self.assertIsNone(quality["funding_r_per_trade"])


class ExpectationTests(unittest.TestCase):
    def verdict(self, rs, setup="THRUST_BREAKOUT"):
        (row,) = metrics.versus_expectation([trade(r, setup) for r in rs], SETUPS)
        return row

    def test_small_samples_are_not_judged(self):
        row = self.verdict([-1.0] * 19)
        self.assertEqual((row["verdict"], row["trades"]), ("TOO_FEW", 19))

    def test_range_narrows_with_more_trades_and_flags_results_outside_it(self):
        few, many = self.verdict([0.15] * 25), self.verdict([0.15] * 400)
        self.assertEqual((few["verdict"], many["verdict"]), ("WITHIN", "WITHIN"))
        self.assertGreater(few["high_r"] - few["low_r"], many["high_r"] - many["low_r"])
        expected = SETUPS["THRUST_BREAKOUT"]
        self.assertAlmostEqual(many["high_r"], expected.expected_r + 2 * expected.r_sd / 20, places=3)
        self.assertEqual(self.verdict([-0.75] * 100)["verdict"], "BELOW")           # mean -0.8R: far under +0.1R
        self.assertEqual(self.verdict([1.5] * 100)["verdict"], "ABOVE")

    def test_each_setup_is_compared_with_its_own_research_numbers(self):
        rows = metrics.versus_expectation([trade(0.0, "RANGE_REVERSION")] * 30 + [trade(0.0)] * 30, SETUPS)
        self.assertEqual([row["setup"] for row in rows], ["THRUST_BREAKOUT", "RANGE_REVERSION"])
        self.assertEqual([row["expected_r"] for row in rows], [0.10, -0.16])


class JournalMigrationTests(unittest.TestCase):
    def test_database_from_the_first_release_gains_the_new_columns(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)             # runs after the journal is closed (cleanups are LIFO)
        path = os.path.join(folder.name, "trading.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE trades (
            position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            entry_time INTEGER NOT NULL, exit_time INTEGER NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL NOT NULL, quantity REAL NOT NULL,
            gross_pnl REAL NOT NULL, fees REAL NOT NULL, net_pnl REAL NOT NULL,
            risk_usdt REAL NOT NULL, r_multiple REAL NOT NULL, reason TEXT NOT NULL, bars INTEGER NOT NULL)""")
        old.execute("INSERT INTO trades VALUES ('p0','ETHUSDT','LONG',1,2,100,98,1,-2,0.1,-2.1,2,-1.05,'STOP_LOSS',3)")
        old.commit()
        old.close()
        journal = Journal(path)
        self.addCleanup(journal.close)
        (row,) = journal.trades()
        self.assertEqual((row["setup"], row["settled"], row["funding"], row["order_ids"], row["signal_price"]),
                         ("TREND_PULLBACK", 0, 0, "[]", None))
        journal.settle_trade("p0", gross_pnl=-2, fees=0.08, funding=-0.01, net_pnl=-2.09, r_multiple=-1.045, settled=1)
        self.assertEqual(journal.unsettled_trades(0), [])
        self.assertEqual(journal.trades()[0]["fees"], 0.08)


if __name__ == "__main__":
    unittest.main()
