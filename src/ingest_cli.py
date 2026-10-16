"""Command-line entrypoint for the ingestion service.

Usage examples
--------------
    # ingest chat from a Discord-style JSON file (stdin)
    cat chat.json | python ingest_cli.py ingest-chat

    # ingest a wiki page
    python ingest_cli.py ingest-wiki --input page.json

    # query
    python ingest_cli.py query --user 123 --text "how do I reset"

    # offline demo against the in-memory backend
    python ingest_cli.py demo --backend memory

The default backend is REST (real Zep); ``--backend memory`` uses the in-memory
backend for offline testing and demos.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from config import Settings, get_settings
from in_memory_backend import InMemoryZepBackend
from ingest import ChatMessage, IngestionService
from logging_setup import configure_logging, get_logger
from utils import parse_ts
from zep_client import RestZepBackend, ZepBackend

logger = get_logger("cli")


def _make_backend(name: str, settings: Settings) -> ZepBackend:
    if name == "memory":
        return InMemoryZepBackend()
    if name == "rest":
        return RestZepBackend(settings)
    raise SystemExit(f"unknown backend: {name} (use 'rest' or 'memory')")


def _read_json(stream) -> dict:
    return json.loads(stream.read())


async def _ingest_chat(service: IngestionService, payload: dict) -> None:
    msgs = []
    for m in payload["messages"]:
        ts = parse_ts(m["timestamp"])
        if ts is None:
            ts = datetime.now(timezone.utc)
        msgs.append(
            ChatMessage(
                content=m["content"],
                timestamp=ts,
                role=m.get("role", "user"),
                message_id=m.get("message_id"),
            )
        )
    result = await service.ingest_chat(
        discord_guild_id=payload["discord_guild_id"],
        discord_channel_id=payload["discord_channel_id"],
        discord_user_id=payload["discord_user_id"],
        discord_username=payload.get("discord_username", "user"),
        messages=msgs,
        max_segments=payload.get("max_segments", 20),
    )
    print(json.dumps(result.to_dict(), indent=2))


async def _ingest_wiki(service: IngestionService, payload: dict) -> None:
    result = await service.ingest_wiki(
        category=payload.get("category", "general"),
        page_title=payload.get("page_title", "Untitled"),
        source_url=payload.get("source_url", ""),
        revid=payload.get("revid", "revid-unknown"),
        text=payload.get("text", ""),
        last_edit=parse_ts(payload.get("last_edit")),
        target_user_id=payload.get("target_user_id"),
        max_segments=payload.get("max_segments", 50),
    )
    print(json.dumps(result.to_dict(), indent=2))


async def _query(service: IngestionService, payload: dict) -> None:
    resp = await service.query(
        discord_user_id=payload["user_id"],
        text=payload["query"],
        n_results=payload.get("n_results", 10),
        include_wiki=payload.get("include_wiki", True),
        categories=payload.get("categories"),
    )
    print(json.dumps(resp.to_dict(), indent=2))


async def _demo(service: IngestionService) -> None:
    """A self-contained demo: chat + wiki for one user, then query."""
    user_id = "424242"
    await service.ingest_chat(
        discord_guild_id="111",
        discord_channel_id="222",
        discord_user_id=user_id,
        discord_username="Trae",
        messages=[
            ChatMessage("The admin panel lives at settings.example.com.",
                        datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc), message_id="m1"),
            ChatMessage("Reset the password from the security tab there.",
                        datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc), message_id="m2"),
        ],
    )
    await service.ingest_wiki(
        category="security",
        page_title="Password Reset",
        source_url="https://wiki.local/wiki/Password_Reset",
        revid="9988",
        text=(
            "To reset your password, navigate to the security settings page and "
            "choose 'Reset password'. A confirmation email is sent to the account "
            "on file. The admin panel is hosted at settings.example.com."
        ),
        last_edit=datetime(2024, 1, 2, tzinfo=timezone.utc),
        target_user_id=user_id,
    )
    resp = await service.query(user_id, "how do I reset my password", n_results=5)
    print(json.dumps(resp.to_dict(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ingest", description="brussy-memory ingestion CLI")
    p.add_argument("--backend", default="rest", choices=["rest", "memory"], help="Zep backend")
    p.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("ingest-chat", help="ingest chat from stdin JSON")
    c.add_argument("--input", help="input file (default stdin)")
    c.add_argument("--backend", default=argparse.SUPPRESS, choices=["rest", "memory"], help="Zep backend")
    c.add_argument("--log-level", default=argparse.SUPPRESS, help="override LOG_LEVEL")

    w = sub.add_parser("ingest-wiki", help="ingest wiki from stdin JSON")
    w.add_argument("--input", help="input file (default stdin)")
    w.add_argument("--backend", default=argparse.SUPPRESS, choices=["rest", "memory"], help="Zep backend")
    w.add_argument("--log-level", default=argparse.SUPPRESS, help="override LOG_LEVEL")

    q = sub.add_parser("query", help="query from stdin JSON")
    q.add_argument("--input", help="input file (default stdin)")
    q.add_argument("--backend", default=argparse.SUPPRESS, choices=["rest", "memory"], help="Zep backend")
    q.add_argument("--log-level", default=argparse.SUPPRESS, help="override LOG_LEVEL")

    d = sub.add_parser("demo", help="run offline demo on the in-memory backend")
    d.add_argument("--backend", default=argparse.SUPPRESS, choices=["rest", "memory"], help="Zep backend")
    d.add_argument("--log-level", default=argparse.SUPPRESS, help="override LOG_LEVEL")
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.log_level:
        settings.log_level = args.log_level
    configure_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        log_format=settings.log_format,
        to_stdout=False,
    )

    backend = _make_backend(args.backend, settings)
    service = IngestionService(backend, settings)

    async def run() -> int:
        if args.command == "ingest-chat":
            payload = _read_json(open(args.input) if args.input else sys.stdin)
            await _ingest_chat(service, payload)
        elif args.command == "ingest-wiki":
            payload = _read_json(open(args.input) if args.input else sys.stdin)
            await _ingest_wiki(service, payload)
        elif args.command == "query":
            payload = _read_json(open(args.input) if args.input else sys.stdin)
            await _query(service, payload)
        elif args.command == "demo":
            await _demo(service)
        return 0

    import asyncio

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
