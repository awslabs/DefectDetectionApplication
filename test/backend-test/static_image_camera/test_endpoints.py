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
"""Example-based endpoint tests for the Pin_API (task 3.3).

Feature: static-image-camera-source (Requirements 1.1, 1.3, 1.4, 1.5,
1.6, 1.7, 1.8, 5.1, 5.4, 5.5).

Exercised through FastAPI's TestClient against a minimal app carrying
only the static-image-camera router (same pattern as the local_auth
endpoint tests). The real StaticImageStore runs underneath over a
tmp_path directory — only the store singleton, the captured-image roots,
and (for the oversize case) the size limit are injected.
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import endpoints.static_image_camera as pin_endpoints
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)


def make_image_bytes(img_format="PNG", width=8, height=6, color=(10, 200, 30)):
    """Encode a solid-color image of the given format/dimensions."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=img_format)
    return buffer.getvalue()


class _ClientAddressInjector:
    """ASGI wrapper setting scope['client'] when the test client leaves it
    unset — AccessLogRoute's request logging dereferences request.client,
    which this starlette version's TestClient does not populate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


def _make_client():
    app = FastAPI()
    app.include_router(pin_endpoints.router)
    return TestClient(_ClientAddressInjector(app))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Real store over tmp_path, installed as the endpoint module's
    singleton; captures root pointed at tmp_path/captures."""
    test_store = StaticImageStore(base_dir=str(tmp_path / "store"))
    monkeypatch.setattr(pin_endpoints, "get_store", lambda: test_store)
    captures_root = tmp_path / "captures"
    captures_root.mkdir()
    monkeypatch.setattr(
        pin_endpoints, "CAPTURED_IMAGE_ROOTS", (str(captures_root),)
    )
    test_store.captures_root_for_test = captures_root
    return test_store


@pytest.fixture
def client(store):
    return _make_client()


