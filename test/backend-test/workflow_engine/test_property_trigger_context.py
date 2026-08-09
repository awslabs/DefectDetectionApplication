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
"""Property test for Trigger_Context fidelity (Task 8.4).

# Feature: trigger-activation-runtime, Property 13: Trigger_Context fidelity

*For any generated delivery, the built Trigger_Context carries exactly
`{topic, payload, qos, timestamp}` (MQTT) or `{endpoint, node_id, value,
source_timestamp}` (OPC UA) with values matching the delivery; OPC UA
contexts are identical in shape under `subscribe` and `poll`; for any
generated sequence of polled values, poll-mode firings occur exactly at
positions where the value differs from the previous read; and the
dispatched execution row persists the context as `trigger_context_json`.*

Sub-properties:

1. MQTT deliveries through a stub paho transport (the worker's
   ``client_factory`` seam — generated topic, payload bytes incl.
   non-UTF8, qos) build exactly the four MQTT context keys with matching
   values.
2. OPC UA data changes through the stub subscription handler (the
   worker's ``client_factory`` + ``waiter`` seams — generated values,
   source timestamps present/absent) build exactly the four OPC UA
   context keys with matching values; subscribe- and poll-mode contexts
   are shape-identical (same key set).
3. Generated polled value sequences (with repeats) driven through the
   poll loop via a scripted stub client and a controllable waiter fire
   exactly at change positions (the first read primes, no firing).
4. A delivered context dispatched through ``default_run_starter`` with a
   file-backed sqlite session factory (no executor registered) persists
   ``trigger_context_json`` that round-trips (``json.loads``) to the
   context; a non-JSON-native value degrades to its string form.

**Validates: Requirements 6.7, 6.8**
"""
import datetime
import json
import threading
import time
import types
import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import make_session_factory

from workflow_engine import executor
from workflow_engine.models import WorkflowExecution
from workflow_engine.trigger_runtime import (
    OpcuaSubscribeWorker,
    PlainBrokerSubscriber,
    RunActivation,
    TriggerHealth,
    default_run_starter,
)

MQTT_CONTEXT_KEYS = frozenset({"topic", "payload", "qos", "timestamp"})
OPCUA_CONTEXT_KEYS = frozenset(
    {"endpoint", "node_id", "value", "source_timestamp"}
)

OPCUA_ENDPOINT = "opc.tcp://plc.local:4840"
OPCUA_NODE_ID = "ns=2;i=5"


# ---------------------------------------------------------------------------
# Stub transports (the workers' injection seams — no paho, no opcua, no
# network)
# ---------------------------------------------------------------------------


class _StubPahoClient:
    """Fits the slice of the paho client the worker uses: assignable
    callbacks, connect/loop/subscribe/disconnect all recorded no-ops."""

    def __init__(self):
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscriptions = []
        self.connected_to = None

    def connect(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))

    def disconnect(self):
        pass


class _StubOpcuaSubscription:
    def __init__(self):
        self.subscribed_nodes = []

    def subscribe_data_change(self, node):
        self.subscribed_nodes.append(node)

    def delete(self):
        pass


class _StaticNode:
    def get_value(self):
        return 0


class _StubOpcuaSubscribeClient:
    """Subscribe-mode session stub: records the handler passed to
    ``create_subscription`` so the test can push data-change
    notifications through it."""

    def __init__(self):
        self.handler = None
        self.sampling_interval = None
        self.subscription = _StubOpcuaSubscription()

    def connect(self):
        pass

    def disconnect(self):
        pass

    def create_subscription(self, interval, handler):
        self.sampling_interval = interval
        self.handler = handler
        return self.subscription

    def get_node(self, node_id):
        return _StaticNode()


class _ScriptedNode:
    """A node whose ``get_value`` returns a scripted value sequence."""

    def __init__(self, values):
        self._values = list(values)
        self.reads = 0

    @property
    def exhausted(self):
        return self.reads >= len(self._values)

    def get_value(self):
        value = self._values[self.reads]
        self.reads += 1
        return value


