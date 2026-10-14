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
"""Recovery of the shared Greengrass IPC connection and of its subscriptions
(rtsp-rtmp-stream-cameras task 30.4; finding 24, Requirement 5.14, AC 5 and 6;
design component 20).

The connection (``utils.ipc_client``):
- after a ``ConnectionClosedError``, ``IoTShadowAccessor``, ``PublishHandler``,
  ``DefectDetectionConfig`` and the size-limit provider reach the new client;
- 20 concurrent ``ConnectionClosedError`` make one reconnect and one ERROR, no
  client is ever closed, and repeated ones during a backoff do not shorten it;
- a denial, a not-found, a validation error and a timeout make no reconnect;
- each signal reconnects: the adopted connection's ``on_disconnect``, and the
  watchdog seeing ``DISCONNECTING`` with no callback; the watchdog's attribute
  path is pinned against the installed SDK; a ``_Lifecycle`` method that
  raises is contained and logged;
- an attempt that resolves after 2 x ``CONNECT_TIMEOUT_S`` is adopted, with no
  second attempt meanwhile; a late ``on_disconnect`` of a replaced connection
  changes nothing;
- a reset during a pending attempt, and during a backoff wait: the wait ends at
  once, the attempt is retired, nothing but the next lazy connect is adopted,
  and ``_open_connection`` is not called again; the watchdog does nothing
  while there is no client; a client whose state cannot be read turns the
  watchdog off for that client only;
- ``call_with_ipc_retry`` fails at once once an outage is older than
  ``RETRY_WAIT_S``, and a first call reports against the connection it made;
- the early-death count: after a connection that dies within
  ``STABLE_CONNECTION_S`` the first attempt waits ``RECONNECT_BACKOFF_S[0]``,
  after one that lived longer it starts at once, and a reset clears the count;
- the handle forwards only public names and never closes anything.

The subscriptions (``mqtt.SubscriptionHandler``):
- re-subscription after a stream error, a stream close and a replaced
  connection, with backoff; a denied re-activation is retried; the R10 lines;
- a stream closed during its activation: ERROR, and a new subscription within
  ``FIRST_RETRY_DELAY_S`` plus backoff;
- order: an event queued before a re-activation runs before its catch-up; a
  catch-up on an empty queue runs with no further event; with the real
  camera-registry agent, a queued delta carrying C1 and a re-activation whose
  catch-up reads C2 for the same camera end with C2 applied, and a change
  written during an outage is applied exactly once;
- a timed-out activation's operation is closed (never the client) before the
  next attempt and released by its ``on_stream_closed``; a failed catch-up is
  not requested again while no stream is active; with the real agent, a
  catch-up whose GET keeps failing while the stream is up gets no report
  written until a GET succeeds (task 30 follow-up, review item 1); operations
  stay retained until their stream closes, with one WARNING.

The fake IPC model is ``f2325_ipc_support`` (plan P4); the timing constants
are patched to milliseconds. Generations are relative to the one a test reads.
"""
import contextlib
import copy
import importlib
import importlib.util
import itertools
import json
import logging
import os
import re
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import Mapping

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

import awsiot.greengrasscoreipc  # noqa: E402
from awsiot.eventstreamrpc import Connection, ConnectionClosedError, StreamClosedError  # noqa: E402
from awsiot.greengrasscoreipc.client import (  # noqa: E402
    GreengrassCoreIPCClient,
    SubscribeToIoTCoreStreamHandler,
)
from awsiot.greengrasscoreipc.model import (  # noqa: E402
    InvalidArgumentsError,
    ResourceNotFoundError,
    ServiceError,
    UnauthorizedError,
)
from fastapi import HTTPException  # noqa: E402

from f2325_ipc_support import (  # noqa: E402
    CLOSED_DURING_ACTIVATION,
    GET_STATE,
    GET_TOPIC,
    HOLD,
    NO_RESPONSE,
    PREFIX,
    REFUSED,
    SHADOW,
    SIZE_LIMIT,
    SLOW,
    SUBSCRIPTION_TIMINGS,
    THING,
    CatchUp,
    Journal,
    RecordingPublisher,
    RecordingStreamHandler,
    delta_event,
    eventually,
    fast_timings,
    install,
    settle,
    uninstall,
)

from camera_sync import CameraSyncStateStore, EdgeSyncAgent, shadow_manager_size_limit_provider  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402
from dao.iotshadow import IoTShadowAccessor as iot_shadow_module  # noqa: E402
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor  # noqa: E402
from mqtt import SubscriptionHandler as subscription_module  # noqa: E402
from mqtt.PublishHandler import PublishHandler  # noqa: E402
from utils import ipc_client as shared_ipc  # noqa: E402

TOPIC = PREFIX + "#"
FIRST_RETRY_DELAY_S = SUBSCRIPTION_TIMINGS["FIRST_RETRY_DELAY_S"]
SUBSCRIBE_BACKOFF_S = SUBSCRIPTION_TIMINGS["SUBSCRIBE_BACKOFF_S"]
SLEEP_TIME = SUBSCRIPTION_TIMINGS["SLEEP_TIME"]


@pytest.fixture
def world(monkeypatch):
    """A fresh shared connection over the fake world, with millisecond timing."""
    fake = install(monkeypatch)
    fast_timings(monkeypatch, subscription_module)
    try:
        yield fake
    finally:
        uninstall(fake)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="utils.ipc_client")
    caplog.set_level(logging.DEBUG, logger="mqtt.SubscriptionHandler")
    return caplog


def _lines(caplog, name, level):
    return [record.getMessage() for record in caplog.records
            if record.name == name and record.levelno == level]


def _ipc(caplog, level):
    return _lines(caplog, "utils.ipc_client", level)


def _subscription(caplog, level):
    return _lines(caplog, "mqtt.SubscriptionHandler", level)


def _lost_line(gen, reason):
    return "Greengrass IPC connection {} lost ({}); reconnecting".format(gen, reason)


def _opens(world):
    """The ``_open_connection`` attempts: [kind, client, started, ended]."""
    return [attempt for attempt in world.attempts if attempt[0] == "open"]


# --- the subscription under test ------------------------------------------------------


