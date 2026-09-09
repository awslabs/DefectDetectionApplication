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
"""Bug condition exploration test for the unreconciled appsrc frame stride
(spec ``appsrc-frame-stride-alignment``, task 1).

**Property 1: Bug Condition / Fix Checking** — a frame wrapped for an
``appsrc`` disagrees with the row stride its own declared caps imply, and
the mismatch is silent.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.10, 2.11**

THESE TESTS ARE EXPECTED TO FAIL ON UNFIXED CODE. The failures ARE the
result: they are the counterexamples that prove the defect exists. They
encode the EXPECTED (post-fix) behavior, so the same file validates the
fix in task 3.3 without being rewritten.

What is exercised, and where the defect lives
---------------------------------------------
``GstPipelineManager.create_buffer`` (``src/backend/gstreamer/gst_pipeline.py``
line 76) declares caps built from the launch string's FIRST ``caps=``
clause plus ``,width={wd} , height={ht}``, then wraps ``frame_data['data']``
with ``Gst.Buffer.new_wrapped(data)`` — nothing in the function inspects
``len(data)``. GStreamer rounds a packed ``video/x-raw`` row up to a
4-byte multiple, so an unaligned width makes the tightly packed frame
SHORT of what the caps promise, ``gst_video_frame_map_id`` refuses the map
and ``videoconvert`` drops the frame (bugfix.md 1.1, 1.2).

The silence half: ``run_pipeline``'s ``on_message`` (lines 113-121) appends
``Gst.MessageType.WARNING`` to ``acceptable_messages`` only when
``status_sink is not None``, and every Pipeline_Configuration caller passes
``None``, so the ``videoconvert`` warning is dropped, the pipeline reaches
EOS, ``run_pipeline`` returns its normal dict, and the endpoint answers
HTTP 200 over a blank file (bugfix.md 1.7).

Device evidence (JP6 AGX Orin, ``LocalServer.arm64JP6`` 1.0.66, commit
``318d021``), reproduced verbatim in this container:

    ERROR default video-frame.c:181:gst_video_frame_map_id:
        invalid buffer size 2624400 < 2626560          (bus.jpg 810x1080)
    WARN  videofilter gstvideofilter.c:296:gst_video_filter_transform:
        <videoconvert3> warning: invalid video buffer received
    invalid buffer size 1187328 < 1187840              (eagle.jpg 773x512)

    preview  mean 0.00/0.00/0.00              (BLACK)
    capture  mean 61.02/61.05/61.04  vs source 117.34/115.61/118.34,
             per-pixel correlation -0.088    (grey cast)

Measured IN THIS CONTAINER while writing the suite (GStreamer 1.20.3,
``appsrc ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink``,
RGB 810x108):

    tight  262440 bytes -> run_pipeline returned {} (NO raise, NO signal),
                           JPEG mean 0.00/0.00/0.00, correlation -0.0003,
                           log line ``invalid buffer size 262440 < 262656``
    padded 262656 bytes -> clean EOS, mean matches source, max error 1.0,
                           correlation 1.000
    truncated 260010 bytes (one row short of tight) -> ALSO silent:
                           run_pipeline returned {} over a written file

FRAME_FEED FINDING (task 1's ESTABLISH item, bugfix.md 1.12)
------------------------------------------------------------
bugfix.md 1.12 marks the deployed-workflow Frame_Feed exposure UNVERIFIED
ON DEVICE. **It REPRODUCES in this container**: for a static-camera frame
``{'pixel_format': 'RGB', 'width': 810, 'height': 108}``,
``WorkflowExecutor._frame_caps`` returns ``video/x-raw,format=RGB``,
``_point_appsrc_at_frame_feed`` renames the compiled ``appsrc_{nodeId}``
to ``appsrc`` and sets that caps string, the rendered launch string is
``appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert ! ...``,
and the REAL ``create_buffer`` then wraps **262440** bytes where the caps
imply **262656** — short by 216 (2 pad bytes x 108 rows), the identical
shortfall the classic preview path shows. So the Frame_Feed path shares
the defect and is fixed by the same ``create_buffer`` call site. This is a
CONTAINER measurement, not a device one; task 5(g) still has to confirm it
on hardware with a deployed workflow bound to the static camera.

Harness
-------
Exercises REAL GStreamer: ``gi`` / ``Gst`` / ``GstVideo`` / ``Aravis`` are
importable in ``flask-app:latest``, so the assertions are about wrapped
buffers and DECODED PIXELS, never about launch-string shapes or HTTP
status. The root ``conftest.py`` installs a stub ``gi`` in ``sys.modules``
(``mock_gi.bogus_gi_module``) for the suites that only need the modules to
import; this file purges that stub and re-imports the real bindings before
importing anything that touches GStreamer (see ``_restore_real_gi``).

``COMPONENT_WORK_PATH`` points at a temporary directory because
``run_pipeline`` writes ``GST_DEBUG_FILE`` there;
``INFERENCE_COMPONENT_DECOMPRESED_PATH`` is set because
``utils.get_gst_plugins_path()`` reads it.

Run it in its own process (``static_image_camera`` stubs
``utils.server_setup``, poisoning a later ``from app import app``):

    docker run --rm -v "$(pwd)":/w/dda -w /w/dda \\
      -e PYTHONPATH=/w/dda/src/backend:/w/dda/test/backend-test:\\
/w/dda/test/backend-test/utils/streaming \\
      -e LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64 \\
      flask-app:latest bash -lc 'PY=$(command -v python3.10 || \\
        command -v python3.11); $PY -m pip install --no-cache-dir --quiet \\
        pytest hypothesis sarge testfixtures; \\
        $PY -m pytest test/backend-test/gstreamer/\\
test_property_appsrc_frame_stride.py -q -p no:cacheprovider'
"""
import atexit
import inspect
import os
import re
import shutil
import sys
import tempfile