class _ScriptedPollClient:
    """Poll-mode session stub returning one scripted node for every
    ``get_node`` call."""

    def __init__(self, values):
        self.node = _ScriptedNode(values)

    def connect(self):
        pass

    def disconnect(self):
        pass

    def get_node(self, node_id):
        return self.node


def _make_mqtt_worker(topic_filter, qos, deliveries, client_holder):
    """One PlainBrokerSubscriber over a stub paho client (the
    ``client_factory`` seam)."""

    def client_factory(client_id):
        client = _StubPahoClient()
        client_holder.append(client)
        return client

    health = TriggerHealth("trig-mqtt", "mqtt_subscribe")
    return PlainBrokerSubscriber(
        {"topic": topic_filter, "qos": qos, "broker_host": "broker.local"},
        deliveries.append,
        lambda error: None,
        health,
        client_factory=client_factory,
    )


def _fire_subscribe_datachange(value, source_timestamp):
    """One subscribe-mode data change through the stub handler; returns
    the delivered contexts."""
    deliveries = []
    client = _StubOpcuaSubscribeClient()
    worker = OpcuaSubscribeWorker(
        {
            "endpoint": OPCUA_ENDPOINT,
            "node_id": OPCUA_NODE_ID,
            "mode": "subscribe",
        },
        deliveries.append,
        lambda error: None,
        TriggerHealth("trig-opcua", "opcua_subscribe"),
        client_factory=lambda endpoint: client,
        # Cancel the Liveness_Watchdog's first wait immediately: the
        # watchdog thread exits without ever reading (no time coupling).
        waiter=lambda delay: True,
    )
    worker.start()
    try:
        data = types.SimpleNamespace(
            monitored_item=types.SimpleNamespace(
                Value=types.SimpleNamespace(SourceTimestamp=source_timestamp)
            )
        )
        client.handler.datachange_notification(object(), value, data)
    finally:
        worker.stop()
    return deliveries


def _run_poll_sequence(values):
    """Drive the poll loop over a scripted value sequence via the
    controllable waiter; returns the delivered contexts."""
    deliveries = []
    client = _ScriptedPollClient(values)
    done = threading.Event()

    def waiter(delay):
        # Called after every poll read: cancel the loop once the script
        # is exhausted, otherwise continue immediately (no sleeping).
        if client.node.exhausted:
            done.set()
            return True
        return False

    worker = OpcuaSubscribeWorker(
        {
            "endpoint": OPCUA_ENDPOINT,
            "node_id": OPCUA_NODE_ID,
            "mode": "poll",
            "poll_interval_ms": 10,
        },
        deliveries.append,
        lambda error: None,
        TriggerHealth("trig-opcua", "opcua_subscribe"),
        client_factory=lambda endpoint: client,
        waiter=waiter,
    )
    worker.start()
    try:
        assert done.wait(timeout=5.0), "poll loop never exhausted its script"
    finally:
        worker.stop()
    assert client.node.reads == len(values)
    return deliveries


# ---------------------------------------------------------------------------
# Sub-property 1: MQTT Trigger_Context fidelity (Requirement 6.8)
# ---------------------------------------------------------------------------


# Feature: trigger-activation-runtime, Property 13: Trigger_Context fidelity
@settings(max_examples=100, deadline=None)
@given(
    topic_filter=st.text(min_size=1, max_size=40),
    delivered_topic=st.text(min_size=1, max_size=40),
    payload=st.binary(max_size=64),
    qos=st.integers(min_value=0, max_value=2),
)
def test_mqtt_delivery_builds_exact_trigger_context(
    topic_filter, delivered_topic, payload, qos
):
    """For any generated MQTT delivery (topic, payload bytes incl.
    non-UTF8, qos), the built Trigger_Context carries exactly
    {topic, payload, qos, timestamp} with values matching the delivery.

    **Validates: Requirements 6.8**
    """
    deliveries = []
    clients = []
    worker = _make_mqtt_worker(topic_filter, qos, deliveries, clients)
    worker.start()
    try:
        client = clients[0]
        # Successful connect (rc=0) issues the subscribe.
        client.on_connect(client, None, None, 0)
        assert client.subscriptions == [(topic_filter, qos)]

        before = time.time()
        message = types.SimpleNamespace(topic=delivered_topic, payload=payload)
        client.on_message(client, None, message)
        after = time.time()
    finally:
        worker.stop()

    assert len(deliveries) == 1
    context = deliveries[0]
    assert set(context.keys()) == MQTT_CONTEXT_KEYS
    assert context["topic"] == delivered_topic
    assert context["payload"] == payload.decode("utf-8", errors="replace")
    # Plain broker: qos unclamped — the effective (subscribed) qos.
    assert context["qos"] == qos
    assert isinstance(context["timestamp"], float)
    assert before <= context["timestamp"] <= after


