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
"""Property test for restart persistence round trip.

**Feature: static-image-camera-source, Property 11: Restart persistence
round trip**

*For any* pinned image, constructing a fresh `StaticImageStore` over the
same directory (modeling a LocalServer restart) reports `pinned` on its
first status call with metadata equal to the pre-restart metadata, and
its first grab returns frame content byte-for-byte identical to a
pre-restart grab.

**Validates: Requirements 6.1, 6.2, 6.3**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings

from utils.static_image_camera import StaticImageStore

from static_image_strategies import file_names, image_specs, render_image_bytes


@settings(deadline=None)
@given(spec=image_specs, file_name=file_names)
def test_restart_persistence_round_trip(spec, file_name):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store_before = StaticImageStore(base_dir=base_dir)
        store_before.pin_bytes(render_image_bytes(*spec), file_name)
        status_before = store_before.status()
        frame_before = store_before.get_frame()

        # Fresh store over the same directory models a LocalServer restart
        # (disk is the source of truth; no init hook).
        store_after = StaticImageStore(base_dir=base_dir)

        # First status call reports pinned with pre-restart metadata
        # (Req 6.1, 6.2 — including the original timestamp: the sidecar is
        # the persisted record).
        status_after = store_after.status()
        assert status_after["pinned"] is True
        assert status_after["metadata"] == status_before["metadata"]
        assert status_after["cameraId"] == status_before["cameraId"]

        # First grab is byte-for-byte identical to a pre-restart grab
        # (Req 6.3).
        frame_after = store_after.get_frame()
        assert frame_after["data"] == frame_before["data"]
        assert frame_after["width"] == frame_before["width"]
        assert frame_after["height"] == frame_before["height"]
        assert frame_after["pixel_format"] == frame_before["pixel_format"]
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
