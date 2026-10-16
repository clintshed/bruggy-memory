"""HTTP API for the ingestion service.

Endpoints
---------
* ``GET  /health``          liveness + backend type + stats
* ``POST /memory/add``      ingest chat or wiki (runs the merge layer)
* ``POST /memory/query``    RAG recall across chat + wiki for a user
* ``GET  /memory/stats``    counts of users/sessions/episodes/messages

The app is constructed with :func:`create_app`, which takes an optional backend
and settings so tests can inject :class:`InMemoryZepBackend`. In production
``app = create_app()`` builds a :class:`RestZepBackend` from the environment.

All responses are JSON with structured, structured-JSON logs emitted per request.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import Settings, get_settings
from ingest import ChatMessage, IngestionService, IngestResult, QueryResponse
from logging_setup import get_logger, log_event
from zep_client import QueryResult, RestZepBackend, ZepBackend

logger = get_logger("api")


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class ChatMessageReq(BaseModel):
    content: str
    timestamp: str = Field(..., description="ISO-8601 timestamp")
    role: str = "user"
    message_id: Optional[str] = None


class AddChatReq(BaseModel):
    type: str = "chat"
    discord_guild_id: str
    discord_channel_id: str
    discord_user_id: str
    discord_username: str = "user"
    messages: List[ChatMessageReq]
    max_segments: int = 20


class AddWikiReq(BaseModel):
    type: str = "wiki"
    category: str = "general"
    page_title: str = "Untitled"
    source_url: str = ""
    revid: str
    text: str
    last_edit: Optional[str] = None
    target_user_id: Optional[str] = None
    max_segments: int = 50


class AddReq(BaseModel):
    """Union request body: either chat or wiki (``type`` selects)."""

    type: str = Field(..., description="chat | wiki")
    # chat fields
    discord_guild_id: Optional[str] = None
    discord_channel_id: Optional[str] = None
    discord_user_id: Optional[str] = None
    discord_username: Optional[str] = None
    messages: Optional[List[ChatMessageReq]] = None
    # wiki fields
    category: Optional[str] = None
    page_title: Optional[str] = None
    source_url: Optional[str] = None
    revid: Optional[str] = None
    text: Optional[str] = None
    last_edit: Optional[str] = None
    target_user_id: Optional[str] = None
    max_segments: int = 20


class AddWikiRes(BaseModel):
    session_id: str
    user_id: str
    kind: str
    segments_ingested: int
    skipped: int
    skipped_reason: Optional[str] = None
    provenance: dict


class AddChatRes(BaseModel):
    session_id: str
    user_id: str
    kind: str
    segments_ingested: int
    messages_ingested: int
    skipped: int
    skipped_reason: Optional[str] = None
    provenance: dict


class QueryReq(BaseModel):
    user_id: str
    query: str
    n_results: int = 10
    include_wiki: bool = True
    categories: Optional[List[str]] = None
    scope: Optional[List[str]] = None


class QueryRes(BaseModel):
    user_id: str
    session_ids: List[str]
    count: int
    results: List[dict]


class UserMemoryRes(BaseModel):
    """A user's memory graph: their sessions plus (optionally) a recall query."""

    user_id: str
    session_ids: List[str]
    count: int
    results: List[dict]


class HealthRes(BaseModel):
    status: str
    backend: str
    version: str
    uptime_seconds: float
    stats: dict


# --------------------------------------------------------------------------- #
# App construction
# --------------------------------------------------------------------------- #
APP_VERSION = "0.1.0"


