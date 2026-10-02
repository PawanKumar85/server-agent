"""Saved chatbot conversations (SQLite, next to the metrics): they survive a page refresh and are the same on every
device, and each answer keeps what produced it (tools used, steps, model, time, tokens) for learning and review."""

import json
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY, title TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, meta TEXT);
CREATE INDEX IF NOT EXISTS chat_messages_conv ON chat_messages (conv_id, id);
"""
RETENTION_DAYS = 90


class ChatStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def ensure(self, conv_id: Optional[str], first_question: str = "") -> str:
        """The conversation to add to: the given one if it exists, else a new one titled from its first question."""
        with self.lock, self._connect() as db:
            if conv_id and db.execute("SELECT 1 FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone():
                return conv_id
            new_id = uuid.uuid4().hex[:12]
            now = time.time()
            db.execute("INSERT INTO chat_conversations (id, title, created, updated) VALUES (?,?,?,?)",
                       (new_id, " ".join(first_question.split())[:80] or "New chat", now, now))
            return new_id

    def add(self, conv_id: str, role: str, content: str, meta: Optional[dict] = None) -> int:
        with self.lock, self._connect() as db:
            cur = db.execute("INSERT INTO chat_messages (conv_id, ts, role, content, meta) VALUES (?,?,?,?,?)",
                             (conv_id, time.time(), role, content, json.dumps(meta) if meta else None))
            db.execute("UPDATE chat_conversations SET updated = ? WHERE id = ?", (time.time(), conv_id))
            return cur.lastrowid

    def messages(self, conv_id: str, limit: int = 200) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM (SELECT * FROM chat_messages WHERE conv_id = ? ORDER BY id DESC LIMIT ?) "
                              "ORDER BY id", (conv_id, limit)).fetchall()
        return [{**{k: r[k] for k in ("id", "ts", "role", "content")}, "meta": json.loads(r["meta"] or "{}")} for r in rows]

    def history(self, conv_id: Optional[str], turns: int = 6) -> List[dict]:
        """The last messages as {"role", "content"} for the model (what the browser used to send)."""
        if not conv_id:
            return []
        return [{"role": m["role"], "content": m["content"]} for m in self.messages(conv_id, turns)]

    def last_exchange(self, conv_id: Optional[str]) -> Optional[dict]:
        """The previous question and its answer (for noticing a rephrased question)."""
        msgs = self.messages(conv_id, 4) if conv_id else []
        q = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        a = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
        if not q or not a or a["id"] < q["id"]:
            return None
        return {"question": q["content"], "answer": a["content"], "ts": a["ts"], "tools": a["meta"].get("toolsUsed", [])}

    def conversations(self, limit: int = 50) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT c.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.conv_id = c.id) AS n "
                              "FROM chat_conversations c ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete(self, conv_id: str) -> bool:
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM chat_messages WHERE conv_id = ?", (conv_id,))
            return db.execute("DELETE FROM chat_conversations WHERE id = ?", (conv_id,)).rowcount > 0

    def prune(self) -> None:
        cutoff = time.time() - RETENTION_DAYS * 86400
        with self.lock, self._connect() as db:
            old = [r[0] for r in db.execute("SELECT id FROM chat_conversations WHERE updated < ?", (cutoff,))]
            db.executemany("DELETE FROM chat_messages WHERE conv_id = ?", [(i,) for i in old])
            db.executemany("DELETE FROM chat_conversations WHERE id = ?", [(i,) for i in old])
