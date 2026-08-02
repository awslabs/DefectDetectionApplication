"""
Workflow_Store API Lambda function (Workflow Manager)

CRUD, versioning, duplication, and deletion of Workflow_Definitions,
scoped per account and Use_Case (Requirements 5.1-5.8, 11.4, 11.5).

Routes (API Gateway REST):
    GET    /workflows                    List workflows for authorized Use_Cases (5.3)
    POST   /workflows                    Create a workflow (version 1)          (5.1)
    GET    /workflows/{id}               Open/load a stored definition          (5.4)
    PUT    /workflows/{id}               Save changes as a new version          (5.2)
    DELETE /workflows/{id}               Delete workflow + versions             (5.5, 5.6)
    POST   /workflows/{id}/duplicate     Duplicate under a new name             (5.7)
    GET    /workflows/{id}/versions      List version history                   (5.2)

Storage layout:
    Workflows table         (WORKFLOWS_TABLE)          PK workflow_id, GSI usecase-workflows-index
    WorkflowVersions table  (WORKFLOW_VERSIONS_TABLE)  PK workflow_id, SK version (NUMBER)
    Definitions in portal S3 under
        {WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/versions/{version}/workflow.json

Error envelope (design): {"error": {"code", "message", "details"}} with
400 parse/validation, 403 RBAC denial (5.8, 11.4), 404 scoped to avoid
cross-tenant existence leaks, 409 delete-with-active-deployments (5.6).
"""
import json
import os
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, rbac_manager, Permission
)
from workflow_core.serializer import parse as parse_definition, serialize as serialize_graph

