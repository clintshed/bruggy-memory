"""Section-aware chunker for wiki pages (pull layer).

Unlike the ingest service's markdown-block :mod:`chunker`, this splits a page's
plaintext *on headings into sections*, then packs each section into token-bounded
segments with light overlap (wiki_to_zep_rag_design §4.2):

* Split the page text on headings into sections (intro before the first heading).
* For each section, pack lines into segments of at most ``wiki_chunk_tokens``
  tokens with ``wiki_overlap_tokens`` tokens of overlap between neighbours.
* Skip sections that are empty or under ``wiki_min_words`` words.
* Every :class:`WikiChunk` carries its section name and sequence index so Zep
  provenance can show "which section of which article" a segment came from.

The heading detector is deliberately light: a line counts as a heading when it is
short, has no trailing sentence punctuation, few words, and introduces a content
block. This handles both the plain MediaWiki extractor output and the stub used
in offline tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# Reasonable defaults (design §4.2 / §5.3): ~600 token wiki segments, ~10 token
# overlap, skip sections under 50 words.
DEFAULT_WIKI_CHUNK_TOKENS = 600
DEFAULT_WIKI_OVERLAP_TOKENS = 10
DEFAULT_WIKI_MIN_WORDS = 50


def _cfg(settings, key, default):
    """Read a chunker setting from either a dict or a settings object.

    The pull chunker may be handed a plain ``dict`` of overrides (tests) or the
    application :class:`~config.Settings` dataclass (scheduler). Support both.
    """
    try:
        return settings.get(key, default)
    except AttributeError:
        return getattr(settings, key, default)


def estimate_tokens(text: str) -> int:
    """Rough token estimator: ~1 token / 4 words + symbol overhead.

    Mirrors the ingest service's chunker estimator so both layers agree in
    order of magnitude.
    """
    if not text:
        return 0
    words = len(text.split())
    # Symbols (~non-space chars) add tokens too.
    symbols = sum(1 for ch in text if not ch.isspace())
    return int(words / 4.0) + int(symbols / 4.0)


def _is_heading(line: str, idx: int, total: int, next_has_content: bool) -> bool:
    """Heuristic: does this line look like a section heading?

    A heading is short, has no trailing sentence punctuation, has few words,
    is not a list/markup line, and is followed by (some) content — where
    "followed" means a later non-blank line exists (wiki text often has a blank
    line between a heading and its body).
    """
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > 80 or len(stripped.split()) > 6:
        return False
    if stripped[-1] in ".!?":
        return False
    if stripped[0] in "*#;:»-":  # list / markup / definition lines
        return False
    if not next_has_content:
        return False
    # All-caps short line (e.g. "HISTORY") reads as a heading.
    if stripped.isupper() and any(c.isalpha() for c in stripped):
        return True
    # Capitalised first word + few words, not ending in punctuation.
    words = stripped.split()
    if len(words) <= 6 and (words[0][0].isupper() or any(c.isupper() for c in words[0])):
        # Guard against prose sentences that happen to start capitalised:
        # they usually end in a period (excluded above) or are long.
        return True
    return False


def _has_content_after(lines, idx: int) -> bool:
    return any(lines[j].strip() for j in range(idx + 1, len(lines)))


def _split_on_headings(extract: str, settings: Dict) -> List[Tuple[str, str]]:
    """Split ``extract`` into ``(section_title, section_text)`` pairs."""
    lines = extract.splitlines()
    sections: List[Tuple[str, str]] = []
    current_title = "(intro)"
    current: List[str] = []

    def _flush() -> None:
        nonlocal current, current_title
        if current:
            text = "\n".join(l for l in current if l.strip())
            if text.strip():
                sections.append((current_title, text.strip()))
        current = []
        current_title = "(intro)"

    for i, line in enumerate(lines):
        if _is_heading(line, i, len(lines), _has_content_after(lines, i)):
            _flush()
            current_title = line.strip()
            current = []
        else:
            current.append(line)
    _flush()
    return sections


@dataclass
class WikiChunk:
    """One token-bounded segment of a wiki section."""

    text: str
    section: str
    seq: int
    tokens: int
    word_count: int
    page_title: str = ""

    def to_dict(self, *, page_title: str = "") -> dict:
        return {
            "text": self.text,
            "section": self.section,
            "seq": self.seq,
            "tokens": self.tokens,
            "word_count": self.word_count,
            "page_title": page_title or self.page_title,
        }


def _pack_section(section_text: str, settings: Dict) -> List[str]:
    """Pack one section's text into token-bounded segments with overlap."""
    chunk_tokens = int(_cfg(settings, "wiki_chunk_tokens", DEFAULT_WIKI_CHUNK_TOKENS))
    overlap = int(_cfg(settings, "wiki_overlap_tokens", DEFAULT_WIKI_OVERLAP_TOKENS))
    lines = [l for l in section_text.splitlines() if l.strip()]
    if not lines:
        return []

    segments: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for line in lines:
        line_tokens = estimate_tokens(line)
        # Start a new segment if this line alone would overflow, or if adding it
        # would push the current segment past the cap.
        if current and (current_tokens + line_tokens > chunk_tokens or line_tokens >= chunk_tokens):
            segments.append("\n".join(current))
            # Overlap: carry the last couple of lines into the next segment.
            if overlap > 0 and len(current) > 1:
                carry = min(overlap, len(current))
                current = current[-carry:]
                current_tokens = estimate_tokens("\n".join(current))
            else:
                current = []
                current_tokens = 0
        current.append(line)
        current_tokens += line_tokens
    if current:
        segments.append("\n".join(current))
    return segments


def chunk_page(page, *, settings: Dict) -> List[WikiChunk]:
    """Chunk a page (:class:`~pull.wiki_client.PageSource` or dict) into segments.

    Uses the page's explicit ``sections`` (from the MediaWiki API) when present;
    otherwise splits the page extract on headings. Empty / short sections are
    skipped.
    """
    min_words = int(_cfg(settings, "wiki_min_words", DEFAULT_WIKI_MIN_WORDS))
    page_title = getattr(page, "title", "")
    extract = getattr(page, "extract", "") or ""
    sections = getattr(page, "sections", []) or []

    if sections:
        pairs = [(s.get("title", "(untitled)") or "(untitled)", s.get("text", "") or "")
                 for s in sections]
    else:
        pairs = _split_on_headings(extract, settings)

    chunks: List[WikiChunk] = []
    for title, text in pairs:
        segments = _pack_section(text, settings)
        for seq, seg in enumerate(segments):
            wc = len(seg.split())
            if wc < min_words:
                continue
            chunks.append(
                WikiChunk(
                    text=seg,
                    section=title,
                    seq=seq,
                    tokens=estimate_tokens(seg),
                    word_count=wc,
                    page_title=page_title,
                )
            )
    return chunks
