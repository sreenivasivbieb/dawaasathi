"""Local, persistent app state in one SQLite file (LOCAL_DB_PATH, default var/local.db).

Kept apart from the drug DB (var/dawasaathi.db), which scripts/load_data.py rebuilds from scratch:
anything in here survives a data reload.

  threads / messages  Chat history per thread. A thread id is the session id the server issued
                      ("s_" + 24 random hex chars); the client keeps its own ids and only those can be
                      read back, so the id works as the access key (there are no user accounts yet).
  learned_brands      Brands found by the web lookup (app/engine/web_brands.py) when the drug DB had
                      no match. Merged into the in-memory store at startup and right after each lookup,
                      so the brand list grows as people ask. Every row keeps its source URL and date.
  web_misses          Names the web lookup could not resolve, so the same miss is not searched again
                      for a while.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  lang TEXT,
  title TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  role TEXT NOT NULL,              -- user | assistant
  text TEXT,
  attachments TEXT,                -- JSON list: ["strip", "rx", "audio"]
  status TEXT,
  verdict TEXT,
  lang TEXT,
  payload TEXT,                    -- assistant: the public QueryResponse JSON (re-renders the card)
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS learned_brands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  composition TEXT NOT NULL,
  form TEXT,
  release TEXT,
  pack_qty REAL,
  pack_unit TEXT,
  mrp REAL,
  manufacturer TEXT,
  source TEXT NOT NULL,
  source_url TEXT NOT NULL,
  evidence TEXT,
  query TEXT,
  fetched_at TEXT NOT NULL,
  UNIQUE(name_key, composition)
);
CREATE TABLE IF NOT EXISTS web_misses (
  query_key TEXT PRIMARY KEY,
  fetched_at REAL NOT NULL
);
"""

TITLE_MAX = 80


class LocalDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ threads
    def thread_exists(self, thread_id: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone() is not None

    def thread_lang(self, thread_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT lang FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return row["lang"] if row else None

    def add_turn(self, thread_id: str, lang: str, user_text: str | None, attachments: list[str],
                 reply: dict[str, Any]) -> None:
        """One user message + the assistant reply, in one transaction."""
        now = time.time()
        title = (user_text or "").strip()[:TITLE_MAX] or None
        script = reply.get("script") or {}
        verdict = (reply.get("verdict") or {}).get("code")
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO threads(id, lang, title, created_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET lang = excluded.lang, updated_at = excluded.updated_at, "
                "title = COALESCE(threads.title, excluded.title)",
                (thread_id, lang, title, now, now))
            if user_text or attachments:
                self.conn.execute(
                    "INSERT INTO messages(thread_id, role, text, attachments, lang, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (thread_id, "user", user_text, json.dumps(attachments), lang, now))
            self.conn.execute(
                "INSERT INTO messages(thread_id, role, text, status, verdict, lang, payload, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (thread_id, "assistant", script.get("text"), reply.get("status"), verdict, lang,
                 json.dumps(reply, ensure_ascii=False), now + 1e-3))

    def threads(self, ids: list[str]) -> list[dict]:
        """Summaries for the given thread ids only (newest first)."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT t.id, t.lang, t.title, t.created_at, t.updated_at, "
                f"(SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS n "
                f"FROM threads t WHERE t.id IN ({marks}) ORDER BY t.updated_at DESC", ids).fetchall()
        return [{"id": r["id"], "lang": r["lang"], "title": r["title"], "messages": r["n"],
                 "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])} for r in rows]

    def messages(self, thread_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?) "
                "ORDER BY id", (thread_id, limit)).fetchall()
        out = []
        for r in rows:
            msg = {"id": r["id"], "role": r["role"], "text": r["text"], "lang": r["lang"],
                   "created_at": _iso(r["created_at"])}
            if r["role"] == "user":
                msg["attachments"] = json.loads(r["attachments"] or "[]")
            else:
                msg["status"] = r["status"]
                msg["verdict"] = r["verdict"]
                msg["response"] = json.loads(r["payload"]) if r["payload"] else None
            out.append(msg)
        return out

    def recent_user_texts(self, thread_id: str, n: int = 4) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT text FROM messages WHERE thread_id = ? AND role = 'user' AND text IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (thread_id, n)).fetchall()
        return [r["text"] for r in reversed(rows)]

    def delete_thread(self, thread_id: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ learned brands
    def learned_brands(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM learned_brands ORDER BY id")]

    def add_learned_brand(self, row: dict) -> int | None:
        """Insert a web-found brand; None if the same name + composition is already stored."""
        cols = ("name", "name_key", "composition", "form", "release", "pack_qty", "pack_unit", "mrp",
                "manufacturer", "source", "source_url", "evidence", "query", "fetched_at")
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO learned_brands({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row.get(c) for c in cols))
        return cur.lastrowid if cur.rowcount else None

    def recent_miss(self, query_key: str, ttl_s: float) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT fetched_at FROM web_misses WHERE query_key = ?",
                                    (query_key,)).fetchone()
        return row is not None and time.time() - row["fetched_at"] < ttl_s

    def add_miss(self, query_key: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO web_misses(query_key, fetched_at) VALUES (?,?)",
                              (query_key, time.time()))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
