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
"""The stream frame-feed planner (rtsp-rtmp-stream-cameras design
component 13; Requirements 10.1, 10.6).

Pure: :func:`plan_stream_feeds` reads a compiled document's
``streamBinding`` point, the registration's Camera_Binding resolution and
the device's configured stream cameras, and returns the one
:class:`StreamFeed` the executor feeds from, or ``[]`` for a document
without a stream node (the exact pre-feature path).

The camera a feed reads is the first of these that applies (Property 15):

1. the camera the binding resolved to (a ``cameraSourceId`` binding);
2. the configured stream camera whose normalized Stream_URL equals the
   node's effective ``url`` (an unbound node, or one bound by override);
3. an anonymous, credential-less session to that URL (``url-<hash>``).

The node's own processing parameters (mode, rate, frame age, retention)
always come from the node, never from the camera.
"""
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from workflow_engine.camera_binding import stream_point_protocol
from workflow_engine.vendor.workflow_core.stream_url import normalize_stream_url

PROCESSING_CONTINUOUS = "continuous"
PROCESSING_ON_TRIGGER = "on_trigger"

#: The node's parameter defaults (the catalog's ``_STREAM_SOURCE_PARAMETERS``).
DEFAULTS = {
    "processing_mode": PROCESSING_CONTINUOUS,
    "frames_per_second": 1.0,
    "max_frame_age_ms": 2000,
    "keep_recent_runs": 20,
    "keep_notable_runs": 200,
}

_TYPE_BY_PROTOCOL = {"rtsp": "RTSP", "rtmp": "RTMP"}


class StreamFeedError(Exception):
    """A stream feed failure, attributed to the stream node (``node_id``
    None for document-level problems). The message names the camera and
    never holds a credential: Stream_URLs are credential-free by rule."""

    def __init__(self, node_id: Optional[str], message: str):
        super().__init__(message)
        self.node_id = node_id


@dataclass(frozen=True)
class StreamFeed:
    """The stream a run's single Frame_Feed reads."""

    node_id: str
    protocol: str
    camera_key: str
    url: str
    processing_mode: str
    frames_per_second: float
    max_frame_age_ms: int
    keep_recent_runs: int
    keep_notable_runs: int
    #: The Camera_Source the feed reads (``cfg-<imageSourceId>``), or None
    #: for an anonymous stream.
    camera_source_id: Optional[str] = None
    #: How the camera was chosen: ``binding``, ``url_match`` or ``anonymous``.
    selected_by: str = "anonymous"
    #: The stream settings of an anonymous session.
    settings: Dict[str, Any] = field(default_factory=dict)

    @property
    def source_type(self) -> str:
        return _TYPE_BY_PROTOCOL[self.protocol]

    @property
    def continuous(self) -> bool:
        return self.processing_mode == PROCESSING_CONTINUOUS


ConfiguredCameras = Mapping[str, str]


def stream_points(document: Any) -> List[Mapping[str, Any]]:
    """The document's ``streamBinding`` points."""
    points = document.get("bindingPoints") if isinstance(document, dict) else None
    return [point for point in (points or [])
            if isinstance(point, Mapping) and point.get("streamBinding") is True]


