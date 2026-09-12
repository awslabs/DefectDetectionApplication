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
"""Device Image_Source ROI (``imageCrop``) applied to a compiled document.

The operator configures a Region_Of_Interest on an Image_Source in the
LocalServer UI. It is persisted as four **pixel edge insets**
(``top``/``bottom``/``left``/``right``) on
``image_source_configuration.imageCrop`` — the ``videocrop`` element's own
parameter shape, not x/y/width/height.

Until now that ROI only reached the **classic** capture path, where
``GstPipelineBuilder`` appends a ``videocrop`` element after the
Image_Source's processing pipeline. A **deployed** (portal-compiled)
workflow runs through :mod:`workflow_engine.pipeline_executor` instead,
whose element chain comes from the compiled document — so the ROI was
silently ignored and every deployed workflow inferred on the full sensor
frame. On a 4608x3288 Basler with a 2560x1376 ROI that is a ~4x pixel-area
difference at the detector's input, which is enough to turn real detections
into none at all.

This module is the pure half of closing that gap: it validates the
persisted ROI and splices a ``videocrop`` element into the document. The
executor owns the I/O (reading the Image_Source configuration) and the
logging.

Placement — after the source node's LAST element, which matters twice:

- ``videocrop`` cannot consume ``video/x-bayer``. A Bayer camera frame is
  demosaiced by the ``bayer2rgb`` the executor injects right after the
  ``appsrc``, and the compiled ``aravis_camera_source`` chain ends in
  ``videoconvert``; inserting after the node's last element therefore
  lands on raw RGB-family caps, which ``videocrop`` handles, and keeps the
  Bayer phase undisturbed (cropping a mosaic at an odd offset would swap
  the colour filter order).
- Everything downstream — inference, tees, capture branches — then sees
  the cropped frame, so the ROI applies to detection AND to the persisted
  images, exactly as it does on the classic path.

Deliberately conservative: a document that already renders a ``videocrop``
(the author placed a Crop node in the graph) is left alone, so the device
ROI can never double-crop a graph that already expresses one.
"""
from typing import Any, Dict, Mapping, Optional

#: The four edge insets, in the order ``videocrop`` documents them.
CROP_EDGES = ("top", "bottom", "left", "right")

#: The GStreamer element the ROI compiles to — the same one the classic
#: ``GstPipelineBuilder`` path and the catalog's ``crop`` node emit.
VIDEOCROP_FACTORY = "videocrop"


def normalized_crop(image_crop: Any) -> Optional[Dict[str, int]]:
    """The persisted ``imageCrop`` as four non-negative ints, or ``None``
    when it is not a usable crop.

    ``None`` means "do not touch the document": the value is absent, is
    not a mapping, carries a non-integral or negative edge, or omits every
    edge. A well-formed crop is returned even when it is all zeros — that
    is a *valid* no-op the caller distinguishes with :func:`is_no_op`, so
    an intentional "no crop" can be logged differently from a malformed
    one.

    Missing individual edges default to 0, matching both the catalog
    ``crop`` node's parameter defaults and ``videocrop``'s own.
    ``bool`` is rejected even though it is an ``int`` subclass: a boolean
    edge inset is a caller mistake, not a pixel count.
    """
    if not isinstance(image_crop, Mapping):
        return None

    crop: Dict[str, int] = {}
    seen_edge = False
    for edge in CROP_EDGES:
        raw = image_crop.get(edge)
        if raw is None:
            crop[edge] = 0
            continue
        seen_edge = True
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, float):
            # Accept 100.0 (JSON round-trips can widen ints) but not 100.5,
            # which would render an unparsable videocrop argument.
            if not raw.is_integer():
                return None
            value = int(raw)
        elif isinstance(raw, str):
            try:
                value = int(raw.strip())
            except ValueError:
                return None
        else:
            return None
        if value < 0:
            return None
        crop[edge] = value

    return crop if seen_edge else None


def is_no_op(crop: Mapping[str, int]) -> bool:
    """True when every edge inset is zero, so the crop would remove
    nothing. Rendering ``videocrop top=0 bottom=0 left=0 right=0`` is
    harmless but pointless, and leaving it out keeps the launch string
    byte-identical to the pre-feature one."""
    return all(int(crop.get(edge, 0)) == 0 for edge in CROP_EDGES)


def fits_frame(crop: Mapping[str, int], width: Any, height: Any) -> bool:
    """True when the crop leaves at least one pixel in both dimensions of
    a ``width`` x ``height`` frame.

    ``videocrop`` fails to negotiate when the insets consume the whole
    frame, which would fail the run outright. The caller would rather skip
    an impossible ROI (and say so) than break a workflow that was
    otherwise fine, so this is checked against the frame actually grabbed
    rather than assumed. Unknown or non-positive dimensions cannot be
    checked and are treated as "cannot confirm it fits".
    """
    try:
        frame_width = int(width)
        frame_height = int(height)
    except (TypeError, ValueError):
        return False
    if frame_width <= 0 or frame_height <= 0:
        return False
    horizontal = int(crop.get("left", 0)) + int(crop.get("right", 0))
    vertical = int(crop.get("top", 0)) + int(crop.get("bottom", 0))
    return horizontal < frame_width and vertical < frame_height


def cropped_size(crop: Mapping[str, int], width: int, height: int):
    """The ``(width, height)`` a fitting crop leaves — for the log line
    that tells an operator what the run actually inferred on."""
    return (
        int(width) - int(crop.get("left", 0)) - int(crop.get("right", 0)),
        int(height) - int(crop.get("top", 0)) - int(crop.get("bottom", 0)),
    )


def has_explicit_crop(document: Mapping[str, Any]) -> bool:
    """True when the document already renders a ``videocrop`` element.

    That element only comes from the catalog's ``crop`` node, so its
    presence means the graph author already decided how this workflow
    crops. The device ROI defers to them rather than cropping twice.
    """
    for segment in document.get("segments", []) or []:
        for element in segment.get("elements", []) or []:
            if isinstance(element, Mapping) and (
                element.get("factory") == VIDEOCROP_FACTORY
            ):
                return True
    return False


def insert_crop_after_node(
    document: Dict[str, Any], node_id: Any, crop: Mapping[str, int]
) -> bool:
    """Splice ``videocrop`` in after ``node_id``'s last element.

    Mutates ``document`` in place (the executor already mutates it to
    point the appsrc at the Frame_Feed) and returns whether an element was
    inserted. The new element carries the source node's ``nodeId`` so a
    ``videocrop`` negotiation failure is attributed to that node by the
    executor's bus-error mapping, exactly like the injected ``bayer2rgb``.

    Must run AFTER the appsrc rewrite: that step may insert ``bayer2rgb``
    under the same ``nodeId``, and this inserts after the node's last
    element so the crop lands downstream of the demosaic either way.
    """
    for segment in document.get("segments", []) or []:
        elements = segment.get("elements")
        if not elements:
            continue
        last_index = None
        for index, element in enumerate(elements):
            if isinstance(element, Mapping) and element.get("nodeId") == node_id:
                last_index = index
        if last_index is None:
            continue
        elements.insert(
            last_index + 1,
            {
                "nodeId": node_id,
                "factory": VIDEOCROP_FACTORY,
                "args": {edge: int(crop.get(edge, 0)) for edge in CROP_EDGES},
            },
        )
        return True
    return False
