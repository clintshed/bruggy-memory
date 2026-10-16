"""Per-page ``revid`` watermark for incremental pulls (design §4.3).

The pull job persists the last-ingested ``revid`` per page in
``data/watermark.json``. On the next run a page is only re-pulled / re-ingested
when its current ``revid`` is greater than the stored one, so re-runs are
idempotent and never duplicate. The watermark is a plain JSON dict of
``page_id -> revid`` and is written atomically (write temp + rename).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, ItemsView, Iterator, Optional

logger = logging.getLogger("pull.watermark")


@dataclass
class Watermark:
    """Mutable ``page_id -> revid`` store with JSON persistence."""

    revids: Dict[str, str] = field(default_factory=dict)
    path: Optional[str] = None

    # ------------------------------------------------------------------ #
    # access
    # ------------------------------------------------------------------ #
    def get_revid(self, page_id: str) -> str:
        """Last known revid for ``page_id`` ("" if none)."""
        return self.revids.get(str(page_id), "")

    def set_revid(self, page_id: str, revid: str) -> None:
        """Record ``revid`` as the last-ingested revid for ``page_id``."""
        self.revids[str(page_id)] = str(revid)

    def should_pull(self, page_id: str, current_revid: str) -> bool:
        """True if the page's current revid is newer than the watermark.

        ``current_revid`` empty (unknown) => pull (we can't prove it's ingested).
        Revids are compared numerically when both are integers (so "101" > "99"),
        falling back to lexical comparison otherwise.
        """
        current = str(current_revid or "").strip()
        stored = str(self.get_revid(page_id) or "").strip()
        if not current:
            return True
        if not stored:
            return True
        try:
            return int(current) > int(stored)
        except ValueError:
            return current > stored

    def items(self) -> ItemsView:
        return self.revids.items()

    def all_revids(self) -> Dict[str, str]:
        return dict(self.revids)

    def __len__(self) -> int:
        return len(self.revids)

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    @classmethod
    def empty(cls, path: Optional[str] = None) -> "Watermark":
        return cls(revids={}, path=path)

    @classmethod
    def load(cls, path: str) -> "Watermark":
        """Load a watermark from ``path`` (fresh object; mutates copy-free)."""
        if not path or not os.path.exists(path):
            return cls.empty(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            revids = {str(k): str(v) for k, v in data.items()}
            logger.info("Loaded watermark from %s (%d pages)", path, len(revids))
            return cls(revids=revids, path=path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read watermark %s (%s); starting fresh", path, exc)
            return cls.empty(path)

    def save(self) -> None:
        """Atomically persist the watermark to ``self.path``."""
        if not self.path:
            return
        revids = self.revids
        target = self.path
        tmp = f"{target}.tmp"
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(revids, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
