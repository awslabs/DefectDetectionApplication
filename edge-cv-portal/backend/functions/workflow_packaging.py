"""
Component_Packager Lambda function (Workflow Manager)

Compiles a validated workflow version for the user-selected device
architectures, assembles per-arch Workflow_Component artifacts, uploads
them to the Use_Case account S3 bucket via the assumed cross-account
role, and registers a Greengrass component named
``dda.workflow.{workflowId}`` with version ``{workflowVersion}.0.0``
(Requirements 7.1-7.5, 11.5, 13.3).

Routes (API Gateway REST):
    POST /workflows/{id}/package
        Body: {"architectures": ["x86_64", ...], "version": N?}

Per-arch artifact zip layout (discovered by LocalServer under
/aws_dda/workflows/{workflowId}/{version}/ — Requirement 13.3):
    manifest.json                     component + workflow metadata
    workflow.json                     the Workflow_Definition
    compiled_pipeline.json            Workflow_Compiler output for the arch
    plugins/{arch}/{plugin}.so        curated plugin library artifacts (7.1)
    python/{nodeId}/handler.py        Custom_Python_Node code (7.3)
    python/{nodeId}/requirements.txt  Custom_Python_Node dependencies (7.3)

All-or-nothing staging (Requirement 7.5): every artifact is uploaded to a
temporary staging prefix first; only after every artifact for every
selected architecture uploads successfully are the objects promoted to
the final prefix and the component registered. On any failure the stage
(and any promoted objects) are deleted, the failing artifact is reported,
and no component version is registered.

The recipe is install-only — no Run lifecycle — so deploying or removing
a Workflow_Component never restarts LocalServer or any other component
(Requirement 13.3).
"""
import json
import os
import logging
import shutil
import tempfile
import time
import uuid
import zipfile
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
    get_usecase, get_usecase_client, rbac_manager, Permission
)
from workflow_core.serializer import parse as parse_definition
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (portal account)
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')
# Curated GStreamer plugin artifact library in portal S3:
#   {WORKFLOW_PLUGIN_LIBRARY_PREFIX}/{arch}/{plugin}.so
WORKFLOW_PLUGIN_LIBRARY_PREFIX = os.environ.get(
    'WORKFLOW_PLUGIN_LIBRARY_PREFIX', 'workflow-plugins')
# Minimum LocalServer component version a Workflow_Component requires
# (surfaced in manifest.json for the deployment compatibility check, 8.4)
MIN_LOCAL_SERVER_VERSION = os.environ.get(
    'WORKFLOW_MIN_LOCAL_SERVER_VERSION',
    os.environ.get('DDA_LOCAL_SERVER_VERSION', '1.0.0'))

# Greengrass component naming (design section 6)
WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.'
COMPONENT_PUBLISHER = 'DDA Portal Workflow Manager'

# Use_Case account S3 prefixes for Workflow_Component artifacts
COMPONENT_S3_PREFIX = 'workflows/components'
STAGING_S3_PREFIX = 'workflows/staging'

# arch id (workflow_core) -> Greengrass platform architecture
ARCH_TO_GG_PLATFORM = {
    'x86_64': 'amd64',
    'arm64_jp4': 'aarch64',
    'arm64_jp5': 'aarch64',
    'arm64_jp6': 'aarch64',
}

# Where the LocalServer workflow engine discovers artifacts (13.3)
DEVICE_WORKFLOWS_ROOT = '/aws_dda/workflows'

# Polling for the registered component to become DEPLOYABLE
COMPONENT_STATUS_MAX_ATTEMPTS = 30
COMPONENT_STATUS_POLL_SECONDS = 2


