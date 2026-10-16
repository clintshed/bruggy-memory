"""Zep client: the backend interface plus a REST implementation.

Why REST instead of the ``zep-python`` SDK?
-------------------------------------------
The ``zep-python`` SDK available in the reference environment (v2.0.2) is an
incomplete build -- it exposes ``memory.add`` / ``memory.add_session`` /
``memory.search_sessions`` / ``user.add`` but is **missing** the graph episode
endpoint (``graph.episode.add``) and the memory query endpoint
(``memory.query``) that this service depends on. Rather than pin to a specific
SDK version, we talk to the stable Zep v2 REST API directly via ``httpx``. The
REST surface is version-stable and, crucially, fully testable offline.

Design
------
``ZepBackend`` is an abstract base class describing exactly what the ingestion
and query paths need. :class:`RestZepBackend` implements it against the live Zep
stack. :class:`InMemoryZepBackend` (in :mod:`in_memory_backend`) implements the
same interface for offline tests and demos. The rest of the service depends on
``ZepBackend`` only, so it runs unchanged against either.

Zep v2 REST endpoints used
--------------------------
* ``POST   /api/v2/users``                      add / upsert user
* ``POST   /api/v2/sessions``                    create session (with user_id)
* ``GET    /api/v2/sessions-ordered``            list a user's sessions
* ``POST   /api/v2/sessions/{id}/memory``        ingest chat messages (NLP)
* ``POST   /api/v2/graph/episode``               ingest a graph episode
* ``GET    /api/v2/graph/episode/{uuid}``        check an episode exists
* ``POST   /api/v2/sessions/search``             memory.query / recall
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from logging_setup import get_logger
from utils import parse_ts, to_iso, now_utc

logger = get_logger("zep_client")

API_PREFIX = "/api/v2"


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class Episode:
    """A recalled episode with its provenance intact."""

    episode_uuid: str
    text: str
    session_id: str
    user_id: str
    business_data: Dict[str, Any] = field(default_factory=dict)
    document_id: Optional[str] = None
    timestamp: Optional[str] = None
    kind: str = "episode"  # chat_segment | wiki_segment

    @property
    def source(self) -> Optional[str]:
        return self.business_data.get("source")

    @property
    def title(self) -> Optional[str]:
        """Human-friendly title: wiki page title or chat channel name."""
        return self.business_data.get("source_title") or self.business_data.get(
            "channel"
        )

    @property
    def url(self) -> Optional[str]:
        return self.business_data.get("source_url")

    @property
    def channel(self) -> Optional[str]:
        return self.business_data.get("discord_channel_id")

    @property
    def author_id(self) -> Optional[str]:
        return self.business_data.get("author_id")

    @property
    def author(self) -> Optional[str]:
        return self.business_data.get("author")

    @property
    def category(self) -> Optional[str]:
        return self.business_data.get("category")

    @property
    def revid(self) -> Optional[str]:
        """Wiki edit id, carried on every wiki result for full provenance."""
        return self.business_data.get("revid")

    def provenance(self) -> Dict[str, Any]:
        """The full, stable provenance map carried on every result.

        Returns the episode's raw ``business_data`` (source-specific fields:
        ``source``, ``source_title``, ``source_url``, ``revid``, ``category``
        for wiki; ``channel``, ``author``, ``author_id`` for chat) augmented
        with the derived generic aliases (``title``, ``url``).
        """
        data = dict(self.business_data)
        data.setdefault("source", self.source)
        data["title"] = self.title
        data["url"] = self.url
        data["timestamp"] = self.timestamp
        return data


@dataclass
class QueryResult:
    """A single RAG recall with score + provenance."""

    score: float
    text: str
    episode: Episode
    kind: str = "episode"
    fact: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        data = {
            "score": round(self.score, 6),
            "text": self.text,
            "kind": self.kind,
            "episode": {
                "episode_uuid": self.episode.episode_uuid,
                "session_id": self.episode.session_id,
                "source": self.episode.source,
                "title": self.episode.title,
                "url": self.episode.url,
                "channel": self.episode.channel,
                "author": self.episode.author,
                "author_id": self.episode.author_id,
                "category": self.episode.category,
                "revid": self.episode.revid,
                "document_id": self.episode.document_id,
                "timestamp": self.episode.timestamp,
            },
            "provenance": self.episode.provenance(),
        }
        if self.fact is not None:
            data["fact"] = self.fact
        return data


@dataclass
class ChatMessage:
    """A chat message to ingest as session memory."""

    content: str
    timestamp: datetime
    role: str = "user"
    user_id: Optional[str] = None
    message_id: Optional[str] = None

    def to_zep(self) -> Dict[str, Any]:
        return {
            "uuid": _msg_uuid(self),
            "content": self.content,
            "role": self.role,
            "timestamp": to_iso(self.timestamp),
        }


def _msg_uuid(msg: ChatMessage) -> str:
    from utils import uuid5

    seed = msg.message_id or msg.content
    return uuid5("chatmsg", msg.timestamp.isoformat(), str(seed))


# --------------------------------------------------------------------------- #
# Backend interface
# --------------------------------------------------------------------------- #
class ZepBackend(ABC):
    """Abstraction over a Zep store. Both REST and in-memory backends."""

    # -- lifecycle --
    @abstractmethod
    async def add_user(self, user_id: str, metadata: Optional[dict] = None) -> None:
        ...

    @abstractmethod
    async def ensure_session(
        self, session_id: str, user_id: str, metadata: Optional[dict] = None
    ) -> None:
        ...

    # -- ingestion --
    @abstractmethod
    async def add_chat_messages(
        self, session_id: str, user_id: str, messages: List[ChatMessage]
    ) -> int:
        """Ingest chat messages as session memory (triggers NLP in real Zep)."""

    @abstractmethod
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
        """Ingest a graph episode (chat segment or wiki segment)."""

    # -- query / introspection --
    @abstractmethod
    async def query(
        self,
        user_id: str,
        text: str,
        session_ids: Optional[List[str]] = None,
        n_results: int = 10,
        scope: Optional[List[str]] = None,
    ) -> List[QueryResult]:
        """Recall episodes/facts across the given sessions for a user."""

    @abstractmethod
    async def list_user_sessions(self, user_id: str) -> List[str]:
        ...

    @abstractmethod
    async def last_timestamp(self, session_id: str) -> Optional[datetime]:
        """Timestamp of the most recent ingested content in a session."""

    @abstractmethod
    async def episode_exists(self, episode_uuid: str) -> bool:
        ...

    # -- stats (best-effort) --
    async def stats(self) -> Dict[str, int]:  # pragma: no cover - optional
        return {"users": 0, "sessions": 0, "episodes": 0, "messages": 0}


# --------------------------------------------------------------------------- #
# REST implementation
# --------------------------------------------------------------------------- #
class RestZepBackend(ZepBackend):
    """Talks to a real Zep v2 stack over HTTP."""

    def __init__(self, settings) -> None:
        self.base_url = settings.zep_api_url.rstrip("/")
        self.api_key = settings.zep_api_key
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        )

    async def _request(self, method: str, path: str, body: Optional[dict] = None):
        url = f"{API_PREFIX}{path}"
        for attempt in range(1, 4):
            try:
                resp = await self._client.request(method, url, json=body)
                resp.raise_for_status()
                if resp.status_code == 204:
                    return {}
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 409:
                    # Conflict == already exists (user/session/episode). Tolerate.
                    return {}
                if status in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt * 0.5, 5.0)
                    await httpx.sleep(wait) if hasattr(httpx, "sleep") else await _sleep(wait)
                    continue
                logger.warning(
                    "zep request failed",
                    extra={"event": "zep_http_error"},
                )
                raise
            except httpx.HTTPError as exc:
                logger.warning(
                    "zep network error",
                    extra={"event": "zep_http_error"},
                )
                await _sleep(min(2 ** attempt * 0.5, 5.0))
                continue
        raise RuntimeError(f"Zep request failed after retries: {method} {path}")

    async def add_user(self, user_id: str, metadata: Optional[dict] = None) -> None:
        metadata = dict(metadata or {})
        metadata["email"] = f"{user_id}@brussy.local"
        body = {"user_id": user_id, "metadata": metadata}
        await self._request("POST", "/users", body)

    async def ensure_session(
        self, session_id: str, user_id: str, metadata: Optional[dict] = None
    ) -> None:
        metadata = dict(metadata or {})
        metadata["user_id"] = user_id
        body = {"session_id": session_id, "user_id": user_id, "metadata": metadata}
        await self._request("POST", "/sessions", body)

    async def add_chat_messages(
        self, session_id: str, user_id: str, messages: List[ChatMessage]
    ) -> int:
        if not messages:
            return 0
        zep_messages = [m.to_zep() for m in messages if m.content.strip()]
        if not zep_messages:
            return 0
        body = {"messages": zep_messages}
        await self._request("POST", f"/sessions/{session_id}/memory", body)
        return len(zep_messages)

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
        body: Dict[str, Any] = {
            "episode_uuid": episode_uuid,
            "session_id": session_id,
            "user_id": user_id,
            "text": text,
            "business_data": business_data,
            "language": "en",
        }
        if document_id:
            body["document_id"] = document_id
        body["timestamp"] = to_iso(timestamp or now_utc())
        body["metadata"] = {"kind": kind}
        await self._request("POST", "/graph/episode", body)

    async def query(
        self,
        user_id: str,
        text: str,
        session_ids: Optional[List[str]] = None,
        n_results: int = 10,
        scope: Optional[List[str]] = None,
    ) -> List[QueryResult]:
        body: Dict[str, Any] = {
            "user_id": user_id,
            "text": text,
            "search_scope": scope or ["facts", "episodes", "segments", "entity_relationships"],
            "search_type": "dense",
            "n_results": n_results,
        }
        if session_ids:
            body["session_ids"] = list(session_ids)
        data = await self._request("POST", "/sessions/search", body)
        return self._parse_results(data)

    @staticmethod
    def _parse_results(data: dict) -> List[QueryResult]:
        results: List[QueryResult] = []
        for item in data.get("results", []):
            episode_raw = item.get("episode")
            if not episode_raw:
                continue
            business_data = episode_raw.get("business_data", {})
            episode = Episode(
                episode_uuid=episode_raw.get("episode_uuid", ""),
                text=episode_raw.get("text", ""),
                session_id=episode_raw.get("session_id", ""),
                user_id=episode_raw.get("user_id", ""),
                business_data=business_data,
                document_id=episode_raw.get("document_id"),
                timestamp=episode_raw.get("timestamp"),
                kind=episode_raw.get("metadata", {}).get("kind", "episode")
                if isinstance(episode_raw.get("metadata"), dict)
                else "episode",
            )
            if episode.kind == "chat_segment":
                episode.kind = "chat_segment"
            results.append(
                QueryResult(
                    score=float(item.get("score", 0.0) or 0.0),
                    text=episode.text,
                    episode=episode,
                    kind=episode.kind,
                    fact=item.get("fact"),
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def list_user_sessions(self, user_id: str) -> List[str]:
        page_size = 1000
        sessions: List[str] = []
        page = 1
        while True:
            path = (
                f"/sessions-ordered?user_id={user_id}"
                f"&page_number={page}&page_size={page_size}"
                "&order_by=created_at&asc=true"
            )
            data = await self._request("GET", path)
            batch = data.get("sessions", [])
            sessions.extend(s["session_id"] for s in batch)
            if len(batch) < page_size:
                break
            page += 1
        return sessions

    async def last_timestamp(self, session_id: str) -> Optional[datetime]:
        try:
            data = await self._request("GET", f"/sessions/{session_id}/messages")
        except Exception:
            return None
        messages = data.get("messages", data) if isinstance(data, dict) else data
        if not isinstance(messages, list):
            return None
        latest: Optional[datetime] = None
        for m in messages:
            ts = parse_ts(m.get("timestamp"))
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    async def episode_exists(self, episode_uuid: str) -> bool:
        try:
            await self._request("GET", f"/graph/episode/{episode_uuid}")
            return True
        except httpx.HTTPStatusError as exc:
            return exc.response.status_code == 404
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
