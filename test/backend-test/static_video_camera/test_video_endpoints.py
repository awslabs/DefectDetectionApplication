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
"""Example-based endpoint tests for the Video_Pin_API.

Feature: static-camera-video-loop (Requirements 1.1–1.9, 5.3, 5.4, 6.2).
FastAPI TestClient against a minimal app carrying the video and image pin
routers; real stores over ``tmp_path`` are installed as the modules'
singletons, and the captured-image roots point at ``tmp_path/captures``.
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import camera_sync.hooks as camera_sync_hooks
import endpoints.static_image_camera as image_endpoints
import endpoints.static_video_camera as video_endpoints
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID, StaticImageStore
from utils.static_video_camera import STATIC_VIDEO_CAMERA_ID, StaticVideoStore


class _ClientAddressInjector:
    """Sets scope['client'] when the TestClient leaves it unset
    (AccessLogRoute dereferences request.client)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    video_store = StaticVideoStore(base_dir=str(tmp_path / "video"))
    image_store = StaticImageStore(base_dir=str(tmp_path / "image"))
    monkeypatch.setattr(video_endpoints, "get_store", lambda: video_store)
    monkeypatch.setattr(image_endpoints, "get_store", lambda: image_store)
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(image_endpoints, "CAPTURED_IMAGE_ROOTS", (str(captures),))
    return video_store, image_store, captures


@pytest.fixture
def client(stores):
    app = FastAPI()
    app.include_router(video_endpoints.router)
    app.include_router(image_endpoints.router)
    return TestClient(_ClientAddressInjector(app))


def _install_agent(agent):
    """Register ``agent`` as the active Edge_Sync_Agent; returns the
    previous one so the caller can restore it."""
    previous = camera_sync_hooks.get_active_agent()
    camera_sync_hooks.set_active_agent(agent)
    return previous


@pytest.fixture
def report_requests():
    """Inventory-report requests reaching the active agent through the real
    ``camera_sync.hooks`` path."""
    calls = []

    class _RecordingAgent:
        def report_inventory(self):
            calls.append("report_inventory")

    previous = _install_agent(_RecordingAgent())
    yield calls
    camera_sync_hooks.set_active_agent(previous)


