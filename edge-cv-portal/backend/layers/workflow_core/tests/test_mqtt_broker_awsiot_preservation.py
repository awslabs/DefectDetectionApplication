"""Preservation property tests — MQTT plain-broker / ``aws_iot`` paths.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2, task 2.2).

**Property 6: Preservation** — behavior unchanged outside every bug
condition. Bug 2 adds an additive, off-by-default Greengrass publishing
option. Every ``mqtt_publish`` configuration that does NOT request
Greengrass publishing (``greengrass`` off/absent) must behave exactly as
it does today: the plain-broker and AWS IoT Core (``aws_iot``) paths must
still validate the same way and the node must still package the
``paho-mqtt`` runtime dependency.

This module follows the observation-first methodology: the invariants
below were observed on the UNFIXED tree and are asserted here as the
baseline the fix MUST preserve. They MUST PASS on the current (unfixed)
code and are re-run after the fix (task 4.6) to prove no regression.

Baselines captured (all observed on the UNFIXED catalog/validator):

1. Validation ACCEPT: a full ``folder_source -> model_inference ->
   mqtt_publish`` graph whose ``mqtt_publish`` node carries a non-empty
   ``broker_host`` and ``topic`` (``greengrass`` off) produces NO
   error-severity finding on the ``mqtt`` node — for both the plain
   broker path and the ``aws_iot`` path.

2. Validation REJECT: the same graph with the ``broker_host`` omitted
   (and ``greengrass`` off, ``aws_iot`` off) is rejected — at least one
   error-severity finding is reported on the ``mqtt`` node (today the
   code is ``V4_MISSING_REQUIRED_PARAMETER`` for ``broker_host``; the
   fix keeps the reject OUTCOME under a dedicated code).

3. Packaged dependency: every physical device-architecture mapping of the
   ``mqtt_publish`` descriptor binds to the ``mqtt_publish`` executor and
   lists the ``python:paho-mqtt`` runtime dependency; the ``sim`` mapping
   is the recording stub. (The Greengrass fix is additive — paho-mqtt
   must remain present, so membership rather than equality is asserted.)

**Validates: Requirements 3.2**
"""

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    ARCH_SIM,
    DEVICE_ARCHITECTURES,
    SIM_RECORDING_BINDING_PREFIX,
    get_node_type,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate

_POS = Position(0.0, 0.0)

#: The paho-mqtt runtime dependency the device mappings must package.
PAHO_DEPENDENCY = "python:paho-mqtt"


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, "out"),
        target=PortEndpoint(target_node, "in"),
    )


def _graph_with_mqtt(mqtt_parameters):
    """folder_source -> model_inference -> mqtt_publish(mqtt_parameters).

    A frame source feeds inference (VideoFrames -> InferenceMeta) which
    feeds the mqtt_publish input (InferenceMeta), so the graph is a valid
    end-to-end workflow and the only variable is the mqtt node config.
    """
    return WorkflowGraph(
        nodes=[
            _node("src", "folder_source", location="/data/images"),
            _node("inf", "model_inference", modelName="widget-anomaly-v3"),
            _node("mqtt", "mqtt_publish", **mqtt_parameters),
        ],
        connections=[
            _conn("c1", "src", "inf"),
            _conn("c2", "inf", "mqtt"),
        ],
    )


def _mqtt_errors(graph):
    return [
        f for f in validate(graph)
        if f.severity == SEVERITY_ERROR and f.node_id == "mqtt"
    ]


# ---------------------------------------------------------------------------
# Config generators (greengrass OFF — plain broker and aws_iot only)
# ---------------------------------------------------------------------------

#: Non-empty single-line strings for hostnames/topics/certificate paths.
_NONEMPTY_TEXT = st.text(min_size=1, max_size=40).filter(
    lambda s: s.strip() != ""
)
_TOPIC = st.one_of(
    st.sampled_from([
        "factory/line1/inspection",
        "dda/results",
        "a",
    ]),
    _NONEMPTY_TEXT,
)
_BROKER_HOST = st.one_of(
    st.sampled_from(["10.0.0.12", "broker.local"]),
    _NONEMPTY_TEXT,
)
_OPTIONAL_PORT = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_OPTIONAL_QOS = st.one_of(st.none(), st.sampled_from([0, 1, 2]))
_OPTIONAL_PAYLOAD = st.one_of(
    st.none(),
    st.sampled_from([
        "{inference_json}",
        "{is_anomalous}",
        "anomaly={is_anomalous} confidence={confidence}",
    ]),
)
_CERT_PATH = st.one_of(
    st.sampled_from([
        "/greengrass/v2/rootCA.pem",
        "/greengrass/v2/thingCert.crt",
        "/greengrass/v2/privKey.key",
    ]),
    _NONEMPTY_TEXT,
)


