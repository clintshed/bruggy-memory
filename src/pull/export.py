"""Per-run export artifact for the pull job (design §4.6).

:func:`export_pull` writes one JSON file per ingested page plus a
``manifest.json`` summarising the run, under ``<out_dir>/pulls/<pull_id>/``.

Page file (matches "title, extract, provenance, chunk list" from the spec):

    data/pulls/<pull_id>/<title-slug>.json
    {
        "page_id", "title", "revid", "url", "source", "extract",
        "provenance": {...},
        "chunks": [ {text, section, seq, tokens, word_count, page_title}, ... ]
    }

manifest.json ties the run together:

    {
        "pull_id", "source", "category", "started_at", "ended_at",
        "page_count", "skipped", "chunk_count", "total_tokens",
        "pages": [ {title, page_id, revid, chunks, skipped, reason}, ... ]
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import List

logger = logging.getLogger("pull.export")


def _slugify(title: str) -> str:
    """Turn a page title into a safe, filesystem-friendly slug."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    if not slug:
        slug = "untitled"
    return slug[:120]


def export_pull(pull_id: str, pull_result, *, out_dir: str, category: str = "") -> str:
    """Write the export artifact for ``pull_result``; return the artifact dir."""
    root = os.path.join(out_dir, "pulls", pull_id)
    pages_dir = os.path.join(root, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    page_files: List[dict] = []
    chunk_count = 0
    total_tokens = 0
    skipped = 0

    for page in pull_result.pages:
        if page.skipped:
            skipped += 1
            continue
        chunk_count += len(page.chunks)
        total_tokens += sum(c.tokens for c in page.chunks)

        provenance = {
            "source": page.source,
            "category": category or pull_result.source,
            "page_id": page.page_id,
            "revid": page.revid,
            "title": page.title,
            "url": page.url,
            "exported_at": pull_result.ended_at,
        }
        doc = {
            "page_id": page.page_id,
            "title": page.title,
            "revid": page.revid,
            "url": page.url,
            "source": page.source,
            "extract": page.extract,
            "provenance": provenance,
            "chunks": [c.to_dict() for c in page.chunks],
        }
        fname = f"{page.page_id or _slugify(page.title)}.json"
        fpath = os.path.join(pages_dir, fname)
        with open(fpath, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        page_files.append(
            {
                "title": page.title,
                "page_id": page.page_id,
                "revid": page.revid,
                "chunks": len(page.chunks),
                "skipped": False,
            }
        )

    manifest = {
        "pull_id": pull_id,
        "source": pull_result.source,
        "category": category or pull_result.category,
        "started_at": pull_result.started_at,
        "ended_at": pull_result.ended_at,
        "page_count": len(page_files),
        "skipped": skipped,
        "chunk_count": chunk_count,
        "total_tokens": total_tokens,
        "pages": page_files,
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    logger.info(
        "Exported pull %s: %d pages, %d chunks (%d tokens) -> %s",
        pull_id,
        len(page_files),
        chunk_count,
        total_tokens,
        root,
    )
    return root


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
