"""
Workflow Validation API Lambda function (Workflow Manager)

Backend Workflow_Validator endpoint plus the node catalog endpoint for
the frontend Node_Palette (Requirements 2.8, 4.6, 4.7, 4.10).

Routes (API Gateway REST):
    POST /workflows/{id}/validate    Run all Workflow_Validator checks on a
                                     stored workflow version, return the
                                     complete findings list, and record the
                                     validation status (passed/failed,
                                     findings key, validated_at) on the
                                     version item (4.6)
    GET  /workflows/node-catalog     Serialized node type catalog in the
                                     camelCase wire form the frontend
                                     palette consumes (2.8)

The shared guard used by packaging/publishing/deployment endpoints to
reject versions with error-severity findings or without a recorded
passed-validation run (4.7, 4.10) lives in workflow_guards.py, which
does not import the workflow_core layer so deployments.py can use it.

Storage layout (shared with workflows.py):
    WorkflowVersions table  (WORKFLOW_VERSIONS_TABLE)  PK workflow_id, SK version
    Findings documents in portal S3 under
        {WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/versions/{version}/findings.json

Error envelope (design): {"error": {"code", "message", "details"}} with
403 RBAC denial (11.4) and 404 scoped to avoid cross-tenant existence
leaks (5.8).
"""
import json
import os
import logging
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
    rbac_manager, Permission
)
from workflow_core.catalog import (
    NODE_CATALOG, GstMapping, NodeTypeDescriptor,
    ParameterDescriptor, PortDescriptor
)
from workflow_core.serializer import parse as parse_definition
from workflow_core.validator import validate as run_validator, SEVERITY_ERROR, SEVERITY_WARNING

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')

# Validation status values recorded on WorkflowVersions items (design data model)
VALIDATION_STATUS_PASSED = 'passed'
VALIDATION_STATUS_FAILED = 'failed'


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