def _upload(client, data, file_name="scene.mp4", route="/static-video-camera/pin"):
    return client.post(route, files={"file": (file_name, data, "application/octet-stream")})


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (6, 4), (200, 10, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_pin_status_and_unpin(client, clip_library):
    clip = clip_library.get("h264.mp4") or clip_library.decodable()[0]
    response = _upload(client, clip.data, "conveyor.mp4")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cameraId"] == STATIC_VIDEO_CAMERA_ID
    metadata = body["metadata"]
    assert metadata["fileName"] == "conveyor.mp4"
    assert metadata["format"] == clip.container
    assert metadata["codec"] == clip.codec
    assert (metadata["width"], metadata["height"]) == (clip.width, clip.height)
    assert metadata["frameCount"] == clip.frame_count
    assert metadata["fileSizeBytes"] == len(clip.data)

    status = client.get("/static-video-camera/pin").json()
    assert status == {"pinned": True, "cameraId": STATIC_VIDEO_CAMERA_ID,
                      "metadata": metadata}

    response = client.delete("/static-video-camera/pin")
    assert response.status_code == 200
    assert response.json() == {"cameraId": STATIC_VIDEO_CAMERA_ID, "pinned": False}
    assert client.get("/static-video-camera/pin").json()["pinned"] is False


def test_unpin_with_nothing_pinned(client):
    response = client.delete("/static-video-camera/pin")
    assert response.status_code == 400
    assert "no video is pinned" in response.json()["detail"]


def test_non_video_upload_lists_containers(client):
    response = _upload(client, _png(), "photo.png")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not a supported video" in detail
    for container in ("MP4", "MOV", "AVI", "MKV", "WEBM"):
        assert container in detail
    assert client.get("/static-video-camera/pin").json()["pinned"] is False


def test_av1_upload_names_the_codec(client, clip_library):
    clip = clip_library.get("av1.mkv")
    if clip is None:
        pytest.skip("this image's ffmpeg has no AV1 encoder")
    response = _upload(client, clip.data, "av1.mkv")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "could not be decoded" in detail and "AV1" in detail


def test_oversize_upload_names_the_limit(client, stores, clip_library, monkeypatch):
    video_store, _, _ = stores
    clip = clip_library.decodable()[0]
    monkeypatch.setattr(video_store, "_max_file_bytes", len(clip.data) - 1)
    response = _upload(client, clip.data)
    assert response.status_code == 400
    assert str(len(clip.data) - 1) in response.json()["detail"]


def test_declared_body_over_the_cap_is_rejected_before_reading(client, monkeypatch):
    monkeypatch.setattr(video_endpoints, "MAX_PIN_VIDEO_BYTES", 10)
    response = _upload(client, b"x" * 200_000, "big.mp4")
    assert response.status_code == 400
    assert "exceeds the maximum accepted video file size of 10 bytes" in \
        response.json()["detail"]


def test_multipart_without_file_part(client):
    response = client.post("/static-video-camera/pin",
                           files={"other": ("a.mp4", b"abc", "application/octet-stream")})
    assert response.status_code == 400
    assert "carry the video in a form part named 'file'" in response.json()["detail"]


def test_pin_by_reference_and_traversal_guard(client, stores, clip_library, tmp_path):
    _, _, captures = stores
    clip = clip_library.decodable()[0]
    inside = captures / "clip.mp4"
    inside.write_bytes(clip.data)
    response = client.post("/static-video-camera/pin",
                           json={"capturedImagePath": str(inside)})
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["fileName"] == "clip.mp4"

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(clip.data)
    response = client.post("/static-video-camera/pin",
                           json={"capturedImagePath": str(captures / ".." / "outside.mp4")})
    assert response.status_code == 400
    assert "outside the captured images directory" in response.json()["detail"]

    response = client.post("/static-video-camera/pin",
                           json={"capturedImagePath": str(captures / "missing.mp4")})
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_both_cameras_pin_independently(client, clip_library):
    """Requirement 6.2: operations on one camera leave the other unchanged."""
    clip = clip_library.decodable()[0]
    assert _upload(client, _png(), "photo.png",
                   route="/static-image-camera/pin").status_code == 200
    assert _upload(client, clip.data).status_code == 200
    image_status = client.get("/static-image-camera/pin").json()
    assert image_status["pinned"] is True
    assert image_status["cameraId"] == STATIC_IMAGE_CAMERA_ID

    assert client.delete("/static-video-camera/pin").status_code == 200
    assert client.get("/static-image-camera/pin").json() == image_status

    assert _upload(client, clip.data).status_code == 200
    video_status = client.get("/static-video-camera/pin").json()
    assert client.delete("/static-image-camera/pin").status_code == 200
    assert client.get("/static-video-camera/pin").json() == video_status


def test_each_successful_pin_change_requests_one_inventory_report(
        client, stores, clip_library, report_requests):
    """Requirement 4.6: a pin, a replace, a pin by reference and an unpin
    each request exactly one camera-registry report. A replace keeps the
    camera's enumeration unchanged, so this request is what reports the new
    Video_Metadata."""
    _, _, captures = stores
    clip = clip_library.decodable()[0]

    assert _upload(client, clip.data, "first.mp4").status_code == 200
    assert report_requests == ["report_inventory"]

    assert _upload(client, clip.data, "replacement.mp4").status_code == 200
    assert len(report_requests) == 2

    referenced = captures / "referenced.mp4"
    referenced.write_bytes(clip.data)
    response = client.post("/static-video-camera/pin",
                           json={"capturedImagePath": str(referenced)})
    assert response.status_code == 200, response.text
    assert len(report_requests) == 3

    assert client.delete("/static-video-camera/pin").status_code == 200
    assert len(report_requests) == 4


def test_rejected_pin_requests_request_no_inventory_report(
        client, stores, report_requests):
    """A request that changes nothing requests no report."""
    _, _, captures = stores
    assert _upload(client, _png(), "photo.png").status_code == 400
    assert client.delete("/static-video-camera/pin").status_code == 400
    outside = str(captures / ".." / "outside.mp4")
    assert client.post("/static-video-camera/pin",
                       json={"capturedImagePath": outside}).status_code == 400
    assert client.post("/static-video-camera/pin", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert report_requests == []


def test_a_failing_report_request_does_not_fail_the_pin(client, clip_library):
    """The hook isolates the route from the sync agent: a broken agent never
    fails the pin API call that triggered the report request."""

    class _BrokenAgent:
        def report_inventory(self):
            raise RuntimeError("camera-registry shadow unavailable")

    previous = _install_agent(_BrokenAgent())
    try:
        clip = clip_library.decodable()[0]
        assert _upload(client, clip.data).status_code == 200
        assert client.get("/static-video-camera/pin").json()["pinned"] is True
        assert client.delete("/static-video-camera/pin").status_code == 200
    finally:
        camera_sync_hooks.set_active_agent(previous)
