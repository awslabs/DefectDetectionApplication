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
"""Property test for Aravis absence marking on re-enumeration.

**Feature: aravis-camera-input, Property 5: Aravis absence marking on
re-enumeration**

*For any* sequence of Aravis enumeration results, a stable id present in
an earlier result and missing from a later one SHALL be marked absent with
an absence timestamp and SHALL never be dropped from the tracked
inventory.

Exercised end-to-end through the real
:class:`camera_discovery.CameraDiscovery` diff/tracking: a mutable fake
Aravis bus is injected as the enumerator, the V4L2 layer is an empty fake,
and an injectable clock controls the absence timestamps. Each generated
enumeration pass re-randomizes the bus's runtime attributes (Aravis
runtime id, address, protocol) and order, so the tracked identity is the
stable id alone.

**Validates: Requirements 2.5**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from types import SimpleNamespace

from fake_v4l2 import FakeV4l2Io
from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import CameraDiscovery
from camera_discovery.aravis import DiscoveredAravisCamera, aravis_stable_id

# --- generators --------------------------------------------------------------

# Printable ASCII excluding "|" (the stable id derivation's join character),
# matching real GenICam identity strings.
_TEXT_ALPHABET = st.characters(
    min_codepoint=32, max_codepoint=126, exclude_characters="|"
)

_VENDORS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)
_MODELS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)
_SERIALS = st.text(alphabet=_TEXT_ALPHABET, max_size=24)  # may be empty
_PHYSICAL_IDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)

# Runtime attributes the bus does NOT keep stable across enumerations.
_RUNTIME_IDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=32)
_ADDRESSES = st.text(alphabet=_TEXT_ALPHABET, max_size=32)
_PROTOCOLS = st.sampled_from(["GigEVision", "USB3Vision", "Fake"])

def _stable_id_key(identity):
    """Uniqueness key: the stable id an identity tuple derives."""
    vendor, model, serial, physical_id = identity
    return aravis_stable_id(vendor, model, serial, physical_id)


# A small pool of bus-stable identities, unique by the stable id they
# derive (so distinct pool members are distinct tracked entries).
_IDENTITY_POOL = st.lists(
    st.tuples(_VENDORS, _MODELS, _SERIALS, _PHYSICAL_IDS),
    min_size=1,
    max_size=5,
    unique_by=_stable_id_key,
)


@st.composite
def _enumeration_sequences(draw):
    """A sequence of fake Aravis bus passes over a shared identity pool.

    Each pass enumerates a random subset of the pool (so identities
    appear, disappear, and return) in a re-randomized order, with fresh
    runtime id / address / protocol values per pass.
    """
    pool = draw(_IDENTITY_POOL)
    pass_count = draw(st.integers(min_value=2, max_value=6))

    passes = []
    for _ in range(pass_count):
        subset = [
            identity for identity in draw(st.permutations(pool))
            if draw(st.booleans())
        ]
        passes.append(
            [
                SimpleNamespace(
                    id=draw(_RUNTIME_IDS),
                    model=model,
                    address=draw(_ADDRESSES),
                    physical_id=physical_id,
                    protocol=draw(_PROTOCOLS),
                    serial=serial,
                    vendor=vendor,
                )
                for vendor, model, serial, physical_id in subset
            ]
        )
    return passes


# --- fakes -------------------------------------------------------------------


class _MutableBus:
    """Injectable Aravis enumerator returning whatever ``cameras`` holds."""

    def __init__(self):
        self.cameras = []

    def __call__(self):
        return list(self.cameras)


class _FakeClock:
    """Injectable epoch-seconds clock (controls absence timestamps)."""

    def __init__(self, start):
        self.now = float(start)

    def __call__(self):
        return self.now


# --- property ----------------------------------------------------------------


@given(
    passes=_enumeration_sequences(),
    start_seconds=st.integers(min_value=0, max_value=2_000_000_000),
)
def test_aravis_absence_marking_on_re_enumeration(passes, start_seconds):
    """**Feature: aravis-camera-input, Property 5: Aravis absence marking
    on re-enumeration**

    **Validates: Requirements 2.5**
    """
    bus = _MutableBus()
    clock = _FakeClock(start_seconds)
    discovery = CameraDiscovery(
        v4l2_io=FakeV4l2Io({}), aravis_enumerator=bus, clock=clock
    )

    seen_ids = set()
    expected_absent_since = {}  # stable id -> epoch ms of first miss, or None

    for step, cameras in enumerate(passes):
        bus.cameras = cameras
        clock.now = float(start_seconds + step * 300)
        now_ms = int(clock.now * 1000)

        snapshot = discovery.run_once()

        present_ids = {
            aravis_stable_id(c.vendor, c.model, c.serial, c.physical_id)
            for c in cameras
        }
        seen_ids |= present_ids

        # Update the expected absence model: a present id carries no
        # absence timestamp; an id missing this pass gets stamped with the
        # timestamp of the enumeration that first noticed the miss and
        # keeps it while it stays missing.
        for stable_id in present_ids:
            expected_absent_since[stable_id] = None
        for stable_id in seen_ids - present_ids:
            if expected_absent_since[stable_id] is None:
                expected_absent_since[stable_id] = now_ms

        # Never dropped: the tracked inventory is exactly every stable id
        # ever enumerated (2.5 — marked absent rather than deleted).
        assert set(snapshot.cameras) == seen_ids

        for stable_id, entry in snapshot.cameras.items():
            assert isinstance(entry.camera, DiscoveredAravisCamera)
            assert entry.camera.stable_id == stable_id
            if stable_id in present_ids:
                # Enumerated this pass: present, no absence marking (a
                # returning camera loses its marking).
                assert entry.absent is False
                assert entry.absent_since is None
            else:
                # Present in an earlier result, missing from this one:
                # marked absent with the absence timestamp of the pass
                # that first noticed the disappearance, retained across
                # further misses.
                assert entry.absent is True
                assert entry.absent_since == expected_absent_since[stable_id]
