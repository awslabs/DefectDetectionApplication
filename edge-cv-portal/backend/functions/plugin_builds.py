"""
Plugin_Build_Service orchestration Lambda function (Custom Node Designer)

Build orchestration, per-arch build status, artifact + signature
recording, and the prebuilt-binary upload path for Plugin_Records
(Requirements 1.6, 3.1, 3.3, 3.4, 3.5, 3.6, 5.1, 5.2).

Routes (API Gateway REST):
    POST /plugins/{id}/versions/{v}/build     Submit the version's source to
                                              the per-arch CodeBuild projects
                                              (marks per-arch build status
                                              "building", 3.1; a plugin-set
                                              import's recorded
                                              selected_plugins are passed to
                                              StartBuild as the
                                              PLUGIN_TARGETS env override)
                                              and/or accept
                                              prebuilt .so binaries per arch
                                              (checksummed + signed identically,
                                              provenance prebuilt: true, 3.6)
    GET  /plugins/{id}/versions/{v}/builds    Per-arch build status for the
                                              Node_Designer UI (3.5), incl.
                                              per-arch staleness, the
                                              Build_Target_Registry, and the
                                              component pointer summary
                                              (custom-node-source-lifecycle)
    POST /plugins/{id}/versions/{v}/architectures
                                              Architecture_Addition: append
                                              Target_Architectures to the
                                              version and build only those
                                              (custom-node-source-lifecycle 6)

EventBridge (rule 'dda-portal-plugin-build-results'):
    CodeBuild Build State Change events for the dda-plugin-build-{arch}
    projects AND the dda-plugin-fetch repository-fetch project are
    delivered to this same handler. Fetch results are delegated to
    plugin_importer.handle_fetch_result (same Lambda bundle), which
    advances the asynchronously imported Plugin_Record out of its
    'fetching' import status. Per-arch build result recording is
    idempotent on the build id: a duplicate delivery, or an event from a
    superseded build, never double-records an artifact.

    - SUCCEEDED: the build image has promoted the .so to the Plugin_Library
      custom prefix (workflow-plugins/custom/{usecase_id}/{arch}/{plugin}.so).
      The handler streams the promoted artifact, records its SHA-256
      checksum, signs the digest with the portal KMS key (ECDSA P-256),
      stores the detached signature alongside as {plugin}.so.sig, and
      records {s3Key, checksum, signature, buildStatus} on the
      Plugin_Record (3.3).
    - FAILED / FAULT / STOPPED / TIMED_OUT: the CloudWatch log tail is
      recorded on the per-arch entry and no artifact is stored (3.4).

When all requested Target_Architecture builds have settled with at least
one success, plugin_components.py (auto Plugin_Component packaging,
Requirement 16.1) is invoked asynchronously by name; its absence or
failure never fails the build (design: "auto-packaging failure never
fails the build"). The trigger is idempotent via a conditional
components_triggered marker on the Plugin_Record version.

DeepStream-flagged records restrict selectable architectures to the
JetPack builds arm64_jp5/jp6 (5.1); the JetPack build projects pin
the DeepStream SDK matching each release (5.2, infrastructure).

Access control: build submission and prebuilt upload require
node-designer:manage (UseCaseAdmin within the Use_Case, PortalAdmin);
build status is readable by every role of the Use_Case.
"""
import base64
import hashlib
import json
import logging
import os
import posixpath
import re
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event, Permission
)
from workflow_core.catalog import (
    DEVICE_ARCHITECTURES,
    DEEPSTREAM_ARCHITECTURES,
)

# Reuse the Plugin_Record item shape, persistence helpers, and error
# envelope from plugin_records.py (same deployment bundle).
import plugin_records
from plugin_records import (
    STATE_PROD,
    artifact_source_revision,
    authorize_record_access,
    error_response,
    get_version_item,
    not_found_response,
    now_ms,
    parse_body,
    plugin_table,
    source_revision_of,
    stale_architectures,
    successful_build_archs,
)
# Architecture_Addition renders the missing per-arch build configuration
# of scaffold-kind records (custom-node-source-lifecycle 6.3).
from workflow_core.scaffold import ScaffoldError, build_config_path, render_scaffold
# Introspection_Report shape validation (gst-parameter-prepopulation
# design component 4): pure module shipped alongside this handler.
from gst_properties import (
    ReportError,
    STATUS_CAPTURED,
    STATUS_FAILED,
    parse_report,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
s3 = boto3.client('s3')
kms = boto3.client('kms')
codebuild = boto3.client('codebuild')
logs_client = boto3.client('logs')
lambda_client = boto3.client('lambda')

# Environment variables (node-designer-stack.ts lambdaEnvironment)
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
PLUGIN_SIGNING_KEY_ARN = os.environ.get('PLUGIN_SIGNING_KEY_ARN')
PLUGIN_COMPONENTS_FUNCTION_NAME = os.environ.get('PLUGIN_COMPONENTS_FUNCTION_NAME')
PLUGIN_STAGING_PREFIX = os.environ.get('PLUGIN_STAGING_PREFIX', 'plugin-staging')
PLUGIN_LIBRARY_CUSTOM_PREFIX = os.environ.get(
    'PLUGIN_LIBRARY_CUSTOM_PREFIX', 'workflow-plugins/custom')

#: arch -> CodeBuild project name (BuildProjectsJson stack output)
BUILD_PROJECTS: Dict[str, str] = json.loads(os.environ.get('BUILD_PROJECTS_JSON', '{}'))
#: project name -> arch (EventBridge result attribution)
PROJECT_ARCHITECTURES: Dict[str, str] = {v: k for k, v in BUILD_PROJECTS.items()}

#: The lightweight repository-fetch project (plugin_importer.py's
#: asynchronous import). Its build state changes arrive on the same
#: EventBridge rule and are delegated to
#: plugin_importer.handle_fetch_result. Defaults to the fixed project
#: name node-designer-stack.ts assigns (this Lambda's environment does
#: not carry the variable).
FETCH_PROJECT_NAME = os.environ.get('FETCH_PROJECT_NAME', 'dda-plugin-fetch')

# ---------------------------------------------------------------- constants

# Per-arch build statuses on the Plugin_Record artifacts map. "queued" is
# written by plugin_importer.py at import time; once the fetch settles
# the record to 'imported' the importer calls start_queued_builds (same
# Lambda bundle) which advances queued -> building, and the EventBridge
# result handler settles building -> succeeded/failed.
BUILD_QUEUED = 'queued'
BUILD_BUILDING = 'building'
BUILD_SUCCEEDED = 'succeeded'
BUILD_FAILED = 'failed'
SETTLED_STATUSES = (BUILD_SUCCEEDED, BUILD_FAILED)

#: CodeBuild terminal statuses that map to a failed Plugin_Artifact build.
CODEBUILD_FAILURE_STATUSES = ('FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT')

#: Signing algorithm for the portal ECDSA P-256 key (3.3).
SIGNING_ALGORITHM = 'ECDSA_SHA_256'

#: CloudWatch log excerpt recorded for failed builds (3.4). A failed
#: CodeBuild run emits ~100+ lines of post-failure phase/upload
#: boilerplate after the actual compiler/configure error, so the
#: excerpt fetches a generous window and centers on the last real
#: error before the BUILD-phase failure marker.
LOG_TAIL_MAX_EVENTS = 500
LOG_TAIL_MAX_CHARS = 8 * 1024
#: Context lines preserved before the last matched error line.
LOG_TAIL_CONTEXT_BEFORE = 40

#: The CodeBuild phase-failure marker: everything after it is
#: post-failure boilerplate (artifact upload, phase bookkeeping).
BUILD_FAILED_MARKER = re.compile(
    r'Phase complete: \w+ State: FAILED')

#: Lines that look like the actual failure cause.
ERROR_LINE_PATTERN = re.compile(
    r'\bERROR\b|error:|\berror\b:|Error while executing|fatal error'
    r'|undefined reference|No such file|command not found'
    r'|ninja: build stopped|FAILED:', re.IGNORECASE)

#: Maximum prebuilt .so accepted inline (base64) - bounded by the API
#: Gateway payload limit; larger binaries arrive via source_key (an
#: object already synced under the version's plugin-sources prefix).
MAX_PREBUILT_INLINE_BYTES = 6 * 1024 * 1024

#: Property_Introspection runs on x86_64 only (gst-parameter-prepopulation
#: design: GObject property declarations are architecture-independent and
#: the x86_64 build is the designer's gating artifact everywhere else).
INTROSPECTION_ARCH = 'x86_64'

#: Size cap on stored Introspection_Report objects
#: (gst-parameter-prepopulation design component 3).
GST_REPORT_MAX_BYTES = 256 * 1024


# ------------------------------------------------------------ pure helpers

def sanitize_plugin_name(name: Optional[str], fallback: str) -> str:
    """File-system/S3-safe plugin base name for {plugin}.so keys"""
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '-', name or '').strip('-.')
    return cleaned or fallback


