"""Bug 2 exploration test — topic-only Greengrass MQTT publish config.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2).

The `mqtt_publish` node can only target a plain MQTT broker (required
`broker_host`) or AWS IoT Core over mutual TLS (certificate file paths).
There is no zero-configuration path that publishes through the on-device
Greengrass-managed MQTT so that the user only has to supply the topic.

This is an EXPLORATION test written against the UNFIXED code: it asserts
the CORRECTED behavior (Property 2 — Bug Condition), so it is EXPECTED TO
FAIL on the current catalog/validator — there is no `greengrass`
parameter and `broker_host` is `required=True`, so a topic-only config is
rejected with `V4_MISSING_REQUIRED_PARAMETER` for `broker_host`. The
failure confirms the bug exists.

Property 2: Bug Condition — MQTT publish through Greengrass with only a topic
  For any mqtt_publish configuration where isBugCondition2 holds (the
  user wants Greengrass-managed publishing and no topic-only config is
  currently valid), the fixed system SHALL accept a Greengrass publish
  configuration supplying only the topic — valid without `broker_host`,
  `broker_port`, or any `iot_*` certificate path — and SHALL package it
  as a valid output node.

Validates: Requirements 1.2, 2.2
"""

from workflow_core.catalog import get_node_type
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    SEVERITY_ERROR,
    validate,
)

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _parameter(descriptor, name):
    for param in descriptor.parameters:
        if param.name == name:
            return param
    return None


def _errors_for_node(findings, node_id):
    return [
        f for f in findings
        if f.severity == SEVERITY_ERROR and f.node_id == node_id
    ]


def _topic_only_greengrass_graph():
    """folder_source -> model_inference -> mqtt_publish(topic + greengrass).

    A frame source feeds inference (VideoFrames -> InferenceMeta) which
    feeds the mqtt_publish input (InferenceMeta). The mqtt_publish node
    supplies only the `topic` plus the (not-yet-existing) `greengrass`
    option — no broker host/port and no certificate paths.
    """
    return WorkflowGraph(
        nodes=[
            _node("src", "folder_source", location="/data/images"),
            _node("inf", "model_inference", modelName="widget-anomaly-v3"),
            _node(
                "mqtt", "mqtt_publish",
                topic="factory/line1/inspection",
                greengrass=True,
            ),
        ],
        connections=[
            _conn("c1", "src", "inf"),
            _conn("c2", "inf", "mqtt"),
        ],
    )


class TestBug2MqttGreengrassConfig:
    """Property 2: Bug Condition — Greengrass publish with only a topic."""

    def test_mqtt_publish_has_greengrass_parameter(self):
        # Fix-checking assertion: a `greengrass` option SHALL exist so the
        # user can request zero-config Greengrass-managed publishing.
        # EXPECTED OUTCOME on UNFIXED code: FAILS — no such parameter.
        descriptor = get_node_type("mqtt_publish")
        assert descriptor is not None
        greengrass = _parameter(descriptor, "greengrass")
        assert greengrass is not None, (
            "mqtt_publish has no `greengrass` parameter"
        )
        assert greengrass.required is False
        assert greengrass.default in (False, None)

    def test_broker_host_not_required(self):
        # Fix-checking assertion: with the Greengrass option, `broker_host`
        # SHALL NOT be statically required (a topic-only config is valid).
        # EXPECTED OUTCOME on UNFIXED code: FAILS — broker_host is required.
        descriptor = get_node_type("mqtt_publish")
        broker_host = _parameter(descriptor, "broker_host")
        assert broker_host is not None
        assert broker_host.required is False, (
            "broker_host is required, so a topic-only config is rejected"
        )

    def test_topic_only_greengrass_config_validates(self):
        # Fix-checking assertion: a topic-only Greengrass config SHALL
        # validate with no error on the mqtt_publish node — in particular
        # no V4_MISSING_REQUIRED_PARAMETER for broker_host.
        # EXPECTED OUTCOME on UNFIXED code: FAILS — validate() reports
        # V4_MISSING_REQUIRED_PARAMETER for broker_host.
        findings = validate(_topic_only_greengrass_graph())
        mqtt_errors = _errors_for_node(findings, "mqtt")
        missing_broker = [
            f for f in mqtt_errors
            if f.code == CODE_V4_MISSING_REQUIRED_PARAMETER
            and "broker_host" in f.message
        ]
        assert missing_broker == [], (
            "topic-only Greengrass config rejected for missing broker_host"
        )
        assert mqtt_errors == [], (
            "topic-only Greengrass config produced errors: "
            f"{[f.code for f in mqtt_errors]}"
        )
