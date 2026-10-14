"""
Custom_Node_Type registration API Lambda function (Custom Node Designer)

Registration, versioning, deprecation, and reference-checked removal of
Custom_Node_Types over the CustomNodeTypes DynamoDB table
(Requirements 8.1, 8.2, 8.5, 8.6, 14.1, 14.3, 14.4, 14.5).

Routes (API Gateway REST, node-designer-api-stack.ts):
    POST   /custom-node-types                Register a Custom_Node_Type for a
                                             built Plugin_Record version: display
                                             name, category, Ports with types,
                                             parameters (types, defaults,
                                             constraints, descriptions, examples),
                                             hardware-dependence flag, element/
                                             property mapping per built
                                             Target_Architecture, Use_Case
                                             scoping                       (8.1, 8.2)
    GET    /custom-node-types/{id}           Latest version + version history
    PUT    /custom-node-types/{id}           Declaration update -> new version
                                             item, prior versions retained (14.1)
    DELETE /custom-node-types/{id}           Reference-checked removal     (14.4, 14.5)
    POST   /custom-node-types/{id}/deprecate Flip the deprecated flag      (14.3)

Storage layout (design "Data Models"):
    CustomNodeTypes table (CUSTOM_NODE_TYPES_TABLE)
        PK node_type_id (S, the declaration's typeId), SK version (N),
        GSI usecase-node-types-index (usecase_id + node_type_id).
        Attributes: usecase_id (owning Use_Case = the plugin's), usecase_ids
        (Use_Case scoping selected at registration), plugin_id +
        plugin_version (pinned backing Plugin_Record version), declaration
        (NodeTypeDescriptor wire JSON), deprecated flag, created_by/at.

Declarations are validated through workflow_core.catalog.custom
.descriptor_from_declaration; invalid Port declarations are rejected with
the offending field identified (8.5). The plugin dependency is recorded
as ``custom:{usecase_id}/{plugin_name}`` in every mapping so the
Workflow_Compiler includes the plugin in compiled dependency lists (8.6),
and every declared mapping must target a successfully built
Target_Architecture of the backing Plugin_Record version (8.1).

Removal (14.4/14.5) scans WorkflowVersions for references to the
Custom_Node_Type. The scan first tries the inverted-index GSI that
workflow save maintains (task 9.2: one ``ref_node_type_id`` keyed entry
per referenced type); until that index exists the scan falls back to a
full WORKFLOW_VERSIONS_TABLE scan, checking the ``custom_node_types``
reference attribute recorded at save when present and otherwise loading
the stored definition document from S3. Zero references deletes the
catalog items, the plugin's Plugin_Library artifacts
(workflow-plugins/custom/{usecase}/...), and the plugin's
Plugin_Component versions in the Use_Case account registry; otherwise
the removal is rejected listing the referencing workflows.

Error envelope: {"error": {"code", "message", "details"}} with 400
parse/validation failures identifying the offending field, 403 RBAC
denial, 404 scoped to avoid cross-tenant existence leaks, and 409 for
type-id conflicts and reference-blocked removal.

Access control (Requirement 13): node-designer:read for every role in
the Use_Case; node-designer:register for registration and
node-designer:manage for update/deprecate/remove (UseCaseAdmin within
the own Use_Case, PortalAdmin), following plugin_records.py conventions.

The versioning and reference-counting decision logic (new_node_type_item,
next_node_type_version, inject_plugin_dependency,
unbuilt_mapping_architectures, definition_references_node_type,
item_references_node_type, evaluate_removal) is pure over plain dicts so
tasks 9.3-9.5 can property-test it without AWS.
"""
import json
import os
import logging
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, get_usecase_client, Permission
)

from workflow_core.catalog.custom import DeclarationError, descriptor_from_declaration
from workflow_core.catalog.nodes import NODE_CATALOG

# Reuse the Plugin_Record persistence helpers, error envelope, and RBAC
# helpers from plugin_records.py, the Plugin_Library key conventions from
# plugin_builds.py, the Plugin_Component naming/prefixes from
# plugin_components.py, and the account-bucket cleanup helper from
# workflow_packaging.py (same deployment bundle).
from plugin_records import (
    decimal_to_native,
    error_response,
    has_node_designer_permission,
    now_ms,
    parse_body,
    get_version_item as get_plugin_version_item,
    get_latest_version_item as get_latest_plugin_version_item,
    query_versions as query_plugin_versions,
    successful_build_archs,
)
from plugin_builds import sanitize_plugin_name, signature_key
from plugin_components import COMPONENT_S3_PREFIX, component_name_for
from workflow_packaging import delete_prefix

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (portal account)
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
CUSTOM_NODE_TYPES_TABLE = os.environ.get('CUSTOM_NODE_TYPES_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')

