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
"""Preservation property tests for the appsrc frame stride fix (task 2).

Feature: appsrc-frame-stride-alignment.
**Property 2: Preservation Checking** — Validates: Requirements 1.9, 1.10,
1.11, 1.13, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12,
3.13.

Written OBSERVATION-FIRST: every number in the ``RECORDED_*`` block below
was produced by running the UNFIXED code in ``flask-app:latest``
(GStreamer 1.20.3, aarch64) and is asserted here so the fix cannot move
it. This suite MUST PASS before the fix and after it.

What the fix will do, and therefore what these tests pin against: one
shared helper pads packed ``video/x-raw`` rows up to
``GST_ROUND_UP_4(width * bytes_per_pixel)`` at the three wrapping sites,
and returns the IDENTICAL object when the data already matches the size
its caps imply. Its format table is ``RGB``/``BGR`` (3), ``RGBA``/``BGRA``
(4), ``GRAY8`` (1); everything else passes through untouched. The
loudness the fix adds comes from up-front validation inside that helper,
NOT from changing ``run_pipeline``'s bus handling — which is why
``acceptable_messages`` for ``status_sink=None`` is pinned here
character-for-character.

REAL GStreamer, on purpose
--------------------------
``gi`` / ``Gst`` / ``GstVideo`` / ``Aravis`` are importable in
``flask-app:latest``, so the wrapping site, the render chain and the bus
are exercised for real rather than mocked. The root ``conftest.py``
installs a stub ``gi`` (``mock_gi``) for the API-endpoint suites, so this
module purges it and re-imports the real bindings BEFORE importing
``gstreamer.gst_pipeline`` (see ``_restore_real_gi``).

What was found about Bayer striding (recorded so nobody re-derives it)
---------------------------------------------------------------------
GStreamer DOES stride ``video/x-bayer`` at ``GST_ROUND_UP_4(width)``:
``bayer2rgb``'s declared unit size is ``GST_ROUND_UP_4(width) * height``
(82416 for 813x101, 876960 for 810x1080). But ``video/x-bayer`` is NOT a
``GstVideoFormat``, so the ``gst_video_frame_map_id`` size check behind
this whole defect NEVER applies to it — that check only runs when a
element maps a buffer as a video frame against a ``GstVideoInfo``, and
``GstVideoInfo`` has no Bayer format to describe. Measured here on
unfixed code: a TIGHT Bayer buffer of an unaligned width (813x101 ->
82113 bytes, 303 short of the 82416 unit size) travels the
``bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert !
jpegenc ! filesink`` chain the device actually runs and produces exactly
one bus message — ``eos`` — plus a full-size JPEG. No warning, no error,
no padding. That is why the fix leaves Bayer alone, and why this file
asserts byte-identical buffers AND identical bus message sequences for
it: nine physical camera configurations across two devices ride that
path (``28183exv``/``g6zrsox3`` and ``o70qz7ci``/``563tiauk`` on
``jetson-thor1``; all 7 on the JP6 Orin), and this is the guard that the
physical Basler path is provably untouched (Requirement 3.4).

The existing suites are preservation coverage too (Requirement 3.13)
--------------------------------------------------------------------
``test/backend-test/static_image_camera/*`` (including the previous
spec's ``test_property_static_camera_pixel_format*.py``),
``test/backend-test/gstreamer/*`` and the Custom_Python bridge suites
must keep passing untouched. The single best existing witness is
``test/backend-test/static_image_camera/test_workflow_feed.py``: its
pinned image is 6x4, which makes ``6 * 3 = 18`` — NOT a multiple of 4 —
so that test is itself an UNALIGNED case, and its
``args == (expected_frame(pinned_store.image_bytes_for_test),)``
assertion proves the frame dict handed to ``run_pipeline`` stays
TIGHT-PACKED. If the fix ever pads ``frame_data['data']`` instead of the
bytes handed to ``Gst.Buffer.new_wrapped``, that assertion breaks. The
same fact is re-asserted executably here in
``TestFrameDictIsNeverMutated`` so this file carries the witness too.

A trap worth recording: ``Gst.Buffer.new_wrapped(b"")`` SEGFAULTS
GStreamer 1.20.3 (``gst_memory_new_wrapped: assertion 'data != NULL'``),
so every case below passes non-empty bytes even when the dimensions are
degenerate.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100). The
pipeline-running properties carry their own smaller ``max_examples``
because each example starts a real pipeline.
"""
import atexit
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment + REAL gi, before any backend import.
# ---------------------------------------------------------------------------

#: ``run_pipeline`` writes ``GST_DEBUG_FILE`` under COMPONENT_WORK_PATH and
#: ``utils.get_gst_plugins_path()`` reads INFERENCE_COMPONENT_DECOMPRESED_PATH.
WORK_PATH = tempfile.mkdtemp(prefix="appsrc-stride-preservation-")
os.environ["COMPONENT_WORK_PATH"] = WORK_PATH
os.environ.setdefault("INFERENCE_COMPONENT_DECOMPRESED_PATH", WORK_PATH)
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")
atexit.register(shutil.rmtree, WORK_PATH, True)


def _real_gi_already_loaded():
    """The REAL pygobject in ``sys.modules``, or ``None``.

    ``mock_gi``'s stub carries no ``get_required_version``, which is the
    same discriminator this module already used to reject a Mock.
    """
    loaded = sys.modules.get("gi")
    if loaded is not None and hasattr(loaded, "get_required_version"):
        return loaded
    return None


def _restore_real_gi():
    """Drop the root conftest's ``mock_gi`` stub and import the REAL
    bindings. Must run before ``gstreamer.gst_pipeline`` is imported, or
    that module would capture the Mock ``Gst``.

    IDEMPOTENT, because the sibling suite
    ``test_property_appsrc_frame_stride.py`` does the same thing at its own
    import and pytest imports both when the directory is collected as a
    whole. Purging a second time would FAIL: real pygobject also registers
    ``gobject`` in ``sys.modules``, and ``gi/__init__.py`` raises
    ``ImportError: ... must not import static modules like "gobject"`` when
    a fresh import sees that. So when the real bindings are already loaded
    we reuse them and purge nothing; only the stub is ever purged.
    ``require_version`` is safe to repeat for an already-loaded namespace
    at the same version.
    """
    gi = _real_gi_already_loaded()
    if gi is None:
        for name in [n for n in list(sys.modules)
                     if n == "gi" or n.startswith("gi.")]:
            del sys.modules[name]
        import gi

        if not hasattr(gi, "get_required_version"):  # the stub, not bindings
            raise RuntimeError(
                "the real gi bindings are not importable; this suite must "
                "run in the flask-app container")
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    gi.require_version("Aravis", "0.8")
    return gi


_gi = _restore_real_gi()

from gi.repository import Gst, GstVideo  # noqa: E402

# Initialize BEFORE the first run_pipeline call: run_pipeline sets
# GST_DEBUG=4 in the environment, which GStreamer only reads at init, so
# initializing here keeps the debug log (and the suite) small. Pixel
# results are unaffected.
Gst.init(None)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from exceptions.api.gst_pipeline_exception import (  # noqa: E402
    PipelineExecutionException,
)
from gstreamer import gst_pipeline  # noqa: E402
from gstreamer.gst_pipeline import GstPipelineManager  # noqa: E402

# The static-camera strategies live in a sibling test directory; pytest only
# puts a test file's own directory on sys.path (the same shim
# test/backend-test/static_image_camera/test_workflow_feed.py uses at lines
# 49-56).
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_TEST = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(os.path.dirname(_BACKEND_TEST))
_STATIC_TESTS = os.path.join(_BACKEND_TEST, "static_image_camera")
if _STATIC_TESTS not in sys.path:
    sys.path.insert(0, _STATIC_TESTS)

from static_image_strategies import (  # noqa: E402
    expected_frame,
    image_specs,
    render_image_bytes,
)

# ===========================================================================
# RECORDED BASELINE — measured on UNFIXED code in flask-app:latest
# (GStreamer 1.20.3). Every value below is an observation, not a guess.
# ===========================================================================

#: The formats the fix's helper will know, with their bytes per pixel.
BYTES_PER_PIXEL = {"RGB": 3, "BGR": 3, "RGBA": 4, "BGRA": 4, "GRAY8": 1}

