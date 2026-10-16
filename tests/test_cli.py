"""Tests for the ingestion CLI argument parsing and subcommand dispatch.

These guard the "global flag works before OR after the subcommand" behaviour
that used to bite us when migrating to subparsers.
"""
import json
import unittest

from unittest import mock

import ingest_cli
from ingest_cli import build_parser


class TestCLIParser(unittest.TestCase):
    def test_backend_before_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["--backend", "memory", "demo"])
        self.assertEqual(args.command, "demo")
        self.assertEqual(args.backend, "memory")

    def test_backend_after_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["demo", "--backend", "memory"])
        self.assertEqual(args.command, "demo")
        self.assertEqual(args.backend, "memory")

    def test_backend_default_unaffected_by_trailing_flag(self):
        # "demo --log-level DEBUG" must not clobber the global backend default.
        parser = build_parser()
        args = parser.parse_args(["demo", "--log-level", "DEBUG"])
        self.assertEqual(args.command, "demo")
        self.assertEqual(args.backend, "rest")
        self.assertEqual(args.log_level, "DEBUG")

    def test_backend_default_with_no_extra_flags(self):
        parser = build_parser()
        args = parser.parse_args(["demo"])
        self.assertEqual(args.backend, "rest")

    def test_unknown_backend_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["demo", "--backend", "bogus"])


class TestCLIDispatch(unittest.TestCase):
    def test_demo_routes_to_handler(self):
        backend_m = mock.MagicMock(return_value=None)
        demo_m = mock.AsyncMock()
        with mock.patch.object(ingest_cli, "_make_backend", backend_m), \
             mock.patch.object(ingest_cli, "_demo", demo_m):
            ingest_cli.main(["--backend", "memory", "demo"])
        demo_m.assert_awaited_once()
        backend_m.assert_called_once_with("memory", ingest_cli.get_settings())

    def test_ingest_chat_routes_to_handler(self):
        backend_m = mock.MagicMock(return_value=None)
        read_m = mock.Mock(return_value={})
        chat_m = mock.AsyncMock()
        with mock.patch.object(ingest_cli, "_make_backend", backend_m), \
             mock.patch.object(ingest_cli, "_read_json", read_m), \
             mock.patch.object(ingest_cli, "_ingest_chat", chat_m):
            ingest_cli.main(["ingest-chat"])
        chat_m.assert_awaited_once()

    def test_backend_flag_after_subcommand_routed(self):
        # end-to-end: flag placement must not affect which handler runs
        backend_m = mock.MagicMock(return_value=None)
        read_m = mock.Mock(return_value={})
        wiki_m = mock.AsyncMock()
        with mock.patch.object(ingest_cli, "_make_backend", backend_m), \
             mock.patch.object(ingest_cli, "_read_json", read_m), \
             mock.patch.object(ingest_cli, "_ingest_wiki", wiki_m):
            ingest_cli.main(["ingest-wiki", "--backend", "memory"])
        wiki_m.assert_awaited_once()
        backend_m.assert_called_once_with("memory", ingest_cli.get_settings())


if __name__ == "__main__":
    unittest.main()