# Custom_Node_Type reference recording at save (custom-node-designer task
# 9.2, Requirement 14.2): the version items carry a `custom_node_types`
# map {typeId: typeVersion} pinning the Custom_Node_Type versions in use.
# Design note: no inverted-index GSI (node-type-refs-index) is created —
# a DynamoDB GSI indexes one scalar attribute value per item, but a
# workflow version may reference several Custom_Node_Types, so a scalar
# ref_node_type_id cannot represent the reference set. The removal scan
# in custom_node_types.py already honors the map attribute in its
# fallback path without loading definition documents from S3.
from node_catalog_resolution import (
    load_registered_node_types,
    referenced_node_type_versions,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')

# Deployment statuses that count as "active" for delete rejection (5.6).
# Follows the convention used by models.py plus in-flight states.
ACTIVE_DEPLOYMENT_STATUSES = {'ACTIVE', 'COMPLETED', 'IN_PROGRESS', 'PENDING', 'QUEUED', 'DEPLOYED'}

# Greengrass component name assigned by the Component_Packager (design section 6)
WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.'


def decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def error_response(status_code: int, code: str, message: str, details: Optional[Dict] = None) -> Dict:
    """Build the Workflow Manager error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def definition_s3_key(usecase_id: str, workflow_id: str, version: int) -> str:
    """S3 key of a stored Workflow_Definition document"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/versions/{version}/workflow.json"


def workflow_s3_prefix(usecase_id: str, workflow_id: str) -> str:
    """S3 prefix holding all documents of a workflow"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/"


def has_workflow_permission(user: Dict, usecase_id: str, permission: Permission) -> bool:
    """Check a workflow permission for the acting user on a Use_Case"""
    return rbac_manager.has_permission(user['user_id'], usecase_id, permission, user_info=user)


def not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a workflow exists (5.8)"""
    return error_response(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')


def forbidden_response(user: Dict, event: Dict, usecase_id: str, permissions: List[Permission]) -> Dict:
    """Uniform 403 authorization error with a denied-access audit entry (11.4)"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='workflow',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [p.value for p in permissions],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [p.value for p in permissions],
        'usecase_id': usecase_id
    })


def authorize_workflow_access(user: Dict, event: Dict, item: Dict,
                              permission: Permission) -> Optional[Dict]:
    """
    Authorize an operation on an existing workflow.

    Returns an error response, or None when authorized.

    Cross-tenant handling (5.8, design error section): a user without even
    read access to the owning Use_Case receives the same 404 as for a
    missing workflow, so existence is never leaked across tenants. A user
    who can read but lacks the operation permission receives a 403.
    """
    usecase_id = item['usecase_id']
    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
        return not_found_response()
    if permission != Permission.WORKFLOW_READ and not has_workflow_permission(user, usecase_id, permission):
        return forbidden_response(user, event, usecase_id, [permission])
    return None


def get_workflow_item(workflow_id: str) -> Optional[Dict]:
    """Fetch a workflow metadata item, or None"""
    table = dynamodb.Table(WORKFLOWS_TABLE)
    response = table.get_item(Key={'workflow_id': workflow_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def canonicalize_definition(definition: Any) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Validate a submitted Workflow_Definition with the Workflow_Serializer
    and return its canonical JSON document.

    Returns (canonical_json, None) on success or (None, error_response).
    """
    if isinstance(definition, str):
        raw = definition
    else:
        try:
            raw = json.dumps(definition)
        except (TypeError, ValueError) as exc:
            return None, error_response(400, 'INVALID_DEFINITION',
                                        f'definition is not JSON-serializable: {exc}')
    result = parse_definition(raw)
    if not result.ok:
        return None, error_response(400, result.error.code, result.error.message,
                                    {'path': result.error.path})
    return serialize_graph(result.graph), None


def put_definition(usecase_id: str, workflow_id: str, version: int, canonical_json: str) -> str:
    """Store a canonical definition document in portal S3; returns the key"""
    key = definition_s3_key(usecase_id, workflow_id, version)
    s3.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=key,
        Body=canonical_json.encode('utf-8'),
        ContentType='application/json'
    )
    return key


def load_definition(s3_key: str) -> Dict:
    """Load a stored Workflow_Definition document from portal S3"""
    response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
    return json.loads(response['Body'].read().decode('utf-8'))


def custom_node_type_references(usecase_id: str, canonical_json: str) -> Dict:
    """
    The Custom_Node_Type versions a definition being saved uses
    (custom-node-designer 14.2): {typeId: typeVersion} recording the
    latest registered version of each referenced custom type at save
    time. Empty when the definition uses only built-in node types or the
    node-designer stack is not deployed.
    """
    definition = json.loads(canonical_json)
    registered = load_registered_node_types(usecase_id)
    if not registered:
        return {}
    return referenced_node_type_versions(definition, registered)


def put_version_item(workflow_id: str, version: int, s3_key: str, user: Dict,
                     custom_node_types: Optional[Dict] = None) -> Dict:
    """Record an immutable workflow version (design: WorkflowVersions table).

    ``custom_node_types`` pins the Custom_Node_Type versions the saved
    definition uses ({typeId: typeVersion}, custom-node-designer 14.2);
    it is always recorded (empty map for built-in-only workflows) so the
    reference scan in custom_node_types.py never needs the S3 document
    for versions saved after task 9.2.
    """
    item = {
        'workflow_id': workflow_id,
        'version': version,
        's3_definition_key': s3_key,
        'validation_status': {'status': 'none'},
        'compiled_arch_keys': {},
        'component_arn': None,
        'custom_node_types': custom_node_types or {},
        'created_by': user['user_id'],
        'created_at': now_ms()
    }
    dynamodb.Table(WORKFLOW_VERSIONS_TABLE).put_item(Item=item)
    return item


def query_workflows_by_usecase(usecase_id: str) -> List[Dict]:
    """All workflow metadata items of one Use_Case via the GSI"""
    table = dynamodb.Table(WORKFLOWS_TABLE)
    items: List[Dict] = []
    kwargs = {
        'IndexName': 'usecase-workflows-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'ExpressionAttributeValues': {':uid': usecase_id}
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return [decimal_to_native(i) for i in items]


def deployment_references_workflow(deployment: Dict, workflow_id: str) -> bool:
    """
    True when a Deployments-table record references the workflow, either via
    the component_type/workflow_id association (design data model) or via a
    packaged Workflow_Component name in its components list.
    """
    if deployment.get('component_type') == 'workflow' and \
            str(deployment.get('workflow_id', '')) == workflow_id:
        return True
    component_name = f"{WORKFLOW_COMPONENT_PREFIX}{workflow_id}"
    for comp in deployment.get('components', []) or []:
        if isinstance(comp, dict) and comp.get('component_name') == component_name:
            return True
        if isinstance(comp, str) and comp == component_name:
            return True
    return False


def find_active_workflow_deployments(usecase_id: str, workflow_id: str) -> List[str]:
    """Deployment ids of active deployments referencing the workflow (5.6)"""
    deployment_ids: List[str] = []
    try:
        table = dynamodb.Table(DEPLOYMENTS_TABLE)
        kwargs = {
            'IndexName': 'usecase-deployments-index',
            'KeyConditionExpression': 'usecase_id = :uid',
            'ExpressionAttributeValues': {':uid': usecase_id}
        }
        while True:
            response = table.query(**kwargs)
            for deployment in response.get('Items', []):
                status = str(deployment.get('deployment_status', '')).upper()
                if status not in ACTIVE_DEPLOYMENT_STATUSES:
                    continue
                if deployment_references_workflow(deployment, workflow_id):
                    dep_id = deployment.get('deployment_id') or deployment.get('deploymentId')
                    if dep_id:
                        deployment_ids.append(str(dep_id))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key
    except ClientError as e:
        # Fail closed: if the deployment check cannot run we must not delete
        logger.error(f"Error checking deployments for workflow {workflow_id}: {str(e)}")
        raise
    return sorted(set(deployment_ids))


def workflow_summary(item: Dict) -> Dict:
    """Public shape of a workflow metadata item"""
    return {
        'workflow_id': item['workflow_id'],
        'usecase_id': item['usecase_id'],
        'account_id': item.get('account_id'),
        'name': item.get('name'),
        'description': item.get('description', ''),
        'created_at': item.get('created_at'),
        'updated_at': item.get('updated_at'),
        'latest_version': item.get('latest_version'),
        'created_by': item.get('created_by')
    }


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def create_workflow(event: Dict, user: Dict) -> Dict:
    """
    POST /workflows
    Body: {usecase_id, name, definition, description?}
    Persists the definition scoped to account + Use_Case as version 1 (5.1).
    """
    body, err = parse_body(event)
    if err:
        return err

    usecase_id = body.get('usecase_id')
    name = body.get('name')
    definition = body.get('definition')

    missing = [f for f in ('usecase_id', 'name', 'definition') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")

    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_CREATE):
        return forbidden_response(user, event, usecase_id, [Permission.WORKFLOW_CREATE])

    # The Use_Case must exist; its account scopes the workflow (5.1)
    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    canonical_json, err = canonicalize_definition(definition)
    if err:
        return err

    workflow_id = str(uuid.uuid4())
    timestamp = now_ms()

    s3_key = put_definition(usecase_id, workflow_id, 1, canonical_json)
    put_version_item(workflow_id, 1, s3_key, user,
                     custom_node_type_references(usecase_id, canonical_json))

    item = {
        'workflow_id': workflow_id,
        'usecase_id': usecase_id,
        'account_id': usecase.get('account_id'),
        'name': name,
        'description': body.get('description', ''),
        'created_at': timestamp,
        'updated_at': timestamp,
        'latest_version': 1,
        'created_by': user['user_id']
    }
    dynamodb.Table(WORKFLOWS_TABLE).put_item(
        Item=item,
        ConditionExpression='attribute_not_exists(workflow_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='create_workflow',
        resource_type='workflow',
        resource_id=workflow_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': name, 'version': 1}
    )

    return create_response(201, {'workflow': workflow_summary(item), 'version': 1})


def list_workflows(event: Dict, user: Dict) -> Dict:
    """
    GET /workflows[?usecase_id=...]
    Returns workflows belonging to Use_Cases the user is authorized to
    access (5.3). With usecase_id the list is scoped to that Use_Case.
    """
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')

    if usecase_id:
        if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
            return forbidden_response(user, event, usecase_id, [Permission.WORKFLOW_READ])
        usecase_ids = [usecase_id]
    else:
        usecase_ids = [
            uc for uc in rbac_manager.get_accessible_usecases(user['user_id'], user_info=user)
            if has_workflow_permission(user, uc, Permission.WORKFLOW_READ)
        ]

    workflows: List[Dict] = []
    for uc in usecase_ids:
        workflows.extend(workflow_summary(i) for i in query_workflows_by_usecase(uc))

    workflows.sort(key=lambda w: w.get('updated_at') or 0, reverse=True)
    return create_response(200, {'workflows': workflows, 'count': len(workflows)})


def get_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    GET /workflows/{id}[?version=N]
    Open/load: returns metadata plus the stored Workflow_Definition of the
    requested (default latest) version exactly as saved (5.4).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_READ)
    if err:
        return err

    params = event.get('queryStringParameters') or {}
    version_param = params.get('version')
    if version_param is not None:
        try:
            version = int(version_param)
        except ValueError:
            return error_response(400, 'INVALID_VERSION', 'version must be an integer')
    else:
        version = int(item.get('latest_version', 1))

    versions_table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = versions_table.get_item(Key={'workflow_id': workflow_id, 'version': version})
    version_item = response.get('Item')
    if not version_item:
        return error_response(404, 'VERSION_NOT_FOUND',
                              f'Version {version} not found for workflow')
    version_item = decimal_to_native(version_item)

    try:
        definition = load_definition(version_item['s3_definition_key'])
    except ClientError as e:
        logger.error(f"Error loading definition for {workflow_id} v{version}: {str(e)}")
        return error_response(500, 'DEFINITION_LOAD_FAILED',
                              'Stored workflow definition could not be loaded')

    return create_response(200, {
        'workflow': workflow_summary(item),
        'version': version,
        'validation_status': version_item.get('validation_status'),
        'definition': definition
    })


def update_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    PUT /workflows/{id}
    Body: {definition, name?, description?}
    Saves changes as a new workflow version; prior versions are never
    modified or removed (5.2).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_SAVE)
    if err:
        return err

    body, err = parse_body(event)
    if err:
        return err
    definition = body.get('definition')
    if not definition:
        return error_response(400, 'MISSING_FIELDS', 'Missing required fields: definition')

    canonical_json, err = canonicalize_definition(definition)
    if err:
        return err

    usecase_id = item['usecase_id']
    timestamp = now_ms()

    # Atomically allocate the next version number and refresh metadata
    update_expr = 'SET latest_version = latest_version + :one, updated_at = :updated'
    expr_values: Dict[str, Any] = {':one': 1, ':updated': timestamp}
    expr_names: Dict[str, str] = {}
    if 'name' in body and body['name']:
        update_expr += ', #name = :name'
        expr_names['#name'] = 'name'
        expr_values[':name'] = body['name']
    if 'description' in body:
        update_expr += ', description = :description'
        expr_values[':description'] = body.get('description', '')

    kwargs = {
        'Key': {'workflow_id': workflow_id},
        'UpdateExpression': update_expr,
        'ExpressionAttributeValues': expr_values,
        'ConditionExpression': 'attribute_exists(workflow_id)',
        'ReturnValues': 'ALL_NEW'
    }
    if expr_names:
        kwargs['ExpressionAttributeNames'] = expr_names
    updated = dynamodb.Table(WORKFLOWS_TABLE).update_item(**kwargs)
    new_item = decimal_to_native(updated['Attributes'])
    new_version = int(new_item['latest_version'])

    s3_key = put_definition(usecase_id, workflow_id, new_version, canonical_json)
    put_version_item(workflow_id, new_version, s3_key, user,
                     custom_node_type_references(usecase_id, canonical_json))

    log_audit_event(
        user_id=user['user_id'],
        action='update_workflow',
        resource_type='workflow',
        resource_id=workflow_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': new_item.get('name'),
                 'version': new_version}
    )

    return create_response(200, {'workflow': workflow_summary(new_item), 'version': new_version})


