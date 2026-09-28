# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A failed grab must never leave the camera lock held.

Spec: .kiro/specs/camera-grab-lock-leak (bugfix).

``get_camera_frame`` acquired ``get_frame_lock`` BEFORE the ``try``/``finally``
that releases it, and the lazy ``connect_camera`` plus the "Camera not able to
connect" raise both sat between the two. A camera that could not be opened
therefore left the process-wide RLock owned by the failing thread: that
thread could re-enter it, every other thread's open/grab/close blocked until
the process restarted. Observed on jetson-thor1 (LocalServer.arm64JP7 1.0.48,
2026-09-28): a Basler on a USB 2 link failed with "Failed to bootstrap USB
device"; three previews answered 500 and the lock stayed held until a
container restart.

Each check runs the failing grab on a WORKER thread that stays alive
afterwards (like the API worker thread that served the previews, which goes
back to its pool) and then asks for the lock from ANOTHER thread. Both
halves matter: the RLock lets its owner re-enter, and a worker that has
EXITED can have its thread ident reused by the next thread, which the RLock
then treats as its owner. The first version of these tests let the worker
exit and passed on the unfixed code for exactly that reason. Every test
swaps in a fresh RLock, so a leak on unfixed code poisons only that test's
lock and never the rest of the session.

* Property 1 (Fix Checking, bugfix.md): for every call in the bug condition
  the caller sees exactly today's exception, and the lock is free afterwards.
* Property 2 (Preservation): every other call returns or raises exactly as
  today (values recorded on the unfixed code), and the lock is free after.
"""
import contextlib
import threading
import time
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from exceptions.api.aravis_camera_exception import AravisCameraException
from utils import camera_manager

THOR1_CAMERA_ID = "Basler-267601652282-23405186"
THOR1_ERROR = ("arv-device-error-quark: Failed to bootstrap USB device "
               "'(null)-(null)-(null)-267601652282' (3)")
STATIC_IDS = {camera_manager.STATIC_IMAGE_CAMERA_ID,
              camera_manager.STATIC_VIDEO_CAMERA_ID}
FRAME = {"data": b"\x10\x20\x30\x40", "height": 2, "width": 2}

camera_ids = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1, max_size=40).filter(lambda value: value not in STATIC_IDS)
camera_configs = st.one_of(
    st.none(),
    st.fixed_dictionaries({"gain": st.integers(0, 48),
                           "exposure": st.integers(1, 10_000_000)}))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class ParkedWorker:
    """Runs ``fn`` on a thread that stays alive after ``fn`` returns, like an
    API worker thread back in its pool, until the context exits. Keeping it
    alive stops a later thread from inheriting its ident (and with it any
    RLock ownership it leaked)."""

    def __init__(self, fn):
        self.result = None
        self.raised = None
        self._done = threading.Event()
        self._leave = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 - recorded for the caller
            self.raised = exc
        finally:
            self._done.set()
        self._leave.wait(30)

    def start(self):
        self._thread.start()
        assert self._done.wait(10), "the grab itself hung (camera lock held by another thread?)"
        return self

    def leave(self):
        self._leave.set()
        self._thread.join(5)


@contextlib.contextmanager
def parked_worker(fn):
    worker = ParkedWorker(fn)
    try:
        yield worker.start()
    finally:
        worker.leave()


def lock_free_for_another_thread(lock, timeout=1.0):
    """True when a thread other than the grab's can take the lock."""
    acquired = {}

    def target():
        acquired["ok"] = lock.acquire(timeout=timeout)
        if acquired["ok"]:
            lock.release()

    probe = threading.Thread(target=target, daemon=True)
    probe.start()
    probe.join(timeout=timeout + 5)
    return acquired.get("ok", False)


class WorkingCamera:
    """A connected camera whose grab cycle succeeds (the real
    ``_get_camera_frame`` drives it)."""

    def __init__(self, frame=FRAME):
        self.frame = frame
        self.calls = []

    def start_acquisition(self, config):
        self.calls.append(("start", config))

    def get_frame(self):
        self.calls.append(("get",))
        return camera_manager.encode_frame(self.frame)

    def stop_acquisition(self):
        self.calls.append(("stop",))


