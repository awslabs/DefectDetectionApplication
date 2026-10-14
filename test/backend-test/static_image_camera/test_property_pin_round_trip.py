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
"""Property test for pin round-trip fidelity.

**Feature: static-image-camera-source, Property 1: Pin round-trip fidelity**

*For any* valid image (any dimensions, any pixel content, any
Supported_Image_Format) and any prior pin state, pinning it succeeds, the
returned and queried metadata report the decoded image's width, height,
format, and the submitted file name, and a subsequent grab returns a frame
whose `data` equals the image's packed RGB decode byte for byte, whose
`width`/`height` equal the decoded dimensions, whose `pixel_format` is
`"RGB"`, and whose `len(data) == 3 * width * height`.

**Validates: Requirements 1.1, 1.5, 1.6, 3.1, 3.2, 3.7**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID, StaticImageStore

from static_image_strategies import (
    expected_frame,
    expected_metadata,
    file_names,
    image_specs,
    render_image_bytes,
)


@settings(deadline=None)
@given(
    spec=image_specs,
    prior_spec=st.none() | image_specs,
    file_name=file_names,
)
def test_pin_round_trip_fidelity(spec, prior_spec, file_name):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)

        # Any prior pin state: none, or some other valid pinned image.
        if prior_spec is not None:
            store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")

        data = render_image_bytes(*spec)
        exp_meta = expected_metadata(data, file_name)

        # Pinning succeeds and returns the decoded metadata (Req 1.1, 1.5).
        returned = store.pin_bytes(data, file_name)
        for key, value in exp_meta.items():
            assert returned[key] == value, key
        assert isinstance(returned["pinnedAtEpochMs"], int)

        # Queried status reports the same metadata (Req 1.6).
        status = store.status()
        assert status["pinned"] is True
        assert status["cameraId"] == STATIC_IMAGE_CAMERA_ID
        for key, value in exp_meta.items():
            assert status["metadata"][key] == value, key

        # A grab reproduces the packed RGB decode byte for byte with the
        # decoded dimensions and a truthful pixel format tag
        # (Req 3.1, 3.2, 3.7).
        frame = store.get_frame()
        exp = expected_frame(data)
        assert frame["pixel_format"] == "RGB"
        assert frame["width"] == exp["width"]
        assert frame["height"] == exp["height"]
        assert frame["data"] == exp["data"]
        assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