def create_app(
    backend: Optional[ZepBackend] = None,
    settings: Optional[Settings] = None,
) -> FastAPI:
    settings = settings or get_settings()
    backend = backend or RestZepBackend(settings)
    service = IngestionService(backend, settings)

    app = FastAPI(title="brussy-memory ingestion", version=APP_VERSION)
    app.state.settings = settings
    app.state.backend = backend
    app.state.service = service
    app.state.started_at = time.time()

    # ---- health ----
    @app.get("/health", response_model=HealthRes)
    async def health() -> HealthRes:
        try:
            stats = await service.stats()
        except Exception as exc:  # pragma: no cover - defensive
            stats = {"error": str(exc)}
        return HealthRes(
            status="ok",
            backend=type(backend).__name__,
            version=APP_VERSION,
            uptime_seconds=round(time.time() - app.state.started_at, 3),
            stats=stats,
        )

    # ---- memory/add ----
    @app.post("/memory/add")
    async def memory_add(req: AddReq):
        t0 = time.time()
        try:
            if req.type == "chat":
                if not req.messages:
                    raise HTTPException(status_code=400, detail="messages required for chat")
                chat_msgs = [
                    ChatMessage(
                        content=m.content,
                        timestamp=datetime.fromisoformat(m.timestamp.replace("Z", "+00:00")),
                        role=m.role,
                        user_id=req.discord_user_id,
                        message_id=m.message_id,
                    )
                    for m in req.messages
                ]
                result = await service.ingest_chat(
                    discord_guild_id=req.discord_guild_id,
                    discord_channel_id=req.discord_channel_id,
                    discord_user_id=req.discord_user_id,
                    discord_username=req.discord_username or "user",
                    messages=chat_msgs,
                    max_segments=req.max_segments,
                )
                log_event(
                    logger,
                    "info",
                    "memory.add.chat",
                    session_id=result.session_id,
                    user_id=result.user_id,
                    segments=result.segments_ingested,
                    duration_ms=round((time.time() - t0) * 1000, 2),
                )
                return AddChatRes(**result.to_dict())
            elif req.type == "wiki":
                last_edit = None
                if req.last_edit:
                    from utils import parse_ts

                    last_edit = parse_ts(req.last_edit)
                result = await service.ingest_wiki(
                    category=req.category or "general",
                    page_title=req.page_title or "Untitled",
                    source_url=req.source_url or "",
                    revid=req.revid or "revid-unknown",
                    text=req.text or "",
                    last_edit=last_edit,
                    target_user_id=req.target_user_id,
                    max_segments=req.max_segments,
                )
                log_event(
                    logger,
                    "info",
                    "memory.add.wiki",
                    session_id=result.session_id,
                    user_id=result.user_id,
                    segments=result.segments_ingested,
                    duration_ms=round((time.time() - t0) * 1000, 2),
                )
                return AddWikiRes(
                    session_id=result.session_id,
                    user_id=result.user_id,
                    kind=result.kind,
                    segments_ingested=result.segments_ingested,
                    skipped=result.skipped,
                    skipped_reason=result.skipped_reason,
                    provenance=result.provenance,
                )
            else:
                raise HTTPException(status_code=400, detail=f"unknown type: {req.type}")
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("memory.add failed", extra={"detail": str(exc)})
            raise HTTPException(status_code=500, detail=str(exc))

    # ---- memory/query ----
    @app.post("/memory/query", response_model=QueryRes)
    async def memory_query(req: QueryReq) -> QueryRes:
        resp: QueryResponse = await service.query(
            discord_user_id=req.user_id,
            text=req.query,
            n_results=req.n_results,
            include_wiki=req.include_wiki,
            categories=req.categories,
            scope=req.scope,
        )
        return QueryRes(
            user_id=resp.user_id,
            session_ids=resp.session_ids,
            count=len(resp.results),
            results=[r.to_dict() for r in resp.results],
        )

    # ---- memory/user/{user_id} ----
    @app.get("/memory/user/{user_id}", response_model=UserMemoryRes)
    async def user_memory(
        user_id: str,
        query: Optional[str] = None,
        n_results: Optional[int] = None,
        include_wiki: bool = True,
        scope: Optional[List[str]] = None,
    ) -> UserMemoryRes:
        """Return a user's memory graph (chat + wiki sessions), optionally recalled."""
        session_ids = await service.backend.list_user_sessions(user_id)
        baseline = service._wiki_sessions_by_user.get(
            settings.zep_baseline_user_id, []
        )
        for sid in baseline:
            if sid not in session_ids:
                session_ids.append(sid)
        results = []
        if query:
            resp = await service.query(
                discord_user_id=user_id,
                text=query,
                n_results=n_results,
                include_wiki=include_wiki,
                scope=scope,
            )
            results = [r.to_dict() for r in resp.results]
        return UserMemoryRes(
            user_id=user_id,
            session_ids=list(dict.fromkeys(session_ids)),
            count=len(results),
            results=results,
        )

    # ---- memory/stats ----
    @app.get("/memory/stats", response_model=dict)
    async def memory_stats() -> dict:
        return await service.stats()

    return app


# Default WSGI-style app object (``uvicorn api_server:app``).
app = create_app()
