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
"""Pin_API for the Static_Image_Camera (feature: static-image-camera-source).

Routes (design section "2. endpoints/static_image_camera.py"):

    POST   /static-image-camera/pin   multipart ``file`` upload, or JSON
                                      ``{"capturedImagePath": ...}``
    GET    /static-image-camera/pin   pin status + metadata
    DELETE /static-image-camera/pin   unpin

The upload variant reads the request body itself with a hard cap — the
request is rejected as soon as more than ``MAX_PIN_FILE_BYTES`` (plus a
small multipart-envelope allowance) have been consumed, never buffering
unbounded input (Requirement 1.4). The multipart payload is parsed with
the small self-contained parser below because the backend image does not
ship ``python-multipart`` (FastAPI's ``File``/``UploadFile`` and
starlette's ``MultiPartParser`` both require it, and ``requirements.txt``
is preservation-tracked, so the dependency cannot be added).

The captured-image variant accepts only paths that resolve (realpath +
commonpath) under one of the on-device captured-image roots — the
Image_Source capture directory (``/aws_dda/image-capture``) and the
workflow inference-results directory (``/aws_dda/inference-results``, cf.
``workflow_accessor.__create_folder``). Arbitrary device paths are not
exposed (Requirements 1.7, 1.8).

All validation/storage failures surface as HTTP 400 carrying the
descriptive ``StaticImagePinError`` message per the design's Error
Handling table (Requirements 1.3, 1.4, 1.8, 5.3, 5.5).
"""
import logging
import os

from fastapi import HTTPException, Request
from starlette.status import HTTP_400_BAD_REQUEST

from endpoints.route.access_log_router import get_api_router
from utils import constants
from utils.static_image_camera import (
    MAX_PIN_FILE_BYTES,
    STATIC_IMAGE_CAMERA_ID,
    StaticImagePinError,
    get_store,
)

logger = logging.getLogger(__name__)

router = get_api_router()

# On-device roots that may hold previously captured images (Requirement
# 1.7). Module-level so tests can point them at a temporary captures root.
CAPTURED_IMAGE_ROOTS = (
    constants.IMAGE_CAPTURE_DIR,
    constants.INFERENCE_RESULTS_DIR,
)

# Allowance on top of MAX_PIN_FILE_BYTES for the multipart envelope
# (boundary lines + part headers) when hard-capping the body read.
_MULTIPART_ENVELOPE_ALLOWANCE = 64 * 1024


def _oversize_exception():
    return HTTPException(
        status_code=HTTP_400_BAD_REQUEST,
        detail=(
            "Submitted image file exceeds the maximum accepted file size "
            "of {} bytes.".format(MAX_PIN_FILE_BYTES)
        ),
    )


def _bad_request(detail):
    return HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)


async def _read_body_capped(request):
    """Read the raw request body, rejecting once the hard cap is exceeded.

    The cap is ``MAX_PIN_FILE_BYTES`` plus the multipart-envelope
    allowance; the store's ``pin_bytes`` still enforces the exact file
    limit afterwards (Requirement 1.4)."""
    cap = MAX_PIN_FILE_BYTES + _MULTIPART_ENVELOPE_ALLOWANCE
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise _oversize_exception()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > cap:
            raise _oversize_exception()
    return bytes(body)


def _multipart_boundary(content_type):
    """Extract the boundary parameter from a multipart Content-Type."""
    for param in content_type.split(";")[1:]:
        name, _, value = param.strip().partition("=")
        if name.strip().lower() == "boundary" and value:
            return value.strip().strip('"')
    raise _bad_request(
        "Malformed multipart request: no boundary parameter in the "
        "Content-Type header."
    )


