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
"""Example tests for the MQTT subscribe transports (task 7.6).

Feature: trigger-activation-runtime — the three MQTT transports of design
C5 exercised through their injection seams (``ipc_connect`` for the
Greengrass IPC worker, ``client_factory`` for the paho workers):

(a) Greengrass IPC: SubscribeToIoTCoreRequest topic/qos wiring (clamped),
    stream event → exact Trigger_Context → on_delivery (Requirement 6.3);
(b) aws_iot mutual TLS: client_id = iot_thing_name, tls_set certificate
    arguments, host/port selection, qos clamp, missing-parameter
    ValueError (Requirement 6.4);
(c) plain broker: connect/subscribe arguments, on_message → Trigger_Context
    (Requirement 6.5);
(d) health transitions on loss/restore through a bound ReconnectEngine
    (Requirement 8.2);
(e) Greengrass UnauthorizedError → failed health naming the topic and the
    mqttproxy accessControl location, no reconnects (Requirement 8.7);
(f) default_mqtt_transport_factory target dispatch.

The Greengrass worker imports the ``awsiot`` SDK lazily inside
``_subscribe``, so those tests install ``sys.modules`` stubs for the
``awsiot.greengrasscoreipc`` module family — no SDK or nucleus socket is
needed.

Requirements: 6.3, 6.4, 6.5, 8.2, 8.7
"""
import sys
import time
import types
from types import SimpleNamespace

import pytest

from workflow_engine.trigger_runtime import (
    HEALTH_FAILED,
    HEALTH_RECONNECTING,
    HEALTH_SUBSCRIBED,
    RECONNECT_PARK,
    AwsIotTlsSubscriber,
    GreengrassIpcSubscriber,
    PlainBrokerSubscriber,
    ReconnectEngine,
    TriggerHealth,
    default_mqtt_transport_factory,
)

NODE_ID = "trig1"
TOPIC = "factory/line1/start"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_health(kind="mqtt_subscribe"):
    return TriggerHealth(NODE_ID, kind)


def record_states(health):
    """Capture every health state transition (order matters for 8.2)."""
    states = []
    original = health.set_state

    def recording_set_state(state, error=None):
        states.append(state)
        original(state, error=error)

    health.set_state = recording_set_state
    return states


class DeliveryRecorder:
    def __init__(self):
        self.contexts = []

    def __call__(self, context):
        self.contexts.append(context)
        return True


# ---------------------------------------------------------------------------
# Greengrass IPC stubs (sys.modules substitutes for the lazy awsiot imports)
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


class StubIpcFuture:
    def __init__(self, error=None):
        self._error = error

    def result(self, timeout=None):
        if self._error is not None:
            raise self._error
        return SimpleNamespace()


class StubIpcOperation:
    def __init__(self, error=None):
        self._error = error
        self.request = None
        self.closed = False

    def activate(self, request):
        self.request = request

    def get_response(self):
        return StubIpcFuture(self._error)

    def close(self):
        self.closed = True


class StubIpcClient:
    def __init__(self, error=None):
        self._error = error
        self.handlers = []
        self.operations = []
        self.closed = False

    def new_subscribe_to_iot_core(self, handler):
        self.handlers.append(handler)
        operation = StubIpcOperation(self._error)
        self.operations.append(operation)
        return operation

    def close(self):
        self.closed = True


class StubIpcConnect:
    """The ``ipc_connect`` seam: one StubIpcClient per call, with an
    optional per-call error plan (None = success)."""

    def __init__(self, error_plan=()):
        self._error_plan = list(error_plan)
        self.clients = []

    def __call__(self):
        error = self._error_plan.pop(0) if self._error_plan else None
        client = StubIpcClient(error)
        self.clients.append(client)
        return client


def greengrass_params(**overrides):
    params = {"topic": TOPIC, "qos": 1, "greengrass": True}
    params.update(overrides)
    return params


