"""Durable WhatsApp state in SQLite: processed message ids (dedup) and per-chat conversation context."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS processed (key TEXT PRIMARY KEY, message_id TEXT, received_at REAL);
            CREATE TABLE IF NOT EXISTS conversations (chat_key TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL);
        """)

    def claim(self, key):
        """Atomically mark a message as taken. False if it was already seen (OpenWA retry / re-fire)."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO processed(key, received_at) VALUES (?, ?)", (key, time.time()))
            return cur.rowcount == 1

    def prune(self, older_than_days=14):
        with self._lock:
            self._db.execute("DELETE FROM processed WHERE received_at < ?", (time.time() - older_than_days * 86400,))

    def load(self, chat_key):
        row = self._db.execute("SELECT state FROM conversations WHERE chat_key=?", (chat_key,)).fetchone()
        state = json.loads(row[0]) if row else {}
        return {"last_plan": state.get("last_plan"), "history": state.get("history", []), "files": state.get("files", []),
                "recent_plans": state.get("recent_plans", []), "focus": state.get("focus"), "pending_rule": state.get("pending_rule"),
                "analysis": state.get("analysis"), "pending_choice": state.get("pending_choice"),
                "last_result": state.get("last_result")}

    def save(self, chat_key, state):
        with self._lock:
            self._db.execute("INSERT INTO conversations(chat_key, state, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(chat_key) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                             (chat_key, json.dumps(state, ensure_ascii=False, default=str), time.time()))

    def reset(self, chat_key):
        with self._lock:
            self._db.execute("DELETE FROM conversations WHERE chat_key=?", (chat_key,))


def chat_key(session_id, chat_id):
    return f"whatsapp:{session_id}:{chat_id}"


def chat_slug(key):
    """Filesystem/source-id safe, not reversible to the phone number."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]
