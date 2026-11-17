import threading
import time

import pytest

from jobengine.web.worker import Worker


def test_submit_returns_at_once_and_runs_in_order():
    worker = Worker()
    release = threading.Event()
    done = []
    started = time.monotonic()
    worker.submit(lambda: (release.wait(5), done.append(1)))
    worker.submit(lambda: done.append(2))
    assert time.monotonic() - started < 0.5 and done == []
    release.set()
    worker.join()
    assert done == [1, 2]
    worker.stop()


def test_call_waits_for_the_result_and_errors_do_not_stop_the_worker():
    worker = Worker()
    with pytest.raises(RuntimeError, match="boom"):
        worker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker.call(lambda: 42) == 42
    worker.stop()