class Running:
    """A ``SubscriptionHandler`` whose ``subscribe()`` runs on a daemon thread
    until ``close()``; ``__enter__`` waits for its first activation."""

    def __init__(self, world, handler=None, on_active=None, journal=None):
        self.world = world
        self.journal = journal if journal is not None else Journal()
        self.handler = handler if handler is not None else RecordingStreamHandler(self.journal)
        self.publisher = RecordingPublisher()
        kwargs = {} if on_active is None else {"on_active": on_active}
        self.subscription = subscription_module.SubscriptionHandler(
            PREFIX, self.handler, self.publisher, **kwargs)
        self.errors = []
        self.thread = threading.Thread(target=self._subscribe, name="f2325-subscribe", daemon=True)

    def _subscribe(self):
        try:
            self.subscription.subscribe()
        except Exception as error:  # noqa: BLE001 - reported by the test
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        assert self.wait_activated(1), (
            "setup: the subscription to {} never activated (errors: {})".format(TOPIC, self.errors))
        return self

    def __exit__(self, *exc_info):
        self.subscription.close()
        self.thread.join(5)
        return False

    def wait_activated(self, count, timeout=3.0):
        """True once ``count`` activations completed: each publishes ``.../get``."""
        return self.publisher.wait_for(GET_TOPIC, count, timeout)

    def latest(self):
        """The operation of the newest completed activation."""
        return self.subscription.operation

    def deliver(self, operation, *names):
        futures = [operation.client.deliver(operation, delta_event(name)) for name in names]
        assert settle(futures, 5.0), "the loop never ran the deliveries"

    def deliver_document(self, operation, document):
        """One ``update/delta`` event carrying ``document``, on the loop thread."""
        event = SimpleNamespace(message=SimpleNamespace(
            topic_name=PREFIX + "delta", payload=json.dumps(document).encode("utf-8")))
        assert settle([operation.client.deliver(operation, event)], 5.0)


def _close_connection(client):
    assert settle([client.close_connection(ConnectionResetError("AWS_IO_SOCKET_CLOSED"))])


def _fail_stream(operation):
    assert settle([operation.client.fail_stream(operation, RuntimeError("the Nucleus reset the stream"))])


# --- the camera-registry agent's fakes (as test_f2325_device_bug_conditions.py) --------


def _merge(target, update):
    """AWS IoT shadow update semantics: nested maps merge, a null deletes."""
    for key, value in update.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class MergingShadow:
    """The camera-registry shadow with AWS update semantics. ``fail_clear``
    maps a csid to how many of its next clears (a desired write nulling it)
    fail with ``TimeoutError``; a failed write changes nothing."""

    def __init__(self):
        self.state = {}
        self.fail_clear = {}
        self.failed = []
        self.gets = 0
        self._lock = threading.Lock()

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        with self._lock:
            self.gets += 1
            return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name, update):
        with self._lock:
            changes = (update.get("desired") or {}).get("changes") or {}
            failing = [csid for csid, value in changes.items()
                       if value is None and self.fail_clear.get(csid, 0) > 0]
            if failing:
                for csid in failing:
                    self.fail_clear[csid] -= 1
                self.failed.append(copy.deepcopy(update))
                raise TimeoutError("the desired-entry clear timed out")
            _merge(self.state, update)

    def portal_writes(self, changes):
        """The Portal's desired write (camera_registry.write_desired_change)."""
        self.update_thing_shadow_state_request(THING, SHADOW, {"desired": {"changes": copy.deepcopy(changes)}})

    def desired_changes(self):
        with self._lock:
            return copy.deepcopy((self.state.get("desired") or {}).get("changes") or {})


class DictAccessor:
    """The ImageSourceAccessor surface the agent uses, over a dict."""

    def __init__(self, *sources):
        self.sources = {source["imageSourceId"]: copy.deepcopy(source) for source in sources}
        self.calls = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def list_image_sources(self, request, session):
        with self._lock:
            return [copy.deepcopy(source) for source in self.sources.values()]

    def create_image_source(self, data, session, managed_stream_settings=None):
        with self._lock:
            self.calls.append(("create", data.get("type")))
            image_source_id = "is-{}".format(next(self._ids))
            source = dict(data, imageSourceId=image_source_id)
            source.pop("credentials", None)
            source.setdefault("imageSourceConfiguration", {})
            self.sources[image_source_id] = source
            return {"imageSourceId": image_source_id}

    def update_image_source(self, image_source_id, data, session, managed_stream_settings=None):
        with self._lock:
            self.calls.append(("update", image_source_id))
            if image_source_id not in self.sources:
                raise HTTPException(status_code=404, detail="no image source {}".format(image_source_id))
            data = dict(data)
            data.pop("credentials", None)
            self.sources[image_source_id].update(data)
            return {"imageSourceId": image_source_id}

    def delete_image_source(self, image_source_id, session):
        with self._lock:
            self.calls.append(("delete", image_source_id))
            self.sources.pop(image_source_id, None)
            return {"imageSourceId": image_source_id}

    def names(self):
        with self._lock:
            return sorted(source["name"] for source in self.sources.values())


class _NoCredentials:
    def configured(self, image_source_id):
        return False


class _UnpinnedStore:
    def status(self):
        return {"pinned": False, "metadata": None}


class _NoMarkerPinWorker:
    report_inventory = None

    def applied_marker(self):
        return None

    def start(self):
        pass

    def stop(self):
        pass


class _RecordingTimer:
    def __init__(self):
        self.pending = []

    def __call__(self, delay, action):
        self.pending.append((delay, action))


class SignallingHandler(SubscribeToIoTCoreStreamHandler):
    """Wraps a real shadow handler: ``started`` and ``finished`` count the
    events it began and finished, on the subscription's worker."""

    def __init__(self, inner):
        super().__init__()
        self.inner = inner
        self.started = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)

    def on_stream_event(self, event):
        self.started.release()
        try:
            self.inner.on_stream_event(event)
        finally:
            self.finished.release()

    def on_stream_error(self, error):
        return self.inner.on_stream_error(error)

    def on_stream_closed(self):
        self.inner.on_stream_closed()


