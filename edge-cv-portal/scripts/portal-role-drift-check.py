#!/usr/bin/env python3
"""Read-only drift check of one IAM role's policies against the repository.

Given a role name, lists its inline and attached policies and classifies each:

* ``in-template`` - a policy the repository produces: for the device
  token-exchange role, a Device_Role_Template policy (setup_station.sh,
  create-edge-device-iam-role.sh, edge-device-iam-policy.json,
  usecase-account-stack.ts DDAPortalComponentAccessPolicy, and the managed
  ``<role name>Access`` policy the Greengrass provisioner creates) whose S3
  statements stay within the template statement set (non-S3 statements are
  not compared); for a portal Lambda role, a policy the CDK stacks create;
* ``portal-written`` - DDAWorkflowTuningSampleAccess and
  DDAStreamCameraCredentialRead in the shapes their writers produce, and the
  Device_Access_Document DDAUseCaseDeviceAccess when every statement has the
  ``UseCase<hex>[Results]`` Sid shape and only the permitted actions;
* ``hand-attached`` - everything else.

Prints the S3 statements of every hand-attached policy, the aggregate size of
the role's inline policies against IAM's 10,240-character limit, and, for a
portal Lambda role, every S3 object statement (and every bucket-level
ListBucket / GetBucketLocation statement) with its Condition or UNCONDITIONED.
With --usecases-table and --deployments-table it also lists the use cases of
the role's account that have a deployment and an out-of-pattern
Device_Read_Bucket whose UseCase<id> statement is missing or does not cover
every such bucket. (portal-authorization-and-iam-scoping, task 6.3; design I6.)

Read-only: IAM GetRole, ListRolePolicies, GetRolePolicy,
ListAttachedRolePolicies, GetPolicy, GetPolicyVersion, and DynamoDB Scan when
the tables are given. Runs on the developer's Python 3.9+ and local boto3.
Account ids are masked in the output.

Exit status: 0 no finding that fails the check; 2 a hand-attached policy
carries an S3 action; 1 usage or read error.
"""
import argparse
import fnmatch
import json
import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

# Shape of the portal-written Device_Access_Document (task 6.1's
# device_grants.py defines the same constants; its unit test asserts they are
# equal to these).
DEVICE_ACCESS_POLICY_NAME = 'DDAUseCaseDeviceAccess'
SID_RE = r'^UseCase[0-9a-f]{32}(Results)?$'
READ_ACTIONS = ('s3:GetObject', 's3:GetObjectVersion', 's3:GetBucketLocation')
RESULTS_ACTIONS = ('s3:PutObject', 's3:PutObjectTagging', 's3:GetObject', 's3:GetBucketLocation')
TEMPLATE_BUCKET_PATTERNS = ('dda-*', '*-dda-*')

# The workflow-tuning sample grant deployments.py writes on the device role.
TUNING_SAMPLE_POLICY_NAME = 'DDAWorkflowTuningSampleAccess'
TUNING_SAMPLE_ACTIONS = ('s3:PutObject', 's3:GetObject')
TUNING_SAMPLE_RESOURCE_RE = re.compile(r'^arn:aws:s3:::[A-Za-z0-9._-]+/workflow-tuning/\*$')

# The stream-camera credential grant stream_credentials.py writes on the
# device role (each thing reads only its own secrets; no S3 action).
STREAM_CREDENTIAL_POLICY_NAME = 'DDAStreamCameraCredentialRead'
STREAM_CREDENTIAL_ACTIONS = ('secretsmanager:GetSecretValue',)
STREAM_CREDENTIAL_RESOURCE_RE = re.compile(
    r'^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[^*]+/\$\{credentials-iot:ThingName\}/\*$')

INLINE_POLICY_SIZE_LIMIT = 10_240

