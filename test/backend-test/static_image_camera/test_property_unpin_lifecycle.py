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
"""Property test for the unpin lifecycle.

**Feature: static-image-camera-source, Property 9: Unpin lifecycle**

*For any* pinned state, removal succeeds, after which the status reports
no Pinned_Image, enumeration excludes the Static_Image_Camera, and grabs
fail; and *for any* unpinned state (never pinned or already removed),
removal fails with an error indicating no image is pinned and changes
neither the stored state nor enumeration results.

**Validates: Requirements 5.4, 5.5**

Enumeration inclusion is gated by ``store.is_pinned()`` (the design's
``getCameras()`` merge appends the static entry iff it returns True); the
full enumeration merge is covered by Property 2 (task 4.2).

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import (
    StaticImagePinError,
    StaticImageStore,
    StaticImageUnavailableError,
)

from static_image_strategies import image_specs, render_image_bytes


@settings(deadline=None)
@given(
    spec=image_specs,
    state=st.sampled_from(["pinned", "never_pinned", "already_removed"]),
)
def test_unpin_lifecycle(spec, state):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)

        if state == "pinned":
            store.pin_bytes(render_image_bytes(*spec), "pinned.img")

            # Removal succeeds (Req 5.4)...
            store.unpin()

            # ...after which status reports no pin, enumeration excludes
            # the camera, and grabs fail.
            status = store.status()
            assert status["pinned"] is False
            assert status["metadata"] is None
            assert store.is_pinned() is False
            with pytest.raises(StaticImageUnavailableError):
                store.get_frame()
        else:
            if state == "already_removed":
                store.pin_bytes(render_image_bytes(*spec), "pinned.img")
                store.unpin()

            before_status = store.status()
            before_pinned = store.is_pinned()

            # Removal fails with an error indicating no image is pinned
            # (Req 5.5)...
            with pytest.raises(StaticImagePinError) as exc_info:
                store.unpin()
            assert "no image is pinned" in str(exc_info.value)

            # ...and changes neither stored state nor enumeration.
            assert store.status() == before_status
            assert store.is_pinned() == before_pinned is False
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
