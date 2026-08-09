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
"""Example tests for the OPC UA Liveness_Watchdog and fallback branches
(task 8.5).

Feature: trigger-activation-runtime — design C6's
:class:`OpcuaSubscribeWorker` exercised through its injection seams
(``client_factory`` for a scripted stub opcua client, ``waiter`` for the
watchdog/poll interval waits — no opcua package, no network, no real
sleeps beyond millisecond cadences):

(a) each of the spike's four keepalive loss signals (TimeoutError /
    BrokenPipeError / ConnectionError / CancelledError) plus a generic
    Exception → FULL session teardown (subscription delete + client
    disconnect attempted) and ``on_connection_lost`` routed exactly once
    (Requirement 8.4);
(b) subscription-setup failure (``create_subscription`` and, separately,
    ``subscribe_data_change``) on a connected session → Polling_Fallback:
    health mechanism ``poll`` with ``autoFallback: true``, state
    ``polling``, the transition logged naming the trigger node, and the
    poll loop firing on value change (Requirement 8.5);
(c) a rebuilt session (``reconnect()`` after a fallback) ALWAYS attempts
    the true subscription first: when subscription setup now succeeds the
    worker restores the subscribe mechanism (autoFallback cleared,
    ``restored_state() == 'subscribed'``); when it still fails the worker
    falls back again (``restored_state() == 'polling'``)
    (Requirement 8.6);
(d) ``stop()`` never signals loss — the watchdog is cancelled cleanly.

Requirements: 8.4, 8.5, 8.6
"""
import logging
import threading
import time
from concurrent.futures import CancelledError

import pytest

from workflow_engine.trigger_runtime import (
    HEALTH_POLLING,
    HEALTH_SUBSCRIBED,
    MECHANISM_POLL,
    MECHANISM_SUBSCRIBE,
    OPCUA_SERVER_STATUS_NODE,
    OpcuaSubscribeWorker,
    TriggerHealth,
)

NODE_ID = "opcua-trig1"
ENDPOINT = "opc.tcp://plc.local:4840"
DATA_NODE = "ns=2;i=5"

#: Generous bound for cross-thread waits; the loops run at millisecond
#: cadence, so these never approach it.
WAIT_TIMEOUT = 5.0

#: Settle window after an expected event, long enough for several loop
#: iterations at the 1 ms test cadence — used to assert nothing MORE
#: happened (exactly-once loss, exactly-one firing).
SETTLE_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Scripted stub opcua client (the ``client_factory`` seam)
# ---------------------------------------------------------------------------


class StubNode:
    """One node handle; reads route back to the owning client's script."""

    def __init__(self, client, node_id):
        self._client = client
        self.node_id = node_id

    def get_value(self):
        return self._client.read_value(self.node_id)


class StubSubscription:
    def __init__(self, client):
        self._client = client
        self.subscribed_nodes = []
        self.delete_calls = 0

    def subscribe_data_change(self, node):
        if self._client.subscribe_data_change_error is not None:
            raise self._client.subscribe_data_change_error
        self.subscribed_nodes.append(node)

    def delete(self):
        self.delete_calls += 1


class ScriptedOpcuaClient:
    """Stub python-opcua client: security/connect calls recorded,
    subscription setup failable on demand, node reads scripted.

    - ``keepalive_errors``: exceptions the server-status node read raises
      in order (then reads succeed).
    - ``value_script``: values the data node returns in order; when
      exhausted, the last value repeats (so a poll loop settles without
      further firings).
    - ``create_subscription_error`` / ``subscribe_data_change_error``:
      raised by the respective setup call while set.
    """

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.security_calls = []
        self.create_subscription_calls = []
        self.subscriptions = []
        self.create_subscription_error = None
        self.subscribe_data_change_error = None
        self.keepalive_errors = []
        self.value_script = []
        self._last_value = None
        self._lock = threading.Lock()

    # -- session lifecycle -------------------------------------------------

    def connect(self):
        self.connect_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1

    def set_user(self, username):
        self.security_calls.append(("set_user", username))

    def set_password(self, password):
        self.security_calls.append(("set_password", password))

    def set_security_string(self, value):
        self.security_calls.append(("set_security_string", value))

    # -- subscription setup --------------------------------------------------

    def create_subscription(self, interval_ms, handler):
        self.create_subscription_calls.append((interval_ms, handler))
        if self.create_subscription_error is not None:
            raise self.create_subscription_error
        subscription = StubSubscription(self)
        self.subscriptions.append(subscription)
        return subscription

    # -- node reads ----------------------------------------------------------

    def get_node(self, node_id):
        return StubNode(self, node_id)

    def read_value(self, node_id):
        with self._lock:
            if node_id == OPCUA_SERVER_STATUS_NODE:
                if self.keepalive_errors:
                    raise self.keepalive_errors.pop(0)
                return "running"
            if self.value_script:
                self._last_value = self.value_script.pop(0)
            return self._last_value


