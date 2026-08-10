"""
Plugin_Component auto-packaging Lambda function (Custom Node Designer)

Packages a Plugin_Record version's successfully built Plugin_Artifacts
into the versioned Greengrass component ``dda.plugin.{pluginId}`` /
``{pluginVersion}.0.0`` in the Use_Case account (Requirements 16.1,
16.7), following the workflow_packaging.py conventions.

Invocation paths:
    1. Asynchronous Lambda invoke from plugin_builds.py when all
       requested Target_Architecture builds have settled with at least
       one success:
           {"action": "package_plugin_component",
            "plugin_id": ..., "version": ..., "usecase_id": ...}
       Auto-packaging failure never fails the build: failures are
       recorded on the Plugin_Record ``component`` pointer and logged,
       never raised back to the caller.
    2. API Gateway REST:
           GET /plugins/{id}/versions/{v}/component
       Returns the Plugin_Record's ``component`` status pointer for the
       Node_Designer UI.

Recipe (Requirement 16.1): install-only (no Run lifecycle - installing
or removing a plugin never restarts LocalServer), one platform manifest
per successfully built Target_Architecture using ARCH_TO_GG_PLATFORM
plus platform attributes:
    - ``variant: {arch}`` on the JetPack arm64 manifests (a JetPack
      build is specific to its L4T/DeepStream release, so unlike
      build_recipe the attribute is always present, not only when
      several arm64 flavors are packaged);
    - ``runtime: nvidia`` on the x86_64_nvidia manifest, with the plain
      x86_64 manifest ordered after x86_64_nvidia so attribute-less
      amd64 devices match plain x86_64 while NVIDIA devices match the
      more specific manifest first.
Each manifest's artifacts are the signed ``.so`` plus a small
``plugin-manifest.json`` (name, version, arch, checksum), copied to the
Use_Case account bucket under
``plugins/components/{pluginId}/{pluginVersion}/{arch}/`` via the
staging prefix and installed on the device to
``/aws_dda/plugins/{pluginId}/{pluginVersion}/{arch}/``.

All-or-nothing: artifacts stage -> promote -> register; a failed
registration deletes the freshly created component version so nothing
partial exists. Retry is idempotent on plugin id + version: an already
"registered" component pointer short-circuits, and a ConflictException
from the registry re-describes the existing version instead of failing.
Rebuilds always create a new Plugin_Record version (existing design),
which packages as a new Plugin_Component version; previously published
versions are never modified or deleted here (16.7).

Recipe assembly (build_plugin_recipe and its helpers) is pure over the
record's built architectures so it is property-testable without AWS
(tasks 6.4 / 6.5).
"""
import json
import logging
import os
import posixpath
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, get_usecase_client
)

