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
"""Endpoint serialization / status-code tests for the ``/streams/...`` API (task 9.3).

These tests pin the HTTP contract of ``src/backend/endpoints/streams.py`` — the thin
client layer over the :class:`StreamBroadcaster`. They assert ONLY the endpoint's
translation of broadcaster domain results into status codes / response bodies / headers;
the broadcaster's own invariants (single claim, lifecycle, viewer-limit enforcement,
disconnection cascade) are covered by the property tests under
``test/backend-test/utils/streaming``.

Strategy: the endpoints call :func:`utils.streaming.broadcaster.get_broadcaster`, imported
into ``endpoints.streams``. Each test patches ``endpoints.streams.get_broadcaster`` to
return a small stub broadcaster whose methods return crafted
:class:`SubscribeResult` / :class:`FrameResult` values or raise
``NoActiveSessionError`` / ``SettingsApplyError`` so the endpoint mapping is exercised in
isolation, with no device / threading dependency.

Mapping asserted (from ``streams.py``):

* subscribe accepted        -> 200 ``{viewerId, viewerCount}``
* subscribe viewer_limit    -> 429
* subscribe camera_unavailable -> 503
* frame OK                  -> 200 bytes + ``X-Frame-Status: ok``      (Req 4.6)
* frame STALE               -> 200 bytes + ``X-Frame-Status: stale``   (Req 4.7)
* frame NO_FRAME            -> 204                                     (Req 2.6)
* frame DISCONNECTED        -> 503                                     (Req 7.5)
* heartbeat refreshed       -> 200 / unknown -> 404
* unsubscribe               -> 200 ``{viewerCount}``
* viewers                   -> 200 ``{viewerCount}``
* settings accepted         -> 200 dict
* settings NoActiveSession  -> 409
* settings SettingsApplyError -> 422 (names the failed control)
* viewer-limit boundary     -> 8th subscribe 200, 9th subscribe 429   (Req 1.3, 1.6)

_Requirements: 1.3, 1.6, 1.7, 2.6, 4.7, 7.5_
"""
from unittest.mock import patch

from local_server_base_test_case import LocalServerBaseTestCase
from fastapi.testclient import TestClient

from utils.streaming.models import (
    FrameResult,
    FrameStatus,
    LatestFrame,
    SubscribeResult,
)
from utils.streaming.broadcaster import NoActiveSessionError, SettingsApplyError

TEST_CAMERA_ID = "Fake_1"
TEST_VIEWER_ID = "viewer-abc"


def _make_frame(seq: int = 7, data: bytes = b"\x01\x02\x03\x04") -> LatestFrame:
    """Build a deterministic LatestFrame for frame-endpoint body/header assertions."""
    return LatestFrame(data=data, width=4, height=2, seq=seq, acquired_at=123.5)


class _StubBroadcaster:
    """Configurable stand-in for :class:`StreamBroadcaster` used by the endpoint tests.

    Each attribute holds the value the corresponding endpoint should translate; tests
    set the one(s) they care about. ``apply_settings`` raises ``apply_settings_exc`` when
    set so the 409 / 422 error mappings can be exercised.
    """

    def __init__(self):
        self.subscribe_result = SubscribeResult(
            viewer_id=TEST_VIEWER_ID, accepted=True, reason=None, viewer_count=1
        )
        self.frame_result = FrameResult(status=FrameStatus.NO_FRAME, frame=None)
        self.heartbeat_return = True
        self.viewer_count_return = 0
        self.apply_settings_return = {}
        self.apply_settings_exc = None
        self.unsubscribe_calls = []

    def subscribe(self, camera_id, config=None):
        return self.subscribe_result

    def get_frame(self, camera_id, viewer_id):
        return self.frame_result

    def heartbeat(self, camera_id, viewer_id):
        return self.heartbeat_return

    def unsubscribe(self, camera_id, viewer_id):
        self.unsubscribe_calls.append((camera_id, viewer_id))

    def viewer_count(self, camera_id):
        return self.viewer_count_return

    def apply_settings(self, camera_id, features):
        if self.apply_settings_exc is not None:
            raise self.apply_settings_exc
        return self.apply_settings_return


