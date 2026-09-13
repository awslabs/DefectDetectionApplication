"""mqtt-iot-endpoint — an explicit AWS IoT Core data endpoint on the
``aws_iot`` path of ``mqtt_publish`` (executor) and ``mqtt_subscribe``
(trigger transport).

``iot_endpoint`` lets a node talk to IoT Core in ANOTHER account or region
(with a thing certificate issued by that account). Rules, identical on
both sides:

* a non-blank ``iot_endpoint`` (trimmed) is the host paho connects to and
  takes precedence over ``broker_host``;
* an absent/blank ``iot_endpoint`` keeps the pre-feature behaviour — the
  host is ``broker_host`` exactly as before, so every existing aws_iot
  workflow publishes/subscribes byte-identically;
* everything else on the aws_iot path (8883 default-port switch, qos
  clamp, thing name as client id, tls dict from the ``iot_*`` paths, the
  retain keyword) is untouched;
* the plain-broker and Greengrass paths ignore ``iot_endpoint`` entirely;
* the subscribe transport accepts ``iot_endpoint`` OR ``broker_host`` as
  the endpoint and still refuses when both are blank.
"""

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH
from workflow_engine.output_bindings import (
    AWS_IOT_MAX_QOS,
    AWS_IOT_TLS_PORT,
    OutputBindingProcessor,
    aws_iot_endpoint,
)
from workflow_engine.trigger_runtime import (
    AwsIotTlsSubscriber,
    PlainBrokerSubscriber,
    TriggerHealth,
    default_mqtt_transport_factory,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}
PAYLOAD = '{"confidence": 0.9, "is_anomalous": true}'
TOPIC = "quality/result"
LOCAL_ENDPOINT = "a1b2c3-ats.iot.us-east-1.amazonaws.com"
REMOTE_ENDPOINT = "z9y8x7-ats.iot.eu-west-1.amazonaws.com"
CERTS = {
    "iot_thing_name": "edge-device-01",
    "iot_ca_cert_path": "/greengrass/v2/remote/AmazonRootCA1.pem",
    "iot_client_cert_path": "/greengrass/v2/remote/device.pem.crt",
    "iot_private_key_path": "/greengrass/v2/remote/private.pem.key",
}
TLS = {
    "ca_certs": CERTS["iot_ca_cert_path"],
    "certfile": CERTS["iot_client_cert_path"],
    "keyfile": CERTS["iot_private_key_path"],
}


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _publish(parameters):
    mqtt, greengrass = _Recorder(), _Recorder()
    processor = OutputBindingProcessor(
        mqtt_publisher=mqtt, greengrass_publisher=greengrass)
    detail = processor._run_mqtt_publish(dict(parameters), dict(METADATA))
    return mqtt.calls, greengrass.calls, detail


def _aws_iot(**extra):
    return dict({"topic": TOPIC, "aws_iot": True}, **CERTS, **extra)


# ---------------------------------------------------------------------------
# The shared resolution helper
# ---------------------------------------------------------------------------

class TestAwsIotEndpointHelper:

    def test_endpoint_wins_over_fallback(self):
        assert aws_iot_endpoint({"iot_endpoint": REMOTE_ENDPOINT}, "broker") == REMOTE_ENDPOINT

    def test_endpoint_is_trimmed(self):
        assert aws_iot_endpoint({"iot_endpoint": "  " + REMOTE_ENDPOINT + "\n"}, "b") == REMOTE_ENDPOINT

    @pytest.mark.parametrize("value", [None, "", "   ", 0, False])
    def test_absent_or_blank_returns_fallback_unchanged(self, value):
        params = {} if value is None else {"iot_endpoint": value}
        assert aws_iot_endpoint(params, " broker.local ") == " broker.local "


# ---------------------------------------------------------------------------
# Publish executor
# ---------------------------------------------------------------------------