def delete_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    DELETE /workflows/{id}
    Removes the workflow and all its versions when no active deployment
    references it (5.5); otherwise rejects with 409 and the referencing
    deployment ids (5.6).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_DELETE)
    if err:
        return err

    usecase_id = item['usecase_id']

    deployment_ids = find_active_workflow_deployments(usecase_id, workflow_id)
    if deployment_ids:
        return error_response(
            409, 'WORKFLOW_HAS_ACTIVE_DEPLOYMENTS',
            'Workflow cannot be deleted while active deployments reference it',
            {'deployment_ids': deployment_ids}
        )

    # Delete version records
    versions_table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    version_keys: List[Dict] = []
    kwargs = {
        'KeyConditionExpression': 'workflow_id = :wid',
        'ExpressionAttributeValues': {':wid': workflow_id},
        # 'version' is a DynamoDB reserved word
        'ProjectionExpression': 'workflow_id, #v',
        'ExpressionAttributeNames': {'#v': 'version'}
    }
    while True:
        response = versions_table.query(**kwargs)
        version_keys.extend(
            {'workflow_id': v['workflow_id'], 'version': v['version']}
            for v in response.get('Items', [])
        )
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    with versions_table.batch_writer() as batch:
        for key in version_keys:
            batch.delete_item(Key=key)

    # Delete stored documents in S3
    prefix = workflow_s3_prefix(usecase_id, workflow_id)
    continuation_token = None
    while True:
        list_kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': prefix}
        if continuation_token:
            list_kwargs['ContinuationToken'] = continuation_token
        listed = s3.list_objects_v2(**list_kwargs)
        objects = [{'Key': o['Key']} for o in listed.get('Contents', [])]
        if objects:
            s3.delete_objects(Bucket=PORTAL_ARTIFACTS_BUCKET, Delete={'Objects': objects})
        if not listed.get('IsTruncated'):
            break
        continuation_token = listed.get('NextContinuationToken')

    # Delete workflow metadata
    dynamodb.Table(WORKFLOWS_TABLE).delete_item(Key={'workflow_id': workflow_id})

    log_audit_event(
        user_id=user['user_id'],
        action='delete_workflow',
        resource_type='workflow',
        resource_id=workflow_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': item.get('name'),
                 'versions_deleted': len(version_keys)}
    )

    return create_response(200, {
        'workflow_id': workflow_id,
        'message': 'Workflow deleted successfully'
    })


