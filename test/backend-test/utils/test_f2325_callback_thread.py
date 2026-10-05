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
"""Bug condition for finding 23 on the IPC callback thread
(rtsp-rtmp-stream-cameras task 30.2; Requirement 5.13, AC 1).

The awsiot v1 SDK delivers stream events (``eventstreamrpc.py:753``) and
operation responses (``:741``) on the connection's one event-loop thread. A
shadow handler that runs on that callback and makes its own blocking IPC call
waits for a response the same thread can deliver only after the callback
returns, so the call times out (``IoTShadowAccessor.TIMEOUT``: 10 s on the
device, 1 s here). Each of the four shadow subscriptions does that on the
unfixed tree: the camera registry's clear (an UPDATE), the user-accounts
report (an UPDATE), the camera-bindings re-read (a GET) and the tuning
re-read (a GET).

``test_f23_shadow_handler_runs_off_the_ipc_callback[<subscription>]`` wraps
the real handler factory in ``mqtt.SubscriptionHandler`` over a fake client
whose one fake event-loop thread resolves every response and delivers every
stream event, then delivers one ``update/delta`` event. All four FAIL on the
unfixed tree (``ac74c0b``) on their first assertion: the loop task that ran
``on_stream_event`` held the thread for about 1 s. Fixed, the callback only
enqueues (under 50 ms) and the handler's call completes within 200 ms of the
delivery.

Self-contained, so the same file runs on the base and on the worktree
(plan P3):

- the unfixed ``SubscriptionHandler`` binds ``utils.server_setup.ipc_client``
  at import: a ``utils.server_setup`` stub carries the fake, and
  ``mqtt.SubscriptionHandler`` is imported again under it, for this test only;
- the fixed one reads ``utils.ipc_client`` at call time:
  ``reset_ipc_client()`` and a patched ``awsiot.greengrasscoreipc.connect``
  (taking ``**kwargs``) hand it the same fake client.
"""
import concurrent.futures
import importlib
import json
import logging
import sys
import threading
import time
import types
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import awsiot.greengrasscoreipc
import utils as utils_package
from utils import ipc_client as shared_ipc
from dao.iotshadow import IoTShadowAccessor as iot_shadow_module
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor
from camera_sync.agent import delta_topic_prefix as camera_registry_prefix
from camera_sync.agent import make_shadow_stream_handler as camera_registry_handler
from user_accounts_sync.agent import delta_topic_prefix as user_accounts_prefix
from user_accounts_sync.agent import make_shadow_stream_handler as user_accounts_handler
from workflow_engine.camera_binding_store import bindings_delta_topic_prefix, make_bindings_shadow_handler
from workflow_engine.tuning.job_runner import make_tuning_shadow_handler, tuning_delta_topic_prefix

logger = logging.getLogger(__name__)

THING = "thing-f2325"
#: The state every fake GET answers with.
GET_STATE = {"desired": {"bindings": {}, "jobs": {}}, "reported": {}}
#: The callback budget, and the delivery-to-completion budget, of AC 1.
CALLBACK_BUDGET_S = 0.05
CALL_BUDGET_S = 0.2


# --- the fake Greengrass IPC client (plan P4) -------------------------------------------


class FakeOperation:
    """One ``new_*`` operation of :class:`FakeIpcClient`. ``activate`` queues
    the response on the client's event-loop thread, as the SDK does."""

    def __init__(self, client, kind, stream_handler=None):
        self.client = client
        self.kind = kind
        self.stream_handler = stream_handler
        self.request = None
        self.responded = threading.Event()
        self._response = Future()

    def activate(self, request):
        self.request = request
        self.client.submit(self._respond)
        flushed = Future()
        flushed.set_result(None)
        return flushed

    def _respond(self):
        self._response.set_result(self.client.response_for(self.kind))
        self.responded.set()

    def get_response(self):
        return self._response

    def close(self):
        """Close the operation's stream: its ``on_stream_closed`` follows on
        the event-loop thread."""
        if self.stream_handler is not None:
            self.client.submit(self._stream_closed)
        closed = Future()
        closed.set_result(None)
        return closed

    def _stream_closed(self):
        try:
            self.stream_handler.on_stream_closed()
        except Exception:  # noqa: BLE001 - the SDK logs and swallows it too
            logger.exception("on_stream_closed raised")


