"""Pull package — MediaWiki ingestion layer for bruggy-memory.

Reconstructed from the wiki_to_zep_rag_design spec (t_dd922f18) to mirror the
``pull/`` conventions of discord-brussy-forever's ``wow/`` retrieval package:

* :class:`~pull.wiki_client.MediaWikiClient` — MediaWiki Action API client with a
  browser user-agent (never a bot UA) and Cloudflare backoff.
* :class:`~pull.chunker.WikiChunk` + :func:`~pull.chunker.chunk_page` — section-aware
  chunker (split on headings, token-bounded segments with light overlap).
* :class:`~pull.watermark.Watermark` — per-page ``revid`` watermark persisted to
  ``data/watermark.json`` for incremental pulls.
* :class:`~pull.pull_job.PullJob` — orchestrates pull + chunk + watermark + export.
* :func:`~pull.export.export_pull` — writes the per-run export artifact (one JSON per
  page + a manifest).

The package is intentionally transport-seam based: :class:`MediaWikiClient` takes an
optional ``transport`` so the whole layer (UA, backoff, section extraction) can be
exercised offline against a stub, with no live wiki egress.
"""
from __future__ import annotations

from pull.wiki_client import (
    BackoffConfig,
    ClientBackoffError,
    ClientError,
    MediaWikiClient,
    MediaWikiClientError,
    PageSource,
)
from pull.chunker import WikiChunk, chunk_page, estimate_tokens
from pull.watermark import Watermark
from pull.pull_job import PagePullResult, PullJob, PullResult
from pull.export import export_pull

__all__ = [
    "BackoffConfig",
    "ClientBackoffError",
    "ClientError",
    "MediaWikiClient",
    "MediaWikiClientError",
    "PageSource",
    "WikiChunk",
    "chunk_page",
    "estimate_tokens",
    "Watermark",
    "PagePullResult",
    "PullJob",
    "PullResult",
    "export_pull",
]

__version__ = "0.1.0"
