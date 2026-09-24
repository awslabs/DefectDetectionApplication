"""
Git_Sync_Service Lambda function (custom-node-source-lifecycle,
Requirements 2, 3, 4, 9.4, 9.5)

Token-based, bidirectional synchronization of Plugin_Version source trees
with GitHub / GitLab repositories. The Lambda manages Git_Connections
(per-Use_Case repository + Secrets Manager token), Git_Links (per
Plugin_Version branch + Repository_Path), and Sync_Operations (verify,
push, pull) that execute asynchronously in the `dda-plugin-git-sync`
CodeBuild project - the only place in the portal with a git binary and
outbound internet. Results settle through EventBridge exactly like plugin
builds and imports do.

Routes (API Gateway REST, node-designer-api-stack.ts):
    GET    /git-connections?usecase_id=            list (no secret ARN)
    POST   /git-connections                        create + verify      (2.1-2.5)
    GET    /git-connections/{cid}                  detail
    PUT    /git-connections/{cid}                  update; re-verify on url/token change
    DELETE /git-connections/{cid}                  delete + schedule secret deletion (2.8)
    POST   /git-connections/{cid}/verify           re-run verification
    PUT    /plugins/{id}/versions/{v}/git          set the Git_Link       (3.1)
    DELETE /plugins/{id}/versions/{v}/git          remove the Git_Link
    POST   /plugins/{id}/versions/{v}/git/push     start a Push          (3.2)
    POST   /plugins/{id}/versions/{v}/git/pull     start a Pull          (4.1)
    GET    /plugins/{id}/versions/{v}/git/operations   history, newest first (4.10)
    GET    /git-sync-operations/{opId}             poll one operation

EventBridge (rule 'dda-portal-git-sync-results'):
    CodeBuild Build State Change events of the git-sync project. The
    runner always writes plugin-git-sync/{operation_id}/result.json; the
    handler reads it (falling back to the CloudWatch log tail as an
    `internal` failure) and settles the operation, the connection's
    verification status, or the version's last sync / pulled tree.

Credential handling (2.3, 2.4): the token is written to Secrets Manager
under dda-portal/git-connections/{usecase}/{connection}; only the secret
ARN is stored. This Lambda's role has no secretsmanager:GetSecretValue -
the token is resolved inside CodeBuild from a SECRETS_MANAGER-typed
environment variable, so no portal Lambda ever holds it, and it never
appears in responses, audit entries, or logs.
"""
import json
import logging
import os
import posixpath
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, rbac_manager, Permission
)

# Plugin_Record persistence, envelopes, RBAC helpers, and the Source_Editor
# primitives (same deployment bundle as plugin_records.py).
import plugin_records
from plugin_records import (
    DEFAULT_SOURCE_REVISION,
    STATE_DEV,
    authorize_record_access,
    bump_source_revision,
    create_version_from_source,
    decimal_to_native,
    error_response,
    get_version_item,
    has_node_designer_permission,
    list_source_objects,
    normalize_source_path,
    not_found_response,
    now_ms,
    parse_body,
    plugin_table,
    scaffold_defects_for_tree,
    source_revision_of,
    stale_architectures,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
secretsmanager = boto3.client('secretsmanager')
codebuild = boto3.client('codebuild')
logs_client = boto3.client('logs')

# Environment variables (node-designer-stack.ts lambdaEnvironment)
GIT_CONNECTIONS_TABLE = os.environ.get('GIT_CONNECTIONS_TABLE')
GIT_SYNC_OPERATIONS_TABLE = os.environ.get('GIT_SYNC_OPERATIONS_TABLE')
GIT_SYNC_PROJECT_NAME = os.environ.get('GIT_SYNC_PROJECT_NAME', 'dda-plugin-git-sync')
GIT_SECRET_PREFIX = os.environ.get('GIT_SECRET_PREFIX', 'dda-portal/git-connections')
PLUGIN_GIT_SYNC_PREFIX = os.environ.get('PLUGIN_GIT_SYNC_PREFIX', 'plugin-git-sync')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')

USECASE_CONNECTIONS_INDEX = 'usecase-connections-index'
PLUGIN_OPERATIONS_INDEX = 'plugin-operations-index'

# ---------------------------------------------------------------- constants

# Provider constants, the Failure_Category vocabulary, the stderr
# classifier, the excerpt redactor, and the token environment override are
# shared with plugin_importer.py through the shared layer
# (private-repo-plugin-import task 1) and re-exported here under their
# original names so existing callers and tests are unchanged.
from git_connections import (  # noqa: E402
    CONNECTION_FAILED,
    CONNECTION_VERIFIED,
    CONNECTION_VERIFYING,
    FAILURE_CATEGORIES,
    PROVIDERS,
    PROVIDER_USERNAMES,
    _CLASSIFY_RULES,
    _REDACTIONS,
    classify_failure,
    get_connection,
    redact,
    token_env_override,
)

OP_VERIFY = 'verify'
OP_PUSH = 'push'
OP_PULL = 'pull'
OPERATION_KINDS = (OP_VERIFY, OP_PUSH, OP_PULL)

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'
SETTLED_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)
IN_FLIGHT_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

PULL_IN_PLACE = 'in_place'
PULL_NEW_VERSION = 'new_version'
PULL_MODES = (PULL_IN_PLACE, PULL_NEW_VERSION)

#: Sync_Operation retention (design data model)
OPERATION_TTL_SECONDS = 180 * 24 * 3600

#: Bound on the scaffold files read back for pull validation.
MAX_VALIDATION_FILE_BYTES = 512 * 1024

#: Redacted log excerpt size stored on failed operations.
LOG_EXCERPT_MAX_CHARS = 8 * 1024

SYSTEM_USER_ID = 'system:git-sync-service'


# ------------------------------------------------------------ pure helpers