class TestPublishAwsIotEndpoint:

    def test_endpoint_replaces_broker_host_as_the_host(self):
        mqtt, greengrass, detail = _publish(
            _aws_iot(broker_host=LOCAL_ENDPOINT, iot_endpoint=REMOTE_ENDPOINT))
        assert greengrass == []
        assert mqtt == [(
            (REMOTE_ENDPOINT, AWS_IOT_TLS_PORT, TOPIC, PAYLOAD, 0,
             "edge-device-01", TLS),
            {},
        )]
        assert detail == "sent to topic 'quality/result' (qos 0, aws_iot): " + PAYLOAD

    def test_endpoint_alone_without_broker_host(self):
        # A cross-account node need not set broker_host at all.
        mqtt, _, _ = _publish(_aws_iot(iot_endpoint=REMOTE_ENDPOINT, broker_port=8884, qos=2))
        args, kwargs = mqtt[0]
        assert args[0] == REMOTE_ENDPOINT
        assert args[1] == 8884                # explicit port kept
        assert args[4] == AWS_IOT_MAX_QOS     # qos still clamped
        assert kwargs == {}

    @pytest.mark.parametrize("endpoint", [None, "", "   "])
    def test_absent_or_blank_endpoint_keeps_broker_host(self, endpoint):
        params = _aws_iot(broker_host=LOCAL_ENDPOINT)
        if endpoint is not None:
            params["iot_endpoint"] = endpoint
        mqtt, _, detail = _publish(params)
        assert mqtt == [(
            (LOCAL_ENDPOINT, AWS_IOT_TLS_PORT, TOPIC, PAYLOAD, 0,
             "edge-device-01", TLS),
            {},
        )]
        assert "aws_iot)" in detail

    def test_endpoint_combines_with_retain(self):
        mqtt, _, detail = _publish(_aws_iot(iot_endpoint=REMOTE_ENDPOINT, retain=True))
        assert mqtt[0][0][0] == REMOTE_ENDPOINT
        assert mqtt[0][1] == {"retain": True}
        assert detail.endswith("(qos 0, aws_iot, retained): " + PAYLOAD)

    def test_plain_broker_path_ignores_endpoint(self):
        mqtt, _, detail = _publish(
            {"topic": TOPIC, "broker_host": "10.0.0.12", "iot_endpoint": REMOTE_ENDPOINT})
        assert mqtt == [(("10.0.0.12", 1883, TOPIC, PAYLOAD, 0), {})]
        assert "(qos 0, plain)" in detail

    def test_greengrass_path_ignores_endpoint(self):
        mqtt, greengrass, detail = _publish(
            {"topic": TOPIC, "greengrass": True, "iot_endpoint": REMOTE_ENDPOINT})
        assert mqtt == []
        assert greengrass == [((TOPIC, PAYLOAD, 0), {})]
        assert "(qos 0, greengrass)" in detail

    def test_iot_credentials_still_required_with_endpoint(self):
        params = _aws_iot(iot_endpoint=REMOTE_ENDPOINT)
        params.pop("iot_private_key_path")
        with pytest.raises(ValueError, match="iot_private_key_path"):
            _publish(params)

    @pytest.mark.parametrize("params", [
        _aws_iot(),                                   # neither key present
        _aws_iot(broker_host=None, iot_endpoint=None),  # compiled defaults
        _aws_iot(broker_host="  ", iot_endpoint=" "),
    ])
    def test_neither_endpoint_nor_broker_host_is_refused(self, params):
        with pytest.raises(ValueError, match="iot_endpoint or broker_host"):
            _publish(params)


# ---------------------------------------------------------------------------
# Subscribe transport
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.connect_calls = []
        self.tls_set_calls = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, **kwargs):
        self.tls_set_calls.append(kwargs)

    def connect(self, host, port):
        self.connect_calls.append((host, port))

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


class _StubFactory:
    def __init__(self):
        self.client_ids = []
        self.clients = []

    def __call__(self, client_id):
        self.client_ids.append(client_id)
        client = _StubClient(client_id)
        self.clients.append(client)
        return client


def _health():
    return TriggerHealth("trigger_1", "mqtt_subscribe")


def _subscriber(params):
    factory = _StubFactory()
    worker = AwsIotTlsSubscriber(
        params, lambda ctx: None, lambda err: None, _health(),
        client_factory=factory)
    return worker, factory


def _sub_params(**extra):
    return dict({"topic": TOPIC, "qos": 1, "aws_iot": True}, **CERTS, **extra)


class TestSubscribeAwsIotEndpoint:

    def test_endpoint_replaces_broker_host_as_connect_host(self):
        worker, factory = _subscriber(
            _sub_params(broker_host=LOCAL_ENDPOINT, iot_endpoint=REMOTE_ENDPOINT))
        worker.start()
        assert factory.clients[0].connect_calls == [(REMOTE_ENDPOINT, AWS_IOT_TLS_PORT)]
        assert factory.client_ids == ["edge-device-01"]
        assert factory.clients[0].tls_set_calls == [TLS]

    def test_endpoint_alone_without_broker_host(self):
        worker, factory = _subscriber(_sub_params(iot_endpoint=" " + REMOTE_ENDPOINT + " ", broker_port=9883))
        worker.start()
        assert factory.clients[0].connect_calls == [(REMOTE_ENDPOINT, 9883)]

    @pytest.mark.parametrize("endpoint", [None, "", "  "])
    def test_absent_or_blank_endpoint_keeps_broker_host(self, endpoint):
        params = _sub_params(broker_host=LOCAL_ENDPOINT)
        if endpoint is not None:
            params["iot_endpoint"] = endpoint
        worker, factory = _subscriber(params)
        worker.start()
        assert factory.clients[0].connect_calls == [(LOCAL_ENDPOINT, AWS_IOT_TLS_PORT)]

    def test_neither_endpoint_nor_broker_host_is_refused(self):
        with pytest.raises(ValueError, match="iot_endpoint or broker_host"):
            _subscriber(_sub_params(iot_endpoint="  "))

    def test_factory_dispatches_aws_iot_with_endpoint_only(self):
        worker = default_mqtt_transport_factory(
            "mqtt_subscribe", _sub_params(iot_endpoint=REMOTE_ENDPOINT),
            lambda ctx: None, lambda err: None, _health())
        assert isinstance(worker, AwsIotTlsSubscriber)
        assert worker._connect_target() == (REMOTE_ENDPOINT, AWS_IOT_TLS_PORT)

    def test_plain_broker_transport_ignores_endpoint(self):
        worker = default_mqtt_transport_factory(
            "mqtt_subscribe",
            {"topic": TOPIC, "qos": 0, "broker_host": "10.0.0.12", "iot_endpoint": REMOTE_ENDPOINT},
            lambda ctx: None, lambda err: None, _health())
        assert isinstance(worker, PlainBrokerSubscriber)
        assert worker._connect_target() == ("10.0.0.12", 1883)
