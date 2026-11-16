import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests

import engine as engine_module
import strategy
from engine import BotConfig, NotifyConfig, TradingEngine
from journal import Journal
from notifier import TelegramNotifier
from strategy import Signal, StrategyParams
from tests.helpers import FakeExchange

TOKEN = "123456:SECRET-TOKEN"


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"ok": True}

    def json(self):
        return self._body


class FakeTelegram:
    """Stands in for requests.post: records what would be sent, answers from a script."""

    def __init__(self, *replies):
        self.calls, self.replies = [], list(replies)

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, **json})
        reply = self.replies.pop(0) if self.replies else Reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def texts(self):
        return [c["text"] for c in self.calls]


def notifier(*replies):
    telegram = FakeTelegram(*replies)
    out = TelegramNotifier(TOKEN, "42", post=telegram)
    out.pause_sec = out.retry_sec = 0
    return out, telegram


class NotifierTests(unittest.TestCase):
    def test_messages_are_sent_in_order_to_the_configured_chat(self):
        bot, telegram = notifier()
        self.assertTrue(bot.send("một"))
        self.assertTrue(bot.send("hai"))
        self.assertTrue(bot.flush())
        self.assertEqual(telegram.texts, ["một", "hai"])
        self.assertEqual({c["chat_id"] for c in telegram.calls}, {"42"})
        self.assertIn(TOKEN, telegram.calls[0]["url"])
        self.assertEqual(bot.status(), {"configured": True, "sent": 2, "last_error": None})

    def test_unconfigured_notifier_does_nothing(self):
        telegram = FakeTelegram()
        for token, chat in (("", "42"), (TOKEN, ""), ("", "")):
            bot = TelegramNotifier(token, chat, post=telegram)
            self.assertFalse(bot.enabled)
            self.assertFalse(bot.send("x"))
            self.assertEqual(bot.send_now("x")[0], False)
        self.assertEqual(telegram.calls, [])

    def test_repeated_text_is_not_sent_twice_in_a_row(self):
        bot, telegram = notifier()
        self.assertTrue(bot.send("Bỏ qua SUIUSDT: lỗi mạng"))
        self.assertFalse(bot.send("Bỏ qua SUIUSDT: lỗi mạng"))
        bot.flush()
        self.assertEqual(len(telegram.calls), 1)

    def test_failed_delivery_is_retried_and_reported_without_the_token(self):
        bot, telegram = notifier(requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage refused"),
                                 Reply(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 0}}),
                                 Reply())
        bot.send("quan trọng")
        self.assertTrue(bot.flush())
        self.assertEqual(telegram.texts, ["quan trọng"] * 3)
        self.assertEqual((bot.sent, bot.last_error), (1, None))

        bot, telegram = notifier(*[requests.ConnectionError(f"bot{TOKEN} unreachable")] * 3)
        bot.send("không tới nơi")
        bot.flush()
        self.assertEqual((bot.sent, len(telegram.calls)), (0, 3))               # gave up after three attempts
        self.assertNotIn(TOKEN, bot.last_error)
        self.assertNotIn("SECRET", bot.last_error)

    def test_send_now_returns_telegrams_own_explanation(self):
        bot, _ = notifier(Reply(400, {"ok": False, "description": "Bad Request: chat not found"}))
        ok, error = bot.send_now("thử")
        self.assertFalse(ok)
        self.assertIn("chat not found", error)
        ok, error = notifier()[0].send_now("thử")
        self.assertEqual((ok, error), (True, None))

    def test_long_text_is_cut_and_a_full_queue_drops_the_oldest(self):
        bot, telegram = notifier()
        bot.send("x" * 10_000)
        bot.flush()
        self.assertEqual(len(telegram.texts[0]), 4000)
        idle = TelegramNotifier(TOKEN, "42", post=telegram)
        idle._run = lambda: None                                # a sender that never drains the queue
        for i in range(250):
            idle.send(f"tin {i}")
        self.assertEqual((len(idle.queue), idle.queue[0]), (200, "tin 50"))


