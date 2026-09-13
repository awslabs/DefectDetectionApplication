"""mqtt-retained-publish — the ``retain`` flag on the ``mqtt_publish``
executor (``OutputBindingProcessor._run_mqtt_publish`` and the two default
publishers).

Spec: .kiro/specs/mqtt-retained-publish (task 2.3).

The flag reaches the injected publisher as the keyword ``retain=True``
ONLY when the binding enables it. Every pre-feature call shape — the
positional tuples pinned by ``test_mqtt_publish_call_preservation.py`` and
``test_mqtt_greengrass_dispatch.py`` — is unchanged and keeps
``kwargs == {}``. The sent-message detail gains ``, retained`` after the
path name only for a retained publish.

Properties (design.md §Correctness):

* P1 (preservation, off): with ``retain`` absent or false, on each of the
  three paths, the Publisher_Call ``(args, kwargs)`` and the detail equal
  the pre-feature executor's byte-for-byte (``kwargs == {}``).
* P2 (retain on): with ``retain`` true, ``args`` equals the P1 tuple for
  the same parameters, ``kwargs == {"retain": True}``, and the detail is
  the P1 detail with ``<path>`` replaced by ``<path>, retained``.

The two default publishers are exercised against fake ``paho`` and fake
``awsiot.greengrasscoreipc`` modules (neither is a host dependency of this
suite): the paho ``single`` call and the IPC request carry ``retain`` only
when requested, an SDK whose request class lacks ``retain`` fails before
any IPC call, and the nucleus-denial ``RuntimeError`` names
``iot:RetainPublish`` only for a retained publish.
"""

import json
import sys
import types
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH
from workflow_engine.output_bindings import (
    AWS_IOT_MAX_QOS,
    AWS_IOT_TLS_PORT,
    DEFAULT_MQTT_PORT,
    OutputBindingProcessor,
    _default_greengrass_publisher,
    _default_mqtt_publisher,
    _preview,
    render_template,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}
_TOPIC = "factory/line1/inspection"
_CERTS = {
    "iot_thing_name": "edge-device-01",
    "iot_ca_cert_path": "/greengrass/v2/rootCA.pem",
    "iot_client_cert_path": "/greengrass/v2/thingCert.crt",
    "iot_private_key_path": "/greengrass/v2/privKey.key",
}


# ---------------------------------------------------------------------------
# Recorder + reference derivation of the PRE-FEATURE call and detail
# ---------------------------------------------------------------------------

