"""Per-channel "ignore alerts" switch (default off). A muted channel is still checked and its history recorded (graphs,
root-cause analysis and reports stay complete), but it raises no voice alert, card toast or notification until it is
unmuted, e.g. during planned maintenance. Kept in the same SQLite file as the metrics."""

import sqlite3
import threading
import time
from typing import Dict, List, Optional

SCHEMA = """CREATE TABLE IF NOT EXISTS channel_mutes (
    channel TEXT PRIMARY KEY, muted INTEGER NOT NULL DEFAULT 0, since REAL, note TEXT)"""


class ChannelMutes:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._connect() as db:
            db.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def muted(self) -> Dict[str, dict]:
        """channel -> {"since", "note"} for every muted channel (an unknown channel is not muted)."""
        with self._connect() as db:
            rows = db.execute("SELECT channel, since, note FROM channel_mutes WHERE muted = 1").fetchall()
        return {r["channel"]: {"since": r["since"], "note": r["note"]} for r in rows}

    def is_muted(self, channel: Optional[str]) -> bool:
        return bool(channel) and channel in self.muted()

    def all_muted(self, channels: List[str]) -> bool:
        """True when every given channel is muted (an alert about several channels speaks if any one is live)."""
        names = [c for c in channels if c]
        return bool(names) and all(c in self.muted() for c in names)

    def set(self, channel: str, muted: bool, note: Optional[str] = None) -> dict:
        with self.lock, self._connect() as db:
            db.execute("INSERT INTO channel_mutes (channel, muted, since, note) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(channel) DO UPDATE SET muted = excluded.muted, since = excluded.since, "
                       "note = excluded.note", (channel, int(muted), time.time() if muted else None, note))
        return {"channel": channel, "muted": bool(muted), "since": time.time() if muted else None, "note": note}