def library_so_key(usecase_id: str, arch: str, plugin_name: str) -> str:
    """Plugin_Library custom key: workflow-plugins/custom/{usecase}/{arch}/{plugin}.so

    This is the UNVERSIONED promotion target the per-arch build image
    writes to — every build of every version of the plugin overwrites
    it. It is only a hand-off location: the recorded Plugin_Artifact
    lives at :func:`versioned_library_so_key`, immutable per version.
    """
    return f"{PLUGIN_LIBRARY_CUSTOM_PREFIX}/{usecase_id}/{arch}/{plugin_name}.so"


def versioned_library_so_key(usecase_id: str, arch: str, plugin_id: str,
                             version: int, plugin_name: str) -> str:
    """Immutable per-version Plugin_Library key:
    workflow-plugins/custom/{usecase}/{arch}/{plugin_id}/{version}/{plugin}.so

    The unversioned promotion key is overwritten by every rebuild, so a
    Plugin_Record version whose artifact stayed there fails checksum
    verification (Requirement 10.4) as soon as ANY later version builds —
    breaking workflow packaging for every workflow pinned to the older
    Custom_Node_Type version (observed: resize-image v2 build broke
    packaging of workflows pinned to v1). Recording the artifact at a
    version-scoped key keeps each version's bytes verifiable forever.
    """
    return (f"{PLUGIN_LIBRARY_CUSTOM_PREFIX}/{usecase_id}/{arch}/"
            f"{plugin_id}/{int(version)}/{plugin_name}.so")


def signature_key(so_key: str) -> str:
    """Detached signature key alongside the artifact (design S3 layout)"""
    return so_key + '.sig'


def gst_report_key(so_key: str) -> str:
    """Introspection_Report key alongside the promoted artifact
    ({plugin}.so.gstinspect.json, gst-parameter-prepopulation design)"""
    return so_key + '.gstinspect.json'


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_id_from_arn(build_arn: str) -> str:
    """Extract 'project:uuid' from the EventBridge detail build-id ARN"""
    if ':build/' in build_arn:
        return build_arn.split(':build/', 1)[1]
    return build_arn


def env_vars_from_detail(detail: Dict) -> Dict[str, str]:
    """Environment variables of the finished build (StartBuild overrides)"""
    env = ((detail.get('additional-information') or {})
           .get('environment') or {}).get('environment-variables') or []
    return {var.get('name'): var.get('value') for var in env
            if isinstance(var, dict) and var.get('name')}


def plugin_targets_value(item: Dict) -> str:
    """
    PLUGIN_TARGETS env value for the per-arch CodeBuild builds: the
    comma-separated individual plugin names the user selected for a
    plugin-set import (recorded as selected_plugins by
    plugin_importer.py's select-plugins endpoint), or '' when no
    selection exists (single-plugin repositories, scaffolds, generated
    plugins). The build image entrypoint builds only the named plugin
    targets when PLUGIN_TARGETS is non-empty, and the whole source tree
    otherwise.
    """
    selected = item.get('selected_plugins') or []
    return ','.join(str(name) for name in selected)


def selected_plugins_value(item: Dict) -> str:
    """
    SELECTED_PLUGINS env value for the per-arch CodeBuild builds: the
    comma-separated plugin selection the record's provenance carries
    (provenance.selectedPlugins — written by the import-time selection
    on POST /plugins/import and by the select-plugins endpoint), with
    the record-level selected_plugins field as fallback; '' when no
    selection exists. The build image entrypoint meson-enables only the
    named plugins (-Dauto_features=disabled -D<plugin>=enabled) when
    SELECTED_PLUGINS is present.
    """
    provenance = item.get('provenance') or {}
    selected = (provenance.get('selectedPlugins')
                or item.get('selected_plugins') or [])
    return ','.join(str(name) for name in selected)


def arch_source_prefix(item: Dict, arch: str) -> str:
    """
    S3 source prefix one architecture's build reads from. Multi-revision
    imports (plugin_importer arch_revisions) record a per-revision fetch
    map on the version item — the arch resolves through
    arch_revisions[arch] -> fetches[slug].source_prefix so architectures
    pinned to different source revisions build from their own synced
    tree. Everything else (single-revision imports, scaffolds, generated
    plugins, existing records) falls back to the flat source_s3_prefix.
    """
    slug = (item.get('arch_revisions') or {}).get(arch)
    fetch = (item.get('fetches') or {}).get(slug) if slug else None
    prefix = (fetch or {}).get('source_prefix')
    return prefix or item.get('source_s3_prefix') or ''


