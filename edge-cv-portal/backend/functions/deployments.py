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

# Minimum LocalServer component version a Workflow_Component requires
# (Requirement 8.4). The Component_Packager writes this same value into each
# packaged component's manifest.json (minLocalServerVersion) from the same
# environment configuration, so resolving it here matches the packaged
# manifests. A per-version override recorded on the WorkflowVersions item
# (min_local_server_version) takes precedence when present.
WORKFLOW_MIN_LOCAL_SERVER_VERSION = os.environ.get(
    'WORKFLOW_MIN_LOCAL_SERVER_VERSION',
    os.environ.get('DDA_LOCAL_SERVER_VERSION', '1.0.0'))

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

# Components that require Nucleus to be explicitly included in deployment
# Model components (model-*) also need Nucleus since they depend on DDA components
DDA_COMPONENTS_REQUIRING_NUCLEUS = [
    'aws.edgeml.dda.LocalServer',
    'aws.edgeml.dda.InferenceApp',
    'model-',  # Model components created by the portal
]


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


def resolve_log_manager_version(greengrass_client, region, running_nucleus):
    """Return the newest aws.greengrass.LogManager version whose Nucleus
    dependency is satisfied by the device's running Nucleus.

    LogManager declares a Nucleus VersionRequirement (e.g. '>=2.1.0 <2.15.0').
    Auto-including a LogManager whose ceiling excludes the device's pinned
    Nucleus produces FAILED_NO_STATE_CHANGE / NoAvailableComponentVersion. We
    therefore select the highest LogManager version compatible with
    `running_nucleus`. Falls back to the static LOG_MANAGER_VERSION when the
    running Nucleus is unknown or resolution fails (network/permission), so the
    behavior is never worse than the previous hard-coded pin."""
    if not running_nucleus:
        return LOG_MANAGER_VERSION

    lm_arn = f"arn:aws:greengrass:{region}:aws:components:aws.greengrass.LogManager"
    candidates = []
    try:
        paginator = greengrass_client.get_paginator('list_component_versions')
        for page in paginator.paginate(arn=lm_arn):
            for cv in page.get('componentVersions', []):
                v = cv.get('componentVersion')
                if v:
                    candidates.append(v)
    except Exception as e:
        logger.warning(f"Could not list LogManager versions, using {LOG_MANAGER_VERSION}: {e}")
        return LOG_MANAGER_VERSION

    # Highest LogManager version first; return the first one compatible with the
    # device's running Nucleus.
    for lm_version in sorted(candidates, key=_version_key, reverse=True):
        try:
            resp = greengrass_client.get_component(
                arn=f"{lm_arn}:versions:{lm_version}",
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
                    f"Resolved LogManager {lm_version} (Nucleus requirement "
                    f"'{nucleus_req}') for running Nucleus {running_nucleus}"
                )
                return lm_version
        except Exception as e:
            logger.warning(f"Could not inspect LogManager {lm_version}: {e}")
            continue

    logger.warning(
        f"No LogManager version found compatible with Nucleus {running_nucleus}; "
        f"falling back to {LOG_MANAGER_VERSION}"
    )
    return LOG_MANAGER_VERSION


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
            if running_nucleus:
                components_map['aws.greengrass.Nucleus'] = {
                    'componentVersion': running_nucleus
                }
                auto_included.append({
                    'component_name': 'aws.greengrass.Nucleus',
                    'component_version': running_nucleus,
                    'reason': 'Pinned to the version already running on the device to satisfy the explicit-nucleus requirement without forcing an incompatible upgrade'
                })
                logger.info(f"Pinned aws.greengrass.Nucleus to running device version {running_nucleus}")
            else:
                # Could not read the running version. Fall back to including Nucleus
                # without a pinned version so Greengrass resolves a compatible one
                # itself (respecting all component constraints).
                components_map['aws.greengrass.Nucleus'] = {}
                auto_included.append({
                    'component_name': 'aws.greengrass.Nucleus',
                    'component_version': 'auto',
                    'reason': 'Included as top-level component (unpinned) so Greengrass resolves a compatible Nucleus version'
                })
                logger.info("Auto-included aws.greengrass.Nucleus (unpinned) as top-level component")
        
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
        
        return create_response(201, {
            'deployment_id': deployment_id,
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'components': deployed_components,
            'auto_included': auto_included,
            'is_revision': is_revision,
            'superseded_deployment_id': existing_deployment.get('deploymentId') if is_revision else None,
            'message': 'Deployment updated successfully' if is_revision else 'Deployment created successfully'
        })
        
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


