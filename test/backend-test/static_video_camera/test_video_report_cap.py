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
"""The camera report cap follows the account's shadow size quota.

Feature: static-camera-video-loop, task 10 (design Decision 7,
Requirement 10.5).

- ``report_cap_for_shadow_limit``: the limit minus the pin-slot reserve,
  within [1 KB, 10 KB]; an unknown or invalid limit is the 8 KB default.
- ``shadow_manager_size_limit_provider``: ShadowManager's
  ``shadowDocumentSizeLimitBytes`` through IPC GetConfiguration.
- The agent: the cap follows the provider's limit and is re-read every
  ``SHADOW_LIMIT_REFRESH_SECONDS``; an unreadable limit keeps the last
  known one; a size rejection (ShadowManager's IPC error included) halves
  the cap for the retry, down to 1 KB, until the limit changes; other
  errors leave the cap alone.
- Headroom: from the default limit up to ShadowManager's 30 KB maximum, a
  capped report plus both pin slots at their worst case fits the limit.
"""
import contextlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import camera_sync.agent as agent_module
from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    MAX_REPORT_BYTES,
    MAX_REPORT_BYTES_CEILING,
    CameraSyncStateStore,
    EdgeSyncAgent,
    build_report_document,
    report_cap_for_shadow_limit,
    shadow_manager_size_limit_provider,
)
from camera_sync.agent import (
    DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES,
    MIN_REPORT_BYTES,
    PIN_SLOTS_RESERVE_BYTES,
    SHADOW_LIMIT_REFRESH_SECONDS,
    SHADOW_MANAGER_COMPONENT,
    SHADOW_MANAGER_SIZE_LIMIT_KEY,
    _is_size_rejection,
)
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID
from utils.static_video_camera import STATIC_VIDEO_CAMERA_ID
from video_shadow_budget import (
    encoded_size,
    fat_inventory,
    shadow_state,
    worst_case_pin_slots,
)

#: ShadowManager's maximum configurable document size.
SHADOW_MANAGER_MAX_LIMIT_BYTES = 30 * 1024


# --- fakes -----------------------------------------------------------------------


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class IpcStyleError(Exception):
    """The shape of a Greengrass IPC error (``awsiot`` model errors): the
    text lives in ``message`` and ``str()`` is empty."""

    def __init__(self, message):
        super().__init__()
        self.message = message

    def __str__(self):
        return ""


SIZE_REJECTION = IpcStyleError("The payload exceeds the maximum size allowed")


class ScriptedShadow:
    """Raises the scripted errors on successive writes, then records."""

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.writes = []
        self.attempts = 0

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        self.writes.append(state["reported"])


