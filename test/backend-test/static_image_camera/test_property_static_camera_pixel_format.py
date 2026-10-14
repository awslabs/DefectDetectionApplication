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
"""Bug condition exploration test for the static camera's pixel format
(spec ``static-camera-pixel-format-and-detection-results``, task 1).

**Property 1: Bug Condition / Fix Checking (Defect A)** — the
Static_Image_Camera's frames are Bayer-demosaiced instead of served as
packed RGB.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

THESE TESTS ARE EXPECTED TO FAIL ON UNFIXED CODE. The failures ARE the
result: they are the counterexamples that prove Defect A exists. They
encode the EXPECTED (post-fix) behavior, so the same file validates the
fix in task 5.3 without being rewritten.

What is exercised, and where the defect lives
---------------------------------------------
The Static_Image_Camera store serves packed RGB and tags every frame
``pixel_format: "RGB"``, but the CLASSIC Image_Source path resolves its
GStreamer conversion chain by camera VENDOR against
``src/backend/utils/config/default_camera_configurations.json``. The
static camera's vendor is ``STATIC_IMAGE_CAMERA_IDENTITY["vendor"]``
(``AWS-DDA``), which is not a top-level key in that file, so the two-step
lookup in ``ImageSourceAccessor.__get_default_image_source_configuration``
collapses to ``default``/``default`` — a BGGR Bayer demosaic. That string
is PERSISTED at Image_Source creation, appended verbatim to ``appsrc`` by
``GstPipelineBuilder._add_camera_image_source``, and turned into the
appsrc caps by ``GstPipelineManager.create_buffer``'s first-``caps=``
regex, so packed RGB bytes are declared to GStreamer as a BGGR mosaic and
``bayer2rgb`` mangles them (bugfix.md 1.1 - 1.8).

Harness
-------
Reuses the wiring of ``test_image_source_wiring.py``: the REAL
``ImageSourceAccessor`` over a PRIVATE sqlite database, with
``constants.DEFAULT_CAMERA_CONFIG_FILE_PATH`` pointed at the REAL
``src/backend/utils/config/default_camera_configurations.json`` and the
real ``StaticImageStore`` installed behind ``aravis_functions.get_store``
so the ``getCamera`` short-circuit answers the shipped identity. The
built pipeline comes from the REAL ``GstPipelineBuilder``, merged exactly
the way ``GstPipelineExecutor.execute_image_source_pipeline`` merges an
acquisition override.

The harness is a context manager rather than pytest fixtures because
Hypothesis rejects function-scoped fixtures; the accessor is driven
directly (rather than through the ``/image-sources`` route, as
``test_image_source_wiring.py`` does) because the route contributes
nothing to conversion-chain resolution and a FastAPI app per generated
example is pure overhead. Both the accessor and the DAO layer are real.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import contextlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))

#: The REAL shipped configuration file — never a fixture copy, so a key
#: that is missing on device is missing here too (Requirement 2.2).
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

from static_image_strategies import image_specs, render_image_bytes  # noqa: E402

# ---------------------------------------------------------------------------
# Expected behavior and live counterexamples
# ---------------------------------------------------------------------------

#: Requirement 2.1 — the chain the RGB-native physical vendors
#: (Lucid Vision Labs, Allied Vision, OMRON SENTECH) already use.
RGB_PASSTHROUGH_CHAIN = "capsfilter caps=video/x-raw,format=RGB ! videoconvert"

#: Requirement 2.3 — the appsrc caps the classic path must declare.
RGB_FIRST_CAPS = "video/x-raw,format=RGB"

#: Live counterexample (bugfix.md 1.2, confirmed on ``jetson-thor1``):
#: Image_Source ``my6j3zx1`` ("static-image-camera1",
#: ``cameraId: static-image-camera``) points at Image_Source_Configuration
#: ``jk1y5ln8``, whose stored ``processingPipeline`` is the BGGR chain.
LIVE_IMAGE_SOURCE_ID = "my6j3zx1"
LIVE_IMAGE_SOURCE_CONFIG = {
    "imageSourceConfigId": "jk1y5ln8",
    "gain": 1,
    "exposure": 500,
    "processingPipeline": (
        "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb"
        " ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
    ),
}

#: The crop block the HMI sends on a preview request; it is what puts the
#: all-zero ``videocrop`` into the executed launch string below.
LIVE_ZERO_CROP = {"top": 0, "bottom": 0, "left": 0, "right": 0}

#: Live counterexample (bugfix.md 1.4): the pipeline the device actually
#: executed for ``POST /image-sources/my6j3zx1/preview`` (request
#: ``d5c45a4d99fe4a578ee35e2a4fc92341``), character-for-character from the
#: backend container log.
LIVE_PREVIEW_LAUNCH = (
    "appsrc name=appsrc"
    " ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb"
    " ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
    " ! videocrop top=0 bottom=0 left=0 right=0"
    " ! jpegenc idct-method=2 quality=100"
    " ! filesink location=/aws_dda/image-capture/preview/"
    "default_file_prefix-my6j3zx1.jpg"
)

#: The SAME regex ``GstPipelineManager.create_buffer`` uses to derive the
#: appsrc caps from the launch string (``gst_pipeline.py`` lines 61-72).
FIRST_CAPS_PATTERN = r'caps=([^!]+)'

#: The backfill task 5.2 must add to
#: ``src/backend/dao/sqlite_db/db_backfill.py``, alongside
#: ``migration_cleanup_imgsrc_db`` / ``migration_cleanup_workflow_db`` and
#: called from ``backfill()``. Taking the same ``(session)`` argument as
#: its two siblings. It does not exist yet, which is exactly why the
#: migration half of this suite fails today (Requirement 2.5).
BACKFILL_FUNCTION_NAME = "migration_static_camera_pipeline_db"


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


def first_caps(launch_string):
    """``firstCaps(launch)`` from bugfix.md, via ``create_buffer``'s regex."""
    match = re.search(FIRST_CAPS_PATTERN, launch_string)
    assert match is not None, (
        "no 'caps=' clause in the launch string, so create_buffer would "
        "raise deriving the appsrc caps: {}".format(launch_string)
    )
    # create_buffer interpolates match.group(1) verbatim; strip only the
    # whitespace the ' ! ' join leaves behind.
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

