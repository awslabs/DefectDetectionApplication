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
"""Bug conditions for finding 24: a lost Greengrass IPC connection
(rtsp-rtmp-stream-cameras task 30.4; Requirement 5.14, AC 5 and 6).

On thor1 the shared IPC connection closed twice. The unfixed backend has no
way back: the import-time holders keep the closed client, nothing reconnects
it, and ``subscribe()`` subscribes once and then sleeps for ever. Fix 12's
publish retry, meanwhile, resets the shared client on any error, a timeout
included. Each test FAILS on the unfixed tree (``ac74c0b``) on its assertion:

- ``test_f24_holders_reach_a_new_connection_after_a_close``: after the
  connection closes, ``IoTShadowAccessor``, ``PublishHandler``,
  ``DefectDetectionConfig`` and the ShadowManager size-limit provider, built on
  ``get_ipc_client()`` as ``server_setup`` builds them, still reach client 1.
- ``test_f24_closed_stream_is_subscribed_again``: a stream error and its
  close are never followed by a new subscription.
- ``test_f24_subscription_moves_to_the_new_connection``: a subscription on a
  closed connection never moves to a new one.
- ``test_f24_reactivation_completes_while_the_old_stream_closes``: no
  re-activation starts at all. Fixed, the re-activation completes within
  200 ms while the old stream's ``on_stream_closed`` arrives on the event-loop
  thread.
- ``test_f24_publish_timeout_neither_reconnects_nor_retries`` (R5): a publish
  timeout reconnects and publishes twice.

The fake IPC model (plan P4): each fake connection has one fake event-loop
thread that resolves every response and delivers every stream event, stream
error and stream close (``eventstreamrpc.py:741`` and ``:753``). A connection
can close: every ``new_*`` then raises the real
``awsiot.eventstreamrpc.ConnectionClosedError``, its open streams get
``on_stream_closed`` on the loop thread, then its lifecycle handler
``on_disconnect`` (``:286``), and ``_connection._synced.state.name`` reads
``"DISCONNECTED"``. A fake connection factory stands in for the fixed tree's
``utils.ipc_client._open_connection(lifecycle)``.

Self-contained, so the same file runs on the base and on the worktree
(plan P3): a ``utils.server_setup`` stub and a fresh import of
``mqtt.SubscriptionHandler`` reach the unfixed handler; ``reset_ipc_client()``
and a patched ``awsiot.greengrasscoreipc.connect`` (taking ``**kwargs``) reach
the fixed one. The fixed tree's timing constants and ``_open_connection`` are
patched with ``raising=False``, so on the unfixed tree they are simply unused.
"""
import concurrent.futures
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import types
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

import awsiot.greengrasscoreipc  # noqa: E402
from awsiot.eventstreamrpc import ConnectionClosedError, StreamClosedError  # noqa: E402
from awsiot.greengrasscoreipc.client import SubscribeToIoTCoreStreamHandler  # noqa: E402

import utils as utils_package  # noqa: E402
from utils import ipc_client as shared_ipc  # noqa: E402
from camera_sync import shadow_manager_size_limit_provider  # noqa: E402
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor  # noqa: E402
from mqtt.PublishHandler import PublishHandler  # noqa: E402
from workflow_engine import output_bindings  # noqa: E402

logger = logging.getLogger(__name__)

THING = "thing-f2325"
SHADOW = "dda-camera-registry"
PREFIX = "$aws/things/{}/shadow/name/{}/update/".format(THING, SHADOW)
GET_TOPIC = PREFIX + "get"
GET_STATE = {"desired": {}, "reported": {"cameras": {}}}
SIZE_LIMIT = 8192

#: The fixed tree's timing constants, in milliseconds (plan item 7).
IPC_TIMINGS = (("CONNECT_TIMEOUT_S", 0.5), ("WATCHDOG_INTERVAL_S", 0.02),
               ("RECONNECT_BACKOFF_S", (0.01, 0.02, 0.04, 0.08, 0.1)), ("RETRY_WAIT_S", 0.5))
