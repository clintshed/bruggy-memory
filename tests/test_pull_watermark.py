"""Tests for pull/watermark (revid watermark persistence + incremental decision)."""
import os
import tempfile
import unittest

from pull.watermark import Watermark


class TestWatermarkDecision(unittest.TestCase):
    def test_empty_get_returns_blank(self):
        w = Watermark.empty()
        self.assertEqual(w.get_revid("1"), "")

    def test_set_then_get(self):
        w = Watermark.empty()
        w.set_revid("1", "42")
        self.assertEqual(w.get_revid("1"), "42")

    def test_should_pull_newer(self):
        w = Watermark.empty()
        w.set_revid("1", "100")
        self.assertTrue(w.should_pull("1", "101"))

    def test_should_pull_older_is_false(self):
        w = Watermark.empty()
        w.set_revid("1", "100")
        self.assertFalse(w.should_pull("1", "99"))

    def test_should_pull_equal_is_false(self):
        w = Watermark.empty()
        w.set_revid("1", "100")
        self.assertFalse(w.should_pull("1", "100"))

    def test_should_pull_blank_current_is_true(self):
        w = Watermark.empty()
        self.assertTrue(w.should_pull("1", ""))
        self.assertTrue(w.should_pull("1", None))

    def test_should_pull_blank_stored_is_true(self):
        w = Watermark.empty()
        self.assertTrue(w.should_pull("1", "100"))

    def test_len_and_items(self):
        w = Watermark.empty()
        w.set_revid("1", "1")
        w.set_revid("2", "2")
        self.assertEqual(len(w), 2)
        self.assertEqual(dict(w.items()), {"1": "1", "2": "2"})


class TestWatermarkPersistence(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "watermark.json")
            w = Watermark(path=path)
            w.set_revid("7", "777")
            w.save()
            self.assertTrue(os.path.exists(path))
            loaded = Watermark.load(path)
            self.assertEqual(loaded.get_revid("7"), "777")

    def test_load_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.json")
            w = Watermark.load(missing)
            self.assertEqual(len(w), 0)
            self.assertEqual(w.get_revid("1"), "")

    def test_save_no_path_is_noop(self):
        w = Watermark.empty()
        w.set_revid("1", "1")
        w.save()  # must not raise when path is None
        self.assertEqual(w.get_revid("1"), "1")


if __name__ == "__main__":
    unittest.main()
