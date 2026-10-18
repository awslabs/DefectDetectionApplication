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

"""The named-shadow control reader (dashboards design §5.2 ``control_shadow.py``).

:class:`ShadowControlAgent` subscribes, through ``mqtt.SubscriptionHandler``,
to ``$aws/things/{thing}/shadow/name/{shadow}/#`` and delivers **complete**
``desired`` documents to ``on_desired(desired, version)``:

- ``get/accepted`` carries ``state.desired`` and ``version``;
- ``update/documents`` carries ``current.state.desired`` and
  ``current.version``;
- ``update/delta`` is never applied as state (it lists only the fields that
  differ from ``reported``, so a key the Portal removed never appears in
  it); it only triggers one fresh, empty ``.../get``, coalesced while one is
  in flight;
- the agent's own request echoes (``.../get``, ``.../update``) and the
  ``update/accepted``, ``update/rejected`` and ``get/rejected`` answers are
  logged at DEBUG and change nothing.

Documents are applied in version order (a lower or equal version is
ignored), so the agent's own ``reported`` writes, which come back as
``update/documents`` with an unchanged ``desired`` and a higher version, are
harmless no-ops for an idempotent ``on_desired``.

``report(state)`` writes ``{"state": {"reported": ...}}`` (at most 4 KB) when
a non-counter field changed since the last write and otherwise at most once
per 5 minutes. No ShadowManager dependency: the one IPC subscription and the
``get``/``update`` publishes are all the agent needs.
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["ShadowControlAgent"]

#: ``reported`` writes with only counter changes happen at most this often.
REPORT_INTERVAL_S = 300.0
#: A ``reported`` document larger than this is not written (the shadow
#: document limit is 8 KB and the Portal reads ``desired`` and ``reported``
#: together).
REPORT_MAX_BYTES = 4096
#: Keys whose values ride along in ``reported`` without triggering a write.
DEFAULT_COUNTER_KEYS = ("counters", "lastPublishAt")
#: A ``get`` requested by a delta is considered in flight for this long; a
#: later delta publishes a new one when the answer never came.
GET_IN_FLIGHT_S = 10.0

_IGNORED_SUBTOPICS = ("get", "update", "update/accepted", "update/rejected", "get/rejected")


class ShadowControlAgent:
    """See the module docstring.

    ``on_desired(desired: dict, version: int)`` receives the complete
    ``desired`` map of every applied document; ``publisher`` is the agent's
    ``IpcPublisher`` (its ``publish_message(topic, payload)`` publishes the
    ``get`` and the ``reported`` writes at QoS 0). ``subscription_factory``
    and ``clock`` are injectable for tests; the default factory builds the
    real ``mqtt.SubscriptionHandler`` lazily, so this module imports without
    the Greengrass SDK.
    """

    def __init__(
        self,
        thing_name: str,
        shadow_name: str,
        on_desired: Callable[[dict, int], None],
        publisher: Any,
        *,
        subscription_factory: Optional[Callable] = None,
        clock: Callable[[], float] = time.monotonic,
        counter_keys: Sequence[str] = DEFAULT_COUNTER_KEYS,
    ) -> None:
        self.thing_name = str(thing_name)
        self.shadow_name = str(shadow_name)
        self.topic_prefix = "$aws/things/{0}/shadow/name/{1}/".format(
            self.thing_name, self.shadow_name
        )
        self._on_desired = on_desired
        self._publisher = publisher
        self._subscription_factory = subscription_factory
        self._clock = clock
        self._counter_keys = tuple(counter_keys)
        self._lock = threading.Lock()
        self._last_version: Optional[int] = None
        self._get_requested_at: Optional[float] = None
        self._last_reported_stripped: Optional[Any] = None
        self._last_report_at: Optional[float] = None
        self._subscription = None
        self._thread: Optional[threading.Thread] = None
        self.applied_documents = 0
        self.report_writes = 0
        self.report_skipped_too_large = 0

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        """Build the subscription and supervise it on a daemon thread.

        ``SubscriptionHandler.subscribe()`` blocks for the life of the
        process (it re-activates after every loss), so it gets its own
        thread. The subscription publishes an empty ``get`` at every
        activation and runs :meth:`on_active` as its catch-up."""
        factory = self._subscription_factory or self._default_subscription
        self._subscription = factory(
            self.topic_prefix, self._stream_handler(), self._publisher,
            on_active=self.on_active,
        )

        def _subscribe():
            try:
                self._subscription.subscribe()
            except Exception:  # noqa: BLE001 - the agent stands without it
                logger.exception(
                    "Shadow %s: the control subscription failed; desired "
                    "changes will not be applied until the next restart",
                    self.shadow_name,
                )

        self._thread = threading.Thread(
            target=_subscribe, name="{0}-shadow-subscription".format(self.shadow_name),
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        subscription = self._subscription
        if subscription is not None:
            try:
                subscription.close()
            except Exception:  # noqa: BLE001 - best-effort
                logger.debug("Shadow %s: close failed", self.shadow_name, exc_info=True)

    @staticmethod
    def _default_subscription(topic_prefix, handler, publish_handler, *, on_active=None):
        from mqtt.SubscriptionHandler import SubscriptionHandler

        return SubscriptionHandler(topic_prefix, handler, publish_handler, on_active=on_active)

    def _stream_handler(self):
        """A ``SubscribeToIoTCoreStreamHandler`` forwarding every event to
        :meth:`handle_message`; built against the SDK base class when it is
        importable, else a plain object (tests)."""
        try:
            from awsiot.greengrasscoreipc.client import SubscribeToIoTCoreStreamHandler as base
        except Exception:  # noqa: BLE001 - no SDK on this host
            base = object
        agent = self

        class _Handler(base):  # type: ignore[misc,valid-type]
            def on_stream_event(self, event) -> None:
                try:
                    message = event.message
                    agent.handle_message(message.topic_name, message.payload)
                except Exception:  # noqa: BLE001 - handler isolation
                    logger.exception("Shadow %s: error handling a message", agent.shadow_name)

            def on_stream_error(self, error) -> bool:
                logger.error("Shadow %s: stream error: %s", agent.shadow_name, error)
                return True  # close the stream; the SubscriptionHandler re-activates

            def on_stream_closed(self) -> None:
                logger.info("Shadow %s: stream closed", agent.shadow_name)

        return _Handler()

    # -- inbound ------------------------------------------------------------------

    def on_active(self) -> bool:
        """The subscription's catch-up: request the complete document."""
        self._publish_get()
        return True

    def handle_message(self, topic: str, payload: Any) -> None:
        """Dispatch one shadow message by its sub-topic (any thread)."""
        if not isinstance(topic, str) or not topic.startswith(self.topic_prefix):
            logger.debug("Shadow %s: ignoring a message on %s", self.shadow_name, topic)
            return
        subtopic = topic[len(self.topic_prefix):]
        if subtopic == "get/accepted":
            with self._lock:
                self._get_requested_at = None
            document = _decode(payload)
            if document is None:
                logger.warning("Shadow %s: get/accepted carried no JSON object", self.shadow_name)
                return
            state = document.get("state") if isinstance(document.get("state"), dict) else {}
            self._apply(state.get("desired"), document.get("version"), "get/accepted")
        elif subtopic == "update/documents":
            document = _decode(payload)
            if document is None:
                logger.warning("Shadow %s: update/documents carried no JSON object",
                               self.shadow_name)
                return
            current = document.get("current") if isinstance(document.get("current"), dict) else {}
            state = current.get("state") if isinstance(current.get("state"), dict) else {}
            self._apply(state.get("desired"), current.get("version"), "update/documents")
        elif subtopic == "update/delta":
            logger.debug("Shadow %s: delta received; requesting the complete document",
                         self.shadow_name)
            self._request_get_coalesced()
        elif subtopic == "get/rejected":
            with self._lock:
                self._get_requested_at = None
            logger.debug("Shadow %s: get rejected (%s); nothing applied",
                         self.shadow_name, _summary(payload))
        elif subtopic in _IGNORED_SUBTOPICS:
            logger.debug("Shadow %s: ignoring %s", self.shadow_name, subtopic)
        else:
            logger.debug("Shadow %s: ignoring unknown sub-topic %s", self.shadow_name, subtopic)

    def _apply(self, desired: Any, version: Any, source: str) -> None:
        if not isinstance(version, int) or isinstance(version, bool):
            logger.debug("Shadow %s: %s without an integer version; ignored",
                         self.shadow_name, source)
            return
        with self._lock:
            if self._last_version is not None and version <= self._last_version:
                logger.debug("Shadow %s: %s version %s not newer than %s; ignored",
                             self.shadow_name, source, version, self._last_version)
                return
            self._last_version = version
            self.applied_documents += 1
        desired_map = desired if isinstance(desired, dict) else {}
        try:
            self._on_desired(desired_map, version)
        except Exception:  # noqa: BLE001 - the agent's handler is contained
            logger.exception("Shadow %s: on_desired failed for version %s",
                             self.shadow_name, version)

    # -- outbound ------------------------------------------------------------------

    def _publish_get(self) -> None:
        with self._lock:
            self._get_requested_at = self._clock()
        try:
            self._publisher.publish_message(self.topic_prefix + "get", "")
        except Exception:  # noqa: BLE001 - the subscription's own get covers it
            logger.warning("Shadow %s: could not request the document", self.shadow_name,
                           exc_info=True)

    def _request_get_coalesced(self) -> None:
        with self._lock:
            requested_at = self._get_requested_at
            now = self._clock()
            if requested_at is not None and now - requested_at < GET_IN_FLIGHT_S:
                return
        self._publish_get()

    def report(self, state: Dict[str, Any]) -> bool:
        """Write ``{"state": {"reported": state}}`` when a non-counter field
        changed since the last write, else at most once per
        :data:`REPORT_INTERVAL_S`. Returns whether a write happened."""
        if not isinstance(state, dict):
            return False
        stripped = _strip(state, self._counter_keys)
        now = self._clock()
        with self._lock:
            changed = stripped != self._last_reported_stripped
            due = (self._last_report_at is None
                   or now - self._last_report_at >= REPORT_INTERVAL_S)
            if not changed and not due:
                return False
        payload = json.dumps({"state": {"reported": state}}, separators=(",", ":"),
                             default=str)
        if len(payload.encode("utf-8")) > REPORT_MAX_BYTES:
            with self._lock:
                self.report_skipped_too_large += 1
            logger.warning("Shadow %s: reported document is %d bytes (> %d); not written",
                           self.shadow_name, len(payload), REPORT_MAX_BYTES)
            return False
        try:
            self._publisher.publish_message(self.topic_prefix + "update", payload)
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning("Shadow %s: could not write reported", self.shadow_name,
                           exc_info=True)
            return False
        with self._lock:
            self._last_reported_stripped = stripped
            self._last_report_at = now
            self.report_writes += 1
        return True

    @property
    def last_version(self) -> Optional[int]:
        with self._lock:
            return self._last_version


def _decode(payload: Any) -> Optional[dict]:
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload).decode("utf-8")
        if isinstance(payload, str):
            if not payload.strip():
                return None
            payload = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _summary(payload: Any) -> str:
    try:
        text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
    except Exception:  # noqa: BLE001
        return "<unprintable>"
    return text[:200]


def _strip(value: Any, counter_keys: Sequence[str]) -> Any:
    """``value`` without the counter keys at any depth, for change detection."""
    if isinstance(value, dict):
        return {k: _strip(v, counter_keys) for k, v in value.items() if k not in counter_keys}
    if isinstance(value, (list, tuple)):
        return [_strip(v, counter_keys) for v in value]
    return value
