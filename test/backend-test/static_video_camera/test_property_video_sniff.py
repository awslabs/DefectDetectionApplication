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
"""Property test for container sniffing (device half).

**Feature: static-camera-video-loop, Property 11: Sniff parity (device <=> Portal) and totality**
*For any* byte string and the shared signature vectors, the sniffer returns
a container name exactly for the documented signatures and ``None`` for
every JPEG/PNG/BMP encoding. (The Portal suite checks the vendored copy
against the same vectors and asserts the two files are byte-identical.)

**Validates: Requirements 1.3, 8.2**
"""
import io
import json
import os
import random
import struct

from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from utils.video_loop import (
    SNIFF_BYTES,
    SUPPORTED_VIDEO_CONTAINERS,
    sniff_video_container,
)

VECTORS_PATH = os.path.join(os.path.dirname(__file__), "goldens",
                            "video_sniff_vectors.json")

_STILL_IMAGE_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"mif1", b"avif", b"crx "}
_QUICKTIME_ATOMS = {b"moov", b"wide", b"mdat", b"free", b"skip", b"pnot"}


def _oracle(head):
    """Independent restatement of the documented signatures."""
    head = head[:SNIFF_BYTES]

    def plausible(size):
        return size >= 8 or size in (0, 1)

    if len(head) >= 12 and head[4:8] == b"ftyp" and plausible(struct.unpack(">I", head[:4])[0]):
        if head[8:12] in _STILL_IMAGE_BRANDS:
            return None
        return "MOV" if head[8:12] == b"qt  " else "MP4"
    if len(head) >= 8 and head[4:8] in _QUICKTIME_ATOMS and plausible(struct.unpack(">I", head[:4])[0]):
        return "MOV"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "AVI"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "WEBM" if b"webm" in head else "MKV"
    return None


def test_shared_vectors():
    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        vectors = json.load(handle)["vectors"]
    assert len(vectors) >= 20
    for vector in vectors:
        head = bytes.fromhex(vector["head_hex"])
        assert sniff_video_container(head) == vector["expected"], vector["name"]


def test_library_clips_sniff_to_their_container(clip_library):
    for clip in clip_library.clips.values():
        assert sniff_video_container(clip.data[:SNIFF_BYTES]) == clip.container, clip.name


@settings(deadline=None)
@given(
    width=st.integers(min_value=1, max_value=32),
    height=st.integers(min_value=1, max_value=32),
    seed=st.integers(min_value=0, max_value=2 ** 32 - 1),
    img_format=st.sampled_from(["JPEG", "PNG", "BMP"]),
)
def test_supported_images_are_never_videos(width, height, seed, img_format):
    pixels = random.Random(seed).randbytes(width * height * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (width, height), pixels).save(buffer, format=img_format)
    assert sniff_video_container(buffer.getvalue()) is None


_signature_prefixes = st.sampled_from([
    b"", b"\x1a\x45\xdf\xa3", b"RIFF\x00\x10\x00\x00AVI ", b"RIFF\x00\x10\x00\x00WAVE",
    b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00\x18ftypqt  ", b"\x00\x00\x00\x18ftypheic",
    b"\x00\x00\x01\x00moov", b"\x00\x00\x00\x05moov", b"\x00\x00\x00\x00mdat",
])


@settings(deadline=None)
@given(prefix=_signature_prefixes, tail=st.binary(max_size=96))
def test_sniff_is_total_and_matches_the_documented_signatures(prefix, tail):
    head = prefix + tail
    result = sniff_video_container(head)
    assert result is None or result in SUPPORTED_VIDEO_CONTAINERS
    assert result == _oracle(head)
    # Only the head matters: bytes past SNIFF_BYTES never change the result.
    if len(head) >= SNIFF_BYTES:
        assert sniff_video_container(head + b"\x00" * 200) == result
