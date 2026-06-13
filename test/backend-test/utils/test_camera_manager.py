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
    """Regression tests for ``get_camera_frame`` after reverting it to the cached,
    persistent connection model (the production ``LIBUSB_ERROR_BUSY`` fix).

    The earlier broadcaster rewiring (task 7.2) opened AND closed a fresh device
    claim on every call via the broadcaster's no-session path; on real USB3Vision
    hardware the ~500 ms preview poll overlapped those open/close cycles and the
    device rejected the next claim with ``LIBUSB_ERROR_BUSY``, breaking the live
    view. ``get_camera_frame`` is back to the proven model: open the camera once
    (``connect_camera`` -> ``camera_objects``) and **reuse** it across calls,
    performing only a per-request ``start_acquisition``/``get_frame``/
    ``stop_acquisition`` (in ``_get_camera_frame``) without releasing the claim
    between calls.

    The caller contract (``digital_input_*`` / ``workflow`` / capture / preview) is
    unchanged: same signature, returns the ``{'data','height','width'}`` dict on
    success, raises ``Exception`` on no-frame/failure. These tests pin that contract
    and the no-per-call-reconnect behavior by patching ``camera_objects`` /
    ``connect_camera`` / ``_get_camera_frame`` so no real device is needed.
    """

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

    def test_reuses_cached_camera_without_reconnecting(self):
        """When the camera is already cached, get_camera_frame reuses it and does NOT
        re-open the USB claim (the crux of the LIBUSB_ERROR_BUSY fix)."""
        from utils.camera_manager import get_camera_frame

        frame = {"data": b"\x00\x01\x02\x03", "height": 2, "width": 2}
        fake_camera = Mock(name="Camera")
        with patch("utils.camera_manager.camera_objects", {"Fake_1": fake_camera}), \
             patch("utils.camera_manager.connect_camera") as mock_connect, \
             patch("utils.camera_manager._get_camera_frame", return_value=frame) as mock_grab:
            result = get_camera_frame("Fake_1", {"gain": 5, "exposure": 1000})

        self.assertEqual(result, frame)
        # Cached connection reused: no re-open/claim per call.
        mock_connect.assert_not_called()
        mock_grab.assert_called_once()
        # camera_config is forwarded to the per-request grab unchanged.
        self.assertEqual(mock_grab.call_args[0][2], {"gain": 5, "exposure": 1000})

    def test_connects_once_when_not_cached(self):
        """When no cached camera exists, get_camera_frame connects exactly once
        (then reuse takes over on subsequent calls)."""
        from utils.camera_manager import get_camera_frame

        frame = {"data": b"x", "height": 1, "width": 1}
        cache = {}
        fake_camera = Mock(name="Camera")

        def _fake_connect(camera_id):
            cache[camera_id] = fake_camera

        with patch("utils.camera_manager.camera_objects", cache), \
             patch("utils.camera_manager.connect_camera", side_effect=_fake_connect) as mock_connect, \
             patch("utils.camera_manager._get_camera_frame", return_value=frame):
            result = get_camera_frame("Fake_1")

        self.assertEqual(result, frame)
        mock_connect.assert_called_once_with("Fake_1")

    def test_raises_when_no_frame(self):
        """A None grab result raises Exception (capture/workflow surface it as HTTP error)."""
        from utils.camera_manager import get_camera_frame

        fake_camera = Mock(name="Camera")
        with patch("utils.camera_manager.camera_objects", {"Fake_1": fake_camera}), \
             patch("utils.camera_manager.connect_camera"), \
             patch("utils.camera_manager._get_camera_frame", return_value=None):
            with self.assertRaises(Exception) as ctx:
                get_camera_frame("Fake_1", {"gain": 1})
        self.assertIn("Fake_1", str(ctx.exception))
