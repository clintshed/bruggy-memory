"""Tests for scheduler (daily pull + catch-up + chat ingestion + APScheduler)."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone

from scheduler import DEFAULT_PULL_HOUR_UTC, MemoryScheduler, build_pull_id, next_run

from _pull_test_stubs import FakeChatSource, StubWikiClient


def _async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeIngest:
    def __init__(self):
        self.wiki_calls = []
        self.chat_calls = []

    async def ingest_wiki(self, **kwargs):
        self.wiki_calls.append(kwargs)

    async def ingest_chat(self, **kwargs):
        self.chat_calls.append(kwargs)


def _make_page(title, revid):
    from pull.wiki_client import PageSource

    return PageSource(page_id=title.upper(), title=title, revid=str(revid), url="u",
                      extract=title, sections=[{"title": "Body", "text": " ".join(f"w{i}" for i in range(60))}])


class TestHelpers(unittest.TestCase):
    def test_build_pull_id(self):
        pid = build_pull_id("Cat", ts=123)
        self.assertEqual(pid, "Cat-123")

    def test_next_run_today_if_future(self):
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        run = next_run(3, now)
        self.assertEqual((run.year, run.month, run.day, run.hour), (2026, 1, 1, 3))

    def test_next_run_tomorrow_if_past(self):
        now = datetime(2026, 1, 1, 5, 0, 0, tzinfo=timezone.utc)
        run = next_run(3, now)
        self.assertEqual((run.year, run.month, run.day, run.hour), (2026, 1, 2, 3))


class TestPullOps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_daily_pull_incremental(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        ingest = _FakeIngest()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                ingest_service=ingest, data_dir=self.tmp)
        res = _async(sched.daily_pull())
        self.assertEqual(res.chunk_count, 1)
        self.assertEqual(len(ingest.wiki_calls), 1)

    def test_daily_pull_skips_when_watermarked(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        ingest = _FakeIngest()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                ingest_service=ingest, data_dir=self.tmp)
        # First run ingests and records watermark; second run skips.
        _async(sched.daily_pull())
        res2 = _async(sched.daily_pull())
        self.assertEqual(res2.skipped_count, 1)

    def test_catch_up_ignores_watermark(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        ingest = _FakeIngest()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                ingest_service=ingest, data_dir=self.tmp)
        _async(sched.catch_up())  # records watermark 100
        res2 = _async(sched.catch_up())  # catch-up again -> re-ingests
        self.assertEqual(res2.skipped_count, 0)

    def test_ingest_chat_now(self):
        client = StubWikiClient()
        ingest = _FakeIngest()
        batch = {"discord_guild_id": "g", "discord_channel_id": "c", "discord_user_id": "u",
                 "discord_username": "me", "messages": [], "max_segments": 20}
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                ingest_service=ingest, chat_source=FakeChatSource(batch=batch),
                                data_dir=self.tmp)
        result = _async(sched.ingest_chat_now())
        self.assertEqual(result, batch)
        self.assertEqual(len(ingest.chat_calls), 1)

    def test_ingest_chat_now_noop_without_source(self):
        client = StubWikiClient()
        ingest = _FakeIngest()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                ingest_service=ingest, chat_source=None, data_dir=self.tmp)
        self.assertIsNone(_async(sched.ingest_chat_now()))


class TestSchedulerLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_start_registers_daily_job(self):
        client = StubWikiClient()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                data_dir=self.tmp, pull_hour_utc=DEFAULT_PULL_HOUR_UTC)
        apsched = sched.start()
        try:
            self.assertIsNotNone(apsched)
            ids = {j.id for j in apsched.get_jobs()}
            self.assertIn("daily_pull", ids)
        finally:
            sched.stop()

    def test_stop_clears_scheduler(self):
        client = StubWikiClient()
        sched = MemoryScheduler(settings={}, client_factory=lambda s: client, category="Cat",
                                data_dir=self.tmp)
        sched.start()
        self.assertIsNotNone(sched._scheduler)
        sched.stop()
        self.assertIsNone(sched._scheduler)


if __name__ == "__main__":
    unittest.main()
