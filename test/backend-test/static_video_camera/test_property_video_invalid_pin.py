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
"""Property test for invalid video input.

**Feature: static-camera-video-loop, Property 6: Invalid input preserves prior state**
*For any* prior state and invalid input (a non-video file, a sniffable
header followed by garbage, a truncated clip, the AV1 clip, an oversize
file against an injected limit, a frame-rate or frame-size violation), the
pin fails with the specified wording, and the prior status, metadata,
Loop_Epoch, frame at a fixed time, and enumeration presence are unchanged,
with no ``.tmp-*`` staging file left behind.

**Validates: Requirements 1.3, 1.4, 1.5, 1.6, 1.9, 10.4**
"""
import io
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from utils.static_video_camera import StaticVideoPinError, StaticVideoStore
from utils.video_loop import (
    SUPPORTED_VIDEO_CODECS,
    SUPPORTED_VIDEO_CONTAINERS,
    VideoInfo,
)
from static_video_support import (
    ManualClock,
    leftover_staging_files,
    store_dir,
    stored_files,
)

_KINDS = ("not_video_bytes", "image_file", "garbage_after_header", "truncated",
          "av1", "oversize", "fps300", "wide4112", "no_frames_stub")


def _invalid_input(kind, library, data):
    """(payload bytes, expected message fragments, injected limit or None,
    stub probe or None) for one invalid-input kind."""
    playable = sorted(clip.name for clip in library.decodable())
    if kind == "not_video_bytes":
        payload = b"\x00not-a-video\x00" + data.draw(st.binary(max_size=200))
        return payload, ["not a supported video"] + list(SUPPORTED_VIDEO_CONTAINERS), None, None
    if kind == "image_file":
        buffer = io.BytesIO()
        pixels = random.Random(data.draw(st.integers(0, 2 ** 16))).randbytes(8 * 8 * 3)
        Image.frombytes("RGB", (8, 8), pixels).save(buffer, format="PNG")
        return buffer.getvalue(), ["not a supported video"], None, None
    if kind == "garbage_after_header":
        clip = library.get(data.draw(st.sampled_from(playable)))
        garbage = random.Random(data.draw(st.integers(0, 2 ** 16))).randbytes(2048)
        return clip.data[:64] + garbage, ["could not be decoded"] + list(SUPPORTED_VIDEO_CODECS), None, None
    if kind == "truncated":
        # ffmpeg writes the MP4 index (moov) last, so any truncation of these
        # files removes it and the container cannot be opened.
        mp4s = sorted(clip.name for clip in library.decodable()
                      if clip.name.endswith(".mp4"))
        clip = library.get(data.draw(st.sampled_from(mp4s)))
        cut = data.draw(st.floats(min_value=0.2, max_value=0.9))
        return clip.data[:max(64, int(len(clip.data) * cut))], ["could not be decoded"], None, None
    if kind == "av1":
        clip = library.get("av1.mkv")
        if clip is None:
            return None
        return clip.data, ["could not be decoded", "AV1"], None, None
    if kind == "oversize":
        clip = library.get(data.draw(st.sampled_from(playable)))
        limit = data.draw(st.integers(min_value=1, max_value=len(clip.data) - 1))
        return clip.data, ["exceeds the maximum accepted video file size", str(limit)], limit, None
    if kind in ("fps300", "wide4112"):
        entry = library.limit_clips.get(kind + ".avi")
        if entry is None:
            return None
        path, wording = entry
        with open(path, "rb") as handle:
            return handle.read(), [wording], None, None
    # no_frames_stub: a probe that reports what a zero-frame file would —
    # the store must surface the probe's rejection verbatim.
    from utils.video_loop import VideoValidationError, undecodable_message

    def stub(_path):
        raise VideoValidationError(undecodable_message("H264", " (the file reports no frames)"))

    clip = library.get(data.draw(st.sampled_from(playable)))
    return clip.data, ["could not be decoded", "reports no frames"], None, stub


def _snapshot(store, clock, at_s):
    status = store.status()
    frame = None
    if status["pinned"]:
        saved = clock.now_s
        clock.now_s = at_s
        frame = store.get_frame()
        clock.now_s = saved
    return status, frame


def _available_kinds(library):
    """Kinds whose inputs this image can produce (e.g. the JetPack 5 image's
    ffmpeg has no AV1 encoder)."""
    kinds = []
    for kind in _KINDS:
        if kind == "av1" and library.get("av1.mkv") is None:
            continue
        if kind in ("fps300", "wide4112") and kind + ".avi" not in library.limit_clips:
            continue
        kinds.append(kind)
    return kinds


@settings(deadline=None)
@given(data=st.data())
def test_invalid_input_preserves_prior_state(clip_library, data):
    kind = data.draw(st.sampled_from(_available_kinds(clip_library)), label="kind")
    payload, fragments, limit, stub = _invalid_input(kind, clip_library, data)
    prior = data.draw(st.none() | st.sampled_from(
        sorted(clip.name for clip in clip_library.decodable())), label="prior")
    clock = ManualClock()
    with store_dir() as base_dir:
        StaticVideoStore(base_dir=base_dir, clock=clock)  # construction only
        if prior is not None:
            StaticVideoStore(base_dir=base_dir, clock=clock).pin_bytes(
                clip_library.get(prior).data, "prior.mp4")
        kwargs = {"base_dir": base_dir, "clock": clock}
        if limit is not None:
            kwargs["max_file_bytes"] = limit
        if stub is not None:
            kwargs["probe"] = stub
        store = StaticVideoStore(**kwargs)
        fixed_time = clock.now_s + 3.25
        before = _snapshot(store, clock, fixed_time)
        files_before = stored_files(base_dir)

        clock.now_s += 7.0  # a successful pin now would move the epoch
        with pytest.raises(StaticVideoPinError) as exc_info:
            store.pin_bytes(payload, "invalid.mp4")
        message = str(exc_info.value)
        for fragment in fragments:
            assert fragment in message, (kind, message)

        assert _snapshot(store, clock, fixed_time) == before
        fresh = StaticVideoStore(base_dir=base_dir, clock=clock)
        assert _snapshot(fresh, clock, fixed_time) == before
        assert stored_files(base_dir) == files_before
        assert leftover_staging_files(base_dir) == []


def test_probe_rejections_name_the_limit(clip_library):
    """Requirement 1.6 wording through the real probe, per violated limit."""
    if not clip_library.limit_clips:
        pytest.skip("limit clips unavailable")
    for name, (path, wording) in clip_library.limit_clips.items():
        with store_dir() as base_dir:
            store = StaticVideoStore(base_dir=base_dir)
            with open(path, "rb") as handle:
                payload = handle.read()
            with pytest.raises(StaticVideoPinError) as exc_info:
                store.pin_bytes(payload, name)
            message = str(exc_info.value)
            assert wording in message, (name, message)
            assert ("240" in message) or ("4096" in message)


def test_video_info_duration():
    info = VideoInfo("MP4", "H264", 10, 10, 29.97, 899)
    assert info.duration_ms == round(899 * 1000 / 29.97)
