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
"""Property test for invalid pin input preserving prior state.

**Feature: static-image-camera-source, Property 6: Invalid pin input
preserves prior state**

*For any* prior state (a valid Pinned_Image or none) and any invalid pin
input — bytes that do not decode as a Supported_Image_Format, an input
exceeding the (injected) size limit, or a reference to a nonexistent
captured image — the pin operation fails with a descriptive error (naming
the supported formats for decode failures, the size limit for oversized
input, or not-found for missing references), and afterwards the pin
status, metadata, served frame content, and enumeration inclusion are
identical to the prior state.

**Validates: Requirements 1.3, 1.4, 1.8, 5.3**

Enumeration inclusion is gated by ``store.is_pinned()`` (the design's
``getCameras()`` merge appends the static entry iff it returns True); the
full enumeration merge is covered by Property 2 (task 4.2).

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import os
import shutil
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import (
    SUPPORTED_FORMATS,
    StaticImagePinError,
    StaticImageStore,
)

from static_image_strategies import image_specs, render_image_bytes

# Injected size limit: large enough that every generated valid image
# (<= 48x48 BMP ~= 7 KB) pins fine, small enough that the oversized branch
# never needs a large payload.
_TEST_MAX_FILE_BYTES = 64 * 1024

_INVALID_KINDS = st.sampled_from(
    ["undecodable", "oversized", "missing_reference"]
)

# Bytes that can never decode as an image: a non-image prefix keeps random
# tails from accidentally forming a valid header.
_undecodable_payloads = st.binary(min_size=0, max_size=256).map(
    lambda tail: b"\x00not-an-image\x00" + tail
)


def _snapshot(store):
    """Observable state: status (incl. metadata), frame, enumeration gate."""
    status = store.status()
    try:
        frame = store.get_frame()
    except Exception:
        frame = None
    return status, frame, store.is_pinned()


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    kind=_INVALID_KINDS,
    undecodable=_undecodable_payloads,
    oversize_extra=st.integers(min_value=1, max_value=4096),
    missing_name=st.integers(min_value=0, max_value=10**6),
)
def test_invalid_pin_preserves_prior_state(
    prior_spec, kind, undecodable, oversize_extra, missing_name
):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    captures_root = os.path.join(base_dir, "captures")
    os.makedirs(captures_root)
    try:
        store = StaticImageStore(
            base_dir=os.path.join(base_dir, "store"),
            max_file_bytes=_TEST_MAX_FILE_BYTES,
        )
        if prior_spec is not None:
            store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")

        before = _snapshot(store)

        with pytest.raises(StaticImagePinError) as exc_info:
            if kind == "undecodable":
                store.pin_bytes(undecodable, "bad.img")
            elif kind == "oversized":
                store.pin_bytes(
                    b"A" * (_TEST_MAX_FILE_BYTES + oversize_extra), "big.img"
                )
            else:  # missing_reference
                store.pin_file(
                    os.path.join(
                        captures_root, "missing-{}.png".format(missing_name)
                    ),
                    captures_root,
                )

        message = str(exc_info.value)
        if kind == "undecodable":
            # Descriptive error enumerating the supported formats (Req 1.3).
            for fmt in SUPPORTED_FORMATS:
                assert fmt in message
        elif kind == "oversized":
            # Descriptive error naming the size limit (Req 1.4).
            assert str(_TEST_MAX_FILE_BYTES) in message
            assert "maximum" in message.lower()
        else:
            # Descriptive not-found error (Req 1.8).
            assert "not found" in message.lower()

        # Status, metadata, frame content, and enumeration inclusion are
        # identical to the prior state (Req 1.3, 1.4, 1.8, 5.3).
        assert _snapshot(store) == before
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
