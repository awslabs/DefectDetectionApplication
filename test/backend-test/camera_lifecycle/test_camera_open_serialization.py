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
"""Camera open/close/grab must be serialized against each other.

A USB3Vision device admits one claim. Two overlapping opens make the second
fail LIBUSB_ERROR_BUSY and leave the cached Camera broken, so every later open
fails too and the camera is stranded until the process restarts. Reproduced on
a DLAP-701 by saving an Image_Source ROI while the ~2 Hz live preview polled:

    16:52:30.287  Connecting          <- open #1 (preview's lazy connect)
    16:52:31.498  Setup camera
    16:52:31.524  Connecting          <- open #2, 26 ms later, overlapping
    16:52:32.812  ERROR ... LIBUSB_ERROR_BUSY

`get_camera_frame` held `get_frame_lock` around its own lazy `connect_camera`,
but `connect_camera`/`disconnect_camera` called straight from an endpoint or a
config-change reconnect took no lock at all.

The fake camera below asserts single-claim exclusivity the way the hardware
does: if a second construction begins while one is already open, it raises.
That makes these tests fail against the unserialized code rather than merely
passing against the fixed code.
"""
import threading
import time

import pytest

from utils import camera_manager


class ClaimViolation(Exception):
    """A second open overlapped the first -- the LIBUSB_ERROR_BUSY case."""


class FakeCamera:
    """Stands in for manager_base.Camera, enforcing exclusive USB claim.

    Construction takes a beat, which is what gives an unserialized second
    caller the window to overlap it.
    """

    open_count = 0
    live = 0
    lock = threading.Lock()
    open_delay = 0.05

    def __init__(self, camera_id):
        self.camera_id = camera_id
        with FakeCamera.lock:
            if FakeCamera.live:
                raise ClaimViolation(
                    "Failed to claim USB interface: LIBUSB_ERROR_BUSY")
            FakeCamera.live += 1
            FakeCamera.open_count += 1
        time.sleep(FakeCamera.open_delay)   # the vulnerable window

    def disconnect(self):
        with FakeCamera.lock:
            FakeCamera.live = max(0, FakeCamera.live - 1)

    def get_status(self):
        return camera_manager.CameraStatusModel(
            status=camera_manager.CameraStatusEnum.CONNECTED,
            lastUpdatedTime=time.time(), error=None)


CAMERA_ID = "Basler-267601652282-23405186"


@pytest.fixture(autouse=True)
def fake_camera(monkeypatch):
    FakeCamera.open_count = 0
    FakeCamera.live = 0
    monkeypatch.setattr(camera_manager.manager_base, "Camera", FakeCamera)
    # A plain dict stands in for the manager dict; the real one is a
    # multiprocessing proxy that needs no server here.
    monkeypatch.setattr(camera_manager, "camera_objects", {})
    yield
    camera_manager.camera_objects.clear()


def run_concurrently(*fns):
    """Run callables on threads, returning the exception each raised (or None)."""
    errors = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def wrap(index, fn):
        def inner():
            barrier.wait()          # maximise overlap
            try:
                fn()
            except Exception as exc:   # noqa: BLE001 - recorded, not swallowed
                errors[index] = exc
        return inner

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a thread deadlocked"
    return errors


class TestLockIsReentrant:
    def test_get_frame_lock_is_reentrant(self):
        """get_camera_frame holds the lock and then calls connect_camera; a
        non-reentrant lock would self-deadlock on that nesting."""
        assert camera_manager.get_frame_lock.acquire(timeout=1)
        try:
            assert camera_manager.get_frame_lock.acquire(timeout=1), \
                "same-thread re-acquire must succeed"
            camera_manager.get_frame_lock.release()
        finally:
            camera_manager.get_frame_lock.release()


class TestConcurrentOpens:
    def test_two_direct_connects_do_not_overlap(self):
        """The endpoint path: two connect_camera calls racing each other."""
        errors = run_concurrently(
            lambda: camera_manager.connect_camera(CAMERA_ID),
            lambda: camera_manager.connect_camera(CAMERA_ID),
        )
        assert not any(isinstance(e, ClaimViolation) for e in errors), \
            "overlapping opens claimed the device twice: %s" % errors

    def test_connect_racing_a_frame_grab_does_not_overlap(self):
        """The reported failure: an ROI save reconnecting while the live preview
        is grabbing."""
        camera_manager.connect_camera(CAMERA_ID)

        def grab():
            camera_manager.get_frame_lock.acquire()
            try:
                time.sleep(0.15)     # stand in for an in-flight acquisition
            finally:
                camera_manager.get_frame_lock.release()

        errors = run_concurrently(
            grab, lambda: camera_manager.connect_camera(CAMERA_ID))
        assert not any(isinstance(e, ClaimViolation) for e in errors), \
            "a reconnect overlapped a grab: %s" % errors

    def test_disconnect_racing_a_connect_does_not_strand_the_device(self):
        camera_manager.connect_camera(CAMERA_ID)
        errors = run_concurrently(
            lambda: camera_manager.disconnect_camera(CAMERA_ID),
            lambda: camera_manager.connect_camera(CAMERA_ID),
        )
        assert not any(isinstance(e, ClaimViolation) for e in errors), errors
        # Whoever finished last, the device must be usable again afterwards.
        camera_manager.disconnect_camera(CAMERA_ID)
        assert camera_manager.connect_camera(CAMERA_ID) is True

    def test_many_racing_opens_all_serialize(self):
        errors = run_concurrently(
            *[lambda: camera_manager.connect_camera(CAMERA_ID) for _ in range(8)])
        assert not any(isinstance(e, ClaimViolation) for e in errors), errors
        assert FakeCamera.live == 1, "exactly one claim should remain open"


class TestBehaviourPreserved:
    def test_connect_still_returns_true_and_caches(self):
        assert camera_manager.connect_camera(CAMERA_ID) is True
        assert CAMERA_ID in camera_manager.camera_objects

    def test_connect_replaces_an_existing_connection(self):
        """Reconnect semantics are unchanged -- an explicit connect still tears
        the old claim down and opens a fresh one."""
        camera_manager.connect_camera(CAMERA_ID)
        first = FakeCamera.open_count
        camera_manager.connect_camera(CAMERA_ID)
        assert FakeCamera.open_count == first + 1
        assert FakeCamera.live == 1

    def test_disconnect_is_idempotent_and_clears_the_cache(self):
        camera_manager.connect_camera(CAMERA_ID)
        assert camera_manager.disconnect_camera(CAMERA_ID) is True
        assert CAMERA_ID not in camera_manager.camera_objects
        assert camera_manager.disconnect_camera(CAMERA_ID) is True

    def test_static_image_camera_never_takes_the_lock(self):
        """The virtual camera has no device to claim; it must not be able to
        block on hardware serialization."""
        camera_manager.get_frame_lock.acquire()
        try:
            assert camera_manager.disconnect_camera(
                camera_manager.STATIC_IMAGE_CAMERA_ID) is True
        finally:
            camera_manager.get_frame_lock.release()
