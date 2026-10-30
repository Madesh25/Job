"""Run slow network work (job pages, careers pages, ATS feeds, LLM calls) on a few threads.

Results come back in the order of the inputs, so a run gives the same output with 1 worker
or with 8. Progress callbacks and all bookkeeping stay on the calling thread; only the
network calls run in the pool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from jobengine.jobctl import CONTROL

TICK_SECONDS = 2.0  # how often a waiting run checks for /end


def run_all[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int,
                  done: Callable[[int, int], None] | None = None,
                  tick: Callable[[], None] | None = None) -> list[R]:
    """[fn(item) for item in items], on up to `workers` threads, in input order.

    `done(finished, total)` is called on this thread after each item finishes. The first
    exception raised by `fn` is raised here once every started item has finished.
    `tick` (default: the /end check) is called on this thread every TICK_SECONDS while it
    waits. After /end it raises Stopped: the run stops at once, items not started are
    dropped and the started ones finish on their own (30 Oct: one hanging source kept /fetch
    running long after /end).
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
    try:
        while pending:
            ready, pending = wait(pending, timeout=TICK_SECONDS, return_when=FIRST_COMPLETED)
            for future in ready:
                results[futures[future]] = future.result()
                finished += 1
                if done is not None:
                    done(finished, len(todo))
            check()
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
