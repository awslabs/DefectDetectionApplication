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
"""Unit tests for Edge_Sync_Agent report timing.

Example-based tests with a fake clock (no hypothesis) covering:

- the 5 s debounce window: a burst of ``report_inventory`` calls yields at
  most one shadow write per window,
- the 30 s publication bound of Requirement 3.1: a triggered report is
  published within ``MAX_REPORT_DELAY_SECONDS``,
- the startup full report of Requirement 3.4: ``start()`` publishes the
  complete current inventory immediately,
- the Image_Source CRUD hook (``notify_image_source_changed``): triggers a
  report on the active agent, is a no-op without one, and never propagates
  a raising agent into the route layer.

_Requirements: 3.1, 3.4_

The agent is driven deterministically through :meth:`EdgeSyncAgent.pump`
(one scheduling step per call) with an injectable monotonic clock, exactly
like the reconnect catch-up property harness in
``test_property_reconnect_catch_up.py``.
"""
import contextlib
import threading

import pytest

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    DEBOUNCE_SECONDS,
    MAX_REPORT_DELAY_SECONDS,
    SCHEMA_VERSION,
    CameraSyncStateStore,
    EdgeSyncAgent,
    build_inventory,
    clear_active_agent,
    get_active_agent,
    notify_image_source_changed,
    set_active_agent,
)

# --- fakes -------------------------------------------------------------------


