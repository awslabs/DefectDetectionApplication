"""
Deployments handler for Edge CV Portal
Manages Greengrass deployments to edge devices
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, get_usecase,
    get_usecase_client, get_usecase_region,
    rbac_manager, Permission
)
# Deployment guard for Workflow_Components (Requirements 4.7, 4.10):
# a workflow version may only be deployed when it has a recorded
# passed-validation run with zero error findings. workflow_guards does
# not need the workflow_core layer, so it is importable here.
import workflow_guards

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE', 'dda-portal-deployments')
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')

# ---------------------------------------------------------------------------
# Plugin_Component deployment gates (custom-node-designer, Requirements
# 9.7, 9.8, 9.11, 16.3, 16.5, 16.6)
# ---------------------------------------------------------------------------

# Plugin_Components are named dda.plugin.{pluginId} by plugin_components.py
PLUGIN_COMPONENT_PREFIX = 'dda.plugin.'

# Backing Plugin_Records (lifecycle_state + component pointer with the
# published platform-manifest architectures)
PLUGIN_RECORDS_TABLE = os.environ.get('PLUGIN_RECORDS_TABLE')

# ---------------------------------------------------------------------------
# vLLM_Model_Component deployment gate (vllm-triton-inference, Requirements
# 3.3-3.7, 8.5, 8.6)
# ---------------------------------------------------------------------------

# vLLM_Model_Components are named model-vllm-{safe_model_name} by
# greengrass_publish.py; the -vllm- infix is the deployment-side
# discriminator that activates the architecture gate.
VLLM_MODEL_COMPONENT_PREFIX = 'model-vllm-'

# Backing vLLM_Model_Records live in the training-jobs table. The publish
# branch materializes a top-level component_name attribute on the record,
# indexed by the component_name-index GSI, so the gate can resolve a
# model-vllm-* component back to its record's supported_architectures.
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
TRAINING_JOBS_COMPONENT_NAME_INDEX = 'component_name-index'

# Per_JetPack_Component naming: greengrass_publish.py suffixes the
# Base_Component_Name with `target.replace('_', '-')` for one component per
# packaged target, so `model-vllm-{safe}-jetson-xavier-jp7` serves exactly
# `arm64_jp7`. This closed suffix vocabulary is the reverse of
# packaging.VLLM_ARCH_TO_TARGET — KEEP IN SYNC with
# `packaging.VLLM_ARCH_TO_TARGET` (the same mirrored-map convention
# greengrass_publish.VLLM_TARGET_TO_ARCH uses; these Lambdas bundle the
# shared layer only, so packaging cannot be imported).
VLLM_TARGET_SUFFIX_TO_ARCH = {
    'jetson-xavier-jp5': 'arm64_jp5',
    'jetson-xavier-jp6': 'arm64_jp6',
    'jetson-xavier-jp7': 'arm64_jp7',
}

# Devices table: carries the UseCaseAdmin-set `test_device` flag (9.8) and
# the device's recorded Target_Architecture (16.6)
DEVICES_TABLE = os.environ.get('DEVICES_TABLE')

# Lifecycle_State values (dev → test → prod). dev-state components are
# rejected for any deployment target; test-state components deploy only to
# devices flagged test_device; prod deploys anywhere in the Use_Case.
LIFECYCLE_TEST = 'test'
LIFECYCLE_PROD = 'prod'

# ---------------------------------------------------------------------------
# Workflow_Component deployment constants (Workflow Manager, Requirement 8)
# ---------------------------------------------------------------------------

# Greengrass component naming assigned by the Component_Packager
# (workflow_packaging.py): dda.workflow.{workflowId} v{workflowVersion}.0.0
WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.'

# LocalServer Greengrass components are named aws.edgeml.dda.LocalServer.<arch>
LOCAL_SERVER_COMPONENT_PREFIX = 'aws.edgeml.dda.LocalServer'

# Named shadows the camera-registry-sync feature keeps in sync between the
# device and IoT Core. These must match the edge-side constants:
# - src/backend/camera_sync/agent.py (SHADOW_NAME): the Edge_Sync_Agent
#   writes camera registry reports to this named shadow via ShadowManager
#   IPC (device -> cloud).
# - src/backend/workflow_engine/camera_binding_store.py
#   (BINDINGS_SHADOW_NAME): the workflow engine reads camera bindings from
#   this named shadow (cloud -> device).
# Without a ShadowManager `synchronize` configuration listing them, these
# local shadows never mirror to IoT Core and the Portal sees devices as
# "Never synced".
CAMERA_REGISTRY_SHADOW_NAME = 'dda-camera-registry'
CAMERA_BINDINGS_SHADOW_NAME = 'dda-camera-bindings'

# Named shadow carrying the device's model GPU-fallback status snapshot
# (per-model active execution providers + the device-level degraded-GPU
# signal; spec: model-gpu-fallback-visibility). Reported-only telemetry
# written by src/backend/utils/model_status_shadow.py on the device; the
# single-device GET in devices.py reads it on demand. Must be in the
# ShadowManager `synchronize` list below or it never mirrors to IoT Core.
MODEL_STATUS_SHADOW_NAME = 'dda-model-status'

# Minimum LocalServer component version a Workflow_Component requires
# (Requirement 8.4). The Component_Packager writes this same value into each
# packaged component's manifest.json (minLocalServerVersion) from the same
# environment configuration, so resolving it here matches the packaged
# manifests. A per-version override recorded on the WorkflowVersions item
# (min_local_server_version) takes precedence when present.
WORKFLOW_MIN_LOCAL_SERVER_VERSION = os.environ.get(
    'WORKFLOW_MIN_LOCAL_SERVER_VERSION',
    os.environ.get('DDA_LOCAL_SERVER_VERSION', '1.0.0'))

# Per-arch minimum LocalServer versions. LocalServer ships as independently-
# versioned per-architecture variants (aws.edgeml.dda.LocalServer.arm64 /
# .arm64JP5 / .arm64JP6 / .amd64) whose version lineages are NOT comparable
# (the arm64 variant may be at 1.0.124 while arm64JP6 is at 1.0.35). A single
# global minimum is therefore variant-blind and falsely blocks the JetPack
# variants (a JP6 device on 1.0.35 can never satisfy an arm64-derived
# "1.0.63"). WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is a JSON object keyed by
# workflow_core arch id ({"arm64_jp6": "1.0.0", ...}), mirroring the same env
# the Component_Packager reads so this pre-submit check matches the packaged
# manifests. A configured (non-empty) map is completed at derivation by
# _fill_missing_arch_floors below so every known arch resolves a per-lineage
# floor; only an empty/unconfigured map (or an arch-undetermined device)
# falls back to the scalar default above.
def _parse_min_versions_map():
    """Per-arch minimum LocalServer versions from WORKFLOW_MIN_LOCAL_SERVER_
    VERSIONS (JSON object). Malformed or non-object values yield {}."""
    raw = os.environ.get('WORKFLOW_MIN_LOCAL_SERVER_VERSIONS', '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is not valid JSON; falling '
            'back to the scalar minimum for every arch')
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


# Arch ids the pre-submit gate can resolve from installed LocalServer
# components — mirrors local_server_component_arch's codomain. Pinned in
# lockstep with workflow_packaging.ARCH_TO_LOCAL_SERVER_COMPONENT and the
# compute-stack.ts floor-map literal by the coverage test
# (tests/test_workflow_min_localserver_floor_coverage.py). Kept as a local
# constant because this module cannot import workflow_packaging (Lambda
# bundling constraint documented above).
LOCAL_SERVER_ARCH_IDS = (
    'arm64_jp4', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7', 'x86_64')

# Safe per-lineage floor: satisfiable in EVERY LocalServer variant lineage
# (all variants version from 1.0.x and workflow support ships in current
# field builds) — the same value every explicit floor-map entry uses. Never
# substitute the cross-lineage scalar above for a known arch: the variant
# lineages are not comparable and a scalar-derived constraint can be
# unsatisfiable in the arch's own lineage.
SAFE_LINEAGE_FLOOR = '1.0.0'


def _fill_missing_arch_floors(by_arch):
    """Complete a configured per-arch floor map so the pre-submit gate never
    silently falls back to the cross-lineage scalar for a known arch.

    An empty map is returned as-is (the scalar fallback chain is the
    documented contract when no map is configured). A non-empty map is
    copied with every LOCAL_SERVER_ARCH_IDS key it is missing set to
    SAFE_LINEAGE_FLOOR, with one loud warning naming the filled archs.
    """
    if not by_arch:
        return by_arch
    missing = [arch for arch in LOCAL_SERVER_ARCH_IDS if arch not in by_arch]
    if not missing:
        return by_arch
    completed = dict(by_arch)
    for arch in missing:
        completed[arch] = SAFE_LINEAGE_FLOOR
    logger.warning(
        'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is configured but missing known '
        'arch(s) %s; filling each with the safe per-lineage floor %s instead '
        'of the cross-lineage scalar. Complete the map in compute-stack.ts — '
        'tests/test_workflow_min_localserver_floor_coverage.py should have '
        'caught this.', ', '.join(missing), SAFE_LINEAGE_FLOOR)
    return completed


WORKFLOW_MIN_LOCAL_SERVER_VERSIONS = _fill_missing_arch_floors(
    _parse_min_versions_map())

# Minimum Greengrass Nucleus version required for DDA components
# Nucleus is already installed on the device — we don't pin versions in deployments.
# The DDA LocalServer recipe declares >=2.4.0 as a dependency.
# If the device's Nucleus is too old, Greengrass will report the conflict.
MIN_NUCLEUS_VERSION = '2.4.0'  # Reference only — not used in deployment components

# CloudWatch log manager for device logging.
# The pinned version's Nucleus ceiling must cover the Nucleus the device runs,
# since this component is auto-included alongside Nucleus (pinned to the device's
# running version). LogManager 2.3.9 requires Nucleus <2.15.0, which conflicts
# with devices on Nucleus >=2.15.0 (e.g. 2.16.1) and yields
# FAILED_NO_STATE_CHANGE / NoAvailableComponentVersion. 2.3.12 allows Nucleus
# <2.18.0, matching the ceilings of the other auto-included AWS components
# (Cli / ShadowManager / DockerApplicationManager).
LOG_MANAGER_VERSION = '2.3.12'

# Shadow manager for named-shadow synchronization (camera-registry-sync).
# The real CreateDeployment API requires a componentVersion on every component
# entry ("Missing required parameter in components.aws.greengrass.ShadowManager:
# componentVersion"), so the auto-included ShadowManager must be pinned. Like
# LOG_MANAGER_VERSION this is the fallback pin used when the newest
# Nucleus-compatible public version can't be resolved; 2.3.15's Nucleus ceiling
# matches the other auto-included AWS components.
SHADOW_MANAGER_VERSION = '2.3.15'

# Components that require Nucleus to be explicitly included in deployment
# Model components (model-*) also need Nucleus since they depend on DDA components
DDA_COMPONENTS_REQUIRING_NUCLEUS = [
    'aws.edgeml.dda.LocalServer',
    'aws.edgeml.dda.InferenceApp',
    'model-',  # Model components created by the portal
]

# ---------------------------------------------------------------------------
# Component store size limit (prevention + automatic remediation)
# ---------------------------------------------------------------------------
#
# The Nucleus defaults componentStoreMaxSizeBytes to 10 GB. DDA LocalServer
# artifacts are multi-GB on the fat-artifact (non-ECR) targets, so a device
# retaining two LocalServer versions exceeds the default and every subsequent
# deployment is rejected during artifact download with
# FAILED_NO_STATE_CHANGE "Component store size limit reached" — before the
# deployment's own configuration merge is applied, so a single deployment
# cannot both raise the limit and ship new artifacts.
#
# Prevention: every portal deployment's auto-included Nucleus entry carries a
# configurationUpdate merge raising the limit, so healthy devices receive it
# before they ever hit the default.
#
# Remediation: a scheduled sweep (remediate_component_store_failures) detects
# targets whose latest deployment failed with the store-limit reason, submits
# a config-only revision built from the device's *installed* root components
# (nothing new to download, so the pre-merge check passes) plus the raised
# limit, then — once that revision completes — resubmits the original
# component set. State is carried in deployment tags, so the sweep is
# stateless and the user never intervenes.
COMPONENT_STORE_MAX_SIZE_BYTES = int(os.environ.get(
    'COMPONENT_STORE_MAX_SIZE_BYTES', str(30 * 1024 ** 3)))  # 30 GB

# Substring the Nucleus puts in the effective-deployment reason when the
# component store limit blocks artifact download.
STORE_LIMIT_REASON_MARKER = 'Component store size limit reached'

# Deployment tags that drive the stateless remediation workflow.
TAG_REMEDIATION_FOR = 'dda-portal:store-remediation-for'
TAG_RESUMED_FROM = 'dda-portal:store-remediation-resumed'


def _nucleus_store_configuration_update():
    """configurationUpdate merge raising componentStoreMaxSizeBytes."""
    return {
        'merge': json.dumps(
            {'componentStoreMaxSizeBytes': COMPONENT_STORE_MAX_SIZE_BYTES})
    }


def _version_key(version_str):
    """Convert a semantic version string to a comparable tuple. Non-numeric parts sort last."""
    parts = []
    for part in str(version_str).split('.'):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def get_device_nucleus_version(greengrass_client, thing_name):
    """
    Return the Greengrass Nucleus version currently running on a core device,
    or None if it can't be determined.
    """
    if not thing_name:
        return None
    try:
        resp = greengrass_client.get_core_device(coreDeviceThingName=thing_name)
        version = resp.get('coreVersion')
        if version:
            logger.info(f"Device {thing_name} is running Nucleus {version}")
            return version
    except Exception as e:
        logger.warning(f"Could not read core device {thing_name} nucleus version: {e}")
    return None


def resolve_target_running_nucleus(greengrass_client, iot_client, target_devices, target_thing_group, region, account_id):
    """
    Determine the Nucleus version currently installed on the deployment target.
    For a single device we query it directly; for a thing group we inspect the
    member devices and use the first running version found (assuming a
    homogeneous group). Returns a version string or None.
    """
    thing_names = []
    if target_devices:
        thing_names = list(target_devices)
    elif target_thing_group:
        try:
            paginator = iot_client.get_paginator('list_things_in_thing_group')
            for page in paginator.paginate(thingGroupName=target_thing_group, maxResults=50):
                thing_names.extend(page.get('things', []))
                if thing_names:
                    break
        except Exception as e:
            logger.warning(f"Could not list things in group {target_thing_group}: {e}")

    for thing_name in thing_names:
        version = get_device_nucleus_version(greengrass_client, thing_name)
        if version:
            return version
    return None


def get_latest_nucleus_version(greengrass_client, region):
    """
    Return the latest available aws.greengrass.Nucleus version.

    Greengrass refuses to update the Nucleus across minor/major versions unless
    the Nucleus is included as an explicit top-level target component. By
    auto-including the latest Nucleus version as a top-level component, the
    deployment is allowed to update the Nucleus and the
    "no component of type nucleus was included as target component" error is avoided.

    Returns the latest version string (e.g. '2.17.0') or None if it can't be
    determined (in which case the caller should include Nucleus without a pinned
    version so Greengrass resolves the latest itself).
    """
    nucleus_arn = f"arn:aws:greengrass:{region}:aws:components:aws.greengrass.Nucleus"
    versions = []
    try:
        paginator = greengrass_client.get_paginator('list_component_versions')
        for page in paginator.paginate(arn=nucleus_arn):
            for cv in page.get('componentVersions', []):
                v = cv.get('componentVersion')
                if v:
                    versions.append(v)
    except Exception as e:
        logger.warning(f"Could not list aws.greengrass.Nucleus versions: {e}")

    if versions:
        latest = sorted(versions, key=_version_key)[-1]
        logger.info(f"Latest available aws.greengrass.Nucleus version: {latest}")
        return latest
    return None


def _nucleus_satisfies(version, requirement):
    """Return True if a concrete Nucleus version satisfies a Greengrass semver
    requirement string of space-separated comparators (AND-ed), e.g.
    '>=2.1.0 <2.18.0' or '=2.16.1'. An empty/unparseable requirement is treated
    as satisfied (SOFT/best-effort)."""
    if not requirement:
        return True
    v = _version_key(version)
    for token in requirement.split():
        token = token.strip()
        if not token:
            continue
        m = re.match(r'^(>=|<=|==|=|>|<)?\s*(.+)$', token)
        if not m:
            continue
        op = m.group(1) or '=='
        bound = _version_key(m.group(2))
        if op in ('=', '==') and not (v == bound):
            return False
        if op == '>=' and not (v >= bound):
            return False
        if op == '<=' and not (v <= bound):
            return False
        if op == '>' and not (v > bound):
            return False
        if op == '<' and not (v < bound):
            return False
    return True


def resolve_public_component_version(greengrass_client, region, component_name,
                                     running_nucleus, fallback_version):
    """Return the newest public `component_name` version whose Nucleus
    dependency is satisfied by the device's running Nucleus.

    Public AWS components (LogManager, ShadowManager, ...) declare a Nucleus
    VersionRequirement (e.g. '>=2.1.0 <2.15.0'). Auto-including a version whose
    ceiling excludes the device's pinned Nucleus produces
    FAILED_NO_STATE_CHANGE / NoAvailableComponentVersion. We therefore select
    the highest version compatible with `running_nucleus`. Falls back to the
    static `fallback_version` when the running Nucleus is unknown or resolution
    fails (network/permission), so the behavior is never worse than a
    hard-coded pin."""
    if not running_nucleus:
        return fallback_version

    component_arn = f"arn:aws:greengrass:{region}:aws:components:{component_name}"
    candidates = []
    try:
        paginator = greengrass_client.get_paginator('list_component_versions')
        for page in paginator.paginate(arn=component_arn):
            for cv in page.get('componentVersions', []):
                v = cv.get('componentVersion')
                if v:
                    candidates.append(v)
    except Exception as e:
        logger.warning(f"Could not list {component_name} versions, using {fallback_version}: {e}")
        return fallback_version

    # Highest version first; return the first one compatible with the
    # device's running Nucleus.
    for candidate_version in sorted(candidates, key=_version_key, reverse=True):
        try:
            resp = greengrass_client.get_component(
                arn=f"{component_arn}:versions:{candidate_version}",
                recipeOutputFormat='JSON'
            )
            recipe_raw = resp.get('recipe')
            if isinstance(recipe_raw, (bytes, bytearray)):
                recipe_raw = recipe_raw.decode('utf-8')
            recipe = json.loads(recipe_raw)
            nucleus_req = (
                recipe.get('ComponentDependencies', {})
                      .get('aws.greengrass.Nucleus', {})
                      .get('VersionRequirement', '')
            )
            if _nucleus_satisfies(running_nucleus, nucleus_req):
                logger.info(
                    f"Resolved {component_name} {candidate_version} (Nucleus "
                    f"requirement '{nucleus_req}') for running Nucleus "
                    f"{running_nucleus}"
                )
                return candidate_version
        except Exception as e:
            logger.warning(f"Could not inspect {component_name} {candidate_version}: {e}")
            continue

    logger.warning(
        f"No {component_name} version found compatible with Nucleus "
        f"{running_nucleus}; falling back to {fallback_version}"
    )
    return fallback_version


def resolve_log_manager_version(greengrass_client, region, running_nucleus):
    """Newest aws.greengrass.LogManager version compatible with the device's
    running Nucleus, falling back to the pinned LOG_MANAGER_VERSION."""
    return resolve_public_component_version(
        greengrass_client, region, 'aws.greengrass.LogManager',
        running_nucleus, LOG_MANAGER_VERSION)


def resolve_shadow_manager_version(greengrass_client, region, running_nucleus):
    """Newest aws.greengrass.ShadowManager version compatible with the
    device's running Nucleus, falling back to the pinned
    SHADOW_MANAGER_VERSION. The auto-included ShadowManager must carry a
    componentVersion: the CreateDeployment API rejects component entries
    without one."""
    return resolve_public_component_version(
        greengrass_client, region, 'aws.greengrass.ShadowManager',
        running_nucleus, SHADOW_MANAGER_VERSION)


def portal_shadow_sync_config():
    """The portal's full ShadowManager `synchronize` configuration — the
    SINGLE source for both the fresh-add auto-include and the ensure/merge
    path (shadowmanager-sync-config-on-revision: the two paths must never
    drift). Returns a fresh dict per call so callers can mutate and
    serialize freely."""
    return {
        'synchronize': {
            'direction': 'betweenDeviceAndCloud',
            'coreThing': {
                'classic': True,
                'namedShadows': [
                    CAMERA_REGISTRY_SHADOW_NAME,
                    CAMERA_BINDINGS_SHADOW_NAME,
                    MODEL_STATUS_SHADOW_NAME
                ]
            }
        }
    }


def ensure_shadow_manager_sync(components_map, resolve_version):
    """Ensure the components map about to be SUBMITTED carries the portal
    ShadowManager `synchronize` configuration (bugfix:
    shadowmanager-sync-config-on-revision).

    Portal deployment revisions used to ship aws.greengrass.ShadowManager
    bare (or with a stale merge) because the auto-include was gated on the
    component being ABSENT from the submitted set. Greengrass preserves the
    device's last-applied configuration for a bare entry, so shadow names
    added to the portal list after a target's last CONFIGURED revision
    (e.g. dda-model-status) never reached revised devices.

    Args:
        components_map: the dict about to be submitted; mutated in place.
        resolve_version: ZERO-ARG callable returning a pinned version
            string; called lazily, at most once, and only when the entry
            lacks an explicit componentVersion (explicit caller versions
            are respected verbatim — requirement 3.5).

    Returns:
        'added'     — ShadowManager was absent: the full portal entry was
                      added (the fresh-add auto-include path, byte-identical
                      to the original inline block — requirement 3.1).
        'merged'    — an existing entry was completed: a bare/corrupt merge
                      got the full portal config injected (corrupt logged),
                      a stale merge got the portal shadow names UNIONED
                      into namedShadows (existing order kept, missing
                      portal names appended in portal-constant order;
                      caller extras and field values preserved — 2.1/2.2,
                      3.2/3.3), and/or a missing componentVersion was
                      resolved.
        'unchanged' — the entry already covers every portal shadow name and
                      carries a version: nothing is touched and the merge
                      STRING is left byte-identical (no re-serialization).
    """
    component_name = 'aws.greengrass.ShadowManager'
    portal_names = [CAMERA_REGISTRY_SHADOW_NAME,
                    CAMERA_BINDINGS_SHADOW_NAME,
                    MODEL_STATUS_SHADOW_NAME]

    if component_name not in components_map:
        components_map[component_name] = {
            'componentVersion': resolve_version(),
            'configurationUpdate': {
                'merge': json.dumps(portal_shadow_sync_config())
            }
        }
        return 'added'

    entry = components_map[component_name]
    version_filled = False
    if not entry.get('componentVersion'):
        # Only a version-less entry triggers the (lazy) resolver: the real
        # CreateDeployment API rejects component entries without a
        # componentVersion.
        entry['componentVersion'] = resolve_version()
        version_filled = True

    config_update = entry.get('configurationUpdate')
    merge = (config_update.get('merge')
             if isinstance(config_update, dict) else None)

    document = None
    entry_state = 'bare'
    if merge:
        try:
            document = json.loads(merge)
        except (TypeError, ValueError):
            document = None
        if isinstance(document, dict):
            entry_state = 'stale'
        else:
            # Nothing recoverable exists to preserve; shipping a corrupt
            # merge helps nobody — replace with the full portal config.
            logger.warning(
                f"Corrupt {component_name} configurationUpdate.merge "
                f"(unparseable or non-dict document); replacing with the "
                f"full portal synchronize config: {merge!r}")
            document = None
            entry_state = 'corrupt'

    if document is not None:
        # Strict no-op gate: an entry already covering every portal shadow
        # name is left untouched — the merge string byte-identical, no
        # default-filling of direction/classic — so a compliant submission
        # is a no-op (preservation scope; requirements 3.2, 3.3).
        sync = document.get('synchronize')
        if isinstance(sync, dict):
            core = sync.get('coreThing')
            if isinstance(core, dict):
                named = core.get('namedShadows')
                if (isinstance(named, list)
                        and all(n in named for n in portal_names)):
                    if version_filled:
                        logger.info(
                            f"Resolved missing componentVersion "
                            f"{entry['componentVersion']} for "
                            f"{component_name} (synchronize merge already "
                            f"compliant, left byte-identical)")
                        return 'merged'
                    return 'unchanged'

    if document is None:
        # Bare (or corrupt) entry: inject the full portal config. Sibling
        # configurationUpdate keys (e.g. a reset list) are preserved.
        if not isinstance(config_update, dict):
            config_update = {}
            entry['configurationUpdate'] = config_update
        config_update['merge'] = json.dumps(portal_shadow_sync_config())
        resulting_names = list(portal_names)
    else:
        # Parseable but stale: setdefault-navigate and UNION — portal
        # defaults fill ABSENT keys only, present values survive
        # byte-for-byte, unknown keys anywhere are untouched.
        sync = document.get('synchronize')
        if not isinstance(sync, dict):
            if sync is not None:
                logger.warning(
                    f"Corrupt {component_name} merge: non-dict "
                    f"'synchronize' node ({sync!r}); replacing the subtree "
                    f"with the portal defaults")
            sync = {}
            document['synchronize'] = sync
        core = sync.get('coreThing')
        if not isinstance(core, dict):
            if core is not None:
                logger.warning(
                    f"Corrupt {component_name} merge: non-dict 'coreThing' "
                    f"node ({core!r}); replacing the subtree with the "
                    f"portal defaults")
            core = {}
            sync['coreThing'] = core
        sync.setdefault('direction', 'betweenDeviceAndCloud')
        core.setdefault('classic', True)
        named = core.get('namedShadows')
        if not isinstance(named, list):
            if named is not None:
                logger.warning(
                    f"Corrupt {component_name} merge: non-list "
                    f"'namedShadows' ({named!r}); replacing with the "
                    f"portal shadow names")
            named = []
            core['namedShadows'] = named
        # Union — never replace: existing names keep their order, missing
        # portal names are appended in portal-constant order.
        for shadow_name in portal_names:
            if shadow_name not in named:
                named.append(shadow_name)
        config_update['merge'] = json.dumps(document)
        resulting_names = list(named)

    # Merge-into-existing is logged, not reported in auto_included (design
    # Decision 4 — the Nucleus caller-supplied elif precedent).
    logger.info(
        f"Completed existing {component_name} entry ({entry_state}) with "
        f"the portal synchronize config; namedShadows now: "
        f"{resulting_names}")
    return 'merged'


def handler(event, context):
    """
    Handle deployment management requests
    
    GET /api/v1/deployments              - List deployments
                                           (?workflow_id=... lists workflow
                                           deployments with per-device status)
    GET /api/v1/deployments/{id}         - Get deployment details
    POST /api/v1/deployments             - Create deployment
                                           (component_type: workflow deploys a
                                           packaged Workflow_Component)
    DELETE /api/v1/deployments/{id}      - Cancel deployment
    """
    try:
        # Scheduled EventBridge invocation: component store limit remediation
        # sweep (no API Gateway envelope on these events).
        if event.get('action') == 'remediate-component-store-limit':
            return remediate_component_store_failures(event, context)

        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Deployments request: {http_method} {path}")
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return create_response(200, {})
        
        user = get_user_from_event(event)
        
        if http_method == 'GET' and not path_parameters.get('id'):
            # Deploy-time Camera_Binding context (camera-registry-sync
            # Requirements 8.1, 8.5): per-target Camera_Sources for each
            # Camera_Input_Node with hint-matching pre-selection. Checked
            # before the target-deployment dispatch because this view also
            # names its targets in the query string.
            if (path.endswith('/binding-context')
                    or query_parameters.get('view') == 'binding-context'):
                return get_camera_binding_context(user, query_parameters)
            # Sub-resource: look up the existing deployment for a specific target
            if path.endswith('/target-deployment') or query_parameters.get('target_device') or query_parameters.get('target_thing_group'):
                return get_target_deployment(user, query_parameters)
            # Workflow page: workflow deployment associations with per-device
            # Greengrass status (Requirements 8.2, 8.3)
            if query_parameters.get('workflow_id'):
                return list_workflow_deployments(user, query_parameters)
            return list_deployments(user, query_parameters)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_deployment(path_parameters['id'], user, query_parameters)
        elif http_method == 'POST':
            body = json.loads(event.get('body') or '{}')
            # Workflow_Component deployments carry component_type: workflow
            # (Requirements 8.1-8.5, 11.5)
            if body.get('component_type') == 'workflow' or body.get('workflow_id'):
                return create_workflow_deployment(body, user)
            return create_deployment(body, user)
        elif http_method == 'DELETE' and path_parameters.get('id'):
            return cancel_deployment(path_parameters['id'], user, query_parameters)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in deployments handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_deployments(user, query_params):
    """List Greengrass deployments for a use case"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        region = get_usecase_region(usecase)
        
        # Create Greengrass client (handles both single-account and cross-account)
        greengrass_client = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-list-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        
        deployments = []
        next_token = None
        
        # List all deployments
        while True:
            params = {'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_deployments(**params)
            
            for dep in response.get('deployments', []):
                # Convert datetime to ISO string
                creation_ts = dep.get('creationTimestamp')
                if creation_ts and hasattr(creation_ts, 'isoformat'):
                    creation_ts = creation_ts.isoformat()
                
                deployments.append({
                    'deployment_id': dep.get('deploymentId'),
                    'deployment_name': dep.get('deploymentName', ''),
                    'target_arn': dep.get('targetArn', ''),
                    'revision_id': dep.get('revisionId', ''),
                    'deployment_status': dep.get('deploymentStatus', 'UNKNOWN'),
                    'is_latest_for_target': dep.get('isLatestForTarget', False),
                    'creation_timestamp': creation_ts,
                    'usecase_id': usecase_id
                })
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        logger.info(f"Found {len(deployments)} deployments")
        
        return create_response(200, {
            'deployments': deployments,
            'count': len(deployments)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing deployments: {str(e)}")
        return create_response(500, {'error': f'Failed to list deployments: {str(e)}'})
    except Exception as e:
        logger.error(f"Error listing deployments: {str(e)}")
        return create_response(500, {'error': 'Failed to list deployments'})


def get_deployment(deployment_id, user, query_params):
    """Get detailed information about a deployment"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        region = get_usecase_region(usecase)
        
        # Create Greengrass client (handles both single-account and cross-account)
        greengrass_client = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-get-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        
        # Get deployment details
        response = greengrass_client.get_deployment(deploymentId=deployment_id)
        
        # Convert datetime fields
        creation_ts = response.get('creationTimestamp')
        if creation_ts and hasattr(creation_ts, 'isoformat'):
            creation_ts = creation_ts.isoformat()
        
        # Get components in deployment
        components = response.get('components', {})
        component_list = []
        for comp_name, comp_config in components.items():
            component_list.append({
                'component_name': comp_name,
                'component_version': comp_config.get('componentVersion', 'latest'),
                'configuration_update': comp_config.get('configurationUpdate', {})
            })
        
        deployment = {
            'deployment_id': response.get('deploymentId'),
            'deployment_name': response.get('deploymentName', ''),
            'target_arn': response.get('targetArn', ''),
            'revision_id': response.get('revisionId', ''),
            'deployment_status': response.get('deploymentStatus', 'UNKNOWN'),
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'is_latest_for_target': response.get('isLatestForTarget', False),
            'creation_timestamp': creation_ts,
            'components': component_list,
            'deployment_policies': response.get('deploymentPolicies', {}),
            'tags': response.get('tags', {}),
            'usecase_id': usecase_id
        }
        
        # Get effective deployments for target devices to show per-device status
        # This is especially useful for failed deployments to see which devices failed
        effective_deployments = []
        target_arn = response.get('targetArn', '')
        
        try:
            # Extract target name from ARN to get effective deployments
            if ':thing/' in target_arn:
                # Single device target
                thing_name = target_arn.split(':thing/')[-1]
                eff_response = greengrass_client.list_effective_deployments(
                    coreDeviceThingName=thing_name
                )
                for eff_dep in eff_response.get('effectiveDeployments', []):
                    if eff_dep.get('deploymentId') == deployment_id:
                        # Convert timestamps
                        modified_ts = eff_dep.get('modifiedTimestamp')
                        if modified_ts and hasattr(modified_ts, 'isoformat'):
                            modified_ts = modified_ts.isoformat()
                        
                        effective_deployments.append({
                            'core_device': thing_name,
                            'deployment_status': eff_dep.get('coreDeviceExecutionStatus', 'UNKNOWN'),
                            'reason': eff_dep.get('reason', ''),
                            'description': eff_dep.get('description', ''),
                            'status_details': eff_dep.get('statusDetails', {}),
                            'modified_timestamp': modified_ts
                        })
                        break
            elif ':thinggroup/' in target_arn:
                # Thing group target - list core devices in the group and get their status
                thing_group_name = target_arn.split(':thinggroup/')[-1]
                
                # List core devices (we'll check effective deployments for each)
                core_devices_response = greengrass_client.list_core_devices(
                    thingGroupArn=target_arn,
                    maxResults=50
                )
                
                for device in core_devices_response.get('coreDevices', []):
                    thing_name = device.get('coreDeviceThingName')
                    if thing_name:
                        try:
                            eff_response = greengrass_client.list_effective_deployments(
                                coreDeviceThingName=thing_name
                            )
                            for eff_dep in eff_response.get('effectiveDeployments', []):
                                if eff_dep.get('deploymentId') == deployment_id:
                                    modified_ts = eff_dep.get('modifiedTimestamp')
                                    if modified_ts and hasattr(modified_ts, 'isoformat'):
                                        modified_ts = modified_ts.isoformat()
                                    
                                    effective_deployments.append({
                                        'core_device': thing_name,
                                        'deployment_status': eff_dep.get('coreDeviceExecutionStatus', 'UNKNOWN'),
                                        'reason': eff_dep.get('reason', ''),
                                        'description': eff_dep.get('description', ''),
                                        'status_details': eff_dep.get('statusDetails', {}),
                                        'modified_timestamp': modified_ts
                                    })
                                    break
                        except ClientError as e:
                            logger.warning(f"Could not get effective deployments for {thing_name}: {e}")
        except ClientError as e:
            logger.warning(f"Could not get effective deployments: {e}")
        
        deployment['effective_deployments'] = effective_deployments
        
        # Extract error information from effective deployments
        error_messages = []
        for eff_dep in effective_deployments:
            if eff_dep.get('deployment_status') in ['FAILED', 'REJECTED', 'TIMED_OUT']:
                error_info = {
                    'device': eff_dep.get('core_device'),
                    'status': eff_dep.get('deployment_status'),
                    'reason': eff_dep.get('reason', ''),
                    'description': eff_dep.get('description', '')
                }
                # Add status details if available
                status_details = eff_dep.get('status_details', {})
                if status_details:
                    error_info['detailed_status'] = status_details.get('detailedStatus', '')
                    error_info['detailed_status_reason'] = status_details.get('detailedStatusReason', '')
                error_messages.append(error_info)
        
        deployment['error_messages'] = error_messages
        
        log_audit_event(
            user['user_id'], 'get_deployment', 'deployment', deployment_id, 'success'
        )
        
        return create_response(200, {'deployment': deployment})
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return create_response(404, {'error': 'Deployment not found'})
        logger.error(f"AWS error getting deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to get deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to get deployment'})


def find_latest_deployment_for_target(greengrass_client, target_arn):
    """
    Return the latest (currently-effective) Greengrass deployment for a target ARN,
    or None if the target has no deployment yet.

    Greengrass deployments are immutable: creating a new deployment to the same
    target ARN supersedes the previous one. There is only ever one
    `isLatestForTarget` deployment per target. We use this to treat re-deployments
    as revisions of the existing deployment rather than brand-new deployments.
    """
    next_token = None
    try:
        while True:
            params = {'maxResults': 100, 'targetArn': target_arn}
            if next_token:
                params['nextToken'] = next_token
            response = greengrass_client.list_deployments(**params)
            for dep in response.get('deployments', []):
                if dep.get('isLatestForTarget'):
                    return dep
            next_token = response.get('nextToken')
            if not next_token:
                break
    except Exception as e:
        logger.warning(f"Could not look up existing deployment for {target_arn}: {e}")
    return None


def find_group_member_individual_deployments(greengrass_client, iot_client, region, account_id, thing_group_name):
    """
    For a thing group target, find member devices that already have their OWN
    individual (thing-level) deployment. These create a conflict: a device with
    both a thing-level deployment and a group-level deployment ends up running two
    deployments, with unpredictable merged behavior. The UI should warn the user
    to cancel the individual deployments first.

    Returns a list of dicts: {device, deployment_id, deployment_name, deployment_status}.
    """
    conflicts = []
    member_things = []
    try:
        paginator = iot_client.get_paginator('list_things_in_thing_group')
        for page in paginator.paginate(thingGroupName=thing_group_name, maxResults=100):
            member_things.extend(page.get('things', []))
    except Exception as e:
        logger.warning(f"Could not list things in group {thing_group_name}: {e}")
        return conflicts

    for thing_name in member_things:
        thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{thing_name}"
        individual = find_latest_deployment_for_target(greengrass_client, thing_arn)
        if individual:
            conflicts.append({
                'device': thing_name,
                'deployment_id': individual.get('deploymentId'),
                'deployment_name': individual.get('deploymentName', ''),
                'deployment_status': individual.get('deploymentStatus', 'UNKNOWN'),
            })
    return conflicts


def get_target_deployment(user, query_params):
    """
    Return the existing latest deployment for a given target so the UI can
    decide whether a new deployment would be a revision of an existing one.

    Query params: usecase_id (required), target_device OR target_thing_group.
    """
    try:
        usecase_id = query_params.get('usecase_id')
        target_device = query_params.get('target_device')
        target_thing_group = query_params.get('target_thing_group')

        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        if not target_device and not target_thing_group:
            return create_response(400, {'error': 'target_device or target_thing_group required'})

        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})

        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})

        region = get_usecase_region(usecase)
        account_id = usecase.get('account_id', '')

        greengrass_client = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-target-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )

        if target_thing_group:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thinggroup/{target_thing_group}"
        else:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thing/{target_device}"

        # For a thing group target, detect member devices that already have their
        # own individual (thing-level) deployment — these conflict with a group
        # deployment and should be cancelled first.
        group_member_conflicts = []
        if target_thing_group:
            iot_client = get_usecase_client(
                'iot',
                usecase,
                session_name=f"iot-target-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
                region=region
            )
            group_member_conflicts = find_group_member_individual_deployments(
                greengrass_client, iot_client, region, account_id, target_thing_group
            )

        existing = find_latest_deployment_for_target(greengrass_client, target_arn)
        if not existing:
            return create_response(200, {
                'existing_deployment': None,
                'group_member_conflicts': group_member_conflicts,
            })

        deployment_id = existing.get('deploymentId')

        # Fetch full details so the UI can pre-load components for revision
        components = []
        deployment_name = existing.get('deploymentName', '')
        try:
            detail = greengrass_client.get_deployment(deploymentId=deployment_id)
            deployment_name = detail.get('deploymentName', deployment_name)
            for comp_name, comp_config in detail.get('components', {}).items():
                components.append({
                    'component_name': comp_name,
                    'component_version': comp_config.get('componentVersion', 'latest'),
                })
        except Exception as e:
            logger.warning(f"Could not get deployment detail for {deployment_id}: {e}")

        creation_ts = existing.get('creationTimestamp')
        if creation_ts and hasattr(creation_ts, 'isoformat'):
            creation_ts = creation_ts.isoformat()

        return create_response(200, {
            'existing_deployment': {
                'deployment_id': deployment_id,
                'deployment_name': deployment_name,
                'target_arn': target_arn,
                'deployment_status': existing.get('deploymentStatus', 'UNKNOWN'),
                'revision_id': existing.get('revisionId', ''),
                'creation_timestamp': creation_ts,
                'components': components,
            },
            'group_member_conflicts': group_member_conflicts,
        })

    except ClientError as e:
        logger.error(f"AWS error getting target deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to get target deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting target deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to get target deployment'})


def create_deployment(body, user):
    """Create a new Greengrass deployment"""
    try:
        usecase_id = body.get('usecase_id')
        components = body.get('components', [])  # List of {component_name, component_version}
        target_devices = body.get('target_devices', [])
        target_thing_group = body.get('target_thing_group')
        deployment_name = body.get('deployment_name', '')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id required'})
        
        if not components:
            return create_response(400, {'error': 'At least one component required'})
        
        if not target_devices and not target_thing_group:
            return create_response(400, {'error': 'Either target_devices or target_thing_group required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        region = get_usecase_region(usecase)
        account_id = usecase.get('account_id', '')
        
        # Create Greengrass client (handles both single-account and cross-account)
        greengrass_client = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-create-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        
        # Build components map for deployment
        components_map = {}
        needs_nucleus = False
        
        for comp in components:
            comp_name = comp.get('component_name')
            comp_version = comp.get('component_version')
            if comp_name:
                # Only include componentVersion if it's a valid version string
                # Skip 0.0.0, 'unknown', 'latest', or empty versions - let Greengrass use the latest
                if comp_version and comp_version not in ['0.0.0', 'unknown', 'latest', '']:
                    components_map[comp_name] = {
                        'componentVersion': comp_version
                    }
                else:
                    # No version specified - Greengrass will use the latest version
                    components_map[comp_name] = {}
                
                # Check if this component requires Nucleus to be included
                for dda_comp in DDA_COMPONENTS_REQUIRING_NUCLEUS:
                    if comp_name.startswith(dda_comp):
                        needs_nucleus = True
                        break
        
        # NOTE: When DDA components are deployed we auto-include the latest Nucleus
        # as a top-level component (resolved below, after the target is known).
        # DDA components depend on Nucleus, and Greengrass refuses to perform a
        # minor/major Nucleus update unless Nucleus is an explicit target component
        # ("no component of type nucleus was included as target component").
        # Including it as a top-level component authorizes the update.
        auto_included = []

        # Resolve the device's running Nucleus once, up front: BOTH the
        # auto-included LogManager version and the pinned Nucleus version depend
        # on it. The iot_client is reused for target resolution below.
        iot_client = get_usecase_client(
            'iot',
            usecase,
            session_name=f"iot-deploy-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        running_nucleus = None
        if needs_nucleus:
            running_nucleus = resolve_target_running_nucleus(
                greengrass_client, iot_client, target_devices,
                target_thing_group, region, account_id
            )

        # Standalone Plugin_Component deployments (custom-node-designer
        # 16.3, 16.6): any dda.plugin.* component in the requested set is
        # subject to the pre-submit lifecycle gate (test-state only to
        # devices flagged test_device; dev-state rejected for any target)
        # and the per-device architecture gate before submission.
        plugin_component_targets = {
            comp['component_name']: comp.get('component_version')
            for comp in components
            if str(comp.get('component_name', '')).startswith(
                PLUGIN_COMPONENT_PREFIX)
        }
        resolved_plugin_devices = []
        if plugin_component_targets:
            resolved_plugin_devices = resolve_target_thing_names(
                iot_client, target_devices, target_thing_group)
            gate_error = check_plugin_deployment_gates(
                plugin_component_targets, resolved_plugin_devices)
            if gate_error:
                return gate_error

        # vLLM architecture gate (vllm-triton-inference 3.3, 3.4, 3.7,
        # 8.5, 8.6): evaluated iff the requested set contains a
        # model-vllm-* component or a workflow component whose version
        # item records has_llm_inference. Component sets with neither
        # produce no manifests and are untouched — pre-feature
        # validation applies verbatim, jp4 included.
        vllm_manifests = collect_vllm_component_manifests({
            comp['component_name']: comp.get('component_version')
            for comp in components if comp.get('component_name')
        })
        if vllm_manifests:
            resolved_vllm_devices = (
                resolved_plugin_devices
                or resolve_target_thing_names(
                    iot_client, target_devices, target_thing_group))
            gate_error = check_vllm_deployment_gate(
                vllm_manifests, resolved_vllm_devices)
            if gate_error:
                return gate_error

        # Auto-include CloudWatch log manager for device logging
        if needs_nucleus and 'aws.greengrass.LogManager' not in components_map:
            # Build componentLogsConfigurationMap dynamically from all components in the deployment
            component_log_config_map = {}
            
            # Always include system-level Greengrass component logs
            component_log_config_map['com.aws.greengrass'] = {
                'minimumLogLevel': 'INFO',
                'diskSpaceLimit': 10,
                'diskSpaceLimitUnit': 'MB',
                'deleteLogFileAfterCloudUpload': False
            }
            
            # Add an entry for every user component in the deployment
            # This ensures model components (user-named) and DDA components all get logged
            skip_components = {'aws.greengrass.Nucleus', 'aws.greengrass.LogManager'}
            for comp_name in components_map:
                if comp_name not in skip_components:
                    component_log_config_map[comp_name] = {
                        'minimumLogLevel': 'INFO',
                        'diskSpaceLimit': 10,
                        'diskSpaceLimitUnit': 'MB',
                        'deleteLogFileAfterCloudUpload': False
                    }
            
            log_manager_config = {
                'logsUploaderConfiguration': {
                    'systemLogsConfiguration': {
                        'uploadToCloudWatch': True,
                        'minimumLogLevel': 'INFO',
                        'diskSpaceLimit': 25,
                        'diskSpaceLimitUnit': 'MB',
                        'deleteLogFileAfterCloudUpload': False
                    },
                    'componentLogsConfigurationMap': component_log_config_map,
                    'periodicUploadIntervalSec': 300  # Upload every 5 minutes
                }
            }
            
            # Pick the newest LogManager whose Nucleus dependency covers the
            # device's running Nucleus (falls back to LOG_MANAGER_VERSION when the
            # running Nucleus is unknown). This prevents the LogManager<->Nucleus
            # version conflict as devices move to newer Nucleus releases.
            log_manager_version = resolve_log_manager_version(
                greengrass_client, region, running_nucleus
            )
            components_map['aws.greengrass.LogManager'] = {
                'componentVersion': log_manager_version,
                'configurationUpdate': {
                    'merge': json.dumps(log_manager_config)
                }
            }
            auto_included.append({
                'component_name': 'aws.greengrass.LogManager',
                'component_version': log_manager_version,
                'reason': 'Required for CloudWatch logging from devices'
            })
            logger.info(f"Auto-included aws.greengrass.LogManager {log_manager_version} with logging for components: {list(component_log_config_map.keys())}")
        
        # Auto-include InferenceUploader for automatic S3 upload of inference results
        # Only include if explicitly enabled in UseCase configuration (opt-in)
        enable_inference_uploader = usecase.get('enable_inference_uploader', False)
        
        if enable_inference_uploader and needs_nucleus and 'aws.edgeml.dda.InferenceUploader' not in components_map:
            # Build S3 configuration for InferenceUploader
            s3_bucket = usecase.get('inference_uploader_s3_bucket') or f"dda-inference-results-{account_id}"
            
            # Get configurable upload interval (default 5 minutes, can be immediate, hourly, etc.)
            upload_interval = usecase.get('inference_uploader_interval_seconds', 300)  # Default 5 minutes
            
            inference_uploader_config = {
                's3Bucket': s3_bucket,
                'uploadIntervalSeconds': upload_interval,
                'batchSize': usecase.get('inference_uploader_batch_size', 100),
                'localRetentionDays': usecase.get('inference_uploader_retention_days', 7),
                'uploadImages': usecase.get('inference_uploader_upload_images', True),
                'uploadMetadata': usecase.get('inference_uploader_upload_metadata', True),
                'inferenceResultsPath': '/aws_dda/inference-results',
                'awsRegion': region
            }
            
            components_map['aws.edgeml.dda.InferenceUploader'] = {
                'componentVersion': '1.0.0',
                'configurationUpdate': {
                    'merge': json.dumps(inference_uploader_config)
                }
            }
            auto_included.append({
                'component_name': 'aws.edgeml.dda.InferenceUploader',
                'component_version': '1.0.0',
                'reason': f'Automatic upload of inference results to S3 (interval: {upload_interval}s)'
            })
            logger.info(f"Auto-included aws.edgeml.dda.InferenceUploader with S3 bucket {s3_bucket}, interval {upload_interval}s")
        elif not enable_inference_uploader:
            logger.info("InferenceUploader not included - disabled in UseCase configuration")
        
        # Ensure ShadowManager carries the portal synchronization config for
        # the camera-registry-sync named shadows. ShadowManager is already an
        # implicit dependency of the LocalServer component (VersionRequirement
        # >=2.2.0), but without an explicit `synchronize` configuration it runs
        # with its default config and the dda-camera-registry /
        # dda-camera-bindings local shadows never mirror to IoT Core
        # (devices stay "Never synced" in the Portal). Absent -> auto-included
        # ('added'), pinned to the newest public version compatible with the
        # device's running Nucleus (fallback SHADOW_MANAGER_VERSION): the
        # CreateDeployment API requires componentVersion on every component
        # entry and rejects the deployment otherwise ("Missing required
        # parameter in components.aws.greengrass.ShadowManager:
        # componentVersion"). Present (the revise flow resubmits it bare —
        # shadowmanager-sync-config-on-revision) -> the portal config is
        # merged into the caller's entry ('merged', logged but not reported
        # in auto_included — the caller-supplied-Nucleus elif precedent
        # below), so revisions can no longer disarm the synchronize config.
        if needs_nucleus:
            shadow_sync_result = ensure_shadow_manager_sync(
                components_map,
                resolve_version=lambda: resolve_shadow_manager_version(
                    greengrass_client, region, running_nucleus))
            if shadow_sync_result == 'added':
                shadow_manager_version = components_map[
                    'aws.greengrass.ShadowManager']['componentVersion']
                auto_included.append({
                    'component_name': 'aws.greengrass.ShadowManager',
                    'component_version': shadow_manager_version,
                    'reason': (f'Syncs the {CAMERA_REGISTRY_SHADOW_NAME}, '
                               f'{CAMERA_BINDINGS_SHADOW_NAME} and '
                               f'{MODEL_STATUS_SHADOW_NAME} named shadows with '
                               'IoT Core for camera registry synchronization '
                               'and model GPU-fallback status visibility')
                })
                logger.info(
                    f"Auto-included aws.greengrass.ShadowManager {shadow_manager_version} with "
                    f"synchronization for named shadows: {CAMERA_REGISTRY_SHADOW_NAME}, "
                    f"{CAMERA_BINDINGS_SHADOW_NAME}, {MODEL_STATUS_SHADOW_NAME}")
        
        # Determine target ARN (iot_client and running_nucleus were resolved above)
        # Greengrass deployments must target thing groups, not individual things
        if target_thing_group:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thinggroup/{target_thing_group}"
        elif target_devices:
            # Deploy directly to the thing ARN (Greengrass supports both thing and thinggroup targets)
            target_arn = f"arn:aws:iot:{region}:{account_id}:thing/{target_devices[0]}"
            logger.info(f"Deploying directly to device: {target_devices[0]}")
        else:
            return create_response(400, {'error': 'No target devices or thing group specified'})

        # Auto-include Nucleus as a top-level component, pinned to the version the
        # device is ALREADY RUNNING.
        #
        # DDA components depend on Nucleus (>=2.4.0). Greengrass refuses to update
        # the Nucleus across minor/major versions unless Nucleus is an explicit
        # top-level target component ("no component of type nucleus was included as
        # target component").
        #
        # We pin Nucleus to the version the device is already running: it is, by
        # definition, already installed and compatible, satisfies the explicit-
        # nucleus requirement, and avoids forcing an incompatible upgrade. The
        # auto-included LogManager version is independently resolved (above) to be
        # compatible with this same running Nucleus, so the two never conflict. We
        # only fall back to unpinned if the running version can't be determined.
        if needs_nucleus and 'aws.greengrass.Nucleus' not in components_map:
            # running_nucleus was resolved once up front; reuse it here.
            # Both branches carry the componentStoreMaxSizeBytes merge so every
            # portal deployment raises the store limit above the 10 GB default
            # before the device ever hits it (see constants above).
            if running_nucleus:
                components_map['aws.greengrass.Nucleus'] = {
                    'componentVersion': running_nucleus,
                    'configurationUpdate': _nucleus_store_configuration_update()
                }
                auto_included.append({
                    'component_name': 'aws.greengrass.Nucleus',
                    'component_version': running_nucleus,
                    'reason': 'Pinned to the version already running on the device to satisfy the explicit-nucleus requirement without forcing an incompatible upgrade; raises componentStoreMaxSizeBytes'
                })
                logger.info(f"Pinned aws.greengrass.Nucleus to running device version {running_nucleus}")
            else:
                # Could not read the running version. Fall back to including Nucleus
                # without a pinned version so Greengrass resolves a compatible one
                # itself (respecting all component constraints).
                components_map['aws.greengrass.Nucleus'] = {
                    'configurationUpdate': _nucleus_store_configuration_update()
                }
                auto_included.append({
                    'component_name': 'aws.greengrass.Nucleus',
                    'component_version': 'auto',
                    'reason': 'Included as top-level component (unpinned) so Greengrass resolves a compatible Nucleus version; raises componentStoreMaxSizeBytes'
                })
                logger.info("Auto-included aws.greengrass.Nucleus (unpinned) as top-level component")
        elif needs_nucleus:
            # Caller supplied Nucleus explicitly — still ensure the store-limit
            # merge is present unless they set their own configurationUpdate.
            nucleus_entry = components_map['aws.greengrass.Nucleus']
            if 'configurationUpdate' not in nucleus_entry:
                nucleus_entry['configurationUpdate'] = (
                    _nucleus_store_configuration_update())

        # Subscribe accessControl (trigger-activation-runtime 10.2, 10.3):
        # when the set carries a workflow component whose version item
        # records subscribed_topics AND the LocalServer component, merge the
        # per-workflow aws.greengrass#SubscribeToIoTCore policy into the
        # LocalServer entry's configurationUpdate; when LocalServer is
        # absent, the returned warnings surface in the response. No-op —
        # zero new keys anywhere — when no workflow records topics.
        deployment_warnings = apply_subscribe_access_control(components_map)

        # Determine whether this target already has a deployment. Greengrass
        # deployments are immutable; creating a new deployment to the same target
        # ARN supersedes the previous one (a revision). To keep the deployment
        # identity stable for the user, reuse the existing deployment's name when
        # revising rather than generating a brand-new timestamped name.
        existing_deployment = find_latest_deployment_for_target(greengrass_client, target_arn)
        is_revision = existing_deployment is not None

        # Generate deployment name if not provided
        if not deployment_name:
            if is_revision and existing_deployment.get('deploymentName'):
                deployment_name = existing_deployment['deploymentName']
                logger.info(
                    f"Revising existing deployment '{deployment_name}' "
                    f"({existing_deployment.get('deploymentId')}) for target {target_arn}"
                )
            else:
                timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
                deployment_name = f"portal-deployment-{timestamp}"
        
        # Create deployment
        deployment_params = {
            'targetArn': target_arn,
            'deploymentName': deployment_name,
            'components': components_map,
            'tags': {
                'dda-portal:managed': 'true',
                'dda-portal:usecase-id': usecase_id,
                'dda-portal:created-by': user['user_id']
            }
        }
        
        # Add deployment policies for rollout configuration
        rollout_config = body.get('rollout_config', {})
        if rollout_config:
            deployment_params['deploymentPolicies'] = {
                'failureHandlingPolicy': 'ROLLBACK' if rollout_config.get('auto_rollback', True) else 'DO_NOTHING',
                'componentUpdatePolicy': {
                    'timeoutInSeconds': rollout_config.get('timeout_seconds', 60),
                    'action': 'NOTIFY_COMPONENTS'
                }
            }
        
        response = greengrass_client.create_deployment(**deployment_params)
        
        deployment_id = response.get('deploymentId')

        # Standalone Plugin_Component deployments are recorded in the
        # Deployments table with component_type: 'plugin' (task 10.5).
        if plugin_component_targets:
            record_plugin_deployment(
                deployment_id, usecase_id, plugin_component_targets,
                target_arn, resolved_plugin_devices, target_thing_group,
                is_revision,
                existing_deployment.get('deploymentId') if is_revision else None,
                user)

        log_audit_event(
            user['user_id'],
            'revise_deployment' if is_revision else 'create_deployment',
            'deployment', deployment_id,
            'success', {
                'usecase_id': usecase_id,
                'components': list(components_map.keys()),
                'target_arn': target_arn,
                'is_revision': is_revision,
                'superseded_deployment_id': existing_deployment.get('deploymentId') if is_revision else None
            }
        )
        
        logger.info(f"{'Revised' if is_revision else 'Created'} deployment {deployment_id} for usecase {usecase_id}")
        
        # Build response with full component list
        deployed_components = [
            {'component_name': name, 'component_version': config.get('componentVersion', 'latest')}
            for name, config in components_map.items()
        ]
        
        response_body = {
            'deployment_id': deployment_id,
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'components': deployed_components,
            'auto_included': auto_included,
            'is_revision': is_revision,
            'superseded_deployment_id': existing_deployment.get('deploymentId') if is_revision else None,
            'message': 'Deployment updated successfully' if is_revision else 'Deployment created successfully'
        }
        # Additive: only present when the subscribe accessControl merge had
        # something to say (trigger-activation-runtime 10.3) — pre-feature
        # responses stay byte-identical.
        if deployment_warnings:
            response_body['warnings'] = deployment_warnings
        return create_response(201, response_body)
        
    except ClientError as e:
        logger.error(f"AWS error creating deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to create deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error creating deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to create deployment'})


def cancel_deployment(deployment_id, user, query_params):
    """Cancel a Greengrass deployment"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        region = get_usecase_region(usecase)
        
        # Create Greengrass client (handles both single-account and cross-account)
        greengrass_client = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-cancel-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        
        # Cancel deployment
        greengrass_client.cancel_deployment(deploymentId=deployment_id)
        
        log_audit_event(
            user['user_id'], 'cancel_deployment', 'deployment', deployment_id,
            'success', {'usecase_id': usecase_id}
        )
        
        logger.info(f"Cancelled deployment {deployment_id}")
        
        return create_response(200, {
            'message': 'Deployment cancelled successfully',
            'deployment_id': deployment_id
        })
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return create_response(404, {'error': 'Deployment not found'})
        logger.error(f"AWS error cancelling deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to cancel deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error cancelling deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to cancel deployment'})


# ===========================================================================
# Component store limit auto-remediation (no user intervention)
# ===========================================================================

def _is_store_limit_failure(reason):
    """True when an effective-deployment reason is the Nucleus component
    store size rejection."""
    return bool(reason) and STORE_LIMIT_REASON_MARKER in reason


def _installed_root_components(greengrass_client, thing_name):
    """{componentName: componentVersion} for the device's installed ROOT
    components. These are already on disk, so a deployment pinning exactly
    this set downloads nothing and passes the store-size check."""
    installed = {}
    paginator = greengrass_client.get_paginator('list_installed_components')
    for page in paginator.paginate(coreDeviceThingName=thing_name,
                                   topologyFilter='ROOT'):
        for comp in page.get('installedComponents', []):
            name = comp.get('componentName')
            version = comp.get('componentVersion')
            if name and version:
                installed[name] = version
    return installed


def _submit_store_remediation(greengrass_client, thing_name, target_arn,
                              failed_deployment_id, deployment_name):
    """Submit the config-only remediation revision for one blocked target:
    the device's installed root components (no downloads) plus the raised
    componentStoreMaxSizeBytes on its running Nucleus."""
    installed = _installed_root_components(greengrass_client, thing_name)
    if not installed:
        logger.warning(
            f"store-remediation: no installed root components found for "
            f"{thing_name}; skipping")
        return None

    components_map = {
        name: {'componentVersion': version}
        for name, version in installed.items()
    }
    # The Nucleus is not a ROOT component; include it pinned to the running
    # version so the configuration merge is a same-version config-only change.
    nucleus_version = get_device_nucleus_version(greengrass_client, thing_name)
    nucleus_entry = {
        'configurationUpdate': _nucleus_store_configuration_update()
    }
    if nucleus_version:
        nucleus_entry['componentVersion'] = nucleus_version
    components_map['aws.greengrass.Nucleus'] = nucleus_entry

    response = greengrass_client.create_deployment(
        targetArn=target_arn,
        deploymentName=deployment_name,
        components=components_map,
        tags={
            'dda-portal:managed': 'true',
            TAG_REMEDIATION_FOR: failed_deployment_id
        }
    )
    remediation_id = response.get('deploymentId')
    logger.info(
        f"store-remediation: submitted config-only revision {remediation_id} "
        f"for {thing_name} (blocked deployment {failed_deployment_id}); "
        f"componentStoreMaxSizeBytes -> {COMPONENT_STORE_MAX_SIZE_BYTES}")
    return remediation_id


def _resume_original_deployment(greengrass_client, target_arn,
                                original_deployment_id):
    """Resubmit the user's original component set after a completed
    remediation. The store limit is now raised, so the downloads that were
    rejected fit. The resubmitted document also carries the Nucleus store
    merge so pre-fix deployment documents cannot reintroduce the default."""
    original = greengrass_client.get_deployment(
        deploymentId=original_deployment_id)
    components_map = original.get('components', {}) or {}
    nucleus_entry = components_map.setdefault('aws.greengrass.Nucleus', {})
    nucleus_entry.setdefault(
        'configurationUpdate', _nucleus_store_configuration_update())

    params = {
        'targetArn': target_arn,
        'deploymentName': original.get('deploymentName', ''),
        'components': components_map,
        'tags': {
            'dda-portal:managed': 'true',
            TAG_RESUMED_FROM: original_deployment_id
        }
    }
    if original.get('deploymentPolicies'):
        params['deploymentPolicies'] = original['deploymentPolicies']
    response = greengrass_client.create_deployment(**params)
    resumed_id = response.get('deploymentId')
    logger.info(
        f"store-remediation: resumed original deployment "
        f"{original_deployment_id} as {resumed_id} on {target_arn}")
    return resumed_id


def _sweep_device_for_store_remediation(greengrass_client, thing_name,
                                        region, account_id):
    """Evaluate one core device's latest effective deployment and advance the
    remediation state machine. Returns a small action record or None."""
    eff = greengrass_client.list_effective_deployments(
        coreDeviceThingName=thing_name, maxResults=1)
    deployments_list = eff.get('effectiveDeployments', [])
    if not deployments_list:
        return None
    latest = deployments_list[0]
    status = latest.get('coreDeviceExecutionStatus')
    reason = latest.get('reason', '')
    deployment_id = latest.get('deploymentId')
    if not deployment_id:
        return None

    # Only thing-targeted deployments are remediated automatically: the
    # portal deploys to thing ARNs. Group-target failures are logged for
    # visibility but left alone (a per-device revision of a group target
    # is not possible without changing the group deployment).
    target_arn = latest.get('targetArn') or (
        f"arn:aws:iot:{region}:{account_id}:thing/{thing_name}")
    if ':thing/' not in target_arn:
        if status in ('FAILED', 'REJECTED') and _is_store_limit_failure(reason):
            logger.warning(
                f"store-remediation: {thing_name} blocked by store limit on "
                f"group-target deployment {deployment_id}; automatic "
                f"remediation supports thing targets only")
        return None

    tags = {}
    try:
        deployment = greengrass_client.get_deployment(
            deploymentId=deployment_id)
        tags = deployment.get('tags', {}) or {}
    except ClientError as e:
        logger.warning(
            f"store-remediation: could not read deployment {deployment_id}: {e}")
        return None

    # State 1: latest deployment failed on the store limit and is not itself
    # a remediation → submit the config-only remediation revision.
    if status in ('FAILED', 'REJECTED') and _is_store_limit_failure(reason):
        if TAG_REMEDIATION_FOR in tags:
            # The remediation itself was rejected on the store check — the
            # installed set should never need downloads, so this indicates
            # something unexpected; stop rather than loop.
            logger.error(
                f"store-remediation: remediation deployment {deployment_id} "
                f"for {thing_name} itself failed with the store-limit "
                f"reason; manual investigation required")
            return None
        if TAG_RESUMED_FROM in tags:
            # The resumed deployment failed on the store limit again even
            # after the raise — the store genuinely cannot fit the new
            # artifacts under COMPONENT_STORE_MAX_SIZE_BYTES. Do not loop.
            logger.error(
                f"store-remediation: resumed deployment {deployment_id} for "
                f"{thing_name} hit the store limit again after raising to "
                f"{COMPONENT_STORE_MAX_SIZE_BYTES} bytes; the device disk "
                f"or limit needs attention")
            return None
        remediation_id = _submit_store_remediation(
            greengrass_client, thing_name, target_arn, deployment_id,
            deployment.get('deploymentName', ''))
        if remediation_id:
            return {'device': thing_name, 'action': 'remediation_submitted',
                    'deployment_id': remediation_id,
                    'blocked_deployment_id': deployment_id}
        return None

    # State 2: latest deployment is a completed remediation → resume the
    # original deployment it was created for.
    if status == 'COMPLETED' and TAG_REMEDIATION_FOR in tags:
        original_id = tags[TAG_REMEDIATION_FOR]
        resumed_id = _resume_original_deployment(
            greengrass_client, target_arn, original_id)
        return {'device': thing_name, 'action': 'original_resumed',
                'deployment_id': resumed_id,
                'original_deployment_id': original_id}

    return None


def remediate_component_store_failures(event, context):
    """Scheduled sweep (EventBridge): advance the store-limit remediation
    state machine across every use case's core devices. Stateless — all
    state lives in deployment tags."""
    actions = []
    try:
        usecases_table = dynamodb.Table(os.environ.get(
            'USECASES_TABLE', 'dda-portal-usecases'))
        scan = usecases_table.scan()
        usecases = scan.get('Items', [])
    except Exception as e:
        logger.error(f"store-remediation: could not list use cases: {e}")
        return {'actions': [], 'error': str(e)}

    for usecase in usecases:
        try:
            region = get_usecase_region(usecase)
            account_id = usecase.get('account_id', '')
            greengrass_client = get_usecase_client(
                'greengrassv2', usecase,
                session_name=f"gg-store-remediation-{int(datetime.utcnow().timestamp())}"[:64],
                region=region)
            paginator = greengrass_client.get_paginator('list_core_devices')
            for page in paginator.paginate():
                for device in page.get('coreDevices', []):
                    thing_name = device.get('coreDeviceThingName')
                    if not thing_name:
                        continue
                    try:
                        action = _sweep_device_for_store_remediation(
                            greengrass_client, thing_name, region, account_id)
                        if action:
                            action['usecase_id'] = usecase.get('usecase_id')
                            actions.append(action)
                    except ClientError as e:
                        logger.warning(
                            f"store-remediation: sweep failed for "
                            f"{thing_name}: {e}")
        except Exception as e:
            logger.warning(
                f"store-remediation: sweep failed for use case "
                f"{usecase.get('usecase_id')}: {e}")

    if actions:
        logger.info(f"store-remediation: actions taken: {actions}")
    return {'actions': actions}


def list_public_components(greengrass_client):
    """List AWS public Greengrass components"""
    try:
        components = []
        next_token = None
        
        while True:
            params = {'scope': 'PUBLIC', 'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_components(**params)
            
            for comp in response.get('components', []):
                components.append({
                    'component_name': comp.get('componentName'),
                    'arn': comp.get('arn'),
                    'latest_version': comp.get('latestVersion', {}).get('componentVersion'),
                    'scope': 'PUBLIC'
                })
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        return components
    except ClientError as e:
        logger.warning(f"Could not list public components: {e}")
        return []


# ===========================================================================
# Workflow_Component deployments (Workflow Manager)
#
# Deployment_Service extension (design section 7): adds the workflow
# component type with device/thing-group targeting within the Use_Case
# (Requirement 8.1), records workflow version -> deployment -> devices
# associations in the Deployments table with component_type: workflow
# (Requirement 8.2), surfaces per-device Greengrass deployment status for
# the workflow page (Requirement 8.3), performs the pre-submit LocalServer
# compatibility check (Requirement 8.4), relies on Greengrass revision
# semantics for version replacement (Requirement 8.5), and writes audit
# log entries for deploy operations (Requirement 11.5).
#
# The association record shape matches what workflows.py's delete flow
# scans for: component_type == 'workflow' plus workflow_id (Requirement 5.6).
# ===========================================================================


def _workflow_error(status_code, code, message, details=None):
    """Workflow Manager error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def _decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Plugin lifecycle and architecture gates (custom-node-designer)
#
# Both gates extend the pre-submit pass that already inspects each target
# device before submission (check_local_server_compatibility): the
# lifecycle gate evaluates the Lifecycle_State of every depended-on
# Plugin_Component over the dependency closure (9.7, 9.8, 9.11, 16.3),
# and the architecture gate checks each target device's recorded
# Target_Architecture against the platform manifests of every depended-on
# Plugin_Component version (16.6). Greengrass dependency resolution
# delivers the depended-on Plugin_Component versions with workflow
# deployments (16.5), so the gates are the only deployment-side change.
#
# evaluate_plugin_lifecycle_gate and evaluate_plugin_arch_gate are pure
# over plain dicts so the gate decision logic is property-testable
# without AWS (tasks 10.6 / 10.7).
# ---------------------------------------------------------------------------


def evaluate_plugin_lifecycle_gate(closure_states, device_flags):
    """
    Lifecycle gate over the dependency closure (9.7, 9.8, 9.11, 16.3).

    ``closure_states``: {plugin component identifier: Lifecycle_State}
    ``device_flags``: {device thing name: bool test_device flag}

    Returns [] when the deployment may be submitted, otherwise the
    complete list of violations, each identifying the Plugin_Component
    and its Lifecycle_State plus the offending target devices:

    - dev state (or unknown — fail closed) is rejected for any target;
    - test state is permitted only to devices flagged ``test_device``;
    - prod deploys anywhere in the Use_Case.
    """
    violations = []
    devices = sorted(device_flags)
    for component in sorted(closure_states):
        state = closure_states[component]
        if state == LIFECYCLE_PROD:
            continue
        if state == LIFECYCLE_TEST:
            offending = [d for d in devices if not device_flags[d]]
            if offending:
                violations.append({
                    'pluginComponent': component,
                    'lifecycleState': state,
                    'devices': offending,
                })
        else:
            # dev, or an unresolvable/unknown state: fail closed for
            # every target device.
            violations.append({
                'pluginComponent': component,
                'lifecycleState': state,
                'devices': devices,
            })
    return violations


def evaluate_plugin_arch_gate(component_manifests, device_archs):
    """
    Architecture gate (16.6): each target device's recorded
    Target_Architecture must appear in the platform manifests of every
    depended-on Plugin_Component version.

    ``component_manifests``: {plugin component name:
        {'version': component version, 'architectures': [archs]}}
    ``device_archs``: {device thing name: recorded Target_Architecture
        or None when the device has none recorded}

    Architectures are matched by exact name — ``x86_64`` and
    ``x86_64_nvidia`` are distinct with no fallback in either direction.
    A device with no recorded Target_Architecture fails closed.

    Returns [] when every device is covered, otherwise one offending
    entry {pluginComponent, version, device, deviceArch} per
    (component, device) miss.
    """
    offending = []
    for name in sorted(component_manifests):
        manifest = component_manifests[name]
        supported = set(manifest.get('architectures') or [])
        for device in sorted(device_archs):
            device_arch = device_archs[device]
            if device_arch not in supported:
                offending.append({
                    'pluginComponent': name,
                    'version': manifest.get('version'),
                    'device': device,
                    'deviceArch': device_arch,
                })
    return offending


VLLM_GATE_REASON_JP4 = 'JP4_UNSUPPORTED'
VLLM_GATE_REASON_ARCH = 'ARCH_UNSUPPORTED'
VLLM_JP4_UNSUPPORTED_MESSAGE = 'JetPack 4 does not support vLLM inference'


def evaluate_vllm_arch_gate(component_manifests, device_archs):
    """
    vLLM architecture gate (3.3-3.7): each target device's recorded
    Target_Architecture must appear in the supported set of every
    vLLM-bearing component. Structural twin of
    ``evaluate_plugin_arch_gate``; pure.

    ``component_manifests``: {vllm component name:
        {'version': component version, 'architectures': [archs]}}
    ``device_archs``: {device thing name: recorded Target_Architecture
        or None when the device has none recorded}

    Architectures are matched by exact name with no cross-architecture
    fallback (3.3); a device with no recorded Target_Architecture fails
    closed (3.6).

    Returns [] when every device is covered (3.7), otherwise one entry
    {component, version, device, deviceArch, supported, reason} per
    (component, device) miss (3.4). ``reason`` is ``'JP4_UNSUPPORTED'``
    ("JetPack 4 does not support vLLM inference") when the device's
    architecture is ``arm64_jp4`` (3.5), else ``'ARCH_UNSUPPORTED'``.
    """
    offending = []
    for name in sorted(component_manifests):
        manifest = component_manifests[name]
        supported = set(manifest.get('architectures') or [])
        for device in sorted(device_archs):
            device_arch = device_archs[device]
            if device_arch not in supported:
                offending.append({
                    'component': name,
                    'version': manifest.get('version'),
                    'device': device,
                    'deviceArch': device_arch,
                    'supported': sorted(supported),
                    'reason': (VLLM_GATE_REASON_JP4
                               if device_arch == 'arm64_jp4'
                               else VLLM_GATE_REASON_ARCH),
                })
    return offending


def parse_plugin_component_ref(component_name, component_version):
    """(plugin_id, Plugin_Record version) of a dda.plugin.* component
    reference. The record version is the leading integer of the component
    version ({pluginVersion}.0.0); None when it cannot be derived."""
    if not str(component_name).startswith(PLUGIN_COMPONENT_PREFIX):
        return None, None
    plugin_id = str(component_name)[len(PLUGIN_COMPONENT_PREFIX):]
    try:
        record_version = int(str(component_version).split('.')[0])
    except (TypeError, ValueError):
        return plugin_id, None
    return plugin_id, record_version


def load_plugin_record(plugin_id, record_version):
    """The backing Plugin_Record of one Plugin_Component version, or None
    (gates fail closed on unresolvable records)."""
    if not PLUGIN_RECORDS_TABLE or not plugin_id or record_version is None:
        return None
    try:
        response = dynamodb.Table(PLUGIN_RECORDS_TABLE).get_item(
            Key={'plugin_id': plugin_id, 'version': record_version})
    except ClientError as e:
        logger.warning(
            f"Could not read plugin record {plugin_id} v{record_version}: {e}")
        return None
    item = response.get('Item')
    return _decimal_to_native(item) if item else None


def plugin_component_architectures(record):
    """The Target_Architectures a Plugin_Component version's platform
    manifests cover. The component pointer's ``architectures`` list is
    written by plugin_components.py as exactly the successfully built
    architectures the recipe carries manifests for (16.1); fall back to
    the per-arch artifact entries when the pointer is absent."""
    if not record:
        return []
    component = record.get('component') or {}
    architectures = component.get('architectures')
    if architectures:
        return [str(arch) for arch in architectures]
    artifacts = record.get('artifacts') or {}
    return sorted(arch for arch, entry in artifacts.items()
                  if (entry or {}).get('buildStatus') == 'succeeded')


def load_device_gate_info(thing_names):
    """The Devices-table gate attributes of each target device: the
    UseCaseAdmin-set ``test_device`` flag (9.8) and the recorded
    ``target_architecture`` (16.6). Devices without a record fail closed
    (not a Test_Device; no recorded architecture)."""
    device_flags = {}
    device_archs = {}
    table = dynamodb.Table(DEVICES_TABLE) if DEVICES_TABLE else None
    for thing_name in thing_names:
        item = None
        if table is not None:
            try:
                item = table.get_item(Key={'device_id': thing_name}).get('Item')
            except ClientError as e:
                logger.warning(
                    f"Could not read device record for {thing_name}: {e}")
        item = item or {}
        device_flags[thing_name] = bool(item.get('test_device'))
        device_archs[thing_name] = item.get('target_architecture') or None
    return device_flags, device_archs


def check_plugin_deployment_gates(plugin_components, thing_names):
    """
    Pre-submit Plugin_Component gates over a deployment's dependency
    closure (9.7, 9.8, 9.11, 16.3, 16.6). ``plugin_components`` maps each
    depended-on Plugin_Component name to its component version — a
    Workflow_Component's recorded closure, or the dda.plugin.* components
    of a standalone deployment. Returns an error response when the
    deployment must be rejected, otherwise None.
    """
    if not plugin_components:
        return None

    device_flags, device_archs = load_device_gate_info(thing_names)

    closure = {}
    for component_name in sorted(plugin_components):
        component_version = plugin_components[component_name]
        plugin_id, record_version = parse_plugin_component_ref(
            component_name, component_version)
        record = load_plugin_record(plugin_id, record_version)
        closure[component_name] = {
            'version': str(component_version),
            'plugin_id': plugin_id,
            'lifecycle_state': (record or {}).get('lifecycle_state'),
            'architectures': plugin_component_architectures(record),
        }

    # Lifecycle gate over the closure (9.7, 9.8, 9.11, 16.3)
    closure_states = {name: info['lifecycle_state']
                      for name, info in closure.items()}
    violations = evaluate_plugin_lifecycle_gate(closure_states, device_flags)
    if violations:
        detailed = [dict(v,
                         version=closure[v['pluginComponent']]['version'],
                         plugin_id=closure[v['pluginComponent']]['plugin_id'])
                    for v in violations]
        return _workflow_error(
            409, 'PLUGIN_LIFECYCLE_VIOLATION',
            'One or more depended-on plugin components are not deployable '
            'to the requested target devices in their current lifecycle '
            'state; the deployment was not submitted',
            {'violations': detailed})

    # Architecture gate: recorded device Target_Architecture vs the
    # platform manifests of every depended-on Plugin_Component (16.6)
    component_manifests = {
        name: {'version': info['version'],
               'architectures': info['architectures']}
        for name, info in closure.items()
    }
    unsupported = evaluate_plugin_arch_gate(component_manifests, device_archs)
    if unsupported:
        return _workflow_error(
            409, 'PLUGIN_ARCH_UNSUPPORTED',
            'One or more target devices have no published Plugin_Artifact '
            'for their recorded Target_Architecture in a depended-on '
            'plugin component version; the deployment was not submitted',
            {'unsupported': unsupported})

    return None


def split_vllm_component_name(component_name):
    """Split a vLLM component name into its Base_Component_Name and the
    Target_Architecture its per-JetPack target suffix names, or
    ``(name, None)`` when the name carries no known suffix (a legacy
    unsuffixed component). Pure: the suffix vocabulary is closed
    (``VLLM_TARGET_SUFFIX_TO_ARCH``), so an arbitrary model name that
    merely happens to end in a `-jetson-…` fragment cannot be misread as
    a target suffix."""
    name = str(component_name or '')
    for suffix, arch in VLLM_TARGET_SUFFIX_TO_ARCH.items():
        marker = f"-{suffix}"
        if name.endswith(marker) and len(name) > len(marker):
            return name[:-len(marker)], arch
    return name, None


def _query_vllm_record_by_component_name(component_name):
    """One GSI query on ``component_name-index`` for the exact name, or
    None. Unresolvable reads warn and fail closed."""
    try:
        response = dynamodb.Table(TRAINING_JOBS_TABLE).query(
            IndexName=TRAINING_JOBS_COMPONENT_NAME_INDEX,
            KeyConditionExpression=Key('component_name').eq(
                str(component_name)))
    except ClientError as e:
        logger.warning(
            f"Could not read vLLM model record for {component_name}: {e}")
        return None
    items = response.get('Items') or []
    if not items:
        return None
    # Repeat publishes of one record keep one component name; should
    # several records ever share it, the newest wins.
    items.sort(key=lambda item: item.get('created_at') or 0, reverse=True)
    return _decimal_to_native(items[0])


def load_vllm_model_record(component_name):
    """The backing vLLM_Model_Record of one model-vllm-* component, via
    the component_name-index GSI on the training-jobs table, or None —
    the gate fails closed on unresolvable records, like
    ``load_plugin_record``.

    The record's top-level ``component_name`` stays the unsuffixed
    Base_Component_Name, so a Per_JetPack_Component name
    (``{base}-jetson-xavier-jp7``) resolves in two steps: the exact name
    FIRST (the legacy path — one query, byte-identical for unsuffixed
    names), and only on a miss AND a known target suffix a second query
    with the stripped base name."""
    if not TRAINING_JOBS_TABLE or not component_name:
        return None
    record = _query_vllm_record_by_component_name(component_name)
    if record is not None:
        return record
    base_name, arch = split_vllm_component_name(component_name)
    if arch is None or base_name == str(component_name):
        return None
    return _query_vllm_record_by_component_name(base_name)


def vllm_component_architectures(record, component_name=None):
    """The supported Target_Architectures of one published vLLM component.

    ``component_name`` is optional and keyword-compatible with the legacy
    single-argument callers. Resolution order (design step 11):

    1. a ``published_component.components`` entry whose ``component_name``
       matches → that entry's ``supported_architectures`` (the per-JetPack
       write-back greengrass_publish.py produced);
    2. elif the name carries a known target suffix → ``[arch]`` when that
       arch is in the record-wide set, else ``[]`` (fail closed on an
       out-of-set suffix);
    3. else (no name given, or an unsuffixed legacy name) → the
       record-wide ``published_component.supported_architectures`` /
       ``record.supported_architectures``, exactly as before.

    Empty when unresolvable, so the gate fails closed for every device."""
    if not record:
        return []
    published = record.get('published_component') or {}
    record_wide = [str(arch) for arch in
                   (published.get('supported_architectures')
                    or record.get('supported_architectures') or [])]

    if component_name:
        name = str(component_name)
        for entry in published.get('components') or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get('component_name') or '') == name:
                return [str(arch) for arch in
                        entry.get('supported_architectures') or []]
        _, arch = split_vllm_component_name(name)
        if arch is not None:
            return [arch] if arch in record_wide else []

    return record_wide


def collect_vllm_component_manifests(components):
    """The vLLM architecture-gate manifests of a requested component set
    (``{component name: component version}``), activating the gate iff
    the set contains a model-vllm-* component or a workflow component
    whose version item records ``has_llm_inference`` (8.5, 8.6):

    - model-vllm-* components: the supported set comes from the backing
      record via the component_name-index GSI; unresolvable records fail
      closed with an empty set;
    - workflow components (dda.workflow.*): the supported set is the
      ``packaged_architectures`` the Component_Packager recorded on the
      version item; versions without ``has_llm_inference`` contribute
      nothing.

    Deployments containing neither contribute zero findings, so
    pre-feature validation applies verbatim — jp4 included (8.5)."""
    manifests = {}
    for name in sorted(components or {}):
        version = components[name]
        name = str(name)
        if name.startswith(VLLM_MODEL_COMPONENT_PREFIX):
            record = load_vllm_model_record(name)
            manifests[name] = {
                'version': str(version) if version else None,
                # Per-component resolution: a Per_JetPack_Component gates
                # on its OWN architecture, a legacy unsuffixed name on the
                # record-wide set (2.12).
                'architectures': vllm_component_architectures(record, name),
            }
        elif name.startswith(WORKFLOW_COMPONENT_PREFIX):
            workflow_id = name[len(WORKFLOW_COMPONENT_PREFIX):]
            if not workflow_id:
                continue
            # D2 scan-first resolution: bumped-major entries (re-packaged
            # versions) resolve their true version item, so LLM-bearing
            # workflows keep activating the gate; unrecorded entries ride
            # the legacy major parse and failures degrade to no item.
            _, version_item = _resolve_workflow_version_item(
                workflow_id, version)
            if not version_item or not version_item.get('has_llm_inference'):
                continue
            manifests[name] = {
                'version': str(version),
                'architectures': [
                    str(arch) for arch in
                    version_item.get('packaged_architectures') or []],
            }
    return manifests


def check_vllm_deployment_gate(component_manifests, thing_names):
    """
    Pre-submit vLLM architecture gate (3.3, 3.4, 3.7): every target
    device's recorded Target_Architecture must appear in the supported
    set of every vLLM-bearing component manifest. Returns an error
    response when the deployment must be rejected (the complete
    offending list; nothing is submitted), otherwise None.
    """
    if not component_manifests:
        return None
    _, device_archs = load_device_gate_info(thing_names)
    unsupported = evaluate_vllm_arch_gate(component_manifests, device_archs)
    if unsupported:
        return _workflow_error(
            409, 'VLLM_ARCH_UNSUPPORTED',
            'One or more target devices have a Target_Architecture outside '
            'the supported set of a vLLM model component or an LLM-bearing '
            'workflow component; the deployment was not submitted',
            {'unsupported': unsupported})
    return None


def record_plugin_deployment(deployment_id, usecase_id, plugin_components,
                             target_arn, target_devices, target_thing_group,
                             is_revision, superseded_deployment_id, user):
    """
    Record a standalone Plugin_Component deployment in the Deployments
    table with component_type: 'plugin' (task 10.5). Mirrors the
    workflow association record shape.
    """
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    names = sorted(plugin_components)
    item = {
        'deployment_id': deployment_id,
        'usecase_id': usecase_id,
        'component_type': 'plugin',
        'plugin_components': {name: str(plugin_components[name])
                              for name in names},
        'component_name': names[0],
        'component_version': str(plugin_components[names[0]]),
        'target_arn': target_arn,
        'target_devices': list(target_devices),
        'target_thing_group': target_thing_group or None,
        'status': 'IN_PROGRESS',
        'deployment_status': 'IN_PROGRESS',
        'is_revision': is_revision,
        'superseded_deployment_id': superseded_deployment_id,
        'created_by': user['user_id'],
        'created_at': timestamp,
        'updated_at': timestamp
    }
    dynamodb.Table(DEPLOYMENTS_TABLE).put_item(Item=item)
    return item


def has_workflow_permission(user, usecase_id, permission):
    """Check a workflow permission for the acting user on a Use_Case"""
    if is_super_user(user['user_id']):
        return True
    return rbac_manager.has_permission(user['user_id'], usecase_id, permission,
                                       user_info=user)


def get_workflow_metadata(workflow_id):
    """Fetch a Workflows-table metadata item, or None"""
    if not WORKFLOWS_TABLE:
        return None
    try:
        response = dynamodb.Table(WORKFLOWS_TABLE).get_item(
            Key={'workflow_id': workflow_id})
    except ClientError as e:
        logger.error(f"Error reading workflow {workflow_id}: {str(e)}")
        return None
    item = response.get('Item')
    return _decimal_to_native(item) if item else None


def workflow_component_name(workflow_id):
    """Greengrass component name assigned by the Component_Packager"""
    return f"{WORKFLOW_COMPONENT_PREFIX}{workflow_id}"


def workflow_component_version(workflow_version):
    """Component version derived from the workflow version"""
    return f"{int(workflow_version)}.0.0"


def resolve_workflow_component_version(version_item, workflow_version):
    """The REGISTERED component version of a packaged workflow version
    (workflow-deploy-component-version design D1, forward resolution).

    Re-packaging deliberately bumps the component MAJOR past the workflow
    version (workflow_packaging.next_component_version — Greengrass
    component versions are immutable), so the legacy
    ``{workflow_version}.0.0`` derivation goes stale after a re-package.
    Resolution order:

    1. the discrete ``component_version`` field the Component_Packager
       records at packaging time (the declared contract);
    2. the ``component_arn`` suffix after the last ``:versions:`` —
       authoritative for every version ever packaged, past and future;
    3. ``workflow_component_version(workflow_version)`` as the last
       resort (present-but-unparseable arn), keeping the function total.

    For first packages the arn suffix equals ``{workflow_version}.0.0``,
    so resolution and the legacy derivation agree by construction.
    """
    version_item = version_item or {}
    recorded = version_item.get('component_version')
    if isinstance(recorded, str) and recorded:
        return recorded
    arn = version_item.get('component_arn')
    if isinstance(arn, str) and ':versions:' in arn:
        suffix = arn.rsplit(':versions:', 1)[-1]
        if re.match(r'^\d+\.\d+\.\d+$', suffix):
            return suffix
    return workflow_component_version(workflow_version)


def _resolve_workflow_version_item(workflow_id, entry_component_version):
    """Resolve a deployment entry's ``componentVersion`` to
    ``(workflow_version, version_item_or_None)`` — design D2, scan-first
    reverse resolution — or ``(None, None)`` when nothing resolves.

    Scan-first: the version item whose RECORDED component version
    (discrete ``component_version`` field or ``component_arn``
    ``:versions:`` suffix) equals the entry's version is the
    authoritative match — unambiguous because component majors strictly
    increase per re-package, so at most one item of a workflow records a
    given component version. When nothing matches (items packaged before
    the discrete field existed, unrecorded items, and test fakes of
    ``get_version_item``), fall back to today's major parse routed
    through ``workflow_guards.get_version_item``. Table-read failures
    are logged and degrade to the fallback (and the fallback's, to a
    ``None`` item), so resolution never raises — mirroring the
    consumers' resilience shape.
    """
    entry_component_version = str(entry_component_version or '')
    try:
        item = workflow_guards.find_version_item_by_component_version(
            workflow_id, entry_component_version)
    except Exception as e:  # noqa: BLE001 — degrade to the major parse
        logger.warning(
            f"Could not scan version items of workflow {workflow_id} for "
            f"component version {entry_component_version}: {e}")
        item = None
    if item is not None:
        try:
            return int(item.get('version')), item
        except (TypeError, ValueError):
            pass  # unusable recorded workflow version; ride the fallback
    major = entry_component_version.split('.')[0]
    if not major.isdigit():
        return None, None
    try:
        return int(major), workflow_guards.get_version_item(
            workflow_id, int(major))
    except Exception as e:  # noqa: BLE001 — resolution never raises
        logger.warning(
            f"Could not read workflow version item {major} of workflow "
            f"{workflow_id}: {e}")
        return int(major), None


def collect_workflow_subscribed_topics(components_map):
    """``{workflow_id: [topic filters]}`` for the workflow components
    (dda.workflow.*) in a deployment component set whose version item
    records non-empty ``subscribed_topics`` (trigger-activation-runtime
    10.2). The Component_Packager records the sorted, de-duplicated
    greengrass-path ``mqtt_subscribe`` topic filters on the version item
    at packaging time; versions without the field (all pre-feature
    packages and every non-subscribing workflow) contribute nothing, so
    deployments without a subscribing workflow are untouched. Entries
    resolve to their version item by D2 scan-first resolution (the
    registered component version survives bumped majors from
    re-packaging), with the legacy major parse as fallback; resolution
    failures degrade to no item, never raise."""
    topics_by_workflow = {}
    for name in sorted(components_map or {}):
        if not name.startswith(WORKFLOW_COMPONENT_PREFIX):
            continue
        workflow_id = name[len(WORKFLOW_COMPONENT_PREFIX):]
        version = (components_map[name] or {}).get('componentVersion')
        if not workflow_id:
            continue
        _, version_item = _resolve_workflow_version_item(workflow_id, version)
        topics = [str(topic) for topic in
                  (version_item or {}).get('subscribed_topics') or []
                  if str(topic)]
        if topics:
            topics_by_workflow[workflow_id] = topics
    return topics_by_workflow


def apply_subscribe_access_control(components_map):
    """Deep-merge the ``aws.greengrass#SubscribeToIoTCore`` accessControl
    policies for subscribing workflow components into the LocalServer
    entry's ``configurationUpdate.merge`` (trigger-activation-runtime
    10.2, 10.3, design D5).

    The IPC subscribe caller is the LocalServer process, so the
    authorization lands on the LocalServer component: one uniquely-keyed
    ``dda:workflow-subscribe:<workflowId>`` policy per subscribing
    workflow, scoped to exactly that workflow's recorded topic filters.
    Greengrass deep-merges configuration maps by key, so the recipe's
    default accessControl policies stay untouched and multiple
    subscribing workflows coexist under their own keys. Any existing
    ``configurationUpdate.merge`` on the LocalServer entry is merged
    into non-destructively.

    Returns the list of actionable warnings: one per subscribing
    workflow when the LocalServer component is absent from the set (the
    merge has nowhere to attach; Requirement 10.3). Empty — and
    ``components_map`` untouched — when no workflow in the set records
    subscribed topics (Requirement 10.4 byte-identity)."""
    topics_by_workflow = collect_workflow_subscribed_topics(components_map)
    if not topics_by_workflow:
        return []

    local_server_name = next(
        (name for name in sorted(components_map)
         if name.startswith(LOCAL_SERVER_COMPONENT_PREFIX)), None)
    if not local_server_name:
        return [
            (f"Workflow {workflow_id} subscribes to MQTT topics "
             f"[{', '.join(topics)}] via Greengrass, but this deployment "
             "does not include the LocalServer component, so the "
             "aws.greengrass#SubscribeToIoTCore authorization could not be "
             "attached. Deploy the LocalServer component together with this "
             "workflow so the subscribe authorization can attach; until "
             "then the trigger's subscription will be denied on the device.")
            for workflow_id, topics in sorted(topics_by_workflow.items())
        ]

    entry = components_map[local_server_name]
    config_update = entry.setdefault('configurationUpdate', {})
    existing_merge = config_update.get('merge')
    merge_doc = {}
    if existing_merge:
        try:
            merge_doc = json.loads(existing_merge)
        except (TypeError, ValueError) as e:
            # Never clobber a caller-supplied merge we cannot parse.
            logger.warning(
                f"Could not parse existing configurationUpdate.merge on "
                f"{local_server_name}; skipping subscribe accessControl "
                f"merge: {e}")
            return [
                (f"Workflow {workflow_id} subscribes to MQTT topics "
                 f"[{', '.join(topics)}] via Greengrass, but the LocalServer "
                 "component's existing configurationUpdate could not be "
                 "parsed, so the aws.greengrass#SubscribeToIoTCore "
                 "authorization was not attached.")
                for workflow_id, topics in sorted(topics_by_workflow.items())
            ]
    if not isinstance(merge_doc, dict):
        merge_doc = {}
    policies = merge_doc.setdefault('accessControl', {}).setdefault(
        'aws.greengrass.ipc.mqttproxy', {})
    for workflow_id, topics in sorted(topics_by_workflow.items()):
        policies[f'dda:workflow-subscribe:{workflow_id}'] = {
            'policyDescription': 'Workflow mqtt_subscribe trigger topics',
            'operations': ['aws.greengrass#SubscribeToIoTCore'],
            'resources': list(topics),
        }
        logger.info(
            f"Merged SubscribeToIoTCore accessControl for workflow "
            f"{workflow_id} ({len(topics)} topic filter(s)) into "
            f"{local_server_name}")
    config_update['merge'] = json.dumps(merge_doc)
    return []


def resolve_target_thing_names(iot_client, target_devices, target_thing_group):
    """
    The individual device thing names a deployment targets: the explicit
    device list, or the current members of the targeted thing group.
    """
    if target_devices:
        return list(target_devices)
    thing_names = []
    if target_thing_group:
        try:
            paginator = iot_client.get_paginator('list_things_in_thing_group')
            for page in paginator.paginate(thingGroupName=target_thing_group,
                                           maxResults=100):
                thing_names.extend(page.get('things', []))
        except ClientError as e:
            logger.warning(
                f"Could not list things in group {target_thing_group}: {e}")
    return thing_names


def local_server_component_arch(component_name):
    """The workflow_core arch id for a LocalServer component name, or None.

    LocalServer components are named ``aws.edgeml.dda.LocalServer.<suffix>``
    where the suffix identifies the variant lineage:
    ``arm64JP7`` -> ``arm64_jp7``,
    ``arm64JP6`` -> ``arm64_jp6``, ``arm64JP5`` -> ``arm64_jp5``,
    ``arm64JP4`` -> ``arm64_jp4`` (explicit JetPack 4), and the legacy
    ``arm64``/``aarch64`` -> ``arm64_jp4`` (the bare pre-rename JetPack 4
    name, still recognized on read for already-provisioned JP4 devices),
    ``amd64``/``x86_64`` -> ``x86_64``. Used to pick the per-arch minimum so
    a device is gated against its own variant lineage.
    """
    if not component_name or not component_name.startswith(
            LOCAL_SERVER_COMPONENT_PREFIX):
        return None
    suffix = component_name[len(LOCAL_SERVER_COMPONENT_PREFIX):].lstrip('.').lower()
    # Match the longer JetPack-tagged tokens BEFORE the bare ``arm64`` prefix:
    # "arm64jp4"/"arm64jp5"/"arm64jp6"/"arm64jp7" all start with "arm64", so
    # an explicit arm64JP4 must not be misclassified as the legacy bare arm64.
    if 'jp7' in suffix:
        return 'arm64_jp7'
    if 'jp6' in suffix:
        return 'arm64_jp6'
    if 'jp5' in suffix:
        return 'arm64_jp5'
    if 'jp4' in suffix:
        return 'arm64_jp4'
    if 'amd64' in suffix or 'x86' in suffix:
        return 'x86_64'
    # Legacy bare JetPack 4 names (retired on the write side, recognized here).
    if 'arm64' in suffix or 'aarch64' in suffix:
        return 'arm64_jp4'
    return None


def get_device_local_server(greengrass_client, thing_name):
    """(component_name, version) of the installed LocalServer root component
    on a core device (named aws.edgeml.dda.LocalServer.<arch>), or
    (None, None) when none is reported installed. The component name suffix
    identifies the arch variant (see local_server_component_arch).

    Raises ClientError when the installed components cannot be listed, so
    the caller can distinguish "no LocalServer" from "could not determine".
    """
    paginator = greengrass_client.get_paginator('list_installed_components')
    for page in paginator.paginate(coreDeviceThingName=thing_name):
        for comp in page.get('installedComponents', []):
            name = comp.get('componentName', '')
            if name.startswith(LOCAL_SERVER_COMPONENT_PREFIX):
                return name, comp.get('componentVersion')
    return None, None


def get_device_local_server_version(greengrass_client, thing_name):
    """The LocalServer component version installed on a core device, or None.
    Thin wrapper over get_device_local_server for callers that only need the
    version."""
    return get_device_local_server(greengrass_client, thing_name)[1]


def check_local_server_compatibility(greengrass_client, thing_names,
                                     min_local_server_version,
                                     min_versions_by_arch=None):
    """
    Pre-submit compatibility check (Requirement 8.4): compare each target
    device's installed LocalServer component version against the
    Workflow_Component's minLocalServerVersion. Returns the list of
    incompatible devices, each with a clear reason; an empty list means
    every checked device is compatible.

    ``min_versions_by_arch`` ({workflow_core arch id: version}) makes the
    check variant-aware: LocalServer variants version on independent,
    non-comparable lineages, so each device is gated against the minimum
    for its own variant (derived from the installed LocalServer component
    name). ``min_local_server_version`` is the fallback for archs not in
    the map (and for devices whose variant cannot be determined), and also
    takes precedence for every device when a per-version override is in
    effect (the caller passes an empty map in that case).
    """
    incompatible = []
    by_arch = min_versions_by_arch or {}
    for thing_name in thing_names:
        try:
            comp_name, installed = get_device_local_server(
                greengrass_client, thing_name)
        except ClientError as e:
            logger.warning(
                f"Could not read installed components for {thing_name}: {e}")
            incompatible.append({
                'device': thing_name,
                'local_server_version': None,
                'min_local_server_version': min_local_server_version,
                'reason': ('The installed LocalServer version could not be '
                           'determined for this device')
            })
            continue
        arch = local_server_component_arch(comp_name)
        effective_min = by_arch.get(arch, min_local_server_version)
        if installed is None:
            incompatible.append({
                'device': thing_name,
                'local_server_version': None,
                'min_local_server_version': effective_min,
                'reason': 'No LocalServer component is installed on this device'
            })
        elif _version_key(installed) < _version_key(effective_min):
            incompatible.append({
                'device': thing_name,
                'local_server_version': installed,
                'min_local_server_version': effective_min,
                'reason': (f'Installed LocalServer version {installed} is older '
                           f'than the required minimum {effective_min}')
            })
    return incompatible


def record_workflow_deployment(deployment_id, usecase_id, workflow_id,
                               workflow_version, target_arn, target_devices,
                               target_thing_group, is_revision,
                               superseded_deployment_id, user,
                               camera_bindings=None, component_version=None):
    """
    Record the workflow version -> deployment -> devices association in the
    Deployments table (Requirement 8.2). component_type: 'workflow' plus
    workflow_id is the shape workflows.py's delete flow matches on (5.6).
    Delivered Camera_Bindings are stored on the record for display and
    audit (camera-registry-sync Requirements 8.2, 12.3).

    ``component_version`` is the REGISTERED component version the caller
    resolved (resolve_workflow_component_version) and actually deployed;
    when omitted the legacy ``{workflow_version}.0.0`` derivation is
    recorded, keeping the signature safe for any other caller.
    """
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    item = {
        'deployment_id': deployment_id,
        'usecase_id': usecase_id,
        'component_type': 'workflow',
        'workflow_id': workflow_id,
        'workflow_version': int(workflow_version),
        'component_name': workflow_component_name(workflow_id),
        'component_version': (component_version
                              or workflow_component_version(workflow_version)),
        'target_arn': target_arn,
        'target_devices': target_devices,
        'target_thing_group': target_thing_group or None,
        # 'status' feeds the status-index GSI; 'deployment_status' is what
        # workflows.py's active-deployment check reads.
        'status': 'IN_PROGRESS',
        'deployment_status': 'IN_PROGRESS',
        'is_revision': is_revision,
        'superseded_deployment_id': superseded_deployment_id,
        'created_by': user['user_id'],
        'created_at': timestamp,
        'updated_at': timestamp
    }
    if camera_bindings is not None:
        item['camera_bindings'] = _dynamo_safe(camera_bindings)
    dynamodb.Table(DEPLOYMENTS_TABLE).put_item(Item=item)
    return item


# ---------------------------------------------------------------------------
# Deploy-time Camera_Binding validation (camera-registry-sync
# Requirements 8.3, 8.4, 8.7, 8.8, 8.9, 9.1-9.5, 11.1)
#
# validate_camera_bindings is a pure function over plain dicts (like the
# plugin gates above) so the binding decision logic is property-testable
# without AWS. The workflow version item's packager-recorded discriminator
# separates the two regimes:
#
# - has_binding_points true: every Camera_Input_Node needs a Camera_Binding
#   (registered source or manual override) per target device — errors
#   reject the deployment (8.7, 9.1, 9.2, 9.4, 8.4); degraded sources and
#   never-synced targets produce warnings that must be confirmed (9.3, 8.8).
# - has_binding_points false (legacy versions): the compiled-in device
#   paths are compared against each target's registry and unmatched paths
#   produce warnings only, never errors (9.5, 11.1).
#
# Workflow versions with no Camera_Input_Nodes produce no errors and no
# warnings (8.9).
# ---------------------------------------------------------------------------

# Error codes (deployment rejected)
CAMERA_ERROR_UNBOUND = 'CAMERA_NODE_UNBOUND'                # 8.7
CAMERA_ERROR_SOURCE_MISSING = 'CAMERA_SOURCE_MISSING'       # 9.1, 9.2
CAMERA_ERROR_TYPE_INCOMPATIBLE = 'CAMERA_TYPE_INCOMPATIBLE' # 9.4
CAMERA_ERROR_OVERRIDE_INVALID = 'CAMERA_OVERRIDE_INVALID'   # 8.4

# Warning codes (require a matching confirmed_warnings id to submit)
CAMERA_WARNING_SOURCE_DEGRADED = 'CAMERA_SOURCE_DEGRADED'   # 9.3
CAMERA_WARNING_NEVER_SYNCED = 'DEVICE_NEVER_SYNCED'         # 8.8
CAMERA_WARNING_LEGACY_PATH = 'COMPILED_PATH_UNREGISTERED'   # 9.5

#: Camera_Source types (registry ``type`` attribute) compatible with each
#: built-in Camera_Input_Node type. icam_source captures a V4L2 smart
#: camera directly (v4l2src device=…), so it binds to an ICam, a
#: discovered V4L2 device, or a configured Camera-type Image_Source
#: (csi-icam-input-nodes Requirement 6.1). csi_camera_source captures an
#: NVIDIA CSI camera through the host capture service, so it binds to an
#: NvidiaCSI source or a configured Camera-type Image_Source flagged CSI
#: (Requirement 6.2). aravis_camera_source must bind to an Aravis-backed
#: source: a discovered bus camera or a configured Camera-type
#: Image_Source (aravis-camera-input Requirement 5.2).
_CAMERA_COMPATIBLE_SOURCE_TYPES = {
    'icam_source': frozenset({'ICam', 'V4L2Discovered', 'Camera'}),
    'csi_camera_source': frozenset({'NvidiaCSI', 'Camera'}),
    'aravis_camera_source': frozenset({'Camera', 'AravisDiscovered'}),
}

#: Camera_Source types that are never a camera. Custom camera-backed node
#: types declare no backing transport, so only the categorically
#: incompatible Folder type (Requirement 9.4's example) is rejected for
#: them; everything else is accepted.
_NEVER_CAMERA_SOURCE_TYPES = frozenset({'Folder'})

#: Registry-entry param keys that may carry the source's device path
#: (the edge report shape writes ``devicePath``; ``device`` is accepted
#: for parity with the node parameter name).
_DEVICE_PATH_PARAM_KEYS = ('devicePath', 'device')


def _registry_device_paths(cameras):
    """Every device path registered in one device's camera map."""
    paths = set()
    for entry in cameras.values():
        params = (entry or {}).get('params') or {}
        for key in _DEVICE_PATH_PARAM_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def _camera_source_type_compatible(node_type, source_type):
    """Whether a Camera_Source type may back a Camera_Input_Node type
    (Requirement 9.4)."""
    compatible = _CAMERA_COMPATIBLE_SOURCE_TYPES.get(node_type)
    if compatible is not None:
        return source_type in compatible
    return source_type not in _NEVER_CAMERA_SOURCE_TYPES


def _degraded_source_conditions(entry):
    """The Requirement 9.3 warning conditions a registry entry is in:
    absent, stale, and/or sync status pending/failed."""
    conditions = []
    if entry.get('absent'):
        conditions.append('absent')
    if entry.get('stale'):
        conditions.append('stale')
    if entry.get('sync_status') in ('pending', 'failed'):
        conditions.append(entry['sync_status'])
    return conditions


def _camera_node_descriptor(node_type, descriptors):
    """The NodeTypeDescriptor of a Camera_Input_Node type: the caller's
    merged-catalog mapping when it covers the type (camera-backed
    Custom_Node_Types), otherwise the built-in workflow_core catalog.
    None when unresolvable — override validation fails closed."""
    if descriptors and node_type in descriptors:
        return descriptors[node_type]
    # Imported lazily: only override validation needs the workflow_core
    # layer; every other deployments.py path stays importable without it.
    from workflow_core.catalog import get_node_type
    return get_node_type(node_type)


def _override_errors(thing_name, node_id, node_type, override, descriptors):
    """Manual-override constraint violations (Requirement 8.4): each
    supplied value is checked against the node type's declared parameter
    constraints with the workflow_core parameter validator. Values are
    validated as supplied — the compiled document keeps rendered defaults
    for parameters an override omits."""
    descriptor = _camera_node_descriptor(node_type, descriptors)
    if descriptor is None:
        # Fail closed, matching the plugin gates' unresolvable-record rule.
        return [{
            'code': CAMERA_ERROR_OVERRIDE_INVALID,
            'device': thing_name,
            'nodeId': node_id,
            'message': (f"Override values for camera input node '{node_id}' "
                        f"on device '{thing_name}' cannot be validated: no "
                        f"parameter declaration is available for node type "
                        f"'{node_type}'"),
        }]
    from workflow_core.validator import check_parameter_value
    parameters = {p.name: p for p in descriptor.parameters}
    errors = []
    for name in sorted(override):
        parameter = parameters.get(name)
        if parameter is None:
            errors.append({
                'code': CAMERA_ERROR_OVERRIDE_INVALID,
                'device': thing_name,
                'nodeId': node_id,
                'parameter': name,
                'message': (f"Override for camera input node '{node_id}' on "
                            f"device '{thing_name}' sets '{name}', which is "
                            f"not a declared parameter of node type "
                            f"'{node_type}'"),
            })
            continue
        violation = check_parameter_value(parameter, override[name])
        if violation is not None:
            errors.append({
                'code': CAMERA_ERROR_OVERRIDE_INVALID,
                'device': thing_name,
                'nodeId': node_id,
                'parameter': name,
                'violation': violation.code,
                'message': (f"Override for camera input node '{node_id}' on "
                            f"device '{thing_name}': {violation.message}"),
            })
    return errors


def _legacy_path_warnings(camera_nodes, thing_name, cameras, confirmed_ids):
    """Requirement 9.5 / 11.1: for a version without binding points, each
    compiled-in device path that matches no registered Camera_Source on
    the target produces a warning — never an error."""
    registered_paths = _registry_device_paths(cameras)
    warnings = []
    for node in camera_nodes:
        node_id = node.get('node_id')
        compiled_paths = node.get('compiled_device_paths') or {}
        unmatched = {}
        for arch in sorted(compiled_paths):
            path = compiled_paths[arch]
            if isinstance(path, str) and path and path not in registered_paths:
                unmatched.setdefault(path, []).append(arch)
        for path in sorted(unmatched):
            warning_id = f'legacy-path:{thing_name}:{node_id}:{path}'
            warnings.append({
                'id': warning_id,
                'code': CAMERA_WARNING_LEGACY_PATH,
                'device': thing_name,
                'nodeId': node_id,
                'path': path,
                'architectures': unmatched[path],
                'confirmed': warning_id in confirmed_ids,
                'message': (f"Compiled-in device path '{path}' of camera "
                            f"input node '{node_id}' matches no camera "
                            f"registered on device '{thing_name}'"),
            })
    return warnings


def validate_camera_bindings(version_item, targets, registry_snapshot,
                             bindings, confirmed, descriptors=None):
    """
    Pre-submit Camera_Binding validation (camera-registry-sync
    Requirements 8.3, 8.4, 8.7, 8.8, 8.9, 9.1-9.5, 11.1). Pure over its
    inputs; returns ``(errors, warnings)``.

    ``version_item``: the workflow version item carrying the
        packager-recorded ``has_binding_points`` flag and
        ``camera_input_nodes`` records ({node_id, node_type,
        binding_hint?, compiled_device_paths: {arch: path}}).
    ``targets``: the target device thing names.
    ``registry_snapshot``: {thing_name: {'never_synced': bool, 'cameras':
        {camera_source_id: registry entry}}} with per-entry ``stale``
        precomputed by the caller against the Staleness_Threshold (the
        function itself is time-free). A device absent from the snapshot
        is treated as never synced with an empty registry (fail-safe).
    ``bindings``: {thing_name: {node_id: {'cameraSourceId': id} |
        {'override': {param: value}}}}. When both keys are present the
        registered-source binding is authoritative.
    ``confirmed``: the submitted ``confirmed_warnings`` ids; each returned
        warning carries ``confirmed`` so the caller accepts the deployment
        only when every warning is confirmed.
    ``descriptors``: optional {node_type: NodeTypeDescriptor} for
        camera-backed Custom_Node_Types (built-ins resolve from the
        workflow_core catalog).

    Errors reject the deployment: unbound Camera_Input_Node on any target
    (8.7); referenced cameraSourceId absent from the target's registry
    (9.1, 9.2, and the never-synced manual-override restriction of 8.8);
    Camera_Source type incompatible with the node type (9.4); override
    values violating declared parameter constraints (8.4). Distinct
    bindings per device for the same node are the natural map shape (8.3).
    """
    camera_nodes = (version_item or {}).get('camera_input_nodes') or []
    if not camera_nodes:
        # No Camera_Input_Nodes: deployment proceeds without bindings (8.9)
        return [], []

    confirmed_ids = set(confirmed or [])
    bindings = bindings or {}
    registry_snapshot = registry_snapshot or {}
    errors = []
    warnings = []

    for thing_name in sorted(targets or []):
        device_snapshot = registry_snapshot.get(thing_name)
        cameras = (device_snapshot or {}).get('cameras') or {}
        if device_snapshot is None:
            never_synced = True
        else:
            never_synced = bool(device_snapshot.get('never_synced'))

        if not version_item.get('has_binding_points'):
            # Legacy regime: compiled-in path comparison, warnings only
            # (9.5, 11.1)
            warnings.extend(_legacy_path_warnings(
                camera_nodes, thing_name, cameras, confirmed_ids))
            continue

        if never_synced and not cameras:
            # 8.8: warn, and permit binding only through manual override
            # (a cameraSourceId binding below fails the existence check
            # against the empty registry).
            warning_id = f'never-synced:{thing_name}'
            warnings.append({
                'id': warning_id,
                'code': CAMERA_WARNING_NEVER_SYNCED,
                'device': thing_name,
                'confirmed': warning_id in confirmed_ids,
                'message': (f"Device '{thing_name}' has never completed a "
                            f"camera registry synchronization; camera "
                            f"bindings are restricted to manual override"),
            })

        device_bindings = bindings.get(thing_name) or {}
        for node in camera_nodes:
            node_id = node.get('node_id')
            node_type = node.get('node_type')
            binding = device_bindings.get(node_id)
            camera_source_id = None
            override = None
            if isinstance(binding, dict):
                if isinstance(binding.get('cameraSourceId'), str) \
                        and binding['cameraSourceId']:
                    camera_source_id = binding['cameraSourceId']
                elif isinstance(binding.get('override'), dict):
                    override = binding['override']

            if camera_source_id is None and override is None:
                # Neither a selected Camera_Source nor a manual override (8.7)
                errors.append({
                    'code': CAMERA_ERROR_UNBOUND,
                    'device': thing_name,
                    'nodeId': node_id,
                    'message': (f"Camera input node '{node_id}' has no camera "
                                f"binding for target device '{thing_name}'"),
                })
                continue

            if override is not None:
                errors.extend(_override_errors(
                    thing_name, node_id, node_type, override, descriptors))
                continue

            entry = cameras.get(camera_source_id)
            if entry is None:
                # Referenced source not in the target's registry (9.1, 9.2)
                errors.append({
                    'code': CAMERA_ERROR_SOURCE_MISSING,
                    'device': thing_name,
                    'nodeId': node_id,
                    'cameraSourceId': camera_source_id,
                    'message': (f"Camera source '{camera_source_id}' bound to "
                                f"node '{node_id}' is not registered on "
                                f"device '{thing_name}'"),
                })
                continue

            source_type = entry.get('type')
            if not _camera_source_type_compatible(node_type, source_type):
                errors.append({
                    'code': CAMERA_ERROR_TYPE_INCOMPATIBLE,
                    'device': thing_name,
                    'nodeId': node_id,
                    'cameraSourceId': camera_source_id,
                    'sourceType': source_type,
                    'nodeType': node_type,
                    'message': (f"Camera source '{camera_source_id}' of type "
                                f"'{source_type}' is not compatible with "
                                f"node '{node_id}' of type '{node_type}' on "
                                f"device '{thing_name}'"),
                })

            conditions = _degraded_source_conditions(entry)
            if conditions:
                # 9.3: degraded source needs explicit confirmation
                warning_id = (f"camera-degraded:{thing_name}:{node_id}:"
                              f"{camera_source_id}:{'+'.join(conditions)}")
                warnings.append({
                    'id': warning_id,
                    'code': CAMERA_WARNING_SOURCE_DEGRADED,
                    'device': thing_name,
                    'nodeId': node_id,
                    'cameraSourceId': camera_source_id,
                    'conditions': conditions,
                    'confirmed': warning_id in confirmed_ids,
                    'message': (f"Camera source '{camera_source_id}' bound to "
                                f"node '{node_id}' on device '{thing_name}' "
                                f"is {', '.join(conditions)}"),
                })

    return errors, warnings


# ---------------------------------------------------------------------------
# Camera_Registry snapshot, binding context, and Camera_Binding delivery
# (camera-registry-sync task 11.7 — Requirements 8.1, 8.2, 8.5, 8.6, 12.3)
# ---------------------------------------------------------------------------

# Per-device Camera_Registry written by the Portal_Sync_Service
# (camera_sync.py); read here for the binding context endpoint and the
# pre-submit binding validation.
CAMERA_REGISTRY_TABLE = os.environ.get('CAMERA_REGISTRY_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# Item-type SK prefixes of the dda-portal-camera-registry table.
CAMERA_SK_META = 'META'
CAMERA_SK_PREFIX = 'CAMERA#'

# Staleness_Threshold setting (camera_registry.py owns the same key):
# per-entry ``stale`` is computed here before the snapshot reaches the
# time-free validate_camera_bindings (Req 4.1 feeding 9.3).
CAMERA_STALENESS_SETTING_KEY = 'camera_registry.staleness_threshold_hours'
CAMERA_DEFAULT_STALENESS_HOURS = 24

# Camera_Binding delivery shadow: desired.bindings["{workflowId}/{version}"]
# per target thing, written at deployment submission (Req 8.6).
CAMERA_BINDINGS_SHADOW_NAME = 'dda-camera-bindings'

# Rejection codes of the submission flow
CAMERA_ERROR_REGISTRY_UNAVAILABLE = 'REGISTRY_UNAVAILABLE'
CAMERA_ERROR_BINDING_DELIVERY = 'BINDING_DELIVERY_FAILED'
CAMERA_ERROR_BINDINGS_INVALID = 'CAMERA_BINDINGS_INVALID'
CAMERA_ERROR_WARNINGS_UNCONFIRMED = 'CAMERA_WARNINGS_UNCONFIRMED'


class CameraRegistryUnavailable(Exception):
    """The Camera_Registry could not be read. Submission rejects with
    REGISTRY_UNAVAILABLE rather than skipping binding validation."""


def _dynamo_safe(obj):
    """A JSON-shaped value with floats as Decimal, safe for DynamoDB."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def _camera_staleness_threshold_ms():
    """The configured Staleness_Threshold in milliseconds (default 24 h).

    A settings-read failure degrades to the default: staleness only grades
    warnings, so it must not turn into a registry-availability rejection.
    """
    hours = CAMERA_DEFAULT_STALENESS_HOURS
    if SETTINGS_TABLE:
        try:
            response = dynamodb.Table(SETTINGS_TABLE).get_item(
                Key={'setting_key': CAMERA_STALENESS_SETTING_KEY})
            value = (response.get('Item') or {}).get('value')
            if value is not None and float(value) > 0:
                hours = float(value)
        except (ClientError, TypeError, ValueError) as e:
            logger.warning(f"Could not read staleness threshold setting: {e}")
    return hours * 3600 * 1000


def load_camera_registry_snapshot(thing_names):
    """The per-target Camera_Registry snapshot consumed by
    validate_camera_bindings and the binding context endpoint:
    ``{thing_name: {'never_synced': bool, 'cameras': {csid: entry}}}``
    with per-entry ``stale`` precomputed against the Staleness_Threshold.

    Raises CameraRegistryUnavailable when the registry cannot be read —
    the caller must reject the submission, never skip validation.
    """
    if not CAMERA_REGISTRY_TABLE:
        raise CameraRegistryUnavailable(
            'Camera registry table is not configured')
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    threshold_ms = _camera_staleness_threshold_ms()
    now = int(datetime.utcnow().timestamp() * 1000)
    snapshot = {}
    for thing_name in thing_names:
        items = []
        kwargs = {'KeyConditionExpression': Key('device_id').eq(thing_name)}
        try:
            while True:
                response = table.query(**kwargs)
                items.extend(response.get('Items', []))
                last_key = response.get('LastEvaluatedKey')
                if not last_key:
                    break
                kwargs['ExclusiveStartKey'] = last_key
        except ClientError as e:
            raise CameraRegistryUnavailable(
                f"Camera registry read failed for device "
                f"'{thing_name}': {e}") from e
        meta = next((item for item in items
                     if item.get('sk') == CAMERA_SK_META), None)
        cameras = {}
        for item in items:
            sk = item.get('sk') or ''
            if not sk.startswith(CAMERA_SK_PREFIX):
                continue
            entry = _decimal_to_native(dict(item))
            last_reported_at = entry.get('last_reported_at')
            entry['stale'] = (last_reported_at is not None
                              and (now - int(last_reported_at)) > threshold_ms)
            csid = entry.get('camera_source_id') or sk[len(CAMERA_SK_PREFIX):]
            cameras[csid] = entry
        snapshot[thing_name] = {
            'never_synced': meta is None or bool(meta.get('never_synced', True)),
            'cameras': cameras,
        }
    return snapshot


def _binding_camera_view(csid, entry):
    """One registry entry as a selectable binding option (Reqs 8.1, 7.4
    display fields plus the degraded-condition inputs of 9.3)."""
    view = {
        'camera_source_id': csid,
        'name': entry.get('name'),
        'type': entry.get('type'),
        'params': entry.get('params') or {},
        'capabilities': entry.get('capabilities') or {},
        'origin': entry.get('origin'),
        'sync_status': entry.get('sync_status'),
        'last_reported_at': entry.get('last_reported_at'),
        'absent': bool(entry.get('absent', False)),
        'stale': bool(entry.get('stale', False)),
    }
    if view['absent'] and entry.get('absent_since') is not None:
        view['absent_since'] = entry['absent_since']
    return view


def get_camera_binding_context(user, query_params):
    """
    GET /deployments?view=binding-context — the CreateDeployment binding
    matrix's data source (camera-registry-sync Requirements 8.1, 8.5, 8.9).

    Query: usecase_id, workflow_id, workflow_version? (defaults to the
    latest version), and target_devices (comma-separated thing names) or
    target_thing_group.

    Returns, for each Camera_Input_Node of the workflow version and each
    target Edge_Device, the device's registered Camera_Sources as binding
    options (8.1), with a pre-selected cameraSourceId per node when the
    node's binding hint matches a source present in that device's
    registry (8.5). Versions without Camera_Input_Nodes return an empty
    matrix so the frontend skips the step entirely (8.9).
    """
    try:
        usecase_id = query_params.get('usecase_id')
        workflow_id = query_params.get('workflow_id')
        if not usecase_id or not workflow_id:
            return _workflow_error(400, 'MISSING_FIELDS',
                                   'usecase_id and workflow_id required')

        if not has_workflow_permission(user, usecase_id,
                                       Permission.WORKFLOW_DEPLOY):
            log_audit_event(
                user['user_id'], 'unauthorized_access', 'workflow',
                workflow_id, 'denied', {
                    'required_permissions': [Permission.WORKFLOW_DEPLOY.value],
                    'usecase_id': usecase_id,
                    'operation': 'camera_binding_context'
                })
            return _workflow_error(403, 'FORBIDDEN',
                                   'Insufficient permissions', {
                                       'required_permissions':
                                           [Permission.WORKFLOW_DEPLOY.value],
                                       'usecase_id': usecase_id
                                   })

        workflow_item = get_workflow_metadata(workflow_id)
        if not workflow_item or workflow_item.get('usecase_id') != usecase_id:
            return _workflow_error(404, 'WORKFLOW_NOT_FOUND',
                                   'Workflow not found')

        version_param = query_params.get(
            'workflow_version', workflow_item.get('latest_version', 1))
        try:
            workflow_version = int(version_param)
        except (TypeError, ValueError):
            return _workflow_error(400, 'INVALID_VERSION',
                                   'workflow_version must be an integer')

        version_item = workflow_guards.get_version_item(
            workflow_id, workflow_version)
        if not version_item:
            return _workflow_error(404, 'VERSION_NOT_FOUND',
                                   'Workflow version not found')
        version_item = _decimal_to_native(version_item)

        camera_nodes = version_item.get('camera_input_nodes') or []
        node_views = []
        for node in camera_nodes:
            node_view = {
                'node_id': node.get('node_id'),
                'node_type': node.get('node_type'),
            }
            if node.get('binding_hint'):
                node_view['binding_hint'] = node['binding_hint']
            node_views.append(node_view)

        context = {
            'workflow_id': workflow_id,
            'workflow_version': workflow_version,
            'has_binding_points': bool(version_item.get('has_binding_points')),
            # Frontend skip discriminator: no Camera_Input_Nodes, no
            # binding step (8.9).
            'binding_required': bool(version_item.get('has_binding_points')
                                     and camera_nodes),
            'camera_input_nodes': node_views,
            'targets': {},
        }
        if not camera_nodes:
            return create_response(200, context)

        target_devices = [name for name in
                          (query_params.get('target_devices') or '').split(',')
                          if name]
        target_thing_group = query_params.get('target_thing_group')
        if not target_devices and not target_thing_group:
            return _workflow_error(
                400, 'MISSING_FIELDS',
                'Either target_devices or target_thing_group required')

        usecase = get_usecase(usecase_id)
        if not usecase:
            return _workflow_error(404, 'USECASE_NOT_FOUND',
                                   'Use case not found')
        if target_devices:
            resolved_devices = list(target_devices)
        else:
            region = get_usecase_region(usecase)
            iot_client = get_usecase_client('iot', usecase, region=region)
            resolved_devices = resolve_target_thing_names(
                iot_client, [], target_thing_group)

        try:
            registry_snapshot = load_camera_registry_snapshot(resolved_devices)
        except CameraRegistryUnavailable as e:
            logger.error(f"Camera registry unavailable: {e}")
            return _workflow_error(
                503, CAMERA_ERROR_REGISTRY_UNAVAILABLE,
                'The camera registry could not be read for the target '
                'devices', {'reason': str(e)})

        for thing_name in resolved_devices:
            device_snapshot = registry_snapshot.get(thing_name) or {}
            cameras = device_snapshot.get('cameras') or {}
            camera_views = sorted(
                (_binding_camera_view(csid, entry)
                 for csid, entry in cameras.items()),
                key=lambda c: (c.get('name') or '', c['camera_source_id']))
            # Hint-matching pre-selection (8.5): the node's recorded
            # binding hint proposes a source only when that source is
            # present in THIS device's registry; the user confirms or
            # changes it in the matrix.
            preselected = {}
            for node in camera_nodes:
                hint = node.get('binding_hint') or {}
                hinted_csid = hint.get('cameraSourceId')
                if hinted_csid and hinted_csid in cameras:
                    preselected[node['node_id']] = hinted_csid
            context['targets'][thing_name] = {
                'state': ('never-synced'
                          if device_snapshot.get('never_synced', True)
                          else 'synced'),
                'cameras': camera_views,
                'preselected': preselected,
            }

        return create_response(200, context)

    except Exception as e:
        logger.error(f"Error building camera binding context: {str(e)}",
                     exc_info=True)
        return _workflow_error(500, 'INTERNAL_ERROR',
                               'Failed to build camera binding context')


def _workflow_binding_key(workflow_id, workflow_version):
    """The dda-camera-bindings desired.bindings key of one deployed
    workflow version (design: '{workflowId}/{version}')."""
    return f"{workflow_id}/{int(workflow_version)}"


def _deployed_workflow_binding_keys(components_map):
    """The binding keys of every Workflow_Component in a deployment's
    component set — the keys that must survive a shadow prune. Each
    entry's workflow version comes from D2 scan-first resolution (a
    bumped-major entry from re-packaging resolves its true workflow
    version, so the live key survives), falling back to the legacy major
    parse for unrecorded entries; table-read failures degrade to the
    fallback rather than raising."""
    keys = set()
    for name in components_map or {}:
        if not name.startswith(WORKFLOW_COMPONENT_PREFIX):
            continue
        wf_id = name[len(WORKFLOW_COMPONENT_PREFIX):]
        version = str((components_map[name] or {}).get('componentVersion', ''))
        if not wf_id:
            continue
        workflow_version, _ = _resolve_workflow_version_item(wf_id, version)
        if workflow_version is not None:
            keys.add(_workflow_binding_key(wf_id, workflow_version))
    return keys


def _existing_binding_keys(iot_data, thing_name):
    """The desired.bindings keys currently in one thing's camera-bindings
    shadow (empty when the shadow does not exist yet)."""
    try:
        response = iot_data.get_thing_shadow(
            thingName=thing_name, shadowName=CAMERA_BINDINGS_SHADOW_NAME)
        payload = json.loads(response['payload'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ResourceNotFoundException':
            return []
        raise
    desired = (payload.get('state') or {}).get('desired') or {}
    return list((desired.get('bindings') or {}).keys())


def rollback_camera_bindings(iot_data, thing_names, binding_key):
    """Best-effort prune of an aborted submission's already-written
    binding keys (mid-submission failure handling, Req 8.6)."""
    for thing_name in thing_names:
        try:
            iot_data.update_thing_shadow(
                thingName=thing_name,
                shadowName=CAMERA_BINDINGS_SHADOW_NAME,
                payload=json.dumps({'state': {'desired': {'bindings': {
                    binding_key: None}}}}))
        except Exception as e:  # noqa: BLE001 — best-effort by contract
            logger.warning(
                f"Best-effort binding rollback failed for {thing_name}: {e}")


def deliver_camera_bindings(iot_data, thing_names, binding_key,
                            camera_bindings, deployed_keys):
    """
    Write ``desired.bindings[binding_key]`` (this device's per-node
    Camera_Bindings) into each target thing's dda-camera-bindings shadow
    and prune keys for workflow versions no longer deployed to the device
    (Reqs 8.2, 8.6). The packaged artifact is untouched — bindings travel
    only in the shadow.

    Returns ``(written, failure)``: the things written so far and, on the
    first failure, a failure record. On failure the already-written
    targets are best-effort pruned and the caller aborts deployment
    creation.
    """
    written = []
    for thing_name in thing_names:
        bindings_update = {binding_key: camera_bindings.get(thing_name) or {}}
        try:
            for key in _existing_binding_keys(iot_data, thing_name):
                if key != binding_key and key not in deployed_keys:
                    # Version no longer deployed to this device: prune.
                    bindings_update[key] = None
            iot_data.update_thing_shadow(
                thingName=thing_name,
                shadowName=CAMERA_BINDINGS_SHADOW_NAME,
                payload=json.dumps(
                    {'state': {'desired': {'bindings': bindings_update}}},
                    default=lambda o: (float(o) if isinstance(o, Decimal)
                                       else str(o))))
        except Exception as e:  # noqa: BLE001 — any write failure aborts
            logger.error(
                f"Camera binding shadow write failed for {thing_name}: {e}")
            rollback_camera_bindings(iot_data, written, binding_key)
            return written, {'device': thing_name, 'error': str(e)}
        written.append(thing_name)
    return written, None


def create_workflow_deployment(body, user):
    """
    Deploy a packaged Workflow_Component to devices or thing groups within
    the Use_Case (Requirements 8.1, 8.2, 8.4, 8.5, 11.5).

    Body: {
        "component_type": "workflow",
        "usecase_id": "...",
        "workflow_id": "...",
        "workflow_version": N,              # defaults to the latest version
        "target_devices": ["thing", ...]    # or
        "target_thing_group": "group",
        "deployment_name": "..."?,          # optional
        "rollout_config": {...}?            # optional, same as create_deployment
    }
    """
    try:
        usecase_id = body.get('usecase_id')
        workflow_id = body.get('workflow_id')
        target_devices = body.get('target_devices', [])
        target_thing_group = body.get('target_thing_group')
        deployment_name = body.get('deployment_name', '')

        if not usecase_id:
            return _workflow_error(400, 'MISSING_FIELDS', 'usecase_id required')
        if not workflow_id:
            return _workflow_error(400, 'MISSING_FIELDS', 'workflow_id required')
        if not target_devices and not target_thing_group:
            return _workflow_error(
                400, 'MISSING_FIELDS',
                'Either target_devices or target_thing_group required')

        # RBAC: workflow deployments require workflow:deploy
        # (Operator, UseCaseAdmin, PortalAdmin — Requirements 11.2, 11.4)
        if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_DEPLOY):
            log_audit_event(
                user['user_id'], 'unauthorized_access', 'workflow', workflow_id,
                'denied', {
                    'required_permissions': [Permission.WORKFLOW_DEPLOY.value],
                    'usecase_id': usecase_id,
                    'operation': 'deploy_workflow'
                }
            )
            return _workflow_error(403, 'FORBIDDEN', 'Insufficient permissions', {
                'required_permissions': [Permission.WORKFLOW_DEPLOY.value],
                'usecase_id': usecase_id
            })

        # The workflow must exist and belong to the Use_Case being deployed
        # into (device/thing-group targeting stays within the Use_Case, 8.1).
        workflow_item = get_workflow_metadata(workflow_id)
        if not workflow_item or workflow_item.get('usecase_id') != usecase_id:
            return _workflow_error(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')

        version_param = body.get('workflow_version',
                                 workflow_item.get('latest_version', 1))
        try:
            workflow_version = int(version_param)
        except (TypeError, ValueError):
            return _workflow_error(400, 'INVALID_VERSION',
                                   'workflow_version must be an integer')

        # Deployment guard: only versions with a recorded passed-validation
        # run and zero error findings may be deployed (Requirements 4.7, 4.10)
        guard_failure = workflow_guards.check_workflow_version_validated(
            workflow_id, workflow_version)
        if guard_failure:
            return _workflow_error(
                guard_failure['status_code'], guard_failure['code'],
                guard_failure['message'], guard_failure['details'])

        # The version must have been packaged as a Greengrass component
        version_item = workflow_guards.get_version_item(workflow_id, workflow_version)
        if not version_item or not version_item.get('component_arn'):
            return _workflow_error(
                409, 'WORKFLOW_NOT_PACKAGED',
                'Workflow version has not been packaged as a Greengrass '
                'component; package it before deploying',
                {'workflow_id': workflow_id, 'version': workflow_version})

        # The REGISTERED component version of this workflow version (D1
        # forward resolution from the version item just gated on):
        # re-packaging bumps the component major past the workflow
        # version, so the legacy {workflow_version}.0.0 derivation goes
        # stale — pinning it deploys the OLD component and Greengrass
        # reports COMPLETED while delivering nothing (live incident
        # 72c2f784). Threaded to the components-map entry, the vLLM gate
        # manifest, the association record, the audit entry, and the 201
        # response below.
        component_version = resolve_workflow_component_version(
            version_item, workflow_version)

        usecase = get_usecase(usecase_id)
        if not usecase:
            return _workflow_error(404, 'USECASE_NOT_FOUND', 'Use case not found')

        region = get_usecase_region(usecase)
        account_id = usecase.get('account_id', '')
        session_name = f"gg-wf-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        greengrass_client = get_usecase_client(
            'greengrassv2', usecase, session_name=session_name, region=region)
        iot_client = get_usecase_client(
            'iot', usecase, session_name=session_name, region=region)

        # Pre-submit LocalServer compatibility check (Requirement 8.4):
        # every target device's installed LocalServer version must satisfy
        # the component's minLocalServerVersion. The Component_Packager
        # writes the same resolved value into the packaged manifest.json.
        # A per-version override (explicit pin on the WorkflowVersions item)
        # applies uniformly to every device; otherwise the per-arch map gates
        # each device against its own LocalServer variant lineage.
        version_override = version_item.get('min_local_server_version')
        min_local_server = version_override or WORKFLOW_MIN_LOCAL_SERVER_VERSION
        by_arch = {} if version_override else WORKFLOW_MIN_LOCAL_SERVER_VERSIONS
        resolved_devices = resolve_target_thing_names(
            iot_client, target_devices, target_thing_group)
        incompatible = check_local_server_compatibility(
            greengrass_client, resolved_devices, min_local_server, by_arch)
        if incompatible:
            return _workflow_error(
                409, 'INCOMPATIBLE_LOCAL_SERVER',
                'One or more target devices do not have a LocalServer '
                'component version compatible with this workflow component; '
                'the deployment was not submitted',
                {
                    'workflow_id': workflow_id,
                    'workflow_version': workflow_version,
                    'min_local_server_version': min_local_server,
                    'incompatible_devices': incompatible
                })

        # Plugin lifecycle + architecture gates over the dependency closure
        # (custom-node-designer 9.7, 9.8, 9.11, 16.3, 16.6), alongside the
        # minLocalServerVersion pass above. The Component_Packager records
        # the workflow's depended-on Plugin_Components (dda.plugin.* name ->
        # component version) on the version item; Greengrass dependency
        # resolution delivers those versions with the deployment (16.5), so
        # only the pre-submit gates run here.
        plugin_closure = {
            str(name): str(version)
            for name, version in
            (version_item.get('plugin_components') or {}).items()
        }
        gate_error = check_plugin_deployment_gates(plugin_closure,
                                                   resolved_devices)
        if gate_error:
            return gate_error

        # vLLM architecture gate (vllm-triton-inference 3.3, 3.4, 3.7,
        # 8.5, 8.6): activated iff the packaged version item records
        # has_llm_inference (written by workflow_packaging.py alongside
        # packaged_architectures — the supported set the gate compares
        # device architectures against). Versions without LLM content
        # contribute zero findings, keeping pre-feature validation
        # verbatim, jp4 included.
        if version_item.get('has_llm_inference'):
            vllm_manifests = {
                workflow_component_name(workflow_id): {
                    'version': component_version,
                    'architectures': [
                        str(arch) for arch in
                        version_item.get('packaged_architectures') or []],
                }
            }
            gate_error = check_vllm_deployment_gate(vllm_manifests,
                                                    resolved_devices)
            if gate_error:
                return gate_error

        # Deploy-time Camera_Binding validation (camera-registry-sync
        # 8.3, 8.4, 8.7-8.9, 9.1-9.5), alongside the pre-submit gates
        # above. Versions without Camera_Input_Nodes skip the step
        # entirely (8.9). A registry read failure rejects the submission
        # with REGISTRY_UNAVAILABLE — validation is never skipped.
        camera_bindings = body.get('camera_bindings') or {}
        confirmed_warnings = body.get('confirmed_warnings') or []
        native_version_item = _decimal_to_native(version_item)
        camera_nodes = native_version_item.get('camera_input_nodes') or []
        camera_warnings = []
        if camera_nodes:
            try:
                registry_snapshot = load_camera_registry_snapshot(
                    resolved_devices)
            except CameraRegistryUnavailable as e:
                logger.error(f"Camera registry unavailable: {e}")
                return _workflow_error(
                    503, CAMERA_ERROR_REGISTRY_UNAVAILABLE,
                    'The camera registry could not be read for the target '
                    'devices; camera binding validation cannot run and the '
                    'deployment was not submitted', {'reason': str(e)})
            camera_errors, camera_warnings = validate_camera_bindings(
                native_version_item, resolved_devices, registry_snapshot,
                camera_bindings, confirmed_warnings)
            if camera_errors:
                return _workflow_error(
                    409, CAMERA_ERROR_BINDINGS_INVALID,
                    'One or more camera bindings are invalid; the '
                    'deployment was not submitted',
                    {'errors': camera_errors, 'warnings': camera_warnings})
            unconfirmed = [w for w in camera_warnings
                           if not w.get('confirmed')]
            if unconfirmed:
                return _workflow_error(
                    409, CAMERA_ERROR_WARNINGS_UNCONFIRMED,
                    'Camera binding warnings require explicit confirmation '
                    'before the deployment can be created',
                    {'warnings': camera_warnings})

        if target_thing_group:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thinggroup/{target_thing_group}"
        else:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thing/{target_devices[0]}"

        # Greengrass revision semantics (Requirement 8.5): deployments are
        # immutable and per-target — creating a new deployment for the same
        # target ARN revises the existing one, replacing any older
        # Workflow_Component version. We merge with the target's current
        # component set so revising never drops LocalServer or other
        # components already on the device (Requirement 13.3).
        existing_deployment = find_latest_deployment_for_target(
            greengrass_client, target_arn)
        is_revision = existing_deployment is not None

        components_map = {}
        if is_revision:
            try:
                detail = greengrass_client.get_deployment(
                    deploymentId=existing_deployment['deploymentId'])
                components_map = dict(detail.get('components', {}) or {})
            except ClientError as e:
                logger.warning(
                    f"Could not read components of existing deployment "
                    f"{existing_deployment.get('deploymentId')}: {e}")

        # ShadowManager synchronize ensure (shadowmanager-sync-config-on-
        # revision): the copy above is verbatim, so a bare or stale
        # ShadowManager entry from the previous revision would otherwise
        # ship forever (Greengrass preserves the device's last-applied
        # config for a bare entry) and portal shadow names added after the
        # target's last CONFIGURED revision would never sync. PRESENCE-gated
        # on purpose: fresh workflow deployments must not start
        # auto-including ShadowManager (out of scope; 3.6 keeps carry-over
        # verbatim). Copied entries always carry componentVersion, so the
        # lazy resolver effectively never fires; if it ever does,
        # running_nucleus=None returns the static SHADOW_MANAGER_VERSION pin
        # immediately — no new Nucleus lookup on this path.
        if 'aws.greengrass.ShadowManager' in components_map:
            ensure_shadow_manager_sync(
                components_map,
                resolve_version=lambda: resolve_shadow_manager_version(
                    greengrass_client, region, None))

        component_name = workflow_component_name(workflow_id)
        # Setting the entry (re)places the workflow component at the new
        # version; Greengrass replaces the older version on the device (8.5).
        # component_version is the REGISTERED version resolved above (D1).
        components_map[component_name] = {'componentVersion': component_version}

        # Subscribe accessControl (trigger-activation-runtime 10.2, 10.3):
        # same merge as create_deployment, applied to the FINAL merged
        # component set (target's existing components + this workflow
        # entry). The helper resolves each entry's version item
        # authoritatively BY RESOLUTION (D2 scan-first on the recorded
        # component version, legacy major parse as fallback) — covering
        # this entry, generic-path entries, and carried-over bumped-major
        # entries alike, superseding the subscribe-merge design's
        # "{workflow_version}.0.0 by construction" note.
        deployment_warnings = apply_subscribe_access_control(components_map)

        if not deployment_name:
            if is_revision and existing_deployment.get('deploymentName'):
                deployment_name = existing_deployment['deploymentName']
            else:
                timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
                deployment_name = f"portal-deployment-{timestamp}"

        deployment_params = {
            'targetArn': target_arn,
            'deploymentName': deployment_name,
            'components': components_map,
            'tags': {
                'dda-portal:managed': 'true',
                'dda-portal:usecase-id': usecase_id,
                'dda-portal:workflow-id': workflow_id,
                'dda-portal:workflow-version': str(workflow_version),
                'dda-portal:created-by': user['user_id']
            }
        }
        rollout_config = body.get('rollout_config', {})
        if rollout_config:
            deployment_params['deploymentPolicies'] = {
                'failureHandlingPolicy': 'ROLLBACK' if rollout_config.get('auto_rollback', True) else 'DO_NOTHING',
                'componentUpdatePolicy': {
                    'timeoutInSeconds': rollout_config.get('timeout_seconds', 60),
                    'action': 'NOTIFY_COMPONENTS'
                }
            }

        # Camera_Binding delivery (Reqs 8.2, 8.6): each target thing's
        # dda-camera-bindings shadow gets desired.bindings["{wf}/{ver}"]
        # via the assumed-role iot-data client, with keys for versions no
        # longer deployed pruned. The Greengrass artifact stays untouched.
        # A mid-submission shadow write failure aborts deployment creation
        # with best-effort pruning of the already-written targets.
        delivered_bindings = None
        if camera_nodes and native_version_item.get('has_binding_points'):
            binding_key = _workflow_binding_key(workflow_id, workflow_version)
            iot_data_client = get_usecase_client(
                'iot-data', usecase, session_name=session_name, region=region)
            written, failure = deliver_camera_bindings(
                iot_data_client, resolved_devices, binding_key,
                camera_bindings, _deployed_workflow_binding_keys(components_map))
            if failure:
                return _workflow_error(
                    502, CAMERA_ERROR_BINDING_DELIVERY,
                    'Camera binding delivery to a target device failed; the '
                    'deployment was not submitted and bindings already '
                    'written were pruned',
                    {'failed_device': failure['device'],
                     'error': failure['error'],
                     'rolled_back_devices': written})
            delivered_bindings = camera_bindings

        response = greengrass_client.create_deployment(**deployment_params)
        deployment_id = response.get('deploymentId')

        # Association record: workflow version -> deployment -> devices
        # (8.2); delivered Camera_Bindings are stored on the record for
        # display and audit (camera-registry-sync 8.2, 12.3).
        superseded_id = existing_deployment.get('deploymentId') if is_revision else None
        record_workflow_deployment(
            deployment_id, usecase_id, workflow_id, workflow_version,
            target_arn, resolved_devices, target_thing_group,
            is_revision, superseded_id, user,
            camera_bindings=delivered_bindings,
            component_version=component_version)

        # Audit log entry for deploy (Requirement 11.5; camera-registry-sync
        # 12.3 — a deployment created with Camera_Bindings records them).
        audit_details = {
            'usecase_id': usecase_id,
            'workflow_version': workflow_version,
            'deployment_id': deployment_id,
            'component_name': component_name,
            'component_version': component_version,
            'target_arn': target_arn,
            'target_devices': resolved_devices,
            'target_thing_group': target_thing_group,
            'is_revision': is_revision,
            'superseded_deployment_id': superseded_id
        }
        if delivered_bindings is not None:
            audit_details['camera_bindings'] = _dynamo_safe(delivered_bindings)
        log_audit_event(
            user['user_id'], 'deploy_workflow', 'workflow', workflow_id,
            'success', audit_details
        )

        logger.info(
            f"{'Revised' if is_revision else 'Created'} workflow deployment "
            f"{deployment_id} for workflow {workflow_id} v{workflow_version}")

        response_body = {
            'deployment_id': deployment_id,
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'workflow_id': workflow_id,
            'workflow_version': workflow_version,
            'component_name': component_name,
            'component_version': component_version,
            'target_arn': target_arn,
            'target_devices': resolved_devices,
            'target_thing_group': target_thing_group,
            'is_revision': is_revision,
            'superseded_deployment_id': superseded_id,
            'camera_bindings_delivered': delivered_bindings is not None,
            'camera_warnings': camera_warnings,
            'message': ('Workflow deployment updated successfully' if is_revision
                        else 'Workflow deployment created successfully')
        }
        # Additive: present only when the merge produced warnings,
        # mirroring create_deployment (10.3/10.4 byte-identity).
        if deployment_warnings:
            response_body['warnings'] = deployment_warnings
        return create_response(201, response_body)

    except ClientError as e:
        logger.error(f"AWS error creating workflow deployment: {str(e)}")
        return _workflow_error(500, 'DEPLOYMENT_FAILED',
                               f'Failed to create workflow deployment: {str(e)}')
    except Exception as e:
        logger.error(f"Error creating workflow deployment: {str(e)}", exc_info=True)
        return _workflow_error(500, 'INTERNAL_ERROR',
                               'Failed to create workflow deployment')


def get_device_workflow_deployment_status(greengrass_client, thing_name,
                                          deployment_id):
    """
    Per-device Greengrass status of one deployment via the device's
    effective deployments (Requirement 8.3). Returns a status dict, or a
    PENDING placeholder when the deployment has not reached the device yet.
    """
    try:
        response = greengrass_client.list_effective_deployments(
            coreDeviceThingName=thing_name)
        for eff_dep in response.get('effectiveDeployments', []):
            if eff_dep.get('deploymentId') != deployment_id:
                continue
            modified_ts = eff_dep.get('modifiedTimestamp')
            if modified_ts and hasattr(modified_ts, 'isoformat'):
                modified_ts = modified_ts.isoformat()
            return {
                'device': thing_name,
                'deployment_status': eff_dep.get('coreDeviceExecutionStatus', 'UNKNOWN'),
                'reason': eff_dep.get('reason', ''),
                'description': eff_dep.get('description', ''),
                'modified_timestamp': modified_ts
            }
    except ClientError as e:
        logger.warning(f"Could not get effective deployments for {thing_name}: {e}")
        return {
            'device': thing_name,
            'deployment_status': 'UNKNOWN',
            'reason': 'Could not read the device deployment status',
            'description': '',
            'modified_timestamp': None
        }
    return {
        'device': thing_name,
        'deployment_status': 'PENDING',
        'reason': 'Deployment has not been reported by this device yet',
        'description': '',
        'modified_timestamp': None
    }


def query_workflow_deployment_records(usecase_id, workflow_id):
    """
    Workflow association records (component_type: workflow) of one workflow
    from the Deployments table via the usecase-deployments-index GSI.
    """
    records = []
    table = dynamodb.Table(DEPLOYMENTS_TABLE)
    kwargs = {
        'IndexName': 'usecase-deployments-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'FilterExpression': 'component_type = :ct AND workflow_id = :wid',
        'ExpressionAttributeValues': {
            ':uid': usecase_id,
            ':ct': 'workflow',
            ':wid': workflow_id
        }
    }
    while True:
        response = table.query(**kwargs)
        records.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return [_decimal_to_native(r) for r in records]


def refresh_workflow_deployment_status(deployment_id, overall_status):
    """Best-effort sync of the association record with the latest overall
    Greengrass deployment status (keeps 5.6 active-deployment checks fresh)"""
    try:
        dynamodb.Table(DEPLOYMENTS_TABLE).update_item(
            Key={'deployment_id': deployment_id},
            UpdateExpression='SET #s = :s, deployment_status = :s, updated_at = :t',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': overall_status,
                ':t': int(datetime.utcnow().timestamp() * 1000)
            }
        )
    except ClientError as e:
        logger.warning(f"Could not refresh status of deployment {deployment_id}: {e}")


def list_workflow_deployments(user, query_params):
    """
    GET /deployments?usecase_id=...&workflow_id=...[&workflow_version=N]

    Workflow deployment associations with per-device Greengrass deployment
    status for the workflow page (Requirements 8.2, 8.3). Viewers may read
    deployment status (Requirement 11.3), so this requires workflow:read.
    """
    try:
        usecase_id = query_params.get('usecase_id')
        workflow_id = query_params.get('workflow_id')

        if not usecase_id:
            return _workflow_error(400, 'MISSING_FIELDS',
                                   'usecase_id parameter required')

        if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
            return _workflow_error(403, 'FORBIDDEN', 'Insufficient permissions', {
                'required_permissions': [Permission.WORKFLOW_READ.value],
                'usecase_id': usecase_id
            })

        usecase = get_usecase(usecase_id)
        if not usecase:
            return _workflow_error(404, 'USECASE_NOT_FOUND', 'Use case not found')

        records = query_workflow_deployment_records(usecase_id, workflow_id)

        version_filter = query_params.get('workflow_version')
        if version_filter is not None:
            try:
                version_filter = int(version_filter)
                records = [r for r in records
                           if int(r.get('workflow_version', -1)) == version_filter]
            except (TypeError, ValueError):
                return _workflow_error(400, 'INVALID_VERSION',
                                       'workflow_version must be an integer')

        region = get_usecase_region(usecase)
        session_name = f"gg-wfls-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        greengrass_client = get_usecase_client(
            'greengrassv2', usecase, session_name=session_name, region=region)

        deployments = []
        for record in sorted(records, key=lambda r: r.get('created_at', 0),
                             reverse=True):
            deployment_id = record.get('deployment_id')

            # Overall Greengrass deployment status
            overall_status = record.get('deployment_status', 'UNKNOWN')
            try:
                detail = greengrass_client.get_deployment(deploymentId=deployment_id)
                latest = detail.get('deploymentStatus')
                if latest and latest != overall_status:
                    overall_status = latest
                    refresh_workflow_deployment_status(deployment_id, latest)
            except ClientError as e:
                logger.warning(f"Could not get deployment {deployment_id}: {e}")

            # Per-device Greengrass deployment status (Requirement 8.3)
            device_statuses = [
                get_device_workflow_deployment_status(
                    greengrass_client, thing_name, deployment_id)
                for thing_name in record.get('target_devices', []) or []
            ]

            deployments.append({
                'deployment_id': deployment_id,
                'usecase_id': usecase_id,
                'component_type': 'workflow',
                'workflow_id': record.get('workflow_id'),
                'workflow_version': record.get('workflow_version'),
                'component_name': record.get('component_name'),
                'component_version': record.get('component_version'),
                'target_arn': record.get('target_arn'),
                'target_devices': record.get('target_devices', []),
                'target_thing_group': record.get('target_thing_group'),
                'deployment_status': overall_status,
                'device_statuses': device_statuses,
                'is_revision': record.get('is_revision', False),
                'created_by': record.get('created_by'),
                'created_at': record.get('created_at'),
                'updated_at': record.get('updated_at')
            })

        return create_response(200, {
            'deployments': deployments,
            'count': len(deployments)
        })

    except ClientError as e:
        logger.error(f"AWS error listing workflow deployments: {str(e)}")
        return _workflow_error(500, 'LIST_FAILED',
                               f'Failed to list workflow deployments: {str(e)}')
    except Exception as e:
        logger.error(f"Error listing workflow deployments: {str(e)}", exc_info=True)
        return _workflow_error(500, 'INTERNAL_ERROR',
                               'Failed to list workflow deployments')