@pytest.fixture
def agent_world(tmp_path, monkeypatch):
    """The real camera-registry agent over a dict accessor and a merging
    shadow, with its real shadow handler wrapped for signalling."""
    monkeypatch.setattr(agent_module, "get_store", lambda: _UnpinnedStore())
    monkeypatch.setattr(agent_module, "get_video_store", lambda: _UnpinnedStore())
    shadow = MergingShadow()
    accessor = DictAccessor(_camera("cam0"), _camera("cam"))
    agent = EdgeSyncAgent(
        iot_shadow_accessor=shadow, image_source_accessor=accessor, camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
        thing_name=THING, clock=lambda: 1000.0, wall_clock=lambda: 1_790_000_000.0,
        debounce_seconds=0.0, pin_worker=_NoMarkerPinWorker(), video_pin_worker=_NoMarkerPinWorker(),
        stream_timer=_RecordingTimer(), credential_store=_NoCredentials(),
        change_retry_timer=_RecordingTimer())
    handler = SignallingHandler(agent_module.make_shadow_stream_handler(agent))
    return SimpleNamespace(shadow=shadow, accessor=accessor, agent=agent, handler=handler)


def _camera(image_source_id):
    return {"imageSourceId": image_source_id, "name": image_source_id, "type": "Camera",
            "cameraId": "camera-" + image_source_id, "imageSourceConfiguration": {}}


def _update(pcid, name):
    return {"op": "update", "portalChangeId": pcid, "name": name}


def _folder_create(pcid, name):
    return {"op": "create", "portalChangeId": pcid, "name": name, "type": "Folder",
            "params": {"location": "/aws_dda/images"}}


def _delta(changes, version):
    return {"state": {"changes": copy.deepcopy(changes)}, "version": version}


# --- routing helpers ------------------------------------------------------------------


def _import_fresh(monkeypatch, name):
    """Import ``name`` again under this test's stubs; the session's module (or
    its absence) comes back at teardown."""
    package_name, _, attribute = name.rpartition(".")
    package = importlib.import_module(package_name)
    monkeypatch.setattr(package, attribute, getattr(package, attribute, None), raising=False)
    monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
    del sys.modules[name]
    return importlib.import_module(name)


def _defect_detection_config(monkeypatch):
    """``defect_detection_config`` imports ``deprecated``, which the host venv
    lacks: a passthrough stub for this test only, never over a real package."""
    name = "defect_detection_config.defect_detection_config"
    if "deprecated" in sys.modules or importlib.util.find_spec("deprecated") is not None:
        return importlib.import_module(name)
    module = types.ModuleType("deprecated")

    def deprecated(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda func: func

    module.deprecated = deprecated
    monkeypatch.setitem(sys.modules, "deprecated", module)
    return _import_fresh(monkeypatch, name)


# --- the connection: holders and concurrency (AC 5) ---------------------------------------


def test_after_a_closed_connection_error_every_holder_reaches_the_new_client(world, monkeypatch, logs):
    config_module = _defect_detection_config(monkeypatch)
    shared = shared_ipc.get_ipc_client()
    shadow = IoTShadowAccessor(shared)
    publisher = PublishHandler(shared)
    config = config_module.DefectDetectionConfig(shared)
    size_limit = shadow_manager_size_limit_provider(config.get_component_config)
    holders = {
        "IoTShadowAccessor": ("get_thing_shadow", lambda: shadow.get_thing_shadow_state_request(THING, SHADOW)),
        "PublishHandler": ("publish_to_iot_core", lambda: publisher.publish_message(PREFIX + "f2325", "{}")),
        "DefectDetectionConfig": ("get_configuration",
                                  lambda: config.get_component_config("aws.greengrass.ShadowManager")),
        "the size-limit provider": ("get_configuration", size_limit),
    }
    [client1] = world.clients
    g1 = shared_ipc.generation()

    client1.mark_closed()  # no callback: only a call can tell
    assert shadow.get_thing_shadow_state_request(THING, SHADOW) is None  # the CCE, swallowed by the accessor

    assert shared_ipc.wait_for_new_connection(g1, 2.0), "no reconnect after a ConnectionClosedError"
    client2 = shared_ipc.current_client()
    assert client2 is world.clients[1]
    for label, (kind, call) in holders.items():
        mark = len(world.entries(kind))
        call()
        assert world.entries(kind)[mark:] == [(client2, True)], "{} did not reach the new client".format(label)
    assert shadow.get_thing_shadow_state_request(THING, SHADOW) == GET_STATE
    assert size_limit() == SIZE_LIMIT
    assert [client.close_calls for client in world.clients] == [0, 0], "a client's close() was called (N1)"
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "ConnectionClosedError from new_get_thing_shadow")]


def test_twenty_concurrent_closed_connection_errors_make_one_reconnect_and_one_error(world, monkeypatch, logs):
    # A backoff longer than the herd takes, so every caller still sees the closed client.
    monkeypatch.setattr(shared_ipc, "RECONNECT_BACKOFF_S", (1.0, 1.0, 1.0, 1.0, 1.0))
    handle = shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    client1.mark_closed()
    start = threading.Barrier(20)
    raised = []

    def caller():
        start.wait(5.0)
        try:
            handle.new_get_thing_shadow()
        except ConnectionClosedError:
            raised.append(threading.current_thread().name)

    threads = [threading.Thread(target=caller, name="f2325-caller-{}".format(n)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert len(raised) == 20 and not any(thread.is_alive() for thread in threads)
    assert shared_ipc.wait_for_new_connection(g1, 2.0)
    time.sleep(0.2)  # room for a second reconnect, if there were one
    assert (world.opened, shared_ipc.generation()) == (1, g1 + 1)
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "ConnectionClosedError from new_get_thing_shadow")]
    assert [client.close_calls for client in world.clients] == [0, 0], "a client's close() was called (N1)"


