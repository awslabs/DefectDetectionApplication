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
"""Property test for pin-from-capture parity.

**Feature: static-image-camera-source, Property 7: Pin-from-capture parity**

*For any* valid image written under the captures root, pinning it by
reference produces the same stored metadata and served frame as pinning
its bytes directly; and *for any* path resolving outside the captures
root, the pin-by-reference is rejected without state change.

**Validates: Requirements 1.7**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import os
import shutil
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import StaticImagePinError, StaticImageStore

from static_image_strategies import (
    file_names,
    image_specs,
    metadata_without_timestamp,
    render_image_bytes,
)


@settings(deadline=None)
@given(
    spec=image_specs,
    file_name=file_names,
    outside_spec=image_specs,
    use_traversal=st.booleans(),
)
def test_pin_from_capture_parity(spec, file_name, outside_spec, use_traversal):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        captures_root = os.path.join(base_dir, "captures")
        os.makedirs(captures_root)
        data = render_image_bytes(*spec)
        capture_path = os.path.join(captures_root, file_name)
        with open(capture_path, "wb") as capture:
            capture.write(data)

        # Pin-by-reference and direct pin_bytes of the same bytes into two
        # independent stores.
        store_ref = StaticImageStore(base_dir=os.path.join(base_dir, "ref"))
        store_direct = StaticImageStore(
            base_dir=os.path.join(base_dir, "direct")
        )
        meta_ref = store_ref.pin_file(capture_path, captures_root)
        meta_direct = store_direct.pin_bytes(data, file_name)

        # Same stored metadata (timestamps differ across distinct pin
        # calls) and same served frame (Req 1.7).
        assert metadata_without_timestamp(meta_ref) == metadata_without_timestamp(
            meta_direct
        )
        status_ref = store_ref.status()
        status_direct = store_direct.status()
        assert status_ref["pinned"] and status_direct["pinned"]
        assert metadata_without_timestamp(
            status_ref["metadata"]
        ) == metadata_without_timestamp(status_direct["metadata"])
        assert store_ref.get_frame() == store_direct.get_frame()

        # A path resolving outside the captures root is rejected without
        # state change — whether it points at a real image elsewhere or
        # traverses out of the root.
        outside_dir = os.path.join(base_dir, "outside")
        os.makedirs(outside_dir, exist_ok=True)
        outside_file = os.path.join(outside_dir, "escape.img")
        with open(outside_file, "wb") as escape:
            escape.write(render_image_bytes(*outside_spec))
        if use_traversal:
            bad_path = os.path.join(captures_root, "..", "outside", "escape.img")
        else:
            bad_path = outside_file

        before_status = store_ref.status()
        before_frame = store_ref.get_frame()
        with pytest.raises(StaticImagePinError):
            store_ref.pin_file(bad_path, captures_root)
        assert store_ref.status() == before_status
        assert store_ref.get_frame() == before_frame
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