def findings_s3_key(usecase_id: str, workflow_id: str, version: int) -> str:
    """S3 key of the stored findings document of one validation run"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/versions/{version}/findings.json"


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


def get_version_item(workflow_id: str, version: int) -> Optional[Dict]:
    """Fetch a WorkflowVersions item, or None"""
    table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = table.get_item(Key={'workflow_id': workflow_id, 'version': version})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def load_definition(s3_key: str) -> Dict:
    """Load a stored Workflow_Definition document from portal S3"""
    response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
    return json.loads(response['Body'].read().decode('utf-8'))


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


# --------------------------------------------------------------------------
# Node catalog serialization (camelCase wire form, Requirement 2.8)
# Mirrors edge-cv-portal/frontend/src/pages/workflows/types.ts.
# --------------------------------------------------------------------------

# Python constraint keys -> wire keys (min/max/regex/values pass through)
_CONSTRAINT_KEY_MAP = {
    'min_length': 'minLength',
    'max_length': 'maxLength',
}


def constraints_to_wire(constraints: Dict) -> Dict:
    """ParameterDescriptor.constraints in wire form"""
    return {_CONSTRAINT_KEY_MAP.get(k, k): v for k, v in (constraints or {}).items()}


def port_to_wire(port: PortDescriptor) -> Dict:
    return {'name': port.name, 'portType': port.port_type}


def parameter_to_wire(parameter: ParameterDescriptor) -> Dict:
    return {
        'name': parameter.name,
        'paramType': parameter.param_type,
        'required': parameter.required,
        'default': parameter.default,
        'constraints': constraints_to_wire(parameter.constraints),
        'dependsOn': parameter.depends_on,
        'description': parameter.description,
        'examples': list(parameter.examples) if parameter.examples is not None else None
    }


def mapping_to_wire(mapping: GstMapping) -> Dict:
    return {
        'arch': mapping.arch,
        'elementChain': [
            {'factory': e['factory'], 'argsTemplate': e.get('args_template', {})}
            for e in mapping.element_chain
        ],
        'executorBinding': mapping.executor_binding,
        'pluginDependencies': list(mapping.plugin_dependencies)
    }


def descriptor_to_wire(descriptor: NodeTypeDescriptor) -> Dict:
    """One NodeTypeDescriptor in the camelCase wire form (Requirement 2.8)"""
    return {
        'typeId': descriptor.type_id,
        'category': descriptor.category,
        'displayName': descriptor.display_name,
        'inputs': [port_to_wire(p) for p in descriptor.inputs],
        'outputs': [port_to_wire(p) for p in descriptor.outputs],
        'parameters': [parameter_to_wire(p) for p in descriptor.parameters],
        'mappings': [mapping_to_wire(m) for m in descriptor.mappings],
        'hardwareDependent': descriptor.hardware_dependent
    }


def get_node_catalog(event: Dict, user: Dict) -> Dict:
    """
    GET /workflows/node-catalog
    Serves the full node type catalog for the frontend Node_Palette:
    every node type's ports, port types, parameters with types, defaults,
    and constraints, per-arch mappings, and hardware-dependence flag
    (Requirement 2.8). The catalog is global, static data with no
    tenant-scoped content, so any authenticated user may read it.
    """
    return create_response(200, {
        'nodeTypes': [descriptor_to_wire(d) for d in NODE_CATALOG],
        'count': len(NODE_CATALOG)
    })


# --------------------------------------------------------------------------
# Validate endpoint (Requirements 4.6, 4.10)
# --------------------------------------------------------------------------

def validate_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    POST /workflows/{id}/validate
    Body: {version?}  (defaults to the latest version)

    Runs all Workflow_Validator checks on the stored Workflow_Definition
    of the requested version, returns the complete findings list (4.6),
    stores the findings document in portal S3, and records the validation
    status (passed/failed, findings key, validated_at) on the
    WorkflowVersions item so packaging/publishing/deployment can verify a
    passed run (4.10).
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_EDIT)
    if err:
        return err

    body, err = parse_body(event)
    if err:
        return err

    version_param = body.get('version')
    if version_param is not None:
        try:
            version = int(version_param)
        except (TypeError, ValueError):
            return error_response(400, 'INVALID_VERSION', 'version must be an integer')
    else:
        version = int(item.get('latest_version', 1))

    version_item = get_version_item(workflow_id, version)
    if not version_item:
        return error_response(404, 'VERSION_NOT_FOUND',
                              f'Version {version} not found for workflow')

    try:
        definition = load_definition(version_item['s3_definition_key'])
    except ClientError as e:
        logger.error(f"Error loading definition for {workflow_id} v{version}: {str(e)}")
        return error_response(500, 'DEFINITION_LOAD_FAILED',
                              'Stored workflow definition could not be loaded')

    # Stored definitions were canonicalized through the serializer on save,
    # so a parse failure here indicates a corrupted document.
    result = parse_definition(json.dumps(definition))
    if not result.ok:
        logger.error(
            f"Stored definition for {workflow_id} v{version} failed to parse: "
            f"{result.error.code}: {result.error.message}"
        )
        return error_response(500, 'STORED_DEFINITION_INVALID',
                              'Stored workflow definition could not be parsed',
                              {'code': result.error.code, 'path': result.error.path})

    # All checks always run; the complete findings list is returned (4.6)
    findings = run_validator(result.graph)
    wire_findings = [f.to_dict() for f in findings]
    error_count = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
    warning_count = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    passed = error_count == 0

    usecase_id = item['usecase_id']
    validated_at = now_ms()
    findings_key = findings_s3_key(usecase_id, workflow_id, version)

    # Store the findings document, then record the validation status on the
    # version item (design: validation_status {passed/failed, findings_key,
    # validated_at}); the packaging/deployment guard reads both (4.7, 4.10).
    s3.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=findings_key,
        Body=json.dumps({
            'workflow_id': workflow_id,
            'version': version,
            'validated_at': validated_at,
            'passed': passed,
            'findings': wire_findings
        }).encode('utf-8'),
        ContentType='application/json'
    )

    validation_status = {
        'status': VALIDATION_STATUS_PASSED if passed else VALIDATION_STATUS_FAILED,
        'findings_key': findings_key,
        'validated_at': validated_at
    }
    dynamodb.Table(WORKFLOW_VERSIONS_TABLE).update_item(
        Key={'workflow_id': workflow_id, 'version': version},
        UpdateExpression='SET validation_status = :vs',
        ExpressionAttributeValues={':vs': validation_status},
        ConditionExpression='attribute_exists(workflow_id)'
    )

    return create_response(200, {
        'workflow_id': workflow_id,
        'version': version,
        'passed': passed,
        'validation_status': validation_status,
        'findings': wire_findings,
        'error_count': error_count,
        'warning_count': warning_count
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

        if resource == '/workflows/node-catalog':
            if http_method == 'GET':
                return get_node_catalog(event, user)
        elif resource == '/workflows/{id}/validate' and workflow_id:
            if http_method == 'POST':
                return validate_workflow(event, user, workflow_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
