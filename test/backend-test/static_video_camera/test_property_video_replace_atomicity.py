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
"""Property test for replace atomicity and decoder release.

**Feature: static-camera-video-loop, Property 7: Replace atomicity and decoder release**
*For any* sequence of clip pins and unpins, every grab after a confirmation
returns the new video's frame at its new epoch (or fails after an unpin);
at most one media file remains; a stale player is closed by the next grab.

**Validates: Requirements 5.1, 5.2, 5.3, 5.5, 10.3, 10.4**

A second store instance plays the "other process": it keeps grabbing
across every change made through the first instance, so a decoder left
open on a replaced or deleted file would serve stale frames or keep
succeeding after an unpin.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_video_camera import (
    StaticVideoPinError,
    StaticVideoStore,
    StaticVideoUnavailableError,
)
from static_video_support import (
    ManualClock,
    expected_frame,
    grab_offsets,
    leftover_staging_files,
    store_dir,
    stored_files,
)


@settings(deadline=None)
@given(data=st.data())
def test_replace_and_unpin_are_atomic_and_release_decoders(clip_library, data):
    names = sorted(clip.name for clip in clip_library.all_playable())
    operations = data.draw(
        st.lists(st.one_of(st.sampled_from(names), st.just("<unpin>")),
                 min_size=1, max_size=6),
        label="operations",
    )
    clock = ManualClock()
    with store_dir() as base_dir:
        writer = StaticVideoStore(base_dir=base_dir, clock=clock)
        other_process = StaticVideoStore(base_dir=base_dir, clock=clock)
        pinned = None  # (clip, epoch_ms) currently pinned
        for operation in operations:
            clock.now_s += 2.5
            if operation == "<unpin>":
                if pinned is None:
                    with pytest.raises(StaticVideoPinError) as exc_info:
                        writer.unpin()
                    assert "no video is pinned" in str(exc_info.value)
                else:
                    writer.unpin()
                    pinned = None
            else:
                clip = clip_library.get(operation)
                metadata = writer.pin_bytes(clip.data, operation)
                pinned = (clip, metadata["pinnedAtEpochMs"])

            offset = data.draw(grab_offsets, label="offset")
            if pinned is None:
                for store in (writer, other_process):
                    with pytest.raises(StaticVideoUnavailableError) as exc_info:
                        store.get_frame()
                    assert "no usable pinned video" in str(exc_info.value)
                assert stored_files(base_dir) == []
            else:
                clip, epoch_ms = pinned
                clock.now_s = epoch_ms / 1000.0 + offset
                want = expected_frame(clip, epoch_ms, clock.now_s)
                assert writer.get_frame()["data"] == want
                assert other_process.get_frame()["data"] == want
                assert stored_files(base_dir) == ["pinned_video", "pinned_video.json"]
            assert leftover_staging_files(base_dir) == []