def _number(value, cast, default):
    if isinstance(value, bool):
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _normalized(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return normalize_stream_url(url)
    except ValueError:
        return None


def plan_stream_feeds(document: Any, resolution=None,
                      configured_cameras: Union[ConfiguredCameras, Callable[[], ConfiguredCameras], None] = None
                      ) -> List[StreamFeed]:
    """The stream feed of ``document`` (see the module docstring).

    ``configured_cameras`` maps each configured stream camera's normalized
    Stream_URL to its Camera_Source id (``cfg-<imageSourceId>``), or is a
    zero-argument callable returning that mapping; it is only consulted
    when no binding chose the camera. Raises :class:`StreamFeedError` for a
    document with more than one stream node, or a node without a usable
    URL.
    """
    points = stream_points(document)
    if not points:
        return []
    if len(points) > 1:
        node_ids = ", ".join("'{0}'".format(point.get("nodeId")) for point in points)
        raise StreamFeedError(None, "document declares {0} stream camera source binding points "
                                    "({1}); a workflow reads exactly one stream".format(len(points), node_ids))
    point = points[0]
    node_id = point.get("nodeId")
    protocol = stream_point_protocol(point)
    if protocol is None:
        raise StreamFeedError(node_id, "stream camera source has no protocol")
    parameters = dict(point.get("parameters") or {})
    assignments = getattr(resolution, "stream_assignments", None) or {}
    assignment = assignments.get(node_id) if isinstance(assignments, Mapping) else None
    assignment_params = dict((assignment or {}).get("params") or {})
    bound_camera = (assignment or {}).get("cameraSourceId")

    if assignment is not None and not bound_camera:
        # An override binding: its values replace the node's rendered ones.
        parameters.update(assignment_params)

    selected_by, camera_source_id = "anonymous", None
    if bound_camera:
        url = _normalized(assignment_params.get("url")) or _normalized(parameters.get("url"))
        selected_by, camera_source_id = "binding", str(bound_camera)
    else:
        url = _normalized(parameters.get("url"))
        cameras = configured_cameras() if callable(configured_cameras) else (configured_cameras or {})
        match = cameras.get(url) if url else None
        if match:
            selected_by, camera_source_id = "url_match", str(match)
    if not url:
        raise StreamFeedError(node_id, "stream camera source has no valid stream URL")

    if camera_source_id is not None:
        camera_key = camera_source_id
    else:
        from stream_ingest.manager import camera_key_for_url
        camera_key = camera_key_for_url(url)

    mode = parameters.get("processing_mode")
    if mode not in (PROCESSING_CONTINUOUS, PROCESSING_ON_TRIGGER):
        mode = DEFAULTS["processing_mode"]
    return [StreamFeed(
        node_id=node_id,
        protocol=protocol,
        camera_key=camera_key,
        url=url,
        processing_mode=mode,
        frames_per_second=_number(parameters.get("frames_per_second"), float, DEFAULTS["frames_per_second"]),
        max_frame_age_ms=_number(parameters.get("max_frame_age_ms"), int, DEFAULTS["max_frame_age_ms"]),
        keep_recent_runs=_number(parameters.get("keep_recent_runs"), int, DEFAULTS["keep_recent_runs"]),
        keep_notable_runs=_number(parameters.get("keep_notable_runs"), int, DEFAULTS["keep_notable_runs"]),
        camera_source_id=camera_source_id,
        selected_by=selected_by,
    )]


def configured_stream_cameras(image_sources) -> Dict[str, str]:
    """``{normalized Stream_URL: cfg-<imageSourceId>}`` of the configured
    stream Image_Sources among ``image_sources`` (ORM rows or dicts). The
    first configured camera wins when two share a URL."""
    from model.stream_source import is_stream_source_type

    cameras: Dict[str, str] = {}
    for source in sorted(image_sources or (), key=lambda item: str(_field(item, "imageSourceId") or "")):
        if not is_stream_source_type(_field(source, "type")):
            continue
        url = _normalized(_field(source, "location"))
        if url and url not in cameras:
            cameras[url] = "cfg-" + str(_field(source, "imageSourceId"))
    return cameras


def _field(record, key):
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def load_configured_stream_cameras(session) -> Dict[str, str]:
    """:func:`configured_stream_cameras` of the device's Image_Sources."""
    from dao.sqlite_db import models

    rows = session.query(models.ImageSource).filter(
        models.ImageSource.type.in_([models.ImageSourceType.RTSP, models.ImageSourceType.RTMP])).all()
    return configured_stream_cameras(rows)


#: The Trigger_Context ``source`` of a run the Continuous_Runner started.
#: Its context is ``{"source": "continuous", "frameSeq": N,
#: "frameAcquiredAtMs": ..., "tickAtMs": ...}`` (design component 14), where
#: ``frameSeq`` is the Latest_Frame sequence number the tick chose.
CONTINUOUS_SOURCE = "continuous"


def continuous_frame_seq(trigger_context: Any) -> Optional[int]:
    """The frame sequence number a continuous run was started for, else
    None (an on-trigger run takes the newest fresh frame)."""
    if not isinstance(trigger_context, Mapping) or trigger_context.get("source") != CONTINUOUS_SOURCE:
        return None
    seq = trigger_context.get("frameSeq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else None


class FrameHandoff:
    """The frame a Continuous_Runner tick chose, handed to the run it
    started (design component 14).

    The runner reads the Latest_Frame at its tick (the only way to know a
    newer frame exists) and puts it here under the run's execution id; the
    executor's stream feed takes it instead of reading the camera again.
    The run then analyzes exactly the tick's frame, each frame sequence
    number starts at most one run, and a multi-megabyte frame is copied
    out of the Stream_Worker once, not twice. Entries live only while
    their run does: the runner discards its entry when the run returns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: Dict[str, Any] = {}

    def put(self, execution_id: str, frame: Any) -> None:
        with self._lock:
            self._frames[str(execution_id)] = frame

    def take(self, execution_id: str) -> Optional[Any]:
        with self._lock:
            return self._frames.pop(str(execution_id), None)

    def discard(self, execution_id: str) -> None:
        self.take(execution_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


#: The process-wide handoff shared by the Continuous_Runner and the executor.
FRAME_HANDOFF = FrameHandoff()


def document_has_stream_or_analytics(document: Any) -> bool:
    """Whether the run metadata gets a ``frame`` key: the document reads a
    stream, or runs a Scene_Analytics_Node (design component 13)."""
    if stream_points(document):
        return True
    bindings = document.get("executorBindings") if isinstance(document, dict) else None
    return any(isinstance(binding, Mapping)
               and binding.get("binding") in ("detection_counter", "object_association", "event_gate")
               for binding in (bindings or []))
