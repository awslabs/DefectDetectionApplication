"""
Plugin_Record API Lambda function (Custom Node Designer)

CRUD, versioning, lifecycle transitions, and security review of
Plugin_Records over the PluginRecords DynamoDB table
(Requirements 9.1, 9.3, 9.4, 9.5, 9.9, 9.10, 9.12, 9.13,
10.1, 10.2, 10.3, 10.5, 15.6).

Routes (API Gateway REST):
    GET    /plugins                                    List Plugin_Records (optionally
                                                       ?usecase_id=... and ?review=pending
                                                       for the PortalAdmin review queue)
    POST   /plugins                                    Create a Plugin_Record (version 1,
                                                       lifecycle dev, review pending)  (9.1, 10.1)
    GET    /plugins/{id}                               Latest version + version history
    PUT    /plugins/{id}                               Update metadata, or create a new
                                                       version (dev + pending)         (9.13, 10.5)
    DELETE /plugins/{id}                               Delete every version of the record
                                                       (bad/duplicate imports) plus
                                                       best-effort cleanup of its S3
                                                       source snapshot and promoted
                                                       Plugin_Library artifacts; 409
                                                       RECORD_IN_USE when any version
                                                       was promoted beyond dev
    GET    /plugins/{id}/versions/{v}                  Version detail: full provenance
                                                       incl. classification, per-arch
                                                       checksums/signatures            (10.2, 15.6)
    GET    /plugins/{id}/versions/{v}/source           Source inspection: file listing
                                                       or single-file content          (10.2)
    GET    /plugins/{id}/versions/{v}/gst-properties   Stored Introspection_Report with
                                                       derived per-element
                                                       Parameter_Suggestions, or a
                                                       machine-readable unavailability
                                                       reason (gst-parameter-
                                                       prepopulation 1.5, 1.6, 7.4, 8.3)
    PUT    /plugins/{id}/versions/{v}/source           Persist submitted scaffold
                                                       source (original or edited)
                                                       ahead of a build               (1.5, 1.6)
    POST   /plugins/{id}/versions/{v}/promote          dev->test (build guard, 9.4/9.5),
                                                       test->prod (review guard, 9.9/9.10)
    POST   /plugins/{id}/versions/{v}/demote           prod->test / test->dev, always
                                                       succeeds; gates apply only to
                                                       subsequent packaging/deployment
                                                       requests                        (9.12)
    POST   /plugins/{id}/versions/{v}/review           Approve/reject security review
                                                       (PortalAdmin only), recorded in
                                                       the existing AuditLog table     (10.3)

Storage layout (design "Data Models"):
    PluginRecords table (PLUGIN_RECORDS_TABLE)
        PK plugin_id (S), SK version (N), GSI usecase-plugins-index
        Attributes: usecase_id, name, kind (scaffold/generated/imported),
        deepstream flag, provenance {repoUrl, revision, prompt,
        scaffoldDeclaration, importedBy/createdBy, timestamps,
        classification, prebuilt}, lifecycle_state (dev/test/prod),
        review {decision, reviewer, reviewedAt}, artifacts {arch:
        {s3Key, checksum, signature, buildStatus, logTail}}, component
        pointer, source_s3_prefix.
    Plugin sources in portal S3 under
        plugin-sources/{usecase_id}/{plugin_id}/{version}/

Error envelope: {"error": {"code", "message", "details"}} with 400
parse/validation, 403 RBAC denial, 404 scoped to avoid cross-tenant
existence leaks, and 409 lifecycle-guard rejections identifying the
missing build (9.5) or missing security review approval (9.10).

Access control (design Requirement 13 table) via the node-designer
RBAC actions registered in shared_utils.Permission and mapped in
rbac_middleware.CommonPermissions: node-designer:read for every role
in the Use_Case (13.3); node-designer:create/promote-demote/manage for
UseCaseAdmin (own Use_Case) and PortalAdmin (13.1);
node-designer:security-review for PortalAdmin only (13.2). Denials
return the standard authorization error envelope (13.4).
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
# Server-side Plugin_Scaffold rendering/validation (workflow_core layer,
# design "Plugin_Scaffold and Node_Generator": scaffold generation is pure
# templating in workflow_core.scaffold, stored under plugin-sources/...).
from workflow_core.scaffold import (
    ScaffoldError,
    render_scaffold,
    scaffold_defects,
)
# Introspection_Report parsing and Parameter_Suggestion derivation
# (gst-parameter-prepopulation design component 4): pure module shipped
# alongside this handler in the functions asset.
from gst_properties import (
    ReportError,
    STATUS_CAPTURED,
    parse_report,
    ports_for_element,
    suggestions_for_element,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
PLUGIN_RECORDS_TABLE = os.environ.get('PLUGIN_RECORDS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
PLUGIN_SOURCES_S3_PREFIX = os.environ.get('PLUGIN_SOURCES_S3_PREFIX', 'plugin-sources')

USECASE_PLUGINS_INDEX = 'usecase-plugins-index'

# ---------------------------------------------------------------- constants

# Lifecycle_State machine (design "The Plugin_Record pipeline")
STATE_DEV = 'dev'
STATE_TEST = 'test'
STATE_PROD = 'prod'
LIFECYCLE_STATES = (STATE_DEV, STATE_TEST, STATE_PROD)

# Legal transitions: promotion moves forward one step, demotion back one.
PROMOTIONS = {STATE_DEV: STATE_TEST, STATE_TEST: STATE_PROD}
DEMOTIONS = {STATE_PROD: STATE_TEST, STATE_TEST: STATE_DEV}

# Security review decisions (Requirement 10)
REVIEW_PENDING = 'pending'
REVIEW_APPROVED = 'approved'
REVIEW_REJECTED = 'rejected'
REVIEW_DECISIONS = (REVIEW_APPROVED, REVIEW_REJECTED)

# Plugin_Record origin kinds (design data model)
RECORD_KINDS = ('scaffold', 'generated', 'imported')

BUILD_SUCCEEDED = 'succeeded'

# Maximum size of a source file returned inline for inspection (10.2)
MAX_SOURCE_FILE_BYTES = 512 * 1024

# Property_Introspection runs on x86_64 only (gst-parameter-prepopulation
# design: GObject property declarations are architecture-independent and
# the x86_64 build is the designer's gating artifact everywhere else).
INTROSPECTION_ARCH = 'x86_64'

# Machine-readable unavailability reasons of the gst-properties route
# (gst-parameter-prepopulation Requirements 1.6, 7.4, 8.3).
GST_REASON_NO_BUILD = 'no_x86_64_build'
GST_REASON_NOT_CAPTURED = 'not_captured'
GST_REASON_FAILED = 'introspection_failed'


# ------------------------------------------------------------------ helpers

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
    """Build the error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def source_s3_prefix(usecase_id: str, plugin_id: str, version: int) -> str:
    """S3 prefix holding the plugin source tree of one version"""
    return f"{PLUGIN_SOURCES_S3_PREFIX}/{usecase_id}/{plugin_id}/{version}/"


