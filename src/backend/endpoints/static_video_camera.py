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
"""Video_Pin_API: pin, inspect, and remove the Static_Video_Camera's video.

Feature: static-camera-video-loop. The routes mirror the Static_Image_Camera
Pin_API (``endpoints/static_image_camera.py``) on their own path, backed by
the separate :class:`~utils.static_video_camera.StaticVideoStore`:

- ``POST /static-video-camera/pin`` — multipart upload (part ``file``) or
  JSON ``{"capturedImagePath": ...}`` referencing a video file under the
  captured-image roots. 200 → ``{cameraId, metadata}``.
- ``GET /static-video-camera/pin`` — ``{pinned, cameraId, metadata}``.
- ``DELETE /static-video-camera/pin`` — ``{cameraId, pinned: false}``.

Every validation or storage failure is an HTTP 400 carrying the store's
descriptive message. The multipart parser and the captured-roots guard are
the image Pin_API's, imported rather than copied; only the body cap (the
100 MB video limit plus the multipart envelope) differs.

A successful pin, replace or unpin requests a debounced camera-registry
inventory report (Requirement 4.6): a replace keeps the camera's
enumeration unchanged, so without the request the reported Video_Metadata
would stay stale until some other report trigger.
"""
import logging

from fastapi import HTTPException, Request
from starlette.status import HTTP_400_BAD_REQUEST

from camera_sync.hooks import notify_image_source_changed
from endpoints import static_image_camera as image_pin_api
from endpoints.route.access_log_router import get_api_router
from utils.static_video_camera import (
    STATIC_VIDEO_CAMERA_ID,
    StaticVideoPinError,
    get_store,
)
from utils.video_loop import MAX_PIN_VIDEO_BYTES

logger = logging.getLogger(__name__)

router = get_api_router()

# Allowance on top of MAX_PIN_VIDEO_BYTES for the multipart envelope
# (boundary lines + part headers) when hard-capping the body read.
_MULTIPART_ENVELOPE_ALLOWANCE = 64 * 1024


def _bad_request(detail):
    return HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)


def _oversize_exception():
    return _bad_request(
        "Submitted video file exceeds the maximum accepted video file size "
        "of {} bytes.".format(MAX_PIN_VIDEO_BYTES)
    )


async def _read_body_capped(request):
    """Read the raw request body, rejecting once the hard cap is exceeded
    (the store's ``pin_bytes`` still enforces the exact limit)."""
    cap = MAX_PIN_VIDEO_BYTES + _MULTIPART_ENVELOPE_ALLOWANCE
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise _oversize_exception()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > cap:
            raise _oversize_exception()
    return bytes(body)


def _extract_uploaded_video(body, content_type):
    """``(data, file_name)`` of the ``file`` part (the image Pin_API's
    parser), with its "image" wording adapted for the video route."""
    try:
        data, file_name = image_pin_api._extract_uploaded_file(body, content_type)
    except HTTPException as error:
        detail = str(error.detail).replace("carry the image", "carry the video")
        raise _bad_request(detail) from error
    if file_name == "uploaded_image":
        file_name = "uploaded_video"
    return data, file_name


@router.post("/static-video-camera/pin")
async def pin_static_video(request: Request):
    """Pin an uploaded video (multipart ``file``) or an existing on-device
    video file (JSON ``{"capturedImagePath": ...}``); 400 with the store's
    message for anything Video_Validation rejects (Requirements 1.1–1.9)."""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() == "multipart/form-data":
        body = await _read_body_capped(request)
        data, file_name = _extract_uploaded_video(body, content_type)
        del body
        try:
            metadata = get_store().pin_bytes(data, file_name)
        except StaticVideoPinError as error:
            raise _bad_request(str(error)) from error
    else:
        try:
            payload = await request.json()
        except Exception as error:
            raise _bad_request(
                "Malformed pin request: expected a multipart 'file' upload "
                "or a JSON body with 'capturedImagePath'."
            ) from error
        captured_path = (
            payload.get("capturedImagePath") if isinstance(payload, dict) else None
        )
        if not isinstance(captured_path, str) or not captured_path:
            raise _bad_request(
                "Malformed pin request: the JSON body must carry a "
                "non-empty 'capturedImagePath' string."
            )
        captures_root = image_pin_api._containing_captures_root(captured_path)
        if captures_root is None:
            raise _bad_request(
                "The referenced file path resolves outside the captured "
                "images directory and cannot be pinned: {}".format(captured_path)
            )
        try:
            metadata = get_store().pin_file(captured_path, captures_root)
        except StaticVideoPinError as error:
            raise _bad_request(str(error)) from error
    # Report the new Video_Metadata (Requirement 4.6); never raises.
    notify_image_source_changed()
    return {"cameraId": STATIC_VIDEO_CAMERA_ID, "metadata": metadata}


@router.get("/static-video-camera/pin")
def get_video_pin_status():
    """Pin status + Video_Metadata (Requirement 1.8)."""
    return get_store().status()


@router.delete("/static-video-camera/pin")
def unpin_static_video():
    """Remove the Pinned_Video (Requirement 5.3); 400 "no video is pinned"
    when nothing is pinned (Requirement 5.4)."""
    try:
        get_store().unpin()
    except StaticVideoPinError as error:
        raise _bad_request(str(error)) from error
    # Report the camera's removal right away (Requirement 4.6); never raises.
    notify_image_source_changed()
    return {"cameraId": STATIC_VIDEO_CAMERA_ID, "pinned": False}
