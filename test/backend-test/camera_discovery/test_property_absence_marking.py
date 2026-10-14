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
"""Property test for the Camera_Discovery re-enumeration diff.

**Feature: camera-registry-sync, Property 4: Absence marking on re-enumeration**

*For any* pair of consecutive discovery snapshots, every stable id present
in the first and missing from the second appears in the diff output marked
absent with an absence timestamp, and no id is ever removed from the
tracked set by a diff.

Exercised over whole *sequences* of :class:`DiscoveryResult` snapshots
folded through the pure :func:`camera_discovery.discovery.diff_snapshot`,
which also pins down the adjacent contract details: an already-absent
entry keeps its original ``absent_since`` across further diffs, a
returning camera loses its absence marking, and the ``changed`` flag is
True exactly when the tracked inventory differs from the previous one.

**Validates: Requirements 2.4**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from hypothesis import given
from hypothesis import strategies as st

from camera_discovery.discovery import (
    DiscoveredCamera,
    DiscoveryResult,
    diff_snapshot,
    make_stable_id,
)

# --- generators --------------------------------------------------------------

# Printable ASCII without NUL, matching what the real enumeration layer
# decodes out of the V4L2 ctypes buffers.
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=126)

_CARDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)
_BUS_INFOS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)

# A small pool of stable camera identities; snapshots draw subsets of the
# pool so the same identity can disappear, persist, and return across the
# sequence. Identities are unique by the bus_info+card concatenation the
# stable id is derived from, so distinct identities get distinct ids.
_IDENTITY_POOL = st.lists(
    st.tuples(_BUS_INFOS, _CARDS),
    min_size=1,
    max_size=5,
    unique_by=lambda identity: identity[0] + identity[1],
)

_FORMATS = st.lists(
    st.fixed_dictionaries(
        {
            "pixel_format": st.sampled_from(["YUYV", "MJPG", "NV12"]),
            "resolutions": st.lists(
                st.tuples(
                    st.integers(min_value=1, max_value=8192),
                    st.integers(min_value=1, max_value=8192),
                ).map(list),
                max_size=2,
            ),
        }
    ),
    max_size=2,
)


@st.composite
def _snapshot_sequences(draw):
    """A sequence of :class:`DiscoveryResult` snapshots over a shared
    identity pool.

    Each snapshot enumerates a subset of the pool; device paths and
    capability metadata may vary between snapshots for the same identity
    (V4L2 does not guarantee ``/dev/videoN`` numbering), so in-place
    updates are generated alongside appearances and disappearances.
    """
    pool = draw(_IDENTITY_POOL)
    snapshot_count = draw(st.integers(min_value=1, max_value=6))

    snapshots = []
    for _ in range(snapshot_count):
        cameras = []
        for index, (bus_info, card_name) in enumerate(pool):
            if not draw(st.booleans()):
                continue  # identity missing from this enumeration pass
            cameras.append(
                DiscoveredCamera(
                    stable_id=make_stable_id(bus_info, card_name),
                    device_path="/dev/video{}".format(
                        draw(st.integers(min_value=0, max_value=9))
                    ),
                    card_name=card_name,
                    bus_info=bus_info,
                    driver=draw(st.sampled_from(["uvcvideo", "tegra-video"])),
                    kind=draw(st.sampled_from(["v4l2", "csi"])),
                    formats=draw(_FORMATS),
                )
            )
        snapshots.append(DiscoveryResult(cameras=cameras))
    return snapshots


# --- property ----------------------------------------------------------------


@given(snapshots=_snapshot_sequences(), start_ms=st.integers(min_value=0))
def test_absence_marking_on_re_enumeration(snapshots, start_ms):
    """**Feature: camera-registry-sync, Property 4: Absence marking on
    re-enumeration**

    **Validates: Requirements 2.4**
    """
    previous = {}
    for step, result in enumerate(snapshots):
        now_ms = start_ms + step * 300_000  # strictly increasing clock
        tracked, changed = diff_snapshot(previous, result, now_ms)

        present_ids = {camera.stable_id for camera in result.cameras}

        # No id is ever removed from the tracked set by a diff (2.4).
        assert set(previous) <= set(tracked)
        # The tracked set is exactly everything ever seen: this pass's
        # cameras plus every previously tracked id.
        assert set(tracked) == present_ids | set(previous)

        for stable_id, entry in tracked.items():
            if stable_id in present_ids:
                # Enumerated cameras are tracked as present; a returning
                # camera loses its absence marking.
                assert entry.absent is False
                assert entry.absent_since is None
            elif previous[stable_id].absent:
                # Already-absent entries keep their original absence
                # timestamp, so repeated identical enumerations produce
                # identical inventories.
                assert entry == previous[stable_id]
            else:
                # Present before, missing now: marked absent with the
                # timestamp of the enumeration that noticed it (2.4),
                # metadata retained rather than deleted.
                assert entry.absent is True
                assert entry.absent_since == now_ms
                assert entry.camera == previous[stable_id].camera

        # changed is True exactly when the tracked inventory differs.
        assert changed == (tracked != previous)

        # Re-diffing the same enumeration at a later time is a no-op: no
        # spurious change and no absence timestamp rewrite.
        replay, replay_changed = diff_snapshot(tracked, result, now_ms + 1)
        assert replay_changed is False
        assert replay == tracked

        previous = tracked
