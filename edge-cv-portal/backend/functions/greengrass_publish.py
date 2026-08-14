"""
Greengrass component publishing Lambda functions
Implements component creation and publishing to AWS IoT Greengrass
Based on DDA_Greengrass_Component_Creator.ipynb Phase 3
"""
import json
import os
import logging
from dataclasses import asdict
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import re
import time

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields,
    is_cross_account_setup, get_usecase_client, assume_usecase_role, get_usecase
)

# Preflight Fit_Check (vllm-sizing-and-packaging-errors Requirements 3.4,
# 3.6, 3.7): pure sizing module bundled alongside this handler in the
# functions asset. Imported as module attributes so tests can monkeypatch
# the estimation/evaluation seams.
from vllm_fit_check import estimate_weights, evaluate_fit

# Shared model-name sanitization transform (same functions bundle):
# single source of truth for the packaged/served name alignment
# (vllm-model-name-mismatch Requirement 2.2).
from model_naming import safe_model_name

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
MODELS_TABLE = os.environ.get('MODELS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

class PublishError(Exception):
    """A model component cannot be published as requested.

    Raised (among other places) by resolve_local_server_component when a
    model's compile target does not resolve to a known JetPack-tagged
    LocalServer variant. publish_component turns this into a failed-publish
    response for the offending target rather than creating a component
    version with a wrong/ambiguous LocalServer dependency.
    """
    pass


# Every LocalServer variant is explicitly JetPack/arch-tagged. The JetPack 4
# variant is `arm64JP4` (renamed from the bare, untagged `arm64`); the bare
# `aws.edgeml.dda.LocalServer.arm64` name is RETIRED as a produced/depended
# name so nothing owns the ambiguous "generic aarch64" catch-all that used to
# be stamped onto models with an unknown compile target (localserver-arch-
# naming). Legacy bare-arm64 installs are still recognized on the READ side
# (deployments.local_server_component_arch), but are never produced here.
JP4_LOCAL_SERVER = 'aws.edgeml.dda.LocalServer.arm64JP4'
_AMD64_LOCAL_SERVER = 'aws.edgeml.dda.LocalServer.amd64'

# Compilation target -> DDA LocalServer component name.
# aarch64 has three JetPack-tagged variants (arm64JP4/JP5/JP6). A model
# compiled for a given JetPack device MUST depend on that JetPack's variant,
# otherwise the deployment pulls in the wrong LocalServer, which collides on
# port 3443 with the correct variant and crash-loops the device to BROKEN.
TARGET_TO_LOCAL_SERVER = {
    'jetson-xavier': JP4_LOCAL_SERVER,                           # JetPack 4
    'jetson-xavier-jp5': 'aws.edgeml.dda.LocalServer.arm64JP5',  # JetPack 5
    'jetson-xavier-jp6': 'aws.edgeml.dda.LocalServer.arm64JP6',  # JetPack 6
    'jetson-xavier-jp7': 'aws.edgeml.dda.LocalServer.arm64JP7',  # JetPack 7
    'arm64-cpu': JP4_LOCAL_SERVER,                               # arm64 CPU -> JP4 baseline
    'x86_64-cpu': _AMD64_LOCAL_SERVER,
    'x86_64-cuda': _AMD64_LOCAL_SERVER,
}

# Target to platform mapping
TARGET_TO_PLATFORM = {
    'jetson-xavier': 'aarch64',
    'jetson-xavier-jp5': 'aarch64',
    'jetson-xavier-jp6': 'aarch64',
    'jetson-xavier-jp7': 'aarch64',
    'arm64-cpu': 'aarch64',
    'x86_64-cpu': 'amd64',
    'x86_64-cuda': 'amd64'
}


# Greengrass component-name constraints (Requirement 2.6). A per-JetPack vLLM
# name is `Base_Component_Name` + '-' + Target_Suffix, so a long model name can
# push the derived name past the service limit. Validating here turns that into
# a recorded failed target with a clear message BEFORE recipe generation,
# instead of an opaque API error at create_component_version time.
GREENGRASS_COMPONENT_NAME_MAX = 128
GREENGRASS_COMPONENT_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


def validate_greengrass_component_name(name: str) -> None:
    """Fail closed on a component name Greengrass would reject.

    Raises PublishError when the name is empty, exceeds
    GREENGRASS_COMPONENT_NAME_MAX characters, or contains a character outside
    the Greengrass charset `^[a-zA-Z0-9._-]+$`. A no-op for vision component
    names (already sanitized and short) and for every in-range vLLM
    Per_JetPack_Component name.
    """
    if not name:
        raise PublishError("Component name must not be empty")
    if len(name) > GREENGRASS_COMPONENT_NAME_MAX:
        raise PublishError(
            f"Component name '{name}' is {len(name)} characters, which "
            f"exceeds the Greengrass limit of "
            f"{GREENGRASS_COMPONENT_NAME_MAX}; shorten the model name so the "
            f"per-target component name fits"
        )
    if not GREENGRASS_COMPONENT_NAME_RE.match(name):
        raise PublishError(
            f"Component name '{name}' contains characters Greengrass does "
            f"not allow (permitted: letters, digits, '.', '_', '-')"
        )


def parse_component_arn(arn: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (component_name, component_version) from a component ARN.

    Used only for log detail during the atomicity rollback (design step 8):
    when a delete is denied or fails, the warning must name the component and
    version that survives cloud-side so it is identifiable straight from the
    logs. Never raises — an unparseable ARN yields (None, None) so the rollback
    stays best-effort.
    """
    if not isinstance(arn, str):
        return None, None
    name, version = None, None
    if ':versions:' in arn:
        head, version = arn.rsplit(':versions:', 1)
    else:
        head = arn
    if ':components:' in head:
        name = head.rsplit(':components:', 1)[-1]
    return (name or None), (version or None)


def resolve_local_server_component(target: str, platform: str) -> str:
    """
    Resolve the correct DDA LocalServer dependency for a model component.

    Known compile targets map to their explicit JetPack-tagged (or amd64)
    variant. x86_64 has a single LocalServer variant, so an unknown target on
    the amd64 platform safely resolves to it. Any other unresolved target
    (aarch64 or otherwise) FAILS CLOSED: it raises PublishError rather than
    silently defaulting to a bare/untagged aarch64 name. This is the root-cause
    fix for the wrong-LocalServer incident — a missing/unknown target must
    never quietly pick a JetPack variant.
    """
    name = TARGET_TO_LOCAL_SERVER.get(target)
    if name:
        return name
    if platform == 'amd64':
        return _AMD64_LOCAL_SERVER
    raise PublishError(
        f"Cannot resolve a LocalServer dependency for target '{target}' "
        f"(platform '{platform}'): no known JetPack-tagged LocalServer "
        f"variant. The model must declare a supported compile target "
        f"(jetson-xavier, jetson-xavier-jp5, jetson-xavier-jp6, "
        f"jetson-xavier-jp7, x86_64-cpu, x86_64-cuda, arm64-cpu)."
    )


def resolve_target_platform(target: str) -> str:
    """Resolve a compile target's manifest platform, failing closed.

    Replaces the old `TARGET_TO_PLATFORM.get(target, 'amd64')` default. That
    default silently stamped an unmapped aarch64 target (classically
    `jetson-xavier-jp7`) with platform `amd64`, which then satisfied
    resolve_local_server_component's amd64 branch and handed back the amd64
    LocalServer instead of failing closed — the fail-closed guarantee of
    `localserver-arch-naming` was bypassed by the platform default, and the
    defect was only observable on the device.

    A target must therefore be mapped in BOTH module-level maps or it fails
    closed here with a PublishError, before any recipe is generated, so a
    future target added to `packaging.VLLM_ARCH_TO_TARGET` without both map
    entries cannot repeat the defect (Requirement 2.19).
    """
    if target not in TARGET_TO_PLATFORM or target not in TARGET_TO_LOCAL_SERVER:
        supported = sorted(set(TARGET_TO_PLATFORM) & set(TARGET_TO_LOCAL_SERVER))
        raise PublishError(
            f"Unsupported compile target '{target}': it has no platform and "
            f"LocalServer mapping (TARGET_TO_PLATFORM / "
            f"TARGET_TO_LOCAL_SERVER) (supported: {supported})"
        )
    return TARGET_TO_PLATFORM[target]


def get_training_job_details(training_id: str) -> Dict:
    """Get training job details from DynamoDB"""
    try:
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            raise ValueError(f"Training job {training_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting training job details: {str(e)}")
        raise


def validate_component_name(name: str) -> bool:
    """Validate component name starts with 'model-'"""
    return name.startswith('model-')


def validate_component_version(version: str) -> bool:
    """Validate component version format x.0.0"""
    pattern = r'^\d+\.0+\.0+$'
    return bool(re.match(pattern, version))


def generate_component_recipe(
    component_name: str,
    component_version: str,
    friendly_name: str,
    platform: str,
    artifact_s3_uri: str,
    model_unarchived_path: str,
    target: str = None
) -> Dict:
    """
    Generate Greengrass component recipe
    Phase 3: Component Creation from DDA notebook
    """
    
    # Determine DDA LocalServer dependency based on target (JP4 vs JP5) / platform
    local_server_component = resolve_local_server_component(target, platform)

    # Model Startup readiness gate.
    #
    # The HARD ComponentDependency on LocalServer below only guarantees that
    # LocalServer's lifecycle has STARTED, not that it is functionally ready.
    # LocalServer uses a `Run` lifecycle (foreground `docker compose up`), and
    # Greengrass reports a Run component as RUNNING the moment its script
    # launches -- seconds before the backend container boots and runs
    # cp_model_conversion_files(), which is what copies model_convertor.py /
    # convert_model_cleanup.py / resources_for_copy onto the host /aws_dda.
    # Without a gate, a first-time deployment (LocalServer + model together on a
    # fresh device) races: the model Startup runs `python3 /aws_dda/model_convertor.py`
    # before that file exists, exits 2, goes BROKEN, and rolls back the whole
    # deployment. Poll (bounded) for the seed to appear before invoking it.
    convertor_cmd = (
        f'python3 /aws_dda/model_convertor.py '
        f'--unarchived_model_path {{artifacts:decompressedPath}}/{model_unarchived_path}/ '
        f'--model_version {component_version} --model_name {component_name}'
    )
    startup_script = (
        '#!/bin/bash\n'
        '# Wait for the LocalServer backend to seed the host model-conversion\n'
        '# scripts onto /aws_dda (cp_model_conversion_files) before running them.\n'
        'seed_timeout=600\n'
        'waited=0\n'
        'while [ ! -f /aws_dda/model_convertor.py ] || '
        '[ ! -f /aws_dda/convert_model_cleanup.py ] || '
        '[ ! -d /aws_dda/resources_for_copy ]; do\n'
        '  if [ "$waited" -ge "$seed_timeout" ]; then\n'
        '    echo "ERROR: LocalServer did not seed /aws_dda within ${seed_timeout}s '
        '(model_convertor.py absent). Is the LocalServer backend container running? '
        'Failing model startup." >&2\n'
        '    exit 1\n'
        '  fi\n'
        '  echo "Waiting for LocalServer to seed /aws_dda (${waited}s/${seed_timeout}s)..."\n'
        '  sleep 5\n'
        '  waited=$((waited + 5))\n'
        'done\n'
        f'{convertor_cmd}\n'
    )

    recipe = {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': component_version,
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': 'Amazon Lookout for Vision',
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'Autostart': False,
                'PYTHONPATH': '/usr/bin/python3.9',
                'ModelName': friendly_name
            }
        },
        'ComponentDependencies': {
            local_server_component: {
                'VersionRequirement': '^1.0.0',
                'DependencyType': 'HARD'
            }
        },
        'Manifests': [
            {
                'Platform': {
                    'os': 'linux',
                    'architecture': platform
                },
                'Lifecycle': {
                    'Startup': {
                        'Script': startup_script,
                        # Bumped from 900 -> 1800 so the bounded seed-wait (up to
                        # 600s) plus the model conversion both fit within the
                        # Startup timeout.
                        'Timeout': 1800,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    },
                    'Shutdown': {
                        'Script': f'python3 /aws_dda/convert_model_cleanup.py --model_name {component_name}',
                        'Timeout': 900,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    }
                },
                'Artifacts': [
                    {
                        'Uri': artifact_s3_uri,
                        'Digest': '',  # Greengrass will calculate
                        'Algorithm': 'SHA-256',
                        'Unarchive': 'ZIP',
                        'Permission': {
                            'Read': 'ALL',
                            'Execute': 'ALL'
                        }
                    }
                ]
            }
        ],
        'Lifecycle': {}
    }
    
    return recipe


# ── vLLM publish branch — pure pieces ───────────────────────────────────────
# The predicate and the safe-name transform mirror packaging.py. This Lambda
# is bundled with the shared layer only; importing packaging would drag in its
# module-level dependencies (e.g. yaml), so the two small pure helpers are
# duplicated here instead.

# JetPack 5 vLLM support is feature-flagged (design: JP5_VLLM_ENABLED, default
# off). Mirrors packaging.py: the catalog flag in workflow_core.catalog.models
# is the source of truth mirrored by this env var at deploy time.
JP5_VLLM_ENABLED = os.environ.get('JP5_VLLM_ENABLED', 'false').lower() == 'true'


def vllm_supported_architectures() -> list:
    """Supported Target_Architecture set for vLLM_Model_Components:
    always arm64_jp6 and arm64_jp7, arm64_jp5 only when JP5 support is
    flagged on, never arm64_jp4 (2.5). Mirrors
    packaging.vllm_supported_architectures."""
    archs = ['arm64_jp6', 'arm64_jp7']
    if JP5_VLLM_ENABLED:
        archs.append('arm64_jp5')
    return archs


# Reverse of packaging.VLLM_ARCH_TO_TARGET: packaging target -> the single
# Target_Architecture that target serves. KEEP IN SYNC with
# packaging.VLLM_ARCH_TO_TARGET (same mirrored-pure-helper convention as
# vllm_supported_architectures / _safe_model_name above — this Lambda is
# bundled with the shared layer only, so packaging cannot be imported).
#
# Each Per_JetPack_Component advertises exactly ONE architecture, so its
# recipe's supported_architectures comes from here rather than from the
# record-wide set (design step 3). A target absent from this map fails closed
# with PublishError rather than advertising a guess.
VLLM_TARGET_TO_ARCH = {
    'jetson-xavier-jp5': 'arm64_jp5',
    'jetson-xavier-jp6': 'arm64_jp6',
    'jetson-xavier-jp7': 'arm64_jp7',
}


def resolve_vllm_target_architecture(target: str) -> str:
    """The single Target_Architecture a vLLM packaging target serves.

    Fails closed with PublishError for an unmapped target: a Per_JetPack
    component must never advertise an architecture guessed from the
    record-wide set (design step 3, Requirement 2.3).
    """
    arch = VLLM_TARGET_TO_ARCH.get(target)
    if not arch:
        raise PublishError(
            f"Cannot resolve a vLLM target architecture for compile target "
            f"'{target}': not mapped in VLLM_TARGET_TO_ARCH (supported: "
            f"{sorted(VLLM_TARGET_TO_ARCH)})"
        )
    return arch


def is_vllm_record(training_job: Dict) -> bool:
    """True if this model record is a vLLM_Model_Record (LLM served through
    the Triton vLLM backend) rather than a vision model."""
    return training_job.get('source') == 'vllm' or \
        str(training_job.get('model_type', '')).lower() == 'vllm'


def _safe_model_name(model_name: str) -> str:
    """Sanitized model name — the same transform _trigger_component_creation
    (packaging.py) uses, so the component name matches the repository
    directory the packager generated. Delegates to the shared single source
    of truth (model_naming.safe_model_name, vllm-model-name-mismatch 2.2)."""
    return safe_model_name(model_name)


def derive_vllm_component_name(model_name: str) -> str:
    """vLLM component naming convention: `model-vllm-{safe_model_name}`.

    Passes the existing `model-` prefix validation; the `-vllm-` infix is the
    deployment-side discriminator that triggers the vLLM architecture gate
    (Requirement 2.4, design section 3).
    """
    return f"model-vllm-{_safe_model_name(model_name)}"


def existing_component_versions(greengrass, component_name: str) -> set:
    """Every registered version string of ``component_name`` in the Use_Case
    account, or an empty set when the component does not exist yet.

    Mirrors ``workflow_packaging._existing_component_versions``: resolve the
    component ARN via ``list_components(scope='PRIVATE')`` paging (no account
    id needed), then page ``list_component_versions``. A ``ClientError`` at
    either step warns and degrades to an empty set — identical degradation to
    the workflow packager, so a transient listing failure never blocks a
    publish (Requirement 2.9).
    """
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
        logger.warning('Could not list components while resolving next '
                       'version for %s: %s', component_name, e)
        return set()
    if not arn:
        return set()
    versions = set()
    try:
        for page in greengrass.get_paginator(
                'list_component_versions').paginate(arn=arn):
            for v in page.get('componentVersions', []):
                if v.get('componentVersion'):
                    versions.add(v['componentVersion'])
    except ClientError as e:
        logger.warning('Could not list versions of %s: %s', component_name, e)
    return versions


def next_major_from_versions(versions) -> str:
    """Pure: the next free MAJOR-only version over ``versions``.

    ``f"{1 + max major}.0.0"``, i.e. ``1.0.0`` when nothing exists and
    ``N+1.0.0`` otherwise. Unparseable entries are ignored (Requirement
    2.10)."""
    highest = 0
    for version in versions or ():
        match = re.match(r'^(\d+)\.', str(version))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{highest + 1}.0.0"


def next_vllm_component_version(greengrass, component_names) -> str:
    """Next vLLM component version: ONE shared `N.0.0` strictly above every
    version that actually EXISTS in Greengrass for the Per_JetPack_Component
    names this publish will register (Requirements 2.9, 2.10).

    The record's own publish history is deliberately NOT consulted. A failed
    attempt writes no publish state (the atomicity gate keeps the record
    retryable), so history-derived versions were a constant `1.0.0` and any
    orphan version left cloud-side by a denied rollback wedged every retry.
    Deriving from Greengrass itself — the same
    ``_existing_component_versions`` / ``next_component_version`` pattern
    ``workflow_packaging.py`` uses — makes the derived version dominate
    everything registered, whatever the record remembers.
    """
    versions = set()
    for name in component_names or ():
        versions |= existing_component_versions(greengrass, name)
    return next_major_from_versions(versions)


def _artifact_unarchive_stem(artifact_uri: str) -> str:
    """Directory name an unarchived Greengrass artifact gets under
    {artifacts:decompressedPath}: the artifact filename with its archive
    extension stripped."""
    filename = str(artifact_uri).rstrip('/').split('/')[-1]
    for suffix in ('.tar.gz', '.tgz', '.zip', '.tar'):
        if filename.lower().endswith(suffix):
            return filename[:-len(suffix)]
    return filename


def generate_vllm_component_recipe(
    component_name: str,
    component_version: str,
    friendly_name: str,
    platform: str,
    artifact_s3_uri: str,
    repo_unarchived_path: str,
    model_name: str,
    target: str = None,
    s3_model_artifact: str = None,
    supported_architectures: list = None
) -> Dict:
    """
    Generate the Greengrass component recipe for a vLLM_Model_Component.

    Pure — mirrors generate_component_recipe: HARD dependency on the target's
    LocalServer variant, the same bounded /aws_dda seed-wait Startup gate
    (here gating on vllm_model_prep.py, seeded by cp_model_conversion_files
    exactly like model_convertor.py), then vllm_model_prep.py stages the
    unarchived Triton_vLLM_Repository and requests the model load. Shutdown
    runs vllm_model_prep.py --cleanup (unstage + unload). Nothing in the
    lifecycle restarts LocalServer (Requirement 2.7).

    For S3-sourced records the S3_Model_Artifact is declared as a second
    Unarchive artifact and its decompressed path is passed to the prep script
    as --weights_path, where the './weights' sentinel in model.json is
    rewritten device-side (Requirement 2.2).

    The component's supported Target_Architecture set and `runtime: 'vllm'`
    are recorded (informational) in ComponentConfiguration.DefaultConfiguration
    (Requirement 2.4, design section 3).
    """
    local_server_component = resolve_local_server_component(target, platform)
    if supported_architectures is None:
        supported_architectures = vllm_supported_architectures()

    prep_cmd = (
        f'python3 /aws_dda/vllm_model_prep.py '
        f'--unarchived_repo_path {{artifacts:decompressedPath}}/{repo_unarchived_path}/ '
    )
    if s3_model_artifact:
        weights_stem = _artifact_unarchive_stem(s3_model_artifact)
        prep_cmd += (
            f'--weights_path {{artifacts:decompressedPath}}/{weights_stem}/ '
        )
    prep_cmd += f'--model_name {model_name} --component_name {component_name}'

    # Same seed-wait rationale as generate_component_recipe: the HARD
    # dependency only guarantees LocalServer's lifecycle STARTED; the backend
    # container seeds /aws_dda (cp_model_conversion_files) seconds-to-minutes
    # later. Poll (bounded) for vllm_model_prep.py before invoking it.
    startup_script = (
        '#!/bin/bash\n'
        '# Wait for the LocalServer backend to seed the host model-preparation\n'
        '# scripts onto /aws_dda (cp_model_conversion_files) before running them.\n'
        'seed_timeout=600\n'
        'waited=0\n'
        'while [ ! -f /aws_dda/vllm_model_prep.py ]; do\n'
        '  if [ "$waited" -ge "$seed_timeout" ]; then\n'
        '    echo "ERROR: LocalServer did not seed /aws_dda within ${seed_timeout}s '
        '(vllm_model_prep.py absent). Is the LocalServer backend container running? '
        'Failing model startup." >&2\n'
        '    exit 1\n'
        '  fi\n'
        '  echo "Waiting for LocalServer to seed /aws_dda (${waited}s/${seed_timeout}s)..."\n'
        '  sleep 5\n'
        '  waited=$((waited + 5))\n'
        'done\n'
        f'{prep_cmd}\n'
    )

    artifacts = [
        {
            'Uri': artifact_s3_uri,
            'Digest': '',  # Greengrass will calculate
            'Algorithm': 'SHA-256',
            'Unarchive': 'ZIP',
            'Permission': {
                'Read': 'ALL',
                'Execute': 'ALL'
            }
        }
    ]
    if s3_model_artifact:
        # Second Unarchive artifact: the LLM weights archive. Greengrass
        # decompresses it under {artifacts:decompressedPath}; the prep
        # script's --weights_path points at it (2.2).
        artifacts.append({
            'Uri': s3_model_artifact,
            'Digest': '',  # Greengrass will calculate
            'Algorithm': 'SHA-256',
            'Unarchive': 'ZIP',
            'Permission': {
                'Read': 'ALL',
                'Execute': 'ALL'
            }
        })

    recipe = {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': component_version,
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': 'Amazon Lookout for Vision',
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'Autostart': False,
                'ModelName': friendly_name,
                'runtime': 'vllm',
                'supported_architectures': list(supported_architectures)
            }
        },
        'ComponentDependencies': {
            local_server_component: {
                'VersionRequirement': '^1.0.0',
                'DependencyType': 'HARD'
            }
        },
        'Manifests': [
            {
                'Platform': {
                    'os': 'linux',
                    'architecture': platform
                },
                'Lifecycle': {
                    'Startup': {
                        'Script': startup_script,
                        # Seed-wait (up to 600s) + repository staging both fit.
                        'Timeout': 1800,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    },
                    'Shutdown': {
                        'Script': (
                            f'python3 /aws_dda/vllm_model_prep.py --cleanup '
                            f'--model_name {model_name} '
                            f'--component_name {component_name}'
                        ),
                        'Timeout': 900,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    }
                },
                'Artifacts': artifacts
            }
        ],
        'Lifecycle': {}
    }

    return recipe


def publish_component(event: Dict, context: Any) -> Dict:
    """
    Publish Greengrass component
    POST /api/v1/training/{training_id}/publish
    
    Request body:
    {
        "component_name": "model-defect-classifier",
        "component_version": "1.0.0",
        "friendly_name": "Defect Classifier",  // Optional
        "targets": ["jetson-xavier", "x86_64-cpu"]  // Optional, defaults to all packaged
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['component_name', 'component_version']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        component_name = body['component_name']
        component_version = body['component_version']
        friendly_name = body.get('friendly_name', component_name)
        requested_targets = body.get('targets')
        
        # Validate component name
        if not validate_component_name(component_name):
            return create_response(400, {
                'error': 'Component name must start with "model-" (e.g., model-defect-classifier)'
            })
        
        # Validate component version
        if not validate_component_version(component_version):
            return create_response(400, {
                'error': 'Component version must be in format x.0.0 (e.g., 1.0.0, 2.0.0)'
            })
        
        # Get training job details
        training_job = get_training_job_details(training_id)
        usecase_id = training_job['usecase_id']
        
        # Check user access (DataScientist role required)
        # Allow 'system' user for auto-triggered publishing
        if user_id != 'system' and not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Check packaged components exist
        packaged_components = training_job.get('packaged_components', [])
        if not packaged_components:
            return create_response(400, {
                'error': 'No packaged components found. Run packaging first.'
            })
        
        # Filter to successfully packaged components
        packaged = [c for c in packaged_components if c.get('status') == 'packaged']
        if not packaged:
            return create_response(400, {'error': 'No successfully packaged components found'})
        
        # Filter by requested targets if specified
        if requested_targets:
            packaged = [c for c in packaged if c['target'] in requested_targets]
            if not packaged:
                return create_response(400, {
                    'error': f"No packaged components for requested targets: {requested_targets}"
                })
        
        # Get use case details
        usecase = get_usecase(usecase_id)

        # Create Greengrass client (handles both single-account and
        # multi-account scenarios).
        #
        # Created HERE — above the vLLM version derivation and the preflight
        # fit-check block (design step 6) — because the derived version is now
        # read from the versions that actually exist cloud-side, and the
        # derived name/version must still be available to the fit gate's 422 /
        # skip_fit_check / unverified branches exactly as before. It needs only
        # `usecase`, already fetched. No create_component_version call moves:
        # the fit gate remains the first fail-closed point before any component
        # registration (Requirement 3.12).
        greengrass = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-pub-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )

        # ── vLLM branch: naming and versioning are convention-derived ──────
        # For vLLM_Model_Records the component name and version are not
        # caller-chosen: the base name is model-vllm-{safe_model_name} and the
        # version is the next free N.0.0 over the versions that already exist
        # in Greengrass for the Per_JetPack_Component names this attempt will
        # register (Requirements 2.9, 2.10).
        vllm_record = is_vllm_record(training_job)
        vllm_model_name = None
        vllm_s3_model_artifact = None
        vllm_archs = None
        # Component-version ARNs created during this attempt. On any vLLM
        # publish failure these are rolled back (best effort) so the retry
        # can re-register the same derived N.0.0 without a cloud-side
        # conflict (Requirement 2.6).
        vllm_created_arns = []
        if vllm_record:
            record_model_name = training_job.get('model_name') or component_name
            component_name = derive_vllm_component_name(record_model_name)
            # The Per_JetPack_Component names this attempt will register — the
            # same `f"{base}-{target_suffix}"` composition the target loop
            # derives (design step 2). ONE shared N.0.0 covers all of them, so
            # `model_id = f"{training_id}-{component_version}"`, the record's
            # single `component_version`, and the response shape are preserved.
            vllm_target_component_names = [
                f"{component_name}-{c['target'].replace('_', '-')}"
                for c in packaged
            ]
            component_version = next_vllm_component_version(
                greengrass, vllm_target_component_names)
            vllm_model_name = _safe_model_name(record_model_name)
            model_source = training_job.get('model_source') or {}
            vllm_s3_model_artifact = model_source.get('s3_model_artifact')
            vllm_archs = vllm_supported_architectures()
            logger.info(
                f"vLLM record: publishing as {component_name} "
                f"v{component_version}"
            )

        # ── vLLM preflight Fit_Check gate (Requirements 3.4, 3.6, 3.7) ──────
        # Runs BEFORE any component registration: publish is the moment the
        # configuration becomes deployable, so this is the fail-closed point.
        # If the estimated weights + minimum KV cache exceed the
        # gpu_memory_utilization × Device_Memory_Profile budget on EVERY
        # supported Target_Architecture, the publish fails with 422 and the
        # full sizing findings — unless the request body carries the explicit
        # `skip_fit_check: true` override, which proceeds and is recorded in
        # the audit event. An undeterminable Weight_Estimate never blocks:
        # the publish proceeds with an 'unverified' annotation.
        vllm_fit_check = None
        vllm_fit_overridden = False
        if vllm_record:
            skip_fit_check = body.get('skip_fit_check') is True
            # S3-sourced records size by the artifact object's ContentLength
            # in the Use_Case account (the bucket model_import verified at
            # registration). A client-construction failure degrades to an
            # unverified estimate rather than blocking the publish.
            s3_head = None
            if (training_job.get('model_source') or {}).get('s3_model_artifact'):
                try:
                    s3_head = get_usecase_client('s3', usecase).head_object
                except Exception as e:
                    logger.warning(
                        f"Could not create use-case S3 client for weight "
                        f"estimation (fit check will be unverified): {e}")

            estimate = estimate_weights(training_job, s3_head=s3_head)
            if estimate is None:
                # Requirement 3.4: fit could not be verified — proceed,
                # annotated, never blocking.
                vllm_fit_check = {
                    'status': 'unverified',
                    'estimate': None,
                    'findings': [],
                    'message': (
                        'Model weight size could not be estimated; the fit '
                        'check was skipped and the publish proceeds '
                        'unverified.'),
                }
                logger.warning(
                    f"vLLM fit check unverified for training {training_id}: "
                    f"weight estimate unavailable; proceeding with publish")
            else:
                engine_configuration = \
                    training_job.get('engine_configuration') or {}
                findings = evaluate_fit(
                    engine_configuration, estimate, vllm_archs)
                findings_payload = [asdict(finding) for finding in findings]
                estimate_payload = asdict(estimate)
                every_arch_fails = bool(findings) and \
                    all(not finding.fits for finding in findings)

                if every_arch_fails and not skip_fit_check:
                    # Requirement 3.6: fail the publish before any component
                    # registration. The FitFinding messages carry the full
                    # sizing statement (estimate, configured fraction,
                    # per-architecture budget, and the raise/reduce/shrink
                    # remediation) and the findings array lets the GUI
                    # render them per architecture.
                    failing_messages = ' '.join(
                        finding.message for finding in findings)
                    logger.error(
                        f"vLLM publish blocked by fit check for training "
                        f"{training_id}: {failing_messages}")
                    return create_response(422, {
                        'error': (
                            f"vLLM fit check failed for every supported "
                            f"architecture: {failing_messages}"),
                        'fit_check': {
                            'status': 'failed',
                            'estimate': estimate_payload,
                            'findings': findings_payload,
                        },
                        'training_id': training_id,
                        'component_name': component_name,
                        'component_version': component_version,
                    })

                if every_arch_fails:
                    # Requirement 3.7: explicit skip_fit_check override —
                    # proceed and record the override in the audit event.
                    vllm_fit_overridden = True
                    vllm_fit_check = {
                        'status': 'overridden',
                        'estimate': estimate_payload,
                        'findings': findings_payload,
                        'message': (
                            'Fit check failed for every supported '
                            'architecture but was overridden by '
                            'skip_fit_check.'),
                    }
                    logger.warning(
                        f"vLLM fit check FAILED for every supported "
                        f"architecture but skip_fit_check was supplied; "
                        f"proceeding with publish for training {training_id}")
                else:
                    all_fit = all(finding.fits for finding in findings)
                    vllm_fit_check = {
                        'status': 'passed' if all_fit else 'warnings',
                        'estimate': estimate_payload,
                        'findings': findings_payload,
                    }

        # Publish component for each target
        # Each target gets its own component with target suffix in the name
        published_components = []
        
        for component in packaged:
            target = component['target']
            artifact_s3_uri = component.get('component_package_s3')
            
            if not artifact_s3_uri:
                logger.warning(f"No artifact S3 URI for target {target}, skipping")
                continue
            
            # Determine platform. The authoritative resolution happens inside
            # the try below via resolve_target_platform, which fails closed for
            # a target absent from either module-level map instead of silently
            # defaulting to amd64 (Requirement 2.19). This lookup only seeds
            # the failed-target record's 'platform' field for that case (None
            # when the target is unmapped) — it never feeds a recipe.
            platform = TARGET_TO_PLATFORM.get(target)
            
            # Create unique component name per target
            # e.g., model-defect-classifier-jetson-xavier, model-defect-classifier-x86-64-cpu
            # vLLM records follow the SAME convention (design step 2): the
            # base name model-vllm-{safe_model_name} stays the record's
            # top-level component_name / GSI key and display name, while each
            # packaged target publishes as its own Per_JetPack_Component, so
            # every create_component_version call carries a distinct identity
            # and each component can depend HARD on exactly one JetPack's
            # LocalServer variant (Requirements 2.1, 2.2).
            target_suffix = target.replace('_', '-')
            target_component_name = f"{component_name}-{target_suffix}"

            # Best-effort arch for the per-target record (design step 7): the
            # authoritative resolution stays resolve_vllm_target_architecture
            # inside the try below, which fails closed. A None here only means
            # the entry carries no supported_architectures (an unmapped target
            # is recorded as a failed target anyway). Vision entries never
            # carry an architecture — their write-back shape is unchanged.
            target_arch = VLLM_TARGET_TO_ARCH.get(target) if vllm_record else None
            arch_fields = ({'supported_architectures': [target_arch]}
                           if target_arch else {})
            
            # Extract model unarchived path from S3 URI
            # Format: s3://bucket/model_artifacts/model-uuid/uuid_greengrass_model_component.zip
            model_unarchived_path = artifact_s3_uri.split('/')[-1].replace('.zip', '')
            
            logger.info(f"Publishing component {target_component_name} for target {target} (platform: {platform})")
            
            try:
                # Fail closed BEFORE creating any component version: an
                # unresolved aarch64 target must never be stamped with a
                # bare/ambiguous LocalServer dependency. resolve_target_platform
                # (here) and resolve_local_server_component (called inside the
                # recipe generators) raise PublishError, which is caught below
                # and recorded as a failed target so no component version is
                # created for it.
                platform = resolve_target_platform(target)
                # The derived per-target name must satisfy the Greengrass
                # component-name constraints before anything is generated for
                # it, so an over-long or malformed name is a recorded failed
                # target rather than an opaque API error (Requirement 2.6).
                validate_greengrass_component_name(target_component_name)
                #
                # Generate component recipe with target-specific name
                if vllm_record:
                    # Per_JetPack_Component advertises exactly the ONE
                    # architecture this target serves — not the record-wide
                    # set, which would advertise architectures its single HARD
                    # LocalServer dependency cannot satisfy (Requirement 2.3).
                    target_arch = resolve_vllm_target_architecture(target)
                    recipe = generate_vllm_component_recipe(
                        component_name=target_component_name,
                        component_version=component_version,
                        friendly_name=f"{friendly_name} ({target})",
                        platform=platform,
                        artifact_s3_uri=artifact_s3_uri,
                        repo_unarchived_path=model_unarchived_path,
                        model_name=vllm_model_name,
                        target=target,
                        s3_model_artifact=vllm_s3_model_artifact,
                        supported_architectures=[target_arch]
                    )
                else:
                    recipe = generate_component_recipe(
                        component_name=target_component_name,
                        component_version=component_version,
                        friendly_name=f"{friendly_name} ({target})",
                        platform=platform,
                        artifact_s3_uri=artifact_s3_uri,
                        model_unarchived_path=model_unarchived_path,
                        target=target
                    )
                
                logger.info(f"Creating Greengrass component: {target_component_name} v{component_version}")
                
                # Create component version with portal tag for filtering
                # Tag: dda-portal:managed=true allows filtering via Resource Groups Tagging API
                response = greengrass.create_component_version(
                    inlineRecipe=json.dumps(recipe),
                    tags={
                        'dda-portal:managed': 'true',
                        'dda-portal:usecase-id': usecase_id,
                        'dda-portal:training-id': training_id,
                        'dda-portal:model-name': friendly_name,
                        'dda-portal:created-by': user_id
                    }
                )
                
                component_arn = response['arn']
                logger.info(f"Component created: {component_arn}")
                if vllm_record:
                    vllm_created_arns.append(component_arn)
                
                # Monitor component status until DEPLOYABLE
                max_attempts = 30
                attempt = 0
                component_status = 'REQUESTED'
                
                while attempt < max_attempts and component_status in ['REQUESTED', 'IN_PROGRESS']:
                    time.sleep(2)  # Wait 2 seconds between checks
                    
                    status_response = greengrass.describe_component(arn=component_arn)
                    component_status = status_response['status']['componentState']
                    
                    logger.info(f"Component status: {component_status}")
                    attempt += 1
                
                if component_status == 'DEPLOYABLE':
                    published_components.append({
                        'target': target,
                        'platform': platform,
                        'component_name': target_component_name,
                        'component_version': component_version,
                        'component_arn': component_arn,
                        'status': 'published',
                        **arch_fields
                    })
                    logger.info(f"Component {target_component_name} published successfully for {target}")
                else:
                    error_msg = status_response['status'].get('message', 'Unknown error')
                    published_components.append({
                        'target': target,
                        'platform': platform,
                        'component_name': target_component_name,
                        'component_version': component_version,
                        'status': 'failed',
                        'error': f"Component status: {component_status}. {error_msg}",
                        **arch_fields
                    })
                    logger.error(f"Component {target_component_name} failed to become DEPLOYABLE: {component_status}")
                
            except PublishError as e:
                # Fail-closed resolver: the target does not map to a known
                # LocalServer variant. Record a failed target and move on
                # without creating a component version (localserver-arch-
                # naming Requirement 2.2).
                error_msg = str(e)
                logger.error(
                    f"Cannot publish component {target_component_name} for "
                    f"{target}: {error_msg}")
                published_components.append({
                    'target': target,
                    'platform': platform,
                    'component_name': target_component_name,
                    'component_version': component_version,
                    'status': 'failed',
                    'error': error_msg,
                    **arch_fields
                })
            except ClientError as e:
                error_msg = str(e)
                logger.error(f"Error publishing component {target_component_name} for {target}: {error_msg}")
                published_components.append({
                    'target': target,
                    'platform': platform,
                    'component_name': target_component_name,
                    'component_version': component_version,
                    'status': 'failed',
                    'error': error_msg,
                    **arch_fields
                })
        
        # ── vLLM atomicity gate (Requirements 2.6, 2.9) ─────────────────────
        # A vLLM publish is all-or-nothing: if any target failed (or nothing
        # was publishable at all), roll back every component version created
        # during this attempt, write NO publish state onto the record, and
        # report the failing step so the operation can be retried against a
        # record still in its pre-publish state.
        vllm_failed = [c for c in published_components
                       if c.get('status') != 'published']
        if vllm_record and (vllm_failed or not published_components):
            for arn in vllm_created_arns:
                # Name/version parsed from the ARN so a version that survives
                # a denied or failed delete is identifiable straight from the
                # log line, without correlating against the record (design
                # step 8, Requirement 2.8). Rollback stays best-effort: the
                # try/except-warning shape is unchanged and nothing is raised,
                # so the reported error remains the publish failure.
                rollback_name, rollback_version = parse_component_arn(arn)
                try:
                    greengrass.delete_component(arn=arn)
                    logger.info(
                        f"Rolled back component version "
                        f"{rollback_name} v{rollback_version} ({arn})")
                except Exception as cleanup_error:
                    logger.warning(
                        f"Rollback of component version "
                        f"{rollback_name} v{rollback_version} ({arn}) failed; "
                        f"it may still exist cloud-side and need manual "
                        f"cleanup: {cleanup_error}")

            failure_audit_details = {
                'component_name': component_name,
                'component_version': component_version,
                'runtime': 'vllm',
                'failed_targets': [c.get('target') for c in vllm_failed],
            }
            if vllm_fit_overridden:
                # Requirement 3.7: the skip_fit_check override is recorded
                # on whichever audit event this attempt produces.
                failure_audit_details['skip_fit_check'] = True
            log_audit_event(
                user_id=user_id,
                action='publish_greengrass_component',
                resource_type='training_job',
                resource_id=training_id,
                result='failure',
                details=failure_audit_details
            )
            error_detail = (
                '; '.join(
                    f"{c.get('target')}: {c.get('error', 'unknown error')}"
                    for c in vllm_failed)
                or 'no packaged artifact available to publish'
            )
            logger.error(
                f"vLLM publish failed for training {training_id}; record "
                f"left in pre-publish state (retryable): {error_detail}")
            return create_response(502, {
                'error': f'vLLM component publish failed: {error_detail}',
                'failed_step': 'greengrass_registration',
                'training_id': training_id,
                'component_name': component_name,
                'component_version': component_version,
                'published_components': published_components,
                'retryable': True
            })

        # Store published components in Models table
        if published_components:
            models_table = dynamodb.Table(MODELS_TABLE)
            timestamp = int(datetime.utcnow().timestamp() * 1000)
            
            # Create model record
            model_id = f"{training_id}-{component_version}"
            
            # Build component ARNs map
            component_arns = {}
            for comp in published_components:
                if comp['status'] == 'published':
                    component_arns[comp['target']] = comp['component_arn']
            
            model_item = {
                'model_id': model_id,
                'usecase_id': usecase_id,
                'name': component_name,
                'version': component_version,
                'stage': 'candidate',
                'training_job_id': training_id,
                'dataset_manifest_id': training_job.get('dataset_manifest_s3', ''),
                'metrics': training_job.get('metrics', {}),
                'component_arns': component_arns,
                'deployed_devices': [],
                'created_by': user_id,
                'created_at': timestamp
            }
            
            models_table.put_item(Item=model_item)
            logger.info(f"Model record created: {model_id}")
        
        # Update training job with published components
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)

        if vllm_record:
            # Publish metadata write-back (Requirements 2.4, 2.9, 2.5): the
            # record's published_component map carries the component
            # name/version, the record-wide supported Target_Architecture
            # union, and the vLLM runtime discriminator — every one of those
            # keys is retained for legacy readers — PLUS a `components` list
            # with one entry per Per_JetPack_Component. The top-level
            # component_name attribute stays the UNSUFFIXED base name, which
            # is the key the deployment gate's component_name-index GSI
            # resolves the record by (task 5.2), so N components still resolve
            # to ONE record from ONE string and no index change is needed.
            component_arns = {
                comp['target']: comp['component_arn']
                for comp in published_components
                if comp['status'] == 'published'
            }
            # One entry per Per_JetPack_Component (design step 7,
            # Requirement 2.5): each carries its OWN suffixed component name
            # and the single architecture it serves, so the deployment gate can
            # resolve a per-JetPack component to exactly [arch] instead of the
            # record-wide union. The record-level keys above/below are all
            # retained unchanged for legacy readers.
            published_component_entries = [
                {
                    'component_name': comp['component_name'],
                    'component_version': comp['component_version'],
                    'target': comp['target'],
                    'architecture': comp['supported_architectures'][0],
                    'supported_architectures': list(
                        comp['supported_architectures']),
                    'component_arn': comp['component_arn'],
                }
                for comp in published_components
                if comp['status'] == 'published'
                and comp.get('supported_architectures')
            ]
            published_component_map = {
                'component_name': component_name,
                'component_version': component_version,
                'supported_architectures': vllm_archs,
                'runtime': 'vllm',
                'component_arns': component_arns,
                'components': published_component_entries,
                'published_at': timestamp
            }
            table.update_item(
                Key={'training_id': training_id},
                UpdateExpression=(
                    'SET published_components = :components, '
                    'published_component = :published_component, '
                    'component_name = :component_name, '
                    'published = :published, '
                    'updated_at = :updated'
                ),
                ExpressionAttributeValues={
                    ':components': published_components,
                    ':published_component': published_component_map,
                    ':component_name': component_name,
                    ':published': True,
                    ':updated': timestamp
                }
            )
        else:
            table.update_item(
                Key={'training_id': training_id},
                UpdateExpression='SET published_components = :components, updated_at = :updated',
                ExpressionAttributeValues={
                    ':components': published_components,
                    ':updated': timestamp
                }
            )
        
        # Log audit event
        success_audit_details = {
            'component_name': component_name,
            'component_version': component_version,
            'targets': [c['target'] for c in published_components],
            'published_count': len([c for c in published_components if c['status'] == 'published'])
        }
        if vllm_fit_overridden:
            # Requirement 3.7: record the skip_fit_check override in the
            # audit event for the publish it allowed to proceed.
            success_audit_details['skip_fit_check'] = True
        log_audit_event(
            user_id=user_id,
            action='publish_greengrass_component',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details=success_audit_details
        )
        
        success_count = len([c for c in published_components if c['status'] == 'published'])
        logger.info(f"Published {success_count}/{len(published_components)} components for training {training_id}")
        
        success_body = {
            'training_id': training_id,
            'component_name': component_name,
            'component_version': component_version,
            'published_components': published_components,
            'message': f'Published {success_count} component(s) successfully'
        }
        if vllm_fit_check is not None:
            # Fit_Check annotation (Requirements 3.4, 3.7): 'unverified'
            # when the estimate could not be determined, 'overridden' when
            # skip_fit_check bypassed an all-architecture failure.
            success_body['fit_check'] = vllm_fit_check
        return create_response(200, success_body)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error publishing component: {str(e)}")
        return create_response(500, {'error': f"Failed to publish component: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler"""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
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
        
        # Route to appropriate handler
        if http_method == 'POST' and '/publish' in path:
            return publish_component(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
