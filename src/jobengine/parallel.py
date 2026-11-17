"""Run slow network work (job pages, careers pages, ATS feeds, LLM calls) on a few threads.

Results come back in the order of the inputs, so a run gives the same output with 1 worker
or with 8. Progress callbacks and all bookkeeping stay on the calling thread; only the
network calls run in the pool.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from jobengine.jobctl import CONTROL

TICK_SECONDS = 2.0  # how often a waiting run checks for /end
WAITING_SECONDS = 60.0  # how often a long wait says what it waits for


def run_all[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int,
                  done: Callable[[int, int], None] | None = None,
                  tick: Callable[[], None] | None = None, *,
                  limit: float | None = None, late: Callable[[T], R] | None = None,
                  waiting: Callable[[list[T], float], None] | None = None) -> list[R]:
    """[fn(item) for item in items], on up to `workers` threads, in input order.

    `done(finished, total)` is called on this thread after each item finishes. The first
    exception raised by `fn` is raised here once every started item has finished.
    `tick` (default: the /end check) is called on this thread every TICK_SECONDS while it
    waits. After /end it raises Stopped: the run stops at once, items not started are
    dropped and the started ones finish on their own (30 Oct: one hanging source kept /fetch
    running long after /end).
    `limit` (seconds, with `late`): items still running then are not waited for; their
    result is late(item) and they finish on their own (30 Oct: /fetch hung on one source
    until you sent /end). `waiting(items, seconds)` is called every WAITING_SECONDS with the
    items still running, for the log and the progress message.
    """
    check = CONTROL.check if tick is None else tick
    todo = list(items)
    if workers <= 1 or len(todo) <= 1:
        out = []
        for i, item in enumerate(todo, 1):
            check()
            out.append(fn(item))
            if done is not None:
                done(i, len(todo))
        return out
    results: list[R | None] = [None] * len(todo)
    pool = ThreadPoolExecutor(max_workers=min(workers, len(todo)))
    futures = {pool.submit(fn, item): i for i, item in enumerate(todo)}
    pending = set(futures)
    finished = 0
    started = time.monotonic()
    told = started
    try:
        while pending:
            ready, pending = wait(pending, timeout=TICK_SECONDS, return_when=FIRST_COMPLETED)
            for future in ready:
                results[futures[future]] = future.result()
                finished += 1
                if done is not None:
                    done(finished, len(todo))
            check()
            now = time.monotonic()
            if pending and waiting is not None and now - told >= WAITING_SECONDS:
                told = now
                waiting([todo[futures[f]] for f in pending], now - started)
            if pending and limit is not None and late is not None and now - started >= limit:
                for future in pending:
                    results[futures[future]] = late(todo[futures[future]])
                pool.shutdown(wait=False, cancel_futures=True)
                return results  # type: ignore[return-value]
    except Exception:
        pool.shutdown(wait=True)
        raise
    except BaseException:  # Stopped by /end: do not wait for the running items
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results  # type: ignore[return-value]


class Budget:
    """A count of calls shared by threads (for example SmartRecruiters detail calls)."""

    def __init__(self, left: int):
        self.left = left
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


def chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), max(1, size)):
        yield items[i:i + max(1, size)]
