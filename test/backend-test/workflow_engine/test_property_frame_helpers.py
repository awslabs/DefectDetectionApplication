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
"""Property-based tests for the ``dda_frames`` Frame_Helpers module.

Each test exec's ``HELPERS_SOURCE`` (the module source the Python_Runner
injects into handler subprocesses as ``dda_frames``) into a fresh module
namespace, exactly as the runner does, and exercises the helper API
directly.

Covers:

- **Feature: custom-python-frames, Property 7: Frame/array conversion
  round trip** — Validates: Requirements 5.2, 5.3, 5.4, 5.5
- **Feature: custom-python-frames, Property 8: Disk image load round
  trip** — Validates: Requirements 6.1, 6.3
- **Feature: custom-python-frames, Property 9: S3 image load round
  trip** — Validates: Requirements 6.2
- **Feature: custom-python-frames, Property 10: load_image failures
  identify the source** — Validates: Requirements 6.4
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
    """Exec HELPERS_SOURCE into a fresh module namespace, as the runner
    does when it registers ``dda_frames`` in ``sys.modules``."""
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


def encode_png(array):
    ok, encoded = cv2.imencode(".png", array)
    assert ok, "cv2.imencode failed for a plain uint8 array"
    return encoded.tobytes()


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 7: Frame/array conversion round trip
#
# For any frame dimensions, supported Pixel_Format, and pixel content,
# to_bytes(to_array(frame_bytes, width, height, format)) equals the original
# unpadded frame bytes, and to_array of padded frame bytes returns the same
# array as to_array of the unpadded bytes; for any unsupported format string
# or byte string shorter than the dimensions require, to_array raises an
# error describing the format or size problem.
#
# **Validates: Requirements 5.2, 5.3, 5.4, 5.5**
# ---------------------------------------------------------------------------


@given(
    width=st.integers(min_value=1, max_value=32),
    height=st.integers(min_value=1, max_value=32),
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    padding=st.integers(min_value=0, max_value=16),
    bad_fmt=st.text(min_size=1, max_size=12).filter(
        lambda s: s not in FORMAT_CHANNELS
    ),
    data=st.data(),
)
def test_property_7_frame_array_conversion_round_trip(
    width, height, fmt, padding, bad_fmt, data
):
    """**Feature: custom-python-frames, Property 7: Frame/array
    conversion round trip**

    **Validates: Requirements 5.2, 5.3, 5.4, 5.5**
    """
    helpers = load_helpers()
    channels = FORMAT_CHANNELS[fmt]
    row_bytes = width * channels
    size = row_bytes * height
    pixels = data.draw(st.binary(min_size=size, max_size=size))

    # Round trip: to_bytes(to_array(...)) == unpadded input (Req 5.2-5.4).
    array = helpers.to_array(pixels, width, height, fmt)
    expected_shape = (
        (height, width) if fmt == "GRAY8" else (height, width, channels)
    )
    assert array.shape == expected_shape
    assert array.dtype == np.uint8
    assert helpers.to_bytes(array) == pixels

    # Row padding tolerated: padded and unpadded bytes decode equal (5.2).
    pad = data.draw(
        st.binary(min_size=padding * height, max_size=padding * height)
    )
    padded = b"".join(
        pixels[i * row_bytes:(i + 1) * row_bytes]
        + pad[i * padding:(i + 1) * padding]
        for i in range(height)
    )
    assert np.array_equal(
        helpers.to_array(padded, width, height, fmt), array
    )

    # Unsupported format: ValueError naming the format (Req 5.5).
    with pytest.raises(ValueError) as bad_format_error:
        helpers.to_array(pixels, width, height, bad_fmt)
    assert bad_fmt in str(bad_format_error.value)

    # Short buffer: ValueError describing the size shortfall (Req 5.5).
    with pytest.raises(ValueError) as short_error:
        helpers.to_array(pixels[: size - 1], width, height, fmt)
    assert "too short" in str(short_error.value)


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 8: Disk image load round trip
#
# For any uint8 BGR image array written losslessly to a PNG file with
# cv2.imwrite, dda_frames.load_image of that path returns an array equal to
# the original.
#
# **Validates: Requirements 6.1, 6.3**
# ---------------------------------------------------------------------------


@given(data=st.data())
def test_property_8_disk_image_load_round_trip(data):
    """**Feature: custom-python-frames, Property 8: Disk image load
    round trip**

    **Validates: Requirements 6.1, 6.3**
    """
    height = data.draw(st.integers(min_value=1, max_value=32))
    width = data.draw(st.integers(min_value=1, max_value=32))
    array = data.draw(hnp.arrays(np.uint8, (height, width, 3)))
    helpers = load_helpers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "image.png")
        assert cv2.imwrite(path, array)
        loaded = helpers.load_image(path)
        assert loaded.dtype == np.uint8
        assert np.array_equal(loaded, array)


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 9: S3 image load round trip
#
# For any bucket name, object key, and uint8 BGR image array PNG-encoded and
# served by an injected fake S3 client,
# dda_frames.load_image("s3://bucket/key", s3_client=fake) requests exactly
# that bucket and key and returns an array equal to the original.
#
# **Validates: Requirements 6.2**
# ---------------------------------------------------------------------------


class FakeS3Client:
    """get_object stub serving fixed bytes and recording requests."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": io.BytesIO(self.payload)}


