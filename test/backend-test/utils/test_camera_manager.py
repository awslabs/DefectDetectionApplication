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

import inspect

from unittest.mock import patch, Mock
from utils.common import CameraStatusEnum
from local_server_base_test_case import LocalServerBaseTestCase
from exceptions.api.aravis_camera_exception import AravisCameraException
from data_models.common import CameraStatusModel

class TestCameraManager(LocalServerBaseTestCase):
    def test_connect_camera(self):
        from utils.camera_manager import connect_camera
        result = connect_camera("Fake_1")
        self.assertEqual(result, True)

    @patch("utils.camera_manager.get_camera_status", return_value = CameraStatusModel(status=CameraStatusEnum.DISCONNECTED, lastUpdatedTime=0.0))
    def test_connect_camera_failed(self, mock_get_camera_status):
        from utils.camera_manager import connect_camera
        try:
            connect_camera("Fake_1")
        except AravisCameraException:
            assert "Camera connection failed" 

    def test_disconnect_camera(self):
        from utils.camera_manager import disconnect_camera, get_all_camera_statuses, connect_camera
        connect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 1)

        disconnect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0)

    def test_disconnect_nonexistent_camera(self):
        from utils.camera_manager import disconnect_camera, get_all_camera_statuses
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0)

        disconnect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0) 

    def test_get_all_camera_statuses(self):
        from utils.camera_manager import get_all_camera_statuses, connect_camera
        connect_camera("Fake_1")
        connect_camera("Fake_2")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 2)

    def test_get_camera_status(self):
        from utils.camera_manager import get_camera_status, connect_camera
        connect_camera("Fake_1")
        result = get_camera_status("Fake_1")
        self.assertEqual(result.status, CameraStatusEnum.CONNECTED)

    def test_get_nonexistent_camera_status(self):
        from utils.camera_manager import get_camera_status 
        result = get_camera_status("fake")
        self.assertEqual(result.status, CameraStatusEnum.DISCONNECTED)


class TestGetCameraFrameCompatibility(LocalServerBaseTestCase):
    """Regression tests for ``get_camera_frame`` after it was rewired (task 7.2)
    onto ``StreamBroadcaster.get_inference_frame`` (task 7.7).

    The existing ``digital_input_*`` / ``workflow`` / capture callers depend on a
    stable contract for ``get_camera_frame(camera_id, camera_config)``:

    * the signature is unchanged — ``camera_config`` is positional-or-keyword and
      defaults to ``None``,
    * on success it returns the broadcaster's frame dict (``{'data','height','width'}``)
      verbatim,
    * on no-frame it raises an ``Exception`` (the capture / workflow endpoints catch
      this and surface an HTTP error),
    * ``camera_id`` and ``camera_config`` are forwarded unchanged to the broadcaster.

    These tests pin that contract by patching the broadcaster singleton, so they do
    not need a real camera / device stack. ``get_camera_frame`` resolves the
    broadcaster via a function-level ``from utils.streaming.broadcaster import
    get_broadcaster``, so patching ``utils.streaming.broadcaster.get_broadcaster`` is
    what the call picks up.
    _Validates: Requirements 6.1, 6.3_
    """

    def _patch_broadcaster(self, frame):
        """Patch the broadcaster singleton so ``get_inference_frame`` returns ``frame``.

        Returns the started patcher (caller stops it) and the stub broadcaster so the
        test can assert how it was called.
        """
        stub = Mock(name="StreamBroadcaster")
        stub.get_inference_frame.return_value = frame
        patcher = patch(
            "utils.streaming.broadcaster.get_broadcaster", return_value=stub
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def test_signature_is_unchanged(self):
        """The public signature stays ``get_camera_frame(camera_id, camera_config=None)``
        so positional callers (``digital_input_*`` / ``workflow`` / capture) keep working."""
        from utils.camera_manager import get_camera_frame

        sig = inspect.signature(get_camera_frame)
        params = list(sig.parameters)
        self.assertEqual(params, ["camera_id", "camera_config"])
        # camera_config must remain optional (defaults to None) for the single-arg
        # callers and for backwards compatibility.
        self.assertEqual(
            sig.parameters["camera_config"].default, None
        )

    def test_returns_broadcaster_frame_dict_on_success(self):
        """A successful inference frame is returned verbatim as the legacy dict shape."""
        from utils.camera_manager import get_camera_frame

        frame = {"data": b"\x00\x01\x02\x03", "height": 2, "width": 2}
        stub = self._patch_broadcaster(frame)

        result = get_camera_frame("Fake_1", {"gain": 5, "exposure": 1000})

        self.assertIs(result, frame)
        self.assertEqual(result, {"data": b"\x00\x01\x02\x03", "height": 2, "width": 2})
        stub.get_inference_frame.assert_called_once_with(
            "Fake_1", {"gain": 5, "exposure": 1000}
        )

    def test_forwards_camera_id_and_config_unchanged(self):
        """``camera_id`` and ``camera_config`` are passed through to the broadcaster
        untouched, so capture/workflow per-capture config still reaches the device path."""
        from utils.camera_manager import get_camera_frame

        config = {"type": "Camera", "imageSourceConfiguration": {"gain": 2}}
        stub = self._patch_broadcaster({"data": b"x", "height": 1, "width": 1})

        get_camera_frame("camera-123", config)

        stub.get_inference_frame.assert_called_once_with("camera-123", config)

    def test_defaults_camera_config_to_none_for_single_arg_callers(self):
        """Single-argument callers (``get_camera_frame(camera_id)``) forward ``None``
        as the config, exercising the broadcaster's no-config path."""
        from utils.camera_manager import get_camera_frame

        stub = self._patch_broadcaster({"data": b"y", "height": 1, "width": 1})

        get_camera_frame("Fake_1")

        stub.get_inference_frame.assert_called_once_with("Fake_1", None)

    def test_raises_when_broadcaster_returns_none(self):
        """When no frame is available the broadcaster returns ``None``; the legacy
        contract requires ``get_camera_frame`` to raise so callers surface an error."""
        from utils.camera_manager import get_camera_frame

        self._patch_broadcaster(None)

        with self.assertRaises(Exception) as ctx:
            get_camera_frame("Fake_1", {"gain": 1})
        self.assertIn("Fake_1", str(ctx.exception))

    def test_capture_caller_surfaces_no_frame_as_exception(self):
        """End-to-end-ish: the capture helper (``captured_images_utils``) wraps
        ``get_camera_frame`` in try/except and re-raises as an HTTP error, so a
        ``None`` from the broadcaster must propagate as a raised exception, not a
        ``None`` return that would slip past the caller's error handling."""
        from utils.camera_manager import get_camera_frame

        self._patch_broadcaster(None)

        # The capture path does: try: return get_camera_frame(...) except Exception: raise HTTPException
        # so confirm the exception is raised (not swallowed / not a None return).
        raised = False
        try:
            get_camera_frame("Fake_1", {})
        except Exception:
            raised = True
        self.assertTrue(raised, "get_camera_frame must raise on no-frame for callers")
