#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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
"""Reconcile raw frame bytes with the row stride their declared caps imply.

Every site that hands raw frame bytes to an ``appsrc`` also declares caps
built from the frame's dimensions. GStreamer rounds a packed
``video/x-raw`` row up to a 4-byte multiple (``GST_ROUND_UP_4``), so for
``RGB`` 810x1080 it expects ``2432 * 1080 = 2626560`` bytes while a tightly
packed frame supplies only ``810 * 3 * 1080 = 2624400``.
``gst_video_frame_map_id`` then refuses the map
(``invalid buffer size 2624400 < 2626560``) and the converter drops the
frame — measured on a JP6 AGX Orin as an HTTP 200 preview over an all-black
image (spec ``appsrc-frame-stride-alignment``).

:func:`reconcile_to_caps_stride` is the ONE helper every fed-frame wrapping
site calls, so the sites cannot drift apart:

* ``GstPipelineManager.create_buffer`` — preview, capture, the classic
  workflow path and the deployed-workflow Frame_Feed all reach it
  (``strict=True``).
* ``python_bridge``'s fed ``appsrc`` for a Custom_Python Produced_Frame
  (``strict=True``).
* ``python_bridge``'s bridge-output ``appsrc``, against the appsink's
  NEGOTIATED caps (``strict=False``: that site is mid-stream and a new
  exception there would fail runs that work today).

Design points worth keeping
---------------------------
* **No ``gi`` import.** This module is pure Python so it stays cheap to
  import and testable outside GStreamer. The stride rule it implements is
  GStreamer's own, and is cross-checked against
  ``GstVideo.VideoInfo.new_from_caps`` for every key of
  :data:`BYTES_PER_PIXEL` by
  ``test/backend-test/gstreamer/test_property_appsrc_frame_stride.py``, so a
  table error fails a test instead of a device preview.
* **An aligned frame is returned as the IDENTICAL object**, not an equal
  copy: no allocation and no copy are added to any path that works today.
* **``video/x-bayer`` is untouched, by construction rather than by a
  special case.** It is not ``video/x-raw``, so it leaves through the very
  first branch below. That is deliberate and must stay that way: ``bggr``
  and friends are NOT ``GstVideoFormat`` values (``GstVideo.VideoFormat.
  from_string("bggr")`` is ``UNKNOWN``), so the ``gst_video_frame_map_id``
  size check behind this whole defect never applies to Bayer at all.
  GStreamer does stride Bayer at ``GST_ROUND_UP_4(width)`` — ``bayer2rgb``
  declares a unit size of ``GST_ROUND_UP_4(width) * height`` — but a
  mismatch there surfaces as a chain-dependent ``GstBaseTransform``
  unit-size complaint (observed as a fatal ERROR in one chain and as a
  pass-through in the ``bayer2rgb ! ... ! jpegenc`` chain the device
  actually runs), i.e. loud or visible, never silently black. Nine physical
  Basler camera configurations across two devices depend on that path
  staying byte-identical, so DO NOT "fix" this by adding a Bayer entry to
  :data:`BYTES_PER_PIXEL`; the preservation suite has a guard that fails if
  anyone does.
"""
import logging
import re

from exceptions.api.gst_pipeline_exception import PipelineExecutionException

logger = logging.getLogger(__name__)

#: Bytes per pixel for the packed ``video/x-raw`` formats the fed-frame
#: wrapping sites actually declare. ANY OTHER FORMAT IS UNKNOWN and passes
#: through untouched, so an unrecognized caps string can never fail a
#: pipeline that works today.
#:
#: ``RGB``/``BGR`` are the exposed cases: a 3-byte pixel makes the row a
#: multiple of 4 only when ``width % 4 == 0``. ``RGBA``/``BGRA`` are 4
#: bytes per pixel, so every row is aligned for every width. ``GRAY8`` is
#: aligned exactly when ``width % 4 == 0``.
BYTES_PER_PIXEL = {"RGB": 3, "BGR": 3, "RGBA": 4, "BGRA": 4, "GRAY8": 1}

