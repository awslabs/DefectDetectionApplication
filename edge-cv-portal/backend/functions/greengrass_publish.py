"""
Greengrass component publishing Lambda functions
Implements component creation and publishing to AWS IoT Greengrass
Based on DDA_Greengrass_Component_Creator.ipynb Phase 3
"""
import json
import os
import logging
from dataclasses import asdict
from typing import Dict, Any, Optional
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
    'arm64-cpu': JP4_LOCAL_SERVER,                               # arm64 CPU -> JP4 baseline
    'x86_64-cpu': _AMD64_LOCAL_SERVER,
    'x86_64-cuda': _AMD64_LOCAL_SERVER,
}

# Target to platform mapping
TARGET_TO_PLATFORM = {
    'jetson-xavier': 'aarch64',
    'jetson-xavier-jp5': 'aarch64',
    'jetson-xavier-jp6': 'aarch64',
    'arm64-cpu': 'aarch64',
    'x86_64-cpu': 'amd64',
    'x86_64-cuda': 'amd64'
}


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
        f"x86_64-cpu, x86_64-cuda, arm64-cpu)."
    )


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


def next_vllm_component_version(training_job: Dict) -> str:
    """Next vLLM component version for a record: `N.0.0` with N = 1 + the
    highest previously published N for this record (Requirement 2.4).

    Scans every version recorded on the record's publish history
    (`published_components` entries and, once task 4.2 lands, the
    `published_component` map), so the derived version is strictly greater
    than everything already sent to Greengrass — including versions from
    failed attempts that may still exist as component versions cloud-side.
    """
    highest = 0
    candidates = []
    published_list = training_job.get('published_components') or []
    if isinstance(published_list, list):
        candidates.extend(
            entry.get('component_version')
            for entry in published_list if isinstance(entry, dict)
        )
    published_map = training_job.get('published_component') or {}
    if isinstance(published_map, dict):
        candidates.append(published_map.get('component_version'))
    for version in candidates:
        match = re.match(r'^(\d+)\.', str(version or ''))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{highest + 1}.0.0"


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
        
        # ── vLLM branch: naming and versioning are convention-derived ──────
        # For vLLM_Model_Records the component name and version are not
        # caller-chosen: the name is model-vllm-{safe_model_name} and the
        # version is N.0.0 with N = 1 + the highest previously published N
        # for this record (Requirement 2.4).
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
            component_version = next_vllm_component_version(training_job)
            vllm_model_name = _safe_model_name(record_model_name)
            model_source = training_job.get('model_source') or {}
            vllm_s3_model_artifact = model_source.get('s3_model_artifact')
            vllm_archs = vllm_supported_architectures()
            logger.info(
                f"vLLM record: publishing as {component_name} "
                f"v{component_version}"
            )

        # Get use case details
        usecase = get_usecase(usecase_id)

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

        # Create Greengrass client (handles both single-account and multi-account scenarios)
        greengrass = get_usecase_client(
            'greengrassv2',
            usecase,
            session_name=f"gg-pub-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Publish component for each target
        # Each target gets its own component with target suffix in the name
        published_components = []
        
        for component in packaged:
            target = component['target']
            artifact_s3_uri = component.get('component_package_s3')
            
            if not artifact_s3_uri:
                logger.warning(f"No artifact S3 URI for target {target}, skipping")
                continue
            
            # Determine platform
            platform = TARGET_TO_PLATFORM.get(target, 'amd64')
            
            # Create unique component name per target
            # e.g., model-defect-classifier-jetson-xavier, model-defect-classifier-x86-64-cpu
            # vLLM components keep the convention-fixed name with no target
            # suffix: model-vllm-{safe_model_name} is the deployment gate's
            # discriminator and (task 4.2) the GSI lookup key.
            target_suffix = target.replace('_', '-')
            if vllm_record:
                target_component_name = component_name
            else:
                target_component_name = f"{component_name}-{target_suffix}"
            
            # Extract model unarchived path from S3 URI
            # Format: s3://bucket/model_artifacts/model-uuid/uuid_greengrass_model_component.zip
            model_unarchived_path = artifact_s3_uri.split('/')[-1].replace('.zip', '')
            
            logger.info(f"Publishing component {target_component_name} for target {target} (platform: {platform})")
            
            try:
                # Fail closed BEFORE creating any component version: an
                # unresolved aarch64 target must never be stamped with a
                # bare/ambiguous LocalServer dependency. resolve_local_server_
                # component (called inside the recipe generators) raises
                # PublishError, which is caught below and recorded as a failed
                # target so no component version is created for it.
                #
                # Generate component recipe with target-specific name
                if vllm_record:
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
                        supported_architectures=vllm_archs
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
                        'status': 'published'
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
                        'error': f"Component status: {component_status}. {error_msg}"
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
                    'error': error_msg
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
                    'error': error_msg
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
                try:
                    greengrass.delete_component(arn=arn)
                    logger.info(f"Rolled back component version: {arn}")
                except Exception as cleanup_error:
                    logger.warning(
                        f"Rollback of component version {arn} failed "
                        f"(may need manual cleanup): {cleanup_error}")

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
            # Publish metadata write-back (Requirements 2.4, 2.9): the
            # record's published_component map carries the component
            # name/version, the supported Target_Architecture set, and the
            # vLLM runtime discriminator; the top-level component_name
            # attribute is the key the deployment gate's
            # component_name-index GSI resolves the record by (task 5.2).
            component_arns = {
                comp['target']: comp['component_arn']
                for comp in published_components
                if comp['status'] == 'published'
            }
            published_component_map = {
                'component_name': component_name,
                'component_version': component_version,
                'supported_architectures': vllm_archs,
                'runtime': 'vllm',
                'component_arns': component_arns,
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
