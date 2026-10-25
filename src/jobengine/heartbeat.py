"""Heartbeat (Autopilot H4, 25 Oct): an alarm from outside when the bot stops silently.

The daily report and the failure mails come from the bot itself: when the PC, the container
or Cloud Run is down, nothing comes. So after each daily report the bot pings your
healthchecks.io check (HEALTHCHECK_URL, free plan). The check is set to a period of 1 day
and a grace time of 2 hours: when no ping comes for about 26 hours, healthchecks.io emails
you. A failed scheduled run also pings `<url>/fail`, so the check turns red at once.

A ping is one plain GET to hc-ping.com (safety.allowed_hosts). It is never a reason to fail:
an error is only logged. Without HEALTHCHECK_URL nothing is sent.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from jobengine import http
from jobengine.settings import Settings

log = logging.getLogger("jobengine.heartbeat")

HOST = "hc-ping.com"


def ping(s: Settings, ok: bool = True) -> bool:
    """Ping the check (ok) or its /fail address. True when the ping went out."""
    url = (s.healthcheck_url or "").strip().rstrip("/")
    if not url:
        return False
    if urlsplit(url).hostname != HOST:
        log.warning("HEALTHCHECK_URL is not an %s address: no ping", HOST)
        return False
    try:
        http.get_text(url if ok else f"{url}/fail", s=s)
    except Exception as exc:  # a heartbeat must never stop the bot
        log.warning("heartbeat ping failed: %s", type(exc).__name__)
        return False
    return True
