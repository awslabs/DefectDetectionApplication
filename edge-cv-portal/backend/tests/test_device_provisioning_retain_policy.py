"""mqtt-retained-publish — the default Greengrass device IoT policy grants
``iot:RetainPublish`` alongside ``iot:Publish`` and nothing else moves.

Spec: .kiro/specs/mqtt-retained-publish (task 4.1, Requirements 7.1, 7.3, 7.5).

``device_provisioning.create_default_policy`` is the one IoT policy the
portal generates for a core device. AWS IoT Core authorizes the MQTT
retain bit as a separate action (``iot:RetainPublish``), so a workflow
whose MQTT Publish node has "Retain message" enabled needs it in the
device's IoT policy. The action is added to the existing ``iot:Publish``
statement on the identical, thing-scoped resource list; every other
statement is byte-identical to the pre-feature document, and the action
never appears on a wildcard resource.

The policy is created against moto's IoT (conftest's ``aws_stack``
puts ``functions/`` and the shared layer on ``sys.path`` with fake
credentials) and read back with ``get_policy`` so the assertion covers
the document exactly as IoT Core would store it.
"""

import copy
import json
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest

REGION = "us-east-1"

# The pre-feature document (device_provisioning.create_default_policy before
# mqtt-retained-publish), transcribed verbatim. The ONLY intended delta is
# "iot:RetainPublish" appended to the iot:Publish statement's action list.
PRE_FEATURE_DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["iot:Connect"],
            "Resource": "arn:aws:iot:*:*:client/${iot:Connection.Thing.ThingName}",
        },
        {
            "Effect": "Allow",
            "Action": ["iot:Publish"],
            "Resource": [
                "arn:aws:iot:*:*:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/*"
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["iot:Subscribe"],
            "Resource": [
                "arn:aws:iot:*:*:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/*"
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["iot:Receive"],
            "Resource": [
                "arn:aws:iot:*:*:topic/$aws/things/${iot:Connection.Thing.ThingName}/*"
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "iot:GetThingShadow",
                "iot:UpdateThingShadow",
                "iot:DeleteThingShadow",
            ],
            "Resource": ["arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}"],
        },
    ],
}


def _expected_document():
    expected = copy.deepcopy(PRE_FEATURE_DOCUMENT)
    publish = expected["Statement"][1]
    assert publish["Action"] == ["iot:Publish"]
    publish["Action"] = ["iot:Publish", "iot:RetainPublish"]
    return expected


def _as_list(value):
    return value if isinstance(value, list) else [value]


@pytest.fixture(scope="module")
def generated_policy(aws_stack):
    """Run the real create_default_policy against moto IoT; return the
    stored document.

    The module builds its boto3 clients at import time, including an
    ``iot-data-plane`` client whose service name this host's botocore may
    not know; only the ``iot`` client is exercised here, so every other
    service is stubbed during the import."""
    real_client = boto3.client

    def _client(service_name, *args, **kwargs):
        if service_name == "iot":
            return real_client(service_name, *args, **kwargs)
        return MagicMock(name="boto3.client({0!r})".format(service_name))

    sys.modules.pop("device_provisioning", None)
    with patch("boto3.client", side_effect=_client):
        import device_provisioning

    policy_name = "test-dda-retain-policy"
    device_provisioning.create_default_policy(policy_name)
    stored = boto3.client("iot", region_name=REGION).get_policy(policyName=policy_name)
    return json.loads(stored["policyDocument"])


class TestDefaultPolicyRetainPublish:

    def test_publish_statement_grants_retain_publish_on_same_resources(self, generated_policy):
        # Validates: Requirements 7.1, 7.3
        publish_statements = [
            s for s in generated_policy["Statement"]
            if "iot:Publish" in _as_list(s["Action"])
        ]
        assert len(publish_statements) == 1
        statement = publish_statements[0]
        assert statement["Action"] == ["iot:Publish", "iot:RetainPublish"]
        assert statement["Resource"] == PRE_FEATURE_DOCUMENT["Statement"][1]["Resource"]
        assert statement["Effect"] == "Allow"

    def test_only_the_publish_action_list_changed(self, generated_policy):
        # Validates: Requirements 7.3 — snapshot: pre-feature document plus
        # exactly the one added action.
        assert generated_policy == _expected_document()

    def test_retain_publish_never_on_wildcard_resource(self, generated_policy):
        # Validates: Requirements 7.5
        for statement in generated_policy["Statement"]:
            if "iot:RetainPublish" not in _as_list(statement["Action"]):
                continue
            resources = _as_list(statement["Resource"])
            assert resources, statement
            for resource in resources:
                assert resource != "*", statement
                assert resource.startswith("arn:aws:iot:"), statement
                assert "${iot:Connection.Thing.ThingName}" in resource, statement

    def test_retain_publish_only_where_publish_is_granted(self, generated_policy):
        # Validates: Requirements 7.1 — the action rides with iot:Publish, it
        # is not sprinkled into Subscribe/Receive/shadow statements.
        for statement in generated_policy["Statement"]:
            actions = _as_list(statement["Action"])
            if "iot:RetainPublish" in actions:
                assert "iot:Publish" in actions, statement
            else:
                assert "iot:Publish" not in actions, statement