# ---------------------------------------------------------------------------
# Sub-property 2: OPC UA Trigger_Context fidelity + shape identity
# (Requirements 6.7, 6.8)
# ---------------------------------------------------------------------------

_OPCUA_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=40),
)


# Feature: trigger-activation-runtime, Property 13: Trigger_Context fidelity
@settings(max_examples=100, deadline=None)
@given(
    value=_OPCUA_VALUES,
    source_timestamp=st.one_of(
        st.none(),
        st.datetimes(
            min_value=datetime.datetime(2000, 1, 1),
            max_value=datetime.datetime(2099, 12, 31),
        ),
    ),
)
def test_opcua_datachange_builds_exact_trigger_context(value, source_timestamp):
    """For any generated data change (value, source timestamp present or
    absent), the subscribe-mode Trigger_Context carries exactly
    {endpoint, node_id, value, source_timestamp} with values matching
    the delivery (source_timestamp ISO-8601 when supplied, else None).

    **Validates: Requirements 6.8**
    """
    deliveries = _fire_subscribe_datachange(value, source_timestamp)

    assert len(deliveries) == 1
    context = deliveries[0]
    assert set(context.keys()) == OPCUA_CONTEXT_KEYS
    assert context["endpoint"] == OPCUA_ENDPOINT
    assert context["node_id"] == OPCUA_NODE_ID
    assert context["value"] == value
    if source_timestamp is None:
        assert context["source_timestamp"] is None
    else:
        assert context["source_timestamp"] == source_timestamp.isoformat()


def test_subscribe_and_poll_contexts_are_shape_identical():
    """The OPC UA Trigger_Context under `subscribe` and under `poll` has
    the identical key set (Requirement 6.8 shape consistency).

    **Validates: Requirements 6.7, 6.8**
    """
    subscribe_contexts = _fire_subscribe_datachange(42, None)
    poll_contexts = _run_poll_sequence([1, 2])

    assert len(subscribe_contexts) == 1
    assert len(poll_contexts) == 1
    assert set(subscribe_contexts[0].keys()) == set(poll_contexts[0].keys())
    assert set(poll_contexts[0].keys()) == OPCUA_CONTEXT_KEYS


# ---------------------------------------------------------------------------
# Sub-property 3: poll-mode firings occur exactly at change positions
# (Requirement 6.7)
# ---------------------------------------------------------------------------


# Feature: trigger-activation-runtime, Property 13: Trigger_Context fidelity
@settings(max_examples=100, deadline=None)
@given(
    values=st.lists(
        # A small alphabet forces repeats (no-fire positions) often.
        st.sampled_from([0, 1, 2, "high", "low"]),
        min_size=1,
        max_size=8,
    )
)
def test_poll_mode_fires_exactly_at_value_changes(values):
    """For any generated polled value sequence, the poll loop fires
    exactly at the positions where the value differs from the previous
    read (the first read primes and never fires), each firing carrying
    the poll-shape Trigger_Context with source_timestamp None.

    **Validates: Requirements 6.7, 6.8**
    """
    deliveries = _run_poll_sequence(values)

    expected = [
        values[i] for i in range(1, len(values)) if values[i] != values[i - 1]
    ]
    assert [context["value"] for context in deliveries] == expected
    for context in deliveries:
        assert set(context.keys()) == OPCUA_CONTEXT_KEYS
        assert context["endpoint"] == OPCUA_ENDPOINT
        assert context["node_id"] == OPCUA_NODE_ID
        assert context["source_timestamp"] is None