def make_greengrass_worker(
    params=None, on_delivery=None, on_connection_lost=None, ipc_connect=None
):
    health = make_health()
    worker = GreengrassIpcSubscriber(
        params if params is not None else greengrass_params(),
        on_delivery if on_delivery is not None else DeliveryRecorder(),
        on_connection_lost if on_connection_lost is not None else (lambda e: None),
        health,
        ipc_connect=ipc_connect if ipc_connect is not None else StubIpcConnect(),
    )
    return worker, health


# ---------------------------------------------------------------------------
# (a) Greengrass: request topic/qos wiring and stream-event delivery
#     (Requirement 6.3)
# ---------------------------------------------------------------------------


def test_greengrass_subscribe_request_carries_topic_and_clamped_qos(awsiot_stubs):
    """The SubscribeToIoTCoreRequest carries the configured topic filter
    and the qos clamped to the Greengrass maximum of 1: a configured qos
    of 2 subscribes AT_LEAST_ONCE with effective_qos 1 (Requirement 6.3)."""
    connect = StubIpcConnect()
    worker, health = make_greengrass_worker(
        params=greengrass_params(qos=2), ipc_connect=connect
    )

    worker.start()

    assert len(connect.clients) == 1
    operation = connect.clients[0].operations[0]
    assert isinstance(operation.request, StubSubscribeToIoTCoreRequest)
    assert operation.request.topic_name == TOPIC
    assert operation.request.qos == StubQOS.AT_LEAST_ONCE
    assert worker.effective_qos == 1
    assert health.state == HEALTH_SUBSCRIBED


def test_greengrass_subscribe_request_qos_zero_is_at_most_once(awsiot_stubs):
    """qos 0 subscribes AT_MOST_ONCE, mirroring the publish path's clamp
    logic (Requirement 6.3)."""
    connect = StubIpcConnect()
    worker, _health = make_greengrass_worker(
        params=greengrass_params(qos=0), ipc_connect=connect
    )

    worker.start()

    operation = connect.clients[0].operations[0]
    assert operation.request.qos == StubQOS.AT_MOST_ONCE
    assert worker.effective_qos == 0


def test_greengrass_stream_event_builds_exact_trigger_context(awsiot_stubs):
    """One stream event delivers exactly {topic, payload, qos, timestamp}
    to on_delivery, with values from the IoTCoreMessage (Requirement 6.3)."""
    connect = StubIpcConnect()
    delivered = DeliveryRecorder()
    worker, _health = make_greengrass_worker(
        params=greengrass_params(qos=2), on_delivery=delivered, ipc_connect=connect
    )
    worker.start()

    handler = connect.clients[0].handlers[0]
    event = SimpleNamespace(
        message=SimpleNamespace(
            topic_name="factory/line1/start/actual",
            payload=b'{"go": true}',
        )
    )
    before = time.time()
    handler.on_stream_event(event)
    after = time.time()

    assert len(delivered.contexts) == 1
    context = delivered.contexts[0]
    assert set(context) == {"topic", "payload", "qos", "timestamp"}
    assert context["topic"] == "factory/line1/start/actual"
    assert context["payload"] == '{"go": true}'
    assert context["qos"] == 1  # the effective (clamped) qos
    assert before <= context["timestamp"] <= after


# ---------------------------------------------------------------------------
# (b) aws_iot: client configuration mirrors the publish path
#     (Requirement 6.4)
# ---------------------------------------------------------------------------


class StubPahoClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.tls_set_calls = []
        self.connect_calls = []
        self.subscribe_calls = []
        self.loop_start_calls = 0
        self.loop_stop_calls = 0
        self.disconnect_calls = 0
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, ca_certs=None, certfile=None, keyfile=None):
        self.tls_set_calls.append(
            {"ca_certs": ca_certs, "certfile": certfile, "keyfile": keyfile}
        )

    def connect(self, host, port):
        self.connect_calls.append((host, port))

    def subscribe(self, topic, qos):
        self.subscribe_calls.append((topic, qos))

    def loop_start(self):
        self.loop_start_calls += 1

    def loop_stop(self):
        self.loop_stop_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1

    def simulate_connack(self, rc=0):
        """Fire the CONNACK callback the way paho's network thread would."""
        self.on_connect(self, None, None, rc)

    def simulate_message(self, topic, payload):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))

    def simulate_disconnect(self, rc):
        self.on_disconnect(self, None, rc)


class StubClientFactory:
    """The paho ``client_factory`` seam: records requested client ids and
    every client built."""

    def __init__(self):
        self.client_ids = []
        self.clients = []

    def __call__(self, client_id):
        self.client_ids.append(client_id)
        client = StubPahoClient(client_id)
        self.clients.append(client)
        return client


AWS_IOT_ENDPOINT = "abcdefg-ats.iot.us-west-2.amazonaws.com"


def aws_iot_params(**overrides):
    params = {
        "topic": TOPIC,
        "qos": 1,
        "aws_iot": True,
        "broker_host": AWS_IOT_ENDPOINT,
        "iot_thing_name": "my-jetson-thing",
        "iot_ca_cert_path": "/greengrass/certs/AmazonRootCA1.pem",
        "iot_client_cert_path": "/greengrass/certs/device.pem.crt",
        "iot_private_key_path": "/greengrass/certs/private.pem.key",
    }
    params.update(overrides)
    return params


def make_aws_iot_worker(params=None, on_delivery=None, on_connection_lost=None):
    factory = StubClientFactory()
    health = make_health()
    worker = AwsIotTlsSubscriber(
        params if params is not None else aws_iot_params(),
        on_delivery if on_delivery is not None else DeliveryRecorder(),
        on_connection_lost if on_connection_lost is not None else (lambda e: None),
        health,
        client_factory=factory,
    )
    return worker, factory, health


def test_aws_iot_client_id_is_the_iot_thing_name():
    """The MQTT client id is iot_thing_name, exactly as the publish path
    connects (Requirement 6.4)."""
    worker, factory, _health = make_aws_iot_worker()

    worker.start()

    assert factory.client_ids == ["my-jetson-thing"]


def test_aws_iot_tls_set_uses_the_iot_certificate_parameters():
    """tls_set receives ca_certs/certfile/keyfile from the iot_* parameters
    (Requirement 6.4)."""
    worker, factory, _health = make_aws_iot_worker()

    worker.start()

    assert factory.clients[0].tls_set_calls == [
        {
            "ca_certs": "/greengrass/certs/AmazonRootCA1.pem",
            "certfile": "/greengrass/certs/device.pem.crt",
            "keyfile": "/greengrass/certs/private.pem.key",
        }
    ]


def test_aws_iot_default_port_switches_to_mutual_tls_8883():
    """A broker_port left unset (the plain-MQTT default) connects on the
    standard mutual-TLS port 8883 (Requirement 6.4)."""
    worker, factory, _health = make_aws_iot_worker()

    worker.start()

    assert factory.clients[0].connect_calls == [(AWS_IOT_ENDPOINT, 8883)]
    assert factory.clients[0].loop_start_calls == 1


def test_aws_iot_explicit_port_is_kept():
    """An explicitly configured non-default broker_port is used as-is
    (Requirement 6.4)."""
    worker, factory, _health = make_aws_iot_worker(
        params=aws_iot_params(broker_port=9883)
    )

    worker.start()

    assert factory.clients[0].connect_calls == [(AWS_IOT_ENDPOINT, 9883)]


def test_aws_iot_qos_is_clamped_to_1_on_subscribe():
    """AWS IoT Core has no QoS 2: a configured qos of 2 subscribes with
    qos 1 once the connection is established (Requirement 6.4)."""
    worker, factory, health = make_aws_iot_worker(params=aws_iot_params(qos=2))
    worker.start()

    factory.clients[0].simulate_connack(rc=0)

    assert factory.clients[0].subscribe_calls == [(TOPIC, 1)]
    assert health.state == HEALTH_SUBSCRIBED


