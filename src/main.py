"""bruggy-memory entrypoint.

Starts the ingestion HTTP API (:mod:`api_server`) and the pull scheduler
(:mod:`scheduler`). Subcommands::

    main.py serve [--source NAME] [--catch-up] [--host H] [--port P]
    main.py pull   [--source NAME]
    main.py catch-up [--source NAME]
    main.py chat   [--input FILE]      # ingest one chat batch from JSON stdin
    main.py health

The MediaWiki source is selected from ``wikis.yaml`` (design §4.4). The API uses a
:class:`~zep_client.RestZepBackend` wired to the configured Zep stack; the pull
layer talks to the MediaWiki Action API over a browser user-agent with Cloudflare
backoff.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import logging_setup
from config import get_settings
from logging_setup import configure_logging, get_logger

logger = get_logger("main")

_WIKIS_YAML = Path(__file__).resolve().parent / "wikis.yaml"


# --------------------------------------------------------------------------- #
# wiring helpers
# --------------------------------------------------------------------------- #
def load_sources(path: Path = _WIKIS_YAML) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {"sources": []}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not load %s (%s); using empty source list", path, exc)
        return {"sources": []}


def select_source(sources: dict, name: Optional[str] = None) -> Optional[dict]:
    names = {s["name"]: s for s in sources.get("sources", [])}
    if name:
        return names.get(name)
    default = sources.get("default_source")
    if default and default in names:
        return names[default]
    return next(iter(sources.get("sources", [])), None)


def build_client(source: dict, settings=None):
    """Build a MediaWikiClient for the given source (network transport)."""
    from pull.wiki_client import BackoffConfig, MediaWikiClient

    return MediaWikiClient(
        source["base_url"],
        user_agent=source.get("user_agent"),
        backoff=BackoffConfig(
            max_retries=source.get("cf_backoff_retries", 4),
            base=source.get("cf_backoff_base", 2.0),
            max=source.get("cf_backoff_max", 60.0),
        ),
    )


def build_ingest(settings, backend):
    from ingest import IngestionService

    return IngestionService(backend, settings)


def build_scheduler(settings, sources, source, ingest_service=None, catch_up_default=False):
    from scheduler import MemoryScheduler

    category = None
    if source and source.get("categories"):
        category = source["categories"][0]
    return MemoryScheduler(
        settings=settings,
        client_factory=lambda s: build_client(source, s),
        category=category,
        target_user_id=settings.zep_baseline_user_id,
        data_dir=settings.staging_dir,
        export_dir=settings.staging_dir,
        ingest_service=ingest_service,
    )


def new_backend(settings):
    from zep_client import RestZepBackend

    return RestZepBackend(settings)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
async def _health(backend) -> dict:
    try:
        return await backend.stats()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def cmd_serve(args) -> int:
    settings = get_settings()
    from api_server import create_app
    from zep_client import RestZepBackend

    sources = load_sources()
    source = select_source(sources, args.source)
    if source is None:
        logger.error("no wiki source selected (check wikis.yaml)")
        return 2

    backend = RestZepBackend(settings)
    app = create_app(backend, settings)
    scheduler = build_scheduler(
        settings, sources, source, build_ingest(settings, backend)
    )
    scheduler.start(run_catch_up=args.catch_up)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_health(backend))
    logger.info("memory health ok; serving on %s:%s", args.host, args.port)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    finally:
        scheduler.stop()
    return 0


def cmd_pull(args, *, catch_up: bool) -> int:
    settings = get_settings()
    sources = load_sources()
    source = select_source(sources, args.source)
    if source is None:
        logger.error("no wiki source selected (check wikis.yaml)")
        return 2

    backend = new_backend(settings)
    service = build_ingest(settings, backend)
    scheduler = build_scheduler(settings, sources, source, service)
    coro = scheduler.catch_up() if catch_up else scheduler.daily_pull()
    result = asyncio.run(coro)
    print(
        json.dumps(
            {
                "pull_id": result.pull_id,
                "page_count": result.page_count,
                "chunk_count": result.chunk_count,
                "total_tokens": result.total_tokens,
                "skipped": result.skipped_count,
            }
        )
    )
    scheduler.stop()
    return 0


def cmd_chat(args) -> int:
    settings = get_settings()
    from zep_client import ChatMessage
    from utils import parse_ts

    raw = Path(args.input).read_text() if args.input else sys.stdin.read()
    data = json.loads(raw)
    messages = []
    for m in data.get("messages", []):
        ts = parse_ts(m.get("timestamp")) or datetime.now(timezone.utc)
        messages.append(
            ChatMessage(
                content=m.get("content", "").strip(),
                timestamp=ts,
                role=m.get("role", "user"),
                user_id=m.get("user_id") or data.get("user_id"),
                message_id=m.get("message_id"),
            )
        )
    messages = [m for m in messages if m.content]
    backend = new_backend(settings)
    service = build_ingest(settings, backend)
    result = asyncio.run(
        service.ingest_chat(
            discord_guild_id=data["guild_id"],
            discord_channel_id=data["channel_id"],
            discord_user_id=data["user_id"],
            discord_username=data.get("username", "user"),
            messages=messages,
            max_segments=data.get("max_segments", 20),
        )
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_health(args) -> int:
    settings = get_settings()
    backend = new_backend(settings)
    info = asyncio.run(_health(backend))
    print(json.dumps(info, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bruggy-memory", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the API + scheduler")
    p_serve.add_argument("--source", default=None)
    p_serve.add_argument("--catch-up", action="store_true", help="catch-up pull on start")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--log-level", default="INFO")
    p_serve.set_defaults(func=cmd_serve)

    p_pull = sub.add_parser("pull", help="run a single incremental pull and exit")
    p_pull.add_argument("--source", default=None)
    p_pull.set_defaults(func=lambda a: cmd_pull(a, catch_up=False))

    p_catch = sub.add_parser("catch-up", help="run a full catch-up pull and exit")
    p_catch.add_argument("--source", default=None)
    p_catch.set_defaults(func=lambda a: cmd_pull(a, catch_up=True))

    p_chat = sub.add_parser("chat", help="ingest one chat batch (JSON on stdin)")
    p_chat.add_argument("--input", default=None)
    p_chat.set_defaults(func=cmd_chat)

    p_health = sub.add_parser("health", help="print backend health/stats")
    p_health.set_defaults(func=cmd_health)
    return parser


def main(argv: Optional[list] = None) -> int:
    configure_logging(to_stdout=True, to_file=False)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
