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
"""Stream session states, failure categories and Stream_Health
(rtsp-rtmp-stream-cameras design component 11, "Stream_Health").

Every failure a Stream_Worker or a Stream_Session reports carries one
category. The category decides the retry schedule:

- transient categories (Requirement 8.5) retry with the 1-30 s backoff;
- configuration categories (Requirement 8.6) retry every 5 minutes, or at
  once when the camera's configuration changes;
- ``hardware_decoder_failed`` restarts the worker at once with the software
  decoder, under the ``auto`` Decoder_Policy (Requirement 7.5);
- ``session_limit`` refuses a lease (Requirement 8.10).

Messages are redacted and bounded before they are stored, so Stream_Health
never carries a credential (Requirement 6.1).
"""
from typing import Any, Dict, Iterable, Optional

from workflow_engine.vendor.workflow_core.stream_url import redact

# -- session states ----------------------------------------------------------

CONNECTING = "connecting"
STREAMING = "streaming"
RECONNECTING = "reconnecting"
FAILED = "failed"
STOPPED = "stopped"
STATES = (CONNECTING, STREAMING, RECONNECTING, FAILED, STOPPED)

# -- failure categories -----------------------------------------------------

NETWORK_ERROR = "network_error"
TIMEOUT = "timeout"
SERVER_ERROR = "server_error"
STALL = "stall"
WORKER_EXIT = "worker_exit"
#: Retried with the transient backoff (Requirement 8.5).
TRANSIENT_CATEGORIES = (NETWORK_ERROR, TIMEOUT, SERVER_ERROR, STALL, WORKER_EXIT)

AUTHENTICATION_FAILED = "authentication_failed"
NOT_FOUND = "not_found"
UNSUPPORTED_CODEC = "unsupported_codec"
DECODER_UNAVAILABLE = "decoder_unavailable"
TLS_VERIFICATION_FAILED = "tls_verification_failed"
#: Retried every 5 minutes or on a configuration change (Requirement 8.6).
CONFIGURATION_CATEGORIES = (
    AUTHENTICATION_FAILED,
    NOT_FOUND,
    UNSUPPORTED_CODEC,
    DECODER_UNAVAILABLE,
    TLS_VERIFICATION_FAILED,
)

#: A hardware decoder failed to start or failed mid-session.
HARDWARE_DECODER_FAILED = "hardware_decoder_failed"
#: A lease was refused at the device session limit.
SESSION_LIMIT = "session_limit"

ALL_CATEGORIES = TRANSIENT_CATEGORIES + CONFIGURATION_CATEGORIES + (
    HARDWARE_DECODER_FAILED, SESSION_LIMIT)

#: Coarse health, as the Edge_Sync_Agent reports it (Requirement 4.5).
COARSE_STREAMING = "streaming"
COARSE_RECONNECTING = "reconnecting"
COARSE_FAILED = "failed"
COARSE_IDLE = "idle"

#: Upper bound on a stored failure message.
MAX_MESSAGE_LENGTH = 300


def is_configuration_category(category: Any) -> bool:
    """Whether ``category`` is a configuration-class failure."""
    return category in CONFIGURATION_CATEGORIES


def normalize_category(category: Any) -> str:
    """``category`` when it is known, else ``network_error``: an unknown
    category from a worker is treated as transient, never as a reason to
    stop retrying."""
    return category if category in ALL_CATEGORIES else NETWORK_ERROR


def clean_message(message: Any, secrets: Iterable[str] = ()) -> str:
    """``message`` as a single redacted line of bounded length.

    URL user information, Secret_Query_Parameter values and each of
    ``secrets`` are masked, control characters become spaces, and the
    result is cut at :data:`MAX_MESSAGE_LENGTH` characters."""
    text = redact(str(message if message is not None else ""), list(secrets or ()))
    text = "".join(character if character >= " " else " " for character in text).strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return text


class StreamError(Exception):
    """A categorized stream failure. ``message`` never holds a secret."""

    def __init__(self, category: str, message: str):
        super().__init__(f"{category}: {message}")
        self.category = normalize_category(category)
        self.message = message


def coarse_health(health: Optional[Dict[str, Any]]) -> str:
    """The coarse health of a camera: ``streaming``, ``reconnecting``
    (connecting counts), ``failed``, or ``idle`` (no session)."""
    state = (health or {}).get("state")
    if state == STREAMING:
        return COARSE_STREAMING
    if state in (CONNECTING, RECONNECTING):
        return COARSE_RECONNECTING
    if state == FAILED:
        return COARSE_FAILED
    return COARSE_IDLE


def empty_health(camera_key: str, state: str = STOPPED) -> Dict[str, Any]:
    """A Stream_Health document with nothing known yet."""
    return {
        "cameraKey": camera_key,
        "state": state,
        "codec": None,
        "width": None,
        "height": None,
        "sourceFps": None,
        "decoder": None,
        "decoderFallback": False,
        "reconnects": 0,
        "lastFrameAtMs": None,
        "lastError": None,
        "leases": 0,
    }
