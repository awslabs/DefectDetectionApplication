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
"""Stream Image_Source rules (rtsp-rtmp-stream-cameras Requirements 4.1, 4.2).

An ``RTSP`` or ``RTMP`` Image_Source keeps its Stream_URL in ``location``,
its non-secret settings in the configuration row's ``streamSettings`` JSON,
and its Stream_Credentials in the Credential_Store only
(``stream_ingest/credentials.py``). This module owns the value domains and
the validation shared by the API, the accessor and the Edge_Sync_Agent:

- :func:`validate_stream_url` applies the vendored ``check_stream_url`` with
  the type's schemes, so the device accepts exactly the URLs the Portal,
  the catalog and the validator accept;
- :func:`normalize_stream_settings` checks each setting against its domain
  and fills the device defaults, so a stored row always carries every
  setting (the Portal compares against these defaults, camera_sync.py);
- :func:`validate_credentials` checks the write-only credential fields.

Every rejection is a :class:`StreamSourceError` naming the field, and no
message ever echoes a submitted value, so a credential or a credentialed
URL cannot reach a response or a log through a validation error.

The domains are the Portal's (``camera_registry.py``); a device test pins
the two copies equal.
"""
from typing import Any, Dict, Mapping, Optional

from workflow_engine.vendor.workflow_core.stream_url import (
    SCHEMES_BY_SOURCE_TYPE,
    check_stream_url,
)

#: The Image_Source types with a Stream_URL (values of ``ImageSourceType``).
RTSP = "RTSP"
RTMP = "RTMP"
STREAM_SOURCE_TYPE_VALUES = (RTSP, RTMP)

#: Value domains of Requirement 4.1.
TRANSPORTS = ("tcp", "udp", "auto")
DECODER_POLICIES = ("auto", "hardware", "software")
LATENCY_MS_RANGE = (0, 5000)
MAX_FRAME_DIMENSION_RANGE = (320, 4096)
STALL_TIMEOUT_S_RANGE = (2, 60)

#: The device defaults of every setting (Requirement 4.1).
SETTING_DEFAULTS: Dict[str, Any] = {
    "transport": "tcp",
    "latencyMs": 200,
    "decoder": "auto",
    "maxFrameDimension": 1920,
    "stallTimeoutS": 10,
}

#: Settings that only an RTSP camera has.
RTSP_ONLY_SETTINGS = ("transport", "latencyMs")

#: The settings an operator or the Portal may set.
USER_SETTINGS = tuple(SETTING_DEFAULTS)

#: Settings the device manages itself: the Credential_Reference delivered
#: by the Portal and the time the credentials last changed. They are kept
#: across updates and never accepted from the LocalServer API.
MANAGED_SETTINGS = ("credentialRef", "credentialsUpdatedAt")

#: The write-only Stream_Credentials fields: an optional username and
#: password, and an optional URL secret suffix (an RTMP stream key or a
#: secret query string) appended to the Stream_URL only when connecting.
CREDENTIAL_FIELDS = ("username", "password", "urlSecret")

#: Length bound on one credential field, as the Portal enforces it.
MAX_CREDENTIAL_FIELD_LENGTH = 1024

#: What follows ``appsrc`` for a stream frame in a Pipeline_Configuration
#: preview or capture: the Stream_Ingest_Service delivers packed RGB, and
#: ``GstPipelineManager.create_buffer`` reads the appsrc caps from the first
#: ``caps=`` of the pipeline string.
STREAM_FRAME_PIPELINE = "capsfilter caps=video/x-raw,format=RGB ! videoconvert"

#: Requirement 4.8: stream cameras cannot feed a classic Pipeline_Configuration
#: workflow or a digital-input capture.
CLASSIC_PIPELINE_REJECTION = (
    "Stream cameras (RTSP and RTMP) are supported in deployed workflows, "
    "live preview, and image capture. They cannot be used by a classic "
    "workflow or a digital-input capture."
)


class StreamSourceError(ValueError):
    """A rejected stream Image_Source field. ``field`` names it (for
    example ``location`` or ``streamSettings.latencyMs``), and ``message``
    never contains a submitted value."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message

    def as_messages(self) -> Dict[str, list]:
        """The error in marshmallow's ``{field: [message]}`` shape."""
        return {self.field: [self.message]}


def is_stream_source_type(source_type: Any) -> bool:
    """Whether ``source_type`` (an ``ImageSourceType`` member or its value)
    is ``RTSP`` or ``RTMP``. Total: anything else is not."""
    value = getattr(source_type, "value", source_type)
    return isinstance(value, str) and value in STREAM_SOURCE_TYPE_VALUES


