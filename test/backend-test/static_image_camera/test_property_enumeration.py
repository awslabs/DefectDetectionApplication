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
"""Property test for enumeration inclusion iff pinned (task 4.2).

**Feature: static-image-camera-source, Property 2: Enumeration includes
the static camera iff pinned, preserving physical cameras**

*For any* list of physical cameras (including the empty list) and any
sequence of pin / replace / unpin operations, every enumeration performed
after an operation contains exactly one Static_Image_Camera entry when a
Pinned_Image currently exists and zero when none does; that entry has all
seven identity fields non-empty; and the physical entries in the result
are exactly the input physical list, unchanged and in order.

**Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.5, 2.6, 7.1, 7.3**

Exercises the WIRED ``aravis_functions.getCameras()`` /
``rescan_cameras()`` (standard enumeration and forced rescan alike, per
Requirements 2.1-2.3, 2.6) with the module-level ``Aravis`` binding
replaced by a physical-bus double (the ``mock_gi`` conftest stub makes
the module importable; the double supplies the enumeration entry points)
and a real ``StaticImageStore`` over a temporary directory installed as
the module's store. Empty physical lists model the Cloud_Environment
(Req 7.1); non-empty lists model the Edge_Device (Req 7.3).

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

import edge_ml1_p_camera_management.aravis_functions as aravis_functions
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    StaticImagePinError,
    StaticImageStore,
)

from static_image_strategies import (
    FakeAravisBus,
    camera_fields,
    image_specs,
    physical_camera_lists,
    physical_fields,
    render_image_bytes,
)

# An operation is ("pin", image_spec) or ("unpin", None). Consecutive pins
# model replacement; unpin on an unpinned store models the no-op error
# branch (state must stay unchanged either way).
_operations = st.lists(
    st.one_of(
        st.tuples(st.just("pin"), image_specs),
        st.tuples(st.just("unpin"), st.none()),
    ),
    min_size=1,
    max_size=4,
)


def _assert_enumeration(cameras, physical, pinned):
    """The Property 2 postcondition over one enumeration result."""
    static_entries = [c for c in cameras if c.id == STATIC_IMAGE_CAMERA_ID]
    physical_entries = [c for c in cameras if c.id != STATIC_IMAGE_CAMERA_ID]

    # Exactly one static entry iff pinned, zero otherwise (Req 1.2, 2.1,
    # 2.2, 2.3, 2.6, 7.1).
    assert len(static_entries) == (1 if pinned else 0)

    if pinned:
        # All seven identity fields non-empty (Req 2.1).
        for value in camera_fields(static_entries[0]):
            assert isinstance(value, str) and value != ""

    # Physical entries are exactly the input list, unchanged and in order
    # (Req 2.5, 7.3).
    assert [camera_fields(c) for c in physical_entries] == [
        physical_fields(p) for p in physical
    ]


@settings(deadline=None)
@given(physical=physical_camera_lists, operations=_operations)
def test_enumeration_includes_static_camera_iff_pinned(physical, operations):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        bus = FakeAravisBus(physical)
        with patch.object(aravis_functions, "Aravis", bus), \
                patch.object(aravis_functions, "get_store", lambda: store):
            pinned = False
            for op, spec in operations:
                if op == "pin":
                    store.pin_bytes(render_image_bytes(*spec), "pinned.img")
                    pinned = True
                else:
                    try:
                        store.unpin()
                    except StaticImagePinError:
                        pass  # nothing pinned; state unchanged
                    pinned = False

                # The first enumeration after the operation reflects it,
                # for standard enumeration and forced rescan alike
                # (Req 2.1, 2.2, 2.3, 2.6).
                _assert_enumeration(
                    aravis_functions.getCameras(), physical, pinned
                )
                _assert_enumeration(
                    aravis_functions.rescan_cameras(), physical, pinned
                )
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
