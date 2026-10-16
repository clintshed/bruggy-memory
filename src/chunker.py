"""Chunking of wiki pages (Markdown) and chat messages (free text).

Chunking lives in the ingestion service so the pull job and the ingest job share
exactly one implementation (no drift between the two stages).

Behaviour
---------
* Markdown is first split into logical blocks by headings, then packed into
  segments up to ``max_tokens`` tokens, keeping paragraphs intact and overlapping
  consecutive segments by ``token_overlap`` tokens so nothing is lost at the
  boundary.
* Plain text (chat) is split by blank lines / sentence boundaries and packed the
  same way.
* Empty and sub-threshold chunks are dropped.

Token estimation is deliberately cheap (words / 4 + a small constant); it never
requires an external tokenizer, which keeps the service dependency-free.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import List

from config import Settings

_TOKEN_RE = re.compile(r"\b[\w']+\b|\d+\.\d+%")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*$")


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 words per token, counting common symbols."""
    if not text:
        return 0
    words = len(re.findall(r"\S+", text))
    return int(words / 4.0) + len(_TOKEN_RE.findall(text))


@dataclass
class Segment:
    """A single chunk of text with its provenance attached."""

    text: str
    index: int  # 0-based position within the source document
    tokens: int
    # Optional rich provenance attached by the ingest layer.
    business_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "index": self.index,
            "tokens": self.tokens,
            "business_data": self.business_data,
        }


def _split_markdown_blocks(text: str) -> List[str]:
    """Split Markdown into heading-prefixed blocks, preserving structure."""
    lines = text.splitlines()
    blocks: List[str] = []
    current: List[str] = []
    for line in lines:
        if _HEADING_RE.match(line):
            if current:
                blocks.append("\n".join(current))
                current = []
            blocks.append(line)
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    # Strip empty blocks.
    return [b for b in blocks if b.strip()]


def _split_text_blocks(text: str) -> List[str]:
    """Split free text into block-level units.

    Multi-paragraph text is split on blank lines. A single long paragraph is
    split on sentence boundaries so oversized blocks can still be re-packed
    under the token cap by :func:`_pack_blocks`.
    """
    text = text.strip()
    if not text:
        return []
    paragraphs = re.split(r"\n\s*\n", text)
    if len(paragraphs) > 1:
        return [p.strip() for p in paragraphs if p.strip()]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def _pack_blocks(blocks: List[str], max_tokens: int, overlap: int) -> List[str]:
    """Greedily pack blocks into segments of <= max_tokens with overlap."""
    if max_tokens <= 0:
        return list(blocks)
    max_tokens = max(max_tokens, overlap + 1)
    segments: List[str] = []
    current_tokens = 0
    current_parts: List[str] = []
    for block in blocks:
        block_tokens = estimate_tokens(block)
        if block_tokens > max_tokens:
            # A single block larger than the cap: split it into sentence-level
            # pieces so we still make progress, packing them back under the cap.
            if current_parts:
                segments.append("\n".join(current_parts))
                current_parts, current_tokens = [], 0
            for piece in _split_text_blocks(block):
                piece_tokens = estimate_tokens(piece)
                if not piece:
                    continue
                if piece_tokens >= max_tokens:
                    # Oversized single piece: flush then emit standalone.
                    if current_parts:
                        segments.append("\n".join(current_parts))
                        current_parts, current_tokens = [], 0
                    segments.append(piece)
                elif current_parts and current_tokens + piece_tokens > max_tokens:
                    segments.append("\n".join(current_parts))
                    current_parts, current_tokens = [piece], piece_tokens
                else:
                    current_parts.append(piece)
                    current_tokens += piece_tokens
            continue
        if current_parts and current_tokens + block_tokens > max_tokens:
            segments.append("\n".join(current_parts))
            # Apply overlap: keep the last `overlap`-ish token worth of text.
            tail = _overlap_tail(current_parts, overlap)
            current_parts = tail
            current_tokens = estimate_tokens("\n".join(tail)) if tail else 0
        current_parts.append(block)
        current_tokens += block_tokens
    if current_parts:
        segments.append("\n".join(current_parts))
    return segments


def _overlap_tail(parts: List[str], overlap_tokens: int) -> List[str]:
    """Keep enough trailing parts to cover ~overlap_tokens of text."""
    if overlap_tokens <= 0 or not parts:
        return []
    kept: List[str] = []
    total = 0
    for part in reversed(parts):
        p_tokens = estimate_tokens(part)
        if kept and total + p_tokens > overlap_tokens:
            break
        kept.insert(0, part)
        total += p_tokens
    return kept


def chunk_markdown(text: str, settings: Settings, business_data: dict | None = None) -> List[Segment]:
    """Chunk a Markdown wiki page into :class:`Segment` objects."""
    return _chunk_blocks(_split_markdown_blocks(text), settings, business_data)


def chunk_text(text: str, settings: Settings, business_data: dict | None = None) -> List[Segment]:
    """Chunk free text (e.g. a chat message) into :class:`Segment` objects."""
    return _chunk_blocks(_split_text_blocks(text), settings, business_data)


def _chunk_blocks(
    blocks: List[str], settings: Settings, business_data: dict | None
) -> List[Segment]:
    segments: List[Segment] = []
    packed = _pack_blocks(blocks, settings.max_segment_tokens, settings.segment_token_overlap)
    for i, raw in enumerate(packed):
        text = raw.strip()
        if not text:
            continue
        if len(text) < settings.chat_segment_min_chars:
            continue
        seg = Segment(
            text=text,
            index=i,
            tokens=estimate_tokens(text),
            business_data=dict(business_data) if business_data else {},
        )
        segments.append(seg)
    return segments