class ClientFactory:
    """``client_factory`` seam: builds one ScriptedOpcuaClient per
    (re)establish, applying ``configure`` to each new client, and keeps
    every built client for assertions."""

    def __init__(self, configure=None):
        self.configure = configure
        self.clients = []

    def __call__(self, endpoint):
        client = ScriptedOpcuaClient(endpoint)
        if self.configure is not None:
            self.configure(client)
        self.clients.append(client)
        return client


# ---------------------------------------------------------------------------
# Controllable waiter and recorders
# ---------------------------------------------------------------------------


class FastWaiter:
    """The ``waiter`` seam: 1 ms loop cadence until cancelled (mirrors
    the worker's event contract — True cancels the loop)."""

    def __init__(self):
        self._cancel = threading.Event()
        self.calls = 0

    def __call__(self, delay_seconds):
        self.calls += 1
        return self._cancel.wait(0.001)

    def cancel(self):
        self._cancel.set()


class DeliveryRecorder:
    def __init__(self):
        self.contexts = []
        self.fired = threading.Event()

    def __call__(self, context):
        self.contexts.append(context)
        self.fired.set()
        return True


class LossRecorder:
    def __init__(self):
        self.errors = []
        self.signalled = threading.Event()

    def __call__(self, error):
        self.errors.append(error)
        self.signalled.set()