def requested_architectures(item: Dict) -> List[str]:
    """Architectures whose builds this version is waiting on — the
    monotonic union of every architecture ever requested for the version
    (custom-node-source-lifecycle 6.5), read through the current
    architecture set: an architecture that is no longer a target (e.g. the
    retired arm64_jp4) is dropped, so a legacy record can never wait on, or
    default a rebuild to, a build that no longer exists."""
    requested = item.get('requested_architectures')
    if requested:
        archs = [str(a) for a in requested]
    else:
        archs = sorted((item.get('artifacts') or {}).keys())
    return [a for a in archs if a in DEVICE_ARCHITECTURES]


def union_requested_architectures(item: Dict, added: List[str]) -> List[str]:
    """The requested-architecture set after a build round or an
    Architecture_Addition: never drops a previously requested arch
    (custom-node-source-lifecycle 6.4, 6.5, Property 5)."""
    return sorted(set(requested_architectures(item)) | set(added))


def buildable_architectures() -> List[str]:
    """The Build_Target_Registry: architectures with a configured
    CodeBuild project (custom-node-source-lifecycle 6.7)."""
    return sorted(BUILD_PROJECTS)


def is_stale(item: Dict, entry: Optional[Dict]) -> bool:
    """A Plugin_Artifact is stale when it was built from a Source_Revision
    lower than the version's current one (custom-node-source-lifecycle
    1.9); legacy items/entries read as revision 1 (10.1)."""
    return artifact_source_revision(entry) < source_revision_of(item)


# Record kinds whose Source_Tree is rendered from a recorded declaration and
# therefore carries one `builds/{arch}/meson.build` per Target_Architecture:
# the create wizard's `scaffold` and the Node_Generator's `generated`. An
# Architecture_Addition must render that file for the added architecture or
# the build fails with "no build configuration found".
SCAFFOLDED_KINDS = ('scaffold', 'generated')

ADDITION_REASON_UNKNOWN = 'unknown'
ADDITION_REASON_ALREADY_REQUESTED = 'already_requested'
ADDITION_REASON_UNAVAILABLE = 'unavailable'
ADDITION_REASON_DEEPSTREAM = 'deepstream_restricted'


def validate_addition(requested: Any, existing: List[str], registry: List[str],
                      deepstream: bool) -> Tuple[List[str], Dict[str, str]]:
    """
    Validate an Architecture_Addition request (custom-node-source-
    lifecycle 6.1, Property 6). Returns (accepted_architectures,
    rejections) where rejections maps each offending architecture to
    exactly one reason: unknown | already_requested | unavailable |
    deepstream_restricted. The request is acceptable iff rejections is
    empty and accepted is non-empty.
    """
    rejections: Dict[str, str] = {}
    accepted: List[str] = []
    if not isinstance(requested, list):
        return [], {}
    for arch in requested:
        arch = str(arch)
        if arch in rejections or arch in accepted:
            continue
        if arch not in DEVICE_ARCHITECTURES:
            rejections[arch] = ADDITION_REASON_UNKNOWN
        elif arch in existing:
            rejections[arch] = ADDITION_REASON_ALREADY_REQUESTED
        elif arch not in registry:
            rejections[arch] = ADDITION_REASON_UNAVAILABLE
        elif deepstream and arch not in DEEPSTREAM_ARCHITECTURES:
            rejections[arch] = ADDITION_REASON_DEEPSTREAM
        else:
            accepted.append(arch)
    return sorted(accepted), rejections


def plan_build_configs(declaration: Dict, existing: List[str], added: List[str],
                       present_paths: List[str]) -> Tuple[Dict[str, str], Dict]:
    """
    Plan the per-architecture build configuration files an
    Architecture_Addition renders for a scaffold-kind record (custom-node-
    source-lifecycle 6.3, Property 7): exactly build_config_path(arch) for
    each added architecture whose file is not already present, and the
    declaration with its `architectures` extended by the added ones (in
    order, without duplicates). Never plans an overwrite.
    """
    ordered = [str(a) for a in existing]
    for arch in added:
        if arch not in ordered:
            ordered.append(arch)
    extended = dict(declaration)
    extended['architectures'] = ordered
    rendered = render_scaffold(extended)
    present = set(present_paths)
    planned = {}
    for arch in added:
        path = build_config_path(arch)
        if path not in present and path in rendered:
            planned[path] = rendered[path]
    return planned, extended


def builds_settled(item: Dict) -> bool:
    """True when every requested Target_Architecture build has settled"""
    artifacts = item.get('artifacts') or {}
    requested = requested_architectures(item)
    if not requested:
        return False
    return all(
        (artifacts.get(arch) or {}).get('buildStatus') in SETTLED_STATUSES
        for arch in requested
    )


def validate_architectures(architectures: List[str],
                           deepstream: bool) -> Optional[Tuple[str, str, Dict]]:
    """
    Validate a Target_Architecture selection; returns None or an error
    tuple (code, message, details). DeepStream-flagged records restrict
    the selectable architectures to arm64_jp5/jp6 (5.1).
    """
    invalid = [a for a in architectures if a not in DEVICE_ARCHITECTURES]
    if invalid:
        return ('INVALID_ARCHITECTURES',
                f"Unknown Target_Architectures: {', '.join(sorted(invalid))}",
                {'valid': list(DEVICE_ARCHITECTURES)})
    if deepstream:
        non_jetson = [a for a in architectures
                      if a not in DEEPSTREAM_ARCHITECTURES]
        if non_jetson:
            return ('INVALID_ARCHITECTURES',
                    'DeepStream-flagged plugins may only target: '
                    f"{', '.join(DEEPSTREAM_ARCHITECTURES)}",
                    {'invalid': sorted(non_jetson)})
    return None


