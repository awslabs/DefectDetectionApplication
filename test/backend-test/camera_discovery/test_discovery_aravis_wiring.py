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
"""Unit tests for the Aravis wiring into the CameraDiscovery loop.

Feature: aravis-camera-input (Requirements 2.1, 2.5, 2.7).

Example tests of the injectable Aravis enumerator running alongside the
V4L2 layer in each periodic pass: both families land in one tracked
snapshot, Aravis stable ids get the identical absence semantics, an empty
Aravis bus leaves V4L2-only behavior unchanged, and ``on_change`` fires
only on change. The exhaustive absence property is the separate
``test_property_*`` module (optional task 3.4).
"""
from fake_v4l2 import FakeDevice, FakeV4l2Io

from camera_discovery import CameraDiscovery, DiscoveredCamera
from camera_discovery.aravis import DiscoveredAravisCamera, aravis_stable_id
from camera_discovery.discovery import diff_snapshot


class FakeAravisCamera:
    """The attribute shape of ``model.Camera`` from aravis_functions."""

    def __init__(
        self,
        id="Aravis-Fake-GV01",
        model="Fake GV Camera",
        address="192.168.0.10",
        physical_id="fake-phys-0",
        protocol="Fake",
        serial="GV01",
        vendor="Aravis",
    ):
        self.id = id
        self.model = model
        self.address = address
        self.physical_id = physical_id
        self.protocol = protocol
        self.serial = serial
        self.vendor = vendor


class FakeAravisBus:
    """Mutable fake bus: the injected enumerator returns ``cameras``."""

    def __init__(self, cameras=()):
        self.cameras = list(cameras)

    def __call__(self):
        return list(self.cameras)


