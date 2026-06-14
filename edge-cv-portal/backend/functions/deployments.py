"""
Deployments handler for Edge CV Portal
Manages Greengrass deployments to edge devices
"""
import json
import logging
import os
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, get_usecase,
    get_usecase_client, get_usecase_region
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE', 'dda-portal-deployments')

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


def handler(event, context):
    """
    Handle deployment management requests
    
    GET /api/v1/deployments              - List deployments
    GET /api/v1/deployments/{id}         - Get deployment details
    POST /api/v1/deployments             - Create deployment
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
            return list_deployments(user, query_parameters)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_deployment(path_parameters['id'], user, query_parameters)
        elif http_method == 'POST':
            body = json.loads(event.get('body') or '{}')
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
            
            components_map['aws.greengrass.LogManager'] = {
                'componentVersion': LOG_MANAGER_VERSION,
                'configurationUpdate': {
                    'merge': json.dumps(log_manager_config)
                }
            }
            auto_included.append({
                'component_name': 'aws.greengrass.LogManager',
                'component_version': LOG_MANAGER_VERSION,
                'reason': 'Required for CloudWatch logging from devices'
            })
            logger.info(f"Auto-included aws.greengrass.LogManager {LOG_MANAGER_VERSION} with logging for components: {list(component_log_config_map.keys())}")
        
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
        
        # Determine target ARN
        # Greengrass deployments must target thing groups, not individual things
        iot_client = get_usecase_client(
            'iot',
            usecase,
            session_name=f"iot-deploy-{user['user_id'][:20]}-{int(datetime.utcnow().timestamp())}"[:64],
            region=region
        )
        
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
        # target component"). However, pinning to the LATEST Nucleus breaks other
        # auto-included components: e.g. aws.greengrass.LogManager requires Nucleus
        # < 2.15.0, so pinning to 2.17.0 produces a NoAvailableComponentVersion
        # conflict.
        #
        # The safe choice is to pin Nucleus to the version the device is already
        # running. That version is, by definition, already installed and compatible
        # with the device, it satisfies the "explicit top-level component"
        # requirement, and it avoids forcing an incompatible upgrade. We only fall
        # back to the latest version (or unpinned) if the running version can't be
        # determined.
        if needs_nucleus and 'aws.greengrass.Nucleus' not in components_map:
            running_nucleus = resolve_target_running_nucleus(
                greengrass_client, iot_client, target_devices,
                target_thing_group, region, account_id
            )
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
