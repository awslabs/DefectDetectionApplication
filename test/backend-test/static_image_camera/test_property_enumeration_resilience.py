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
"""Property test for enumeration resilience to static-entry failure
(task 4.4).

**Feature: static-image-camera-source, Property 4: Enumeration resilience
to static-entry failure**

*For any* list of physical cameras, when the static-entry construction
raises any exception, the enumeration result equals exactly the physical
camera list and no exception propagates to the caller.

**Validates: Requirements 2.7**

The static-entry construction inside the wired
``aravis_functions.getCameras()`` is forced to raise at each of its three
constituent steps — resolving the store (``get_store``), consulting the
pin state (``is_pinned``), and building the ``model.Camera`` entry — with
generated exception types. The physical loop's own ``Camera(...)`` calls
are positional, so the entry-construction failure (keyword construction
from ``STATIC_IMAGE_CAMERA_IDENTITY``) is injected without disturbing
them.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

import edge_ml1_p_camera_management.aravis_functions as aravis_functions
from utils.static_image_camera import StaticImageStore

from static_image_strategies import (
    FakeAravisBus,
    camera_fields,
    image_specs,
    physical_camera_lists,
    physical_fields,
    render_image_bytes,
)

_exception_types = st.sampled_from(
    [ValueError, RuntimeError, OSError, KeyError, TypeError, Exception]
)

_failure_points = st.sampled_from(["get_store", "is_pinned", "camera_ctor"])


class _ExplodingStore:
    """A store double whose pin-state consultation raises."""

    def __init__(self, exc_type):
        self._exc_type = exc_type

    def is_pinned(self):
        raise self._exc_type("static-entry pin-state consultation failed")


@settings(deadline=None)
@given(
    physical=physical_camera_lists,
    exc_type=_exception_types,
    failure_point=_failure_points,
    spec=image_specs,
)
def test_enumeration_survives_static_entry_failure(
    physical, exc_type, failure_point, spec
):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        bus = FakeAravisBus(physical)
        patches = [patch.object(aravis_functions, "Aravis", bus)]

        if failure_point == "get_store":
            def _raising_get_store():
                raise exc_type("static-entry store resolution failed")

            patches.append(
                patch.object(aravis_functions, "get_store", _raising_get_store)
            )
        elif failure_point == "is_pinned":
            exploding = _ExplodingStore(exc_type)
            patches.append(
                patch.object(aravis_functions, "get_store", lambda: exploding)
            )
        else:  # camera_ctor: the Camera(**STATIC_IMAGE_CAMERA_IDENTITY) call
            store = StaticImageStore(base_dir=base_dir)
            store.pin_bytes(render_image_bytes(*spec), "pinned.img")
            patches.append(
                patch.object(aravis_functions, "get_store", lambda: store)
            )
            real_camera = aravis_functions.Camera

            def _exploding_camera(*args, **kwargs):
                if kwargs:  # only the static entry is built from kwargs
                    raise exc_type("static entry construction failed")
                return real_camera(*args, **kwargs)

            patches.append(
                patch.object(aravis_functions, "Camera", _exploding_camera)
            )

        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            # Must not raise (no exception propagates to the caller)...
            cameras = aravis_functions.getCameras()

        # ...and the result equals exactly the physical camera list (Req 2.7).
        assert [camera_fields(c) for c in cameras] == [
            physical_fields(p) for p in physical
        ]
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