class PackagingError(Exception):
    """A packaging failure attributable to one artifact (Requirement 7.5)."""

    def __init__(self, artifact: str, message: str):
        super().__init__(message)
        self.artifact = artifact
        self.message = message


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

    Returns an error response, or None when authorized. Mirrors
    workflows.py: a user without read access to the owning Use_Case gets
    the same 404 as for a missing workflow (no existence leak); a user
    who can read but lacks the operation permission gets a 403.
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
    """Fetch a workflow version item, or None"""
    table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = table.get_item(Key={'workflow_id': workflow_id, 'version': version})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def validation_guard(version_item: Dict) -> Optional[Dict]:
    """
    Reject packaging unless the workflow version has a recorded passed
    Workflow_Validator run with zero errors (Requirements 4.7, 4.10).

    A version that was validated and failed gets 400 with the findings
    reference; a version never validated (or with a stale record) gets
    409 asking the user to validate first.
    """
    validation_status = version_item.get('validation_status') or {}
    status = validation_status.get('status')
    if status == 'passed':
        return None
    if status == 'failed':
        return error_response(
            400, 'VALIDATION_FAILED',
            'Workflow version has validation errors and cannot be packaged',
            {
                'version': version_item.get('version'),
                'findings_key': validation_status.get('findings_key'),
                'validated_at': validation_status.get('validated_at')
            }
        )
    return error_response(
        409, 'VALIDATION_REQUIRED',
        'Workflow version has no passed validation record; run validation before packaging',
        {'version': version_item.get('version')}
    )


def load_definition(s3_key: str) -> str:
    """Load a stored Workflow_Definition document (raw JSON) from portal S3"""
    response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
    return response['Body'].read().decode('utf-8')


def compiled_doc_portal_key(usecase_id: str, workflow_id: str, version: int, arch: str) -> str:
    """Portal S3 key of a compiled pipeline document (data model: compiled docs)"""
    return (f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/versions/"
            f"{version}/compiled/{arch}.json")


def plugin_library_key(arch: str, plugin: str) -> str:
    """Portal S3 key of a curated plugin library artifact"""
    return f"{WORKFLOW_PLUGIN_LIBRARY_PREFIX}/{arch}/{plugin}.so"


def component_name_for(workflow_id: str) -> str:
    return f"{WORKFLOW_COMPONENT_PREFIX}{workflow_id}"


def component_version_for(workflow_version: int) -> str:
    """Component version derived from the workflow version (Requirement 7.2)"""
    return f"{int(workflow_version)}.0.0"


def zip_artifact_name(arch: str) -> str:
    return f"workflow-{arch}.zip"


# --------------------------------------------------------------------------
# Artifact assembly
# --------------------------------------------------------------------------

def gather_custom_python_nodes(graph) -> List[Dict]:
    """Custom_Python_Nodes whose code + declared dependencies ship in the
    Workflow_Component artifacts (Requirement 7.3)"""
    nodes = []
    for node in graph.nodes:
        if node.type == 'custom_python':
            nodes.append({
                'node_id': node.id,
                'code': str(node.parameters.get('code') or ''),
                'requirements': str(node.parameters.get('requirements') or '')
            })
    return nodes


def split_plugin_dependencies(plugin_dependencies: List[str]) -> Tuple[List[str], List[str]]:
    """Split compiler pluginDependencies into GStreamer plugin names
    (packaged as plugins/{arch}/*.so) and python: runtime packages
    (surfaced in manifest.json for the edge executor)."""
    gst_plugins, python_packages = [], []
    for dep in plugin_dependencies:
        if dep.startswith('python:'):
            python_packages.append(dep[len('python:'):])
        else:
            gst_plugins.append(dep)
    return sorted(gst_plugins), sorted(python_packages)


def build_manifest(workflow_id: str, workflow_version: int, arch: str,
                   gst_plugins: List[str], python_packages: List[str],
                   custom_python_nodes: List[Dict], user: Dict) -> Dict:
    """manifest.json content: what WorkflowWatcher needs to register the
    workflow and what the deployment compatibility check reads (8.4)"""
    return {
        'componentName': component_name_for(workflow_id),
        'componentVersion': component_version_for(workflow_version),
        'workflowId': workflow_id,
        'workflowVersion': int(workflow_version),
        'targetArch': arch,
        'minLocalServerVersion': MIN_LOCAL_SERVER_VERSION,
        'pluginDependencies': gst_plugins,
        'pythonDependencies': python_packages,
        'customPythonNodeIds': [n['node_id'] for n in custom_python_nodes],
        'packagedAt': now_ms(),
        'packagedBy': user['user_id']
    }


