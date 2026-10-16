"""PullJob — orchestrates a single pull run (design §4.5).

``PullJob.run()`` is the pull-layer orchestrator:

1. Resolve the page list (from the source category via
   :meth:`MediaWikiClient.get_page_titles`, or an explicit ``titles`` list).
2. For each page, fetch it (best-effort; Cloudflare / client errors skip the
   page rather than aborting the run).
3. Skip pages whose current ``revid`` is not newer than the
   :class:`~pull.watermark.Watermark` (``catch_up=False``); in ``catch_up`` mode
   every page is ingested (initial load).
4. Chunk the page with the section-aware :mod:`pull.chunker`.
5. Optionally push the page into an ingest service (Zep) if one is wired in.
6. Advance and persist the watermark.
7. Write the per-run export artifact (:func:`pull.export.export_pull`).

Returns a :class:`PullResult` summarising the run.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from pull.chunker import WikiChunk, chunk_page
from pull.export import export_pull
from pull.watermark import Watermark
from pull.wiki_client import ClientBackoffError, ClientError, MediaWikiClient, PageSource

logger = logging.getLogger("pull.job")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class PagePullResult:
    """Outcome of pulling one page."""

    title: str
    page_id: str = ""
    revid: str = ""
    url: str = ""
    source: str = "wiki"
    extract: str = ""
    chunks: List[WikiChunk] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "page_id": self.page_id,
            "revid": self.revid,
            "url": self.url,
            "source": self.source,
            "chunks": len(self.chunks),
            "skipped": self.skipped,
            "reason": self.reason,
        }


@dataclass
class PullResult:
    """Summary of a full pull run."""

    pull_id: str
    source: str
    category: str
    started_at: str
    ended_at: str
    pages: List[PagePullResult] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def chunk_count(self) -> int:
        return sum(len(p.chunks) for p in self.pages)

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for p in self.pages for c in p.chunks)

    @property
    def skipped_count(self) -> int:
        return sum(1 for p in self.pages if p.skipped)

    def to_dict(self) -> dict:
        return {
            "pull_id": self.pull_id,
            "source": self.source,
            "category": self.category,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "page_count": self.page_count,
            "skipped": self.skipped_count,
            "chunk_count": self.chunk_count,
            "total_tokens": self.total_tokens,
            "pages": [p.to_dict() for p in self.pages],
        }


class PullJob:
    """Pull one wiki source's pages into chunks + export (+ optional ingest)."""

    def __init__(
        self,
        client: MediaWikiClient,
        *,
        settings: dict,
        watermark: Watermark,
        export_dir: Optional[str] = None,
        ingest=None,
        category: Optional[str] = None,
        target_user_id: Optional[str] = None,
        max_segments: Optional[int] = None,
        titles_limit: int = 500,
    ) -> None:
        self.client = client
        self.settings = settings
        self.watermark = watermark
        self.export_dir = export_dir
        self.ingest = ingest
        self.category = category
        self.target_user_id = target_user_id
        self.max_segments = max_segments
        self.titles_limit = titles_limit

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    async def _safe_fetch(self, title: str) -> Optional[PageSource]:
        """Fetch one page, swallowing Cloudflare / client errors as 'skip'."""
        try:
            return await self.client.fetch_page(title)
        except ClientBackoffError as exc:
            logger.warning("Skipping %s: %s", title, exc)
            return None
        except (ClientError, Exception) as exc:  # noqa: BLE001 - batch job resilience
            logger.warning("Skipping %s (fetch error): %s", title, exc)
            return None

    async def _ingest_page(self, page_result: PagePullResult, page: PageSource) -> None:
        """Push one page's full extract into the ingest service, if wired."""
        if self.ingest is None or not page_result.chunks:
            return
        try:
            await self.ingest.ingest_wiki(
                category=self.category,
                page_title=page_result.title,
                source_url=page_result.url,
                revid=page_result.revid,
                text=page.extract,
                last_edit=None,
                target_user_id=self.target_user_id,
                max_segments=self.max_segments,
            )
            logger.info("Ingested wiki page %s (%d chunks)", page_result.title, len(page_result.chunks))
        except Exception as exc:  # noqa: BLE001 - ingest failure shouldn't abort pull
            logger.warning("Ingest of %s failed: %s", page_result.title, exc)

    # ------------------------------------------------------------------ #
    # orchestrator
    # ------------------------------------------------------------------ #
    async def run(
        self,
        *,
        titles: Optional[List[str]] = None,
        category: Optional[str] = None,
        catch_up: bool = False,
    ) -> PullResult:
        """Run the pull orchestrator (design §4.5)."""
        category = category or self.category
        pull_id = f"{(category or 'wiki').replace(' ', '-')}-{int(time.time())}"
        started_at = _now_iso()
        pages: List[PagePullResult] = []

        if titles is None:
            if not category:
                raise ValueError("PullJob.run requires a category or explicit titles")
            titles = await self.client.get_page_titles(category, limit=self.titles_limit)

        for title in titles:
            page = await self._safe_fetch(title)
            if page is None:
                pages.append(PagePullResult(title=title, skipped=True, reason="missing"))
                continue

            if not catch_up and not self.watermark.should_pull(page.page_id, page.revid):
                pages.append(
                    PagePullResult(
                        title=title,
                        page_id=page.page_id,
                        revid=page.revid,
                        url=page.url,
                        extract=page.extract,
                        skipped=True,
                        reason="already-ingested",
                    )
                )
                continue

            chunks = chunk_page(page, settings=self.settings)
            page_result = PagePullResult(
                title=page.title,
                page_id=page.page_id,
                revid=page.revid,
                url=page.url,
                source="wiki",
                extract=page.extract,
                chunks=chunks,
            )
            await self._ingest_page(page_result, page)
            if page.revid:
                self.watermark.set_revid(page.page_id, page.revid)
            pages.append(page_result)

        # Persist watermark, then write the export artifact.
        self.watermark.save()
        ended_at = _now_iso()
        pull_result = PullResult(
            pull_id=pull_id,
            source="wiki",
            category=category or "",
            started_at=started_at,
            ended_at=ended_at,
            pages=pages,
        )
        if self.export_dir:
            export_pull(pull_id, pull_result, out_dir=self.export_dir, category=category or "")
        return pull_result

    async def pull_one(self, title: str, *, catch_up: bool = False) -> PagePullResult:
        """Pull a single page (useful for tests / manual runs)."""
        page = await self._safe_fetch(title)
        if page is None:
            return PagePullResult(title=title, skipped=True, reason="missing")
        if not catch_up and not self.watermark.should_pull(page.page_id, page.revid):
            return PagePullResult(
                title=title,
                page_id=page.page_id,
                revid=page.revid,
                url=page.url,
                extract=page.extract,
                skipped=True,
                reason="already-ingested",
            )
        chunks = chunk_page(page, settings=self.settings)
        page_result = PagePullResult(
            title=page.title,
            page_id=page.page_id,
            revid=page.revid,
            url=page.url,
            source="wiki",
            extract=page.extract,
            chunks=chunks,
        )
        await self._ingest_page(page_result, page)
        if page.revid:
            self.watermark.set_revid(page.page_id, page.revid)
            self.watermark.save()
        return page_result