def not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a Plugin_Record exists"""
    return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')


def has_node_designer_permission(user: Dict, usecase_id: str,
                                 permission: Permission) -> bool:
    """Check a registered node-designer RBAC action for the acting user"""
    return rbac_manager.has_permission(user['user_id'], usecase_id,
                                       permission, user_info=user)


def is_portal_admin(user: Dict) -> bool:
    """node-designer:security-review holders only: PortalAdmin, resolved
    globally and independent of Use_Case (13.2)"""
    return has_node_designer_permission(
        user, 'global', Permission.NODE_DESIGNER_SECURITY_REVIEW)


def can_manage(user: Dict, usecase_id: str,
               permission: Permission = Permission.NODE_DESIGNER_MANAGE) -> bool:
    """Registered manage-family action: UseCaseAdmin within the Use_Case,
    or PortalAdmin (13.1)"""
    return has_node_designer_permission(user, usecase_id, permission)


def forbidden_response(user: Dict, event: Dict, usecase_id: str,
                       required: Permission) -> Dict:
    """Standard authorization error envelope with a denied-access audit
    entry (13.4), matching the workflow feature area's FORBIDDEN shape"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='plugin_record',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [required.value],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [required.value],
        'usecase_id': usecase_id
    })


# ------------------------------------------------------------- persistence

def plugin_table():
    return dynamodb.Table(PLUGIN_RECORDS_TABLE)


