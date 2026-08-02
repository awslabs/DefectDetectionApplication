"""Least-privilege IAM session policy for Quick Setup provisioning credentials.

This module holds the pure, side-effect-free `build_session_policy` core used by
the token-authenticated `quick_setup` Lambda when it calls
`sts.assume_role(..., Policy=...)` to mint Provisioning_Credentials for a single
registered device (Requirement 5.2).

The effective permissions of the resulting credentials are the *intersection* of
this session policy and the assumed role's own policy, so this policy can only
ever narrow what the role already allows. It is scoped to the one registered IoT
Thing (`thing/{device_name}`) and its Device_Group (`thinggroup/{device_group}`),
and enumerates exactly the fixed set of provisioning actions performed by
`station_install/setup_station.sh` and the Greengrass provisioner. No statement
uses a wildcard action (e.g. `iot:*`).

The document is kept COMPACT (no `Sid`s, statements merged by resource) because
`sts.assume_role` enforces a 2048-character *packed* limit on the inline session
policy; verbose per-action statements overflow it for long device names.
"""

# Fixed IoT provisioning actions whose target resources cannot be known in
# advance (certificate ids are generated during provisioning, DescribeEndpoint /
# role-alias operations are account-scoped). These use `Resource: "*"` but never
# a wildcard *action*.
_IOT_CERT_POLICY_ENDPOINT_ACTIONS = [
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
]

# Greengrass Token Exchange Service (TES) role + policy setup performed by the
# provisioner (scoped to the GreengrassV2TokenExchangeRole* names below).
_IAM_TES_ACTIONS = [
    "iam:GetRole",
    "iam:CreateRole",
    "iam:AttachRolePolicy",
    "iam:PutRolePolicy",
    "iam:PassRole",
    "iam:CreatePolicy",
    "iam:GetPolicy",
]

# IoT thing + Device_Group operations, scoped to the registered device's thing
# and group ARNs. `ListThingGroupsForThing` (resource = the thing) lets the
# station's post-provisioning step verify Device_Group membership conclusively
# (Req 4.7) instead of degrading to an inconclusive warning.
_IOT_THING_GROUP_ACTIONS = [
    "iot:CreateThing",
    "iot:DescribeThing",
    "iot:CreateThingGroup",
    "iot:DescribeThingGroup",
    "iot:AddThingToThingGroup",
    "iot:ListThingGroupsForThing",
]


def build_session_policy(device_name, device_group, region, account_id):
    """Build the least-privilege IAM session policy for one device (Req 5.2).

    Args:
        device_name: The registered IoT Thing name (the only device this policy
            authorizes operations for).
        device_group: The IoT Thing Group the core device joins.
        region: The Use_Case AWS region.
        account_id: The Use_Case AWS account id.

    Returns:
        A dict IAM policy document suitable for passing to
        `sts.assume_role(Policy=json.dumps(build_session_policy(...)))`.

    The statements are merged by resource to keep the packed document under the
    STS 2048-character inline-policy limit. Merging an action onto an extra
    (still device-scoped) resource is harmless: the effective grant is the
    intersection with the assumed role, and every resource ARN here references
    only the registered device name / Device_Group.
    """
    thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{device_name}"
    thing_group_arn = f"arn:aws:iot:{region}:{account_id}:thinggroup/{device_group}"
    core_device_arn = (
        f"arn:aws:greengrass:{region}:{account_id}:coreDevices:{device_name}"
    )
    tes_role_arn = f"arn:aws:iam::{account_id}:role/GreengrassV2TokenExchangeRole*"
    tes_policy_arn = (
        f"arn:aws:iam::{account_id}:policy/GreengrassV2TokenExchangeRoleAccess*"
    )

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # IoT thing + Device_Group, scoped to this device.
                "Effect": "Allow",
                "Action": list(_IOT_THING_GROUP_ACTIONS),
                "Resource": [thing_arn, thing_group_arn],
            },
            {
                # Cert/policy/endpoint/role-alias (generated ids -> Resource
                # "*") plus the STS identity check. No wildcard *action*.
                "Effect": "Allow",
                "Action": list(_IOT_CERT_POLICY_ENDPOINT_ACTIONS)
                + ["sts:GetCallerIdentity"],
                "Resource": "*",
            },
            {
                # Greengrass TES role + policy setup.
                "Effect": "Allow",
                "Action": list(_IAM_TES_ACTIONS),
                "Resource": [tes_role_arn, tes_policy_arn],
            },
            {
                # Tag the Greengrass core device (dda-portal:managed).
                "Effect": "Allow",
                "Action": "greengrass:TagResource",
                "Resource": core_device_arn,
            },
        ],
    }
