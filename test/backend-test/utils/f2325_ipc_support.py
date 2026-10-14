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
"""The fake Greengrass IPC model of the task 30 tests (rtsp-rtmp-stream-cameras
30.2 and 30.4, plan P4). Not a test module: the ``test_f2325_*`` files of this
directory import it by basename (the FEAT-002 bug-condition files carry their
own copy, so they run on the unfixed tree too).

- :class:`FakeIpcClient` is a ``GreengrassCoreIPCClient`` over one fake
  connection with ONE fake event-loop thread. A task on that thread resolves
  every response (the ``get_response()`` future) and delivers every stream
  event, stream error and stream close, as the SDK does
  (``eventstreamrpc.py:741`` and ``:753``). So a call made from a callback
  cannot complete until the callback returns.
- A connection can close (:meth:`FakeIpcClient.close_connection`): every
  ``new_*`` then raises the real ``awsiot.eventstreamrpc.ConnectionClosedError``,
  its open streams get ``on_stream_closed`` on the loop thread, then its
  lifecycle handler ``on_disconnect``, and ``_connection._synced.state.name``
  reads ``"DISCONNECTED"``.
- :class:`FakeIpcWorld` stands in for ``awsiot.greengrasscoreipc.connect``
  (:meth:`FakeIpcWorld.connect`, taking ``**kwargs``) and for the connection
  factory ``_open_connection(lifecycle)`` (:meth:`FakeIpcWorld.open_connection`).
  Each fake operation records the client that made it, so "reaches the newest
  client" is checked by identity. ``outcomes[kind]`` scripts an operation's
  response for every call, :meth:`FakeIpcWorld.script` for the next calls: an
  exception instance or class is raised through its ``get_response()`` future,
  :data:`NO_RESPONSE` never answers, and :data:`CLOSED_DURING_ACTIVATION`
  closes the stream as its response arrives.
- Connect outcomes (30.4): ``open_script`` scripts the next
  ``_open_connection`` attempts (:data:`CONNECTED`, the default, acknowledged
  at once; :data:`REFUSED`, whose future fails; ``(SLOW, seconds)``,
  acknowledged later; :data:`HOLD`, acknowledged by
  :meth:`FakeIpcWorld.acknowledge`; or an exception, raised by the factory
  itself), and ``connect_script`` the next first/lazy connects (an exception,
  raised; or a delay in seconds). ``lifecycles`` keeps every lifecycle handler
  passed, in order, and ``attempts`` every connect attempt's interval.
- :func:`install` routes ``utils.ipc_client`` to a fresh world for one test;
  :func:`fast_timings` patches the reconnect and supervision timing to
  milliseconds.
- A world serves only the test's threads (:data:`OWN_THREADS`). The handle
  and the supervision follow the shared connection by design (R6, R7), so in a
  shared test process the production syncs that an earlier ``server_setup``
  import started (``LocalServerBaseTestCase`` imports ``app``) would reach the
  current world too; their calls are refused (:meth:`FakeIpcWorld.admit`).
"""
import concurrent.futures
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import awsiot.greengrasscoreipc
from awsiot.eventstreamrpc import ConnectionClosedError, StreamClosedError
from awsiot.greengrasscoreipc.client import SubscribeToIoTCoreStreamHandler

from utils import ipc_client as shared_ipc

logger = logging.getLogger(__name__)

THING = "thing-f2325"
SHADOW = "dda-camera-registry"
PREFIX = "$aws/things/{}/shadow/name/{}/update/".format(THING, SHADOW)
GET_TOPIC = PREFIX + "get"
#: The state every fake GET answers with.
GET_STATE = {"desired": {}, "reported": {"cameras": {}}}
SIZE_LIMIT = 8192

#: An operation outcome: the response never arrives (until the operation closes).
NO_RESPONSE = object()
#: A subscription outcome: the response, and the stream's close, both on the
#: loop thread before the caller sees the response (``eventstreamrpc.py:741``,
#: ``:783-794``), as when the loop runs the close before the subscribing thread
#: is scheduled. The close is delivered first, so the order the caller sees is
#: deterministic.
CLOSED_DURING_ACTIVATION = object()
#: Connect outcomes of ``FakeIpcWorld.open_script``.
CONNECTED = "connected"
REFUSED = "refused"
SLOW = "slow"
HOLD = "hold"

