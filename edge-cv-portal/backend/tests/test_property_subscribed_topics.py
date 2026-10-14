"""Subscribed-topics packaging and deployment accessControl merge.

# Feature: trigger-activation-runtime, Property 16: Subscribed-topics packaging and accessControl merge

For any generated workflow definition, the recorded ``subscribed_topics``
equal the sorted, de-duplicated ``topic`` values of its greengrass-enabled
``mqtt_subscribe`` nodes (and are absent when that set is empty, leaving
packaging output byte-identical to pre-feature); and for any non-empty
recorded topic set, the deployment merge produces a ``SubscribeToIoTCore``
policy whose resources equal exactly those topics under a workflow-unique
policy key, leaving all other accessControl content untouched.

**Validates: Requirements 10.1, 10.2, 10.4**

Both halves are pure over their inputs:

- ``workflow_packaging.gather_subscribed_topics(definition)`` reads only
  the definition dict;
- ``deployments.apply_subscribe_access_control(components_map)`` mutates
  only the components map and resolves recorded topics through
  ``workflow_guards.get_version_item`` — swapped here for a fake returning
  generated version items (deployments.py binds the module, not the
  function, so a module-attribute swap intercepts the call).

The modules are imported through the shared moto-backed session fixture
only so their module-level boto3 clients are intercepted and the real
``shared_utils`` layer backs the import, mirroring the other packaging /
deployment property tests in this directory.
"""

from __future__ import annotations

import copy
import json
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Module fixtures (moto-backed session stack from conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ---------------------------------------------------------------------------
# Reference expectation, restated from Requirement 10.1 — deliberately NOT
# derived from the implementation: the sorted, de-duplicated set of valid
# (non-blank string) ``topic`` values of the definition's ``mqtt_subscribe``
# nodes whose effective ``greengrass`` is truthy (explicit value when the
# key is present — explicit null counts as cleared — else the declared
# default, false).
# ---------------------------------------------------------------------------


def expected_subscribed_topics(definition: dict) -> list:
    topics = set()
    for node in definition.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "mqtt_subscribe":
            continue
        parameters = node.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        greengrass = (parameters["greengrass"]
                      if "greengrass" in parameters else False)
        if not greengrass:
            continue
        topic = parameters.get("topic")
        if isinstance(topic, str) and topic.strip():
            topics.add(topic)
    return sorted(topics)


# ---------------------------------------------------------------------------
# Strategies — definitions mixing greengrass/aws_iot/broker subscribe nodes,
# duplicate topics, blank/non-string topics, and non-trigger nodes.
# ---------------------------------------------------------------------------

#: A small pool of realistic topic filters, so duplicates across nodes are
#: common (exercising de-duplication), plus free-form topic text.
TOPIC_POOL = [
    "dda/trigger/start",
    "dda/trigger/start",  # doubled weight: duplicates are the point
    "factory/line1/+/state",
    "sensors/#",
    "a",
    " padded/topic ",  # non-blank, kept verbatim (strip() only gates)
]

valid_topics = st.one_of(
    st.sampled_from(TOPIC_POOL),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/+#_-",
            min_size=1, max_size=20),
)

#: Blank or non-string topics: excluded from recording (validation's
#: problem, not packaging's).
invalid_topics = st.sampled_from(["", "   ", "\t\n", None, 7, 1.5, True])

#: How the ``greengrass`` parameter appears on a node: explicit true,
#: explicit false, absent, or explicit null (cleared).
greengrass_states = st.sampled_from(["true", "false", "absent", "null"])


@st.composite
def mqtt_subscribe_nodes(draw, index):
    """An ``mqtt_subscribe`` node mixing transports and topic validity."""
    parameters = {}
    topic_valid = draw(st.booleans())
    parameters["topic"] = draw(valid_topics if topic_valid else invalid_topics)

    greengrass = draw(greengrass_states)
    if greengrass == "true":
        parameters["greengrass"] = True
    elif greengrass == "false":
        parameters["greengrass"] = False
    elif greengrass == "null":
        parameters["greengrass"] = None
    # "absent": key not present at all

    # aws_iot / broker-only transports never contribute to the recording,
    # whatever their values.
    if draw(st.booleans()):
        parameters["aws_iot"] = draw(st.booleans())
    if draw(st.booleans()):
        parameters["broker_host"] = draw(st.sampled_from(
            ["broker.local", "10.0.0.7", ""]))
        parameters["broker_port"] = 1883

    return {
        "id": f"trig-{index}",
        "type": "mqtt_subscribe",
        "position": {"x": 0, "y": index * 100},
        "parameters": parameters,
    }