@pytest.mark.parametrize(
    "missing",
    [
        "iot_thing_name",
        "iot_ca_cert_path",
        "iot_client_cert_path",
        "iot_private_key_path",
    ],
)
def test_aws_iot_missing_iot_parameter_raises_value_error(missing):
    """Each required iot_* parameter is enforced at construction with a
    ValueError naming the gap, mirroring the publish path's requirement
    check (Requirement 6.4)."""
    params = aws_iot_params()
    params.pop(missing)

    with pytest.raises(ValueError, match=missing):
        AwsIotTlsSubscriber(
            params,
            DeliveryRecorder(),
            lambda e: None,
            make_health(),
            client_factory=StubClientFactory(),
        )


def test_aws_iot_missing_broker_host_raises_value_error():
    """The AWS IoT endpoint (broker_host) is required too
    (Requirement 6.4)."""
    with pytest.raises(ValueError, match="broker_host"):
        AwsIotTlsSubscriber(
            aws_iot_params(broker_host="  "),
            DeliveryRecorder(),
            lambda e: None,
            make_health(),
            client_factory=StubClientFactory(),
        )


# ---------------------------------------------------------------------------
# (c) Plain broker: connect/subscribe arguments and message delivery
#     (Requirement 6.5)
# ---------------------------------------------------------------------------


def plain_params(**overrides):
    params = {
        "topic": TOPIC,
        "qos": 1,
        "broker_host": "broker.factory.local",
        "broker_port": 1884,
    }
    params.update(overrides)
    return params


def make_plain_worker(params=None, on_delivery=None, on_connection_lost=None):
    factory = StubClientFactory()
    health = make_health()
    worker = PlainBrokerSubscriber(
        params if params is not None else plain_params(),
        on_delivery if on_delivery is not None else DeliveryRecorder(),
        on_connection_lost if on_connection_lost is not None else (lambda e: None),
        health,
        client_factory=factory,
    )
    return worker, factory, health


def test_plain_broker_connects_with_host_and_port():
    """connect(broker_host, broker_port) with no TLS and no client id
    override (Requirement 6.5)."""
    worker, factory, _health = make_plain_worker()

    worker.start()

    client = factory.clients[0]
    assert factory.client_ids == [""]  # paho generates the id
    assert client.connect_calls == [("broker.factory.local", 1884)]
    assert client.tls_set_calls == []
    assert client.loop_start_calls == 1


def test_plain_broker_subscribes_topic_and_qos_on_connect():
    """Once the connection is established, subscribe(topic, qos) is issued
    and health turns subscribed (Requirement 6.5)."""
    worker, factory, health = make_plain_worker()
    worker.start()

    factory.clients[0].simulate_connack(rc=0)

    assert factory.clients[0].subscribe_calls == [(TOPIC, 1)]
    assert health.state == HEALTH_SUBSCRIBED


def test_plain_broker_message_builds_exact_trigger_context():
    """on_message delivers exactly {topic, payload, qos, timestamp} with the
    message's topic and UTF-8-decoded payload (Requirement 6.5)."""
    delivered = DeliveryRecorder()
    worker, factory, _health = make_plain_worker(on_delivery=delivered)
    worker.start()
    client = factory.clients[0]
    client.simulate_connack(rc=0)

    before = time.time()
    client.simulate_message("factory/line1/evt", b"hello world")
    after = time.time()

    assert len(delivered.contexts) == 1
    context = delivered.contexts[0]
    assert set(context) == {"topic", "payload", "qos", "timestamp"}
    assert context["topic"] == "factory/line1/evt"
    assert context["payload"] == "hello world"
    assert context["qos"] == 1
    assert before <= context["timestamp"] <= after