# Optional inverted-index GSI over WorkflowVersions. Task 9.2 decided
# AGAINST creating it: a DynamoDB GSI indexes one scalar attribute value
# per item, but a workflow version may reference several
# Custom_Node_Types, so a scalar ref_node_type_id cannot represent the
# reference set. Workflow save records the `custom_node_types` map
# attribute ({typeId: typeVersion}) instead, which the scan fallback
# below honors without loading definition documents. The index query is
# kept so a future design (e.g. one ref item per referenced type) can
# swap it in without touching callers.
NODE_TYPE_REFS_INDEX = os.environ.get(
    'WORKFLOW_NODE_TYPE_REFS_INDEX', 'node-type-refs-index')
NODE_TYPE_REF_ATTRIBUTE = 'ref_node_type_id'

USECASE_NODE_TYPES_INDEX = 'usecase-node-types-index'

#: Built-in Node_Type_Catalog type ids: a Custom_Node_Type may never
#: collide with a built-in type (resolve_catalog lets built-ins win, so a
#: colliding registration would silently never appear in the palette).
BUILTIN_TYPE_IDS = frozenset(descriptor.type_id for descriptor in NODE_CATALOG)


# ------------------------------------------------------------ pure helpers
#
# Everything from here to evaluate_removal is pure over plain dicts, so
# the versioning and reference-counting decision logic is
# property-testable without AWS (tasks 9.3-9.5).

def plugin_dependency_for(usecase_id: str, plugin_name: str) -> str:
    """The recorded plugin dependency of a Custom_Node_Type (8.6): the
    ``custom:`` prefix routes the Component_Packager to the plugin's
    Plugin_Component instead of the built-in Plugin_Library prefix."""
    return f"custom:{usecase_id}/{plugin_name}"


def inject_plugin_dependency(declaration: Dict, dependency: str) -> Dict:
    """Return a copy of the wire declaration with the plugin dependency
    recorded in every mapping's pluginDependencies (8.6). Idempotent:
    mappings already listing the dependency are unchanged."""
    decl = json.loads(json.dumps(declaration))
    mappings = decl.get('mappings')
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            dependencies = mapping.get('pluginDependencies')
            if not isinstance(dependencies, list):
                dependencies = []
            if dependency not in dependencies:
                dependencies = dependencies + [dependency]
            mapping['pluginDependencies'] = dependencies
    return decl


def unbuilt_mapping_architectures(declaration: Dict,
                                  built_archs: List[str]) -> List[Tuple[str, Any]]:
    """Mappings declared for Target_Architectures without a successfully
    built Plugin_Artifact (8.1: element/property mapping per *built*
    Target_Architecture). Returns [(field, arch)] identifying each
    offending mapping entry."""
    built = set(built_archs)
    offending: List[Tuple[str, Any]] = []
    mappings = declaration.get('mappings')
    if not isinstance(mappings, list):
        return offending
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            continue
        arch = mapping.get('arch')
        if arch not in built:
            offending.append((f"mappings[{index}].arch", arch))
    return offending


def next_node_type_version(latest_version: Optional[int]) -> int:
    """Version numbering: strictly increasing, prior versions retained (14.1)"""
    return 1 if latest_version is None else int(latest_version) + 1


def new_node_type_item(node_type_id: str, version: int, usecase_id: str,
                       usecase_ids: List[str], plugin_id: str,
                       plugin_version: int, declaration: Dict, user_id: str,
                       timestamp: int, deprecated: bool = False) -> Dict:
    """Build a CustomNodeTypes version item (design data model). The
    backing Plugin_Record version is pinned so packaging of saved
    workflows resolves the recorded version regardless of later updates
    (14.2 groundwork)."""
    return {
        'node_type_id': node_type_id,
        'version': int(version),
        'usecase_id': usecase_id,
        'usecase_ids': list(usecase_ids),
        'plugin_id': plugin_id,
        'plugin_version': int(plugin_version),
        'declaration': declaration,
        'deprecated': bool(deprecated),
        'created_by': user_id,
        'created_at': timestamp,
        'updated_at': timestamp,
    }


def definition_references_node_type(definition: Any, node_type_id: str) -> bool:
    """Whether a stored Workflow_Definition document places the
    Custom_Node_Type on the canvas (a node whose type is the type id)."""
    if not isinstance(definition, dict):
        return False
    nodes = definition.get('nodes')
    if not isinstance(nodes, list):
        return False
    return any(isinstance(node, dict) and node.get('type') == node_type_id
               for node in nodes)