class _FakeClock:
    """Injectable monotonic/wall clock advanced explicitly by the test."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeShadowAccessor:
    """IoTShadowAccessor stand-in recording each successful write together
    with the fake-clock time at which it happened."""

    def __init__(self, clock):
        self._clock = clock
        self.writes = []  # (document, clock_time)
        self.on_write = None  # optional callback (threaded startup test)

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self.writes.append((state["reported"], self._clock.now))
        if self.on_write is not None:
            self.on_write()


class _FakeImageSourceAccessor:
    def __init__(self, sources):
        self._sources = sources

    def list_image_sources(self, request, session):
        return [dict(source) for source in self._sources]


class _FakeDiscovery:
    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


# --- fixtures / helpers --------------------------------------------------------

_CONFIGURED_SOURCE = {
    "imageSourceId": "is-1",
    "name": "Line 1 inspection cam",
    "type": "Camera",
    "cameraId": "cam-1",
    "imageSourceConfiguration": {"device": "/dev/video0", "gain": 4},
}

_DISCOVERED_CAMERA = DiscoveredCamera(
    stable_id="disc-000000000001",
    device_path="/dev/video1",
    card_name="USB Camera",
    bus_info="usb-0000:00:14.0-1",
    driver="uvcvideo",
    kind="v4l2",
    formats=[{"pixel_format": "YUYV", "resolutions": [[1920, 1080]]}],
)


@pytest.fixture(autouse=True)
def _clean_hook_registry():
    """The hook registry is module-global state; keep tests isolated."""
    clear_active_agent()
    yield
    clear_active_agent()


def _make_agent(tmp_path, clock, shadow=None):
    sources = [_CONFIGURED_SOURCE]
    snapshot = DiscoveryResult(cameras=[_DISCOVERED_CAMERA])
    shadow = shadow if shadow is not None else _FakeShadowAccessor(clock)
    agent = EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=_FakeImageSourceAccessor(sources),
        camera_discovery=_FakeDiscovery(snapshot),
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "camera_sync_state.json")),
        thing_name="test-thing",
        clock=clock,
        wall_clock=clock,
    )
    return agent, shadow, sources, snapshot


# --- debounce window (Requirement 3.1) -----------------------------------------


def test_burst_of_triggers_yields_at_most_one_write_per_debounce_window(tmp_path):
    """A burst of report_inventory calls inside one 5 s window coalesces
    into a single shadow write; the next write happens only after the
    window has elapsed. _Requirements: 3.1_"""
    clock = _FakeClock()
    agent, shadow, _, _ = _make_agent(tmp_path, clock)

    # First trigger publishes immediately (no earlier write to debounce).
    agent.report_inventory()
    agent.pump()
    assert len(shadow.writes) == 1

    # A burst of triggers spread across the debounce window: no write
    # while the window is open.
    for _ in range(10):
        clock.advance(0.4)  # 10 * 0.4 = 4.0 s < DEBOUNCE_SECONDS
        agent.report_inventory()
        delay = agent.pump()
        assert delay is not None and delay > 0.0
    assert len(shadow.writes) == 1

    # Once the window expires, exactly one coalesced write goes out.
    clock.advance(DEBOUNCE_SECONDS - 4.0 + 0.001)
    agent.pump()
    assert len(shadow.writes) == 2

    # Consecutive writes are spaced by at least the debounce window.
    assert shadow.writes[1][1] - shadow.writes[0][1] >= DEBOUNCE_SECONDS


def test_no_pending_trigger_means_no_write(tmp_path):
    """Pumping without a trigger never writes: writes happen only in
    response to report triggers. _Requirements: 3.1_"""
    clock = _FakeClock()
    agent, shadow, _, _ = _make_agent(tmp_path, clock)

    assert agent.pump() is None
    clock.advance(DEBOUNCE_SECONDS * 10)
    assert agent.pump() is None
    assert shadow.writes == []


# --- 30 s publication bound (Requirement 3.1) -----------------------------------


def test_triggered_report_is_published_within_30_seconds(tmp_path):
    """Worst case for a trigger: it lands immediately after a write, so it
    waits the full debounce window — still well inside the 30 s bound.
    _Requirements: 3.1_"""
    assert DEBOUNCE_SECONDS <= MAX_REPORT_DELAY_SECONDS

    clock = _FakeClock()
    agent, shadow, _, _ = _make_agent(tmp_path, clock)

    agent.report_inventory()
    agent.pump()
    assert len(shadow.writes) == 1

    # Trigger right after the write: maximal debounce delay.
    trigger_time = clock.now
    agent.report_inventory()
    for _ in range(10):
        delay = agent.pump()
        if len(shadow.writes) == 2:
            break
        assert delay is not None
        clock.advance(delay)
    assert len(shadow.writes) == 2
    assert shadow.writes[1][1] - trigger_time <= MAX_REPORT_DELAY_SECONDS


# --- startup full report (Requirement 3.4) ---------------------------------------


def test_start_publishes_immediate_full_report(tmp_path):
    """start() schedules an immediate report of the complete current
    inventory (configured and discovered sources). _Requirements: 3.4_"""
    clock = _FakeClock()
    shadow = _FakeShadowAccessor(clock)
    written = threading.Event()
    shadow.on_write = written.set
    agent, shadow, sources, snapshot = _make_agent(tmp_path, clock, shadow=shadow)

    agent.start()
    try:
        assert written.wait(timeout=10.0), "startup report was never published"
    finally:
        agent.stop()

    assert len(shadow.writes) == 1
    document = shadow.writes[0][0]
    assert document["schemaVersion"] == SCHEMA_VERSION

    # Full report: every inventory entry (configured + discovered) present.
    expected = build_inventory(sources, snapshot)
    assert len(expected) == 2  # harness sanity: one configured, one discovered
    assert set(document["cameras"]) == {
        entry.camera_source_id for entry in expected
    }
    for entry in expected:
        reported = document["cameras"][entry.camera_source_id]
        assert reported["name"] == entry.name
        assert reported["origin"] == entry.origin
        assert reported["params"] == entry.params


# --- Image_Source CRUD hook (Requirement 3.1) -------------------------------------


def test_crud_hook_triggers_report_on_active_agent(tmp_path):
    """notify_image_source_changed with an active agent requests a report,
    which the next pump publishes. _Requirements: 3.1_"""
    clock = _FakeClock()
    agent, shadow, _, _ = _make_agent(tmp_path, clock)

    set_active_agent(agent)
    assert get_active_agent() is agent

    notify_image_source_changed()
    agent.pump()
    assert len(shadow.writes) == 1


def test_crud_hook_without_active_agent_is_noop():
    """Without a registered agent the hook does nothing and never raises,
    so the Image_Source API carries no dependency on the sync feature."""
    clear_active_agent()
    assert get_active_agent() is None
    notify_image_source_changed()  # must not raise


def test_crud_hook_swallows_raising_agent():
    """A broken agent must not fail the Image_Source API call that
    triggered the notification (Requirement 11.2 isolation)."""

    class _RaisingAgent:
        def __init__(self):
            self.calls = 0

        def report_inventory(self):
            self.calls += 1
            raise RuntimeError("agent exploded")

    raising = _RaisingAgent()
    set_active_agent(raising)
    notify_image_source_changed()  # must not raise
    assert raising.calls == 1