def test_repeated_closed_connection_errors_during_the_backoff_do_not_shorten_it(world, monkeypatch, logs):
    monkeypatch.setattr(shared_ipc, "RECONNECT_BACKOFF_S", (0.4, 0.4, 0.4, 0.4, 0.4))
    handle = shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    client1.mark_closed()
    lost_at = time.monotonic()
    with pytest.raises(ConnectionClosedError):
        handle.new_get_thing_shadow()

    while time.monotonic() - lost_at < 0.3:
        with pytest.raises(ConnectionClosedError):
            handle.new_get_thing_shadow()
        assert not shared_ipc.report_connection_closed(g1, "a repeated report")
        time.sleep(0.01)
    assert world.opened == 0, "a repeated report during the backoff started an attempt early"

    assert shared_ipc.wait_for_new_connection(g1, 2.0)
    [attempt] = _opens(world)
    assert attempt[2] - lost_at >= 0.38, "the backoff was cut short: {:.3f} s".format(attempt[2] - lost_at)
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "ConnectionClosedError from new_get_thing_shadow")]


@pytest.mark.parametrize("outcome, answer", [
    (UnauthorizedError(message="denied"), None),
    (ResourceNotFoundError(message="no such shadow"), False),
    (InvalidArgumentsError(message="bad request"), None),
    (NO_RESPONSE, None),
], ids=["denial", "not-found", "validation-error", "timeout"])
def test_a_denial_a_not_found_a_validation_error_and_a_timeout_make_no_reconnect(
        world, monkeypatch, logs, outcome, answer):
    monkeypatch.setattr(iot_shadow_module, "TIMEOUT", 0.1)
    shadow = IoTShadowAccessor(shared_ipc.get_ipc_client())
    g1 = shared_ipc.generation()
    world.outcomes["get_thing_shadow"] = outcome

    assert shadow.get_thing_shadow_state_request(THING, SHADOW) is answer

    def get(client):
        operation = client.new_get_thing_shadow()
        operation.activate(None)
        return operation.get_response().result(0.1)

    with pytest.raises(Exception) as raised:
        shared_ipc.call_with_ipc_retry(get)
    assert not isinstance(raised.value, ConnectionClosedError)
    time.sleep(0.1)  # several watchdog ticks
    assert (world.connects, world.opened, shared_ipc.generation()) == (1, 0, g1)
    assert shared_ipc.connection_usable()
    assert len(world.entries("get_thing_shadow")) == 2, "the call was retried"
    assert _ipc(logs, logging.ERROR) == []


# --- the connection: each signal -------------------------------------------------------


def test_the_adopted_connections_on_disconnect_reconnects(world, logs):
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()

    _close_connection(client1)

    assert shared_ipc.wait_for_new_connection(g1, 2.0)
    assert shared_ipc.current_client() is world.clients[1]
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "disconnected: ConnectionResetError")]

    def up_lines():
        return [line for line in _ipc(logs, logging.INFO) if line.startswith("Greengrass IPC connection")]

    # The INFO line follows the adoption (after the listeners), on the reconnect thread.
    assert eventually(lambda: up_lines(), 2.0), "no INFO line for the new connection"
    [up] = up_lines()
    # The kept count is the process's (replaced clients are kept for life, N1).
    assert re.fullmatch(r"Greengrass IPC connection {} is up after \d+\.\d s \(0 failed attempts, "
                        r"\d+ replaced connections kept\)".format(g1 + 1), up), up


def test_the_watchdog_reports_a_disconnecting_connection_that_gave_no_signal(world, logs):
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()

    client1.mark_closed("DISCONNECTING")  # no callback and no call

    assert shared_ipc.wait_for_new_connection(g1, 2.0), "the watchdog never saw the closing connection"
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "state DISCONNECTING")]
    assert shared_ipc.current_client() is world.clients[1]


def test_the_watchdog_reads_the_state_of_the_installed_sdk(world, monkeypatch, logs):
    """The attribute path the watchdog reads, pinned against the installed
    awsiotsdk: a never-connected ``GreengrassCoreIPCClient`` reads the str
    ``"DISCONNECTED"``, and its ``new_*`` raise ``ConnectionClosedError``, a
    ``RuntimeError``. Adopted as the shared client, the watchdog reports it."""
    real = GreengrassCoreIPCClient(Connection(host_name="x", port=0, bootstrap=None))
    state = real._connection._synced.state.name
    assert state == "DISCONNECTED" and type(state) is str
    assert issubclass(ConnectionClosedError, RuntimeError)
    assert shared_ipc.ConnectionClosedError is ConnectionClosedError
    with pytest.raises(ConnectionClosedError):
        real.new_get_thing_shadow()
    closes = []
    monkeypatch.setattr(real, "close", lambda *args, **kwargs: closes.append(args), raising=False)
    monkeypatch.setattr(awsiot.greengrasscoreipc, "connect", lambda **kwargs: real)

    handle = shared_ipc.get_ipc_client()
    g1 = shared_ipc.generation()
    assert shared_ipc.current_client() is real

    assert shared_ipc.wait_for_new_connection(g1, 2.0), "the watchdog did not report the SDK's closed state"
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "state DISCONNECTED")]
    [replacement] = world.clients
    assert handle.new_get_thing_shadow().client is replacement
    assert closes == [], "the real client's close() was called (N1)"


def test_a_lifecycle_callback_that_raises_is_contained_and_logged(world, monkeypatch, logs):
    shared_ipc.get_ipc_client()
    [lifecycle] = world.lifecycles

    def failing_report(gen, reason):
        raise RuntimeError("the report failed")

    def failing_describe(error):
        raise RuntimeError("the description failed")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(shared_ipc, "report_connection_closed", failing_report)
        assert lifecycle.on_disconnect(ConnectionResetError("AWS_IO_SOCKET_CLOSED")) is None
        patched.setattr(shared_ipc, "_describe", failing_describe)
        assert lifecycle.on_error(RuntimeError("a protocol error")) is True
    assert lifecycle.on_error(RuntimeError("a protocol error")) is True
    lifecycle.on_connect()
    lifecycle.on_ping([], b"")

    contained = {record.getMessage() for record in logs.records
                 if record.name == "utils.ipc_client" and record.exc_info}
    assert contained == {
        "Error handling the disconnect of Greengrass IPC attempt {}".format(lifecycle.token),
        "Error handling a protocol error of Greengrass IPC attempt {}".format(lifecycle.token),
    }
    assert ("Greengrass IPC protocol error on attempt {}: RuntimeError: a protocol error".format(lifecycle.token)
            in _ipc(logs, logging.ERROR))