def item_references_node_type(item: Dict, node_type_id: str,
                              definition_loader: Callable[[str], Any]) -> bool:
    """
    Whether one WorkflowVersions item references the Custom_Node_Type.

    Prefers the ``custom_node_types`` reference attribute recorded at
    workflow save (task 9.2 maintains it; a dict of {typeId: typeVersion}
    or a list of type ids). Items saved before that wiring carry no
    reference attribute, so the stored definition document is loaded and
    inspected instead. A definition that cannot be loaded is treated as
    non-referencing (logged by the loader).
    """
    references = item.get('custom_node_types')
    if isinstance(references, dict):
        return node_type_id in references
    if isinstance(references, (list, tuple, set)):
        return node_type_id in list(references)

    s3_key = item.get('s3_definition_key')
    if not s3_key:
        return False
    definition = definition_loader(s3_key)
    return definition_references_node_type(definition, node_type_id)


def evaluate_removal(node_type_id: str,
                     references: List[Dict]) -> Optional[Dict]:
    """
    Decide a removal request (14.4, 14.5): removal succeeds if and only
    if no saved workflow references the Custom_Node_Type. Returns None
    when removal is permitted, or {code, message, details} listing
    exactly the referencing workflows.
    """
    if not references:
        return None
    return {
        'code': 'CUSTOM_NODE_TYPE_IN_USE',
        'message': f"Custom node type '{node_type_id}' cannot be removed: "
                   f"{len(references)} saved workflow version(s) reference it",
        'details': {
            'node_type_id': node_type_id,
            'referencing_workflows': references,
        },
    }


# ------------------------------------------------------------------ views

def node_type_detail(item: Dict) -> Dict:
    """Full Custom_Node_Type version view"""
    item = decimal_to_native(item)
    return {
        'node_type_id': item['node_type_id'],
        'version': item['version'],
        'usecase_id': item['usecase_id'],
        'usecase_ids': item.get('usecase_ids', []),
        'plugin_id': item.get('plugin_id'),
        'plugin_version': item.get('plugin_version'),
        'declaration': item.get('declaration', {}),
        'deprecated': item.get('deprecated', False),
        'created_by': item.get('created_by'),
        'created_at': item.get('created_at'),
        'updated_at': item.get('updated_at'),
    }


def node_type_summary(item: Dict) -> Dict:
    """Version-history summary of one Custom_Node_Type version"""
    item = decimal_to_native(item)
    declaration = item.get('declaration') or {}
    return {
        'node_type_id': item['node_type_id'],
        'version': item['version'],
        'usecase_id': item['usecase_id'],
        'plugin_id': item.get('plugin_id'),
        'plugin_version': item.get('plugin_version'),
        'display_name': declaration.get('displayName'),
        'category': declaration.get('category'),
        'deprecated': item.get('deprecated', False),
        'updated_at': item.get('updated_at'),
    }


# ------------------------------------------------------------- persistence

def node_types_table():
    return dynamodb.Table(CUSTOM_NODE_TYPES_TABLE)


def to_dynamo_json(value: Any) -> Any:
    """JSON-shaped value with floats as Decimal for DynamoDB storage"""
    return json.loads(json.dumps(value), parse_float=Decimal)


