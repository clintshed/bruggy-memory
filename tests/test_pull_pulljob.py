"""Tests for pull/pull_job (orchestrator: pull + chunk + watermark + export)."""
import asyncio
import json
import os
import tempfile
import unittest

from pull.chunker import chunk_page
from pull.pull_job import PagePullResult, PullJob, PullResult
from pull.watermark import Watermark
from pull.wiki_client import ClientBackoffError, PageSource

from _pull_test_stubs import FakeIngest, StubWikiClient


def _words(n):
    return " ".join(f"w{i % 40}" for i in range(n))


def _make_page(title, revid, n_words=60):
    return PageSource(page_id=title.upper(), title=title, revid=str(revid), url=f"u/{title}",
                      extract=title, sections=[{"title": "Body", "text": _words(n_words)}])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class StubFlakyClient(StubWikiClient):
    """Raises ClientBackoffError for a configured set of titles."""

    def __init__(self, *a, broken=(), **k):
        super().__init__(*a, **k)
        self.broken = set(broken)

    async def fetch_page(self, title, *, max_retries=None):
        if title in self.broken:
            raise ClientBackoffError("cf")
        return await super().fetch_page(title, max_retries=max_retries)


class TestPullNewPages(unittest.TestCase):
    def test_catch_up_pulls_chunks_and_counts(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        result = _run(job.run(catch_up=True))
        self.assertIsInstance(result, PullResult)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.chunk_count, 1)
        self.assertEqual(result.pages[0].chunks[0].section, "Body")

    def test_pull_one_returns_chunks(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        pr = _run(job.pull_one("History", catch_up=True))
        self.assertFalse(pr.skipped)
        self.assertEqual(pr.chunks[0].word_count, len(_words(60).split()))

    def test_category_required(self):
        client = StubWikiClient()
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category=None)
        with self.assertRaises(ValueError):
            _run(job.run())  # no category, no titles


class TestWatermarkIncrement(unittest.TestCase):
    def test_skips_already_ingested(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        wm = Watermark.empty()
        wm.set_revid(page.page_id, "100")
        job = PullJob(client, settings={}, watermark=wm, category="Cat")
        result = _run(job.run(catch_up=False))
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.pages[0].reason, "already-ingested")

    def test_second_run_skips_when_watermark_recorded(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        wm = Watermark.empty()
        job = PullJob(client, settings={}, watermark=wm, category="Cat")
        _run(job.run(catch_up=True))  # records watermark 100
        self.assertEqual(wm.get_revid(page.page_id), "100")
        result = _run(job.run(catch_up=False))  # same revid -> skip
        self.assertEqual(result.skipped_count, 1)

    def test_catch_up_ignores_watermark(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        wm = Watermark.empty()
        wm.set_revid(page.page_id, "100")
        job = PullJob(client, settings={}, watermark=wm, category="Cat")
        result = _run(job.run(catch_up=True))
        self.assertEqual(result.skipped_count, 0)

    def test_newer_revid_pulled(self):
        page = _make_page("History", 200)
        client = StubWikiClient(pages=[page], titles=["History"])
        wm = Watermark.empty()
        wm.set_revid(page.page_id, "100")
        job = PullJob(client, settings={}, watermark=wm, category="Cat")
        result = _run(job.run(catch_up=False))
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.pages[0].revid, "200")


class TestMissingAndFailure(unittest.TestCase):
    def test_missing_title_skipped(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History", "Ghost"])
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        result = _run(job.run(catch_up=True))
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.pages[1].reason, "missing")

    def test_cf_exhaustion_skips_page_others_proceed(self):
        a = _make_page("A", 1)
        b = _make_page("B", 2)
        client = StubFlakyClient(pages=[a, b], titles=["A", "B"], broken={"A"})
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        result = _run(job.run(catch_up=True))
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.pages[0].reason, "missing")  # A broken -> treated missing
        self.assertEqual(result.pages[1].chunks[0].section, "Body")  # B succeeded

    def test_pull_one_cf_raises_but_run_skips(self):
        page = _make_page("A", 1)
        client = StubFlakyClient(pages=[page], titles=["A"], broken={"A"})
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        pr = _run(job.pull_one("A", catch_up=True))
        self.assertTrue(pr.skipped)


class TestIngestionWiring(unittest.TestCase):
    def test_ingest_called_when_wired(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        ingest = FakeIngest()
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat", ingest=ingest,
                      target_user_id="u1")
        _run(job.run(catch_up=True))
        self.assertEqual(len(ingest.ingested), 1)
        self.assertEqual(ingest.ingested[0]["page_title"], "History")
        self.assertEqual(ingest.ingested[0]["target_user_id"], "u1")

    def test_ingest_noop_when_none(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat", ingest=None)
        result = _run(job.run(catch_up=True))
        self.assertEqual(result.chunk_count, 1)

    def test_ingest_failure_does_not_abort_pull(self):
        page = _make_page("History", 100)
        client = StubWikiClient(pages=[page], titles=["History"])
        ingest = FakeIngest()
        ingest.fail = True
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat", ingest=ingest)
        result = _run(job.run(catch_up=True))  # ingest raises internally but is swallowed
        self.assertEqual(result.chunk_count, 1)


class TestExport(unittest.TestCase):
    def test_export_writes_manifest_and_pages(self):
        with tempfile.TemporaryDirectory() as d:
            page = _make_page("History", 100)
            client = StubWikiClient(pages=[page], titles=["History"])
            job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat", export_dir=d)
            _run(job.run(catch_up=True))
            root = os.path.join(d, "pulls")
            self.assertTrue(os.path.isdir(root))
            manifest_files = [f for f in os.listdir(root) if f.endswith(".json")]
            # manifest.json lives at the pull dir, not directly under pulls/.
            self.assertTrue(os.path.exists(os.path.join(root, list(os.listdir(root))[0], "manifest.json")))

    def test_result_counts(self):
        a = _make_page("A", 1)
        b = _make_page("B", 2)
        client = StubWikiClient(pages=[a, b], titles=["A", "B"])
        job = PullJob(client, settings={}, watermark=Watermark.empty(), category="Cat")
        result = _run(job.run(catch_up=True))
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.chunk_count, 2)


if __name__ == "__main__":
    unittest.main()