def builds_view(item: Dict) -> Dict:
    """Per-arch build status view for the Node_Designer UI (3.5), extended
    with per-arch staleness, the version's Source_Revision, the
    Build_Target_Registry, and the Plugin_Component pointer summary
    (custom-node-source-lifecycle 1.8, 6.7, 8.3)"""
    artifacts = item.get('artifacts') or {}
    component = item.get('component') or {}
    return {
        'plugin_id': item['plugin_id'],
        'version': item['version'],
        'requested_architectures': requested_architectures(item),
        'builds': {
            arch: {
                'buildStatus': (entry or {}).get('buildStatus'),
                's3Key': (entry or {}).get('s3Key'),
                'checksum': (entry or {}).get('checksum'),
                'signature': (entry or {}).get('signature'),
                'logTail': (entry or {}).get('logTail', ''),
                'prebuilt': (entry or {}).get('prebuilt', False),
                'sourceRevision': artifact_source_revision(entry),
                'stale': bool((entry or {}).get('buildStatus'))
                and is_stale(item, entry),
            }
            for arch, entry in artifacts.items()
        },
        'settled': builds_settled(item),
        'component_packaging_triggered': bool(item.get('components_triggered')),
        'source_revision': source_revision_of(item),
        'stale_architectures': stale_architectures(item),
        'buildable_architectures': buildable_architectures(),
        'component': {
            'version': component.get('version'),
            'revision': int(component.get('revision') or 0),
            'architectures': [str(a) for a in component.get('architectures') or []],
            'status': component.get('status'),
        },
    }


# --------------------------------------------------------- signing/storage

def sign_digest(digest: bytes) -> str:
    """KMS-sign a SHA-256 digest with the portal signing key (3.3)"""
    response = kms.sign(
        KeyId=PLUGIN_SIGNING_KEY_ARN,
        Message=digest,
        MessageType='DIGEST',
        SigningAlgorithm=SIGNING_ALGORITHM,
    )
    return base64.b64encode(response['Signature']).decode('ascii')


def store_signed_artifact(usecase_id: str, arch: str, plugin_name: str,
                          data: bytes, plugin_id: str, version: int) -> Dict:
    """
    Store artifact bytes to the Plugin_Library with the detached
    signature ({plugin}.so + {plugin}.so.sig) at the immutable
    per-version key and return the per-arch artifact entry fields
    (s3Key, checksum, signature). Used by the prebuilt upload path (3.6).
    """
    so_key = versioned_library_so_key(usecase_id, arch, plugin_id, version,
                                      plugin_name)
    checksum = sha256_hex(data)
    signature = sign_digest(hashlib.sha256(data).digest())
    s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=so_key, Body=data)
    s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=signature_key(so_key),
                  Body=base64.b64decode(signature))
    return {'s3Key': so_key, 'checksum': checksum, 'signature': signature}


def build_gst_introspection_stanza(so_key: str) -> Dict:
    """
    gstIntrospection stanza for the x86_64 artifact entry
    (gst-parameter-prepopulation Requirements 1.1, 1.6): fetch the
    Introspection_Report the build uploaded next to the promoted .so
    ({plugin}.so.gstinspect.json), enforce the 256 KiB size cap, and
    validate its shape via gst_properties.parse_report.

    Returns either:
      - {status: "captured", s3Key, gstVersion, capturedAt} for a valid
        captured report, or
      - {status: "failed", message} for a report that itself recorded a
        capture failure (its message is carried through), a missing
        object, an oversized object, or malformed JSON/shape (8.3
        handled at write time too).

    Never raises: any unexpected error is folded into a failed stanza so
    the SUCCEEDED handling is untouched (Requirement 1.4).
    """
    report_key = gst_report_key(so_key)
    try:
        try:
            obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                                Key=report_key)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return {'status': STATUS_FAILED,
                        'message': 'No introspection report was uploaded '
                                   'by the build'}
            raise

        size = obj.get('ContentLength')
        if size is not None and size > GST_REPORT_MAX_BYTES:
            return {'status': STATUS_FAILED,
                    'message': f'Introspection report exceeds the '
                               f'{GST_REPORT_MAX_BYTES // 1024} KiB size cap '
                               f'({size} bytes)'}
        data = obj['Body'].read(GST_REPORT_MAX_BYTES + 1)
        if len(data) > GST_REPORT_MAX_BYTES:
            return {'status': STATUS_FAILED,
                    'message': f'Introspection report exceeds the '
                               f'{GST_REPORT_MAX_BYTES // 1024} KiB size cap'}

        try:
            report = parse_report(json.loads(data.decode('utf-8')))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {'status': STATUS_FAILED,
                    'message': 'Introspection report is not valid JSON'}
        except ReportError as e:
            return {'status': STATUS_FAILED,
                    'message': f'Introspection report is malformed: {e}'}

        if report.status != STATUS_CAPTURED:
            return {'status': STATUS_FAILED,
                    'message': report.message
                    or 'Introspection reported a capture failure'}

        return {'status': STATUS_CAPTURED,
                's3Key': report_key,
                'gstVersion': report.gst_version,
                'capturedAt': report.captured_at}
    except Exception as e:  # introspection recording never fails the build (1.4)
        logger.warning(
            f"Could not record introspection report {report_key}: {e}")
        return {'status': STATUS_FAILED,
                'message': f'Could not record introspection report: {e}'}


def record_promoted_artifact(usecase_id: str, arch: str, plugin_name: str,
                             plugin_id: str, version: int) -> Optional[Dict]:
    """
    Checksum + sign the artifact a successful CodeBuild run promoted to
    the Plugin_Library (3.3): stream the .so from the build image's
    unversioned promotion key, RE-HOME it to the immutable per-version
    key (rebuilds of later versions overwrite the promotion key, which
    must never invalidate this version's recorded artifact — Requirement
    10.4), record its SHA-256, sign the digest with the portal key, and
    write the authoritative detached signature beside the versioned
    copy. Returns the artifact entry fields (s3Key = the versioned key),
    or None when the promoted object is missing.

    For the x86_64 artifact the entry additionally carries the
    gstIntrospection stanza validated from the report the build uploaded
    next to the promoted .so (gst-parameter-prepopulation 1.1, 1.6);
    stanza recording is best-effort and never alters the build outcome
    (1.4). Non-x86_64 entries are unchanged.
    """
    promoted_key = library_so_key(usecase_id, arch, plugin_name)
    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=promoted_key)
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        raise
    data = obj['Body'].read()
    checksum = sha256_hex(data)
    signature = sign_digest(hashlib.sha256(data).digest())
    so_key = versioned_library_so_key(usecase_id, arch, plugin_id, version,
                                      plugin_name)
    s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=so_key, Body=data)
    s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=signature_key(so_key),
                  Body=base64.b64decode(signature))
    fields = {'s3Key': so_key, 'checksum': checksum, 'signature': signature}
    if arch == INTROSPECTION_ARCH:
        # The introspection report is uploaded beside the PROMOTED key by
        # the build image; record it from there.
        fields['gstIntrospection'] = build_gst_introspection_stanza(
            promoted_key)
    return fields