class FakeClock:
    """Injectable clock returning a controlled epoch-seconds value."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _v4l2_stable_ids(io):
    return {c.stable_id for c in CameraDiscovery(v4l2_io=io).enumerate().cameras}


class TestBothFamiliesInOnePass:
    def test_one_pass_tracks_v4l2_and_aravis_cameras(self):
        # Requirements 2.1, 2.7: each periodic pass runs both enumerations;
        # the snapshot carries both families, distinguished by entry type.
        io = FakeV4l2Io({"/dev/video0": FakeDevice(card="Cam A")})
        bus = FakeAravisBus([FakeAravisCamera()])
        discovery = CameraDiscovery(v4l2_io=io, aravis_enumerator=bus)

        snapshot = discovery.run_once()

        aravis_id = aravis_stable_id("Aravis", "Fake GV Camera", "GV01")
        assert set(snapshot.cameras) == _v4l2_stable_ids(io) | {aravis_id}

        aravis_entry = snapshot.cameras[aravis_id]
        assert isinstance(aravis_entry.camera, DiscoveredAravisCamera)
        assert aravis_entry.camera.camera_id == "Aravis-Fake-GV01"
        assert aravis_entry.absent is False

        v4l2_entries = [
            entry
            for stable_id, entry in snapshot.cameras.items()
            if stable_id != aravis_id
        ]
        assert all(isinstance(e.camera, DiscoveredCamera) for e in v4l2_entries)
        assert snapshot.failures == ()

    def test_aravis_enumeration_failure_keeps_v4l2_inventory(self):
        # Requirement 2.6 adjacent: a raising enumerator records a failure
        # and leaves the V4L2 side of the pass intact.
        io = FakeV4l2Io({"/dev/video0": FakeDevice(card="Cam A")})

        def broken():
            raise RuntimeError("bus exploded")

        discovery = CameraDiscovery(v4l2_io=io, aravis_enumerator=broken)
        snapshot = discovery.run_once()

        assert set(snapshot.cameras) == _v4l2_stable_ids(io)
        (failure,) = snapshot.failures
        assert "bus exploded" in failure["error"]


class TestAravisAbsenceMarking:
    def test_disappeared_aravis_camera_marked_absent_not_dropped(self):
        # Requirement 2.5: a previously discovered Aravis camera missing
        # from a re-enumeration is marked absent with a timestamp, never
        # deleted from the tracked set.
        clock = FakeClock()
        bus = FakeAravisBus([FakeAravisCamera()])
        discovery = CameraDiscovery(
            v4l2_io=FakeV4l2Io({}), aravis_enumerator=bus, clock=clock
        )

        discovery.run_once()

        bus.cameras = []  # camera unplugged from the bus
        clock.advance(300)
        snapshot = discovery.run_once()

        aravis_id = aravis_stable_id("Aravis", "Fake GV Camera", "GV01")
        entry = snapshot.cameras[aravis_id]
        assert entry.absent is True
        assert entry.absent_since == int(clock.now * 1000)
        assert isinstance(entry.camera, DiscoveredAravisCamera)

    def test_returning_aravis_camera_loses_absence_marking(self):
        clock = FakeClock()
        camera = FakeAravisCamera()
        bus = FakeAravisBus([camera])
        discovery = CameraDiscovery(
            v4l2_io=FakeV4l2Io({}), aravis_enumerator=bus, clock=clock
        )

        discovery.run_once()
        bus.cameras = []
        clock.advance(300)
        discovery.run_once()
        bus.cameras = [camera]
        clock.advance(300)
        snapshot = discovery.run_once()

        aravis_id = aravis_stable_id("Aravis", "Fake GV Camera", "GV01")
        entry = snapshot.cameras[aravis_id]
        assert entry.absent is False
        assert entry.absent_since is None


class TestV4l2OnlyBehaviorUnchanged:
    def test_empty_aravis_bus_yields_pure_v4l2_snapshot(self):
        # Requirement 2.7: with an empty Aravis enumeration the tracked
        # snapshot equals the pure V4L2 diff for the same devices.
        clock = FakeClock()
        devices = {
            "/dev/video0": FakeDevice(card="Cam A"),
            "/dev/video1": FakeDevice(card="Cam B"),
        }
        discovery = CameraDiscovery(
            v4l2_io=FakeV4l2Io(devices),
            aravis_enumerator=FakeAravisBus([]),
            clock=clock,
        )

        snapshot = discovery.run_once()

        v4l2_result = CameraDiscovery(v4l2_io=FakeV4l2Io(devices)).enumerate()
        expected, _ = diff_snapshot({}, v4l2_result, int(clock.now * 1000))
        assert dict(snapshot.cameras) == expected
        assert snapshot.failures == ()

    def test_v4l2_absence_semantics_unchanged_with_empty_aravis_bus(self):
        clock = FakeClock()
        io = FakeV4l2Io({"/dev/video0": FakeDevice(card="Cam A")})
        discovery = CameraDiscovery(
            v4l2_io=io, aravis_enumerator=FakeAravisBus([]), clock=clock
        )

        discovery.run_once()
        del io.devices["/dev/video0"]
        clock.advance(300)
        snapshot = discovery.run_once()

        (entry,) = snapshot.cameras.values()
        assert entry.absent is True
        assert entry.absent_since == int(clock.now * 1000)


class TestOnChangeFiresOnlyOnChange:
    def test_identical_combined_enumerations_suppress_on_change(self):
        clock = FakeClock()
        io = FakeV4l2Io({"/dev/video0": FakeDevice(card="Cam A")})
        bus = FakeAravisBus([FakeAravisCamera()])
        discovery = CameraDiscovery(
            v4l2_io=io, aravis_enumerator=bus, clock=clock
        )

        calls = []
        discovery._on_change = calls.append

        discovery.run_once()  # empty -> two cameras: a change
        assert len(calls) == 1

        for _ in range(3):
            clock.advance(300)
            discovery.run_once()
        assert len(calls) == 1

    def test_aravis_bus_change_fires_on_change(self):
        clock = FakeClock()
        bus = FakeAravisBus([FakeAravisCamera()])
        discovery = CameraDiscovery(
            v4l2_io=FakeV4l2Io({}), aravis_enumerator=bus, clock=clock
        )

        calls = []
        discovery._on_change = calls.append

        discovery.run_once()
        clock.advance(300)
        discovery.run_once()
        assert len(calls) == 1

        bus.cameras = []  # disappearance changes the tracked inventory
        clock.advance(300)
        discovery.run_once()
        assert len(calls) == 2

        clock.advance(300)
        discovery.run_once()  # repeated empty bus: suppressed again
        assert len(calls) == 2
