"""
Devices handler for Edge CV Portal
Queries IoT Core Things tagged with dda-portal:managed=true
"""
import json
import logging
import os
import boto3
from botocore.exceptions import ClientError
from datetime import datetime

from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, assume_cross_account_role, get_usecase,
    create_boto3_client, get_usecase_client, rbac_manager, Permission
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# Devices table: portal-recorded device attributes — the UseCaseAdmin-set
# `test_device` flag and the device's recorded Target_Architecture
# (custom-node-designer, Requirements 9.7, 9.8, 16.3, 16.6). The live
# Greengrass/IoT state continues to come from the Use_Case account.
DEVICES_TABLE = os.environ.get('DEVICES_TABLE')

# DDA Target_Architectures a device can be recorded as (matched exactly by
# the deployment architecture gate — x86_64 and x86_64_nvidia are distinct)
TARGET_ARCHITECTURES = ('x86_64', 'x86_64_nvidia',
                        'arm64_jp4', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7')

# Named shadow carrying the device's model GPU-fallback status snapshot
# (spec: model-gpu-fallback-visibility). Reported-only telemetry mirrored to
# IoT Core by ShadowManager (deployments.py auto-include); the single-device
# GET reads it on demand as the additive `model_status` field. Must match
# deployments.MODEL_STATUS_SHADOW_NAME and the edge-side constant in
# src/backend/utils/model_status_shadow.py.
MODEL_STATUS_SHADOW_NAME = 'dda-model-status'


def get_device_record(device_id):
    """The Devices-table record of one device, or {} when none exists"""
    if not DEVICES_TABLE or not device_id:
        return {}
    try:
        response = dynamodb.Table(DEVICES_TABLE).get_item(
            Key={'device_id': device_id})
    except ClientError as e:
        logger.warning(f"Could not read device record for {device_id}: {e}")
        return {}
    return response.get('Item') or {}


def handler(event, context):
    """
    Handle device management requests
    
    GET /api/v1/devices       - List devices (IoT Things tagged with dda-portal:managed=true)
    GET /api/v1/devices/{id}  - Get device details
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Devices request: {http_method} {path}")
        
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
        device_id = path_parameters.get('id')

        # SSH tunnel (AWS IoT Secure Tunneling) endpoints — checked before the
        # generic get_device so the /ssh-tunnel sub-paths route correctly.
        if device_id and path.endswith('/ssh-tunnel/open') and http_method == 'POST':
            return open_ssh_tunnel(device_id, user, query_parameters)
        if device_id and path.endswith('/ssh-tunnel') and http_method == 'POST':
            body = json.loads(event.get('body') or '{}')
            return set_ssh_tunnel(device_id, user, query_parameters, body)
        if device_id and path.endswith('/ssh-tunnel') and http_method == 'GET':
            return get_ssh_tunnel_status(device_id, user, query_parameters)

        if http_method == 'GET' and not device_id:
            return list_devices(user, query_parameters)
        elif http_method == 'GET' and device_id:
            return get_device(device_id, user, query_parameters)
        elif http_method == 'PUT' and device_id:
            body = json.loads(event.get('body') or '{}')
            return update_device_flags(device_id, user, query_parameters, body)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in devices handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def update_device_flags(device_id, user, query_params, body):
    """
    PUT /api/v1/devices/{id}

    Record portal-managed device attributes on the Devices table
    (custom-node-designer task 10.5):

    - ``test_device`` (bool): designates the device as a Test_Device, the
      only kind of target a test-state plugin may be deployed to
      (Requirements 9.7, 9.8, 16.3). Set by a UseCaseAdmin.
    - ``target_architecture`` (optional): the device's recorded DDA
      Target_Architecture, checked by the deployment architecture gate
      against Plugin_Component platform manifests (Requirement 16.6).

    Body: {"usecase_id": "...", "test_device": true|false,
           "target_architecture": "x86_64"|...|null}
    """
    try:
        usecase_id = body.get('usecase_id') or query_params.get('usecase_id')
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id required'})

        # Designating a Test_Device is a UseCaseAdmin action within their
        # own Use_Case (PortalAdmin anywhere) — the same principal set as
        # node-designer:manage, which this flag exists to serve (the
        # test-state deployment gate). Operator-held device permissions
        # (manage_devices) deliberately do not grant it.
        if not is_super_user(user['user_id']) and not rbac_manager.has_permission(
                user['user_id'], usecase_id, Permission.NODE_DESIGNER_MANAGE,
                user_info=user):
            log_audit_event(
                user['user_id'], 'update_device_flags', 'device', device_id,
                'denied', {'usecase_id': usecase_id,
                           'required_permissions': [
                               Permission.NODE_DESIGNER_MANAGE.value]}
            )
            return create_response(403, {'error': 'Access denied'})

        if 'test_device' not in body and 'target_architecture' not in body:
            return create_response(400, {
                'error': 'test_device or target_architecture required'})

        if not DEVICES_TABLE:
            return create_response(500, {'error': 'Devices table not configured'})

        updates = {}
        if 'test_device' in body:
            if not isinstance(body['test_device'], bool):
                return create_response(400, {'error': 'test_device must be a boolean'})
            updates['test_device'] = body['test_device']
        if 'target_architecture' in body:
            target_architecture = body['target_architecture']
            if target_architecture is not None and \
                    target_architecture not in TARGET_ARCHITECTURES:
                return create_response(400, {
                    'error': (f"target_architecture must be one of "
                              f"{list(TARGET_ARCHITECTURES)} or null")})
            updates['target_architecture'] = target_architecture

        set_parts = ['usecase_id = :uid', 'updated_at = :at', 'updated_by = :by']
        values = {
            ':uid': usecase_id,
            ':at': int(datetime.utcnow().timestamp() * 1000),
            ':by': user['user_id'],
        }
        for index, (attr, value) in enumerate(sorted(updates.items())):
            set_parts.append(f"{attr} = :v{index}")
            values[f":v{index}"] = value

        result = dynamodb.Table(DEVICES_TABLE).update_item(
            Key={'device_id': device_id},
            UpdateExpression='SET ' + ', '.join(set_parts),
            ExpressionAttributeValues=values,
            ReturnValues='ALL_NEW'
        )
        record = result.get('Attributes', {})

        log_audit_event(
            user['user_id'], 'update_device_flags', 'device', device_id,
            'success', {'usecase_id': usecase_id, **updates}
        )

        return create_response(200, {
            'device_id': device_id,
            'usecase_id': usecase_id,
            'test_device': bool(record.get('test_device')),
            'target_architecture': record.get('target_architecture'),
        })

    except ClientError as e:
        logger.error(f"AWS error updating device flags: {str(e)}")
        return create_response(500, {'error': f'Failed to update device: {str(e)}'})
    except Exception as e:
        logger.error(f"Error updating device flags: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to update device'})


def list_devices(user, query_params):
    """
    List Greengrass Core Devices tagged with dda-portal:managed=true.
    Only shows devices set up via setup_station.sh script.
    """
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'list_devices', 'device', 'all',
                'failure', {'reason': 'access_denied', 'usecase_id': usecase_id}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details for cross-account access
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        # Get region from use case, fallback to environment variable, then us-east-1
        region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))
        logger.info(f"Using region {region} for use case {usecase_id}")
        
        # Create clients with assumed role
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        iot_client = create_boto3_client('iot', credentials, region)
        
        devices = []
        next_token = None
        account_id = usecase.get('account_id', '')
        
        # List all Greengrass core devices, then filter by tag on core device
        while True:
            params = {'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_core_devices(**params)
            
            for device in response.get('coreDevices', []):
                thing_name = device.get('coreDeviceThingName')
                
                # Check if this Greengrass Core Device has the dda-portal:managed tag
                try:
                    gg_arn = f"arn:aws:greengrass:{region}:{account_id}:coreDevices:{thing_name}"
                    gg_tags_response = greengrass_client.list_tags_for_resource(resourceArn=gg_arn)
                    tags = gg_tags_response.get('tags', {})
                    
                    # Only include devices with dda-portal:managed=true tag
                    if tags.get('dda-portal:managed') == 'true':
                        # Get IoT Thing ARN
                        thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{thing_name}"
                        try:
                            thing_response = iot_client.describe_thing(thingName=thing_name)
                            thing_arn = thing_response.get('thingArn', thing_arn)
                        except ClientError:
                            pass
                        
                        # Get platform/architecture from get_core_device
                        platform = ''
                        architecture = ''
                        try:
                            core_device_detail = greengrass_client.get_core_device(coreDeviceThingName=thing_name)
                            platform = core_device_detail.get('platform', '')
                            architecture = core_device_detail.get('architecture', '')
                        except ClientError:
                            pass
                        
                        # Get installed components count
                        installed_components = []
                        try:
                            comp_response = greengrass_client.list_installed_components(
                                coreDeviceThingName=thing_name,
                                maxResults=100
                            )
                            for comp in comp_response.get('installedComponents', []):
                                installed_components.append({
                                    'componentName': comp.get('componentName'),
                                    'componentVersion': comp.get('componentVersion'),
                                    'lifecycleState': comp.get('lifecycleState'),
                                })
                        except ClientError as comp_err:
                            logger.warning(f"Could not get components for {thing_name}: {comp_err}")
                        
                        # Convert datetime to ISO string
                        last_status = device.get('lastStatusUpdateTimestamp')
                        if last_status:
                            last_status = last_status.isoformat() if hasattr(last_status, 'isoformat') else str(last_status)
                        
                        # Portal-recorded attributes (Devices table): the
                        # UseCaseAdmin-set Test_Device flag and the
                        # recorded Target_Architecture.
                        device_record = get_device_record(thing_name)

                        devices.append({
                            'device_id': thing_name,
                            'thing_name': thing_name,
                            'thing_arn': thing_arn,
                            'status': device.get('status', 'UNKNOWN'),
                            'last_status_update': last_status,
                            'platform': platform,
                            'architecture': architecture,
                            'test_device': bool(device_record.get('test_device')),
                            'target_architecture': device_record.get('target_architecture'),
                            'tags': tags,
                            'usecase_id': usecase_id,
                            'installed_components': installed_components,
                            'component_count': len(installed_components)
                        })
                except ClientError as e:
                    logger.warning(f"Could not check tags for {thing_name}: {e}")
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        logger.info(f"Found {len(devices)} portal-managed Greengrass core devices")
        
        log_audit_event(
            user['user_id'], 'list_devices', 'device', 'all',
            'success', {'usecase_id': usecase_id, 'count': len(devices)}
        )
        
        return create_response(200, {
            'devices': devices,
            'count': len(devices)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing devices: {str(e)}")
        return create_response(500, {'error': f'Failed to list devices: {str(e)}'})
    except Exception as e:
        logger.error(f"Error listing devices: {str(e)}")
        return create_response(500, {'error': 'Failed to list devices'})


def get_device(device_id, user, query_params):
    """Get detailed information about a specific device (Greengrass Core Device)"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'get_device', 'device', device_id,
                'failure', {'reason': 'access_denied'}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details for cross-account access
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        # Get region from use case, fallback to environment variable, then us-east-1
        region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))
        logger.info(f"Using region {region} for device {device_id}")
        account_id = usecase.get('account_id', '')
        
        # Create clients with assumed role
        iot_client = create_boto3_client('iot', credentials, region)
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        # Get thing details
        thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{device_id}"
        thing_type = ''
        attributes = {}
        version = 0
        try:
            thing_details = iot_client.describe_thing(thingName=device_id)
            thing_arn = thing_details.get('thingArn', thing_arn)
            thing_type = thing_details.get('thingTypeName', '')
            attributes = thing_details.get('attributes', {})
            version = thing_details.get('version', 0)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return create_response(404, {'error': 'Device not found'})
            logger.warning(f"Could not get IoT thing details: {e}")
        
        # Get Greengrass Core Device tags
        tags = {}
        try:
            gg_arn = f"arn:aws:greengrass:{region}:{account_id}:coreDevices:{device_id}"
            gg_tags_response = greengrass_client.list_tags_for_resource(resourceArn=gg_arn)
            tags = gg_tags_response.get('tags', {})
        except ClientError as e:
            logger.warning(f"Could not get Greengrass tags: {e}")
        
        # Get Greengrass core device status
        gg_status = get_greengrass_status(greengrass_client, device_id)
        
        # Get installed components
        installed_components = get_installed_components(greengrass_client, device_id)
        
        # Get effective deployments
        deployments = get_device_deployments(greengrass_client, device_id)
        
        # Additive model GPU-fallback status (spec:
        # model-gpu-fallback-visibility): read the device's reported
        # dda-model-status shadow on demand. Absence-tolerant — a missing
        # shadow or ANY read error degrades to None (today's rendering),
        # never an error response.
        model_status = get_model_status(usecase, region, device_id)
        
        # Convert datetime to ISO string
        last_status = gg_status.get('lastStatusUpdateTimestamp')
        if last_status:
            last_status = last_status.isoformat() if hasattr(last_status, 'isoformat') else str(last_status)
        
        # Portal-recorded attributes (Devices table): the UseCaseAdmin-set
        # Test_Device flag and the recorded Target_Architecture.
        device_record = get_device_record(device_id)

        device = {
            'device_id': device_id,
            'thing_name': device_id,
            'thing_arn': thing_arn,
            'thing_type': thing_type,
            'attributes': attributes,
            'version': version,
            'tags': tags,
            'status': gg_status.get('status', 'UNKNOWN'),
            'last_status_update': last_status,
            'greengrass_version': gg_status.get('coreVersion', ''),
            'platform': gg_status.get('platform', ''),
            'architecture': gg_status.get('architecture', ''),
            'test_device': bool(device_record.get('test_device')),
            'target_architecture': device_record.get('target_architecture'),
            'installed_components': installed_components,
            'deployments': deployments,
            'model_status': model_status,
            'usecase_id': usecase_id
        }
        
        log_audit_event(
            user['user_id'], 'get_device', 'device', device_id, 'success'
        )
        
        return create_response(200, {'device': device})
        
    except ClientError as e:
        logger.error(f"AWS error getting device: {str(e)}")
        return create_response(500, {'error': f'Failed to get device: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting device: {str(e)}")
        return create_response(500, {'error': 'Failed to get device'})


def get_model_status(usecase, region, thing_name):
    """The device's dda-model-status shadow reported document, or None.

    Reads the reported-only model GPU-fallback status shadow through the
    use-case-scoped iot-data client (the camera_registry.py refresh
    pattern). Absence means "no information" (spec
    model-gpu-fallback-visibility, Decision 6): ResourceNotFoundException
    (no shadow — older device software) or ANY other error returns None so
    the device renders exactly as today; never raises.
    """
    try:
        iot_data = get_usecase_client('iot-data', usecase, region=region)
        response = iot_data.get_thing_shadow(
            thingName=thing_name, shadowName=MODEL_STATUS_SHADOW_NAME)
        payload = json.loads(response['payload'].read())
        return (payload.get('state') or {}).get('reported')
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            logger.warning(
                f"Could not read {MODEL_STATUS_SHADOW_NAME} shadow for "
                f"{thing_name}: {e}")
        return None
    except Exception as e:
        logger.warning(
            f"Could not read {MODEL_STATUS_SHADOW_NAME} shadow for "
            f"{thing_name}: {e}")
        return None


def get_greengrass_status(greengrass_client, thing_name):
    """Get Greengrass core device status"""
    try:
        response = greengrass_client.get_core_device(coreDeviceThingName=thing_name)
        return {
            'status': response.get('status', 'UNKNOWN'),
            'lastStatusUpdateTimestamp': response.get('lastStatusUpdateTimestamp'),
            'coreVersion': response.get('coreVersion', ''),
            'platform': response.get('platform', ''),
            'architecture': response.get('architecture', ''),
            'tags': response.get('tags', {})
        }
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return {'status': 'NOT_GREENGRASS'}
        logger.warning(f"Could not get Greengrass status for {thing_name}: {e}")
        return {'status': 'UNKNOWN'}


def get_installed_components(greengrass_client, thing_name):
    """Get list of installed components on a Greengrass core device"""
    try:
        components = []
        paginator = greengrass_client.get_paginator('list_installed_components')
        
        for page in paginator.paginate(coreDeviceThingName=thing_name):
            for comp in page.get('installedComponents', []):
                # Convert datetime fields to ISO strings
                last_status_change = comp.get('lastStatusChangeTimestamp')
                if last_status_change and hasattr(last_status_change, 'isoformat'):
                    last_status_change = last_status_change.isoformat()
                
                last_reported = comp.get('lastReportedTimestamp')
                if last_reported and hasattr(last_reported, 'isoformat'):
                    last_reported = last_reported.isoformat()
                
                components.append({
                    'componentName': comp.get('componentName'),
                    'componentVersion': comp.get('componentVersion'),
                    'lifecycleState': comp.get('lifecycleState'),
                    'lifecycleStateDetails': comp.get('lifecycleStateDetails'),
                    'isRoot': comp.get('isRoot', False),
                    'lastStatusChangeTimestamp': last_status_change,
                    'lastInstallationSource': comp.get('lastInstallationSource'),
                    'lastReportedTimestamp': last_reported
                })
        
        return components
    except ClientError as e:
        logger.warning(f"Could not get installed components for {thing_name}: {e}")
        return []


def get_device_deployments(greengrass_client, thing_name):
    """Get deployments targeting this device"""
    try:
        deployments = []
        
        # List deployments targeting this core device
        response = greengrass_client.list_effective_deployments(
            coreDeviceThingName=thing_name,
            maxResults=50
        )
        
        for dep in response.get('effectiveDeployments', []):
            # Convert datetime fields to ISO strings
            creation_ts = dep.get('creationTimestamp')
            if creation_ts and hasattr(creation_ts, 'isoformat'):
                creation_ts = creation_ts.isoformat()
            
            modified_ts = dep.get('modifiedTimestamp')
            if modified_ts and hasattr(modified_ts, 'isoformat'):
                modified_ts = modified_ts.isoformat()
            
            deployments.append({
                'deploymentId': dep.get('deploymentId'),
                'deploymentName': dep.get('deploymentName'),
                'iotJobId': dep.get('iotJobId'),
                'iotJobArn': dep.get('iotJobArn'),
                'targetArn': dep.get('targetArn'),
                'coreDeviceExecutionStatus': dep.get('coreDeviceExecutionStatus'),
                'reason': dep.get('reason'),
                'creationTimestamp': creation_ts,
                'modifiedTimestamp': modified_ts
            })
        
        return deployments
    except ClientError as e:
        logger.warning(f"Could not get deployments for {thing_name}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SSH via AWS IoT Secure Tunneling
#
# Edge devices are typically behind NAT with no routable/inbound port, so we
# reach them over AWS IoT Secure Tunneling (outbound WebSocket from the device
# to the AWS IoT endpoint). Because there is NO inbound port on the device,
# there is nothing for a security group / IP allowlist to filter — access is
# gated by IAM (who may call OpenTunnel here) plus the short-lived tunnel access
# tokens. See docs/connect-to-device.md.
#
# "Enable" deploys the AWS-managed aws.greengrass.SecureTunneling component to
# the device (merged into a thing-targeted deployment, preserving existing
# components). "Open" creates a tunnel for the SSH service and returns the
# source access token the operator uses with the AWS IoT local proxy.
# ─────────────────────────────────────────────────────────────────────────────

SECURE_TUNNELING_COMPONENT = 'aws.greengrass.SecureTunneling'
DEFAULT_OS_USER = 'ggc_user'

# Newest SecureTunneling version deployable to an arm64_jp5 device.
#
# JetPack 5 devices run Ubuntu 20.04 / GLIBC 2.31. The AWS-managed
# aws.greengrass.SecureTunneling >= 2.0.0 is built against GLIBC >= 2.32 and
# crash-loops on JP5 ("GLIBC_2.32/2.33/2.34 not found"), which fails the whole
# thing-targeted deployment and triggers a rollback. 1.1.3 is the newest
# pre-2.0 release and the last line compatible with GLIBC 2.31. Other arches
# (arm64_jp6 on Ubuntu 22.04 / GLIBC 2.35, x86_64) run newer GLIBC and are
# unaffected, so the cap is scoped to arm64_jp5 only.
SECURE_TUNNELING_MAX_JP5 = '1.1.3'


def _semver_tuple(version):
    """Best-effort (major, minor, patch) tuple for a component version string,
    for ordered comparison. Non-numeric or missing parts sort as 0."""
    parts = []
    for token in str(version or '').split('.')[:3]:
        digits = ''.join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _device_local_server_arch(greengrass_client, thing_name):
    """The DDA arch id (e.g. 'arm64_jp5') of a device, derived from its
    installed LocalServer component name, or None if it cannot be determined.

    Reuses the LocalServer-name -> arch mapping already defined for the
    deployment compatibility gate so the tunnel guard and the deployment gate
    classify a device the same way.
    """
    try:
        from deployments import (
            get_device_local_server, local_server_component_arch)
        name, _ = get_device_local_server(greengrass_client, thing_name)
        return local_server_component_arch(name)
    except Exception as e:
        logger.warning(
            f"Could not determine LocalServer arch for {thing_name}: {e}")
        return None


def _cap_secure_tunneling_version(version, device_arch):
    """Cap a SecureTunneling version to the max supported by the device arch.

    arm64_jp5 (GLIBC 2.31) cannot run SecureTunneling >= 2.0.0, so any such
    version is capped down to SECURE_TUNNELING_MAX_JP5 (1.1.3). All other
    arches pass the requested version through unchanged.
    """
    if (device_arch == 'arm64_jp5'
            and _semver_tuple(version) > _semver_tuple(SECURE_TUNNELING_MAX_JP5)):
        logger.info(
            f"Capping SecureTunneling {version} -> {SECURE_TUNNELING_MAX_JP5} "
            f"for JP5 device (GLIBC 2.31 incompatibility with >= 2.0.0)")
        return SECURE_TUNNELING_MAX_JP5
    return version


def _resolve_usecase_context(device_id, user, query_params):
    """Access-check + resolve (usecase, region, credentials) for a device op.
    Returns (context_dict, error_response). One of them is None."""
    usecase_id = query_params.get('usecase_id')
    if not usecase_id:
        return None, create_response(400, {'error': 'usecase_id parameter required'})
    if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
        return None, create_response(403, {'error': 'Access denied'})
    usecase = get_usecase(usecase_id)
    if not usecase:
        return None, create_response(404, {'error': 'Use case not found'})
    credentials = assume_cross_account_role(usecase['cross_account_role_arn'], usecase['external_id'])
    region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))
    account_id = usecase.get('account_id', '')
    return {
        'usecase': usecase, 'usecase_id': usecase_id, 'credentials': credentials,
        'region': region, 'account_id': account_id,
    }, None


def _latest_secure_tunneling_version(greengrass_client, region):
    """Resolve the latest version of the AWS-managed SecureTunneling component."""
    arn = f"arn:aws:greengrass:{region}:aws:components:{SECURE_TUNNELING_COMPONENT}"
    try:
        resp = greengrass_client.list_component_versions(arn=arn, maxResults=1)
        versions = resp.get('componentVersions', [])
        if versions:
            return versions[0]['componentVersion']
    except ClientError as e:
        logger.warning(f"Could not resolve SecureTunneling version: {e}")
    # Fallback to a known-good recent version if lookup fails.
    return '1.0.19'


def _thing_target_arn(region, account_id, thing_name):
    return f"arn:aws:iot:{region}:{account_id}:thing/{thing_name}"


def _current_thing_components(greengrass_client, target_arn):
    """Return the components dict of the latest thing-targeted deployment, in
    create_deployment shape, or {} if none."""
    try:
        resp = greengrass_client.list_deployments(
            targetArn=target_arn, historyFilter='LATEST_ONLY', maxResults=1)
        deployments = resp.get('deployments', [])
        if not deployments:
            return {}
        dep = greengrass_client.get_deployment(deploymentId=deployments[0]['deploymentId'])
        return dep.get('components', {}) or {}
    except ClientError as e:
        logger.warning(f"Could not read current deployment for {target_arn}: {e}")
        return {}


def set_ssh_tunnel(device_id, user, query_params, body):
    """Enable/disable SSH via Secure Tunneling by adding/removing the
    aws.greengrass.SecureTunneling component on a thing-targeted deployment."""
    ctx, err = _resolve_usecase_context(device_id, user, query_params)
    if err:
        return err
    enabled = bool(body.get('enabled', True))
    os_user = str(body.get('osUser') or DEFAULT_OS_USER).strip() or DEFAULT_OS_USER
    region, account_id = ctx['region'], ctx['account_id']

    greengrass_client = create_boto3_client('greengrassv2', ctx['credentials'], region)
    target_arn = _thing_target_arn(region, account_id, device_id)

    components = dict(_current_thing_components(greengrass_client, target_arn))
    if enabled:
        version = _latest_secure_tunneling_version(greengrass_client, region)
        # Guard: JP5 (GLIBC 2.31) cannot run SecureTunneling >= 2.0.0. Cap the
        # deployed version so enabling/updating SSH on a JP5 device never
        # installs an incompatible SecureTunneling that would crash-loop and
        # roll the deployment back. Non-JP5 arches are unaffected.
        device_arch = _device_local_server_arch(greengrass_client, device_id)
        version = _cap_secure_tunneling_version(version, device_arch)
        components[SECURE_TUNNELING_COMPONENT] = {
            'componentVersion': version,
            'configurationUpdate': {'merge': json.dumps({'OSUser': os_user})},
        }
    else:
        components.pop(SECURE_TUNNELING_COMPONENT, None)
        if not components:
            # Nothing left to deploy; a thing deployment must have >=1 component.
            return create_response(200, {
                'device_id': device_id, 'enabled': False,
                'message': 'Secure Tunneling was not present in a thing-level deployment.'
            })

    try:
        dep = greengrass_client.create_deployment(
            targetArn=target_arn,
            deploymentName=f"ssh-tunnel-{'on' if enabled else 'off'}-{device_id}",
            components=components,
        )
    except ClientError as e:
        logger.error(f"Failed to create SSH-tunnel deployment: {e}")
        return create_response(500, {'error': f'Failed to update deployment: {str(e)}'})

    log_audit_event(
        user['user_id'], 'set_ssh_tunnel', 'device', device_id, 'success',
        {'enabled': enabled, 'os_user': os_user, 'deployment_id': dep.get('deploymentId')})

    return create_response(200, {
        'device_id': device_id,
        'enabled': enabled,
        'os_user': os_user if enabled else None,
        'deployment_id': dep.get('deploymentId'),
        'iot_job_id': dep.get('iotJobId'),
        'message': (
            'Secure Tunneling enabled. The device will pull the component; then '
            'use "Open SSH session" to start a tunnel.' if enabled
            else 'Secure Tunneling disabled.'),
    })


def get_ssh_tunnel_status(device_id, user, query_params):
    """Report whether the SecureTunneling component is installed on the device."""
    ctx, err = _resolve_usecase_context(device_id, user, query_params)
    if err:
        return err
    greengrass_client = create_boto3_client('greengrassv2', ctx['credentials'], ctx['region'])
    installed = False
    version = None
    try:
        paginator = greengrass_client.get_paginator('list_installed_components')
        for page in paginator.paginate(coreDeviceThingName=device_id):
            for comp in page.get('installedComponents', []):
                if comp.get('componentName') == SECURE_TUNNELING_COMPONENT:
                    installed = True
                    version = comp.get('componentVersion')
    except ClientError as e:
        logger.warning(f"Could not list installed components for {device_id}: {e}")

    # Report the device arch and the SecureTunneling ceiling that applies to
    # it, so the UI can explain why a JP5 device is held at 1.1.3 instead of
    # the public latest (2.0.x).
    device_arch = _device_local_server_arch(greengrass_client, device_id)
    max_version = (SECURE_TUNNELING_MAX_JP5
                   if device_arch == 'arm64_jp5' else None)
    return create_response(200, {
        'device_id': device_id,
        'enabled': installed,
        'component_version': version,
        'device_arch': device_arch,
        'secure_tunneling_max_version': max_version,
    })


def open_ssh_tunnel(device_id, user, query_params):
    """Open an AWS IoT Secure Tunnel to the device's SSH service and return the
    SOURCE access token for use with the AWS IoT local proxy."""
    ctx, err = _resolve_usecase_context(device_id, user, query_params)
    if err:
        return err
    region = ctx['region']
    lifetime = 60
    try:
        lifetime = max(1, min(720, int(query_params.get('lifetime_minutes', 60))))
    except (ValueError, TypeError):
        lifetime = 60

    tunnel_client = create_boto3_client('iotsecuretunneling', ctx['credentials'], region)
    try:
        resp = tunnel_client.open_tunnel(
            destinationConfig={'thingName': device_id, 'services': ['SSH']},
            timeoutConfig={'maxLifetimeTimeoutMinutes': lifetime},
            tags=[{'key': 'dda-portal', 'value': 'ssh'}],
        )
    except ClientError as e:
        logger.error(f"Failed to open tunnel for {device_id}: {e}")
        return create_response(500, {'error': f'Failed to open tunnel: {str(e)}'})

    log_audit_event(
        user['user_id'], 'open_ssh_tunnel', 'device', device_id, 'success',
        {'tunnel_id': resp.get('tunnelId'), 'lifetime_minutes': lifetime})

    # NOTE: only the SOURCE token is returned to the operator. The destination
    # side is handled by the on-device SecureTunneling component automatically.
    return create_response(200, {
        'device_id': device_id,
        'tunnel_id': resp.get('tunnelId'),
        'region': region,
        'source_access_token': resp.get('sourceAccessToken'),
        'lifetime_minutes': lifetime,
        'message': 'Tunnel opened. Use the source access token with the AWS IoT '
                   'local proxy, then SSH to localhost. See docs/connect-to-device.md.',
    })
