import threading
import time

import pytest

from jobengine.parallel import Budget, chunks, run_all


def test_run_all_keeps_the_input_order_and_reports_progress():
    done = []

    def slow_square(n):
        time.sleep(0.01 * (5 - n))  # the last items finish first
        return n * n

    assert run_all(slow_square, [1, 2, 3, 4], 4, lambda d, t: done.append((d, t))) == [
        1, 4, 9, 16]
    assert done == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_run_all_really_runs_at_the_same_time():
    running, peak, lock = [0], [0], threading.Lock()

    def work(_):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    run_all(work, range(8), 4)
    assert peak[0] == 4


def test_one_worker_is_plain_sequential_and_errors_are_raised():
    assert run_all(str, [1, 2], 1) == ["1", "2"]

    def boom(n):
        raise ValueError(f"bad {n}")

    with pytest.raises(ValueError, match="bad"):
        run_all(boom, [1, 2, 3], 3)


def test_budget_is_shared_between_threads():
    budget = Budget(10)
    taken = run_all(lambda _: budget.take(), range(25), 8)
    assert taken.count(True) == 10 and budget.left == 0


def test_chunks():
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
