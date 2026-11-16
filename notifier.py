"""Telegram notifications.

Messages are queued and sent by a background thread, so a slow or unreachable Telegram can
never delay the trading loop. Credentials come from the environment only:

    TELEGRAM_BOT_TOKEN   the token @BotFather gives for your bot
    TELEGRAM_CHAT_ID     the chat (or channel) the bot should write to

The token is never logged, stored in the database or returned by the API. Error text shown
to the user comes from Telegram's own reply, never from the request (whose URL holds the token).
"""
import os
import threading
import time
from collections import deque
from typing import Callable, Optional, Tuple

import requests

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_TEXT = 4000                 # Telegram accepts 4096 characters per message
MAX_QUEUE = 200                 # beyond this the oldest unsent messages are dropped
DEDUPE_SEC = 600                # the same text is not repeated within this window
ATTEMPTS = 3


class TelegramNotifier:
    pause_sec = 1.0             # between messages: Telegram allows about one per second per chat
    retry_sec = 3.0

    def __init__(self, token: str = "", chat_id: str = "", post: Optional[Callable] = None):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self._post = post or requests.post
        self.queue = deque(maxlen=MAX_QUEUE)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.recent = {}                        # text -> when it was queued
        self.thread: Optional[threading.Thread] = None
        self.stopping = False
        self.sent = 0
        self.last_error: Optional[str] = None

    @classmethod
    def from_env(cls):
        return cls(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        return {"configured": self.enabled, "sent": self.sent, "last_error": self.last_error}

    # ---------------- sending ----------------

    def send(self, text: str) -> bool:
        """Queue a message. Returns at once and never raises; False when nothing was queued."""
        if not self.enabled or not text:
            return False
        now = time.monotonic()
        with self.lock:
            self.recent = {t: at for t, at in self.recent.items() if now - at < DEDUPE_SEC}
            if text in self.recent:
                return False
            self.recent[text] = now
            self.queue.append(text[:MAX_TEXT])
            self.idle.clear()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
        self.wake.set()
        return True

    def send_now(self, text: str) -> Tuple[bool, Optional[str]]:
        """Send one message and wait for the answer. For the 'send a test message' button."""
        if not self.enabled:
            return False, "Chưa cấu hình TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID."
        ok, _, error = self._deliver(text[:MAX_TEXT])
        return ok, error

    def _deliver(self, text: str) -> Tuple[bool, Optional[float], Optional[str]]:
        """(sent, seconds Telegram asked us to wait, error text safe to show)."""
        try:
            response = self._post(API_URL.format(token=self.token), timeout=10,
                                  json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
            data = response.json()
        except (requests.RequestException, ValueError):
            # Deliberately no exception text: it would contain the request URL, and with it the token.
            return False, None, "Không kết nối được tới Telegram."
        if response.status_code == 200 and data.get("ok"):
            return True, None, None
        wait = (data.get("parameters") or {}).get("retry_after")
        return False, float(wait) if wait else None, f"Telegram từ chối: {data.get('description', response.status_code)}"

    def _run(self):
        while not self.stopping:
            with self.lock:
                text = self.queue.popleft() if self.queue else None
                if text is None:
                    self.idle.set()
            if text is None:
                self.wake.wait(30)
                self.wake.clear()
                continue
            for attempt in range(ATTEMPTS):
                ok, wait, error = self._deliver(text)
                if ok:
                    self.sent += 1
                    self.last_error = None
                    break
                self.last_error = error
                if attempt + 1 < ATTEMPTS:
                    time.sleep(min(wait or self.retry_sec, 30))
            time.sleep(self.pause_sec)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued has been sent or given up on."""
        return self.idle.wait(timeout)

    def close(self):
        self.flush(3.0)
        self.stopping = True
        self.wake.set()
