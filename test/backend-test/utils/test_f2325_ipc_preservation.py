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
"""Preservation for the shared Greengrass IPC connection
(rtsp-rtmp-stream-cameras task 30.4; DD-19576, Requirement 5.14, N1).

Everything here behaves the same before the fix (``ac74c0b``) and after it,
when the connection never closes:

- fifty ``get_ipc_client()`` calls and their ``new_*`` calls make one
  connect, and every operation comes from that client;
- twenty concurrent first callers make one connect;
- ``reset_ipc_client()``, then a call, makes exactly one new connect, and no
  client's ``close()`` is ever called;
- a publish denial (``UnauthorizedError``) neither reconnects nor retries, and
  its ``RuntimeError`` names the topic and ``aws.greengrass.ipc.mqttproxy``;
- a first connect that raises propagates to the caller.

Self-contained: its own fake clients behind a patched
``awsiot.greengrasscoreipc.connect`` (taking ``**kwargs``, as the fixed first
connect passes ``lifecycle_handler`` and ``timeout``). The fixed tree's
``_open_connection`` and timing constants are patched with ``raising=False``,
so on the unfixed tree they are simply unused.
"""
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

import awsiot.greengrasscoreipc  # noqa: E402
from awsiot.eventstreamrpc import ConnectionClosedError  # noqa: E402
from awsiot.greengrasscoreipc.model import UnauthorizedError  # noqa: E402

from utils import ipc_client as shared_ipc  # noqa: E402
from workflow_engine import output_bindings  # noqa: E402

TOPIC = "factory/line1/events"
GET_STATE = {"desired": {}, "reported": {}}

#: The fixed tree's timing constants, in milliseconds.
IPC_TIMINGS = (("CONNECT_TIMEOUT_S", 0.5), ("WATCHDOG_INTERVAL_S", 0.02),
               ("RECONNECT_BACKOFF_S", (0.01, 0.02, 0.04, 0.08, 0.1)), ("RETRY_WAIT_S", 0.5))


def _done_future(result=None):
    future = Future()
    future.set_result(result)
    return future


# --- the fake Greengrass IPC client (copied, so each file stands alone) ----------------


class FakeOperation:
    """One ``new_*`` operation; ``activate`` queues the response on its
    client's event-loop thread."""

    def __init__(self, client, kind):
        self.client = client
        self.kind = kind
        self.request = None
        self._response = Future()

    def activate(self, request):
        self.request = request
        self.client.submit(self._respond)
        return _done_future()

    def _respond(self):
        outcome = self.client.world.outcome_for(self)
        if isinstance(outcome, BaseException):
            self._response.set_exception(outcome)
        else:
            self._response.set_result(outcome)

    def get_response(self):
        return self._response

    def close(self):
        return _done_future()


class FakeIpcClient:
    """A ``GreengrassCoreIPCClient`` with one fake event-loop thread."""

    def __init__(self, world, number):
        self.world = world
        self.number = number
        self.loop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake-AwsEventLoop")
        # What the fixed watchdog reads: client._connection._synced.state.name.
        self._connection = SimpleNamespace(_synced=SimpleNamespace(state=SimpleNamespace(name="CONNECTED")))
        self.close_calls = 0

    def __repr__(self):
        return "<client {}>".format(self.number)

    def _new(self, kind):
        self.world.record(kind, self)
        return FakeOperation(self, kind)

    def new_get_thing_shadow(self):
        return self._new("get_thing_shadow")

    def new_update_thing_shadow(self):
        return self._new("update_thing_shadow")

    def new_publish_to_iot_core(self):
        return self._new("publish_to_iot_core")

    def new_list_components(self):
        return self._new("list_components")

    def close(self):
        """The SDK client's close(): no code may call it (N1)."""
        self.close_calls += 1
        return _done_future()

    def submit(self, task):
        try:
            return self.loop.submit(task)
        except RuntimeError:  # the loop is shut down: the test is over
            return None