def _with_optionals(params, broker_port, qos, payload_template):
    if broker_port is not None:
        params["broker_port"] = broker_port
    if qos is not None:
        params["qos"] = qos
    if payload_template is not None:
        params["payload_template"] = payload_template
    return params


@st.composite
def plain_broker_config(draw):
    """A valid plain-MQTT-broker config: broker_host + topic (+ optionals),
    ``aws_iot`` off, ``greengrass`` absent."""
    params = {
        "broker_host": draw(_BROKER_HOST),
        "topic": draw(_TOPIC),
    }
    # aws_iot omitted or explicitly False — both are the plain-broker path.
    if draw(st.booleans()):
        params["aws_iot"] = False
    return _with_optionals(
        params,
        draw(_OPTIONAL_PORT),
        draw(_OPTIONAL_QOS),
        draw(_OPTIONAL_PAYLOAD),
    )


@st.composite
def aws_iot_config(draw):
    """A valid AWS IoT Core config: aws_iot on with thing name + the three
    device-local certificate paths (+ optionals), ``greengrass`` absent."""
    params = {
        "broker_host": draw(_BROKER_HOST),
        "topic": draw(_TOPIC),
        "aws_iot": True,
        "iot_thing_name": draw(_NONEMPTY_TEXT),
        "iot_ca_cert_path": draw(_CERT_PATH),
        "iot_client_cert_path": draw(_CERT_PATH),
        "iot_private_key_path": draw(_CERT_PATH),
    }
    return _with_optionals(
        params,
        draw(_OPTIONAL_PORT),
        draw(_OPTIONAL_QOS),
        draw(_OPTIONAL_PAYLOAD),
    )


# ---------------------------------------------------------------------------
# 1. Validation ACCEPT (plain broker + aws_iot)
# ---------------------------------------------------------------------------

class TestMqttValidationAcceptPreserved:
    """A broker-targeting config (host + topic, greengrass off) validates
    with no error on the mqtt node — the accept outcome the fix preserves."""

    @given(config=plain_broker_config())
    def test_plain_broker_config_accepted(self, config):
        assert _mqtt_errors(_graph_with_mqtt(config)) == [], (
            "plain-broker config unexpectedly rejected: {0}".format(config)
        )

    @given(config=aws_iot_config())
    def test_aws_iot_config_accepted(self, config):
        assert _mqtt_errors(_graph_with_mqtt(config)) == [], (
            "aws_iot config unexpectedly rejected: {0}".format(config)
        )


# ---------------------------------------------------------------------------
# 2. Validation REJECT (host-less plain config, greengrass off)
# ---------------------------------------------------------------------------

class TestMqttValidationRejectPreserved:
    """A host-less config with neither aws_iot nor greengrass is rejected —
    the reject OUTCOME the fix preserves (under a dedicated code)."""

    @given(
        topic=_TOPIC,
        qos=_OPTIONAL_QOS,
        payload_template=_OPTIONAL_PAYLOAD,
        aws_iot_present=st.booleans(),
    )
    def test_missing_broker_host_rejected(
        self, topic, qos, payload_template, aws_iot_present
    ):
        params = {"topic": topic}
        if aws_iot_present:
            params["aws_iot"] = False
        _with_optionals(params, None, qos, payload_template)
        errors = _mqtt_errors(_graph_with_mqtt(params))
        assert errors, (
            "host-less plain config (greengrass off) should be rejected; "
            "got no error on the mqtt node for {0}".format(params)
        )


# ---------------------------------------------------------------------------
# 3. Packaged dependency (paho-mqtt) preserved
# ---------------------------------------------------------------------------

class TestMqttPackagedDependencyPreserved:
    """Every device-arch mapping binds the mqtt_publish executor and
    packages paho-mqtt; the sim mapping is the recording stub."""

    def test_device_mappings_bind_executor_and_package_paho(self):
        descriptor = get_node_type("mqtt_publish")
        assert descriptor is not None
        by_arch = {m.arch: m for m in descriptor.mappings}

        for arch in DEVICE_ARCHITECTURES:
            mapping = by_arch.get(arch)
            assert mapping is not None, "no mqtt_publish mapping for {0}".format(arch)
            assert mapping.executor_binding == "mqtt_publish", (
                "device mapping for {0} lost the mqtt_publish executor "
                "binding".format(arch)
            )
            assert PAHO_DEPENDENCY in mapping.plugin_dependencies, (
                "device mapping for {0} no longer packages {1}".format(
                    arch, PAHO_DEPENDENCY)
            )

    def test_sim_mapping_is_recording_stub(self):
        descriptor = get_node_type("mqtt_publish")
        by_arch = {m.arch: m for m in descriptor.mappings}
        sim = by_arch.get(ARCH_SIM)
        assert sim is not None, "no sim mapping for mqtt_publish"
        assert sim.executor_binding == (
            SIM_RECORDING_BINDING_PREFIX + "mqtt_publish"
        )
