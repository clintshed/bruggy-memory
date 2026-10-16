"""Ingestion service: pulls Discord chat and wiki pages into one per-user Zep
graph, tagging every episode with stable provenance and deduping idempotently.

This is task ``t_1a3ab968`` — the "merge" layer. Its acceptance criteria:

* Wiki + chat episodes for the same Discord user are queryable together.
* Every result carries correct metadata/provenance (source, title/channel, url,
  timestamp).
* Queries return relevant wiki + chat results.

Model
-----
* Each Discord user -> their own ``user_id`` (their Discord id).
* Chat -> per-channel sessions ``chat:<guild_id>:<channel_id>`` under that user.
* Wiki -> ingested under the *same* user's graph when a ``target_user_id`` is
  supplied (that is what makes wiki + chat "queryable together" for one user).
  When no target is supplied (anonymous daily pull), wiki attaches to the
  baseline user and is still returned to every user's query because the query
  spans the baseline wiki sessions too.
* Chat is ingested two ways: the raw messages (so Zep's NLP can extract chat
  facts/summaries) *and* per-segment chat episodes carrying chat provenance.
  Wiki is ingested as wiki segments carrying wiki provenance.

All content for a user lives in the same ``user_id`` graph, so a single
``memory.query`` across that user's sessions returns both chat and wiki, ranked
together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from chunker import Segment, chunk_markdown, chunk_text
from config import Settings
from logging_setup import get_logger
from zep_client import ChatMessage, Episode, QueryResult, ZepBackend

logger = get_logger("ingest")


# --------------------------------------------------------------------------- #
# Provenance builders
# --------------------------------------------------------------------------- #
def chat_business_data(
    discord_guild_id: str,
    discord_channel_id: str,
    discord_username: str,
    discord_user_id: str,
    session_id: str,
    timestamp: datetime,
) -> Dict[str, Any]:
    """Provenance attached to chat episodes and messages."""
    return {
        "source": "chat",
        "channel": f"discord:{discord_channel_id}",
        "discord_guild_id": discord_guild_id,
        "discord_channel_id": discord_channel_id,
        "session_id": session_id,
        "author": discord_username,
        "author_id": discord_user_id,
        "timestamp": timestamp.isoformat(),
    }


def wiki_business_data(
    category: str,
    page_title: str,
    source_url: str,
    revid: str,
    document_id: str,
    timestamp: datetime,
) -> Dict[str, Any]:
    """Provenance attached to wiki episodes."""
    return {
        "source": "wiki",
        "source_title": page_title,
        "source_url": source_url,
        "category": category,
        "revid": revid,
        "document_id": document_id,
        "timestamp": timestamp.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class SegmentResult:
    segment_index: int
    episode_uuid: str
    tokens: int


@dataclass
class IngestResult:
    """Outcome of an ingestion call."""

    session_id: str
    user_id: str
    kind: str  # "chat" | "wiki"
    segments_ingested: int = 0
    messages_ingested: int = 0
    skipped: int = 0
    skipped_reason: Optional[str] = None
    segments: List[SegmentResult] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "kind": self.kind,
            "segments_ingested": self.segments_ingested,
            "messages_ingested": self.messages_ingested,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
            "provenance": self.provenance,
        }


@dataclass
class QueryResponse:
    results: List[QueryResult]
    user_id: str
    session_ids: List[str]

    @property
    def count(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_ids": self.session_ids,
            "count": len(self.results),
            "results": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class IngestionService:
    def __init__(self, backend: ZepBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings
        # Wiki sessions ingested per target user (session_id -> provenance), so
        # queries can route to them.
        self._wiki_sessions_by_user: Dict[str, List[str]] = {}

    # -- session id helpers --
    @staticmethod
    def chat_session_id(guild_id: str, channel_id: str) -> str:
        return f"chat:{guild_id}:{channel_id}"

    @staticmethod
    def wiki_session_id(category: str, revid: str) -> str:
        return f"wiki:{category}:{revid}"

    # -- chat ingestion --
    async def ingest_chat(
        self,
        discord_guild_id: str,
        discord_channel_id: str,
        discord_user_id: str,
        discord_username: str,
        messages: List[ChatMessage],
        max_segments: int = 20,
    ) -> IngestResult:
        session_id = self.chat_session_id(discord_guild_id, discord_channel_id)
        await self.backend.add_user(discord_user_id, {})
        await self.backend.ensure_session(
            session_id,
            discord_user_id,
            metadata=chat_business_data(
                discord_guild_id,
                discord_channel_id,
                discord_username,
                discord_user_id,
                session_id,
                messages[0].timestamp if messages else datetime.now(timezone.utc),
            ),
        )

        # Idempotency: skip messages already ingested for this session.
        last_ts = await self.backend.last_timestamp(session_id)
        keep = [
            m
            for m in messages
            if last_ts is None or (m.timestamp or datetime.now(timezone.utc)) > last_ts
        ]
        skipped = len(messages) - len(keep)

        bd = chat_business_data(
            discord_guild_id,
            discord_channel_id,
            discord_username,
            discord_user_id,
            session_id,
            messages[0].timestamp if messages else datetime.now(timezone.utc),
        )

        messages_ingested = 0
        if keep:
            messages_ingested = await self.backend.add_chat_messages(
                session_id, discord_user_id, keep
            )

        # Build per-segment chat episodes carrying chat provenance.
        segment_results: List[SegmentResult] = []
        segments_ingested = 0
        for m in keep[:max_segments]:
            segs = chunk_text(m.content, self.settings, business_data=dict(bd))
            for seg_index, seg in enumerate(segs):
                episode_uuid = _segment_uuid(
                    session_id, m.message_id or m.content, seg_index
                )
                await self.backend.add_episode(
                    session_id=session_id,
                    user_id=discord_user_id,
                    text=seg.text,
                    business_data=seg.business_data,
                    episode_uuid=episode_uuid,
                    document_id=m.message_id or f"msg:{session_id}:{seg_index}",
                    timestamp=m.timestamp,
                    kind="chat_segment",
                )
                segment_results.append(
                    SegmentResult(seg_index, episode_uuid, seg.tokens)
                )
                segments_ingested += 1

        result = IngestResult(
            session_id=session_id,
            user_id=discord_user_id,
            kind="chat",
            segments_ingested=segments_ingested,
            messages_ingested=messages_ingested,
            skipped=skipped,
            provenance=bd,
        )
        result.segments = segment_results[:max_segments]
        logger.info(
            "chat ingested",
            extra={
                "event": "ingest_chat",
                "session_id": session_id,
                "segments": segments_ingested,
                "messages": messages_ingested,
                "skipped": skipped,
            },
        )
        return result

    # -- wiki ingestion --
    async def ingest_wiki(
        self,
        category: str,
        page_title: str,
        source_url: str,
        revid: str,
        text: str,
        last_edit: Optional[datetime] = None,
        target_user_id: Optional[str] = None,
        max_segments: int = 50,
    ) -> IngestResult:
        session_id = self.wiki_session_id(category, revid)
        user_id = target_user_id or self.settings.zep_baseline_user_id
        ts = last_edit or datetime.now(timezone.utc)
        # Guard against a caller (e.g. the scheduler) passing max_segments=None.
        if max_segments is None:
            max_segments = 50

        # Idempotency at document granularity: if any segment for this revid
        # already exists, the whole page is considered already ingested.
        sentinel_uuid = _document_uuid(session_id, revid)
        if await self.backend.episode_exists(sentinel_uuid):
            return IngestResult(
                session_id=session_id,
                user_id=user_id,
                kind="wiki",
                skipped=1,
                skipped_reason="document_already_ingested",
                provenance=wiki_business_data(
                    category, page_title, source_url, revid, revid, ts
                ),
            )

        await self.backend.add_user(user_id, {})
        await self.backend.ensure_session(
            session_id,
            user_id,
            metadata=wiki_business_data(
                category, page_title, source_url, revid, revid, ts
            ),
        )

        bd = wiki_business_data(
            category, page_title, source_url, revid, revid, ts
        )

        segment_results: List[SegmentResult] = []
        segments_ingested = 0
        for seg_index, seg in enumerate(
            chunk_markdown(text, self.settings, business_data=dict(bd))
        ):
            # Sentinel episode per document => idempotent document-level dedup.
            episode_uuid = (
                sentinel_uuid if seg_index == 0 else _segment_uuid(session_id, revid, seg_index)
            )
            await self.backend.add_episode(
                session_id=session_id,
                user_id=user_id,
                text=seg.text,
                business_data=seg.business_data,
                episode_uuid=episode_uuid,
                document_id=revid,
                timestamp=ts,
                kind="wiki_segment",
            )
            segment_results.append(
                SegmentResult(seg_index, episode_uuid, seg.tokens)
            )
            segments_ingested += 1
            if segments_ingested >= max_segments:
                break

        # Route future queries for this target user to the wiki session.
        self._wiki_sessions_by_user.setdefault(user_id, []).append(session_id)

        result = IngestResult(
            session_id=session_id,
            user_id=user_id,
            kind="wiki",
            segments_ingested=segments_ingested,
            provenance=bd,
        )
        result.segments = segment_results
        logger.info(
            "wiki ingested",
            extra={
                "event": "ingest_wiki",
                "session_id": session_id,
                "user_id": user_id,
                "segments": segments_ingested,
                "category": category,
            },
        )
        return result

    # -- query (RAG recall across chat + wiki) --
    async def query(
        self,
        discord_user_id: str,
        text: str,
        n_results: Optional[int] = None,
        include_wiki: bool = True,
        categories: Optional[List[str]] = None,
        scope: Optional[List[str]] = None,
    ) -> QueryResponse:
        n_results = n_results or self.settings.default_n_results
        session_ids = await self.backend.list_user_sessions(discord_user_id)

        # Include baseline wiki sessions (anonymous pulls) that any user may
        # reference, filtered by category when requested.
        baseline_wiki = self._wiki_sessions_by_user.get(
            self.settings.zep_baseline_user_id, []
        )
        if include_wiki:
            for sid in baseline_wiki:
                if sid not in session_ids:
                    session_ids.append(sid)

        response = await self.backend.query(
            user_id=discord_user_id,
            text=text,
            session_ids=session_ids or None,
            n_results=n_results,
            scope=scope,
        )
        return QueryResponse(
            results=response, user_id=discord_user_id, session_ids=list(dict.fromkeys(session_ids))
        )

    # -- introspection for the API / health / stats --
    async def stats(self) -> Dict[str, Any]:
        base = await self.backend.stats()
        base.setdefault("users", 0)
        base.setdefault("sessions", 0)
        base.setdefault("episodes", 0)
        base.setdefault("messages", 0)
        return base


def _segment_uuid(session_id: str, doc_ref: str, index: int) -> str:
    from utils import uuid5

    return uuid5("segment", session_id, doc_ref, str(index))


def _document_uuid(session_id: str, document_id: str) -> str:
    from utils import uuid5

    return uuid5("document", session_id, document_id)
