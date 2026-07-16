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
"""Property test for the Edge_Sync_Agent reconnect catch-up publication.

**Feature: camera-registry-sync, Property 6: Reconnect publishes complete
current state**

*For any* sequence of local inventory changes interleaved with failing
shadow writes, the first successful shadow write after the failures carries
the complete current inventory (equal to ``build_inventory`` over the state
at publish time), so no retained change is lost.

**Validates: Requirements 3.3**

The agent is driven deterministically with a fake clock through
:meth:`EdgeSyncAgent.pump` (one scheduling step per call — no threads), a
fake shadow accessor whose writes can be toggled to fail (simulating a
device with no AWS IoT connectivity), fake Image_Source data behind a fake
accessor, a fake Camera_Discovery snapshot holder, and a tmp state-store
path. The fake shadow captures, at the instant of every *successful* write,
what ``build_inventory`` produces over the then-current device state — the
oracle each published document is compared against.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import contextlib
import os
import tempfile

from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    SCHEMA_VERSION,
    CameraSyncStateStore,
    EdgeSyncAgent,
    build_inventory,
)

# --- generators --------------------------------------------------------------

# Small shared path pool so configured and discovered paths collide often.
_DEVICE_PATHS = st.integers(min_value=0, max_value=7).map(
    lambda n: "/dev/video{}".format(n)
)

_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=15,
)

# Small capability payloads: this property is about completeness across
# reconnects, not the size-truncation ladder, so documents stay far below
# the truncation threshold.
_FORMATS = st.lists(
    st.fixed_dictionaries(
        {
            "pixel_format": st.sampled_from(["YUYV", "MJPG", "NV12"]),
            "resolutions": st.lists(
                st.sampled_from([[1920, 1080], [1280, 720], [640, 480]]),
                max_size=2,
            ),
        }
    ),
    max_size=2,
)


@st.composite
def _image_source(draw):
    """One configured Image_Source; ids come from a small pool so upserts
    overwrite and removals hit existing sources."""
    configuration = {}
    if draw(st.booleans()):
        configuration["device"] = draw(_DEVICE_PATHS)
    if draw(st.booleans()):
        configuration["gain"] = draw(st.integers(min_value=0, max_value=48))
    return {
        "imageSourceId": "is-{}".format(draw(st.integers(min_value=0, max_value=5))),
        "name": draw(_TEXT),
        "type": draw(st.sampled_from(["Camera", "ICam", "NvidiaCSI"])),
        "cameraId": draw(_TEXT),
        "imageSourceConfiguration": configuration,
    }


@st.composite
def _discovered_cameras(draw):
    """A full discovery snapshot: unique stable ids, unique device paths,
    as the enumeration layer guarantees."""
    paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=4))
    return [
        DiscoveredCamera(
            stable_id="disc-{:012d}".format(index),
            device_path=path,
            card_name=draw(_TEXT),
            bus_info=draw(_TEXT),
            driver=draw(st.sampled_from(["uvcvideo", "tegra-video"])),
            kind=draw(st.sampled_from(["v4l2", "csi"])),
            formats=draw(_FORMATS),
        )
        for index, path in enumerate(paths)
    ]


# One local inventory change: create/update an Image_Source, delete one, or
# swap the discovery snapshot (hardware appearing/disappearing).
_MUTATIONS = st.one_of(
    st.tuples(st.just("upsert_source"), _image_source()),
    st.tuples(st.just("remove_source"), st.integers(min_value=0, max_value=5)),
    st.tuples(st.just("set_discovery"), _discovered_cameras()),
)

# An interleaving: each step applies a change and says whether the shadow
# accepts writes while the agent tries to publish it.
_STEPS = st.lists(
    st.tuples(_MUTATIONS, st.booleans()), min_size=1, max_size=8
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
    """IoTShadowAccessor stand-in with toggleable write failures.

    On every successful write it snapshots the oracle inventory (what
    ``build_inventory`` yields over the current device state) next to the
    published document, so the test can assert completeness *at publish
    time*.
    """

    def __init__(self, oracle):
        self.failing = False
        self.events = []  # ("fail",) | ("ok", document, expected_inventory)
        self._oracle = oracle

    @property
    def successful_writes(self):
        return [event for event in self.events if event[0] == "ok"]

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if self.failing:
            self.events.append(("fail",))
            raise ConnectionError("shadow offline")
        self.events.append(("ok", state["reported"], self._oracle()))


class _FakeImageSourceAccessor:
    def __init__(self, state):
        self._state = state

    def list_image_sources(self, request, session):
        return [dict(source) for source in self._state["sources"].values()]


class _FakeDiscovery:
    def __init__(self, state):
        self._state = state

    @property
    def latest_snapshot(self):
        return self._state["snapshot"]


# --- helpers -----------------------------------------------------------------


def _apply_mutation(state, mutation) -> None:
    kind, payload = mutation
    if kind == "upsert_source":
        state["sources"][payload["imageSourceId"]] = payload
    elif kind == "remove_source":
        if state["sources"]:
            keys = sorted(state["sources"])
            del state["sources"][keys[payload % len(keys)]]
    else:  # set_discovery
        state["snapshot"] = DiscoveryResult(cameras=payload)


def _drive(agent, clock, max_iterations: int) -> bool:
    """Pump the agent, advancing the fake clock past every debounce/backoff
    wait, until it goes idle or the iteration budget runs out. Returns True
    when the agent went idle (everything pending was published)."""
    for _ in range(max_iterations):
        delay = agent.pump()
        if delay is None:
            return True
        clock.advance(delay + 0.001)
    return False


def _assert_complete_current_inventory(document, expected) -> None:
    """The published document is the complete inventory as of publish time:
    exactly the oracle's entries, each with its full reported content."""
    assert document["schemaVersion"] == SCHEMA_VERSION
    cameras = document["cameras"]
    assert set(cameras) == {entry.camera_source_id for entry in expected}
    for entry in expected:
        reported = cameras[entry.camera_source_id]
        assert reported["name"] == entry.name
        assert reported["type"] == entry.type
        assert reported["origin"] == entry.origin
        assert reported["params"] == entry.params
        assert reported["capabilities"] == entry.capabilities
        assert reported["discovered"] == entry.discovered
        assert reported["absent"] == entry.absent
        assert isinstance(reported["version"], int) and reported["version"] >= 1


