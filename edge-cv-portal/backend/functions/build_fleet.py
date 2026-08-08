"""
Build Fleet API Lambda function (Fleet_Manager, portal build fleet)

Fleet management for Dedicated_Build_Servers: list, launch, start, stop,
and terminate, following the portal handler conventions (error envelope
{error: {code, message, details}}, get_user_from_event, log_audit_event,
RBAC via rbac_middleware).

Every lifecycle decision is delegated to the pure module build_domain.py
(validate_fleet_action); this handler does I/O and wiring only.

Routes (API Gateway REST):
    GET    /build-servers             Fleet list with live EC2 state
                                      reconciliation (DescribeInstances)
                                      (Req 6.1)
    POST   /build-servers             Launch: RunInstances with the
                                      arch-selected Ubuntu 22.04 AMI, the
                                      configured instance type/volume,
                                      hardened profile (extended
                                      dda-build-role, no key pair, no
                                      inbound rules, IMDSv2), user-data
                                      bootstrap (setup-build-server.sh
                                      equivalent + repo clone), register
                                      in BuildServers (Req 6.5)
    POST   /build-servers/{id}/start  StartInstances when stopped
                                      (Req 6.2, 6.10)
    POST   /build-servers/{id}/stop   StopInstances when running and no
                                      running Build_Job (Req 6.3, 6.4,
                                      6.10)
    DELETE /build-servers/{id}        Terminate with an explicit
                                      confirm: "<server name>" body echo
                                      (Req 6.6, 6.12); no running
                                      Build_Job (Req 6.4)

Accepted start/stop/terminate/launch actions record a pending_action
marker with a 10-minute deadline; the dispatcher tick reports the action
failed when the server has not reached the expected lifecycle state by
the deadline (Req 6.11, build_planner.decide_pending_action). Every
fleet action outcome (success or failure) is recorded in the Audit_Log
(Req 6.8); non-PortalAdmin requests are rejected with an authorization
error and a denied-access Audit_Log entry by the RBAC decorators
(Req 6.7).

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.10, 6.12
"""
import json
import logging
import os
import shlex
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event
)
from rbac_middleware import require_builds_read, super_user_only

# Pure decision modules (no AWS clients): fleet lifecycle decisions come
# from build_domain.validate_fleet_action; the pending-action deadline
# arithmetic is shared with the dispatcher via build_planner.
import build_domain
import build_planner
# The one authoritative on-server repository directory: the directory this
# handler's bootstrap clones into is the same value the dispatcher invokes
# the build agent from, so the two cannot drift apart (Req 5.1, 5.2).
import build_source

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
ec2 = boto3.client('ec2')
ssm = boto3.client('ssm')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
BUILD_SERVERS_TABLE = os.environ.get('BUILD_SERVERS_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')
#: Instance profile attached to launched servers: the CDK-created
#: extension of dda-build-role (SSM core + build/publish permissions +
#: events:PutEvents; design §2/§10).
BUILD_INSTANCE_PROFILE_NAME = os.environ.get(
    'BUILD_INSTANCE_PROFILE_NAME', 'dda-build-role')
#: Security group with NO inbound rules (all access is SSM; design §2).
BUILD_SECURITY_GROUP_ID = os.environ.get('BUILD_SECURITY_GROUP_ID')
#: Optional subnet pin; the account default VPC/subnet is used when unset.
BUILD_SUBNET_ID = os.environ.get('BUILD_SUBNET_ID')
#: Source repository cloned by the user-data bootstrap.
BUILD_REPO_URL = os.environ.get(
    'BUILD_REPO_URL',
    'https://github.com/awslabs/DefectDetectionApplication')
#: On-server clone location used by the user-data bootstrap and recorded on
#: every launched Build_Server, resolved through the shared resolver: the
#: operator override when one is configured, else
#: build_source.DEFAULT_REPO_DIR — the directory every server bootstrapped
#: before this change already uses, so none of them needs re-bootstrapping
#: (Req 5.2, 5.3). No directory literal lives in this module.
BUILD_REPO_DIR = build_source.resolve_repo_dir(
    None, env_default=os.environ.get('BUILD_REPO_DIR'))

# ---------------------------------------------------------------- constants

#: PortalSettings item key holding the build infrastructure configuration
#: (design §7; build_config.py owns the full config API).
BUILD_CONFIG_SETTING_KEY = 'build_infrastructure_config'

