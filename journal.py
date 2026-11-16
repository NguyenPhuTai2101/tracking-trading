"""SQLite store: live bot state, closed trades, equity snapshots and the event log.

State and the trades it closes are written in one transaction, so a crash can never
leave a trade recorded twice or a closed position still open.
"""
import json
import sqlite3
import threading
import time
from typing import Dict, List, Optional

TRADE_COLUMNS = ("position_id", "symbol", "direction", "entry_time", "exit_time", "entry_price",
                 "exit_price", "quantity", "gross_pnl", "fees", "net_pnl", "risk_usdt", "r_multiple",
                 "reason", "bars", "setup")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
    entry_time INTEGER NOT NULL, exit_time INTEGER NOT NULL,
    entry_price REAL NOT NULL, exit_price REAL NOT NULL, quantity REAL NOT NULL,
    gross_pnl REAL NOT NULL, fees REAL NOT NULL, net_pnl REAL NOT NULL,
    risk_usdt REAL NOT NULL, r_multiple REAL NOT NULL, reason TEXT NOT NULL, bars INTEGER NOT NULL,
    setup TEXT NOT NULL DEFAULT 'TREND_PULLBACK');
CREATE INDEX IF NOT EXISTS trades_exit ON trades (exit_time);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER PRIMARY KEY, equity REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
"""
MAX_EVENTS = 2000


class Journal:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        # Autocommit mode; multi-statement writes use explicit transactions below.
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "setup" not in {row["name"] for row in self.db.execute("PRAGMA table_info(trades)")}:
            # Databases created before setups existed: every trade then was a trend pullback.
            self.db.execute("ALTER TABLE trades ADD COLUMN setup TEXT NOT NULL DEFAULT 'TREND_PULLBACK'")

    def close(self):
        with self.lock:
            self.db.close()

    # ---------------- state + trades ----------------

    def load_state(self) -> Optional[Dict]:
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key = 'state'").fetchone()
        return json.loads(row["value"]) if row else None

    def commit(self, state: Dict, trades: List[Dict] = ()):
        """Persist the bot state and any trades it just closed, atomically."""
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        marks = ", ".join("?" * len(TRADE_COLUMNS))
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO kv (key, value) VALUES ('state', ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (payload,))
                for trade in trades:
                    self.db.execute(f"INSERT OR REPLACE INTO trades ({', '.join(TRADE_COLUMNS)}) VALUES ({marks})",
                                    [trade[c] for c in TRADE_COLUMNS])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def trades(self, since_ms: int = 0) -> List[Dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM trades WHERE exit_time >= ? ORDER BY exit_time", (since_ms,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- equity ----------------

    def add_equity(self, equity: float, ts_ms: Optional[int] = None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO equity (ts, equity) VALUES (?, ?)",
                            (ts_ms or int(time.time() * 1000), equity))

    def equity_curve(self, since_ms: int = 0, max_points: int = 600) -> List[Dict]:
        with self.lock:
            rows = self.db.execute("SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts", (since_ms,)).fetchall()
        step = max(1, -(-len(rows) // max_points))
        picked = rows[::step]
        if rows and picked[-1] is not rows[-1]:
            picked.append(rows[-1])
        return [{"time": r["ts"], "equity": r["equity"]} for r in picked]

    # ---------------- events ----------------

    def add_event(self, level: str, message: str):
        with self.lock:
            cursor = self.db.execute("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)",
                                     (int(time.time() * 1000), level, message))
            if cursor.lastrowid % 200 == 0:
                self.db.execute("DELETE FROM events WHERE id <= ?", (cursor.lastrowid - MAX_EVENTS,))

    def events(self, limit: int = 60) -> List[Dict]:
        with self.lock:
            rows = self.db.execute("SELECT ts, level, message FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"time": r["ts"], "level": r["level"], "message": r["message"]} for r in rows]
