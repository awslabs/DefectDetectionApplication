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
"""Property test: the HTTP/prefix-gate extension preserves the
pre-existing ``dda_frames`` behavior.

The pre-feature oracle is implemented by construction: the pre-change
``load_image`` read the source's raw bytes (local ``open``/``read``,
S3 ``get_object``) and decoded them with ``cv2.imdecode``
(IMREAD_UNCHANGED for 2-D uint8 grayscale, IMREAD_COLOR otherwise),
raising ``ValueError`` messages naming the source with the exact forms
asserted below. ``to_array``/``to_bytes`` round-tripped contiguous
uint8 arrays, and ``frame_info`` reflected the runner-set caps.

- **Feature: custom-python-source, Property 23: Pre-existing
  Frame_Helpers behavior is preserved** — Validates: Requirements 4.2,
  11.2
"""
import io
import os
import tempfile
import types

import cv2
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from workflow_engine.python_bridge import HELPERS_SOURCE

FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}

#: Filesystem/S3-safe name alphabet (no separators, no null bytes).
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def load_helpers():
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


def encode_png(array):
    ok, encoded = cv2.imencode(".png", array)
    assert ok, "cv2.imencode failed for a plain uint8 array"
    return encoded.tobytes()


def oracle_decode(data):
    """The pre-change decode path, constructed from its specification:
    IMREAD_UNCHANGED when that yields a 2-D uint8 array (grayscale),
    IMREAD_COLOR (8-bit BGR) otherwise."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
    if image is not None and image.ndim == 2 and image.dtype == np.uint8:
        return image
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    assert image is not None, "oracle expected decodable content"
    return image


class FakeS3Client:
    """get_object stub serving fixed bytes and recording requests."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": io.BytesIO(self.payload)}


class RaisingS3Client:
    """get_object stub that always fails, like a denied/absent object."""

    def get_object(self, Bucket, Key):
        raise RuntimeError(
            "simulated S3 failure for {0}/{1}".format(Bucket, Key)
        )


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 23: Pre-existing Frame_Helpers
# behavior is preserved
#
# For any uint8 array and frame dims, to_array/to_bytes round-trip exactly
# as today; for any image written to a local path or fetched through an
# injected S3 client, load_image returns the same decoded array (and raises
# the same source-naming errors on failure) as the pre-change
# implementation; and frame_info reflects the current invocation's caps
# unchanged.
#
# **Validates: Requirements 4.2, 11.2**
# ---------------------------------------------------------------------------


@given(
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    bucket=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    key=st.text(alphabet=_NAME_ALPHABET + "/.", min_size=1, max_size=30),
    name=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    junk=st.binary(min_size=0, max_size=64),
    data=st.data(),
)
def test_property_23_pre_existing_helper_behavior_is_preserved(
    fmt, bucket, key, name, junk, data
):
    """**Feature: custom-python-source, Property 23: Pre-existing
    Frame_Helpers behavior is preserved**

    **Validates: Requirements 4.2, 11.2**
    """
    helpers = load_helpers()
    height = data.draw(st.integers(min_value=1, max_value=32))
    width = data.draw(st.integers(min_value=1, max_value=32))

    # to_array / to_bytes round-trip exactly as today (Req 11.2).
    channels = FORMAT_CHANNELS[fmt]
    size = width * height * channels
    pixels = data.draw(st.binary(min_size=size, max_size=size))
    array = helpers.to_array(pixels, width, height, fmt)
    expected_shape = (
        (height, width) if fmt == "GRAY8" else (height, width, channels)
    )
    assert array.shape == expected_shape
    assert array.dtype == np.uint8
    assert helpers.to_bytes(array) == pixels

    # frame_info reflects the runner-set caps, and clears (Req 11.2).
    info = {"width": width, "height": height, "format": fmt}
    helpers._set_current(info)
    assert helpers.frame_info() == info
    helpers._set_current(None)
    assert helpers.frame_info() is None

    # Local-path load: same decoded array as the pre-change
    # implementation (bytes -> oracle decode), color and gray (Req 4.2).
    grayscale = data.draw(st.booleans())
    shape = (height, width) if grayscale else (height, width, 3)
    image = data.draw(hnp.arrays(np.uint8, shape))
    png = encode_png(image)
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, name + ".png")
        with open(path, "wb") as f:
            f.write(png)
        loaded = helpers.load_image(path)
        assert np.array_equal(loaded, oracle_decode(png))
        assert np.array_equal(loaded, image)

        # Same source-naming errors as the pre-change implementation.
        missing_path = os.path.join(tmp_dir, name + "-missing.png")
        with pytest.raises(ValueError) as missing_error:
            helpers.load_image(missing_path)
        assert missing_path in str(missing_error.value)
        assert "could not read" in str(missing_error.value)

        undecodable_path = os.path.join(tmp_dir, name + ".bin")
        with open(undecodable_path, "wb") as f:
            f.write(b"not-an-image:" + junk)
        with pytest.raises(ValueError) as decode_error:
            helpers.load_image(undecodable_path)
        assert undecodable_path in str(decode_error.value)
        assert "could not be decoded" in str(decode_error.value)

    # Injected-S3-client load: exactly one get_object for the URI's
    # bucket/key and the same decoded array (Req 4.2).
    fake = FakeS3Client(png)
    s3_source = "s3://{0}/{1}".format(bucket, key)
    loaded = helpers.load_image(s3_source, s3_client=fake)
    assert fake.calls == [(bucket, key)]
    assert np.array_equal(loaded, oracle_decode(png))

    # S3 failure modes keep their pre-change source-naming messages.
    with pytest.raises(ValueError) as fetch_error:
        helpers.load_image(s3_source, s3_client=RaisingS3Client())
    assert s3_source in str(fetch_error.value)
    assert "could not fetch" in str(fetch_error.value)

    malformed_uri = "s3://" + bucket  # bucket, no key
    with pytest.raises(ValueError) as uri_error:
        helpers.load_image(malformed_uri, s3_client=fake)
    assert malformed_uri in str(uri_error.value)
    assert "malformed S3 URI" in str(uri_error.value)