# Arbitrary acquisition configuration (gain / exposure / crop). Never a
# processingPipeline: supplying one makes the accessor store the payload as
# given and skip default resolution (the documented behavior of
# Requirement 3.8), which is not the path under test. Acquisition settings
# must not influence the pixel-format chain.
_acquisition_configs = st.fixed_dictionaries(
    {},
    optional={
        "gain": st.integers(0, 100),
        "exposure": st.integers(0, 1_000_000),
        "imageCrop": st.fixed_dictionaries({
            "top": st.integers(0, 8),
            "bottom": st.integers(0, 8),
            "left": st.integers(0, 8),
            "right": st.integers(0, 8),
        }),
    },
)


@contextlib.contextmanager
def static_camera_provisioning(image_bytes):
    """REAL ``ImageSourceAccessor`` over a private sqlite database with the
    REAL camera configuration file and a pinned image.

    Mirrors the ``client`` fixture of ``test_image_source_wiring.py``:
    absolute config-file path, a tmp capture root, directory creation
    without the dda-admin chown, and the test store installed behind both
    providers (``aravis_functions.get_store`` for enumeration/getCamera and
    ``camera_manager.get_static_image_store`` for the grab short-circuit).
    """
    tmp_dir = tempfile.mkdtemp(prefix="static-camera-pixel-format-")
    session = None
    engine = None
    try:
        store = StaticImageStore(base_dir=os.path.join(tmp_dir, "store"))
        store.pin_bytes(image_bytes, "pinned.img")

        engine = create_engine(
            "sqlite:///{}".format(os.path.join(tmp_dir, "pixel_format.db")),
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


def create_static_image_source(accessor, session, name="static-image-camera1"):
    """Create a ``Camera`` Image_Source for the static camera with NO
    supplied ``imageSourceConfiguration`` — the provisioning path that
    resolves and PERSISTS the conversion chain (bugfix.md 1.1, 1.2)."""
    created = accessor.create_image_source(
        {"type": ImageSourceType.CAMERA.value, "name": name,
         "cameraId": STATIC_IMAGE_CAMERA_ID},
        session,
    )
    return created["imageSourceId"]


def read_persisted_image_source(session, image_source_id):
    """The persisted Image_Source in the shape the pipeline executor hands
    to the builder (``convert_sqlalchemy_object_to_dict`` on the row and on
    its nested configuration)."""
    row = image_source_dao.get_image_source(session, image_source_id)
    assert row is not None, "Image_Source {} was not persisted".format(
        image_source_id)
    image_source = utils.convert_sqlalchemy_object_to_dict(row)
    image_source["imageSourceConfiguration"] = (
        utils.convert_sqlalchemy_object_to_dict(row.imageSourceConfiguration)
    )
    return image_source


def build_preview_launch(image_source, override=None):
    """The REAL ``GstPipelineBuilder`` preview build for a Camera
    Image_Source, with an acquisition override merged EXACTLY the way
    ``GstPipelineExecutor.execute_image_source_pipeline`` merges one."""
    from gstreamer.pipeline_builder import GstPipelineBuilder

    image_source = dict(image_source)
    config = dict(image_source.get("imageSourceConfiguration") or {})
    for param in ("gain", "exposure", "processingPipeline", "imageCrop"):
        if override and override.get(param):
            config[param] = override.get(param)
    image_source["imageSourceConfiguration"] = config
    image_source["type"] = ImageSourceType.CAMERA

    launch_string, location = (
        GstPipelineBuilder().add_image_source(image_source)
                            .build(is_preview=True)
    )
    return launch_string, location


def resolve_backfill():
    """The backfill task 5.2 adds. Missing today — Requirement 2.5."""
    from dao.sqlite_db import db_backfill

    backfill_fn = getattr(db_backfill, BACKFILL_FUNCTION_NAME, None)
    assert backfill_fn is not None, (
        "dao.sqlite_db.db_backfill.{}(session) does not exist, so an "
        "Image_Source that already persisted the default/default Bayer "
        "chain (live: {} -> config {}) can never converge to the RGB chain "
        "on its own and the user must delete and recreate the source "
        "(bugfix.md 1.8, Requirement 2.5). Existing migrations in that "
        "module: {}".format(
            BACKFILL_FUNCTION_NAME, LIVE_IMAGE_SOURCE_ID,
            LIVE_IMAGE_SOURCE_CONFIG["imageSourceConfigId"],
            sorted(name for name in vars(db_backfill)
                   if name.startswith("migration_")),
        )
    )
    return backfill_fn


@contextlib.contextmanager
def configuration_database():
    """A private configuration database for the migration half."""
    tmp_dir = tempfile.mkdtemp(prefix="static-camera-backfill-")
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


def stored_pipelines(session):
    """Every stored ``processingPipeline`` keyed by configuration id."""
    return {
        row.imageSourceConfigId: row.processingPipeline
        for row in session.query(db_models.ImageSourceConfiguration).all()
    }


# ---------------------------------------------------------------------------
# Counterexample anchor (bugfix.md 1.2, 1.4)
# ---------------------------------------------------------------------------


def test_live_counterexamples_are_the_shipped_bayer_default():
    """The live evidence, pinned against the shipped file and the REAL
    builder. This documents the counterexamples the rest of the suite is
    about; it holds both before and after the fix.

    _Bug_Condition: a2 — a stored static-camera configuration whose
    processingPipeline is bayerDefaultChain()_
    """
    config = load_default_camera_config()

    # The value stored for Image_Source my6j3zx1 IS the default/default
    # fall-through, byte-for-byte — not a user customization.
    assert LIVE_IMAGE_SOURCE_CONFIG["processingPipeline"] == (
        bayer_default_chain(config)
    )

    # The pipeline the device executed is what the REAL builder produces
    # for that stored row, character-for-character (bugfix.md 1.3, 1.4).
    launch_string, location = build_preview_launch(
        {"imageSourceId": LIVE_IMAGE_SOURCE_ID,
         "cameraId": STATIC_IMAGE_CAMERA_ID,
         "imageSourceConfiguration": LIVE_IMAGE_SOURCE_CONFIG},
        override={"imageCrop": LIVE_ZERO_CROP},
    )
    assert launch_string == LIVE_PREVIEW_LAUNCH
    assert location == (
        "/aws_dda/image-capture/preview/default_file_prefix-{}.jpg".format(
            LIVE_IMAGE_SOURCE_ID)
    )

    # ... and create_buffer would therefore declare the packed-RGB Pinned
    # Image bytes to GStreamer as a BGGR mosaic, with bayer2rgb next.
    assert first_caps(launch_string) == "video/x-bayer,format=bggr"
    assert "bayer2rgb" in launch_string


# ---------------------------------------------------------------------------
# Requirement 2.1, 2.2 — the config file entry, keyed by the shipped
# identity rather than a drifting literal
# ---------------------------------------------------------------------------


def test_config_file_resolves_rgb_for_the_shipped_static_camera_identity():
    """The shipped ``default_camera_configurations.json`` carries an entry
    for the static camera's enumeration identity, that entry has the
    ``default`` sub-key the lookup requires, and it resolves to the RGB
    passthrough chain.

    The identity comes from ``STATIC_IMAGE_CAMERA_IDENTITY`` rather than a
    literal, so a future identity change fails loudly HERE instead of
    silently reverting to the Bayer default (Requirement 2.2).

    **Validates: Requirements 2.1, 2.2**
    """
    config = load_default_camera_config()
    vendor = STATIC_IMAGE_CAMERA_IDENTITY["vendor"]
    model = STATIC_IMAGE_CAMERA_IDENTITY["model"]

    assert vendor in config, (
        "STATIC_IMAGE_CAMERA_IDENTITY['vendor'] = {!r} is not a top-level "
        "key of default_camera_configurations.json (shipped keys: {}), so "
        "the vendor-then-model lookup collapses to default/default and "
        "resolves the BGGR Bayer demosaic {!r} for the static camera "
        "(bugfix.md 1.1)".format(
            vendor, sorted(config), bayer_default_chain(config))
    )

    # Required by the lookup: .get(vendor).get('default') must not be None
    # or the chained .get('processingPipeline') raises (Requirement 3.2).
    assert "default" in config[vendor]

    # Any model sub-key must be the shipped model name, not a literal that
    # can drift away from the enumeration identity (Requirement 2.2).
    for model_key in config[vendor]:
        if model_key != "default":
            assert model_key == model

    # Every sub-key resolves to the RGB passthrough chain, so both the
    # exact model and an unknown model resolve correctly (Requirement 2.1).
    for model_key, entry in config[vendor].items():
        assert entry.get("processingPipeline") == RGB_PASSTHROUGH_CHAIN, (
            "config[{!r}][{!r}] resolves {!r}".format(
                vendor, model_key, entry.get("processingPipeline"))
        )

    assert resolve_pipeline(config, vendor, model) == RGB_PASSTHROUGH_CHAIN
    assert (
        resolve_pipeline(config, vendor, "Some Future Model")
        == RGB_PASSTHROUGH_CHAIN
    )


# ---------------------------------------------------------------------------
# Requirement 2.1 — provisioning through the REAL accessor persists RGB
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=10)
@given(spec=image_specs)
def test_provisioning_persists_the_rgb_chain(spec):
    """For ANY pinned image, creating a ``Camera`` Image_Source for
    ``cameraId: STATIC_IMAGE_CAMERA_ID`` with no supplied
    ``imageSourceConfiguration`` PERSISTS the RGB passthrough chain.

    Today it persists the ``default``/``default`` BGGR chain, and because
    resolution happens once at creation time the stored value is what
    preview, capture, and classic-path inference all read back
    (bugfix.md 1.2, 1.3, 1.8).

    **Validates: Requirements 2.1, 2.4**
    """
    image_bytes = render_image_bytes(*spec)

    with static_camera_provisioning(image_bytes) as (accessor, session, _):
        image_source_id = create_static_image_source(accessor, session)
        persisted = read_persisted_image_source(session, image_source_id)
        config = persisted["imageSourceConfiguration"]

        assert config["processingPipeline"] == RGB_PASSTHROUGH_CHAIN, (
            "Image_Source {} persisted {!r}; the live device row is the "
            "same string (config {} on Image_Source {})".format(
                image_source_id, config["processingPipeline"],
                LIVE_IMAGE_SOURCE_CONFIG["imageSourceConfigId"],
                LIVE_IMAGE_SOURCE_ID)
        )
        # Unchanged acquisition defaults (Requirement 3.8) — the fix
        # changes the conversion chain and nothing else.
        assert config["gain"] == 1
        assert config["exposure"] == 500


