"""Environment aware settings.

APP_ENV selects one of local, dev or prod. config/base.yaml is loaded first and
config/{APP_ENV}.yaml is deep-merged on top. Secrets come only from environment
variables (plus .env when APP_ENV is local).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
ALLOWED_ENVS = ("local", "dev", "prod")
DEFAULT_ENV = "local"

# Environment variable name -> Settings field name.
SECRET_VARS = {
    "NOTION_TOKEN": "notion_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "GMAIL_ALERTS_TOKEN_JSON": "gmail_alerts_token_json",
    "GMAIL_SENDER_TOKEN_JSON": "gmail_sender_token_json",
    "ADZUNA_APP_ID": "adzuna_app_id",
    "ADZUNA_APP_KEY": "adzuna_app_key",
    "APOLLO_API_KEY": "apollo_api_key",
    "HUNTER_API_KEY": "hunter_api_key",
    "SNOV_CLIENT_ID": "snov_client_id",
    "SNOV_CLIENT_SECRET": "snov_client_secret",
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: str
    dry_run: bool
    env_label: str
    notion_read: dict[str, str]
    notion_write: dict[str, str]
    notion_pages: dict[str, str] = Field(default_factory=dict)
    gmail_sender: str
    redirect_to: str
    prod_sender: str
    dev_sender: str
    force_model: str | None
    sweep: dict[str, Any] = Field(default_factory=dict)
    screening: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)
    drive: dict[str, Any] = Field(default_factory=dict)
    contacts: dict[str, Any] = Field(default_factory=dict)
    mail: dict[str, Any] = Field(default_factory=dict)
    tracking: dict[str, Any] = Field(default_factory=dict)
    strategy: dict[str, Any] = Field(default_factory=dict)
    allowed_hosts: tuple[str, ...] = ()

    notion_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    anthropic_api_key: str | None = None
    gmail_alerts_token_json: str | None = None
    gmail_sender_token_json: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    apollo_api_key: str | None = None
    hunter_api_key: str | None = None
    snov_client_id: str | None = None
    snov_client_secret: str | None = None


def parse_dry_run(value: str | None) -> bool:
    """DRY_RUN is false only for the exact string "false". Anything else is true."""
    return value != "false"


def _validate_env(env: str) -> str:
    if env not in ALLOWED_ENVS:
        raise ValueError(f"APP_ENV must be one of {', '.join(ALLOWED_ENVS)}, got {env!r}")
    return env


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(env: str) -> dict[str, Any]:
    """Return config/base.yaml deep-merged with config/{env}.yaml."""
    env = _validate_env(env)
    return _deep_merge(_read_yaml(CONFIG_DIR / "base.yaml"), _read_yaml(CONFIG_DIR / f"{env}.yaml"))


def _load_prod_write_ids() -> frozenset[str]:
    prod = _read_yaml(CONFIG_DIR / "prod.yaml")
    return frozenset(prod.get("notion", {}).get("write", {}).values())


# Data source IDs that only prod may write to, whatever the current env is.
PROD_WRITE_IDS: frozenset[str] = _load_prod_write_ids()


def load_settings(env: str | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from config files and the given environ mapping.

    No caching and no access to the real process environment. When env is None
    it is taken from environ["APP_ENV"], defaulting to local.
    """
    environ = dict(environ or {})
    if env is None:
        env = environ.get("APP_ENV", DEFAULT_ENV)
    env = _validate_env(env)
    cfg = load_config(env)

    notion = cfg.get("notion", {})
    safety = cfg.get("safety", {})
    secrets = {field: environ.get(var) or None for var, field in SECRET_VARS.items()}

    return Settings(
        app_env=env,
        dry_run=parse_dry_run(environ.get("DRY_RUN")),
        env_label=cfg.get("env_label") or "",
        notion_read=dict(notion.get("read") or {}),
        notion_write=dict(notion.get("write") or {}),
        notion_pages=dict(notion.get("pages") or {}),
        gmail_sender=cfg.get("gmail", {}).get("sender", ""),
        redirect_to=safety["redirect_to"],
        prod_sender=safety["prod_sender"],
        dev_sender=safety["dev_sender"],
        force_model=cfg.get("llm", {}).get("force_model"),
        sweep=dict(cfg.get("sweep") or {}),
        screening=dict(cfg.get("screening") or {}),
        resume=dict(cfg.get("resume") or {}),
        drive=dict(cfg.get("drive") or {}),
        contacts=dict(cfg.get("contacts") or {}),
        mail=dict(cfg.get("mail") or {}),
        tracking=dict(cfg.get("tracking") or {}),
        strategy=dict(cfg.get("strategy") or {}),
        allowed_hosts=tuple(safety.get("allowed_hosts") or ()),
        **secrets,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build Settings once from the real process environment."""
    environ: dict[str, str] = dict(os.environ)
    env = environ.get("APP_ENV", DEFAULT_ENV)
    if env == "local":
        dotenv = {k: v for k, v in dotenv_values(ROOT_DIR / ".env").items() if v is not None}
        environ = {**dotenv, **environ}
    return load_settings(env, environ)