def upload(client, data, file_name="sample.png"):
    return client.post(
        "/static-image-camera/pin",
        files={"file": (file_name, data, "application/octet-stream")},
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_pin_upload_returns_camera_id_and_metadata(client):
    """Req 1.1, 1.5: pin upload → 200 {cameraId, metadata}."""
    data = make_image_bytes("PNG", width=8, height=6)
    response = upload(client, data, file_name="part_sample.png")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cameraId"] == STATIC_IMAGE_CAMERA_ID
    metadata = body["metadata"]
    assert metadata["fileName"] == "part_sample.png"
    assert metadata["format"] == "PNG"
    assert metadata["width"] == 8
    assert metadata["height"] == 6
    assert metadata["fileSizeBytes"] == len(data)


def test_status_without_pin(client):
    """Req 1.6: status reports no pin and no metadata."""
    response = client.get("/static-image-camera/pin")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pinned"] is False
    assert body["cameraId"] == STATIC_IMAGE_CAMERA_ID
    assert body["metadata"] is None


def test_status_with_pin(client):
    """Req 1.6: status reports the pin with its metadata."""
    data = make_image_bytes("BMP", width=5, height=7)
    assert upload(client, data, file_name="ref.bmp").status_code == 200
    response = client.get("/static-image-camera/pin")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pinned"] is True
    assert body["metadata"]["fileName"] == "ref.bmp"
    assert body["metadata"]["format"] == "BMP"
    assert body["metadata"]["width"] == 5
    assert body["metadata"]["height"] == 7


def test_unpin_after_pin(client):
    """Req 5.4: unpin succeeds and status returns to not pinned."""
    assert upload(client, make_image_bytes()).status_code == 200
    response = client.delete("/static-image-camera/pin")
    assert response.status_code == 200, response.text
    assert response.json()["pinned"] is False
    assert client.get("/static-image-camera/pin").json()["pinned"] is False


def test_replace_returns_success_and_new_metadata(client, store):
    """Req 5.1: pinning over an existing pin returns the success
    confirmation, and subsequent frames carry only the new content."""
    first = make_image_bytes("PNG", width=4, height=4, color=(255, 0, 0))
    second = make_image_bytes("JPEG", width=9, height=3, color=(0, 0, 255))
    assert upload(client, first, file_name="first.png").status_code == 200
    response = upload(client, second, file_name="second.jpg")
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["fileName"] == "second.jpg"
    assert metadata["format"] == "JPEG"
    assert metadata["width"] == 9
    assert metadata["height"] == 3
    frame = store.get_frame()
    assert (frame["width"], frame["height"]) == (9, 3)


def test_pin_from_capture(client, store):
    """Req 1.7: a captured image referenced by path pins successfully."""
    data = make_image_bytes("JPEG", width=6, height=6)
    capture_path = store.captures_root_for_test / "capture-0001.jpg"
    capture_path.write_bytes(data)
    response = client.post(
        "/static-image-camera/pin",
        json={"capturedImagePath": str(capture_path)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cameraId"] == STATIC_IMAGE_CAMERA_ID
    assert body["metadata"]["fileName"] == "capture-0001.jpg"
    assert body["metadata"]["format"] == "JPEG"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_undecodable_upload_names_supported_formats(client):
    """Req 1.3: undecodable upload → 400 naming JPEG, PNG, BMP; prior
    state (no pin) unchanged."""
    response = upload(client, b"this is not an image", file_name="junk.bin")
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    for fmt in ("JPEG", "PNG", "BMP"):
        assert fmt in detail
    assert client.get("/static-image-camera/pin").json()["pinned"] is False


def test_oversize_upload_names_limit(tmp_path, monkeypatch):
    """Req 1.4: upload above the (injected) size limit → 400 naming the
    limit; prior pin unchanged."""
    small_limit = 1024
    test_store = StaticImageStore(
        base_dir=str(tmp_path / "store"), max_file_bytes=small_limit
    )
    monkeypatch.setattr(pin_endpoints, "get_store", lambda: test_store)
    monkeypatch.setattr(pin_endpoints, "MAX_PIN_FILE_BYTES", small_limit)
    client = _make_client()

    tiny = make_image_bytes("PNG", width=2, height=2)
    assert len(tiny) <= small_limit
    assert upload(client, tiny, file_name="prior.png").status_code == 200

    big = make_image_bytes("BMP", width=64, height=64)
    assert len(big) > small_limit
    response = upload(client, big, file_name="big.bmp")
    assert response.status_code == 400, response.text
    assert str(small_limit) in response.json()["detail"]

    status = client.get("/static-image-camera/pin").json()
    assert status["pinned"] is True
    assert status["metadata"]["fileName"] == "prior.png"


def test_missing_capture_reference_not_found(client):
    """Req 1.8: reference to a nonexistent captured image → 400 not
    found; no pin created."""
    response = client.post(
        "/static-image-camera/pin",
        json={"capturedImagePath": str(
            pin_endpoints.CAPTURED_IMAGE_ROOTS[0] + "/does-not-exist.jpg"
        )},
    )
    assert response.status_code == 400, response.text
    assert "not found" in response.json()["detail"]
    assert client.get("/static-image-camera/pin").json()["pinned"] is False


def test_capture_path_escaping_root_rejected(client, tmp_path, store):
    """Path-traversal guard: a path resolving outside the captures root
    → 400; no pin created."""
    outside = tmp_path / "outside.png"
    outside.write_bytes(make_image_bytes())
    escaping = store.captures_root_for_test / ".." / "outside.png"
    for candidate in (str(outside), str(escaping)):
        response = client.post(
            "/static-image-camera/pin", json={"capturedImagePath": candidate}
        )
        assert response.status_code == 400, response.text
        assert "outside" in response.json()["detail"]
    assert client.get("/static-image-camera/pin").json()["pinned"] is False


def test_unpin_with_nothing_pinned(client):
    """Req 5.5: unpin with no pin → 400 "no image is pinned"."""
    response = client.delete("/static-image-camera/pin")
    assert response.status_code == 400, response.text
    assert "no image is pinned" in response.json()["detail"]