def extract_error_excerpt(lines: List[str]) -> str:
    """
    Reduce a failed build's log lines to the excerpt that shows the
    actual failure (3.4). CodeBuild keeps running post-failure phases
    (UPLOAD_ARTIFACTS etc.), so a plain tail shows only boilerplate:

    1. Cut the log at the first 'Phase complete: <PHASE> State: FAILED'
       marker (keeping the phase-context message right after it) -
       everything later is post-failure bookkeeping.
    2. Within what remains, find the last line that looks like a real
       error (compiler/meson/ninja/entrypoint) and keep a window of
       context before it through the failure marker.
    3. Fall back to the plain tail when no error line matches.
    """
    cut = len(lines)
    for i, line in enumerate(lines):
        if BUILD_FAILED_MARKER.search(line):
            cut = min(i + 2, len(lines))  # keep the phase-context line
            break
    trimmed = lines[:cut]

    error_idx = None
    for i in range(len(trimmed) - 1, -1, -1):
        if (ERROR_LINE_PATTERN.search(trimmed[i])
                and not BUILD_FAILED_MARKER.search(trimmed[i])):
            error_idx = i
            break
    if error_idx is not None:
        start = max(0, error_idx - LOG_TAIL_CONTEXT_BEFORE)
        trimmed = trimmed[start:]

    return '\n'.join(trimmed)[-LOG_TAIL_MAX_CHARS:]


def fetch_log_tail(detail: Dict) -> str:
    """CloudWatch error excerpt of a failed build for the Plugin_Record (3.4)"""
    log_info = (detail.get('additional-information') or {}).get('logs') or {}
    group = log_info.get('group-name')
    stream = log_info.get('stream-name')
    if not group or not stream:
        return ''
    try:
        response = logs_client.get_log_events(
            logGroupName=group,
            logStreamName=stream,
            limit=LOG_TAIL_MAX_EVENTS,
            startFromHead=False,
        )
    except ClientError as e:
        logger.warning(f"Could not fetch build log tail {group}/{stream}: {e}")
        return ''
    lines = [e.get('message', '').rstrip('\n')
             for e in response.get('events', [])]
    return extract_error_excerpt(lines)


# ------------------------------------------------------------- persistence

def set_arch_entry(plugin_id: str, version: int, arch: str, entry: Dict) -> None:
    """Write one per-arch artifact entry on the Plugin_Record version"""
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET artifacts.#a = :entry, updated_at = :t',
        ExpressionAttributeNames={'#a': arch},
        ExpressionAttributeValues={':entry': entry, ':t': now_ms()},
    )


def trigger_component_packaging(item: Dict) -> bool:
    """
    Invoke plugin_components.py asynchronously when all requested arch
    builds have settled with at least one success (Requirement 16.1
    trigger). Idempotent: a conditional components_triggered marker on
    the version item guarantees a single trigger per build round even
    under duplicate EventBridge delivery. plugin_components.py absence
    or invocation failure never fails the build.
    """
    if not builds_settled(item) or not successful_build_archs(item):
        return False

    plugin_id, version = item['plugin_id'], item['version']
    try:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression='SET components_triggered = :t',
            ConditionExpression='attribute_not_exists(components_triggered)',
            ExpressionAttributeValues={':t': now_ms()},
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False  # already triggered for this build round
        raise

    if not PLUGIN_COMPONENTS_FUNCTION_NAME:
        logger.warning('PLUGIN_COMPONENTS_FUNCTION_NAME unset; skipping '
                       'Plugin_Component auto-packaging trigger')
        return False
    try:
        lambda_client.invoke(
            FunctionName=PLUGIN_COMPONENTS_FUNCTION_NAME,
            InvocationType='Event',
            Payload=json.dumps({
                'action': 'package_plugin_component',
                'plugin_id': plugin_id,
                'version': version,
                'usecase_id': item.get('usecase_id'),
            }).encode('utf-8'),
        )
        return True
    except Exception as e:  # auto-packaging failure never fails the build
        logger.warning(
            f"Plugin_Component auto-packaging trigger failed for "
            f"{plugin_id} v{version}: {e}")
        return False


# -------------------------------------------------------- build submission

def submit_arch_builds(item: Dict, build_archs: List[str]) -> Dict[str, Dict]:
    """
    StartBuild the matching per-arch CodeBuild project for each
    Target_Architecture in `build_archs` (3.1), the version's
    plugin-sources tree as the source location (per-arch: multi-revision
    imports resolve each arch's own rev-{slug}/ prefix via
    arch_source_prefix). Returns the per-arch artifact entries
    {buildStatus: building, buildId, logTail} for the caller to persist.
    Every arch in `build_archs` must have a configured BUILD_PROJECTS
    entry.
    """
    plugin_id, version = item['plugin_id'], item['version']
    usecase_id = item['usecase_id']
    plugin_name = sanitize_plugin_name(item.get('name'), plugin_id)
    plugin_targets = plugin_targets_value(item)
    selected_plugins = selected_plugins_value(item)
    entries: Dict[str, Dict] = {}
    for arch in build_archs:
        env_overrides = [
            {'name': 'USECASE_ID', 'value': usecase_id, 'type': 'PLAINTEXT'},
            {'name': 'PLUGIN_ID', 'value': plugin_id, 'type': 'PLAINTEXT'},
            {'name': 'PLUGIN_VERSION', 'value': str(version), 'type': 'PLAINTEXT'},
            {'name': 'PLUGIN_NAME', 'value': plugin_name, 'type': 'PLAINTEXT'},
            {'name': 'TARGET_ARCH', 'value': arch, 'type': 'PLAINTEXT'},
            # PLUGIN_TARGETS: comma-separated individual plugin
            # names selected for a plugin-set import (e.g.
            # gst-plugins-good). The build image entrypoint builds
            # only the named plugin targets when non-empty, and the
            # whole source tree when empty (single-plugin repos,
            # scaffolds, generated plugins).
            {'name': 'PLUGIN_TARGETS', 'value': plugin_targets,
             'type': 'PLAINTEXT'},
        ]
        if selected_plugins:
            # SELECTED_PLUGINS: added only when the record's provenance
            # carries a plugin selection, so the build entrypoint can
            # meson-enable exactly those plugins
            # (-Dauto_features=disabled -D<plugin>=enabled).
            env_overrides.append(
                {'name': 'SELECTED_PLUGINS', 'value': selected_plugins,
                 'type': 'PLAINTEXT'})
        # Per-arch source tree: multi-revision imports resolve the
        # arch's own rev-{slug}/ prefix; everything else builds from
        # the flat source_s3_prefix as before.
        source_prefix = arch_source_prefix(item, arch)
        start = codebuild.start_build(
            projectName=BUILD_PROJECTS[arch],
            sourceLocationOverride=f"{PORTAL_ARTIFACTS_BUCKET}/{source_prefix}",
            environmentVariablesOverride=env_overrides,
        )
        entries[arch] = {
            'buildStatus': BUILD_BUILDING,
            'buildId': start['build']['id'],
            'logTail': '',
            # The Source_Revision this build compiles (custom-node-
            # source-lifecycle 1.9): a later save makes it stale.
            'sourceRevision': source_revision_of(item),
        }
    return entries


