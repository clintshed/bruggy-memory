"""API-layer tests using FastAPI's TestClient with the in-memory backend.

Covers /health, /memory/add (chat + wiki), /memory/query, and /memory/stats.
"""
import json
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_TESTS_DIR, os.path.dirname(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient

import api_server
from config import Settings
from in_memory_backend import InMemoryZepBackend
from utils import parse_ts


def _settings(**kw) -> Settings:
    """Same tuned chunking settings used by the ingestion tests."""
    s = Settings()
    s.max_segment_tokens = 20
    s.segment_token_overlap = 5
    s.chat_segment_min_chars = 20
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _app():
    backend = InMemoryZepBackend()
    return api_server.create_app(backend=backend, settings=_settings())


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app())

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["backend"], "InMemoryZepBackend")
        self.assertIn("stats", body)

    def test_add_and_query_chat(self):
        payload = {
            "type": "chat",
            "discord_guild_id": "111",
            "discord_channel_id": "222",
            "discord_user_id": "424242",
            "discord_username": "Trae",
            "messages": [
                {"content": "The admin panel is at settings.example.com.",
                 "timestamp": "2024-01-01T10:00:00Z", "message_id": "m1"},
                {"content": "Reset your password from the security tab.",
                 "timestamp": "2024-01-01T10:05:00Z", "message_id": "m2"},
            ],
        }
        r = self.client.post("/memory/add", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["session_id"], "chat:111:222")
        self.assertEqual(body["kind"], "chat")
        self.assertGreater(body["messages_ingested"], 0)

        q = {"user_id": "424242", "query": "admin panel", "n_results": 5}
        r = self.client.post("/memory/query", json=q)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["count"], 1)
        self.assertTrue(body["results"])

    def test_add_and_query_wiki(self):
        payload = {
            "type": "wiki",
            "category": "security",
            "page_title": "Password Reset",
            "source_url": "https://wiki.local/wiki/Password_Reset",
            "revid": "9988",
            "text": "To reset your password go to the security settings page at "
                    "settings.example.com and confirm via email.",
            "target_user_id": "424242",
        }
        r = self.client.post("/memory/add", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["session_id"], "wiki:security:9988")
        self.assertEqual(body["provenance"]["source"], "wiki")

        q = {"user_id": "424242", "query": "reset password", "n_results": 5}
        r = self.client.post("/memory/query", json=q)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["count"], 1)
        provenance = body["results"][0]["episode"]["provenance"] \
            if "provenance" in body["results"][0]["episode"] else None
        # The episode block includes source provenance.
        self.assertEqual(body["results"][0]["episode"]["source"], "wiki")

    def test_query_returns_provenance_on_every_result(self):
        chat = {
            "type": "chat",
            "discord_guild_id": "111", "discord_channel_id": "222",
            "discord_user_id": "424242", "discord_username": "Trae",
            "messages": [
                {"content": "The admin panel is at settings.example.com.",
                 "timestamp": "2024-01-01T10:00:00Z", "message_id": "m1"},
            ],
        }
        wiki = {
            "type": "wiki",
            "category": "security", "page_title": "Password Reset",
            "source_url": "https://wiki.local/wiki/Password_Reset",
            "revid": "9988",
            "text": "Reset your password on the security settings page.",
            "target_user_id": "424242",
        }
        self.client.post("/memory/add", json=chat)
        self.client.post("/memory/add", json=wiki)
        r = self.client.post("/memory/query", json={"user_id": "424242", "query": "reset", "n_results": 10})
        body = r.json()
        for res in body["results"]:
            self.assertIsNotNone(res["episode"]["source"])
            self.assertIn(res["episode"]["source"], {"chat", "wiki"})
            self.assertIsNotNone(res["episode"]["timestamp"])

    def test_stats(self):
        chat = {
            "type": "chat",
            "discord_guild_id": "111", "discord_channel_id": "222",
            "discord_user_id": "424242", "discord_username": "Trae",
            "messages": [
                {"content": "The admin panel is at settings.example.com.",
                 "timestamp": "2024-01-01T10:00:00Z", "message_id": "m1"},
            ],
        }
        self.client.post("/memory/add", json=chat)
        r = self.client.get("/memory/stats")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body.get("users", 0), 1)
        self.assertGreaterEqual(body.get("episodes", 0), 1)

    def test_bad_request_unknown_type(self):
        r = self.client.post("/memory/add", json={"type": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_chat_requires_messages(self):
        r = self.client.post("/memory/add", json={"type": "chat"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
