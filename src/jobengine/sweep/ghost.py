"""Ghost-job risk (spec section 6, Config ghost_job.rules). Pure function."""

from __future__ import annotations

from datetime import date


def ghost_risk(times_seen: int, first_seen: date, posted_date: date | None, today: date) -> str:
    """High, Medium, Low or Unknown.

    High: seen 3+ times and first seen 60+ days ago.
    Medium: seen 2+ times and first seen 30+ days ago, or posted more than 45 days ago.
    Low: posted date known and neither rule applies.
    Unknown: otherwise.
    """
    days = (today - first_seen).days
    if times_seen >= 3 and days >= 60:
        return "High"
    if (times_seen >= 2 and days >= 30) or (
        posted_date is not None and (today - posted_date).days > 45
    ):
        return "Medium"
    if posted_date is not None:
        return "Low"
    return "Unknown"