def start_queued_builds(plugin_id: str, version: int) -> Dict[str, Dict]:
    """
    Start the builds an asynchronous import queued: plugin_importer.py
    (same Lambda bundle) calls this once a fetch settles the record to
    import_status 'imported' with per-arch {'buildStatus': 'queued'}
    artifact entries (4.3). Finds the requested architectures still
    queued, StartBuilds them via submit_arch_builds, and persists the
    advanced entries per arch.

    Safe and idempotent: an already-started (non-queued) arch is left
    alone, architectures without a configured CodeBuild project are
    skipped (left queued), a StartBuild failure is recorded as a failed
    arch entry instead of raising, and nothing here ever raises to the
    caller — auto-start failure must never fail the fetch-result
    handler. Audit-logged as the record's created_by (there is no
    authenticated user on the EventBridge path).
    """
    try:
        # Consistent read: this runs in the same invocation as the write
        # that queued the builds (fetch-settle or revision adjustment), so
        # an eventually-consistent read could miss the just-written
        # arch_revisions mapping and resolve the wrong source prefix.
        item = get_version_item(plugin_id, version, consistent_read=True)
        if not item:
            logger.warning(f"Auto-start: Plugin_Record {plugin_id} "
                           f"v{version} not found")
            return {}
        artifacts = item.get('artifacts') or {}
        queued = [arch for arch in requested_architectures(item)
                  if (artifacts.get(arch) or {}).get('buildStatus')
                  == BUILD_QUEUED]
        unconfigured = [a for a in queued if a not in BUILD_PROJECTS]
        if unconfigured:
            logger.warning(
                'Auto-start: no CodeBuild project is configured for '
                f"{', '.join(sorted(unconfigured))}; leaving queued")

        entries: Dict[str, Dict] = {}
        for arch in queued:
            if arch not in BUILD_PROJECTS:
                continue
            try:
                entries.update(submit_arch_builds(item, [arch]))
            except Exception as e:
                logger.warning(f"Auto-start of {arch} build for "
                               f"{plugin_id} v{version} failed: {e}")
                entries[arch] = {
                    'buildStatus': BUILD_FAILED,
                    'logTail': f'Automatic build start failed: {e}',
                }
        if not entries:
            return {}

        for arch, entry in entries.items():
            set_arch_entry(plugin_id, version, arch, entry)

        started = sorted(a for a, e in entries.items()
                         if e['buildStatus'] == BUILD_BUILDING)
        log_audit_event(
            user_id=item.get('created_by') or 'system',
            action='build_plugin_record',
            resource_type='plugin_record',
            resource_id=plugin_id,
            result='success' if started else 'failure',
            details={'usecase_id': item.get('usecase_id'),
                     'version': version,
                     'architectures': started,
                     'trigger': 'import_auto_start'}
        )
        logger.info(f"Auto-started builds for {plugin_id} v{version}: "
                    f"{', '.join(started) or 'none'}")
        return entries
    except Exception as e:  # auto-start failure never fails the caller
        logger.warning(f"Auto-start of queued builds for {plugin_id} "
                       f"v{version} failed: {e}")
        return {}


# ----------------------------------------------------------------- handlers