#: The media type this reconciliation applies to. See the module docstring
#: for why ``video/x-bayer`` is deliberately excluded.
PACKED_RAW_MEDIA_TYPE = "video/x-raw"

# Caps strings reach this module in three shapes and all three must parse:
#   create_buffer:   'video/x-raw,format=RGB  ,width=810 , height=108'
#                    (the launch string's first ``caps=`` clause keeps its
#                    trailing space, and the append adds ' , ' spacing)
#   _fed_frame_caps: 'video/x-raw,format=RGB,width=810,height=108'
#   Gst.Caps.to_string(): 'video/x-raw, format=(string)RGB, width=(int)810,
#                          height=(int)108, framerate=(fraction)0/1'
#
# The media type is taken up to the first separator INCLUDING any caps
# features, so ``video/x-raw(memory:NVMM)`` does not read as plain
# ``video/x-raw``: a hardware-memory buffer carries a surface handle rather
# than pixel rows and must never be padded.
_MEDIA_TYPE_RE = re.compile(r"^\s*([^\s,;]+/[^\s,;]+)")
_FORMAT_RE = re.compile(
    r"(?:^|[,;\s])format\s*=\s*(?:\(\s*string\s*\)\s*)?\"?([\w-]+)\"?")


def _dimension_re(name):
    return re.compile(
        r"(?:^|[,;\s])" + name + r"\s*=\s*(?:\(\s*int\s*\)\s*)?(-?\d+)")


_WIDTH_RE = _dimension_re("width")
_HEIGHT_RE = _dimension_re("height")


