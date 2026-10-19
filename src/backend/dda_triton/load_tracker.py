#  Copyright 2026 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Tracked Triton loads (triton-model-cleanup-wedge, design D1).

Every load the backend requests goes through
``TritonEdgeClient.start_triton_model`` and from there through
:func:`request_load`, which holds the model's cross-process lock
(``dda_triton.model_locks``) SHARED from the request until the load reaches
a confirmed terminal state. A model component's Shutdown takes the same lock
EXCLUSIVE before it removes the model's repository entries, so it can never
delete them under a tracked load; while it holds the lock, a load request
is refused with :class:`ModelReleaseInProgress`.

"Terminal" needs confirmation. edgemlsdk's ``LoadModel`` only enqueues;
until a job-queue worker starts the load, every index refresh copies
Triton's previous state (often ``UNAVAILABLE`` from an earlier load) into
the cache. Only edgemlsdk's ``_markModelLoadFailed`` writes a ``reason``,
so ``READY`` or ``UNAVAILABLE`` with a reason proves that a load requested
after the registration finished or failed. Every other state that is not
``LOADING`` is unconfirmed and keeps the lock until it has lasted
:data:`TRACK_UNCONFIRMED_S`.

Backend-only. Imports neither ``panorama`` nor ``triton_edge_client``.
"""
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from dda_triton import model_locks

logger = logging.getLogger(__name__)

TRACK_POLL_S = 2.0
TRACK_UNCONFIRMED_S = 120.0

#: Injectable clocks (tests replace them).
_monotonic = time.monotonic
_wall = time.time
_sleep = time.sleep


class ModelReleaseInProgress(Exception):
    """The model's lock is held exclusive: a component Shutdown is removing
    its repository entries, or a publish is replacing them."""

    def __init__(self, model_id: str):
        super().__init__(
            "the repository entries of {0} are being removed or replaced".format(model_id))
        self.model_id = model_id


class _Track:
    __slots__ = ("handle", "generation", "unconfirmed_since", "unconfirmed_state")

    def __init__(self, handle, generation=0, unconfirmed_since=None):
        self.handle = handle
        self.generation = generation
        self.unconfirmed_since = unconfirmed_since
        self.unconfirmed_state = None


_lock = threading.Lock()
_tracked: Dict[str, _Track] = {}
#: model_id -> (wall, mono) of the latest accepted request; kept after release.
_requested: Dict[str, Tuple[float, float]] = {}
#: The index reader of the latest request (one client per process).
_read_states: Optional[Callable[[], Dict[str, Tuple[str, Optional[str]]]]] = None
_warned_unavailable = set()

_thread_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_failing_streak = False


def request_load(model_id: str, native_load: Callable[[str], object],
                 read_states: Callable[[], Dict[str, Tuple[str, Optional[str]]]],
                 lock_dir: Optional[str] = None) -> None:
    """Request a native load of ``model_id`` and track it.

    Raises :class:`ModelReleaseInProgress` (nothing reaches the native load)
    while the model's lock is held exclusive, ``ValueError`` for an invalid
    model name, and whatever ``native_load`` raises (after releasing the
    lock it took).
    """
    global _read_states
    with _lock:
        _read_states = read_states
        track = _tracked.get(model_id)
        if track is not None:
            # Already tracked: edgemlsdk dedupes while its cache says LOADING,
            # and a model whose last load failed enqueues a new one.
            native_load(model_id)
            track.generation += 1
            track.unconfirmed_since = None
            track.unconfirmed_state = None
            _requested[model_id] = (_wall(), _monotonic())
            return
        try:
            handle = model_locks.acquire(model_id, exclusive=False, timeout_s=0,
                                         lock_dir=lock_dir)
        except model_locks.LockTimeout:
            logger.info(
                "Refusing a load of %s: its repository entries are being removed or replaced "
                "(component shutdown or update in progress)", model_id)
            raise ModelReleaseInProgress(model_id)
        except model_locks.LockUnavailable as exc:
            key = model_locks.lock_key(model_id)
            if key not in _warned_unavailable:
                _warned_unavailable.add(key)
                logger.warning("Model lock for %s is unavailable (%s); loading without it",
                               key, exc)
            handle = None
        try:
            native_load(model_id)  # LoadModel only enqueues
        except Exception:
            if handle is not None:
                handle.release()
            raise
        _tracked[model_id] = _Track(handle)
        _requested[model_id] = (_wall(), _monotonic())
    ensure_tracker_thread()


def requested_at(model_id: str) -> Optional[Tuple[float, float]]:
    """``(wall, mono)`` of the latest accepted request of ``model_id``."""
    with _lock:
        return _requested.get(model_id)


def inflight() -> List[str]:
    """The models whose loads are tracked (their shared lock is held)."""
    with _lock:
        return sorted(_tracked)


def _classify(state, reason) -> str:
    state = str(state or "").strip().upper()
    if state in ("", "LOADING"):
        return "in_flight"  # an empty read counts as a failed read
    if state == "READY" or (state == "UNAVAILABLE" and reason):
        return "terminal"
    # UNAVAILABLE without a reason, UNKNOWN, UNLOADING, or absent.
    return "unconfirmed"


def _tracker_pass(now_mono: float) -> None:
    with _lock:
        pending = [(m, t.generation) for m, t in _tracked.items()]
        reader = _read_states
    if not pending or reader is None:
        return
    try:
        states = reader()  # one index read per pass, outside _lock
    except Exception:  # noqa: BLE001 - every handle is kept; retried next pass
        logger.debug("Triton index read for the load tracker failed", exc_info=True)
        return
    for m, generation in pending:
        state, reason = states.get(m, ("UNKNOWN", None))
        verdict = _classify(state, reason)
        with _lock:
            t = _tracked.get(m)
            if t is None or t.generation != generation:
                continue  # a newer request owns the handle now
            if verdict == "in_flight":
                t.unconfirmed_since = None  # no time bound: a wedged load keeps its lock
                t.unconfirmed_state = None
                continue
            if verdict == "unconfirmed":
                if t.unconfirmed_since is None:
                    t.unconfirmed_since = now_mono
                t.unconfirmed_state = str(state or "UNKNOWN").strip().upper() or "UNKNOWN"
                if now_mono - t.unconfirmed_since < TRACK_UNCONFIRMED_S:
                    continue
                logger.info(
                    "Released the model lock of %s: its load state stayed %s without a recorded "
                    "reason for %ds, so the load is treated as ended",
                    m, t.unconfirmed_state, int(TRACK_UNCONFIRMED_S))
            del _tracked[m]
            if t.handle is not None:
                t.handle.release()


def _contained_pass(now_mono: Optional[float] = None) -> bool:
    """One tracker pass inside the thread's containment. False when it
    raised (one ERROR per streak of failing passes, DEBUG after that)."""
    global _failing_streak
    try:
        _tracker_pass(_monotonic() if now_mono is None else now_mono)
    except Exception as exc:  # noqa: BLE001 - every handle is kept
        if not _failing_streak:
            logger.error(
                "Triton model-load tracker pass failed (%s); every model lock is kept and the "
                "next pass runs in %ds", exc, int(TRACK_POLL_S), exc_info=True)
        else:
            logger.debug("Triton model-load tracker pass failed again", exc_info=True)
        _failing_streak = True
        return False
    _failing_streak = False
    return True


def _run() -> None:
    while True:
        _sleep(TRACK_POLL_S)
        _contained_pass()


def ensure_tracker_thread() -> threading.Thread:
    """Start the tracker thread unless one is alive (for example after a
    ``BaseException`` ended it)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        thread = threading.Thread(target=_run, name="triton-load-tracker", daemon=True)
        thread.start()
        _thread = thread
        return thread


def _reset_for_tests() -> None:
    """Release every handle and forget every request (tests only)."""
    global _read_states, _failing_streak
    with _lock:
        for track in _tracked.values():
            if track.handle is not None:
                track.handle.release()
        _tracked.clear()
        _requested.clear()
        _warned_unavailable.clear()
        _read_states = None
        _failing_streak = False