SUBSCRIPTION_TIMINGS = (("FIRST_RETRY_DELAY_S", 0.01),
                        ("SUBSCRIBE_BACKOFF_S", (0.01, 0.02, 0.04, 0.08, 0.1)), ("SLEEP_TIME", 0.05))


def _done_future(result=None):
    future = Future()
    future.set_result(result)
    return future


# --- the fake Greengrass IPC model (plan P4) ----------------------------------------------


class FakeOperation:
    """One ``new_*`` operation. ``activate`` queues the response on its
    client's event-loop thread; a subscription's stream is open once that
    response is delivered."""

    def __init__(self, client, kind, stream_handler=None):
        self.client = client
        self.kind = kind
        self.stream_handler = stream_handler
        self.request = None
        self.activated_at = None
        self.responded_at = None
        self.stream_open = False
        self._close_delivered = False
        self._response = Future()

    def activate(self, request):
        self.request = request
        self.activated_at = time.monotonic()
        if self.stream_handler is not None:
            # A close the test deferred lands during this activation, before
            # its response, on the loop thread.
            for old in self.client.take_deferred_closes():
                self.client.submit(old.deliver_stream_closed)
        self.client.submit(self._respond)
        return _done_future()

    def _respond(self):
        outcome = StreamClosedError() if self.client.closed else self.client.world.outcome_for(self)
        if isinstance(outcome, BaseException):
            self._response.set_exception(outcome)
            return
        if self.stream_handler is not None:
            self.stream_open = True
        self.responded_at = time.monotonic()
        self._response.set_result(outcome)

    def get_response(self):
        return self._response

    def close(self):
        """Close the operation (never the client): a stream's
        ``on_stream_closed`` follows on the loop thread."""
        if self.stream_handler is not None:
            self.client.submit(self.deliver_stream_closed)
        return _done_future()

    def deliver_stream_closed(self):
        """On the loop thread, as ``_on_continuation_closed`` does."""
        if self._close_delivered:
            return
        self._close_delivered = True
        self.stream_open = False
        if not self._response.done():
            self._response.set_exception(StreamClosedError())
        try:
            self.stream_handler.on_stream_closed()
        except Exception:  # noqa: BLE001 - the SDK logs and swallows it
            logger.exception("on_stream_closed raised")