#: The reconnect timing of the 30.4 tests, in seconds (``utils.ipc_client``).
IPC_TIMINGS = {"CONNECT_TIMEOUT_S": 0.2, "WATCHDOG_INTERVAL_S": 0.02,
               "RECONNECT_BACKOFF_S": (0.01, 0.02, 0.04, 0.08, 0.1),
               "STABLE_CONNECTION_S": 60.0, "RETRY_WAIT_S": 0.5}
#: The supervision timing of the 30.4 tests (``mqtt.SubscriptionHandler``).
#: ``STABLE_SUBSCRIPTION_S`` is 0, so every lost stream counts as stable: each
#: loss is an ERROR and waits ``FIRST_RETRY_DELAY_S``. The early-death ladder
#: (task 30 follow-up) has its own window in ``test_f2325_resubscribe_backoff.py``.
SUBSCRIPTION_TIMINGS = {"FIRST_RETRY_DELAY_S": 0.02,
                        "SUBSCRIBE_BACKOFF_S": (0.02, 0.04, 0.08, 0.16, 0.2),
                        "SLEEP_TIME": 0.05,
                        "STABLE_SUBSCRIPTION_S": 0.0}


def done_future(result=None):
    future = Future()
    future.set_result(result)
    return future


# --- the fake Greengrass IPC model ------------------------------------------------------


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
        self.close_calls = 0
        self.closed_at = None
        self._close_delivered = False
        self._response = Future()

    def activate(self, request):
        self.request = request
        self.activated_at = time.monotonic()
        self.client.submit(self._respond)
        return done_future()

    def _respond(self):
        outcome = StreamClosedError() if self.client.closed else self.client.world.outcome_for(self)
        if outcome is NO_RESPONSE:
            return
        if outcome is CLOSED_DURING_ACTIVATION:
            self.responded_at = time.monotonic()
            self.deliver_stream_closed(fail_response=False)
            self._response.set_result(SimpleNamespace())
            return
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
        self.close_calls += 1
        self.closed_at = time.monotonic()
        if self.stream_handler is not None:
            self.client.submit(self.deliver_stream_closed)
        return done_future()

    def deliver_stream_closed(self, fail_response=True):
        """On the loop thread, as ``_on_continuation_closed`` does."""
        if self._close_delivered:
            return
        self._close_delivered = True
        self.stream_open = False
        if fail_response and not self._response.done():
            self._response.set_exception(StreamClosedError())
        try:
            self.stream_handler.on_stream_closed()
        except Exception:  # noqa: BLE001 - the SDK logs and swallows it
            logger.exception("on_stream_closed raised")


class FakeIpcClient:
    """A ``GreengrassCoreIPCClient`` over one fake connection, with one fake
    event-loop thread."""

    def __init__(self, world, number, lifecycle=None):
        self.world = world
        self.number = number
        self.lifecycle = lifecycle
        self.loop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake-AwsEventLoop")
        # What the fixed watchdog reads: client._connection._synced.state.name.
        self._connection = SimpleNamespace(_synced=SimpleNamespace(state=SimpleNamespace(name="CONNECTED")))
        self.closed = False
        self.close_calls = 0
        self.operations = []
        self._lock = threading.Lock()

    def __repr__(self):
        return "<client {}>".format(self.number)

    def _new(self, kind, stream_handler=None):
        self.world.admit(kind)
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
        return done_future()

    def submit(self, task):
        try:
            return self.loop.submit(task)
        except RuntimeError:  # the loop is shut down: the test is over
            return None

    def notify_lifecycle(self, method, *args):
        if self.lifecycle is not None:
            getattr(self.lifecycle, method)(*args)

    def subscriptions(self):
        """The subscribe operations whose response the loop delivered."""
        with self._lock:
            return [op for op in self.operations
                    if op.kind == "subscribe_to_iot_core" and op.responded_at is not None]

    def deliver(self, operation, event):
        """Deliver one stream event on the event-loop thread. The future
        resolves to ``(started, returned, error)`` of that loop task."""
        def _on_stream_event():
            started, error = time.monotonic(), None
            try:
                operation.stream_handler.on_stream_event(event)
            except Exception as exc:  # noqa: BLE001 - the SDK turns it into a stream error
                error = exc
            return started, time.monotonic(), error
        return self.submit(_on_stream_event)

    def mark_closed(self, state="DISCONNECTED"):
        """The connection is gone, with no callback: every ``new_*`` raises
        ``ConnectionClosedError`` and the state reads ``state``."""
        with self._lock:
            self.closed = True
            self._connection._synced.state.name = state

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

    def fail_stream(self, operation, error):
        """A stream error on the loop thread (``eventstreamrpc.py:765-776``):
        ``on_stream_error``, and when it returns True or None the SDK closes
        the stream, whose ``on_stream_closed`` follows on the loop thread.
        The future resolves to ``on_stream_error``'s result."""
        def _stream_error():
            result = operation.stream_handler.on_stream_error(error)
            if result or result is None:
                self.submit(operation.deliver_stream_closed)
            return result

        return self.submit(_stream_error)