# Reuse the Plugin_Record persistence helpers and error envelope from
# plugin_records.py, and the Use_Case-account packaging conventions
# (platform map, staging cleanup, failure type) from workflow_packaging.py
# (same deployment bundle).
from plugin_records import (
    authorize_record_access,
    error_response,
    get_version_item,
    not_found_response,
    now_ms,
    plugin_table,
    successful_build_archs,
)
from workflow_packaging import (
    ARCH_TO_GG_PLATFORM as WORKFLOW_ARCH_TO_GG_PLATFORM,
    PackagingError,
    delete_prefix,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (portal account)
s3 = boto3.client('s3')

# Environment variables
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')

# Greengrass component naming (design: Plugin_Component packaging, 16.1)
PLUGIN_COMPONENT_PREFIX = 'dda.plugin.'
COMPONENT_PUBLISHER = 'DDA Portal Node Designer'

# Use_Case account S3 prefixes for Plugin_Component artifacts (mirrors the
# workflows/components + workflows/staging layout)
COMPONENT_S3_PREFIX = 'plugins/components'
STAGING_S3_PREFIX = 'plugins/staging'

# Where the LocalServer plugin loader discovers installed Plugin_Components
DEVICE_PLUGINS_ROOT = '/aws_dda/plugins'

# The per-arch plugin metadata artifact shipped beside the signed .so
PLUGIN_MANIFEST_FILENAME = 'plugin-manifest.json'

# The Frame_Processing_Hook the scaffold's C skeleton imports at run time.
# It MUST ship beside the installed .so: the element self-locates its own
# shared library (dladdr) and imports the hook from that directory. A
# scaffold-built plugin without its hook loads but fails every frame
# (custom-node-plugin-runtime-fixes, defect 2 - verified on
# ryan-orin-nano/JP6, LocalServer v1.0.56).
HOOK_FILENAME = 'frame_processing_hook.py'

# Where the hook lives inside the version's plugin-sources prefix (the
# scaffold layout rendered by workflow_core.scaffold).
HOOK_SOURCE_RELATIVE_KEY = 'plugin/' + HOOK_FILENAME

# arch id -> Greengrass platform architecture. Task 10.1 extends
# workflow_packaging.ARCH_TO_GG_PLATFORM with x86_64_nvidia; until then the
# mapping is completed locally (established deviation - workflow_packaging.py
# is not modified here). Both x86_64 flavors map to Greengrass amd64 and are
# disambiguated by the 'runtime: nvidia' platform attribute.
ARCH_TO_GG_PLATFORM: Dict[str, str] = dict(WORKFLOW_ARCH_TO_GG_PLATFORM)
ARCH_TO_GG_PLATFORM.setdefault('x86_64_nvidia', 'amd64')

ARCH_X86_64 = 'x86_64'
ARCH_X86_64_NVIDIA = 'x86_64_nvidia'

# Component pointer statuses on the Plugin_Record (design data model)
COMPONENT_PACKAGING = 'packaging'
COMPONENT_REGISTERED = 'registered'
COMPONENT_FAILED = 'failed'

# Polling for the registered component to become DEPLOYABLE
COMPONENT_STATUS_MAX_ATTEMPTS = 30
COMPONENT_STATUS_POLL_SECONDS = 2

# Async trigger action name (plugin_builds.py payload)
ACTION_PACKAGE = 'package_plugin_component'

# Acting principal recorded for the automatic (non-interactive) packaging path
SYSTEM_USER_ID = 'system:plugin-build-service'


# ------------------------------------------------------------ pure helpers
#
# Everything from here to build_plugin_recipe is pure over the record's
# built architectures, so Plugin_Component recipe assembly is
# property-testable without AWS (tasks 6.4 / 6.5).

def component_name_for(plugin_id: str) -> str:
    return f"{PLUGIN_COMPONENT_PREFIX}{plugin_id}"


def component_version_for(plugin_version: int) -> str:
    """Component version derived from the Plugin_Record version (16.1)"""
    return f"{int(plugin_version)}.0.0"


def artifact_final_prefix(plugin_id: str, plugin_version: int, arch: str) -> str:
    """Account-bucket prefix of one arch's Plugin_Component artifacts"""
    return f"{COMPONENT_S3_PREFIX}/{plugin_id}/{plugin_version}/{arch}"


def device_install_dir(plugin_id: str, plugin_version: int, arch: str) -> str:
    """Where the component installs its artifacts on the device (16.1)"""
    return f"{DEVICE_PLUGINS_ROOT}/{plugin_id}/{plugin_version}/{arch}"


def build_plugin_manifest(plugin_name: str, plugin_version: int, arch: str,
                          checksum: str) -> Dict:
    """plugin-manifest.json content: name, version, arch, checksum (16.1)"""
    return {
        'name': plugin_name,
        'version': int(plugin_version),
        'arch': arch,
        'checksum': checksum,
    }


def manifest_arch_order(archs) -> List[str]:
    """
    Deterministic platform-manifest order. Sorted, except the plain
    x86_64 manifest is listed after the x86_64_nvidia one: both map to
    Greengrass amd64, so ordering plain x86_64 last lets attribute-less
    amd64 devices match it while devices declaring 'runtime: nvidia'
    match the more specific manifest first.
    """
    ordered = sorted(archs)
    if ARCH_X86_64 in ordered and ARCH_X86_64_NVIDIA in ordered:
        ordered.remove(ARCH_X86_64)
        ordered.insert(ordered.index(ARCH_X86_64_NVIDIA) + 1, ARCH_X86_64)
    return ordered


def platform_for(arch: str) -> Dict[str, str]:
    """
    Greengrass platform block for one Target_Architecture: os +
    ARCH_TO_GG_PLATFORM architecture, plus the platform attributes that
    split one Greengrass architecture into DDA Target_Architectures -
    'variant' for the JetPack arm64 builds (declared in the device's
    Nucleus platform overrides) and 'runtime: nvidia' for x86_64_nvidia.
    """
    platform = {'os': 'linux', 'architecture': ARCH_TO_GG_PLATFORM[arch]}
    if ARCH_TO_GG_PLATFORM[arch] == 'aarch64':
        platform['variant'] = arch
    elif arch == ARCH_X86_64_NVIDIA:
        platform['runtime'] = 'nvidia'
    return platform


def build_plugin_recipe(plugin_id: str, plugin_version: int, bucket: str,
                        arch_so_names: Dict[str, str],
                        include_hook: bool = False) -> Dict:
    """
    Install-only Greengrass recipe for a Plugin_Component (16.1): one
    platform manifest per successfully built Target_Architecture (the
    keys of arch_so_names, mapping each arch to its .so file name).
    There is deliberately no Run lifecycle - installing or removing a
    plugin never restarts LocalServer; the LocalServer plugin loader
    discovers the installed files under /aws_dda/plugins/ at runtime.

    ``include_hook`` ships the Frame_Processing_Hook
    (frame_processing_hook.py) beside the .so on every manifest - the
    scaffold's C skeleton imports it from its own install directory at
    run time (custom-node-plugin-runtime-fixes, defect 2).

    Pure over (plugin_id, plugin_version, bucket, arch_so_names,
    include_hook) so recipe assembly is property-testable without AWS.
    """
    manifests = []
    for arch in manifest_arch_order(arch_so_names):
        so_name = arch_so_names[arch]
        final_prefix = artifact_final_prefix(plugin_id, plugin_version, arch)
        install_dir = device_install_dir(plugin_id, plugin_version, arch)
        filenames = [so_name, PLUGIN_MANIFEST_FILENAME]
        if include_hook:
            filenames.append(HOOK_FILENAME)
        install_script = (
            f"mkdir -p {install_dir} && "
            f"cp -f "
            + " ".join(f"{{artifacts:path}}/{name}" for name in filenames)
            + f" {install_dir}/"
        )
        manifests.append({
            'Platform': platform_for(arch),
            'Lifecycle': {
                'Install': {
                    'Script': install_script,
                    'Timeout': 300,
                    'requiresPrivilege': True
                }
            },
            'Artifacts': [
                {
                    'Uri': f"s3://{bucket}/{final_prefix}/{name}",
                    'Permission': {'Read': 'ALL'}
                }
                for name in filenames
            ]
        })

    return {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name_for(plugin_id),
        'ComponentVersion': component_version_for(plugin_version),
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': COMPONENT_PUBLISHER,
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'PluginId': plugin_id,
                'PluginVersion': str(plugin_version)
            }
        },
        'Manifests': manifests,
        'Lifecycle': {}
    }