class FakeIpcWorld:
    """``awsiot.greengrasscoreipc.connect`` (and the fixed tree's
    ``_open_connection``) over numbered fake clients; the journal records every
    ``new_*`` call as ``(kind, client)``."""

    def __init__(self):
        self.clients = []
        self.connects = 0
        self.opened = 0
        self.journal = []
        self.connect_delay = 0.0
        self.connect_errors = []
        self.publish_errors = []
        self._lock = threading.Lock()
        self._shut = False

    def record(self, kind, client):
        with self._lock:
            self.journal.append((kind, client))

    def _new_client(self):
        with self._lock:
            if self._shut:
                raise ConnectionRefusedError("the fake Nucleus is gone: the test is over")
            client = FakeIpcClient(self, len(self.clients) + 1)
            self.clients.append(client)
        return client

    def connect(self, **kwargs):
        with self._lock:
            self.connects += 1
            error = self.connect_errors.pop(0) if self.connect_errors else None
        if self.connect_delay:
            time.sleep(self.connect_delay)  # widens the first callers' race
        if error is not None:
            raise error
        return self._new_client()

    def open_connection(self, lifecycle):
        with self._lock:
            self.opened += 1
        return self._new_client(), _done_future()

    def outcome_for(self, operation):
        if operation.kind == "get_thing_shadow":
            return SimpleNamespace(payload=json.dumps({"state": GET_STATE}).encode("utf-8"))
        if operation.kind == "publish_to_iot_core":
            with self._lock:
                error = self.publish_errors.pop(0) if self.publish_errors else None
            if error is not None:
                return error
        return SimpleNamespace(payload=b"{}")

    def clients_of(self, kind):
        with self._lock:
            return [client for k, client in self.journal if k == kind]

    def shutdown(self):
        with self._lock:
            self._shut = True
            clients = list(self.clients)
        for client in clients:
            client.loop.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def world(monkeypatch):
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


def _get(client):
    operation = client.new_get_thing_shadow()
    operation.activate(None)
    return operation.get_response().result(5.0)


# --- one connection, reused -------------------------------------------------------------


def test_fifty_calls_make_one_connect_and_every_operation_comes_from_it(world):
    for _ in range(50):
        _get(shared_ipc.get_ipc_client())

    assert (world.connects, world.opened) == (1, 0)
    [client] = world.clients
    assert world.clients_of("get_thing_shadow") == [client] * 50


def test_twenty_concurrent_first_callers_make_one_connect(world):
    world.connect_delay = 0.05
    start = threading.Barrier(20)
    errors = []

    def first_caller():
        try:
            start.wait(5.0)
            _get(shared_ipc.get_ipc_client())
        except Exception as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [threading.Thread(target=first_caller, name="f2325-first-caller") for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert errors == [] and not any(thread.is_alive() for thread in threads)
    assert (world.connects, world.opened) == (1, 0)
    [client] = world.clients
    assert world.clients_of("get_thing_shadow") == [client] * 20


def test_a_reset_then_a_call_makes_exactly_one_new_connect(world):
    _get(shared_ipc.get_ipc_client())
    shared_ipc.reset_ipc_client()
    _get(shared_ipc.get_ipc_client())
    _get(shared_ipc.get_ipc_client())

    assert world.connects + world.opened == 2
    first, second = world.clients
    assert world.clients_of("get_thing_shadow") == [first, second, second]
    assert [client.close_calls for client in world.clients] == [0, 0], "a client's close() was called (N1)"


# --- what is not a lost connection ------------------------------------------------------


def test_a_publish_denial_neither_reconnects_nor_retries(world):
    world.publish_errors = [UnauthorizedError(message="denied")]

    raised = None
    try:
        output_bindings._default_greengrass_publisher(TOPIC, "payload", 1)
    except Exception as error:  # noqa: BLE001 - asserted below
        raised = error

    assert isinstance(raised, RuntimeError) and not isinstance(raised, ConnectionClosedError), repr(raised)
    assert TOPIC in str(raised) and "aws.greengrass.ipc.mqttproxy" in str(raised)
    assert world.connects + world.opened == 1
    assert len(world.clients_of("publish_to_iot_core")) == 1, "the denied publish was retried"

    output_bindings._default_greengrass_publisher(TOPIC, "payload", 1)
    assert world.connects + world.opened == 1, "a denial must not drop the shared connection"


def test_a_first_connect_that_raises_propagates(world):
    failure = ConnectionRefusedError("the Nucleus refused the IPC connection")
    world.connect_errors = [failure]

    raised = None
    try:
        shared_ipc.get_ipc_client()
    except Exception as error:  # noqa: BLE001 - asserted below
        raised = error

    assert raised is failure
    assert (world.connects, world.opened, world.clients) == (1, 0, [])