def start_builds(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/build
    Body: {architectures?: [arch, ...],
           prebuilt?: {arch: {source_key} | {content_base64}}}

    Marks the per-arch build status building and StartBuilds the
    matching CodeBuild project per selected Target_Architecture with the
    version's plugin-sources tree as the source location (3.1).
    Architectures under `prebuilt` skip the build: the provided .so is
    accepted as the Plugin_Artifact, checksummed and KMS-signed
    identically, stored in the Plugin_Library, and recorded with
    prebuilt: true provenance (3.6). Without an explicit architectures
    list the record's previously requested architectures (e.g. queued by
    an import) are built.
    """
    body, err = parse_body(event)
    if err:
        return err

    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_MANAGE)
    if err:
        return err

    prebuilt = body.get('prebuilt') or {}
    if not isinstance(prebuilt, dict) or not all(
            isinstance(v, dict) for v in prebuilt.values()):
        return error_response(400, 'INVALID_PREBUILT',
                              'prebuilt must map architectures to '
                              '{source_key} or {content_base64} objects')

    architectures = body.get('architectures')
    if architectures is None:
        architectures = [a for a in requested_architectures(item)
                         if a not in prebuilt]
    if (not isinstance(architectures, list)
            or not all(isinstance(a, str) for a in architectures)):
        return error_response(400, 'INVALID_ARCHITECTURES',
                              'architectures must be a list of '
                              'Target_Architecture identifiers')

    build_archs = sorted(set(architectures) - set(prebuilt))
    all_archs = sorted(set(architectures) | set(prebuilt))
    if not all_archs:
        return error_response(400, 'INVALID_ARCHITECTURES',
                              'Select at least one Target_Architecture to '
                              'build or provide a prebuilt binary')

    arch_error = validate_architectures(all_archs, bool(item.get('deepstream')))
    if arch_error:
        return error_response(400, *arch_error)

    unconfigured = [a for a in build_archs if a not in BUILD_PROJECTS]
    if unconfigured:
        # Outside the Build_Target_Registry: a client error naming the
        # architecture and the registry (custom-node-source-lifecycle 6.8).
        return error_response(
            400, 'BUILD_TARGET_UNAVAILABLE',
            'No build target is available for: '
            f"{', '.join(unconfigured)}",
            {'architectures': unconfigured,
             'buildable': buildable_architectures()})

    usecase_id = item['usecase_id']
    plugin_name = sanitize_plugin_name(item.get('name'), plugin_id)
    entries: Dict[str, Dict] = {}

    # --- prebuilt binaries: checksum + sign identically (3.6)
    for arch in sorted(prebuilt):
        data, err = _load_prebuilt_bytes(item, arch, prebuilt[arch])
        if err:
            return err
        entry = store_signed_artifact(usecase_id, arch, plugin_name, data,
                                      plugin_id, version)
        entry.update({'buildStatus': BUILD_SUCCEEDED, 'logTail': '',
                      'prebuilt': True,
                      'sourceRevision': source_revision_of(item)})
        entries[arch] = entry

    # --- source builds: mark building, StartBuild per architecture (3.1)
    entries.update(submit_arch_builds(item, build_archs))

    # Persist the build round: merge the requested-arch entries, extend
    # the requested set (a monotonic union — a retry of a subset never
    # drops the other architectures, custom-node-source-lifecycle 6.5),
    # and reset the auto-packaging trigger marker so this round can
    # trigger again.
    artifacts = dict(item.get('artifacts') or {})
    artifacts.update(entries)
    requested_union = union_requested_architectures(item, all_archs)
    update_expr = ('SET artifacts = :a, requested_architectures = :r, '
                   'updated_at = :t REMOVE components_triggered')
    expr_values = {':a': artifacts, ':r': requested_union, ':t': now_ms()}
    if prebuilt:
        update_expr = ('SET artifacts = :a, requested_architectures = :r, '
                       'updated_at = :t, provenance.prebuilt = :p '
                       'REMOVE components_triggered')
        expr_values[':p'] = True
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )

    log_audit_event(
        user_id=user['user_id'],
        action='build_plugin_record',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'version': version,
                 'architectures': build_archs,
                 'prebuilt_architectures': sorted(prebuilt)}
    )

    # A prebuilt-only submission settles immediately (16.1 trigger).
    updated = get_version_item(plugin_id, version)
    trigger_component_packaging(updated)
    updated = get_version_item(plugin_id, version)
    return create_response(202, builds_view(updated))


def _load_prebuilt_bytes(item: Dict, arch: str,
                         spec: Dict) -> Tuple[Optional[bytes], Optional[Dict]]:
    """
    Resolve the prebuilt .so bytes for one architecture (3.6): either
    inline base64 content or a source_key relative to the version's
    plugin-sources tree (e.g. a binary shipped in an imported repo).
    """
    content_b64 = spec.get('content_base64')
    source_key = spec.get('source_key')
    if bool(content_b64) == bool(source_key):
        return None, error_response(
            400, 'INVALID_PREBUILT',
            f"prebuilt.{arch} must provide exactly one of content_base64 "
            'or source_key')

    if content_b64:
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception:
            return None, error_response(
                400, 'INVALID_PREBUILT',
                f"prebuilt.{arch}.content_base64 is not valid base64")
        if not data or len(data) > MAX_PREBUILT_INLINE_BYTES:
            return None, error_response(
                400, 'INVALID_PREBUILT',
                f"prebuilt.{arch} binary must be non-empty and at most "
                f"{MAX_PREBUILT_INLINE_BYTES} bytes inline")
        return data, None

    normalized = posixpath.normpath(str(source_key)).lstrip('/')
    if normalized.startswith('..'):
        return None, error_response(400, 'INVALID_PREBUILT',
                                    f"prebuilt.{arch}.source_key is invalid")
    key = (item.get('source_s3_prefix') or '') + normalized
    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None, error_response(
                400, 'INVALID_PREBUILT',
                f"prebuilt.{arch}.source_key does not exist in the "
                'version source tree', {'source_key': source_key})
        raise
    return obj['Body'].read(), None


def add_architectures(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/architectures
    Body: {architectures: [arch, ...]}

    Architecture_Addition (custom-node-source-lifecycle 6.1-6.4): append
    Target_Architectures to an existing version and build exactly those.
    Every requested architecture must be known, not yet requested for the
    version, in the Build_Target_Registry, and allowed by the version's
    DeepStream flag (400 with a per-arch reason otherwise). `prod`
    versions are locked (409 LIFECYCLE_LOCKED — create a new version);
    a version with builds in flight is rejected (409 BUILDS_IN_PROGRESS).

    Declaration-backed records (`scaffold` and `generated`, see
    SCAFFOLDED_KINDS) gain the per-arch build configuration for each added
    architecture (rendered from the recorded declaration, only where no
    such file exists), the declaration's `architectures` list is extended,
    and the Source_Revision is bumped only when a file was written (6.3).
    Existing Plugin_Artifacts are never modified.
    """
    body, err = parse_body(event)
    if err:
        return err

    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_MANAGE)
    if err:
        return err

    requested = body.get('architectures')
    if (not isinstance(requested, list) or not requested
            or not all(isinstance(a, str) for a in requested)):
        return error_response(400, 'INVALID_ARCHITECTURES',
                              'architectures must be a non-empty list of '
                              'Target_Architecture identifiers')

    existing = requested_architectures(item)
    added, rejections = validate_addition(
        requested, existing, buildable_architectures(),
        bool(item.get('deepstream')))
    if rejections or not added:
        return error_response(
            400, 'INVALID_ARCHITECTURES',
            'One or more architectures cannot be added: '
            + ', '.join(f"{arch} ({reason})"
                        for arch, reason in sorted(rejections.items())),
            {'rejected': rejections,
             'requested_architectures': existing,
             'buildable': buildable_architectures()})

    if item.get('lifecycle_state') == STATE_PROD:
        return error_response(
            409, 'LIFECYCLE_LOCKED',
            'Architectures cannot be added to a prod version; create a '
            'new version and add them there',
            {'lifecycle_state': STATE_PROD, 'hint': 'create a new version'})

    artifacts = dict(item.get('artifacts') or {})
    in_flight = sorted(
        arch for arch, entry in artifacts.items()
        if (entry or {}).get('buildStatus') in (BUILD_QUEUED, BUILD_BUILDING))
    if in_flight:
        return error_response(
            409, 'BUILDS_IN_PROGRESS',
            'Wait for the running builds to settle before adding architectures',
            {'architectures': in_flight})

    usecase_id = item['usecase_id']
    prefix = item.get('source_s3_prefix') or ''
    sets = ['artifacts = :a', 'requested_architectures = :r', 'updated_at = :t']
    values: Dict[str, Any] = {':t': now_ms()}
    files_written: List[str] = []

    # Declaration-backed records (`scaffold` from the create wizard and
    # `generated` from the Node_Generator both render the same
    # `builds/{arch}/meson.build` layout from a recorded declaration):
    # render the missing per-arch build configuration and extend the
    # recorded declaration (6.3). `imported` records carry no declaration
    # and build from their upstream meson/autotools tree, so they skip this.
    declaration_json = (item.get('provenance') or {}).get('scaffoldDeclaration')
    if declaration_json and item.get('kind') in SCAFFOLDED_KINDS:
        declaration = json.loads(declaration_json)
        declared = [str(a) for a in declaration.get('architectures') or []]
        present = [f['file'] for f in plugin_records.list_source_objects(prefix)]
        try:
            planned, extended = plan_build_configs(declaration, declared,
                                                   added, present)
        except ScaffoldError as exc:
            return error_response(400, 'INVALID_DECLARATION', str(exc),
                                  {'field': exc.field})
        for path, content in sorted(planned.items()):
            s3.put_object(
                Bucket=PORTAL_ARTIFACTS_BUCKET,
                Key=prefix + path,
                Body=content.encode('utf-8'),
                ContentType='text/plain; charset=utf-8',
            )
            files_written.append(path)
        sets.append('provenance.scaffoldDeclaration = :d')
        values[':d'] = json.dumps(extended, sort_keys=True)
        if files_written:
            sets.append('source_revision = '
                        'if_not_exists(source_revision, :base) + :inc')
            values[':base'] = plugin_records.DEFAULT_SOURCE_REVISION
            values[':inc'] = 1

    # The build starts read the version item; refresh it so the stamped
    # Source_Revision matches the bump above when files were written.
    build_item = dict(item)
    if files_written:
        build_item['source_revision'] = source_revision_of(item) + 1
    entries = submit_arch_builds(build_item, added)
    artifacts.update(entries)
    values[':a'] = artifacts
    values[':r'] = union_requested_architectures(item, added)

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET ' + ', '.join(sets) + ' REMOVE components_triggered',
        ExpressionAttributeValues=values,
    )

    log_audit_event(
        user_id=user['user_id'],
        action='add_plugin_architectures',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'version': version,
                 'architectures': added, 'files_written': files_written}
    )

    updated = get_version_item(plugin_id, version)
    return create_response(202, builds_view(updated))


