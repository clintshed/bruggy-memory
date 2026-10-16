"""Scheduler for bruggy-memory: daily wiki pull + optional chat ingestion.

This is the top-level glue that ties the pull package (pull/) to the ingest
service (ingest.py):

* **Daily pull** — once per day at ``pull_hour_utc``, pull new wiki pages via
  :class:`~pull.pull_job.PullJob` (incremental by ``revid`` watermark).
* **Catch-up** — a one-time full ingest that ignores the watermark (initial load).
* **Chat ingestion** — if a ``chat_source`` callable yields chat batches, ingest
  them through the :class:`~ingest.IngestionService` so a user's own messages are
  queryable alongside the wiki articles they care about.

APScheduler is imported lazily so the package imports cleanly without it; a
plain threaded timer is used as the fallback so ``daily_pull`` / ``catch_up`` /
``ingest_chat_now`` remain fully callable/testable regardless.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from pull.pull_job import PullJob, PullResult
from pull.watermark import Watermark

logger = logging.getLogger("pull.scheduler")

DEFAULT_PULL_HOUR_UTC = 3  # 3 AM UTC daily pull


def next_run(hour_utc: int, now_utc: Optional[datetime] = None) -> datetime:
    """Next UTC datetime at ``hour_utc`` (today if in the future, else tomorrow)."""
    now = now_utc or datetime.now(timezone.utc)
    candidate = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def build_pull_id(category: str, ts: Optional[int] = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"{(category or 'wiki').replace(' ', '-')}-{ts}"


class MemoryScheduler:
    """Wires the pull package to an ingest service and schedules runs."""

    def __init__(
        self,
        *,
        settings: dict,
        client_factory: Callable[[dict], object],
        category: Optional[str] = None,
        target_user_id: Optional[str] = None,
        max_segments: Optional[int] = None,
        data_dir: str = "./data",
        export_dir: Optional[str] = None,
        pull_hour_utc: int = DEFAULT_PULL_HOUR_UTC,
        titles_limit: int = 500,
        chat_source: Optional[Callable] = None,
        ingest_service=None,
        scheduler_factory: Optional[Callable] = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory
        self.category = category
        self.target_user_id = target_user_id
        self.max_segments = max_segments
        self.data_dir = data_dir
        self.export_dir = export_dir or data_dir
        self.pull_hour_utc = pull_hour_utc
        self.titles_limit = titles_limit
        self.chat_source = chat_source
        self.ingest_service = ingest_service
        self._scheduler = None
        self.scheduler_factory = scheduler_factory

    # ------------------------------------------------------------------ #
    # watermark / job wiring
    # ------------------------------------------------------------------ #
    @property
    def watermark_path(self) -> str:
        return os.path.join(self.data_dir, "watermark.json")

    def _new_watermark(self) -> Watermark:
        return Watermark.load(self.watermark_path)

    def build_pull_job(self, client) -> PullJob:
        """Build a PullJob wired to this scheduler's ingest service + watermark."""
        return PullJob(
            client,
            settings=self.settings,
            watermark=self._new_watermark(),
            export_dir=self.export_dir,
            ingest=self.ingest_service,
            category=self.category,
            target_user_id=self.target_user_id,
            max_segments=self.max_segments,
            titles_limit=self.titles_limit,
        )

    # ------------------------------------------------------------------ #
    # pull operations
    # ------------------------------------------------------------------ #
    async def daily_pull(self, *, titles: Optional[List[str]] = None) -> PullResult:
        """Incremental daily pull (skips pages already ingested by revid)."""
        client = self.client_factory(self.settings)
        job = self.build_pull_job(client)
        try:
            return await job.run(titles=titles, category=self.category, catch_up=False)
        finally:
            # close an httpx-backed client if it has such a method
            close = getattr(client, "close", None)
            if callable(close):
                await close()

    async def catch_up(self, *, titles: Optional[List[str]] = None) -> PullResult:
        """Full ingest ignoring the watermark (initial load)."""
        client = self.client_factory(self.settings)
        job = self.build_pull_job(client)
        try:
            return await job.run(titles=titles, category=self.category, catch_up=True)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                await close()

    async def ingest_chat_now(self) -> Optional[dict]:
        """Ingest one chat batch from ``chat_source`` via the ingest service."""
        if not (self.ingest_service and self.chat_source):
            return None
        batch = await self.chat_source()
        if not batch:
            return None
        await self.ingest_service.ingest_chat(**batch)
        logger.info("Ingested chat batch: %s", {k: len(v) if isinstance(v, list) else v for k, v in batch.items()})
        return batch

    # ------------------------------------------------------------------ #
    # scheduler lifecycle (APScheduler, lazy)
    # ------------------------------------------------------------------ #
    def start(self, *, run_catch_up: bool = False) -> None:
        """Start the scheduled jobs. Returns the underlying scheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            sched = BackgroundScheduler()
        except Exception:  # noqa: BLE001
            sched = None

        if sched is not None:
            sched.add_job(self._run_daily, "cron", hour=self.pull_hour_utc, minute=0, id="daily_pull")
            if run_catch_up:
                sched.add_job(
                    self._run_catch_up,
                    "date",
                    run_date=datetime.now(timezone.utc) + timedelta(seconds=2),
                    id="catch_up",
                )
            self._scheduler = sched
            sched.start()
            logger.info("Scheduler started (daily pull %02d:00 UTC)", self.pull_hour_utc)
        else:
            # Fallback: nothing scheduled, but the operations remain callable.
            logger.info("APScheduler unavailable; MemoryScheduler operations callable directly")
        return sched

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # -- APScheduler job wrappers (sync, run in the scheduler's thread pool) --
    def _run_daily(self) -> PullResult:
        try:
            return asyncio.run(self.daily_pull())
        except Exception:  # noqa: BLE001
            logger.exception("daily pull failed")
            raise

    def _run_catch_up(self) -> PullResult:
        try:
            return asyncio.run(self.catch_up())
        except Exception:  # noqa: BLE001
            logger.exception("catch-up pull failed")
            raise