def get_version_item(plugin_id: str, version: int,
                     consistent_read: bool = False) -> Optional[Dict]:
    """Fetch one Plugin_Record version item, or None.

    consistent_read exists for same-invocation read-your-own-write
    callers (auto-start after an adjustment/fetch-settle write), which
    must see the mapping they just wrote — mirroring the
    _handle_multi_fetch_result ConsistentRead=True settlement check in
    plugin_importer.py. The default (False) issues exactly the same
    eventually-consistent get_item as before, with no ConsistentRead
    key at all.
    """
    kwargs = {'Key': {'plugin_id': plugin_id, 'version': version}}
    if consistent_read:
        kwargs['ConsistentRead'] = True
    response = plugin_table().get_item(**kwargs)
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def query_versions(plugin_id: str) -> List[Dict]:
    """All version items of a Plugin_Record, newest first"""
    from boto3.dynamodb.conditions import Key
    items: List[Dict] = []
    kwargs = {
        'KeyConditionExpression': Key('plugin_id').eq(plugin_id),
        'ScanIndexForward': False,
    }
    while True:
        response = plugin_table().query(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return [decimal_to_native(i) for i in items]


def get_latest_version_item(plugin_id: str) -> Optional[Dict]:
    """Fetch the newest version item of a Plugin_Record, or None"""
    from boto3.dynamodb.conditions import Key
    response = plugin_table().query(
        KeyConditionExpression=Key('plugin_id').eq(plugin_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get('Items', [])
    return decimal_to_native(items[0]) if items else None


def query_plugins_by_usecase(usecase_id: str) -> List[Dict]:
    """All Plugin_Record version items of a Use_Case via the GSI"""
    from boto3.dynamodb.conditions import Key
    items: List[Dict] = []
    kwargs = {
        'IndexName': USECASE_PLUGINS_INDEX,
        'KeyConditionExpression': Key('usecase_id').eq(usecase_id),
    }
    while True:
        response = plugin_table().query(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return [decimal_to_native(i) for i in items]


# -------------------------------------------------- record shape / guards
#
# These functions are pure over the item dicts, so the lifecycle state
# machine is testable (and property-testable) without AWS.

def new_version_item(plugin_id: str, version: int, usecase_id: str, name: str,
                     kind: str, user_id: str, timestamp: int,
                     description: str = '', deepstream: bool = False,
                     provenance: Optional[Dict] = None) -> Dict:
    """
    Build a Plugin_Record version item.

    Every new record and every new version starts in Lifecycle_State dev
    (9.1, 9.13) with the security review decision pending (10.1, 10.5),
    independently of any prior version's state or approvals.
    """
    return {
        'plugin_id': plugin_id,
        'version': version,
        'usecase_id': usecase_id,
        'name': name,
        'description': description,
        'kind': kind,
        'deepstream': bool(deepstream),
        'provenance': provenance or {},
        'lifecycle_state': STATE_DEV,
        'review': {'decision': REVIEW_PENDING, 'reviewer': None, 'reviewedAt': None},
        'artifacts': {},
        'component': {},
        'source_s3_prefix': source_s3_prefix(usecase_id, plugin_id, version),
        'created_by': user_id,
        'created_at': timestamp,
        'updated_at': timestamp,
    }


def successful_build_archs(item: Dict) -> List[str]:
    """Architectures with a successfully built Plugin_Artifact"""
    artifacts = item.get('artifacts') or {}
    return sorted(
        arch for arch, entry in artifacts.items()
        if isinstance(entry, dict) and entry.get('buildStatus') == BUILD_SUCCEEDED
    )


def evaluate_promotion(item: Dict) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Decide a promotion request against the lifecycle state machine.

    Returns (next_state, None) when the promotion is permitted, or
    (None, {code, message, details}) describing the rejection:
      - dev -> test requires at least one successfully built
        Plugin_Artifact; rejection identifies the missing build (9.4, 9.5)
      - test -> prod requires an approved security review; rejection
        identifies the missing approval (9.9, 9.10)
      - anything else is an invalid transition
    """
    state = item.get('lifecycle_state')
    if state not in PROMOTIONS:
        return None, {
            'code': 'INVALID_LIFECYCLE_TRANSITION',
            'message': f"Cannot promote from lifecycle state '{state}'",
            'details': {'lifecycle_state': state},
        }

    if state == STATE_DEV:
        built = successful_build_archs(item)
        if not built:
            return None, {
                'code': 'PLUGIN_BUILD_REQUIRED',
                'message': 'Promotion from dev to test requires at least one '
                           'successfully built Plugin_Artifact; none exists '
                           'for this Plugin_Record version',
                'details': {
                    'plugin_id': item.get('plugin_id'),
                    'version': item.get('version'),
                    'missing': 'successfully built Plugin_Artifact',
                    'successful_architectures': [],
                },
            }
        return STATE_TEST, None

    # state == STATE_TEST
    review = item.get('review') or {}
    if review.get('decision') != REVIEW_APPROVED:
        return None, {
            'code': 'SECURITY_REVIEW_REQUIRED',
            'message': 'Promotion from test to prod requires an approved '
                       'security review; the review decision is '
                       f"'{review.get('decision', REVIEW_PENDING)}'",
            'details': {
                'plugin_id': item.get('plugin_id'),
                'version': item.get('version'),
                'missing': 'approved security review',
                'review_decision': review.get('decision', REVIEW_PENDING),
            },
        }
    return STATE_PROD, None


def evaluate_demotion(item: Dict) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Decide a demotion request. Demotion (prod->test, test->dev) always
    succeeds and only changes the state (9.12): already-deployed
    Workflow_Components are untouched; the demoted state's gates apply
    only to subsequent packaging/deployment requests.
    """
    state = item.get('lifecycle_state')
    if state not in DEMOTIONS:
        return None, {
            'code': 'INVALID_LIFECYCLE_TRANSITION',
            'message': f"Cannot demote from lifecycle state '{state}'",
            'details': {'lifecycle_state': state},
        }
    return DEMOTIONS[state], None


def version_detail(item: Dict) -> Dict:
    """
    Full Plugin_Record version view: provenance (repo URL/revision,
    scaffold origin or generation prompt, user, timestamps,
    classification, 10.2/15.6), per-arch artifact checksums and
    signatures, lifecycle state, review decision, component pointer.

    Imported records also carry their import fields (import_status,
    import_finding, plugins_found, selected_plugins) so clients can
    poll GET /plugins/{id}/versions/{v} while the asynchronous fetch
    runs (import_status 'fetching') and act on the outcome.
    """
    detail = {
        'plugin_id': item['plugin_id'],
        'version': item['version'],
        'usecase_id': item['usecase_id'],
        'name': item.get('name'),
        'description': item.get('description', ''),
        'kind': item.get('kind'),
        'deepstream': item.get('deepstream', False),
        'provenance': item.get('provenance', {}),
        'lifecycle_state': item.get('lifecycle_state'),
        'review': item.get('review', {}),
        'artifacts': item.get('artifacts', {}),
        'component': item.get('component', {}),
        'source_s3_prefix': item.get('source_s3_prefix'),
        'created_by': item.get('created_by'),
        'created_at': item.get('created_at'),
        'updated_at': item.get('updated_at'),
    }
    if item.get('import_status') is not None:
        detail['import_status'] = item['import_status']
    if item.get('import_finding'):
        detail['import_finding'] = item['import_finding']
    if item.get('plugins_found') is not None:
        detail['plugins_found'] = item['plugins_found']
    if item.get('selected_plugins') is not None:
        detail['selected_plugins'] = item['selected_plugins']
    if item.get('platform_compatibility') is not None:
        # Advisory per-platform requirements check recorded when the
        # import fetch settled (plugin_importer.platform_compatibility):
        # {arch: {compatible, platformVersion, requiredVersion, reason,
        # suggestedRevision}}. Never blocks builds.
        detail['platform_compatibility'] = item['platform_compatibility']
    if item.get('arch_revisions') is not None:
        # Multi-revision import: {arch: revision-slug} into the fetches
        # map, so the UI can show which source revision each
        # architecture builds from.
        detail['arch_revisions'] = item['arch_revisions']
    if item.get('fetches') is not None:
        # Per-revision fetch map of a multi-revision import:
        # {slug: {revision, source_prefix, fetch_build_id, status}}.
        detail['fetches'] = item['fetches']
    return detail


def record_summary(item: Dict) -> Dict:
    """List-view summary of one Plugin_Record version. Imported records
    additionally carry their recorded plugin selection
    (selected_plugins) and the enumeration size (plugins_found_count)
    so the library list can show which plugins an import covers."""
    artifacts = item.get('artifacts') or {}
    summary = {
        'plugin_id': item['plugin_id'],
        'version': item['version'],
        'usecase_id': item['usecase_id'],
        'name': item.get('name'),
        'kind': item.get('kind'),
        'deepstream': item.get('deepstream', False),
        'lifecycle_state': item.get('lifecycle_state'),
        'review_decision': (item.get('review') or {}).get('decision'),
        'import_status': item.get('import_status'),
        'classification': (item.get('provenance') or {}).get('classification'),
        'build_status': {
            arch: (entry or {}).get('buildStatus')
            for arch, entry in artifacts.items()
        },
        'updated_at': item.get('updated_at'),
    }
    if item.get('selected_plugins') is not None:
        summary['selected_plugins'] = item['selected_plugins']
    if item.get('plugins_found') is not None:
        summary['plugins_found_count'] = len(item['plugins_found'])
    return summary


# ------------------------------------------------------------ authorization

def authorize_record_access(
        user: Dict, event: Dict, item: Dict, manage: bool = False,
        permission: Permission = Permission.NODE_DESIGNER_MANAGE) -> Optional[Dict]:
    """
    Authorize an operation on an existing Plugin_Record.

    Returns an error response, or None when authorized. Read access is
    granted to every role of the Use_Case (13.3); manage operations
    require UseCaseAdmin within the Use_Case or PortalAdmin (13.1). A
    user without any resolvable access receives the same 404 as for a
    missing record so existence is never leaked across tenants.
    """
    usecase_id = item['usecase_id']
    if not has_node_designer_permission(user, usecase_id,
                                        Permission.NODE_DESIGNER_READ):
        return not_found_response()
    if manage and not has_node_designer_permission(user, usecase_id, permission):
        return forbidden_response(user, event, usecase_id, permission)
    return None


# ----------------------------------------------------------------- handlers

def create_plugin(event: Dict, user: Dict) -> Dict:
    """
    POST /plugins
    Body: {usecase_id, name, kind, description?, deepstream?, provenance?,
           declaration?}
    Creates version 1 in Lifecycle_State dev (9.1) with the security
    review decision pending (10.1).

    With `declaration` (the create wizard's scaffold path, kind must be
    'scaffold'), the Plugin_Scaffold is rendered server-side via
    workflow_core.scaffold and stored under the version's plugin-sources
    prefix; the rendered file map is returned as `files` for preview,
    download, and editing (1.2, 1.5). An invalid declaration fails with
    the offending field identified and creates no Plugin_Record (1.7).
    """
    body, err = parse_body(event)
    if err:
        return err

    usecase_id = body.get('usecase_id')
    name = body.get('name')
    kind = body.get('kind')

    missing = [f for f in ('usecase_id', 'name', 'kind') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")
    if kind not in RECORD_KINDS:
        return error_response(400, 'INVALID_KIND',
                              f"kind must be one of: {', '.join(RECORD_KINDS)}")

    provenance = body.get('provenance') or {}
    if not isinstance(provenance, dict):
        return error_response(400, 'INVALID_PROVENANCE',
                              'provenance must be a JSON object')

    declaration = body.get('declaration')
    scaffold_files: Optional[Dict[str, str]] = None
    if declaration is not None:
        if kind != 'scaffold':
            return error_response(400, 'INVALID_DECLARATION',
                                  "declaration is only accepted for kind 'scaffold'")
        # Scaffold generation failure identifies the failing input and
        # creates no Plugin_Record (Requirement 1.7).
        try:
            scaffold_files = render_scaffold(declaration)
        except ScaffoldError as exc:
            return error_response(400, 'INVALID_DECLARATION', str(exc),
                                  {'field': exc.field})

    if not can_manage(user, usecase_id, Permission.NODE_DESIGNER_CREATE):
        return forbidden_response(user, event, usecase_id,
                                  Permission.NODE_DESIGNER_CREATE)

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    plugin_id = str(uuid.uuid4())
    timestamp = now_ms()
    provenance.setdefault('createdBy', user['user_id'])
    provenance.setdefault('createdAt', timestamp)
    if declaration is not None:
        provenance.setdefault('scaffoldDeclaration',
                              json.dumps(declaration, sort_keys=True))

    item = new_version_item(
        plugin_id=plugin_id, version=1, usecase_id=usecase_id, name=name,
        kind=kind, user_id=user['user_id'], timestamp=timestamp,
        description=body.get('description', ''),
        deepstream=body.get('deepstream', False),
        provenance=provenance,
    )

    # The rendered scaffold lands under the standard plugin-sources
    # layout the Plugin_Build_Service builds from (design S3 layout).
    if scaffold_files is not None:
        prefix = item['source_s3_prefix']
        for path, content in sorted(scaffold_files.items()):
            s3.put_object(
                Bucket=PORTAL_ARTIFACTS_BUCKET,
                Key=prefix + path,
                Body=content.encode('utf-8'),
                ContentType='text/plain; charset=utf-8',
            )

    plugin_table().put_item(
        Item=item,
        ConditionExpression='attribute_not_exists(plugin_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='create_plugin_record',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': name, 'kind': kind, 'version': 1}
    )

    payload: Dict[str, Any] = {'plugin': version_detail(item)}
    if scaffold_files is not None:
        payload['files'] = scaffold_files
    return create_response(201, payload)


def list_plugins(event: Dict, user: Dict) -> Dict:
    """
    GET /plugins[?usecase_id=...][&review=pending]
    Lists Plugin_Record versions of accessible Use_Cases. With
    review=pending the list is restricted to versions awaiting a
    security review decision (the PortalAdmin review queue).
    """
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    review_filter = params.get('review')

    if usecase_id:
        if not has_node_designer_permission(user, usecase_id,
                                            Permission.NODE_DESIGNER_READ):
            return forbidden_response(user, event, usecase_id,
                                      Permission.NODE_DESIGNER_READ)
        usecase_ids = [usecase_id]
    else:
        usecase_ids = rbac_manager.get_accessible_usecases(user['user_id'], user_info=user)

    items: List[Dict] = []
    for uc in usecase_ids:
        items.extend(query_plugins_by_usecase(uc))

    if review_filter:
        items = [i for i in items
                 if (i.get('review') or {}).get('decision') == review_filter]

    items.sort(key=lambda i: i.get('updated_at') or 0, reverse=True)
    return create_response(200, {
        'plugins': [record_summary(i) for i in items],
        'count': len(items)
    })


def get_plugin(event: Dict, user: Dict, plugin_id: str) -> Dict:
    """
    GET /plugins/{id}
    Latest version detail plus the version history.
    """
    versions = query_versions(plugin_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_record_access(user, event, latest)
    if err:
        return err

    return create_response(200, {
        'plugin': version_detail(latest),
        'versions': [record_summary(i) for i in versions],
    })


def update_plugin(event: Dict, user: Dict, plugin_id: str) -> Dict:
    """
    PUT /plugins/{id}
    Body: {name?, description?, deepstream?, new_version?, provenance?}

    Without new_version, updates mutable metadata on the latest version.
    With new_version=true (changed source or declaration), creates a new
    version item whose Lifecycle_State is dev (9.13) and whose security
    review decision is pending (10.5), independently of prior versions.
    """
    body, err = parse_body(event)
    if err:
        return err

    latest = get_latest_version_item(plugin_id)
    if not latest:
        return not_found_response()
    err = authorize_record_access(user, event, latest, manage=True)
    if err:
        return err

    timestamp = now_ms()

    if body.get('new_version'):
        provenance = dict(latest.get('provenance') or {})
        updates = body.get('provenance') or {}
        if not isinstance(updates, dict):
            return error_response(400, 'INVALID_PROVENANCE',
                                  'provenance must be a JSON object')
        provenance.update(updates)
        provenance['createdBy'] = user['user_id']
        provenance['createdAt'] = timestamp

        item = new_version_item(
            plugin_id=plugin_id,
            version=latest['version'] + 1,
            usecase_id=latest['usecase_id'],
            name=body.get('name', latest.get('name')),
            kind=latest.get('kind'),
            user_id=user['user_id'],
            timestamp=timestamp,
            description=body.get('description', latest.get('description', '')),
            deepstream=body.get('deepstream', latest.get('deepstream', False)),
            provenance=provenance,
        )
        plugin_table().put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(version)'
        )
        log_audit_event(
            user_id=user['user_id'],
            action='create_plugin_record_version',
            resource_type='plugin_record',
            resource_id=plugin_id,
            result='success',
            details={'usecase_id': latest['usecase_id'], 'version': item['version']}
        )
        return create_response(201, {'plugin': version_detail(item)})

    # Metadata-only update of the latest version
    changed = {}
    for field in ('name', 'description', 'deepstream'):
        if field in body:
            changed[field] = body[field]
    if not changed:
        return error_response(400, 'NO_UPDATES',
                              'Provide name, description, deepstream, or new_version')

    expr_names = {f'#{k}': k for k in changed}
    expr_values = {f':{k}': v for k, v in changed.items()}
    expr_values[':updated_at'] = now_ms()
    update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in changed) + ', updated_at = :updated_at'

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': latest['version']},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
    log_audit_event(
        user_id=user['user_id'],
        action='update_plugin_record',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': latest['usecase_id'],
                 'version': latest['version'], 'fields': sorted(changed)}
    )
    item = get_version_item(plugin_id, latest['version'])
    return create_response(200, {'plugin': version_detail(item)})


def _delete_prefix_objects(prefix: str) -> None:
    """Delete every S3 object under one plugin-sources prefix."""
    paginator = s3.get_paginator('list_objects_v2')
    keys: List[str] = []
    for page in paginator.paginate(Bucket=PORTAL_ARTIFACTS_BUCKET,
                                   Prefix=prefix):
        keys.extend(obj['Key'] for obj in page.get('Contents', []))
    for start in range(0, len(keys), 1000):  # DeleteObjects caps at 1000
        s3.delete_objects(
            Bucket=PORTAL_ARTIFACTS_BUCKET,
            Delete={'Objects': [{'Key': k}
                                for k in keys[start:start + 1000]]},
        )


def _cleanup_record_objects(versions: List[Dict]) -> None:
    """
    Best-effort S3 cleanup of a deleted Plugin_Record: the source
    snapshots under every version's plugin-sources prefix
    (source_s3_prefix plus any multi-revision fetches[*].source_prefix)
    and the promoted Plugin_Library artifacts (artifacts[*].s3Key and
    the detached .sig alongside). A cleanup failure never fails the
    delete — it only logs a warning (the record itself is gone).
    """
    prefixes = set()
    keys = set()
    for item in versions:
        if item.get('source_s3_prefix'):
            prefixes.add(item['source_s3_prefix'])
        for fetch in (item.get('fetches') or {}).values():
            if (fetch or {}).get('source_prefix'):
                prefixes.add(fetch['source_prefix'])
        for entry in (item.get('artifacts') or {}).values():
            so_key = (entry or {}).get('s3Key')
            if so_key:
                keys.add(so_key)
                keys.add(so_key + '.sig')
    for prefix in sorted(prefixes):
        try:
            _delete_prefix_objects(prefix)
        except Exception as e:
            logger.warning(
                f"Could not clean up plugin sources under {prefix}: {e}")
    for key in sorted(keys):
        try:
            s3.delete_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key)
        except Exception as e:
            logger.warning(f"Could not clean up plugin artifact {key}: {e}")


def delete_plugin(event: Dict, user: Dict, plugin_id: str) -> Dict:
    """
    DELETE /plugins/{id}
    Deletes every version of the Plugin_Record (bad or duplicate
    imports) from the table, with best-effort S3 cleanup of the source
    snapshots and promoted Plugin_Library artifacts (cleanup failure
    never fails the delete). Refused with 409 RECORD_IN_USE when any
    version was promoted beyond the draft-like dev state (test/prod) —
    demote first. Records whose import failed or is still fetching or
    awaiting a plugin selection are always in dev and thus deletable.
    """
    versions = query_versions(plugin_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_record_access(user, event, latest, manage=True)
    if err:
        return err

    promoted = sorted(v['version'] for v in versions
                      if v.get('lifecycle_state') in (STATE_TEST, STATE_PROD))
    if promoted:
        return error_response(
            409, 'RECORD_IN_USE',
            'This Plugin_Record has versions promoted beyond dev and '
            'cannot be deleted; demote them first',
            {'versions': promoted})

    _cleanup_record_objects(versions)

    deleted_versions = sorted(v['version'] for v in versions)
    with plugin_table().batch_writer() as batch:
        for v in versions:
            batch.delete_item(Key={'plugin_id': plugin_id,
                                   'version': v['version']})

    log_audit_event(
        user_id=user['user_id'],
        action='delete_plugin_record',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': latest['usecase_id'],
                 'name': latest.get('name'),
                 'versions': deleted_versions}
    )
    return create_response(200, {'deleted': True, 'plugin_id': plugin_id,
                                 'versions': deleted_versions})


def get_version(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    GET /plugins/{id}/versions/{v}
    Full version detail for display and security review: provenance
    (repo URL/revision, scaffold origin, or generation prompt, plus the
    importing/creating user, timestamps, and classification), per-arch
    Plugin_Artifact checksums and signatures (10.2, 15.6).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err
    return create_response(200, {'plugin': version_detail(item)})


def get_version_source(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    GET /plugins/{id}/versions/{v}/source[?file=relative/path]
    Source inspection for the security review (10.2): without `file`,
    lists the source files under the version's S3 prefix; with `file`,
    returns that file's content (text, size-capped).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err

    prefix = item['source_s3_prefix']
    params = event.get('queryStringParameters') or {}
    file_path = params.get('file')

    if file_path:
        # Normalize and confine the key to the version's source prefix
        normalized = os.path.normpath(file_path).lstrip('/')
        if normalized.startswith('..'):
            return error_response(400, 'INVALID_FILE_PATH', 'Invalid file path')
        key = prefix + normalized
        try:
            obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                                Range=f'bytes=0-{MAX_SOURCE_FILE_BYTES - 1}')
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return error_response(404, 'SOURCE_FILE_NOT_FOUND',
                                      'Source file not found', {'file': file_path})
            raise
        content = obj['Body'].read().decode('utf-8', errors='replace')
        return create_response(200, {'file': file_path, 'content': content})

    files: List[Dict] = []
    kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': prefix}
    while True:
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get('Contents', []):
            files.append({
                'file': obj['Key'][len(prefix):],
                'size': obj['Size'],
            })
        if not response.get('IsTruncated'):
            break
        kwargs['ContinuationToken'] = response['NextContinuationToken']

    return create_response(200, {'files': files, 'count': len(files)})


def _gst_unavailable(reason: str, message: Optional[str] = None) -> Dict:
    """200 {available: false, reason, message?} — scan unavailability is a
    normal, machine-readable outcome, never an error (1.6, 7.4, 8.3)."""
    payload: Dict[str, Any] = {'available': False, 'reason': reason}
    if message:
        payload['message'] = message
    return create_response(200, payload)


def get_version_gst_properties(event: Dict, user: Dict, plugin_id: str,
                               version: int) -> Dict:
    """
    GET /plugins/{id}/versions/{v}/gst-properties

    Serves the stored Introspection_Report of the version's x86_64
    Plugin_Artifact together with the derived Parameter_Suggestions
    (gst-parameter-prepopulation Requirement 1.5), or a machine-readable
    unavailability reason (1.6, 7.4):

      - `no_x86_64_build`: no successfully built x86_64 Plugin_Artifact
      - `not_captured`: the build predates Property_Introspection (no
        gstIntrospection stanza on the artifact entry)
      - `introspection_failed`: capture recorded a failure, or the stored
        report is missing or malformed at read time (8.3 — never a 500)

    Available responses carry per-element suggestions in the
    ParameterDeclaration wire shape: base-class-filtered, type-mapped,
    and required-classified by gst_properties.suggestions_for_element,
    plus the skipped properties with reasons (2.5).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err

    entry = (item.get('artifacts') or {}).get(INTROSPECTION_ARCH)
    if (not isinstance(entry, dict)
            or entry.get('buildStatus') != BUILD_SUCCEEDED):
        return _gst_unavailable(GST_REASON_NO_BUILD)

    stanza = entry.get('gstIntrospection')
    if not isinstance(stanza, dict):
        # Successful build recorded before this feature existed (7.4).
        return _gst_unavailable(GST_REASON_NOT_CAPTURED)

    if stanza.get('status') != STATUS_CAPTURED:
        return _gst_unavailable(GST_REASON_FAILED, stanza.get('message'))

    report_key = stanza.get('s3Key')
    if not report_key:
        return _gst_unavailable(
            GST_REASON_FAILED, 'Stored introspection stanza has no report key')

    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=report_key)
        document = json.loads(obj['Body'].read().decode('utf-8'))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return _gst_unavailable(
                GST_REASON_FAILED, 'Stored introspection report is missing')
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _gst_unavailable(
            GST_REASON_FAILED, 'Stored introspection report is not valid JSON')

    # Malformed stored documents map to the unavailability reason, never
    # an internal error (8.3).
    try:
        report = parse_report(document)
    except ReportError as e:
        return _gst_unavailable(
            GST_REASON_FAILED, f'Stored introspection report is malformed: {e}')

    if report.status != STATUS_CAPTURED:
        return _gst_unavailable(GST_REASON_FAILED, report.message)

    elements = []
    for element in report.elements:
        derived = suggestions_for_element(element)
        # Port_Scan derivation (port-guidance-and-pad-prepopulation 4.5):
        # additive per-element fields; existing keys stay untouched (4.6).
        ports = ports_for_element(element)
        elements.append({
            'factory': element.factory,
            'suggestions': derived['suggestions'],
            'skipped': derived['skipped'],
            'portSuggestions': ports['portSuggestions'],
            'unmappedPads': ports['unmappedPads'],
            'padsReason': ports['padsReason'],
            'padsMessage': ports['padsMessage'],
        })

    return create_response(200, {
        'available': True,
        'gstVersion': report.gst_version or stanza.get('gstVersion'),
        'capturedAt': report.captured_at or stanza.get('capturedAt'),
        'elements': elements,
    })


def put_version_source(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    PUT /plugins/{id}/versions/{v}/source
    Body: {files: {relative/path: content}}

    Persists user-submitted Plugin_Scaffold source (original or edited,
    Requirement 1.6) under the version's plugin-sources prefix so a
    subsequent build submission builds exactly what the user reviewed.
    For scaffold-kind records the submitted map is the complete source
    tree and is validated for buildability against the recorded
    declaration (422 with every defect described) before anything is
    written.
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

    files = body.get('files')
    if (not isinstance(files, dict) or not files
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in files.items())):
        return error_response(400, 'INVALID_FILES',
                              'files must be a non-empty object mapping '
                              'relative paths to text content')

    # Confine every key to the version's source prefix.
    normalized: Dict[str, str] = {}
    for path, content in files.items():
        clean = os.path.normpath(path).lstrip('/')
        if clean.startswith('..') or clean in ('.', ''):
            return error_response(400, 'INVALID_FILE_PATH',
                                  'Invalid file path', {'file': path})
        normalized[clean] = content

    # Scaffold-kind records keep the buildability guarantee: reject
    # non-buildable source with every defect described (1.7, 2.6).
    declaration_json = (item.get('provenance') or {}).get('scaffoldDeclaration')
    if item.get('kind') == 'scaffold' and declaration_json:
        defects = scaffold_defects(normalized, json.loads(declaration_json))
        if defects:
            return error_response(
                422, 'SCAFFOLD_INVALID',
                'The submitted source does not form a buildable '
                'Plugin_Scaffold: ' + '; '.join(defects),
                {'defects': defects})

    prefix = item['source_s3_prefix']
    for path, content in sorted(normalized.items()):
        s3.put_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET,
            Key=prefix + path,
            Body=content.encode('utf-8'),
            ContentType='text/plain; charset=utf-8',
        )

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET updated_at = :t',
        ExpressionAttributeValues={':t': now_ms()},
    )

    log_audit_event(
        user_id=user['user_id'],
        action='update_plugin_source',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'files': sorted(normalized)}
    )

    return create_response(200, {'files': sorted(normalized),
                                 'count': len(normalized)})


def promote_version(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/promote
    dev->test requires at least one successfully built Plugin_Artifact
    (409 identifying the missing build, 9.4/9.5); test->prod requires an
    approved security review (409 identifying the missing approval,
    9.9/9.10).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_PROMOTE_DEMOTE)
    if err:
        return err

    next_state, guard_error = evaluate_promotion(item)
    if guard_error:
        log_audit_event(
            user_id=user['user_id'],
            action='promote_plugin_record',
            resource_type='plugin_record',
            resource_id=plugin_id,
            result='denied',
            details={'usecase_id': item['usecase_id'], 'version': version,
                     'from': item.get('lifecycle_state'), 'reason': guard_error['code']}
        )
        return error_response(409, guard_error['code'], guard_error['message'],
                              guard_error['details'])

    return _apply_transition(user, item, next_state, 'promote_plugin_record')


def demote_version(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/demote
    prod->test and test->dev always succeed; the demoted state's gates
    apply only to subsequent packaging/deployment requests while
    deployed Workflow_Components continue to run unchanged (9.12).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_PROMOTE_DEMOTE)
    if err:
        return err

    next_state, guard_error = evaluate_demotion(item)
    if guard_error:
        return error_response(409, guard_error['code'], guard_error['message'],
                              guard_error['details'])

    return _apply_transition(user, item, next_state, 'demote_plugin_record')


def _apply_transition(user: Dict, item: Dict, next_state: str, action: str) -> Dict:
    """Persist a lifecycle transition and record the audit entry (13.5)"""
    plugin_id, version = item['plugin_id'], item['version']
    previous = item.get('lifecycle_state')
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET lifecycle_state = :s, updated_at = :t',
        ExpressionAttributeValues={':s': next_state, ':t': now_ms()},
    )
    log_audit_event(
        user_id=user['user_id'],
        action=action,
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'from': previous, 'to': next_state}
    )
    updated = get_version_item(plugin_id, version)
    return create_response(200, {'plugin': version_detail(updated)})


def review_version(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/review
    Body: {decision: approved|rejected, notes?}
    PortalAdmin only (13.2). Records the decision, the acting
    PortalAdmin, and a timestamp on the Plugin_Record version and in the
    existing AuditLog table (10.3).
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()

    if not is_portal_admin(user):
        return forbidden_response(user, event, item['usecase_id'],
                                  Permission.NODE_DESIGNER_SECURITY_REVIEW)

    body, err = parse_body(event)
    if err:
        return err
    decision = body.get('decision')
    if decision not in REVIEW_DECISIONS:
        return error_response(400, 'INVALID_REVIEW_DECISION',
                              f"decision must be one of: {', '.join(REVIEW_DECISIONS)}")

    timestamp = now_ms()
    review = {
        'decision': decision,
        'reviewer': user['user_id'],
        'reviewedAt': timestamp,
    }
    if body.get('notes'):
        review['notes'] = body['notes']

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET review = :r, updated_at = :t',
        ExpressionAttributeValues={':r': review, ':t': timestamp},
    )

    # Requirement 10.3: decision + acting PortalAdmin + timestamp in the
    # existing audit log (log_audit_event writes AUDIT_LOG_TABLE with a
    # timestamp attribute).
    log_audit_event(
        user_id=user['user_id'],
        action=f'security_review_{decision}',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'decision': decision, 'reviewer': user['user_id']}
    )

    updated = get_version_item(plugin_id, version)
    return create_response(200, {'plugin': version_detail(updated)})


# ------------------------------------------------------------------ routing

def _parse_version(path_params: Dict) -> Tuple[Optional[int], Optional[Dict]]:
    """Parse the {v} path parameter as an integer"""
    raw = path_params.get('v')
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, error_response(400, 'INVALID_VERSION', 'version must be an integer')


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
        plugin_id = path_params.get('id')

        if resource == '/plugins':
            if http_method == 'GET':
                return list_plugins(event, user)
            if http_method == 'POST':
                return create_plugin(event, user)
        elif resource == '/plugins/{id}' and plugin_id:
            if http_method == 'GET':
                return get_plugin(event, user, plugin_id)
            if http_method == 'PUT':
                return update_plugin(event, user, plugin_id)
            if http_method == 'DELETE':
                return delete_plugin(event, user, plugin_id)
        elif resource.startswith('/plugins/{id}/versions/{v}') and plugin_id:
            version, err = _parse_version(path_params)
            if err:
                return err
            if resource == '/plugins/{id}/versions/{v}':
                if http_method == 'GET':
                    return get_version(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/source':
                if http_method == 'GET':
                    return get_version_source(event, user, plugin_id, version)
                if http_method == 'PUT':
                    return put_version_source(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/gst-properties':
                if http_method == 'GET':
                    return get_version_gst_properties(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/promote':
                if http_method == 'POST':
                    return promote_version(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/demote':
                if http_method == 'POST':
                    return demote_version(event, user, plugin_id, version)
            elif resource == '/plugins/{id}/versions/{v}/review':
                if http_method == 'POST':
                    return review_version(event, user, plugin_id, version)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