def registry_tags(usecase_id: str, plugin_id: str, plugin_version: int) -> Dict[str, str]:
    """Registry tags linking the component back to its Plugin_Record"""
    return {
        'dda-portal:managed': 'true',
        'dda-portal:usecase-id': usecase_id,
        'dda-portal:plugin-id': plugin_id,
        'dda-portal:plugin-version': str(plugin_version),
    }


def component_version_arn(region: str, account_id: str, plugin_id: str,
                          plugin_version: int) -> str:
    """ARN of one Plugin_Component version in a Use_Case account registry"""
    return (f"arn:aws:greengrass:{region}:{account_id}:components:"
            f"{component_name_for(plugin_id)}:versions:"
            f"{component_version_for(plugin_version)}")


# ------------------------------------------------------------- persistence

def set_component_pointer(plugin_id: str, version: int, pointer: Dict) -> None:
    """Write the Plugin_Record 'component' status pointer (data model)"""
    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression='SET component = :c, updated_at = :t',
        ExpressionAttributeValues={':c': pointer, ':t': now_ms()},
    )


# ----------------------------------------------- staging (all-or-nothing)

def load_portal_artifact(s3_key: str, label: str) -> bytes:
    """Read a signed .so from the portal Plugin_Library, or raise
    PackagingError identifying the missing artifact."""
    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('NoSuchKey', '404'):
            raise PackagingError(
                label,
                f"Plugin artifact was not found in the Plugin_Library "
                f"(s3://{PORTAL_ARTIFACTS_BUCKET}/{s3_key})")
        raise PackagingError(
            label,
            f"Plugin artifact could not be read from the Plugin_Library: "
            f"{code or str(e)}")
    return obj['Body'].read()