# --- the connection: attempts, retired tokens and resets ------------------------------------


def test_a_slow_attempt_is_waited_on_past_its_timeout_and_adopted(world, monkeypatch, logs):
    monkeypatch.setattr(shared_ipc, "CONNECT_TIMEOUT_S", 0.1)
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    world.open_script = [(SLOW, 0.25)]  # acknowledged after 2 x CONNECT_TIMEOUT_S

    client1.mark_closed()
    assert shared_ipc.report_connection_closed(g1, "test")

    assert shared_ipc.wait_for_new_connection(g1, 3.0)
    assert world.opened == 1, "a second attempt was made while the first was pending"
    [attempt] = _opens(world)
    assert attempt[3] - attempt[2] >= 0.24, "setup: the attempt was not slow"
    assert shared_ipc.current_client() is attempt[1]
    waits = [line for line in _ipc(logs, logging.WARNING) if line.startswith("Greengrass IPC connect attempt")]
    assert waits[:2] == ["Greengrass IPC connect attempt 1 still waiting after 0.1 s",
                         "Greengrass IPC connect attempt 1 still waiting after 0.2 s"]


def test_a_late_disconnect_of_a_replaced_connection_changes_nothing(world, logs):
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    _close_connection(client1)
    assert shared_ipc.wait_for_new_connection(g1, 2.0)
    g2 = shared_ipc.generation()

    client1.notify_lifecycle("on_disconnect", ConnectionResetError("a late callback"))  # a retired token

    time.sleep(0.1)
    assert (shared_ipc.generation(), world.opened) == (g2, 1)
    assert shared_ipc.connection_usable()
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "disconnected: ConnectionResetError")]


def test_a_reset_during_a_pending_attempt_retires_it_and_adopts_only_the_next_lazy_connect(world, logs):
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    world.open_script = [HOLD]
    client1.mark_closed()
    assert shared_ipc.report_connection_closed(g1, "test")
    assert eventually(lambda: world.opened == 1, 2.0)
    [pending] = [attempt[1] for attempt in _opens(world)]

    shared_ipc.reset_ipc_client()
    assert shared_ipc.current_client() is None
    time.sleep(0.1)  # the watchdog does nothing while there is no client
    assert world.connects == 1 and shared_ipc.connection_usable()

    handle = shared_ipc.get_ipc_client()  # the next lazy connect
    lazy, g_lazy = shared_ipc.current_client(), shared_ipc.generation()
    assert lazy is world.clients[-1] and lazy is not pending and world.connects == 2
    world.acknowledge(pending)  # the retired attempt connects now
    time.sleep(3 * shared_ipc.CONNECT_TIMEOUT_S)  # its wait slice ends, and it is retired

    assert (shared_ipc.current_client(), shared_ipc.generation()) == (lazy, g_lazy)
    assert world.opened == 1, "_open_connection was called again"
    assert pending in shared_ipc._retired, "the retired attempt is not kept referenced (N1)"
    pending.notify_lifecycle("on_disconnect", ConnectionResetError("the retired attempt"))
    time.sleep(0.05)
    assert shared_ipc.connection_usable() and shared_ipc.generation() == g_lazy
    assert handle.new_get_thing_shadow().client is lazy
    assert [client.close_calls for client in world.clients] == [0] * len(world.clients)


def test_a_reset_during_a_backoff_wait_ends_the_wait_at_once(world, monkeypatch, logs):
    monkeypatch.setattr(shared_ipc, "RECONNECT_BACKOFF_S", (5.0, 5.0, 5.0, 5.0, 5.0))
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    client1.mark_closed()
    assert shared_ipc.report_connection_closed(g1, "test")
    time.sleep(0.1)  # the reconnect thread is in its 5 s backoff wait
    assert world.opened == 0

    shared_ipc.reset_ipc_client()
    shared_ipc.get_ipc_client()  # the next lazy connect
    lazy, g2 = shared_ipc.current_client(), shared_ipc.generation()
    time.sleep(0.1)
    assert world.opened == 0, "a reset must retire the loss, not start an attempt"

    # The reconnect thread is back to waiting for losses: the loss of a
    # connection that outlived STABLE_CONNECTION_S starts an attempt at once,
    # well inside the 5 s it would still be waiting otherwise.
    monkeypatch.setattr(shared_ipc, "STABLE_CONNECTION_S", 0.0)
    lazy.mark_closed()
    started = time.monotonic()
    assert shared_ipc.report_connection_closed(g2, "test")
    assert shared_ipc.wait_for_new_connection(g2, 2.0)
    assert time.monotonic() - started < 1.0
    assert world.opened == 1


def test_a_client_without_a_readable_state_turns_the_watchdog_off_for_that_client_only(world, monkeypatch, logs):
    def connect_without_state(**kwargs):
        client = world.connect(**kwargs)
        client._connection = SimpleNamespace()  # no _synced: the state cannot be read
        return client

    monkeypatch.setattr(awsiot.greengrasscoreipc, "connect", connect_without_state)
    handle = shared_ipc.get_ipc_client()
    [client1] = world.clients
    g1 = shared_ipc.generation()
    time.sleep(0.15)  # several watchdog ticks
    off = [line for line in _ipc(logs, logging.WARNING) if line.startswith("Cannot read the state")]
    assert off == ["Cannot read the state of Greengrass IPC connection {}; its state watchdog is off".format(g1)]

    client1.closed = True  # closed, with no callback: only a call can tell now
    time.sleep(0.1)
    assert shared_ipc.generation() == g1 and shared_ipc.connection_usable()
    with pytest.raises(ConnectionClosedError):
        handle.new_get_thing_shadow()
    assert shared_ipc.wait_for_new_connection(g1, 2.0)
    client2 = shared_ipc.current_client()

    client2.mark_closed("DISCONNECTING")  # the watchdog is on again for the new client
    assert shared_ipc.wait_for_new_connection(g1 + 1, 2.0), "the watchdog stayed off for the new client"
    assert _ipc(logs, logging.ERROR)[-1] == _lost_line(g1 + 1, "state DISCONNECTING")
    assert len([line for line in _ipc(logs, logging.WARNING) if line.startswith("Cannot read the state")]) == 1


