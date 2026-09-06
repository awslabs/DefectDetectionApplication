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
"""Image_Source, preview, and capture wiring tests (task 7.2).

Feature: static-image-camera-source (Requirements 4.1, 4.3, 4.4, 4.6).

Exercised through the REAL ``endpoints/image_source.py`` routes over the
REAL ``ImageSourceAccessor`` / DAO layer (a private sqlite database) and
the REAL ``utils.camera_manager.get_camera_frame`` static short-circuit
over a real :class:`StaticImageStore` — the exact production wiring for
everything up to GStreamer.

Scoping (documented per the task): the full ``utils.server_setup`` module
cannot be constructed in a unit-test process (it spins up IPC clients,
sync agents, and the GStreamer executor at import), so this suite installs
a stub ``utils.server_setup`` module before importing the endpoint module
and swaps the endpoint's accessor/executor globals per test:

* ``image_source_accessor`` / ``image_src_cfg_accessor`` — REAL accessor
  instances over the test database (real CRUD, Req 4.1);
* ``gst_pipeline_executor`` — a Mock, exactly as the container-side
  ``api-endpoints/test_image_source_api.py`` suite mocks it. The pixels
  handed to it ARE asserted (the real Pinned_Image decode); the actual
  GStreamer encode/store of that frame is deferred to on-device
  verification (task 9.1), as it is for physical cameras.
"""
import io
import os
import sys
import types
from unittest.mock import Mock

import pytest
from PIL import Image

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_DEFAULT_CAMERA_CONFIG = os.path.join(
    _REPO_ROOT, "src", "backend", "utils", "config",
    "default_camera_configurations.json",
)

# utils.camera_manager first: forkserver-safe import (see the helper).
from camera_manager_support import import_camera_manager  # noqa: E402

camera_manager = import_camera_manager()

# Stub utils.server_setup BEFORE importing the endpoint module so the
# import never reaches the IPC/GStreamer construction chain. The stub only
# satisfies the ``from utils.server_setup import ...`` statement; every
# test swaps the endpoint module's globals for real accessors / a fresh
# executor mock.
import utils as _utils_package  # noqa: E402

if "utils.server_setup" not in sys.modules:
    _stub = types.ModuleType("utils.server_setup")
    _stub.gst_pipeline_executor = Mock(name="unwired gst_pipeline_executor")
    _stub.image_source_accessor = Mock(name="unwired image_source_accessor")
    _stub.image_src_cfg_accessor = Mock(name="unwired image_src_cfg_accessor")
    sys.modules["utils.server_setup"] = _stub
    _utils_package.server_setup = _stub

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import endpoints.image_source as image_source_endpoints  # noqa: E402
import dao.sqlite_db.models as db_models  # noqa: E402
from dao.sqlite_db.sqlite_db_operations import Base  # noqa: E402
from edge_ml1_p_camera_management import aravis_functions  # noqa: E402
from utils import constants, dda_user_management_utils  # noqa: E402
from utils.static_image_camera import (  # noqa: E402
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)

from static_image_strategies import expected_frame  # noqa: E402


def make_image_bytes(img_format="PNG", width=7, height=5, color=(200, 40, 10)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=img_format)
    return buffer.getvalue()