# The Device_Role_Template set: policy names the repository's station scripts
# and the account stack create on the device role, and the S3 statements
# (actions, resource patterns) they carry. The Greengrass provisioner, which
# setup_station.sh runs, also creates a managed policy named after the role
# with this suffix (logs and s3:GetBucketLocation).
DEVICE_ROLE_TEMPLATE_POLICIES = (
    'DDAPortalComponentAccessPolicy',        # usecase-account-stack.ts (managed)
    'GreengrassComponentS3Access',           # setup_station.sh (inline)
    'CloudWatchLogsPolicy',                  # setup_station.sh (inline)
    'ECRComponentAccess',                    # setup_station.sh (inline)
    'ShadowManagerSyncPolicy',               # setup_station.sh (inline)
    'dda-edge-device-policy',                # create-edge-device-iam-role.sh (inline)
    'DDAEdgeDevicePolicy',                   # edge-device-iam-policy.json
)
PROVISIONER_POLICY_SUFFIX = 'Access'
DEVICE_ROLE_TEMPLATE_S3_STATEMENTS = (
    # usecase-account-stack.ts DDAPortalComponentAccessPolicy
    (('s3:GetObject', 's3:GetBucketLocation'),
     ('arn:aws:s3:::dda-component-*', 'arn:aws:s3:::dda-component-*/*')),
    (('s3:GetObject', 's3:GetBucketLocation', 's3:HeadObject'),
     ('arn:aws:s3:::dda-*', 'arn:aws:s3:::dda-*/*', 'arn:aws:s3:::*-dda-*', 'arn:aws:s3:::*-dda-*/*')),
    (('s3:PutObject', 's3:PutObjectTagging', 's3:GetObject', 's3:GetBucketLocation'),
     ('arn:aws:s3:::dda-inference-results-*', 'arn:aws:s3:::dda-inference-results-*/*')),
    # setup_station.sh GreengrassComponentS3Access
    (('s3:GetObject', 's3:GetObjectVersion'),
     ('arn:aws:s3:::dda-component-*/*', 'arn:aws:s3:::dda-component-us-east-1-*/*')),
    # create-edge-device-iam-role.sh dda-edge-device-policy
    (('s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:GetBucketLocation',
      's3:GetBucketVersioning', 's3:ListBucketVersions'),
     ('arn:aws:s3:::dda-component-*', 'arn:aws:s3:::dda-component-*/*',
      'arn:aws:s3:::dda-inference-results-*', 'arn:aws:s3:::dda-inference-results-*/*')),
    # edge-device-iam-policy.json
    (('s3:GetObject', 's3:GetObjectVersion'),
     ('arn:aws:s3:::dda-component-*/*', 'arn:aws:s3:::greengrass-component-artifacts-*/*')),
    # the Greengrass provisioner's role access policy
    (('s3:GetBucketLocation',), ('*',)),
)

# Portal Lambda roles: CloudFormation-created roles of the portal stacks.
PORTAL_ROLE_PREFIX = 'EdgeCVPortal'
PORTAL_AWS_MANAGED_POLICIES = ('AWSLambdaBasicExecutionRole', 'AmazonEventBridgeFullAccess',
                               'AmazonSSMManagedInstanceCore')
# CDK names an iam.Policy / default policy after its construct path plus an
# eight-character hash.
CDK_INLINE_POLICY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*[0-9A-F]{8}$')

OBJECT_ACTIONS = ('s3:getobject', 's3:putobject', 's3:deleteobject', 's3:getobjectversion',
                  's3:deleteobjectversion', 's3:putobjecttagging', 's3:getobjecttagging',
                  's3:abortmultipartupload', 's3:listmultipartuploadparts', 's3:restoreobject')
BUCKET_LIST_ACTIONS = ('s3:listbucket', 's3:getbucketlocation')

ACCOUNT_RE = re.compile(r'(?<![0-9])[0-9]{12}(?![0-9])')
_SID = re.compile(SID_RE)
_EXPLICIT_BUCKET_ARN = re.compile(r'^arn:aws:s3:::[A-Za-z0-9._-]+(/\*)?$')

IN_TEMPLATE = 'in-template'
PORTAL_WRITTEN = 'portal-written'
HAND_ATTACHED = 'hand-attached'


