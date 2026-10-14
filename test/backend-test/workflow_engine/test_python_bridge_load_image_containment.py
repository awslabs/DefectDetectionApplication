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
"""Example test for ``load_image`` failure containment through the bridge.

A handler calling ``dda_frames.load_image`` on a missing path raises
through a real :class:`CustomPythonBridge` subprocess as
``CustomPythonNodeError`` carrying the node id and the source in its
message — consistent with existing handler failure containment.

Requirements: 6.5
"""
import os

import pytest

from workflow_engine.python_bridge import (
    CustomPythonBridge,
    CustomPythonNodeError,
)

NODE_ID = "load-image-containment-node"

#: Generous per-frame limit: the subprocess pays the numpy/cv2 import
#: cost on its first frame.
WALL_CLOCK_LIMIT_SEC = 30.0

#: Handler whose per-frame code loads an image from a path supplied by
#: the caller through the request metadata.
LOAD_IMAGE_HANDLER = """\
import dda_frames

def process_frame(frame, metadata):
    dda_frames.load_image(metadata["image_path"])
    return None
"""


def test_load_image_missing_path_contained_as_custom_python_node_error(
    tmp_path,
):
    """A handler calling dda_frames.load_image on a missing local path
    fails the run through the bridge as CustomPythonNodeError carrying
    the node id and the source path in its message (Requirement 6.5)."""
    handler_path = str(tmp_path / "handler.py")
    with open(handler_path, "w") as f:
        f.write(LOAD_IMAGE_HANDLER)

    missing_path = str(tmp_path / "no-such-image.png")
    assert not os.path.exists(missing_path)

    bridge = CustomPythonBridge(
        NODE_ID,
        handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    try:
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(
                b"\x00",
                metadata={"image_path": missing_path},
                width=1,
                height=1,
                frame_format="GRAY8",
            )
    finally:
        bridge.stop()

    # The error carries the node id (existing containment behavior).
    assert excinfo.value.node_id == NODE_ID
    message = str(excinfo.value)
    assert NODE_ID in message

    # The error message identifies the failing source (Requirement 6.4
    # message surfaced through the bridge per Requirement 6.5).
    assert missing_path in message
    assert "load_image" in message
