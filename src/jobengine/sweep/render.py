"""Careers pages built by JavaScript, read in a browser (30 Oct, Phase 18).

Many careers pages (Cognizant, Emerging Travel Group, ...) send an empty page and fill in
the jobs with JavaScript: your browser shows the jobs, a plain page read gets none. When
Playwright is installed (every Docker build since 30 Oct; the same browser /dryrun
uses), the direct job link search opens such a page in a headless browser instead.

The browser is guarded like the form bot's: plain GETs to public https hosts only, never
LinkedIn, no login, nothing sent. A site that refuses the bot (HTTP 403, a captcha) is left
alone: this reads pages that work in any browser, it does not get around a block.
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from jobengine import http, safety
from jobengine.settings import Settings

log = logging.getLogger("jobengine.sweep")

DEFAULT_MAX_PAGES = 40  # pages opened in the browser per sweep (sweep.job_links.max_rendered)
DEFAULT_BROWSERS = 1  # browsers open at the same time (sweep.job_links.browsers)
LOAD_MS = 20_000
SETTLE_MS = 8_000  # time for the jobs list to appear after the page loaded
# 20 Nov /fetch: one browser page never answered (Fortray, from 10:20): the job-link search
# waited for it without end, and /end could not finish /fetch either, because closing the
# browser waited behind that page. A page now gets PAGE_SECONDS in all (loading, settling,
# reading); a browser still busy then is left alone for the rest of the sweep and the other
# browsers go on. Closing waits CLOSE_SECONDS per browser at most.
PAGE_SECONDS = 60.0
CLOSE_SECONDS = 10.0


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


MAX_FRAMES = 5
HREF = re.compile(r"""href=(["'])([^"']*)\1""", re.I)


def _frames(page: Any) -> str:
    """The jobs list shown in a box from another site (an iframe: Recruitee, Homerun,
    Personio widgets, 4 Nov) is a page of its own: its HTML is added after the page's."""
    out = []
    for frame in page.frames[1:MAX_FRAMES + 1]:
        try:
            base = frame.url
            # The box's own links are relative to the box's site, not to the careers page.
            html = HREF.sub(lambda m, base=base: (f"href={m.group(1)}"
                                                  f"{urljoin(base, m.group(2))}{m.group(1)}"),
                            frame.content())
            out.append(f"\n<!-- frame {http.safe_url(base)} -->\n{html}")
        except Exception as exc:  # a frame that went away
            log.info("frame not read: %s", str(exc)[:80])
    return "".join(out)


def page_key(url: str) -> str:
    """One page, however its address is written: "https://werkenbij.alliander.com" and
    "https://werkenbij.alliander.com/" are the same page (16 Nov /fetch: it was opened twice
    and the job's 4 browser pages ran out before its job list)."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


class Renderer:
    """url -> (final url, html after JavaScript ran). Opens up to `browsers` browsers for the
    whole sweep, each on first use; close() ends them. Each browser lives on its own thread:
    Playwright works only on the thread that started it, and the link search runs on several
    workers, so up to `browsers` pages are open at the same time (FEAT-01, 17 Nov: 3 browsers
    read 26 pages in 15 s instead of 46 s; about 170 MB more memory each)."""

    def __init__(self, s: Settings, left: int = DEFAULT_MAX_PAGES, browsers: int = 1):
        self.s = s
        self.left = left
        self._threads = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"browser{n}")
                         for n in range(max(1, browsers))]
        self._free: queue.Queue[int] = queue.Queue()
        for n in range(len(self._threads)):
            self._free.put(n)
        self._open_ones: dict[int, tuple[Any, Any]] = {}  # thread -> (playwright, browser)
        self._lock = threading.Lock()
        self._seen: dict[str, Any] = {}  # url -> (final, html) or the HttpError, this sweep
        self._busy: dict[str, threading.Event] = {}  # pages being opened right now
        self._stuck: set[int] = set()  # browsers that did not answer in PAGE_SECONDS

    def has(self, url: str) -> bool:
        """The page was opened in this sweep already (its answer is kept: no new page)."""
        with self._lock:
            return page_key(url) in self._seen or page_key(url) in self._busy

    def __call__(self, url: str) -> tuple[str, str]:
        key = page_key(url)
        while True:
            with self._lock:
                # One page once per sweep (4 Nov: CGI's, Apple's and Prism's careers pages
                # were opened again for each of their jobs and the 80 pages ran out halfway).
                if key in self._seen:
                    answer = self._seen[key]
                    if isinstance(answer, http.HttpError):
                        raise answer
                    return answer
                wait = self._busy.get(key)
                if wait is None:  # nobody is opening it: this call does
                    if self.left <= 0:
                        raise http.HttpError("page not rendered: the browser budget of this "
                                             "sweep is used")
                    self.left -= 1
                    self._busy[key] = threading.Event()
                    break
            wait.wait()  # another worker is opening the same page: its answer is kept
        try:
            answer = self._render(url)
        except http.HttpError as exc:
            self._done(key, exc)
            raise
        except BaseException:
            self._done(key, None)
            raise
        self._done(key, answer)
        return answer

    def _done(self, key: str, answer: Any) -> None:
        with self._lock:
            if answer is not None:
                self._seen[key] = answer
            event = self._busy.pop(key, None)
        if event is not None:
            event.set()

    def _render(self, url: str) -> tuple[str, str]:
        try:
            safety.assert_page_fetch_allowed(url, self.s)
        except safety.SafetyError as exc:
            raise http.HttpError(f"page not rendered: {exc}") from None
        with self._lock:
            if len(self._stuck) >= len(self._threads):
                raise http.HttpError("page not rendered: no browser left (all stuck)")
        try:
            n = self._free.get(timeout=PAGE_SECONDS)  # a free browser (waits while all are busy)
        except queue.Empty:
            raise http.HttpError("page not rendered: no browser was free in time") from None
        try:
            answer = self._threads[n].submit(self._open, n, url).result(timeout=PAGE_SECONDS)
        except FutureTimeout:
            with self._lock:
                self._stuck.add(n)
            log.warning("browser page did not answer in %.0f s: %s (that browser is not used "
                        "again in this sweep)", PAGE_SECONDS, http.safe_url(url))
            raise http.HttpError(f"page not rendered: no answer in {PAGE_SECONDS:.0f} s") from None
        except BaseException:
            self._free.put(n)
            raise
        self._free.put(n)
        return answer

    def _open(self, n: int, url: str) -> tuple[str, str]:
        """On browser thread n."""
        from jobengine.apply import form_bot

        if n not in self._open_ones:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            self._open_ones[n] = (playwright, form_bot._launch(playwright))
        page = self._open_ones[n][1].new_page()
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
            return page.url, page.content() + _frames(page)
        except http.HttpError:
            raise
        except Exception as exc:
            raise http.HttpError(f"page not rendered: {str(exc)[:120]}") from None
        finally:
            page.close()

    def close(self) -> None:
        try:
            for n, thread in enumerate(self._threads):
                if n in self._stuck:  # its thread is still on the page that never answered
                    continue
                try:
                    thread.submit(self._close, n).result(timeout=CLOSE_SECONDS)
                except FutureTimeout:
                    log.info("browser %d not closed in %.0f s: left", n, CLOSE_SECONDS)
        finally:
            for thread in self._threads:
                thread.shutdown(wait=False)

    def _close(self, n: int) -> None:
        """On browser thread n."""
        playwright, browser = self._open_ones.pop(n, (None, None))
        try:
            if browser is not None:
                browser.close()
            if playwright is not None:
                playwright.stop()
        except Exception:  # closing never fails a sweep
            log.exception("browser not closed")