# ---------------------------------------------------------------------------
# (d) Health transitions on connection loss and restore (Requirement 8.2)
# ---------------------------------------------------------------------------


def make_synchronous_engine(health, worker_holder, retry_limit=0):
    """A ReconnectEngine that runs its loop synchronously (spawn calls the
    loop inline) with a non-blocking waiter, late-bound to the worker the
    holder carries — the same wiring the manager performs."""
    engine = ReconnectEngine(
        node_id=NODE_ID,
        target=TOPIC,
        health=health,
        retry_limit=retry_limit,
        waiter=lambda delay: False,  # backoff wait elapses, never cancelled
        spawn=lambda target: target(),
    )
    engine.bind(lambda: worker_holder["worker"].reconnect())
    return engine


def test_unexpected_disconnect_routes_to_on_connection_lost():
    """An unexpected paho disconnect (rc != 0) routes the loss to
    on_connection_lost; a deliberate disconnect (rc 0) does not
    (Requirement 8.2)."""
    losses = []
    worker, factory, _health = make_plain_worker(
        on_connection_lost=losses.append
    )
    worker.start()
    client = factory.clients[0]
    client.simulate_connack(rc=0)

    client.simulate_disconnect(rc=0)  # deliberate: reconnect must not engage
    assert losses == []

    client.simulate_disconnect(rc=1)  # unexpected loss
    assert len(losses) == 1
    assert isinstance(losses[0], BaseException)
    # The handler flips paho into its deliberate-disconnect state so its
    # own loop cannot race the ReconnectEngine.
    assert client.disconnect_calls == 1


def test_health_goes_reconnecting_then_subscribed_on_successful_reconnect():
    """With a bound ReconnectEngine, a connection loss drives health
    reconnecting → subscribed once the reconnect attempt succeeds, with a
    fresh client built and the attempt counter reset (Requirement 8.2)."""
    factory = StubClientFactory()
    health = make_health()
    states = record_states(health)
    worker_holder = {}
    engine = make_synchronous_engine(health, worker_holder)
    worker = PlainBrokerSubscriber(
        plain_params(),
        DeliveryRecorder(),
        engine.on_connection_lost,
        health,
        client_factory=factory,
    )
    worker_holder["worker"] = worker

    worker.start()
    factory.clients[0].simulate_connack(rc=0)
    assert health.state == HEALTH_SUBSCRIBED

    # Unexpected loss: the engine runs synchronously — reconnecting, one
    # successful attempt, health restored to subscribed.
    factory.clients[0].simulate_disconnect(rc=1)

    assert states[-2:] == [HEALTH_RECONNECTING, HEALTH_SUBSCRIBED]
    assert health.state == HEALTH_SUBSCRIBED
    assert health.reconnect_attempts == 0  # reset on success
    assert len(factory.clients) == 2  # the dead client was rebuilt
    assert not engine.parked


# ---------------------------------------------------------------------------
# (e) Greengrass UnauthorizedError → denial diagnostics, no retries
#     (Requirement 8.7)
# ---------------------------------------------------------------------------


def test_greengrass_denial_at_start_fails_health_with_access_control_message(
    awsiot_stubs,
):
    """An UnauthorizedError at subscribe time marks health failed with a
    message naming the topic and the aws.greengrass.ipc.mqttproxy
    accessControl location; on_connection_lost is never called and start
    does not raise (Requirement 8.7)."""
    connect = StubIpcConnect(error_plan=[StubUnauthorizedError("denied")])
    losses = []
    worker, health = make_greengrass_worker(
        on_connection_lost=losses.append, ipc_connect=connect
    )

    worker.start()  # must not raise: the denial parks the worker

    assert health.state == HEALTH_FAILED
    message = health.to_wire()["lastError"]
    assert TOPIC in message
    assert "aws.greengrass.ipc.mqttproxy" in message
    assert "SubscribeToIoTCore" in message
    assert losses == []
    # The failed operation and client were released quietly.
    assert connect.clients[0].operations[0].closed
    assert connect.clients[0].closed


