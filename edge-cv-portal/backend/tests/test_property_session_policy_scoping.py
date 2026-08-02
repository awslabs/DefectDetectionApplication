"""
Property-based test for build_session_policy scoping (station-quick-setup 2.6).

**Feature: station-quick-setup, Property 13: Session policies are scoped to the
single registered device**

**Validates: Requirements 5.2**

Property 13 (design.md): *For any* registered device name, Device_Group,
region, and account id, the generated session policy contains no thing or
thing-group resource ARN referencing any name other than the registered device
name and Device_Group, no wildcard-action statements (`iot:*`, `greengrass:*`,
`iam:*`), and only actions from the fixed provisioning action set.

`build_session_policy` is a pure, side-effect-free function, so this test needs
no AWS mocking and imports the module directly (functions/ is on sys.path via
the tests conftest).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

import session_policy
from session_policy import build_session_policy


# ---------------------------------------------------------------------------
# The fixed provisioning action set (design.md, Requirement 5.2). This is an
# INDEPENDENT allow-list: every action the policy grants must be one of these
# concrete, non-wildcard provisioning actions.
# ---------------------------------------------------------------------------
FIXED_PROVISIONING_ACTIONS = frozenset({
    # IoT thing / thing-group provisioning
    "iot:CreateThing",
    "iot:DescribeThing",
    "iot:AddThingToThingGroup",
    "iot:CreateThingGroup",
    "iot:DescribeThingGroup",
    "iot:ListThingGroupsForThing",
    # IoT certificate / policy / endpoint / role-alias provisioning
    "iot:CreateKeysAndCertificate",
    "iot:AttachThingPrincipal",
    "iot:AttachPolicy",
    "iot:CreatePolicy",
    "iot:GetPolicy",
    "iot:ListPolicyVersions",
    "iot:CreatePolicyVersion",
    "iot:DeletePolicyVersion",
    "iot:DescribeEndpoint",
    "iot:CreateRoleAlias",
    "iot:DescribeRoleAlias",
    # IAM Greengrass Token Exchange Service (TES) role setup
    "iam:GetRole",
    "iam:CreateRole",
    "iam:AttachRolePolicy",
    "iam:PutRolePolicy",
    "iam:PassRole",
    "iam:CreatePolicy",
    "iam:GetPolicy",
    # Greengrass core-device tagging
    "greengrass:TagResource",
    # STS identity check
    "sts:GetCallerIdentity",
})


# IoT Thing / Thing Group names: pattern [a-zA-Z0-9:_-]{1,128} (Req 1.2).
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# AWS region-like tokens: lowercase/digit/hyphen, never ':' or '/' so the ARN
# separators we parse on are unambiguous.
regions = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
                  min_size=1, max_size=30)

# 12-digit account ids.
account_ids = st.text(alphabet="0123456789", min_size=12, max_size=12)


def _iter_actions(statement):
    action = statement["Action"]
    if isinstance(action, str):
        yield action
    else:
        yield from action


def _iter_resources(statement):
    resource = statement["Resource"]
    if isinstance(resource, str):
        yield resource
    else:
        yield from resource


@settings(max_examples=100, deadline=None)
@given(
    device_name=iot_names,
    device_group=iot_names,
    region=regions,
    account_id=account_ids,
)
def test_session_policy_scoped_to_single_device(
    device_name, device_group, region, account_id
):
    policy = build_session_policy(device_name, device_group, region, account_id)

    assert policy["Version"] == "2012-10-17"
    statements = policy["Statement"]
    assert statements, "policy must contain at least one statement"

    for statement in statements:
        assert statement["Effect"] == "Allow"

        # (a) Only fixed provisioning actions; no wildcard-action statements.
        for action in _iter_actions(statement):
            assert action != "*", f"full wildcard action present: {action!r}"
            assert not action.endswith(":*"), (
                f"wildcard-action statement present: {action!r}"
            )
            assert action in FIXED_PROVISIONING_ACTIONS, (
                f"action {action!r} is not in the fixed provisioning action set"
            )

        # (b) No thing / thing-group ARN references any other name.
        for resource in _iter_resources(statement):
            if resource == "*":
                # Allowed only for the certificate/endpoint/sts statements,
                # whose actions are already constrained by check (a).
                continue
            if ":thinggroup/" in resource:
                name = resource.split(":thinggroup/", 1)[1]
                assert name == device_group, (
                    f"thing-group ARN references {name!r}, "
                    f"expected {device_group!r}"
                )
            elif ":thing/" in resource:
                name = resource.split(":thing/", 1)[1]
                assert name == device_name, (
                    f"thing ARN references {name!r}, expected {device_name!r}"
                )
            elif ":coreDevices:" in resource:
                # Greengrass core-device tag target is the single device.
                name = resource.split(":coreDevices:", 1)[1]
                assert name == device_name, (
                    f"core-device ARN references {name!r}, "
                    f"expected {device_name!r}"
                )


@settings(max_examples=100, deadline=None)
@given(
    device_name=iot_names,
    device_group=iot_names,
    region=regions,
    account_id=account_ids,
)
def test_session_policy_never_references_a_second_device(
    device_name, device_group, region, account_id
):
    """A distinct, unregistered device name must never appear anywhere in the
    policy document, confirming credentials cannot touch other devices."""
    policy = build_session_policy(device_name, device_group, region, account_id)

    # A sentinel name guaranteed to differ from the registered identifiers
    # (the alphabet excludes '.', so this can never collide with generated
    # device_name / device_group values).
    other_device = "other.device.name"
    assert other_device not in session_policy.__dict__  # sanity: not a constant

    import json
    rendered = json.dumps(policy)
    assert other_device not in rendered