class FailingOpenCamera:
    """Stands in for ``manager_base.Camera`` when the device cannot be
    opened: construction succeeds, the status reports the Aravis error, and
    ``connect_camera`` raises it after cleaning up."""

    error = THOR1_ERROR

    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.disconnected = False

    def get_status(self):
        return camera_manager.CameraStatusModel(
            status=camera_manager.CameraStatusEnum.DISCONNECTED,
            lastUpdatedTime=time.time(), error=self.error)

    def disconnect(self):
        self.disconnected = True


# --------------------------------------------------------------------------
# Property 1 - Fix Checking (bug condition)
# --------------------------------------------------------------------------


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(camera_id=camera_ids, config=camera_configs,
       failure=st.sampled_from(["aravis_exception", "other_exception",
                                "no_camera_object"]),
       message=st.text(min_size=1, max_size=60))
def test_failed_open_raises_todays_exception_and_frees_the_lock(
        camera_id, config, failure, message):
    """**Feature: camera-grab-lock-leak, Property 1: a get_camera_frame call
    whose lazy open fails raises exactly today's exception and leaves the
    camera lock free for other threads.**"""
    lock = threading.RLock()
    raised_by_connect = {
        "aravis_exception": AravisCameraException(message),
        "other_exception": RuntimeError(message),
        "no_camera_object": None,
    }[failure]

    def fake_connect(requested_id):
        assert requested_id == camera_id
        if raised_by_connect is not None:
            raise raised_by_connect
        # returns without registering a camera object

    with patch.object(camera_manager, "get_frame_lock", lock), \
            patch.object(camera_manager, "camera_objects", {}), \
            patch.object(camera_manager, "connect_camera", side_effect=fake_connect), \
            parked_worker(lambda: camera_manager.get_camera_frame(camera_id, config)) as worker:
        raised = worker.raised
        if raised_by_connect is not None:
            assert raised is raised_by_connect
        else:
            assert type(raised) is Exception
            assert str(raised) == "Camera not able to connect for ID {}".format(camera_id)
        assert lock_free_for_another_thread(lock), (
            "get_camera_frame left the camera lock held after a failed open "
            "({}, camera {!r})".format(failure, camera_id))


def test_thor1_failed_open_does_not_block_another_cameras_grab():
    """The observed case end to end, through the REAL connect_camera: the
    Basler cannot be opened, the preview's exception is unchanged, and a
    grab of a working camera on another thread still completes."""
    lock = threading.RLock()
    objects = {"Fake_1": WorkingCamera()}
    config = {"gain": 1, "exposure": 500}
    with patch.object(camera_manager, "get_frame_lock", lock), \
            patch.object(camera_manager, "camera_objects", objects), \
            patch.object(camera_manager.manager_base, "Camera", FailingOpenCamera), \
            parked_worker(lambda: camera_manager.get_camera_frame(THOR1_CAMERA_ID, config)) as failed:
        assert isinstance(failed.raised, AravisCameraException)
        assert str(failed.raised) == THOR1_ERROR
        assert THOR1_CAMERA_ID not in objects

        with parked_worker(lambda: camera_manager.get_camera_frame("Fake_1", config)) as other:
            assert other.raised is None, other.raised
            assert other.result == FRAME


def test_repeated_failed_opens_leave_no_lock_behind():
    """Three failing previews in a row (as on thor1), each on its own
    worker thread: the lock is free after every one."""
    lock = threading.RLock()
    with patch.object(camera_manager, "get_frame_lock", lock), \
            patch.object(camera_manager, "camera_objects", {}), \
            patch.object(camera_manager.manager_base, "Camera", FailingOpenCamera):
        for _ in range(3):
            with parked_worker(lambda: camera_manager.get_camera_frame(THOR1_CAMERA_ID, None)) as worker:
                assert isinstance(worker.raised, AravisCameraException)
                assert lock_free_for_another_thread(lock)