def load_frame_processing_hook(item: Dict) -> Optional[bytes]:
    """The version's Frame_Processing_Hook bytes from its plugin-sources
    prefix, or None when the record ships no hook (prebuilt imports).
    Read errors other than absence raise PackagingError: silently
    packaging a scaffold-built plugin WITHOUT its hook produces a
    component that loads but fails every frame at run time
    (custom-node-plugin-runtime-fixes, defect 2)."""
    source_prefix = item.get('source_s3_prefix')
    if not source_prefix:
        return None
    hook_key = source_prefix + HOOK_SOURCE_RELATIVE_KEY
    try:
        obj = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=hook_key)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('NoSuchKey', '404'):
            return None
        raise PackagingError(
            HOOK_FILENAME,
            f"Frame_Processing_Hook could not be read from the plugin "
            f"sources (s3://{PORTAL_ARTIFACTS_BUCKET}/{hook_key}): "
            f"{code or str(e)}")
    return obj['Body'].read()


def stage_and_promote_artifacts(usecase_s3, bucket: str, plugin_id: str,
                                plugin_version: int,
                                arch_payloads: Dict[str, Dict[str, bytes]]
                                ) -> Tuple[str, Dict[str, List[str]]]:
    """
    Upload every arch's artifacts ({arch: {filename: bytes}}) to a
    temporary staging prefix in the Use_Case account bucket, then
    promote all of them to the final component prefix (all-or-nothing).

    Returns (staging_root, {arch: [final keys]}). Raises PackagingError
    with the failing artifact identified; the caller cleans up.
    """
    stage_id = uuid.uuid4().hex
    staging_root = f"{STAGING_S3_PREFIX}/{plugin_id}/{plugin_version}/{stage_id}"

    staged: List[Tuple[str, str, str]] = []  # (label, stage_key, final_key)
    for arch in sorted(arch_payloads):
        final_prefix = artifact_final_prefix(plugin_id, plugin_version, arch)
        for filename in sorted(arch_payloads[arch]):
            label = f"{arch}/{filename}"
            stage_key = f"{staging_root}/{label}"
            try:
                usecase_s3.put_object(Bucket=bucket, Key=stage_key,
                                      Body=arch_payloads[arch][filename])
            except ClientError as e:
                raise PackagingError(
                    label,
                    f"Failed to upload artifact '{label}' to the staging "
                    f"area: {str(e)}")
            staged.append((label, stage_key, f"{final_prefix}/{filename}"))

    # Every artifact staged successfully -> promote to the final prefix.
    final_keys: Dict[str, List[str]] = {arch: [] for arch in arch_payloads}
    for label, stage_key, final_key in staged:
        try:
            usecase_s3.copy_object(Bucket=bucket, Key=final_key,
                                   CopySource={'Bucket': bucket, 'Key': stage_key})
        except ClientError as e:
            raise PackagingError(
                label,
                f"Failed to promote artifact '{label}' from the staging "
                f"area: {str(e)}")
        final_keys[label.split('/', 1)[0]].append(final_key)

    return staging_root, final_keys


