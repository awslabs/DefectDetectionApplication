# Copyright 2026 Amazon Web Services, Inc.
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
"""Bug condition exploration tests for greengrass-denial-health-race.

**EXPECTED TO FAIL ON UNFIXED CODE** — the failures CONFIRM the defect
observed on ryan-orin-nano (LocalServer arm64JP6 v1.0.51, 2026-08-05):
a Greengrass ``SubscribeToIoTCore`` denial whose operation/client close
fires ``on_stream_closed`` on the SAME-generation stream handler races
``_mark_denied``. The stream-lost signal passes ``_handle_stream_lost``'s
guard (``_denied`` still False, generation current), reaches
``ReconnectEngine.on_connection_lost``, and the spawned loop's pre-attempt
``set_state(reconnecting)`` lands AFTER the worker's ``failed`` write. The
attempt returns ``RECONNECT_PARK`` and the engine parks WITHOUT restoring
health — Trigger_Health permanently surfaces ``reconnecting`` with
``reconnectAttempts: 1`` instead of ``failed`` + the actionable
accessControl denial message (Requirement 8.7 of
trigger-activation-runtime).

The existing stubs in test_mqtt_transports.py never fire the denial
teardown callback, which is why the suite is green while the device is
not. The stub IPC client here mirrors the on-device SDK behavior: the
denial raises ``UnauthorizedError`` from ``get_response().result(...)``
AND the client's ``close()`` (invoked by ``_close_quietly``) fires
``handler.on_stream_closed()``.

The engine is driven deterministically — zero threads, zero sleeps:

- ``waiter=lambda delay: False`` (the backoff wait elapses instantly);
- the DEFERRED-SPAWN harness: ``spawn`` appends the reconnect loop
  callable to a list and the test drains it AFTER ``worker.start()``
  returns (i.e. after ``_mark_denied`` completed) — pinning the exact
  on-device thread interleaving (loop's ``set_state(reconnecting)``
  after the worker's ``set_state(failed)``).

Tests:

- interleaving (iii), deferred spawn — the on-device shape. MUST FAIL on
  unfixed code (yields ``reconnecting`` / ``reconnectAttempts >= 1``).
  Property 1: Bug Condition — denial end-state deterministic under the
  raced interleaving; Property 2 (iii). **Validates: Requirements 1.1,
  1.2, 1.3, 2.1, 2.2**
- interleaving (ii), inline spawn (``spawn=lambda fn: fn()``, callback
  fired synchronously inside ``_close_quietly``). Same end-state
  assertions. NOTE: this variant alone can MASK the defect — inline
  execution lets ``_mark_denied`` land last, so it may pass on unfixed
  code; the deferred variant is the reproducing one. Property 2 (ii).
  **Validates: Requirements 2.1, 2.2**
- short-circuit re-assert — denied worker with health corrupted to
  ``reconnecting`` (as the raced loop does): ``reconnect()`` returns
  ``RECONNECT_PARK``, re-asserts ``failed`` + denial message, no new IPC
  connect. MUST FAIL on unfixed code (no re-assert exists). Property 3:
  Bug Condition — short-circuit re-assert. **Validates: Requirements
  2.3, 3.2**

Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3
"""
import sys
import types
from types import SimpleNamespace

import pytest

from workflow_engine.trigger_runtime import (
    HEALTH_FAILED,
    HEALTH_RECONNECTING,
    RECONNECT_PARK,
    GreengrassIpcSubscriber,
    ReconnectEngine,
    TriggerHealth,
    greengrass_subscribe_denial_message,
)

NODE_ID = "trig1"
TOPIC = "factory/line1/start"


# ---------------------------------------------------------------------------
# awsiot sys.modules stubs (same discipline as test_mqtt_transports.py:
# the worker imports awsiot.greengrasscoreipc lazily inside _subscribe,
# so no SDK or nucleus socket is required)
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


@pytest.fixture
def awsiot_stubs(monkeypatch):
    """Install the awsiot module family into sys.modules so the worker's
    lazy ``import awsiot.greengrasscoreipc...`` statements resolve to
    these stubs (the ipc_connect seam supplies the client itself)."""
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
# Stub IPC client mirroring the on-device SDK behavior: a denial raises
# UnauthorizedError from get_response().result(...) AND the client's
# close() fires handler.on_stream_closed() — the same-generation denial
# teardown callback the existing stubs omit.
# ---------------------------------------------------------------------------


