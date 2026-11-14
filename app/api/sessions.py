"""Per-session state, in memory only, dropped after SESSION_TTL_S (15 min by default).

Holds: last transcript, matched ids, pending confirmation context, last verdict item.
No audio or images are kept.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


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
    def __init__(self, ttl_s: int = 900):
        self.ttl_s = ttl_s
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None, lang: str = "hi") -> Session:
        self.gc()
        if session_id and session_id in self._sessions:
            s = self._sessions[session_id]
            s.last_seen = time.time()
            return s
        sid = session_id if session_id and len(session_id) <= 64 and session_id.isascii() else None
        s = Session(id=sid or f"s_{secrets.token_hex(6)}", lang=lang)
        self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session | None:
        self.gc()
        return self._sessions.get(session_id)

    def gc(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._sessions.items() if now - v.last_seen > self.ttl_s]:
            del self._sessions[sid]

    def __len__(self) -> int:
        return len(self._sessions)