class _ClientAddressInjector:
    """ASGI wrapper setting scope['client'] when the test client leaves it
    unset — AccessLogRoute's request logging dereferences request.client."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def store(tmp_path):
    """Real store over tmp_path with a known pinned image."""
    test_store = StaticImageStore(base_dir=str(tmp_path / "store"))
    test_store.image_bytes_for_test = make_image_bytes()
    test_store.pin_bytes(test_store.image_bytes_for_test, "wiring.png")
    return test_store


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        "sqlite:///{}".format(tmp_path / "wiring.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def gst_executor(monkeypatch):
    """The mocked GStreamer executor (same seam the container-side
    api-endpoints tests mock)."""
    executor = Mock(name="gst_pipeline_executor")
    executor.execute_image_source_pipeline.return_value = {
        "image": "base64-image-stub"
    }
    monkeypatch.setattr(
        image_source_endpoints, "gst_pipeline_executor", executor
    )
    return executor


@pytest.fixture
def client(monkeypatch, tmp_path, store, db_session, gst_executor):
    """TestClient over the real router, real accessors, real DB, real
    camera-manager short-circuits, mocked GStreamer executor."""
    # Environment scaffolding: config file path (absolute), a tmp capture
    # root, and directory creation without the dda-admin chown (host/CI has
    # no dda user; the real function chowns every parent directory).
    monkeypatch.setattr(
        constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH", _DEFAULT_CAMERA_CONFIG
    )
    monkeypatch.setattr(
        constants, "IMAGE_CAPTURE_DIR", str(tmp_path / "image-capture")
    )
    monkeypatch.setattr(
        dda_user_management_utils,
        "create_dda_user_directory",
        lambda folder_path: (os.makedirs(folder_path, exist_ok=True),
                             folder_path)[1],
    )

    # Wire the test store into BOTH providers: enumeration/getCamera and
    # the camera-manager grab/status/connect short-circuits.
    monkeypatch.setattr(aravis_functions, "get_store", lambda: store)
    monkeypatch.setattr(
        camera_manager, "get_static_image_store", lambda: store
    )

    # REAL accessors over the test database.
    from resources.accessors.image_source_accessor import ImageSourceAccessor

    accessor = ImageSourceAccessor()
    monkeypatch.setattr(
        image_source_endpoints, "image_source_accessor", accessor
    )
    monkeypatch.setattr(
        image_source_endpoints,
        "image_src_cfg_accessor",
        accessor.image_source_config_accessor,
    )

    app = FastAPI()
    app.include_router(image_source_endpoints.router)
    app.dependency_overrides[image_source_endpoints.get_db] = (
        lambda: db_session
    )
    return TestClient(_ClientAddressInjector(app))


def add_static_image_source(client, name="Static bench"):
    response = client.post(
        "/image-sources",
        json={"type": "Camera", "name": name,
              "cameraId": STATIC_IMAGE_CAMERA_ID},
    )
    assert response.status_code == 200, response.text
    return response.json()["imageSourceId"]


# ---------------------------------------------------------------------------
# Req 4.1 — Image_Source CRUD accepts the static cameraId like a physical one
# ---------------------------------------------------------------------------


def test_add_image_source_with_static_camera_persists_record(
    client, db_session
):
    """POST /image-sources with the static cameraId goes through the same
    interface as physical cameras and persists a retrievable record."""
    image_source_id = add_static_image_source(client)

    listed = client.get("/image-sources")
    assert listed.status_code == 200, listed.text
    records = {entry["imageSourceId"]: entry for entry in listed.json()}
    record = records[image_source_id]
    assert record["cameraId"] == STATIC_IMAGE_CAMERA_ID
    assert record["type"] == "Camera"
    # The camera status flows through the REAL get_camera_status
    # short-circuit: pinned == Connected.
    assert record["cameraStatus"]["status"] == "Connected"

    # The record persisted with a default ImageSourceConfiguration created
    # through the same path physical cameras use (getCamera vendor/model
    # lookup resolving to the "default" config).
    row = db_session.get(db_models.ImageSource, image_source_id)
    assert row.cameraId == STATIC_IMAGE_CAMERA_ID
    assert row.imageSourceConfigId
    assert row.imageSourceConfiguration.processingPipeline


def test_update_configuration_uses_same_fields_as_physical_cameras(
    client, db_session
):
    """PATCH accepts the same ImageSourceConfiguration fields (gain,
    exposure, processing pipeline, advanced settings) as physical cameras
    and persists them; subsequent queries return the record."""
    image_source_id = add_static_image_source(client)
    configuration = {
        "gain": 10,
        "exposure": 4000,
        "processingPipeline": (
            "appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert"
        ),
        "advancedSettings": {"reverseX": True},
    }

    response = client.patch(
        "/image-sources/{}".format(image_source_id),
        json={"imageSourceConfiguration": configuration},
    )
    assert response.status_code == 200, response.text

    row = db_session.get(db_models.ImageSource, image_source_id)
    persisted = row.imageSourceConfiguration
    assert persisted.gain == 10
    assert persisted.exposure == 4000
    assert persisted.processingPipeline == configuration["processingPipeline"]
    assert persisted.advancedSettings == {"reverseX": True}

    # The configuration is retrievable through the same query interface
    # physical camera configurations use.
    configs = client.get("/image-source-configurations")
    assert configs.status_code == 200, configs.text
    assert any(
        entry["imageSourceConfigId"] == row.imageSourceConfigId
        for entry in configs.json()
    )


# ---------------------------------------------------------------------------
# Req 4.3 / 4.4 — preview and capture flow the Pinned_Image through the
# existing paths
# ---------------------------------------------------------------------------


def test_preview_serves_pinned_image_through_existing_path(
    client, store, gst_executor
):
    """POST /image-sources/{id}/preview reaches the same preview interface
    as physical cameras; the frame handed to the pipeline IS the decoded
    Pinned_Image."""
    image_source_id = add_static_image_source(client)

    response = client.post(
        "/image-sources/{}/preview".format(image_source_id), json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["image"] == "base64-image-stub"

    gst_executor.execute_image_source_pipeline.assert_called_once()
    _, kwargs = gst_executor.execute_image_source_pipeline.call_args
    assert kwargs["is_preview"] is True
    # The frame pushed through the existing Frame path is the Pinned_Image
    # decode, byte for byte, tagged RGB.
    assert kwargs["frame_data"] == expected_frame(store.image_bytes_for_test)
    image_source_arg = gst_executor.execute_image_source_pipeline.call_args[0][0]
    assert image_source_arg.cameraId == STATIC_IMAGE_CAMERA_ID


def test_preview_accepts_configuration_override_like_physical(
    client, store, gst_executor
):
    """A preview override configuration (the physical-camera contract) is
    accepted; the static grab ignores acquisition settings so the frame is
    unchanged (Req 3.4 through the preview path)."""
    image_source_id = add_static_image_source(client)
    override = {
        "gain": 42,
        "exposure": 100000,
        "processingPipeline": (
            "appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert"
        ),
    }

    response = client.post(
        "/image-sources/{}/preview".format(image_source_id),
        json={"imageSourceConfiguration": override},
    )
    assert response.status_code == 200, response.text

    _, kwargs = gst_executor.execute_image_source_pipeline.call_args
    assert kwargs["image_source_config_override"] == override
    assert kwargs["frame_data"] == expected_frame(store.image_bytes_for_test)


def test_capture_stores_pinned_image_through_existing_path(
    client, store, gst_executor
):
    """POST /image-sources/{id}/capture goes through the same capture path
    as physical cameras with the Pinned_Image as the frame."""
    image_source_id = add_static_image_source(client)
    gst_executor.execute_image_source_pipeline.return_value = {
        "image": "captures/static-0001.jpg"
    }

    response = client.post(
        "/image-sources/{}/capture".format(image_source_id), json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["image"] == "captures/static-0001.jpg"

    gst_executor.execute_image_source_pipeline.assert_called_once()
    _, kwargs = gst_executor.execute_image_source_pipeline.call_args
    assert kwargs["is_preview"] is False
    assert kwargs["frame_data"] == expected_frame(store.image_bytes_for_test)


# ---------------------------------------------------------------------------
# Req 4.6 — preview/capture with no Pinned_Image fail descriptively and
# store nothing
# ---------------------------------------------------------------------------


def test_preview_without_pin_fails_naming_the_camera(
    client, store, gst_executor
):
    """Preview with no Pinned_Image fails with a descriptive error naming
    the Static_Image_Camera; the pipeline (and thus any image) is never
    produced."""
    image_source_id = add_static_image_source(client)
    store.unpin()

    response = client.post(
        "/image-sources/{}/preview".format(image_source_id), json={}
    )
    assert response.status_code == 500, response.text
    detail = response.json()["detail"]
    assert STATIC_IMAGE_CAMERA_ID in detail
    assert "no usable pinned image" in detail
    gst_executor.execute_image_source_pipeline.assert_not_called()


def test_capture_without_pin_fails_and_stores_no_image(
    client, store, gst_executor, tmp_path
):
    """Capture with no Pinned_Image fails with the error naming the
    Static_Image_Camera and stores no captured image."""
    image_source_id = add_static_image_source(client)
    capture_dir = os.path.join(
        str(tmp_path / "image-capture"), image_source_id
    )
    store.unpin()

    # The capture route has no local handler for grab failures — the
    # error propagates to the app-level exception handler (a 500 in the
    # production app). TestClient surfaces it directly.
    with pytest.raises(Exception) as excinfo:
        client.post(
            "/image-sources/{}/capture".format(image_source_id), json={}
        )
    message = str(excinfo.value)
    assert STATIC_IMAGE_CAMERA_ID in message
    assert "no usable pinned image" in message

    gst_executor.execute_image_source_pipeline.assert_not_called()
    assert os.listdir(capture_dir) == []