@st.composite
def non_trigger_nodes(draw, index):
    """A non-mqtt_subscribe node. mqtt_publish deliberately carries a
    greengrass-true ``topic`` — it must never be recorded (type-keyed)."""
    node_type = draw(st.sampled_from(
        ["folder_source", "mqtt_publish", "digital_input", "custom_python",
         "opcua_subscribe"]))
    parameters = {}
    if node_type == "mqtt_publish":
        parameters = {"topic": draw(valid_topics), "greengrass": True}
    elif node_type == "opcua_subscribe":
        parameters = {"endpoint": "opc.tcp://plc:4840", "node_id": "ns=2;i=1"}
    elif node_type == "folder_source":
        parameters = {"location": "/aws_dda/images"}
    node = {
        "id": f"node-{index}",
        "type": node_type,
        "position": {"x": 200, "y": index * 100},
        "parameters": parameters,
    }
    # Occasionally a node with no parameters key at all (tolerated shape).
    if draw(st.booleans()):
        node.pop("parameters")
    return node


@st.composite
def workflow_definitions(draw):
    """A Workflow_Definition dict mixing subscribe and non-trigger nodes."""
    node_count = draw(st.integers(min_value=0, max_value=6))
    nodes = []
    for index in range(node_count):
        if draw(st.booleans()):
            nodes.append(draw(mqtt_subscribe_nodes(index)))
        else:
            nodes.append(draw(non_trigger_nodes(index)))
    return {"schemaVersion": 1, "nodes": nodes, "connections": []}


@st.composite
def non_subscribing_definitions(draw):
    """Definitions guaranteed to record nothing: every mqtt_subscribe node
    has greengrass off (false/absent/null) or an invalid topic."""
    definition = draw(workflow_definitions())
    for node in definition["nodes"]:
        if node.get("type") != "mqtt_subscribe":
            continue
        parameters = node.get("parameters") or {}
        if draw(st.booleans()):
            # Kill the transport: greengrass false / absent / null.
            state = draw(st.sampled_from(["false", "absent", "null"]))
            if state == "false":
                parameters["greengrass"] = False
            elif state == "null":
                parameters["greengrass"] = None
            else:
                parameters.pop("greengrass", None)
        else:
            # Kill the topic: blank or non-string.
            parameters["topic"] = draw(invalid_topics)
        node["parameters"] = parameters
    return definition


# ---------------------------------------------------------------------------
# Packaging half — Requirement 10.1, 10.4
# ---------------------------------------------------------------------------


class TestSubscribedTopicsRecording:
    """# Feature: trigger-activation-runtime, Property 16: Subscribed-topics packaging and accessControl merge

    **Validates: Requirements 10.1, 10.4**
    """

    @settings(max_examples=100, deadline=None)
    @given(definition=workflow_definitions())
    def test_recorded_topics_equal_greengrass_enabled_topic_set(
            self, packaging, definition):
        """The recorded topics equal the sorted, de-duplicated valid
        ``topic`` values of greengrass-enabled ``mqtt_subscribe`` nodes —
        aws_iot/broker-only subscribe nodes, blank/non-string topics, and
        non-trigger nodes contribute nothing."""
        assert (packaging.gather_subscribed_topics(definition)
                == expected_subscribed_topics(definition))

    @settings(max_examples=100, deadline=None)
    @given(definition=non_subscribing_definitions())
    def test_empty_set_is_absent_and_manifest_byte_identical(
            self, packaging, definition):
        """When no greengrass-enabled subscribe node records a topic, the
        gathered set is empty and a manifest built with it is byte-identical
        to the pre-feature manifest (built without the ``subscribed_topics``
        argument at all) — no key appears."""
        topics = packaging.gather_subscribed_topics(definition)
        assert topics == []

        # Freeze the timestamp so the byte comparison is deterministic.
        original_now_ms = packaging.now_ms
        packaging.now_ms = lambda: 1700000000000
        try:
            user = {"user_id": "prop16-user"}
            with_empty = packaging.build_manifest(
                "wf-prop16", 3, "arm64_jp6", ["plugin-a"], ["paho-mqtt"],
                [], user, workflow_name="Prop 16",
                subscribed_topics=topics)
            pre_feature = packaging.build_manifest(
                "wf-prop16", 3, "arm64_jp6", ["plugin-a"], ["paho-mqtt"],
                [], user, workflow_name="Prop 16")
        finally:
            packaging.now_ms = original_now_ms

        assert "subscribed_topics" not in with_empty
        assert json.dumps(with_empty) == json.dumps(pre_feature)

    def test_nonempty_set_is_recorded_on_manifest(self, packaging):
        """Sanity anchor for the non-empty side of 10.1: a non-empty set
        lands verbatim under ``subscribed_topics``."""
        original_now_ms = packaging.now_ms
        packaging.now_ms = lambda: 1700000000000
        try:
            manifest = packaging.build_manifest(
                "wf-prop16", 3, "arm64_jp6", [], [], [],
                {"user_id": "prop16-user"},
                subscribed_topics=["a/b", "c/#"])
        finally:
            packaging.now_ms = original_now_ms
        assert manifest["subscribed_topics"] == ["a/b", "c/#"]


