"""Server-side preparation of the Greengrass token exchange role for Station
Quick Setup.

Every Greengrass core device in an account gets its AWS credentials from the
token exchange role, through the IoT role alias. When they are missing, the
Greengrass installer that setup_station.sh runs with `--provision true`
creates the role, the role alias and the role's managed access policy, and
setup_station.sh then writes four inline policies on the role. If the
station's provisioning credentials allowed those IAM writes, anyone holding a
setup token could attach any managed policy or write any inline policy on the
role, or create another `GreengrassV2TokenExchangeRole*` role with any trust
policy.

So the quick_setup Lambda performs these writes itself, through a session
that never leaves the Lambda (session_policy.build_tes_preparation_policy),
and the station's credentials keep only `iam:GetRole` and `iam:GetPolicy`
(session_policy.build_session_policy). The names and documents below are the
ones setup_station.sh and the Greengrass installer use, so a device set up
through Quick Setup gets the same role as a device installed by hand.

Keep `inline_policies` in sync with the "Updating
GreengrassV2TokenExchangeRole" section of station_install/setup_station.sh.
"""
import json
import logging

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# The names setup_station.sh passes to the Greengrass installer
# (--tes-role-name, --tes-role-alias-name). The installer names the role's
# managed access policy "<role name>Access".
TES_ROLE_NAME = "GreengrassV2TokenExchangeRole"
TES_ROLE_ALIAS_NAME = "GreengrassCoreTokenExchangeRoleAlias"
TES_ACCESS_POLICY_NAME = TES_ROLE_NAME + "Access"

# The trust policy the installer gives a role it creates: only the AWS IoT
# credentials provider can assume the role.
TES_ROLE_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "credentials.iot.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# The installer's default managed access policy for the role.
TES_ACCESS_POLICY_DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
                "s3:GetBucketLocation",
            ],
            "Resource": "*",
        }
    ],
}

_NOT_FOUND = ("NoSuchEntity",)
_ACCESS_DENIED = ("AccessDenied", "AccessDeniedException")


def tes_role_arn(account_id):
    return f"arn:aws:iam::{account_id}:role/{TES_ROLE_NAME}"


def tes_access_policy_arn(account_id):
    return f"arn:aws:iam::{account_id}:policy/{TES_ACCESS_POLICY_NAME}"


def tes_role_alias_arn(region, account_id):
    return f"arn:aws:iot:{region}:{account_id}:rolealias/{TES_ROLE_ALIAS_NAME}"


def inline_policies(account_id):
    """The inline policies setup_station.sh writes on the role, by name."""
    return {
        "GreengrassComponentS3Access": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowPortalComponentBucketAccess",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetBucketLocation"],
                    "Resource": [
                        "arn:aws:s3:::dda-component-*",
                        "arn:aws:s3:::dda-component-*/*",
                    ],
                },
                {
                    "Sid": "AllowDDABucketPatternAccess",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:GetBucketLocation",
                        "s3:HeadObject",
                    ],
                    "Resource": ["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*"],
                },
                {
                    "Sid": "AllowInferenceResultsUpload",
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:PutObjectTagging",
                        "s3:GetObject",
                        "s3:GetBucketLocation",
                    ],
                    "Resource": [
                        "arn:aws:s3:::dda-inference-results-*",
                        "arn:aws:s3:::dda-inference-results-*/*",
                    ],
                },
            ],
        },
        "CloudWatchLogsPolicy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogStreams",
                    ],
                    "Resource": "arn:aws:logs:*:*:log-group:/aws/greengrass/*",
                }
            ],
        },
        "ECRComponentAccess": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowEcrAuthToken",
                    "Effect": "Allow",
                    "Action": ["ecr:GetAuthorizationToken"],
                    "Resource": "*",
                },
                {
                    "Sid": "AllowEcrImagePull",
                    "Effect": "Allow",
                    "Action": [
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchCheckLayerAvailability",
                    ],
                    "Resource": f"arn:aws:ecr:*:{account_id}:repository/dda/*",
                },
            ],
        },
        "ShadowManagerSyncPolicy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ShadowManagerCloudSync",
                    "Effect": "Allow",
                    "Action": [
                        "iot:GetThingShadow",
                        "iot:UpdateThingShadow",
                        "iot:DeleteThingShadow",
                        "iot:ListNamedShadowsForThing",
                    ],
                    "Resource": f"arn:aws:iot:*:{account_id}:thing/*",
                }
            ],
        },
    }


