"""Tests for pull/export (per-run export artifact: manifest + per-page JSON)."""
import json
import os
import tempfile
import unittest

from pull.chunker import chunk_page
from pull.export import export_pull
from pull.pull_job import PagePullResult, PullResult
from pull.wiki_client import PageSource


def _words(n):
    return " ".join(f"w{i % 40}" for i in range(n))


def _make_page(title, revid, n_words=60):
    return PageSource(page_id=title.upper(), title=title, revid=str(revid), url=f"u/{title}",
                      extract=title, sections=[{"title": "Body", "text": _words(n_words)}])


def _page_result(page, chunks):
    return PagePullResult(
        title=page.title,
        page_id=page.page_id,
        revid=page.revid,
        url=page.url,
        source="wiki",
        extract=page.extract,
        chunks=chunks,
    )


def _build_result():
    a = _make_page("History", 1)
    b = _make_page("Origins", 2)
    pa = _page_result(a, chunk_page(a, settings={}))
    pb = _page_result(b, chunk_page(b, settings={}))
    skipped = PagePullResult(title="Ghost", skipped=True, reason="missing")
    return PullResult(pull_id="Cat-1", source="wiki", category="Cat", started_at="t0",
                      ended_at="t1", pages=[pa, pb, skipped])


class TestExport(unittest.TestCase):
    def test_manifest_written_with_counts(self):
        with tempfile.TemporaryDirectory() as d:
            root = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            manifest_path = os.path.join(root, "manifest.json")
            self.assertTrue(os.path.exists(manifest_path))
            with open(manifest_path) as fh:
                m = json.load(fh)
            self.assertEqual(m["pull_id"], "Cat-1")
            self.assertEqual(m["page_count"], 2)  # skipped excluded
            self.assertEqual(m["skipped"], 1)
            self.assertGreater(m["chunk_count"], 0)
            self.assertGreater(m["total_tokens"], 0)

    def test_per_page_json_shape(self):
        with tempfile.TemporaryDirectory() as d:
            root = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            pages_dir = os.path.join(root, "pages")
            files = os.listdir(pages_dir)
            self.assertEqual(len(files), 2)
            with open(os.path.join(pages_dir, files[0])) as fh:
                doc = json.load(fh)
            for key in ("page_id", "title", "revid", "url", "extract", "provenance", "chunks"):
                self.assertIn(key, doc)
            self.assertIn("source", doc["provenance"])
            self.assertIn("page_id", doc["provenance"])

    def test_skipped_pages_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            pages_dir = os.path.join(root, "pages")
            contents = "".join(open(os.path.join(pages_dir, f)).read() for f in os.listdir(pages_dir))
            self.assertNotIn("Ghost", contents)

    def test_slug_is_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            root = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            pages_dir = os.path.join(root, "pages")
            for f in os.listdir(pages_dir):
                self.assertFalse(f.startswith("."))
                self.assertNotIn(" ", f)

    def test_idempotent_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            first = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            second = export_pull("Cat-1", _build_result(), out_dir=d, category="Cat")
            self.assertEqual(first, second)
            files = os.listdir(os.path.join(second, "pages"))
            self.assertEqual(len(files), 2)


class TestSlugify(unittest.TestCase):
    def test_special_chars_stripped(self):
        from pull.export import _slugify

        self.assertEqual(_slugify("Hello World!"), "hello-world")
        self.assertEqual(_slugify("  a/b//c  "), "a-b-c")
        self.assertEqual(_slugify("???"), "untitled")


if __name__ == "__main__":
    unittest.main()
