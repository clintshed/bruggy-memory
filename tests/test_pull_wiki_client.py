"""Tests for pull/wiki_client (browser UA, Cloudflare backoff, section extraction).

Exercised entirely offline against a stub transport — no live wiki egress.
"""
import asyncio
import json
import unittest

from pull.wiki_client import (
    BackoffConfig,
    ClientBackoffError,
    ClientError,
    MediaWikiClient,
    PageSource,
    _has_challenge,
)


class _SleepLog:
    def __init__(self):
        self.calls = []

    async def __call__(self, d):
        self.calls.append(d)


class _Transport:
    """Emits a sequence of (status, body) tuples, then repeats the last."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, url, params, headers):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def _async(fn):
    return asyncio.new_event_loop().run_until_complete(fn)


class TestUserAgent(unittest.TestCase):
    def test_default_is_browser_not_bot(self):
        c = MediaWikiClient("https://wiki.example")
        self.assertNotIn("bot", c.user_agent.lower())
        self.assertIn("Chrome", c.user_agent)
        self.assertIn("AppleWebKit", c.user_agent)

    def test_no_recognisable_automation_ua(self):
        c = MediaWikiClient("https://wiki.example")
        lowered = c.user_agent.lower()
        for bad in ("bot", "python-requests", "curl", "okhttp", "go-http"):
            self.assertNotIn(bad, lowered, f"UA contains '{bad}'")

    def test_custom_ua_respected(self):
        c = MediaWikiClient("https://wiki.example", user_agent="MyAgent/1.0")
        self.assertEqual(c.user_agent, "MyAgent/1.0")


class TestChallengeDetection(unittest.TestCase):
    def test_markers_detected(self):
        for marker in ("Just a second...", "Attention Required!", "Checking your browser"):
            self.assertTrue(_has_challenge("blah " + marker + " blah"))

    def test_empty_body_is_challenge(self):
        self.assertTrue(_has_challenge(""))
        self.assertTrue(_has_challenge("   "))

    def test_normal_html_is_not_challenge(self):
        self.assertFalse(_has_challenge('{"query":{"pages":{}}'))

    def test_cf_status_codes(self):
        c = MediaWikiClient("https://wiki.example")
        self.assertTrue(c._cf_trigger(429, ""))
        self.assertTrue(c._cf_trigger(403, ""))
        self.assertTrue(c._cf_trigger(503, ""))
        self.assertFalse(c._cf_trigger(200, "ok"))


class TestRequestBuilding(unittest.TestCase):
    def test_get_adds_format_and_ua(self):
        seen = {}

        async def transport(url, params, headers):
            seen["params"] = params
            seen["headers"] = headers
            return 200, json.dumps({"query": {}})

        c = MediaWikiClient("https://wiki.example", transport=transport, sleep=lambda d: None)
        _async(c.get_page_titles("Cat"))
        self.assertEqual(seen["params"].get("format"), "json")
        self.assertIn("User-Agent", seen["headers"])
        self.assertEqual(seen["headers"]["User-Agent"], c.user_agent)


class TestGetPageTitles(unittest.TestCase):
    def test_parses_titles(self):
        resp = {"query": {"categorymembers": [{"title": "A", "redirects": 1}, {"title": "B"}]}}
        c = MediaWikiClient("https://wiki.example", transport=_Transport([(200, json.dumps(resp))]),
                            sleep=lambda d: None)
        titles = _async(c.get_page_titles("Cat"))
        self.assertEqual(titles, ["A", "B"])

    def test_missing_category_empty(self):
        c = MediaWikiClient("https://wiki.example", transport=_Transport([(200, json.dumps({"query": {}}))]),
                            sleep=lambda d: None)
        self.assertEqual(_async(c.get_page_titles("Cat")), [])


class TestFetchPage(unittest.TestCase):
    def _client(self, body):
        return MediaWikiClient("https://wiki.example", transport=_Transport([(200, body)]),
                               sleep=lambda d: None)

    def test_returns_pagesource_with_sections(self):
        page = {"pageid": 42, "title": "History", "revid": 100, "extract": "x",
                "sections": [{"title": "Origins", "segment": "hello world"}]}
        body = json.dumps({"query": {"pages": {"42": page}}})
        c = self._client(body)
        src = _async(c.fetch_page("History"))
        self.assertIsInstance(src, PageSource)
        self.assertEqual(src.page_id, "42")
        self.assertEqual(src.revid, "100")
        self.assertEqual(src.sections[0]["title"], "Origins")
        self.assertEqual(src.sections[0]["text"], "hello world")

    def test_missing_page_returns_none(self):
        body = json.dumps({"query": {"pages": {}}})
        c = self._client(body)
        self.assertIsNone(_async(c.fetch_page("Nope")))

    def test_negative_pageid_returns_none(self):
        body = json.dumps({"query": {"pages": {"-1": {"pageid": -1, "title": "NoPE"}}}})
        c = self._client(body)
        self.assertIsNone(_async(c.fetch_page("NoPE")))

    def test_invalid_json_raises_client_error(self):
        c = MediaWikiClient("https://wiki.example", transport=_Transport([(200, "not json")]),
                            sleep=lambda d: None)
        with self.assertRaises(ClientError):
            _async(c.get_page_titles("Cat"))

    def test_non_200_raises_client_error(self):
        c = MediaWikiClient("https://wiki.example", transport=_Transport([(500, "boom")]),
                            sleep=lambda d: None)
        with self.assertRaises(ClientError):
            _async(c.get_page_titles("Cat"))


class TestCloudflareBackoff(unittest.TestCase):
    def test_backoff_then_success(self):
        body = json.dumps({"query": {"pages": {"42": {"pageid": 42, "title": "H", "revid": 1,
                            "extract": "x", "sections": []}}}})
        c = MediaWikiClient("https://wiki.example",
                            transport=_Transport([(429, "Just a second..."), (200, body)]),
                            sleep=_SleepLog())
        _async(c.get_page_titles("Cat"))
        self.assertEqual(c.transport.calls, 2)
        self.assertEqual(len(c.sleep.calls), 1)

    def test_backoff_waits_increase_exponentially(self):
        body = json.dumps({"query": {"pages": {}}})
        c = MediaWikiClient("https://wiki.example",
                            backoff=BackoffConfig(max_retries=3, base=2.0, max=100),
                            transport=_Transport([(429, "x"), (429, "x"), (429, "x"), (200, body)]),
                            sleep=_SleepLog())
        _async(c.get_page_titles("Cat"))
        self.assertEqual(c.sleep.calls, [2.0, 4.0, 8.0])

    def test_exhaustion_raises(self):
        c = MediaWikiClient("https://wiki.example",
                            backoff=BackoffConfig(max_retries=2, base=1.0, max=10),
                            transport=_Transport([(403, "x")]),
                            sleep=_SleepLog())
        with self.assertRaises(ClientBackoffError):
            _async(c.get_page_titles("Cat"))
        # max_retries retries after the first attempt => 3 total calls
        self.assertEqual(c.transport.calls, 3)

    def test_challenge_marker_triggers_backoff(self):
        body = json.dumps({"query": {"categorymembers": []}})
        c = MediaWikiClient("https://wiki.example",
                            transport=_Transport([(200, "Just a second..."), (200, body)]),
                            sleep=_SleepLog())
        _async(c.get_page_titles("Cat"))
        self.assertEqual(c.transport.calls, 2)


if __name__ == "__main__":
    unittest.main()