def gst_round_up_4(value):
    """``GST_ROUND_UP_4`` — GStreamer's packed-row rounding."""
    return ((value + 3) // 4) * 4


def tight_stride(frame_format, width):
    """The row size a tightly packed (unpadded) producer supplies."""
    return width * BYTES_PER_PIXEL[frame_format]


def tight_size(frame_format, width, height):
    """The byte count of a tightly packed frame."""
    return tight_stride(frame_format, width) * height


def expected_stride(frame_format, width):
    """The row stride the declared caps imply — ``GST_ROUND_UP_4(width *
    bytes_per_pixel)``, which is what ``GstVideoInfo`` reports and what
    ``gst_video_frame_map_id`` requires."""
    return gst_round_up_4(tight_stride(frame_format, width))


def expected_size(frame_format, width, height):
    """The buffer size the declared caps imply: ``expected_stride *
    height``."""
    return expected_stride(frame_format, width) * height


def caps_media_type(caps_string):
    """The media type of a caps string, or ``None`` when it has none."""
    match = _MEDIA_TYPE_RE.search(_as_text(caps_string))
    return match.group(1) if match else None


def caps_format(caps_string):
    """The ``format=`` value of a caps string, or ``None`` when absent."""
    match = _FORMAT_RE.search(_as_text(caps_string))
    return match.group(1) if match else None


def caps_dimensions(caps_string):
    """``(width, height)`` parsed out of a caps string; either may be
    ``None`` when the caps do not carry it."""
    text = _as_text(caps_string)
    return _search_int(_WIDTH_RE, text), _search_int(_HEIGHT_RE, text)


def reconcile_to_caps_stride(data, caps_string, width=None, height=None,
                             strict=True):
    """Return frame bytes whose layout matches ``caps_string``'s stride.

    The single entry point every fed-frame wrapping site uses.

    :param data: the frame bytes about to be wrapped.
    :param caps_string: the caps the ``appsrc`` declares for them.
    :param width: the frame width; parsed out of ``caps_string`` when not
        given.
    :param height: the frame height; parsed out of ``caps_string`` when not
        given.
    :param strict: raise :class:`PipelineExecutionException` when the byte
        count matches neither the tight nor the padded layout. ``True`` at
        the up-front wrapping sites (``create_buffer``, the fed ``appsrc``),
        ``False`` mid-stream at the bridge output.
    :returns: ``data`` ITSELF when nothing needs reconciling, otherwise a
        new ``bytes`` with each row copied at the caps-implied stride.

    The four outcomes, in order:

    1. The caps are not packed ``video/x-raw``, name a format outside
       :data:`BYTES_PER_PIXEL`, or carry no usable positive width/height:
       ``data`` is returned unchanged and NOTHING is raised, whatever
       ``strict`` says. This is what keeps ``video/x-bayer``, ``I420``,
       ``image/jpeg`` and every other unrecognized caps string behaving
       exactly as it does today.
    2. ``len(data)`` already equals the caps-implied size: the IDENTICAL
       object is returned. Every aligned width takes this branch, so the
       working cases stay byte-identical and copy-free.
    3. ``len(data)`` equals the tight size and the tight size differs from
       the caps-implied size: each row's ``width * bytes_per_pixel`` bytes
       are copied at the caps-implied stride and the pad bytes are zeroed.
       This is the fix.
    4. Anything else — no row layout explains the byte count: raise when
       ``strict``, otherwise return ``data`` unchanged.
    """
    caps_text = _as_text(caps_string)
    if caps_media_type(caps_text) != PACKED_RAW_MEDIA_TYPE:
        return data
    frame_format = caps_format(caps_text)
    if frame_format not in BYTES_PER_PIXEL:
        return data

    caps_width, caps_height = caps_dimensions(caps_text)
    width = _coerce_dimension(caps_width if width is None else width)
    height = _coerce_dimension(caps_height if height is None else height)
    if width is None or height is None or width <= 0 or height <= 0:
        return data

    row_bytes = tight_stride(frame_format, width)
    stride = expected_stride(frame_format, width)
    caps_size = stride * height
    packed_size = row_bytes * height
    actual = len(data)

    if actual == caps_size:
        # Already the layout the caps promise (every aligned width, and a
        # mid-stream buffer that arrived with GStreamer's own padding).
        # Return the IDENTICAL object: no copy, no allocation.
        return data

    if actual == packed_size:
        logger.debug(
            "Padding a tightly packed %s %dx%d frame from %d to %d bytes "
            "(row %d -> stride %d) to match its declared caps",
            frame_format, width, height, actual, caps_size, row_bytes, stride)
        return _pad_rows(data, row_bytes, stride, height, caps_size)

    # ASCII only: this text reaches the HTTP response, the component log and
    # the device's journal.
    message = (
        "Frame buffer cannot be reconciled with the caps it is pushed "
        "under: caps {0!r} ({1}x{2}, format {3}) imply {4} bytes at a row "
        "stride of {5}, and a tightly packed frame would be {6} bytes, but "
        "{7} bytes were supplied; no row layout explains that size"
    ).format(caps_string, width, height, frame_format, caps_size, stride,
             packed_size, actual)
    if strict:
        raise PipelineExecutionException(message)
    logger.warning("%s; pushing the buffer unchanged", message)
    return data


def _pad_rows(data, row_bytes, stride, height, caps_size):
    """Each row's ``row_bytes`` copied at ``stride``, pad bytes zeroed."""
    padded = bytearray(caps_size)
    for row in range(height):
        source = row * row_bytes
        target = row * stride
        padded[target:target + row_bytes] = data[source:source + row_bytes]
    return bytes(padded)


def _as_text(caps_string):
    if caps_string is None:
        return ""
    if isinstance(caps_string, str):
        return caps_string
    return str(caps_string)


def _search_int(pattern, text):
    match = pattern.search(text)
    return int(match.group(1)) if match else None


def _coerce_dimension(value):
    """``value`` as an ``int``, or ``None`` when it is not usable as one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