class EngineNotificationTests(unittest.TestCase):
    SYM = "ETHUSDT"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        journal = Journal(os.path.join(self.tmp.name, "trading.db"))
        self.addCleanup(journal.close)
        self.exchange = FakeExchange()
        self.exchange.price[self.SYM] = 100.0
        self.bot, self.telegram = notifier()
        self.engine = TradingEngine(self.exchange, journal, notifier=self.bot)
        self.engine.supervise = lambda: None
        self.assertTrue(self.engine.start()["success"])

    def sent(self):
        self.bot.flush()
        return self.telegram.texts

    def test_trade_open_and_close_are_announced_with_the_result(self):
        signal = Signal("LONG", 100.0, 98.0, 1.0, "UP", "test", "THRUST_BREAKOUT", strategy.trail_exit(4.0))
        self.engine._execute_entry(self.SYM, signal)
        self.exchange.price[self.SYM] = 97.9
        self.engine._tick()
        texts = self.sent()
        self.assertTrue(any("Mở LONG ETHUSDT" in t and "Phá vỡ có lực" in t for t in texts), texts)
        self.assertTrue(any("Đóng LONG ETHUSDT" in t and "R" in t for t in texts), texts)
        self.assertFalse(any("rủi ro lệnh" in t for t in texts))                 # sizing detail is INFO: log only

    def test_circuit_breaker_and_drawdown_halt_are_announced(self):
        self.exchange.equity = 975.0
        self.engine._refresh_account(force=True)
        self.assertTrue(any("Lỗ trong ngày" in t for t in self.sent()))
        self.exchange.equity = 790.0
        self.engine._refresh_account(force=True)
        texts = self.sent()
        self.assertTrue(any("Drawdown" in t for t in texts), texts)
        self.assertTrue(any("Bot dừng mở lệnh" in t for t in texts), texts)

    def test_switches_decide_what_is_sent(self):
        self.engine.stop()
        self.engine.update_config(BotConfig(notify=NotifyConfig(trades=False, risk=False, signals=True)))
        self.assertTrue(self.engine.start()["success"])
        self.telegram.calls.clear()
        self.engine.add_log("SUCCESS", "một lệnh")
        self.engine.add_log("RISK", "một ngắt mạch")
        self.engine.add_log("CASE", "một case")
        self.engine.add_log("WARN", "một cảnh báo")
        self.engine.add_log("INFO", "chi tiết")
        self.assertEqual(self.sent(), ["một case"])

    def test_losing_the_exchange_alerts_once_and_recovery_once(self):
        self.exchange.get_futures_account_info = lambda: {"success": False, "error": "timeout"}
        clock = [1000.0]
        self.engine.account_checked = 0.0                       # the cached account read is older than the fake clock
        with patch.object(engine_module.time, "monotonic", lambda: clock[0]):
            for step in (0, 120, 290, 310, 600, 900):          # 5 minutes are passed at 310
                clock[0] = 1000.0 + step
                self.engine._tick()
            alerts = [t for t in self.sent() if "không đọc được sàn" in t]
            self.assertEqual(len(alerts), 1)
            self.assertIn("SL đã đặt trên sàn vẫn có hiệu lực", alerts[0])
            del self.exchange.get_futures_account_info         # the exchange answers again
            clock[0] += 60
            self.engine._tick()
            self.engine._tick()
        self.assertEqual(sum("đã kết nối lại" in t for t in self.sent()), 1)

    def test_daily_summary_is_sent_when_the_utc_day_rolls_over(self):
        self.engine._refresh_account(force=True)
        self.engine.risk_state.update(day="2026-11-15", day_start_equity=1000.0)
        day_ms = int(datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp() * 1000)
        self.engine.journal.commit(self.engine.journal.load_state(), [{
            "position_id": "y1", "symbol": "SOLUSDT", "direction": "LONG", "entry_time": day_ms - 1, "exit_time": day_ms,
            "entry_price": 50, "exit_price": 51, "quantity": 10, "gross_pnl": 10, "fees": 0.5, "net_pnl": 9.5,
            "risk_usdt": 7.5, "r_multiple": 1.27, "reason": "TRAILING_STOP", "bars": 5, "setup": "THRUST_BREAKOUT"}])
        self.exchange.equity = 1009.5
        self.engine._refresh_account(force=True)                 # today is a later UTC day
        (summary,) = [t for t in self.sent() if t.startswith("📊")]
        self.assertIn("Tổng kết 2026-11-15", summary)
        self.assertIn("+0.95% trong ngày", summary)
        self.assertIn("Lệnh đã đóng: 1, ròng +9.50 USDT (+1.27R)", summary)
        self.assertIn("Vị thế đang mở: 0", summary)
        self.engine._refresh_account(force=True)
        self.assertEqual(sum(t.startswith("📊") for t in self.sent()), 1)       # once per day

    def test_bot_runs_the_same_without_a_notifier(self):
        journal = Journal(os.path.join(self.tmp.name, "other.db"))
        self.addCleanup(journal.close)
        plain = TradingEngine(self.exchange, journal)
        plain.add_log("SUCCESS", "không ai nghe")
        self.assertEqual(plain.get_status()["config"]["notify"]["trades"], True)


if __name__ == "__main__":
    unittest.main()
