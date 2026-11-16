import json
import os
import tempfile
import unittest
from unittest.mock import patch

_STATE = tempfile.TemporaryDirectory()
os.environ["STATE_DIR"] = _STATE.name          # before importing main: keep the real trading.db untouched

from fastapi.testclient import TestClient      # noqa: E402

import auth                                    # noqa: E402
import main                                    # noqa: E402
from engine import TradingEngine               # noqa: E402
from journal import Journal                    # noqa: E402
from tests.helpers import FakeExchange, candles_from_closes   # noqa: E402


def tearDownModule():
    main.engine.journal.close()
    _STATE.cleanup()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        journal = Journal(os.path.join(self.tmp.name, "trading.db"))
        self.addCleanup(journal.close)
        self.exchange = FakeExchange()
        self.engine = TradingEngine(self.exchange, journal)
        self.engine.supervise = lambda: None
        for target, value in ((main, "engine"), (auth, "settings")):
            replacement = self.engine if value == "engine" else auth.AuthConfig()
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)       # no lifespan: no threads, no process lock

    def test_status_and_start_stop(self):
        status = self.client.get("/api/status").json()
        self.assertFalse(status["is_running"])
        self.assertEqual(status["config"]["risk"]["risk_pct"], 0.75)
        self.assertEqual(self.client.post("/api/bot/start").json()["success"], True)
        status = self.client.get("/api/status").json()
        self.assertTrue(status["is_running"])
        self.assertEqual(status["account"]["equity"], 1000.0)
        self.assertEqual(status["risk"]["risk_per_trade_usdt"], 7.5)
        self.client.post("/api/bot/stop")
        self.assertFalse(self.client.get("/api/status").json()["is_running"])

    def test_telegram_status_and_test_message(self):
        self.assertEqual(self.client.get("/api/status").json()["telegram"], {"configured": False})
        self.assertEqual(self.client.post("/api/notify/test").status_code, 400)
        sent = []

        class Reply:
            status_code = 200

            def json(self):
                return {"ok": True}

        from notifier import TelegramNotifier
        self.engine.notifier = TelegramNotifier("1:token", "42", post=lambda url, json=None, timeout=None: sent.append(json) or Reply())
        response = self.client.post("/api/notify/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((len(sent), sent[0]["chat_id"]), (1, "42"))
        status = self.client.get("/api/status").json()["telegram"]
        self.assertTrue(status["configured"])
        self.assertNotIn("token", json.dumps(self.client.get("/api/status").json()))      # the token never leaves the server
        config = self.client.get("/api/config").json()["config"]
        self.assertEqual(config["notify"], {"trades": True, "risk": True, "health": True, "daily": True,
                                            "signals": False, "warnings": False})

    def test_start_refusal_is_a_400_with_the_reason(self):
        self.exchange.position["LINKUSDT"] = 5.0
        response = self.client.post("/api/bot/start")
        self.assertEqual(response.status_code, 400)
        self.assertIn("LINKUSDT", response.json()["detail"])

    def test_config_is_validated_and_saved(self):
        payload = self.client.get("/api/config").json()
        self.assertEqual([s["key"] for s in payload["setups"]][0], "THRUST_BREAKOUT")
        self.assertTrue(all(s["evidence"] and s["case"] for s in payload["setups"]))
        config = payload["config"]
        config["strategy"]["setups"] = ["NOPE"]
        self.assertEqual(self.client.post("/api/config", json=config).status_code, 422)
        config["strategy"]["setups"] = ["TREND_PULLBACK", "THRUST_BREAKOUT"]
        config["risk"]["risk_pct"] = 5
        self.assertEqual(self.client.post("/api/config", json=config).status_code, 422)
        config["risk"]["risk_pct"] = 0.5
        config["watchlist"] = ["btcusdt", "ethusdt"]
        response = self.client.post("/api/config", json=config)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.engine.config.risk.risk_pct, 0.5)
        self.assertEqual(self.engine.config.watchlist, ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(self.engine.config.strategy.setups, ["THRUST_BREAKOUT", "TREND_PULLBACK"])
        config["watchlist"] = ["not a symbol"]
        self.assertEqual(self.client.post("/api/config", json=config).status_code, 422)
        config["watchlist"], config["unknown"] = ["BTCUSDT"], 1
        self.assertEqual(self.client.post("/api/config", json=config).status_code, 422)

    def test_dashboard_reports_closed_trades_and_equity(self):
        self.engine.journal.commit({}, [{
            "position_id": "p1", "symbol": "ETHUSDT", "direction": "LONG", "entry_time": 1, "exit_time": 2_000_000_000_000,
            "entry_price": 100, "exit_price": 104, "quantity": 1, "gross_pnl": 4, "fees": 0.1, "net_pnl": 3.9,
            "risk_usdt": 2, "r_multiple": 1.95, "reason": "TRAILING_STOP", "bars": 9, "setup": "THRUST_BREAKOUT"}])
        self.engine.journal.add_equity(1000, 1_999_999_000_000)
        self.engine.journal.add_equity(1003.9, 2_000_000_000_000)
        data = self.client.get("/api/dashboard?days=0").json()
        self.assertEqual((data["summary"]["trades"], data["summary"]["expectancy_r"]), (1, 1.95))
        self.assertTrue(data["summary"]["low_sample"])
        self.assertEqual(len(data["equity_curve"]), 2)
        self.assertEqual(data["trades"][0]["reason"], "TRAILING_STOP")
        self.assertEqual(data["by_setup"][0]["setup"], "THRUST_BREAKOUT")
        self.assertEqual((data["summary"]["estimated_trades"], data["execution"]["settled_trades"]), (1, 0))
        self.assertEqual(data["execution"]["assumed_fee_rate_pct"], 0.05)
        self.assertEqual(data["expectation"][0]["verdict"], "TOO_FEW")

    def test_backtest_endpoint_uses_saved_config_and_rejects_bad_input(self):
        candles = candles_from_closes([100.0] * 400, step=4 * 3600)
        data = {"start_ms": candles[150]["time"] * 1000, "end_ms": candles[-1]["close_time_ms"] + 1,
                "candles": {"4h": {"ETHUSDT": candles}}, "funding": {}, "funding_ok": True}
        with patch.object(main.backtest_module, "load_data", return_value=data):
            response = self.client.post("/api/backtest", json={"symbols": ["ETHUSDT"], "days": 10})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total_trades"], 0)                       # a flat market has no trend to trade
        self.assertEqual(body["config"]["risk"]["risk_pct"], 0.75)
        self.assertEqual(body["config"]["strategy"]["setups"], ["THRUST_BREAKOUT"])
        self.assertEqual(self.client.post("/api/backtest", json={"symbols": [], "days": 10}).status_code, 422)
        self.assertEqual(self.client.post("/api/backtest", json={"symbols": ["ETHUSDT"], "days": 10,
                                                                 "risk": {"risk_pct": 9}}).status_code, 422)


class AuthTests(unittest.TestCase):
    def client(self, config):
        patcher = patch.object(auth, "settings", config)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(main.app, follow_redirects=False)

    def test_everything_needs_a_session_when_login_is_enabled(self):
        client = self.client(auth.AuthConfig(username="demo", password="local-test-pass", secret=b"k"))
        self.assertEqual(client.get("/api/status").status_code, 401)
        self.assertEqual(client.get("/").status_code, 303)
        self.assertEqual(client.get("/static/app.js").status_code, 303)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.post("/api/auth/login", json={"username": "demo", "password": "wrong"}).status_code, 401)
        self.assertEqual(client.post("/api/auth/login",
                                     json={"username": "demo", "password": "local-test-pass"}).status_code, 200)
        self.assertEqual(client.get("/api/config").status_code, 200)
        self.assertEqual(client.post("/api/bot/stop", headers={"sec-fetch-site": "cross-site"}).status_code, 403)

    def test_public_deployment_without_credentials_is_locked(self):
        client = self.client(auth.AuthConfig(required=True))
        self.assertEqual(client.get("/api/status").status_code, 503)
        self.assertEqual(client.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
