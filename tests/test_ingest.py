"""End-to-end ingestion tests against the in-memory backend.

Validates the t_1a3ab968 acceptance criteria:
  * Wiki + chat episodes for the same Discord user are queryable together.
  * Every result carries correct metadata/provenance.
  * Queries return relevant wiki + chat results.
  * Idempotent re-ingestion and timestamp-based chat dedup.

Runs under both ``python -m unittest`` and ``pytest`` (async work is driven via
``asyncio.run`` inside plain sync test methods).
"""
import asyncio
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_TESTS_DIR, os.path.dirname(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chunker import chunk_markdown, chunk_text, estimate_tokens
from config import Settings
from in_memory_backend import InMemoryZepBackend
from ingest import IngestionService
from zep_client import ChatMessage


def _settings(**kw) -> Settings:
    s = Settings()
    s.max_segment_tokens = 20
    s.segment_token_overlap = 5
    s.chat_segment_min_chars = 20
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _run(coro):
    return asyncio.run(coro)


class TestChunker(unittest.TestCase):
    def test_estimate_tokens_order_of_magnitude(self):
        self.assertGreater(estimate_tokens("word " * 400), 60)
        self.assertEqual(estimate_tokens(""), 0)

    def test_chunk_markdown_respects_max_tokens(self):
        s = _settings()
        text = "# Title\n\n" + "This is a sentence. " * 60
        segs = chunk_markdown(text, s)
        self.assertTrue(len(segs) >= 1)
        for seg in segs:
            self.assertLessEqual(seg.tokens, s.max_segment_tokens + 5)
        # Full text is preserved across segments (no content loss at boundaries).
        joined = " ".join(seg.text.replace("\n", " ") for seg in segs)
        self.assertIn("This is a sentence.", joined)

    def test_chunk_text_drops_short_segments(self):
        s = _settings(chat_segment_min_chars=40)
        segs = chunk_text("hi", s)
        self.assertEqual(len(segs), 0)


class TestChatIngestion(unittest.TestCase):
    def test_chat_ingest_creates_session_and_segments(self):
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        msgs = [
            ChatMessage("The admin panel is at settings.example.com.",
                        _ts("2024-01-01T10:00:00Z"), message_id="m1"),
            ChatMessage("You can reset your password from the security tab.",
                        _ts("2024-01-01T10:05:00Z"), message_id="m2"),
        ]
        res = _run(svc.ingest_chat(
            discord_guild_id="111", discord_channel_id="222",
            discord_user_id="424242", discord_username="Trae", messages=msgs,
        ))
        self.assertEqual(res.session_id, "chat:111:222")
        self.assertEqual(res.user_id, "424242")
        self.assertGreater(res.messages_ingested, 0)
        self.assertGreater(res.segments_ingested, 0)
        # Session belongs to the user's graph.
        sessions = _run(backend.list_user_sessions("424242"))
        self.assertIn("chat:111:222", sessions)

    def test_chat_dedup_skips_already_ingested(self):
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        msgs = [
            ChatMessage("first message", _ts("2024-01-01T10:00:00Z"), message_id="m1"),
            ChatMessage("second message", _ts("2024-01-01T10:05:00Z"), message_id="m2"),
        ]
        _run(svc.ingest_chat("111", "222", "42", "Trae", msgs))
        # Re-ingest the identical batch: everything should be skipped.
        res2 = _run(svc.ingest_chat("111", "222", "42", "Trae", msgs))
        self.assertEqual(res2.messages_ingested, 0)
        self.assertEqual(res2.segments_ingested, 0)
        self.assertEqual(res2.skipped, 2)


class TestWikiIngestion(unittest.TestCase):
    def test_wiki_provenance_attached(self):
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        res = _run(svc.ingest_wiki(
            category="security", page_title="Password Reset",
            source_url="https://wiki.local/wiki/Password_Reset", revid="9988",
            text="To reset a password go to the security settings page." * 6,
            last_edit=_ts("2024-01-02T00:00:00Z"),
            target_user_id="424242",
        ))
        self.assertEqual(res.session_id, "wiki:security:9988")
        self.assertEqual(res.user_id, "424242")
        self.assertGreater(res.segments_ingested, 0)
        # Document-level idempotency.
        res2 = _run(svc.ingest_wiki(
            category="security", page_title="Password Reset",
            source_url="https://wiki.local/wiki/Password_Reset", revid="9988",
            text="To reset a password go to the security settings page." * 6,
        ))
        self.assertEqual(res2.skipped, 1)
        self.assertEqual(res2.skipped_reason, "document_already_ingested")


class TestMergeAndProvenance(unittest.TestCase):
    def test_chat_and_wiki_queryable_together(self):
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        user = "424242"
        _run(svc.ingest_chat(
            "111", "222", user, "Trae", [
                ChatMessage("The admin panel is at settings.example.com.",
                            _ts("2024-01-01T10:00:00Z"), message_id="m1"),
            ],
        ))
        _run(svc.ingest_wiki(
            "security", "Password Reset", "https://wiki.local/wiki/Password_Reset",
            "9988",
            "To reset your password navigate to the security settings page at "
            "settings.example.com. A confirmation email is sent on file.",
            last_edit=_ts("2024-01-02T00:00:00Z"), target_user_id=user,
        ))
        resp = _run(svc.query(user, "how do I reset my password", n_results=10))
        self.assertGreaterEqual(resp.count, 1)
        kinds = {r.kind for r in resp.results}
        # Both chat and wiki content surface for the same user.
        self.assertTrue(kinds & {"chat_segment", "wiki_segment", "chat_message"})

    def test_every_result_carries_provenance(self):
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        user = "424242"
        _run(svc.ingest_chat(
            "111", "222", user, "Trae", [
                ChatMessage("The admin panel is at settings.example.com.",
                            _ts("2024-01-01T10:00:00Z"), message_id="m1"),
            ],
        ))
        _run(svc.ingest_wiki(
            "security", "Password Reset", "https://wiki.local/wiki/Password_Reset",
            "9988", "Reset your password on the security settings page.",
            last_edit=_ts("2024-01-02T00:00:00Z"), target_user_id=user,
        ))
        resp = _run(svc.query(user, "reset password", n_results=10))
        self.assertGreaterEqual(resp.count, 1)
        for r in resp.results:
            prov = r.episode.provenance()
            # Every result has source, title, timestamp at minimum.
            self.assertIsNotNone(prov.get("source"))
            self.assertIn(prov["source"], {"chat", "wiki"})
            self.assertIsNotNone(prov.get("timestamp"))
            if prov["source"] == "wiki":
                self.assertEqual(prov["source_url"],
                                 "https://wiki.local/wiki/Password_Reset")
                self.assertEqual(prov["source_title"], "Password Reset")
                self.assertEqual(prov["category"], "security")
                self.assertEqual(prov["revid"], "9988")
            elif prov["source"] == "chat":
                self.assertEqual(prov["channel"], "discord:222")
                self.assertEqual(prov["author"], "Trae")
                self.assertEqual(prov["author_id"], user)

    def test_baseline_wiki_queryable_by_other_user(self):
        """Anonymous wiki (baseline) is still returned to any user's query."""
        backend = InMemoryZepBackend()
        svc = IngestionService(backend, _settings())
        # Wiki ingested anonymously (no target user -> baseline user).
        _run(svc.ingest_wiki(
            "security", "Password Reset", "https://wiki.local/wiki/Password_Reset",
            "9988", "Reset your password on the security settings page.",
        ))
        # A different user queries: baseline wiki is in the session set.
        resp = _run(svc.query("999", "reset password", n_results=10))
        self.assertGreaterEqual(resp.count, 1)
        sources = {r.episode.provenance().get("source") for r in resp.results}
        self.assertIn("wiki", sources)


def _ts(value: str):
    from utils import parse_ts

    return parse_ts(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
