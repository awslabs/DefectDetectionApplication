"""The bounded thread pool for named-shadow writes
(workflow-telemetry-dashboards design §10.2; workflow-debug-mode §7.3).

One ``UpdateThingShadow`` takes about 0.2-0.5 s; the writes of one request
run on 8 threads with a 2 s ceiling per write, so 20 s of an API function's
29 s budget complete at least ``8 x 20 / 2 = 80`` writes in the worst case.
The synchronous threshold is half of that: ``SHADOW_WRITE_SYNC_LIMIT = 40``
devices. The debug mode caps a session at 32 devices, below the limit, so its
creation always completes inside the request; the dashboards answer 202 above
it and finish the writes from their maintenance function.
"""
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, List, Optional

__all__ = ["SHADOW_WRITE_SYNC_LIMIT", "WRITE_THREADS", "WRITE_CEILING_SECONDS",
           "WriteResult", "write_all"]

SHADOW_WRITE_SYNC_LIMIT = 40
WRITE_THREADS = 8
WRITE_CEILING_SECONDS = 2.0

#: One per input item, in input order. ``ok`` is True when ``fn`` returned;
#: ``value`` is what it returned; ``error`` is the exception's text (at most
#: 200 characters) or ``'timeout'`` when the ceiling passed first.
WriteResult = namedtuple("WriteResult", ["item", "ok", "value", "error", "timed_out"])


def write_all(items: Iterable[Any], fn: Callable[[Any], Any], *,
              threads: int = WRITE_THREADS,
              ceiling_seconds: float = WRITE_CEILING_SECONDS) -> List[WriteResult]:
    """Call ``fn(item)`` for every item on at most ``threads`` threads, each
    call bounded by ``ceiling_seconds`` from the moment it starts running. A
    call that exceeds the ceiling is reported ``timed_out`` (its thread is
    left to finish on its own; nothing waits for it). Exceptions never
    propagate: they become ``ok=False`` results."""
    items = list(items)
    if not items:
        return []
    started: List[Optional[float]] = [None] * len(items)
    started_lock = threading.Lock()

    def run(index: int) -> Any:
        with started_lock:
            started[index] = time.monotonic()
        return fn(items[index])

    executor = ThreadPoolExecutor(max_workers=max(1, min(threads, len(items))),
                                  thread_name_prefix="shadow-write")
    results: List[Optional[WriteResult]] = [None] * len(items)
    try:
        futures = [executor.submit(run, index) for index in range(len(items))]
        # Every item is waited for at most ``ceiling_seconds`` after it
        # started; an item that has not started yet is waited for until the
        # pool reaches it (bounded by the ceilings of the items before it).
        overall_deadline = time.monotonic() + ceiling_seconds * (len(items) / max(1, threads) + 1.0)
        for index, future in enumerate(futures):
            while True:
                with started_lock:
                    begun = started[index]
                now = time.monotonic()
                if begun is None:
                    if future.done():
                        break
                    if now >= overall_deadline:
                        break
                    time.sleep(0.005)
                    continue
                remaining = begun + ceiling_seconds - now
                if remaining <= 0 and not future.done():
                    break
                try:
                    future.result(timeout=max(0.0, remaining))
                except Exception:  # noqa: BLE001 - recorded below
                    pass
                break
            if future.done():
                exc = future.exception()
                if exc is None:
                    results[index] = WriteResult(items[index], True, future.result(), None, False)
                else:
                    results[index] = WriteResult(items[index], False, None, str(exc)[:200], False)
            else:
                future.cancel()
                results[index] = WriteResult(items[index], False, None, "timeout", True)
    finally:
        executor.shutdown(wait=False)
    return [r for r in results if r is not None]