# ---------------------------------------------------------------------------
# Requirement 2.3, 2.4 — the pipeline the classic path actually executes
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=10)
@given(spec=image_specs, override=_acquisition_configs)
def test_built_pipeline_declares_rgb_caps_and_no_demosaic(spec, override):
    """For ANY pinned image and ANY acquisition configuration, the launch
    string the REAL ``GstPipelineBuilder`` produces for a static-camera
    Image_Source declares ``video/x-raw,format=RGB`` as its FIRST ``caps=``
    clause — the one ``create_buffer`` turns into the appsrc caps — and
    contains no ``bayer2rgb`` anywhere.

    Acquisition settings (gain, exposure, crop) must not influence the
    pixel-format chain, which is why they are generated around the
    assertion rather than fixed.

    **Validates: Requirements 2.3, 2.4**
    """
    image_bytes = render_image_bytes(*spec)

    with static_camera_provisioning(image_bytes) as (accessor, session, _):
        image_source_id = create_static_image_source(accessor, session)
        persisted = read_persisted_image_source(session, image_source_id)
        launch_string, _ = build_preview_launch(persisted, override=override)

        assert first_caps(launch_string) == RGB_FIRST_CAPS, (
            "create_buffer would declare the packed-RGB Pinned_Image bytes "
            "as {!r}; launch string: {}".format(
                first_caps(launch_string), launch_string)
        )
        assert "bayer2rgb" not in launch_string, (
            "a Bayer demosaic is applied to already-packed RGB, which "
            "destroys color (bugfix.md 1.4); launch string: {}".format(
                launch_string)
        )
        assert "video/x-bayer" not in launch_string