def build_arch_zip(zip_path: str, arch: str, manifest: Dict, definition_json: str,
                   compiled_json: str, gst_plugins: List[str],
                   custom_python_nodes: List[Dict]) -> None:
    """
    Assemble one architecture's artifact zip on local disk (Requirement 7.1).

    Plugin binaries are resolved from the curated plugin library in portal
    S3; a missing or unreadable plugin artifact fails packaging with that
    artifact identified (Requirement 7.5).
    """
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, sort_keys=True, indent=2))
        zf.writestr('workflow.json', definition_json)
        zf.writestr('compiled_pipeline.json', compiled_json)

        for plugin in gst_plugins:
            library_key = plugin_library_key(arch, plugin)
            artifact_label = f"plugins/{arch}/{plugin}.so"
            try:
                response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=library_key)
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code', '')
                if code in ('NoSuchKey', '404'):
                    raise PackagingError(
                        artifact_label,
                        f"Plugin artifact '{plugin}' for architecture '{arch}' was not "
                        f"found in the plugin library (s3://{PORTAL_ARTIFACTS_BUCKET}/{library_key})")
                raise PackagingError(
                    artifact_label,
                    f"Plugin artifact '{plugin}' for architecture '{arch}' could not be "
                    f"read from the plugin library: {code or str(e)}")
            # Stream the .so into the zip without holding it all in memory
            with zf.open(artifact_label, 'w') as target:
                body = response['Body']
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)

        for node in custom_python_nodes:
            zf.writestr(f"python/{node['node_id']}/handler.py", node['code'])
            zf.writestr(f"python/{node['node_id']}/requirements.txt", node['requirements'])


# --------------------------------------------------------------------------
# Use_Case account S3 staging (all-or-nothing, Requirement 7.5)
# --------------------------------------------------------------------------

def delete_prefix(s3_client, bucket: str, prefix: str) -> None:
    """Best-effort delete of every object under a prefix (stage cleanup)"""
    try:
        continuation_token = None
        while True:
            kwargs = {'Bucket': bucket, 'Prefix': prefix}
            if continuation_token:
                kwargs['ContinuationToken'] = continuation_token
            listed = s3_client.list_objects_v2(**kwargs)
            objects = [{'Key': o['Key']} for o in listed.get('Contents', [])]
            if objects:
                s3_client.delete_objects(Bucket=bucket, Delete={'Objects': objects})
            if not listed.get('IsTruncated'):
                break
            continuation_token = listed.get('NextContinuationToken')
    except ClientError as e:
        # Cleanup must never mask the original failure
        logger.error(f"Error cleaning s3://{bucket}/{prefix}: {str(e)}")


def stage_and_promote_artifacts(usecase_s3, bucket: str, workflow_id: str,
                                workflow_version: int, arch_zip_paths: Dict[str, str]
                                ) -> Tuple[str, Dict[str, str]]:
    """
    Upload every arch zip to a temporary staging prefix, then promote all
    of them to the final component prefix (Requirement 7.5).

    Returns (final_prefix, {arch: final_key}). Raises PackagingError with
    the failing artifact identified; the caller cleans up.
    """
    stage_id = uuid.uuid4().hex
    stage_prefix = f"{STAGING_S3_PREFIX}/{workflow_id}/{workflow_version}/{stage_id}"
    final_prefix = f"{COMPONENT_S3_PREFIX}/{workflow_id}/{workflow_version}"

    staged_keys: Dict[str, str] = {}
    for arch, zip_path in arch_zip_paths.items():
        artifact_label = f"{arch}/{zip_artifact_name(arch)}"
        stage_key = f"{stage_prefix}/{artifact_label}"
        try:
            with open(zip_path, 'rb') as fh:
                usecase_s3.put_object(Bucket=bucket, Key=stage_key, Body=fh,
                                      ContentType='application/zip')
        except (ClientError, OSError) as e:
            raise PackagingError(
                artifact_label,
                f"Failed to upload artifact '{artifact_label}' to the staging area: {str(e)}")
        staged_keys[arch] = stage_key

    # Every artifact staged successfully -> promote to the final prefix.
    final_keys: Dict[str, str] = {}
    for arch, stage_key in staged_keys.items():
        artifact_label = f"{arch}/{zip_artifact_name(arch)}"
        final_key = f"{final_prefix}/{artifact_label}"
        try:
            usecase_s3.copy_object(Bucket=bucket, Key=final_key,
                                   CopySource={'Bucket': bucket, 'Key': stage_key})
        except ClientError as e:
            raise PackagingError(
                artifact_label,
                f"Failed to promote artifact '{artifact_label}' from the staging area: {str(e)}")
        final_keys[arch] = final_key

    return final_prefix, final_keys