class FakeIpcClient:
    """A ``GreengrassCoreIPCClient`` with one fake event-loop thread: every
    response is resolved, and every stream event delivered, by a task on that
    thread (``eventstreamrpc.py:741`` and ``:753``). So a call made from a
    stream callback cannot complete until the callback returns, and its
    ``fut.result(timeout)`` raises ``TimeoutError``."""

    def __init__(self, number):
        self.number = number
        self.loop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake-AwsEventLoop")
        # What the fixed watchdog reads: client._connection._synced.state.name.
        self._connection = SimpleNamespace(_synced=SimpleNamespace(state=SimpleNamespace(name="CONNECTED")))
        self.operations = []
        self.close_calls = 0
        self._lock = threading.Lock()

    def _new(self, kind, stream_handler=None):
        operation = FakeOperation(self, kind, stream_handler)
        with self._lock:
            self.operations.append(operation)
        return operation

    def new_get_thing_shadow(self):
        return self._new("get_thing_shadow")

    def new_update_thing_shadow(self):
        return self._new("update_thing_shadow")

    def new_publish_to_iot_core(self):
        return self._new("publish_to_iot_core")

    def new_subscribe_to_iot_core(self, stream_handler):
        return self._new("subscribe_to_iot_core", stream_handler)

    def close(self):
        self.close_calls += 1
        closed = Future()
        closed.set_result(None)
        return closed

    def response_for(self, kind):
        if kind == "get_thing_shadow":
            return SimpleNamespace(payload=json.dumps({"state": GET_STATE}).encode("utf-8"))
        if kind == "update_thing_shadow":
            return SimpleNamespace(payload=b'{"state": {}}')
        return SimpleNamespace()

    def submit(self, task):
        try:
            return self.loop.submit(task)
        except RuntimeError:  # the loop is shut down: the test is over
            return None

    def subscriptions(self):
        """The subscribe operations whose response the loop delivered."""
        with self._lock:
            return [op for op in self.operations
                    if op.kind == "subscribe_to_iot_core" and op.responded.is_set()]

    def deliver(self, operation, event):
        """Deliver one stream event on the event-loop thread. The future
        resolves to ``(started, returned, error)`` of that loop task."""
        def _on_stream_event():
            started, error = time.monotonic(), None
            try:
                operation.stream_handler.on_stream_event(event)
            except Exception as exc:  # noqa: BLE001 - the SDK logs and swallows it
                error = exc
            return started, time.monotonic(), error
        return self.loop.submit(_on_stream_event)


class FakeIpcWorld:
    """``awsiot.greengrasscoreipc.connect`` over numbered fake clients. The
    unfixed client calls it with no arguments, the fixed one with
    ``lifecycle_handler`` and ``timeout``."""

    def __init__(self):
        self.clients = []
        self._shut = False

    def connect(self, **kwargs):
        if self._shut:
            raise ConnectionRefusedError("the fake Nucleus is gone: the test is over")
        client = FakeIpcClient(len(self.clients) + 1)
        self.clients.append(client)
        return client

    def shutdown(self):
        self._shut = True
        for client in self.clients:
            client.loop.shutdown(wait=False, cancel_futures=True)


# --- what the handlers call ------------------------------------------------------------


class RecordingPublisher:
    """``PublishHandler``'s surface: records ``(time, topic, message)``."""

    def __init__(self):
        self.published = []
        self._cond = threading.Condition()

    def publish_message(self, topic, message):
        with self._cond:
            self.published.append((time.monotonic(), topic, message))
            self._cond.notify_all()

    def wait_for(self, topic, count, timeout):
        with self._cond:
            return self._cond.wait_for(
                lambda: sum(1 for _, t, _ in self.published if t == topic) >= count, timeout)


