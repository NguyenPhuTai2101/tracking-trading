"""Single-user login with a signed session cookie.

Enabled when APP_USERNAME and APP_PASSWORD are set. When they are missing the app is
open only for local use; on Railway (or with REQUIRE_AUTH=1) it refuses every request
instead of exposing trading controls to the internet.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

COOKIE_NAME = "tt_session"
SESSION_TTL_SEC = 7 * 86_400
MAX_FAILURES = 5
LOCKOUT_SEC = 300


@dataclass
class AuthConfig:
    username: str = ""
    password: str = ""
    secret: bytes = b""
    required: bool = False
    revoked: Dict[str, int] = field(default_factory=dict)       # nonce -> expiry
    failures: Dict[str, Tuple[int, float]] = field(default_factory=dict)  # ip -> (count, locked_until)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls):
        username = os.getenv("APP_USERNAME", "")
        password = os.getenv("APP_PASSWORD", "")
        explicit = os.getenv("SESSION_SECRET", "")
        # Without SESSION_SECRET, sessions are tied to the credentials: changing the
        # password logs every browser out.
        secret = explicit.encode() if explicit else hashlib.sha256(
            f"tracking-trading-session|{username}|{password}".encode()).digest()
        on_railway = any(os.getenv(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))
        required = on_railway or os.getenv("REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        return cls(username, password, secret, required)

    @property
    def enabled(self):
        return bool(self.username and self.password)

    @property
    def misconfigured(self):
        return self.required and not self.enabled

    # ---------------- credentials ----------------

    def check_credentials(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pass_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return self.enabled and user_ok and pass_ok

    def locked_for(self, ip: str) -> int:
        with self.lock:
            count, until = self.failures.get(ip, (0, 0.0))
            return max(0, int(until - time.time()))

    def record_failure(self, ip: str):
        with self.lock:
            count, until = self.failures.get(ip, (0, 0.0))
            count += 1
            if count >= MAX_FAILURES:
                self.failures[ip] = (0, time.time() + LOCKOUT_SEC)
            else:
                self.failures[ip] = (count, until)
            if len(self.failures) > 10_000:   # bound memory under a spray of addresses
                self.failures.clear()

    def record_success(self, ip: str):
        with self.lock:
            self.failures.pop(ip, None)

    # ---------------- sessions ----------------

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_TTL_SEC
        nonce = secrets.token_hex(16)
        payload = f"{expires}.{nonce}"
        return f"{payload}.{self._sign(self.username + '|' + payload)}"

    def verify(self, token: Optional[str]) -> bool:
        if not token or not self.enabled:
            return False
        try:
            expires_raw, nonce, signature = token.split(".")
            expires = int(expires_raw)
        except ValueError:
            return False
        expected = self._sign(f"{self.username}|{expires_raw}.{nonce}")
        if not hmac.compare_digest(signature, expected) or expires < time.time():
            return False
        with self.lock:
            return nonce not in self.revoked

    def revoke(self, token: Optional[str]):
        if not self.verify(token):
            return
        expires_raw, nonce, _ = token.split(".")
        now = time.time()
        with self.lock:
            self.revoked = {n: e for n, e in self.revoked.items() if e > now}
            self.revoked[nonce] = int(expires_raw)


settings = AuthConfig.from_env()

# Paths reachable without a session. Everything else, including /static, needs login.
PUBLIC_PATHS = {"/healthz", "/login", "/api/auth/login", "/api/auth/logout", "/api/auth/status"}


def client_ip(request) -> str:
    # Railway terminates TLS at a proxy and appends the caller to X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_https(request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def same_origin(request) -> bool:
    """Reject cross-site writes. SameSite=Strict already blocks the cookie; this is defence in depth."""
    # Browsers label every request with Sec-Fetch-Site. Prefer it: behind Railway's proxy the
    # Host header may not be the public domain, so comparing Origin to Host can misfire.
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    origin_host = origin.split("://", 1)[-1].lower()
    hosts = {request.headers.get(h, "").split(",")[0].strip().lower() for h in ("x-forwarded-host", "host")}
    return origin_host in hosts