def query_node_type_versions(node_type_id: str) -> List[Dict]:
    """All version items of a Custom_Node_Type, newest first (14.1)"""
    from boto3.dynamodb.conditions import Key
    items: List[Dict] = []
    kwargs = {
        'KeyConditionExpression': Key('node_type_id').eq(node_type_id),
        'ScanIndexForward': False,
    }
    while True:
        response = node_types_table().query(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return [decimal_to_native(item) for item in items]


# --------------------------------------------------------- reference scan

def load_workflow_definition(s3_key: str) -> Optional[Dict]:
    """Load one stored Workflow_Definition document; None when unreadable"""
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except (ClientError, ValueError) as e:
        logger.warning(f"Could not load workflow definition {s3_key} during "
                       f"the reference scan: {str(e)}")
        return None


def query_reference_index(node_type_id: str) -> List[Dict]:
    """
    Query the inverted-index GSI over WorkflowVersions that workflow save
    maintains (task 9.2). Raises ClientError when the index does not
    exist yet; the caller falls back to the table scan.
    """
    from boto3.dynamodb.conditions import Key
    table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    references: List[Dict] = []
    kwargs = {
        'IndexName': NODE_TYPE_REFS_INDEX,
        'KeyConditionExpression': Key(NODE_TYPE_REF_ATTRIBUTE).eq(node_type_id),
    }
    while True:
        response = table.query(**kwargs)
        for item in response.get('Items', []):
            item = decimal_to_native(item)
            references.append({'workflow_id': item.get('workflow_id'),
                               'version': item.get('version')})
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return references


def scan_workflow_references(node_type_id: str) -> List[Dict]:
    """Fallback reference scan over the whole WORKFLOW_VERSIONS_TABLE"""
    table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    references: List[Dict] = []
    kwargs: Dict = {}
    while True:
        response = table.scan(**kwargs)
        for item in response.get('Items', []):
            item = decimal_to_native(item)
            if item_references_node_type(item, node_type_id,
                                         load_workflow_definition):
                references.append({'workflow_id': item.get('workflow_id'),
                                   'version': item.get('version')})
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last
    return references


def find_workflow_references(node_type_id: str) -> List[Dict]:
    """
    Saved workflow versions referencing a Custom_Node_Type (14.4/14.5),
    as [{workflow_id, version}].

    Tries the optional inverted-index GSI first; task 9.2 does not create
    it (see NODE_TYPE_REFS_INDEX above), so the scan normally falls back
    to a full WorkflowVersions scan. Versions saved after task 9.2 carry
    the `custom_node_types` map recorded by workflows.py, so the fallback
    decides membership from the item alone; only pre-9.2 items require
    loading the stored definition document.
    """
    try:
        return query_reference_index(node_type_id)
    except ClientError as e:
        logger.info(f"Reference index '{NODE_TYPE_REFS_INDEX}' unavailable "
                    f"({e.response.get('Error', {}).get('Code', 'error')}); "
                    f"falling back to a WorkflowVersions scan")
        return scan_workflow_references(node_type_id)


# --------------------------------------------------------- removal effects

def delete_plugin_library_artifacts(plugin_id: str) -> List[str]:
    """
    Delete every Plugin_Library artifact of the backing plugin (14.4):
    each recorded per-arch .so under workflow-plugins/custom/{usecase}/...
    plus its detached .sig, across all Plugin_Record versions.
    """
    deleted: List[str] = []
    for record in query_plugin_versions(plugin_id):
        artifacts = record.get('artifacts') or {}
        for entry in artifacts.values():
            if not isinstance(entry, dict):
                continue
            so_key = entry.get('s3Key')
            if not so_key:
                continue
            for key in (so_key, signature_key(so_key)):
                try:
                    s3.delete_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key)
                    deleted.append(key)
                except ClientError as e:
                    logger.warning(f"Could not delete Plugin_Library object "
                                   f"{key}: {str(e)}")
    return deleted


def delete_plugin_component_versions(usecase_id: str, plugin_id: str) -> None:
    """
    Delete the plugin's Plugin_Component versions from the Use_Case
    account Greengrass registry and its component artifacts from the
    account bucket (14.4). Best-effort: registry/account failures are
    logged and never abort the catalog removal (the catalog and
    Plugin_Library deletions are the authoritative part of 14.4).
    """
    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        logger.warning(f"Use case '{usecase_id}' not found; skipping "
                       f"Plugin_Component cleanup for plugin {plugin_id}")
        return

    session_name = f"node-type-rm-{plugin_id[:24]}-{now_ms() // 1000}"[:64]
    component_name = component_name_for(plugin_id)

    try:
        greengrass = get_usecase_client('greengrassv2', usecase,
                                        session_name=session_name)
        region = getattr(getattr(greengrass, 'meta', None), 'region_name', None) \
            or os.environ.get('AWS_REGION', 'us-east-1')
        component_arn = (f"arn:aws:greengrass:{region}:"
                         f"{usecase.get('account_id')}:components:{component_name}")
        kwargs = {'arn': component_arn}
        while True:
            response = greengrass.list_component_versions(**kwargs)
            for version in response.get('componentVersions', []):
                version_arn = version.get('arn')
                if not version_arn:
                    continue
                try:
                    greengrass.delete_component(arn=version_arn)
                except ClientError as e:
                    logger.warning(f"Could not delete Plugin_Component "
                                   f"version {version_arn}: {str(e)}")
            token = response.get('nextToken')
            if not token:
                break
            kwargs['nextToken'] = token
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code not in ('ResourceNotFoundException', '404'):
            logger.warning(f"Could not enumerate Plugin_Component versions of "
                           f"{component_name}: {str(e)}")

    bucket = usecase.get('s3_bucket')
    if bucket:
        try:
            usecase_s3 = get_usecase_client('s3', usecase,
                                            session_name=session_name)
            delete_prefix(usecase_s3, bucket,
                          f"{COMPONENT_S3_PREFIX}/{plugin_id}/")
        except ClientError as e:
            logger.warning(f"Could not clean up account component artifacts "
                           f"of plugin {plugin_id}: {str(e)}")