#: Documented defaults applied on read for absent values (Req 9.2).
#: build_domain.DEFAULT_BUILD_CONFIG is the one authoritative parameter
#: table (build-source-selection design B1 collapsed the former duplicate
#: literal); readers copy it (dict(...)) before merging stored values.
DEFAULT_BUILD_CONFIG: Dict[str, Any] = build_domain.DEFAULT_BUILD_CONFIG

#: Configured instance type key per CPU architecture (Req 6.5, 9.1).
INSTANCE_TYPE_CONFIG_KEY = {
    build_domain.ARCH_ARM64: 'arm64_instance_type',
    build_domain.ARCH_X86_64: 'x86_64_instance_type',
}

#: SSM public parameter paths for the latest Ubuntu 22.04 AMI per
#: architecture (Canonical-maintained; design §2 replaces the manual
#: script's 18.04 AMI with 22.04).
UBUNTU_2204_SSM_PARAMETER = {
    build_domain.ARCH_ARM64:
        '/aws/service/canonical/ubuntu/server/22.04/stable/current/'
        'arm64/hvm/ebs-gp2/ami-id',
    build_domain.ARCH_X86_64:
        '/aws/service/canonical/ubuntu/server/22.04/stable/current/'
        'amd64/hvm/ebs-gp2/ami-id',
}

#: DescribeImages fallback (Canonical owner id, jammy server images).
CANONICAL_OWNER_ID = '099720109477'
UBUNTU_2204_NAME_FILTER = {
    build_domain.ARCH_ARM64:
        'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*',
    build_domain.ARCH_X86_64:
        'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*',
}
EC2_ARCHITECTURE = {
    build_domain.ARCH_ARM64: 'arm64',
    build_domain.ARCH_X86_64: 'x86_64',
}

#: Lifecycle state a server is expected to reach after each accepted
#: action; reaching it clears the pending_action marker (Req 6.11).
EXPECTED_STATE_AFTER_ACTION = {
    'launch': build_domain.SERVER_STATE_RUNNING,
    build_domain.FLEET_ACTION_START: build_domain.SERVER_STATE_RUNNING,
    build_domain.FLEET_ACTION_STOP: build_domain.SERVER_STATE_STOPPED,
    build_domain.FLEET_ACTION_TERMINATE: build_domain.SERVER_STATE_TERMINATED,
}

#: Optimistic lifecycle state recorded immediately after the EC2 call is
#: accepted (the authoritative state arrives via EC2 state-change events
#: and the DescribeInstances reconciliation on read).
INITIATED_STATE_AFTER_ACTION = {
    build_domain.FLEET_ACTION_START: build_domain.SERVER_STATE_PENDING,
    build_domain.FLEET_ACTION_STOP: build_domain.SERVER_STATE_STOPPING,
    build_domain.FLEET_ACTION_TERMINATE:
        build_domain.SERVER_STATE_SHUTTING_DOWN,
}