def get_builds(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    GET /plugins/{id}/versions/{v}/builds
    Per-arch build status (succeeded/failed/building + log excerpt) for
    the Node_Designer UI (3.5). Readable by every role of the Use_Case.
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err
    return create_response(200, builds_view(item))


# ------------------------------------------------- EventBridge result path

def handle_build_result(event: Dict) -> Dict:
    """
    EventBridge CodeBuild Build State Change handler (rule
    'dda-portal-plugin-build-results'). Records the per-arch build
    outcome on the Plugin_Record, idempotent on the build id.
    """
    detail = event.get('detail') or {}
    project = detail.get('project-name')
    if project == FETCH_PROJECT_NAME:
        # Repository-fetch result of an asynchronous import: delegate to
        # plugin_importer.handle_fetch_result (same Lambda bundle).
        # Imported lazily so the API request path never pays for the
        # importer module's dependencies.
        import plugin_importer
        return plugin_importer.handle_fetch_result(detail)
    arch = PROJECT_ARCHITECTURES.get(project)
    if not arch:
        # Not a plugin build or fetch project - nothing to record.
        logger.info(f"Ignoring build result for project '{project}'")
        return {'recorded': False, 'reason': 'not a plugin build project'}

    build_id = build_id_from_arn(detail.get('build-id') or '')
    build_status = detail.get('build-status')
    env = env_vars_from_detail(detail)
    plugin_id = env.get('PLUGIN_ID')
    version_raw = env.get('PLUGIN_VERSION')
    if not plugin_id or not version_raw:
        logger.warning(f"Build {build_id} carries no PLUGIN_ID/PLUGIN_VERSION; skipping")
        return {'recorded': False, 'reason': 'missing build metadata'}
    version = int(version_raw)

    item = get_version_item(plugin_id, version)
    if not item:
        logger.warning(f"Build {build_id}: Plugin_Record {plugin_id} v{version} not found")
        return {'recorded': False, 'reason': 'plugin record not found'}

    # Idempotency on the build id: skip duplicate deliveries of an
    # already-settled result and events from superseded builds.
    current = (item.get('artifacts') or {}).get(arch) or {}
    recorded_build_id = current.get('buildId')
    if recorded_build_id and recorded_build_id != build_id:
        logger.info(f"Build {build_id} superseded by {recorded_build_id}; skipping")
        return {'recorded': False, 'reason': 'superseded build'}
    if (recorded_build_id == build_id
            and current.get('buildStatus') in SETTLED_STATUSES):
        logger.info(f"Build {build_id} already recorded; skipping duplicate delivery")
        return {'recorded': False, 'reason': 'already recorded'}

    usecase_id = env.get('USECASE_ID') or item.get('usecase_id')
    plugin_name = env.get('PLUGIN_NAME') or sanitize_plugin_name(
        item.get('name'), plugin_id)

    if build_status == 'SUCCEEDED':
        fields = record_promoted_artifact(usecase_id, arch, plugin_name,
                                          plugin_id, version)
        if fields is None:
            entry = {
                'buildStatus': BUILD_FAILED,
                'buildId': build_id,
                'logTail': 'Build reported success but no artifact was '
                           f"promoted to "
                           f"{library_so_key(usecase_id, arch, plugin_name)}",
            }
        else:
            entry = {'buildStatus': BUILD_SUCCEEDED, 'buildId': build_id,
                     'logTail': '', **fields}
    else:
        # Failed builds store the CloudWatch log tail and no artifact (3.4).
        entry = {
            'buildStatus': BUILD_FAILED,
            'buildId': build_id,
            'logTail': fetch_log_tail(detail),
        }
    # The settled entry keeps the Source_Revision the build was started
    # from (custom-node-source-lifecycle 1.9).
    entry['sourceRevision'] = artifact_source_revision(current)

    set_arch_entry(plugin_id, version, arch, entry)
    logger.info(f"Recorded {arch} build {build_id} for {plugin_id} v{version}: "
                f"{entry['buildStatus']}")

    updated = get_version_item(plugin_id, version)
    triggered = trigger_component_packaging(updated)
    return {'recorded': True, 'arch': arch,
            'buildStatus': entry['buildStatus'],
            'component_packaging_triggered': triggered}


# ------------------------------------------------------------------ routing

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler: API Gateway routes + EventBridge results"""
    # EventBridge CodeBuild results arrive on the same handler as the
    # API routes; distinguish by the event source (design build service).
    if event.get('source') == 'aws.codebuild':
        try:
            return handle_build_result(event)
        except Exception as e:
            logger.error(f"Build result handler error: {str(e)}", exc_info=True)
            raise  # let EventBridge retry delivery

    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}
        plugin_id = path_params.get('id')
        try:
            version = int(path_params.get('v'))
        except (TypeError, ValueError):
            return error_response(400, 'INVALID_VERSION',
                                  'version must be an integer')

        if resource == '/plugins/{id}/versions/{v}/build' and plugin_id:
            if http_method == 'POST':
                return start_builds(event, user, plugin_id, version)
        elif resource == '/plugins/{id}/versions/{v}/builds' and plugin_id:
            if http_method == 'GET':
                return get_builds(event, user, plugin_id, version)
        elif resource == '/plugins/{id}/versions/{v}/architectures' and plugin_id:
            if http_method == 'POST':
                return add_architectures(event, user, plugin_id, version)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