def mask(text: Any) -> str:
    """Mask every twelve-digit account id to its last four digits."""
    return ACCOUNT_RE.sub(lambda m: '********' + m.group(0)[-4:], str(text))


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def policy_document(value: Any) -> Dict[str, Any]:
    """A policy document from an IAM response (dict, or URL-encoded JSON)."""
    if isinstance(value, str):
        value = json.loads(unquote(value))
    return value or {}


def statements(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in _as_list(document.get('Statement')) if isinstance(s, dict)]


def _actions(statement: Dict[str, Any]) -> List[str]:
    return [str(a).lower() for a in _as_list(statement.get('Action'))]


def is_s3_statement(statement: Dict[str, Any]) -> bool:
    """An Allow statement that can grant an S3 action (NotAction counts)."""
    if statement.get('Effect') != 'Allow':
        return False
    if 'NotAction' in statement:
        return True
    return any(a == '*' or a.startswith('s3:') for a in _actions(statement))


def _action_covered(action: str, allowed: Iterable[str]) -> bool:
    allowed = [a.lower() for a in allowed]
    if action in allowed:
        return True
    return '*' not in action and '?' not in action and any(fnmatch.fnmatchcase(action, a) for a in allowed)


def _resource_covered(resource: str, patterns: Iterable[str]) -> bool:
    return any(resource == p or fnmatch.fnmatchcase(resource, p) for p in patterns)


def covered_by_templates(statement: Dict[str, Any],
                         templates: Iterable[Tuple[Tuple[str, ...], Tuple[str, ...]]]) -> bool:
    """True when one template statement allows every action on every resource."""
    if 'NotAction' in statement or 'NotResource' in statement:
        return False
    actions = [a for a in _actions(statement) if a == '*' or a.startswith('s3:')]
    resources = [str(r) for r in _as_list(statement.get('Resource'))]
    return any(all(_action_covered(a, t_actions) for a in actions)
               and all(_resource_covered(r, t_resources) for r in resources)
               for t_actions, t_resources in templates)


def _portal_written_device_access(document: Dict[str, Any]) -> bool:
    stmts = statements(document)
    if not stmts:
        return False
    for statement in stmts:
        sid = statement.get('Sid')
        if (statement.get('Effect') != 'Allow' or not isinstance(sid, str) or not _SID.match(sid)
                or {'NotAction', 'NotResource', 'Condition'} & set(statement)):
            return False
        permitted = RESULTS_ACTIONS if sid.endswith('Results') else READ_ACTIONS
        if not all(_action_covered(a, permitted) and '*' not in a for a in _actions(statement)):
            return False
        resources = _as_list(statement.get('Resource'))
        if not resources or not all(isinstance(r, str) and _EXPLICIT_BUCKET_ARN.match(r) for r in resources):
            return False
    return True


def _portal_written_single_shape(document: Dict[str, Any], actions: Iterable[str],
                                 resource_re: 're.Pattern') -> bool:
    """Every statement an unconditioned Allow of `actions` (no wildcard) on
    resources matching `resource_re`."""
    stmts = statements(document)
    if not stmts:
        return False
    for statement in stmts:
        if (statement.get('Effect') != 'Allow'
                or {'NotAction', 'NotResource', 'Condition'} & set(statement)):
            return False
        if not all(_action_covered(a, actions) and '*' not in a for a in _actions(statement)):
            return False
        resources = _as_list(statement.get('Resource'))
        if not resources or not all(isinstance(r, str) and resource_re.match(r) for r in resources):
            return False
    return True


def is_device_template_name(name: str, kind: str, role_name: str) -> bool:
    """A Device_Role_Template policy name, or the provisioner's managed policy
    for this role (`<role name>Access`)."""
    return name in DEVICE_ROLE_TEMPLATE_POLICIES or (
        kind == 'customer-managed' and bool(role_name) and name == role_name + PROVISIONER_POLICY_SUFFIX)