#: User-data bootstrap: the setup-build-server.sh equivalent plus the
#: repository clone and the Source_Sync onto the selected ref (design
#: §2/A3). The repo's own setup-build-server.sh is executed afterwards so
#: the build environment (snap docker, docker-compose, Python 3.11, AWS
#: CLI, botocore[crt], GDK) exactly matches the manual process. SSM agent
#: is preinstalled on Ubuntu 22.04.
#:
#: Placeholders, all bound by `_user_data_body()`:
#:   `{repo_dir}`     the clone location, fed from the shared resolver
#:                    (Req 5.2) — never a literal;
#:   `{source_sync}`  the Source_Sync block generated by
#:                    build_source.source_sync_commands, the single origin
#:                    of all sync command text (Req 4.3), run as the build
#:                    user so the tree keeps its ownership;
#:   `{bootstrap_log}` / `{bootstrap_marker}` read from build_planner,
#:                    their one definition;
#:   `{repo_url}`     left unbound, so the rendered template stays
#:                    `.format(repo_url=...)`-able.
#:
#: The log redirect and the marker write are deliberately NON-FATAL: a
#: failed `exec >` aborts a non-interactive bash outright, so an unwritable
#: log path would kill a bootstrap before the repository is even cloned.
#: An unwritable marker simply leaves the readiness gate shut, which the
#: bootstrap budget resolves (Req 6.3) — the fail-safe direction.
USER_DATA_BODY = """#!/bin/bash
set -x
BOOTSTRAP_LOG={bootstrap_log}
if : > "$BOOTSTRAP_LOG" 2>/dev/null; then
  exec >> "$BOOTSTRAP_LOG" 2>&1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git

# Clone the source repository for the build agent (design §2/§5).
sudo -u ubuntu -H git clone {repo_url} {repo_dir}

# Put that tree on the selected (repository, ref) through the shared
# Sync_Generator (Req 4.1, 4.3): clone-if-absent, fetch, checkout. Run as
# the build user, both because the clone above is owned by it and because
# the build itself runs as that user. A sync failure echoes
# PORTAL_SOURCE_SYNC_FAILED and exits 65/66 inside this block; the
# bootstrap continues so the marker below is still written and the
# dispatcher's readiness gate is not left hanging.
sudo -u ubuntu -H bash -s <<'DDA_SOURCE_SYNC'
{source_sync}
DDA_SOURCE_SYNC

# Run the repository's build-environment bootstrap as the build user
# (setup-build-server.sh equivalent: docker via snap, docker-compose,
# Python 3.11, AWS CLI, botocore[crt], GDK CLI).
cd {repo_dir}
sudo -u ubuntu -H bash -c 'cd {repo_dir} && ./setup-build-server.sh' || true

touch {bootstrap_marker} || true
"""


def _escape_braces(text: str) -> str:
    """Braces doubled so `text` survives one `str.format` pass verbatim.

    The generated Source_Sync block contains `{ ...; }` failure guards, and
    a repository/ref value may contain a brace of its own; without this the
    `.format(repo_url=...)` render would misread them as fields.
    """
    return text.replace('{', '{{').replace('}', '}}')


#: Stand-in for an unbound repository URL while the Source_Sync block is
#: generated: `shlex.quote` leaves it untouched (it is made of safe
#: characters), so it can be swapped back to the `{repo_url}` format field
#: after brace escaping. This keeps ONE `{repo_url}` placeholder covering
#: both the clone line and the sync block, so a template rendered with a
#: different repository can never end up half-bound.
_REPO_URL_SENTINEL = '@@DDA_REPO_URL@@'


def _user_data_body(repo_dir: Optional[str] = None,
                    source_ref: Optional[str] = None,
                    repo_url: Optional[str] = None) -> str:
    """USER_DATA_BODY with everything except `{repo_url}` bound.

    Both the module-level USER_DATA_TEMPLATE and render_user_data() go
    through here, so the constant callers read is exactly the text a launch
    renders — there is no second copy of the bootstrap to drift.
    """
    resolved_dir = build_source.resolve_repo_dir(
        None, env_default=repo_dir or BUILD_REPO_DIR)
    sync = '\n'.join(build_source.source_sync_commands(
        repo_url if repo_url is not None else _REPO_URL_SENTINEL,
        resolved_dir, source_ref))
    return (USER_DATA_BODY
            .replace('{repo_dir}', _escape_braces(resolved_dir))
            .replace('{source_sync}', _escape_braces(sync))
            .replace('{bootstrap_log}',
                     _escape_braces(shlex.quote(
                         build_planner.BOOTSTRAP_LOG_PATH)))
            .replace('{bootstrap_marker}',
                     _escape_braces(shlex.quote(
                         build_planner.BOOTSTRAP_MARKER_PATH)))
            .replace(_REPO_URL_SENTINEL, '{repo_url}'))


#: The bootstrap template with the resolved repository directory and the
#: no-ref Source_Sync already bound, leaving `{repo_url}` as its only
#: placeholder: rendering it with `.format(repo_url=...)` alone stays valid
#: for every existing caller. Use render_user_data() to bootstrap into a
#: different directory or onto a selected ref.
USER_DATA_TEMPLATE = _user_data_body()


