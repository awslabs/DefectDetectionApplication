"""
Device Provisioning handler for Edge CV Portal
Provisions IoT Things and certificates for edge devices (Jetson, etc.)
"""
import json
import logging
import os
import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, get_usecase
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot_client = boto3.client('iot')
iot_data_client = boto3.client('iot-data-plane')


def handler(event, context):
    """
    Handle device provisioning requests
    
    POST /api/v1/devices/provision - Provision a new edge device
    GET /api/v1/devices/{id}/provisioning-status - Check provisioning status
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        logger.info(f"Device provisioning request: {http_method} {path}")
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'POST,GET,OPTIONS',
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        
        if http_method == 'POST':
            return provision_device(user, event)
        elif http_method == 'GET':
            path_parameters = event.get('pathParameters') or {}
            device_id = path_parameters.get('id')
            return get_provisioning_status(device_id, user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in device provisioning handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def provision_device(user, event):
    """
    Provision a new IoT Thing and generate certificates for an edge device.
    
    Request body:
    {
        "device_name": "jetson-1",
        "usecase_id": "uc-123",
        "device_type": "jetson-nano",  # optional
        "tags": {"location": "factory-1"}  # optional
    }
    """
    try:
        body = json.loads(event.get('body', '{}'))
        device_name = body.get('device_name')
        usecase_id = body.get('usecase_id')
        device_type = body.get('device_type', 'edge-device')
        tags = body.get('tags', {})
        
        if not device_name or not usecase_id:
            return create_response(400, {
                'error': 'device_name and usecase_id are required'
            })
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'provision_device', 'device', device_name,
                'failure', {'reason': 'access_denied', 'usecase_id': usecase_id}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # Verify usecase exists
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Check if thing already exists
        try:
            iot_client.describe_thing(thingName=device_name)
            return create_response(409, {
                'error': f'Device {device_name} already exists'
            })
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
        
        # Create IoT Thing
        logger.info(f"Creating IoT Thing: {device_name}")
        iot_client.create_thing(
            thingName=device_name,
            thingTypeName='EdgeDevice',
            attributePayload={
                'attributes': {
                    'usecase_id': usecase_id,
                    'device_type': device_type,
                    'provisioned_by': user['user_id'],
                    **tags
                }
            }
        )
        
        # Create certificate
        logger.info(f"Creating certificate for: {device_name}")
        cert_response = iot_client.create_keys_and_certificate(setAsActive=True)
        cert_id = cert_response['certificateId']
        cert_arn = cert_response['certificateArn']
        
        # Attach certificate to thing
        logger.info(f"Attaching certificate to thing: {device_name}")
        iot_client.attach_thing_principal(
            thingName=device_name,
            principal=cert_arn
        )
        
        # Create and attach policy
        policy_name = 'GreengrassV2IoTThingPolicy'
        try:
            iot_client.attach_policy(
                policyName=policy_name,
                target=cert_arn
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'PolicyVersionNotFound':
                logger.warning(f"Policy {policy_name} not found, creating default policy")
                create_default_policy(policy_name)
                iot_client.attach_policy(
                    policyName=policy_name,
                    target=cert_arn
                )
            else:
                raise
        
        # Get IoT endpoint
        endpoint_response = iot_client.describe_endpoint(endpointType='iot:Data-ATS')
        iot_endpoint = endpoint_response['endpointAddress']
        
        # Get root CA
        root_ca_url = 'https://www.amazontrust.com/repository/AmazonRootCA1.pem'
        
        log_audit_event(
            user['user_id'], 'provision_device', 'device', device_name,
            'success', {
                'usecase_id': usecase_id,
                'certificate_id': cert_id,
                'device_type': device_type
            }
        )
        
        return create_response(200, {
            'device_name': device_name,
            'certificate_id': cert_id,
            'certificate_pem': cert_response['certificatePem'],
            'private_key': cert_response['keyPair']['PrivateKey'],
            'iot_endpoint': iot_endpoint,
            'root_ca_url': root_ca_url,
            'region': region,
            'provisioning_status': 'success',
            'next_steps': [
                '1. Download the certificate and private key',
                '2. Download the root CA certificate',
                '3. Run setup_station.sh with the certificates',
                '4. Device will connect to AWS IoT Core'
            ]
        })
        
    except ClientError as e:
        logger.error(f"AWS error provisioning device: {str(e)}")
        return create_response(500, {
            'error': f'Failed to provision device: {str(e)}'
        })
    except Exception as e:
        logger.error(f"Error provisioning device: {str(e)}")
        return create_response(500, {'error': 'Failed to provision device'})


def get_provisioning_status(device_id, user):
    """
    Get the provisioning status of a device.
    """
    try:
        if not device_id:
            return create_response(400, {'error': 'Device ID is required'})
        
        # Describe thing
        thing = iot_client.describe_thing(thingName=device_id)
        
        # Get principals (certificates)
        principals = iot_client.list_thing_principals(thingName=device_id)
        
        return create_response(200, {
            'device_id': device_id,
            'thing_arn': thing['thingArn'],
            'attributes': thing.get('attributes', {}),
            'certificates': [
                {
                    'certificate_arn': p,
                    'certificate_id': p.split('/')[-1]
                }
                for p in principals.get('principals', [])
            ],
            'provisioning_status': 'provisioned'
        })
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return create_response(404, {'error': 'Device not found'})
        logger.error(f"AWS error getting provisioning status: {str(e)}")
        return create_response(500, {'error': 'Failed to get provisioning status'})
    except Exception as e:
        logger.error(f"Error getting provisioning status: {str(e)}")
        return create_response(500, {'error': 'Failed to get provisioning status'})


def create_default_policy(policy_name):
    """
    Create a default policy for Greengrass devices.
    """
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "iot:Connect"
                ],
                "Resource": "arn:aws:iot:*:*:client/${iot:Connection.Thing.ThingName}"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "iot:Publish"
                ],
                "Resource": [
                    "arn:aws:iot:*:*:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/*"
                ]
            },
            {
                "Effect": "Allow",
                "Action": [
                    "iot:Subscribe"
                ],
                "Resource": [
                    "arn:aws:iot:*:*:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/*"
                ]
            },
            {
                "Effect": "Allow",
                "Action": [
                    "iot:Receive"
                ],
                "Resource": [
                    "arn:aws:iot:*:*:topic/$aws/things/${iot:Connection.Thing.ThingName}/*"
                ]
            }
        ]
    }
    
    iot_client.create_policy(
        policyName=policy_name,
        policyDocument=json.dumps(policy_document)
    )
    logger.info(f"Created policy: {policy_name}")