@given(
    bucket=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=30),
    key=st.text(alphabet=_NAME_ALPHABET + "/.", min_size=1, max_size=40),
    data=st.data(),
)
def test_property_9_s3_image_load_round_trip(bucket, key, data):
    """**Feature: custom-python-frames, Property 9: S3 image load round
    trip**

    **Validates: Requirements 6.2**
    """
    height = data.draw(st.integers(min_value=1, max_value=16))
    width = data.draw(st.integers(min_value=1, max_value=16))
    array = data.draw(hnp.arrays(np.uint8, (height, width, 3)))
    helpers = load_helpers()
    fake = FakeS3Client(encode_png(array))

    loaded = helpers.load_image(
        "s3://{0}/{1}".format(bucket, key), s3_client=fake
    )

    assert fake.calls == [(bucket, key)]
    assert np.array_equal(loaded, array)


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 10: load_image failures identify
# the source
#
# For any failing source — a non-existent local path, a malformed s3:// URI,
# an S3 client raising on fetch, or existing content that does not decode as
# an image — dda_frames.load_image raises an error whose message contains
# the source.
#
# **Validates: Requirements 6.4**
# ---------------------------------------------------------------------------


class RaisingS3Client:
    """get_object stub that always fails, like a denied/absent object."""

    def get_object(self, Bucket, Key):
        raise RuntimeError("simulated S3 failure for {0}/{1}".format(
            Bucket, Key
        ))


@given(
    name=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    bucket=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    key=st.text(alphabet=_NAME_ALPHABET + "/.", min_size=1, max_size=30),
    malformed_uri=st.one_of(
        st.just("s3://"),
        st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20).map(
            lambda b: "s3://" + b  # bucket, no key
        ),
        st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20).map(
            lambda b: "s3://" + b + "/"  # bucket, empty key
        ),
        st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20).map(
            lambda k: "s3:///" + k  # empty bucket
        ),
    ),
    junk=st.binary(min_size=0, max_size=64),
)
def test_property_10_load_image_failures_identify_the_source(
    name, bucket, key, malformed_uri, junk
):
    """**Feature: custom-python-frames, Property 10: load_image failures
    identify the source**

    **Validates: Requirements 6.4**
    """
    helpers = load_helpers()

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Non-existent local path.
        missing_path = os.path.join(tmp_dir, name + ".png")
        with pytest.raises(ValueError) as missing_error:
            helpers.load_image(missing_path)
        assert missing_path in str(missing_error.value)

        # Existing content that does not decode as an image (the prefix
        # guarantees no valid image magic bytes).
        undecodable_path = os.path.join(tmp_dir, name + ".bin")
        with open(undecodable_path, "wb") as f:
            f.write(b"not-an-image:" + junk)
        with pytest.raises(ValueError) as decode_error:
            helpers.load_image(undecodable_path)
        assert undecodable_path in str(decode_error.value)

    # Malformed s3:// URI.
    with pytest.raises(ValueError) as uri_error:
        helpers.load_image(malformed_uri, s3_client=FakeS3Client(b""))
    assert malformed_uri in str(uri_error.value)

    # S3 client raising on fetch.
    s3_source = "s3://{0}/{1}".format(bucket, key)
    with pytest.raises(ValueError) as fetch_error:
        helpers.load_image(s3_source, s3_client=RaisingS3Client())
    assert s3_source in str(fetch_error.value)
