"""Fakes for every sweep source, backed by fixtures/sweep/. Used by tests and --fake runs.

The fake HTTP getter still runs safety.assert_fetch_allowed on every URL and records it, so
a fake run proves the same rules as a real one (no linkedin, allowlisted hosts only).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from jobengine import http
from jobengine.gmail_reader import GmailMessage
from jobengine.notion_repo import FakeJobsRepo
from jobengine.safety import assert_fetch_allowed
from jobengine.settings import ROOT_DIR, Settings
from jobengine.sweep.models import TargetCompany

FIXTURES = ROOT_DIR / "fixtures" / "sweep"
# Fake runs default to this date so fixture dates and the strategy gate stay meaningful.
FAKE_TODAY = date(2026, 10, 1)


def _load(name: str, base: Path = FIXTURES) -> Any:
    return json.loads((base / name).read_text(encoding="utf-8"))


def gmail_messages(base: Path = FIXTURES) -> list[GmailMessage]:
    folder = base / "gmail"
    return [
        GmailMessage(
            id=item["id"],
            sender=item["from"],
            subject=item["subject"],
            html=(folder / item["file"]).read_text(encoding="utf-8"),
        )
        for item in _load("index.json", folder)
    ]


def config(base: Path = FIXTURES) -> dict[str, str]:
    return _load("config.json", base)


def target_companies(base: Path = FIXTURES) -> list[TargetCompany]:
    return [TargetCompany(**row) for row in _load("target_companies.json", base)]


def jobs_repo(base: Path = FIXTURES) -> FakeJobsRepo:
    return FakeJobsRepo.from_fixture(base / "job_opportunities_seed.json")


class FixtureHttp:
    """Answers source GET requests from fixture files and records every URL requested."""

    def __init__(self, s: Settings, base: Path = FIXTURES):
        self.s = s
        self.base = base
        self.requested: list[str] = []

    def __call__(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        assert_fetch_allowed(url, self.s)
        self.requested.append(http.safe_url(url))
        params = params or {}
        if "api.adzuna.com" in url:
            country, page = url.rstrip("/").split("/")[-3], url.rstrip("/").split("/")[-1]
            if page != "1":
                return {"results": []}
            return _load(f"adzuna_{country}.json", self.base)
        if "boards-api.greenhouse.io" in url:
            return _load("greenhouse.json", self.base)
        if "lever.co" in url:
            return _load("lever.json", self.base)
        if "api.smartrecruiters.com" in url:
            if url.rstrip("/").endswith("/postings"):
                if int(params.get("offset", 0)):
                    return {"content": [], "totalFound": 0}
                return _load("smartrecruiters_list.json", self.base)
            detail = _load("smartrecruiters_detail.json", self.base)
            if url.rstrip("/").endswith(detail["id"]):
                return detail
            raise http.HttpError(f"GET {http.safe_url(url)} failed: HTTP 404", 404)
        raise http.HttpError(f"GET {http.safe_url(url)} failed: no fixture", 404)