def test_live_image_source_preview_launch_is_rgb():
    """The live counterexample as a concrete case: provision the static
    camera through the REAL accessor, then build the preview pipeline under
    the live Image_Source id ``my6j3zx1`` and the crop the HMI sent, so the
    launch string is directly comparable to the one the device executed.

    Expected: the same launch string with the RGB passthrough chain in
    place of the BGGR demosaic. Today this produces
    ``LIVE_PREVIEW_LAUNCH`` verbatim (bugfix.md 1.4).

    **Validates: Requirements 2.3, 2.4**
    """
    expected_launch = LIVE_PREVIEW_LAUNCH.replace(
        bayer_default_chain(), RGB_PASSTHROUGH_CHAIN)
    image_bytes = render_image_bytes(64, 48, 7, "PNG")

    with static_camera_provisioning(image_bytes) as (accessor, session, _):
        image_source_id = create_static_image_source(accessor, session)
        persisted = read_persisted_image_source(session, image_source_id)
        # Only the filesink location depends on the id; substituting the
        # live one makes the comparison character-for-character.
        persisted["imageSourceId"] = LIVE_IMAGE_SOURCE_ID
        launch_string, _ = build_preview_launch(
            persisted, override={"imageCrop": LIVE_ZERO_CROP})

        assert launch_string == expected_launch, (
            "the executed preview pipeline for Image_Source {} on "
            "jetson-thor1 was:\n  {}".format(
                LIVE_IMAGE_SOURCE_ID, LIVE_PREVIEW_LAUNCH)
        )