# --------------------------------------------------------------------------
# Greengrass component registration (Requirements 7.2, 13.3)
# --------------------------------------------------------------------------

def build_recipe(workflow_id: str, workflow_version: int, bucket: str,
                 final_keys: Dict[str, str]) -> Dict:
    """
    Install-only Greengrass recipe with one platform manifest per selected
    architecture (Requirements 7.2, 7.4). There is deliberately no Run
    lifecycle: the component installs its artifacts under
    /aws_dda/workflows/ and finishes, so deploying or removing it never
    disturbs LocalServer or any other component (Requirement 13.3) — the
    LocalServer workflow engine discovers the files at runtime.
    """
    component_name = component_name_for(workflow_id)
    component_version = component_version_for(workflow_version)
    install_dir = f"{DEVICE_WORKFLOWS_ROOT}/{workflow_id}/{workflow_version}"

    # Greengrass matches manifests on platform attributes. amd64 vs aarch64
    # separates x86_64 from the Jetson builds; when more than one arm64
    # JetPack variant is packaged, a custom 'variant' attribute (declared
    # in the device's Nucleus platform overrides) disambiguates them.
    arm_archs = [a for a in final_keys if ARCH_TO_GG_PLATFORM.get(a) == 'aarch64']
    disambiguate_arm = len(arm_archs) > 1

    manifests = []
    for arch in sorted(final_keys):
        platform = {'os': 'linux', 'architecture': ARCH_TO_GG_PLATFORM[arch]}
        if disambiguate_arm and ARCH_TO_GG_PLATFORM[arch] == 'aarch64':
            platform['variant'] = arch
        unarchived_dir = zip_artifact_name(arch)[:-len('.zip')]
        install_script = (
            f"mkdir -p {install_dir} && "
            f"cp -r {{artifacts:decompressedPath}}/{unarchived_dir}/. {install_dir}/"
        )
        manifests.append({
            'Platform': platform,
            'Lifecycle': {
                'Install': {
                    'Script': install_script,
                    'Timeout': 300,
                    'requiresPrivilege': True
                }
            },
            'Artifacts': [
                {
                    'Uri': f"s3://{bucket}/{final_keys[arch]}",
                    'Unarchive': 'ZIP',
                    'Permission': {
                        'Read': 'ALL'
                    }
                }
            ]
        })

    return {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': component_version,
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': COMPONENT_PUBLISHER,
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'WorkflowId': workflow_id,
                'WorkflowVersion': str(workflow_version)
            }
        },
        'Manifests': manifests,
        'Lifecycle': {}
    }


def register_component(greengrass, recipe: Dict, usecase_id: str,
                       workflow_id: str, workflow_version: int, user: Dict) -> str:
    """
    Register the component version in the Use_Case account Greengrass
    registry and wait until it is DEPLOYABLE (Requirement 7.2). A version
    that fails to become DEPLOYABLE is deleted so no partial or broken
    component version remains (Requirement 7.5).
    """
    component_label = (f"component {recipe['ComponentName']} "
                       f"v{recipe['ComponentVersion']}")
    try:
        response = greengrass.create_component_version(
            inlineRecipe=json.dumps(recipe),
            tags={
                'dda-portal:managed': 'true',
                'dda-portal:usecase-id': usecase_id,
                'dda-portal:workflow-id': workflow_id,
                'dda-portal:workflow-version': str(workflow_version),
                'dda-portal:created-by': user['user_id']
            }
        )
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code == 'ConflictException':
            raise PackagingError(
                component_label,
                f"Component version {recipe['ComponentVersion']} already exists for "
                f"{recipe['ComponentName']}")
        raise PackagingError(component_label,
                             f"Component registration failed: {str(e)}")

    component_arn = response['arn']
    component_status = 'REQUESTED'
    status_message = ''
    for _ in range(COMPONENT_STATUS_MAX_ATTEMPTS):
        if component_status not in ('REQUESTED', 'IN_PROGRESS'):
            break
        time.sleep(COMPONENT_STATUS_POLL_SECONDS)
        status_response = greengrass.describe_component(arn=component_arn)
        component_status = status_response['status']['componentState']
        status_message = status_response['status'].get('message', '')

    if component_status != 'DEPLOYABLE':
        # Remove the failed registration so nothing partial exists (7.5)
        try:
            greengrass.delete_component(arn=component_arn)
        except ClientError as e:
            logger.error(f"Error deleting failed component {component_arn}: {str(e)}")
        raise PackagingError(
            component_label,
            f"Component did not become DEPLOYABLE (state {component_status}): "
            f"{status_message or 'no status message'}")

    return component_arn


