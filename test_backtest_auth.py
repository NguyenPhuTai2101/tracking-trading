import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import backtest
import main
from strategy import decide_entry, exit_prices

STEP = 900  # 15m in seconds


def make_candles(prices, start=1_700_000_000 // STEP * STEP):
    """prices: list of (open, high, low, close)."""
    return [{"time": start + i * STEP, "open": o, "high": h, "low": l, "close": c, "volume": 1.0,
             "close_time_ms": (start + (i + 1) * STEP) * 1000 - 1, "closed": True}
            for i, (o, h, l, c) in enumerate(prices)]


def flat(n, price=100.0):
    return [(price, price, price, price)] * n


class BacktestEngineTests(unittest.TestCase):
    def setUp(self):
        # Signal only on chosen candle indices; the indicator itself is tested elsewhere.
        self.signal_at = {}
        self.calls = 0
        def fake_decide(strategy, analysis, allow_short):
            index = analysis["index"]
            return self.signal_at.get(index)
        patches = [patch.object(backtest, "analyze_market_signals", lambda window: {"index": window[-1]["i"]}),
                   patch.object(backtest, "decide_entry", fake_decide)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_bt(self, prices, signals, **overrides):
        candles = make_candles(prices)
        for i, c in enumerate(candles):
            c["i"] = i
        self.signal_at = signals
        params = dict(symbols=["BTCUSDT"], leverage=10, trade_amount_usdt=20, take_profit_pct=3,
                      stop_loss_pct=1.5, fee_rate=0.0005, slippage_bps=0)
        params.update(overrides)
        config = backtest.BacktestConfig(**params)
        return backtest.run_backtest(config, {"BTCUSDT": candles})

    def test_take_profit_fills_next_open_and_charges_fees_both_sides(self):
        w = backtest.WINDOW
        prices = flat(w) + [(100, 100, 100, 100), (100, 100.5, 99.9, 100.4)] + flat(3, 100.4)
        result = self.run_bt(prices, {w - 1: ("LONG", "test")})
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "TAKE_PROFIT")
        self.assertAlmostEqual(trade["entry_price"], 100)
        self.assertAlmostEqual(trade["exit_price"], 100.3)          # TP = 3% ROE / 10x = 0.3%
        self.assertAlmostEqual(trade["gross_pnl"], 0.6)             # 200 notional * 0.3%
        self.assertAlmostEqual(trade["fees"], 200 * 0.0005 + 200.6 * 0.0005)
        self.assertAlmostEqual(result.summary["net_pnl"], round(0.6 - trade["fees"], 4))

    def test_candle_touching_both_levels_counts_as_stop_loss(self):
        w = backtest.WINDOW
        prices = flat(w) + [(100, 101, 99, 100)] + flat(2)
        result = self.run_bt(prices, {w - 1: ("LONG", "test")})
        self.assertEqual(result.trades[0]["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(result.trades[0]["exit_price"], 99.85)
        self.assertEqual(result.summary["ambiguous_candles"], 1)

    def test_gap_through_stop_fills_at_open_and_short_mirrors(self):
        w = backtest.WINDOW
        prices = flat(w) + [(100, 100, 100, 100), (101, 101, 101, 101)] + flat(2, 101)
        result = self.run_bt(prices, {w - 1: ("SHORT", "test")})
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["exit_price"], 101)             # worse than the 100.15 stop
        self.assertAlmostEqual(trade["gross_pnl"], -2.0)

    def test_entry_skipped_when_next_open_moves_more_than_one_percent(self):
        w = backtest.WINDOW
        prices = flat(w) + [(102, 102, 102, 102)] + flat(2, 102)
        result = self.run_bt(prices, {w - 1: ("LONG", "test")})
        self.assertEqual(result.summary["trades"], 0)
        self.assertEqual(result.summary["gap_skipped_entries"], 1)

    def test_cooldown_blocks_reentry(self):
        w = backtest.WINDOW
        prices = flat(w) + [(100, 100, 99, 99.9)] + flat(6, 99.9)
        signals = {i: ("LONG", "test") for i in range(w - 1, w + 6)}
        no_cooldown = self.run_bt(prices, signals)
        with_cooldown = self.run_bt(prices, signals, cooldown_candles=3)
        stop_candle = make_candles(prices)[w]["time"]
        # Without cooldown the bot re-signals on the stop candle and enters on the next open.
        self.assertEqual(no_cooldown.trades[1]["entry_time"], stop_candle + STEP)
        self.assertEqual(with_cooldown.trades[1]["entry_time"], stop_candle + 4 * STEP)

    def test_breakeven_winrate_includes_fees(self):
        result = self.run_bt(flat(backtest.WINDOW + 2), {})
        # 20 USDT margin, 10x: TP +0.6, SL -0.3, round trip fees 0.2 -> 0.5 / 0.9
        self.assertAlmostEqual(result.summary["breakeven_winrate_pct"], 55.56, places=2)

    def test_config_validation_and_spot_has_no_leverage(self):
        with self.assertRaises(ValueError):
            backtest.BacktestConfig(symbols=[], days=10)
        with self.assertRaises(ValueError):
            backtest.BacktestConfig(symbols=["BTCUSDT"], days=500)
        spot = backtest.BacktestConfig(symbols=["BTCUSDT"], market="SPOT", leverage=10)
        self.assertEqual(spot.leverage, 1)
        self.assertEqual(spot.fee_rate, 0.001)


class StrategyTests(unittest.TestCase):
    def test_shorts_only_when_allowed(self):
        analysis = {"rsi": 75, "ema_cross": "NONE", "ema9": 1, "ema21": 1, "net_score": 0, "action": "NEUTRAL"}
        self.assertEqual(decide_entry("RSI_SCALPING", analysis, True)[0], "SHORT")
        self.assertIsNone(decide_entry("RSI_SCALPING", analysis, False))

    def test_exit_prices_match_roe_convention(self):
        tp, sl = exit_prices(100, "LONG", 3, 1.5, 10)
        self.assertAlmostEqual(tp, 100.3)
        self.assertAlmostEqual(sl, 99.85)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.config = auth.AuthConfig("owner", "s3cret-pass", b"k" * 32, required=True)
        for p in (patch.object(auth, "settings", self.config), patch.object(main.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app, base_url="https://testserver")

    def login(self, password="s3cret-pass", **headers):
        return self.client.post("/api/auth/login", json={"username": "owner", "password": password}, headers=headers)

    def test_everything_but_health_and_login_requires_session(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/api/bots/status").status_code, 401)
        self.assertEqual(self.client.post("/api/bot/start?market=FUTURES").status_code, 401)
        self.assertEqual(self.client.get("/static/js/app.js", follow_redirects=False).status_code, 303)
        page = self.client.get("/", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/login")

    def test_login_sets_strict_httponly_cookie_and_logout_revokes(self):
        self.assertEqual(self.login("wrong").status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        for flag in ("httponly", "samesite=strict", "secure"):
            self.assertIn(flag, cookie)
        token = self.client.cookies.get(auth.COOKIE_NAME)
        self.assertEqual(self.client.get("/api/auth/status").json(), {"enabled": True, "authenticated": True})
        with patch.object(main.bot_manager.engines["FUTURES"], "get_status", return_value={"ok": 1}):
            self.assertEqual(self.client.get("/api/bot/status?market=FUTURES").status_code, 200)
        self.client.post("/api/auth/logout")
        self.client.cookies.set(auth.COOKIE_NAME, token)
        self.assertEqual(self.client.get("/api/bots/status").status_code, 401)

    def test_forged_or_expired_tokens_rejected(self):
        self.assertFalse(self.config.verify("9999999999.abc.deadbeef"))
        other = auth.AuthConfig("owner", "s3cret-pass", b"x" * 32)
        self.assertFalse(self.config.verify(other.issue()))
        with patch.object(auth.time, "time", return_value=0):
            old = self.config.issue()
        self.assertFalse(self.config.verify(old))

    def test_lockout_after_repeated_failures(self):
        for _ in range(auth.MAX_FAILURES):
            self.assertEqual(self.login("bad").status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_cross_origin_write_rejected(self):
        self.login()
        response = self.client.post("/api/bot/stop?market=FUTURES", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/bot/stop?market=FUTURES", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_same_origin_write_allowed_behind_proxy(self):
        # Railway's proxy may hand the app an internal Host; the browser's own label decides.
        self.login()
        engine = main.bot_manager.engines["FUTURES"]
        with patch.object(engine, "stop", return_value={"success": True}):
            response = self.client.post("/api/bot/stop?market=FUTURES", headers={
                "Origin": "https://my-app.up.railway.app", "Host": "10.0.0.5:8080", "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(response.status_code, 200)
            response = self.client.post("/api/bot/stop?market=FUTURES", headers={
                "Origin": "https://my-app.up.railway.app", "X-Forwarded-Host": "my-app.up.railway.app"})
            self.assertEqual(response.status_code, 200)

    def test_public_deploy_without_credentials_is_locked(self):
        with patch.object(auth, "settings", auth.AuthConfig(required=True)):
            self.assertEqual(self.client.get("/api/bots/status").status_code, 503)
            self.assertEqual(self.client.get("/").status_code, 503)
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_local_run_without_credentials_stays_open(self):
        with patch.object(auth, "settings", auth.AuthConfig(required=False)), \
                patch.object(main.bot_manager.engines["FUTURES"], "get_status", return_value={"ok": 1}):
            self.assertEqual(self.client.get("/api/bot/status?market=FUTURES").status_code, 200)


if __name__ == "__main__":
    unittest.main()