def test_call_with_ipc_retry_fails_at_once_once_the_outage_is_older_than_the_retry_wait(world, monkeypatch, logs):
    monkeypatch.setattr(shared_ipc, "RETRY_WAIT_S", 0.3)
    shared_ipc.get_ipc_client()
    [client1] = world.clients
    world.open_script = [REFUSED] * 1000  # the Nucleus refuses for the whole test
    client1.mark_closed()

    def get(client):
        return client.new_get_thing_shadow()

    started = time.monotonic()
    with pytest.raises(ConnectionClosedError):
        shared_ipc.call_with_ipc_retry(get)
    first = time.monotonic() - started
    assert 0.25 <= first < 1.0, "the first call did not wait out RETRY_WAIT_S: {:.3f} s".format(first)

    started = time.monotonic()
    with pytest.raises(ConnectionClosedError):
        shared_ipc.call_with_ipc_retry(get)
    assert time.monotonic() - started < 0.1, "a call during an outage older than RETRY_WAIT_S waited"
    refused = [line for line in _ipc(logs, logging.WARNING) if line.startswith("Could not reconnect")]
    assert refused and refused[0].startswith(
        "Could not reconnect to Greengrass IPC (attempt 1): ConnectionRefusedError: ")


def test_call_with_ipc_retry_reports_a_first_calls_loss_against_the_connection_it_made(world, logs):
    shared_ipc.get_ipc_client()
    shared_ipc.reset_ipc_client()  # no client: the call below makes the connect
    g_before = shared_ipc.generation()
    reached = []

    def operation(client):
        reached.append(shared_ipc.current_client())
        if len(reached) == 1:
            raise ConnectionClosedError()  # say, the response of a call the connection dropped
        return "ok"

    assert shared_ipc.call_with_ipc_retry(operation) == "ok"
    assert reached == world.clients[1:3]
    assert _ipc(logs, logging.ERROR) == [_lost_line(g_before + 1, "ConnectionClosedError from a call")]


def test_the_early_death_count_sets_the_first_backoff_and_a_reset_clears_it(world, monkeypatch):
    monkeypatch.setattr(shared_ipc, "RECONNECT_BACKOFF_S", (0.3, 0.9, 0.9, 0.9, 0.9))
    monkeypatch.setattr(shared_ipc, "STABLE_CONNECTION_S", 0.25)

    def lose_current():
        """The delay from the loss to the first attempt."""
        client, gen = shared_ipc.current_client(), shared_ipc.generation()
        mark = len(_opens(world))
        client.mark_closed()
        lost_at = time.monotonic()
        assert shared_ipc.report_connection_closed(gen, "test")
        assert shared_ipc.wait_for_new_connection(gen, 3.0)
        [attempt] = _opens(world)[mark:]
        return attempt[2] - lost_at

    shared_ipc.get_ipc_client()
    delay = lose_current()  # died within STABLE_CONNECTION_S: the count is 1
    assert 0.29 <= delay < 0.8, delay
    delay = lose_current()  # again: the count is 2
    assert 0.89 <= delay < 1.6, delay
    time.sleep(0.4)
    delay = lose_current()  # lived past STABLE_CONNECTION_S: the count is 0, no wait
    assert delay < 0.2, delay
    delay = lose_current()  # an early death again: the count is 1
    assert 0.29 <= delay < 0.8, delay

    shared_ipc.reset_ipc_client()  # the count goes back to 0
    shared_ipc.get_ipc_client()
    delay = lose_current()  # 1, not 2
    assert 0.29 <= delay < 0.8, delay


def test_the_handle_forwards_only_public_names_and_never_closes_anything(world, logs):
    handle = shared_ipc.get_ipc_client()
    [client1] = world.clients
    assert handle.number == client1.number  # a public attribute, as it is
    shared_ipc.reset_ipc_client()

    with pytest.raises(AttributeError):
        handle._connection  # never forwarded, and no connect
    assert world.connects == 1 and shared_ipc.current_client() is None

    future = handle.close()
    assert future.done() and future.result() is None
    assert world.connects == 1, "close() connected"
    assert _ipc(logs, logging.WARNING) == ["the shared Greengrass IPC client is never closed"]
    assert [client.close_calls for client in world.clients] == [0]


# --- the subscriptions: re-subscription (AC 6, R7, R10) -------------------------------------


def test_a_subscription_is_made_again_after_a_stream_error_a_stream_close_and_a_replacement(world, logs):
    with Running(world) as running:
        first = running.latest()
        client1 = first.client

        _fail_stream(first)
        assert running.wait_activated(2)
        second = running.latest()
        assert second is not first and second.client is client1

        assert settle([client1.submit(second.deliver_stream_closed)])
        assert running.wait_activated(3)
        third = running.latest()
        assert third is not second and third.client is client1

        g1 = shared_ipc.generation()
        _close_connection(client1)
        assert running.wait_activated(4)
        fourth = running.latest()
        assert shared_ipc.generation() == g1 + 1
        assert fourth.client is shared_ipc.current_client() is world.clients[1]
        assert running.errors == []

    lost = [line for line in _subscription(logs, logging.ERROR)]
    assert lost == ["Lost the IPC subscription to {} (stream error: RuntimeError)".format(TOPIC),
                    "Lost the IPC subscription to {} (stream closed)".format(TOPIC),
                    "Lost the IPC subscription to {} (stream closed)".format(TOPIC)]
    infos = _subscription(logs, logging.INFO)
    assert infos.count("Subscribing to topic {}".format(TOPIC)) == 1
    assert infos.count("Subscribed to {} again".format(TOPIC)) == 3
    assert _ipc(logs, logging.ERROR) == [_lost_line(g1, "disconnected: ConnectionResetError")]
    assert eventually(lambda: any(line.startswith("Greengrass IPC connection {} is up after".format(g1 + 1))
                                  for line in _ipc(logs, logging.INFO)), 2.0)