def render_user_data(repo_url: str,
                     repo_dir: Optional[str] = None,
                     source_ref: Optional[str] = None) -> str:
    """User-data bootstrap text for a launched Dedicated_Build_Server.

    ``repo_dir`` defaults, through the shared resolver, to the module's
    resolved directory (the operator override when configured, else
    ``build_source.DEFAULT_REPO_DIR``), so the directory the bootstrap
    clones into is exactly the directory recorded on the Build_Server
    record and later read back by the dispatcher (Req 5.1, 5.2).

    ``source_ref`` is the configured selection: the server is bootstrapped
    directly onto that ref through the shared Sync_Generator (Req 4.1,
    4.3), so its tree can carry `scripts/portal-build-agent.sh` even when
    the repository default branch does not. Omitted or empty, the bootstrap
    is the clone-only sequence, i.e. today's behavior.
    """
    return _user_data_body(repo_dir, source_ref, repo_url).format(
        repo_url=repo_url)


# ------------------------------------------------------------ pure helpers

def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """Build the portal error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {},
        }
    })


def now_ms() -> int:
    """Current epoch milliseconds (BuildServers timestamps are ms epoch)"""
    return int(time.time() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be a JSON object')
    return body, None


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals to native ints/floats (deep)"""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


# ------------------------------------------------------------- persistence

def servers_table():
    """BuildServers DynamoDB table accessor"""
    return dynamodb.Table(BUILD_SERVERS_TABLE)


def scan_all(table) -> List[Dict]:
    """Full paginated scan of a table"""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def get_server(server_id: str) -> Optional[Dict]:
    """Fetch one Dedicated_Build_Server record (native types) or None"""
    response = servers_table().get_item(Key={'server_id': server_id})
    item = response.get('Item')
    return to_native(item) if item else None


def effective_build_config() -> Dict[str, Any]:
    """Effective build infrastructure configuration: stored PortalSettings
    values merged over the documented defaults (Req 9.2). Read failures
    fall back to defaults so a fleet action never fails on a missing
    settings item."""
    config = dict(DEFAULT_BUILD_CONFIG)
    if not SETTINGS_TABLE:
        return config
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': BUILD_CONFIG_SETTING_KEY})
        item = response.get('Item')
        if item:
            stored = item.get('value') if isinstance(item.get('value'), dict) \
                else item
            stored = to_native(stored)
            for key in DEFAULT_BUILD_CONFIG:
                if stored.get(key) is not None:
                    config[key] = stored[key]
    except ClientError as e:
        logger.warning(
            f"Could not read build configuration, using defaults: {e}")
    return config


# --------------------------------------------------- EC2 state reconciliation

def describe_instance_states(instance_ids: List[str]) -> Dict[str, str]:
    """Live EC2 lifecycle state per instance id via DescribeInstances.
    Instances EC2 no longer knows (past the post-termination retention
    window) are reported terminated."""
    states: Dict[str, str] = {}
    if not instance_ids:
        return states
    remaining = list(instance_ids)
    try:
        paginator = ec2.get_paginator('describe_instances')
        for page in paginator.paginate(InstanceIds=remaining):
            for reservation in page.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    states[instance['InstanceId']] = \
                        instance.get('State', {}).get('Name')
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'InvalidInstanceID.NotFound':
            # Batch call rejected: fall back to per-instance lookups so
            # one forgotten instance cannot hide the others' states.
            for instance_id in remaining:
                try:
                    response = ec2.describe_instances(
                        InstanceIds=[instance_id])
                    for reservation in response.get('Reservations', []):
                        for instance in reservation.get('Instances', []):
                            states[instance['InstanceId']] = \
                                instance.get('State', {}).get('Name')
                except ClientError as inner:
                    if inner.response.get('Error', {}).get('Code') == \
                            'InvalidInstanceID.NotFound':
                        states[instance_id] = \
                            build_domain.SERVER_STATE_TERMINATED
                    else:
                        raise
        else:
            raise
    return states