class FakeIpcWorld:
    """``awsiot.greengrasscoreipc.connect`` and the connection factory over
    numbered fake clients. The journal records every ``new_*`` call,
    ``(kind, client, accepted)``; ``outcomes[kind]`` and :meth:`script` script
    responses; ``open_script`` and ``connect_script`` script connects (see the
    module docstring)."""

    def __init__(self):
        self.clients = []
        self.connects = 0
        self.opened = 0
        self.journal = []
        #: ``(kind, thread name)`` of the calls refused to threads outside the test.
        self.refused = []
        self.outcomes = {}
        self.scripts = {}
        self.open_script = []
        self.connect_script = []
        self.lifecycles = []
        #: Every connect attempt: [kind ("connect" or "open"), client, started, ended or None].
        self.attempts = []
        self._held = {}
        self._timers = []
        self._lock = threading.Lock()
        self._shut = False

    def admit(self, kind):
        """Refuse a call from a thread outside the test: the production
        syncs an earlier ``server_setup`` import left running follow the
        shared connection by design (R6, R7), so in a shared test process
        they would reach this world. The refusal is a plain ``RuntimeError``,
        never a ``ConnectionClosedError``: it reports nothing, records no
        journal entry and consumes no script."""
        name = threading.current_thread().name
        if _named(name, OWN_THREADS):
            return
        with self._lock:
            self.refused.append((kind, name))
        raise RuntimeError("{} from thread {}: not a call of this test".format(kind, name))

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
        """The first and lazy connects; the fixed client passes
        ``lifecycle_handler`` and ``timeout``. ``connect_script`` entries: an
        exception is raised, a number is a delay first."""
        self.admit("connect")
        lifecycle = kwargs.get("lifecycle_handler")
        with self._lock:
            self.connects += 1
            self.lifecycles.append(lifecycle)
            outcome = self.connect_script.pop(0) if self.connect_script else None
            attempt = ["connect", None, time.monotonic(), None, threading.current_thread().name]
            self.attempts.append(attempt)
        try:
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome:
                time.sleep(outcome)
            client = self._new_client(lifecycle)
        finally:
            attempt[3] = time.monotonic()
        attempt[1] = client
        client.submit(lambda: client.notify_lifecycle("on_connect"))
        return client

    def open_connection(self, lifecycle):
        """``_open_connection(lifecycle) -> (client, connect future)``, as
        ``open_script`` says (the default: acknowledged at once)."""
        with self._lock:
            self.opened += 1
            self.lifecycles.append(lifecycle)
            outcome = self.open_script.pop(0) if self.open_script else CONNECTED
        if isinstance(outcome, BaseException):
            raise outcome
        client = self._new_client(lifecycle)
        connected = Future()
        attempt = ["open", client, time.monotonic(), None, threading.current_thread().name]
        with self._lock:
            self.attempts.append(attempt)

        def _acknowledge():
            attempt[3] = time.monotonic()
            if not connected.done():
                connected.set_result(None)
                client.notify_lifecycle("on_connect")

        def _refuse():
            attempt[3] = time.monotonic()
            client.mark_closed()  # it never connected
            connected.set_exception(ConnectionRefusedError("the fake Nucleus refused the connection"))

        if outcome == REFUSED:
            client.submit(_refuse)
        elif outcome == HOLD:
            with self._lock:
                self._held[client] = lambda: client.submit(_acknowledge)
        elif isinstance(outcome, tuple) and outcome[0] == SLOW:
            timer = threading.Timer(outcome[1], lambda: client.submit(_acknowledge))
            timer.daemon = True
            with self._lock:
                self._timers.append(timer)
            timer.start()
        else:
            client.submit(_acknowledge)
        return client, connected

    def acknowledge(self, client):
        """Acknowledge a :data:`HOLD` attempt now."""
        with self._lock:
            acknowledge = self._held.pop(client)
        acknowledge()

    def pending_attempts(self):
        """The attempts still waiting for their acknowledgement."""
        with self._lock:
            return [attempt for attempt in self.attempts if attempt[3] is None]

    def script(self, kind, *outcomes, topic=None):
        """The outcomes of the next ``kind`` operations, in order (then the
        ``outcomes[kind]`` or default answer); with ``topic``, only of the
        operations whose request names that topic."""
        with self._lock:
            self.scripts.setdefault((kind, topic), []).extend(outcomes)

    def outcome_for(self, operation):
        topic = getattr(operation.request, "topic_name", None)
        with self._lock:
            queued = self.scripts.get((operation.kind, topic)) or self.scripts.get((operation.kind, None))
            scripted = queued.pop(0) if queued else self.outcomes.get(operation.kind)
        if scripted is not None:
            return scripted() if isinstance(scripted, type) else scripted
        if operation.kind == "get_thing_shadow":
            return SimpleNamespace(payload=json.dumps({"state": GET_STATE}).encode("utf-8"))
        if operation.kind == "update_thing_shadow":
            return SimpleNamespace(payload=b'{"state": {}}')
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

    def subscribe_attempts(self, topic=None):
        """Every subscription activated, answered or not, in order; with
        ``topic``, only those to it."""
        with self._lock:
            clients = list(self.clients)
        return sorted((operation for client in clients for operation in list(client.operations)
                       if operation.kind == "subscribe_to_iot_core" and operation.activated_at is not None
                       and (topic is None or operation.request.topic_name == topic)),
                      key=lambda operation: operation.activated_at)

    def shutdown(self):
        with self._lock:
            self._shut = True
            clients = list(self.clients)
            timers, self._timers = self._timers, []
            self._held.clear()
        for timer in timers:
            timer.cancel()
            timer.join(5.0)
        for client in clients:
            client.loop.shutdown(wait=False, cancel_futures=True)