def get_device_local_server_version(greengrass_client, thing_name):
    """
    The LocalServer component version installed on a core device
    (components are named aws.edgeml.dda.LocalServer.<arch>), or None when
    no LocalServer component is reported installed.

    Raises ClientError when the installed components cannot be listed, so
    the caller can distinguish "no LocalServer" from "could not determine".
    """
    paginator = greengrass_client.get_paginator('list_installed_components')
    for page in paginator.paginate(coreDeviceThingName=thing_name):
        for comp in page.get('installedComponents', []):
            name = comp.get('componentName', '')
            if name.startswith(LOCAL_SERVER_COMPONENT_PREFIX):
                return comp.get('componentVersion')
    return None


def check_local_server_compatibility(greengrass_client, thing_names,
                                     min_local_server_version):
    """
    Pre-submit compatibility check (Requirement 8.4): compare each target
    device's installed LocalServer component version against the
    Workflow_Component's minLocalServerVersion. Returns the list of
    incompatible devices, each with a clear reason; an empty list means
    every checked device is compatible.
    """
    incompatible = []
    min_key = _version_key(min_local_server_version)
    for thing_name in thing_names:
        try:
            installed = get_device_local_server_version(greengrass_client, thing_name)
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
        if installed is None:
            incompatible.append({
                'device': thing_name,
                'local_server_version': None,
                'min_local_server_version': min_local_server_version,
                'reason': 'No LocalServer component is installed on this device'
            })
        elif _version_key(installed) < min_key:
            incompatible.append({
                'device': thing_name,
                'local_server_version': installed,
                'min_local_server_version': min_local_server_version,
                'reason': (f'Installed LocalServer version {installed} is older '
                           f'than the required minimum {min_local_server_version}')
            })
    return incompatible


def record_workflow_deployment(deployment_id, usecase_id, workflow_id,
                               workflow_version, target_arn, target_devices,
                               target_thing_group, is_revision,
                               superseded_deployment_id, user):
    """
    Record the workflow version -> deployment -> devices association in the
    Deployments table (Requirement 8.2). component_type: 'workflow' plus
    workflow_id is the shape workflows.py's delete flow matches on (5.6).
    """
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    item = {
        'deployment_id': deployment_id,
        'usecase_id': usecase_id,
        'component_type': 'workflow',
        'workflow_id': workflow_id,
        'workflow_version': int(workflow_version),
        'component_name': workflow_component_name(workflow_id),
        'component_version': workflow_component_version(workflow_version),
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
    dynamodb.Table(DEPLOYMENTS_TABLE).put_item(Item=item)
    return item


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
        min_local_server = (version_item.get('min_local_server_version')
                            or WORKFLOW_MIN_LOCAL_SERVER_VERSION)
        resolved_devices = resolve_target_thing_names(
            iot_client, target_devices, target_thing_group)
        incompatible = check_local_server_compatibility(
            greengrass_client, resolved_devices, min_local_server)
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

        component_name = workflow_component_name(workflow_id)
        component_version = workflow_component_version(workflow_version)
        # Setting the entry (re)places the workflow component at the new
        # version; Greengrass replaces the older version on the device (8.5).
        components_map[component_name] = {'componentVersion': component_version}

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

        response = greengrass_client.create_deployment(**deployment_params)
        deployment_id = response.get('deploymentId')

        # Association record: workflow version -> deployment -> devices (8.2)
        superseded_id = existing_deployment.get('deploymentId') if is_revision else None
        record_workflow_deployment(
            deployment_id, usecase_id, workflow_id, workflow_version,
            target_arn, resolved_devices, target_thing_group,
            is_revision, superseded_id, user)

        # Audit log entry for deploy (Requirement 11.5)
        log_audit_event(
            user['user_id'], 'deploy_workflow', 'workflow', workflow_id,
            'success', {
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
        )

        logger.info(
            f"{'Revised' if is_revision else 'Created'} workflow deployment "
            f"{deployment_id} for workflow {workflow_id} v{workflow_version}")

        return create_response(201, {
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
            'message': ('Workflow deployment updated successfully' if is_revision
                        else 'Workflow deployment created successfully')
        })

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