def _extract_uploaded_file(body, content_type):
    """Return ``(data, file_name)`` for the ``file`` part of ``body``.

    Minimal RFC 2046 multipart/form-data parsing: parts are delimited by
    ``--boundary`` lines, each part carries headers separated from its
    content by a blank line, and the part content ends with the CRLF that
    precedes the next delimiter."""
    delimiter = b"--" + _multipart_boundary(content_type).encode("utf-8")
    for section in body.split(delimiter):
        # The preamble before the first delimiter and the epilogue after
        # the closing "--" delimiter are not parts.
        if not section or section.startswith(b"--"):
            continue
        part = section
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, separator, content = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        disposition = ""
        for line in header_blob.split(b"\r\n"):
            name, _, value = line.decode("utf-8", "replace").partition(":")
            if name.strip().lower() == "content-disposition":
                disposition = value.strip()
                break
        params = {}
        for item in disposition.split(";")[1:]:
            name, _, value = item.strip().partition("=")
            params[name.strip().lower()] = value.strip().strip('"')
        if params.get("name") == "file":
            return content, params.get("filename") or "uploaded_image"
    raise _bad_request(
        "Malformed pin request: multipart uploads must carry the image in "
        "a form part named 'file'."
    )


def _containing_captures_root(path):
    """Return the captured-image root containing ``path``, or ``None``.

    Same realpath + commonpath containment check the store's ``pin_file``
    re-applies (path-traversal guard, Requirement 1.7 security)."""
    real_path = os.path.realpath(path)
    for root in CAPTURED_IMAGE_ROOTS:
        real_root = os.path.realpath(root)
        try:
            if os.path.commonpath([real_path, real_root]) == real_root:
                return root
        except ValueError:
            continue
    return None


@router.post("/static-image-camera/pin")
async def pin_static_image(request: Request):
    """Pin an uploaded image (multipart ``file``) or an existing on-device
    captured image (JSON ``{"capturedImagePath": ...}``).

    200 → ``{cameraId, metadata}`` (Requirements 1.1, 1.5; a replace
    returns the same success confirmation, Requirement 5.1). 400 with the
    descriptive message on undecodable input (naming JPEG/PNG/BMP,
    Requirement 1.3), oversize input (naming the limit, Requirement 1.4),
    missing captured-image references (Requirement 1.8), and paths
    escaping the captured-image roots."""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() == "multipart/form-data":
        body = await _read_body_capped(request)
        data, file_name = _extract_uploaded_file(body, content_type)
        try:
            metadata = get_store().pin_bytes(data, file_name)
        except StaticImagePinError as error:
            raise _bad_request(str(error)) from error
    else:
        try:
            payload = await request.json()
        except Exception as error:
            raise _bad_request(
                "Malformed pin request: expected a multipart 'file' upload "
                "or a JSON body with 'capturedImagePath'."
            ) from error
        captured_image_path = (
            payload.get("capturedImagePath")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(captured_image_path, str) or not captured_image_path:
            raise _bad_request(
                "Malformed pin request: the JSON body must carry a "
                "non-empty 'capturedImagePath' string."
            )
        captures_root = _containing_captures_root(captured_image_path)
        if captures_root is None:
            raise _bad_request(
                "The referenced captured image path resolves outside the "
                "captured images directory and cannot be pinned: "
                "{}".format(captured_image_path)
            )
        try:
            metadata = get_store().pin_file(captured_image_path, captures_root)
        except StaticImagePinError as error:
            raise _bad_request(str(error)) from error
    return {"cameraId": STATIC_IMAGE_CAMERA_ID, "metadata": metadata}


@router.get("/static-image-camera/pin")
def get_pin_status():
    """Pin status + metadata (Requirements 1.6, 6.2)."""
    return get_store().status()


@router.delete("/static-image-camera/pin")
def unpin_static_image():
    """Remove the Pinned_Image (Requirement 5.4); 400 "no image is
    pinned" when nothing is pinned (Requirement 5.5)."""
    try:
        get_store().unpin()
    except StaticImagePinError as error:
        raise _bad_request(str(error)) from error
    return {"cameraId": STATIC_IMAGE_CAMERA_ID, "pinned": False}