#: ``GstVideo.VideoInfo.new_from_caps`` for the device dimensions —
#: recorded from the container and re-derived from GStreamer in the tests.
#: (format, width, height) -> (stride, size, tight size)
RECORDED_VIDEO_INFO = {
    ("RGB", 810, 1080): (2432, 2626560, 2624400),   # unaligned (the defect)
    ("RGB", 773, 512): (2320, 1187840, 1187328),    # unaligned (the defect)
    ("RGB", 768, 576): (2304, 1327104, 1327104),    # ALIGNED
    ("RGB", 1280, 720): (3840, 2764800, 2764800),   # ALIGNED
    ("RGBA", 810, 1080): (3240, 3499200, 3499200),  # 4 bpp -> always aligned
    ("BGRA", 773, 512): (3092, 1583104, 1583104),   # 4 bpp -> always aligned
    ("GRAY8", 810, 1080): (812, 876960, 874800),    # unaligned (810 % 4 != 0)
    ("GRAY8", 812, 1080): (812, 876960, 876960),    # ALIGNED (812 % 4 == 0)
}

#: The two ALIGNED device dimensions, rendered through the real
#: ``videoconvert ! jpegenc idct-method=2 quality=100 ! filesink`` chain
#: with the deterministic gradient content ``gradient_bytes`` produces.
#: Recorded: source mean, JPEG mean, correlation, max per-pixel error.
RECORDED_ALIGNED_RENDER = {
    (768, 576): {
        "source_mean": (127.5, 127.5, 127.5),
        "jpeg_mean": (127.5039, 127.5039, 127.5039),
        "correlation": 0.999999,
        "max_error": 1.0,
    },
    (1280, 720): {
        "source_mean": (127.5, 127.5, 127.5),
        "jpeg_mean": (127.5039, 127.5039, 127.5039),
        "correlation": 0.999999,
        "max_error": 1.0,
    },
}

#: The envelope observed across every aligned case measured (4x4, 40x30,
#: 100x50, 768x576, 1280x720): the max per-pixel error was EXACTLY 1.0 at
#: every size, correlation never below 0.999954, and the per-channel mean
#: drifted at most 0.125.
RECORDED_MAX_PIXEL_ERROR = 1.0
RECORDED_MIN_CORRELATION = 0.99995
RECORDED_MAX_MEAN_DRIFT = 0.2
#: ...but mean drift and correlation are AVERAGES, so on a frame of a
#: handful of pixels a single +-1 JPEG rounding dominates them: 4x2
#: measured a 0.25 mean drift (47.00 -> 47.25), outside the 0.2 envelope
#: above while still inside the per-pixel bound of 1.0. The averaged
#: bounds are therefore asserted only from this frame size up, which is
#: the regime they were measured in; the per-pixel bound is asserted
#: everywhere.
RECORDED_AVERAGED_BOUND_MIN_PIXELS = 1024

#: ``video/x-bayer`` on unfixed code: wrapped size == tight size (no
#: padding), byte-identical buffer, and the device chain posts EXACTLY one
#: bus message. Recorded for aligned (812) and unaligned (813, 810)
#: widths at 1 byte per pixel.
RECORDED_BAYER_BUS_SEQUENCE = [("eos", "<pipeline>", None)]
RECORDED_BAYER_UNIT_SIZES = {(813, 101): 82416, (810, 1080): 876960}

#: ``create_buffer``'s caps derivation, recorded verbatim.
RECORDED_CAPS_APPEND = " ,width={0} , height={1}"

#: ``run_pipeline``'s bus contract on the ``status_sink=None`` path.
RECORDED_ACCEPTABLE_MESSAGES_LINE = (
    "            acceptable_messages = [Gst.MessageType.ERROR, "
    "Gst.MessageType.EOS, Gst.MessageType.TAG]"
)
RECORDED_SINK_ONLY_MESSAGES = ("WARNING", "STATE_CHANGED")

#: A short ``I420`` buffer (a format OUTSIDE the helper's table, so it
#: passes through before AND after the fix) makes ``videoconvert`` post a
#: generic warning. Recorded: with ``status_sink=None`` ``run_pipeline``
#: returns ``{}`` and the file is still written; with a sink, the warning
#: and the STATE_CHANGED "running" signals arrive.
RECORDED_BENIGN_WARNING_RETURN = {}
RECORDED_WARNING_TEXT = "Internal GStreamer error: code not implemented."
RECORDED_RUNNING_ELEMENTS = {
    "appsrc", "videoconvert", "jpegenc", "filesink", "<pipeline>"}

#: The ERROR-capture / raise-after-``loop.run()`` shape.
RECORDED_ERROR_PREFIX = "Pipeline failed with: "
RECORDED_IDENTITY_ERROR_TEXT = "Failed after iterations as requested."

#: The watchdog message, with PIPELINE_TIMEOUT_SEC patched down to 2s.
RECORDED_TIMEOUT_MESSAGE = (
    "Pipeline timed out after {0}s without completing (no EOS/ERROR "
    "received).")
RECORDED_PIPELINE_TIMEOUT_SEC = 120

#: ``pipeline_executor._frame_caps`` for the three shapes that matter.
RECORDED_FRAME_CAPS = {
    "rgb_tagged": "video/x-raw,format=RGB",
    "bayer_tagged": "video/x-bayer,format=bggr",
    "untagged_3bpp": "video/x-raw,format=RGB",
    "untagged_1bpp": "video/x-raw,format=GRAY8",
    "untagged_4bpp": "video/x-raw,format=RGBA",
    "untagged_ragged": "video/x-raw,format=GRAY8",
}

#: ``dda_frames`` — the already-stride-aware read/write helpers.
RECORDED_FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}
RECORDED_TO_ARRAY_TOO_SHORT = (
    "to_array: frame bytes too short: got 30 bytes for 5x3 RGB, which "
    "needs at least 45 bytes")
RECORDED_TO_ARRAY_BAD_FORMAT = (
    "to_array: unsupported format 'NV12' (supported: BGR, GRAY8, RGB, "
    "RGBA)")

#: The previous spec's deployed surface (Requirement 3.9). The digests are
#: of the shipped JSON file and of the backfill FUNCTION's source (not the
#: whole module, so an unrelated migration added elsewhere cannot fail
#: this). If another spec legitimately changes either, re-record here.
RECORDED_RGB_CHAIN = "capsfilter caps=video/x-raw,format=RGB ! videoconvert"
RECORDED_CONFIG_SHA256 = (
    "d2aee820d3678f42fdb680db693391aff96dceaccac44f012d42613038bf177e")
RECORDED_BACKFILL_NAME = "migration_static_camera_pipeline_db"
RECORDED_BACKFILL_SHA256 = (
    "11865d93a7a6cac1014428b9dd31fbccb62b6b8c1cd4b595f322918d2abb9f69")

DEFAULT_CAMERA_CONFIG_PATH = os.path.join(
    _REPO_ROOT, "src", "backend", "utils", "config",
    "default_camera_configurations.json")


# ===========================================================================
# Helpers
# ===========================================================================


