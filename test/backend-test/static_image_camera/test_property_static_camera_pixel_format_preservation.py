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
"""Preservation property tests for Defect A (spec
``static-camera-pixel-format-and-detection-results``, task 2).

**Property 2: Preservation Checking (Defect A)** — every physical
vendor's and model's conversion chain, the ``default``/``default`` Bayer
fall-through, the Static_Image_Camera store's packed-RGB frame contract,
the workflow Frame_Feed path, the PFNC map, enumeration, the shipped
identity, and the scoping of the new backfill.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

THESE TESTS PASS ON UNFIXED CODE. That is the point: they pin the
baseline the fix must not regress. Every recorded value below was
OBSERVED by running the UNFIXED code in the flask-app x86 container
before it was written down — none of it was inferred from reading the
source alone. They are re-run unchanged in task 5.4.

What "unchanged" means here
---------------------------
The fix (task 5.1) ADDS one top-level ``AWS-DDA`` entry to
``src/backend/utils/config/default_camera_configurations.json`` and
NOTHING else. So this suite asserts, exactly as Property 2 states:

* every RECORDED (vendor, model) pair still resolves byte-for-byte —
  including the recorded set of model sub-keys per recorded vendor, so a
  removed or renamed model fails here;
* every top-level key in the POST-fix file (recorded or new) carries a
  ``default`` sub-key, because the lookup does
  ``.get(vendor).get(model).get("processingPipeline")`` and a vendor
  without one raises on ``None.get(...)`` (Requirement 3.2);
* the top-level key SET is deliberately NOT pinned — task 5.1 adds a key
  on purpose. Task 1's suite is what pins the added key against
  ``STATIC_IMAGE_CAMERA_IDENTITY``.

The Frame_Feed path is a "verify and preserve", not a fix
--------------------------------------------------------
``workflow_engine.pipeline_executor.WorkflowExecutor._frame_caps``
(``pipeline_executor.py`` ~line 2821) already derives appsrc caps from
the frame's own ``pixel_format`` tag, and the store already tags every
frame ``"RGB"``, so the newer workflow Frame_Feed path is ALREADY CORRECT
for the static camera (bugfix.md 3.4). This file records that mapping and
asserts it unchanged. The existing assertion
``"appsrc name=appsrc caps=video/x-raw,format=RGB "`` in
``test/backend-test/static_image_camera/test_workflow_feed.py`` documents
the same thing end to end and MUST keep passing untouched.

Backfill scoping tests SKIP until task 5.2 lands
------------------------------------------------
``dao.sqlite_db.db_backfill.migration_static_camera_pipeline_db`` DOES
NOT EXIST YET — observed on unfixed code, where
``db_backfill`` exposes exactly
``['migration_cleanup_imgsrc_db', 'migration_cleanup_workflow_db']``.
Task 5.2 adds it; task 1 pins the name in its
``BACKFILL_FUNCTION_NAME`` constant and this file reuses the same name.
The scoping assertions therefore ``pytest.skip`` while the function is
absent — which is what lets this whole file PASS on unfixed code as task
2 requires — and start asserting the moment 5.2 lands, with no edit to
this file.

Existing suites are preservation coverage too
---------------------------------------------
Everything under ``test/backend-test/static_image_camera/`` is itself
preservation coverage for Defect A and must keep passing untouched. In
particular:

* ``test_image_source_wiring.py`` — its
  ``assert row.imageSourceConfiguration.processingPipeline`` is a
  TRUTHINESS check, so it stays valid across the chain change; it is also
  the harness this file (and task 1's) reuses;
* ``test_workflow_feed.py`` — the end-to-end RGB appsrc caps assertion
  described above;
* ``test_property_grab_determinism.py`` — grab determinism and
  acquisition-config invariance (this file asserts the FORMAT contract
  those tests do not pin);
* ``test_camera_manager_short_circuit.py``, ``test_property_enumeration.py``,
  ``test_property_identifier.py`` — the short-circuit, enumeration, and
  identity behaviors Requirements 3.6 and 3.7 hold unchanged.

Harness
-------
Reuses task 1's harness verbatim in shape: the REAL ``ImageSourceAccessor``
over a PRIVATE sqlite database via a context manager (Hypothesis rejects
function-scoped fixtures), ``constants.DEFAULT_CAMERA_CONFIG_FILE_PATH``
pointed at the REAL shipped
``src/backend/utils/config/default_camera_configurations.json``, the real
``StaticImageStore`` installed behind both ``aravis_functions.get_store``
and ``camera_manager.get_static_image_store``, the forkserver-safe
``import_camera_manager`` from ``camera_manager_support``, and
``image_specs`` / ``render_image_bytes`` from ``static_image_strategies``.
``bayer_default_chain()`` reads ``default``/``default`` FROM THE FILE
rather than hardcoding it, so the two halves of Defect A cannot drift.

The repo's ``utils.*`` modules are imported at MODULE level (as every
existing suite does) so the repo copies are pinned in ``sys.modules``
before the flask-app image's baked-in backend copy at ``/`` can win; a
lazy in-test import dies with
``ModuleNotFoundError: No module named 'dda_triton.provider_visibility'``.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
from unittest.mock import Mock, patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from PIL import Image, ImageOps

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))

#: The REAL shipped configuration file — never a fixture copy.
DEFAULT_CAMERA_CONFIG_PATH = os.path.join(
    _REPO_ROOT, "src", "backend", "utils", "config",
    "default_camera_configurations.json",
)

# utils.camera_manager first: forkserver-safe import (see the helper).
from camera_manager_support import import_camera_manager  # noqa: E402

camera_manager = import_camera_manager()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import dao.sqlite_db.models as db_models  # noqa: E402
from dao.sqlite_db import image_source_dao  # noqa: E402
from dao.sqlite_db.sqlite_db_operations import Base  # noqa: E402
from edge_ml1_p_camera_management import aravis_functions  # noqa: E402
from model.image_source import ImageSourceType  # noqa: E402
from utils import constants, dda_user_management_utils, utils  # noqa: E402
from utils.static_image_camera import (  # noqa: E402
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
    StaticImageStore,
)
from workflow_engine.pipeline_executor import WorkflowExecutor  # noqa: E402

from static_image_strategies import (  # noqa: E402
    FakeAravisBus,
    camera_fields,
    expected_frame,
    image_specs,
    physical_camera_lists,
    physical_fields,
    render_image_bytes,
)

# ===========================================================================
# RECORDED BASELINE — observed on UNFIXED code in the flask-app container
# ===========================================================================

#: The four distinct conversion chains the shipped file uses, recorded
#: character-for-character.
RGB_CHAIN = "capsfilter caps=video/x-raw,format=RGB ! videoconvert"
GRAY8_CHAIN = "capsfilter caps=video/x-raw,format=GRAY8 ! videoconvert"
BAYER_RGGB_CHAIN = (
    "capsfilter caps=video/x-bayer,format=rggb ! bayer2rgb"
    " ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
)
BAYER_BGGR_CHAIN = (
    "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb"
    " ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
)
NVIDIA_CSI_CHAIN = (
    "video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1"
    " ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
)
ICAM_CHAIN = "videoconvert"

#: The loaded ``default_camera_configurations.json`` IN FULL — all eight
#: recorded top-level keys, every model sub-key under each, and every
#: ``processingPipeline`` / ``device`` / ``deviceName`` / ``gain`` /
#: ``exposure`` value (Requirement 3.1). ``device`` / ``deviceName`` /
#: ``gain`` / ``exposure`` appear ONLY under ``Nvidia CSI`` and ``ICAM``,
#: which short-circuit before the vendor lookup.
RECORDED_CONFIG = {
    "Lucid Vision Labs": {
        "default": {"processingPipeline": RGB_CHAIN},
        "TRI050S-C": {"processingPipeline": RGB_CHAIN},
    },
    "Zebra Technologies": {
        "default": {"processingPipeline": BAYER_RGGB_CHAIN},
        "CV60-AS05CG": {"processingPipeline": BAYER_RGGB_CHAIN},
        "CV60-AS05MG": {"processingPipeline": GRAY8_CHAIN},
        "GOX5103C-PGE": {"processingPipeline": BAYER_RGGB_CHAIN},
        "GOX-5103M-PGE": {"processingPipeline": GRAY8_CHAIN},
        "GOX2402M-PGE": {"processingPipeline": GRAY8_CHAIN},
        "GOX2402C-PGE": {"processingPipeline": BAYER_RGGB_CHAIN},
    },
    "Basler": {
        "default": {"processingPipeline": BAYER_BGGR_CHAIN},
        "acA-640-120gm": {"processingPipeline": GRAY8_CHAIN},
        "acA1300-30gc": {"processingPipeline": BAYER_BGGR_CHAIN},
        "acA1300-60gm": {"processingPipeline": GRAY8_CHAIN},
        "acA2040-55um": {"processingPipeline": GRAY8_CHAIN},
        "acA2440-35uc": {"processingPipeline": BAYER_BGGR_CHAIN},
    },
    "Allied Vision": {
        "default": {"processingPipeline": RGB_CHAIN},
        "ALVIUM 1800 U-500m": {"processingPipeline": GRAY8_CHAIN},
    },
    "OMRON SENTECH": {
        "default": {"processingPipeline": RGB_CHAIN},
        "STC-MCS312POE": {"processingPipeline": RGB_CHAIN},
    },
    "Nvidia CSI": {
        "default": {
            "processingPipeline": NVIDIA_CSI_CHAIN,
            "device": "0",
            "deviceName": "nvarguscamerasrc",
            "gain": 4,
            "exposure": 5000000,
        },
    },
    "ICAM": {
        "default": {
            "processingPipeline": ICAM_CHAIN,
            "device": "/dev/video0",
            "deviceName": "v4l2src",
        },
    },
    "default": {
        "default": {"processingPipeline": BAYER_BGGR_CHAIN},
    },
}

#: The recorded top-level keys IN FILE ORDER (bugfix.md 3.1). The fix adds
#: ``AWS-DDA`` after these, so the assertion is "these eight are all still
#: present, in this relative order" rather than "the key set is exactly
#: these eight".
RECORDED_TOP_LEVEL_KEYS = [
    "Lucid Vision Labs", "Zebra Technologies", "Basler", "Allied Vision",
    "OMRON SENTECH", "Nvidia CSI", "ICAM", "default",
]

#: The RGB-native physical vendors whose ``default`` is the RGB chain —
#: the same chain task 5.1 gives the static camera (bugfix.md 2.1, 3.1).
RGB_NATIVE_VENDORS = ("Lucid Vision Labs", "Allied Vision", "OMRON SENTECH")

#: The GRAY8 model variants, recorded as (vendor, model) pairs.
GRAY8_MODEL_VARIANTS = (
    ("Zebra Technologies", "CV60-AS05MG"),
    ("Zebra Technologies", "GOX-5103M-PGE"),
    ("Zebra Technologies", "GOX2402M-PGE"),
    ("Basler", "acA-640-120gm"),
    ("Basler", "acA1300-60gm"),
    ("Basler", "acA2040-55um"),
    ("Allied Vision", "ALVIUM 1800 U-500m"),
)

#: The special cases that never reach the vendor lookup: NVIDIA CSI is
#: selected by ``cameraId is None`` and ICAM by an ``ICAM`` id prefix, both
#: BEFORE ``aravis_functions.getCamera`` is called at all (Requirement 3.8).
RECORDED_NVIDIA_CSI_CONFIGURATION = {
    "gain": 1,
    "exposure": 500,
    "processingPipeline": NVIDIA_CSI_CHAIN,
    "device": "0",
    "deviceName": "nvarguscamerasrc",
}
RECORDED_ICAM_CONFIGURATION = {
    "gain": 1,
    "exposure": 500,
    "processingPipeline": ICAM_CHAIN,
    "device": "/dev/video0",
    "deviceName": "v4l2src",
}

#: ``camera_manager._PFNC_TO_TAG`` in full (Requirement 3.5).
RECORDED_PFNC_TO_TAG = {
    0x01080001: "GRAY8",         # Mono8
    0x01080008: "bayer:grbg",    # BayerGR8
    0x01080009: "bayer:rggb",    # BayerRG8
    0x0108000A: "bayer:gbrg",    # BayerGB8
    0x0108000B: "bayer:bggr",    # BayerBG8
    0x02180014: "RGB",           # RGB8Packed
    0x02180015: "BGR",           # BGR8Packed
    0x02200016: "RGBA",          # RGBA8Packed
    0x02200017: "BGRA",          # BGRA8Packed
}

#: ``gst_pixel_format`` for inputs OUTSIDE the map: unmapped codes and
#: non-integer values yield ``None`` so the caller falls back to the
#: historic bytes-per-pixel guess (Requirement 3.5). Recorded, including
#: the two cases that are NOT ``None``: a numeric STRING and a whole
#: FLOAT both survive ``int()`` and hit the map.
RECORDED_GST_PIXEL_FORMAT_EDGE_CASES = (
    (0x00000000, None),
    (0x02180099, None),
    (1, None),               # int(True) — a mapped-looking small int
    (True, None),
    (None, None),
    ("Mono8", None),
    ("", None),
    ([], None),
    ({}, None),
    (0x01080001, "GRAY8"),
    ("17301505", "GRAY8"),   # str(0x01080001)
    (17301505.0, "GRAY8"),
)

#: ``WorkflowExecutor._frame_caps`` (``pipeline_executor.py`` ~line 2821)
#: recorded as (frame_data, expected caps) — Requirement 3.4. The static
#: camera's own frame is the ``pixel_format: "RGB"`` row: the path is
#: ALREADY CORRECT and stays correct.
RECORDED_FRAME_CAPS = (
    # An explicit `format` (Custom Python Produced_Frame) wins outright.
    ({"format": "BGR", "pixel_format": "RGB"}, "video/x-raw,format=BGR"),
    # A camera-grab `pixel_format` tag names the ACTUAL format.
    ({"pixel_format": "RGB", "width": 4, "height": 3, "data": b"x" * 36},
     "video/x-raw,format=RGB"),
    ({"pixel_format": "GRAY8"}, "video/x-raw,format=GRAY8"),
    # `bayer:<pattern>` becomes video/x-bayer caps (the caller inserts the
    # bayer2rgb demosaic) — a Bayer mosaic is 1 byte/pixel like Mono8, so
    # the size guess below cannot tell them apart.
    ({"pixel_format": "bayer:bggr", "width": 4, "height": 3,
      "data": b"x" * 12}, "video/x-bayer,format=bggr"),
    ({"pixel_format": "bayer:rggb"}, "video/x-bayer,format=rggb"),
    # Untagged frames keep the historic bytes-per-pixel derivation,
    # defaulting to GRAY8.
    ({"width": 4, "height": 3, "data": b"x" * 12}, "video/x-raw,format=GRAY8"),
    ({"width": 4, "height": 3, "data": b"x" * 24}, "video/x-raw,format=GRAY8"),
    ({"width": 4, "height": 3, "data": b"x" * 36}, "video/x-raw,format=RGB"),
    ({"width": 4, "height": 3, "data": b"x" * 48}, "video/x-raw,format=RGBA"),
    ({"width": 4, "height": 3, "data": b"x" * 13}, "video/x-raw,format=GRAY8"),
    ({"width": 4, "height": 3}, "video/x-raw,format=GRAY8"),
    ({"data": b"x" * 12}, "video/x-raw,format=GRAY8"),
    ({}, "video/x-raw,format=GRAY8"),
    # An empty or non-string tag is no tag: fall through to the guess.
    ({"pixel_format": ""}, "video/x-raw,format=GRAY8"),
    ({"pixel_format": 3, "width": 4, "height": 3, "data": b"x" * 36},
     "video/x-raw,format=RGB"),
)

#: The caps a Static_Image_Camera frame yields on the Frame_Feed path —
#: what ``test_workflow_feed.py`` asserts end to end (Requirement 3.4).
STATIC_FRAME_CAPS = "video/x-raw,format=RGB"

#: ``STATIC_IMAGE_CAMERA_IDENTITY`` field-for-field (Requirement 3.6). The
#: identity is the JSON key's source of truth and is NOT changed.
RECORDED_IDENTITY = {
    "id": "static-image-camera",
    "model": "Static Image Camera",
    "address": "internal",
    "physical_id": "static-image-camera",
    "protocol": "StaticImage",
    "serial": "STATIC-IMAGE-0",
    "vendor": "AWS-DDA",
}

#: The seven identity fields of the enumeration entry, in ``model.Camera``
#: constructor order.
IDENTITY_FIELDS = ("id", "model", "address", "physical_id", "protocol",
                   "serial", "vendor")

#: The backfill task 5.2 adds — the SAME name task 1 pins in its
#: ``BACKFILL_FUNCTION_NAME``. Absent on unfixed code: ``db_backfill``
#: exposes exactly ``migration_cleanup_imgsrc_db`` and
#: ``migration_cleanup_workflow_db`` (observed).
BACKFILL_FUNCTION_NAME = "migration_static_camera_pipeline_db"

#: Physical-camera Image_Source_Configuration rows live on ``jetson-thor1``
#: that store the SAME ``default``/``default`` BGGR chain as the broken
#: static-camera row and MUST keep it — Basler sources, for which a BGGR
#: demosaic is correct (Requirement 3.8, bugfix.md task 5.2). Five ids;
#: bugfix.md's prose says "four" while listing five, and task 5.2 says
#: five. All five are pinned here.
LIVE_PHYSICAL_BAYER_SOURCE_IDS = (
    "pk0pppde", "u5ox1y1s", "563tiauk", "3w200mtb", "g6zrsox3",
)

#: A representative live Basler camera id on ``jetson-thor1`` (Image_Source
#: ``28183exv``), used so the physical rows carry a realistic non-static
#: ``cameraId``.
LIVE_BASLER_CAMERA_ID = "Basler-26760165225D-23405149"


# ===========================================================================
# Helpers (same shapes as task 1's suite)
# ===========================================================================


def load_default_camera_config():
    """The REAL shipped ``default_camera_configurations.json``."""
    with open(DEFAULT_CAMERA_CONFIG_PATH, "r") as json_file:
        return json.load(json_file)


def bayer_default_chain(config=None):
    """``bayerDefaultChain()`` from bugfix.md: the shipped
    ``default``/``default`` fall-through, READ FROM THE FILE rather than
    hardcoded, so the two halves of Defect A cannot drift."""
    config = config if config is not None else load_default_camera_config()
    return config["default"]["default"]["processingPipeline"]


def resolve_pipeline(config, vendor, model):
    """``resolvedPipeline(X)`` from bugfix.md — the two-step vendor-then-
    model lookup of ``image_source_accessor.py`` lines 277-282, each step
    falling back to ``default``."""
    vendor_key = vendor if vendor in config else "default"
    model_key = model if model in config[vendor_key] else "default"
    return config.get(vendor_key).get(model_key).get("processingPipeline")


class FakeCameraHandle:
    """A ``getCamera()`` return value answering a chosen vendor/model.

    Exactly the two methods
    ``ImageSourceAccessor.__get_default_image_source_configuration`` calls
    (``image_source_accessor.py`` lines 272-274), so a physical vendor can
    be exercised through the REAL accessor without hardware.
    """

    def __init__(self, vendor, model):
        self._vendor = vendor
        self._model = model

    def get_vendor_name(self):
        return self._vendor

    def get_model_name(self):
        return self._model


@contextlib.contextmanager
def provisioning_harness(image_bytes=None):
    """REAL ``ImageSourceAccessor`` over a private sqlite database with the
    REAL camera configuration file and (optionally) a pinned image.

    Task 1's harness verbatim in shape: absolute config-file path, a tmp
    capture root, directory creation without the dda-admin chown, and the
    test store installed behind BOTH providers
    (``aravis_functions.get_store`` for enumeration / ``getCamera`` and
    ``camera_manager.get_static_image_store`` for the grab short-circuit).

    A context manager rather than pytest fixtures because Hypothesis
    rejects function-scoped fixtures.
    """
    tmp_dir = tempfile.mkdtemp(prefix="static-camera-preservation-")
    session = None
    engine = None
    try:
        store = StaticImageStore(base_dir=os.path.join(tmp_dir, "store"))
        if image_bytes is not None:
            store.pin_bytes(image_bytes, "pinned.img")

        engine = create_engine(
            "sqlite:///{}".format(os.path.join(tmp_dir, "preservation.db")),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(
                constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH",
                DEFAULT_CAMERA_CONFIG_PATH,
            ))
            stack.enter_context(patch.object(
                constants, "IMAGE_CAPTURE_DIR",
                os.path.join(tmp_dir, "image-capture"),
            ))
            stack.enter_context(patch.object(
                dda_user_management_utils, "create_dda_user_directory",
                lambda folder_path: (os.makedirs(folder_path, exist_ok=True),
                                     folder_path)[1],
            ))
            stack.enter_context(patch.object(
                aravis_functions, "get_store", lambda: store))
            stack.enter_context(patch.object(
                camera_manager, "get_static_image_store", lambda: store))

            # Constructed INSIDE the patch: __init__ loads the config file.
            from resources.accessors.image_source_accessor import (
                ImageSourceAccessor,
            )
            yield ImageSourceAccessor(), session, store
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def default_configuration(accessor, camera_id):
    """``resolvedPipeline(X)``'s host function on the REAL accessor:
    ``ImageSourceAccessor.__get_default_image_source_configuration``, the
    exact function bugfix.md names as F for Defect A. Reached through its
    mangled name because it is the private provisioning resolver."""
    resolver = getattr(
        accessor,
        "_ImageSourceAccessor__get_default_image_source_configuration",
        None,
    )
    assert resolver is not None, (
        "ImageSourceAccessor.__get_default_image_source_configuration is "
        "gone; it is the provisioning resolver Defect A lives in "
        "(image_source_accessor.py line 246)"
    )
    return resolver(camera_id)


def resolve_for_vendor(accessor, vendor, model, camera_id="physical-cam-1"):
    """The configuration the REAL accessor resolves for a camera whose
    handle answers ``vendor`` / ``model``."""
    with patch.object(aravis_functions, "getCamera",
                      lambda cid: FakeCameraHandle(vendor, model)):
        return default_configuration(accessor, camera_id)


def create_camera_image_source(accessor, session, camera_id, name):
    """Create a ``Camera`` Image_Source with NO supplied
    ``imageSourceConfiguration`` — the path that resolves and PERSISTS the
    conversion chain."""
    created = accessor.create_image_source(
        {"type": ImageSourceType.CAMERA.value, "name": name,
         "cameraId": camera_id},
        session,
    )
    return created["imageSourceId"]


def read_persisted_configuration(session, image_source_id):
    """The persisted Image_Source_Configuration as a plain dict."""
    row = image_source_dao.get_image_source(session, image_source_id)
    assert row is not None, "Image_Source {} was not persisted".format(
        image_source_id)
    return utils.convert_sqlalchemy_object_to_dict(row.imageSourceConfiguration)


def exif_rotated_jpeg_bytes(width=12, height=5, color=(10, 20, 30)):
    """A JPEG carrying EXIF orientation 6, so the DISPLAYED orientation is
    the transpose of the encoded one."""
    image = Image.new("RGB", (width, height), color)
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: rotate for display -> dimensions swap
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


# --- backfill scoping support (skips until task 5.2 lands) -----------------


def require_backfill():
    """The backfill task 5.2 adds, or ``pytest.skip``.

    This suite must PASS on unfixed code (task 2), and on unfixed code the
    function does not exist — ``db_backfill`` exposes exactly
    ``migration_cleanup_imgsrc_db`` and ``migration_cleanup_workflow_db``.
    Skipping keeps this file green today and turns these into real
    assertions the moment 5.2 lands. Task 1's suite is what FAILS loudly
    on the missing function; duplicating that failure here would defeat
    task 2's purpose.
    """
    from dao.sqlite_db import db_backfill

    backfill_fn = getattr(db_backfill, BACKFILL_FUNCTION_NAME, None)
    if backfill_fn is None:
        pytest.skip(
            "dao.sqlite_db.db_backfill.{} does not exist yet (task 5.2 adds "
            "it; task 1 asserts its absence is the defect). Existing "
            "migrations: {}".format(
                BACKFILL_FUNCTION_NAME,
                sorted(name for name in vars(db_backfill)
                       if name.startswith("migration_")),
            )
        )
    return backfill_fn


@contextlib.contextmanager
def configuration_database():
    """A private configuration database for the backfill scoping half."""
    tmp_dir = tempfile.mkdtemp(prefix="static-camera-preservation-backfill-")
    session = None
    engine = None
    try:
        engine = create_engine(
            "sqlite:///{}".format(os.path.join(tmp_dir, "backfill.db")),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        with patch.object(
            constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH",
            DEFAULT_CAMERA_CONFIG_PATH,
        ):
            yield session
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def add_camera_image_source(session, image_source_id, config_id, camera_id,
                            processing_pipeline, gain=1, exposure=500):
    """Insert a Camera Image_Source and its configuration row directly."""
    now = int(time.time() * 1000)
    session.add(db_models.ImageSourceConfiguration(
        imageSourceConfigId=config_id,
        gain=gain,
        exposure=exposure,
        processingPipeline=processing_pipeline,
        creationTime=now,
    ))
    session.add(db_models.ImageSource(
        imageSourceId=image_source_id,
        name=image_source_id,
        type=ImageSourceType.CAMERA,
        cameraId=camera_id,
        creationTime=now,
        lastUpdateTime=now,
        imageCapturePath="/aws_dda/image-capture/{}".format(image_source_id),
        imageSourceConfigId=config_id,
    ))
    session.commit()


def configuration_rows(session):
    """Every stored configuration row as a comparable snapshot."""
    return {
        row.imageSourceConfigId: (row.processingPipeline, row.gain,
                                  row.exposure)
        for row in session.query(db_models.ImageSourceConfiguration).all()
    }


# ===========================================================================
# Requirement 3.1, 3.2 — the shipped configuration file
# ===========================================================================


def test_recorded_config_entries_are_byte_for_byte_unchanged():
    """Every recorded (vendor, model) entry of
    ``default_camera_configurations.json`` is byte-for-byte what it is
    today — every ``processingPipeline``, ``device``, ``deviceName``,
    ``gain``, and ``exposure`` value included.

    The top-level key SET is deliberately not pinned: task 5.1 ADDS the
    static camera's entry on purpose, and task 1 is what pins that added
    key against ``STATIC_IMAGE_CAMERA_IDENTITY``.

    **Validates: Requirements 3.1**
    """
    config = load_default_camera_config()

    for vendor, recorded_models in RECORDED_CONFIG.items():
        assert vendor in config, (
            "recorded top-level key {!r} was removed from "
            "default_camera_configurations.json".format(vendor)
        )
        # The model sub-key SET per recorded vendor is pinned: task 5.1
        # touches no existing top-level key.
        assert set(config[vendor]) == set(recorded_models), (
            "model sub-keys under {!r} changed: {} -> {}".format(
                vendor, sorted(recorded_models), sorted(config[vendor]))
        )
        for model, recorded_entry in recorded_models.items():
            assert config[vendor][model] == recorded_entry, (
                "config[{!r}][{!r}] changed:\n  recorded: {!r}\n  actual:   "
                "{!r}".format(vendor, model, recorded_entry,
                              config[vendor][model])
            )

    # File order of the recorded keys is preserved (json.load keeps
    # insertion order), so an entry cannot be quietly reshuffled.
    present = [key for key in config if key in RECORDED_CONFIG]
    assert present == RECORDED_TOP_LEVEL_KEYS


def test_every_top_level_key_carries_a_default_sub_key():
    """EVERY top-level key — the eight recorded ones and anything the fix
    adds — carries a ``default`` sub-key, and the chained lookup the code
    performs resolves a non-empty string for it.

    The code REQUIRES this: ``.get(vendor).get(model).get(...)`` raises
    ``AttributeError`` on a vendor whose ``default`` is missing, because
    the middle ``.get`` returns ``None``.

    **Validates: Requirements 3.2**
    """
    config = load_default_camera_config()

    for vendor, models in config.items():
        assert "default" in models, (
            "top-level key {!r} has no 'default' sub-key, so "
            ".get({!r}).get('default').get('processingPipeline') raises for "
            "any unrecognized model of that vendor".format(vendor, vendor)
        )
        # The exact chained expression from image_source_accessor.py line 282.
        chain = config.get(vendor).get("default").get("processingPipeline")
        assert isinstance(chain, str) and chain, (
            "config[{!r}]['default']['processingPipeline'] is {!r}".format(
                vendor, chain)
        )


def test_default_default_is_still_the_bggr_bayer_chain():
    """``default``/``default`` is STILL the BGGR Bayer demosaic.

    A Bayer guess remains CORRECT for an unknown physical bus camera, so
    the fall-through must not be repointed at RGB — that is precisely why
    Defect A is fixed by ADDING a vendor entry rather than by changing
    this one.

    **Validates: Requirements 3.1**
    """
    config = load_default_camera_config()

    assert config["default"]["default"] == {
        "processingPipeline": BAYER_BGGR_CHAIN}
    assert bayer_default_chain(config) == BAYER_BGGR_CHAIN
    assert "bayer2rgb" in bayer_default_chain(config)


@settings(deadline=None)
@given(
    vendor=st.text(min_size=1, max_size=24),
    model=st.text(min_size=0, max_size=24),
)
def test_unknown_physical_vendors_still_fall_through_to_bayer(vendor, model):
    """For ANY vendor/model string that is not a key in the file, the
    two-step lookup still yields the ``default``/``default`` BGGR chain.

    Generated rather than enumerated because the population that must keep
    falling through is "every physical vendor nobody has catalogued yet".

    **Validates: Requirements 3.1, 3.2**
    """
    config = load_default_camera_config()
    assume(vendor not in config)

    assert resolve_pipeline(config, vendor, model) == BAYER_BGGR_CHAIN


@settings(deadline=None)
@given(model=st.text(min_size=0, max_size=24))
def test_unknown_models_of_known_vendors_fall_back_to_vendor_default(model):
    """For ANY model string a recorded vendor does not list, the lookup
    falls back to that VENDOR's ``default`` — not to ``default``/``default``.

    **Validates: Requirements 3.1, 3.2**
    """
    config = load_default_camera_config()

    for vendor in RECORDED_CONFIG:
        assume(model not in config[vendor])
        assert resolve_pipeline(config, vendor, model) == (
            RECORDED_CONFIG[vendor]["default"]["processingPipeline"]
        )


# ===========================================================================
# Requirement 3.1, 3.8 — resolution through the REAL accessor
# ===========================================================================


@pytest.mark.parametrize("vendor", RGB_NATIVE_VENDORS)
def test_rgb_native_vendors_resolve_the_rgb_chain(vendor):
    """The RGB-native physical vendors' ``default`` resolves the RGB
    passthrough chain through the REAL accessor, with the recorded
    ``gain: 1`` / ``exposure: 500`` defaults and no extra keys — for the
    ``default`` sub-key itself and for an arbitrary model the vendor does
    not list.

    This is the chain task 5.1 gives the static camera, which is exactly
    why it must be shown to still belong to these vendors afterwards.

    Scoped to the entries RECORDED as RGB: ``Allied Vision`` also carries a
    GRAY8 model sub-key (``ALVIUM 1800 U-500m``), which
    ``test_gray8_model_variants_resolve_gray8`` pins separately.

    **Validates: Requirements 3.1, 3.8**
    """
    rgb_models = [
        model for model, entry in RECORDED_CONFIG[vendor].items()
        if entry["processingPipeline"] == RGB_CHAIN
    ]
    assert "default" in rgb_models

    with provisioning_harness() as (accessor, _session, _store):
        for model in rgb_models + ["Some Uncatalogued Model"]:
            assert resolve_for_vendor(accessor, vendor, model) == {
                "gain": 1,
                "exposure": 500,
                "processingPipeline": RGB_CHAIN,
            }, "vendor {!r} model {!r}".format(vendor, model)


@pytest.mark.parametrize("vendor,model", GRAY8_MODEL_VARIANTS)
def test_gray8_model_variants_resolve_gray8(vendor, model):
    """The mono model variants under ``Zebra Technologies`` / ``Basler`` /
    ``Allied Vision`` still resolve the GRAY8 chain — the model sub-key
    still overrides its vendor's ``default``.

    **Validates: Requirements 3.1, 3.8**
    """
    with provisioning_harness() as (accessor, _session, _store):
        assert resolve_for_vendor(accessor, vendor, model) == {
            "gain": 1,
            "exposure": 500,
            "processingPipeline": GRAY8_CHAIN,
        }


@settings(deadline=None, max_examples=25)
@given(
    pair=st.sampled_from([
        (vendor, model)
        for vendor, models in sorted(RECORDED_CONFIG.items())
        for model in sorted(models)
    ]),
    unknown_model=st.one_of(st.none(), st.text(min_size=1, max_size=16)),
)
def test_every_recorded_pair_resolves_unchanged_through_the_accessor(
        pair, unknown_model):
    """For EVERY recorded (vendor, model) pair, the REAL accessor resolves
    exactly the recorded chain; and for an arbitrary model the vendor does
    not list, exactly that vendor's recorded ``default``.

    Covers the Bayer vendors (``Basler`` bggr, ``Zebra Technologies``
    rggb) as well as the RGB and GRAY8 ones, so the whole file's
    resolution is pinned in one property.

    **Validates: Requirements 3.1, 3.2, 3.8**
    """
    vendor, model = pair
    config = load_default_camera_config()

    with provisioning_harness() as (accessor, _session, _store):
        assert resolve_for_vendor(accessor, vendor, model) == {
            "gain": 1,
            "exposure": 500,
            "processingPipeline":
                RECORDED_CONFIG[vendor][model]["processingPipeline"],
        }

        if unknown_model is not None and unknown_model not in config[vendor]:
            assert resolve_for_vendor(accessor, vendor, unknown_model) == {
                "gain": 1,
                "exposure": 500,
                "processingPipeline":
                    RECORDED_CONFIG[vendor]["default"]["processingPipeline"],
            }


def test_nvidia_csi_and_icam_special_cases_never_reach_the_vendor_lookup():
    """The NVIDIA CSI (``cameraId is None``) and ICAM (``ICAM`` id prefix)
    branches resolve their recorded configurations — ``processingPipeline``
    plus ``device`` and ``deviceName`` — and return BEFORE
    ``aravis_functions.getCamera`` is called at all.

    ``getCamera`` is replaced with a raising Mock, so reaching the vendor
    lookup would fail loudly rather than silently.

    **Validates: Requirements 3.1, 3.8**
    """
    exploding_get_camera = Mock(side_effect=AssertionError(
        "getCamera() was called: the special case fell through to the "
        "vendor lookup"))

    with provisioning_harness() as (accessor, _session, _store):
        with patch.object(aravis_functions, "getCamera",
                          exploding_get_camera):
            assert default_configuration(accessor, None) == (
                RECORDED_NVIDIA_CSI_CONFIGURATION)
            for camera_id in ("ICAM", "ICAM-abc", "ICAM/dev/video0"):
                assert default_configuration(accessor, camera_id) == (
                    RECORDED_ICAM_CONFIGURATION)

        assert not exploding_get_camera.called


@settings(deadline=None, max_examples=10)
@given(
    vendor_model=st.sampled_from([
        ("Basler", "default"),
        ("Basler", "acA2440-35uc"),
        ("Zebra Technologies", "default"),
        ("Lucid Vision Labs", "TRI050S-C"),
    ]),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
                   min_size=4, max_size=8),
)
def test_physical_camera_provisioning_persists_the_recorded_chain(
        vendor_model, suffix):
    """End to end through the REAL accessor and the REAL DAO: creating a
    ``Camera`` Image_Source for a PHYSICAL camera persists exactly the
    recorded chain, with ``gain: 1`` / ``exposure: 500``.

    This is the same creation path Defect A's static-camera row took, so
    it is the one that must be shown untouched for physical cameras
    (Requirement 3.8).

    **Validates: Requirements 3.1, 3.8**
    """
    vendor, model = vendor_model
    camera_id = "{}-{}".format(vendor.replace(" ", ""), suffix.upper())

    with provisioning_harness() as (accessor, session, _store):
        with patch.object(aravis_functions, "getCamera",
                          lambda cid: FakeCameraHandle(vendor, model)):
            image_source_id = create_camera_image_source(
                accessor, session, camera_id, "physical-{}".format(suffix))

        persisted = read_persisted_configuration(session, image_source_id)
        assert persisted["processingPipeline"] == (
            RECORDED_CONFIG[vendor][model]["processingPipeline"])
        assert persisted["gain"] == 1
        assert persisted["exposure"] == 500


# ===========================================================================
# Requirement 3.3, 3.7 — the store's packed-RGB frame contract
# ===========================================================================


@settings(deadline=None)
@given(
    spec=image_specs,
    configs=st.lists(
        st.one_of(
            st.none(),
            st.fixed_dictionaries({}, optional={
                "gain": st.integers(-100, 100),
                "exposure": st.integers(0, 10_000_000),
                "advancedSettings": st.dictionaries(
                    keys=st.sampled_from(
                        ["reverseX", "reverseY", "balanceWhiteAuto"]),
                    values=st.one_of(st.booleans(), st.integers(-5, 5)),
                    max_size=2,
                ),
            }),
        ),
        min_size=1, max_size=3,
    ),
)
def test_store_frame_contract_is_packed_rgb(spec, configs):
    """For ANY pinned image, the store's frame is packed 24-bit RGB tagged
    ``"RGB"`` with exactly ``3 * width * height`` bytes, byte-identical to
    an independent decode of the same submitted bytes, and byte-identical
    across repeated grabs and across ANY acquisition configuration — both
    directly from ``StaticImageStore.get_frame()`` and through
    ``camera_manager.get_camera_frame``'s static short-circuit.

    ``test_property_grab_determinism.py`` already covers determinism; this
    pins the FORMAT contract the fix must not disturb. The ``"RGB"`` tag in
    particular is what makes the workflow Frame_Feed path already correct
    (Requirement 3.4), so it is load-bearing rather than incidental.

    **Validates: Requirements 3.3, 3.7**
    """
    image_bytes = render_image_bytes(*spec)
    expected = expected_frame(image_bytes)

    with provisioning_harness(image_bytes) as (_accessor, _session, store):
        frames = [store.get_frame(), store.get_frame()]
        for config in configs:
            frames.append(camera_manager.get_camera_frame(
                STATIC_IMAGE_CAMERA_ID, config))
        frames.append(camera_manager.get_camera_frame(STATIC_IMAGE_CAMERA_ID))

        for frame in frames:
            assert sorted(frame) == ["data", "height", "pixel_format", "width"]
            assert frame["pixel_format"] == "RGB"
            assert isinstance(frame["data"], bytes)
            assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
            # Byte-for-byte against an independent decode, so the packed
            # layout itself is pinned and not just its length.
            assert frame == expected


def test_store_frame_reports_exif_transposed_dimensions():
    """A pinned JPEG carrying EXIF orientation 6 reports the DISPLAYED
    (transposed) dimensions, and ``3 * width * height`` still holds
    against them.

    Recorded on unfixed code: a 12x5 encoded JPEG with orientation 6 is
    served as 5x12 with 180 bytes. The swap assertion is what keeps this
    from passing vacuously if EXIF handling were dropped.

    **Validates: Requirements 3.3**
    """
    image_bytes = exif_rotated_jpeg_bytes(width=12, height=5)

    with Image.open(io.BytesIO(image_bytes)) as encoded:
        assert encoded.size == (12, 5)
        assert ImageOps.exif_transpose(encoded).size == (5, 12)

    with provisioning_harness(image_bytes) as (_accessor, _session, store):
        frame = store.get_frame()

        assert (frame["width"], frame["height"]) == (5, 12)
        assert frame["pixel_format"] == "RGB"
        assert len(frame["data"]) == 3 * 5 * 12 == 180
        assert frame == expected_frame(image_bytes)


def test_static_grab_short_circuits_and_names_the_camera_when_unpinned():
    """``camera_manager.get_camera_frame`` for the static id returns the
    store's frame WITHOUT calling ``connect_camera``, accepts and ignores
    an acquisition configuration, and raises an error NAMING the camera
    when nothing is pinned.

    **Validates: Requirements 3.7**
    """
    image_bytes = render_image_bytes(6, 4, 5, "PNG")
    exploding_connect = Mock(side_effect=AssertionError(
        "connect_camera() was called for the static camera"))

    with provisioning_harness(image_bytes) as (_accessor, _session, store):
        with patch.object(camera_manager, "connect_camera",
                          exploding_connect):
            frame = camera_manager.get_camera_frame(
                STATIC_IMAGE_CAMERA_ID, {"gain": 77, "exposure": 3})
        assert not exploding_connect.called
        assert frame == expected_frame(image_bytes)

        store.unpin()
        with pytest.raises(Exception) as excinfo:
            camera_manager.get_camera_frame(STATIC_IMAGE_CAMERA_ID)
        assert STATIC_IMAGE_CAMERA_ID in str(excinfo.value)


# ===========================================================================
# Requirement 3.4 — the workflow Frame_Feed path (verify and preserve)
# ===========================================================================


@pytest.mark.parametrize("frame_data,expected_caps", RECORDED_FRAME_CAPS)
def test_frame_caps_mapping_is_unchanged(frame_data, expected_caps):
    """``WorkflowExecutor._frame_caps`` maps every recorded frame shape to
    exactly the recorded caps: a ``"RGB"`` tag to
    ``video/x-raw,format=RGB``, a ``bayer:bggr`` tag to
    ``video/x-bayer,format=bggr``, and an untagged frame to the
    bytes-per-pixel guess (GRAY8 by default).

    **Validates: Requirements 3.4**
    """
    assert WorkflowExecutor._frame_caps(dict(frame_data)) == expected_caps


@settings(deadline=None)
@given(
    raw_format=st.sampled_from(["RGB", "BGR", "RGBA", "BGRA", "GRAY8", "I420"]),
    bayer_pattern=st.sampled_from(["bggr", "rggb", "grbg", "gbrg"]),
)
def test_frame_caps_honors_any_tag_the_pfnc_map_can_produce(raw_format,
                                                            bayer_pattern):
    """For every tag ``camera_manager.gst_pixel_format`` can produce, a raw
    name becomes ``video/x-raw,format={tag}`` and ``bayer:<pattern>``
    becomes ``video/x-bayer,format={pattern}`` — the tag is honored, never
    second-guessed by the byte-count heuristic.

    Sizes are generated to CONTRADICT the tag (1 byte/pixel for a 3-byte
    format and vice versa) so a regression that ignored the tag could not
    accidentally agree.

    **Validates: Requirements 3.4, 3.5**
    """
    contradicting = {"width": 4, "height": 4, "data": b"x" * 64}

    frame = dict(contradicting, pixel_format=raw_format)
    assert WorkflowExecutor._frame_caps(frame) == (
        "video/x-raw,format={}".format(raw_format))

    frame = dict(contradicting, pixel_format="bayer:{}".format(bayer_pattern))
    assert WorkflowExecutor._frame_caps(frame) == (
        "video/x-bayer,format={}".format(bayer_pattern))


@settings(deadline=None, max_examples=10)
@given(spec=image_specs)
def test_static_camera_frame_yields_rgb_caps_on_the_frame_feed_path(spec):
    """The Frame_Feed path is ALREADY CORRECT for the static camera and
    stays correct: for ANY pinned image, the store's own frame — the exact
    dict ``camera_manager.get_camera_frame`` returns verbatim through the
    static short-circuit — yields ``video/x-raw,format=RGB``.

    This is the "verify and preserve" the requirement asks for. The
    end-to-end form of it is the existing assertion
    ``"appsrc name=appsrc caps=video/x-raw,format=RGB "`` in
    ``test_workflow_feed.py``, which must keep passing untouched.

    **Validates: Requirements 3.3, 3.4**
    """
    image_bytes = render_image_bytes(*spec)

    with provisioning_harness(image_bytes) as (_accessor, _session, _store):
        frame = camera_manager.get_camera_frame(STATIC_IMAGE_CAMERA_ID)

    assert WorkflowExecutor._frame_caps(frame) == STATIC_FRAME_CAPS
    assert not WorkflowExecutor._frame_caps(frame).startswith("video/x-bayer")


# ===========================================================================
# Requirement 3.5 — the PFNC map and gst_pixel_format
# ===========================================================================


def test_pfnc_map_is_unchanged():
    """``camera_manager._PFNC_TO_TAG`` maps exactly today's codes to
    exactly today's tags — no additions, no removals, no re-pointing.

    **Validates: Requirements 3.5**
    """
    assert camera_manager._PFNC_TO_TAG == RECORDED_PFNC_TO_TAG


@pytest.mark.parametrize("code,tag", sorted(RECORDED_PFNC_TO_TAG.items()))
def test_gst_pixel_format_maps_every_recorded_code(code, tag):
    """``gst_pixel_format`` returns the recorded tag for every mapped PFNC
    code.

    **Validates: Requirements 3.5**
    """
    assert camera_manager.gst_pixel_format(code) == tag


@pytest.mark.parametrize("value,expected",
                         RECORDED_GST_PIXEL_FORMAT_EDGE_CASES)
def test_gst_pixel_format_edge_cases_are_unchanged(value, expected):
    """``gst_pixel_format`` yields ``None`` for unmapped codes and for
    non-integer values (so the caller falls back to the historic
    bytes-per-pixel guess), and still resolves values that survive
    ``int()`` — a numeric string and a whole float both hit the map.

    **Validates: Requirements 3.5**
    """
    assert camera_manager.gst_pixel_format(value) == expected


@settings(deadline=None)
@given(code=st.integers(min_value=0, max_value=2 ** 32 - 1))
def test_gst_pixel_format_returns_none_for_any_unmapped_code(code):
    """For ANY integer code outside the map, ``gst_pixel_format`` is
    ``None``; for any code inside it, the recorded tag.

    **Validates: Requirements 3.5**
    """
    if code in RECORDED_PFNC_TO_TAG:
        assert camera_manager.gst_pixel_format(code) == (
            RECORDED_PFNC_TO_TAG[code])
    else:
        assert camera_manager.gst_pixel_format(code) is None


# ===========================================================================
# Requirement 3.6 — enumeration, getCamera, and the shipped identity
# ===========================================================================


def test_static_image_camera_identity_is_unchanged_field_for_field():
    """``STATIC_IMAGE_CAMERA_IDENTITY`` is unchanged field-for-field, and
    ``STATIC_IMAGE_CAMERA_ID`` still equals its ``id`` / ``physical_id``.

    The identity is the source of truth for the JSON key task 5.1 adds, so
    changing it here would silently revert the static camera to the Bayer
    default — which is exactly what task 1's assertion catches.

    **Validates: Requirements 3.6**
    """
    assert dict(STATIC_IMAGE_CAMERA_IDENTITY) == RECORDED_IDENTITY
    assert STATIC_IMAGE_CAMERA_ID == RECORDED_IDENTITY["id"]
    assert STATIC_IMAGE_CAMERA_ID == RECORDED_IDENTITY["physical_id"]
    # The keys match model.Camera's constructor keywords exactly, which is
    # what lets getCameras() do Camera(**STATIC_IMAGE_CAMERA_IDENTITY).
    assert set(STATIC_IMAGE_CAMERA_IDENTITY) == set(IDENTITY_FIELDS)


@settings(deadline=None)
@given(physical=physical_camera_lists, spec=image_specs)
def test_enumeration_is_unchanged_pinned_and_unpinned(physical, spec):
    """For ANY physical camera list, ``getCameras()`` and
    ``rescan_cameras()`` return the physical entries unchanged and in
    order, with the static entry APPENDED last while a Pinned_Image exists
    and absent when none does — the recorded seven identity fields
    verbatim.

    **Validates: Requirements 3.6**
    """
    image_bytes = render_image_bytes(*spec)
    base_dir = tempfile.mkdtemp(prefix="static-camera-preservation-enum-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        bus = FakeAravisBus(physical)
        expected_physical = [physical_fields(entry) for entry in physical]
        expected_static = tuple(
            RECORDED_IDENTITY[field] for field in IDENTITY_FIELDS)

        with patch.object(aravis_functions, "Aravis", bus), \
                patch.object(aravis_functions, "get_store", lambda: store):
            for enumerate_cameras in (aravis_functions.getCameras,
                                      aravis_functions.rescan_cameras):
                assert [camera_fields(c) for c in enumerate_cameras()] == (
                    expected_physical)

            store.pin_bytes(image_bytes, "pinned.img")
            for enumerate_cameras in (aravis_functions.getCameras,
                                      aravis_functions.rescan_cameras):
                assert [camera_fields(c) for c in enumerate_cameras()] == (
                    expected_physical + [expected_static])

            store.unpin()
            for enumerate_cameras in (aravis_functions.getCameras,
                                      aravis_functions.rescan_cameras):
                assert [camera_fields(c) for c in enumerate_cameras()] == (
                    expected_physical)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


def test_get_camera_is_unchanged_pinned_and_unpinned():
    """``getCamera(STATIC_IMAGE_CAMERA_ID)`` returns a truthy handle
    answering the recorded vendor and model while pinned, and raises
    ``AravisCameraNotFound`` mentioning the pin requirement when nothing
    is pinned.

    The vendor this handle reports IS what the provisioning lookup keys
    on, so it is the hinge of Defect A and must stay exactly as it is.

    **Validates: Requirements 3.6**
    """
    from exceptions.api.aravis_camera_not_found import AravisCameraNotFound

    base_dir = tempfile.mkdtemp(prefix="static-camera-preservation-handle-")
    try:
        store = StaticImageStore(base_dir=base_dir)
        with patch.object(aravis_functions, "get_store", lambda: store):
            with pytest.raises(AravisCameraNotFound) as excinfo:
                aravis_functions.getCamera(STATIC_IMAGE_CAMERA_ID)
            assert STATIC_IMAGE_CAMERA_ID in str(excinfo.value)
            assert "pinned" in str(excinfo.value)

            store.pin_bytes(render_image_bytes(5, 5, 2, "PNG"), "pinned.img")
            handle = aravis_functions.getCamera(STATIC_IMAGE_CAMERA_ID)
            assert bool(handle) is True
            assert handle.get_vendor_name() == RECORDED_IDENTITY["vendor"]
            assert handle.get_model_name() == RECORDED_IDENTITY["model"]
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


# ===========================================================================
# Requirement 3.8 — the backfill is scoped
#
# SKIPS until task 5.2 adds the function; see require_backfill().
# ===========================================================================


def test_backfill_leaves_a_customized_static_camera_pipeline_untouched():
    """A static-camera Image_Source whose ``processingPipeline`` was
    deliberately CUSTOMIZED is left byte-for-byte alone: the backfill's
    condition is an EXACT match against the known-wrong
    ``default``/``default`` string, not "any static-camera row".

    **Validates: Requirements 3.8**
    """
    backfill_fn = require_backfill()
    bayer_chain = bayer_default_chain()
    customizations = (
        "capsfilter caps=video/x-raw,format=GRAY8 ! videoconvert",
        "videoconvert",
        # Near-misses on the known-wrong string: trailing space, a
        # different Bayer pattern, and an added element.
        bayer_chain + " ",
        bayer_chain.replace("bggr", "rggb"),
        bayer_chain + " ! videoflip method=vertical-flip",
        bayer_chain.upper(),
    )

    with configuration_database() as session:
        for index, pipeline in enumerate(customizations):
            add_camera_image_source(
                session,
                image_source_id="custom-{}".format(index),
                config_id="cfg-custom-{}".format(index),
                camera_id=STATIC_IMAGE_CAMERA_ID,
                processing_pipeline=pipeline,
            )

        before = configuration_rows(session)
        backfill_fn(session)
        session.commit()
        assert configuration_rows(session) == before


def test_backfill_leaves_physical_camera_bayer_rows_untouched():
    """The five physical-camera Image_Source_Configuration rows live on
    ``jetson-thor1`` that store the SAME ``default``/``default`` BGGR chain
    keep it: they are Basler sources, for which a BGGR demosaic is
    correct.

    Storing the known-wrong string is not sufficient — the row must ALSO
    belong to a static-camera Image_Source.

    **Validates: Requirements 3.8**
    """
    backfill_fn = require_backfill()
    bayer_chain = bayer_default_chain()

    with configuration_database() as session:
        for image_source_id in LIVE_PHYSICAL_BAYER_SOURCE_IDS:
            add_camera_image_source(
                session,
                image_source_id=image_source_id,
                config_id="cfg-{}".format(image_source_id),
                camera_id=LIVE_BASLER_CAMERA_ID,
                processing_pipeline=bayer_chain,
            )

        before = configuration_rows(session)
        backfill_fn(session)
        session.commit()
        after = configuration_rows(session)

        assert after == before
        for image_source_id in LIVE_PHYSICAL_BAYER_SOURCE_IDS:
            assert after["cfg-{}".format(image_source_id)][0] == bayer_chain


@settings(deadline=None, max_examples=25)
@given(
    rows=st.lists(
        st.tuples(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
                    min_size=6, max_size=8),
            # cameraId: the static id, a physical id, or absent.
            st.sampled_from([STATIC_IMAGE_CAMERA_ID, LIVE_BASLER_CAMERA_ID,
                             "Aravis-Fake-GV01", None]),
            # processingPipeline: the known-wrong string, another shipped
            # chain, a customization, or absent.
            st.sampled_from([None, RGB_CHAIN, GRAY8_CHAIN, BAYER_RGGB_CHAIN,
                             ICAM_CHAIN, NVIDIA_CSI_CHAIN, "",
                             "__KNOWN_WRONG__"]),
        ),
        min_size=1, max_size=4, unique_by=lambda row: row[0],
    ),
)
def test_backfill_is_a_noop_for_everything_that_is_not_the_known_wrong_row(
        rows):
    """``backfill'(X) = X`` for EVERY configuration that is not
    (a static-camera Image_Source AND storing exactly the known-wrong
    ``default``/``default`` string).

    Rows matching BOTH conditions are excluded from the assertion and left
    in the database, so the property is checked in the presence of rows the
    backfill legitimately rewrites — task 1 is what asserts those rows DO
    converge.

    **Validates: Requirements 3.8**
    """
    backfill_fn = require_backfill()
    bayer_chain = bayer_default_chain()

    with configuration_database() as session:
        untouchable = []
        for suffix, camera_id, pipeline in rows:
            pipeline = (bayer_chain if pipeline == "__KNOWN_WRONG__"
                        else pipeline)
            config_id = "cfg-{}".format(suffix)
            add_camera_image_source(
                session,
                image_source_id="src-{}".format(suffix),
                config_id=config_id,
                camera_id=camera_id,
                processing_pipeline=pipeline,
            )
            is_known_wrong_static = (camera_id == STATIC_IMAGE_CAMERA_ID
                                     and pipeline == bayer_chain)
            if not is_known_wrong_static:
                untouchable.append(config_id)

        before = configuration_rows(session)
        backfill_fn(session)
        session.commit()
        # A second run must not disturb them either.
        backfill_fn(session)
        session.commit()
        after = configuration_rows(session)

        for config_id in untouchable:
            assert after[config_id] == before[config_id], (
                "backfill modified {}: {!r} -> {!r}".format(
                    config_id, before[config_id], after[config_id])
            )
        # No row is created or deleted by the backfill.
        assert set(after) == set(before)


def test_backfill_leaves_non_camera_image_sources_untouched():
    """A Folder Image_Source whose configuration stores the known-wrong
    string is untouched: the backfill is scoped to ``Camera`` sources whose
    ``cameraId`` is the static camera.

    **Validates: Requirements 3.8**
    """
    backfill_fn = require_backfill()
    bayer_chain = bayer_default_chain()
    now = int(time.time() * 1000)

    with configuration_database() as session:
        session.add(db_models.ImageSourceConfiguration(
            imageSourceConfigId="cfg-folder",
            gain=1, exposure=500,
            processingPipeline=bayer_chain,
            creationTime=now,
        ))
        session.add(db_models.ImageSource(
            imageSourceId="folder-src",
            name="folder-src",
            type=ImageSourceType.FOLDER,
            creationTime=now,
            lastUpdateTime=now,
            imageCapturePath="",
            imageSourceConfigId="cfg-folder",
        ))
        session.commit()

        before = configuration_rows(session)
        backfill_fn(session)
        session.commit()
        assert configuration_rows(session) == before
