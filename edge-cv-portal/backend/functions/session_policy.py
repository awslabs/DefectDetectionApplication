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

The station's policy carries no IAM write action and no `iam:PassRole`. The
Lambda prepares the Greengrass token exchange role, its IoT role alias and its
policies itself before it returns the station's credentials
(token_exchange_role.py), in a separate session narrowed by
`build_tes_preparation_policy`. That session's credentials never leave the
Lambda. The station keeps the read-only `iam:GetRole` and `iam:GetPolicy` that
the Greengrass installer calls before it would create those resources, and
`iot:DescribeRoleAlias`, which tells the installer that the alias exists.
"""

from token_exchange_role import (
    tes_access_policy_arn,
    tes_role_alias_arn,
    tes_role_arn,
)

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
    "iot:DescribeRoleAlias",
]

# Read-only checks the Greengrass installer makes on the token exchange role
# and its access policy before it would create them.
_IAM_TES_READ_ACTIONS = [
    "iam:GetRole",
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
                # Installer checks on the token exchange role and its access
                # policy. Read-only.
                "Effect": "Allow",
                "Action": list(_IAM_TES_READ_ACTIONS),
                "Resource": [
                    tes_role_arn(account_id),
                    tes_access_policy_arn(account_id),
                ],
            },
            {
                # Tag the Greengrass core device (dda-portal:managed).
                "Effect": "Allow",
                "Action": "greengrass:TagResource",
                "Resource": core_device_arn,
            },
        ],
    }


def build_tes_preparation_policy(region, account_id):
    """Session policy for the Lambda's own token exchange role preparation.

    Used only server-side (token_exchange_role.prepare); these credentials are
    never returned to the station. Every resource is one fixed name:
    the role, its access policy and its IoT role alias. `iam:AttachRolePolicy`
    may attach only that access policy. `iam:PassRole` is what
    `iot:CreateRoleAlias` requires on the role it points at.

    The assumed role's own policy still applies: DDAStationProvisioningRole
    allows all of these, and a cross-account DDAPortalAccessRole allows only
    `iam:PutRolePolicy` (and `iam:GetRolePolicy`) on the role.
    """
    role_arn = tes_role_arn(account_id)
    access_policy_arn = tes_access_policy_arn(account_id)

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "iam:GetRole",
                    "iam:CreateRole",
                    "iam:PutRolePolicy",
                    "iam:PassRole",
                ],
                "Resource": role_arn,
            },
            {
                "Effect": "Allow",
                "Action": "iam:AttachRolePolicy",
                "Resource": role_arn,
                "Condition": {"ArnEquals": {"iam:PolicyARN": access_policy_arn}},
            },
            {
                "Effect": "Allow",
                "Action": ["iam:GetPolicy", "iam:CreatePolicy"],
                "Resource": access_policy_arn,
            },
            {
                "Effect": "Allow",
                "Action": ["iot:DescribeRoleAlias", "iot:CreateRoleAlias"],
                "Resource": tes_role_alias_arn(region, account_id),
            },
        ],
    }