# ------------------------------------------------------------ authorization

def not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a Custom_Node_Type exists"""
    return error_response(404, 'NODE_TYPE_NOT_FOUND', 'Custom node type not found')


def forbidden_response(user: Dict, event: Dict, usecase_id: str,
                       required: Permission) -> Dict:
    """Standard authorization error envelope with a denied-access audit
    entry (13.4)"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='custom_node_type',
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


def can_read_node_type(user: Dict, item: Dict) -> bool:
    """Read access via the owning Use_Case or any scoped Use_Case (13.3)"""
    usecase_ids = [item['usecase_id']] + [
        uc for uc in (item.get('usecase_ids') or []) if uc != item['usecase_id']
    ]
    return any(
        has_node_designer_permission(user, uc, Permission.NODE_DESIGNER_READ)
        for uc in usecase_ids
    )


def authorize_node_type_access(user: Dict, event: Dict, item: Dict,
                               manage: bool = False,
                               permission: Permission = Permission.NODE_DESIGNER_MANAGE
                               ) -> Optional[Dict]:
    """
    Authorize an operation on an existing Custom_Node_Type. Returns an
    error response, or None when authorized. A user without any
    resolvable read access receives the same 404 as for a missing type so
    existence is never leaked across tenants; manage operations require
    the permission on the owning Use_Case (13.1).
    """
    if not can_read_node_type(user, item):
        return not_found_response()
    if manage and not has_node_designer_permission(user, item['usecase_id'],
                                                   permission):
        return forbidden_response(user, event, item['usecase_id'], permission)
    return None


# ------------------------------------------------------ declaration checks

def prepare_declaration(declaration: Dict, record: Dict
                        ) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Normalize and validate a submitted wire declaration against the
    backing Plugin_Record version.

    Stamps the record's DeepStream flag (5.3 restriction enforced by
    descriptor_from_declaration), records the plugin dependency
    ``custom:{usecase_id}/{plugin_name}`` in every mapping (8.6), then
    validates through descriptor_from_declaration, rejecting invalid
    declarations with the offending field identified (8.5), and rejects
    mappings that target Target_Architectures without a successfully
    built Plugin_Artifact (8.1).

    Returns (normalized_declaration, None) or (None, error_response).
    """
    plugin_name = sanitize_plugin_name(record.get('name'), record['plugin_id'])
    dependency = plugin_dependency_for(record['usecase_id'], plugin_name)

    declaration = inject_plugin_dependency(declaration, dependency)
    declaration['deepstream'] = bool(record.get('deepstream', False))

    try:
        descriptor_from_declaration(declaration)
    except DeclarationError as e:
        return None, error_response(400, 'INVALID_DECLARATION', str(e), {
            'field': e.field,
        })

    mappings = declaration.get('mappings') or []
    if not mappings:
        return None, error_response(
            400, 'INVALID_DECLARATION',
            'mappings: must declare the element/property mapping for at '
            'least one built Target_Architecture',
            {'field': 'mappings'})

    built = successful_build_archs(record)
    offending = unbuilt_mapping_architectures(declaration, built)
    if offending:
        field, arch = offending[0]
        return None, error_response(
            400, 'UNBUILT_ARCHITECTURE',
            f"{field}: architecture {arch!r} has no successfully built "
            f"Plugin_Artifact on Plugin_Record "
            f"{record['plugin_id']} v{record['version']}",
            {
                'field': field,
                'offending_mappings': [
                    {'field': f, 'arch': a} for f, a in offending
                ],
                'built_architectures': built,
            })

    return declaration, None


def parse_usecase_ids(body: Dict, default_usecase_id: str
                      ) -> Tuple[Optional[List[str]], Optional[Dict]]:
    """Use_Case scoping selected at registration (8.2); defaults to the
    plugin's own Use_Case"""
    usecase_ids = body.get('usecase_ids')
    if usecase_ids is None:
        return [default_usecase_id], None
    if (not isinstance(usecase_ids, list) or not usecase_ids
            or not all(isinstance(uc, str) and uc for uc in usecase_ids)):
        return None, error_response(
            400, 'INVALID_USECASE_IDS',
            'usecase_ids must be a non-empty list of use case id strings')
    return list(dict.fromkeys(usecase_ids)), None