def _named(thread, names):
    return any(thread == name or (name.endswith("*") and thread.startswith(name[:-1])) for name in names)


#: The threads of a test that uses this world: the test's own (``MainThread``
#: and threads it names ``f2325-*``), the reconnect thread and the fake loops.
#: Anything else (``subscription-worker`` included: the handlers and catch-ups
#: these tests give their subscriptions make no IPC call) is refused.
OWN_THREADS = ("MainThread", "f2325-*", "ipc-reconnect", "fake-AwsEventLoop*")


def install(monkeypatch):
    """Route ``utils.ipc_client`` to a fresh :class:`FakeIpcWorld` for one
    test: ``reset_ipc_client()``, then ``connect`` and (where it exists)
    ``_open_connection`` patched. The caller ends it with :func:`uninstall`."""
    world = FakeIpcWorld()
    shared_ipc.reset_ipc_client()
    monkeypatch.setattr(awsiot.greengrasscoreipc, "connect", world.connect)
    monkeypatch.setattr(shared_ipc, "_open_connection", world.open_connection, raising=False)
    return world


def uninstall(world):
    shared_ipc.reset_ipc_client()
    world.shutdown()


def fast_timings(monkeypatch, subscription_module=None, **overrides):
    """Patch the reconnect timing of ``utils.ipc_client`` (and, given the
    module, the supervision timing of ``mqtt.SubscriptionHandler``) to
    :data:`IPC_TIMINGS` / :data:`SUBSCRIPTION_TIMINGS`, with ``overrides`` by
    name. The reconnect thread is then poked, so a wait it began before the
    patch ends now (:func:`poke_reconnect_thread`)."""
    for name, value in IPC_TIMINGS.items():
        monkeypatch.setattr(shared_ipc, name, overrides.pop(name, value))
    if subscription_module is not None:
        for name, value in SUBSCRIPTION_TIMINGS.items():
            monkeypatch.setattr(subscription_module, name, overrides.pop(name, value))
    for name, value in overrides.items():
        target = shared_ipc if hasattr(shared_ipc, name) else subscription_module
        monkeypatch.setattr(target, name, value)
    poke_reconnect_thread()