# --------------------------------------------------------------------------
# POST /workflows/{id}/package
# --------------------------------------------------------------------------

def package_workflow(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    Compile, assemble, upload, and register a Workflow_Component for the
    user-selected architectures (Requirements 7.1-7.5, 11.5, 13.3).

    Body: {"architectures": ["x86_64", "arm64_jp5", ...], "version": N?}
    version defaults to the workflow's latest version.
    """
    item = get_workflow_item(workflow_id)
    if not item:
        return not_found_response()
    err = authorize_workflow_access(user, event, item, Permission.WORKFLOW_PACKAGE)
    if err:
        return err

    body, err = parse_body(event)
    if err:
        return err

    architectures = body.get('architectures')
    if not architectures or not isinstance(architectures, list):
        return error_response(400, 'MISSING_FIELDS',
                              'architectures must be a non-empty list of target architectures',
                              {'supported_architectures': list(DEVICE_ARCHITECTURES)})
    architectures = list(dict.fromkeys(architectures))  # dedupe, keep order
    unsupported = [a for a in architectures if a not in DEVICE_ARCHITECTURES]
    if unsupported:
        return error_response(400, 'UNSUPPORTED_ARCHITECTURE',
                              f"Unsupported architectures: {', '.join(map(str, unsupported))}",
                              {'supported_architectures': list(DEVICE_ARCHITECTURES)})

    version_param = body.get('version', item.get('latest_version', 1))
    try:
        version = int(version_param)
    except (TypeError, ValueError):
        return error_response(400, 'INVALID_VERSION', 'version must be an integer')

    version_item = get_version_item(workflow_id, version)
    if not version_item:
        return error_response(404, 'VERSION_NOT_FOUND',
                              f'Version {version} not found for workflow')

    # Packaging requires a recorded passed validation (Requirements 4.7, 4.10)
    err = validation_guard(version_item)
    if err:
        return err

    usecase_id = item['usecase_id']
    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')
    usecase_bucket = usecase.get('s3_bucket')
    if not usecase_bucket:
        return error_response(500, 'USECASE_MISCONFIGURED',
                              'Use case has no S3 bucket configured for component artifacts')

    # Load and parse the stored Workflow_Definition
    try:
        definition_json = load_definition(version_item['s3_definition_key'])
    except ClientError as e:
        logger.error(f"Error loading definition for {workflow_id} v{version}: {str(e)}")
        return error_response(500, 'DEFINITION_LOAD_FAILED',
                              'Stored workflow definition could not be loaded')
    parse_result = parse_definition(definition_json)
    if not parse_result.ok:
        return error_response(400, parse_result.error.code, parse_result.error.message,
                              {'path': parse_result.error.path})
    graph = parse_result.graph

    # Compile once per user-selected architecture (Requirement 7.4)
    compile_context = CompileContext(workflow_id=workflow_id, workflow_version=str(version))
    compiled_docs: Dict[str, Any] = {}
    for arch in architectures:
        result = compile_workflow(graph, arch, compile_context, simulation=False)
        if isinstance(result, list):
            return error_response(400, 'COMPILATION_FAILED',
                                  f"Workflow failed to compile for architecture '{arch}'",
                                  {'arch': arch,
                                   'errors': [e.to_dict() for e in result]})
        compiled_docs[arch] = result

    custom_python_nodes = gather_custom_python_nodes(graph)

    # Assemble the per-arch artifact zips locally, then run the
    # all-or-nothing stage -> promote -> register sequence (7.1, 7.3, 7.5)
    work_dir = tempfile.mkdtemp(prefix='workflow-packaging-')
    usecase_s3 = None
    final_keys: Dict[str, str] = {}
    component_arn = None
    staging_root = f"{STAGING_S3_PREFIX}/{workflow_id}/{version}/"
    # Computed up front so failure cleanup can always delete any promoted
    # objects, even when promotion itself failed partway (Requirement 7.5).
    final_prefix = f"{COMPONENT_S3_PREFIX}/{workflow_id}/{version}"
    try:
        arch_zip_paths: Dict[str, str] = {}
        arch_plugins: Dict[str, List[str]] = {}
        for arch, compiled in compiled_docs.items():
            gst_plugins, python_packages = split_plugin_dependencies(
                compiled.plugin_dependencies)
            arch_plugins[arch] = gst_plugins
            manifest = build_manifest(workflow_id, version, arch, gst_plugins,
                                      python_packages, custom_python_nodes, user)
            zip_path = os.path.join(work_dir, zip_artifact_name(arch))
            build_arch_zip(zip_path, arch, manifest, definition_json,
                           compiled.to_json(), gst_plugins, custom_python_nodes)
            arch_zip_paths[arch] = zip_path

        # Use_Case account clients via the assumed cross-account role (7.2)
        session_name = f"wf-pkg-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        usecase_s3 = get_usecase_client('s3', usecase, session_name=session_name)
        greengrass = get_usecase_client('greengrassv2', usecase, session_name=session_name)

        final_prefix, final_keys = stage_and_promote_artifacts(
            usecase_s3, usecase_bucket, workflow_id, version, arch_zip_paths)

        # Register only after every artifact uploaded successfully (7.5)
        recipe = build_recipe(workflow_id, version, usecase_bucket, final_keys)
        component_arn = register_component(greengrass, recipe, usecase_id,
                                           workflow_id, version, user)
    except PackagingError as e:
        # All-or-nothing: delete the stage and any promoted artifacts,
        # report the failing artifact, register nothing (Requirement 7.5)
        if usecase_s3 is not None:
            delete_prefix(usecase_s3, usecase_bucket, staging_root)
            if final_prefix:
                delete_prefix(usecase_s3, usecase_bucket, final_prefix)
        log_audit_event(
            user_id=user['user_id'],
            action='package_workflow',
            resource_type='workflow',
            resource_id=workflow_id,
            result='failure',
            details={'usecase_id': usecase_id, 'version': version,
                     'architectures': architectures,
                     'failing_artifact': e.artifact, 'error': e.message}
        )
        logger.error(f"Packaging failed for {workflow_id} v{version} "
                     f"at artifact {e.artifact}: {e.message}")
        return error_response(502, 'PACKAGING_FAILED', e.message,
                              {'failing_artifact': e.artifact,
                               'version': version,
                               'architectures': architectures})
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        # The stage is temporary in every outcome; on success the promoted
        # copies under the final prefix are the component's artifacts.
        if usecase_s3 is not None and component_arn is not None:
            delete_prefix(usecase_s3, usecase_bucket, staging_root)

    # Success bookkeeping: compiled documents to portal S3 + version record
    compiled_arch_keys: Dict[str, str] = {}
    for arch, compiled in compiled_docs.items():
        portal_key = compiled_doc_portal_key(usecase_id, workflow_id, version, arch)
        s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=portal_key,
                      Body=compiled.to_json().encode('utf-8'),
                      ContentType='application/json')
        compiled_arch_keys[arch] = portal_key

    dynamodb.Table(WORKFLOW_VERSIONS_TABLE).update_item(
        Key={'workflow_id': workflow_id, 'version': version},
        UpdateExpression=('SET component_arn = :arn, compiled_arch_keys = :keys, '
                          'packaged_at = :at, packaged_by = :by'),
        ExpressionAttributeValues={
            ':arn': component_arn,
            ':keys': compiled_arch_keys,
            ':at': now_ms(),
            ':by': user['user_id']
        }
    )

    # Audit log entry for packaging (Requirement 11.5)
    log_audit_event(
        user_id=user['user_id'],
        action='package_workflow',
        resource_type='workflow',
        resource_id=workflow_id,
        result='success',
        details={
            'usecase_id': usecase_id,
            'version': version,
            'architectures': architectures,
            'component_name': component_name_for(workflow_id),
            'component_version': component_version_for(version),
            'component_arn': component_arn
        }
    )

    return create_response(201, {
        'workflow_id': workflow_id,
        'version': version,
        'component_name': component_name_for(workflow_id),
        'component_version': component_version_for(version),
        'component_arn': component_arn,
        'architectures': architectures,
        'artifacts': {
            arch: f"s3://{usecase_bucket}/{key}" for arch, key in final_keys.items()
        }
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

        if resource == '/workflows/{id}/package' and workflow_id:
            if http_method == 'POST':
                return package_workflow(event, user, workflow_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