def test_failed_re_activations_back_off_and_a_denied_one_is_retried(world, logs):
    with Running(world) as running:
        first = running.latest()
        world.script("subscribe_to_iot_core", UnauthorizedError(message="not authorized"),
                     ServiceError(message="busy"), StreamClosedError())
        _fail_stream(first)
        assert running.wait_activated(2, timeout=5.0), "the subscription was not made again"
        attempts = world.subscribe_attempts()
        assert len(attempts) == 5
        gaps = [later.activated_at - earlier.activated_at for earlier, later in zip(attempts[1:], attempts[2:])]
        for gap, backoff in zip(gaps, SUBSCRIBE_BACKOFF_S):
            assert gap >= backoff * 0.9, (gaps, SUBSCRIBE_BACKOFF_S)
        assert running.errors == [], "a denied re-activation ended supervision"

    assert [line for line in _subscription(logs, logging.WARNING) if line.startswith("Could not subscribe")] == [
        "Could not subscribe to {} (attempt 1): UnauthorizedError; retrying in 0.02 s".format(TOPIC),
        "Could not subscribe to {} (attempt 2): ServiceError; retrying in 0.04 s".format(TOPIC),
        "Could not subscribe to {} (attempt 3): StreamClosedError; retrying in 0.08 s".format(TOPIC),
    ]
    assert _subscription(logs, logging.INFO).count("Subscribed to {} again".format(TOPIC)) == 1


def test_a_stream_closed_during_its_activation_is_subscribed_again(world, logs):
    with Running(world) as running:
        first = running.latest()
        world.script("subscribe_to_iot_core", CLOSED_DURING_ACTIVATION)
        _fail_stream(first)
        assert running.wait_activated(2)
        attempts = world.subscribe_attempts()
        assert len(attempts) == 3
        closed, again = attempts[1], attempts[2]
        delay = again.activated_at - closed.responded_at
        assert FIRST_RETRY_DELAY_S * 0.9 <= delay < FIRST_RETRY_DELAY_S + SUBSCRIBE_BACKOFF_S[0] + 0.5, delay
        assert running.latest() is again

    assert "Lost the IPC subscription to {} (stream closed during activation)".format(TOPIC) in _subscription(
        logs, logging.ERROR)


# --- the subscriptions: order and the catch-up ----------------------------------------------