def duplicate_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    POST /workflows/{id}/duplicate
    Body: {name?, description?}
    Creates a new workflow with a copy of the source's latest
    Workflow_Definition under a new name (5.7).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_CREATE)
    if err:
        return err

    body, err = parse_body(event)
    if err:
        return err
    new_name = body.get('name') or f"{item.get('name', 'Workflow')} (copy)"

    usecase_id = item['usecase_id']
    latest_version = int(item.get('latest_version', 1))

    versions_table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = versions_table.get_item(
        Key={'workflow_id': workflow_id, 'version': latest_version}
    )
    source_version = response.get('Item')
    if not source_version:
        return error_response(404, 'VERSION_NOT_FOUND',
                              f'Version {latest_version} not found for workflow')

    try:
        definition = load_definition(source_version['s3_definition_key'])
    except ClientError as e:
        logger.error(f"Error loading definition for {workflow_id} v{latest_version}: {str(e)}")
        return error_response(500, 'DEFINITION_LOAD_FAILED',
                              'Stored workflow definition could not be loaded')

    canonical_json, err = canonicalize_definition(definition)
    if err:
        return err

    new_workflow_id = str(uuid.uuid4())
    timestamp = now_ms()

    s3_key = put_definition(usecase_id, new_workflow_id, 1, canonical_json)
    put_version_item(new_workflow_id, 1, s3_key, user,
                     custom_node_type_references(usecase_id, canonical_json))

    new_item = {
        'workflow_id': new_workflow_id,
        'usecase_id': usecase_id,
        'account_id': item.get('account_id'),
        'name': new_name,
        'description': body.get('description', item.get('description', '')),
        'created_at': timestamp,
        'updated_at': timestamp,
        'latest_version': 1,
        'created_by': user['user_id']
    }
    dynamodb.Table(WORKFLOWS_TABLE).put_item(
        Item=new_item,
        ConditionExpression='attribute_not_exists(workflow_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='duplicate_workflow',
        resource_type='workflow',
        resource_id=new_workflow_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': new_name,
                 'source_workflow_id': workflow_id, 'source_version': latest_version}
    )

    return create_response(201, {'workflow': workflow_summary(new_item), 'version': 1})