class _Recorder:
    """Injected at the publisher boundary; records ``(args, kwargs)``."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _run(parameters, metadata=None):
    """Run ``_run_mqtt_publish`` with both publishers injected; return
    (greengrass_calls, mqtt_calls, detail)."""
    greengrass, mqtt = _Recorder(), _Recorder()
    processor = OutputBindingProcessor(
        mqtt_publisher=mqtt, greengrass_publisher=greengrass)
    detail = processor._run_mqtt_publish(
        dict(parameters), dict(metadata or METADATA))
    return greengrass.calls, mqtt.calls, detail


def _payload_text(parameters, metadata):
    payload = render_template(
        str(parameters.get("payload_template") or "{inference_json}"),
        metadata,
    )
    return payload if isinstance(payload, str) else json.dumps(payload, default=str)


def _reference(parameters, metadata):
    """The pre-feature executor's behaviour for ``parameters`` (retain
    ignored): which publisher is called, its positional args, the path
    name, and the detail string — transcribed from the pinned
    preservation suites so P1/P2 compare against a recorded reference,
    not against the code under test."""
    topic = str(parameters["topic"])
    qos = int(parameters.get("qos", 0))
    payload = _payload_text(parameters, metadata)
    if parameters.get("greengrass"):
        publisher, args, path = "greengrass", (topic, payload, qos), "greengrass"
    else:
        host = str(parameters["broker_host"])
        port = int(parameters.get("broker_port", DEFAULT_MQTT_PORT))
        if not parameters.get("aws_iot"):
            publisher, args, path = "mqtt", (host, port, topic, payload, qos), "plain"
        else:
            if port == DEFAULT_MQTT_PORT:
                port = AWS_IOT_TLS_PORT
            qos = min(qos, AWS_IOT_MAX_QOS)
            tls = {
                "ca_certs": str(parameters["iot_ca_cert_path"]),
                "certfile": str(parameters["iot_client_cert_path"]),
                "keyfile": str(parameters["iot_private_key_path"]),
            }
            publisher = "mqtt"
            args = (host, port, topic, payload, qos,
                    str(parameters["iot_thing_name"]), tls)
            path = "aws_iot"
    detail = "sent to topic '{0}' (qos {1}, {2}): {3}".format(
        topic, qos, path, _preview(payload))
    return publisher, args, path, detail


def _greengrass(**extra):
    return dict({"topic": _TOPIC, "greengrass": True}, **extra)


def _plain(**extra):
    return dict({"topic": _TOPIC, "broker_host": "10.0.0.12"}, **extra)


def _aws_iot(**extra):
    return dict({"topic": _TOPIC, "broker_host": "endpoint.iot",
                 "aws_iot": True}, **_CERTS, **extra)


# ---------------------------------------------------------------------------
# Per-path retain ON (P2) and OFF/absent (P1) — concrete anchors
# ---------------------------------------------------------------------------

class TestRetainOnPerPath:
    """# Validates: Requirements 4.1, 4.2, 4.3, 4.4, 5.1, 5.2"""

    def test_greengrass_retain_true_passes_keyword_only(self):
        params = _greengrass(qos=1, retain=True)
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert mqtt == []
        assert gg == [(ref_args, {"retain": True})]
        assert gg[0][0] == (_TOPIC, _payload_text(params, METADATA), 1)
        assert detail == ref_detail.replace(
            ", greengrass)", ", greengrass, retained)")
        assert "(qos 1, greengrass, retained)" in detail

    def test_plain_broker_retain_true_passes_keyword_only(self):
        params = _plain(broker_port=1884, qos=2, retain=True)
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert gg == []
        assert mqtt == [(ref_args, {"retain": True})]
        assert mqtt[0][0] == (
            "10.0.0.12", 1884, _TOPIC, _payload_text(params, METADATA), 2)
        assert detail == ref_detail.replace(", plain)", ", plain, retained)")

    def test_aws_iot_retain_true_passes_keyword_only(self):
        params = _aws_iot(qos=2, retain=True)
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert gg == []
        assert mqtt == [(ref_args, {"retain": True})]
        # The aws_iot positional shape (port switch, qos clamp, tls dict)
        # is exactly the pinned one.
        assert mqtt[0][0][1] == AWS_IOT_TLS_PORT
        assert mqtt[0][0][4] == AWS_IOT_MAX_QOS
        assert mqtt[0][0][5] == "edge-device-01"
        assert detail == ref_detail.replace(", aws_iot)", ", aws_iot, retained)")


class TestRetainOffPerPath:
    """# Validates: Requirements 4.5, 4.6, 5.3, 10.1"""

    @pytest.mark.parametrize("retain_value", [None, False])
    def test_greengrass_off_or_absent_is_pre_feature_call(self, retain_value):
        params = _greengrass(qos=1)
        if retain_value is not None:
            params["retain"] = retain_value
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert gg == [(ref_args, {})]
        assert mqtt == []
        assert detail == ref_detail
        assert "retained" not in detail

    @pytest.mark.parametrize("retain_value", [None, False])
    def test_plain_off_or_absent_is_pre_feature_call(self, retain_value):
        params = _plain()
        if retain_value is not None:
            params["retain"] = retain_value
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert mqtt == [(ref_args, {})]
        assert detail == ref_detail == (
            "sent to topic 'factory/line1/inspection' (qos 0, plain): "
            '{"confidence": 0.9, "is_anomalous": true}')

    @pytest.mark.parametrize("retain_value", [None, False])
    def test_aws_iot_off_or_absent_is_pre_feature_call(self, retain_value):
        params = _aws_iot()
        if retain_value is not None:
            params["retain"] = retain_value
        gg, mqtt, detail = _run(params)
        _, ref_args, _, ref_detail = _reference(params, METADATA)
        assert mqtt == [(ref_args, {})]
        assert detail == ref_detail
        assert "retained" not in detail

    def test_document_level_binding_retain_true_reaches_publisher(self):
        # Through the public processor entry point (a compiled document),
        # not only the private helper.
        greengrass = _Recorder()
        processor = OutputBindingProcessor(
            mqtt_publisher=_Recorder(), greengrass_publisher=greengrass)
        document = {
            "schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
            "targetArch": "aarch64-jp7", "segments": [],
            "executorBindings": [{
                "nodeId": "m1", "binding": "mqtt_publish",
                "parameters": _greengrass(retain=True),
            }],
            "pluginDependencies": [],
        }
        processor(None, document, dict(METADATA))
        assert greengrass.calls == [(
            (_TOPIC, '{"confidence": 0.9, "is_anomalous": true}', 0),
            {"retain": True},
        )]