def _error_code(exc):
    return exc.response.get("Error", {}).get("Code", "")


def _ensure_role(iam, account_id):
    """Return the role ARN, creating the role the way the installer does."""
    try:
        return iam.get_role(RoleName=TES_ROLE_NAME)["Role"]["Arn"]
    except ClientError as exc:
        if _error_code(exc) not in _NOT_FOUND:
            raise
    try:
        return iam.create_role(
            RoleName=TES_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TES_ROLE_TRUST_POLICY),
            Description=(
                "Role for Greengrass IoT things to interact with AWS services "
                "using token exchange service"
            ),
        )["Role"]["Arn"]
    except ClientError as exc:
        if _error_code(exc) != "EntityAlreadyExists":
            raise
    # Created concurrently by another exchange or installer run.
    return tes_role_arn(account_id)


def _ensure_role_alias(iot, role_arn):
    """Create the role alias if it is missing. An existing alias is used as it
    is, as the installer does."""
    try:
        iot.describe_role_alias(roleAlias=TES_ROLE_ALIAS_NAME)
        return
    except ClientError as exc:
        if _error_code(exc) != "ResourceNotFoundException":
            raise
    try:
        iot.create_role_alias(roleAlias=TES_ROLE_ALIAS_NAME, roleArn=role_arn)
    except ClientError as exc:
        if _error_code(exc) != "ResourceAlreadyExistsException":
            raise


def _ensure_access_policy(iam, account_id):
    """Create the managed access policy and attach it to the role if the
    policy is missing. Like the installer, an existing policy is left as it
    is, attached or not."""
    policy_arn = tes_access_policy_arn(account_id)
    try:
        iam.get_policy(PolicyArn=policy_arn)
        return
    except ClientError as exc:
        if _error_code(exc) not in _NOT_FOUND:
            raise
    try:
        iam.create_policy(
            PolicyName=TES_ACCESS_POLICY_NAME,
            PolicyDocument=json.dumps(TES_ACCESS_POLICY_DOCUMENT),
            Description=(
                "Policy for Greengrass IoT things to interact with AWS "
                "services using token exchange service"
            ),
        )
    except ClientError as exc:
        if _error_code(exc) != "EntityAlreadyExists":
            raise
    iam.attach_role_policy(RoleName=TES_ROLE_NAME, PolicyArn=policy_arn)


def _put_inline_policies(iam, account_id):
    for name, document in inline_policies(account_id).items():
        iam.put_role_policy(
            RoleName=TES_ROLE_NAME,
            PolicyName=name,
            PolicyDocument=json.dumps(document),
        )


def prepare(iam, iot, account_id, create_missing):
    """Prepare the token exchange role for one Quick Setup exchange.

    Args:
        iam: IAM client for the use case account, from the token exchange
            role preparation session.
        iot: IoT client for the use case account and region, from the same
            session.
        account_id: The use case account id.
        create_missing: True for same-account use cases, whose
            DDAStationProvisioningRole may create the role, the role alias
            and the access policy. False for cross-account use cases, whose
            DDAPortalAccessRole may only write the role's inline policies.

    Returns:
        True when the role is ready and setup_station.sh can skip its own
        role policy updates. False only when ``create_missing`` is False and
        the role is missing or the inline policies are denied. The station
        then runs its own role policy updates, which end in warnings, as
        they already did for a missing role or grant.

    Raises:
        botocore ClientError for any other failure. Every step is idempotent,
        so the station can retry the exchange.
    """
    if create_missing:
        role_arn = _ensure_role(iam, account_id)
        _ensure_role_alias(iot, role_arn)
        _ensure_access_policy(iam, account_id)
        _put_inline_policies(iam, account_id)
        return True

    try:
        _put_inline_policies(iam, account_id)
    except ClientError as exc:
        code = _error_code(exc)
        if code in _NOT_FOUND or code in _ACCESS_DENIED:
            logger.warning(
                "Token exchange role %s not prepared in account %s (%s)",
                TES_ROLE_NAME, account_id, code,
            )
            return False
        raise
    return True
