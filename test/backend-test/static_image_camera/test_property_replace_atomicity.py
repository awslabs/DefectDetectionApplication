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
"""Property test for replace atomicity and freshness.

**Feature: static-image-camera-source, Property 8: Replace atomicity and
freshness**

*For any* two valid images A and B, after pinning A then pinning B, every
grab that begins after the second pin's confirmation returns exactly B's
full decoded content; and every grab performed at any point returns a
frame equal in its entirety to exactly one of the decoded images — never
a combination of the two.

**Validates: Requirements 5.1, 5.2**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import StaticImageStore

from static_image_strategies import (
    expected_frame,
    image_specs,
    render_image_bytes,
)


def _frame_tuple(frame):
    return (frame["data"], frame["width"], frame["height"], frame["pixel_format"])


@settings(deadline=None)
@given(
    spec_a=image_specs,
    spec_b=image_specs,
    grabs_between=st.integers(min_value=1, max_value=3),
    grabs_after=st.integers(min_value=1, max_value=3),
)
def test_replace_atomicity_and_freshness(
    spec_a, spec_b, grabs_between, grabs_after
):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        data_a = render_image_bytes(*spec_a)
        data_b = render_image_bytes(*spec_b)
        decode_a = _frame_tuple(expected_frame(data_a))
        decode_b = _frame_tuple(expected_frame(data_b))

        store.pin_bytes(data_a, "a.img")
        frames_between = [store.get_frame() for _ in range(grabs_between)]

        store.pin_bytes(data_b, "b.img")  # replace confirmed on return
        frames_after = [store.get_frame() for _ in range(grabs_after)]

        # Every grab after the replacement's confirmation returns exactly
        # B's full decoded content (Req 5.2).
        for frame in frames_after:
            assert _frame_tuple(frame) == decode_b

        # Every grab at any point equals exactly one of the two decodes in
        # its entirety — never a combination (Req 5.1).
        for frame in frames_between + frames_after:
            assert _frame_tuple(frame) in (decode_a, decode_b)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