def validate_connection(body: Dict, partial: bool = False) -> Optional[Dict]:
    """
    Validate a Git_Connection payload (Requirements 2.1, 2.2, Property 9):
    provider in PROVIDERS, an https URL with a host, non-empty name and
    default branch, and (on create) a non-empty token. `partial` validates
    only the fields present (PUT).
    """
    def present(field):
        return field in body

    if not partial or present('provider'):
        if body.get('provider') not in PROVIDERS:
            return error_response(400, 'INVALID_PROVIDER',
                                  f"provider must be one of: {', '.join(PROVIDERS)}")
    if not partial or present('repo_url'):
        url_error = validate_repo_url(body.get('repo_url'))
        if url_error:
            return error_response(400, 'INVALID_REPO_URL', url_error,
                                  {'repo_url': body.get('repo_url')})
    for field in ('name', 'default_branch'):
        if not partial or present(field):
            value = body.get(field)
            if not isinstance(value, str) or not value.strip():
                return error_response(400, 'MISSING_FIELDS',
                                      f'{field} must be a non-empty string')
    if not partial or present('token'):
        token = body.get('token')
        if not isinstance(token, str) or not token.strip():
            return error_response(400, 'MISSING_FIELDS',
                                  'token must be a non-empty string')
    return None


def validate_repo_url(url: Any) -> Optional[str]:
    """None when `url` is a syntactically valid https URL with a host;
    otherwise the rejection reason (2.2)."""
    if not isinstance(url, str) or not url.strip():
        return 'repo_url is required'
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https':
        return 'repo_url must use the https scheme'
    if not parsed.netloc or '@' in parsed.netloc:
        return 'repo_url must name a host without embedded credentials'
    if not parsed.path or parsed.path == '/':
        return 'repo_url must include the repository path'
    return None


def normalize_repo_path(path: Any) -> Optional[str]:
    """Repository_Path confinement (3.1, Property 2): relative, non-empty,
    no `..` segment - the same rule as Source_Tree paths."""
    clean = normalize_source_path(path)
    if clean is None:
        return None
    return posixpath.normpath(clean.replace('\\', '/'))


def connection_view(item: Dict) -> Dict:
    """API view of a Git_Connection: never the secret ARN or a token (2.4)."""
    return {
        'connection_id': item['connection_id'],
        'usecase_id': item['usecase_id'],
        'name': item.get('name'),
        'provider': item.get('provider'),
        'repo_url': item.get('repo_url'),
        'default_branch': item.get('default_branch'),
        'status': item.get('status'),
        'verification': item.get('verification') or {},
        'created_by': item.get('created_by'),
        'created_at': item.get('created_at'),
        'updated_by': item.get('updated_by'),
        'updated_at': item.get('updated_at'),
    }


def operation_view(item: Dict) -> Dict:
    return {
        'operation_id': item['operation_id'],
        'usecase_id': item.get('usecase_id'),
        'connection_id': item.get('connection_id'),
        'plugin_id': item.get('plugin_id'),
        'version': item.get('version'),
        'kind': item.get('kind'),
        'target': item.get('target') or {},
        'status': item.get('status'),
        'build_id': item.get('build_id'),
        'started_by': item.get('started_by'),
        'started_at': item.get('started_at'),
        'finished_at': item.get('finished_at'),
        'result': item.get('result'),
        'failure': item.get('failure'),
    }


def secret_name_for(usecase_id: str, connection_id: str) -> str:
    return f"{GIT_SECRET_PREFIX}/{usecase_id}/{connection_id}"


def staging_prefix_for(operation_id: str) -> str:
    return f"{PLUGIN_GIT_SYNC_PREFIX}/{operation_id}/tree/"


def result_key_for(operation_id: str) -> str:
    return f"{PLUGIN_GIT_SYNC_PREFIX}/{operation_id}/result.json"


def build_sync_manifest(item: Dict, user_id: str) -> Dict:
    """The Sync_Manifest (dda-plugin.json) a Push writes (design data model)."""
    provenance = item.get('provenance') or {}
    declaration = provenance.get('scaffoldDeclaration')
    try:
        declaration_obj = json.loads(declaration) if isinstance(declaration, str) else declaration
    except (TypeError, ValueError):
        declaration_obj = None
    return {
        'ddaPlugin': 1,
        'pluginId': item['plugin_id'],
        'version': int(item['version']),
        'sourceRevision': source_revision_of(item),
        'kind': item.get('kind'),
        'name': item.get('name'),
        'scaffoldDeclaration': declaration_obj,
        'pushedBy': user_id,
        'pushedAt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'portal': 'edge-cv-portal',
    }


def build_start_environment(kind: str, operation_id: str, connection: Dict,
                            item: Optional[Dict], target: Dict,
                            manifest: Optional[Dict] = None,
                            message: Optional[str] = None) -> List[Dict]:
    """
    The StartBuild environmentVariablesOverride of a Sync_Operation
    (design §5 table, Property 10): exactly one SECRETS_MANAGER variable
    (GIT_TOKEN = "{secret_arn}:token"), everything else PLAINTEXT, and no
    plaintext value ever carries the token.
    """
    def plain(name, value):
        return {'name': name, 'value': '' if value is None else str(value),
                'type': 'PLAINTEXT'}

    provider = connection.get('provider') or 'github'
    env = [
        plain('SYNC_KIND', kind),
        plain('OPERATION_ID', operation_id),
        plain('USECASE_ID', connection.get('usecase_id')),
        plain('REPO_URL', connection.get('repo_url')),
        plain('GIT_PROVIDER', provider),
        plain('GIT_USERNAME', PROVIDER_USERNAMES.get(provider, 'x-access-token')),
        token_env_override(connection),
        plain('DEFAULT_BRANCH', connection.get('default_branch')),
        plain('RESULT_KEY', result_key_for(operation_id)),
    ]
    if item is not None:
        env += [plain('PLUGIN_ID', item['plugin_id']),
                plain('PLUGIN_VERSION', item['version'])]
    if kind in (OP_PUSH, OP_PULL):
        env += [plain('BRANCH', target.get('branch')),
                plain('REPO_PATH', target.get('path'))]
    if kind == OP_PUSH:
        env += [
            plain('SOURCE_PREFIX', (item or {}).get('source_s3_prefix')),
            plain('LAST_SYNC_COMMIT', target.get('last_sync_commit')),
            plain('FORCE', '1' if target.get('force') else '0'),
            plain('COMMIT_MESSAGE', message),
            plain('MANIFEST_JSON', json.dumps(manifest or {}, sort_keys=True)),
        ]
    if kind == OP_PULL:
        env += [plain('REF', target.get('ref') or target.get('branch')),
                plain('STAGING_PREFIX', staging_prefix_for(operation_id))]
    return env


