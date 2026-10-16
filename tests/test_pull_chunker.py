"""Tests for pull/chunker (section-aware, token-bounded chunking)."""
import unittest

from pull.chunker import DEFAULT_WIKI_CHUNK_TOKENS, WikiChunk, chunk_page, estimate_tokens
from pull.wiki_client import PageSource


def _words(n):
    return " ".join(f"word{i % 50}" for i in range(n))


def _page(sections=None, extract="", title="History", revid="1"):
    return PageSource(page_id="1", title=title, revid=revid, url="u", extract=extract,
                      sections=sections or [])


class TestEstimateTokens(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("   "), 0)

    def test_order_of_magnitude(self):
        self.assertGreater(estimate_tokens(_words(1000)), 100)
        self.assertLess(estimate_tokens(_words(10)), 50)

    def test_more_words_more_tokens(self):
        self.assertGreater(estimate_tokens(_words(200)), estimate_tokens(_words(20)))


class TestSectionDetection(unittest.TestCase):
    def test_splits_on_headings(self):
        body = " ".join(f"word{i}" for i in range(60))
        page = _page(extract=_words(60) + "\n\nHistory\n\n" + body + "\n\nOrigins\n\n" + body)
        chunks = chunk_page(page, settings={"wiki_min_words": 1})
        titles = sorted({c.section for c in chunks})
        self.assertIn("History", titles)
        self.assertIn("Origins", titles)

    def test_intro_before_first_heading(self):
        page = _page(extract=_words(60) + "\n\nHistory\n\n" + _words(60))
        chunks = chunk_page(page, settings={"wiki_min_words": 1})
        self.assertTrue(any(c.section == "(intro)" for c in chunks))
        self.assertTrue(any(c.section == "History" for c in chunks))

    def test_allcaps_heading_detected(self):
        body = " ".join(f"word{i}" for i in range(60))
        page = _page(extract=body + "\n\nHISTORY\n\n" + body + "\n\nOrigins\n\n" + body)
        chunks = chunk_page(page, settings={"wiki_min_words": 1})
        titles = {c.section for c in chunks}
        self.assertIn("HISTORY", titles)
        self.assertIn("Origins", titles)

    def test_line_ending_in_period_is_not_heading(self):
        # A sentence ending in a period should not be treated as a heading.
        page = _page(extract=_words(80))
        chunks = chunk_page(page, settings={"wiki_min_words": 1})
        self.assertEqual({c.section for c in chunks}, {"(intro)"})

    def test_explicit_sections_used_when_present(self):
        page = _page(sections=[
            {"title": "A", "text": _words(60)},
            {"title": "B", "text": _words(60)},
        ])
        chunks = chunk_page(page, settings={})
        self.assertEqual({c.section for c in chunks}, {"A", "B"})


class TestTokenBounding(unittest.TestCase):
    def test_segments_split_large_section(self):
        section_text = "\n".join(f"line{i} " + _words(6) for i in range(30))
        page = _page(sections=[{"title": "Big", "text": section_text}])
        settings = {"wiki_chunk_tokens": 15, "wiki_overlap_tokens": 3, "wiki_min_words": 1}
        chunks = chunk_page(page, settings=settings)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            # Allow for overlap lines carried into the next segment.
            self.assertLessEqual(c.tokens, settings["wiki_chunk_tokens"] + settings["wiki_overlap_tokens"] + 5)

    def test_min_words_skips_short_section(self):
        page = _page(sections=[{"title": "Short", "text": _words(10)}, {"title": "Long", "text": _words(60)}])
        chunks = chunk_page(page, settings={"wiki_min_words": 50})
        self.assertEqual({c.section for c in chunks}, {"Long"})

    def test_overlap_produces_continuity(self):
        section_text = "\n".join(f"line{i} " + _words(6) for i in range(20))
        page = _page(sections=[{"title": "S", "text": section_text}])
        settings = {"wiki_chunk_tokens": 15, "wiki_overlap_tokens": 2, "wiki_min_words": 1}
        chunks = chunk_page(page, settings=settings)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.seq == i for i, c in enumerate(chunks)))


class TestChunkShape(unittest.TestCase):
    def test_seq_sequential(self):
        page = _page(sections=[{"title": "S", "text": "\n".join(f"line{i} " + _words(6) for i in range(20))}])
        chunks = chunk_page(page, settings={})
        self.assertEqual([c.seq for c in chunks], list(range(len(chunks))))

    def test_word_count_matches(self):
        page = _page(sections=[{"title": "S", "text": _words(42)}])
        chunks = chunk_page(page, settings={"wiki_min_words": 0})
        self.assertEqual(chunks[0].word_count, len(_words(42).split()))

    def test_to_dict_shape(self):
        page = _page(sections=[{"title": "S", "text": _words(42)}])
        (chunk,) = chunk_page(page, settings={"wiki_min_words": 0})
        d = chunk.to_dict()
        for key in ("text", "section", "seq", "tokens", "word_count", "page_title"):
            self.assertIn(key, d)
        self.assertIsInstance(chunk, WikiChunk)

    def test_defaults(self):
        self.assertEqual(DEFAULT_WIKI_CHUNK_TOKENS, 600)


if __name__ == "__main__":
    unittest.main()