def test_an_event_queued_before_a_re_activation_runs_before_that_activations_catch_up(world):
    journal = Journal()
    with Running(world, on_active=CatchUp(journal), journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        first = running.latest()
        entered = running.handler.gate("e1")
        running.deliver(first, "e1")
        assert entered.wait(2.0)
        running.deliver(first, "e2")  # queued behind e1
        _fail_stream(first)
        assert running.wait_activated(2)
        running.handler.release("e1")

        assert journal.wait_for_count(4, 2.0)
        assert journal.snapshot() == ["catch-up", "e1", "e2", "catch-up"]


def test_a_re_activations_catch_up_on_an_empty_queue_runs_with_no_further_event(world):
    journal = Journal()
    catch_up = CatchUp(journal)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        _fail_stream(running.latest())
        assert running.wait_activated(2)
        assert journal.wait_for_count(2, 1.0), "the re-activation's catch-up waited for an event"
        assert journal.snapshot() == ["catch-up", "catch-up"]
        assert catch_up.threads == ["subscription-worker"] * 2


def test_a_queued_change_then_a_re_activation_catching_up_a_newer_one_ends_with_the_newer(world, agent_world):
    """The review's C1/C2 case, with the real agent: a delta carrying C1
    (``pc-1``) is queued behind a busy handler; the stream is lost and made
    again, and that activation's catch-up reads C2 (``pc-2``) for the same
    camera. C1 runs first, then the catch-up: the camera ends with C2. (C1's
    clear fails, so the shadow keeps C2: the first clear's race is out of
    scope.)"""
    shadow, accessor, agent, handler = (agent_world.shadow, agent_world.accessor,
                                        agent_world.agent, agent_world.handler)
    with Running(world, handler=handler, on_active=agent.on_subscription_active) as running:
        first = running.latest()
        assert eventually(lambda: shadow.gets >= 1, 2.0), "setup: the first catch-up never read the shadow"
        c0, c1, c2 = _update("pc-0", "Line 0"), _update("pc-1", "Dock A"), _update("pc-2", "Dock B")
        with agent._apply_lock:  # holds the worker in the first delta
            shadow.portal_writes({"cfg-cam0": c0})
            running.deliver_document(first, _delta({"cfg-cam0": c0}, 2))
            assert handler.started.acquire(timeout=2.0), "the worker never took the first delta"
            shadow.portal_writes({"cfg-cam": c1})
            running.deliver_document(first, _delta({"cfg-cam": c1}, 3))  # queued
            shadow.portal_writes({"cfg-cam": c2})  # the Portal's newer change
            shadow.fail_clear["cfg-cam"] = 1
            _fail_stream(first)
            assert running.wait_activated(2)  # its catch-up is queued after C1
        assert handler.finished.acquire(timeout=2.0) and handler.finished.acquire(timeout=2.0)
        assert eventually(lambda: accessor.sources["cam"]["name"] == "Dock B", 2.0), (
            "the camera ended with {!r}, not C2".format(accessor.sources["cam"]["name"]))
        assert accessor.sources["cam0"]["name"] == "Line 0"
        assert accessor.calls.count(("update", "cam")) == 2
        assert shadow.failed, "setup: C1's clear did not fail"


def test_the_catch_up_applies_a_change_written_during_the_outage_exactly_once(world, agent_world):
    shadow, accessor, agent, handler = (agent_world.shadow, agent_world.accessor,
                                        agent_world.agent, agent_world.handler)
    with Running(world, handler=handler, on_active=agent.on_subscription_active) as running:
        first = running.latest()
        world.open_script = [HOLD]
        _close_connection(first.client)
        assert eventually(lambda: world.opened == 1, 2.0)
        create = _folder_create("pc-7", "Dock C")
        shadow.portal_writes({"portal-c": create})  # during the outage
        [pending] = [attempt[1] for attempt in _opens(world)]
        world.acknowledge(pending)

        assert running.wait_activated(2)
        second = running.latest()
        assert second.client is pending
        assert eventually(lambda: "Dock C" in accessor.names(), 2.0), "the catch-up did not apply the change"
        # The shadow service also delivers the change as a delta on the new stream.
        running.deliver_document(second, _delta({"portal-c": create}, 9))
        assert handler.finished.acquire(timeout=2.0)

    assert accessor.names().count("Dock C") == 1, "the change was applied more than once"
    assert accessor.calls.count(("create", "Folder")) == 1
    assert shadow.desired_changes() == {}


# --- the subscriptions: round 3 cases -------------------------------------------------------


def test_a_timed_out_activation_closes_its_operation_never_the_client_before_the_next_attempt(
        world, monkeypatch, logs):
    monkeypatch.setattr(subscription_module, "TIMEOUT", 0.1)
    world.script("subscribe_to_iot_core", NO_RESPONSE)
    with Running(world) as running:  # the first attempt times out; the second activates
        timed_out, active = world.subscribe_attempts()
        assert timed_out.close_calls == 1, "the timed-out operation was not closed"
        assert timed_out.closed_at <= active.activated_at, "it was closed after the next attempt"
        assert eventually(lambda: list(running.subscription._retained) == [2], 1.0), (
            "its on_stream_closed did not release it: {}".format(list(running.subscription._retained)))
        assert running.latest() is active
        assert [client.close_calls for client in world.clients] == [0], "a client's close() was called (N1)"
        assert running.errors == []
    [warning] = [line for line in _subscription(logs, logging.WARNING) if line.startswith("Could not subscribe")]
    assert warning == "Could not subscribe to {} (attempt 1): TimeoutError; retrying in 0.02 s".format(TOPIC)


def test_a_failed_catch_up_is_not_requested_again_while_no_stream_is_active(world):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def on_active():
        calls.append(time.monotonic())
        if len(calls) == 1:
            entered.set()
            release.wait(5.0)
            return False
        return True

    with Running(world, on_active=on_active) as running:
        assert entered.wait(2.0)
        first = running.latest()
        world.open_script = [HOLD]
        _close_connection(first.client)
        assert eventually(lambda: world.opened == 1, 2.0)
        release.set()  # the catch-up fails while no stream is active
        assert eventually(lambda: running.subscription._catch_up_failed, 1.0)
        time.sleep(6 * SLEEP_TIME)
        assert len(calls) == 1, "a failed catch-up was requested again while no stream was active"

        world.acknowledge(_opens(world)[0][1])
        assert running.wait_activated(2)
        assert eventually(lambda: len(calls) == 2, 1.0), "the next activation did not request its own catch-up"
        time.sleep(6 * SLEEP_TIME)
        assert len(calls) == 2


def test_a_catch_up_whose_get_keeps_failing_while_the_stream_is_up_forces_no_report(world, agent_world):
    """Task 30 follow-up, review item 1: while the camera-registry stream is
    up and the catch-up GET keeps failing, the supervisor runs the catch-up
    again every ``SLEEP_TIME``, and none of those retries gets a report
    written (the agent's clock is frozen, so only a reset of its report
    schedule can). The first readable GET resets it: one report, and the
    retries stop."""
    shadow, agent = agent_world.shadow, agent_world.agent
    readable = threading.Event()
    reports, catch_ups = [], []
    get, update = shadow.get_thing_shadow_state_request, shadow.update_thing_shadow_state_request

    def get_while_unreadable(thing_name, shadow_name):
        state = get(thing_name, shadow_name)
        return state if readable.is_set() else None  # the accessor's answer for an unreadable shadow

    def recording_update(thing_name, shadow_name, payload):
        if "reported" in payload:
            reports.append(time.monotonic())
        return update(thing_name, shadow_name, payload)

    def on_active():
        catch_ups.append(time.monotonic())
        return agent.on_subscription_active()

    shadow.get_thing_shadow_state_request = get_while_unreadable
    shadow.update_thing_shadow_state_request = recording_update
    agent.start()
    try:
        assert eventually(lambda: len(reports) == 1, 2.0), "setup: the start-time report was not written"
        with Running(world, handler=agent_world.handler, on_active=on_active):
            assert eventually(lambda: len(catch_ups) >= 8, 3.0), (
                "setup: the failed catch-up was not retried while the stream was up: {} catch-ups".format(
                    len(catch_ups)))
            time.sleep(2 * SLEEP_TIME)
            assert len(reports) == 1, "{} failed catch-ups got {} report(s) written".format(
                len(catch_ups), len(reports) - 1)

            readable.set()
            assert eventually(lambda: len(reports) == 2, 2.0), "the readable catch-up got no report written"
            assert eventually(lambda: not agent_world.agent._dirty, 1.0)
            retried = len(catch_ups)
            time.sleep(6 * SLEEP_TIME)
            assert len(catch_ups) == retried, "the catch-up was retried after it succeeded"
            assert len(reports) == 2
    finally:
        agent.stop()


def test_operations_stay_retained_until_their_stream_closes_with_one_warning(world, monkeypatch, logs):
    monkeypatch.setattr(subscription_module, "RETAINED_OPERATIONS_WARN", 2)
    with Running(world) as running:
        for count in range(2, 5):  # three connections lost without their streams reporting closed
            current = running.latest()
            g = shared_ipc.generation()
            current.client.mark_closed()
            current.client.notify_lifecycle("on_disconnect", ConnectionResetError("gone"))
            assert shared_ipc.wait_for_new_connection(g, 2.0)
            assert running.wait_activated(count)
        assert list(running.subscription._retained) == [1, 2, 3, 4]
        lost = _subscription(logs, logging.ERROR)
        assert len(lost) == 3 and all(line.startswith("Lost the IPC subscription to {} (IPC connection ".format(TOPIC))
                                      for line in lost)
    held = [line for line in _subscription(logs, logging.WARNING) if line.startswith("Holding")]
    assert held == ["Holding 3 operations of the IPC subscription to {} whose streams never reported "
                    "closed".format(TOPIC)]