def classify_policy(name: str, document: Dict[str, Any], *, kind: str, portal_lambda_role: bool,
                    role_prefix: str = PORTAL_ROLE_PREFIX, role_name: str = '') -> str:
    """`in-template` | `portal-written` | `hand-attached` for one policy.
    `kind` is 'inline', 'aws-managed' or 'customer-managed'."""
    if name == DEVICE_ACCESS_POLICY_NAME:
        return PORTAL_WRITTEN if _portal_written_device_access(document) else HAND_ATTACHED
    if name == TUNING_SAMPLE_POLICY_NAME:
        return PORTAL_WRITTEN if _portal_written_single_shape(
            document, TUNING_SAMPLE_ACTIONS, TUNING_SAMPLE_RESOURCE_RE) else HAND_ATTACHED
    if name == STREAM_CREDENTIAL_POLICY_NAME:
        return PORTAL_WRITTEN if _portal_written_single_shape(
            document, STREAM_CREDENTIAL_ACTIONS, STREAM_CREDENTIAL_RESOURCE_RE) else HAND_ATTACHED
    if portal_lambda_role:
        if kind == 'aws-managed':
            return IN_TEMPLATE if name in PORTAL_AWS_MANAGED_POLICIES else HAND_ATTACHED
        if kind == 'customer-managed':
            return IN_TEMPLATE if name.startswith(role_prefix) else HAND_ATTACHED
        return IN_TEMPLATE if CDK_INLINE_POLICY_RE.match(name) else HAND_ATTACHED
    if kind != 'aws-managed' and is_device_template_name(name, kind, role_name) and all(
            covered_by_templates(s, DEVICE_ROLE_TEMPLATE_S3_STATEMENTS)
            for s in statements(document) if is_s3_statement(s)):
        return IN_TEMPLATE
    return HAND_ATTACHED


def is_portal_lambda_role(role: Dict[str, Any], role_prefix: str = PORTAL_ROLE_PREFIX) -> bool:
    trust = json.dumps(policy_document(role.get('AssumeRolePolicyDocument')))
    return str(role.get('RoleName', '')).startswith(role_prefix) and 'lambda.amazonaws.com' in trust


def statement_kinds(statement: Dict[str, Any]) -> Tuple[bool, bool]:
    """(object-level, bucket-level list/location) S3 statement of an Allow."""
    if statement.get('Effect') != 'Allow':
        return False, False
    actions = _actions(statement)
    wide = any(a in ('*', 's3:*') for a in actions) or 'NotAction' in statement
    obj = wide or any(any(fnmatch.fnmatchcase(o, a) for o in OBJECT_ACTIONS) for a in actions
                      if a.startswith('s3:'))
    bucket = wide or any(any(fnmatch.fnmatchcase(b, a) for b in BUCKET_LIST_ACTIONS) for a in actions
                         if a.startswith('s3:'))
    return obj, bucket


def document_size(document: Dict[str, Any]) -> int:
    return len(json.dumps(document, separators=(',', ':')))


def usecase_sid(usecase_id: str) -> str:
    return 'UseCase' + str(usecase_id).replace('-', '')


def device_read_buckets(record: Dict[str, Any]) -> List[str]:
    """The record's out-of-pattern Device_Read_Buckets: s3_bucket and every
    additional_s3_buckets entry not matching TEMPLATE_BUCKET_PATTERNS,
    de-duplicated and sorted."""
    additional = record.get('additional_s3_buckets') or []
    if isinstance(additional, (set, frozenset)):
        additional = sorted(additional)
    names = [record.get('s3_bucket')] + _as_list(additional)
    return sorted({n for n in names if isinstance(n, str) and n.strip()
                   and not any(fnmatch.fnmatchcase(n, p) for p in TEMPLATE_BUCKET_PATTERNS)})


