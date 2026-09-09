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
"""Shared Hypothesis strategies and stride oracles for the appsrc
frame-stride suites (spec ``appsrc-frame-stride-alignment``, tasks 1
and 2).

PURE PYTHON AND SIDE-EFFECT FREE ON IMPORT: no ``gi``, no numpy, no
environment mutation, no filesystem access. The bug-condition suite
(``test_property_appsrc_frame_stride.py``) and the preservation suite
(``test_property_appsrc_frame_stride_preservation.py``) both import it,
and the preservation suite must be able to do so without dragging in
GStreamer.

Why the stride arithmetic lives HERE and not in the code under test
-------------------------------------------------------------------
These suites are written BEFORE ``src/backend/gstreamer/frame_stride.py``
exists (test-first ordering, tasks.md Notes), so the oracle cannot import
the helper. It is instead cross-checked against GStreamer itself —
``GstVideo.VideoInfo.new_from_caps(...).stride[0]`` / ``.size`` — by
``test_expected_stride_and_size_are_gstreamers_own_numbers``
(Requirement 2.10). Every buffer-size assertion in both suites is stated
against THIS table, so if the shipped helper's table ever drifts from
GStreamer's rules a test fails rather than a device preview.

Measured with ``GstVideo.VideoInfo`` in ``flask-app:latest``
(GStreamer 1.20.3), matching the JP6 Orin's ``gst-debug.log`` exactly:

    RGB   810x1080 -> stride 2432, size 2626560  (tight 2624400)
    RGB   773x512  -> stride 2320, size 1187840  (tight 1187328)
    RGBA  810x1080 -> stride 3240, size 3499200  (tight; 4 bpp is always
                                                  aligned)
    GRAY8 810x1080 -> stride  812, size  876960  (tight 874800)

The generator deliberately STRADDLES ``width * bpp % 4 != 0``: a
generator that only produced aligned widths would pass vacuously on
unfixed code and is itself a test defect (Requirement 2.11). The four
measured device widths are exposed as ``BOUNDARY_WIDTHS`` so both suites
can pin them as explicit ``@example`` cases.
"""
import random
import re

from hypothesis import strategies as st

#: The formats the fed-frame wrapping sites actually declare — the table
#: ``src/backend/gstreamer/frame_stride.py`` must carry (task 3.1).
#: ``video/x-bayer`` is deliberately absent: it is not a
#: ``GstVideoFormat``, so the ``gst_video_frame_map_id`` size check behind
#: this defect never applies to it, and the physical Basler path must stay
#: byte-identical (Requirement 3.4).
BYTES_PER_PIXEL = {"RGB": 3, "BGR": 3, "RGBA": 4, "BGRA": 4, "GRAY8": 1}

KNOWN_FORMATS = tuple(sorted(BYTES_PER_PIXEL))

#: The shipped test assets' widths, measured on the JP6 AGX Orin
#: (bugfix.md 1.5, 1.6). ``width * 3`` is not a multiple of 4 for the
#: first two: ``bus.jpg`` 810x1080 and ``eagle.jpg`` / ``horses.jpg``
#: 773x512 render BLACK today; ``dog.jpg`` 768x576 and ``zidane.jpg``
#: 1280x720 are aligned and render correctly.
DEVICE_UNALIGNED_WIDTHS = (810, 773)
DEVICE_ALIGNED_WIDTHS = (768, 1280)
BOUNDARY_WIDTHS = DEVICE_UNALIGNED_WIDTHS + DEVICE_ALIGNED_WIDTHS

#: The (width, height) pairs of the five shipped assets, so both suites
#: can drive the device's own dimensions rather than invented ones.
DEVICE_DIMENSIONS = {
    "bus.jpg": (810, 1080),
    "eagle.jpg": (773, 512),
    "horses.jpg": (773, 512),
    "dog.jpg": (768, 576),
    "zidane.jpg": (1280, 720),
}

#: The SAME regex ``GstPipelineManager.create_buffer`` uses to derive the
#: appsrc caps from the launch string (``gst_pipeline.py`` line 68).
FIRST_CAPS_PATTERN = r'caps=([^!]+)'


# ---------------------------------------------------------------------------
# Stride oracles (GST_ROUND_UP_4; cross-checked against GstVideo.VideoInfo)
# ---------------------------------------------------------------------------