import pytest
from hypothesis import HealthCheck, event, example, given, settings

# ---------------------------------------------------------------------------
# Environment: run_pipeline writes GST_DEBUG_FILE into COMPONENT_WORK_PATH
# and reads INFERENCE_COMPONENT_DECOMPRESED_PATH through
# utils.get_gst_plugins_path(). GST_DEBUG* are set here, BEFORE the first
# Gst.init, because GStreamer reads them at init time; run_pipeline sets the
# same values itself.
# ---------------------------------------------------------------------------

WORK_DIR = tempfile.mkdtemp(prefix="appsrc-frame-stride-")
atexit.register(shutil.rmtree, WORK_DIR, ignore_errors=True)

os.environ["COMPONENT_WORK_PATH"] = WORK_DIR
os.environ["INFERENCE_COMPONENT_DECOMPRESED_PATH"] = os.path.join(
    WORK_DIR, "plugins")
os.makedirs(os.environ["INFERENCE_COMPONENT_DECOMPRESED_PATH"], exist_ok=True)
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")
DEBUG_LOG = os.path.join(WORK_DIR, "gst-debug.log")
os.environ["GST_DEBUG"] = "4"
os.environ["GST_DEBUG_FILE"] = DEBUG_LOG
os.environ["GST_DEBUG_NO_COLOR"] = "1"


def _real_gi_already_loaded():
    """The REAL pygobject in ``sys.modules``, or ``None``.

    ``mock_gi``'s stub carries no ``get_required_version`` — the same
    discriminator the preservation suite uses to reject a Mock.
    """
    loaded = sys.modules.get("gi")
    if loaded is not None and hasattr(loaded, "get_required_version"):
        return loaded
    return None


def _restore_real_gi():
    """Drop the root conftest's ``mock_gi`` stub and import the real
    bindings.

    ``test/backend-test/conftest.py`` does ``from mock_gi import
    bogus_gi_module``, and importing ``mock_gi`` installs a stub ``gi``
    plus ``gi.repository`` Mocks in ``sys.modules`` so the device modules
    import without GStreamer. This suite needs the REAL thing (the whole
    point is what GStreamer does with the buffer), and the stub is only in
    ``sys.modules`` — purging those entries lets the real package import
    normally.

    IDEMPOTENT, because the sibling suite
    ``test_property_appsrc_frame_stride_preservation.py`` does the same
    thing at its own import and pytest imports both when the directory is
    collected as a whole. Purging a second time would FAIL: real pygobject
    also registers ``gobject`` in ``sys.modules``, and ``gi/__init__.py``
    raises ``ImportError: ... must not import static modules like
    "gobject"`` when a fresh import sees that. So when the real bindings
    are already loaded we reuse them and purge nothing; only the stub is
    ever purged. ``require_version`` is safe to repeat for an
    already-loaded namespace at the same version, and ``Gst.init`` is a
    no-op once GStreamer is initialized — so running this file alone in its
    own process behaves exactly as before.
    """
    gi = _real_gi_already_loaded()
    if gi is None:
        for name in [n for n in list(sys.modules)
                     if n == "gi" or n.startswith("gi.")]:
            del sys.modules[name]
        import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    gi.require_version("Aravis", "0.8")
    from gi.repository import Gst, GstVideo

    Gst.init(None)
    return Gst, GstVideo


Gst, GstVideo = _restore_real_gi()

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from exceptions.api.gst_pipeline_exception import (  # noqa: E402
    PipelineExecutionException,
)
from gstreamer.gst_pipeline import GstPipelineManager  # noqa: E402
from workflow_engine.pipeline_executor import WorkflowExecutor  # noqa: E402
from workflow_engine.python_bridge import (  # noqa: E402
    _fed_frame_caps,
    run_bridged_pipeline,
)
from workflow_engine.rendering import render_launch_string  # noqa: E402

# The static-image-camera oracles live in the sibling directory; pytest only
# puts a test file's own directory on sys.path (the same shim
# test/backend-test/static_image_camera/test_workflow_feed.py uses at lines
# 49-56).
_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC_CAMERA_TESTS = os.path.join(
    os.path.dirname(_HERE), "static_image_camera")
if _STATIC_CAMERA_TESTS not in sys.path:
    sys.path.insert(0, _STATIC_CAMERA_TESTS)

from static_image_strategies import (  # noqa: E402
    expected_frame,
    image_specs,
    render_image_bytes,
)

import stride_strategies as strides  # noqa: E402

# ---------------------------------------------------------------------------
# The measured device evidence, pinned as data
# ---------------------------------------------------------------------------

