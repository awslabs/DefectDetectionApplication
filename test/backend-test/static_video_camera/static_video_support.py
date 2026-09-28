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
"""Shared helpers for the static-video-camera store suites."""
import contextlib
import os
import shutil
import tempfile

from hypothesis import strategies as st

from utils.video_loop import loop_frame_index

#: A plausible wall-clock origin (seconds) for injected clocks.
BASE_TIME_S = 1_790_000_000.0


class ManualClock:
    """Injectable wall clock (seconds) the test moves explicitly."""

    def __init__(self, now_s=BASE_TIME_S):
        self.now_s = float(now_s)

    def __call__(self):
        return self.now_s


@contextlib.contextmanager
def store_dir():
    """A fresh temporary store directory, removed afterwards."""
    base_dir = tempfile.mkdtemp(prefix="static-video-camera-test-")
    try:
        yield base_dir
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


def expected_frame(clip, epoch_ms, now_s):
    """Oracle: the clip's reference frame at the Loop_Position for ``now_s``."""
    index = loop_frame_index(now_s * 1000.0, epoch_ms, clip.fps, clip.frame_count)
    return clip.frames[index]


def expected_metadata(clip, file_name, epoch_ms):
    return {
        "fileName": file_name,
        "format": clip.container,
        "codec": clip.codec,
        "width": clip.width,
        "height": clip.height,
        "fps": clip.fps,
        "frameCount": clip.frame_count,
        "durationMs": int(round(clip.frame_count * 1000.0 / clip.fps)),
        "fileSizeBytes": len(clip.data),
        "pinnedAtEpochMs": epoch_ms,
    }


def leftover_staging_files(base_dir):
    return [name for name in os.listdir(base_dir) if name.startswith(".tmp-")]


def stored_files(base_dir):
    return sorted(name for name in os.listdir(base_dir) if not name.startswith(".tmp-"))


#: Offsets (seconds) from the pin time at which grabs are taken: inside
#: the first loop, many loops later, and a long time later.
grab_offsets = st.one_of(
    st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    st.floats(min_value=5.0, max_value=3600.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=86400.0 * 30, allow_nan=False),
)

file_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=24,
).map(lambda stem: stem + ".mp4")
