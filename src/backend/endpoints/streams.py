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
"""Broadcast-model live-preview stream API endpoints.

These endpoints are **thin clients** over the process-wide
:class:`~utils.streaming.broadcaster.StreamBroadcaster` singleton
(:func:`~utils.streaming.broadcaster.get_broadcaster`). They translate the
broadcaster's domain results (:class:`SubscribeResult`, :class:`FrameResult`, the
device-accepted settings dict, and the broadcaster's ``NoActiveSessionError`` /
``SettingsApplyError``) into HTTP responses and status codes. All device access and
the single-claim / lifecycle invariants live in the broadcaster; this layer adds no
acquisition logic of its own.

Routes (registered under the same auth / access-log router as the other endpoints):

* ``POST /streams/{camera_id}/subscribe``   — start-on-first-viewer; returns the
  server-issued ``viewerId`` and current ``viewerCount`` (Req 1.2, 3.1, 8.1).
  Rejections map ``viewer_limit`` -> 429 and ``camera_unavailable`` -> 503.
* ``GET  /streams/{camera_id}/frame``        — latest frame for a viewer; doubles as a
  heartbeat (Req 2.3, 4.1, 4.6). ``OK`` / ``STALE`` return the frame bytes (200, with a
  ``X-Frame-Status`` header flagging staleness), ``NO_FRAME`` returns 204, and
  ``DISCONNECTED`` returns 503.
* ``POST /streams/{camera_id}/heartbeat``    — explicit keep-alive; 200 when refreshed,
  404 when the viewer / session is unknown (Req 8.2, 8.3).
* ``POST /streams/{camera_id}/unsubscribe``  — stop-on-last-viewer; always 200 (Req 3.6,
  8.8).
* ``GET  /streams/{camera_id}/viewers``      — active viewer count (Req 8.4).
* ``POST /streams/{camera_id}/settings``     — apply gain/exposure/advanced controls to
  the live session (Req 5.1, 5.2); ``NoActiveSessionError`` -> 409, ``SettingsApplyError``
  -> 422 naming the failed control.

Frame transport: the broadcaster's :class:`~utils.streaming.models.LatestFrame` carries
the raw image payload bytes (the same shape as the legacy ``get_camera_frame`` payload).
To stay a thin pass-through (and avoid re-grabbing / re-encoding through the heavy
GStreamer preview pipeline, which would defeat the broadcast model), the frame endpoint
returns those bytes directly as the response body with the frame metadata
(``seq`` / ``width`` / ``height`` / ``acquired_at`` / freshness) carried in
``X-Frame-*`` headers. See the module-level note in the task report for this decision.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Body, Query, HTTPException, Response
from pydantic import BaseModel
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from endpoints.route.access_log_router import get_api_router
from utils.streaming.broadcaster import (
    NoActiveSessionError,
    SettingsApplyError,
    get_broadcaster,
)
from utils.streaming.models import FrameStatus

logger = logging.getLogger(__name__)

router = get_api_router()


# --- request / response models -------------------------------------------


class SubscribeRequest(BaseModel):
    """Optional body for subscribe; carries the image-source/stream config.

    The ``config`` dict is forwarded verbatim to the broadcaster, which uses it (its
    ``type`` / ``imageSourceConfiguration``) to build the backend when starting a new
    session. It is ignored when a session already exists (the existing claim is reused).
    """

    config: Optional[dict] = None


class SubscribeResponse(BaseModel):
    """Accepted-subscription payload: the new viewer id and the active viewer count."""

    viewerId: str
    viewerCount: int


class ViewerIdBody(BaseModel):
    """Body carrying a ``viewerId`` for heartbeat / unsubscribe.

    Supports clients that POST a JSON body (and ``navigator.sendBeacon`` on tab close,
    which cannot set query params). The ``viewerId`` may alternatively be supplied as a
    query parameter.
    """

    viewerId: Optional[str] = None


class HeartbeatResponse(BaseModel):
    """Result of an explicit heartbeat: whether the viewer was refreshed."""

    refreshed: bool


class ViewerCountResponse(BaseModel):
    """Active viewer count for a camera."""

    viewerCount: int


def _resolve_viewer_id(query_viewer_id: Optional[str], body: Optional[ViewerIdBody]) -> str:
    """Return the viewer id from the query param or body, or raise 422 when absent.

    Accepts the id from either source so both query-string clients and JSON-body /
    ``sendBeacon`` clients work against the same endpoint.
    """
    viewer_id = query_viewer_id or (body.viewerId if body is not None else None)
    if not viewer_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="viewerId is required (provide it as a query parameter or in the request body).",
        )
    return viewer_id


# --- endpoints ------------------------------------------------------------


@router.post("/streams/{camera_id}/subscribe")
def subscribe_stream(
    camera_id: str, request: SubscribeRequest = SubscribeRequest()
) -> SubscribeResponse:
    """Register a new viewer, starting the session on the first subscriber.

    Maps the broadcaster's :class:`SubscribeResult`: accepted -> 200 with the new
    ``viewerId`` / ``viewerCount``; ``viewer_limit`` -> 429; ``camera_unavailable`` ->
    503 (Req 1.2, 3.1, 8.1).
    """
    result = get_broadcaster().subscribe(camera_id, request.config)
    if result.accepted:
        return SubscribeResponse(viewerId=result.viewer_id, viewerCount=result.viewer_count)

    if result.reason == "viewer_limit":
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Camera {camera_id} has reached the maximum number of concurrent viewers "
                f"({result.viewer_count}). Try again when a viewer disconnects."
            ),
        )
    if result.reason == "camera_unavailable":
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Camera {camera_id} is unavailable and could not start streaming. "
                f"Check the camera connection and try again."
            ),
        )
    # Defensive: any other rejection reason is surfaced as a generic 503.
    raise HTTPException(
        status_code=HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Unable to subscribe to camera {camera_id}: {result.reason}.",
    )


@router.get("/streams/{camera_id}/frame")
def get_stream_frame(camera_id: str, viewerId: str = Query(...)) -> Response:
    """Return the latest frame for a viewer; the request doubles as a heartbeat.

    Maps the broadcaster's :class:`FrameResult` status:

    * ``OK``   -> 200 with the raw frame bytes as the body and ``X-Frame-Status: ok``.
    * ``STALE``-> 200 with the (still-served) frame bytes and ``X-Frame-Status: stale``
      so the client can flag staleness (Req 4.7).
    * ``NO_FRAME`` -> 204 No Content (session up, nothing published yet — Req 2.6).
    * ``DISCONNECTED`` -> 503 (camera dropped or not streaming — Req 7.5).

    Frame metadata (sequence, dimensions, acquired-at) is returned in ``X-Frame-*``
    headers alongside the binary body.
    """
    result = get_broadcaster().get_frame(camera_id, viewerId)

    if result.status in (FrameStatus.OK, FrameStatus.STALE):
        frame = result.frame
        if frame is None:
            # Defensive: OK/STALE should always carry a frame; treat a missing payload
            # as "nothing to serve yet" rather than emitting an empty body.
            return Response(status_code=HTTP_204_NO_CONTENT)
        headers = {
            "X-Frame-Status": result.status.value,
            "X-Frame-Seq": str(frame.seq),
            "X-Frame-Width": str(frame.width),
            "X-Frame-Height": str(frame.height),
            "X-Frame-Acquired-At": str(frame.acquired_at),
        }
        return Response(
            content=frame.data,
            media_type="application/octet-stream",
            headers=headers,
        )

    if result.status == FrameStatus.NO_FRAME:
        # Session is up but no frame has been published yet.
        return Response(
            status_code=HTTP_204_NO_CONTENT,
            headers={"X-Frame-Status": FrameStatus.NO_FRAME.value},
        )

    # DISCONNECTED: the camera has dropped or there is no active session.
    raise HTTPException(
        status_code=HTTP_503_SERVICE_UNAVAILABLE,
        detail=result.error
        or f"Camera {camera_id} is disconnected; no live frame is available.",
    )


@router.post("/streams/{camera_id}/heartbeat")
def heartbeat_stream(
    camera_id: str,
    viewerId: Optional[str] = Query(default=None),
    body: Optional[ViewerIdBody] = Body(default=None),
) -> HeartbeatResponse:
    """Refresh a viewer's keep-alive timestamp.

    Returns 200 with ``refreshed=True`` when the viewer exists and was refreshed; 404
    when the camera has no session or the viewer id is unknown / expired so the client
    knows to re-subscribe (Req 8.2, 8.3).
    """
    viewer_id = _resolve_viewer_id(viewerId, body)
    refreshed = get_broadcaster().heartbeat(camera_id, viewer_id)
    if not refreshed:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"Viewer {viewer_id} is not subscribed to camera {camera_id} "
                f"(unknown or expired); re-subscribe to continue."
            ),
        )
    return HeartbeatResponse(refreshed=True)


@router.post("/streams/{camera_id}/unsubscribe")
def unsubscribe_stream(
    camera_id: str,
    viewerId: Optional[str] = Query(default=None),
    body: Optional[ViewerIdBody] = Body(default=None),
) -> ViewerCountResponse:
    """Deregister a viewer; stop the session if it was the last (Req 3.6, 8.8).

    Unsubscribing is idempotent — removing an unknown camera/viewer is a no-op — so this
    always returns 200 with the resulting active viewer count.
    """
    viewer_id = _resolve_viewer_id(viewerId, body)
    broadcaster = get_broadcaster()
    broadcaster.unsubscribe(camera_id, viewer_id)
    return ViewerCountResponse(viewerCount=broadcaster.viewer_count(camera_id))


@router.get("/streams/{camera_id}/viewers")
def get_stream_viewers(camera_id: str) -> ViewerCountResponse:
    """Return the active viewer count for a camera (0 when not streaming) (Req 8.4)."""
    return ViewerCountResponse(viewerCount=get_broadcaster().viewer_count(camera_id))


@router.post("/streams/{camera_id}/settings")
def apply_stream_settings(camera_id: str, features: dict = Body(default={})) -> dict:
    """Apply gain/exposure/advanced controls to the live session (Req 5.1, 5.2).

    Forwards the request body to the broadcaster's ``apply_settings`` and returns the
    device-accepted values. Maps the broadcaster's errors: ``NoActiveSessionError`` ->
    409 (the camera is not streaming, so there is no live session to adjust);
    ``SettingsApplyError`` -> 422 naming the failed control while the prior values are
    retained and the session stays active (Req 5.5).
    """
    try:
        accepted = get_broadcaster().apply_settings(camera_id, features)
    except NoActiveSessionError as exc:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=(
                f"Camera {camera_id} is not streaming; subscribe before applying settings. "
                f"({exc})"
            ),
        )
    except SettingsApplyError as exc:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to apply control(s) [{exc.control}] for camera {camera_id}: {exc}",
        )
    return accepted if isinstance(accepted, dict) else {"accepted": accepted}