#: Verbatim from the JP6 Orin's ``gst-debug.log`` (bugfix.md 1.2), and
#: reproduced in this container. The numbers in the assertions below are
#: PARSED out of these lines, so the evidence and the arithmetic cannot
#: drift apart.
DEVICE_LOG_LINES = (
    "ERROR                default video-frame.c:181:gst_video_frame_map_id: "
    "invalid buffer size 2624400 < 2626560",
    "ERROR                default video-frame.c:181:gst_video_frame_map_id: "
    "invalid buffer size 1187328 < 1187840",
)
DEVICE_LOG_WARNING = (
    "WARN             videofilter gstvideofilter.c:296:"
    "gst_video_filter_transform:<videoconvert3> warning: "
    "invalid video buffer received"
)
#: The dimensions each log line was produced by: bus.jpg and eagle.jpg.
DEVICE_LOG_DIMENSIONS = ((810, 1080), (773, 512))

#: bugfix.md 1.3 — the preview for bus.jpg (810x1080), measured twice.
DEVICE_PREVIEW_MEAN = (0.00, 0.00, 0.00)
#: bugfix.md 1.4 — the capture for the same image, against the source.
DEVICE_CAPTURE_MEAN = (61.02, 61.05, 61.04)
DEVICE_SOURCE_MEAN = (117.34, 115.61, 118.34)
DEVICE_CAPTURE_CORRELATION = -0.088

#: The marker ``gst_video_frame_map_id`` writes when the map is refused.
INVALID_BUFFER_MARKER = "invalid buffer size"
INVALID_FRAME_MARKER = "invalid video buffer received"

#: jpegenc at ``quality=100`` with a direct RGB input is very nearly
#: lossless: max per-pixel error 1.0 measured on random noise at 810x108.
#: The tolerances are loose enough for JPEG quantization and nowhere near
#: loose enough to accept a black or scrambled frame (max error 255).
MEAN_TOLERANCE = 1.0
REGION_MEAN_TOLERANCE = 2.5
MAX_PIXEL_ERROR = 12
MIN_CORRELATION = 0.999


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def gst_stride_and_size(caps_string):
    """What GStreamer itself says a caps string requires."""
    info = GstVideo.VideoInfo.new_from_caps(Gst.Caps.from_string(caps_string))
    return info.stride[0], info.size


def wrap_through_create_buffer(launch_string, frame):
    """Drive the REAL ``GstPipelineManager.create_buffer`` and return
    ``(caps_string, buffer_size, extracted_bytes)``.

    The pipeline is parsed exactly as ``run_pipeline`` parses it and set
    back to NULL afterwards; nothing is ever set to PLAYING, so this is the
    pure wrapping-site behavior.
    """
    pipeline = Gst.parse_launch(launch_string)
    try:
        source, buffer = GstPipelineManager().create_buffer(
            launch_string, pipeline, frame)
        caps = source.get_property("caps")
        size = buffer.get_size()
        return caps.to_string(), size, buffer.extract_dup(0, size)
    finally:
        pipeline.set_state(Gst.State.NULL)


def debug_log_offset():
    return os.path.getsize(DEBUG_LOG) if os.path.exists(DEBUG_LOG) else 0


def debug_log_since(offset):
    """The GStreamer debug output written since ``offset`` (the log is
    append-only across runs in one process, so a delta is the only sound
    way to attribute lines to a run)."""
    if not os.path.exists(DEBUG_LOG):
        return ""
    with open(DEBUG_LOG, "r", errors="replace") as handle:
        handle.seek(offset)
        return handle.read()


