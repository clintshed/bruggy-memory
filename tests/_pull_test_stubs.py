"""Shared offline stubs for the pull package tests (no live wiki egress).

The stub client + fake ingest let the whole pull layer (client, chunker,
watermark, pull job, export) be exercised deterministically.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from pull.wiki_client import PageSource


class StubWikiClient:
    """In-memory MediaWiki stand-in implementing the fetch_page/get_page_titles API."""

    def __init__(self, pages: Optional[List[PageSource]] = None, titles: Optional[List[str]] = None):
        self._pages = {p.title: p for p in (pages or [])}
        self._titles = titles if titles is not None else list(self._pages)

    async def get_page_titles(self, category, *, limit=500):
        return list(self._titles)[:limit]

    async def fetch_page(self, title, *, max_retries=None):
        return self._pages.get(title)

    async def close(self):
        return None


class FakeIngest:
    """Records ingest_wiki calls so tests can assert ingestion happened."""

    def __init__(self):
        self.ingested: List[dict] = []
        self.fail = False

    async def ingest_wiki(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.ingested.append(kwargs)
        return kwargs


class FakeChatSource:
    def __init__(self, batch=None):
        self.batch = batch
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self.batch