# --------------------------------------------------------------- registry

def register_plugin_component(greengrass, recipe: Dict, usecase: Dict,
                              usecase_id: str, plugin_id: str,
                              plugin_version: int) -> str:
    """
    Register the Plugin_Component version in the Use_Case account
    Greengrass registry and wait until it is DEPLOYABLE. A freshly
    created version that fails to become DEPLOYABLE is deleted so
    nothing partial exists. A ConflictException (version already
    registered - idempotent retry on plugin id + version) re-describes
    the existing version instead of failing; the pre-existing version is
    never deleted (16.7).
    """
    component_label = (f"component {recipe['ComponentName']} "
                       f"v{recipe['ComponentVersion']}")
    created = True
    try:
        response = greengrass.create_component_version(
            inlineRecipe=json.dumps(recipe),
            tags=registry_tags(usecase_id, plugin_id, plugin_version),
        )
        component_arn = response['arn']
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code != 'ConflictException':
            raise PackagingError(component_label,
                                 f"Component registration failed: {str(e)}")
        # Idempotent retry: the version already exists - re-describe it.
        created = False
        region = getattr(getattr(greengrass, 'meta', None), 'region_name', None) \
            or os.environ.get('AWS_REGION', 'us-east-1')
        component_arn = component_version_arn(
            region, str(usecase.get('account_id')), plugin_id, plugin_version)

    component_status = 'REQUESTED'
    status_message = ''
    for attempt in range(COMPONENT_STATUS_MAX_ATTEMPTS):
        try:
            status_response = greengrass.describe_component(arn=component_arn)
        except ClientError as e:
            raise PackagingError(
                component_label,
                f"Component could not be described after registration: {str(e)}")
        component_status = status_response['status']['componentState']
        status_message = status_response['status'].get('message', '')
        if component_status not in ('REQUESTED', 'IN_PROGRESS'):
            break
        time.sleep(COMPONENT_STATUS_POLL_SECONDS)

    if component_status != 'DEPLOYABLE':
        if created:
            # Remove the failed registration so nothing partial exists.
            try:
                greengrass.delete_component(arn=component_arn)
            except ClientError as e:
                logger.error(f"Error deleting failed component "
                             f"{component_arn}: {str(e)}")
        raise PackagingError(
            component_label,
            f"Component did not become DEPLOYABLE (state {component_status}): "
            f"{status_message or 'no status message'}")

    return component_arn


# ---------------------------------------------------- packaging operation

