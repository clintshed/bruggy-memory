"""End-to-end test: stub MediaWiki -> pull scheduler -> ingest service -> query.

Exercises the whole pipeline offline (no live wiki egress):

1. A real MediaWikiClient talks to a stub transport that returns MediaWiki-shaped
   JSON for category listing and per-page extract/sections.
2. The scheduler runs a catch-up pull that ingests wiki pages into an
   InMemoryZepBackend via the IngestionService.
3. The scheduler ingests a chat batch into the same service.
4. A query for the user returns BOTH wiki and chat episodes interleaved with
   provenance — the core acceptance criterion (design §3 / acceptance #1).

Also asserts idempotent incremental pulls: re-running daily pull with the same
revids skips, and the revid watermark is persisted to disk.
"""
import asyncio
import json
import os
import tempfile
import unittest

from config import Settings
from ingest import IngestionService
from pull.wiki_client import MediaWikiClient, PageSource
from scheduler import MemoryScheduler
from utils import parse_ts
from zep_client import ChatMessage
from in_memory_backend import InMemoryZepBackend


def _words(n):
    return " ".join(f"word{i % 60}" for i in range(n))


# Meaningful wiki prose that survives the min-words filter and mentions the
# query term ("password") so both wiki and chat sources can match together.
_WIKI_HISTORY = (
    "The history section documents how the password reset flow has evolved over "
    "many releases. Users who forget their password can reclaim their account by "
    "opening settings and clicking the reset link, then entering their email to "
    "receive a fresh password link by email. This page records the password reset "
    "procedures and the account recovery steps that support them across versions. "
)
_WIKI_FAQ = (
    "The frequently asked questions page explains what to do when a password reset "
    "email never arrives. If you did not receive your password reset link, check "
    "your spam folder first. Re-request the password reset link after a minute. The "
    "account password can always be changed later from the settings panel. "
)


def _make_transport():
    pages = {
        "History": {"pageid": 42, "title": "History", "revid": 100, "extract": _WIKI_HISTORY,
                    "sections": [{"title": "Body", "text": "", "segment": _words(90)}]},
        "FAQ": {"pageid": 43, "title": "FAQ", "revid": 200, "extract": _WIKI_FAQ,
                "sections": [{"title": "Q", "text": "", "segment": _words(90)}]},
    }

    async def transport(url, params, headers):
        if params.get("list") == "categorymembers":
            body = {"query": {"categorymembers": [{"title": "History"}, {"title": "FAQ"}]}}
            return 200, json.dumps(body)
        if "extracts" in (params.get("prop") or ""):
            title = params.get("titles", "")
            p = pages.get(title)
            if p is None:
                return 200, json.dumps({"query": {"pages": {}}})
            key = "42" if title == "History" else "43"
            return 200, json.dumps({"query": {"pages": {key: p}}})
        return 200, json.dumps({"query": {}})

    return transport


class _ChatSource:
    def __init__(self, batch):
        self.batch = batch

    async def __call__(self):
        return self.batch


def _async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestEndToEndStubWiki(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(staging_dir=self.tmp)
        self.backend = InMemoryZepBackend()
        self.service = IngestionService(self.backend, self.settings)
        client = MediaWikiClient("https://wiki.example", transport=_make_transport())
        self.chat_batch = {
            "discord_guild_id": "g1",
            "discord_channel_id": "c1",
            "discord_user_id": "user-1",
            "discord_username": "Trae",
            "messages": [
                ChatMessage(content="How can I reset my account password if I forgot it?", timestamp=parse_ts("2026-10-01T10:00:00Z"), role="user", user_id="user-1"),
                ChatMessage(content="Open the settings panel and click the password reset link to continue.", timestamp=parse_ts("2026-10-01T10:01:00Z"), role="assistant", user_id="user-1"),
            ],
            "max_segments": 20,
        }
        self.scheduler = MemoryScheduler(
            settings=self.settings,
            client_factory=lambda s: client,
            category="Cat",
            target_user_id=self.settings.zep_baseline_user_id,
            data_dir=self.tmp,
            export_dir=self.tmp,
            ingest_service=self.service,
            chat_source=_ChatSource(self.chat_batch),
        )

    def test_wiki_and_chat_queryable_together(self):
        # Pull (catch-up ingests wiki) + ingest chat via the scheduler.
        _async(self.scheduler.catch_up())
        _async(self.scheduler.ingest_chat_now())

        resp = _async(self.service.query("user-1", "password", include_wiki=True))
        kinds = {r.kind for r in resp.results}
        # Both the chat message and the wiki article for this user come back.
        self.assertIn("chat_segment", kinds)
        self.assertIn("wiki_segment", kinds)
        # Every result carries provenance.
        for r in resp.results:
            d = r.to_dict()
            self.assertIn("episode", d)
            self.assertIn("provenance", d)
            self.assertEqual(d["kind"], r.kind)

    def test_provenance_carrys_page_and_channel(self):
        _async(self.scheduler.catch_up())
        _async(self.scheduler.ingest_chat_now())
        resp = _async(self.service.query("user-1", "password", include_wiki=True))
        wiki = [r.to_dict() for r in resp.results if r.kind == "wiki_segment"]
        chat = [r.to_dict() for r in resp.results if r.kind == "chat_segment"]
        self.assertTrue(wiki)
        self.assertTrue(chat)
        # Wiki provenance points at the article + revid.
        self.assertIn("revid", wiki[0]["provenance"])
        self.assertIn("title", wiki[0]["provenance"])
        # Chat provenance points at the channel.
        self.assertTrue(chat[0].get("provenance", {}).get("channel"))

    def test_idempotent_incremental_pull(self):
        # First catch-up ingests and records watermarks.
        _async(self.scheduler.catch_up())
        watermark_path = os.path.join(self.tmp, "watermark.json")
        self.assertTrue(os.path.exists(watermark_path))
        # A subsequent daily pull with unchanged revids skips everything.
        res = _async(self.scheduler.daily_pull())
        self.assertEqual(res.skipped_count, res.page_count)
        self.assertEqual(res.chunk_count, 0)

    def test_newer_revid_repulls(self):
        _async(self.scheduler.catch_up())
        # Simulate the wiki advancing History to revid 101.
        page = PageSource(page_id="42", title="History", revid="101", url="u", extract="History",
                          sections=[{"title": "Body", "text": "", "segment": _words(90)}])
        client = MediaWikiClient("https://wiki.example", transport=_revid_transport(101))
        new_sched = MemoryScheduler(
            settings=self.settings, client_factory=lambda s: client, category="Cat",
            target_user_id=self.settings.zep_baseline_user_id, data_dir=self.tmp,
            ingest_service=self.service,
        )
        res = _async(new_sched.daily_pull())
        self.assertEqual(res.skipped_count, 0)
        self.assertGreater(res.chunk_count, 0)


def _revid_transport(revid=101):
    async def transport(url, params, headers):
        if params.get("list") == "categorymembers":
            return 200, json.dumps({"query": {"categorymembers": [{"title": "History"}]}})
        if "extracts" in (params.get("prop") or ""):
            return 200, json.dumps({"query": {"pages": {"42": {
                "pageid": 42, "title": "History", "revid": revid, "extract": "History",
                "sections": [{"title": "Body", "segment": _words(90)}]}}}})
        return 200, json.dumps({"query": {}})
    return transport


if __name__ == "__main__":
    unittest.main()
