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
"""What a Stream_Session connects to (rtsp-rtmp-stream-cameras design
component 11).

A configured camera (``cfg-<imageSourceId>``) is read from the database and
the Credential_Store every time its worker starts, so a changed URL,
setting or credential is always the one used. An anonymous stream
(``url-<hash>``) carries its URL and settings with the lease and has no
credentials.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from model import stream_source
from stream_ingest import health
from stream_ingest.health import StreamError


@dataclass(frozen=True)
class StreamSource:
    """``protocol`` is ``rtsp`` or ``rtmp``; ``settings`` are complete
    Stream_Settings; ``credentials`` hold ``username``/``password``/``urlSecret``
    and are never logged or returned."""

    protocol: str
    url: str
    settings: Dict[str, Any]
    credentials: Dict[str, str] = field(default_factory=dict, repr=False)

    def secret_values(self) -> List[str]:
        return [value for value in self.credentials.values() if value]


def protocol_for_type(source_type: Any) -> str:
    """``rtsp`` or ``rtmp`` for an Image_Source type."""
    value = str(getattr(source_type, "value", source_type) or "").upper()
    if value not in stream_source.STREAM_SOURCE_TYPE_VALUES:
        raise ValueError(f"{value or 'this'} is not a stream Image_Source type")
    return value.lower()


def anonymous_source(source_type: Any, url: str, settings: Optional[Mapping[str, Any]] = None) -> StreamSource:
    """A stream named by URL alone (a workflow node without a configured
    camera): validated like a configured one, and without credentials."""
    try:
        clean_url = stream_source.validate_stream_url(source_type, url)
        complete = stream_source.normalize_stream_settings(source_type, settings or {})
    except stream_source.StreamSourceError as error:
        raise StreamError(health.NOT_FOUND, error.message) from None
    return StreamSource(protocol_for_type(source_type), clean_url, complete)


def configured_source(image_source_id: str, session_factory=None, credential_store=None) -> StreamSource:
    """The current URL, settings and credentials of a stream Image_Source.

    Raises ``StreamError(not_found)`` when the Image_Source is gone or is
    not a stream camera, a configuration-class failure that retries when
    the configuration changes.
    """
    from dao.sqlite_db import image_source_dao
    from stream_ingest.credentials import get_credential_store

    if session_factory is None:
        from dao.sqlite_db.sqlite_db_operations import SessionLocal as session_factory
    with session_factory() as db:
        image_source = image_source_dao.get_image_source(db, image_source_id)
        if image_source is None or not stream_source.is_stream_source_type(image_source.type):
            raise StreamError(health.NOT_FOUND, "the stream camera is no longer configured on this device")
        configuration = image_source.imageSourceConfiguration
        stored = dict(getattr(configuration, "streamSettings", None) or {})
        source_type, url = image_source.type, image_source.location
    try:
        settings = stream_source.normalize_stream_settings(
            source_type, {}, base={key: value for key, value in stored.items()
                                   if key not in stream_source.MANAGED_SETTINGS})
    except stream_source.StreamSourceError as error:
        raise StreamError(health.NOT_FOUND, f"the stored camera settings are invalid: {error.message}") from None
    store = credential_store or get_credential_store()
    credentials = store.get(image_source_id) or {}
    return StreamSource(protocol_for_type(source_type), url, settings, credentials)
