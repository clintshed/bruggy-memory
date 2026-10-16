"""MediaWiki Action API client (pull layer).

A thin, section-aware client for the MediaWiki Action API used by the pull job.
Design highlights (wiki_to_zep_rag_design §4.1 / §4.5):

* **Browser user-agent, never a bot UA.** Some wikis (wiki.gg, the 7 Days to Die
  wiki) sit behind Cloudflare and reject obvious bot / python-client user
  agents with a challenge page or a 429. Every request carries a desktop
  browser UA.
* **Cloudflare backoff.** On 429/403/503, an empty body, or a response body that
  contains a known Cloudflare-challenge marker, the client backs off with
  exponential delay and retries (configurable). Exhausting the retry budget
  raises :class:`ClientBackoffError`; the :class:`~pull.pull_job.PullJob` treats
  that as "skip this page" rather than aborting the whole run.
* **Section extraction.** ``prop=extracts`` with ``exsectionformat=plain`` returns
  each section's text separately, which the section-aware chunker consumes.

The client is transport-seam based: pass a custom ``transport`` coroutine
``(url, params, headers) -> (status_code, body_text)`` to exercise the whole layer
offline against a stub MediaWiki (see the e2e / unit tests). The default
transport uses :mod:`httpx`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("pull.wiki")

# Desktop browser user-agent. This is a hard requirement of the pull layer — some
# source wikis sit behind Cloudflare and reject bot / python user agents. Do NOT
# substitute a recognizable automation UA here.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Cloudflare / anti-bot challenge markers detected in the response body. A single
# match is enough to treat the response as a challenge and back off.
_CHALLENGE_MARKERS = (
    "just a second...",
    "attention required!",
    "cf-challenge",
    "checking your browser",
    "js_chl_container",
    "cf-browser-verification",
    "unblock captcha",
    "why have I been blocked",
    "access denied",
    "security check",
)

# HTTP status codes that signal Cloudflare / rate-limit backoff.
_CF_STATUS = {429, 403, 503}


class MediaWikiClientError(Exception):
    """Base error for the MediaWiki client."""


class ClientError(MediaWikiClientError):
    """Non-recoverable client/server error (bad JSON, unexpected status)."""


class ClientBackoffError(MediaWikiClientError):
    """Cloudflare / rate-limit retry budget exhausted. The caller should skip."""


@dataclass
class BackoffConfig:
    """Cloudflare backoff tuning (design §4.1)."""

    max_retries: int = 4
    base: float = 2.0  # seconds; wait = min(base ** attempt, max)
    max: float = 60.0  # seconds, cap on any single backoff wait


@dataclass
class PageSource:
    """A single fetched page + its sections (design §4.5)."""

    page_id: str
    title: str
    revid: str
    url: str
    extract: str
    sections: List[dict] = field(default_factory=list)  # [{"title", "text"}]

    @property
    def source(self) -> str:
        return "wiki"

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "title": self.title,
            "revid": self.revid,
            "url": self.url,
            "source": self.source,
            "extract": self.extract,
            "sections": list(self.sections),
        }


def _has_challenge(body: str) -> bool:
    """True if ``body`` looks like a Cloudflare / anti-bot challenge page."""
    if not body or not body.strip():
        return True  # empty body => likely a challenge / dropped response
    low = body.lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


class MediaWikiClient:
    """Async MediaWiki Action API client with browser UA + Cloudflare backoff."""

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: Optional[str] = None,
        backoff: Optional[BackoffConfig] = None,
        transport: Optional[Callable] = None,
        sleep: Optional[Callable] = None,
        timeout: float = 30.0,
        api_path: str = "/w/api.php",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or BROWSER_UA
        self.backoff = backoff or BackoffConfig()
        # transport: (url, params, headers) -> (status_code, body_text)
        self.transport = transport or self._default_transport
        self.sleep = sleep or asyncio.sleep
        self.timeout = timeout
        self.api_path = api_path

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    async def _default_transport(self, url: str, params: dict, headers: dict):
        """Default transport over httpx (network)."""
        import httpx

        r = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
        return r.status_code, r.text

    # ------------------------------------------------------------------ #
    # request plumbing
    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def _cf_trigger(self, status: int, body: str) -> bool:
        return status in _CF_STATUS or _has_challenge(body)

    async def _get(self, params: dict, *, max_retries: Optional[int] = None) -> dict:
        """GET the Action API with Cloudflare backoff; return parsed JSON."""
        retries = self.backoff.max_retries if max_retries is None else max_retries
        attempt = 0
        url = self.base_url + self.api_path
        merged = dict(params)
        merged["format"] = "json"
        while True:
            status, body = await self.transport(
                url, {**merged, "format": "json"}, self._headers
            )
            if self._cf_trigger(status, body):
                attempt += 1
                if attempt > retries:
                    logger.warning(
                        "MediaWiki Cloudflare backoff exhausted after %d retries "
                        "(status=%s)",
                        attempt,
                        status,
                    )
                    raise ClientBackoffError(
                        f"Cloudflare/rate-limit backoff exhausted after {attempt} "
                        f"attempts (status={status})"
                    )
                wait = min(self.backoff.base ** attempt, self.backoff.max)
                logger.warning(
                    "MediaWiki backoff attempt %d/%d (status=%s) in %.1fs",
                    attempt,
                    retries,
                    status,
                    wait,
                )
                await self.sleep(wait)
                continue
            if status != 200:
                raise ClientError(f"MediaWiki returned HTTP {status}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise ClientError(f"MediaWiki returned invalid JSON: {exc}")

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def get_page_titles(
        self, category: str, *, limit: int = 500
    ) -> List[str]:
        """Return page titles in ``category`` (namespace 0), newest first."""
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmprop": "title",
            "cmnamespace": "0",
            "cmsort": "timestamp",
            "cmdir": "descending",
            "cmlimit": str(min(limit, 5000)),
        }
        data = await self._get(params)
        pages = data.get("query", {}).get("categorymembers", [])
        return [p["title"] for p in pages if p.get("title")]

    async def fetch_page(self, title: str, *, max_retries: Optional[int] = None) -> Optional[PageSource]:
        """Fetch one page's extract + sections + latest revid.

        Returns ``None`` when the page is missing / redirects away. Raises
        :class:`ClientBackoffError` on Cloudflare budget exhaustion, or
        :class:`ClientError` on malformed responses.
        """
        params = {
            "action": "query",
            "prop": "extracts|revisions",
            "redirects": "1",
            "rvprop": "id|timestamp",
            "rvlimit": "1",
            "exsectionformat": "plain",
            "exaintext": "1",
            "titles": title,
        }
        data = await self._get(params, max_retries=max_retries)
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return None
        page_id, page = next(iter(pages.items()))
        # Missing / redir-only page.
        if str(page_id).startswith("-1"):
            return None
        revid = None
        if page.get("revisions"):
            revid = str(page["revisions"][0]["revid"])
        # Some API shapes put the latest revid at the top level of the page.
        if not revid:
            revid = str(page.get("revid") or "")
        sections: List[dict] = []
        for sec in page.get("sections", []):
            text = sec.get("segment", "")
            if text is None:
                text = ""
            sections.append({"title": sec.get("title", "") or "(untitled)", "text": text})
        base = self.base_url.rstrip("/")
        url = f"{base}/index.php?curid={page_id}"
        return PageSource(
            page_id=str(page_id),
            title=page.get("title", title),
            revid=revid or "",
            url=url,
            extract=page.get("extract", "") or "",
            sections=sections,
        )
