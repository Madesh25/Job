"""The only place that decides whether something may leave the system."""

from __future__ import annotations

from jobengine.settings import PROD_WRITE_IDS, Settings

PROD = "prod"


class SafetyError(Exception):
    """Raised when the configuration could leak test actions into production."""


def check_startup(s: Settings) -> None:
    """Refuse to start with a configuration that is unsafe for the current env."""
    if s.app_env != PROD:
        for name, ds_id in s.notion_write.items():
            if ds_id in PROD_WRITE_IDS:
                raise SafetyError(
                    f"{s.app_env}: notion write target {name!r} points to a production database"
                )
            if ds_id.startswith("<"):
                raise SafetyError(
                    f"{s.app_env}: notion write target {name!r} is still a placeholder ({ds_id})"
                )
        if s.gmail_sender != s.dev_sender:
            raise SafetyError(
                f"{s.app_env}: gmail sender must be {s.dev_sender}, got {s.gmail_sender}"
            )
    else:
        if s.gmail_sender != s.prod_sender:
            raise SafetyError(f"prod: gmail sender must be {s.prod_sender}, got {s.gmail_sender}")
        if s.force_model is not None:
            raise SafetyError(f"prod: force_model must be unset, got {s.force_model}")


def route_recipients(
    to: list[str], cc: list[str], subject: str, s: Settings
) -> tuple[list[str], list[str], str]:
    """Return the real recipients in prod, otherwise redirect everything to redirect_to."""
    if s.app_env == PROD:
        return to, cc, subject
    return [s.redirect_to], [], f"{s.env_label} to {', '.join(to + cc)} | {subject}"


def gmail_write_allowed(s: Settings) -> bool:
    return not s.dry_run


def paid_api_allowed(provider: str, s: Settings) -> bool:
    return s.app_env == PROD and not s.dry_run


def notion_write_target(name: str, s: Settings) -> str | None:
    """Data source ID to write `name` to, or None when not writable in this env.

    Callers must log "DRY RUN: would write to <name>" when they get None.
    """
    ds_id = s.notion_write.get(name)
    if ds_id is None:
        return None
    if s.app_env != PROD and ds_id in PROD_WRITE_IDS:
        return None
    return ds_id


def resolve_model(requested: str, s: Settings) -> str:
    return s.force_model or requested


def telegram_text(text: str, s: Settings) -> str:
    return f"{s.env_label} {text}" if s.env_label else text