# ---------------------------------------------------------------------------
# Requirement 2.5 — the migration half: already-stored Bayer chains
# converge without the user deleting and recreating the Image_Source
# ---------------------------------------------------------------------------


def test_backfill_is_wired_into_the_startup_hook():
    """The backfill runs from the EXISTING ``backfill()`` startup hook
    (``app.py`` line 273) alongside the two shipped migrations, rather than
    from a new hook.

    **Validates: Requirements 2.5**
    """
    from dao.sqlite_db import db_backfill

    resolve_backfill()
    source = inspect.getsource(db_backfill.backfill)
    assert BACKFILL_FUNCTION_NAME in source, (
        "db_backfill.backfill() does not call {}, so nothing runs the "
        "migration at startup:\n{}".format(BACKFILL_FUNCTION_NAME, source)
    )


def test_backfill_converges_the_live_stored_bayer_chain():
    """The exact live row — Image_Source ``my6j3zx1`` pointing at
    configuration ``jk1y5ln8`` with the ``default``/``default`` Bayer chain
    — is rewritten to the RGB passthrough chain, and a second run changes
    nothing (idempotence).

    **Validates: Requirements 2.5**
    """
    backfill_fn = resolve_backfill()

    with configuration_database() as session:
        add_camera_image_source(
            session,
            image_source_id=LIVE_IMAGE_SOURCE_ID,
            config_id=LIVE_IMAGE_SOURCE_CONFIG["imageSourceConfigId"],
            camera_id=STATIC_IMAGE_CAMERA_ID,
            processing_pipeline=LIVE_IMAGE_SOURCE_CONFIG[
                "processingPipeline"],
            gain=LIVE_IMAGE_SOURCE_CONFIG["gain"],
            exposure=LIVE_IMAGE_SOURCE_CONFIG["exposure"],
        )

        backfill_fn(session)
        session.commit()

        row = session.get(
            db_models.ImageSourceConfiguration,
            LIVE_IMAGE_SOURCE_CONFIG["imageSourceConfigId"],
        )
        assert row.processingPipeline == RGB_PASSTHROUGH_CHAIN
        # Acquisition values are not the backfill's business.
        assert row.gain == LIVE_IMAGE_SOURCE_CONFIG["gain"]
        assert row.exposure == LIVE_IMAGE_SOURCE_CONFIG["exposure"]

        after_first = stored_pipelines(session)
        backfill_fn(session)
        session.commit()
        assert stored_pipelines(session) == after_first