class _LogRecorder(logging.Handler):
    """Captures trigger_runtime log records directly (the repo-level
    conftest repurposes ``caplog`` for class-based tests)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def runtime_log():
    logger = logging.getLogger("workflow_engine.trigger_runtime")
    handler = _LogRecorder()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def make_worker(factory, delivery=None, loss=None, parameters=None,
                waiter=None, watchdog_interval=0.001):
    params = {
        "endpoint": ENDPOINT,
        "node_id": DATA_NODE,
        "sampling_interval_ms": 100,
        "poll_interval_ms": 10,
    }
    params.update(parameters or {})
    health = TriggerHealth(NODE_ID, "opcua_subscribe")
    worker = OpcuaSubscribeWorker(
        params,
        delivery if delivery is not None else DeliveryRecorder(),
        loss if loss is not None else LossRecorder(),
        health,
        client_factory=factory,
        watchdog_interval=watchdog_interval,
        waiter=waiter,
    )
    return worker, health


# ---------------------------------------------------------------------------
# (a) Liveness_Watchdog loss signals (Requirement 8.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_type",
    [TimeoutError, BrokenPipeError, ConnectionError, CancelledError,
     RuntimeError],
    ids=["TimeoutError", "BrokenPipeError", "ConnectionError",
         "CancelledError", "generic-Exception"],
)
def test_watchdog_keepalive_failure_tears_down_and_signals_loss_once(
    error_type,
):
    """ANY keepalive read failure — the spike's four loss signals and a
    generic Exception alike — tears the ENTIRE session down (subscription
    delete + client disconnect attempted) and routes on_connection_lost
    exactly once (Requirement 8.4)."""
    error = error_type("keepalive died")

    def configure(client):
        client.keepalive_errors.append(error)

    factory = ClientFactory(configure)
    loss = LossRecorder()
    waiter = FastWaiter()
    worker, health = make_worker(factory, loss=loss, waiter=waiter)
    worker.start()
    assert health.state == HEALTH_SUBSCRIBED

    assert loss.signalled.wait(WAIT_TIMEOUT), (
        "watchdog never routed the keepalive failure to on_connection_lost"
    )
    time.sleep(SETTLE_SECONDS)  # several loop cadences: nothing more fires
    assert loss.errors == [error]  # exactly once, the original error

    client = factory.clients[0]
    assert client.subscriptions[0].delete_calls == 1  # full teardown:
    assert client.disconnect_calls == 1  # subscription AND client released

    worker.stop()
    assert loss.errors == [error]  # stop added no further loss signal


# ---------------------------------------------------------------------------
# (b) Polling_Fallback on subscription-setup failure (Requirement 8.5)
# ---------------------------------------------------------------------------


def _assert_auto_fallback_polling(health):
    assert health.state == HEALTH_POLLING
    assert health.mechanism == MECHANISM_POLL
    assert health.to_wire()["autoFallback"] is True


def test_create_subscription_failure_falls_back_to_polling(runtime_log):
    """A failing ``create_subscription`` on a connected session enters
    Polling_Fallback on that SAME session: health mechanism ``poll`` with
    autoFallback, state ``polling``, transition logged naming the trigger
    node, and the poll loop fires on value change (Requirement 8.5)."""

    def configure(client):
        client.create_subscription_error = RuntimeError(
            "no subscription service"
        )
        client.value_script.extend([17, 17, 42])

    factory = ClientFactory(configure)
    delivery = DeliveryRecorder()
    waiter = FastWaiter()
    worker, health = make_worker(factory, delivery=delivery, waiter=waiter)
    worker.start()

    client = factory.clients[0]
    assert client.connect_calls == 1  # the session connect succeeded
    assert len(client.create_subscription_calls) == 1  # subscribe was tried
    _assert_auto_fallback_polling(health)

    transition = [r for r in runtime_log
                  if "falling back to polling" in r.getMessage()]
    assert len(transition) == 1
    assert NODE_ID in transition[0].getMessage()

    # The poll loop primes on 17, sees 17 again (no firing), then fires
    # exactly once on the 17 → 42 change.
    assert delivery.fired.wait(WAIT_TIMEOUT), "poll loop never fired"
    time.sleep(SETTLE_SECONDS)  # script exhausted: value repeats, no refire
    assert delivery.contexts == [
        {
            "endpoint": ENDPOINT,
            "node_id": DATA_NODE,
            "value": 42,
            "source_timestamp": None,
        }
    ]

    worker.stop()


def test_subscribe_data_change_failure_falls_back_to_polling(runtime_log):
    """A failing ``subscribe_data_change`` (monitored-item setup) is the
    same auto-fallback branch (Requirement 8.5)."""

    def configure(client):
        client.subscribe_data_change_error = RuntimeError(
            "monitored item rejected"
        )

    factory = ClientFactory(configure)
    waiter = FastWaiter()
    worker, health = make_worker(factory, waiter=waiter)
    worker.start()

    _assert_auto_fallback_polling(health)
    assert any("falling back to polling" in r.getMessage()
               for r in runtime_log)

    worker.stop()


# ---------------------------------------------------------------------------
# (c) Rebuilt sessions attempt the true subscription FIRST (Requirement 8.6)
# ---------------------------------------------------------------------------


def test_reconnect_after_fallback_restores_subscription_when_it_succeeds():
    """After an auto-fallback, a reconnect rebuild attempts the true
    subscription FIRST; when setup now succeeds the worker restores the
    subscribe mechanism (autoFallback cleared) and ``restored_state()``
    answers ``subscribed`` (Requirement 8.6)."""

    def configure_failing(client):
        client.create_subscription_error = RuntimeError(
            "no subscription service"
        )

    factory = ClientFactory(configure_failing)
    waiter = FastWaiter()
    worker, health = make_worker(factory, waiter=waiter)
    worker.start()
    _assert_auto_fallback_polling(health)
    assert worker.restored_state() == HEALTH_POLLING

    # The server recovered: subsequent sessions accept subscriptions.
    factory.configure = None
    assert worker.reconnect() is True

    assert len(factory.clients) == 2  # rebuilt from scratch, nothing reused
    assert factory.clients[0].disconnect_calls == 1  # old session torn down
    rebuilt = factory.clients[1]
    assert len(rebuilt.create_subscription_calls) == 1  # subscribe FIRST
    assert health.state == HEALTH_SUBSCRIBED
    assert health.mechanism == MECHANISM_SUBSCRIBE
    assert health.to_wire()["autoFallback"] is False  # cleared on restore
    assert worker.restored_state() == HEALTH_SUBSCRIBED

    worker.stop()


def test_reconnect_falls_back_again_when_subscription_still_fails():
    """When subscription setup STILL fails on the rebuilt session, the
    worker falls back to polling again and ``restored_state()`` answers
    ``polling`` (Requirement 8.6)."""

    def configure_failing(client):
        client.create_subscription_error = RuntimeError(
            "no subscription service"
        )

    factory = ClientFactory(configure_failing)
    waiter = FastWaiter()
    worker, health = make_worker(factory, waiter=waiter)
    worker.start()
    _assert_auto_fallback_polling(health)

    assert worker.reconnect() is True  # rebuilt (into polling), not failed

    assert len(factory.clients) == 2
    rebuilt = factory.clients[1]
    assert len(rebuilt.create_subscription_calls) == 1  # still tried FIRST
    _assert_auto_fallback_polling(health)
    assert worker.restored_state() == HEALTH_POLLING

    worker.stop()


# ---------------------------------------------------------------------------
# (d) stop() cancels the watchdog cleanly — no loss signal
# ---------------------------------------------------------------------------


def test_stop_cancels_parked_watchdog_without_signalling_loss():
    """``stop()`` on a healthy subscribe-mode worker cancels the parked
    watchdog immediately (event-based wait, no bare sleep) and never
    routes on_connection_lost — even with a keepalive failure scripted
    for the next read, which stop() prevents from ever happening."""

    def configure(client):
        # Would signal loss IF the watchdog ever read again after stop.
        client.keepalive_errors.append(ConnectionError("server gone"))

    factory = ClientFactory(configure)
    loss = LossRecorder()
    # Default event-based waiter with a long interval: the watchdog parks
    # on its wake event; stop() must cancel that wait promptly.
    worker, health = make_worker(
        factory, loss=loss, waiter=None, watchdog_interval=30.0
    )
    worker.start()
    assert health.state == HEALTH_SUBSCRIBED

    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < WAIT_TIMEOUT  # no 30 s park

    time.sleep(SETTLE_SECONDS)
    assert loss.errors == []  # a stopping worker's watchdog never
    # signals loss (generation/stop guard)
    client = factory.clients[0]
    assert client.subscriptions[0].delete_calls == 1  # quiet teardown ran
    assert client.disconnect_calls == 1
