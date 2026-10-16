# Bruggy Memory — app-side settings (template)

Mirrors the `settings.md` → `settings.sample.md` fallback convention from
`discord-brussy-forever`. This file is the committed template; the live
`config/settings.md` overrides it at runtime. All tunables can also be set via
environment variables or `src/config.sample` (JSON) — whichever is present wins,
with environment variables taking precedence.

Keys here map 1:1 to the `Settings` dataclass in `src/config.py` and to the
environment variables it reads.

## Pull

```text
# --- pull ---
wiki_min_words      = 50     # skip wiki sections under this many words
wiki_chunk_tokens   = 800    # target tokens per wiki chunk (segment)
wiki_chunk_overlap  = 10     # tokens of overlap between adjacent wiki chunks
wiki_max_pages_run  = 0      # cap pages pulled per run (0 = unbounded)
cf_backoff_base     = 2      # Cloudflare backoff base seconds
cf_backoff_max      = 60     # Cloudflare backoff max seconds
cf_max_retries      = 4      # max attempts per wiki request before skipping
```

## Zep

```text
# --- zep ---
zep_api_url        = http://zep-api:8080
zep_api_key        = ${ZEP_AUTH_SECRET}          # from the gitignored env file
zep_embedding_model= bge-large-en-v1.5
zep_nlp_model      = zep-nlp-model
zep_embedding_dim  = 1024
zep_embedding_chunk_size = 128
zep_local_embeddings = true                       # extract embeddings locally (no cloud)
```

## Discord / chat

```text
# --- discord ---
discord_bot_token  =            # from environment (never committed)
max_segment_tokens = 128       # chat segment size (tokens)
segment_token_overlap = 20     # chat segment overlap (tokens)
max_chat_chunk_chars = 1000    # max chars per chat message chunk
chat_segment_min_chars = 40    # drop chat segments shorter than this many chars
default_n_results  = 10        # default RAG recall count
```

## Schedule

```text
# --- schedule ---
pull_hour_utc      = 3         # daily pull hour (UTC); catch-up also runs on restart
log_level          = INFO      # DEBUG enables verbose logging (verbose toggle)
log_dir            = ./logs
log_format         = json      # json | console
```

## Secrets

Secrets (`ZEP_AUTH_SECRET`, `DISCORD_BOT_TOKEN`, wiki API keys) come from a
gitignored `--env-file` in the compose stack (see `env/.env.example`), exactly
like `bruggy2`'s `.env`. Never commit real secrets.