class FakeIpcClient:
    """A ``GreengrassCoreIPCClient`` over one fake connection, with one fake
    event-loop thread."""

    def __init__(self, world, number, lifecycle):
        self.world = world
        self.number = number
        self.lifecycle = lifecycle
        self.loop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake-AwsEventLoop")
        # What the fixed watchdog reads: client._connection._synced.state.name.
        self._connection = SimpleNamespace(_synced=SimpleNamespace(state=SimpleNamespace(name="CONNECTED")))
        self.closed = False
        self.close_calls = 0
        self.operations = []
        self._deferred_closes = []
        self._lock = threading.Lock()

    def __repr__(self):
        return "<client {}>".format(self.number)

    def _new(self, kind, stream_handler=None):
        with self._lock:
            accepted = not self.closed
            self.world.record(kind, self, accepted)
            if not accepted:
                raise ConnectionClosedError()  # eventstreamrpc.py:503-507
            operation = FakeOperation(self, kind, stream_handler)
            self.operations.append(operation)
        return operation

    def new_get_thing_shadow(self):
        return self._new("get_thing_shadow")

    def new_update_thing_shadow(self):
        return self._new("update_thing_shadow")

    def new_publish_to_iot_core(self):
        return self._new("publish_to_iot_core")

    def new_get_configuration(self):
        return self._new("get_configuration")

    def new_list_components(self):
        return self._new("list_components")

    def new_subscribe_to_iot_core(self, stream_handler):
        return self._new("subscribe_to_iot_core", stream_handler)

    def close(self):
        """The SDK client's close(): no code may call it (N1)."""
        self.close_calls += 1
        return _done_future()

    def submit(self, task):
        try:
            return self.loop.submit(task)
        except RuntimeError:  # the loop is shut down: the test is over
            return None

    def notify_lifecycle(self, method, *args):
        if self.lifecycle is not None:
            getattr(self.lifecycle, method)(*args)

    def close_connection(self, reason):
        """The Nucleus closes this connection."""
        with self._lock:
            self.closed = True
            self._connection._synced.state.name = "DISCONNECTED"
            streams = [operation for operation in self.operations if operation.stream_open]

        def _shutdown():
            for operation in streams:
                operation.deliver_stream_closed()
            self.notify_lifecycle("on_disconnect", reason)

        return self.submit(_shutdown)

    def fail_stream(self, operation, error, defer_close=False):
        """A stream error on the loop thread (``eventstreamrpc.py:765-776``):
        ``on_stream_error``, and when it returns True or None the SDK closes
        the stream, whose ``on_stream_closed`` follows on the loop thread.
        With ``defer_close`` that close lands during the next subscription's
        activation instead."""
        def _stream_error():
            result = operation.stream_handler.on_stream_error(error)
            if result or result is None:
                if defer_close:
                    with self._lock:
                        self._deferred_closes.append(operation)
                else:
                    self.submit(operation.deliver_stream_closed)
            return result

        return self.submit(_stream_error)

    def take_deferred_closes(self):
        with self._lock:
            closes, self._deferred_closes = self._deferred_closes, []
        return closes


class FakeIpcWorld:
    """``awsiot.greengrasscoreipc.connect`` and the fixed tree's
    ``utils.ipc_client._open_connection`` over numbered fake clients. The
    journal records every ``new_*`` call, ``(kind, client, accepted)``."""

    def __init__(self):
        self.clients = []
        self.connects = 0
        self.opened = 0
        self.journal = []
        self.publish_error = None
        self._lock = threading.Lock()
        self._shut = False

    def record(self, kind, client, accepted):
        with self._lock:
            self.journal.append((kind, client, accepted))

    def _new_client(self, lifecycle):
        with self._lock:
            if self._shut:
                raise ConnectionRefusedError("the fake Nucleus is gone: the test is over")
            client = FakeIpcClient(self, len(self.clients) + 1, lifecycle)
            self.clients.append(client)
        return client

    def connect(self, **kwargs):
        """The unfixed client calls it with no arguments; the fixed one with
        ``lifecycle_handler`` and ``timeout``."""
        with self._lock:
            self.connects += 1
        client = self._new_client(kwargs.get("lifecycle_handler"))
        client.submit(lambda: client.notify_lifecycle("on_connect"))
        return client

    def open_connection(self, lifecycle):
        """``_open_connection(lifecycle) -> (client, connect future)``; the
        Nucleus acknowledges at once."""
        with self._lock:
            self.opened += 1
        client = self._new_client(lifecycle)
        connected = Future()

        def _connect_ack():
            connected.set_result(None)
            client.notify_lifecycle("on_connect")

        client.submit(_connect_ack)
        return client, connected

    def outcome_for(self, operation):
        if operation.kind == "get_thing_shadow":
            return SimpleNamespace(payload=json.dumps({"state": GET_STATE}).encode("utf-8"))
        if operation.kind == "update_thing_shadow":
            return SimpleNamespace(payload=b'{"state": {}}')
        if operation.kind == "publish_to_iot_core" and self.publish_error is not None:
            return self.publish_error()
        if operation.kind == "get_configuration":
            return SimpleNamespace(value={"shadowDocumentSizeLimitBytes": SIZE_LIMIT})
        if operation.kind == "list_components":
            return SimpleNamespace(components=[])
        return SimpleNamespace()

    def entries(self, kind):
        with self._lock:
            return [(client, accepted) for k, client, accepted in self.journal if k == kind]

    def activations(self):
        """Every subscription whose response the loop delivered, in order."""
        with self._lock:
            clients = list(self.clients)
        return sorted((operation for client in clients for operation in list(client.operations)
                       if operation.kind == "subscribe_to_iot_core" and operation.responded_at is not None),
                      key=lambda operation: operation.responded_at)

    def subscribe_attempts(self):
        with self._lock:
            clients = list(self.clients)
        return sorted((operation for client in clients for operation in list(client.operations)
                       if operation.kind == "subscribe_to_iot_core" and operation.activated_at is not None),
                      key=lambda operation: operation.activated_at)

    def shutdown(self):
        with self._lock:
            self._shut = True
            clients = list(self.clients)
        for client in clients:
            client.loop.shutdown(wait=False, cancel_futures=True)