# ---------------------------------------------------------------------------
# P1 / P2 as a Hypothesis property over (greengrass, aws_iot, qos, port, retain)
# ---------------------------------------------------------------------------

_RETAIN = st.sampled_from(["absent", False, True])
_QOS = st.one_of(st.none(), st.sampled_from([0, 1, 2]))
_PORT = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_META = st.fixed_dictionaries({
    "is_anomalous": st.booleans(),
    "confidence": st.floats(min_value=0.0, max_value=1.0),
})


@st.composite
def _config(draw):
    greengrass = draw(st.booleans())
    aws_iot = draw(st.booleans())
    params = {"topic": draw(st.sampled_from([_TOPIC, "dda/results", "a/b/c"]))}
    if greengrass:
        params["greengrass"] = True
    else:
        params["broker_host"] = draw(st.sampled_from(["10.0.0.12", "broker.local"]))
        if aws_iot:
            params["aws_iot"] = True
            params.update(_CERTS)
        elif draw(st.booleans()):
            params["aws_iot"] = False
    qos, port = draw(_QOS), draw(_PORT)
    if qos is not None:
        params["qos"] = qos
    if port is not None:
        params["broker_port"] = port
    retain = draw(_RETAIN)
    if retain != "absent":
        params["retain"] = retain
    return params


@settings(max_examples=60)
@given(config=_config(), metadata=_META)
def test_property_retain_changes_only_kwargs_and_detail_flag(config, metadata):
    """# Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3

    P1: retain absent/false -> exactly the recorded pre-feature call
    (``kwargs == {}``) and detail. P2: retain true -> same positional
    args, ``kwargs == {"retain": True}``, detail with ``, retained``
    appended to the path segment. Nothing else differs between the two.
    """
    publisher, ref_args, path, ref_detail = _reference(config, metadata)
    gg, mqtt, detail = _run(config, metadata)
    calls = gg if publisher == "greengrass" else mqtt
    other = mqtt if publisher == "greengrass" else gg
    assert other == []
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ref_args
    if config.get("retain") is True:
        assert kwargs == {"retain": True}
        assert detail == ref_detail.replace(
            ", {0})".format(path), ", {0}, retained)".format(path))
        assert detail != ref_detail
    else:
        assert kwargs == {}
        assert detail == ref_detail


# ---------------------------------------------------------------------------
# _default_mqtt_publisher against a fake paho: retain forwarded only when true
# ---------------------------------------------------------------------------

def _fake_paho(single):
    publish_module = types.ModuleType("paho.mqtt.publish")
    publish_module.single = single
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.publish = publish_module
    paho_module = types.ModuleType("paho")
    paho_module.mqtt = mqtt_module
    return {"paho": paho_module, "paho.mqtt": mqtt_module,
            "paho.mqtt.publish": publish_module}