class TeardownFuture:
    def __init__(self, error=None):
        self._error = error

    def result(self, timeout=None):
        if self._error is not None:
            raise self._error
        return SimpleNamespace()


class TeardownOperation:
    def __init__(self, error=None):
        self._error = error
        self.request = None
        self.closed = False

    def activate(self, request):
        self.request = request

    def get_response(self):
        return TeardownFuture(self._error)

    def close(self):
        self.closed = True


class TeardownFiringIpcClient:
    """One IPC client whose ``close()`` fires ``on_stream_closed`` on the
    handler captured by ``new_subscribe_to_iot_core`` — exactly what the
    real SDK did on-device during the denial teardown."""

    def __init__(self, error=None):
        self._error = error
        self.handlers = []
        self.operations = []
        self.closed = False

    def new_subscribe_to_iot_core(self, handler):
        self.handlers.append(handler)
        operation = TeardownOperation(self._error)
        self.operations.append(operation)
        return operation

    def close(self):
        self.closed = True
        # The denial teardown callback: fired on the CURRENT-generation
        # handler, while (on unfixed code) _denied is still False —
        # _close_quietly runs before _mark_denied in _subscribe's denial
        # branch.
        for handler in self.handlers:
            handler.on_stream_closed()


class TeardownFiringIpcConnect:
    """The ``ipc_connect`` seam: one TeardownFiringIpcClient per call,
    with an optional per-call error plan (None = success)."""

    def __init__(self, error_plan=()):
        self._error_plan = list(error_plan)
        self.clients = []

    def __call__(self):
        error = self._error_plan.pop(0) if self._error_plan else None
        client = TeardownFiringIpcClient(error)
        self.clients.append(client)
        return client


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def greengrass_params():
    return {"topic": TOPIC, "qos": 1, "greengrass": True}


def build_worker_with_engine(connect, spawn):
    """A real ReconnectEngine (non-blocking waiter, injected spawn seam)
    wired to a GreengrassIpcSubscriber exactly as the manager wires them:
    the worker's on_connection_lost is the engine's entry point and the
    engine late-binds the worker's reconnect callable."""
    health = TriggerHealth(NODE_ID, "mqtt_subscribe")
    engine = ReconnectEngine(
        node_id=NODE_ID,
        target=TOPIC,
        health=health,
        retry_limit=0,  # retry forever — the denial must park regardless
        waiter=lambda delay: False,  # backoff elapses, never cancelled
        spawn=spawn,
    )
    worker = GreengrassIpcSubscriber(
        greengrass_params(),
        lambda context: True,  # on_delivery (unused: the denial precedes)
        engine.on_connection_lost,
        health,
        ipc_connect=connect,
    )
    engine.bind(lambda: worker.reconnect())
    return worker, engine, health


# ---------------------------------------------------------------------------
# Interleaving (iii): deferred spawn — the on-device shape.
# MUST FAIL on unfixed code.
# ---------------------------------------------------------------------------


def test_deferred_spawn_denial_end_state_is_failed_with_denial_message(
    awsiot_stubs,
):
    """Property 1: Bug Condition — denial end-state is deterministic under
    the raced interleaving; Property 2 interleaving (iii).

    The denial teardown fires on_stream_closed on the same-generation
    handler during _close_quietly (before _mark_denied); the engine's
    reconnect loop is captured by the deferred-spawn harness and drained
    AFTER worker.start() returns — i.e. after _mark_denied's ``failed``
    write — so the loop's pre-attempt ``reconnecting`` write lands last,
    exactly as on-device. Once everything settles the surfaced health
    must be ``failed`` with the actionable denial message and the denial
    must not have counted a reconnect attempt.

    UNFIXED code yields state ``reconnecting`` with reconnectAttempts 1
    and the stream-closed text as lastError — this test MUST FAIL.

    **Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2**
    """
    connect = TeardownFiringIpcConnect(
        error_plan=[StubUnauthorizedError("denied")]
    )
    spawned = []  # the deferred-spawn harness: capture, don't run
    worker, engine, health = build_worker_with_engine(
        connect, spawn=spawned.append
    )

    worker.start()  # denial: must not raise; teardown callback fires inside

    # Drain any captured reconnect loop AFTER _mark_denied completed —
    # the deterministic stand-in for the on-device daemon thread whose
    # set_state(reconnecting) landed after the worker's set_state(failed).
    for loop in spawned:
        loop()

    wire = health.to_wire()
    assert health.state == HEALTH_FAILED, (
        "denial end-state must be 'failed', got "
        f"{wire['state']!r} (lastError={wire['lastError']!r}, "
        f"reconnectAttempts={wire['reconnectAttempts']})"
    )
    assert wire["lastError"] == greengrass_subscribe_denial_message(TOPIC)
    # The denial is not a genuine connection loss: nothing may be counted
    # against retry_limit (on unfixed code the raced loop records 1).
    assert wire["reconnectAttempts"] == 0
    # The engine either never saw the denial teardown (loop never spawned)
    # or parked on it — it must not be left mid-reconnect.
    assert engine.parked or not spawned