class RecordingStreamHandler(SubscribeToIoTCoreStreamHandler):
    """A shadow handler: ``on_stream_error`` asks the SDK to close the
    stream, as the four handler factories do."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.errors = []
        self.closes = 0

    def on_stream_event(self, event):
        self.events.append(event)

    def on_stream_error(self, error):
        self.errors.append(error)
        return True

    def on_stream_closed(self):
        self.closes += 1


class RecordingPublisher:
    """``PublishHandler``'s surface: records ``(time, topic, message)``."""

    def __init__(self):
        self.published = []
        self._cond = threading.Condition()

    def publish_message(self, topic, message):
        with self._cond:
            self.published.append((time.monotonic(), topic, message))
            self._cond.notify_all()

    def times(self, topic):
        with self._cond:
            return [at for at, t, _ in self.published if t == topic]

    def wait_for(self, topic, count, timeout):
        with self._cond:
            return self._cond.wait_for(
                lambda: sum(1 for _, t, _ in self.published if t == topic) >= count, timeout)


# --- routing (plan P3) ----------------------------------------------------------------------


def _import_fresh(monkeypatch, name):
    """Import ``name`` again under this test's stubs; the session's module (or
    its absence) comes back at teardown."""
    package_name, _, attribute = name.rpartition(".")
    package = importlib.import_module(package_name)
    monkeypatch.setattr(package, attribute, getattr(package, attribute, None), raising=False)
    monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
    del sys.modules[name]
    return importlib.import_module(name)


def _install_deprecated_stub(monkeypatch):
    """``defect_detection_config`` imports ``deprecated``, which the host venv
    lacks: the passthrough stub of ``test_server_setup_isolation.py``, for this
    test only, never over a real package."""
    if "deprecated" in sys.modules or importlib.util.find_spec("deprecated") is not None:
        return
    module = types.ModuleType("deprecated")

    def deprecated(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):
            return func

        return wrap

    module.deprecated = deprecated
    monkeypatch.setitem(sys.modules, "deprecated", module)


@pytest.fixture
def world(monkeypatch):
    """A fresh shared connection over the fake world."""
    fake = FakeIpcWorld()
    for name, value in IPC_TIMINGS:
        monkeypatch.setattr(shared_ipc, name, value, raising=False)
    shared_ipc.reset_ipc_client()
    monkeypatch.setattr(awsiot.greengrasscoreipc, "connect", fake.connect)
    monkeypatch.setattr(shared_ipc, "_open_connection", fake.open_connection, raising=False)
    try:
        yield fake
    finally:
        shared_ipc.reset_ipc_client()
        fake.shutdown()


