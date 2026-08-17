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

Custom_Node_Type plugins (custom-node-designer Requirements 10.4, 11.1,
11.2, 11.3, 16.4): compilation runs against the merged Node_Type_Catalog
resolving the Custom_Node_Type versions pinned at workflow save (14.2).
Compiled ``custom:{usecase}/{name}`` plugin dependencies are NEVER
bundled inline; each resolves to its backing Plugin_Record, which is
gated (dev lifecycle state, missing per-arch Plugin_Artifact, or missing
Plugin_Component version reject the request identifying the
Custom_Node_Type and arch/state), verified (streamed SHA-256 recompute +
KMS signature verification, failing via the PackagingError path on
either mismatch), recorded per arch in manifest.json ``pluginChecksums``
/ ``pluginComponents``, and delivered by a Greengrass
``ComponentDependencies`` entry on ``dda.plugin.{pluginId}`` pinned to
the recorded Plugin_Record version. Built-in/curated plugins keep the
inline ``plugins/{arch}/*.so`` bundling unchanged.

Workflow dependency edges (edge-deploy-reliability Requirements 2.8,
2.9): the recipe's ComponentDependencies additionally carries one
unpinned HARD entry per distinct published model component the
workflow's ``model_ref`` parameters resolve to, and one HARD entry per
distinct LocalServer variant of the selected architectures at that
arch's minimum-version floor — so Greengrass owns the ordering/health
relationship between a workflow, its models, and the LocalServer
backend that executes them.

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
import base64
import binascii
import copy
import hashlib
import json
import os
import logging
import posixpath
import re
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
from workflow_core.catalog import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_ARM64_JP7,
    DEVICE_ARCHITECTURES,
    VLLM_ARCHITECTURES,
)
from workflow_core.catalog.custom import resolve_catalog
from workflow_core.catalog.models import PARAM_TYPE_MODEL_REF

# Merged-catalog resolution + Plugin_Record persistence (same bundle)
from node_catalog_resolution import (
    descriptors_from_items,
    load_registered_node_types,
    resolution_items,
)
from plugin_records import get_version_item as get_plugin_record_version
# Model_Registry snapshot (same bundle): model_ref values resolve against
# the same registry view workflow_validation.py validates them against
# (edge-deploy-reliability Requirement 2.8).
from model_registry_snapshot import build_model_registry_snapshot
# Shared model-name sanitization transform (same bundle): the ONE transform
# greengrass_publish.py / packaging.py derive the served vLLM model name
# with, applied here to each llm_inference node's packaged modelName so the
# packaged name equals the served name (vllm-model-name-mismatch 2.1, 2.2).
from model_naming import safe_model_name

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (portal account)
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
kms = boto3.client('kms')

# Environment variables
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')
# Curated GStreamer plugin artifact library in portal S3:
#   {WORKFLOW_PLUGIN_LIBRARY_PREFIX}/{arch}/{plugin}.so
WORKFLOW_PLUGIN_LIBRARY_PREFIX = os.environ.get(
    'WORKFLOW_PLUGIN_LIBRARY_PREFIX', 'workflow-plugins')
# Model_Registry sources for resolving model_ref parameter values to their
# published model components (edge-deploy-reliability 2.8): the same
# tables/GSIs workflow_validation.py builds its resolution snapshot from.
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
MODELS_TABLE = os.environ.get('MODELS_TABLE')
# Minimum LocalServer component version a Workflow_Component requires
# (surfaced in manifest.json for the deployment compatibility check, 8.4).
#
# LocalServer ships as independently-versioned per-architecture variants
# (aws.edgeml.dda.LocalServer.arm64 / .arm64JP5 / .arm64JP6 / .amd64), whose
# version lineages are NOT comparable to each other: at time of writing the
# .arm64 variant is ~1.0.124 while .arm64JP6 is ~1.0.35. A single global
# minimum therefore falsely blocks the JetPack variants (a JP6 device running
# 1.0.35 can never satisfy an arm64-derived "1.0.63"). WORKFLOW_MIN_LOCAL_
# SERVER_VERSIONS is a JSON object keyed by workflow_core arch id
# ({"arm64_jp6": "1.0.0", ...}) giving each variant lineage its own floor.
# Hardened contract (jp7-workflow-min-localserver-floor): when a map IS
# configured (non-empty), every arch known to ARCH_TO_LOCAL_SERVER_COMPONENT
# must have its own entry (test_workflow_min_localserver_floor_coverage.py
# pins the deployed CDK literal to that key set); a known arch missing from a
# configured map resolves the safe per-lineage floor SAFE_LINEAGE_FLOOR with
# a loud warning — NEVER the cross-lineage scalar below. The scalar remains
# the last-resort default only for an empty/unconfigured map, a None arch,
# or an arch unknown to ARCH_TO_LOCAL_SERVER_COMPONENT.
MIN_LOCAL_SERVER_VERSION = os.environ.get(
    'WORKFLOW_MIN_LOCAL_SERVER_VERSION',
    os.environ.get('DDA_LOCAL_SERVER_VERSION', '1.0.0'))

# Safe per-lineage floor substituted when a KNOWN arch is missing from a
# configured floor map: '1.0.0' is satisfiable in EVERY LocalServer variant
# lineage (all variants version from 1.0.x and workflow support ships in
# current field builds) — the same reasoning behind every existing map
# entry, so the hardened path can never emit an unsatisfiable constraint
# (design Decision 1, jp7-workflow-min-localserver-floor).
SAFE_LINEAGE_FLOOR = '1.0.0'


def _parse_min_versions_map():
    """Per-arch minimum LocalServer versions from WORKFLOW_MIN_LOCAL_SERVER_
    VERSIONS (JSON object). Malformed or non-object values yield {} so the
    scalar default is used for every arch."""
    raw = os.environ.get('WORKFLOW_MIN_LOCAL_SERVER_VERSIONS', '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logging.warning(
            'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is not valid JSON; '
            'falling back to the scalar minimum for every arch')
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


MIN_LOCAL_SERVER_VERSIONS = _parse_min_versions_map()


def min_local_server_version_for(arch):
    """The minimum LocalServer version for a Workflow_Component targeting
    ``arch``: the per-arch entry when mapped; for a KNOWN arch missing
    from a configured (non-empty) map, the safe per-lineage floor
    SAFE_LINEAGE_FLOOR with a loud warning — never the cross-lineage
    scalar (design Decision 2, jp7-workflow-min-localserver-floor); else
    the scalar default (empty map, None, or unknown arch). Keeps each
    independently-versioned LocalServer variant lineage self-consistent
    (a JP6 package is gated against JP6 builds, not the arm64 lineage)."""
    if arch and arch in MIN_LOCAL_SERVER_VERSIONS:
        return MIN_LOCAL_SERVER_VERSIONS[arch]
    # ARCH_TO_LOCAL_SERVER_COMPONENT is defined below; module-level name
    # resolution at call time makes this forward reference valid.
    if MIN_LOCAL_SERVER_VERSIONS and arch in ARCH_TO_LOCAL_SERVER_COMPONENT:
        logging.warning(
            'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is configured but has no '
            'entry for known arch %r; substituting the safe per-lineage '
            'floor %s instead of the cross-lineage scalar. The deployed '
            'floor map must cover every ARCH_TO_LOCAL_SERVER_COMPONENT key '
            '(pinned by test_workflow_min_localserver_floor_coverage.py) - '
            'add the missing key to compute-stack.ts.',
            arch, SAFE_LINEAGE_FLOOR)
        return SAFE_LINEAGE_FLOOR
    return MIN_LOCAL_SERVER_VERSION

# Greengrass component naming (design section 6)
WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.'
COMPONENT_PUBLISHER = 'DDA Portal Workflow Manager'

# Plugin_Component naming (custom-node-designer, Requirement 16.4). Kept as
# a local constant: plugin_components.py imports THIS module, so importing
# it back would be circular.
PLUGIN_COMPONENT_PREFIX = 'dda.plugin.'

# Use_Case account S3 prefixes for Workflow_Component artifacts
COMPONENT_S3_PREFIX = 'workflows/components'
STAGING_S3_PREFIX = 'workflows/staging'

# Compiler pluginDependencies prefixes (split_plugin_dependencies)
PYTHON_DEP_PREFIX = 'python:'
#: A Custom_Node_Type plugin dependency: custom:{usecase_id}/{plugin_name}
#: (recorded by custom_node_types.py, Requirement 8.6). Routed to the
#: plugin's Plugin_Component instead of inline bundling (16.4).
CUSTOM_DEP_PREFIX = 'custom:'

# The portal Plugin_Artifact signing key (custom-node-designer 10.4):
# packaging KMS-Verifies each custom plugin artifact's recorded signature.
PLUGIN_SIGNING_KEY_ARN = os.environ.get('PLUGIN_SIGNING_KEY_ARN')
SIGNING_ALGORITHM = 'ECDSA_SHA_256'

# Backing Plugin_Record lifecycle states allowed to package (11.3: dev is
# rejected; anything unknown fails closed).
PACKAGEABLE_LIFECYCLE_STATES = ('test', 'prod')

# Custom-plugin packaging gate codes (11.2, 11.3, 16.4)
GATE_RECORD_MISSING = 'PLUGIN_RECORD_NOT_FOUND'
GATE_LIFECYCLE = 'PLUGIN_LIFECYCLE_VIOLATION'
GATE_ARTIFACT_MISSING = 'PLUGIN_ARTIFACT_MISSING'
GATE_COMPONENT_MISSING = 'PLUGIN_COMPONENT_MISSING'

ARCH_X86_64 = 'x86_64'
ARCH_X86_64_NVIDIA = 'x86_64_nvidia'

# arch id (workflow_core) -> Greengrass platform architecture. Both x86_64
# flavors map to Greengrass amd64; recipes disambiguate with the
# 'runtime: nvidia' platform attribute on x86_64_nvidia manifests plus the
# manifest ordering from recipe_manifest_order (design: x86_64_nvidia).
ARCH_TO_GG_PLATFORM = {
    'x86_64': 'amd64',
    'x86_64_nvidia': 'amd64',
    'arm64_jp4': 'aarch64',
    'arm64_jp5': 'aarch64',
    'arm64_jp6': 'aarch64',
    'arm64_jp7': 'aarch64',
}

# arch id (workflow_core) -> per-architecture LocalServer component variant
# (edge-deploy-reliability Requirement 2.9). Same fail-closed naming
# discipline as greengrass_publish.TARGET_TO_LOCAL_SERVER: every variant is
# explicitly JetPack/arch-tagged, the retired bare '.arm64' name is never
# emitted, and an unknown arch raises instead of guessing a variant. Both
# x86_64 flavors run the single amd64 LocalServer build.
ARCH_TO_LOCAL_SERVER_COMPONENT = {
    ARCH_ARM64_JP4: 'aws.edgeml.dda.LocalServer.arm64JP4',
    ARCH_ARM64_JP5: 'aws.edgeml.dda.LocalServer.arm64JP5',
    ARCH_ARM64_JP6: 'aws.edgeml.dda.LocalServer.arm64JP6',
    ARCH_ARM64_JP7: 'aws.edgeml.dda.LocalServer.arm64JP7',
    ARCH_X86_64: 'aws.edgeml.dda.LocalServer.amd64',
    ARCH_X86_64_NVIDIA: 'aws.edgeml.dda.LocalServer.amd64',
}

# arch id (workflow_core) -> greengrass_publish.py compile-target id, for
# reading the VISION publish shape: greengrass_publish.py writes one
# ``published_components`` entry per compile target (``{component_name,
# target, component_version, status}``), so resolving a vision model's
# component name(s) for the selected architectures means matching entries
# on these target ids (vision-model-packaging-regression 2.2). Values are
# the exact TARGET_TO_LOCAL_SERVER / TARGET_TO_PLATFORM keys in
# greengrass_publish.py ('jetson-xavier' is the legacy JetPack 4 id;
# x86_64/x86_64_nvidia publish as the 'x86_64-cpu'/'x86_64-cuda' targets).
# Same fail-closed discipline as ARCH_TO_LOCAL_SERVER_COMPONENT: an
# unknown arch raises instead of guessing a target.
ARCH_TO_PUBLISH_TARGET = {
    ARCH_ARM64_JP4: 'jetson-xavier',
    ARCH_ARM64_JP5: 'jetson-xavier-jp5',
    ARCH_ARM64_JP6: 'jetson-xavier-jp6',
    # JP7 publish-target id, following the jp5/jp6 'jetson-xavier-jpN'
    # convention. 'jetson-xavier-jp7' is now producible: BYO ONNX imports
    # publish it (packaging.py's defaulted import target list includes
    # JP7). Compiled-ONNX coverage for arm64_jp7 arrives via the
    # additional 'onnx-jetson-xavier-jp7' id accepted through
    # ARCH_TO_EXTRA_PUBLISH_TARGETS below — ONNX is the delivered JP7
    # vision route (Neo cannot target CUDA 13).
    ARCH_ARM64_JP7: 'jetson-xavier-jp7',
    ARCH_X86_64: 'x86_64-cpu',
    ARCH_X86_64_NVIDIA: 'x86_64-cuda',
}

# Additional publish-target ids accepted per arch when resolving VISION
# published_components. arm64_jp7's vision route is ONNX (Neo cannot
# target CUDA 13): compiled-ONNX publishes 'onnx-jetson-xavier-jp7'
# (packaging.ONNX_ARCH_TO_TARGET — keep in sync) and BYO ONNX imports
# publish 'jetson-xavier-jp7' (already the primary id above). JP5/JP6
# deliberately get NO onnx acceptance here: Neo remains their primary
# vision route and their coverage semantics are unchanged.
ARCH_TO_EXTRA_PUBLISH_TARGETS = {
    ARCH_ARM64_JP7: ('onnx-jetson-xavier-jp7',),
}


def publish_targets_for_arch(arch):
    """Accepted published_components target ids for one arch, or ()
    when the arch has no known publish target (caller fails closed)."""
    primary = ARCH_TO_PUBLISH_TARGET.get(arch)
    if not primary:
        return ()
    return (primary,) + ARCH_TO_EXTRA_PUBLISH_TARGETS.get(arch, ())

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
    """Base component version derived from the workflow version
    (Requirement 7.2): ``{workflow_version}.0.0``. This is the FIRST package
    of a workflow version; re-packaging bumps the MAJOR to the next free
    ``N.0.0`` (see next_component_version) because Greengrass component
    versions are immutable and its on-device store reliably re-installs a new
    major but can leave a patch/minor bump stale."""
    return f"{int(workflow_version)}.0.0"


def _existing_component_versions(greengrass, component_name: str) -> set:
    """Every registered version string of ``component_name`` in the Use_Case
    account, or an empty set when the component does not exist yet. Resolves
    the component ARN via list_components (no account id needed) then pages
    list_component_versions."""
    arn = None
    try:
        for page in greengrass.get_paginator('list_components').paginate(
                scope='PRIVATE'):
            for comp in page.get('components', []):
                if comp.get('componentName') == component_name:
                    arn = comp.get('arn')
                    break
            if arn:
                break
    except ClientError as e:
        logger.warning('Could not list components while resolving next version '
                       'for %s: %s', component_name, e)
        return set()
    if not arn:
        return set()
    versions = set()
    try:
        for page in greengrass.get_paginator('list_component_versions').paginate(
                arn=arn):
            for v in page.get('componentVersions', []):
                if v.get('componentVersion'):
                    versions.add(v['componentVersion'])
    except ClientError as e:
        logger.warning('Could not list versions of %s: %s', component_name, e)
    return versions


def next_component_version(greengrass, component_name: str,
                           workflow_version: int) -> str:
    """The next component version for (re-)packaging a workflow version, as a
    MAJOR-only ``N.0.0`` bump.

    Greengrass component versions are immutable, so re-packaging an unchanged
    workflow version cannot reuse ``{workflow_version}.0.0``. A patch bump
    (``{v}.0.1``) registers cloud-side but Greengrass's on-device component
    store can leave the previous artifact in place across a patch/minor
    revision, so the LocalServer workflow watcher keeps scanning the stale
    files (observed on JP6: a re-packaged workflow stays "invalid" against the
    old manifest). The model/vLLM components work around the same behavior by
    versioning major-only and always publishing the next major
    (next_vllm_component_version); mirror that here so every (re-)package is a
    clean new major Greengrass reliably re-installs.

    Returns ``N.0.0`` where N is the greater of the workflow version and one
    past the highest existing major — so the FIRST package of workflow v3 is
    still ``3.0.0`` (workflow-version traceability preserved; the true workflow
    version also lives in the manifest ``workflowVersion`` and the recipe
    config), and each re-package strictly increases the major (3.0.0 -> 4.0.0
    -> ...)."""
    existing = _existing_component_versions(greengrass, component_name)
    highest_major = 0
    for v in existing:
        match = re.match(r'^(\d+)\.', str(v))
        if match:
            highest_major = max(highest_major, int(match.group(1)))
    major = max(int(workflow_version), highest_major + 1)
    return f"{major}.0.0"


def zip_artifact_name(arch: str) -> str:
    return f"workflow-{arch}.zip"


# --------------------------------------------------------------------------
# Artifact assembly
# --------------------------------------------------------------------------

#: Node types whose per-node code and declared pip dependencies ship as
#: python/{nodeId}/handler.py + requirements.txt in every architecture
#: artifact zip and are listed together in the manifest's
#: customPythonNodeIds (custom-python-frames Requirements 2.4, 2.5;
#: custom-python-source Requirements 9.1, 9.2).
CUSTOM_PYTHON_NODE_TYPES = ('custom_python', 'custom_python_preprocess',
                            'custom_python_source')


def gather_custom_python_nodes(graph) -> List[Dict]:
    """Custom_Python_Nodes whose code + declared dependencies ship in the
    Workflow_Component artifacts (Requirement 7.3; all three Custom Python
    node types — custom-python-frames Requirements 2.4, 2.5;
    custom-python-source Requirements 9.1, 9.2)"""
    nodes = []
    for node in graph.nodes:
        if node.type in CUSTOM_PYTHON_NODE_TYPES:
            nodes.append({
                'node_id': node.id,
                'code': str(node.parameters.get('code') or ''),
                'requirements': str(node.parameters.get('requirements') or '')
            })
    return nodes


def split_plugin_dependencies(plugin_dependencies: List[str]
                              ) -> Tuple[List[str], List[str], List[str]]:
    """Split compiler pluginDependencies into curated GStreamer plugin
    names (packaged inline as plugins/{arch}/*.so), custom:{usecase}/{name}
    Custom_Node_Type plugin dependencies (delivered by Plugin_Component
    dependency, never bundled inline — Requirement 16.4), and python:
    runtime packages (surfaced in manifest.json for the edge executor)."""
    gst_plugins, custom_plugins, python_packages = [], [], []
    for dep in plugin_dependencies:
        if dep.startswith(PYTHON_DEP_PREFIX):
            python_packages.append(dep[len(PYTHON_DEP_PREFIX):])
        elif dep.startswith(CUSTOM_DEP_PREFIX):
            custom_plugins.append(dep)
        else:
            gst_plugins.append(dep)
    return sorted(gst_plugins), sorted(custom_plugins), sorted(python_packages)


# --------------------------------------------------------------------------
# Camera_Input_Node binding points
# (camera-registry-sync Requirements 8.6, 11.5)
#
# For each Camera_Input_Node the packager appends a ``bindingPoints`` entry
# to compiled_pipeline.json mapping the node's logical parameters to the
# rendered element arguments, and records the ``has_binding_points`` /
# ``camera_input_nodes`` discriminator on the workflow version item. The
# compiled elements keep their fully rendered default values, so an unbound
# document behaves byte-identically to pre-feature output, and workflows
# without Camera_Input_Nodes produce byte-identical documents (11.5).
# --------------------------------------------------------------------------

#: The built-in NVIDIA CSI Camera_Input_Node type. CSI capture is host-
#: service based (nvidia-csi-capture.service stages frames + reads
#: gain/exposure from config.json), so the binding never lands in an
#: element argument: the binding point carries ``csiSensorBinding: true``
#: with empty slots on every physical device architecture and the rendered
#: gain/exposure in ``parameters``.
CSI_CAMERA_SOURCE_TYPE_ID = 'csi_camera_source'

#: The built-in ICAM (V4L2 smart camera) Camera_Input_Node type. Captured
#: directly through ``v4l2src device={device}``, so the ``device``
#: parameter lands in exactly one element argument: the binding point
#: carries the generic single ``device`` slot computed by
#: ``binding_point_slots``.
ICAM_SOURCE_TYPE_ID = 'icam_source'

#: The Aravis (GenICam) Camera_Input_Node type (aravis-camera-input
#: Requirements 4.1, 4.2). Aravis acquisition happens in the LocalServer
#: process through the camera manager, so the binding never lands in an
#: element argument: the binding point carries ``aravisBinding: true``
#: with empty slots on every physical device architecture.
ARAVIS_CAMERA_SOURCE_TYPE_ID = 'aravis_camera_source'

#: The Custom Python source node type (custom-python-source Requirements
#: 9.1, 9.2). The Frame_Producer runs in the LocalServer's Python_Bridge
#: and feeds the compiled ``appsrc_{nodeId}`` through the executor's
#: single-frame Frame_Feed, so the binding never lands in an element
#: argument: the binding point carries ``pythonSourceBinding: true`` with
#: empty slots and ONLY the rendered ``allowed_uri_prefixes`` parameter —
#: ``code``/``requirements`` ship as artifact files, never duplicated
#: into the binding point.
CUSTOM_PYTHON_SOURCE_TYPE_ID = 'custom_python_source'


def gather_python_source_nodes(graph) -> List:
    """The graph's Custom Python source nodes, in graph node order: each
    gains a ``pythonSourceBinding`` bindingPoints entry so the device
    planner can locate the node's compiled appsrc
    (custom-python-source Requirement 9.2)."""
    return [node for node in graph.nodes
            if node.type == CUSTOM_PYTHON_SOURCE_TYPE_ID]

#: Optional Custom_Node_Type descriptor flag declaring the type
#: camera-backed. Both the snake_case spelling from the design and the
#: camelCase convention of custom declaration wire shapes are honored.
CAMERA_BACKED_FLAGS = ('camera_backed', 'cameraBacked')

#: An argument template value that is exactly one ``{placeholder}`` token —
#: the only shape that lands a node parameter verbatim in an element arg.
_SLOT_PLACEHOLDER = re.compile(r'^\{(\w+)\}$')


def camera_backed_type_ids(node_type_items: List[Dict]) -> set:
    """Type ids of resolved Custom_Node_Types declared camera-backed via
    the optional ``camera_backed: true`` descriptor flag."""
    type_ids = set()
    for item in node_type_items or []:
        declaration = item.get('declaration')
        if not isinstance(declaration, dict):
            continue
        if any(declaration.get(flag) is True for flag in CAMERA_BACKED_FLAGS):
            type_id = declaration.get('typeId')
            if isinstance(type_id, str):
                type_ids.add(type_id)
    return type_ids


def gather_camera_input_nodes(graph, camera_backed_types: set) -> List:
    """The graph's Camera_Input_Nodes: csi_camera_source, icam_source and
    aravis_camera_source nodes plus nodes of any Custom_Node_Type declared
    camera-backed, in graph node order."""
    return [node for node in graph.nodes
            if node.type == CSI_CAMERA_SOURCE_TYPE_ID
            or node.type == ICAM_SOURCE_TYPE_ID
            or node.type == ARAVIS_CAMERA_SOURCE_TYPE_ID
            or node.type in camera_backed_types]


def binding_hints_from_definition(definition: Dict) -> Dict[str, Dict]:
    """Per-node ``cameraBindingHint`` advisory data recorded by the
    Workflow_Builder in the definition document (``nodes[].data``), keyed
    by node id. Tolerant of definitions carrying no node data at all —
    every pre-feature definition — so packaging them is unchanged (11.5)."""
    hints: Dict[str, Dict] = {}
    for node in definition.get('nodes') or []:
        if not isinstance(node, dict):
            continue
        data = node.get('data')
        hint = data.get('cameraBindingHint') if isinstance(data, dict) else None
        node_id = node.get('id')
        if isinstance(hint, dict) and hint and isinstance(node_id, str):
            hints[node_id] = hint
    return hints


def rendered_default_parameters(node, descriptor) -> Dict[str, Any]:
    """The parameter values rendered into the compiled document: declared
    defaults overlaid with the node's explicit values (the compiler's
    effective-value rule)."""
    values = {parameter.name: parameter.default
              for parameter in descriptor.parameters}
    values.update(node.parameters)
    return values


def binding_point_slots(compiled_doc: Dict, node_id: str, mapping,
                        parameter_names: set) -> List[Dict]:
    """Where each of the node's parameters lands in THIS compiled document:
    one slot per element argument whose catalog template is exactly a
    single ``{parameter}`` placeholder (e.g. the v4l2src ``device`` arg on
    x86_64 / x86_64_nvidia). The node's element chain appears contiguously
    in exactly one segment (compiler Requirement 6.6), so template index k
    addresses the k-th element of that run."""
    if mapping is None or not mapping.element_chain:
        return []
    slots: List[Dict] = []
    for segment_index, segment in enumerate(compiled_doc.get('segments') or []):
        elements = segment.get('elements') or []
        run_start = next((index for index, element in enumerate(elements)
                          if element.get('nodeId') == node_id), None)
        if run_start is None:
            continue
        for offset, template in enumerate(mapping.element_chain):
            for arg in sorted(template.get('args_template') or {}):
                value = template['args_template'][arg]
                if not isinstance(value, str):
                    continue
                match = _SLOT_PLACEHOLDER.match(value)
                if match and match.group(1) in parameter_names:
                    slots.append({
                        'param': match.group(1),
                        'segment': segment_index,
                        'element': run_start + offset,
                        'arg': arg,
                    })
        break  # each node's chain lives in exactly one segment
    return slots


def build_binding_points(camera_nodes: List, compiled_doc: Dict, arch: str,
                         hints: Dict[str, Dict],
                         descriptors_by_id: Dict) -> List[Dict]:
    """The ``bindingPoints`` section of one architecture's compiled
    document: nodeId, nodeType, bindingHint from the definition, rendered
    default parameters, and arch-specific slots. csi_camera_source is
    host-service (CSI capture) bound on every physical device architecture
    (``csiSensorBinding: true``, empty slots) with the rendered
    gain/exposure values in ``parameters``; the binding selects the CSI
    sensor the capture host service stages from, never an element argument.
    icam_source is captured directly through ``v4l2src device={device}``,
    so it carries the generic single ``device`` slot with the rendered
    device value. aravis_camera_source is executor-feed-bound on every
    physical device architecture (``aravisBinding: true``, empty slots)
    with the rendered camera_id/gain/exposure values in ``parameters``
    (aravis-camera-input Requirement 4.2). custom_python_source is
    executor-feed-bound the same way (``pythonSourceBinding: true``,
    empty slots) with ONLY the rendered ``allowed_uri_prefixes`` value in
    ``parameters`` — ``code``/``requirements`` ship as artifact files and
    are never duplicated into the binding point (custom-python-source
    Requirements 9.1, 9.2)."""
    binding_points: List[Dict] = []
    for node in camera_nodes:
        descriptor = descriptors_by_id[node.type]
        entry: Dict[str, Any] = {
            'nodeId': node.id,
            'nodeType': node.type,
            'parameters': rendered_default_parameters(node, descriptor),
            'slots': [],
        }
        hint = hints.get(node.id)
        if hint:
            entry['bindingHint'] = hint
        if node.type == CUSTOM_PYTHON_SOURCE_TYPE_ID:
            entry['pythonSourceBinding'] = True
            entry['parameters'] = {
                'allowed_uri_prefixes':
                    entry['parameters'].get('allowed_uri_prefixes') or ''}
        elif node.type == ARAVIS_CAMERA_SOURCE_TYPE_ID:
            entry['aravisBinding'] = True
        elif node.type == CSI_CAMERA_SOURCE_TYPE_ID:
            entry['csiSensorBinding'] = True
        else:
            entry['slots'] = binding_point_slots(
                compiled_doc, node.id, descriptor.mapping_for(arch),
                {parameter.name for parameter in descriptor.parameters})
        binding_points.append(entry)
    return binding_points


def compiled_document_json(compiled, binding_points: List[Dict]) -> str:
    """compiled_pipeline.json content: the canonical compiler output with
    each ``llm_inference`` executor binding's ``modelName`` rewritten to
    the sanitized served name (vllm-model-name-mismatch 2.1), plus the
    ``bindingPoints`` section when the workflow has Camera_Input_Nodes.
    With no camera nodes and no llm rewrite the output is byte-identical
    to the compiler's own serialization (camera-registry-sync 11.5;
    vllm-model-name-mismatch 3.1, 3.2)."""
    document, changed = rewrite_compiled_llm_model_names(compiled.to_dict())
    if not binding_points and not changed:
        return compiled.to_json()
    if binding_points:
        document['bindingPoints'] = binding_points
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)


def _rendered_slot_value(compiled_doc: Dict, slot: Dict) -> Any:
    """The rendered element-argument value a slot points at."""
    try:
        segment = compiled_doc['segments'][slot['segment']]
        return segment['elements'][slot['element']]['args'][slot['arg']]
    except (KeyError, IndexError, TypeError):
        return None


def _dynamo_safe(obj: Any) -> Any:
    """Floats are not storable in DynamoDB; convert them to Decimal."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {key: _dynamo_safe(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_dynamo_safe(item) for item in obj]
    return obj


def camera_input_nodes_record(camera_nodes: List, hints: Dict[str, Dict],
                              arch_binding_points: Dict[str, List[Dict]],
                              arch_compiled_docs: Dict[str, Dict]) -> List[Dict]:
    """The ``camera_input_nodes`` version-item attribute: node id, node
    type, binding hint, and the per-arch compiled device paths (the
    rendered ``device`` slot values) the Deployment_Service's legacy path
    check and binding matrix read without re-fetching compiled documents
    from S3 (camera-registry-sync 8.6, 9.5)."""
    records: List[Dict] = []
    for node in camera_nodes:
        record: Dict[str, Any] = {
            'node_id': node.id,
            'node_type': node.type,
            'compiled_device_paths': {},
        }
        hint = hints.get(node.id)
        if hint:
            record['binding_hint'] = hint
        for arch in sorted(arch_binding_points):
            entry = next((point for point in arch_binding_points[arch]
                          if point['nodeId'] == node.id), None)
            if not entry:
                continue
            for slot in entry['slots']:
                if slot['param'] != 'device':
                    continue
                value = _rendered_slot_value(arch_compiled_docs[arch], slot)
                if isinstance(value, str):
                    record['compiled_device_paths'][arch] = value
        records.append(record)
    return records


# --------------------------------------------------------------------------
# Custom_Node_Type plugin resolution, gates, and verification
# (custom-node-designer Requirements 10.4, 11.1, 11.2, 11.3, 16.4)
#
# Everything up to load_custom_plugin_records is pure over plain dicts so
# the gate/dependency decision logic is property-testable without AWS
# (tasks 10.2-10.4).
# --------------------------------------------------------------------------

def plugin_component_name(plugin_id: str) -> str:
    """The Greengrass Plugin_Component name of a Plugin_Record (16.4)"""
    return f"{PLUGIN_COMPONENT_PREFIX}{plugin_id}"


def plugin_version_requirement(plugin_version: int) -> str:
    """Greengrass VersionRequirement pinning the Plugin_Record version
    recorded by the workflow's pinned Custom_Node_Type version (16.4)."""
    version = int(plugin_version)
    return f">={version}.0.0 <{version + 1}.0.0"


def custom_dependency_index(node_type_items: List[Dict]) -> Dict[str, Dict]:
    """Map each ``custom:`` plugin dependency recorded in the resolved
    Custom_Node_Type declarations to its CustomNodeTypes item, so a
    compiled dependency resolves to the pinned backing Plugin_Record and
    gate rejections can identify the Custom_Node_Type (11.2, 11.3).
    Deterministic: items visited sorted by (node_type_id, version)."""
    index: Dict[str, Dict] = {}
    ordered = sorted(node_type_items,
                     key=lambda i: (str(i.get('node_type_id')),
                                    int(i.get('version', 0))))
    for item in ordered:
        declaration = item.get('declaration')
        if not isinstance(declaration, dict):
            continue
        mappings = declaration.get('mappings')
        if not isinstance(mappings, list):
            continue
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for dep in mapping.get('pluginDependencies') or []:
                if isinstance(dep, str) and dep.startswith(CUSTOM_DEP_PREFIX):
                    index[dep] = item
    return index


def artifact_entry_complete(entry: Optional[Dict]) -> bool:
    """A per-arch Plugin_Record artifact entry usable for packaging: a
    succeeded build with the recorded key, checksum, and signature that
    verification (10.4) needs."""
    return bool(entry
                and entry.get('buildStatus') == 'succeeded'
                and entry.get('s3Key')
                and entry.get('checksum')
                and entry.get('signature'))


def custom_plugin_gate_findings(arch_custom_deps: Dict[str, List[str]],
                                dep_index: Dict[str, Dict],
                                dep_records: Dict[str, Optional[Dict]]
                                ) -> List[Dict]:
    """
    Evaluate the custom-plugin packaging gates (11.2, 11.3, 16.4) before
    any artifact is assembled. Returns [] when packaging may proceed,
    otherwise the complete list of findings, each identifying the
    Custom_Node_Type and the offending lifecycle state or missing
    Target_Architecture / Plugin_Component.

    ``arch_custom_deps``: {arch: [custom: deps compiled for that arch]}
    ``dep_index``: custom_dependency_index over the resolved items
    ``dep_records``: {dep: backing Plugin_Record item or None}
    """
    findings: List[Dict] = []
    dep_archs: Dict[str, List[str]] = {}
    for arch in sorted(arch_custom_deps):
        for dep in arch_custom_deps[arch]:
            dep_archs.setdefault(dep, []).append(arch)

    for dep in sorted(dep_archs):
        item = dep_index.get(dep)
        node_type_id = item.get('node_type_id') if item else None
        record = dep_records.get(dep)
        if not item or not record:
            findings.append({
                'code': GATE_RECORD_MISSING,
                'message': (f"Custom plugin dependency '{dep}' of "
                            f"Custom_Node_Type '{node_type_id or 'unknown'}' has no "
                            f"resolvable backing Plugin_Record"),
                'node_type_id': node_type_id,
                'dependency': dep,
            })
            continue

        plugin_id = record.get('plugin_id')
        plugin_version = record.get('version')
        state = record.get('lifecycle_state')
        if state not in PACKAGEABLE_LIFECYCLE_STATES:
            # 11.3: dev (or unknown — fail closed) lifecycle state rejects,
            # identifying the Custom_Node_Type and its Lifecycle_State.
            findings.append({
                'code': GATE_LIFECYCLE,
                'message': (f"Custom_Node_Type '{node_type_id}' is backed by "
                            f"plugin '{plugin_id}' v{plugin_version} in lifecycle "
                            f"state '{state}'; packaging requires test or prod"),
                'node_type_id': node_type_id,
                'dependency': dep,
                'plugin_id': plugin_id,
                'plugin_version': plugin_version,
                'lifecycle_state': state,
            })
            continue

        artifacts = record.get('artifacts') or {}
        for arch in dep_archs[dep]:
            if not artifact_entry_complete(artifacts.get(arch)):
                # 11.2: missing per-arch Plugin_Artifact rejects, identifying
                # the Custom_Node_Type and the missing Target_Architecture.
                findings.append({
                    'code': GATE_ARTIFACT_MISSING,
                    'message': (f"Custom_Node_Type '{node_type_id}' has no built "
                                f"Plugin_Artifact for architecture '{arch}' "
                                f"(plugin '{plugin_id}' v{plugin_version})"),
                    'node_type_id': node_type_id,
                    'dependency': dep,
                    'plugin_id': plugin_id,
                    'plugin_version': plugin_version,
                    'arch': arch,
                })

        component = record.get('component') or {}
        if component.get('status') != 'registered':
            # 16.4: the Workflow_Component depends on the Plugin_Component,
            # so a missing registered version rejects packaging.
            findings.append({
                'code': GATE_COMPONENT_MISSING,
                'message': (f"Custom_Node_Type '{node_type_id}' has no registered "
                            f"Plugin_Component version for plugin '{plugin_id}' "
                            f"v{plugin_version} "
                            f"({plugin_component_name(str(plugin_id))})"),
                'node_type_id': node_type_id,
                'dependency': dep,
                'plugin_id': plugin_id,
                'plugin_version': plugin_version,
            })
    return findings


# --------------------------------------------------------------------------
# LLM_Inference_Node packaging gate + version-item discriminator
# (vllm-triton-inference Requirements 7.1, 7.2, 8.1)
#
# ``llm_inference`` nodes compile only for VLLM_ARCHITECTURES, so a
# packaging request that includes any other architecture is rejected
# before compilation with the complete finding list (409, no component
# version registered — 7.2). Workflows without an ``llm_inference`` node
# always produce zero findings, keeping the pre-feature packaging path
# untouched (8.1). On success the packager records the
# ``has_llm_inference`` discriminator and the packaged architecture list
# on the workflow version item — the same way the camera-binding
# discriminator is written — so the Deployment_Service's vLLM
# architecture gate can evaluate workflow components without re-reading
# compiled documents from S3.
# --------------------------------------------------------------------------

#: The LLM_Inference_Node catalog type (workflow_core.catalog LLM_INFERENCE).
LLM_INFERENCE_TYPE_ID = 'llm_inference'

#: LLM packaging gate finding code (vllm-triton-inference 7.2).
GATE_LLM_ARCH_UNSUPPORTED = 'V6_LLM_ARCH_UNSUPPORTED'


def gather_llm_inference_node_ids(definition: Dict) -> List[str]:
    """Ids of the definition's ``llm_inference`` nodes, in definition
    order. Empty for every pre-feature workflow (8.1)."""
    node_ids: List[str] = []
    for node in definition.get('nodes') or []:
        if isinstance(node, dict) and node.get('type') == LLM_INFERENCE_TYPE_ID:
            node_id = node.get('id')
            if isinstance(node_id, str):
                node_ids.append(node_id)
    return node_ids


def llm_arch_gate_findings(definition: Dict, requested_archs: List[str]) -> List[Dict]:
    """
    The LLM architecture packaging gate (vllm-triton-inference 7.2), a
    pure structural twin of ``custom_plugin_gate_findings``: one finding
    ``{code: 'V6_LLM_ARCH_UNSUPPORTED', nodeId, arch}`` per
    (``llm_inference`` node, requested architecture outside
    ``VLLM_ARCHITECTURES``). Empty when the workflow has no
    ``llm_inference`` node (8.1) or every requested architecture
    supports vLLM execution.
    """
    llm_node_ids = gather_llm_inference_node_ids(definition)
    if not llm_node_ids:
        return []
    unsupported = [arch for arch in requested_archs
                   if arch not in VLLM_ARCHITECTURES]
    findings: List[Dict] = []
    for node_id in llm_node_ids:
        for arch in unsupported:
            findings.append({
                'code': GATE_LLM_ARCH_UNSUPPORTED,
                'message': (f"LLM inference node '{node_id}' cannot be "
                            f"packaged for architecture '{arch}': vLLM "
                            f"execution is supported only on "
                            f"{', '.join(VLLM_ARCHITECTURES)}"),
                'nodeId': node_id,
                'arch': arch,
            })
    return findings


# --------------------------------------------------------------------------
# Subscribed-topic recording for greengrass mqtt_subscribe triggers
# (trigger-activation-runtime Requirements 10.1, 10.4)
#
# A workflow whose ``mqtt_subscribe`` trigger nodes use the Greengrass IPC
# transport needs a ``SubscribeToIoTCore`` accessControl authorization at
# deployment time. The packager records the set of subscribed topic
# filters as ``subscribed_topics`` on the workflow version item and in the
# packaged manifest.json — ONLY when the set is non-empty, so every
# workflow without a greengrass-enabled ``mqtt_subscribe`` node packages
# byte-identically to pre-feature output (10.4). The deployment merge
# (deployments.py) reads the version-item copy.
# --------------------------------------------------------------------------

#: The MQTT subscribe trigger catalog type (workflow_core.catalog
#: MQTT_SUBSCRIBE, trigger-activation-runtime C1).
MQTT_SUBSCRIBE_TYPE_ID = 'mqtt_subscribe'


def gather_subscribed_topics(definition: Dict) -> List[str]:
    """The sorted, de-duplicated ``topic`` values of the definition's
    ``mqtt_subscribe`` nodes whose effective ``greengrass`` is true
    (Requirement 10.1).

    The effective value follows the validator's rule (``_effective_value``
    / gather_model_references): the explicitly set value when the key is
    present — an explicit null counts as cleared — else the declared
    default, which is false for ``greengrass``. Only the greengrass
    transport needs recipe accessControl, so aws_iot/broker subscribe
    nodes contribute nothing. Non-string/blank topics are excluded
    (validation's problem, not packaging's). Empty for every pre-feature
    workflow (10.4)."""
    topics: List[str] = []
    for node in definition.get('nodes') or []:
        if not isinstance(node, dict) or node.get('type') != MQTT_SUBSCRIBE_TYPE_ID:
            continue
        parameters = node.get('parameters')
        if not isinstance(parameters, dict):
            parameters = {}
        # Effective greengrass: explicit value if present, else the
        # declared default (False). Truthiness mirrors the V6/V8 target
        # checks.
        greengrass = parameters['greengrass'] if 'greengrass' in parameters else False
        if not greengrass:
            continue
        topic = parameters.get('topic')
        if isinstance(topic, str) and topic.strip() and topic not in topics:
            topics.append(topic)
    return sorted(topics)


# --------------------------------------------------------------------------
# Packaged/served vLLM model name alignment
# (vllm-model-name-mismatch Requirements 2.1, 2.2, 3.1, 3.2, 3.3)
#
# The publish pipeline serves a vLLM model under the SANITIZED registry
# name (model_naming.safe_model_name), so the packaged artifacts must
# carry each llm_inference node's modelName as that served name — a
# verbatim registry name like 'Qwen2.5-7B-Instruct-AWQ' guarantees a 409
# from the device Text_Generation_API. Both rewrites below are pure,
# copy-on-write, and keyed STRICTLY on the llm node type: no other node
# type or parameter is ever touched (3.2), stable names are a no-op
# (3.1), and the ORIGINAL definition keeps feeding
# gather_model_references so Model_Registry resolution stays keyed by
# the original registry names (3.3).
# --------------------------------------------------------------------------

def rewrite_llm_model_names(definition: Dict,
                            descriptors_by_id: Optional[Dict] = None) -> Dict:
    """A deep-copied definition with each ``llm_inference`` node's
    EFFECTIVE ``modelName`` — the explicitly set value when present, else
    the descriptor default (the validator's effective-value rule) —
    replaced by ``safe_model_name(value)``. Definitions without an
    ``llm_inference`` node come back equal to the input."""
    default = None
    descriptor = (descriptors_by_id or {}).get(LLM_INFERENCE_TYPE_ID)
    if descriptor is not None:
        for parameter in descriptor.parameters:
            if parameter.name == 'modelName':
                default = parameter.default
                break
    rewritten = copy.deepcopy(definition)
    for node in rewritten.get('nodes') or []:
        if not isinstance(node, dict) or node.get('type') != LLM_INFERENCE_TYPE_ID:
            continue
        parameters = node.get('parameters')
        if not isinstance(parameters, dict):
            parameters = {}
        value = parameters['modelName'] if 'modelName' in parameters else default
        if isinstance(value, str) and value:
            parameters['modelName'] = safe_model_name(value)
            node['parameters'] = parameters
    return rewritten


def rewrite_compiled_llm_model_names(document: Dict) -> Tuple[Dict, bool]:
    """``(document, changed)``: a compiled pipeline document with each
    ``llm_inference`` executor binding's ``modelName`` replaced by
    ``safe_model_name(value)``. Copy-on-write: when no binding needs a
    rewrite the input document is returned untouched with ``changed``
    False, so unaffected workflows keep their byte-identical
    serialization path."""
    def _bindings(doc):
        bindings = doc.get('executorBindings')
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict) or \
                    binding.get('binding') != LLM_INFERENCE_TYPE_ID:
                continue
            parameters = binding.get('parameters')
            if isinstance(parameters, dict):
                yield parameters

    if not any(isinstance(p.get('modelName'), str) and p['modelName']
               and safe_model_name(p['modelName']) != p['modelName']
               for p in _bindings(document)):
        return document, False
    rewritten = copy.deepcopy(document)
    for parameters in _bindings(rewritten):
        value = parameters.get('modelName')
        if isinstance(value, str) and value:
            parameters['modelName'] = safe_model_name(value)
    return rewritten, True


def packaged_workflow_definition_json(definition_json: str, definition_dict: Dict,
                                      descriptors_by_id: Dict) -> str:
    """The zip's ``workflow.json`` content: the stored definition with the
    llm ``modelName`` rewrite applied. When the rewrite is a no-op the
    stored ``definition_json`` string passes through byte-identically
    (3.1, 3.2); only a changed definition is re-serialized."""
    rewritten = rewrite_llm_model_names(definition_dict, descriptors_by_id)
    if rewritten == definition_dict:
        return definition_json
    return json.dumps(rewritten, sort_keys=True, indent=2, ensure_ascii=True)


def plugin_component_dependencies(dep_records: Dict[str, Optional[Dict]]) -> Dict:
    """The Greengrass ComponentDependencies block of the Workflow_Component
    recipe (16.4): one HARD dependency on dda.plugin.{pluginId} per distinct
    custom plugin, pinned to the recorded Plugin_Record version."""
    dependencies: Dict[str, Dict] = {}
    for dep in sorted(dep_records):
        record = dep_records[dep]
        if not record:
            continue
        dependencies[plugin_component_name(str(record['plugin_id']))] = {
            'VersionRequirement': plugin_version_requirement(record['version']),
            'DependencyType': 'HARD',
        }
    return dependencies


# --------------------------------------------------------------------------
# Workflow component dependency edges: model components + LocalServer
# (edge-deploy-reliability Requirements 2.8, 2.9, 3.8 — Defect C)
#
# The registered dda.workflow.* recipe declares HARD ComponentDependencies
# on every published model component the workflow uses (via its model_ref
# parameters) and on the LocalServer variant of each target architecture,
# merged with the existing dda.plugin.* entries in the packaging handler.
# This gives Greengrass the ordering/health relationship the JP6 incident
# lacked: the workflow (and the model components it drags in) no longer
# deploys with no dependency edge to the LocalServer backend actually
# executing it.
# --------------------------------------------------------------------------

def gather_model_references(definition: Dict, descriptors_by_id: Dict
                            ) -> List[str]:
    """The effective values of every ``model_ref``-typed parameter across
    the definition's nodes — today ``model_inference.modelName`` and
    ``llm_inference.modelName`` — deduplicated, in stable definition order.

    Generic over ``PARAM_TYPE_MODEL_REF`` (not node-type allowlists), so
    any future model-bound node type is covered automatically. The
    effective value follows the validator's rule: the explicitly set value
    when the key is present, else the declared default. Non-string/blank
    values are validation's problem (V4 / MODEL_REF_UNRESOLVED) and are
    skipped here.
    """
    references: List[str] = []
    for node in definition.get('nodes') or []:
        if not isinstance(node, dict):
            continue
        descriptor = descriptors_by_id.get(node.get('type'))
        if descriptor is None:
            continue
        parameters = node.get('parameters')
        if not isinstance(parameters, dict):
            parameters = {}
        for parameter in descriptor.parameters:
            if parameter.param_type != PARAM_TYPE_MODEL_REF:
                continue
            if parameter.name in parameters:
                value = parameters[parameter.name]
            else:
                value = parameter.default
            if isinstance(value, str) and value and value not in references:
                references.append(value)
    return references


def _resolve_vllm_components(record: Dict, archs, label: str) -> set:
    """Per-architecture resolution of a vLLM-shape record's platform-suffixed
    Per_JetPack_Component names (vllm-model-reload-after-backend-restart 2.6,
    design Decision 5). Returns a SET of suffixed component names — the
    vision resolved-value shape, so model_component_dependencies needs no
    change — or raises PackagingError when any selected architecture lacks
    suffixed coverage.

    Sources, in order:

    1. PRIMARY — ``published_component['components']`` (the per-JetPack
       entries the multi-arch vLLM publish writes back): entries with a
       non-empty string ``component_name`` whose ``architecture`` (an
       ``arm64_jpN`` arch id, the workflow's own vocabulary) is one of the
       selected archs contribute their suffixed names.
    2. SECONDARY — the record's plural ``published_components`` entries with
       ``status == 'published'``, matched on ``target`` against each selected
       arch's PRIMARY publish-target id (``ARCH_TO_PUBLISH_TARGET[arch]``,
       the ``jetson-xavier-jpN`` ids). The vision-only extra acceptance
       (ARCH_TO_EXTRA_PUBLISH_TARGETS, ``onnx-jetson-xavier-jp7``)
       deliberately does NOT apply to vLLM records.

    FAIL CLOSED (coverage gate): every selected architecture must be covered
    by at least one suffixed entry from either source, else PackagingError
    naming the model AND the uncovered architecture(s) — with the
    legacy-record remediation for records that carry only the unsuffixed
    base name. The unsuffixed base ``component_name`` (kept in the record as
    the component_name-index GSI key for legacy readers) is NEVER a fallback
    and NEVER appears in a resolved value: emitting it verbatim is exactly
    the incident's arch-agnostic HARD dependency (defect 1.6 — the JP6-era
    ``model-vllm-qwen3-vl-8b-instruct`` artifact dragging
    LocalServer.arm64JP6 onto the JP7 Thor).
    """
    published = record.get('published_component')
    published = published if isinstance(published, dict) else {}
    base_name = published.get('component_name')
    base_name = base_name if isinstance(base_name, str) else ''
    model_name = record.get('model_name')
    if not (isinstance(model_name, str) and model_name):
        model_name = label.split('/', 1)[-1]

    names_by_arch: Dict[str, set] = {arch: set() for arch in archs}

    def _suffixed_name(entry) -> Optional[str]:
        """The entry's component_name when it is usable suffixed evidence:
        a non-empty string that is NOT the unsuffixed base name (2.6)."""
        if not isinstance(entry, dict):
            return None
        entry_name = entry.get('component_name')
        if not (isinstance(entry_name, str) and entry_name):
            return None
        if base_name and entry_name == base_name:
            return None
        return entry_name

    # Primary source: per-JetPack ``components`` entries, matched on the
    # workflow's own arch vocabulary — no mirrored target map needed.
    per_jetpack = published.get('components')
    per_jetpack = per_jetpack if isinstance(per_jetpack, list) else []
    for entry in per_jetpack:
        entry_name = _suffixed_name(entry)
        if entry_name is None:
            continue
        arch = entry.get('architecture')
        if arch in names_by_arch:
            names_by_arch[arch].add(entry_name)

    # Secondary source: plural published entries matched on the PRIMARY
    # publish-target id only (an arch with no known primary target simply
    # gains no secondary coverage and the gate below fails closed on it).
    target_to_archs: Dict[str, set] = {}
    for arch in archs:
        primary = ARCH_TO_PUBLISH_TARGET.get(arch)
        if primary:
            target_to_archs.setdefault(primary, set()).add(arch)
    plural_entries = record.get('published_components')
    plural_entries = plural_entries if isinstance(plural_entries, list) else []
    for entry in plural_entries:
        if not isinstance(entry, dict) or entry.get('status') != 'published':
            continue
        entry_name = _suffixed_name(entry)
        if entry_name is None:
            continue
        for arch in target_to_archs.get(entry.get('target'), ()):
            names_by_arch[arch].add(entry_name)

    uncovered = sorted(arch for arch, names in names_by_arch.items()
                       if not names)
    if uncovered:
        # A legacy record carries only the unsuffixed base name: no usable
        # suffixed evidence in either source for ANY architecture.
        legacy = (not any(_suffixed_name(e) for e in per_jetpack)
                  and not any(_suffixed_name(e) for e in plural_entries
                              if isinstance(e, dict)
                              and e.get('status') == 'published'))
        if legacy:
            remediation = (
                "re-publish the model for every selected architecture - "
                "this record predates per-JetPack vLLM components")
        else:
            remediation = ("re-publish the model for every selected "
                           "architecture before packaging workflows that "
                           "use it")
        raise PackagingError(
            label,
            f"Model '{model_name}' has no platform-suffixed published vLLM "
            f"component covering the selected architecture(s) "
            f"[{', '.join(uncovered)}]; the arch-agnostic base component "
            f"name is never emitted as a dependency "
            f"(vllm-model-reload-after-backend-restart 2.6). {remediation}")
    resolved_names = set()
    for names in names_by_arch.values():
        resolved_names.update(names)
    return resolved_names


def resolve_model_components(model_names: List[str], usecase_id: str,
                             archs) -> Dict[str, Any]:
    """Resolve each referenced model name to its published Greengrass model
    component(s) through the Use_Case's Model_Registry — the same snapshot
    workflow_validation.py resolves ``model_ref`` parameters against
    (training-jobs table via ``usecase-training-index``, keyed by
    ``model_name``, plus the models-table published-name aliases).

    Both publish shapes are read (vision-model-packaging-regression
    2.1/2.2):

    - vLLM shape — ``published_component`` (singular map written by the
      vLLM publish path, carrying the arch-agnostic base
      ``component_name`` plus, since the multi-arch publish fix,
      platform-suffixed per-JetPack ``components`` entries): resolved
      PER SELECTED ARCHITECTURE through the suffixed evidence by
      _resolve_vllm_components (vllm-model-reload-after-backend-restart
      2.6, Decision 5) — the resolved value is a set of suffixed names,
      the vision shape; every selected architecture must be covered or
      PackagingError names the model and the uncovered arch(s); the
      unsuffixed base name is NEVER emitted. A singular map with no
      evidence at all (empty map / empty component_name and no
      per-JetPack entries) falls through to the plural/unpublished gates
      below unchanged.
    - vision shape — ``published_components`` (plural, per-compile-target
      list of ``{component_name, target, component_version, status}``
      entries written by greengrass_publish.py): entries with
      ``status == 'published'`` whose ``target`` matches one of the
      selected architectures' publish targets (ARCH_TO_PUBLISH_TARGET)
      contribute their names; the resolved value is that set of
      component names. EVERY selected architecture must be covered by at
      least one published entry, else PackagingError naming the model
      AND the uncovered architecture/target (edge-deploy-reliability
      Defect G, 2.19) — an accurate coverage error, unlike the
      misleading 'publish the model' message, which survives only for
      genuinely unpublished records (no singular component_name and no
      plural entries at all).

    Returns ``{model_name: resolved value}`` — a set of component names in
    both shapes — consumed by model_component_dependencies.

    FAIL CLOSED, mirroring the plugin gates: a model with no registry
    record, or no published component in either shape, raises
    PackagingError naming the model — the existing all-or-nothing path,
    so no component version is registered (2.5). A selected architecture
    with no published vision entry raises naming the model and the
    uncovered architecture (2.19), and an architecture with no known
    publish target raises too (the ARCH_TO_LOCAL_SERVER_COMPONENT naming
    discipline: never guess a target). When TRAINING_JOBS_TABLE is not
    configured the registry does not exist (pre-feature environment;
    validation skips model-reference resolution the same way) and model
    dependencies are skipped.
    """
    if not model_names:
        return {}
    if not TRAINING_JOBS_TABLE:
        logger.warning(
            'TRAINING_JOBS_TABLE not configured; model component '
            'dependencies are skipped for this packaging run')
        return {}
    snapshot = build_model_registry_snapshot(usecase_id, TRAINING_JOBS_TABLE,
                                             MODELS_TABLE, dynamodb)
    resolved: Dict[str, Any] = {}
    for name in model_names:
        label = f"models/{name}"
        record = snapshot.get(name)
        if not isinstance(record, dict):
            raise PackagingError(
                label,
                f"Model '{name}' referenced by the workflow has no record "
                f"in the Use_Case model registry; it may have been removed "
                f"since the workflow was validated")
        # vLLM shape: singular published_component map. The verbatim
        # short-circuit is GONE (vllm-model-reload-after-backend-restart
        # 2.6, Decision 5): a record carrying vLLM evidence — a non-empty
        # base component_name or per-JetPack ``components`` entries —
        # resolves per selected architecture through its platform-suffixed
        # entries, failing closed on any uncovered architecture. Evidence-
        # free singular maps keep falling through so the genuinely-
        # unpublished gate below is byte-identical.
        published = record.get('published_component')
        base_component_name = published.get('component_name') \
            if isinstance(published, dict) else None
        per_jetpack_entries = published.get('components') \
            if isinstance(published, dict) else None
        if (isinstance(base_component_name, str) and base_component_name) \
                or (isinstance(per_jetpack_entries, list)
                    and per_jetpack_entries):
            resolved[name] = _resolve_vllm_components(record, archs, label)
            continue
        # Vision shape: plural per-target published_components list.
        published_entries = [
            entry for entry in record.get('published_components') or []
            if isinstance(entry, dict)
            and entry.get('status') == 'published'
            and isinstance(entry.get('component_name'), str)
            and entry.get('component_name')]
        if not published_entries:
            raise PackagingError(
                label,
                f"Model '{name}' referenced by the workflow has no "
                f"published Greengrass component; publish the model before "
                f"packaging workflows that use it")
        targets_of_arch = {}
        for arch in archs:
            accepted = publish_targets_for_arch(arch)
            if not accepted:
                raise PackagingError(
                    label,
                    f"Cannot resolve published model components for "
                    f"architecture '{arch}': no known publish target. "
                    f"Supported architectures: "
                    f"{', '.join(sorted(ARCH_TO_PUBLISH_TARGET))}")
            targets_of_arch[arch] = accepted
        published_targets = {entry.get('target')
                             for entry in published_entries}
        uncovered = sorted(
            arch for arch, accepted in targets_of_arch.items()
            if not any(t in published_targets for t in accepted))
        if uncovered:
            # Fail closed naming the model AND the uncovered arch(s)
            # (edge-deploy-reliability Defect G, 2.19): an accurate
            # coverage error — re-publishing for the missing targets can
            # fix it, unlike the misleading 'publish the model' message.
            # Singleton archs render exactly as before ('(target X)');
            # arm64_jp7's multi-id acceptance renders as
            # '(targets X or Y)'.
            def _accepted_phrase(accepted):
                if len(accepted) == 1:
                    return f"target {accepted[0]}"
                return f"targets {' or '.join(accepted)}"
            raise PackagingError(
                label,
                f"Model '{name}' has no published Greengrass component "
                f"for the selected architecture(s) "
                f"{', '.join(f'{a} ({_accepted_phrase(targets_of_arch[a])})' for a in uncovered)}; "
                f"it is published for targets "
                f"[{', '.join(sorted(str(t) for t in published_targets))}]. "
                f"Publish the model for every selected architecture "
                f"before packaging workflows that use it")
        accepted_union = {t for accepted in targets_of_arch.values()
                          for t in accepted}
        names = {entry['component_name'] for entry in published_entries
                 if entry.get('target') in accepted_union}
        resolved[name] = names
    return resolved


def model_component_dependencies(resolved: Dict[str, Any]) -> Dict:
    """One HARD ComponentDependencies entry per distinct published model
    component of the workflow's resolved model references (2.8).

    Consumes resolve_model_components' per-model resolved values in both
    shapes: the singular vLLM published map (one ``component_name`` —
    emitted exactly as before) and the vision set of per-target component
    names. Per model: exactly one resolved name → one entry; MULTIPLE
    distinct names (per-target vision names diverging across the selected
    architectures) → that model's entries are OMITTED with a warning
    naming the model and the divergent components (the Defect F
    single-variant discipline — a recipe-global dependency block carrying
    per-target components is undeployable on any single device, 2.4);
    zero names (only possible with an empty architecture selection —
    resolution fails closed on any uncovered arch, 2.19) → nothing.
    Distinct models resolving to the SAME component name still dedupe to
    one entry.

    Deliberately UNPINNED ('>=0.0.0'), unlike the dda.plugin.* entries:
    model components version independently (major-only bumps on republish)
    and the deployment specifies the concrete version — this dependency's
    job is the ordering/health edge, not version pinning.
    """
    components = set()
    for model_name, value in resolved.items():
        if isinstance(value, dict):
            names = {value['component_name']}
        else:
            names = set(value)
        if not names:
            continue
        if len(names) > 1:
            logger.warning(
                "Model '%s' resolves to multiple published components "
                "[%s] across the selected architectures; omitting its "
                "model dependency entries — per-target component names "
                "diverge and a recipe-global HARD dependency on all of "
                "them would be undeployable on any single device",
                model_name, ', '.join(sorted(names)))
            continue
        components.add(next(iter(names)))
    return {name: {'VersionRequirement': '>=0.0.0',
                   'DependencyType': 'HARD'}
            for name in sorted(components)}


#: Recipe ComponentDependencies key prefix the arch-contradiction guard
#: inspects (vllm-model-reload-after-backend-restart 2.6, Decision 6).
_LOCAL_SERVER_DEP_PREFIX = 'aws.edgeml.dda.LocalServer.'


def _latest_component_version_arn(greengrass, component_name: str):
    """The versioned ARN of ``component_name``'s latest registered version
    in the Use_Case account, or None when the component is not found."""
    for page in greengrass.get_paginator('list_components').paginate(
            scope='PRIVATE'):
        for comp in page.get('components', []):
            if comp.get('componentName') == component_name:
                return (comp.get('latestVersion') or {}).get('arn')
    return None


def _model_arch_contradiction_guard(greengrass, resolved: Dict[str, Any],
                                    archs) -> None:
    """Defense-in-depth arch-contradiction guard
    (vllm-model-reload-after-backend-restart 2.6, design Decision 6),
    invoked AFTER model resolution: REFUSE packaging when a resolved model
    component's own recipe HARD-depends on a LocalServer variant serving
    NONE of the selected architectures.

    Per resolved model component name: fetch the component's LATEST version
    recipe from Greengrass (one ``get_component`` call, alongside the
    existing ``list_components`` version-resolution traffic), read its
    ``ComponentDependencies`` keys matching ``aws.edgeml.dda.LocalServer.*``,
    and compare each against the selected architectures' variants
    (ARCH_TO_LOCAL_SERVER_COMPONENT). A variant serving none of them raises
    PackagingError naming the model component, the contradicting LocalServer
    variant, and the target architecture(s) — the incident's blast radius
    (a wrong-arch LocalServer LINEAGE crash-looping a production device) is
    cheapest to stop at packaging time, and false positives are structurally
    implausible: a Per_JetPack_Component's LocalServer dependency IS its
    architecture identity (the vllm-multi-arch publish invariant).

    FAIL OPEN on reads: a recipe naming no LocalServer dependency, a
    component that cannot be found, or ANY read failure (throttle, transient
    API error, malformed recipe) logs a warning and proceeds — the guard is
    secondary (the per-arch resolution already prevents the incident class)
    and must never make packaging flakier than the primary fix.
    """
    selected_variants = {
        ARCH_TO_LOCAL_SERVER_COMPONENT[arch]
        for arch in archs if arch in ARCH_TO_LOCAL_SERVER_COMPONENT}
    component_names = set()
    for value in resolved.values():
        if isinstance(value, dict):
            # Tolerated legacy shape (pre-2.6 resolved values were the
            # singular published map); current resolution emits sets only.
            name = value.get('component_name')
            if isinstance(name, str) and name:
                component_names.add(name)
        else:
            component_names.update(
                name for name in value if isinstance(name, str) and name)
    for component_name in sorted(component_names):
        try:
            arn = _latest_component_version_arn(greengrass, component_name)
            if not arn:
                logger.warning(
                    'Arch-contradiction guard: resolved model component %s '
                    'not found in the Use_Case account (or has no latest '
                    'version); skipping the guard check for it',
                    component_name)
                continue
            recipe_blob = greengrass.get_component(
                recipeOutputFormat='JSON', arn=arn).get('recipe')
            if hasattr(recipe_blob, 'read'):
                recipe_blob = recipe_blob.read()
            dependencies = json.loads(recipe_blob).get(
                'ComponentDependencies') or {}
            local_server_variants = sorted(
                key for key in dependencies
                if isinstance(key, str)
                and key.startswith(_LOCAL_SERVER_DEP_PREFIX))
        except Exception as err:  # ANY read failure: warn and proceed
            logger.warning(
                'Arch-contradiction guard: could not read the recipe of '
                'resolved model component %s (%s); proceeding without the '
                'guard check for it - per-arch resolution already '
                'guarantees platform-suffixed dependencies',
                component_name, err)
            continue
        if not local_server_variants:
            logger.warning(
                'Arch-contradiction guard: resolved model component %s '
                'names no aws.edgeml.dda.LocalServer.* dependency in its '
                'recipe; proceeding', component_name)
            continue
        contradicting = [variant for variant in local_server_variants
                         if variant not in selected_variants]
        if contradicting:
            raise PackagingError(
                f"models/{component_name}",
                f"Model component '{component_name}' HARD-depends on "
                f"LocalServer variant(s) [{', '.join(contradicting)}] "
                f"serving none of the workflow's target architecture(s) "
                f"[{', '.join(sorted(archs))}] "
                f"(expected variant(s): "
                f"[{', '.join(sorted(selected_variants))}]). Deploying it "
                f"would drag a wrong-architecture LocalServer lineage onto "
                f"the device; re-publish or repoint the model component "
                f"for the selected architecture(s)")


def local_server_component_dependencies(archs) -> Dict:
    """The HARD LocalServer ComponentDependencies entry for the selected
    architectures — emitted ONLY when they collapse to exactly one distinct
    LocalServer variant (edge-deploy-reliability Defect F, 2.15/2.16/2.17).

    Single distinct variant (any single arch, or the x86_64 +
    x86_64_nvidia pair, which both run the single ``...amd64`` build):
    one entry carrying the arch's existing minimum-version floor
    (min_local_server_version_for) as the ``VersionRequirement`` — the
    same per-lineage discipline as the manifest's
    ``minLocalServerVersion``; when several archs collapse to the one
    variant, the floor is the maximum of their per-arch floors. FAILS
    CLOSED on an unknown arch (the greengrass_publish.
    TARGET_TO_LOCAL_SERVER naming discipline): the retired bare '.arm64'
    name is never emitted and no variant is guessed.

    Multiple distinct variants: return {} and log a warning naming the
    omitted variants. Greengrass ComponentDependencies is recipe-GLOBAL,
    not per-platform-manifest, and Greengrass installs the full recipe
    dependency closure regardless of the deployment document's component
    list — so a recipe carrying HARD deps on more than one LocalServer
    variant is undeployable on EVERY device (verified incident:
    dda.workflow.f81a4c66 v1.0.0 packaged for arm64_jp5 + arm64_jp6;
    deployment 44f2c596 to ryan-orin-nano failed FAILED_ROLLBACK_COMPLETE
    with the JP5 variant broken on the JP6 device). Deployability takes
    precedence over the LocalServer ordering/health edge, which is
    partially restored transitively through the model components' own
    LocalServer dependencies; model and plugin entries are unaffected
    either way.
    """
    def floor_key(version):
        return tuple(int(token) if token.isdigit() else 0
                     for token in version.split('.'))

    variant_floors: Dict[str, List[str]] = {}
    for arch in sorted(archs):
        component_name = ARCH_TO_LOCAL_SERVER_COMPONENT.get(arch)
        if not component_name:
            raise PackagingError(
                f"local-server/{arch}",
                f"Cannot resolve a LocalServer dependency for architecture "
                f"'{arch}': no known LocalServer variant. Supported "
                f"architectures: "
                f"{', '.join(sorted(ARCH_TO_LOCAL_SERVER_COMPONENT))}")
        variant_floors.setdefault(component_name, []).append(
            min_local_server_version_for(arch))
    if not variant_floors:
        return {}
    if len(variant_floors) > 1:
        logger.warning(
            'Workflow packaged for multiple LocalServer variants [%s]; '
            'omitting LocalServer ComponentDependencies — a recipe-global '
            'dependency closure spanning variants is undeployable on any '
            'single device, so deployability takes precedence over the '
            'ordering/health edge (model and plugin dependencies are '
            'unaffected)', ', '.join(sorted(variant_floors)))
        return {}
    component_name, floors = next(iter(variant_floors.items()))
    return {component_name: {
        'VersionRequirement': '>=' + max(floors, key=floor_key),
        'DependencyType': 'HARD',
    }}


def recipe_manifest_order(archs) -> List[str]:
    """Deterministic platform-manifest order: sorted, except the plain
    x86_64 manifest is listed after the x86_64_nvidia one — both map to
    Greengrass amd64, so attribute-less amd64 devices match plain x86_64
    while devices declaring 'runtime: nvidia' match the more specific
    manifest first (design: x86_64_nvidia)."""
    ordered = sorted(archs)
    if ARCH_X86_64 in ordered and ARCH_X86_64_NVIDIA in ordered:
        ordered.remove(ARCH_X86_64)
        ordered.insert(ordered.index(ARCH_X86_64_NVIDIA) + 1, ARCH_X86_64)
    return ordered


def load_custom_plugin_records(arch_custom_deps: Dict[str, List[str]],
                               dep_index: Dict[str, Dict]
                               ) -> Dict[str, Optional[Dict]]:
    """The backing Plugin_Record of every distinct compiled custom plugin
    dependency, resolved through the pinned Custom_Node_Type version
    (14.2). Unresolvable dependencies map to None (gates fail closed)."""
    records: Dict[str, Optional[Dict]] = {}
    for deps in arch_custom_deps.values():
        for dep in deps:
            if dep in records:
                continue
            item = dep_index.get(dep)
            if not item or item.get('plugin_id') is None \
                    or item.get('plugin_version') is None:
                records[dep] = None
                continue
            records[dep] = get_plugin_record_version(
                item['plugin_id'], int(item['plugin_version']))
    return records


def verify_custom_plugin_artifact(dependency: str, node_type_id: Optional[str],
                                  record: Dict, arch: str) -> Tuple[str, str]:
    """
    Stream one custom Plugin_Artifact's bytes for ``arch`` from the
    Plugin_Library, recompute the SHA-256, and KMS-Verify the recorded
    signature against the portal signing key (Requirement 10.4). Returns
    ``(manifest_key, checksum)`` for the arch manifest's pluginChecksums
    ({<pluginComponentName>/<file>: <sha256>}). Raises PackagingError —
    the existing all-or-nothing path (stage cleanup, no partial
    component) — on a missing artifact, checksum mismatch, or signature
    verification failure.
    """
    entry = (record.get('artifacts') or {}).get(arch) or {}
    so_key = entry['s3Key']
    so_name = posixpath.basename(so_key)
    component_name = plugin_component_name(str(record['plugin_id']))
    manifest_key = f"{component_name}/{so_name}"
    label = f"custom-plugins/{arch}/{so_name}"
    identity = (f"Custom plugin artifact '{so_name}' for architecture '{arch}' "
                f"(Custom_Node_Type '{node_type_id}', plugin "
                f"'{record.get('plugin_id')}' v{record.get('version')})")

    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=so_key)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        raise PackagingError(
            label,
            f"{identity} could not be read from the Plugin_Library "
            f"(s3://{PORTAL_ARTIFACTS_BUCKET}/{so_key}): {code or str(e)}")

    digest = hashlib.sha256()
    body = response['Body']
    while True:
        chunk = body.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)

    if digest.hexdigest() != entry['checksum']:
        raise PackagingError(
            label,
            f"{identity} failed checksum verification against the "
            f"Plugin_Record (Requirement 10.4)")

    try:
        signature = base64.b64decode(entry['signature'])
    except (TypeError, ValueError, binascii.Error):
        signature = None
    signature_valid = False
    if signature:
        try:
            verified = kms.verify(
                KeyId=PLUGIN_SIGNING_KEY_ARN,
                Message=digest.digest(),
                MessageType='DIGEST',
                SigningAlgorithm=SIGNING_ALGORITHM,
                Signature=signature,
            )
            signature_valid = bool(verified.get('SignatureValid'))
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code != 'KMSInvalidSignatureException':
                raise PackagingError(
                    label,
                    f"{identity} signature could not be verified: {code or str(e)}")
    if not signature_valid:
        raise PackagingError(
            label,
            f"{identity} failed signature verification against the portal "
            f"signing key (Requirement 10.4)")

    return manifest_key, entry['checksum']


def build_manifest(workflow_id: str, workflow_version: int, arch: str,
                   gst_plugins: List[str], python_packages: List[str],
                   custom_python_nodes: List[Dict], user: Dict,
                   plugin_checksums: Optional[Dict[str, str]] = None,
                   plugin_components: Optional[Dict[str, str]] = None,
                   component_version: Optional[str] = None,
                   workflow_name: Optional[str] = None,
                   subscribed_topics: Optional[List[str]] = None) -> Dict:
    """manifest.json content: what WorkflowWatcher needs to register the
    workflow and what the deployment compatibility check reads (8.4).

    ``plugin_checksums`` ({<pluginComponentName>/<file>: <sha256>}) and
    ``plugin_components`` ({<pluginComponentName>: <componentVersion>})
    let the LocalServer plugin loader verify each Plugin_Component-
    delivered custom plugin file and derive its install root
    (custom-node-designer Requirements 10.4, 10.6, 11.1).

    ``subscribed_topics`` (trigger-activation-runtime 10.1, 10.4): the
    greengrass mqtt_subscribe topic filters, recorded ONLY when non-empty
    so trigger-less manifests stay byte-identical to pre-feature output."""
    manifest = {
        'componentName': component_name_for(workflow_id),
        # The resolved (possibly patch-bumped) version when provided, else the
        # base version for the workflow version.
        'componentVersion': component_version or component_version_for(workflow_version),
        'workflowId': workflow_id,
        # Human-friendly workflow name (from the workflows table) so the
        # on-device deployed-workflows UI can label rows by name instead of the
        # opaque workflowId. Absent/None for pre-existing packages; the edge
        # falls back to workflowId in that case.
        'workflowName': workflow_name,
        'workflowVersion': int(workflow_version),
        'targetArch': arch,
        # Arch-scoped minimum: this package targets `arch`, so the scalar is
        # the minimum for that variant's lineage (backward-compatible field).
        'minLocalServerVersion': min_local_server_version_for(arch),
        # Full per-arch map so a variant-aware device selects the floor for
        # its own arch rather than comparing across incomparable lineages.
        'minLocalServerVersions': dict(MIN_LOCAL_SERVER_VERSIONS),
        'pluginDependencies': gst_plugins,
        'pythonDependencies': python_packages,
        'pluginChecksums': dict(plugin_checksums or {}),
        'pluginComponents': dict(plugin_components or {}),
        'customPythonNodeIds': [n['node_id'] for n in custom_python_nodes],
        'packagedAt': now_ms(),
        'packagedBy': user['user_id']
    }
    if subscribed_topics:
        manifest['subscribed_topics'] = list(subscribed_topics)
    return manifest


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
                                workflow_version: int, arch_zip_paths: Dict[str, str],
                                component_version: str
                                ) -> Tuple[str, Dict[str, str]]:
    """
    Upload every arch zip to a temporary staging prefix, then promote all
    of them to the final component prefix (Requirement 7.5).

    The final prefix includes the ``component_version`` so every (re-)packaged
    component version gets a UNIQUE artifact S3 URI. Greengrass reuses a
    cached artifact when a new component version reuses the same URI (it keys
    downloads/dedup on the artifact URI), which left re-packaged workflows
    running the original on-device artifact and never re-running the Install
    lifecycle (observed on JP6: a re-packaged workflow stayed pinned to the
    first package's manifest). A per-version URI forces a fresh fetch + Install
    on every re-package — matching how model components (unique artifact path
    per publish) already behave.

    Returns (final_prefix, {arch: final_key}). Raises PackagingError with
    the failing artifact identified; the caller cleans up.
    """
    stage_id = uuid.uuid4().hex
    stage_prefix = f"{STAGING_S3_PREFIX}/{workflow_id}/{workflow_version}/{stage_id}"
    final_prefix = (f"{COMPONENT_S3_PREFIX}/{workflow_id}/{workflow_version}/"
                    f"{component_version}")

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
                 final_keys: Dict[str, str],
                 component_dependencies: Optional[Dict] = None,
                 component_version: Optional[str] = None) -> Dict:
    """
    Greengrass recipe with one platform manifest per selected architecture
    (Requirements 7.2, 7.4). Each manifest carries a one-shot ``Run``
    lifecycle that copies the workflow artifacts under /aws_dda/workflows/
    and exits 0 (→ FINISHED); it starts no long-lived process, so deploying
    or removing the component never disturbs LocalServer or any other
    component (Requirement 13.3) — the LocalServer workflow engine discovers
    the copied files at runtime.

    ``Run`` (not ``Install``) is deliberate: Greengrass re-executes the
    ``Run`` lifecycle every time the component version changes, so a
    re-packaged workflow's fresh manifest/artifacts overwrite the on-disk
    copy on redeploy. An ``Install``-only generic component, by contrast, is
    installed once and its lifecycle is not re-run on in-place version
    updates, which left re-packaged workflows pinned to the first package's
    on-disk manifest (observed on JP6: the device kept the original
    ``minLocalServerVersion`` after re-packaging and never became runnable).

    Stale-version cleanup (stale-workflow-registrations 2.1) rides INSIDE
    the Run script, not a ``Shutdown`` lifecycle step: before staging, the
    script best-effort removes the workflow's whole directory tree
    (``rm -rf /aws_dda/workflows/{id}`` — every previously staged version,
    including a prior copy of the incoming one), then re-creates and
    re-copies the incoming version. A Shutdown step CANNOT be used for
    replace-time cleanup on these one-shot components: verified on-device,
    Greengrass transitions a FINISHED generic component RUNNING → STOPPING
    as soon as its Run script exits 0 and executes Shutdown ~10ms later, so
    a Shutdown ``rm -rf {install_dir}`` deleted the freshly staged
    artifacts on EVERY deploy (the workflow never registered), not just on
    replace/remove. With cleanup in Run, sibling version directories are
    removed exactly when a new version lands (Run re-executes on every
    component version change) and the re-copy-on-every-(re)start behavior
    is unchanged. The cleanup is best-effort (joined with ``;`` so a
    cleanup failure never blocks staging) while the ``mkdir && cp`` staging
    chain remains mandatory; ``rm -rf`` is a safe no-op when the workflow
    directory does not exist yet (first install).

    ``component_dependencies`` is the ComponentDependencies block
    declaring the Plugin_Components of the workflow's Custom_Node_Types
    (custom-node-designer Requirement 16.4); omitted when the workflow
    uses none.
    """
    component_name = component_name_for(workflow_id)
    component_version = component_version or component_version_for(workflow_version)
    install_dir = f"{DEVICE_WORKFLOWS_ROOT}/{workflow_id}/{workflow_version}"

    # Greengrass matches manifests on platform attributes. amd64 vs aarch64
    # separates the x86 flavors from the Jetson builds; when more than one
    # arm64 JetPack variant is packaged, a custom 'variant' attribute
    # (declared in the device's Nucleus platform overrides) disambiguates
    # them, and x86_64_nvidia always carries 'runtime: nvidia' (with the
    # plain x86_64 manifest ordered after it — recipe_manifest_order).
    arm_archs = [a for a in final_keys if ARCH_TO_GG_PLATFORM.get(a) == 'aarch64']
    disambiguate_arm = len(arm_archs) > 1

    manifests = []
    for arch in recipe_manifest_order(final_keys):
        platform = {'os': 'linux', 'architecture': ARCH_TO_GG_PLATFORM[arch]}
        if disambiguate_arm and ARCH_TO_GG_PLATFORM[arch] == 'aarch64':
            platform['variant'] = arch
        elif arch == ARCH_X86_64_NVIDIA:
            platform['runtime'] = 'nvidia'
        unarchived_dir = zip_artifact_name(arch)[:-len('.zip')]
        # Stale-version cleanup rides the Run script: best-effort remove
        # every previously staged version of THIS workflow (rm -rf is a
        # no-op when the dir doesn't exist yet), then re-create and re-copy
        # the incoming version. Deliberately NOT a Shutdown lifecycle step:
        # Greengrass runs Shutdown ~10ms after a one-shot Run exits 0
        # (RUNNING → STOPPING on FINISHED, verified on device), which would
        # delete the freshly staged artifacts on every deploy
        # (stale-workflow-registrations 2.1). The ';' keeps the cleanup
        # best-effort — a cleanup failure never blocks the mandatory
        # 'mkdir && cp' staging chain.
        run_script = (
            f"rm -rf {DEVICE_WORKFLOWS_ROOT}/{workflow_id} 2>/dev/null; "
            f"mkdir -p {install_dir} && "
            f"cp -r {{artifacts:decompressedPath}}/{unarchived_dir}/. {install_dir}/"
        )
        manifests.append({
            'Platform': platform,
            'Lifecycle': {
                # One-shot Run (not Install): re-executes on every component
                # version change so a re-packaged workflow's fresh manifest
                # overwrites the on-disk copy; exits 0 → FINISHED, starting
                # no long-lived process (Requirement 13.3).
                'Run': {
                    'Script': run_script,
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

    recipe = {
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
    if component_dependencies:
        recipe['ComponentDependencies'] = component_dependencies
    return recipe


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
    definition_dict = json.loads(definition_json)

    # LLM architecture packaging gate (vllm-triton-inference 7.2, 8.1):
    # evaluated before compilation so a request mixing an llm_inference
    # workflow with a non-vLLM architecture is rejected with the complete
    # finding list (409) and no component version is registered. Workflows
    # without an llm_inference node contribute zero findings.
    llm_node_ids = gather_llm_inference_node_ids(definition_dict)
    llm_findings = llm_arch_gate_findings(definition_dict, architectures)

    # Greengrass mqtt_subscribe topic filters (trigger-activation-runtime
    # 10.1, 10.4): recorded on the manifests and the version item only
    # when non-empty, so trigger-less packaging output is byte-identical
    # to pre-feature output.
    subscribed_topics = gather_subscribed_topics(definition_dict)
    if llm_findings:
        return error_response(
            409, GATE_LLM_ARCH_UNSUPPORTED, llm_findings[0]['message'],
            {'findings': llm_findings, 'version': version,
             'architectures': architectures})

    # Merged Node_Type_Catalog resolving the Custom_Node_Type versions
    # pinned at workflow save (custom-node-designer 14.2; built-in-only
    # workflows resolve to the built-in catalog unchanged).
    pinned_versions = version_item.get('custom_node_types') or {}
    node_type_items = load_registered_node_types(usecase_id)
    resolved_items = resolution_items(node_type_items, pinned_versions)
    catalog = resolve_catalog(descriptors_from_items(resolved_items))

    # Compile once per user-selected architecture (Requirement 7.4)
    compile_context = CompileContext(workflow_id=workflow_id, workflow_version=str(version))
    compiled_docs: Dict[str, Any] = {}
    for arch in architectures:
        result = compile_workflow(graph, arch, compile_context, simulation=False,
                                  catalog=catalog)
        if isinstance(result, list):
            return error_response(400, 'COMPILATION_FAILED',
                                  f"Workflow failed to compile for architecture '{arch}'",
                                  {'arch': arch,
                                   'errors': [e.to_dict() for e in result]})
        compiled_docs[arch] = result

    custom_python_nodes = gather_custom_python_nodes(graph)

    # Camera_Input_Node binding points (camera-registry-sync 8.6, 11.5):
    # one bindingPoints entry per camera node in each arch's compiled
    # document, plus the version-item discriminator recorded on success.
    # Custom Python source nodes additionally get a pythonSourceBinding
    # point so the device planner can locate the node's compiled appsrc
    # (custom-python-source 9.2); they never join camera_nodes, so the
    # camera-binding discriminator and camera_input_nodes record are
    # untouched. Workflows without camera or source nodes serialize
    # byte-identically to the plain compiler output.
    camera_nodes = gather_camera_input_nodes(
        graph, camera_backed_type_ids(resolved_items))
    python_source_nodes = gather_python_source_nodes(graph)
    binding_hints = binding_hints_from_definition(definition_dict)
    descriptors_by_id = {descriptor.type_id: descriptor for descriptor in catalog}

    # Packaged/served vLLM model name alignment (vllm-model-name-mismatch
    # 2.1): the zip's workflow.json carries each llm_inference node's
    # effective modelName rewritten to the sanitized served name. The
    # rewrite applies only to this artifact copy — the ORIGINAL
    # definition_dict keeps feeding gather_model_references below, so
    # Model_Registry resolution stays keyed by the original registry
    # names (3.3).
    packaged_definition_json = packaged_workflow_definition_json(
        definition_json, definition_dict, descriptors_by_id)

    arch_compiled_dicts: Dict[str, Dict] = {}
    arch_binding_points: Dict[str, List[Dict]] = {}
    arch_compiled_json: Dict[str, str] = {}
    for arch, compiled in compiled_docs.items():
        compiled_dict = compiled.to_dict()
        binding_points = build_binding_points(
            camera_nodes + python_source_nodes, compiled_dict, arch,
            binding_hints, descriptors_by_id)
        arch_compiled_dicts[arch] = compiled_dict
        arch_binding_points[arch] = binding_points
        arch_compiled_json[arch] = compiled_document_json(compiled, binding_points)

    # Split each arch's compiled plugin dependencies: curated plugins stay
    # bundled inline; custom: dependencies resolve to Plugin_Components.
    arch_gst_plugins: Dict[str, List[str]] = {}
    arch_python_packages: Dict[str, List[str]] = {}
    arch_custom_deps: Dict[str, List[str]] = {}
    for arch, compiled in compiled_docs.items():
        gst_plugins, custom_plugins, python_packages = split_plugin_dependencies(
            compiled.plugin_dependencies)
        arch_gst_plugins[arch] = gst_plugins
        arch_python_packages[arch] = python_packages
        arch_custom_deps[arch] = custom_plugins

    # Custom-plugin packaging gates before any assembly (11.2, 11.3, 16.4):
    # dev lifecycle state, a missing per-arch Plugin_Artifact, or a missing
    # Plugin_Component version rejects with the Custom_Node_Type and the
    # arch/state identified.
    dep_records: Dict[str, Optional[Dict]] = {}
    if any(arch_custom_deps.values()):
        dep_index = custom_dependency_index(resolved_items)
        dep_records = load_custom_plugin_records(arch_custom_deps, dep_index)
        findings = custom_plugin_gate_findings(arch_custom_deps, dep_index,
                                               dep_records)
        if findings:
            return error_response(
                409, findings[0]['code'], findings[0]['message'],
                {'findings': findings, 'version': version,
                 'architectures': architectures})
    else:
        dep_index = {}

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
        # Workflow dependency edges (edge-deploy-reliability 2.8, 2.9, 3.8
        # — Defect C): resolve the workflow's model_ref values to their
        # published model components (fail closed on unpublished models via
        # the PackagingError path below) and merge the model + per-arch
        # LocalServer HARD entries with the existing dda.plugin.* entries.
        # The three namespaces (dda.plugin.*, model-*,
        # aws.edgeml.dda.LocalServer.*) are disjoint, so the merge cannot
        # collide; plugin entries pass through byte-identical (3.8) and
        # build_recipe itself is unchanged.
        model_references = gather_model_references(definition_dict,
                                                   descriptors_by_id)
        resolved_models = resolve_model_components(model_references,
                                                   usecase_id, architectures)
        component_dependencies = {
            **plugin_component_dependencies(dep_records),
            **model_component_dependencies(resolved_models),
            **local_server_component_dependencies(architectures),
        }

        # Verify every custom Plugin_Artifact per selected architecture
        # against its Plugin_Record — streamed SHA-256 recompute + KMS
        # signature verification (10.4) — collecting the per-arch
        # pluginChecksums for the manifests. Custom .so files are never
        # bundled inline (16.4).
        arch_plugin_checksums: Dict[str, Dict[str, str]] = {}
        arch_plugin_components: Dict[str, Dict[str, str]] = {}
        verified_cache: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for arch in compiled_docs:
            arch_plugin_checksums[arch] = {}
            arch_plugin_components[arch] = {}
            for dep in arch_custom_deps[arch]:
                record = dep_records[dep]
                cache_key = (dep, arch)
                if cache_key not in verified_cache:
                    node_type_item = dep_index.get(dep) or {}
                    verified_cache[cache_key] = verify_custom_plugin_artifact(
                        dep, node_type_item.get('node_type_id'), record, arch)
                manifest_key, checksum = verified_cache[cache_key]
                arch_plugin_checksums[arch][manifest_key] = checksum
                arch_plugin_components[arch][
                    plugin_component_name(str(record['plugin_id']))] = \
                    component_version_for(record['version'])

        # Use_Case account clients via the assumed cross-account role (7.2).
        # Created before the manifests are built so the component version can be
        # resolved up front and stamped consistently into every manifest and
        # the recipe.
        session_name = f"wf-pkg-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        usecase_s3 = get_usecase_client('s3', usecase, session_name=session_name)
        greengrass = get_usecase_client('greengrassv2', usecase, session_name=session_name)

        # Defense-in-depth arch-contradiction guard
        # (vllm-model-reload-after-backend-restart 2.6, Decision 6): refuse
        # packaging when a resolved model component's recipe HARD-depends on
        # a LocalServer variant serving none of the selected architectures;
        # any read failure warns and proceeds.
        _model_arch_contradiction_guard(greengrass, resolved_models,
                                        architectures)

        # Greengrass component versions are immutable, so re-packaging an
        # unchanged workflow version (e.g. after a portal config change like a
        # new minLocalServerVersion) would collide with the existing
        # {version}.0.0. Resolve the next major (N.0.0) now and thread it
        # through the manifests and recipe: a major bump is what Greengrass
        # reliably re-installs on-device (a patch/minor bump can be left stale),
        # matching the model-component convention.
        resolved_component_version = next_component_version(
            greengrass, component_name_for(workflow_id), version)

        arch_zip_paths: Dict[str, str] = {}
        for arch, compiled in compiled_docs.items():
            gst_plugins = arch_gst_plugins[arch]
            manifest = build_manifest(
                workflow_id, version, arch, gst_plugins,
                arch_python_packages[arch], custom_python_nodes, user,
                plugin_checksums=arch_plugin_checksums[arch],
                plugin_components=arch_plugin_components[arch],
                component_version=resolved_component_version,
                workflow_name=item.get('name'),
                subscribed_topics=subscribed_topics)
            zip_path = os.path.join(work_dir, zip_artifact_name(arch))
            build_arch_zip(zip_path, arch, manifest, packaged_definition_json,
                           arch_compiled_json[arch], gst_plugins,
                           custom_python_nodes)
            arch_zip_paths[arch] = zip_path

        final_prefix, final_keys = stage_and_promote_artifacts(
            usecase_s3, usecase_bucket, workflow_id, version, arch_zip_paths,
            resolved_component_version)

        # Register only after every artifact uploaded successfully (7.5);
        # the recipe pins each custom plugin's Plugin_Component (16.4).
        recipe = build_recipe(workflow_id, version, usecase_bucket, final_keys,
                              component_dependencies=component_dependencies,
                              component_version=resolved_component_version)
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
    for arch in compiled_docs:
        portal_key = compiled_doc_portal_key(usecase_id, workflow_id, version, arch)
        s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=portal_key,
                      Body=arch_compiled_json[arch].encode('utf-8'),
                      ContentType='application/json')
        compiled_arch_keys[arch] = portal_key

    # The dependency closure of the packaged Workflow_Component: every
    # depended-on Plugin_Component (dda.plugin.* name -> component version)
    # across all packaged architectures. Recorded on the version item so
    # the Deployment_Service's pre-submit lifecycle/architecture gates can
    # evaluate the closure without re-resolving the recipe
    # (custom-node-designer task 10.5, Requirements 9.7, 9.8, 16.3, 16.6).
    workflow_plugin_components: Dict[str, str] = {}
    for arch_map in arch_plugin_components.values():
        workflow_plugin_components.update(arch_map)

    # The version-item binding discriminator (camera-registry-sync 8.6,
    # 11.5): has_binding_points separates the strict deploy-time binding
    # rule from legacy leniency, and camera_input_nodes feeds the binding
    # matrix and the legacy compiled-path check (9.5) without re-reading
    # compiled documents from S3.
    camera_input_nodes = camera_input_nodes_record(
        camera_nodes, binding_hints, arch_binding_points, arch_compiled_dicts)

    # The version-item LLM discriminator (vllm-triton-inference 7.1, 8.5,
    # 8.6), written the same way the camera-binding discriminator is:
    # has_llm_inference activates the Deployment_Service's vLLM
    # architecture gate for this workflow component, and
    # packaged_architectures is the arch set the gate compares device
    # architectures against (3.3) without re-reading compiled documents.
    # Greengrass mqtt_subscribe topic filters (trigger-activation-runtime
    # 10.1, 10.4): the attribute is written ONLY when the set is non-empty,
    # keeping the version item byte-identical to pre-feature output for
    # every workflow without a greengrass-enabled mqtt_subscribe node.
    update_expression = ('SET component_arn = :arn, component_version = :cv, '
                         'compiled_arch_keys = :keys, '
                         'plugin_components = :pc, '
                         'has_binding_points = :hbp, '
                         'camera_input_nodes = :cin, '
                         'has_llm_inference = :hli, '
                         'packaged_architectures = :pa, '
                         'packaged_at = :at, packaged_by = :by')
    update_values = {
        ':arn': component_arn,
        ':cv': resolved_component_version,
        ':keys': compiled_arch_keys,
        ':pc': workflow_plugin_components,
        ':hbp': bool(camera_nodes),
        ':cin': _dynamo_safe(camera_input_nodes),
        ':hli': bool(llm_node_ids),
        ':pa': architectures,
        ':at': now_ms(),
        ':by': user['user_id']
    }
    if subscribed_topics:
        update_expression += ', subscribed_topics = :st'
        update_values[':st'] = subscribed_topics
    dynamodb.Table(WORKFLOW_VERSIONS_TABLE).update_item(
        Key={'workflow_id': workflow_id, 'version': version},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=update_values
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
            'component_version': resolved_component_version,
            'component_arn': component_arn
        }
    )

    return create_response(201, {
        'workflow_id': workflow_id,
        'version': version,
        'component_name': component_name_for(workflow_id),
        'component_version': resolved_component_version,
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
