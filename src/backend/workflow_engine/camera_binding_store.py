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
"""CameraBindingStore: cached access to the ``dda-camera-bindings``
named shadow (camera-registry-sync Requirements 10.2, 10.4, 11.1).

The Portal's Deployment_Service writes Camera_Bindings for each deployed
workflow version into the per-thing shadow's desired state:

    { "desired": { "bindings": {
        "{workflowId}/{version}": { "{nodeId}": binding, ... }, ... } } }

The store reads the shadow through the existing ``IoTShadowAccessor``
(device IoT identity, Requirement 12.4), caches the whole ``bindings``
map, and is invalidated on shadow delta so the next read refetches. Read
outcomes are three-valued, and the distinction matters to the watcher:

- a readable shadow yields the bindings map (possibly empty);
- a shadow that does not exist yet (no deployment ever delivered
  bindings to this device) counts as readable with **no** bindings —
  documents register exactly as today (10.5, 11.1);
- an unreadable shadow (transport/IPC error) yields ``None`` — the
  watcher marks documents *with* binding points invalid with reason
  ``bindings unavailable`` while legacy documents without binding
  points register as today (11.1). Failures are never cached, so the
  next scan retries and recovery flips registrations back (10.4).
"""
import logging
import os
import threading
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: The named shadow carrying Camera_Bindings (design: Camera_Binding
#: delivery decision).
BINDINGS_SHADOW_NAME = "dda-camera-bindings"


def binding_key(workflow_id: str, version: str) -> str:
    """The ``desired.bindings`` key for one deployed workflow version."""
    return "{}/{}".format(workflow_id, version)


def bindings_delta_topic_prefix(
    thing_name: str, shadow_name: str = BINDINGS_SHADOW_NAME
) -> str:
    """The shadow update topic prefix for the MQTT ``SubscriptionHandler``
    (its ``#`` wildcard covers the ``delta`` subtopic)."""
    return "$aws/things/{}/shadow/name/{}/update/".format(thing_name, shadow_name)


class CameraBindingStore:
    """Cached reader of the ``dda-camera-bindings`` shadow.

    ``iot_shadow_accessor`` is the existing ``IoTShadowAccessor`` (or a
    fake exposing ``get_thing_shadow_state_request``). Its contract:
    a state dict on success, ``False`` when the shadow does not exist,
    and ``None`` (or an exception) on transport errors.

    Thread-safe: the watcher thread reads while the MQTT delta handler
    invalidates.
    """

    def __init__(
        self,
        iot_shadow_accessor,
        thing_name: Optional[str] = None,
        shadow_name: str = BINDINGS_SHADOW_NAME,
    ) -> None:
        self._shadow = iot_shadow_accessor
        self.thing_name = (
            thing_name
            if thing_name is not None
            else os.environ.get("AWS_IOT_THING_NAME", "")
        )
        self.shadow_name = shadow_name
        self._lock = threading.Lock()
        self._cached = False
        self._bindings: Dict[str, Any] = {}

    def bindings_for(
        self, workflow_id: str, version: str
    ) -> Optional[Dict[str, Any]]:
        """The bindings map for ``{workflow_id}/{version}``.

        Returns ``{}`` when the shadow is readable but carries no
        bindings for this version, and ``None`` when the bindings shadow
        is unreadable (the ``bindings unavailable`` case, 10.2/11.1).
        """
        bindings = self._load()
        if bindings is None:
            return None
        entry = bindings.get(binding_key(workflow_id, version))
        return dict(entry) if isinstance(entry, Mapping) else {}

    def invalidate(self) -> None:
        """Drop the cache; the next read refetches the shadow (delta
        refresh, 10.4)."""
        with self._lock:
            self._cached = False

    def on_delta(self, message: Optional[Mapping[str, Any]] = None) -> None:
        """Shadow delta notification: refresh on next read."""
        self.invalidate()

    # ------------------------------------------------------------------

    def _load(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._cached:
                return dict(self._bindings)
        fetched = self._fetch()
        if fetched is None:
            # Never cache a failure: the next scan retries (10.4).
            return None
        with self._lock:
            self._cached = True
            self._bindings = fetched
            return dict(self._bindings)

    def _fetch(self) -> Optional[Dict[str, Any]]:
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception:  # noqa: BLE001 - transport errors => unavailable
            logger.exception("Could not read the camera-bindings shadow")
            return None
        if state is False:
            # Shadow does not exist: no bindings were ever delivered.
            return {}
        if not isinstance(state, Mapping):
            # The accessor swallowed a transport error and returned None.
            return None
        desired = state.get("desired")
        bindings = desired.get("bindings") if isinstance(desired, Mapping) else None
        return dict(bindings) if isinstance(bindings, Mapping) else {}


# --- delta subscription (SubscriptionHandler pattern) -------------------------


def make_bindings_shadow_handler(watcher):
    """A ``SubscribeToIoTCoreStreamHandler`` for the bindings shadow,
    following the existing ``SubscriptionHandler`` pattern (see
    ``camera_sync.agent.make_shadow_stream_handler``): a ``delta``
    notification invalidates the store's cache and re-resolves invalid
    registrations through ``watcher.on_bindings_delta`` (10.4).

    The awsiot import is deferred so this module stays importable without
    the Greengrass IPC runtime (tests use fakes).
    """
    import awsiot.greengrasscoreipc.client as client

    from dao.iotshadow.ShadowUtils import decode_shadow_payload, remove_prefix

    store = watcher.binding_store
    prefix = bindings_delta_topic_prefix(store.thing_name, store.shadow_name)

    class _CameraBindingsShadowHandler(client.SubscribeToIoTCoreStreamHandler):
        def on_stream_event(self, event) -> None:
            try:
                topic_name = event.message.topic_name
                subtopic = remove_prefix(topic_name, prefix)
                if subtopic == "delta":
                    message = decode_shadow_payload(event.message.payload)
                    watcher.on_bindings_delta(message)
                # accepted/documents notifications need no edge-side action
            except Exception:  # noqa: BLE001 - handler isolation (11.2)
                logger.exception("Error handling camera-bindings shadow message")

        def on_stream_error(self, error: Exception) -> bool:
            logger.error("Camera-bindings shadow stream error: %s", error)
            return True  # close the stream; the wiring layer resubscribes

        def on_stream_closed(self) -> None:
            logger.info("Camera-bindings shadow stream closed")

    return _CameraBindingsShadowHandler()