# --- property ----------------------------------------------------------------


@given(steps=_STEPS)
def test_reconnect_publishes_complete_current_state(steps):
    """**Feature: camera-registry-sync, Property 6: Reconnect publishes
    complete current state**

    **Validates: Requirements 3.3**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        state = {"sources": {}, "snapshot": DiscoveryResult()}
        clock = _FakeClock()
        shadow = _FakeShadowAccessor(
            oracle=lambda: build_inventory(
                list(state["sources"].values()), state["snapshot"]
            )
        )
        agent = EdgeSyncAgent(
            iot_shadow_accessor=shadow,
            image_source_accessor=_FakeImageSourceAccessor(state),
            camera_discovery=_FakeDiscovery(state),
            db_session_factory=lambda: contextlib.nullcontext(),
            state_store=CameraSyncStateStore(
                os.path.join(tmp_dir, "camera_sync_state.json")
            ),
            thing_name="test-thing",
            clock=clock,
            wall_clock=clock,
        )

        for mutation, online in steps:
            _apply_mutation(state, mutation)
            shadow.failing = not online
            agent.report_inventory()
            if online:
                # Connected: the pending report must flush completely.
                assert _drive(agent, clock, max_iterations=50)
            else:
                # Offline: let the agent burn a few failed attempts; the
                # change stays retained (dirty) for the eventual catch-up.
                _drive(agent, clock, max_iterations=3)

        # Connectivity restored: the retry loop's next success is the
        # catch-up publication (Requirement 3.3).
        shadow.failing = False
        assert _drive(agent, clock, max_iterations=50)

        # A report was published, and the run ended on a success — every
        # failure streak was followed by a successful catch-up write.
        assert shadow.successful_writes
        assert shadow.events[-1][0] == "ok"

        # Every successful write — in particular the first one after each
        # failure streak — carries the complete current inventory as of the
        # moment it was published, so no change made while offline is lost.
        for _, document, expected in shadow.successful_writes:
            _assert_complete_current_inventory(document, expected)