class TestDefaultMqttPublisherRetain:
    """# Validates: Requirements 4.2, 4.3, 4.6"""

    def _publish(self, **kwargs):
        published = []

        def single(topic, **call_kwargs):
            published.append((topic, call_kwargs))

        with patch.dict(sys.modules, _fake_paho(single)):
            _default_mqtt_publisher(
                "10.0.0.12", 1883, _TOPIC, "payload", 1, **kwargs)
        return published

    def test_retain_true_forwards_retain_to_paho(self):
        published = self._publish(retain=True)
        assert published == [(_TOPIC, {
            "payload": "payload", "qos": 1, "hostname": "10.0.0.12",
            "port": 1883, "client_id": "", "tls": None, "retain": True,
        })]

    def test_retain_false_or_absent_is_pre_feature_paho_call(self):
        expected = [(_TOPIC, {
            "payload": "payload", "qos": 1, "hostname": "10.0.0.12",
            "port": 1883, "client_id": "", "tls": None,
        })]
        assert self._publish() == expected
        assert self._publish(retain=False) == expected

    def test_retain_is_keyword_only(self):
        with pytest.raises(TypeError):
            with patch.dict(sys.modules, _fake_paho(lambda *a, **k: None)):
                _default_mqtt_publisher(
                    "h", 1883, "t", "p", 0, None, None, True)  # positional


# ---------------------------------------------------------------------------
# _default_greengrass_publisher against a fake awsiot IPC
# ---------------------------------------------------------------------------

def _fake_awsiot(*, with_retain_field, raise_unauthorized=False):
    """Fake ``awsiot`` tree in the shape ``_default_greengrass_publisher``
    imports lazily. The request records attribute assignments so the
    tests can assert ``retain`` is set only when requested; when
    ``with_retain_field`` is false the request class models an SDK that
    predates ``PublishToIoTCoreRequest.retain``."""
    model = types.ModuleType("awsiot.greengrasscoreipc.model")

    class QOS:
        AT_MOST_ONCE = 0
        AT_LEAST_ONCE = 1

    class PublishToIoTCoreRequest:
        """Mirrors the SDK constructor (fields default to None) and then
        records every attribute the publisher assigns afterwards."""

        def __init__(self):
            self.topic_name = None
            self.payload = None
            self.qos = None
            if with_retain_field:
                self.retain = None
            # Only assignments made by the code under test are recorded.
            self.__dict__["assigned"] = []

        def __setattr__(self, name, value):
            if "assigned" in self.__dict__:
                self.__dict__["assigned"].append(name)
            object.__setattr__(self, name, value)

    class UnauthorizedError(Exception):
        pass

    model.QOS = QOS
    model.PublishToIoTCoreRequest = PublishToIoTCoreRequest
    model.UnauthorizedError = UnauthorizedError

    state = {"operations": 0, "connects": 0, "request": None}

    class _Future:
        def result(self, timeout=None):
            if raise_unauthorized:
                raise UnauthorizedError("UnauthorizedError")
            return None

    class _Operation:
        def activate(self, request):
            state["request"] = request

        def get_response(self):
            return _Future()

    class _IpcClient:
        def new_publish_to_iot_core(self):
            state["operations"] += 1
            return _Operation()

    def connect():
        state["connects"] += 1
        return _IpcClient()

    greengrasscoreipc = types.ModuleType("awsiot.greengrasscoreipc")
    greengrasscoreipc.model = model
    greengrasscoreipc.connect = connect
    awsiot = types.ModuleType("awsiot")
    awsiot.greengrasscoreipc = greengrasscoreipc
    modules = {
        "awsiot": awsiot,
        "awsiot.greengrasscoreipc": greengrasscoreipc,
        "awsiot.greengrasscoreipc.model": model,
    }
    return modules, state