# ----------------------------------------------------------------- handlers

def list_node_types(event: Dict, user: Dict) -> Dict:
    """
    GET /custom-node-types?plugin_id=...
    Latest version of every Custom_Node_Type backed by the plugin.

    Serves the registration wizard's duplicate detection: a plugin that
    already backs a node type is offered an update (a new version of
    the existing registration) instead of registering a duplicate
    palette entry. Readable with node-designer:read on the plugin's
    Use_Case.
    """
    params = event.get('queryStringParameters') or {}
    plugin_id = params.get('plugin_id')
    if not plugin_id:
        return error_response(400, 'MISSING_PLUGIN_ID',
                              'plugin_id query parameter is required')

    record = get_latest_plugin_version_item(plugin_id)
    if not record:
        return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')
    if not has_node_designer_permission(user, record['usecase_id'],
                                        Permission.NODE_DESIGNER_READ):
        # Same 404 as a missing record: existence never leaks cross-tenant.
        return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')

    from boto3.dynamodb.conditions import Attr
    items: List[Dict] = []
    kwargs: Dict = {'FilterExpression': Attr('plugin_id').eq(plugin_id)}
    while True:
        response = node_types_table().scan(**kwargs)
        items.extend(response.get('Items', []))
        last = response.get('LastEvaluatedKey')
        if not last:
            break
        kwargs['ExclusiveStartKey'] = last

    # Latest version per node_type_id (14.1 retains every version).
    latest: Dict[str, Dict] = {}
    for item in items:
        type_id = item.get('node_type_id')
        current = latest.get(type_id)
        if current is None or int(item.get('version', 0)) > int(current.get('version', 0)):
            latest[type_id] = item

    node_types = [node_type_summary(latest[type_id])
                  for type_id in sorted(latest)]
    return create_response(200, {'nodeTypes': node_types,
                                 'count': len(node_types)})


