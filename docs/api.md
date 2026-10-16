# Bruggy Memory — API documentation

The ingestion service exposes a small REST surface on port **8000**. The machine
readable contract lives in [`docs/openapi.json`](../docs/openapi.json); paste it
into any OpenAPI editor, or serve it locally:

```bash
pip install -r requirements.txt
cd src && uvicorn api_server:app --reload   # interactive docs at http://127.0.0.1:8000/docs
```

Auth endpoints accept a Bearer token derived from `ZEP_AUTH_SECRET` (or a
separate API key) via the `Authorization: Bearer <token>` header. In local/dev
mode auth is disabled; in production wire the token through the reverse proxy.

## Base URL

```
http://<host>:8000
```

## Endpoints

### `GET /health`

Liveness + backend type + stats + last pull status.

```json
// 200 OK
{ "status": "ok", "backend": "RestZepBackend", "version": "0.1.0",
  "uptime_seconds": 12.3, "stats": { "users": 1, "sessions": 3, "episodes": 42 } }
```

### `POST /memory/add`

Ingest chat **or** wiki (runs the merge layer). Body selects the kind with
`type`.

| Field | When | Meaning |
|---|---|---|
| `type` | always | `"chat"` or `"wiki"` |
| `discord_guild_id` | chat | Discord server id |
| `discord_channel_id` | chat | Channel the messages came from |
| `discord_user_id` | chat | Author id |
| `discord_username` | chat | Display name |
| `messages` | chat | `[{content, timestamp, role, message_id}]` |
| `max_segments` | both | Cap segments per ingest |
| `category` | wiki | Pull category (default `"general"`) |
| `page_title` | wiki | Page title |
| `source_url` | wiki | Canonical page URL |
| `revid` | wiki | MediaWiki edit id (idempotency watermark) |
| `text` | wiki | Page body (Markdown) |
| `last_edit` | wiki | ISO-8601 edit timestamp |
| `target_user_id` | wiki | Baseline user to attribute wiki to |

Chat example:

```bash
curl -X POST http://127.0.0.1:8000/memory/add \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "chat",
    "discord_guild_id": "123", "discord_channel_id": "456",
    "discord_user_id": "789", "discord_username": "trae",
    "messages": [{"content": "How do I reset my password?", "timestamp": "2026-10-16T12:00:00Z", "role": "user"}]
  }'
```

Wiki example:

```bash
curl -X POST http://127.0.0.1:8000/memory/add \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "wiki", "category": "General", "page_title": "FAQ",
    "source_url": "https://wiki.gg/index.php?title=FAQ", "revid": "200",
    "text": "# FAQ\n\nHow do I reset my password?\n...",
    "target_user_id": "__baseline__"
  }'
```

Response (chat / wiki) carries provenance:

```json
{ "session_id": "...", "user_id": "789", "kind": "chat_segment",
  "segments_ingested": 1, "messages_ingested": 1, "skipped": 0,
  "skipped_reason": null,
  "provenance": { "source": "chat", "channel": "discord:456", "author": "trae",
                  "author_id": "789", "title": "general", "timestamp": "..." } }
```

### `POST /memory/query`

RAG recall across chat + wiki for a user.

| Field | Default | Meaning |
|---|---|---|
| `user_id` | — | Discord user to query |
| `query` | — | Natural-language question |
| `n_results` | 10 | Max results |
| `include_wiki` | `true` | Include wiki segments in the pool |
| `categories` | — | Restrict to wiki categories |
| `scope` | — | Subgraph scope (`facts`, `episodes`, `segments`, `entity_relationships`) |

```bash
curl -X POST http://127.0.0.1:8000/memory/query \
  -H 'Content-Type: application/json' \
  -d '{ "user_id": "789", "query": "how do I reset my password", "n_results": 5 }'
```

Response:

```json
{ "user_id": "789",
  "session_ids": ["discord:456", "wiki:FAQ:200"],
  "count": 2,
  "results": [
    { "score": 0.428845,
      "text": "Open settings and click the reset link…",
      "kind": "wiki_segment",
      "episode": { "episode_uuid": "…", "session_id": "wiki:FAQ:200",
                   "source": "wiki", "title": "FAQ", "url": "…",
                   "revid": "200" },
      "provenance": { "source": "wiki", "title": "FAQ", "revid": "200",
                      "category": "General", "url": "…", "timestamp": "…" } }
  ] }
```

Every result carries `provenance` (source-specific fields plus derived
`title`/`url`/`timestamp`), so consumers can route results by source.

### `GET /memory/user/{user_id}`

Return a user's memory graph (all sessions), optionally recalled with a query.

Query params: `query`, `n_results`, `include_wiki`, `scope`.

### `GET /memory/stats`

Counts of users, sessions, episodes, chat + wiki segments.

## Provenance schema

Every wiki result carries `revid` and `title`; every chat result carries
`channel`, `author`, `author_id`. `provenance` = the episode's raw
`business_data` augmented with derived `title` / `url` / `timestamp`.

## Local end-to-end smoke test (no network)

The pull layer and ingest service are exercised against a stub MediaWiki source
in `src/test_e2e_stub_wiki.py`:

```bash
pip install -r requirements.txt
pytest            # from repo root (pyproject.toml sets pythonpath=src)
```