class ShadowCall:
    """The handler's own blocking IPC call: the real ``IoTShadowAccessor``
    on the subscription's client, as the four agents make it."""

    def __init__(self, client, kind):
        self.accessor = IoTShadowAccessor(client)
        self.kind = kind
        self.done = threading.Event()
        self.returned_at = None
        self.result = None
        self.error = None

    def run(self, shadow_name, update=None):
        try:
            if self.kind == "UPDATE":
                self.result = self.accessor.update_thing_shadow_state_request(THING, shadow_name, update)
            else:
                self.result = self.accessor.get_thing_shadow_state_request(THING, shadow_name)
        except Exception as error:  # noqa: BLE001 - recorded for the assertion
            self.error = error
        finally:
            self.returned_at = time.monotonic()
            self.done.set()

    @property
    def succeeded(self):
        """No exception, and (the GET swallows its errors into None) the
        GET's state."""
        if self.error is not None:
            return False
        return self.result == GET_STATE if self.kind == "GET" else self.result is not None


class CameraRegistryAgent:
    """The Edge_Sync_Agent's surface for ``make_shadow_stream_handler``; its
    ``on_delta`` makes the clear's UPDATE (``_clear_desired_entries``)."""

    shadow_name = "dda-camera-registry"

    def __init__(self, call):
        self.thing_name = THING
        self.call = call

    def on_delta(self, message):
        self.call.run(self.shadow_name, {"desired": {"changes": {"portal-a": None}}})


class UserAccountsAgent:
    """The user-accounts agent's surface; ``on_delta`` makes the reported
    UPDATE."""

    shadow_name = "dda-user-accounts"

    def __init__(self, call):
        self.thing_name = THING
        self.call = call

    def on_delta(self, message):
        self.call.run(self.shadow_name, {"reported": {"syncVersion": 7}})


class WorkflowWatcher:
    """The WorkflowWatcher's surface for ``make_bindings_shadow_handler``;
    ``on_bindings_delta`` re-reads the bindings shadow with a GET."""

    def __init__(self, call):
        self.binding_store = SimpleNamespace(thing_name=THING, shadow_name="dda-camera-bindings")
        self.call = call

    def on_bindings_delta(self, message):
        self.call.run(self.binding_store.shadow_name)


class TuningRunner:
    """The tuning JobRunner's surface for ``make_tuning_shadow_handler``;
    ``on_delta`` re-reads ``desired.jobs`` with a GET."""

    shadow_name = "dda-workflow-tuning"

    def __init__(self, call):
        self.thing_name = THING
        self.call = call

    def on_delta(self, message):
        self.call.run(self.shadow_name)


def _camera_registry(call):
    owner = CameraRegistryAgent(call)
    return camera_registry_handler(owner), camera_registry_prefix(owner.thing_name, owner.shadow_name)


def _user_accounts(call):
    owner = UserAccountsAgent(call)
    return user_accounts_handler(owner), user_accounts_prefix(owner.thing_name, owner.shadow_name)


def _camera_bindings(call):
    owner = WorkflowWatcher(call)
    store = owner.binding_store
    return make_bindings_shadow_handler(owner), bindings_delta_topic_prefix(store.thing_name, store.shadow_name)


def _tuning(call):
    owner = TuningRunner(call)
    return make_tuning_shadow_handler(owner), tuning_delta_topic_prefix(owner.thing_name, owner.shadow_name)


#: subscription -> (wiring, the handler's IPC call)
SUBSCRIPTIONS = {
    "camera-registry": (_camera_registry, "UPDATE"),
    "user-accounts": (_user_accounts, "UPDATE"),
    "camera-bindings": (_camera_bindings, "GET"),
    "tuning": (_tuning, "GET"),
}


# --- routing (plan P3) ------------------------------------------------------------------