def package_plugin_component(payload: Dict) -> Dict:
    """
    Package a Plugin_Record version's successfully built Plugin_Artifacts
    into the Plugin_Component dda.plugin.{pluginId} v{pluginVersion}.0.0
    in the Use_Case account (16.1). Never raises: auto-packaging failure
    is recorded on the Plugin_Record component pointer and never fails
    the triggering build.
    """
    plugin_id = payload.get('plugin_id')
    try:
        version = int(payload.get('version'))
    except (TypeError, ValueError):
        logger.error(f"package_plugin_component: invalid version in {payload}")
        return {'packaged': False, 'reason': 'invalid version'}
    if not plugin_id:
        logger.error(f"package_plugin_component: missing plugin_id in {payload}")
        return {'packaged': False, 'reason': 'missing plugin_id'}

    item = get_version_item(plugin_id, version)
    if not item:
        logger.error(f"package_plugin_component: Plugin_Record {plugin_id} "
                     f"v{version} not found")
        return {'packaged': False, 'reason': 'plugin record not found'}

    name = component_name_for(plugin_id)
    comp_version = component_version_for(version)

    # Idempotent retry on plugin id + version: registered short-circuits.
    existing = item.get('component') or {}
    if existing.get('status') == COMPONENT_REGISTERED:
        logger.info(f"Plugin_Component {name} v{comp_version} already "
                    f"registered; short-circuiting")
        return {'packaged': True, 'short_circuited': True,
                'component_name': name, 'component_version': comp_version,
                'component_arn': existing.get('arn')}

    built = successful_build_archs(item)
    if not built:
        logger.error(f"package_plugin_component: {plugin_id} v{version} has "
                     f"no successfully built Plugin_Artifact")
        return {'packaged': False, 'reason': 'no successful builds'}

    usecase_id = item['usecase_id']
    try:
        return _package(item, plugin_id, version, usecase_id, built)
    except PackagingError as e:
        _record_failure(plugin_id, version, usecase_id, built, e.message,
                        failing_artifact=e.artifact)
        return {'packaged': False, 'reason': e.message,
                'failing_artifact': e.artifact}
    except Exception as e:  # never fails the build (16.1 trigger contract)
        logger.error(f"Plugin_Component packaging failed for {plugin_id} "
                     f"v{version}: {str(e)}", exc_info=True)
        _record_failure(plugin_id, version, usecase_id, built, str(e))
        return {'packaged': False, 'reason': str(e)}


def _record_failure(plugin_id: str, version: int, usecase_id: str,
                    built: List[str], message: str,
                    failing_artifact: Optional[str] = None) -> None:
    """Best-effort failure bookkeeping on the component pointer + audit"""
    try:
        set_component_pointer(plugin_id, version, {
            'name': component_name_for(plugin_id),
            'version': component_version_for(version),
            'arn': None,
            'architectures': built,
            'status': COMPONENT_FAILED,
            'packagedAt': None,
            'failure': message,
        })
    except Exception as e:
        logger.error(f"Could not record packaging failure for {plugin_id} "
                     f"v{version}: {str(e)}")
    log_audit_event(
        user_id=SYSTEM_USER_ID,
        action='package_plugin_component',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='failure',
        details={'usecase_id': usecase_id, 'version': version,
                 'architectures': built, 'error': message,
                 'failing_artifact': failing_artifact}
    )