# ---------------------------------------------------------------------------
# Sub-property 4: the dispatched execution row persists the context as
# trigger_context_json (Requirement 6.8)
# ---------------------------------------------------------------------------

_session_factory = None


def _shared_session_factory():
    """One file-backed sqlite database shared across examples (rows are
    isolated per example by a unique registration id)."""
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    return _session_factory


def _start_run_and_load_row(context):
    """Dispatch one activation carrying ``context`` through
    default_run_starter (no executor registered → the row stays pending,
    the manual-trigger behavior) and return the persisted execution row."""
    factory = _shared_session_factory()
    registration_id = f"reg-{uuid.uuid4().hex}"
    activation = RunActivation(
        trigger_node_id="trig-1",
        activation_group="trig-1",
        priority=100,
        firing_seq=0,
        context=context,
    )
    previous = executor.get_executor()
    executor.set_executor(None)
    try:
        default_run_starter(registration_id, session_factory=factory)(
            activation
        )
    finally:
        executor.set_executor(previous)

    session = factory()
    try:
        rows = (
            session.query(WorkflowExecution)
            .filter(WorkflowExecution.registration_id == registration_id)
            .all()
        )
        assert len(rows) == 1
        return rows[0].status, rows[0].trigger_context_json
    finally:
        session.close()


_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=30),
)

_JSON_VALUES = st.one_of(
    _JSON_SCALARS,
    st.lists(_JSON_SCALARS, max_size=4),
    st.dictionaries(st.text(max_size=10), _JSON_SCALARS, max_size=4),
)

_MQTT_CONTEXTS = st.fixed_dictionaries(
    {
        "topic": st.text(min_size=1, max_size=40),
        "payload": st.text(max_size=64),
        "qos": st.integers(min_value=0, max_value=2),
        "timestamp": st.floats(
            min_value=0, max_value=4e9, allow_nan=False, allow_infinity=False
        ),
    }
)

_OPCUA_CONTEXTS = st.fixed_dictionaries(
    {
        "endpoint": st.just(OPCUA_ENDPOINT),
        "node_id": st.just(OPCUA_NODE_ID),
        "value": _JSON_VALUES,
        "source_timestamp": st.one_of(st.none(), st.text(max_size=30)),
    }
)


# Feature: trigger-activation-runtime, Property 13: Trigger_Context fidelity
@settings(max_examples=100, deadline=None)
@given(context=st.one_of(_MQTT_CONTEXTS, _OPCUA_CONTEXTS))
def test_dispatched_context_round_trips_through_trigger_context_json(context):
    """For any delivered context (MQTT- or OPC-UA-shaped, JSON-native
    values), the execution row default_run_starter inserts persists
    trigger_context_json that json.loads back to the exact context.

    **Validates: Requirements 6.8**
    """
    status, trigger_context_json = _start_run_and_load_row(context)
    assert status == "pending"
    assert json.loads(trigger_context_json) == context


def test_non_json_value_degrades_to_string_in_persisted_context():
    """A non-JSON-native OPC UA value (e.g. a datetime variant) degrades
    to its string form in trigger_context_json rather than failing the
    run (serialize_trigger_context's default=str).

    **Validates: Requirements 6.8**
    """
    value = datetime.datetime(2026, 8, 5, 12, 30, 15)
    context = {
        "endpoint": OPCUA_ENDPOINT,
        "node_id": OPCUA_NODE_ID,
        "value": value,
        "source_timestamp": None,
    }
    status, trigger_context_json = _start_run_and_load_row(context)
    assert status == "pending"
    persisted = json.loads(trigger_context_json)
    assert set(persisted.keys()) == OPCUA_CONTEXT_KEYS
    assert persisted["value"] == str(value)
    assert persisted["endpoint"] == OPCUA_ENDPOINT
    assert persisted["node_id"] == OPCUA_NODE_ID
    assert persisted["source_timestamp"] is None