def _import_fresh(monkeypatch, name):
    """Import ``name`` again under this test's stubs; the session's module (or
    its absence) comes back at teardown."""
    package_name, _, attribute = name.rpartition(".")
    package = importlib.import_module(package_name)
    monkeypatch.setattr(package, attribute, getattr(package, attribute, None), raising=False)
    monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
    del sys.modules[name]
    return importlib.import_module(name)


@pytest.fixture
def routed(monkeypatch):
    """``(world, mqtt.SubscriptionHandler module)``: one fake client, the
    world's first, that both the unfixed and the fixed
    ``SubscriptionHandler`` reach."""
    monkeypatch.setattr(iot_shadow_module, "TIMEOUT", 1)
    world = FakeIpcWorld()
    shared_ipc.reset_ipc_client()
    monkeypatch.setattr(awsiot.greengrasscoreipc, "connect", world.connect)
    try:
        stub = types.ModuleType("utils.server_setup")
        stub.ipc_client = shared_ipc.get_ipc_client()  # as server_setup does at import
        monkeypatch.setitem(sys.modules, "utils.server_setup", stub)
        monkeypatch.setattr(utils_package, "server_setup", stub, raising=False)
        yield world, _import_fresh(monkeypatch, "mqtt.SubscriptionHandler")
    finally:
        shared_ipc.reset_ipc_client()
        world.shutdown()


def _subscribe(subscription, errors):
    try:
        subscription.subscribe()
    except Exception as error:  # noqa: BLE001 - reported by the test
        errors.append(error)


def _delta_event(prefix):
    """A ``SubscriptionResponseMessage``-shaped ``update/delta`` event."""
    payload = json.dumps({"state": {"changes": {"portal-a": None}}, "version": 7}).encode("utf-8")
    return SimpleNamespace(message=SimpleNamespace(topic_name=prefix + "delta", payload=payload))


# --- the bug condition -------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SUBSCRIPTIONS))
def test_f23_shadow_handler_runs_off_the_ipc_callback(name, routed):
    world, subscription_module = routed
    assert len(world.clients) == 1, "setup: expected one connect, got {}".format(len(world.clients))
    client = world.clients[0]
    wiring, call_kind = SUBSCRIPTIONS[name]
    call = ShadowCall(client, call_kind)
    handler, prefix = wiring(call)
    publisher = RecordingPublisher()
    subscription = subscription_module.SubscriptionHandler(prefix, handler, publisher)
    errors = []
    thread = threading.Thread(target=_subscribe, args=(subscription, errors),
                              name="f2325-subscribe-" + name, daemon=True)
    thread.start()
    try:
        assert publisher.wait_for(prefix + "get", 1, timeout=2.0) and client.subscriptions(), (
            "setup: the {} subscription never activated (errors: {})".format(name, errors))
        operation = client.subscriptions()[-1]

        delivered_at = time.monotonic()
        delivery = client.deliver(operation, _delta_event(prefix))
        concurrent.futures.wait([delivery], timeout=10.0)
        assert delivery.done(), "the {} stream callback did not return within 10 s".format(name)
        started, returned, error = delivery.result()
        held = returned - started
        assert held < CALLBACK_BUDGET_S, (
            "the {} handler held the IPC event-loop thread for {:.3f} s (budget {} s): it runs "
            "on the stream callback, so its own {} waited out the IoTShadowAccessor timeout "
            "(finding 23)".format(name, held, CALLBACK_BUDGET_S, call_kind))
        assert error is None, "the {} stream callback raised {!r}".format(name, error)

        call.done.wait(2.0)
        returned_at = call.returned_at
        elapsed = float("inf") if returned_at is None else returned_at - delivered_at
        assert call.done.is_set() and call.succeeded and elapsed < CALL_BUDGET_S, (
            "the {} handler's {} did not complete within {} s of the delivery: done={}, "
            "error={!r}, result={!r}, after {:.3f} s (finding 23)".format(
                name, call_kind, CALL_BUDGET_S, call.done.is_set(), call.error, call.result,
                elapsed))
    finally:
        subscription.close()