def missing_or_stale(usecases: List[Dict[str, Any]], deployed: Iterable[str], account_id: str,
                     device_document: Optional[Dict[str, Any]]) -> List[Tuple[str, str, List[str]]]:
    """[(usecase_id, 'missing' | 'stale', uncovered buckets)] for the use cases
    of `account_id` with a deployment and a non-empty device_read_buckets."""
    deployed_ids = set(deployed)
    by_sid = {s.get('Sid'): s for s in statements(device_document or {})}
    findings = []
    for record in sorted(usecases, key=lambda r: str(r.get('usecase_id'))):
        usecase_id = record.get('usecase_id')
        if usecase_id not in deployed_ids or record.get('account_id') != account_id:
            continue
        buckets = device_read_buckets(record)
        if not buckets:
            continue
        statement = by_sid.get(usecase_sid(usecase_id))
        if statement is None:
            findings.append((usecase_id, 'missing', buckets))
            continue
        resources = set(_as_list(statement.get('Resource')))
        uncovered = [b for b in buckets
                     if f'arn:aws:s3:::{b}' not in resources or f'arn:aws:s3:::{b}/*' not in resources]
        if uncovered:
            findings.append((usecase_id, 'stale', uncovered))
    return findings


def scan_all(table: Any, projection: Iterable[str]) -> List[Dict[str, Any]]:
    names = {f'#a{i}': name for i, name in enumerate(projection)}
    kwargs: Dict[str, Any] = {'ProjectionExpression': ', '.join(names), 'ExpressionAttributeNames': names}
    items: List[Dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        if not response.get('LastEvaluatedKey'):
            return items
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']


def _paged(call: Callable[..., Dict[str, Any]], key: str, **kwargs: Any) -> List[Any]:
    items: List[Any] = []
    while True:
        response = call(**kwargs)
        items.extend(response.get(key, []))
        if not response.get('IsTruncated'):
            return items
        kwargs['Marker'] = response['Marker']


def read_role(iam: Any, role_name: str) -> Dict[str, Any]:
    """The role and every policy document on it (IAM read calls only)."""
    role = iam.get_role(RoleName=role_name)['Role']
    policies = []
    for name in _paged(iam.list_role_policies, 'PolicyNames', RoleName=role_name):
        document = iam.get_role_policy(RoleName=role_name, PolicyName=name)['PolicyDocument']
        policies.append({'name': name, 'kind': 'inline', 'document': policy_document(document)})
    for attached in _paged(iam.list_attached_role_policies, 'AttachedPolicies', RoleName=role_name):
        arn = attached['PolicyArn']
        version = iam.get_policy(PolicyArn=arn)['Policy']['DefaultVersionId']
        document = iam.get_policy_version(PolicyArn=arn, VersionId=version)['PolicyVersion']['Document']
        policies.append({'name': attached['PolicyName'], 'arn': arn, 'version': version,
                         'kind': 'aws-managed' if ':aws:policy/' in arn else 'customer-managed',
                         'document': policy_document(document)})
    return {'role': role, 'policies': policies}


def check(state: Dict[str, Any], out: Callable[[str], None], *, role_prefix: str = PORTAL_ROLE_PREFIX,
          usecases: Optional[List[Dict[str, Any]]] = None,
          deployed: Optional[Iterable[str]] = None) -> int:
    """Classify and print; return the exit status."""
    role, policies = state['role'], state['policies']
    lambda_role = is_portal_lambda_role(role, role_prefix)
    arn_parts = str(role.get('Arn', '')).split(':')
    account_id = arn_parts[4] if len(arn_parts) > 5 else ''
    out(f"role {role.get('RoleName')} ({role.get('Arn')}): "
        f"{'portal Lambda role' if lambda_role else 'device or other role'}")
    hand_attached_s3 = []
    for policy in policies:
        verdict = classify_policy(policy['name'], policy['document'], kind=policy['kind'],
                                  portal_lambda_role=lambda_role, role_prefix=role_prefix,
                                  role_name=str(role.get('RoleName', '')))
        policy['verdict'] = verdict
        label = policy['kind'] + (f", version {policy['version']}" if policy.get('version') else '')
        out(f"policy {policy['name']} ({label}): {verdict}")
        if verdict == HAND_ATTACHED:
            for statement in statements(policy['document']):
                if is_s3_statement(statement):
                    hand_attached_s3.append((policy['name'], statement))
    out(f'hand-attached S3 statements: {len(hand_attached_s3)}')
    for name, statement in hand_attached_s3:
        out(f"  {name} [{statement.get('Sid', '-')}] {json.dumps(statement, sort_keys=True)}")
    size = sum(document_size(p['document']) for p in policies if p['kind'] == 'inline')
    out(f'inline policy size: {size} / {INLINE_POLICY_SIZE_LIMIT} characters')
    if lambda_role:
        for title, index in (('S3 object statements', 0), ('S3 ListBucket / GetBucketLocation statements', 1)):
            out(f'{title}:')
            for policy in policies:
                for statement in statements(policy['document']):
                    if statement_kinds(statement)[index]:
                        condition = statement.get('Condition')
                        out(f"  {policy['name']} [{statement.get('Sid', '-')}] "
                            f"{json.dumps(_as_list(statement.get('Action') or statement.get('NotAction')))} on "
                            f"{json.dumps(_as_list(statement.get('Resource')))}: "
                            f"{json.dumps(condition, sort_keys=True) if condition else 'UNCONDITIONED'}")
    if usecases is not None:
        device_document = next((p['document'] for p in policies
                                if p['name'] == DEVICE_ACCESS_POLICY_NAME and p['kind'] == 'inline'), None)
        findings = missing_or_stale(usecases, deployed or [], account_id, device_document)
        out(f"device access statements: {DEVICE_ACCESS_POLICY_NAME} "
            f"{'present' if device_document is not None else 'absent'}; "
            f"use cases missing or stale: {len(findings)}")
        for usecase_id, finding, buckets in findings:
            out(f"  {finding} {usecase_sid(usecase_id)} (use case {usecase_id}): {', '.join(buckets)}")
    return 2 if hand_attached_s3 else 0


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1, so exit 2 always means the hand-attached finding."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f'{self.prog}: error: {message}\n')


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog='portal-role-drift-check.py',
                     description='Read-only drift check of one IAM role against the repository templates.',
                     epilog='Exit status: 0 clean, 2 a hand-attached policy carries an S3 action, '
                            '1 usage or read error. Table defaults: the UseCasesTableName / '
                            'DeploymentsTableName outputs of EdgeCVPortalStorageStack, fixed names '
                            'dda-portal-usecases / dda-portal-deployments.')
    parser.add_argument('--role-name', required=True)
    parser.add_argument('--profile')
    parser.add_argument('--region')
    parser.add_argument('--usecases-table', help='with --deployments-table: report missing or stale '
                                                 'device access statements')
    parser.add_argument('--deployments-table')
    parser.add_argument('--portal-role-prefix', default=PORTAL_ROLE_PREFIX,
                        help=f'name prefix of the portal stacks\' roles (default {PORTAL_ROLE_PREFIX})')
    return parser