@settings(deadline=None, max_examples=10)
@given(
    sources=st.lists(
        st.tuples(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
                    min_size=6, max_size=8),
            st.integers(0, 100),
            st.integers(0, 1_000_000),
        ),
        min_size=1, max_size=3, unique_by=lambda entry: entry[0],
    )
)
def test_backfill_converges_every_stored_bayer_static_source(sources):
    """For ANY set of static-camera Image_Sources storing exactly the
    ``default``/``default`` Bayer chain, the backfill rewrites every one of
    them to the RGB passthrough chain and is idempotent.

    **Validates: Requirements 2.5**
    """
    backfill_fn = resolve_backfill()
    bayer_chain = bayer_default_chain()

    with configuration_database() as session:
        config_ids = []
        for index, (suffix, gain, exposure) in enumerate(sources):
            config_id = "cfg-{}".format(suffix)
            config_ids.append(config_id)
            add_camera_image_source(
                session,
                image_source_id="src-{}-{}".format(index, suffix),
                config_id=config_id,
                camera_id=STATIC_IMAGE_CAMERA_ID,
                processing_pipeline=bayer_chain,
                gain=gain,
                exposure=exposure,
            )

        backfill_fn(session)
        session.commit()

        rewritten = stored_pipelines(session)
        for config_id in config_ids:
            assert rewritten[config_id] == RGB_PASSTHROUGH_CHAIN

        backfill_fn(session)
        session.commit()
        assert stored_pipelines(session) == rewritten
