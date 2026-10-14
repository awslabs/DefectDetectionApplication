"""
Shared workflow validation guard (Workflow Manager)

Guard helper used by the packaging, publishing, and deployment endpoints
(workflow_packaging.py, deployments.py): a workflow version may only be
packaged, published, or deployed when it has a recorded passed-validation
run with zero error-severity findings (Requirements 4.7, 4.10).

This module deliberately does NOT import the workflow_core layer: it only
reads the recorded validation status from the WorkflowVersions table and
the stored findings document from portal S3, so it is importable by
Lambdas that do not attach the workflow_core layer (e.g. deployments.py).

Usage:
    import workflow_guards

    failure = workflow_guards.check_workflow_version_validated(workflow_id, version)
    if failure:
        return error_response(failure['status_code'], failure['code'],
                              failure['message'], failure['details'])
"""
import json
import os
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (module-level, mocked by moto in tests)
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables (shared with workflows.py / workflow_validation.py)
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')

# Wire-form severity written by workflow_core.validator ValidationFinding.to_dict()
SEVERITY_ERROR = 'error'

# Validation status values recorded on WorkflowVersions items
VALIDATION_STATUS_PASSED = 'passed'
VALIDATION_STATUS_FAILED = 'failed'
VALIDATION_STATUS_NONE = 'none'

# Guard failure codes (error envelope "code" values)
GUARD_CODE_VERSION_NOT_FOUND = 'WORKFLOW_VERSION_NOT_FOUND'
GUARD_CODE_NOT_VALIDATED = 'WORKFLOW_VERSION_NOT_VALIDATED'
GUARD_CODE_VALIDATION_ERRORS = 'WORKFLOW_VALIDATION_ERRORS'


def _decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def _guard_failure(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """Shape a guard rejection for the caller's error envelope"""
    return {
        'status_code': status_code,
        'code': code,
        'message': message,
        'details': details or {}
    }


def get_version_item(workflow_id: str, version: int) -> Optional[Dict]:
    """Fetch a WorkflowVersions item, or None"""
    table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = table.get_item(Key={'workflow_id': workflow_id, 'version': int(version)})
    item = response.get('Item')
    return _decimal_to_native(item) if item else None


def find_version_item_by_component_version(workflow_id: str,
                                           component_version: str) -> Optional[Dict]:
    """
    Find the WorkflowVersions item whose registered component version
    equals `component_version`, or None.

    Scans the workflow's version items (paged Query on the partition;
    version counts per workflow are small) and matches on the discrete
    `component_version` field when present, else on the `component_arn`
    suffix after the last ':versions:'.

    The match is unambiguous by construction: component majors strictly
    increase per workflow_packaging.next_component_version (first package
    of version N is N.0.0, each re-package takes the next free major), so
    at most one version item of a workflow can record a given component
    version.

    Table-read errors are logged and return None so callers can fall back
    (e.g. to the legacy major-parse resolution).
    """
    if not component_version:
        return None
    try:
        table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
        query_kwargs = {
            'KeyConditionExpression': Key('workflow_id').eq(workflow_id)
        }
        while True:
            response = table.query(**query_kwargs)
            for item in response.get('Items', []):
                recorded = item.get('component_version')
                if not recorded:
                    arn = item.get('component_arn')
                    if isinstance(arn, str) and ':versions:' in arn:
                        recorded = arn.rsplit(':versions:', 1)[-1]
                if recorded == component_version:
                    return _decimal_to_native(item)
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return None
            query_kwargs['ExclusiveStartKey'] = last_key
    except ClientError as e:
        logger.error(
            f"Error querying version items for workflow {workflow_id} "
            f"by component version {component_version}: {str(e)}"
        )
        return None


def load_recorded_findings(validation_status: Dict) -> List[Dict]:
    """
    Load the findings recorded by the last validation run: inline under
    'findings', or from portal S3 under 'findings_key'. Returns [] when
    no findings are recorded or the document cannot be loaded (the
    passed-status check in the guard still applies in that case).
    """
    if not isinstance(validation_status, dict):
        return []

    inline = validation_status.get('findings')
    if isinstance(inline, list):
        return inline

    findings_key = validation_status.get('findings_key')
    if not findings_key:
        return []
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=findings_key)
        document = json.loads(response['Body'].read().decode('utf-8'))
    except (ClientError, ValueError) as e:
        logger.error(f"Error loading findings document {findings_key}: {str(e)}")
        return []
    findings = document.get('findings') if isinstance(document, dict) else document
    return findings if isinstance(findings, list) else []


def error_findings(findings: List[Dict]) -> List[Dict]:
    """The error-severity subset of a findings list"""
    return [f for f in findings if isinstance(f, dict) and f.get('severity') == SEVERITY_ERROR]


def check_workflow_version_validated(workflow_id: str, version: int) -> Optional[Dict]:
    """
    Guard for packaging/publishing/deployment of a workflow version.

    Returns None when the version passed a Workflow_Validator run with
    zero validation errors (Requirement 4.10). Otherwise returns a guard
    failure dict {status_code, code, message, details}:

      - the recorded findings contain error-severity findings: rejected
        with the validation errors so the caller can display them
        (Requirement 4.7)
      - the version has no recorded passed-validation run: rejected
        (Requirement 4.10)
      - the version does not exist: rejected with 404
    """
    version_item = get_version_item(workflow_id, version)
    if not version_item:
        return _guard_failure(
            404, GUARD_CODE_VERSION_NOT_FOUND,
            f'Version {version} not found for workflow',
            {'workflow_id': workflow_id, 'version': version}
        )

    validation_status = version_item.get('validation_status') or {}
    status = validation_status.get('status', VALIDATION_STATUS_NONE)

    errors = error_findings(load_recorded_findings(validation_status))
    if errors:
        return _guard_failure(
            409, GUARD_CODE_VALIDATION_ERRORS,
            'Workflow version has validation errors and cannot be '
            'packaged, published, or deployed',
            {
                'workflow_id': workflow_id,
                'version': version,
                'validation_status': status,
                'validated_at': validation_status.get('validated_at'),
                'errors': errors
            }
        )

    if status != VALIDATION_STATUS_PASSED:
        return _guard_failure(
            409, GUARD_CODE_NOT_VALIDATED,
            'Workflow version has not passed validation; run validation '
            'with zero errors before packaging, publishing, or deploying',
            {
                'workflow_id': workflow_id,
                'version': version,
                'validation_status': status
            }
        )

    return None