def _type_value(source_type: Any) -> str:
    return str(getattr(source_type, "value", source_type))


def validate_stream_url(source_type: Any, url: Any) -> str:
    """The Stream_URL of a stream Image_Source, or a ``location`` error.

    Applies ``check_stream_url`` with the type's accepted schemes, which
    rejects a missing or malformed URL, the other protocol's scheme,
    embedded user information and a Secret_Query_Parameter, each with a
    message stating what to fix and never echoing the value (Requirement
    4.2). Returns the URL stripped of surrounding whitespace.
    """
    schemes = SCHEMES_BY_SOURCE_TYPE.get(_type_value(source_type), ())
    problem = check_stream_url(url, schemes)
    if problem is not None:
        raise StreamSourceError("location", problem.message)
    return url.strip()


def _integer_setting(name: str, value: Any, bounds) -> int:
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, int):
        raise StreamSourceError(
            f"streamSettings.{name}", f"must be a whole number from {low} to {high}")
    if not low <= value <= high:
        raise StreamSourceError(
            f"streamSettings.{name}", f"must be from {low} to {high}")
    return value


def _enum_setting(name: str, value: Any, values) -> str:
    if value not in values:
        raise StreamSourceError(
            f"streamSettings.{name}", "must be one of " + ", ".join(values))
    return value


def normalize_stream_settings(source_type: Any, settings: Optional[Mapping[str, Any]],
                              base: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The complete ``streamSettings`` of a stream Image_Source.

    ``settings`` holds the values the request sets; ``base`` is the stored
    settings of an existing source, whose values (managed keys included)
    carry over where ``settings`` is silent. A setting that is ``None`` in
    ``settings`` returns to its default. The result has every user setting
    for the type, with defaults filled, plus the managed keys ``base``
    carried. An RTMP source never has the RTSP-only settings.

    Raises :class:`StreamSourceError` for an unknown or managed key, or a
    value outside its domain.
    """
    rtsp = _type_value(source_type) == RTSP
    allowed = USER_SETTINGS if rtsp else tuple(
        name for name in USER_SETTINGS if name not in RTSP_ONLY_SETTINGS)
    provided = dict(settings or {})
    for name in provided:
        if name in MANAGED_SETTINGS:
            raise StreamSourceError(f"streamSettings.{name}", "is managed by the device and cannot be set")
        if name not in allowed:
            detail = ("applies to RTSP cameras only" if name in RTSP_ONLY_SETTINGS
                      else "is not a stream camera setting")
            raise StreamSourceError(f"streamSettings.{name}", detail)

    result: Dict[str, Any] = {}
    stored = dict(base or {})
    for name in allowed:
        if name in provided:
            value = provided[name]
        else:
            value = stored.get(name)
        if value is None:
            value = SETTING_DEFAULTS[name]
        if name == "transport":
            value = _enum_setting(name, value, TRANSPORTS)
        elif name == "decoder":
            value = _enum_setting(name, value, DECODER_POLICIES)
        elif name == "latencyMs":
            value = _integer_setting(name, value, LATENCY_MS_RANGE)
        elif name == "maxFrameDimension":
            value = _integer_setting(name, value, MAX_FRAME_DIMENSION_RANGE)
        else:
            value = _integer_setting(name, value, STALL_TIMEOUT_S_RANGE)
        result[name] = value
    for name in MANAGED_SETTINGS:
        if stored.get(name) is not None:
            result[name] = stored[name]
    return result


def validate_credentials(credentials: Any) -> Dict[str, str]:
    """The non-empty write-only credential fields of a request.

    ``credentials`` must be an object whose keys are among
    :data:`CREDENTIAL_FIELDS` and whose values are strings of at most
    :data:`MAX_CREDENTIAL_FIELD_LENGTH` characters. Empty strings and
    ``None`` are dropped, so a form that leaves a field blank sends
    nothing for it. Messages name the field only.
    """
    if credentials is None:
        return {}
    if not isinstance(credentials, Mapping):
        raise StreamSourceError("credentials", "must be an object with username, password or urlSecret")
    result: Dict[str, str] = {}
    for name, value in credentials.items():
        if name not in CREDENTIAL_FIELDS:
            raise StreamSourceError("credentials", "accepts only username, password and urlSecret")
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise StreamSourceError(f"credentials.{name}", "must be a string")
        if len(value) > MAX_CREDENTIAL_FIELD_LENGTH:
            raise StreamSourceError(
                f"credentials.{name}", f"must be at most {MAX_CREDENTIAL_FIELD_LENGTH} characters")
        if any(ord(character) < 0x20 for character in value):
            raise StreamSourceError(f"credentials.{name}", "must not contain control characters")
        result[name] = value
    return result
