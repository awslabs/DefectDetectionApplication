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
"""Property test for no-usable-image grab failure.

**Feature: static-image-camera-source, Property 10: No-usable-image grab
failure**

*For any* state in which no usable Pinned_Image exists — never pinned,
removed, the stored file deleted out from under the store, or the stored
bytes corrupted — a grab against the Static_Image_Camera identifier
raises an error whose message names the Static_Image_Camera and indicates
that no usable Pinned_Image is available.

**Validates: Requirements 3.5**

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
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
    StaticImageUnavailableError,
)

from static_image_strategies import image_specs, render_image_bytes

_STATES = st.sampled_from(
    ["never_pinned", "removed", "file_deleted", "file_corrupted"]
)


@settings(deadline=None)
@given(
    spec=image_specs,
    state=_STATES,
    grab_before_break=st.booleans(),
    junk=st.binary(min_size=1, max_size=64),
)
def test_no_usable_image_grab_failure(spec, state, grab_before_break, junk):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        pinned_path = os.path.join(base_dir, "pinned_image")

        if state != "never_pinned":
            store.pin_bytes(render_image_bytes(*spec), "pinned.img")
            if grab_before_break:
                # Populate the decode cache first: staleness must not mask
                # the broken state.
                store.get_frame()

        if state == "removed":
            store.unpin()
        elif state == "file_deleted":
            os.remove(pinned_path)
        elif state == "file_corrupted":
            with open(pinned_path, "wb") as pinned:
                pinned.write(b"\x00corrupted\x00" + junk)

        with pytest.raises(StaticImageUnavailableError) as exc_info:
            store.get_frame()

        message = str(exc_info.value)
        assert STATIC_IMAGE_CAMERA_ID in message
        assert "no usable pinned image" in message.lower()
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