def apply_observed_state(server: Dict, observed_state: str) -> Dict:
    """Persist an observed EC2 lifecycle state onto a BuildServers record
    when it differs from the stored state: lifecycle_state,
    last_state_change_at, terminated_at on termination, and clearing of
    the pending_action marker when its expected state is reached
    (Req 6.1, 6.9, 6.11). Returns the updated record."""
    if not observed_state or observed_state == server.get('lifecycle_state'):
        return server

    updated = dict(server)
    updated['lifecycle_state'] = observed_state
    updated['last_state_change_at'] = now_ms()

    update_expression = ('SET lifecycle_state = :state, '
                         'last_state_change_at = :changed')
    expression_values: Dict[str, Any] = {
        ':state': observed_state,
        ':changed': updated['last_state_change_at'],
    }

    if observed_state == build_domain.SERVER_STATE_TERMINATED and \
            not server.get('terminated_at'):
        updated['terminated_at'] = updated['last_state_change_at']
        update_expression += ', terminated_at = :terminated'
        expression_values[':terminated'] = updated['terminated_at']

    remove_clauses = []
    pending = server.get('pending_action') or {}
    if pending and pending.get('expected_state') == observed_state:
        # The accepted fleet action reached its expected state within its
        # deadline window: the marker is cleared (Req 6.11).
        remove_clauses.append('pending_action')
        updated.pop('pending_action', None)

    if remove_clauses:
        update_expression += ' REMOVE ' + ', '.join(remove_clauses)

    try:
        servers_table().update_item(
            Key={'server_id': server['server_id']},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as e:
        logger.warning(f"State reconciliation write for server "
                       f"{server.get('server_id')} failed: {e}")
    return updated


def reconcile_servers(servers: List[Dict]) -> List[Dict]:
    """Reconcile stored BuildServers records with the live EC2 state
    (DescribeInstances) on read (design §2, Req 6.1). Servers already
    terminated in the record are not re-queried."""
    lookup = [s for s in servers
              if s.get('instance_id')
              and s.get('lifecycle_state') !=
              build_domain.SERVER_STATE_TERMINATED]
    try:
        states = describe_instance_states(
            [s['instance_id'] for s in lookup])
    except ClientError as e:
        logger.warning(f"DescribeInstances reconciliation failed; "
                       f"returning stored states: {e}")
        return servers

    reconciled = []
    for server in servers:
        observed = states.get(server.get('instance_id'))
        reconciled.append(apply_observed_state(server, observed)
                          if observed else server)
    return reconciled


# --------------------------------------------------------------- audit

def audit_fleet_action(user_id: str, action: str, server_id: str,
                       outcome: str, details: Dict) -> None:
    """Record a fleet management action outcome in the Audit_Log: the
    action, the acting user, the target server, and the outcome
    (Req 6.8)."""
    log_audit_event(
        user_id=user_id,
        action=f'fleet_server_{action}',
        resource_type='build_server',
        resource_id=server_id,
        result=outcome,
        details=details,
    )


# ------------------------------------------------------- GET /build-servers

@require_builds_read()
def list_build_servers(event: Dict, context: Any) -> Dict:
    """GET /build-servers — the fleet list with live DescribeInstances
    state reconciliation: name, instance identifier, instance type, CPU
    architecture, lifecycle state, the running Build_Job when one exists,
    and the time of the last state change (Req 6.1)."""
    servers = [to_native(item) for item in scan_all(servers_table())]
    servers = reconcile_servers(servers)
    servers.sort(key=lambda s: (s.get('created_at') or 0,
                                str(s.get('server_id'))), reverse=True)
    return create_response(200, {'servers': servers})


# ------------------------------------------------------ POST /build-servers

def resolve_ubuntu_2204_ami(arch: str) -> str:
    """Latest Ubuntu 22.04 AMI id for the requested CPU architecture:
    the Canonical-maintained SSM public parameter, with a DescribeImages
    fallback (design §2)."""
    try:
        response = ssm.get_parameter(Name=UBUNTU_2204_SSM_PARAMETER[arch])
        ami_id = response.get('Parameter', {}).get('Value')
        if ami_id:
            return ami_id
    except ClientError as e:
        logger.warning(f"Ubuntu 22.04 SSM parameter lookup failed for "
                       f"{arch}; falling back to DescribeImages: {e}")

    response = ec2.describe_images(
        Owners=[CANONICAL_OWNER_ID],
        Filters=[
            {'Name': 'name', 'Values': [UBUNTU_2204_NAME_FILTER[arch]]},
            {'Name': 'architecture', 'Values': [EC2_ARCHITECTURE[arch]]},
            {'Name': 'state', 'Values': ['available']},
        ],
    )
    images = sorted(response.get('Images', []),
                    key=lambda i: i.get('CreationDate', ''))
    if not images:
        raise RuntimeError(
            f'No Ubuntu 22.04 AMI found for architecture {arch}')
    return images[-1]['ImageId']


def run_fleet_instance(server_id: str, name: str, arch: str,
                       instance_type: str, volume_size_gb: int,
                       ami_id: str, repo_dir: Optional[str] = None,
                       source_ref: Optional[str] = None) -> str:
    """RunInstances for a Dedicated_Build_Server with the hardened
    profile: extended dda-build-role instance profile, NO key pair, the
    no-inbound-rules security group, IMDSv2 required (design §2).

    The bootstrap clones into ``repo_dir`` (defaulting to the module's
    resolved directory), which the caller records on the Build_Server
    record so the dispatcher reads back the exact directory this
    server's bootstrap used (Req 5.2), and syncs that tree to
    ``source_ref`` — the configured selection — through the shared
    Sync_Generator (Req 4.1). Returns the instance id."""
    kwargs: Dict[str, Any] = {
        'ImageId': ami_id,
        'InstanceType': instance_type,
        'MinCount': 1,
        'MaxCount': 1,
        # No KeyName: all access is IAM-audited SSM (design §2).
        'IamInstanceProfile': {'Name': BUILD_INSTANCE_PROFILE_NAME},
        'MetadataOptions': {
            'HttpTokens': 'required',       # IMDSv2
            'HttpPutResponseHopLimit': 2,
            'HttpEndpoint': 'enabled',
        },
        'BlockDeviceMappings': [{
            'DeviceName': '/dev/sda1',
            'Ebs': {
                'VolumeSize': int(volume_size_gb),
                'VolumeType': 'gp3',
                'DeleteOnTermination': True,
            },
        }],
        'EbsOptimized': True,
        'UserData': render_user_data(BUILD_REPO_URL, repo_dir, source_ref),
        'TagSpecifications': [{
            'ResourceType': 'instance',
            'Tags': [
                {'Key': 'Name', 'Value': name},
                # dda-build:* tags scope the fleet Lambdas' EC2 IAM
                # permissions (design §10).
                {'Key': 'dda-build:fleet', 'Value': 'true'},
                {'Key': 'dda-build:server-id', 'Value': server_id},
            ],
        }],
    }
    if BUILD_SECURITY_GROUP_ID:
        kwargs['SecurityGroupIds'] = [BUILD_SECURITY_GROUP_ID]
    if BUILD_SUBNET_ID:
        kwargs['SubnetId'] = BUILD_SUBNET_ID

    response = ec2.run_instances(**kwargs)
    return response['Instances'][0]['InstanceId']


@super_user_only
def launch_build_server(event: Dict, context: Any) -> Dict:
    """POST /build-servers — launch a new Dedicated_Build_Server: an EC2
    instance of the selected CPU architecture with the configured
    instance type and volume size, the build environment installed via
    the user-data bootstrap, registered in the fleet list under the
    provided name with its CPU architecture (Req 6.5). PortalAdmin only
    (Req 6.7, enforced by the decorator)."""
    body, err = parse_body(event)
    if err:
        return err
    user = get_user_from_event(event)

    name = body.get('name')
    arch = body.get('architecture')
    validation_errors = []
    if not isinstance(name, str) or not name.strip():
        validation_errors.append({
            'rule': 'name_missing',
            'message': 'A server name must be provided.',
        })
    if arch not in INSTANCE_TYPE_CONFIG_KEY:
        validation_errors.append({
            'rule': 'architecture_invalid',
            'message': (
                f"The CPU architecture must be one of: "
                f"{', '.join(sorted(INSTANCE_TYPE_CONFIG_KEY))}."
            ),
        })
    if validation_errors:
        return error_response(
            400, 'LAUNCH_REQUEST_INVALID',
            'The launch request is invalid: '
            + ' '.join(e['message'] for e in validation_errors),
            {'errors': validation_errors})

    name = name.strip()
    config = effective_build_config()
    instance_type = config[INSTANCE_TYPE_CONFIG_KEY[arch]]
    volume_size_gb = config['volume_size_gb']
    server_id = f'srv-{uuid.uuid4()}'
    requested_at = now_ms()
    # The directory this server's bootstrap will clone into; recorded on
    # the Build_Server record below (Req 5.2).
    repo_dir = BUILD_REPO_DIR
    # The configured ref this server is bootstrapped onto (Req 4.1); None
    # means the repository default branch, i.e. today's behavior.
    source_ref = config.get('source_ref')

    try:
        ami_id = resolve_ubuntu_2204_ami(arch)
        instance_id = run_fleet_instance(
            server_id=server_id,
            name=name,
            arch=arch,
            instance_type=instance_type,
            volume_size_gb=volume_size_gb,
            ami_id=ami_id,
            repo_dir=repo_dir,
            source_ref=source_ref,
        )
    except (ClientError, RuntimeError) as e:
        logger.error(f"Fleet launch failed: {e}", exc_info=True)
        audit_fleet_action(
            user['user_id'], 'launch', server_id, 'failure',
            {'name': name, 'architecture': arch,
             'instance_type': instance_type, 'error': str(e)})
        return error_response(
            502, 'LAUNCH_FAILED',
            f"Launching the Dedicated_Build_Server failed: {e}",
            {'name': name, 'architecture': arch})

    server = {
        'server_id': server_id,
        'name': name,
        'instance_id': instance_id,
        'instance_type': instance_type,
        'cpu_architecture': arch,
        # The exact directory this server's bootstrap cloned into, so the
        # dispatcher invokes the build agent from that tree instead of
        # assuming a directory of its own (Req 5.1, 5.2). Servers
        # bootstrapped before this change carry no such field and resolve
        # to build_source.DEFAULT_REPO_DIR (Req 5.3).
        'repo_dir': repo_dir,
        'lifecycle_state': build_domain.SERVER_STATE_PENDING,
        'last_state_change_at': requested_at,
        'pending_action': {
            'action': 'launch',
            'requested_by': user['user_id'],
            'requested_at': requested_at,
            'initiated_at': requested_at,
            'deadline': build_planner.fleet_action_deadline(requested_at),
            'expected_state': EXPECTED_STATE_AFTER_ACTION['launch'],
        },
        'created_by': user['user_id'],
        'created_at': requested_at,
        'terminated_at': None,
    }
    servers_table().put_item(Item=server)

    audit_fleet_action(
        user['user_id'], 'launch', server_id, 'success',
        {'name': name, 'architecture': arch,
         'instance_id': instance_id, 'instance_type': instance_type,
         'volume_size_gb': volume_size_gb, 'ami_id': ami_id})

    return create_response(201, {'server': server})


# ------------------------------- POST /build-servers/{id}/start | /stop
# ------------------------------- DELETE /build-servers/{id}

def record_pending_action(server_id: str, action: str, user_id: str,
                          requested_at: int,
                          initiated_state: str) -> Dict[str, Any]:
    """Persist the accepted action's pending_action marker (10-minute
    deadline, Req 6.11) and the optimistic lifecycle state; returns the
    marker."""
    pending_action = {
        'action': action,
        'requested_by': user_id,
        'requested_at': requested_at,
        'initiated_at': requested_at,
        'deadline': build_planner.fleet_action_deadline(requested_at),
        'expected_state': EXPECTED_STATE_AFTER_ACTION[action],
    }
    servers_table().update_item(
        Key={'server_id': server_id},
        UpdateExpression=('SET pending_action = :pending, '
                          'lifecycle_state = :state, '
                          'last_state_change_at = :changed'),
        ExpressionAttributeValues={
            ':pending': pending_action,
            ':state': initiated_state,
            ':changed': requested_at,
        },
    )
    return pending_action


def execute_fleet_action(event: Dict, context: Any, action: str) -> Dict:
    """Shared start/stop/terminate execution: reconcile the server's live
    EC2 state, validate the action via build_domain.validate_fleet_action
    (Req 6.2, 6.3, 6.4, 6.10), call EC2, record the pending_action marker
    with its 10-minute deadline (Req 6.11), and audit the outcome
    (Req 6.8). Terminate additionally requires the confirm body echo
    (handled by the caller, Req 6.6, 6.12)."""
    server_id = (event.get('pathParameters') or {}).get('id')
    server = get_server(server_id) if server_id else None
    if not server:
        return error_response(404, 'BUILD_SERVER_NOT_FOUND',
                              'Build server not found')

    user = get_user_from_event(event)

    # Validate against the live lifecycle state, not a stale record.
    server = reconcile_servers([server])[0]
    running_job = server.get('running_build_job_id')

    result = build_domain.validate_fleet_action(action, server, running_job)
    if not result.valid:
        # Rejected without changing the server; the error identifies the
        # current lifecycle state and any running Build_Job (Req 6.4,
        # 6.10) and the failed action is audited (Req 6.8).
        audit_fleet_action(
            user['user_id'], action, server_id, 'failure',
            {'name': server.get('name'),
             'lifecycle_state': server.get('lifecycle_state'),
             'running_build_job_id': running_job,
             'errors': [dict(e) for e in result.errors]})
        return error_response(
            409, 'FLEET_ACTION_REJECTED',
            result.errors[0]['message'],
            {'errors': [dict(e) for e in result.errors],
             'lifecycle_state': server.get('lifecycle_state')})

    instance_id = server.get('instance_id')
    try:
        if action == build_domain.FLEET_ACTION_START:
            ec2.start_instances(InstanceIds=[instance_id])
        elif action == build_domain.FLEET_ACTION_STOP:
            ec2.stop_instances(InstanceIds=[instance_id])
        else:
            ec2.terminate_instances(InstanceIds=[instance_id])
    except ClientError as e:
        logger.error(f"Fleet {action} of {server_id} ({instance_id}) "
                     f"failed: {e}")
        audit_fleet_action(
            user['user_id'], action, server_id, 'failure',
            {'name': server.get('name'), 'instance_id': instance_id,
             'lifecycle_state': server.get('lifecycle_state'),
             'error': str(e)})
        return error_response(
            502, 'FLEET_ACTION_FAILED',
            f"The {action} action failed on the Dedicated_Build_Server: {e}",
            {'lifecycle_state': server.get('lifecycle_state')})

    requested_at = now_ms()
    pending_action = record_pending_action(
        server_id, action, user['user_id'], requested_at,
        INITIATED_STATE_AFTER_ACTION[action])

    audit_fleet_action(
        user['user_id'], action, server_id, 'success',
        {'name': server.get('name'), 'instance_id': instance_id,
         'previous_state': server.get('lifecycle_state'),
         'deadline': pending_action['deadline']})

    server = get_server(server_id) or server
    return create_response(200, {'server': server})


@super_user_only
def start_build_server(event: Dict, context: Any) -> Dict:
    """POST /build-servers/{id}/start — StartInstances when the server is
    stopped (Req 6.2, 6.10). PortalAdmin only (Req 6.7)."""
    return execute_fleet_action(event, context,
                                build_domain.FLEET_ACTION_START)


@super_user_only
def stop_build_server(event: Dict, context: Any) -> Dict:
    """POST /build-servers/{id}/stop — StopInstances when the server is
    running and no Build_Job is running on it (Req 6.3, 6.4, 6.10).
    PortalAdmin only (Req 6.7)."""
    return execute_fleet_action(event, context,
                                build_domain.FLEET_ACTION_STOP)


@super_user_only
def terminate_build_server(event: Dict, context: Any) -> Dict:
    """DELETE /build-servers/{id} — terminate with an explicit
    confirmation: the request body must echo confirm: "<server name>"
    exactly; anything else performs no termination and leaves the server
    unchanged (Req 6.6, 6.12). No running Build_Job (Req 6.4).
    PortalAdmin only (Req 6.7)."""
    server_id = (event.get('pathParameters') or {}).get('id')
    server = get_server(server_id) if server_id else None
    if not server:
        return error_response(404, 'BUILD_SERVER_NOT_FOUND',
                              'Build server not found')

    body, err = parse_body(event)
    if err:
        return err

    if body.get('confirm') != server.get('name'):
        # Incomplete/cancelled confirmation: no termination is performed
        # and the server is left unchanged (Req 6.6, 6.12).
        return error_response(
            400, 'CONFIRMATION_REQUIRED',
            "Termination requires the request body to echo the server "
            "name: confirm: \"" + str(server.get('name')) + "\".",
            {'expected_confirmation': server.get('name')})

    return execute_fleet_action(event, context,
                                build_domain.FLEET_ACTION_TERMINATE)


# ------------------------------------------------------------------ routing

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        resource = event.get('resource', '')

        if resource == '/build-servers':
            if http_method == 'GET':
                return list_build_servers(event, context)
            if http_method == 'POST':
                return launch_build_server(event, context)
        elif resource == '/build-servers/{id}':
            if http_method == 'DELETE':
                return terminate_build_server(event, context)
        elif resource == '/build-servers/{id}/start':
            if http_method == 'POST':
                return start_build_server(event, context)
        elif resource == '/build-servers/{id}/stop':
            if http_method == 'POST':
                return stop_build_server(event, context)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