def main(argv: Optional[List[str]] = None, *, session_factory: Optional[Callable[..., Any]] = None,
         out: Optional[Callable[[str], None]] = None) -> int:
    args = build_parser().parse_args(argv)
    emit = out or (lambda line: print(mask(line)))
    if bool(args.usecases_table) != bool(args.deployments_table):
        emit('--usecases-table and --deployments-table are used together')
        return 1
    if session_factory is None:
        import boto3  # local boto3 on the developer's machine
        session_factory = boto3.session.Session
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        state = read_role(session.client('iam'), args.role_name)
        usecases = deployed = None
        if args.usecases_table:
            dynamodb = session.resource('dynamodb')
            usecases = scan_all(dynamodb.Table(args.usecases_table),
                                ('usecase_id', 's3_bucket', 'additional_s3_buckets', 'account_id'))
            deployed = {item.get('usecase_id') for item in
                        scan_all(dynamodb.Table(args.deployments_table), ('usecase_id',))}
    except Exception as error:
        code = ((getattr(error, 'response', None) or {}).get('Error') or {}).get('Code')
        emit(f'read failed: {code or type(error).__name__}')
        return 1
    return check(state, emit, role_prefix=args.portal_role_prefix, usecases=usecases, deployed=deployed)


if __name__ == '__main__':
    sys.exit(main())