class _ViewerLimitBroadcaster:
    """Stateful stub that accepts subscribes up to ``max_viewers`` then rejects.

    Reproduces the broadcaster's viewer-limit contract (Req 1.3, 1.6) at the endpoint
    boundary so the test can assert the endpoint maps the Nth-accepted subscribe to 200
    and the (max+1)th rejected subscribe to 429.
    """

    def __init__(self, max_viewers=8):
        self.max_viewers = max_viewers
        self.count = 0

    def subscribe(self, camera_id, config=None):
        if self.count >= self.max_viewers:
            return SubscribeResult(
                viewer_id=None,
                accepted=False,
                reason="viewer_limit",
                viewer_count=self.count,
            )
        self.count += 1
        return SubscribeResult(
            viewer_id=f"viewer-{self.count}",
            accepted=True,
            reason=None,
            viewer_count=self.count,
        )


class TestStreamsApi(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()
        from app import app

        # raise_server_exceptions=False routes errors through the app's exception
        # handlers (matching the other api-endpoints tests) rather than re-raising.
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.stub = _StubBroadcaster()
        self._patcher = patch("endpoints.streams.get_broadcaster", return_value=self.stub)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        super().tearDown()

    # --- subscribe -------------------------------------------------------

    def test_subscribe_accepted_returns_200_with_viewer_id_and_count(self):
        self.stub.subscribe_result = SubscribeResult(
            viewer_id=TEST_VIEWER_ID, accepted=True, reason=None, viewer_count=3
        )
        response = self.client.post(f"/streams/{TEST_CAMERA_ID}/subscribe", json={})
        assert response.status_code == 200, f"status_code: {response.status_code}"
        body = response.json()
        assert body["viewerId"] == TEST_VIEWER_ID
        assert body["viewerCount"] == 3

    def test_subscribe_viewer_limit_returns_429(self):
        self.stub.subscribe_result = SubscribeResult(
            viewer_id=None, accepted=False, reason="viewer_limit", viewer_count=8
        )
        response = self.client.post(f"/streams/{TEST_CAMERA_ID}/subscribe", json={})
        assert response.status_code == 429, f"status_code: {response.status_code}"

    def test_subscribe_camera_unavailable_returns_503(self):
        self.stub.subscribe_result = SubscribeResult(
            viewer_id=None, accepted=False, reason="camera_unavailable", viewer_count=0
        )
        response = self.client.post(f"/streams/{TEST_CAMERA_ID}/subscribe", json={})
        assert response.status_code == 503, f"status_code: {response.status_code}"

    def test_subscribe_viewer_limit_boundary_8th_accepted_9th_rejected(self):
        """8 subscribes accepted (200), the 9th rejected with 429 (Req 1.3, 1.6)."""
        limit_stub = _ViewerLimitBroadcaster(max_viewers=8)
        with patch("endpoints.streams.get_broadcaster", return_value=limit_stub):
            for i in range(1, 9):
                response = self.client.post(
                    f"/streams/{TEST_CAMERA_ID}/subscribe", json={}
                )
                assert response.status_code == 200, (
                    f"subscribe #{i} expected 200, got {response.status_code}"
                )
                assert response.json()["viewerCount"] == i

            # The 9th subscribe crosses the limit and must be rejected.
            ninth = self.client.post(f"/streams/{TEST_CAMERA_ID}/subscribe", json={})
            assert ninth.status_code == 429, (
                f"9th subscribe expected 429, got {ninth.status_code}"
            )

    # --- frame -----------------------------------------------------------

    def test_frame_ok_returns_200_bytes_with_ok_header(self):
        frame = _make_frame(seq=11, data=b"\xaa\xbb\xcc")
        self.stub.frame_result = FrameResult(status=FrameStatus.OK, frame=frame)
        response = self.client.get(
            f"/streams/{TEST_CAMERA_ID}/frame", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.content == b"\xaa\xbb\xcc"
        assert response.headers["X-Frame-Status"] == "ok"
        assert response.headers["X-Frame-Seq"] == "11"
        assert response.headers["X-Frame-Width"] == "4"
        assert response.headers["X-Frame-Height"] == "2"

    def test_frame_stale_returns_200_bytes_with_stale_header(self):
        frame = _make_frame(seq=12, data=b"\x10\x20")
        self.stub.frame_result = FrameResult(status=FrameStatus.STALE, frame=frame)
        response = self.client.get(
            f"/streams/{TEST_CAMERA_ID}/frame", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.content == b"\x10\x20"
        assert response.headers["X-Frame-Status"] == "stale"

    def test_frame_no_frame_returns_204(self):
        self.stub.frame_result = FrameResult(status=FrameStatus.NO_FRAME, frame=None)
        response = self.client.get(
            f"/streams/{TEST_CAMERA_ID}/frame", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 204, f"status_code: {response.status_code}"
        assert response.content == b""

    def test_frame_disconnected_returns_503(self):
        self.stub.frame_result = FrameResult(status=FrameStatus.DISCONNECTED, frame=None)
        response = self.client.get(
            f"/streams/{TEST_CAMERA_ID}/frame", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 503, f"status_code: {response.status_code}"

    def test_frame_requires_viewer_id_query_param(self):
        # viewerId is a required query parameter; omitting it is a 422 validation error.
        response = self.client.get(f"/streams/{TEST_CAMERA_ID}/frame")
        assert response.status_code == 422, f"status_code: {response.status_code}"

    # --- heartbeat -------------------------------------------------------

    def test_heartbeat_refreshed_returns_200(self):
        self.stub.heartbeat_return = True
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/heartbeat", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.json()["refreshed"] is True

    def test_heartbeat_unknown_viewer_returns_404(self):
        self.stub.heartbeat_return = False
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/heartbeat", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 404, f"status_code: {response.status_code}"

    # --- unsubscribe -----------------------------------------------------

    def test_unsubscribe_returns_200_with_viewer_count(self):
        self.stub.viewer_count_return = 2
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/unsubscribe", params={"viewerId": TEST_VIEWER_ID}
        )
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.json()["viewerCount"] == 2
        assert (TEST_CAMERA_ID, TEST_VIEWER_ID) in self.stub.unsubscribe_calls

    # --- viewers ---------------------------------------------------------

    def test_viewers_returns_200_with_viewer_count(self):
        self.stub.viewer_count_return = 5
        response = self.client.get(f"/streams/{TEST_CAMERA_ID}/viewers")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.json()["viewerCount"] == 5

    # --- settings --------------------------------------------------------

    def test_settings_accepted_returns_200_dict(self):
        self.stub.apply_settings_return = {"gain": 10, "exposure": 4000}
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/settings", json={"gain": 10, "exposure": 4000}
        )
        assert response.status_code == 200, f"status_code: {response.status_code}"
        assert response.json() == {"gain": 10, "exposure": 4000}

    def test_settings_no_active_session_returns_409(self):
        self.stub.apply_settings_exc = NoActiveSessionError(
            f"no active stream session for camera {TEST_CAMERA_ID}"
        )
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/settings", json={"gain": 10}
        )
        assert response.status_code == 409, f"status_code: {response.status_code}"

    def test_settings_apply_error_returns_422_naming_control(self):
        self.stub.apply_settings_exc = SettingsApplyError(
            "failed to apply control(s) [gain]",
            camera_id=TEST_CAMERA_ID,
            control="gain",
            retained={"gain": 5},
        )
        response = self.client.post(
            f"/streams/{TEST_CAMERA_ID}/settings", json={"gain": 999}
        )
        assert response.status_code == 422, f"status_code: {response.status_code}"
        # The 422 detail names the failed control so the client can surface it.
        assert "gain" in response.json()["detail"]
