"""In-memory Zep backend for offline tests, demos and CI.

Implements the exact :class:`~zep_client.ZepBackend` interface. It stores chat
messages and graph episodes in plain data structures and performs a lightweight
term-overlap scoring so that the full ingestion + query + provenance logic can
be exercised end-to-end without a live Zep stack.

It is intentionally *not* a faithful semantic replica; its job is to validate
that the ingestion service produces the right graph shape, attaches correct
provenance, dedups idempotently, and that query routing returns results from
both chat and wiki sessions for the same user.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils import parse_ts, now_utc
from zep_client import ChatMessage, Episode, QueryResult, ZepBackend

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD_RE.findall((text or "").lower())


def _freq(tokens: List[str]) -> Dict[str, int]:
    freq: Dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    return freq


def _cosine(a: Dict[str, int], b: Dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    intersect = set(a) & set(b)
    dot = sum(a[w] * b.get(w, 0) for w in intersect)
    if dot == 0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class _StoredMessage:
    msg: ChatMessage
    tokens: List[str] = field(default_factory=list)
    session_id: Optional[str] = None


@dataclass
class _StoredEpisode:
    episode: Episode
    tokens: List[str] = field(default_factory=list)


class InMemoryZepBackend(ZepBackend):
    """A minimal, deterministic in-memory Zep store."""

    def __init__(self, seed: Optional[Dict[str, Any]] = None) -> None:
        self.users: Dict[str, Dict[str, Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}  # session_id -> meta
        self.messages: List[_StoredMessage] = []
        self.episodes: List[_StoredEpisode] = []
        self._stats = {"users": 0, "sessions": 0, "episodes": 0, "messages": 0}
        if seed:
            for user_id, meta in seed.items():
                self.users[user_id] = meta
                self._stats["users"] += 1

    # -- lifecycle --
    async def add_user(self, user_id: str, metadata: Optional[dict] = None) -> None:
        if user_id not in self.users:
            self._stats["users"] += 1
        self.users[user_id] = dict(metadata or {})

    async def ensure_session(
        self, session_id: str, user_id: str, metadata: Optional[dict] = None
    ) -> None:
        meta = self.sessions.get(session_id)
        if meta is None:
            self.sessions[session_id] = {
                "session_id": session_id,
                "user_id": user_id,
                "metadata": dict(metadata or {}),
            }
            self._stats["sessions"] += 1
        else:
            meta["metadata"] = dict(metadata or {})

    # -- ingestion --
    async def add_chat_messages(
        self, session_id: str, user_id: str, messages: List[ChatMessage]
    ) -> int:
        added = 0
        for m in messages:
            if not m.content.strip():
                continue
            self.messages.append(
                _StoredMessage(msg=m, tokens=_tokenize(m.content), session_id=session_id)
            )
            added += 1
        self._stats["messages"] += added
        return added

    async def add_episode(
        self,
        session_id: str,
        user_id: str,
        text: str,
        business_data: dict,
        episode_uuid: str,
        document_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        kind: str = "episode",
    ) -> None:
        # Deterministic id => re-ingest is idempotent (no duplicate episode).
        if any(e.episode.episode_uuid == episode_uuid for e in self.episodes):
            return
        business_data = dict(business_data)
        business_data.setdefault("source", kind)
        episode = Episode(
            episode_uuid=episode_uuid,
            text=text,
            session_id=session_id,
            user_id=user_id,
            business_data=business_data,
            document_id=document_id,
            timestamp=(timestamp or now_utc()).isoformat(),
            kind=kind,
        )
        self.episodes.append(_StoredEpisode(episode=episode, tokens=_tokenize(text)))
        self._stats["episodes"] += 1

    # -- query --
    async def query(
        self,
        user_id: str,
        text: str,
        session_ids: Optional[List[str]] = None,
        n_results: int = 10,
        scope: Optional[List[str]] = None,
    ) -> List[QueryResult]:
        q = _tokenize(text)
        if not q:
            return []
        session_set = set(session_ids) if session_ids else None

        scored: List[tuple] = []
        # Episodes (chat segments + wiki segments) are the recallable content;
        # they all carry provenance so every result is self-describing. Raw
        # chat messages are stored only for stats / real-Zep NLP extraction.
        for stored in self.episodes:
            ep = stored.episode
            if session_set is not None and ep.session_id not in session_set:
                continue
            score = _cosine(_freq(q), _freq(stored.tokens))
            if score > 0:
                scored.append((score, ep, stored))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: List[QueryResult] = []
        for score, ep, stored in scored[: n_results or None]:
            results.append(
                QueryResult(
                    score=score,
                    text=ep.text,
                    episode=ep,
                    kind=ep.kind,
                )
            )
        return results

    async def list_user_sessions(self, user_id: str) -> List[str]:
        return [
            sid
            for sid, meta in self.sessions.items()
            if meta.get("user_id") == user_id
        ]

    async def last_timestamp(self, session_id: str) -> Optional[datetime]:
        latest: Optional[datetime] = None
        for m in self.messages:
            if m.session_id != session_id:
                continue
            ts = m.msg.timestamp
            if ts and (latest is None or ts > latest):
                latest = ts
        for e in self.episodes:
            ts = parse_ts(e.episode.timestamp)
            if ts is None:
                continue
            if e.episode.session_id != session_id:
                continue
            if latest is None or ts > latest:
                latest = ts
        return latest

    async def episode_exists(self, episode_uuid: str) -> bool:
        return any(e.episode.episode_uuid == episode_uuid for e in self.episodes)

    async def stats(self) -> Dict[str, int]:
        return dict(self._stats)