class TestDefaultGreengrassPublisherRetain:
    """# Validates: Requirements 4.4, 4.5, 4.8, 6.1, 6.2"""

    def test_retain_true_sets_retain_on_request(self):
        modules, state = _fake_awsiot(with_retain_field=True)
        with patch.dict(sys.modules, modules):
            _default_greengrass_publisher(_TOPIC, "payload", 1, retain=True)
        request = state["request"]
        assert request.topic_name == _TOPIC
        assert request.payload == b"payload"
        assert request.qos == 1
        assert request.retain is True
        assert request.assigned == ["topic_name", "payload", "qos", "retain"]
        assert state["operations"] == 1

    @pytest.mark.parametrize("kwargs", [{}, {"retain": False}])
    def test_retain_off_never_assigns_retain(self, kwargs):
        modules, state = _fake_awsiot(with_retain_field=True)
        with patch.dict(sys.modules, modules):
            _default_greengrass_publisher(_TOPIC, "payload", 0, **kwargs)
        request = state["request"]
        # Exactly the pre-feature three assignments; the SDK default
        # (None) is left untouched.
        assert request.assigned == ["topic_name", "payload", "qos"]
        assert request.retain is None
        assert request.qos == 0

    def test_retain_off_on_sdk_without_field_still_publishes(self):
        # Pre-feature behaviour on an old SDK is unaffected by the guard.
        modules, state = _fake_awsiot(with_retain_field=False)
        with patch.dict(sys.modules, modules):
            _default_greengrass_publisher(_TOPIC, "payload", 0)
        assert state["operations"] == 1
        assert not hasattr(state["request"], "retain")

    def test_retain_true_on_sdk_without_field_fails_before_ipc(self):
        modules, state = _fake_awsiot(with_retain_field=False)
        with patch.dict(sys.modules, modules):
            with pytest.raises(RuntimeError) as excinfo:
                _default_greengrass_publisher(
                    _TOPIC, "payload", 0, retain=True)
        message = str(excinfo.value)
        assert _TOPIC in message
        assert "retain" in message
        assert "awsiotsdk" in message
        # Nothing was sent: no IPC connection, no operation.
        assert state["connects"] == 0
        assert state["operations"] == 0
        assert state["request"] is None

    def test_denial_text_names_retain_publish_only_when_retained(self):
        retained_modules, _ = _fake_awsiot(
            with_retain_field=True, raise_unauthorized=True)
        with patch.dict(sys.modules, retained_modules):
            with pytest.raises(RuntimeError) as retained:
                _default_greengrass_publisher(
                    _TOPIC, "payload", 0, retain=True)
        plain_modules, _ = _fake_awsiot(
            with_retain_field=True, raise_unauthorized=True)
        with patch.dict(sys.modules, plain_modules):
            with pytest.raises(RuntimeError) as plain:
                _default_greengrass_publisher(_TOPIC, "payload", 0)

        retained_text, plain_text = str(retained.value), str(plain.value)
        # Both keep the existing accessControl diagnosis naming the topic.
        for text in (retained_text, plain_text):
            assert _TOPIC in text
            assert "aws.greengrass.ipc.mqttproxy accessControl" in text
        assert "iot:RetainPublish" in retained_text
        assert "iot:RetainPublish" not in plain_text
        # The retained text is the plain text plus the appended sentence.
        assert retained_text.startswith(plain_text)
        assert isinstance(retained.value.__cause__,
                          retained_modules["awsiot.greengrasscoreipc.model"].UnauthorizedError)


# ---------------------------------------------------------------------------
# Unit anchor for the detail composer
# ---------------------------------------------------------------------------

class TestMqttDetailRetainFlag:
    """# Validates: Requirements 5.1, 5.2, 5.3"""

    def test_default_is_pre_feature_string(self):
        assert OutputBindingProcessor._mqtt_detail("t", 1, "plain", "p") == (
            "sent to topic 't' (qos 1, plain): p")
        assert OutputBindingProcessor._mqtt_detail(
            "t", 1, "plain", "p", False) == "sent to topic 't' (qos 1, plain): p"

    def test_retained_appends_flag_after_path(self):
        for path in ("plain", "aws_iot", "greengrass"):
            assert OutputBindingProcessor._mqtt_detail(
                "t", 0, path, "p", True
            ) == "sent to topic 't' (qos 0, {0}, retained): p".format(path)

    def test_retained_detail_still_previews_payload(self):
        long_payload = "x" * 2000
        detail = OutputBindingProcessor._mqtt_detail(
            "t", 0, "greengrass", long_payload, True)
        assert detail.startswith("sent to topic 't' (qos 0, greengrass, retained): ")
        assert detail.endswith(_preview(long_payload))
