"""Careers pages built by JavaScript, read in a browser (30 Oct, Phase 18).

Many careers pages (Cognizant, Emerging Travel Group, ...) send an empty page and fill in
the jobs with JavaScript: your browser shows the jobs, a plain page read gets none. When
Playwright is installed (docker build --build-arg FORM_BOT=true, the same browser /dryrun
uses), the direct job link search opens such a page in a headless browser instead.

The browser is guarded like the form bot's: plain GETs to public https hosts only, never
LinkedIn, no login, nothing sent. A site that refuses the bot (HTTP 403, a captcha) is left
alone: this reads pages that work in any browser, it does not get around a block.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from jobengine import http, safety
from jobengine.settings import Settings

log = logging.getLogger("jobengine.sweep")

DEFAULT_MAX_PAGES = 15  # pages opened in the browser per sweep (sweep.job_links.max_rendered)
LOAD_MS = 20_000
SETTLE_MS = 8_000  # time for the jobs list to appear after the page loaded


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class Renderer:
    """url -> (final url, html after JavaScript ran). Opens one browser for the whole sweep,
    on first use; close() ends it."""

    def __init__(self, s: Settings, left: int = DEFAULT_MAX_PAGES):
        self.s = s
        self.left = left
        self._playwright: Any = None
        self._browser: Any = None

    def __call__(self, url: str) -> tuple[str, str]:
        from jobengine.apply import form_bot

        if self.left <= 0:
            raise http.HttpError("page not rendered: the browser budget of this sweep is used")
        try:
            safety.assert_page_fetch_allowed(url, self.s)
        except safety.SafetyError as exc:
            raise http.HttpError(f"page not rendered: {exc}") from None
        self.left -= 1
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = form_bot._launch(self._playwright)
        page = self._browser.new_page()
        try:
            refused: list[str] = []
            page.route("**/*", form_bot._guard(self.s, refused))
            answer = page.goto(url, wait_until="domcontentloaded", timeout=LOAD_MS)
            if answer is not None and answer.status >= 400:
                raise http.HttpError(f"page not rendered: HTTP {answer.status}", answer.status)
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            except Exception:  # a page that keeps loading: use what is there
                log.info("page still loading after %d ms: %s", SETTLE_MS, http.safe_url(url))
            return page.url, page.content()
        except http.HttpError:
            raise
        except Exception as exc:
            raise http.HttpError(f"page not rendered: {str(exc)[:120]}") from None
        finally:
            page.close()

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # closing never fails a sweep
            log.exception("browser not closed")
        self._browser = self._playwright = None
