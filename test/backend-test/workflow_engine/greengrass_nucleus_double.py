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
"""A Greengrass nucleus double for the IPC ``SubscribeToIoTCore`` transport.

``FakeNucleus`` stands behind ``GreengrassIpcSubscriber``'s ``ipc_connect``
seam and implements the nucleus's (2.12.0 and current main) bookkeeping for
``SubscribeToIoTCore`` streams of ONE component, which is what the trigger
runtime has to tolerate:

- every open stream of a topic filter is counted (``streams``); the first
  stream of a filter is a "real" cloud SUBSCRIBE, every further one is a
  "piggyback" on the already-subscribed filter (``subscribes``);
- when the LAST open stream of a cloud-subscribed filter closes, the cloud
  UNSUBSCRIBE is sent at once (``unsubscribes``, ``unsub_in_flight``) but the
  filter leaves the nucleus's map only on the UNSUBACK — which the test
  delivers explicitly with ``complete_unsubacks()``;
- a ``SubscribeToIoTCore`` for a filter whose UNSUBSCRIBE is in flight is
  acknowledged as a piggyback and does NOT re-add the filter: once the
  UNSUBACK lands the stream is open but receives nothing (the bug);
- ``deliver()`` reaches every open stream of a filter only while the filter
  is cloud-subscribed with no UNSUBSCRIBE in flight.

Also here: ``install_awsiot_stubs(monkeypatch)``, the ``sys.modules`` stubs
for the ``awsiot.greengrasscoreipc`` module family the worker imports lazily
(the same shape ``test_mqtt_transports.py`` installs), so the worker runs
without the SDK or a nucleus socket. Test modules declare their own
``awsiot_stubs`` fixture around it.

This module imports nothing from ``workflow_engine`` so it loads against any
revision of the production code (the reproduction module runs against the
pre-fix revision too).
"""
import sys
import threading
import types
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# awsiot SDK stubs (sys.modules substitutes for the worker's lazy imports)
# ---------------------------------------------------------------------------


class StubQOS:
    AT_MOST_ONCE = "QOS_AT_MOST_ONCE"
    AT_LEAST_ONCE = "QOS_AT_LEAST_ONCE"


class StubSubscribeToIoTCoreRequest:
    def __init__(self):
        self.topic_name = None
        self.qos = None


class StubUnauthorizedError(Exception):
    """Stands in for awsiot...model.UnauthorizedError."""


class StubStreamHandlerBase:
    """Stands in for SubscribeToIoTCoreStreamHandler (plain base class)."""