# ------------------------------------------------------------- persistence

def connections_table():
    return dynamodb.Table(GIT_CONNECTIONS_TABLE)


def operations_table():
    return dynamodb.Table(GIT_SYNC_OPERATIONS_TABLE)


def query_connections(usecase_id: str) -> List[Dict]:
    from boto3.dynamodb.conditions import Key
    items: List[Dict] = []
    kwargs = {'IndexName': USECASE_CONNECTIONS_INDEX,
              'KeyConditionExpression': Key('usecase_id').eq(usecase_id)}
    while True:
        response = connections_table().query(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return [decimal_to_native(i) for i in items]


def get_operation(operation_id: str) -> Optional[Dict]:
    response = operations_table().get_item(Key={'operation_id': operation_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def query_operations(plugin_id: str, version: Optional[int] = None) -> List[Dict]:
    from boto3.dynamodb.conditions import Key
    items: List[Dict] = []
    kwargs = {'IndexName': PLUGIN_OPERATIONS_INDEX,
              'KeyConditionExpression': Key('plugin_id').eq(plugin_id),
              'ScanIndexForward': False}
    while True:
        response = operations_table().query(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    ops = [decimal_to_native(i) for i in items]
    if version is not None:
        ops = [o for o in ops if int(o.get('version') or 0) == int(version)]
    return ops


def set_connection_status(connection_id: str, status: str,
                          verification: Dict) -> None:
    connections_table().update_item(
        Key={'connection_id': connection_id},
        UpdateExpression='SET #s = :s, verification = :v, updated_at = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': status, ':v': verification, ':t': now_ms()},
    )


def acquire_sync_lock(plugin_id: str, version: int, operation_id: str) -> bool:
    """Single-flight lock on the version (3.12): True when acquired."""
    try:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression='SET active_sync_operation = :op, updated_at = :t',
            ConditionExpression='attribute_not_exists(active_sync_operation)',
            ExpressionAttributeValues={':op': operation_id, ':t': now_ms()},
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False
        raise


def release_sync_lock(plugin_id: str, version: int, operation_id: str) -> None:
    """Release the lock iff this operation holds it."""
    try:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression='REMOVE active_sync_operation SET updated_at = :t',
            ConditionExpression='active_sync_operation = :op',
            ExpressionAttributeValues={':op': operation_id, ':t': now_ms()},
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            raise


def settle_operation(operation: Dict, status: str, result: Optional[Dict] = None,
                     failure: Optional[Dict] = None) -> Dict:
    """Write the terminal state of a Sync_Operation and return the item."""
    names = {'#s': 'status'}
    values: Dict[str, Any] = {':s': status, ':t': now_ms()}
    sets = ['#s = :s', 'finished_at = :t']
    if result is not None:
        sets.append('#r = :r'); names['#r'] = 'result'; values[':r'] = result
    if failure is not None:
        sets.append('failure = :f'); values[':f'] = failure
    operations_table().update_item(
        Key={'operation_id': operation['operation_id']},
        UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    updated = dict(operation)
    updated.update({'status': status, 'finished_at': values[':t']})
    if result is not None:
        updated['result'] = result
    if failure is not None:
        updated['failure'] = failure
    return updated


# ------------------------------------------------------------ authorization

def can_manage(user: Dict, usecase_id: str) -> bool:
    return has_node_designer_permission(user, usecase_id, Permission.NODE_DESIGNER_MANAGE)


def can_read(user: Dict, usecase_id: str) -> bool:
    return has_node_designer_permission(user, usecase_id, Permission.NODE_DESIGNER_READ)


def forbidden(user: Dict, event: Dict, usecase_id: str, permission: Permission) -> Dict:
    return plugin_records.forbidden_response(user, event, usecase_id, permission)


# ---------------------------------------------------------- operation start

def start_sync_operation(kind: str, connection: Dict, user_id: str,
                         item: Optional[Dict] = None,
                         target: Optional[Dict] = None,
                         message: Optional[str] = None) -> Dict:
    """
    Persist a queued Sync_Operation and StartBuild the git-sync project
    with the design's environment overrides. A StartBuild failure settles
    the operation as `internal` (and releases the version lock, which the
    caller acquired) so nothing stays in flight.
    """
    operation_id = str(uuid.uuid4())
    timestamp = now_ms()
    target = dict(target or {})
    operation: Dict[str, Any] = {
        'operation_id': operation_id,
        'usecase_id': connection['usecase_id'],
        'connection_id': connection['connection_id'],
        'kind': kind,
        'target': target,
        'status': STATUS_QUEUED,
        'started_by': user_id,
        'started_at': timestamp,
        'ttl': int(timestamp / 1000) + OPERATION_TTL_SECONDS,
    }
    if item is not None:
        operation['plugin_id'] = item['plugin_id']
        operation['version'] = int(item['version'])
    operations_table().put_item(Item=operation)

    manifest = build_sync_manifest(item, user_id) if (kind == OP_PUSH and item) else None
    env = build_start_environment(kind, operation_id, connection, item, target,
                                  manifest=manifest, message=message)
    try:
        start = codebuild.start_build(projectName=GIT_SYNC_PROJECT_NAME,
                                      environmentVariablesOverride=env)
    except Exception as exc:  # StartBuild failure settles synchronously
        logger.error(f"git-sync StartBuild failed for {operation_id}: {exc}",
                     exc_info=True)
        failure = {'category': 'internal',
                   'message': 'The sync runner could not be started',
                   'log_excerpt': redact(str(exc))[-LOG_EXCERPT_MAX_CHARS:]}
        settled = settle_operation(operation, STATUS_FAILED, failure=failure)
        if item is not None:
            release_sync_lock(item['plugin_id'], int(item['version']), operation_id)
        if kind == OP_VERIFY:
            set_connection_status(connection['connection_id'], CONNECTION_FAILED,
                                  {'at': now_ms(), 'category': 'internal',
                                   'message': failure['message'],
                                   'operation_id': operation_id})
        return settled

    build_id = start['build']['id']
    operations_table().update_item(
        Key={'operation_id': operation_id},
        UpdateExpression='SET build_id = :b, #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':b': build_id, ':s': STATUS_RUNNING},
    )
    operation.update({'build_id': build_id, 'status': STATUS_RUNNING})
    return operation


# ----------------------------------------------------- Git_Connection routes

def list_connections(event: Dict, user: Dict) -> Dict:
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    if usecase_id:
        if not can_read(user, usecase_id):
            return forbidden(user, event, usecase_id, Permission.NODE_DESIGNER_READ)
        usecase_ids = [usecase_id]
    else:
        usecase_ids = rbac_manager.get_accessible_usecases(user['user_id'], user_info=user)
    items: List[Dict] = []
    for uc in usecase_ids:
        items.extend(query_connections(uc))
    items.sort(key=lambda i: i.get('updated_at') or 0, reverse=True)
    return create_response(200, {'connections': [connection_view(i) for i in items],
                                 'count': len(items)})


def create_connection(event: Dict, user: Dict) -> Dict:
    """
    POST /git-connections
    Body: {usecase_id, name, provider, repo_url, default_branch, token}
    Stores the token in Secrets Manager, records the connection with the
    secret ARN and status `verifying`, and starts the verify operation
    (2.1-2.5). A failed item write removes the secret again.
    """
    body, err = parse_body(event)
    if err:
        return err
    usecase_id = body.get('usecase_id')
    if not usecase_id:
        return error_response(400, 'MISSING_FIELDS', 'Missing required fields: usecase_id')
    err = validate_connection(body)
    if err:
        return err
    if not can_manage(user, usecase_id):
        return forbidden(user, event, usecase_id, Permission.NODE_DESIGNER_MANAGE)
    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    connection_id = str(uuid.uuid4())
    timestamp = now_ms()
    secret = secretsmanager.create_secret(
        Name=secret_name_for(usecase_id, connection_id),
        Description=f"DDA portal Git connection {connection_id} ({body['name'].strip()})",
        SecretString=json.dumps({'token': body['token'].strip()}),
        Tags=[{'Key': 'dda-portal:managed', 'Value': 'true'},
              {'Key': 'dda-portal:usecase-id', 'Value': usecase_id},
              {'Key': 'dda-portal:connection-id', 'Value': connection_id}],
    )
    secret_arn = secret['ARN']
    item = {
        'connection_id': connection_id,
        'usecase_id': usecase_id,
        'name': body['name'].strip(),
        'provider': body['provider'],
        'repo_url': body['repo_url'].strip(),
        'default_branch': body['default_branch'].strip(),
        'secret_arn': secret_arn,
        'status': CONNECTION_VERIFYING,
        'verification': {},
        'created_by': user['user_id'],
        'created_at': timestamp,
        'updated_by': user['user_id'],
        'updated_at': timestamp,
    }
    try:
        connections_table().put_item(Item=item,
                                     ConditionExpression='attribute_not_exists(connection_id)')
    except Exception:
        # Roll back: never leave a token behind without its connection.
        try:
            secretsmanager.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
        except Exception as cleanup_error:  # pragma: no cover - best effort
            logger.error(f"Secret rollback failed for {secret_arn}: {cleanup_error}")
        raise

    log_audit_event(
        user_id=user['user_id'], action='create_git_connection',
        resource_type='git_connection', resource_id=connection_id, result='success',
        details={'usecase_id': usecase_id, 'provider': item['provider'],
                 'repo_url': item['repo_url'], 'default_branch': item['default_branch']})

    operation = start_sync_operation(OP_VERIFY, item, user['user_id'])
    refreshed = get_connection(connection_id) or item
    return create_response(202, {'connection': connection_view(refreshed),
                                 'operation': operation_view(operation)})


def _load_connection_for(user: Dict, event: Dict, connection_id: str,
                         manage: bool) -> Tuple[Optional[Dict], Optional[Dict]]:
    item = get_connection(connection_id)
    if not item:
        return None, error_response(404, 'CONNECTION_NOT_FOUND', 'Git connection not found')
    usecase_id = item['usecase_id']
    if not can_read(user, usecase_id):
        return None, error_response(404, 'CONNECTION_NOT_FOUND', 'Git connection not found')
    if manage and not can_manage(user, usecase_id):
        return None, forbidden(user, event, usecase_id, Permission.NODE_DESIGNER_MANAGE)
    return item, None


def get_connection_route(event: Dict, user: Dict, connection_id: str) -> Dict:
    item, err = _load_connection_for(user, event, connection_id, manage=False)
    if err:
        return err
    return create_response(200, {'connection': connection_view(item)})


def update_connection(event: Dict, user: Dict, connection_id: str) -> Dict:
    """
    PUT /git-connections/{cid}
    Body: {name?, provider?, repo_url?, default_branch?, token?}
    A new token replaces the secret value (2.7); a changed URL or token
    re-verifies the connection (2.5).
    """
    body, err = parse_body(event)
    if err:
        return err
    item, err = _load_connection_for(user, event, connection_id, manage=True)
    if err:
        return err
    err = validate_connection(body, partial=True)
    if err:
        return err
    changed = {}
    for field in ('name', 'provider', 'repo_url', 'default_branch'):
        if field in body and body[field] != item.get(field):
            changed[field] = body[field].strip() if isinstance(body[field], str) else body[field]
    token_changed = 'token' in body
    if not changed and not token_changed:
        return error_response(400, 'NO_UPDATES',
                              'Provide name, provider, repo_url, default_branch, or token')

    if token_changed:
        secretsmanager.put_secret_value(
            SecretId=item['secret_arn'],
            SecretString=json.dumps({'token': body['token'].strip()}))

    reverify = token_changed or 'repo_url' in changed or 'provider' in changed
    timestamp = now_ms()
    names = {}
    values: Dict[str, Any] = {':t': timestamp, ':u': user['user_id']}
    sets = ['updated_at = :t', 'updated_by = :u']
    for index, (field, value) in enumerate(sorted(changed.items())):
        names[f'#f{index}'] = field
        values[f':v{index}'] = value
        sets.append(f'#f{index} = :v{index}')
    if reverify:
        names['#s'] = 'status'
        values[':s'] = CONNECTION_VERIFYING
        values[':ver'] = {}
        sets += ['#s = :s', 'verification = :ver']
    kwargs = dict(Key={'connection_id': connection_id},
                  UpdateExpression='SET ' + ', '.join(sets),
                  ExpressionAttributeValues=values)
    if names:
        kwargs['ExpressionAttributeNames'] = names
    connections_table().update_item(**kwargs)

    log_audit_event(
        user_id=user['user_id'], action='update_git_connection',
        resource_type='git_connection', resource_id=connection_id, result='success',
        details={'usecase_id': item['usecase_id'], 'fields': sorted(changed),
                 'token_rotated': token_changed})

    updated = get_connection(connection_id)
    payload: Dict[str, Any] = {'connection': connection_view(updated)}
    status = 200
    if reverify:
        operation = start_sync_operation(OP_VERIFY, updated, user['user_id'])
        payload['operation'] = operation_view(operation)
        payload['connection'] = connection_view(get_connection(connection_id) or updated)
        status = 202
    return create_response(status, payload)


def delete_connection(event: Dict, user: Dict, connection_id: str) -> Dict:
    """DELETE /git-connections/{cid}: schedule the secret for deletion
    (default recovery window) and remove the connection (2.8). Linked
    Plugin_Versions keep their recorded sync provenance."""
    item, err = _load_connection_for(user, event, connection_id, manage=True)
    if err:
        return err
    try:
        secretsmanager.delete_secret(SecretId=item['secret_arn'])
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
            raise
    connections_table().delete_item(Key={'connection_id': connection_id})
    log_audit_event(
        user_id=user['user_id'], action='delete_git_connection',
        resource_type='git_connection', resource_id=connection_id, result='success',
        details={'usecase_id': item['usecase_id'], 'repo_url': item.get('repo_url')})
    return create_response(200, {'deleted': True, 'connection_id': connection_id})


def verify_connection(event: Dict, user: Dict, connection_id: str) -> Dict:
    item, err = _load_connection_for(user, event, connection_id, manage=True)
    if err:
        return err
    set_connection_status(connection_id, CONNECTION_VERIFYING, {})
    item['status'] = CONNECTION_VERIFYING
    operation = start_sync_operation(OP_VERIFY, item, user['user_id'])
    log_audit_event(
        user_id=user['user_id'], action='verify_git_connection',
        resource_type='git_connection', resource_id=connection_id, result='success',
        details={'usecase_id': item['usecase_id'], 'operation_id': operation['operation_id']})
    return create_response(202, {'connection': connection_view(get_connection(connection_id) or item),
                                 'operation': operation_view(operation)})


# ---------------------------------------------------------- Git_Link routes

def set_git_link(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    PUT /plugins/{id}/versions/{v}/git
    Body: {connection_id, branch?, path?}
    Records the Git_Link (3.1): branch defaults to the connection's default
    branch, path to the plugin's sanitized name. A previous last sync is
    kept only when the connection, branch, and path are unchanged.
    """
    body, err = parse_body(event)
    if err:
        return err
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True)
    if err:
        return err
    connection_id = body.get('connection_id')
    if not isinstance(connection_id, str) or not connection_id:
        return error_response(400, 'MISSING_FIELDS', 'Missing required fields: connection_id')
    connection = get_connection(connection_id)
    if not connection or connection['usecase_id'] != item['usecase_id']:
        return error_response(404, 'CONNECTION_NOT_FOUND', 'Git connection not found')

    branch = body.get('branch') or connection.get('default_branch')
    if not isinstance(branch, str) or not branch.strip():
        return error_response(400, 'MISSING_FIELDS', 'branch must be a non-empty string')
    import plugin_builds
    raw_path = body.get('path')
    if raw_path is None or raw_path == '':
        raw_path = plugin_builds.sanitize_plugin_name(item.get('name'), plugin_id)
    path = normalize_repo_path(raw_path)
    if path is None:
        return error_response(400, 'INVALID_REPO_PATH',
                              'path must be a relative repository path without ".." segments',
                              {'path': raw_path})

    previous = item.get('git') or {}
    link: Dict[str, Any] = {
        'connection_id': connection_id,
        'branch': branch.strip(),
        'path': path,
        'linked_by': user['user_id'],
        'linked_at': now_ms(),
    }
    if (previous.get('connection_id') == connection_id and previous.get('branch') == link['branch']
            and previous.get('path') == path and previous.get('last_sync')):
        link['last_sync'] = previous['last_sync']

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET git = :g, updated_at = :t',
        ExpressionAttributeValues={':g': link, ':t': now_ms()},
    )
    log_audit_event(
        user_id=user['user_id'], action='set_plugin_git_link',
        resource_type='plugin_record', resource_id=plugin_id, result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'connection_id': connection_id, 'branch': link['branch'], 'path': path})
    return create_response(200, {'git': link})


def remove_git_link(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True)
    if err:
        return err
    if item.get('active_sync_operation'):
        return error_response(409, 'SYNC_IN_PROGRESS',
                              'A sync operation is running for this version',
                              {'operation_id': item['active_sync_operation']})
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='REMOVE git SET updated_at = :t',
        ExpressionAttributeValues={':t': now_ms()},
    )
    log_audit_event(
        user_id=user['user_id'], action='remove_plugin_git_link',
        resource_type='plugin_record', resource_id=plugin_id, result='success',
        details={'usecase_id': item['usecase_id'], 'version': version})
    return create_response(200, {'git': None})


def _prepare_sync(event: Dict, user: Dict, plugin_id: str, version: int
                  ) -> Tuple[Optional[Tuple[Dict, Dict]], Optional[Dict]]:
    """Shared guards of push/pull: record + manage permission, Git_Link
    present, connection present and verified (2.6, 3.12)."""
    item = get_version_item(plugin_id, version)
    if not item:
        return None, not_found_response()
    err = authorize_record_access(user, event, item, manage=True)
    if err:
        return None, err
    link = item.get('git')
    if not isinstance(link, dict) or not link.get('connection_id'):
        return None, error_response(409, 'GIT_LINK_REQUIRED',
                                    'Link this version to a Git connection first')
    connection = get_connection(link['connection_id'])
    if not connection:
        return None, error_response(409, 'CONNECTION_NOT_VERIFIED',
                                    'The linked Git connection no longer exists',
                                    {'status': 'missing', 'connection_id': link['connection_id']})
    if connection.get('status') != CONNECTION_VERIFIED:
        return None, error_response(409, 'CONNECTION_NOT_VERIFIED',
                                    'The linked Git connection is not verified',
                                    {'status': connection.get('status'),
                                     'connection_id': connection['connection_id']})
    return (item, connection), None


def _start_locked(kind: str, item: Dict, connection: Dict, user: Dict,
                  target: Dict, message: Optional[str] = None) -> Dict:
    operation_id_probe = str(uuid.uuid4())
    plugin_id, version = item['plugin_id'], int(item['version'])
    if not acquire_sync_lock(plugin_id, version, operation_id_probe):
        current = get_version_item(plugin_id, version) or item
        return error_response(409, 'SYNC_IN_PROGRESS',
                              'A sync operation is already running for this version',
                              {'operation_id': current.get('active_sync_operation')})
    operation = start_sync_operation(kind, connection, user['user_id'], item=item,
                                     target=target, message=message)
    # The lock names the real operation id (the probe id reserved it).
    if operation['status'] != STATUS_FAILED:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression='SET active_sync_operation = :op',
            ConditionExpression='active_sync_operation = :probe',
            ExpressionAttributeValues={':op': operation['operation_id'],
                                       ':probe': operation_id_probe},
        )
    else:
        release_sync_lock(plugin_id, version, operation_id_probe)
    log_audit_event(
        user_id=user['user_id'], action=f'git_{kind}_started',
        resource_type='plugin_record', resource_id=plugin_id,
        result='success' if operation['status'] != STATUS_FAILED else 'failure',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'operation_id': operation['operation_id'], 'target': target})
    status = 202 if operation['status'] != STATUS_FAILED else 502
    return create_response(status, {'operation': operation_view(operation)})


def push(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """POST .../git/push  Body: {message?, force?}  (3.2, 3.7, 3.8, 3.11)"""
    body, err = parse_body(event)
    if err:
        return err
    prepared, err = _prepare_sync(event, user, plugin_id, version)
    if err:
        return err
    item, connection = prepared
    link = item['git']
    force = bool(body.get('force'))
    message = body.get('message')
    if message is not None and not isinstance(message, str):
        return error_response(400, 'INVALID_JSON', 'message must be a string')
    default_message = (f"DDA Portal: {item.get('name')} v{version} "
                       f"(source revision {source_revision_of(item)}) by {user['user_id']}")
    last_sync = (link.get('last_sync') or {})
    target = {'branch': link['branch'], 'path': link['path'], 'force': force,
              'last_sync_commit': last_sync.get('commit')}
    return _start_locked(OP_PUSH, item, connection, user, target,
                         message=(message.strip() if message and message.strip()
                                  else default_message))


def pull(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """POST .../git/pull  Body: {ref?, mode}  (4.1, 4.2)"""
    body, err = parse_body(event)
    if err:
        return err
    prepared, err = _prepare_sync(event, user, plugin_id, version)
    if err:
        return err
    item, connection = prepared
    link = item['git']
    mode = body.get('mode') or (PULL_IN_PLACE if item.get('lifecycle_state') == STATE_DEV
                                else PULL_NEW_VERSION)
    if mode not in PULL_MODES:
        return error_response(400, 'INVALID_JSON',
                              f"mode must be one of: {', '.join(PULL_MODES)}")
    if mode == PULL_IN_PLACE and item.get('lifecycle_state') != STATE_DEV:
        return error_response(
            409, 'SOURCE_LOCKED',
            f"Source of a '{item.get('lifecycle_state')}' version cannot be replaced in "
            'place; pull into a new version instead',
            {'lifecycle_state': item.get('lifecycle_state'), 'hint': 'pull as new version'})
    ref = body.get('ref')
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        return error_response(400, 'INVALID_JSON', 'ref must be a non-empty string')
    target = {'branch': link['branch'], 'path': link['path'], 'mode': mode,
              'ref': ref.strip() if ref else link['branch']}
    return _start_locked(OP_PULL, item, connection, user, target)


def list_operations(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err
    ops = query_operations(plugin_id, version)
    return create_response(200, {'operations': [operation_view(o) for o in ops],
                                 'count': len(ops)})


def get_operation_route(event: Dict, user: Dict, operation_id: str) -> Dict:
    op = get_operation(operation_id)
    if not op or not can_read(user, op.get('usecase_id') or ''):
        return error_response(404, 'OPERATION_NOT_FOUND', 'Sync operation not found')
    return create_response(200, {'operation': operation_view(op)})


# --------------------------------------------------------- result handling

def read_runner_result(operation_id: str) -> Optional[Dict]:
    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=result_key_for(operation_id))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        raise
    try:
        payload = json.loads(obj['Body'].read().decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def fetch_log_excerpt(detail: Dict) -> str:
    log_info = (detail.get('additional-information') or {}).get('logs') or {}
    group, stream = log_info.get('group-name'), log_info.get('stream-name')
    if not group or not stream:
        return ''
    try:
        response = logs_client.get_log_events(logGroupName=group, logStreamName=stream,
                                              limit=200, startFromHead=False)
    except Exception as e:  # the excerpt is best-effort
        logger.warning(f"Could not fetch git-sync log tail: {e}")
        return ''
    lines = [ev.get('message', '').rstrip('\n') for ev in response.get('events', [])]
    return '\n'.join(lines)[-LOG_EXCERPT_MAX_CHARS:]


def _failure_from(result: Optional[Dict], detail: Dict, build_status: str) -> Dict:
    if result and not result.get('ok'):
        category = result.get('category') if result.get('category') in FAILURE_CATEGORIES \
            else classify_failure(result.get('message'))
        failure = {'category': category,
                   'message': redact(result.get('message') or 'sync failed')[:2000],
                   'log_excerpt': ''}
        for key in ('changed_files', 'defects', 'limit', 'ref', 'last_sync_commit'):
            if key in result:
                failure[key] = result[key]
        return failure
    excerpt = redact(fetch_log_excerpt(detail))
    return {'category': classify_failure(excerpt) if excerpt else 'internal',
            'message': f'The sync runner ended with status {build_status}',
            'log_excerpt': excerpt}


def delete_prefix_objects(prefix: str) -> None:
    paginator = s3.get_paginator('list_objects_v2')
    keys: List[str] = []
    for page in paginator.paginate(Bucket=PORTAL_ARTIFACTS_BUCKET, Prefix=prefix):
        keys.extend(obj['Key'] for obj in page.get('Contents', []))
    for start in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=PORTAL_ARTIFACTS_BUCKET,
                          Delete={'Objects': [{'Key': k} for k in keys[start:start + 1000]],
                                  'Quiet': True})


def install_pulled_tree(operation: Dict, item: Dict, result: Dict
                        ) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Install a validated staged tree (4.3, 4.6-4.8): scaffold validation
    first (nothing changes on defects), then in_place = copy staging over
    the version's tree and delete stale objects + bump the Source_Revision,
    or new_version = create latest+1 from the staged tree. Returns
    (result_extra, failure).
    """
    staging = staging_prefix_for(operation['operation_id'])
    staged = [f['file'] for f in list_source_objects(staging)]
    if not staged:
        return None, {'category': 'not_found', 'message': 'The pulled tree is empty',
                      'log_excerpt': ''}

    defects = scaffold_defects_for_tree(item, staging, staged, {})
    if defects:
        return None, {'category': 'invalid_source',
                      'message': 'The pulled source does not form a buildable Plugin_Scaffold: '
                                 + '; '.join(defects),
                      'defects': defects, 'log_excerpt': ''}

    mode = (operation.get('target') or {}).get('mode') or PULL_IN_PLACE
    plugin_id, version = item['plugin_id'], int(item['version'])
    if mode == PULL_NEW_VERSION:
        started_by = operation.get('started_by') or SYSTEM_USER_ID
        new_item, err = create_version_from_source(
            item, started_by, {}, [],
            provenance_updates={'gitPull': {'commit': result.get('commit'),
                                            'ref': (operation.get('target') or {}).get('ref'),
                                            'by': started_by, 'at': now_ms()}},
            source_prefix=staging, source_paths=staged)
        if err:
            body = json.loads(err['body'])['error']
            return None, {'category': 'invalid_source', 'message': body['message'],
                          'defects': body.get('details', {}).get('defects', []),
                          'log_excerpt': ''}
        affected_version = int(new_item['version'])
    else:
        if item.get('lifecycle_state') != STATE_DEV:
            return None, {'category': 'internal',
                          'message': 'The version left the dev state while the pull ran',
                          'log_excerpt': ''}
        target_prefix = item['source_s3_prefix']
        current = [f['file'] for f in list_source_objects(target_prefix)]
        for path in staged:
            s3.copy_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                           CopySource={'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Key': staging + path},
                           Key=target_prefix + path)
        stale = sorted(set(current) - set(staged))
        if stale:
            for start in range(0, len(stale), 1000):
                s3.delete_objects(Bucket=PORTAL_ARTIFACTS_BUCKET,
                                  Delete={'Objects': [{'Key': target_prefix + p}
                                                      for p in stale[start:start + 1000]],
                                          'Quiet': True})
        bump_source_revision(plugin_id, version)
        affected_version = version
    delete_prefix_objects(staging)
    return {'version': affected_version, 'files': len(staged), 'mode': mode}, None


def record_last_sync(plugin_id: str, version: int, last_sync: Dict) -> None:
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET git.last_sync = :l, updated_at = :t',
        ConditionExpression='attribute_exists(git)',
        ExpressionAttributeValues={':l': last_sync, ':t': now_ms()},
    )


def handle_sync_result(detail: Dict) -> Dict:
    """
    EventBridge CodeBuild Build State Change handler for the git-sync
    project. Idempotent on the build id; settles the operation and applies
    the per-kind side effects (design §5).
    """
    env = {v.get('name'): v.get('value')
           for v in (((detail.get('additional-information') or {}).get('environment') or {})
                     .get('environment-variables') or []) if isinstance(v, dict)}
    operation_id = env.get('OPERATION_ID')
    if not operation_id:
        return {'recorded': False, 'reason': 'missing OPERATION_ID'}
    operation = get_operation(operation_id)
    if not operation:
        logger.warning(f"git-sync result for unknown operation {operation_id}")
        return {'recorded': False, 'reason': 'operation not found'}
    if operation.get('status') in SETTLED_STATUSES:
        return {'recorded': False, 'reason': 'already settled'}
    build_id_arn = detail.get('build-id') or ''
    build_id = build_id_arn.split(':build/', 1)[1] if ':build/' in build_id_arn else build_id_arn
    if operation.get('build_id') and build_id and operation['build_id'] != build_id:
        return {'recorded': False, 'reason': 'superseded build'}

    build_status = detail.get('build-status') or 'UNKNOWN'
    result = read_runner_result(operation_id)
    kind = operation.get('kind')
    plugin_id = operation.get('plugin_id')
    version = int(operation['version']) if operation.get('version') is not None else None
    item = get_version_item(plugin_id, version) if plugin_id and version is not None else None
    started_by = operation.get('started_by') or SYSTEM_USER_ID

    ok = bool(result and result.get('ok')) and build_status == 'SUCCEEDED'
    settled: Dict
    if not ok:
        failure = _failure_from(result, detail, build_status)
        settled = settle_operation(operation, STATUS_FAILED, failure=failure)
        if kind == OP_VERIFY:
            set_connection_status(operation['connection_id'], CONNECTION_FAILED,
                                  {'at': now_ms(), 'category': failure['category'],
                                   'message': failure['message'],
                                   'operation_id': operation_id})
        if kind == OP_PULL:
            delete_prefix_objects(staging_prefix_for(operation_id))
    elif kind == OP_VERIFY:
        settled = settle_operation(operation, STATUS_SUCCEEDED,
                                   result={'default_branch': result.get('default_branch')})
        set_connection_status(operation['connection_id'], CONNECTION_VERIFIED,
                              {'at': now_ms(), 'default_branch_detected': result.get('default_branch'),
                               'operation_id': operation_id})
    elif kind == OP_PUSH:
        target = operation.get('target') or {}
        summary = {'commit': result.get('commit'), 'files': result.get('files'),
                   'no_changes': bool(result.get('no_changes')),
                   'branch_created': bool(result.get('branch_created'))}
        settled = settle_operation(operation, STATUS_SUCCEEDED, result=summary)
        if item is not None:
            try:
                record_last_sync(plugin_id, version, {
                    'kind': OP_PUSH, 'commit': result.get('commit'),
                    'branch': target.get('branch'), 'path': target.get('path'),
                    'source_revision': source_revision_of(item),
                    'by': started_by, 'at': now_ms()})
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                    raise
    else:  # OP_PULL
        if item is None:
            settled = settle_operation(operation, STATUS_FAILED, failure={
                'category': 'internal', 'message': 'Plugin version not found',
                'log_excerpt': ''})
        else:
            extra, failure = install_pulled_tree(operation, item, result)
            if failure:
                delete_prefix_objects(staging_prefix_for(operation_id))
                settled = settle_operation(operation, STATUS_FAILED, failure=failure)
            else:
                target = operation.get('target') or {}
                summary = {'commit': result.get('commit'), 'ref': result.get('ref'),
                           'files': extra['files'], 'version': extra['version'],
                           'mode': extra['mode']}
                settled = settle_operation(operation, STATUS_SUCCEEDED, result=summary)
                last_sync = {'kind': OP_PULL, 'commit': result.get('commit'),
                             'ref': result.get('ref'), 'branch': target.get('branch'),
                             'path': target.get('path'), 'by': started_by,
                             'at': now_ms(), 'version': extra['version']}
                for affected in {version, extra['version']}:
                    try:
                        record_last_sync(plugin_id, int(affected), last_sync)
                    except ClientError as e:
                        if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                            raise

    if plugin_id and version is not None:
        release_sync_lock(plugin_id, version, operation_id)
    log_audit_event(
        user_id=started_by, action='git_sync_operation_settled',
        resource_type='plugin_record' if plugin_id else 'git_connection',
        resource_id=plugin_id or operation.get('connection_id'),
        result='success' if settled['status'] == STATUS_SUCCEEDED else 'failure',
        details={'usecase_id': operation.get('usecase_id'), 'operation_id': operation_id,
                 'kind': kind, 'version': version,
                 'category': (settled.get('failure') or {}).get('category')})
    return {'recorded': True, 'operation_id': operation_id, 'status': settled['status']}


# ------------------------------------------------------------------ routing

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler: API Gateway routes + EventBridge results"""
    if event.get('source') == 'aws.codebuild':
        detail = event.get('detail') or {}
        if detail.get('project-name') != GIT_SYNC_PROJECT_NAME:
            return {'recorded': False, 'reason': 'not the git-sync project'}
        try:
            return handle_sync_result(detail)
        except Exception as e:
            logger.error(f"git-sync result handler error: {str(e)}", exc_info=True)
            raise

    try:
        http_method = event.get('httpMethod')
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}

        if resource == '/git-connections':
            if http_method == 'GET':
                return list_connections(event, user)
            if http_method == 'POST':
                return create_connection(event, user)
        elif resource.startswith('/git-connections/{cid}'):
            connection_id = path_params.get('cid')
            if not connection_id:
                return error_response(404, 'NOT_FOUND', 'Not found')
            if resource == '/git-connections/{cid}':
                if http_method == 'GET':
                    return get_connection_route(event, user, connection_id)
                if http_method == 'PUT':
                    return update_connection(event, user, connection_id)
                if http_method == 'DELETE':
                    return delete_connection(event, user, connection_id)
            elif resource == '/git-connections/{cid}/verify' and http_method == 'POST':
                return verify_connection(event, user, connection_id)
        elif resource == '/git-sync-operations/{opId}' and http_method == 'GET':
            return get_operation_route(event, user, path_params.get('opId') or '')
        elif resource.startswith('/plugins/{id}/versions/{v}/git'):
            plugin_id = path_params.get('id')
            try:
                version = int(path_params.get('v'))
            except (TypeError, ValueError):
                return error_response(400, 'INVALID_VERSION', 'version must be an integer')
            if not plugin_id:
                return error_response(404, 'NOT_FOUND', 'Not found')
            if resource == '/plugins/{id}/versions/{v}/git':
                if http_method == 'PUT':
                    return set_git_link(event, user, plugin_id, version)
                if http_method == 'DELETE':
                    return remove_git_link(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/git/push' and http_method == 'POST':
                return push(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/git/pull' and http_method == 'POST':
                return pull(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/git/operations' and http_method == 'GET':
                return list_operations(event, user, plugin_id, version)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
