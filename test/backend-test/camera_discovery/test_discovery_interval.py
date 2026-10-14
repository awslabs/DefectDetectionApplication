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
"""Unit tests for the Camera_Discovery re-enumeration interval
configuration and ``on_change`` suppression.

Feature: camera-registry-sync (Requirement 2.3): re-enumeration at a
configurable interval defaulting to 5 minutes, the interval override
flowing through the existing feature-config mechanism
(``CameraDiscoveryIntervalSeconds``), and ``on_change`` firing only when
consecutive enumerations actually change the tracked inventory.
"""
from fake_v4l2 import FakeDevice, FakeV4l2Io

from camera_discovery import CameraDiscovery
from camera_discovery.discovery import (
    DEFAULT_INTERVAL_SECONDS,
    INTERVAL_CONFIG_KEY,
)


class FakeClock:
    """Injectable clock returning a controlled epoch-seconds value."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _discovery(devices=None, config=None, config_error=None, clock=None):
    io = FakeV4l2Io(devices or {})

    provider = None
    if config_error is not None:
        def provider():  # noqa: E306 - tiny local fake
            raise config_error
    elif config is not None:
        def provider():
            return config

    return CameraDiscovery(
        v4l2_io=io,
        config_provider=provider,
        clock=clock if clock is not None else FakeClock(),
    )


class TestIntervalResolution:
    def test_default_interval_is_300_seconds(self):
        # Requirement 2.3: default 5 minutes
        assert DEFAULT_INTERVAL_SECONDS == 300
        discovery = _discovery()
        assert discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS) == 300.0

    def test_feature_config_override_honored(self):
        discovery = _discovery(config={INTERVAL_CONFIG_KEY: 45})
        assert discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS) == 45.0

    def test_numeric_string_override_honored(self):
        # Component configuration values commonly arrive as strings
        discovery = _discovery(config={INTERVAL_CONFIG_KEY: "120"})
        assert discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS) == 120.0

    def test_missing_key_falls_back_to_argument(self):
        discovery = _discovery(config={"StationName": "line-1"})
        assert discovery._resolve_interval(600) == 600.0

    def test_non_numeric_value_falls_back(self):
        discovery = _discovery(config={INTERVAL_CONFIG_KEY: "soon"})
        assert (
            discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS)
            == float(DEFAULT_INTERVAL_SECONDS)
        )

    def test_non_positive_value_falls_back(self):
        for bad in (0, -5):
            discovery = _discovery(config={INTERVAL_CONFIG_KEY: bad})
            assert (
                discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS)
                == float(DEFAULT_INTERVAL_SECONDS)
            )

    def test_raising_config_provider_falls_back(self):
        # Requirement 11.2 adjacent: a broken config read must not break
        # discovery startup
        discovery = _discovery(config_error=RuntimeError("config exploded"))
        assert (
            discovery._resolve_interval(DEFAULT_INTERVAL_SECONDS)
            == float(DEFAULT_INTERVAL_SECONDS)
        )

    def test_none_config_falls_back(self):
        discovery = _discovery(config=None)
        # No provider at all
        assert discovery._resolve_interval(90) == 90.0


class TestStartAppliesInterval:
    def test_start_defaults_to_300_seconds(self):
        discovery = _discovery()
        discovery.start()
        try:
            assert discovery._interval == float(DEFAULT_INTERVAL_SECONDS)
        finally:
            discovery.stop()

    def test_start_uses_feature_config_override(self):
        discovery = _discovery(config={INTERVAL_CONFIG_KEY: 30})
        discovery.start(interval_seconds=900)
        try:
            assert discovery._interval == 30.0
        finally:
            discovery.stop()

    def test_start_uses_argument_without_override(self):
        discovery = _discovery(config={})
        discovery.start(interval_seconds=900)
        try:
            assert discovery._interval == 900.0
        finally:
            discovery.stop()


class TestOnChangeSuppression:
    def test_identical_enumerations_do_not_refire_on_change(self):
        clock = FakeClock()
        devices = {"/dev/video0": FakeDevice(card="Cam A")}
        io = FakeV4l2Io(devices)
        discovery = CameraDiscovery(v4l2_io=io, clock=clock)

        calls = []
        discovery._on_change = calls.append

        # First pass: empty -> one camera is a change.
        discovery.run_once()
        assert len(calls) == 1

        # Identical re-enumerations: suppressed even as the clock moves.
        for _ in range(3):
            clock.advance(300)
            discovery.run_once()
        assert len(calls) == 1

    def test_inventory_change_fires_on_change_again(self):
        clock = FakeClock()
        io = FakeV4l2Io({"/dev/video0": FakeDevice(card="Cam A")})
        discovery = CameraDiscovery(v4l2_io=io, clock=clock)

        calls = []
        discovery._on_change = calls.append

        discovery.run_once()
        clock.advance(300)
        discovery.run_once()
        assert len(calls) == 1

        # A camera disappearing changes the inventory (marked absent).
        del io.devices["/dev/video0"]
        clock.advance(300)
        snapshot = discovery.run_once()
        assert len(calls) == 2
        (entry,) = snapshot.cameras.values()
        assert entry.absent is True
        assert entry.absent_since == int(clock.now * 1000)

        # And the now-empty enumeration repeating is again suppressed.
        clock.advance(300)
        discovery.run_once()
        assert len(calls) == 2

    def test_empty_enumerations_never_fire_on_change(self):
        clock = FakeClock()
        discovery = CameraDiscovery(v4l2_io=FakeV4l2Io({}), clock=clock)

        calls = []
        discovery._on_change = calls.append

        for _ in range(3):
            discovery.run_once()
            clock.advance(300)
        assert calls == []