# ---------------------------------------------------------------------------
# Interleaving (ii): inline spawn — callback fired synchronously inside
# _close_quietly. NOTE: this variant ALONE can mask the defect (inline
# execution lets _mark_denied land last); the deferred variant above is
# the reproducing one. Kept for interleaving universality.
# ---------------------------------------------------------------------------


def test_inline_spawn_denial_end_state_is_failed_with_denial_message(
    awsiot_stubs,
):
    """Property 2: Bug Condition — interleaving universality, variant (ii).

    Same denial teardown callback, but the engine loop runs inline
    (``spawn=lambda fn: fn()``) — synchronously inside _close_quietly,
    BEFORE _mark_denied. The end-state must be identical: ``failed`` +
    denial message. (On unfixed code this variant may pass because
    _mark_denied lands last — documenting exactly why the deferred-spawn
    harness is the load-bearing reproduction detail.)

    **Validates: Requirements 2.1, 2.2**
    """
    # Two denials: the start subscribe, and the raced-in inline reconnect
    # attempt's re-subscribe (unfixed code reaches it before _denied is
    # set; fixed code never spawns the loop and uses only the first).
    connect = TeardownFiringIpcConnect(
        error_plan=[
            StubUnauthorizedError("denied"),
            StubUnauthorizedError("denied"),
        ]
    )
    worker, engine, health = build_worker_with_engine(
        connect, spawn=lambda loop: loop()
    )

    worker.start()  # must not raise

    wire = health.to_wire()
    assert health.state == HEALTH_FAILED, (
        "denial end-state must be 'failed', got "
        f"{wire['state']!r} (lastError={wire['lastError']!r}, "
        f"reconnectAttempts={wire['reconnectAttempts']})"
    )
    assert wire["lastError"] == greengrass_subscribe_denial_message(TOPIC)


# ---------------------------------------------------------------------------
# Property 3: the reconnect() _denied short-circuit re-asserts the denial
# health. MUST FAIL on unfixed code (no re-assert exists).
# ---------------------------------------------------------------------------


def test_denied_short_circuit_reasserts_failed_denial_health(awsiot_stubs):
    """Property 3: Bug Condition — short-circuit re-assert.

    A denied worker whose health was overwritten to ``reconnecting`` (as
    the raced loop's pre-attempt set_state does): ``reconnect()`` must
    return RECONNECT_PARK without a new IPC connect AND re-assert the
    ``failed`` denial health so the parked end-state always surfaces the
    denial.

    UNFIXED code returns RECONNECT_PARK but leaves the corrupted
    ``reconnecting`` health standing — this test MUST FAIL.

    **Validates: Requirements 2.3, 3.2**
    """
    connect = TeardownFiringIpcConnect(
        error_plan=[StubUnauthorizedError("denied")]
    )
    health = TriggerHealth(NODE_ID, "mqtt_subscribe")
    worker = GreengrassIpcSubscriber(
        greengrass_params(),
        lambda context: True,
        lambda error: None,  # loss sink: this test isolates reconnect()
        health,
        ipc_connect=connect,
    )
    worker.start()
    assert health.state == HEALTH_FAILED  # denial marked at start

    # Corrupt the health exactly as the raced loop's pre-attempt write
    # does before invoking the attempt.
    health.set_state(
        HEALTH_RECONNECTING,
        error=f"SubscribeToIoTCore stream closed for topic '{TOPIC}'",
    )

    outcome = worker.reconnect()

    assert outcome is RECONNECT_PARK
    assert len(connect.clients) == 1  # no new IPC connect was attempted
    wire = health.to_wire()
    assert health.state == HEALTH_FAILED, (
        "the denied short-circuit must re-assert the failed denial "
        f"health, got {wire['state']!r} (lastError={wire['lastError']!r})"
    )
    assert wire["lastError"] == greengrass_subscribe_denial_message(TOPIC)