class CountingProvider:
    """A size-limit provider returning scripted values (an exception
    instance is raised)."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _NoSources:
    def list_image_sources(self, request, session):
        return []


class _Discovery:
    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


class _UnpinnedStore:
    def __init__(self, camera_id):
        self.camera_id = camera_id

    def status(self):
        return {"pinned": False, "cameraId": self.camera_id, "metadata": None}


@pytest.fixture(autouse=True)
def unpinned_stores(monkeypatch):
    monkeypatch.setattr(agent_module, "get_store",
                        lambda: _UnpinnedStore(STATIC_IMAGE_CAMERA_ID))
    monkeypatch.setattr(agent_module, "get_video_store",
                        lambda: _UnpinnedStore(STATIC_VIDEO_CAMERA_ID))


@pytest.fixture
def captured_caps(monkeypatch):
    """The ``max_bytes`` of every report the agent builds."""
    caps = []
    real = agent_module.build_report_document

    def _capturing(*args, **kwargs):
        caps.append(kwargs["max_bytes"])
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_module, "build_report_document", _capturing)
    return caps


def _fat_snapshot(count=12):
    formats = [{"pixel_format": "FMT{}".format(i),
                "resolutions": [[3840 + i, 2160 + j] for j in range(8)]}
               for i in range(8)]
    return DiscoveryResult(cameras=[
        DiscoveredCamera(
            stable_id="disc-{:012d}".format(index),
            device_path="/dev/video{}".format(index),
            card_name="Inspection camera {}".format(index),
            bus_info="usb-0000:00:14.0-{}".format(index),
            driver="uvcvideo",
            kind="v4l2",
            formats=formats,
        )
        for index in range(count)
    ])


def make_agent(tmp_path, shadow, clock, provider=None, snapshot=None):
    kwargs = {}
    if provider is not None:
        kwargs["shadow_size_limit_provider"] = provider
    return EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=_NoSources(),
        camera_discovery=_Discovery(snapshot) if snapshot is not None else None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "camera_sync_state.json")),
        thing_name="test-thing",
        clock=clock,
        wall_clock=clock,
        **kwargs,
    )


def flush(agent, clock, max_iterations=20):
    """Pump until idle, advancing the fake clock over debounce/backoff."""
    for _ in range(max_iterations):
        delay = agent.pump()
        if delay is None:
            return
        clock.advance(delay)
    raise AssertionError("agent did not go idle")


# --- the cap for a limit ----------------------------------------------------------


@pytest.mark.parametrize("limit, cap", [
    (8192, 4608),
    (12288, 8704),
    (13824, 10240),
    (16384, 10240),
    (30720, 10240),
    (6144, 2560),
    (4608, 1024),
    (4000, 1024),
    ("16384", 10240),
    (16384.0, 10240),
    (None, 4608),
    ("unlimited", 4608),
    ("16384.0", 4608),
    (0, 4608),
    (-8192, 4608),
    (True, 4608),
    (float("nan"), 4608),
    (float("inf"), 4608),
])
def test_report_cap_for_shadow_limit(limit, cap):
    assert report_cap_for_shadow_limit(limit) == cap


def test_the_default_cap_is_the_8_kb_cap():
    assert DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES == 8192
    assert MAX_REPORT_BYTES == 4608 == report_cap_for_shadow_limit(None)
    assert MAX_REPORT_BYTES_CEILING == 10 * 1024


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=MIN_REPORT_BYTES + PIN_SLOTS_RESERVE_BYTES,
                   max_value=SHADOW_MANAGER_MAX_LIMIT_BYTES),
       st.integers(min_value=0, max_value=SHADOW_MANAGER_MAX_LIMIT_BYTES))
def test_property_cap_leaves_the_reserve_and_is_monotonic(limit, extra):
    cap = report_cap_for_shadow_limit(limit)
    assert MIN_REPORT_BYTES <= cap <= MAX_REPORT_BYTES_CEILING
    assert cap + PIN_SLOTS_RESERVE_BYTES <= limit
    assert report_cap_for_shadow_limit(limit + extra) >= cap


# --- the ShadowManager provider ---------------------------------------------------------


@pytest.mark.parametrize("config, expected", [
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: 16384}, 16384),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: 16384.0}, 16384),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: "30720"}, 30720),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: 8192, "strategy": {"type": "realTime"}}, 8192),
    ({"strategy": {"type": "realTime"}}, None),
    ({}, None),
    (None, None),
    ([], None),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: None}, None),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: True}, None),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: "big"}, None),
    ({SHADOW_MANAGER_SIZE_LIMIT_KEY: {"bytes": 16384}}, None),
])
def test_provider_reads_shadow_manager_size_limit(config, expected):
    asked = []

    def reader(name):
        asked.append(name)
        return config

    assert shadow_manager_size_limit_provider(reader)() == expected
    assert asked == [SHADOW_MANAGER_COMPONENT] == ["aws.greengrass.ShadowManager"]


# --- size rejections ------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    SIZE_REJECTION,
    RuntimeError("The payload exceeds the maximum size allowed"),
    RuntimeError('{"code":413,"message":"payload too large"}'),
    RuntimeError("Error 413"),
    type("RequestEntityTooLargeException", (Exception,), {})("rejected"),
    ValueError("Payload too large for the shadow document size limit"),
])
def test_size_rejections_are_recognized(error):
    assert _is_size_rejection(error)


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"),
    IpcStyleError("Unauthorized to update shadow"),
    RuntimeError("AWS_ERROR_INVALID_STATE"),
    RuntimeError("thing cam-4130 not found"),
    ConnectionError("connection reset"),
    IpcStyleError(None),
])
def test_other_failures_are_not_size_rejections(error):
    assert not _is_size_rejection(error)


def test_the_real_ipc_invalid_arguments_error_is_recognized():
    model = pytest.importorskip("awsiot.greengrasscoreipc.model")
    error = model.InvalidArgumentsError(
        message="The payload exceeds the maximum size allowed")
    assert str(error) == ""  # why ``message`` has to be inspected
    assert _is_size_rejection(error)
    assert not _is_size_rejection(model.InvalidArgumentsError(message="bad key"))


# --- the agent's cap --------------------------------------------------------------------


def test_without_provider_the_agent_caps_at_the_default(tmp_path, captured_caps):
    clock = FakeClock()
    shadow = ScriptedShadow()
    agent = make_agent(tmp_path, shadow, clock, snapshot=_fat_snapshot())
    assert agent.report_cap() == MAX_REPORT_BYTES
    agent.report_inventory()
    flush(agent, clock)
    assert captured_caps == [MAX_REPORT_BYTES]
    assert encoded_size(shadow.writes[-1]) <= MAX_REPORT_BYTES


def test_raised_limit_raises_the_cap_end_to_end(tmp_path, captured_caps):
    """At a 16 KB limit the same fat inventory publishes more capability
    detail than at the default: the cap is 10 KB, not 4.5 KB."""
    snapshot = _fat_snapshot()
    default_clock, raised_clock = FakeClock(), FakeClock()
    default_shadow, raised_shadow = ScriptedShadow(), ScriptedShadow()
    (tmp_path / "default").mkdir()
    (tmp_path / "raised").mkdir()
    default_agent = make_agent(tmp_path / "default", default_shadow,
                               default_clock, snapshot=snapshot)
    raised_agent = make_agent(tmp_path / "raised", raised_shadow, raised_clock,
                              provider=CountingProvider(16384),
                              snapshot=snapshot)
    for agent, clock in ((default_agent, default_clock),
                         (raised_agent, raised_clock)):
        agent.report_inventory()
        flush(agent, clock)

    assert captured_caps == [MAX_REPORT_BYTES, 10240]
    default_size = encoded_size(default_shadow.writes[-1])
    raised_size = encoded_size(raised_shadow.writes[-1])
    assert default_size <= MAX_REPORT_BYTES < raised_size <= 10240
    assert (set(raised_shadow.writes[-1]["cameras"])
            == set(default_shadow.writes[-1]["cameras"]))


def test_limit_is_reread_after_the_refresh_interval(tmp_path):
    clock = FakeClock()
    provider = CountingProvider(12288, 12288, 16384)
    agent = make_agent(tmp_path, ScriptedShadow(), clock, provider=provider)

    assert agent.report_cap() == 8704
    assert provider.calls == 1
    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS - 1)
    assert agent.report_cap() == 8704
    assert provider.calls == 1
    clock.advance(1)
    assert agent.report_cap() == 8704  # re-read, unchanged
    assert provider.calls == 2
    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS)
    assert agent.report_cap() == 10240  # a deployment raised the limit
    assert provider.calls == 3


def test_unreadable_limit_keeps_the_last_known_one(tmp_path):
    clock = FakeClock()
    provider = CountingProvider(16384, RuntimeError("IPC down"))
    agent = make_agent(tmp_path, ScriptedShadow(), clock, provider=provider)
    assert agent.report_cap() == 10240
    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS)
    assert agent.report_cap() == 10240
    assert provider.calls == 2


def test_unset_limit_is_the_default(tmp_path):
    agent = make_agent(tmp_path, ScriptedShadow(), FakeClock(),
                       provider=CountingProvider(None))
    assert agent.report_cap() == MAX_REPORT_BYTES


def test_size_rejection_halves_the_cap_for_the_retry(tmp_path, captured_caps):
    clock = FakeClock()
    shadow = ScriptedShadow(errors=[SIZE_REJECTION] * 5)
    agent = make_agent(tmp_path, shadow, clock,
                       provider=CountingProvider(16384),
                       snapshot=_fat_snapshot())
    agent.report_inventory()
    flush(agent, clock)

    # 10 KB, then halved per rejection down to the 1 KB floor.
    assert captured_caps == [10240, 5120, 2560, 1280, 1024, 1024]
    assert shadow.attempts == 6 and len(shadow.writes) == 1
    # At the floor the report is in its smallest form: every camera kept,
    # capability metadata dropped.
    cameras = shadow.writes[-1]["cameras"]
    assert len(cameras) >= 12
    assert all(entry.get("capabilitiesTruncated") for key, entry in cameras.items()
               if key.startswith("disc-"))
    # The back-off sticks for later reports while the limit is unchanged.
    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS)
    agent.report_inventory()
    flush(agent, clock)
    assert captured_caps[-1] == 1024


def test_other_write_failures_leave_the_cap_alone(tmp_path, captured_caps):
    clock = FakeClock()
    shadow = ScriptedShadow(errors=[TimeoutError("timed out"),
                                    IpcStyleError("Unauthorized")])
    agent = make_agent(tmp_path, shadow, clock,
                       provider=CountingProvider(16384))
    agent.report_inventory()
    flush(agent, clock)
    assert captured_caps == [10240, 10240, 10240]
    assert len(shadow.writes) == 1


def test_a_new_limit_clears_the_back_off(tmp_path, captured_caps):
    clock = FakeClock()
    shadow = ScriptedShadow(errors=[SIZE_REJECTION])
    provider = CountingProvider(16384, 16384, 12288)
    agent = make_agent(tmp_path, shadow, clock, provider=provider)
    agent.report_inventory()
    flush(agent, clock)
    assert captured_caps == [10240, 5120]

    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS)
    assert agent.report_cap() == 5120  # same limit: the back-off holds
    clock.advance(SHADOW_LIMIT_REFRESH_SECONDS)
    assert agent.report_cap() == 8704  # a new limit: derived afresh


def test_default_limit_rejection_backs_off_below_the_default_cap(tmp_path,
                                                                 captured_caps):
    """Without a provider (the limit unreadable), a rejection still backs
    the cap off from the 4.5 KB default."""
    clock = FakeClock()
    shadow = ScriptedShadow(errors=[SIZE_REJECTION])
    agent = make_agent(tmp_path, shadow, clock, snapshot=_fat_snapshot())
    agent.report_inventory()
    flush(agent, clock)
    assert captured_caps == [MAX_REPORT_BYTES, MAX_REPORT_BYTES // 2]


# --- headroom at raised limits ------------------------------------------------------------


@pytest.mark.parametrize("limit", [8192, 10240, 12288, 13824, 16384,
                                   SHADOW_MANAGER_MAX_LIMIT_BYTES])
def test_capped_report_plus_both_pin_slots_fit_each_limit(tmp_path, limit):
    cap = report_cap_for_shadow_limit(limit)
    inventory = fat_inventory()
    report = build_report_document(
        inventory, {e.camera_source_id: 7 for e in inventory},
        reported_at_ms=1_790_000_000_000, max_bytes=cap)
    assert encoded_size(report) <= cap

    for desired_sections, reported_sections in worst_case_pin_slots(tmp_path):
        state = shadow_state(report, desired_sections, reported_sections)
        assert encoded_size(state) <= limit
        slots = encoded_size(state) - encoded_size(report)
        # The measured worst case fits the reserve, so a report exactly
        # at the cap still fits the limit.
        assert slots <= PIN_SLOTS_RESERVE_BYTES
        assert cap + slots <= limit