# --------------------------------------------------------------------------
# Property 2 - Preservation (everything outside the bug condition)
# --------------------------------------------------------------------------


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(camera_id=camera_ids, config=camera_configs,
       scenario=st.sampled_from(["cached_ok", "cached_no_frame", "cached_grab_raises",
                                 "connect_then_ok", "connect_then_no_frame"]))
def test_calls_outside_the_bug_condition_behave_as_today(camera_id, config, scenario):
    """**Feature: camera-grab-lock-leak, Property 2: every get_camera_frame
    call outside the bug condition returns or raises exactly as the unfixed
    code does, reuses a cached camera without reconnecting, and frees the
    lock.**

    Recorded on the unfixed code: a frame is returned unchanged; a missing
    frame or a raising grab both surface as a plain Exception "Unable to get
    camera frame for camera id: {id}"; a cached camera is never reconnected;
    the configuration reaches the grab unchanged."""
    lock = threading.RLock()
    objects = {}
    connects = []
    grabs = []

    def fake_connect(requested_id):
        connects.append(requested_id)
        objects[requested_id] = object()

    def fake_grab(requested_id, camera, requested_config):
        grabs.append((requested_id, requested_config))
        if scenario.endswith("no_frame"):
            return None
        if scenario == "cached_grab_raises":
            raise RuntimeError("stream timeout")
        return FRAME

    if scenario.startswith("cached"):
        objects[camera_id] = object()

    with patch.object(camera_manager, "get_frame_lock", lock), \
            patch.object(camera_manager, "camera_objects", objects), \
            patch.object(camera_manager, "connect_camera", side_effect=fake_connect), \
            patch.object(camera_manager, "_get_camera_frame", side_effect=fake_grab), \
            parked_worker(lambda: camera_manager.get_camera_frame(camera_id, config)) as worker:
        if scenario in ("cached_ok", "connect_then_ok"):
            assert worker.raised is None and worker.result == FRAME
        else:
            assert type(worker.raised) is Exception
            assert str(worker.raised) == "Unable to get camera frame for camera id: {}".format(camera_id)
        assert connects == ([] if scenario.startswith("cached") else [camera_id])
        assert grabs == [(camera_id, config)]
        assert lock_free_for_another_thread(lock)


def test_static_cameras_still_bypass_the_lock():
    """Static ids are served before the lock: a grab succeeds even while
    another thread holds it (recorded on the unfixed code)."""
    lock = threading.RLock()
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    holding.wait(5)
    try:
        with patch.object(camera_manager, "get_frame_lock", lock), \
                patch.object(camera_manager, "get_static_image_store") as image_store, \
                patch.object(camera_manager, "get_static_video_store") as video_store:
            image_store.return_value.get_frame.return_value = FRAME
            video_store.return_value.get_frame.return_value = FRAME
            for static_id in sorted(STATIC_IDS):
                with parked_worker(
                        lambda sid=static_id: camera_manager.get_camera_frame(sid, None)) as worker:
                    assert worker.raised is None and worker.result == FRAME
    finally:
        release.set()
        holder.join(5)


def test_grab_still_holds_the_lock_while_it_runs():
    """Opens and grabs stay serialized: while one thread's grab is in
    progress, another thread cannot take the lock (recorded on the unfixed
    code; the LIBUSB_ERROR_BUSY protection must not regress)."""
    lock = threading.RLock()
    in_grab = threading.Event()
    finish = threading.Event()

    def slow_grab(camera_id, camera, config):
        in_grab.set()
        finish.wait(5)
        return FRAME

    with patch.object(camera_manager, "get_frame_lock", lock), \
            patch.object(camera_manager, "camera_objects", {"Fake_1": object()}), \
            patch.object(camera_manager, "_get_camera_frame", side_effect=slow_grab):
        worker = threading.Thread(
            target=lambda: camera_manager.get_camera_frame("Fake_1", None), daemon=True)
        worker.start()
        assert in_grab.wait(5)
        assert not lock_free_for_another_thread(lock, timeout=0.3)
        finish.set()
        worker.join(5)
        assert lock_free_for_another_thread(lock)