def gst_round_up_4(value):
    """``GST_ROUND_UP_4`` — GStreamer's packed-row rounding."""
    return ((value + 3) // 4) * 4


def bytes_per_pixel(frame_format):
    return BYTES_PER_PIXEL[frame_format]


def tight_stride(frame_format, width):
    """The row size the producing side supplies (no padding)."""
    return width * BYTES_PER_PIXEL[frame_format]


def tight_size(frame_format, width, height):
    return tight_stride(frame_format, width) * height


def expected_stride(frame_format, width):
    """The row stride the declared caps imply — what
    ``gst_video_frame_map_id`` requires."""
    return gst_round_up_4(tight_stride(frame_format, width))


def expected_size(frame_format, width, height):
    return expected_stride(frame_format, width) * height


def is_aligned(frame_format, width):
    """True when the tight row is already a 4-byte multiple, i.e. when the
    reconciliation must be a no-op returning the identical object
    (Requirement 2.2)."""
    return expected_stride(frame_format, width) == tight_stride(
        frame_format, width)


def alignment_label(frame_format, width):
    """``"aligned"`` / ``"unaligned"`` — the tag both suites feed to
    ``hypothesis.event`` so the straddle is visible in the statistics."""
    return "aligned" if is_aligned(frame_format, width) else "unaligned"


def pad_rows(data, frame_format, width, height):
    """The oracle for the fix: every row copied at ``expected_stride``
    with the pad bytes zeroed. Returns ``data`` unchanged when the format
    is already aligned."""
    row_bytes = tight_stride(frame_format, width)
    stride = expected_stride(frame_format, width)
    if stride == row_bytes:
        return data
    pad = b"\x00" * (stride - row_bytes)
    return b"".join(
        bytes(data[row * row_bytes:(row + 1) * row_bytes]) + pad
        for row in range(height)
    )


# ---------------------------------------------------------------------------
# Caps / launch-string builders (the exact strings the code under test uses)
# ---------------------------------------------------------------------------


def base_caps(frame_format):
    """The base caps clause a launch string carries, before
    ``create_buffer`` appends the frame's dimensions."""
    return "video/x-raw,format={0}".format(frame_format)


def caps_string(frame_format, width, height):
    """The fully dimensioned caps string (``_fed_frame_caps``' shape)."""
    return "video/x-raw,format={0},width={1},height={2}".format(
        frame_format, width, height)


def first_caps(launch_string):
    """``create_buffer``'s first-``caps=`` clause, verbatim (NOT stripped:
    the trailing space is part of the string it interpolates)."""
    match = re.search(FIRST_CAPS_PATTERN, launch_string)
    assert match is not None, (
        "no 'caps=' clause in the launch string, so create_buffer would "
        "raise deriving the appsrc caps: {0}".format(launch_string))
    return match.group(1)


def created_buffer_caps(launch_string, width, height):
    """The caps string ``create_buffer`` builds, character for character
    (``f"{first_caps} ,width={wd} , height={ht}"``, line 71)."""
    return "{0} ,width={1} , height={2}".format(
        first_caps(launch_string), width, height)


def appsrc_launch(frame_format, tail="fakesink"):
    """An ``appsrc name=appsrc caps=<base caps> ! <tail>`` launch string —
    the shape ``_add_camera_image_source`` and the compiled Frame_Feed
    both produce, and the shape ``create_buffer``'s regex expects."""
    return "appsrc name=appsrc caps={0} ! {1}".format(
        base_caps(frame_format), tail)


def jpeg_tail(location):
    """The preview/capture chain's tail: the exact elements the device
    runs after the conversion chain."""
    return (
        "videoconvert ! jpegenc idct-method=2 quality=100 "
        "! filesink location={0}".format(location)
    )


# ---------------------------------------------------------------------------
# Frame content
# ---------------------------------------------------------------------------


def noise_bytes(frame_format, width, height, seed):
    """Tight, seed-derived random frame bytes.

    Random content makes ROW-level assertions strong (every row differs,
    so a shifted or dropped row is caught) and is cheap for
    device-sized frames. It is useless for MEAN-based assertions —
    see ``structured_rgb_bytes``.
    """
    return random.Random(seed).randbytes(tight_size(
        frame_format, width, height))


def structured_rgb_bytes(width, height, seed=0):
    """Tight packed RGB with STRUCTURE: R ramps across x, G ramps down y,
    B steps through a 4x4 block pattern.

    Why not random noise for the rendered-pixel comparisons: a uniform
    random frame's per-channel mean is ~127.5 no matter how the bytes are
    scrambled, so a mean comparison against it is nearly vacuous. Measured
    in ``flask-app:latest`` while writing this suite: a buffer TRUNCATED by
    one row rendered to a JPEG whose mean was ``127.13/127.39/127.68``
    against a source mean of ``127.12/127.39/127.67`` — indistinguishable
    — while the per-pixel correlation was ``-0.0003``. The device's own
    grey-cast signature (capture mean ``61.02/61.05/61.04`` vs source
    ``117.34/115.61/118.34``) is only visible because a photograph has
    structure. Region means over this pattern move with the content, so
    the mean assertions discriminate.
    """
    rows = []
    x_span = max(width - 1, 1)
    y_span = max(height - 1, 1)
    for y in range(height):
        green = (y * 255) // y_span
        y_block = (y * 4) // max(height, 1)
        row = bytearray()
        for x in range(width):
            block = ((x * 4) // max(width, 1)) + y_block * 4
            row += bytes(((x * 255) // x_span, green,
                          (block * 15 + seed) % 256))
        rows.append(bytes(row))
    return b"".join(rows)


def frame_dict(frame_format, width, height, data, tag_format=False):
    """A fed-frame dict in the shape the wrapping sites receive.

    ``tag_format=True`` adds the ``format`` key a Custom Python
    Produced_Frame carries (what ``_fed_frame_caps`` reads); the store's
    static-camera frame instead carries ``pixel_format`` (what
    ``pipeline_executor._frame_caps`` reads) and is built by
    ``static_image_strategies.expected_frame``.
    """
    frame = {"data": data, "width": width, "height": height}
    if tag_format:
        frame["format"] = frame_format
    return frame


# ---------------------------------------------------------------------------
# Strategies — deliberately straddling width * bpp % 4 != 0 (Req 2.11)
# ---------------------------------------------------------------------------

#: Small widths (1..64 covers every residue of ``width % 4``, so RGB rows
#: land on both sides of the boundary) mixed with the four measured device
#: widths.
widths = st.one_of(
    st.integers(min_value=1, max_value=64),
    st.sampled_from(BOUNDARY_WIDTHS),
)

#: Heights: small ones keep 25-example runs cheap; the device heights make
#: the generated frames byte-for-byte the size of the real ones.
heights = st.one_of(
    st.integers(min_value=1, max_value=12),
    st.sampled_from((108, 512, 576, 720, 1080)),
)

_seeds = st.integers(min_value=0, max_value=2 ** 32 - 1)

#: ``(frame_format, width, height, seed)`` — the full description of a
#: generated fed frame, in a tuple shape so both suites can pin explicit
#: boundary cases with ``@example``.
frame_specs = st.tuples(st.sampled_from(KNOWN_FORMATS), widths, heights,
                        _seeds)

#: The exposed format: ``RGB`` is what the static camera and the
#: Frame_Feed declare.
rgb_frame_specs = st.tuples(st.just("RGB"), widths, heights, _seeds)

#: Only the aligned half — the preservation generator (Requirement 2.2,
#: 3.1). ``filter`` is safe here: aligned widths are ~1 in 4 of the small
#: integers and 2 of the 4 boundary widths for RGB, and every width for
#: RGBA/BGRA.
aligned_frame_specs = frame_specs.filter(
    lambda spec: is_aligned(spec[0], spec[1]))

#: Only the exposed half — the bug-condition generator.
unaligned_frame_specs = frame_specs.filter(
    lambda spec: not is_aligned(spec[0], spec[1]))


def spec_frame(spec, tag_format=False):
    """``(frame_format, frame_dict)`` for a generated spec, with tight
    (unpadded) random content — the store's documented contract."""
    frame_format, width, height, seed = spec
    data = noise_bytes(frame_format, width, height, seed)
    return frame_format, frame_dict(frame_format, width, height, data,
                                    tag_format=tag_format)


def describe(spec):
    """A one-line description of a spec for failure messages."""
    frame_format, width, height, _seed = spec
    return (
        "{0} {1}x{2}: tight={3} caps-implied={4} (stride {5} vs {6}), {7}"
        .format(frame_format, width, height,
                tight_size(frame_format, width, height),
                expected_size(frame_format, width, height),
                expected_stride(frame_format, width),
                tight_stride(frame_format, width),
                alignment_label(frame_format, width))
    )


def sampled_rows(height, limit=32):
    """Every row for short frames; a deterministic spread for tall ones,
    always including the first and last row."""
    if height <= limit:
        return list(range(height))
    picks = {0, 1, height // 4, height // 3, height // 2,
             (2 * height) // 3, (3 * height) // 4, height - 2, height - 1}
    return sorted(row for row in picks if 0 <= row < height)