# ---------------------------------------------------------------------------
# Deployment-merge half — Requirements 10.2, 10.4
# ---------------------------------------------------------------------------

workflow_ids = st.text(
    alphabet="abcdef0123456789", min_size=4, max_size=10
).map(lambda s: f"wf-{s}")

#: Per-workflow recording: a non-empty sorted de-duplicated topic list
#: (what the packager records), or None for a version item without the
#: field (pre-feature packages / non-subscribing workflows).
recorded_topic_sets = st.one_of(
    st.none(),
    st.lists(valid_topics, min_size=1, max_size=4, unique=True)
    .map(sorted),
)

LOCAL_SERVER_NAMES = [
    "aws.edgeml.dda.LocalServer.arm64JP6",
    "aws.edgeml.dda.LocalServer.arm64JP5",
    "aws.edgeml.dda.LocalServer.amd64",
]

#: Pre-existing configurationUpdate.merge content on the LocalServer
#: entry: absent, empty, or carrying accessControl policies and other
#: config keys the merge must leave untouched.
EXISTING_MERGE_DOCS = [
    None,
    {},
    {"logLevel": "DEBUG"},
    {
        "accessControl": {
            "aws.greengrass.ipc.mqttproxy": {
                "aws.edgeml.dda.LocalServer:mqttproxy:1": {
                    "operations": ["aws.greengrass#PublishToIoTCore"],
                    "resources": ["dda/#"],
                },
            },
            "aws.greengrass.ipc.pubsub": {
                "aws.edgeml.dda.LocalServer:pubsub:1": {
                    "operations": ["aws.greengrass#SubscribeToTopic"],
                    "resources": ["*"],
                },
            },
        },
        "logLevel": "INFO",
    },
]


@st.composite
def deployment_scenarios(draw):
    """A deployment component set: workflow components with generated
    version items (with/without recorded topics), optionally the
    LocalServer component (optionally with pre-existing merge content),
    plus unrelated components."""
    recordings = draw(st.dictionaries(
        workflow_ids, recorded_topic_sets, min_size=0, max_size=3))
    include_local_server = draw(st.booleans())
    local_server_name = draw(st.sampled_from(LOCAL_SERVER_NAMES))
    existing_merge = draw(st.sampled_from(EXISTING_MERGE_DOCS))
    include_extra = draw(st.booleans())

    components_map = {}
    version_items = {}
    for workflow_id, topics in recordings.items():
        version = draw(st.integers(min_value=1, max_value=20))
        components_map[f"dda.workflow.{workflow_id}"] = {
            "componentVersion": f"{version}.0.0",
        }
        item = {"workflow_id": workflow_id, "version": version,
                "validation_status": {"status": "passed"}}
        if topics is not None:
            item["subscribed_topics"] = list(topics)
        version_items[(workflow_id, version)] = item

    if include_local_server:
        entry = {"componentVersion": "1.0.99"}
        if existing_merge is not None:
            entry["configurationUpdate"] = {
                "merge": json.dumps(existing_merge)}
        components_map[local_server_name] = entry

    if include_extra:
        components_map["aws.greengrass.Nucleus"] = {
            "componentVersion": "2.12.0"}
        components_map["dda.plugin.abc123"] = {"componentVersion": "1.0.0"}

    expected = {workflow_id: topics
                for workflow_id, topics in recordings.items()
                if topics}
    return (components_map, version_items, expected,
            local_server_name if include_local_server else None)


SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"
MQTTPROXY = "aws.greengrass.ipc.mqttproxy"


class TestSubscribeAccessControlMerge:
    """# Feature: trigger-activation-runtime, Property 16: Subscribed-topics packaging and accessControl merge

    **Validates: Requirements 10.2, 10.4**
    """

    @settings(max_examples=100, deadline=None)
    @given(scenario=deployment_scenarios())
    def test_merge_policy_exactness_warnings_and_non_interference(
            self, deployments, scenario):
        """For any component set: with recorded topics and LocalServer
        present, the LocalServer merge carries one
        ``dda:workflow-subscribe:<workflowId>`` policy per subscribing
        workflow with operations/resources exactly as recorded and every
        other key untouched; with LocalServer absent, one actionable
        warning per subscribing workflow and no mutation; with nothing
        recorded, a byte-identical no-op."""
        components_map, version_items, expected, local_server_name = scenario
        before = copy.deepcopy(components_map)

        def fake_get_version_item(workflow_id, version):
            return version_items.get((workflow_id, version))

        original = deployments.workflow_guards.get_version_item
        deployments.workflow_guards.get_version_item = fake_get_version_item
        try:
            warnings = deployments.apply_subscribe_access_control(
                components_map)
        finally:
            deployments.workflow_guards.get_version_item = original

        if not expected:
            # Requirement 10.4: byte-identical no-op — zero new keys.
            assert warnings == []
            assert components_map == before
            assert json.dumps(components_map, sort_keys=True) == json.dumps(
                before, sort_keys=True)
            return

        if local_server_name is None:
            # Requirement 10.3-adjacent guard on the 10.2 path: nowhere to
            # attach — actionable warnings, one per subscribing workflow in
            # sorted order, naming the workflow and its topics; the
            # component set is untouched.
            assert components_map == before
            assert len(warnings) == len(expected)
            for warning, (workflow_id, topics) in zip(
                    warnings, sorted(expected.items())):
                assert workflow_id in warning
                assert "LocalServer" in warning
                for topic in topics:
                    assert topic in warning
            return

        # Requirement 10.2: the policy lands on the LocalServer entry.
        assert warnings == []
        entry = components_map[local_server_name]
        merge_doc = json.loads(entry["configurationUpdate"]["merge"])
        policies = merge_doc["accessControl"][MQTTPROXY]

        existing_merge_json = (
            (before[local_server_name].get("configurationUpdate") or {})
            .get("merge"))
        existing_merge = (json.loads(existing_merge_json)
                          if existing_merge_json else {})
        existing_policies = (existing_merge.get("accessControl") or {}).get(
            MQTTPROXY) or {}

        # Workflow-unique policy keys with exact operations/resources.
        for workflow_id, topics in expected.items():
            policy = policies[f"dda:workflow-subscribe:{workflow_id}"]
            assert policy["operations"] == [SUBSCRIBE_OPERATION]
            assert policy["resources"] == list(topics)

        # Non-interference: the policy key set is exactly the pre-existing
        # keys plus the new workflow-unique keys, and every pre-existing
        # policy survives byte-for-byte.
        assert set(policies) == set(existing_policies) | {
            f"dda:workflow-subscribe:{workflow_id}"
            for workflow_id in expected}
        for key, policy in existing_policies.items():
            assert policies[key] == policy

        # Every other merge-document key (other accessControl services,
        # logLevel, ...) is untouched.
        for key, value in existing_merge.items():
            if key == "accessControl":
                for service, service_policies in value.items():
                    if service != MQTTPROXY:
                        assert merge_doc["accessControl"][service] == \
                            service_policies
            else:
                assert merge_doc[key] == value

        # Every other component entry — workflows, Nucleus, plugins — is
        # byte-identical to its pre-merge content.
        for name, before_entry in before.items():
            if name != local_server_name:
                assert components_map[name] == before_entry
        # And the LocalServer entry changed ONLY in configurationUpdate.
        for key, value in before[local_server_name].items():
            if key != "configurationUpdate":
                assert entry[key] == value
