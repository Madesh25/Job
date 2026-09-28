"""Run slow network work (job pages, careers pages, ATS feeds, LLM calls) on a few threads.

Results come back in the order of the inputs, so a run gives the same output with 1 worker
or with 8. Progress callbacks and all bookkeeping stay on the calling thread; only the
network calls run in the pool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_all[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int,
                  done: Callable[[int, int], None] | None = None) -> list[R]:
    """[fn(item) for item in items], on up to `workers` threads, in input order.

    `done(finished, total)` is called on this thread after each item finishes. The first
    exception raised by `fn` is raised here once every started item has finished.
    """
    todo = list(items)
    if workers <= 1 or len(todo) <= 1:
        out = []
        for i, item in enumerate(todo, 1):
            out.append(fn(item))
            if done is not None:
                done(i, len(todo))
        return out
    results: list[R | None] = [None] * len(todo)
    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(todo)}
        for finished, future in enumerate(as_completed(futures), 1):
            results[futures[future]] = future.result()
            if done is not None:
                done(finished, len(todo))
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
