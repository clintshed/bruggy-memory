-- bruggy-memory local state schema.
--
-- The running app keeps its lightweight local state on disk as JSON
-- (watermark.json, outbox.jsonl, pull_status.json). This schema documents the
-- equivalent relational structure used by the `setup_local_state.py` helper to
-- initialise a local SQLite store (e.g. for tests / local dev) that mirrors the
-- JSON state exactly.
--
--   sqlite3 config/schema.sql
--   sqlite3 local_state.db < config/schema.sql

PRAGMA foreign_keys = ON;

-- Per-page high-water mark (incremental pull idempotency, design §4.3).
CREATE TABLE IF NOT EXISTS watermark (
    page_title TEXT    NOT NULL PRIMARY KEY,
    revid      TEXT    NOT NULL,          -- latest ingested MediaWiki edit id
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Per-run pull bookkeeping (reflected in /health as last_pull).
CREATE TABLE IF NOT EXISTS pull_status (
    pull_id       TEXT    NOT NULL PRIMARY KEY,
    source        TEXT,
    category      TEXT,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    page_count    INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    state         TEXT    NOT NULL DEFAULT 'running'  -- running | done | failed
);

-- Idempotent retry queue: failed upstream ingest attempts, replayed by the
-- scheduler until drained (design §8).
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,      -- chat | wiki
    entity_key   TEXT    NOT NULL,      -- session_id or page_title
    payload      TEXT    NOT NULL,      -- JSON envelope
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_retry   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_outbox_next_retry ON outbox(next_retry);