def register_node_type(event: Dict, user: Dict) -> Dict:
    """
    POST /custom-node-types
    Body: {plugin_id, plugin_version?, declaration, usecase_ids?}

    Registers a Custom_Node_Type for a built Plugin_Record version (8.1):
    the declaration collects the display name, palette category, Ports
    with types, parameters (types, defaults, constraints, descriptions,
    examples), the hardware-dependence flag, and the element/property
    mapping per built Target_Architecture. Requires
    node-designer:register on the plugin's Use_Case.
    """
    body, err = parse_body(event)
    if err:
        return err

    plugin_id = body.get('plugin_id')
    declaration = body.get('declaration')
    missing = [f for f in ('plugin_id', 'declaration') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")
    if not isinstance(declaration, dict):
        return error_response(400, 'INVALID_DECLARATION',
                              'declaration must be a JSON object',
                              {'field': 'declaration'})

    if body.get('plugin_version') is not None:
        try:
            plugin_version = int(body['plugin_version'])
        except (TypeError, ValueError):
            return error_response(400, 'INVALID_PLUGIN_VERSION',
                                  'plugin_version must be an integer')
        record = get_plugin_version_item(plugin_id, plugin_version)
    else:
        record = get_latest_plugin_version_item(plugin_id)

    if not record:
        return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')

    usecase_id = record['usecase_id']
    if not has_node_designer_permission(user, usecase_id,
                                        Permission.NODE_DESIGNER_READ):
        # Same 404 as a missing record: existence never leaks cross-tenant.
        return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')
    if not has_node_designer_permission(user, usecase_id,
                                        Permission.NODE_DESIGNER_REGISTER):
        return forbidden_response(user, event, usecase_id,
                                  Permission.NODE_DESIGNER_REGISTER)

    usecase_ids, err = parse_usecase_ids(body, usecase_id)
    if err:
        return err

    declaration, err = prepare_declaration(declaration, record)
    if err:
        return err

    node_type_id = declaration['typeId']
    if node_type_id in BUILTIN_TYPE_IDS:
        return error_response(
            409, 'TYPE_ID_CONFLICT',
            f"typeId {node_type_id!r} collides with a built-in node type",
            {'field': 'typeId', 'node_type_id': node_type_id})
    if query_node_type_versions(node_type_id):
        return error_response(
            409, 'TYPE_ID_CONFLICT',
            f"Custom node type {node_type_id!r} is already registered; "
            f"update it to create a new version",
            {'field': 'typeId', 'node_type_id': node_type_id})

    timestamp = now_ms()
    item = new_node_type_item(
        node_type_id=node_type_id, version=1, usecase_id=usecase_id,
        usecase_ids=usecase_ids, plugin_id=plugin_id,
        plugin_version=record['version'], declaration=declaration,
        user_id=user['user_id'], timestamp=timestamp,
    )
    node_types_table().put_item(
        Item=to_dynamo_json(item),
        ConditionExpression='attribute_not_exists(node_type_id)',
    )

    log_audit_event(
        user_id=user['user_id'],
        action='register_custom_node_type',
        resource_type='custom_node_type',
        resource_id=node_type_id,
        result='success',
        details={'usecase_id': usecase_id, 'usecase_ids': usecase_ids,
                 'plugin_id': plugin_id, 'plugin_version': record['version'],
                 'version': 1}
    )

    return create_response(201, {'nodeType': node_type_detail(item)})


def get_node_type(event: Dict, user: Dict, node_type_id: str) -> Dict:
    """
    GET /custom-node-types/{id}
    Latest version detail plus the retained version history (14.1).
    """
    versions = query_node_type_versions(node_type_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_node_type_access(user, event, latest)
    if err:
        return err
    return create_response(200, {
        'nodeType': node_type_detail(latest),
        'versions': [node_type_summary(item) for item in versions],
    })


def update_node_type(event: Dict, user: Dict, node_type_id: str) -> Dict:
    """
    PUT /custom-node-types/{id}
    Body: {declaration?, plugin_version?, usecase_ids?}

    A declaration update creates a new CustomNodeTypes version item and
    retains every prior version (14.1). The new version pins the
    (possibly updated) backing Plugin_Record version. Requires
    node-designer:manage on the owning Use_Case.
    """
    body, err = parse_body(event)
    if err:
        return err

    versions = query_node_type_versions(node_type_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_node_type_access(user, event, latest, manage=True)
    if err:
        return err

    if not any(f in body for f in ('declaration', 'plugin_version', 'usecase_ids')):
        return error_response(400, 'NO_UPDATES',
                              'Provide declaration, plugin_version, or usecase_ids')

    declaration = body.get('declaration')
    if declaration is None:
        declaration = latest.get('declaration') or {}
    if not isinstance(declaration, dict):
        return error_response(400, 'INVALID_DECLARATION',
                              'declaration must be a JSON object',
                              {'field': 'declaration'})

    plugin_id = latest['plugin_id']
    plugin_version = body.get('plugin_version', latest.get('plugin_version'))
    try:
        plugin_version = int(plugin_version)
    except (TypeError, ValueError):
        return error_response(400, 'INVALID_PLUGIN_VERSION',
                              'plugin_version must be an integer')
    record = get_plugin_version_item(plugin_id, plugin_version)
    if not record:
        return error_response(404, 'PLUGIN_NOT_FOUND',
                              'Backing plugin record version not found',
                              {'plugin_id': plugin_id,
                               'plugin_version': plugin_version})

    if 'usecase_ids' in body:
        usecase_ids, err = parse_usecase_ids(body, latest['usecase_id'])
        if err:
            return err
    else:
        usecase_ids = latest.get('usecase_ids') or [latest['usecase_id']]

    declaration, err = prepare_declaration(declaration, record)
    if err:
        return err
    if declaration['typeId'] != node_type_id:
        return error_response(
            400, 'TYPE_ID_MISMATCH',
            f"typeId {declaration['typeId']!r} must match the registered "
            f"custom node type id {node_type_id!r}",
            {'field': 'typeId'})

    timestamp = now_ms()
    item = new_node_type_item(
        node_type_id=node_type_id,
        version=next_node_type_version(latest['version']),
        usecase_id=latest['usecase_id'],
        usecase_ids=usecase_ids,
        plugin_id=plugin_id,
        plugin_version=record['version'],
        declaration=declaration,
        user_id=user['user_id'],
        timestamp=timestamp,
        deprecated=latest.get('deprecated', False),
    )
    node_types_table().put_item(
        Item=to_dynamo_json(item),
        ConditionExpression='attribute_not_exists(version)',
    )

    log_audit_event(
        user_id=user['user_id'],
        action='update_custom_node_type',
        resource_type='custom_node_type',
        resource_id=node_type_id,
        result='success',
        details={'usecase_id': latest['usecase_id'], 'version': item['version'],
                 'plugin_id': plugin_id, 'plugin_version': record['version']}
    )

    return create_response(201, {'nodeType': node_type_detail(item)})


def deprecate_node_type(event: Dict, user: Dict, node_type_id: str) -> Dict:
    """
    POST /custom-node-types/{id}/deprecate
    Body: {deprecated?: bool} (default true)

    Flips the deprecated flag on every version item (14.3): the palette
    stops offering the type for new placement while saved workflows
    referencing it remain loadable, packagable, and deployable (the merge
    in task 9.2 excludes deprecated types from the palette only).
    Requires node-designer:manage on the owning Use_Case.
    """
    body, err = parse_body(event)
    if err:
        return err

    versions = query_node_type_versions(node_type_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_node_type_access(user, event, latest, manage=True)
    if err:
        return err

    deprecated = body.get('deprecated', True)
    if not isinstance(deprecated, bool):
        return error_response(400, 'INVALID_DEPRECATED',
                              'deprecated must be a boolean')

    timestamp = now_ms()
    for item in versions:
        node_types_table().update_item(
            Key={'node_type_id': node_type_id, 'version': item['version']},
            UpdateExpression='SET deprecated = :d, updated_at = :t',
            ExpressionAttributeValues={':d': deprecated, ':t': timestamp},
        )

    log_audit_event(
        user_id=user['user_id'],
        action='deprecate_custom_node_type',
        resource_type='custom_node_type',
        resource_id=node_type_id,
        result='success',
        details={'usecase_id': latest['usecase_id'], 'deprecated': deprecated,
                 'versions': [item['version'] for item in versions]}
    )

    updated = query_node_type_versions(node_type_id)
    return create_response(200, {
        'nodeType': node_type_detail(updated[0]),
        'versions': [node_type_summary(item) for item in updated],
    })


def remove_node_type(event: Dict, user: Dict, node_type_id: str) -> Dict:
    """
    DELETE /custom-node-types/{id}

    Reference-checked removal (14.4, 14.5): scans WorkflowVersions for
    references (inverted-index GSI when available, full scan otherwise).
    Zero references deletes every catalog version item, the plugin's
    Plugin_Library artifacts, and the plugin's Plugin_Component versions;
    otherwise the removal is rejected listing the referencing workflows.
    Requires node-designer:manage on the owning Use_Case.
    """
    versions = query_node_type_versions(node_type_id)
    if not versions:
        return not_found_response()
    latest = versions[0]
    err = authorize_node_type_access(user, event, latest, manage=True)
    if err:
        return err

    references = find_workflow_references(node_type_id)
    rejection = evaluate_removal(node_type_id, references)
    if rejection:
        log_audit_event(
            user_id=user['user_id'],
            action='remove_custom_node_type',
            resource_type='custom_node_type',
            resource_id=node_type_id,
            result='denied',
            details={'usecase_id': latest['usecase_id'],
                     'referencing_workflows': references}
        )
        return error_response(409, rejection['code'], rejection['message'],
                              rejection['details'])

    plugin_id = latest['plugin_id']

    # Plugin_Component versions in the Use_Case account registry (+ the
    # account-bucket component artifacts), then the portal Plugin_Library
    # artifacts, then the catalog items last so a partial failure leaves
    # the type visible and the removal retryable.
    delete_plugin_component_versions(latest['usecase_id'], plugin_id)
    deleted_artifacts = delete_plugin_library_artifacts(plugin_id)

    for item in versions:
        node_types_table().delete_item(
            Key={'node_type_id': node_type_id, 'version': item['version']})

    log_audit_event(
        user_id=user['user_id'],
        action='remove_custom_node_type',
        resource_type='custom_node_type',
        resource_id=node_type_id,
        result='success',
        details={'usecase_id': latest['usecase_id'], 'plugin_id': plugin_id,
                 'versions_removed': [item['version'] for item in versions],
                 'artifacts_deleted': deleted_artifacts}
    )

    return create_response(200, {
        'removed': True,
        'node_type_id': node_type_id,
        'versions_removed': [item['version'] for item in versions],
    })


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
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}
        node_type_id = path_params.get('id')

        if resource == '/custom-node-types':
            if http_method == 'GET':
                return list_node_types(event, user)
            if http_method == 'POST':
                return register_node_type(event, user)
        elif resource == '/custom-node-types/{id}' and node_type_id:
            if http_method == 'GET':
                return get_node_type(event, user, node_type_id)
            if http_method == 'PUT':
                return update_node_type(event, user, node_type_id)
            if http_method == 'DELETE':
                return remove_node_type(event, user, node_type_id)
        elif resource == '/custom-node-types/{id}/deprecate' and node_type_id:
            if http_method == 'POST':
                return deprecate_node_type(event, user, node_type_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
