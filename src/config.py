"""Configuration for the brussy-memory ingestion service.

Resolution order (later overrides earlier)::

    defaults  ->  config.sample  ->  environment variables

``config.sample`` holds the project defaults and is never mutated at runtime;
environment variables are the deployment override. This mirrors the
``config-knobs`` fallback convention used by the rest of the homelab stack.

Environment variables
---------------------
ZEP_API_URL          Zep API base URL              (default http://zep-api:8080)
ZEP_API_KEY          Zep API key                   (default localdevapikey)
ZEP_BASELINE_USER_ID baseline wiki user id         (default __baseline__)
ZEP_BASELINE_SESSION baseline wiki session id       (default baseline)
ZEP_EMBEDDING_MODEL  embedding model name          (default bge-large-en-v1.5)
ZEP_NLP_MODEL        NLP model name                (default zep-nlp-model)
ZEP_EMBEDDING_DIM    embedding dimension           (default 1024)
ZEP_EMBEDDING_CHUNK  embedding chunk size          (default 128)
DISCORD_BOT_TOKEN    Discord bot token (optional; needed by the pull job only)
LOG_LEVEL            log verbosity (DEBUG/INFO/WARNING/ERROR) (default INFO)
LOG_DIR              structured log directory      (default ./logs)
LOG_ROTATION         rotation size (e.g. 20MB)     (default 20MB)
LOG_RETENTION        retention count               (default 14)
LOG_FORMAT           json|console                  (default json)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
SAMPLE_CONFIG = _HERE / "config.sample"


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _to_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    return str(value).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Zep ---
    zep_api_url: str = "http://zep-api:8080"
    zep_api_key: str = "localdevapikey"
    zep_baseline_user_id: str = "__baseline__"
    zep_baseline_session_id: str = "baseline"
    zep_embedding_model: str = "bge-large-en-v1.5"
    zep_nlp_model: str = "zep-nlp-model"
    zep_embedding_dim: int = 1024
    zep_embedding_chunk_size: int = 128

    # --- Discord (used by the daily pull job, not strictly by ingestion) ---
    discord_bot_token: Optional[str] = None

    # --- Chunking ---
    max_segment_tokens: int = 128
    segment_token_overlap: int = 20
    max_chat_chunk_chars: int = 1000
    chat_segment_min_chars: int = 40

    # --- RAG / query ---
    default_n_results: int = 10
    query_search_scope: tuple = ("facts", "episodes", "segments", "entity_relationships")

    # --- Logging ---
    log_level: str = "INFO"
    log_dir: str = "./logs"
    log_rotation: str = "20MB"
    log_retention: int = 14
    log_format: str = "json"

    # --- Paths / lifecycle ---
    staging_dir: str = "./staging"

    @classmethod
    def _from_env(cls, values: dict[str, Any]) -> dict[str, Any]:
        mapping = {f.name: f for f in fields(cls)}
        overrides: dict[str, Any] = {}
        env_map = {
            "ZEP_API_URL": "zep_api_url",
            "ZEP_API_KEY": "zep_api_key",
            "ZEP_BASELINE_USER_ID": "zep_baseline_user_id",
            "ZEP_BASELINE_SESSION_ID": "zep_baseline_session_id",
            "ZEP_EMBEDDING_MODEL": "zep_embedding_model",
            "ZEP_NLP_MODEL": "zep_nlp_model",
            "ZEP_EMBEDDING_DIM": "zep_embedding_dim",
            "ZEP_EMBEDDING_CHUNK_SIZE": "zep_embedding_chunk_size",
            "DISCORD_BOT_TOKEN": "discord_bot_token",
            "LOG_LEVEL": "log_level",
            "LOG_DIR": "log_dir",
            "LOG_ROTATION": "log_rotation",
            "LOG_RETENTION": "log_retention",
            "LOG_FORMAT": "log_format",
            "STAGING_DIR": "staging_dir",
            "MAX_SEGMENT_TOKENS": "max_segment_tokens",
            "SEGMENT_TOKEN_OVERLAP": "segment_token_overlap",
            "MAX_CHAT_CHUNK_CHARS": "max_chat_chunk_chars",
            "CHAT_SEGMENT_MIN_CHARS": "chat_segment_min_chars",
            "DEFAULT_N_RESULTS": "default_n_results",
        }
        for env_name, attr in env_map.items():
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            target = mapping.get(attr)
            if target is None:
                continue
            ftype = target.type
            if isinstance(ftype, type) and issubclass(ftype, int):
                overrides[attr] = _to_int(raw)
            elif ftype is Optional[str] or ftype is str:
                overrides[attr] = raw
            elif ftype is tuple:
                overrides[attr] = tuple(raw.split(","))
        return overrides

    @classmethod
    def load(cls, sample_path: Optional[Path] = None) -> "Settings":
        sample_path = sample_path or SAMPLE_CONFIG
        values: dict[str, Any] = {}
        if sample_path.exists():
            try:
                data = json.loads(sample_path.read_text())
                values.update(data)
            except (json.JSONDecodeError, OSError) as exc:
                # A malformed sample file must never crash startup; warn and
                # fall back to built-in defaults.
                print(f"[config] warning: ignoring unreadable {sample_path}: {exc}")
        values.update(cls._from_env(values))
        return cls(**values)

    @property
    def embedder_enabled(self) -> bool:
        return bool(self.zep_embedding_dim and self.zep_embedding_model)


def get_settings() -> Settings:
    """Module-level cache of settings (env read once per process)."""
    return Settings.load()