def _package(item: Dict, plugin_id: str, version: int, usecase_id: str,
             built: List[str]) -> Dict:
    """The stage -> promote -> register sequence; raises PackagingError."""
    name = component_name_for(plugin_id)
    comp_version = component_version_for(version)

    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        raise PackagingError(f"component {name} v{comp_version}",
                             f"Use case '{usecase_id}' not found")
    bucket = usecase.get('s3_bucket')
    if not bucket:
        raise PackagingError(
            f"component {name} v{comp_version}",
            f"Use case '{usecase_id}' has no S3 bucket configured for "
            f"component artifacts")

    set_component_pointer(plugin_id, version, {
        'name': name,
        'version': comp_version,
        'arn': None,
        'architectures': built,
        'status': COMPONENT_PACKAGING,
        'packagedAt': None,
        'failure': None,
    })

    # Assemble each successfully built arch's payload: the signed .so from
    # the Plugin_Library plus its plugin-manifest.json (16.1).
    plugin_name = item.get('name') or plugin_id
    artifacts = item.get('artifacts') or {}
    # The Frame_Processing_Hook (arch-independent) travels beside every
    # arch's .so: the C skeleton imports it from its own install
    # directory at run time (custom-node-plugin-runtime-fixes, defect 2).
    hook_bytes = load_frame_processing_hook(item)
    arch_payloads: Dict[str, Dict[str, bytes]] = {}
    arch_so_names: Dict[str, str] = {}
    for arch in built:
        entry = artifacts.get(arch) or {}
        so_key = entry.get('s3Key')
        checksum = entry.get('checksum')
        if not so_key or not checksum:
            raise PackagingError(
                f"{arch}/.so",
                f"Successful build for '{arch}' has no recorded artifact "
                f"key/checksum on the Plugin_Record")
        so_name = posixpath.basename(so_key)
        manifest = build_plugin_manifest(plugin_name, version, arch, checksum)
        arch_payloads[arch] = {
            so_name: load_portal_artifact(so_key, f"{arch}/{so_name}"),
            PLUGIN_MANIFEST_FILENAME: json.dumps(
                manifest, sort_keys=True, indent=2).encode('utf-8'),
        }
        if hook_bytes is not None:
            arch_payloads[arch][HOOK_FILENAME] = hook_bytes
        arch_so_names[arch] = so_name

    # Use_Case account clients via the assumed cross-account role.
    session_name = f"plugin-pkg-{plugin_id[:24]}-{int(datetime.utcnow().timestamp())}"[:64]
    usecase_s3 = get_usecase_client('s3', usecase, session_name=session_name)
    greengrass = get_usecase_client('greengrassv2', usecase,
                                    session_name=session_name)

    staging_root = f"{STAGING_S3_PREFIX}/{plugin_id}/{version}/"
    final_root = f"{COMPONENT_S3_PREFIX}/{plugin_id}/{version}/"
    component_arn = None
    try:
        stage_root, _final_keys = stage_and_promote_artifacts(
            usecase_s3, bucket, plugin_id, version, arch_payloads)

        # Register only after every artifact promoted successfully.
        recipe = build_plugin_recipe(plugin_id, version, bucket, arch_so_names,
                                     include_hook=hook_bytes is not None)
        component_arn = register_plugin_component(
            greengrass, recipe, usecase, usecase_id, plugin_id, version)
    except PackagingError:
        # All-or-nothing: delete the stage and any promoted artifacts of
        # this version; previously published component versions and their
        # artifacts live under other version prefixes and stay untouched.
        delete_prefix(usecase_s3, bucket, staging_root)
        delete_prefix(usecase_s3, bucket, final_root)
        raise
    finally:
        if component_arn is not None:
            delete_prefix(usecase_s3, bucket, staging_root)

    set_component_pointer(plugin_id, version, {
        'name': name,
        'version': comp_version,
        'arn': component_arn,
        'architectures': built,
        'status': COMPONENT_REGISTERED,
        'packagedAt': now_ms(),
        'failure': None,
    })
    log_audit_event(
        user_id=SYSTEM_USER_ID,
        action='package_plugin_component',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'version': version,
                 'architectures': built, 'component_name': name,
                 'component_version': comp_version,
                 'component_arn': component_arn}
    )
    return {'packaged': True, 'component_name': name,
            'component_version': comp_version, 'component_arn': component_arn,
            'architectures': built}


# ------------------------------------------------------------- API route

def get_component(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    GET /plugins/{id}/versions/{v}/component
    The Plugin_Record's component status pointer (packaging/registered/
    failed) for the Node_Designer UI. Readable by every role of the
    Use_Case.
    """
    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item)
    if err:
        return err
    return create_response(200, {
        'plugin_id': plugin_id,
        'version': version,
        'component': item.get('component') or {},
    })


# ------------------------------------------------------------------ routing

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler: async packaging trigger + API Gateway route"""
    # Asynchronous invoke from plugin_builds.py (16.1 trigger).
    if event.get('action') == ACTION_PACKAGE:
        return package_plugin_component(event)

    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,OPTIONS',
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

        if resource == '/plugins/{id}/versions/{v}/component' and plugin_id:
            if http_method == 'GET':
                return get_component(event, user, plugin_id, version)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
