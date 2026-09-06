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
"""Property test for corruption containment at restore.

**Feature: static-image-camera-source, Property 12: Corruption containment
at restore**

*For any* corruption mode of the stored state (image file missing,
metadata sidecar missing, or image bytes undecodable), a fresh store
completes construction, logs an error identifying the failure cause
category (missing data versus undecodable data), reports no Pinned_Image,
leaves enumeration returning exactly the physical cameras, and accepts a
subsequent valid pin that fully restores normal behavior.

**Validates: Requirements 6.4, 6.5**

The enumeration merge is modeled exactly as the design's ``getCameras()``
change specifies (append the static entry iff ``is_pinned()``); the wired
``aravis_functions`` merge itself is covered by Property 2 (task 4.2).

The log assertion uses a handler attached directly to the module logger
rather than the ``caplog`` fixture: function-scoped fixtures do not mix
with ``@given`` (hypothesis reuses the fixture across examples).

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import logging
import os
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

import utils.static_image_camera as sic
from utils.static_image_camera import StaticImageStore

from static_image_strategies import (
    expected_frame,
    image_specs,
    render_image_bytes,
)

_CORRUPTION_MODES = st.sampled_from(
    ["image_missing", "sidecar_missing", "undecodable"]
)

# The cause category each corruption mode must be reported under.
_EXPECTED_CATEGORY = {
    "image_missing": "missing data",
    "sidecar_missing": "missing data",
    "undecodable": "undecodable data",
}

_physical_camera_ids = st.lists(
    st.sampled_from(["Aravis-Fake-GV01", "Basler-40022199", "Lucid-223700XX"]),
    min_size=0,
    max_size=3,
    unique=True,
)


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _enumerate(store, physical):
    """The design's getCameras() merge: physical + static entry iff pinned."""
    cameras = list(physical)
    if store.is_pinned():
        cameras.append(sic.STATIC_IMAGE_CAMERA_ID)
    return cameras


@settings(deadline=None)
@given(
    spec=image_specs,
    mode=_CORRUPTION_MODES,
    recovery_spec=image_specs,
    physical=_physical_camera_ids,
    junk=st.binary(min_size=1, max_size=64),
)
def test_corruption_containment_at_restore(
    spec, mode, recovery_spec, physical, junk
):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    handler = _RecordingHandler()
    module_logger = logging.getLogger(sic.__name__)
    original_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.ERROR)
    try:
        # Pin normally, then corrupt the stored state out from under it.
        StaticImageStore(base_dir=base_dir).pin_bytes(
            render_image_bytes(*spec), "pinned.img"
        )
        image_path = os.path.join(base_dir, "pinned_image")
        sidecar_path = os.path.join(base_dir, "pinned_image.json")
        if mode == "image_missing":
            os.remove(image_path)
        elif mode == "sidecar_missing":
            os.remove(sidecar_path)
        else:  # undecodable
            with open(image_path, "wb") as pinned:
                pinned.write(b"\x00corrupted\x00" + junk)

        # A fresh store (modeling restart) completes construction...
        store = StaticImageStore(base_dir=base_dir)

        # ...reports no Pinned_Image (Req 6.4)...
        status = store.status()
        assert status["pinned"] is False
        assert status["metadata"] is None

        # ...logs an error identifying the cause category (Req 6.4)...
        messages = [record.getMessage() for record in handler.records]
        assert any(_EXPECTED_CATEGORY[mode] in message for message in messages)
        assert any(sic.STATIC_IMAGE_CAMERA_ID in message for message in messages)

        # ...and leaves enumeration returning exactly the physical cameras
        # (Req 6.5).
        assert _enumerate(store, physical) == list(physical)

        # A subsequent valid pin fully restores normal behavior (Req 6.5).
        recovery_data = render_image_bytes(*recovery_spec)
        store.pin_bytes(recovery_data, "recovery.img")
        assert store.status()["pinned"] is True
        expected = expected_frame(recovery_data)
        frame = store.get_frame()
        assert frame["data"] == expected["data"]
        assert frame["width"] == expected["width"]
        assert frame["height"] == expected["height"]
        assert frame["pixel_format"] == "RGB"
        assert _enumerate(store, physical) == list(physical) + [
            sic.STATIC_IMAGE_CAMERA_ID
        ]
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(original_level)
        shutil.rmtree(base_dir, ignore_errors=True)