def decode_rendered(path):
    """The written JPEG as an ``(h, w, 3)`` float array, or ``None`` when
    nothing renderable was written."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"), dtype=np.float64)
    except Exception:  # noqa: BLE001 - an unreadable render is a failure
        return None


def source_array(data, width, height):
    return np.frombuffer(data, dtype=np.uint8).reshape(
        height, width, 3).astype(np.float64)


def mean_rgb(array):
    return tuple(round(float(array[:, :, c].mean()), 2) for c in range(3))


def region_means(array, grid=3):
    """Per-channel means over a ``grid`` x ``grid`` tiling — the
    per-region comparison task 5(b) performs on device."""
    height, width = array.shape[0], array.shape[1]
    means = []
    for row in range(grid):
        for col in range(grid):
            tile = array[
                (row * height) // grid:((row + 1) * height) // grid,
                (col * width) // grid:((col + 1) * width) // grid,
            ]
            if tile.size == 0:
                continue
            means.append(mean_rgb(tile))
    return means


def correlation(rendered, source):
    left = rendered.reshape(-1)
    right = source.reshape(-1)
    if left.std() == 0 or right.std() == 0:
        return 0.0
    return round(float(np.corrcoef(left, right)[0, 1]), 4)


def render_through_run_pipeline(frame, frame_format="RGB", label="render",
                               status_sink=None):
    """Drive the REAL ``run_pipeline`` over the REAL preview/capture tail
    and return ``(raised, result, path, rendered_array, debug_delta)``."""
    path = os.path.join(WORK_DIR, "{0}.jpg".format(label))
    if os.path.exists(path):
        os.remove(path)
    launch_string = strides.appsrc_launch(
        frame_format, strides.jpeg_tail(path))
    offset = debug_log_offset()
    raised = None
    result = None
    try:
        result = GstPipelineManager().run_pipeline(
            launch_string, frame, status_sink=status_sink)
    except Exception as exc:  # noqa: BLE001 - the raise is the assertion
        raised = exc
    return (raised, result, path, decode_rendered(path),
            debug_log_since(offset))


def pixel_report(label, rendered, source, frame, expected):
    """A failure message carrying every number a reader needs."""
    lines = [
        "{0}: rendered pixels do not match the source.".format(label),
        "  frame: {0}x{1}, len(data)={2}, caps-implied size={3}".format(
            frame["width"], frame["height"], len(frame["data"]), expected),
        "  source mean      {0}".format(mean_rgb(source)),
    ]
    if rendered is None:
        lines.append("  rendered: NOTHING readable was written")
    else:
        lines.append("  rendered mean    {0}".format(mean_rgb(rendered)))
        if rendered.shape == source.shape:
            lines.append("  correlation      {0}".format(
                correlation(rendered, source)))
            lines.append("  max pixel error  {0}".format(
                float(np.abs(rendered - source).max())))
        else:
            lines.append("  rendered shape {0} != source shape {1}".format(
                rendered.shape, source.shape))
    lines.append(
        "  device evidence: preview mean {0} (BLACK), capture mean {1} vs "
        "source {2}, correlation {3}".format(
            DEVICE_PREVIEW_MEAN, DEVICE_CAPTURE_MEAN, DEVICE_SOURCE_MEAN,
            DEVICE_CAPTURE_CORRELATION))
    return "\n".join(lines)


def assert_pixels_match(label, rendered, source, frame, expected, debug_delta):
    """The pixel oracle: per-channel mean, per-region mean, per-pixel
    correlation and max per-pixel error, plus the absence of GStreamer's
    own map-refusal marker."""
    report = pixel_report(label, rendered, source, frame, expected)
    assert rendered is not None, report
    assert rendered.shape == source.shape, report

    rendered_mean = mean_rgb(rendered)
    source_mean = mean_rgb(source)
    for channel in range(3):
        assert abs(rendered_mean[channel] - source_mean[channel]) <= (
            MEAN_TOLERANCE), report

    for rendered_tile, source_tile in zip(region_means(rendered),
                                          region_means(source)):
        for channel in range(3):
            assert abs(rendered_tile[channel] - source_tile[channel]) <= (
                REGION_MEAN_TOLERANCE), report

    assert correlation(rendered, source) >= MIN_CORRELATION, report
    assert float(np.abs(rendered - source).max()) <= MAX_PIXEL_ERROR, report
    # GStreamer's own verdict: the map must not have been refused.
    assert INVALID_BUFFER_MARKER not in debug_delta, (
        "{0}\n  GStreamer refused the buffer map:\n    {1}".format(
            report,
            "\n    ".join(line.strip() for line in debug_delta.splitlines()
                          if INVALID_BUFFER_MARKER in line
                          or INVALID_FRAME_MARKER in line)[:600]))


# ---------------------------------------------------------------------------
# The stride rule is GStreamer's, not ours (Requirement 2.10)
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=25,
          suppress_health_check=[HealthCheck.too_slow])
@given(spec=strides.frame_specs)
@example(spec=("RGB", 810, 1080, 0))
@example(spec=("RGB", 773, 512, 0))
@example(spec=("RGBA", 810, 1080, 0))
@example(spec=("GRAY8", 810, 1080, 0))
def test_expected_stride_and_size_are_gstreamers_own_numbers(spec):
    """For every format the fix claims to know and a generated range of
    dimensions, the oracle's stride and size equal
    ``GstVideo.VideoInfo.new_from_caps(...).stride[0]`` and ``.size``.

    This anchor passes BOTH before and after the fix — that is the point:
    it keeps the ``BYTES_PER_PIXEL`` table honest, so a table error fails a
    test instead of a device preview.

    **Validates: Requirements 2.10**
    """
    frame_format, width, height, _seed = spec
    event("format: {0}".format(frame_format))
    event("alignment: {0}".format(
        strides.alignment_label(frame_format, width)))

    gst_stride, gst_size = gst_stride_and_size(
        strides.caps_string(frame_format, width, height))

    assert strides.expected_stride(frame_format, width) == gst_stride, (
        "stride table disagrees with GStreamer for {0}".format(
            strides.describe(spec)))
    assert strides.expected_size(frame_format, width, height) == gst_size, (
        "size oracle disagrees with GStreamer for {0}".format(
            strides.describe(spec)))


def test_device_log_lines_are_gstreamers_arithmetic():
    """The two ``invalid buffer size`` lines from the JP6 Orin's
    ``gst-debug.log`` decompose exactly into the tight size the store
    supplies and the caps-implied size GStreamer demands.

    Passes before and after the fix; it is the evidence anchor the rest of
    the suite is about (bugfix.md 1.2).

    **Validates: Requirements 2.10**
    """
    for line, (width, height) in zip(DEVICE_LOG_LINES,
                                     DEVICE_LOG_DIMENSIONS):
        match = re.search(r"invalid buffer size (\d+) < (\d+)", line)
        assert match is not None, line
        received, required = int(match.group(1)), int(match.group(2))

        assert received == strides.tight_size("RGB", width, height), (
            "device received {0} bytes for RGB {1}x{2}; tight size is "
            "{3}".format(received, width, height,
                         strides.tight_size("RGB", width, height)))
        gst_stride, gst_size = gst_stride_and_size(
            strides.caps_string("RGB", width, height))
        assert required == gst_size == strides.expected_size(
            "RGB", width, height), (
            "device required {0} bytes for RGB {1}x{2}; GStreamer here says "
            "{3} (stride {4})".format(required, width, height, gst_size,
                                      gst_stride))
        assert required - received == (
            gst_stride - strides.tight_stride("RGB", width)) * height

    # The two shipped strides, stated outright so a reader does not have to
    # recompute them.
    assert gst_stride_and_size(
        strides.caps_string("RGB", 810, 1080)) == (2432, 2626560)
    assert gst_stride_and_size(
        strides.caps_string("RGB", 773, 512)) == (2320, 1187840)


# ---------------------------------------------------------------------------
# The generator itself must straddle the boundary (Requirement 2.11)
# ---------------------------------------------------------------------------


def test_generator_straddles_the_four_byte_alignment_boundary():
    """The generator produces BOTH ``width * bpp % 4 == 0`` and
    ``width * bpp % 4 != 0`` cases, and reaches beyond the small widths
    into the measured device widths.

    A generator that only produced aligned widths would pass every
    assertion in this file vacuously on unfixed code, which Requirement
    2.11 calls a test defect. This is the meta-assertion over the generated
    sample; the four measured device widths (810 and 773 unaligned, 768 and
    1280 aligned) are additionally pinned as explicit ``@example`` cases on
    the properties below.

    **Validates: Requirements 2.11**
    """
    seen_alignment = set()
    seen_widths = set()

    @settings(deadline=None, max_examples=200, database=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(spec=strides.frame_specs)
    def collect(spec):
        frame_format, width, _height, _seed = spec
        seen_alignment.add(strides.alignment_label(frame_format, width))
        seen_widths.add(width)

    collect()

    assert seen_alignment == {"aligned", "unaligned"}, (
        "the generated sample only produced {0} rows over {1} distinct "
        "widths; a generator that cannot produce an unaligned row proves "
        "nothing about this defect (Requirement 2.11)".format(
            sorted(seen_alignment), len(seen_widths)))
    assert seen_widths & set(strides.BOUNDARY_WIDTHS), (
        "the generated sample never reached a measured device width {0}; "
        "sample was {1}".format(sorted(strides.BOUNDARY_WIDTHS),
                                sorted(seen_widths)))

    # The four device widths sit on the sides the device measured them on.
    for width in strides.DEVICE_UNALIGNED_WIDTHS:
        assert not strides.is_aligned("RGB", width), width
    for width in strides.DEVICE_ALIGNED_WIDTHS:
        assert strides.is_aligned("RGB", width), width


# ---------------------------------------------------------------------------
# The wrapping site: buffer size, row layout, untouched caps and frame dict
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=25,
          suppress_health_check=[HealthCheck.too_slow])
@given(spec=strides.frame_specs)
@example(spec=("RGB", 810, 1080, 1))
@example(spec=("RGB", 773, 512, 2))
@example(spec=("RGB", 768, 576, 3))
@example(spec=("RGB", 1280, 720, 4))
@example(spec=("GRAY8", 810, 108, 5))
def test_wrapped_buffer_size_equals_the_size_its_caps_imply(spec):
    """For EVERY generated frame, the buffer the REAL ``create_buffer``
    returns is the size the caps it just declared imply.

    Today this fails for every unaligned width: RGB 810x1080 wraps 2624400
    where the caps demand 2626560 (the device's
    ``invalid buffer size 2624400 < 2626560``).

    **Validates: Requirements 2.1, 2.2, 2.11**
    """
    frame_format, width, height, _seed = spec
    event("alignment: {0}".format(
        strides.alignment_label(frame_format, width)))
    _fmt, frame = strides.spec_frame(spec)
    data = frame["data"]
    launch_string = strides.appsrc_launch(frame_format)

    caps_string, size, _wrapped = wrap_through_create_buffer(
        launch_string, frame)

    expected = strides.expected_size(frame_format, width, height)
    _gst_stride, gst_size = gst_stride_and_size(caps_string)
    assert expected == gst_size, (
        "oracle/GStreamer disagreement for {0}".format(strides.describe(spec)))
    assert size == expected, (
        "create_buffer wrapped {0} bytes against caps {1!r} which imply {2} "
        "({3}); short by {4}".format(
            size, caps_string, expected, strides.describe(spec),
            expected - size))

    # The padding is applied to the BUFFER, never to the frame dict: the
    # store's tight frame must survive byte-for-byte (Requirement 3.3).
    assert frame["data"] is data
    assert len(frame["data"]) == strides.tight_size(
        frame_format, width, height)


@settings(deadline=None, max_examples=20,
          suppress_health_check=[HealthCheck.too_slow])
@given(spec=strides.unaligned_frame_specs)
@example(spec=("RGB", 810, 1080, 6))
@example(spec=("RGB", 773, 512, 7))
def test_rows_are_preserved_and_padding_is_appended_per_row(spec):
    """Each row's first ``width * bpp`` bytes of the wrapped buffer equal
    the corresponding source row, so the padding is appended PER ROW and no
    pixel is shifted.

    This is what distinguishes the chosen fix from simply appending bytes
    to the end of the buffer, which would satisfy the size check and still
    render a sheared image.

    **Validates: Requirements 2.1**
    """
    frame_format, width, height, _seed = spec
    _fmt, frame = strides.spec_frame(spec)
    data = frame["data"]
    row_bytes = strides.tight_stride(frame_format, width)
    stride = strides.expected_stride(frame_format, width)

    _caps, size, wrapped = wrap_through_create_buffer(
        strides.appsrc_launch(frame_format), frame)

    assert size == strides.expected_size(frame_format, width, height), (
        "cannot check row layout: {0} wrapped {1} bytes".format(
            strides.describe(spec), size))

    for row in strides.sampled_rows(height):
        assert wrapped[row * stride:row * stride + row_bytes] == (
            data[row * row_bytes:(row + 1) * row_bytes]), (
            "row {0} of {1} is not the source row at stride {2} — a pixel "
            "shift, not padding ({3})".format(
                row, height, stride, strides.describe(spec)))


@settings(deadline=None, max_examples=20,
          suppress_health_check=[HealthCheck.too_slow])
@given(spec=image_specs)
def test_store_shaped_frame_wraps_to_caps_size_with_caps_derivation_intact(
        spec):
    """For an arbitrary pinned image, the store's tightly packed RGB frame
    (``static_image_strategies.expected_frame`` — the oracle the previous
    spec's suites pin) is wrapped to the caps-implied size, WHILE the caps
    derivation and the frame dict stay exactly as they are today.

    ``image_specs`` generates widths 1..48, so ``width * 3`` lands on both
    sides of the 4-byte boundary; the assertion below records which.

    **Validates: Requirements 2.1, 2.2**
    """
    image_bytes = render_image_bytes(*spec)
    frame = expected_frame(image_bytes)
    width, height = frame["width"], frame["height"]
    event("alignment: {0}".format(strides.alignment_label("RGB", width)))

    # The store's documented contract: tight, unpadded, packed RGB.
    assert len(frame["data"]) == strides.tight_size("RGB", width, height)
    assert frame["pixel_format"] == "RGB"

    before = dict(frame)
    data = frame["data"]
    launch_string = strides.appsrc_launch("RGB")

    caps_string, size, wrapped = wrap_through_create_buffer(
        launch_string, frame)

    # Requirement 3.7 / 3.3 — the caps derivation is UNCHANGED while the
    # buffer is fixed: the first-caps regex still yields the launch
    # string's clause and the appended dimensions are still create_buffer's
    # own ``,width={wd} , height={ht}``.
    assert strides.first_caps(launch_string) == "video/x-raw,format=RGB "
    assert caps_string == Gst.Caps.from_string(
        strides.created_buffer_caps(launch_string, width, height)).to_string()

    expected = strides.expected_size("RGB", width, height)
    assert size == expected, (
        "the store's tight {0}x{1} RGB frame ({2} bytes) was wrapped as {3} "
        "against caps {4!r} which imply {5}".format(
            width, height, len(data), size, caps_string, expected))
    assert wrapped[:strides.tight_stride("RGB", width)] == (
        data[:strides.tight_stride("RGB", width)])

    # The frame dict handed in is untouched — test_workflow_feed.py's
    # ``args == (expected_frame(...),)`` assertion depends on it.
    assert frame == before
    assert frame["data"] is data


# ---------------------------------------------------------------------------
# End to end, on PIXELS (Requirements 2.6, 2.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width,height,expected_today", [
    (810, 108, "BLACK"),    # bus.jpg's width, the reproduced device case
    (773, 64, "BLACK"),     # eagle.jpg / horses.jpg width
    (768, 108, "correct"),  # dog.jpg width: aligned, already correct
])
def test_preview_chain_renders_the_source_pixels(width, height,
                                                 expected_today):
    """A tightly packed RGB frame driven through the REAL
    ``appsrc ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink``
    chain renders the SOURCE pixels, judged on per-channel mean, per-region
    mean, per-pixel correlation and max per-pixel error — never on a status
    code, which is exactly what hid this defect.

    Today the two unaligned widths render BLACK (mean ``0.00/0.00/0.00``,
    correlation ~0) and the aligned width renders correctly, which is the
    width-dependence that made this easy to miss.

    **Validates: Requirements 2.6, 2.7**
    """
    data = strides.structured_rgb_bytes(width, height, seed=13)
    frame = strides.frame_dict("RGB", width, height, data)
    source = source_array(data, width, height)

    raised, result, path, rendered, debug_delta = render_through_run_pipeline(
        frame, label="preview-{0}x{1}".format(width, height))

    assert raised is None, (
        "run_pipeline raised for a reconcilable {0}x{1} frame, which no "
        "working path should: {2!r}".format(width, height, raised))
    assert result == {}, result
    assert_pixels_match(
        "preview {0}x{1} (aligned={2}, today {3})".format(
            width, height, strides.is_aligned("RGB", width), expected_today),
        rendered, source, frame,
        strides.expected_size("RGB", width, height), debug_delta)
    assert os.path.exists(path)


def test_status_sink_none_cannot_answer_normally_over_a_blank_render():
    """THE SILENCE HALF (bugfix.md 1.7). With ``status_sink=None`` — what
    every Pipeline_Configuration caller passes — a buffer/caps mismatch
    must NOT let ``run_pipeline`` return its normal dict over a blank file.

    Either the mismatch is reconciled and the pixels are the source's, or
    it is refused with a raise. Returning ``{}`` over an all-black JPEG,
    which is what happens today, is the behavior this asserts against.

    The mechanism is documented rather than changed: ``on_message`` adds
    ``WARNING`` to ``acceptable_messages`` only when a ``status_sink`` was
    supplied, so the ``videoconvert`` warning behind the black frame is
    dropped on this path. The loudness therefore has to come from the
    wrapping site (Requirement 2.5), which is what
    ``test_irreconcilable_buffer_size_fails_before_playing`` pins.

    **Validates: Requirements 2.4, 2.5, 2.6**
    """
    # The dropped-WARNING gate, read from the shipped source: this is why a
    # bus warning cannot be the signal.
    on_message_source = inspect.getsource(GstPipelineManager.run_pipeline)
    assert "if status_sink is not None:" in on_message_source
    assert "acceptable_messages = [Gst.MessageType.ERROR, " \
           "Gst.MessageType.EOS, Gst.MessageType.TAG]" in on_message_source

    width, height = 810, 108
    data = strides.structured_rgb_bytes(width, height, seed=21)
    frame = strides.frame_dict("RGB", width, height, data)
    source = source_array(data, width, height)

    raised, result, path, rendered, debug_delta = render_through_run_pipeline(
        frame, label="silence", status_sink=None)

    if raised is not None:
        # A refusal is an acceptable answer; a silent success is not.
        assert isinstance(raised, PipelineExecutionException), raised
        return

    assert rendered is not None, (
        "run_pipeline returned {0!r} and wrote nothing readable to {1} — the "
        "caller cannot tell that from success".format(result, path))
    assert_pixels_match(
        "silent status_sink=None path", rendered, source, frame,
        strides.expected_size("RGB", width, height), debug_delta)


def test_irreconcilable_buffer_size_fails_before_playing():
    """A buffer whose size is neither the tight size nor the padded size —
    here truncated by exactly one row — is refused DETERMINISTICALLY before
    the pipeline reaches PLAYING, with an error naming the declared caps,
    the dimensions, the bytes received and the bytes expected.

    Measured today: ``run_pipeline`` returned ``{}`` for a 260010-byte
    buffer against caps implying 262656, wrote a file, and logged
    ``invalid buffer size 260010 < 262656`` where nobody reads it.

    **Validates: Requirements 2.4, 2.5**
    """
    width, height = 810, 108
    row_bytes = strides.tight_stride("RGB", width)
    truncated = strides.structured_rgb_bytes(width, height, seed=31)[
        :-row_bytes]
    frame = strides.frame_dict("RGB", width, height, truncated)
    expected = strides.expected_size("RGB", width, height)
    tight = strides.tight_size("RGB", width, height)
    assert len(truncated) not in (tight, expected)

    # create_buffer runs BEFORE the PLAYING transition in run_pipeline, so a
    # raise from the wrapping site cannot leave a half-started pipeline.
    run_pipeline_source = inspect.getsource(GstPipelineManager.run_pipeline)
    assert run_pipeline_source.index("self.create_buffer(") < (
        run_pipeline_source.index("set_state(Gst.State.PLAYING)"))

    with pytest.raises(PipelineExecutionException) as direct:
        wrap_through_create_buffer(strides.appsrc_launch("RGB"), frame)
    message = str(direct.value)
    for token in ("video/x-raw,format=RGB", str(width), str(height),
                  str(len(truncated)), str(expected)):
        assert token in message, (
            "the error must name the caps, the dimensions, the bytes "
            "received and the bytes expected; {0!r} is missing {1!r}".format(
                message, token))

    raised, result, path, rendered, _delta = render_through_run_pipeline(
        frame, label="irreconcilable")
    assert isinstance(raised, PipelineExecutionException), (
        "run_pipeline returned {0!r} for a buffer that matches neither the "
        "tight size {1} nor the caps-implied size {2}; the render is "
        "{3}".format(result, tight, expected,
                     "absent" if rendered is None else mean_rgb(rendered)))
    assert not os.path.exists(path), (
        "the pipeline reached PLAYING and wrote {0} before failing".format(
            path))


# ---------------------------------------------------------------------------
# The second wrapping site: python_bridge's fed appsrc (bugfix.md 1.8)
# ---------------------------------------------------------------------------


def test_bridged_fed_appsrc_wraps_to_the_caps_it_declares():
    """A Produced_Frame of unaligned width fed through ``python_bridge``'s
    fed ``appsrc`` — caps from ``_fed_frame_caps``, buffer from
    ``Gst.Buffer.new_wrapped`` at line 1897 — renders the source pixels.

    Driven through the REAL ``run_bridged_pipeline`` with NO bridges, so
    the fed-frame wrap and the caps it declares are exercised exactly as a
    bridged run does them without standing up a handler subprocess.

    **Validates: Requirements 2.1, 2.3**
    """
    width, height = 810, 108
    data = strides.structured_rgb_bytes(width, height, seed=41)
    frame = strides.frame_dict("RGB", width, height, data, tag_format=True)
    source = source_array(data, width, height)

    caps_string = _fed_frame_caps("appsrc name=appsrc ! videoconvert", frame)
    assert caps_string == strides.caps_string("RGB", width, height)
    _stride, caps_size = gst_stride_and_size(caps_string)
    assert caps_size == strides.expected_size("RGB", width, height)

    path = os.path.join(WORK_DIR, "bridged-fed.jpg")
    if os.path.exists(path):
        os.remove(path)
    launch_string = "appsrc name=appsrc ! {0}".format(strides.jpeg_tail(path))
    offset = debug_log_offset()
    raised = None
    result = None
    try:
        result = run_bridged_pipeline(launch_string, [], frame_data=frame)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    debug_delta = debug_log_since(offset)

    assert raised is None, (
        "run_bridged_pipeline raised for a reconcilable fed frame: "
        "{0!r}".format(raised))
    assert result == {}, result
    assert_pixels_match(
        "bridged fed appsrc {0}x{1}".format(width, height),
        decode_rendered(path), source, frame, caps_size, debug_delta)


# ---------------------------------------------------------------------------
# The deployed-workflow Frame_Feed: ESTABLISHED in this container
# (bugfix.md 1.12, UNVERIFIED ON DEVICE)
# ---------------------------------------------------------------------------


class _Feed(object):
    """The duck-typed feed ``_point_appsrc_at_frame_feed`` accepts (an
    Aravis feed or a Python source feed; both carry ``node_id``)."""

    node_id = "n1"


def _compiled_frame_feed_document(node_id, location):
    """A compiled_pipeline.json with one camera-source appsrc chain, the
    shape ``test_workflow_feed.py``'s ``make_aravis_document`` builds,
    terminated by the preview/capture tail so the run is measurable."""
    return {
        "schemaVersion": 1,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": node_id, "factory": "appsrc",
                     "args": {"name": "appsrc_{0}".format(node_id)}},
                    {"nodeId": node_id, "factory": "videoconvert",
                     "args": {}},
                    {"nodeId": None, "factory": "jpegenc",
                     "args": {"idct-method": 2, "quality": 100}},
                    {"nodeId": None, "factory": "filesink",
                     "args": {"location": location}},
                ],
            },
        ],
    }


def test_deployed_workflow_frame_feed_shares_the_exposure():
    """The deployed-workflow Frame_Feed reaches the SAME wrapping site, so
    the same fix covers it.

    ESTABLISHED IN THIS CONTAINER (task 1's establish item for bugfix.md
    1.12, which is marked UNVERIFIED ON DEVICE): for a static-camera frame
    of unaligned width, ``_frame_caps`` returns ``video/x-raw,format=RGB``,
    ``_point_appsrc_at_frame_feed`` renames the compiled
    ``appsrc_{nodeId}`` to ``appsrc`` and sets that caps string, the
    rendered launch string's first ``caps=`` clause is therefore the one
    ``create_buffer`` reads, and ``create_buffer`` wrapped **262440** bytes
    against caps implying **262656** for a 810x108 frame — short by 216,
    the identical shortfall the classic preview path shows. It REPRODUCES.
    Device confirmation with a deployed workflow bound to the static camera
    is still outstanding (task 5(g)).

    The plumbing assertions here pass before and after the fix (they are
    Requirement 3.3's unchanged strings); the buffer-size assertion is the
    Requirement 2.8 half that fails today.

    **Validates: Requirements 2.1, 2.8**
    """
    width, height = 810, 108
    data = strides.structured_rgb_bytes(width, height, seed=51)
    # The store's frame: tight packed RGB tagged ``pixel_format``.
    frame = {"data": data, "width": width, "height": height,
             "pixel_format": "RGB"}
    location = os.path.join(WORK_DIR, "frame-feed.jpg")
    document = _compiled_frame_feed_document(_Feed.node_id, location)

    assert WorkflowExecutor._frame_caps(frame) == "video/x-raw,format=RGB"
    WorkflowExecutor._point_appsrc_at_frame_feed(document, _Feed(), frame)
    appsrc = document["segments"][0]["elements"][0]
    assert appsrc["args"]["name"] == "appsrc"
    assert appsrc["args"]["caps"] == "video/x-raw,format=RGB"
    assert not any(element["factory"] == "bayer2rgb"
                   for element in document["segments"][0]["elements"])

    launch_string = render_launch_string(document)
    assert launch_string.startswith(
        "appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert")
    assert strides.first_caps(launch_string) == "video/x-raw,format=RGB "

    caps_string, size, _wrapped = wrap_through_create_buffer(
        launch_string, frame)
    expected = strides.expected_size("RGB", width, height)
    _gst_stride, gst_size = gst_stride_and_size(caps_string)
    assert gst_size == expected
    assert size == expected, (
        "the deployed-workflow Frame_Feed reproduces the classic path's "
        "shortfall: create_buffer wrapped {0} bytes for the store's "
        "{1}x{2} RGB frame against caps {3!r} implying {4} — short by {5} "
        "(bugfix.md 1.12, established in-container by this test)".format(
            size, width, height, caps_string, expected, expected - size))
