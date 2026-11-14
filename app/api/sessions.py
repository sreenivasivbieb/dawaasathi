"""Per-session state. A session id is also the chat thread id.

In memory (dropped after SESSION_TTL_S, 15 min by default): last transcript, matched ids, pending
confirmation context, last verdict item. No audio or images are kept.

Persistent (app/localdb.py, when a LocalDB is given): the thread's messages. A thread id the server
issued earlier can be resumed after the in-memory state expired or the server restarted; the chat
history is still there, only the short-term context (a pending "which one?" question) is gone.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.localdb import LocalDB

PUBLIC_ID = re.compile(r"^s_[0-9a-f]{12,24}$")


@dataclass
class Session:
    id: str
    lang: str = "hi"
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_transcript: str | None = None
    pending: dict[str, Any] | None = None  # confirmation context
    last_item: Any = None  # DrugItem of the last verdict
    last_context: dict[str, Any] | None = None
    confirmed_brand_ids: set[int] = field(default_factory=set)
    current_audio_id: str | None = None
    last_chemist_text: str | None = None
    turns: int = 0


class SessionStore:
    def __init__(self, ttl_s: int = 900, history: "LocalDB | None" = None):
        self.ttl_s = ttl_s
        self.history = history
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None, lang: str = "hi") -> Session:
        """A client may only resume an id this server issued (random, unguessable): one still in
        memory, or a stored chat thread. Any other id gets a fresh session, never one the client named."""
        self.gc()
        if session_id and not PUBLIC_ID.match(session_id):
            session_id = None
        if session_id and session_id in self._sessions:
            s = self._sessions[session_id]
            s.last_seen = time.time()
            return s
        if session_id and self.history is not None and self.history.thread_exists(session_id):
            s = Session(id=session_id, lang=lang)
            self._sessions[s.id] = s
            return s
        s = Session(id=f"s_{secrets.token_hex(12)}", lang=lang)
        self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session | None:
        self.gc()
        if not PUBLIC_ID.match(session_id or ""):
            return None
        return self._sessions.get(session_id)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def gc(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._sessions.items() if now - v.last_seen > self.ttl_s]:
            del self._sessions[sid]

    def __len__(self) -> int:
        return len(self._sessions)