def round_up_4(value):
    return ((value + 3) // 4) * 4


def video_info(frame_format, width, height):
    """GStreamer's own answer for a packed ``video/x-raw`` caps."""
    caps = Gst.Caps.from_string(
        "video/x-raw,format={0},width={1},height={2}".format(
            frame_format, width, height))
    return GstVideo.VideoInfo.new_from_caps(caps)


def gradient_bytes(width, height, channels, seed=0):
    """Deterministic, smooth frame content — row-major, tightly packed,
    ``(x * 3 + y * 5 + c * 40 + seed) % 256``.

    Smooth on purpose: it survives the render chain's chroma subsampling,
    so a fidelity regression is attributable to the buffer rather than to
    JPEG. The recorded means below were measured with exactly this
    content.
    """
    x = np.arange(width, dtype=np.int64).reshape(1, width, 1) * 3
    y = np.arange(height, dtype=np.int64).reshape(height, 1, 1) * 5
    c = np.arange(channels, dtype=np.int64).reshape(1, 1, channels) * 40
    return ((x + y + c + seed) % 256).astype(np.uint8).tobytes()


def manager():
    return GstPipelineManager()


def jpeg_launch(frame_format, path):
    return ("appsrc name=appsrc caps=video/x-raw,format={0} ! videoconvert "
            "! jpegenc idct-method=2 quality=100 ! filesink location={1}"
            ).format(frame_format, path)


def bayer_launch(path):
    """The chain the device actually runs for a Bayer camera."""
    return ("appsrc name=appsrc caps=video/x-bayer,format=bggr ! bayer2rgb "
            "! capsfilter caps=video/x-raw,format=RGBA ! videoconvert "
            "! jpegenc ! filesink location={0}").format(path)


def out_path(name):
    return os.path.join(WORK_PATH, "{0}-{1}.jpg".format(name, time.time_ns()))


def wrap_frame(launch, frame_data):
    """Drive the REAL ``GstPipelineManager.create_buffer`` and report
    everything the preservation properties need:

    ``(source, buffer, wrapped_object)`` where ``wrapped_object`` is the
    object ``create_buffer`` handed to ``Gst.Buffer.new_wrapped``.

    That last value is the non-vacuous half of the no-copy guarantee: on
    unfixed code it is ``frame_data['data']`` itself, and after the fix it
    must STILL be ``frame_data['data']`` for every aligned/pass-through
    case, because the helper returns the identical object rather than an
    equal copy (Requirements 2.2, 3.1).
    """
    captured = []
    original = Gst.Buffer.new_wrapped

    def spy(data):
        captured.append(data)
        return original(data)

    Gst.Buffer.new_wrapped = spy
    try:
        pipeline = Gst.parse_launch(launch)
        source, buffer = manager().create_buffer(launch, pipeline, frame_data)
        pipeline.set_state(Gst.State.NULL)
    finally:
        Gst.Buffer.new_wrapped = original
    assert len(captured) == 1, "create_buffer wrapped {0} buffers".format(
        len(captured))
    return source, buffer, captured[0]


def buffer_bytes(buffer):
    return buffer.extract_dup(0, buffer.get_size())


def load_stride_helper():
    """``gstreamer.frame_stride.reconcile_to_caps_stride`` once task 3.1
    lands, else ``None``.

    Task 2 runs BEFORE the fix, so the helper does not exist yet; the
    tests below express their assertions through ``reconcile`` so they
    say something true today (the wrapping sites pass the frame bytes
    straight through) and BIND to the real helper the moment it appears,
    with no edit to this file.
    """
    try:
        from gstreamer.frame_stride import reconcile_to_caps_stride
    except ImportError:
        return None
    return reconcile_to_caps_stride


def reconcile(data, caps_string, width=None, height=None, strict=True):
    helper = load_stride_helper()
    if helper is None:
        # Pre-fix: nothing reconciles anything, the bytes go through as is.
        return data
    return helper(data, caps_string, width, height, strict=strict)


def jpeg_array(path):
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB")).astype(float)


def channel_means(array):
    return tuple(round(float(array[:, :, c].mean()), 4) for c in range(3))


def correlation(left, right):
    left = left.reshape(-1).astype(float)
    right = right.reshape(-1).astype(float)
    if left.std() == 0 or right.std() == 0:
        return None
    return float(np.corrcoef(left, right)[0, 1])


def normalized_element(name):
    """GStreamer numbers auto-named elements per process, so the same
    chain yields ``videoconvert1`` or ``videoconvert7`` depending on what
    ran before. Compare the stable stem."""
    if name is None:
        return None
    stem = re.sub(r"\d+$", "", name)
    return "<pipeline>" if stem == "pipeline" else stem


def record_bus(launch, frame_data, timeout_ns=15 * Gst.SECOND):
    """The bus message sequence for a real run of ``launch``, wrapped by
    the REAL ``create_buffer``.

    Deliberately drained with ``timed_pop_filtered`` instead of
    ``run_pipeline``'s signal watch + GLib loop: the sequence is a
    property of GStreamer and the buffer, and this way it is deterministic
    and always terminates.
    """
    pipeline = Gst.parse_launch(launch)
    source, buffer = manager().create_buffer(launch, pipeline, frame_data)
    bus = pipeline.get_bus()
    sequence = []
    pipeline.set_state(Gst.State.PLAYING)
    source.emit("push-buffer", buffer)
    source.emit("end-of-stream")
    try:
        while True:
            message = bus.timed_pop_filtered(
                timeout_ns,
                Gst.MessageType.ERROR | Gst.MessageType.EOS
                | Gst.MessageType.WARNING | Gst.MessageType.TAG)
            if message is None:
                sequence.append(("timeout", None, None))
                break
            detail = None
            if message.type == Gst.MessageType.WARNING:
                detail = message.parse_warning()[0].message
            elif message.type == Gst.MessageType.ERROR:
                detail = message.parse_error()[0].message
            sequence.append((
                Gst.MessageType.get_name(message.type),
                normalized_element(
                    message.src.get_name() if message.src else None),
                detail,
            ))
            if message.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
                break
    finally:
        pipeline.set_state(Gst.State.NULL)
    return buffer, sequence


def load_dda_frames():
    """Exec ``HELPERS_SOURCE`` as the Python_Runner does."""
    from workflow_engine.python_bridge import HELPERS_SOURCE

    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


def load_invoke_process_frame():
    """The REAL shipped ``_invoke_process_frame``, sliced out of
    ``RUNNER_SOURCE`` (exec'ing the whole runner would call ``main()`` and
    block on stdin). The function is self-contained: it imports numpy
    itself and takes ``dda_frames`` as an argument."""
    from workflow_engine.python_bridge import RUNNER_SOURCE

    start = RUNNER_SOURCE.index("def _invoke_process_frame(")
    end = RUNNER_SOURCE.index("\ndef _invoke_handle(", start)
    namespace = {}
    exec(RUNNER_SOURCE[start:end], namespace)
    return namespace["_invoke_process_frame"]


def pad_rows(data, row_bytes, stride, height):
    return b"".join(
        data[row * row_bytes:(row + 1) * row_bytes]
        + b"\x00" * (stride - row_bytes)
        for row in range(height))


# ---------------------------------------------------------------------------
# Generators.
#
# Task 1 owns ``test/backend-test/gstreamer/stride_strategies.py``, which was
# being authored concurrently with this file: the aligned-half generators
# below are deliberately local so this suite's green result does not depend
# on a module another task is still shaping. They are small and specific to
# the preservation half (ALIGNED widths, 4-bpp formats, pass-through caps);
# consolidating them into ``stride_strategies`` later is a safe follow-up.
# The cross-directory reuse that matters — ``render_image_bytes`` /
# ``expected_frame`` / ``image_specs`` — comes from
# ``static_image_camera/static_image_strategies.py`` above, not duplicated.
# ---------------------------------------------------------------------------

#: ALIGNED RGB widths: ``width * 3 % 4 == 0`` iff ``width % 4 == 0``.
aligned_rgb_widths = st.integers(min_value=1, max_value=32).map(lambda k: 4 * k)
#: GRAY8 is aligned exactly when ``width % 4 == 0``.
aligned_gray_widths = aligned_rgb_widths
unaligned_gray_widths = st.integers(min_value=1, max_value=127).filter(
    lambda w: w % 4 != 0)
#: 4-bytes-per-pixel formats are aligned for EVERY width.
any_widths = st.integers(min_value=1, max_value=97)
small_heights = st.integers(min_value=1, max_value=24)

#: Caps the helper must never touch: another media type, or a
#: ``video/x-raw`` format outside its table.
pass_through_caps = st.sampled_from([
    "image/jpeg",
    "video/x-bayer,format=bggr",
    "video/x-bayer,format=rggb",
    "video/x-raw,format=I420",
    "video/x-raw,format=NV12",
    "video/x-raw,format=YUY2",
    "video/x-raw,format=BGRx",
    "video/x-raw,format=GRAY16_LE",
])


# ===========================================================================
# Guard: this suite is only meaningful against the REAL bindings
# ===========================================================================


def test_real_gstreamer_bindings_are_in_use():
    """The root conftest installs a Mock ``gi`` for the API-endpoint
    suites. Against a Mock every assertion in this file would be vacuous,
    so assert up front that the real bindings are loaded and that the
    module under test shares them.

    **Validates: Requirements 3.12**
    """
    assert _gi.__file__ and os.path.exists(_gi.__file__)
    assert Gst.version_string().startswith("GStreamer 1.")
    assert gst_pipeline.Gst is Gst
    assert isinstance(Gst.Buffer.new_wrapped(b"probe"), Gst.Buffer)
    assert video_info("RGB", 810, 1080).size == 2626560


# ===========================================================================
# Requirements 2.2, 3.1 — aligned widths byte-identical AND copy-free
# ===========================================================================


class TestAlignedWidthsAreByteIdenticalAndCopyFree:
    """``FOR ALL X WHERE isPackedRawVideo(X) AND isAligned(X): wrapFrame'(X)
    IS wrapFrame(X)`` — the same object, not an equal one."""

    @settings(deadline=None, max_examples=25)
    @given(width=aligned_rgb_widths, height=small_heights)
    @example(width=768, height=576)
    @example(width=1280, height=720)
    def test_wrapped_size_equals_tight_and_caps_implied_size(self, width,
                                                            height):
        """For an aligned width the tight buffer ALREADY satisfies the
        stride its caps imply, so ``create_buffer``'s wrapped size stays
        ``len(data)`` and equals ``GstVideoInfo.size``.

        **Validates: Requirements 3.1**
        """
        data = gradient_bytes(width, height, 3)
        launch = jpeg_launch("RGB", out_path("aligned"))
        info = video_info("RGB", width, height)

        _, buffer, _ = wrap_frame(
            launch, {"data": data, "width": width, "height": height})

        assert info.stride[0] == width * 3 == round_up_4(width * 3)
        assert info.size == len(data)
        assert buffer.get_size() == len(data)
        assert buffer_bytes(buffer) == data

    @settings(deadline=None, max_examples=25)
    @given(width=aligned_rgb_widths, height=small_heights)
    @example(width=768, height=576)
    @example(width=1280, height=720)
    def test_create_buffer_passes_the_identical_object_to_new_wrapped(
            self, width, height):
        """The NON-VACUOUS half of the no-copy guarantee: the object
        ``create_buffer`` hands to ``Gst.Buffer.new_wrapped`` IS
        ``frame_data['data']``. After the fix the helper must return that
        identical object for an aligned width, so this assertion keeps
        holding — an equal copy would break it.

        **Validates: Requirements 2.2, 3.1**
        """
        data = gradient_bytes(width, height, 3)
        frame = {"data": data, "width": width, "height": height}

        _, _, wrapped = wrap_frame(jpeg_launch("RGB", out_path("id")), frame)

        assert wrapped is data
        assert frame["data"] is data

    @settings(deadline=None, max_examples=25)
    @given(width=aligned_rgb_widths, height=small_heights)
    @example(width=768, height=576)
    @example(width=1280, height=720)
    def test_reconciliation_returns_the_identical_object(self, width, height):
        """The same guarantee at the helper boundary. Pre-fix this pins
        that nothing reconciles the bytes at all; post-fix it binds to
        ``reconcile_to_caps_stride`` and pins ``result is data``.

        **Validates: Requirements 2.2, 3.1**
        """
        data = gradient_bytes(width, height, 3)
        caps = "video/x-raw,format=RGB ,width={0} , height={1}".format(
            width, height)

        assert reconcile(data, caps, width, height) is data

    @pytest.mark.parametrize("width,height", sorted(RECORDED_ALIGNED_RENDER))
    def test_recorded_aligned_render_is_unchanged(self, width, height):
        """The two ALIGNED device dimensions, rendered through the real
        ``videoconvert ! jpegenc idct-method=2 quality=100 ! filesink``
        chain, still produce the recorded per-channel means, correlation
        and max per-pixel error. These are the cases the fix must be a
        NO-OP for (`dog.jpg` 768x576, `zidane.jpg` 1280x720 on device).

        **Validates: Requirements 3.1**
        """
        recorded = RECORDED_ALIGNED_RENDER[(width, height)]
        data = gradient_bytes(width, height, 3)
        path = out_path("render-{0}x{1}".format(width, height))
        launch = jpeg_launch("RGB", path)

        result = manager().run_pipeline(
            launch, {"data": data, "width": width, "height": height})

        assert result == {}
        source = np.frombuffer(data, dtype=np.uint8).reshape(
            height, width, 3).astype(float)
        rendered = jpeg_array(path)
        assert rendered.shape == source.shape
        assert channel_means(source) == recorded["source_mean"]
        for observed, expected in zip(channel_means(rendered),
                                      recorded["jpeg_mean"]):
            assert abs(observed - expected) <= 0.01
        assert correlation(source, rendered) >= recorded["correlation"] - 1e-6
        assert float(abs(source - rendered).max()) <= recorded["max_error"]

    @settings(deadline=None, max_examples=8)
    @given(width=aligned_rgb_widths, height=st.integers(min_value=2,
                                                       max_value=40))
    def test_generated_aligned_renders_stay_faithful(self, width, height):
        """Every aligned case renders the store's pixels. The per-pixel
        bound (max error 1.0) is asserted at every size; the averaged
        bounds (mean drift, correlation) are asserted from
        ``RECORDED_AVERAGED_BOUND_MIN_PIXELS`` up, the regime they were
        measured in — see that constant for the 4x2 measurement that
        motivates the split. Each example starts a real pipeline, hence
        the small example budget.

        **Validates: Requirements 3.1**
        """
        data = gradient_bytes(width, height, 3)
        path = out_path("gen-render")
        launch = jpeg_launch("RGB", path)

        assert manager().run_pipeline(
            launch, {"data": data, "width": width, "height": height}) == {}

        source = np.frombuffer(data, dtype=np.uint8).reshape(
            height, width, 3).astype(float)
        rendered = jpeg_array(path)
        assert rendered.shape == source.shape
        assert float(abs(source - rendered).max()) <= RECORDED_MAX_PIXEL_ERROR
        if width * height >= RECORDED_AVERAGED_BOUND_MIN_PIXELS:
            for observed, expected in zip(channel_means(rendered),
                                          channel_means(source)):
                assert abs(observed - expected) <= RECORDED_MAX_MEAN_DRIFT
            measured = correlation(source, rendered)
            if measured is not None:
                assert measured >= RECORDED_MIN_CORRELATION


# ===========================================================================
# Requirement 3.4 — video/x-bayer untouched, end to end
# ===========================================================================


class TestBayerPathIsProvablyUntouched:
    """The guard for the physical Basler cameras. See the module docstring
    for what was found about Bayer striding."""

    @settings(deadline=None, max_examples=15)
    @given(width=any_widths, height=small_heights)
    @example(width=812, height=20)   # aligned
    @example(width=813, height=20)   # unaligned
    @example(width=810, height=20)   # unaligned (the device's width)
    def test_bayer_buffers_are_byte_identical_and_never_padded(self, width,
                                                              height):
        """A ``video/x-bayer`` frame is wrapped at its own tight size, the
        identical object reaches ``Gst.Buffer.new_wrapped``, and the bytes
        come back unchanged — for aligned AND unaligned widths.

        **Validates: Requirements 3.4**
        """
        data = gradient_bytes(width, height, 1)
        caps = "video/x-bayer,format=bggr ,width={0} , height={1}".format(
            width, height)

        _, buffer, wrapped = wrap_frame(
            bayer_launch(out_path("bayer")),
            {"data": data, "width": width, "height": height})

        assert buffer.get_size() == len(data) == width * height
        assert buffer_bytes(buffer) == data
        assert wrapped is data
        # ...and at the helper boundary: video/x-bayer is not video/x-raw,
        # so it falls through the helper's first branch untouched.
        assert reconcile(data, caps, width, height) is data

    @pytest.mark.parametrize("width,height", [(812, 20), (813, 20),
                                              (810, 20), (813, 101)])
    def test_bayer_bus_message_sequence_is_unchanged(self, width, height):
        """The device's ``bayer2rgb ! capsfilter
        caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc ! filesink``
        chain posts EXACTLY ``eos`` for a tight Bayer buffer, aligned or
        not, and writes a real JPEG. No warning, no error, no padding.

        **Validates: Requirements 3.4**
        """
        data = gradient_bytes(width, height, 1)
        path = out_path("bayer-bus")

        buffer, sequence = record_bus(
            bayer_launch(path),
            {"data": data, "width": width, "height": height})

        assert buffer_bytes(buffer) == data
        assert sequence == RECORDED_BAYER_BUS_SEQUENCE
        assert os.path.exists(path) and os.path.getsize(path) > 0

    @pytest.mark.parametrize("width,height", [(812, 20), (813, 20),
                                             (810, 20)])
    def test_bayer_run_pipeline_returns_normally(self, width, height):
        """The same chain through the REAL ``run_pipeline`` with
        ``status_sink=None``: returns its normal empty tag dict.

        **Validates: Requirements 3.4, 3.6**
        """
        data = gradient_bytes(width, height, 1)
        path = out_path("bayer-run")

        result = manager().run_pipeline(
            bayer_launch(path),
            {"data": data, "width": width, "height": height})

        assert result == {}
        assert os.path.getsize(path) > 0

    @pytest.mark.parametrize("dims,unit_size",
                             sorted(RECORDED_BAYER_UNIT_SIZES.items()))
    def test_recorded_bayer_unit_size_rule(self, dims, unit_size):
        """``bayer2rgb``'s declared unit size is
        ``GST_ROUND_UP_4(width) * height`` — recorded so a future reader
        does not re-derive it. A tight buffer of an unaligned width is
        therefore SHORT of it and still passes through (above), because
        ``video/x-bayer`` is not a ``GstVideoFormat`` and
        ``gst_video_frame_map_id``'s check never applies to it.

        **Validates: Requirements 3.4**
        """
        width, height = dims
        assert round_up_4(width) * height == unit_size
        # GstVideoInfo cannot describe Bayer — "bggr" is not a
        # GstVideoFormat — which is exactly why gst_video_frame_map_id's
        # size check, the check behind this whole defect, never fires for
        # video/x-bayer.
        assert GstVideo.VideoFormat.from_string("bggr") == \
            GstVideo.VideoFormat.UNKNOWN
        assert GstVideo.VideoFormat.from_string("RGB") == \
            GstVideo.VideoFormat.RGB


# ===========================================================================
# Requirement 3.10 — pass-through cases that must never raise
# ===========================================================================


class TestPassThroughCasesNeverRaise:

    @settings(deadline=None, max_examples=25)
    @given(caps_clause=pass_through_caps, width=any_widths,
           height=small_heights)
    def test_unknown_media_types_and_formats_pass_through(self, caps_clause,
                                                          width, height):
        """Caps that are not ``video/x-raw``, or a ``video/x-raw`` format
        outside the helper's table, wrap byte-identically with no
        exception — the buffer size stays ``len(data)`` whatever the caps
        would imply.

        **Validates: Requirements 3.10**
        """
        data = gradient_bytes(width, height, 2)
        launch = "appsrc name=appsrc caps={0} ! fakesink".format(caps_clause)
        caps = "{0} ,width={1} , height={2}".format(caps_clause, width, height)

        _, buffer, wrapped = wrap_frame(
            launch, {"data": data, "width": width, "height": height})

        assert buffer.get_size() == len(data)
        assert buffer_bytes(buffer) == data
        assert wrapped is data
        assert reconcile(data, caps, width, height) is data

    @pytest.mark.parametrize("width,height", [(810, 0), (810, -1), (0, 10),
                                              (-5, 10)])
    def test_non_positive_dimensions_pass_through(self, width, height):
        """A non-positive height (or width) is wrapped exactly as today:
        the dimensions still land in the caps string verbatim, the buffer
        is the frame's own bytes, nothing raises.

        (Non-empty bytes on purpose — ``Gst.Buffer.new_wrapped(b"")``
        segfaults GStreamer 1.20.3.)

        **Validates: Requirements 3.10**
        """
        data = gradient_bytes(10, 1, 3)
        launch = "appsrc name=appsrc caps=video/x-raw,format=RGB ! fakesink"

        source, buffer, wrapped = wrap_frame(
            launch, {"data": data, "width": width, "height": height})

        assert buffer_bytes(buffer) == data
        assert wrapped is data
        caps_string = source.get_property("caps").to_string()
        assert "width=(int){0}".format(width) in caps_string
        assert "height=(int){0}".format(height) in caps_string
        assert reconcile(data, caps_string, width, height) is data

    def test_caps_without_width_or_height_pass_through(self):
        """Caps carrying no dimensions cannot imply a stride, so the
        reconciliation is a pass-through. ``create_buffer`` always appends
        the frame's dimensions, so this case is only reachable at the
        helper boundary — which is exactly where the fix has to tolerate
        it (the bridge-output site reads dimensions off negotiated caps
        that may omit them).

        **Validates: Requirements 3.10**
        """
        data = gradient_bytes(9, 4, 3)
        for caps in ("video/x-raw,format=RGB",
                     "video/x-raw,format=RGB,width=9",
                     "video/x-raw,format=RGB,height=4",
                     "video/x-raw",
                     ""):
            assert reconcile(data, caps, None, None) is data
            assert reconcile(data, caps, None, None, strict=False) is data


# ===========================================================================
# Requirement 3.5 — inherently aligned formats stay no-ops
# ===========================================================================


class TestInherentlyAlignedFormats:

    @settings(deadline=None, max_examples=25)
    @given(frame_format=st.sampled_from(["RGBA", "BGRA"]), width=any_widths,
           height=small_heights)
    @example(frame_format="RGBA", width=810, height=1080)
    @example(frame_format="BGRA", width=773, height=512)
    def test_four_byte_formats_are_aligned_for_every_width(
            self, frame_format, width, height):
        """4 bytes per pixel means every row is already a multiple of 4:
        GStreamer's own stride equals the tight row size, so the
        reconciliation is a no-op by construction.

        **Validates: Requirements 3.5**
        """
        info = video_info(frame_format, width, height)
        tight = width * 4 * height

        assert info.stride[0] == width * 4
        assert info.size == tight

        if width * height <= 4096:  # keep the real wrap cheap
            data = gradient_bytes(width, height, 4)
            _, buffer, wrapped = wrap_frame(
                "appsrc name=appsrc caps=video/x-raw,format={0} ! "
                "fakesink".format(frame_format),
                {"data": data, "width": width, "height": height})
            assert buffer_bytes(buffer) == data
            assert wrapped is data

    @settings(deadline=None, max_examples=25)
    @given(width=aligned_gray_widths, height=small_heights)
    @example(width=812, height=1080)
    def test_gray8_aligned_widths_are_byte_identical(self, width, height):
        """``GRAY8`` at ``width % 4 == 0``: tight == caps-implied, so the
        wrapped buffer stays byte-identical.

        **Validates: Requirements 3.5**
        """
        info = video_info("GRAY8", width, height)
        assert info.stride[0] == width
        assert info.size == width * height

        if width * height <= 4096:
            data = gradient_bytes(width, height, 1)
            _, buffer, wrapped = wrap_frame(
                "appsrc name=appsrc caps=video/x-raw,format=GRAY8 ! fakesink",
                {"data": data, "width": width, "height": height})
            assert buffer_bytes(buffer) == data
            assert wrapped is data
            assert reconcile(
                data,
                "video/x-raw,format=GRAY8 ,width={0} , height={1}".format(
                    width, height), width, height) is data

    @settings(deadline=None, max_examples=25)
    @given(width=unaligned_gray_widths, height=small_heights)
    @example(width=810, height=1080)
    def test_gray8_unaligned_widths_gain_exactly_the_reported_padding(
            self, width, height):
        """``GRAY8`` at ``width % 4 != 0`` is the one aligned-format
        exception: GStreamer reports ``GST_ROUND_UP_4(width)``, so the
        padding the fix must add is EXACTLY
        ``(GST_ROUND_UP_4(width) - width) * height`` — no more, no less.
        Asserted against ``GstVideo.VideoInfo``, so the amount is
        GStreamer's answer and not ours.

        **Validates: Requirements 3.5**
        """
        info = video_info("GRAY8", width, height)
        tight = width * height

        assert info.stride[0] == round_up_4(width) > width
        assert info.size - tight == (round_up_4(width) - width) * height
        assert info.size == round_up_4(width) * height

    @pytest.mark.parametrize("key,recorded",
                             sorted(RECORDED_VIDEO_INFO.items()))
    def test_recorded_video_info_numbers_are_unchanged(self, key, recorded):
        """The recorded stride/size/tight triples for every device
        dimension — including the two the defect exposes — still come back
        from GStreamer exactly as measured. This is what keeps the format
        table honest.

        **Validates: Requirements 3.5**
        """
        frame_format, width, height = key
        stride, size, tight = recorded
        info = video_info(frame_format, width, height)

        assert info.stride[0] == stride
        assert info.size == size
        assert width * BYTES_PER_PIXEL[frame_format] * height == tight
        assert round_up_4(width * BYTES_PER_PIXEL[frame_format]) == stride


# ===========================================================================
# Requirement 3.6 — run_pipeline's bus behavior unchanged
# ===========================================================================


class TestRunPipelineBusBehaviorUnchanged:

    def test_acceptable_messages_for_no_status_sink_is_error_eos_tag(self):
        """The load-bearing pin for the "loudness by up-front validation,
        NOT by promoting bus warnings" decision (Requirement 2.5): on the
        ``status_sink=None`` path ``acceptable_messages`` stays exactly
        ``[ERROR, EOS, TAG]``, and WARNING / STATE_CHANGED are added ONLY
        under ``if status_sink is not None``.

        **Validates: Requirements 3.6**
        """
        source = inspect.getsource(GstPipelineManager.run_pipeline)
        assignments = [line for line in source.splitlines()
                       if "acceptable_messages = [" in line]

        assert assignments == [RECORDED_ACCEPTABLE_MESSAGES_LINE]
        additive = source.split("if status_sink is not None:", 1)[1]
        for message in RECORDED_SINK_ONLY_MESSAGES:
            assert "Gst.MessageType.{0}".format(message) in additive
            # ...and nowhere else in the function.
            assert source.count("Gst.MessageType.{0}".format(message)) == \
                additive.count("Gst.MessageType.{0}".format(message))
        assert gst_pipeline.PIPELINE_TIMEOUT_SEC == \
            RECORDED_PIPELINE_TIMEOUT_SEC

    def test_benign_warning_is_still_ignored_with_no_status_sink(self):
        """A short ``I420`` buffer (a format OUTSIDE the helper's table,
        so it passes through before and after the fix) makes
        ``videoconvert`` post a WARNING. With ``status_sink=None`` that
        warning is dropped, the pipeline reaches EOS, and
        ``run_pipeline`` returns its normal dict — the behavior several
        existing flows depend on, which is why the fix does not promote
        WARNINGs to fatal.

        **Validates: Requirements 3.6, 3.10**
        """
        path = out_path("warn-none")
        launch = ("appsrc name=appsrc caps=video/x-raw,format=I420 ! "
                  "videoconvert ! jpegenc ! filesink location={0}"
                  ).format(path)
        frame = {"data": gradient_bytes(810 * 20 * 3 // 2, 1, 1),
                 "width": 810, "height": 20}

        _, sequence = record_bus(launch, dict(frame))
        result = manager().run_pipeline(launch, dict(frame))

        assert result == RECORDED_BENIGN_WARNING_RETURN
        assert [entry[0] for entry in sequence] == ["warning", "eos"]
        assert sequence[0][1] == "videoconvert"
        assert sequence[0][2].startswith(RECORDED_WARNING_TEXT)
        assert os.path.exists(path)

    def test_warning_and_state_changed_reach_a_status_sink(self):
        """With a ``status_sink`` supplied, the additive bus messages
        still arrive: STATE_CHANGED->PLAYING as ``running`` for every
        element, and the WARNING as ``warning`` naming the element and
        carrying its message.

        **Validates: Requirements 3.6**
        """
        path = out_path("warn-sink")
        launch = ("appsrc name=appsrc caps=video/x-raw,format=I420 ! "
                  "videoconvert ! jpegenc ! filesink location={0}"
                  ).format(path)
        frame = {"data": gradient_bytes(810 * 20 * 3 // 2, 1, 1),
                 "width": 810, "height": 20}
        events = []

        result = manager().run_pipeline(
            launch, frame,
            status_sink=lambda name, kind, detail: events.append(
                (normalized_element(name), kind, detail)))

        assert result == {}
        warnings = [event for event in events if event[1] == "warning"]
        running = {event[0] for event in events if event[1] == "running"}
        assert warnings
        assert warnings[0][0] == "videoconvert"
        assert warnings[0][2].startswith(RECORDED_WARNING_TEXT)
        assert running == RECORDED_RUNNING_ELEMENTS
        assert {event[1] for event in events} == {"running", "warning"}

    def test_status_sink_exception_is_still_swallowed(self):
        """A sink that raises can never disrupt the pipeline.

        **Validates: Requirements 3.6**
        """
        path = out_path("sink-raises")
        launch = ("appsrc name=appsrc caps=video/x-raw,format=I420 ! "
                  "videoconvert ! jpegenc ! filesink location={0}"
                  ).format(path)
        frame = {"data": gradient_bytes(810 * 20 * 3 // 2, 1, 1),
                 "width": 810, "height": 20}

        def exploding_sink(name, kind, detail):
            raise RuntimeError("sink boom")

        assert manager().run_pipeline(
            launch, frame, status_sink=exploding_sink) == {}
        assert os.path.exists(path)

    def test_eos_path_returns_the_tag_dict_and_writes_the_file(self):
        """The plain EOS path with ``status_sink=None``: an empty tag dict
        and a written file, with only ``running``-free bus traffic.

        **Validates: Requirements 3.6**
        """
        path = out_path("eos")
        data = gradient_bytes(40, 30, 3)
        launch = jpeg_launch("RGB", path)

        _, sequence = record_bus(launch, {"data": data, "width": 40,
                                          "height": 30})
        result = manager().run_pipeline(
            launch, {"data": data, "width": 40, "height": 30})

        assert result == {}
        assert sequence == [("eos", "<pipeline>", None)]
        assert os.path.getsize(path) > 0

    def test_bus_error_is_captured_and_raised_after_the_loop(self):
        """An ERROR on the bus is captured in the callback, quits the
        loop, and is raised as ``PipelineExecutionException`` AFTER
        ``loop.run()`` returns — with the ``Pipeline failed with: `` shape
        and the element's own message.

        **Validates: Requirements 3.6**
        """
        launch = ("appsrc name=appsrc caps=video/x-raw,format=RGB ! "
                  "identity error-after=1 ! fakesink")
        frame = {"data": gradient_bytes(40, 30, 3), "width": 40,
                 "height": 30}

        with pytest.raises(PipelineExecutionException) as excinfo:
            manager().run_pipeline(launch, frame)

        message = str(excinfo.value)
        assert message.startswith(RECORDED_ERROR_PREFIX)
        assert RECORDED_IDENTITY_ERROR_TEXT in message

    def test_watchdog_still_quits_a_stalled_pipeline_and_raises(self):
        """The ``PIPELINE_TIMEOUT_SEC`` watchdog still force-quits a
        pipeline that never posts EOS or ERROR, and reports it verbatim.
        The constant is patched down so the test terminates quickly; the
        shipped value is asserted separately above.

        **Validates: Requirements 3.6**
        """
        original = gst_pipeline.PIPELINE_TIMEOUT_SEC
        gst_pipeline.PIPELINE_TIMEOUT_SEC = 2
        try:
            with pytest.raises(PipelineExecutionException) as excinfo:
                manager().run_pipeline("videotestsrc ! fakesink sync=true")
        finally:
            gst_pipeline.PIPELINE_TIMEOUT_SEC = original

        assert str(excinfo.value) == RECORDED_TIMEOUT_MESSAGE.format(2)

    def test_parse_msg_values_are_unchanged(self):
        """``parse_msg``: EOS and a tag list without the eminfer tags both
        yield ``{}``; an ERROR message raises with the same
        ``Pipeline failed with: <err>. <dbg>`` text.

        **Validates: Requirements 3.6**
        """
        from gi.repository import GLib

        pipeline = Gst.parse_launch(
            "videotestsrc name=vts num-buffers=1 ! fakesink")
        element = pipeline.get_by_name("vts")
        try:
            assert manager().parse_msg(Gst.Message.new_eos(element)) == {}
            taglist = Gst.TagList.new_from_string(
                "taglist, title=(string)hello")
            assert manager().parse_msg(
                Gst.Message.new_tag(element, taglist)) == {}
            gerror = GLib.Error.new_literal(
                Gst.CoreError.quark(), "boom-msg", int(Gst.CoreError.FAILED))
            with pytest.raises(PipelineExecutionException) as excinfo:
                manager().parse_msg(
                    Gst.Message.new_error(element, gerror, "dbg-detail"))
            assert str(excinfo.value) == \
                "Pipeline failed with: boom-msg. dbg-detail"
        finally:
            pipeline.set_state(Gst.State.NULL)


# ===========================================================================
# Requirement 3.7 — create_buffer's contract unchanged
# ===========================================================================


class TestCreateBufferContractUnchanged:

    @settings(deadline=None, max_examples=25)
    @given(width=any_widths, height=small_heights)
    @example(width=810, height=1080)
    @example(width=768, height=576)
    def test_caps_derivation_and_source_properties(self, width, height):
        """The FIRST ``caps=`` clause of the launch string (same
        ``caps=([^!]+)`` regex), the ``,width={wd} , height={ht}`` append,
        ``block=True``, ``format=Gst.Format.TIME`` and the
        ``(source, buffer)`` return shape are all exactly today's.

        The content is sized from the DECLARED dimensions on purpose. An
        earlier revision capped it at ``min(width, 16) x min(height, 4)``
        as a cost shortcut, which made every ``width > 16`` /
        ``height > 4`` case an IRRECONCILABLE buffer — neither the tight
        size nor the caps-implied size — so ``create_buffer`` correctly
        raises on it after the fix (Requirement 2.4, the same raise
        ``test_property_appsrc_frame_stride.py``'s
        ``test_irreconcilable_buffer_size_fails_before_playing`` asserts).
        Nothing this test pins depends on a short buffer, and
        ``wrap_frame`` never reaches PLAYING (it goes straight to NULL),
        so the largest example here is just a 2.6 MB allocation.

        **Validates: Requirements 3.7**
        """
        data = gradient_bytes(width, height, 3)
        launch = jpeg_launch("RGB", out_path("contract"))
        first_caps = re.search(r"caps=([^!]+)", launch).group(1)

        source, buffer, _ = wrap_frame(
            launch, {"data": data, "width": width, "height": height})

        assert first_caps == "video/x-raw,format=RGB "
        expected = Gst.Caps.from_string(
            first_caps + RECORDED_CAPS_APPEND.format(width, height))
        assert source.get_property("caps").to_string() == expected.to_string()
        assert source.get_name() == "appsrc"
        assert source.get_property("block") is True
        assert source.get_property("format") == Gst.Format.TIME
        assert isinstance(buffer, Gst.Buffer)

    def test_first_caps_clause_wins_over_later_capsfilters(self):
        """The Bayer chain carries TWO ``caps=`` clauses; the regex takes
        the FIRST, so the appsrc is configured with the Bayer caps and the
        downstream ``video/x-raw,format=RGBA`` capsfilter is untouched.

        **Validates: Requirements 3.7, 3.11**
        """
        launch = bayer_launch(out_path("first-caps"))
        data = gradient_bytes(12, 3, 1)

        source, _, _ = wrap_frame(
            launch, {"data": data, "width": 12, "height": 3})

        assert re.search(r"caps=([^!]+)", launch).group(1) == \
            "video/x-bayer,format=bggr "
        caps_string = source.get_property("caps").to_string()
        assert caps_string.startswith("video/x-bayer")
        assert "RGBA" not in caps_string

    def test_aravis_fake_interface_is_still_enabled(self):
        """``create_buffer`` still enables the Aravis "Fake" interface.

        **Validates: Requirements 3.7**
        """
        calls = []

        class AravisSpy:
            @staticmethod
            def enable_interface(name):
                calls.append(name)

        original = gst_pipeline.Aravis
        gst_pipeline.Aravis = AravisSpy
        try:
            wrap_frame(
                "appsrc name=appsrc caps=video/x-raw,format=RGB ! fakesink",
                {"data": gradient_bytes(4, 2, 3), "width": 4, "height": 2})
        finally:
            gst_pipeline.Aravis = original

        assert calls == ["Fake"]


# ===========================================================================
# Requirements 3.2, 3.3 — the store's tight contract and the caps planners
# ===========================================================================


class TestStoreAndFrameCapsUnchanged:

    @settings(deadline=None, max_examples=25)
    @given(spec=image_specs)
    def test_store_frame_is_tight_packed_rgb_and_deterministic(self, spec):
        """``StaticImageStore.get_frame()`` keeps its contract for any
        pinned image: ``pixel_format == "RGB"``,
        ``len(data) == 3 * width * height`` (TIGHT, unpadded),
        EXIF-transposed dimensions, byte-identical across repeated grabs.
        The padding the fix adds happens at the WRAPPING site, never here.

        **Validates: Requirements 3.2**
        """
        from utils.static_image_camera import StaticImageStore

        width, height, seed, img_format = spec
        data = render_image_bytes(width, height, seed, img_format)
        base_dir = tempfile.mkdtemp(prefix="stride-store-", dir=WORK_PATH)
        store = StaticImageStore(base_dir=base_dir)
        store.pin_bytes(data, "pinned.img")

        frame = store.get_frame()

        assert frame == expected_frame(data)
        assert frame["pixel_format"] == "RGB"
        assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
        assert (frame["width"], frame["height"]) == (width, height)
        assert store.get_frame() == frame

    @pytest.mark.parametrize("key,frame_data", [
        ("rgb_tagged", {"data": b"x" * 72, "width": 6, "height": 4,
                        "pixel_format": "RGB"}),
        ("bayer_tagged", {"data": b"x" * 24, "width": 6, "height": 4,
                          "pixel_format": "bayer:bggr"}),
        ("untagged_3bpp", {"data": b"x" * 72, "width": 6, "height": 4}),
        ("untagged_1bpp", {"data": b"x" * 24, "width": 6, "height": 4}),
        ("untagged_4bpp", {"data": b"x" * 96, "width": 6, "height": 4}),
        ("untagged_ragged", {"data": b"x" * 7, "width": 6, "height": 4}),
    ])
    def test_frame_caps_is_unchanged(self, key, frame_data):
        """``pipeline_executor._frame_caps`` for an ``"RGB"``-tagged
        frame, a ``bayer:bggr`` frame and untagged frames — the strings
        are exactly today's, and this spec changes none of them.

        **Validates: Requirements 3.3**
        """
        from workflow_engine.pipeline_executor import WorkflowExecutor

        assert WorkflowExecutor._frame_caps(frame_data) == \
            RECORDED_FRAME_CAPS[key]

    def test_static_camera_frame_still_yields_the_rgb_frame_feed_caps(self):
        """The Frame_Feed caps for a real store frame stay
        ``video/x-raw,format=RGB``, which is what
        ``test_workflow_feed.py``'s
        ``"appsrc name=appsrc caps=video/x-raw,format=RGB "`` assertion
        depends on.

        **Validates: Requirements 3.3**
        """
        from utils.static_image_camera import StaticImageStore
        from workflow_engine.pipeline_executor import WorkflowExecutor

        base_dir = tempfile.mkdtemp(prefix="stride-feed-", dir=WORK_PATH)
        store = StaticImageStore(base_dir=base_dir)
        store.pin_bytes(render_image_bytes(6, 4, 7, "PNG"), "pinned.png")

        frame = store.get_frame()

        assert WorkflowExecutor._frame_caps(frame) == "video/x-raw,format=RGB"


class TestFrameDictIsNeverMutated:
    """The witness ``test_workflow_feed.py`` carries, re-asserted here."""

    def test_the_workflow_feed_pinned_image_is_an_unaligned_case(self):
        """``test/backend-test/static_image_camera/test_workflow_feed.py``
        pins a 6x4 image, and ``6 * 3 = 18`` is NOT a multiple of 4 — so
        that suite is itself an UNALIGNED case, and its
        ``args == (expected_frame(...),)`` assertion is the single best
        existing proof that the frame dict handed to ``run_pipeline`` must
        stay TIGHT-packed. Recorded here so the connection is not lost.

        **Validates: Requirements 3.3, 3.13**
        """
        frame = expected_frame(render_image_bytes(6, 4, 1, "PNG"))

        assert (frame["width"], frame["height"]) == (6, 4)
        assert frame["width"] * 3 % 4 != 0
        assert len(frame["data"]) == 3 * 6 * 4 == 72
        assert video_info("RGB", 6, 4).size == 80  # what the caps imply

    def test_create_buffer_does_not_touch_the_frame_dict(self):
        """``create_buffer`` leaves ``frame_data`` — and
        ``frame_data['data']`` — exactly as handed in, for the aligned and
        the unaligned case alike. The fix pads the bytes given to
        ``Gst.Buffer.new_wrapped``, never the frame dict.

        **Validates: Requirements 3.3**
        """
        for width, height in [(6, 4), (768, 4)]:
            data = gradient_bytes(width, height, 3)
            frame = {"data": data, "width": width, "height": height,
                     "pixel_format": "RGB"}
            snapshot = dict(frame)

            wrap_frame("appsrc name=appsrc caps=video/x-raw,format=RGB ! "
                       "fakesink", frame)

            assert frame == snapshot
            assert frame["data"] is data
            assert len(frame["data"]) == width * 3 * height


# ===========================================================================
# Requirement 3.8 — the already-stride-aware bridge helpers
# ===========================================================================


class TestBridgeStrideHelpersUnchanged:
    """These are the precedent the fix follows, so they must not shift."""

    def test_format_channels_table_is_unchanged(self):
        """**Validates: Requirements 3.8**"""
        assert load_dda_frames().FORMAT_CHANNELS == RECORDED_FORMAT_CHANNELS

    @settings(deadline=None, max_examples=25)
    @given(frame_format=st.sampled_from(sorted(RECORDED_FORMAT_CHANNELS)),
           width=st.integers(min_value=1, max_value=17),
           height=st.integers(min_value=1, max_value=9))
    def test_to_array_reads_padded_and_tight_identically(
            self, frame_format, width, height):
        """``dda_frames.to_array`` already tolerates row padding on the
        READ side: a tight buffer and the same rows at
        ``GST_ROUND_UP_4`` stride decode to the SAME array, and
        ``to_bytes`` writes back with no padding.

        **Validates: Requirements 3.8**
        """
        helpers = load_dda_frames()
        channels = RECORDED_FORMAT_CHANNELS[frame_format]
        row_bytes = width * channels
        stride = round_up_4(row_bytes)
        tight = gradient_bytes(width, height, channels, seed=3)
        padded = pad_rows(tight, row_bytes, stride, height)

        from_tight = helpers.to_array(tight, width, height, frame_format)
        from_padded = helpers.to_array(padded, width, height, frame_format)

        assert len(padded) == stride * height
        assert from_tight.shape == from_padded.shape
        assert (from_tight == from_padded).all()
        assert helpers.to_bytes(from_padded) == tight
        assert len(helpers.to_bytes(from_padded)) == row_bytes * height

    def test_to_array_error_messages_are_unchanged(self):
        """Including the too-short message, verbatim.

        **Validates: Requirements 3.8**
        """
        helpers = load_dda_frames()
        tight = gradient_bytes(5, 3, 3, seed=3)

        with pytest.raises(ValueError) as too_short:
            helpers.to_array(tight[: 5 * 3 * 2], 5, 3, "RGB")
        assert str(too_short.value) == RECORDED_TO_ARRAY_TOO_SHORT

        with pytest.raises(ValueError) as bad_format:
            helpers.to_array(tight, 5, 3, "NV12")
        assert str(bad_format.value) == RECORDED_TO_ARRAY_BAD_FORMAT

    @settings(deadline=None, max_examples=25)
    @given(frame_format=st.sampled_from(sorted(RECORDED_FORMAT_CHANNELS)),
           width=st.integers(min_value=1, max_value=13),
           height=st.integers(min_value=1, max_value=7),
           padded=st.booleans())
    def test_invoke_process_frame_preserves_the_input_stride(
            self, frame_format, width, height, padded):
        """``_invoke_process_frame`` writes rows back into a COPY of the
        input at the input's own stride, so the byte length and the row
        padding survive — for padded AND tight inputs. A ``None`` return
        is still the identical pass-through object.

        **Validates: Requirements 3.8**
        """
        helpers = load_dda_frames()
        invoke = load_invoke_process_frame()
        channels = RECORDED_FORMAT_CHANNELS[frame_format]
        row_bytes = width * channels
        stride = round_up_4(row_bytes) if padded else row_bytes
        tight = gradient_bytes(width, height, channels, seed=11)
        frame = pad_rows(tight, row_bytes, stride, height) if padded else tight
        info = {"width": width, "height": height, "format": frame_format}

        echoed, meta = invoke(lambda array, m: array, helpers, frame,
                              {"k": 1}, info)
        filled, _ = invoke(lambda array, m: array * 0 + 9, helpers, frame,
                           {}, info)
        skipped, skipped_meta = invoke(lambda array, m: None, helpers, frame,
                                      {"z": 2}, info)

        assert echoed == frame and len(echoed) == stride * height
        assert meta == {"k": 1}
        assert len(filled) == len(frame)
        for row in range(height):
            start = row * stride
            assert filled[start:start + row_bytes] == b"\x09" * row_bytes
            # The pad bytes are carried over from the input untouched.
            assert filled[start + row_bytes:start + stride] == \
                frame[start + row_bytes:start + stride]
        assert skipped is frame
        assert skipped_meta == {"z": 2}


# ===========================================================================
# Requirement 3.9 — the previous spec's deployed surface is untouched
# ===========================================================================


class TestPreviousSpecSurfaceUntouched:

    def test_static_camera_resolves_the_rgb_conversion_chain(self):
        """``default_camera_configurations.json`` still resolves the
        static camera's shipped identity to
        ``capsfilter caps=video/x-raw,format=RGB ! videoconvert``, and the
        file is byte-for-byte the deployed one.

        (The exhaustive entry-by-entry pin lives in the previous spec's
        ``test_property_static_camera_pixel_format_preservation.py``; this
        is the zero-diff guard for THIS spec.)

        **Validates: Requirements 3.9**
        """
        from utils.static_image_camera import STATIC_IMAGE_CAMERA_IDENTITY

        with open(DEFAULT_CAMERA_CONFIG_PATH, "rb") as handle:
            raw = handle.read()
        config = json.loads(raw.decode("utf-8"))
        vendor = STATIC_IMAGE_CAMERA_IDENTITY["vendor"]
        model = STATIC_IMAGE_CAMERA_IDENTITY["model"]
        entry = config[vendor]

        resolved = (entry.get(model) or entry["default"])["processingPipeline"]
        assert resolved == RECORDED_RGB_CHAIN
        assert entry["default"]["processingPipeline"] == RECORDED_RGB_CHAIN
        assert hashlib.sha256(raw).hexdigest() == RECORDED_CONFIG_SHA256

    def test_backfill_migration_still_exists_and_is_source_identical(self):
        """``db_backfill.migration_static_camera_pipeline_db`` still
        exists, is still wired into ``backfill()``, and its source is
        byte-for-byte the deployed one.

        **Validates: Requirements 3.9**
        """
        from dao.sqlite_db import db_backfill

        migration = getattr(db_backfill, RECORDED_BACKFILL_NAME, None)

        assert migration is not None
        source = inspect.getsource(migration)
        assert hashlib.sha256(source.encode("utf-8")).hexdigest() == \
            RECORDED_BACKFILL_SHA256
        assert RECORDED_BACKFILL_NAME in inspect.getsource(
            db_backfill.backfill)

    def test_backfill_is_still_idempotent(self):
        """Running the migration twice over the same database converges
        once and then changes nothing.

        **Validates: Requirements 3.9**
        """
        from unittest.mock import patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import dao.sqlite_db.models as db_models
        from dao.sqlite_db import db_backfill
        from dao.sqlite_db.sqlite_db_operations import Base
        from model.image_source import ImageSourceType
        from utils import constants
        from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID

        with open(DEFAULT_CAMERA_CONFIG_PATH) as handle:
            config = json.load(handle)
        known_wrong = config["default"]["default"]["processingPipeline"]

        tmp_dir = tempfile.mkdtemp(prefix="stride-backfill-", dir=WORK_PATH)
        engine = create_engine("sqlite:///{0}".format(
            os.path.join(tmp_dir, "backfill.db")),
            connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        now = int(time.time() * 1000)
        try:
            session.add(db_models.ImageSourceConfiguration(
                imageSourceConfigId="cfg-static", gain=1, exposure=500,
                processingPipeline=known_wrong, creationTime=now))
            session.add(db_models.ImageSource(
                imageSourceId="src-static", name="src-static",
                type=ImageSourceType.CAMERA,
                cameraId=STATIC_IMAGE_CAMERA_ID, creationTime=now,
                lastUpdateTime=now,
                imageCapturePath="/aws_dda/image-capture/src-static",
                imageSourceConfigId="cfg-static"))
            session.commit()

            def rows():
                return {row.imageSourceConfigId: row.processingPipeline
                        for row in session.query(
                            db_models.ImageSourceConfiguration).all()}

            with patch.object(constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH",
                              DEFAULT_CAMERA_CONFIG_PATH):
                db_backfill.migration_static_camera_pipeline_db(session)
                session.commit()
                after_first = rows()
                db_backfill.migration_static_camera_pipeline_db(session)
                session.commit()
                after_second = rows()

            assert after_first == {"cfg-static": RECORDED_RGB_CHAIN}
            assert after_second == after_first
        finally:
            session.close()
            engine.dispose()


# ===========================================================================
# Requirement 3.13 — the neighbouring suites are preservation coverage
# ===========================================================================


def test_the_neighbouring_preservation_suites_still_exist():
    """The suites that document the untouched surface must keep passing
    untouched; task 3.4 re-runs each in its OWN process (``utils`` /
    ``static_image_camera`` leave module stubs behind). This asserts they
    are still present and still carry the assertions this spec leans on.

    **Validates: Requirements 3.13**
    """
    static_tests = os.path.join(_BACKEND_TEST, "static_image_camera")
    for name in ("test_workflow_feed.py",
                 "test_property_static_camera_pixel_format.py",
                 "test_property_static_camera_pixel_format_preservation.py"):
        assert os.path.exists(os.path.join(static_tests, name)), name

    with open(os.path.join(static_tests, "test_workflow_feed.py")) as handle:
        feed_source = handle.read()
    assert '"appsrc name=appsrc caps=video/x-raw,format=RGB " in launch' in \
        feed_source
    assert "args == (expected_frame(pinned_store.image_bytes_for_test),)" in \
        feed_source
    # The 6x4 default is what makes that suite an unaligned witness.
    assert "width=6, height=4" in feed_source