def list_versions(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    GET /workflows/{id}/versions
    Version history, newest first; prior versions are retained (5.2).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_READ)
    if err:
        return err

    versions_table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    versions: List[Dict] = []
    kwargs = {
        'KeyConditionExpression': 'workflow_id = :wid',
        'ExpressionAttributeValues': {':wid': workflow_id},
        'ScanIndexForward': False
    }
    while True:
        response = versions_table.query(**kwargs)
        versions.extend(decimal_to_native(v) for v in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key

    return create_response(200, {
        'workflow_id': workflow_id,
        'latest_version': item.get('latest_version'),
        'versions': [
            {
                'version': v['version'],
                'created_at': v.get('created_at'),
                'created_by': v.get('created_by'),
                'validation_status': v.get('validation_status'),
                'component_arn': v.get('component_arn')
            }
            for v in versions
        ],
        'count': len(versions)
    })


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
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}
        workflow_id = path_params.get('id')

        if resource == '/workflows':
            if http_method == 'GET':
                return list_workflows(event, user)
            if http_method == 'POST':
                return create_workflow(event, user)
        elif resource == '/workflows/{id}' and workflow_id:
            if http_method == 'GET':
                return get_workflow(event, user, workflow_id)
            if http_method == 'PUT':
                return update_workflow(event, user, workflow_id)
            if http_method == 'DELETE':
                return delete_workflow(event, user, workflow_id)
        elif resource == '/workflows/{id}/duplicate' and workflow_id:
            if http_method == 'POST':
                return duplicate_workflow(event, user, workflow_id)
        elif resource == '/workflows/{id}/versions' and workflow_id:
            if http_method == 'GET':
                return list_versions(event, user, workflow_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