@pytest.fixture
def subscription_module(monkeypatch, world):
    """``mqtt.SubscriptionHandler``, imported for this test under a
    ``utils.server_setup`` stub whose ``ipc_client`` is
    ``get_ipc_client()``, as ``server_setup`` sets it at import."""
    stub = types.ModuleType("utils.server_setup")
    stub.ipc_client = shared_ipc.get_ipc_client()
    monkeypatch.setitem(sys.modules, "utils.server_setup", stub)
    monkeypatch.setattr(utils_package, "server_setup", stub, raising=False)
    module = _import_fresh(monkeypatch, "mqtt.SubscriptionHandler")
    for name, value in SUBSCRIPTION_TIMINGS:
        monkeypatch.setattr(module, name, value, raising=False)
    return module


def _eventually(predicate, timeout, interval=0.01):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _settle(future, timeout=5.0):
    """Wait for a loop task, without raising."""
    if future is not None:
        concurrent.futures.wait([future], timeout=timeout)


def _subscribe(subscription, errors):
    try:
        subscription.subscribe()
    except Exception as error:  # noqa: BLE001 - reported by the test
        errors.append(error)


class Running:
    """A ``SubscriptionHandler`` whose ``subscribe()`` runs on a daemon
    thread (the unfixed one never returns); ``close()`` ends it."""

    def __init__(self, module):
        self.handler = RecordingStreamHandler()
        self.publisher = RecordingPublisher()
        self.subscription = module.SubscriptionHandler(PREFIX, self.handler, self.publisher)
        self.errors = []
        self.thread = threading.Thread(target=_subscribe, args=(self.subscription, self.errors),
                                       name="f2325-subscribe", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.subscription.close()
        return False


def _first_activation(world, running):
    assert running.publisher.wait_for(GET_TOPIC, 1, 2.0) and world.activations(), (
        "setup: the subscription to {} never activated (errors: {})".format(PREFIX, running.errors))
    return world.activations()[0]


# --- the bug conditions ---------------------------------------------------------------------


def test_f24_holders_reach_a_new_connection_after_a_close(world, monkeypatch):
    """The holders ``server_setup`` builds at import reach the new
    connection once the old one closes, and no client is ever closed."""
    _install_deprecated_stub(monkeypatch)
    config_module = _import_fresh(monkeypatch, "defect_detection_config.defect_detection_config")
    shared = shared_ipc.get_ipc_client()
    shadow = IoTShadowAccessor(shared)
    publisher = PublishHandler(shared)
    config = config_module.DefectDetectionConfig(shared)
    size_limit = shadow_manager_size_limit_provider(config.get_component_config)
    holders = (
        ("IoTShadowAccessor", "get_thing_shadow", lambda: shadow.get_thing_shadow_state_request(THING, SHADOW)),
        ("PublishHandler", "publish_to_iot_core", lambda: publisher.publish_message(PREFIX + "f2325", "{}")),
        ("DefectDetectionConfig", "get_configuration",
         lambda: config.get_component_config("aws.greengrass.ShadowManager")),
        ("the size-limit provider", "get_configuration", size_limit),
    )

    def reached():
        """Each holder's call -> ``(client, accepted)`` of the newest
        operation it asked for; ``(None, False)`` when it asked for none."""
        clients = {}
        for label, kind, call in holders:
            mark = len(world.entries(kind))
            try:
                call()
            except Exception:  # noqa: BLE001 - a holder that lets ConnectionClosedError out
                pass
            made = world.entries(kind)[mark:]
            clients[label] = made[-1] if made else (None, False)
        return clients

    def describe(clients):
        return {label: "{}{}".format(client, "" if accepted else " (ConnectionClosedError)")
                for label, (client, accepted) in clients.items()}

    assert len(world.clients) == 1, "setup: expected one connect, got {}".format(len(world.clients))
    client1 = world.clients[0]
    assert all(entry == (client1, True) for entry in reached().values()), (
        "setup: the holders do not reach client 1")

    _settle(client1.close_connection(ConnectionResetError("AWS_IO_SOCKET_CLOSED")))

    def on_client_2():
        return len(world.clients) >= 2 and all(
            entry == (world.clients[1], True) for entry in reached().values())

    moved = _eventually(on_client_2, 2.0, interval=0.05)
    assert moved, (
        "2 s after connection 1 closed, the holders built on get_ipc_client() still reach {}: they keep "
        "the closed client, and nothing reconnects (finding 24)".format(describe(reached())))
    assert shadow.get_thing_shadow_state_request(THING, SHADOW) == GET_STATE
    assert size_limit() == SIZE_LIMIT
    assert [client.close_calls for client in world.clients] == [0] * len(world.clients), (
        "a client's close() was called (N1)")


def test_f24_closed_stream_is_subscribed_again(world, subscription_module):
    """After a stream error and its close, the subscription is made again."""
    with Running(subscription_module) as running:
        first = _first_activation(world, running)
        _settle(first.client.fail_stream(first, RuntimeError("the Nucleus reset the stream")))

        again = _eventually(lambda: len(world.activations()) >= 2, 2.0)
        assert again, (
            "no new subscription to {} within 2 s of its stream error and close: the subscription "
            "is never made again (finding 24); activations: {}".format(
                PREFIX, [operation.client for operation in world.activations()]))


def test_f24_subscription_moves_to_the_new_connection(world, subscription_module):
    """A subscription on a connection that closes is made again on the new
    connection."""
    with Running(subscription_module) as running:
        first = _first_activation(world, running)
        client1 = first.client
        _settle(client1.close_connection(ConnectionResetError("AWS_IO_SOCKET_CLOSED")))

        moved = _eventually(
            lambda: any(operation.client is not client1 for operation in world.activations()), 2.0)
        assert moved, (
            "the subscription to {} did not move to a new connection within 2 s of connection 1 "
            "closing (finding 24); activations: {}".format(
                PREFIX, [operation.client for operation in world.activations()]))


def test_f24_reactivation_completes_while_the_old_stream_closes(world, subscription_module):
    """A stream error whose ``on_stream_closed`` arrives on the event-loop
    thread during the re-activation: the re-activation completes within
    200 ms of its start."""
    with Running(subscription_module) as running:
        first = _first_activation(world, running)
        _settle(first.client.fail_stream(first, RuntimeError("the Nucleus reset the stream"),
                                         defer_close=True))

        started = _eventually(lambda: len(world.subscribe_attempts()) >= 2, 2.0)
        assert started, (
            "no re-activation of {} started within 2 s of its stream error: the subscription is "
            "never made again (finding 24)".format(PREFIX))
        reactivation = world.subscribe_attempts()[1]
        running.publisher.wait_for(GET_TOPIC, 2, 2.0)
        completions = running.publisher.times(GET_TOPIC)[1:]
        elapsed = completions[0] - reactivation.activated_at if completions else float("inf")
        assert elapsed < 0.2, (
            "the re-activation of {} did not complete within 0.2 s while the old stream's "
            "on_stream_closed arrived on the event-loop thread: {:.3f} s (finding 24)".format(PREFIX, elapsed))


def test_f24_publish_timeout_neither_reconnects_nor_retries(world):
    """R5: a publish that times out says nothing about the connection: the
    error propagates after one connect and one publish attempt."""
    world.publish_error = concurrent.futures.TimeoutError

    raised = None
    try:
        output_bindings._default_greengrass_publisher("factory/line1/events", "{}", 1)
    except Exception as error:  # noqa: BLE001 - asserted below
        raised = error

    assert isinstance(raised, concurrent.futures.TimeoutError), (
        "the publish timeout did not propagate: {!r}".format(raised))
    connects = world.connects + world.opened
    publishes = len(world.entries("publish_to_iot_core"))
    assert (connects, publishes) == (1, 1), (
        "a publish timeout reconnected the shared client and retried: {} connects, {} publish "
        "attempts (fix 12's reset-and-retry; a timeout says nothing about the connection, R5, "
        "finding 24)".format(connects, publishes))
