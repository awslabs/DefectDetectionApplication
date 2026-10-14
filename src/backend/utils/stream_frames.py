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
"""Single frames of stream Image_Sources for preview and capture
(rtsp-rtmp-stream-cameras Requirement 4.4).

A preview or capture of an RTSP/RTMP Image_Source takes the Latest_Frame of
its shared Stream_Session through the StreamBroadcaster, exactly as
inference takes a camera frame: a viewer's running session is reused, and
otherwise a lease holds the session open for the one grab. The frame is
RGB, and :data:`STREAM_FRAME_PIPELINE` is the appsrc caps chain the
Pipeline_Configuration builder puts after it.
"""
from fastapi import HTTPException
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from model.stream_source import STREAM_FRAME_PIPELINE  # noqa: F401 - re-exported
from stream_ingest.manager import camera_key_for_image_source, get_stream_ingest_manager


def stream_frame_config(image_source_dict: dict) -> dict:
    """The StreamBroadcaster config of a stream Image_Source: its type and
    id, from which the stream backend resolves the session."""
    source_type = image_source_dict.get("type")
    return {
        "type": getattr(source_type, "value", source_type),
        "imageSourceId": image_source_dict.get("imageSourceId"),
    }


def get_stream_frame(image_source_dict: dict) -> dict:
    """The Latest_Frame of a stream Image_Source as the legacy
    ``{"data", "height", "width"}`` frame dict.

    Raises a 503 naming the session state when the camera has no frame, for
    example while it reconnects or after an authentication failure. The
    state and message come from Stream_Health, which is redacted.
    """
    # Imported here: the broadcaster pulls in the streaming backends, which
    # only frame grabs need.
    from utils.streaming.broadcaster import get_broadcaster

    image_source_id = image_source_dict.get("imageSourceId")
    frame = get_broadcaster().get_inference_frame(
        camera_key_for_image_source(image_source_id), stream_frame_config(image_source_dict))
    if frame is not None:
        return frame
    health = get_stream_ingest_manager().health_for_image_source(image_source_id) or {}
    state = health.get("state") or "not streaming"
    last_error = (health.get("lastError") or {}).get("message")
    reason = f"state {state}" + (f": {last_error}" if last_error else "")
    raise HTTPException(
        status_code=HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"The stream camera has no frame to show ({reason}). Check the camera and its connection test, then try again.",
    )
