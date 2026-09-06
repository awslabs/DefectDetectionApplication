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
"""Property test for grab determinism and config invariance (task 5.2).

**Feature: static-image-camera-source, Property 5: Grab determinism and
acquisition-config invariance**

*For any* pinned image, any number of repeated grabs, and any acquisition
configuration dict (arbitrary gain, exposure, and advancedSettings
values, or none), every grab completes without error and returns
byte-for-byte identical ``(data, width, height, pixel_format)``
regardless of the configuration supplied.

**Validates: Requirements 3.3, 3.4**

Exercises the WIRED ``utils.camera_manager.get_camera_frame`` short
circuit (the ``mock_gi`` conftest stub makes the module importable
without the Aravis/GLib stack) with a real ``StaticImageStore`` over a
temporary directory installed as the module's store. The import happens
inside the test body so the conftest's import mocker (asgi_correlation_id
et al.) is active, matching ``utils/test_camera_manager.py``.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)

from camera_manager_support import import_camera_manager
from static_image_strategies import (
    expected_frame,
    image_specs,
    render_image_bytes,
)

# Arbitrary acquisition configuration: any combination of gain, exposure,
# and advancedSettings values (all accepted and ignored), or None.
_advanced_settings = st.dictionaries(
    keys=st.sampled_from(["reverseX", "reverseY", "balanceWhiteAuto"]),
    values=st.one_of(st.booleans(), st.integers(-5, 5), st.text(max_size=8)),
    max_size=3,
)

_acquisition_configs = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {},
        optional={
            "gain": st.one_of(
                st.integers(-100, 100),
                st.floats(allow_nan=False, allow_infinity=False),
            ),
            "exposure": st.integers(0, 10_000_000),
            "advancedSettings": _advanced_settings,
        },
    ),
)


@settings(deadline=None)
@given(
    spec=image_specs,
    configs=st.lists(_acquisition_configs, min_size=1, max_size=4),
)
def test_grab_determinism_and_config_invariance(spec, configs):
    # Imported inside the test so the conftest import mocker is active
    # (same pattern as utils/test_camera_manager.py); the support helper
    # handles the forkserver-host import fallback.
    camera_manager = import_camera_manager()

    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        data = render_image_bytes(*spec)
        store.pin_bytes(data, "pinned.img")
        expected = expected_frame(data)

        with patch.object(
            camera_manager, "get_static_image_store", lambda: store
        ):
            # Repeated grabs across the generated configs plus a bare
            # no-config grab as the invariance baseline (Req 3.4).
            frames = [
                camera_manager.get_camera_frame(STATIC_IMAGE_CAMERA_ID, config)
                for config in configs
            ]
            frames.append(
                camera_manager.get_camera_frame(STATIC_IMAGE_CAMERA_ID)
            )

        for frame in frames:
            # Byte-for-byte identical to the decode oracle on every grab,
            # regardless of the configuration supplied (Req 3.3, 3.4).
            assert frame["data"] == expected["data"]
            assert frame["width"] == expected["width"]
            assert frame["height"] == expected["height"]
            assert frame["pixel_format"] == "RGB"
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