def poke_reconnect_thread():
    """Wake the ``ipc-reconnect`` thread once: it runs its watchdog now and
    waits with the current ``WATCHDOG_INTERVAL_S`` after (with no loss
    marked it does nothing else)."""
    shared_ipc._wake.set()


# --- what subscriptions call -------------------------------------------------------------


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


class RecordingStreamHandler(SubscribeToIoTCoreStreamHandler):
    """A shadow handler that records what runs, in order, into ``journal``
    (shared with an ``on_active`` when a test passes one). ``gate(name)``
    makes the event named ``name`` wait until ``release(name)``; ``raises``
    names events whose handling raises. ``on_stream_error`` returns
    ``error_result``, as the four handler factories return True."""

    def __init__(self, journal=None, error_result=True):
        super().__init__()
        self.journal = journal if journal is not None else Journal()
        self.error_result = error_result
        self.errors = []
        self.closes = 0
        self.threads = set()
        self.raises = set()
        self._gates = {}

    def gate(self, name):
        entered, release = threading.Event(), threading.Event()
        self._gates[name] = (entered, release)
        return entered

    def release(self, name):
        self._gates[name][1].set()

    def on_stream_event(self, event):
        name = event_name(event)
        self.threads.add(threading.current_thread().name)
        gate = self._gates.get(name)
        if gate is not None:
            gate[0].set()
            gate[1].wait(10)
        if name in self.raises:
            raise RuntimeError("the handler failed on {}".format(name))
        self.journal.add(name)

    def on_stream_error(self, error):
        self.errors.append(error)
        if isinstance(self.error_result, BaseException):
            raise self.error_result
        return self.error_result

    def on_stream_closed(self):
        self.closes += 1


class Journal:
    """What a subscription's worker ran, in order: event names and
    ``"catch-up"``."""

    def __init__(self):
        self.entries = []
        self._cond = threading.Condition()

    def add(self, name):
        with self._cond:
            self.entries.append(name)
            self._cond.notify_all()

    def wait_for_count(self, count, timeout=5.0):
        with self._cond:
            return self._cond.wait_for(lambda: len(self.entries) >= count, timeout)

    def snapshot(self):
        with self._cond:
            return list(self.entries)


class CatchUp:
    """An ``on_active``: records ``"catch-up"`` in the journal and the thread
    it ran on, and returns (or raises) the scripted results in turn, then
    True."""

    def __init__(self, journal, *results):
        self.journal = journal
        self.results = list(results)
        self.calls = 0
        self.threads = []

    def __call__(self):
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        self.journal.add("catch-up")
        result = self.results.pop(0) if self.results else True
        if isinstance(result, BaseException):
            raise result
        return result


def delta_event(name, prefix=PREFIX):
    """A ``SubscriptionResponseMessage``-shaped ``update/delta`` event whose
    payload names it."""
    payload = json.dumps({"state": {"name": name}}).encode("utf-8")
    return SimpleNamespace(message=SimpleNamespace(topic_name=prefix + "delta", payload=payload))


def event_name(event):
    return json.loads(event.message.payload.decode("utf-8"))["state"]["name"]


# --- waiting ----------------------------------------------------------------------------


def eventually(predicate, timeout, interval=0.01):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def settle(futures, timeout=5.0):
    """Wait for loop tasks, without raising; True when all are done."""
    futures = [future for future in futures if future is not None]
    done, _ = concurrent.futures.wait(futures, timeout=timeout)
    return len(done) == len(futures)