def install_awsiot_stubs(monkeypatch):
    """Install the awsiot module family into ``sys.modules`` so the worker's
    lazy ``import awsiot.greengrasscoreipc...`` statements resolve to these
    stubs; the ``ipc_connect`` seam supplies the client itself (the stub
    ``connect`` refuses to connect). Returns ``SimpleNamespace(model=…,
    client=…)``."""
    awsiot = types.ModuleType("awsiot")
    ggc = types.ModuleType("awsiot.greengrasscoreipc")
    client_mod = types.ModuleType("awsiot.greengrasscoreipc.client")
    model_mod = types.ModuleType("awsiot.greengrasscoreipc.model")

    model_mod.QOS = StubQOS
    model_mod.SubscribeToIoTCoreRequest = StubSubscribeToIoTCoreRequest
    model_mod.UnauthorizedError = StubUnauthorizedError
    client_mod.SubscribeToIoTCoreStreamHandler = StubStreamHandlerBase

    def refuse_real_connect():  # the ipc_connect seam must be used
        raise AssertionError("tests must inject ipc_connect")

    ggc.connect = refuse_real_connect
    ggc.client = client_mod
    ggc.model = model_mod
    awsiot.greengrasscoreipc = ggc

    for name, module in (
        ("awsiot", awsiot),
        ("awsiot.greengrasscoreipc", ggc),
        ("awsiot.greengrasscoreipc.client", client_mod),
        ("awsiot.greengrasscoreipc.model", model_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(model=model_mod, client=client_mod)


# ---------------------------------------------------------------------------
# The nucleus double
# ---------------------------------------------------------------------------


class FakeFuture:
    """The ``get_response()`` future: honours an armed hold (the straddle
    tests release it by hand), then succeeds or raises the connect's
    planned error."""

    def __init__(self, nucleus: "FakeNucleus", error: Optional[BaseException]):
        self._nucleus = nucleus
        self._error = error

    def result(self, timeout=None):
        hold = self._nucleus._take_hold()
        if hold is not None:
            self._nucleus.holding = True
            try:
                hold.wait(timeout)
            finally:
                self._nucleus.holding = False
        if self._error is not None:
            raise self._error
        return SimpleNamespace()


class FakeOperation:
    """One ``SubscribeToIoTCore`` operation (= one stream) of one client."""

    def __init__(self, nucleus: "FakeNucleus", client: "FakeIpcClient", handler: Any):
        self.nucleus = nucleus
        self.client = client
        self.handler = handler
        self.request = None
        self.topic: Optional[str] = None
        #: ``"real"`` (cloud SUBSCRIBE sent) or ``"piggyback"`` (the filter
        #: was already in the nucleus's map — or its UNSUBSCRIBE in flight).
        self.kind: Optional[str] = None
        self.closed = False

    def activate(self, request):
        self.request = request
        topic = request.topic_name
        self.topic = topic
        nucleus = self.nucleus
        with nucleus._lock:
            if topic in nucleus.cloud_subscribed or topic in nucleus.unsub_in_flight:
                # An in-flight filter is NOT re-added to the map: that is
                # the bug the runtime must tolerate.
                kind = "piggyback"
            else:
                kind = "real"
                nucleus.cloud_subscribed.add(topic)
            self.kind = kind
            nucleus.streams.setdefault(topic, []).append(self)
            nucleus.subscribes.append((topic, kind))
        hook = nucleus.on_subscribed
        if hook is not None:
            hook(topic, self)

    def get_response(self):
        return FakeFuture(self.nucleus, self.client.error)

    def close(self):
        """Idempotent. Closing the last open stream of a cloud-subscribed
        filter sends the UNSUBSCRIBE (one per zero crossing)."""
        nucleus = self.nucleus
        with nucleus._lock:
            if self.closed:
                return
            self.closed = True
            topic = self.topic
            if topic is None:
                return  # never activated
            streams = nucleus.streams.get(topic)
            if streams is None:
                return
            streams[:] = [stream for stream in streams if stream is not self]
            if streams:
                return
            del nucleus.streams[topic]
            if topic in nucleus.cloud_subscribed and topic not in nucleus.unsub_in_flight:
                nucleus.unsub_in_flight.add(topic)
                nucleus.unsubscribes.append(topic)


class FakeIpcClient:
    """One IPC connection. Closing it closes its streams (as the nucleus's
    RpcServer does) and counts the close calls."""

    def __init__(self, nucleus: "FakeNucleus", error: Optional[BaseException] = None):
        self.nucleus = nucleus
        self.error = error
        self.handlers: List[Any] = []
        self.operations: List[FakeOperation] = []
        self.close_calls = 0
        self.closed = False

    def new_subscribe_to_iot_core(self, handler):
        self.handlers.append(handler)
        operation = FakeOperation(self.nucleus, self, handler)
        self.operations.append(operation)
        return operation

    def close(self):
        self.close_calls += 1
        self.closed = True
        for operation in list(self.operations):
            operation.close()


class FakeNucleus:
    """The component's view of the nucleus (see the module docstring).
    ``connect`` is the ``ipc_connect`` seam; ``error_plan`` plans one error
    per connect (``None`` = success), as ``StubIpcConnect`` does."""

    def __init__(self, error_plan=()):
        self._lock = threading.Lock()
        self._error_plan: List[Optional[BaseException]] = list(error_plan)
        self._hold: Optional[threading.Event] = None
        #: The nucleus's ``subscriptionTopics`` view: filters subscribed in
        #: the cloud (an in-flight UNSUBSCRIBE keeps the filter here until
        #: ``complete_unsubacks()``).
        self.cloud_subscribed: Set[str] = set()
        self.unsub_in_flight: Set[str] = set()
        #: Open streams per filter (the nucleus's ``subscriptions``).
        self.streams: Dict[str, List[FakeOperation]] = {}
        #: Every successful activate: ``(topic, "real" | "piggyback")``.
        self.subscribes: List[Tuple[str, str]] = []
        #: Every cloud UNSUBSCRIBE sent, in order.
        self.unsubscribes: List[str] = []
        self.clients: List[FakeIpcClient] = []
        #: Called after every successful activate with ``(topic, operation)``
        #: — e.g. to deliver a message the instant a stream opens.
        self.on_subscribed: Optional[Callable[[str, FakeOperation], None]] = None
        #: True while a ``get_response().result()`` waits on an armed hold.
        self.holding = False

    # -- the ipc_connect seam ---------------------------------------------

    def connect(self) -> FakeIpcClient:
        with self._lock:
            error = self._error_plan.pop(0) if self._error_plan else None
            client = FakeIpcClient(self, error)
            self.clients.append(client)
        return client

    # -- test controls ------------------------------------------------------

    def hold_next_response(self) -> threading.Event:
        """Arm a hold: the next ``get_response().result()`` blocks until the
        returned event is set (``holding`` is True meanwhile)."""
        event = threading.Event()
        with self._lock:
            self._hold = event
        return event

    def _take_hold(self) -> Optional[threading.Event]:
        with self._lock:
            hold = self._hold
            self._hold = None
        return hold

    def complete_unsubacks(self) -> None:
        """The UNSUBACKs land: every filter with an UNSUBSCRIBE in flight
        leaves the nucleus's map."""
        with self._lock:
            self.cloud_subscribed -= self.unsub_in_flight
            self.unsub_in_flight.clear()

    def deliver(self, topic: str, payload: bytes) -> int:
        """A cloud message on ``topic``: reaches every open stream of the
        filter only while the filter is cloud-subscribed with no UNSUBSCRIBE
        in flight. Returns the number of streams it reached."""
        with self._lock:
            if topic not in self.cloud_subscribed or topic in self.unsub_in_flight:
                return 0
            targets = list(self.streams.get(topic, ()))
        event = SimpleNamespace(
            message=SimpleNamespace(topic_name=topic, payload=payload)
        )
        for operation in targets:
            operation.handler.on_stream_event(event)
        return len(targets)

    def lose_stream(self, operation: FakeOperation) -> None:
        """The nucleus closes one stream (connection trouble): the stream
        is gone from the double, then the handler hears about it."""
        operation.close()
        operation.handler.on_stream_error(
            RuntimeError(f"stream for '{operation.topic}' closed by the nucleus")
        )

    def open_streams(self, topic: str) -> int:
        with self._lock:
            return len(self.streams.get(topic, ()))