def test_greengrass_denied_worker_parks_reconnect_with_no_retries(awsiot_stubs):
    """After a denial, reconnect() returns RECONNECT_PARK without another
    subscribe attempt — authorization does not self-heal by retrying
    (Requirement 8.7)."""
    connect = StubIpcConnect(error_plan=[StubUnauthorizedError("denied")])
    worker, health = make_greengrass_worker(ipc_connect=connect)
    worker.start()
    assert health.state == HEALTH_FAILED

    outcome = worker.reconnect()

    assert outcome is RECONNECT_PARK
    assert len(connect.clients) == 1  # no new IPC connection was attempted


def test_greengrass_denial_during_reconnect_parks_the_engine(awsiot_stubs):
    """A denial hit inside an engine-driven reconnect parks the engine on
    the first attempt: the denial health stands and no further attempts
    run even with retry_limit=0 (retry forever) (Requirement 8.7)."""
    # First connect succeeds; the reconnect attempt is denied.
    connect = StubIpcConnect(error_plan=[None, StubUnauthorizedError("denied")])
    health = make_health()
    worker_holder = {}
    engine = make_synchronous_engine(health, worker_holder, retry_limit=0)
    worker = GreengrassIpcSubscriber(
        greengrass_params(),
        DeliveryRecorder(),
        engine.on_connection_lost,
        health,
        ipc_connect=connect,
    )
    worker_holder["worker"] = worker

    worker.start()
    assert health.state == HEALTH_SUBSCRIBED

    # The stream drops: the engine reconnects synchronously and the denial
    # parks it after exactly one attempt.
    handler = connect.clients[0].handlers[0]
    handler.on_stream_error(RuntimeError("stream broke"))

    assert engine.parked
    assert health.state == HEALTH_FAILED
    message = health.to_wire()["lastError"]
    assert TOPIC in message
    assert "aws.greengrass.ipc.mqttproxy" in message
    assert len(connect.clients) == 2  # initial connect + the one denied attempt


# ---------------------------------------------------------------------------
# (f) default_mqtt_transport_factory target dispatch
# ---------------------------------------------------------------------------


def test_factory_dispatches_greengrass_target():
    worker = default_mqtt_transport_factory(
        "mqtt_subscribe",
        {"topic": TOPIC, "qos": 1, "greengrass": True},
        DeliveryRecorder(),
        lambda e: None,
        make_health(),
    )
    assert isinstance(worker, GreengrassIpcSubscriber)


def test_factory_dispatches_aws_iot_target():
    worker = default_mqtt_transport_factory(
        "mqtt_subscribe",
        aws_iot_params(),
        DeliveryRecorder(),
        lambda e: None,
        make_health(),
    )
    assert isinstance(worker, AwsIotTlsSubscriber)


def test_factory_dispatches_plain_broker_target():
    worker = default_mqtt_transport_factory(
        "mqtt_subscribe",
        {"topic": TOPIC, "qos": 1, "broker_host": "broker.factory.local"},
        DeliveryRecorder(),
        lambda e: None,
        make_health(),
    )
    assert isinstance(worker, PlainBrokerSubscriber)


def test_factory_raises_for_target_less_configuration():
    """No greengrass, no aws_iot, blank broker_host → a ValueError naming
    the node and the three target options (validator V8 normally prevents
    this configuration)."""
    with pytest.raises(ValueError) as excinfo:
        default_mqtt_transport_factory(
            "mqtt_subscribe",
            {"topic": TOPIC, "qos": 1, "broker_host": "   "},
            DeliveryRecorder(),
            lambda e: None,
            make_health(),
        )
    message = str(excinfo.value)
    assert NODE_ID in message
    assert "greengrass" in message
    assert "aws_iot" in message
    assert "broker_host" in message
