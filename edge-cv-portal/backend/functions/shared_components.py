"""
Shared Components Management for Edge CV Portal

This module handles the sharing of portal-managed Greengrass components (like dda-LocalServer)
to usecase accounts. Components are created in the portal account and shared read-only
to usecase accounts during onboarding.

Architecture:
- Portal Account: Owns and publishes dda-LocalServer components
- Usecase Accounts: Receive read-only access to deploy the component

Cross-Account Access Pattern:
Since Greengrass doesn't support resource-based policies or RAM sharing for components,
we use a "component mirroring" approach:
1. Portal stores component artifacts in a shared S3 bucket
2. During usecase onboarding, we create the component in the usecase account
3. The component recipe points to the portal's S3 bucket for artifacts
4. Usecase accounts get s3:GetObject permission to the portal's artifact bucket
"""
import json
import os
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, assume_cross_account_role
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')
s3 = boto3.client('s3')
greengrass_portal = boto3.client('greengrassv2')

# Environment variables
USECASES_TABLE = os.environ.get('USECASES_TABLE')
SHARED_COMPONENTS_TABLE = os.environ.get('SHARED_COMPONENTS_TABLE', 'SharedComponents')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
PORTAL_ACCOUNT_ID = os.environ.get('PORTAL_ACCOUNT_ID')

# DDA LocalServer component configurations
DDA_LOCAL_SERVER_COMPONENTS = {
    'arm64': {
        'name': 'aws.edgeml.dda.LocalServer.arm64',
        'description': 'DDA LocalServer for ARM64 devices (Jetson, Raspberry Pi)',
        'platforms': ['aarch64'],
        'artifact_key': 'shared-components/dda-localserver/arm64/dda-localserver.zip'
    },
    'amd64': {
        'name': 'aws.edgeml.dda.LocalServer.amd64', 
        'description': 'DDA LocalServer for AMD64 devices (x86_64)',
        'platforms': ['amd64', 'x86_64'],
        'artifact_key': 'shared-components/dda-localserver/amd64/dda-localserver.zip'
    }
}


def get_portal_component_recipe(component_name: str, version: str) -> Optional[Dict]:
    """
    Get the recipe for a portal-managed component.
    This retrieves the component recipe from the portal account.
    """
    try:
        # List component versions to find the ARN
        response = greengrass_portal.list_component_versions(
            arn=f"arn:aws:greengrass:{os.environ.get('AWS_REGION')}:{PORTAL_ACCOUNT_ID}:components:{component_name}"
        )
        
        versions = response.get('componentVersions', [])
        target_version = None
        
        for v in versions:
            if v.get('componentVersion') == version:
                target_version = v
                break
        
        if not target_version:
            # If specific version not found, get latest
            if versions:
                target_version = versions[0]
            else:
                return None
        
        # Get the component recipe
        component_arn = target_version.get('arn')
        if not component_arn:
            return None
            
        describe_response = greengrass_portal.describe_component(arn=component_arn)
        
        # Parse recipe
        recipe_data = describe_response.get('recipe')
        if recipe_data:
            if isinstance(recipe_data, bytes):
                recipe_str = recipe_data.decode('utf-8')
            else:
                recipe_str = str(recipe_data)
            
            try:
                return json.loads(recipe_str)
            except json.JSONDecodeError:
                import yaml
                return yaml.safe_load(recipe_str)
        
        return None
        
    except ClientError as e:
        logger.error(f"Error getting portal component recipe: {str(e)}")
        return None


def generate_dda_localserver_recipe(
    platform: str,
    version: str,
    artifact_s3_uri: str
) -> Dict:
    """
    Generate the DDA LocalServer component recipe.
    This is the base recipe that will be created in usecase accounts.
    """
    config = DDA_LOCAL_SERVER_COMPONENTS.get(platform)
    if not config:
        raise ValueError(f"Unknown platform: {platform}")
    
    component_name = config['name']
    arch = 'aarch64' if platform == 'arm64' else 'amd64'
    
    recipe = {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': version,
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': 'AWS Edge ML - DDA Portal',
        'ComponentDescription': config['description'],
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'ServerPort': 8080,
                'ModelPath': '/aws_dda/models',
                'LogLevel': 'INFO'
            }
        },
        'Manifests': [
            {
                'Platform': {
                    'os': 'linux',
                    'architecture': arch
                },
                'Lifecycle': {
                    'Install': {
                        'Script': 'pip3 install -r {artifacts:decompressedPath}/requirements.txt',
                        'Timeout': 300
                    },
                    'Run': {
                        'Script': 'python3 {artifacts:decompressedPath}/dda_server.py',
                        'Timeout': -1,
                        'requiresPrivilege': True
                    },
                    'Shutdown': {
                        'Script': 'pkill -f dda_server.py || true',
                        'Timeout': 30
                    }
                },
                'Artifacts': [
                    {
                        'Uri': artifact_s3_uri,
                        'Unarchive': 'ZIP',
                        'Permission': {
                            'Read': 'ALL',
                            'Execute': 'ALL'
                        }
                    }
                ]
            }
        ]
    }
    
    return recipe


def share_component_to_usecase(
    usecase_id: str,
    usecase_account_id: str,
    cross_account_role_arn: str,
    external_id: str,
    component_name: str,
    component_version: str,
    user_id: str
) -> Dict:
    """
    Share a portal component to a usecase account.
    
    This creates the component in the usecase account with:
    1. The same recipe as the portal component
    2. Artifact URIs pointing to the portal's S3 bucket
    3. Read-only tag to indicate it's a shared component
    """
    try:
        # Determine platform from component name
        if 'arm64' in component_name.lower():
            platform = 'arm64'
        elif 'amd64' in component_name.lower():
            platform = 'amd64'
        else:
            raise ValueError(f"Cannot determine platform from component name: {component_name}")
        
        config = DDA_LOCAL_SERVER_COMPONENTS[platform]
        
        # Build artifact S3 URI (portal bucket)
        artifact_s3_uri = f"s3://{PORTAL_ARTIFACTS_BUCKET}/{config['artifact_key']}"
        
        # Generate recipe
        recipe = generate_dda_localserver_recipe(platform, component_version, artifact_s3_uri)
        
        # Assume cross-account role
        credentials = assume_cross_account_role(cross_account_role_arn, external_id)
        
        # Create Greengrass client for usecase account
        greengrass_usecase = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )
        
        # Create component in usecase account
        response = greengrass_usecase.create_component_version(
            inlineRecipe=json.dumps(recipe),
            tags={
                'dda-portal:managed': 'true',
                'dda-portal:shared-component': 'true',
                'dda-portal:source-account': PORTAL_ACCOUNT_ID,
                'dda-portal:usecase-id': usecase_id,
                'dda-portal:read-only': 'true',
                'dda-portal:shared-by': user_id,
                'dda-portal:shared-at': datetime.utcnow().isoformat()
            }
        )
        
        component_arn = response['arn']
        logger.info(f"Created shared component {component_name} v{component_version} in account {usecase_account_id}: {component_arn}")
        
        # Record in shared components table
        table = dynamodb.Table(SHARED_COMPONENTS_TABLE)
        table.put_item(Item={
            'usecase_id': usecase_id,
            'component_name': component_name,
            'component_version': component_version,
            'component_arn': component_arn,
            'usecase_account_id': usecase_account_id,
            'source_account_id': PORTAL_ACCOUNT_ID,
            'platform': platform,
            'shared_by': user_id,
            'shared_at': int(datetime.utcnow().timestamp() * 1000),
            'status': 'active'
        })
        
        return {
            'component_name': component_name,
            'component_version': component_version,
            'component_arn': component_arn,
            'platform': platform,
            'status': 'shared'
        }
        
    except ClientError as e:
        logger.error(f"Error sharing component to usecase: {str(e)}")
        raise


def provision_shared_components_for_usecase(
    usecase_id: str,
    usecase_account_id: str,
    cross_account_role_arn: str,
    external_id: str,
    user_id: str,
    component_version: str = '1.0.0'
) -> List[Dict]:
    """
    Provision all shared components (dda-LocalServer variants) for a new usecase.
    Called during usecase onboarding.
    
    Returns list of provisioned components.
    """
    results = []
    
    for platform, config in DDA_LOCAL_SERVER_COMPONENTS.items():
        try:
            result = share_component_to_usecase(
                usecase_id=usecase_id,
                usecase_account_id=usecase_account_id,
                cross_account_role_arn=cross_account_role_arn,
                external_id=external_id,
                component_name=config['name'],
                component_version=component_version,
                user_id=user_id
            )
            results.append(result)
            logger.info(f"Provisioned {config['name']} for usecase {usecase_id}")
            
        except Exception as e:
            logger.error(f"Failed to provision {config['name']} for usecase {usecase_id}: {str(e)}")
            results.append({
                'component_name': config['name'],
                'platform': platform,
                'status': 'failed',
                'error': str(e)
            })
    
    return results


def update_usecase_bucket_policy(
    usecase_account_id: str,
    cross_account_role_arn: str,
    external_id: str
) -> bool:
    """
    Update the usecase account's Greengrass device role to allow
    reading artifacts from the portal's S3 bucket.
    
    This adds s3:GetObject permission for the portal artifacts bucket.
    """
    try:
        # Assume cross-account role
        credentials = assume_cross_account_role(cross_account_role_arn, external_id)
        
        # Create IAM client for usecase account
        iam_usecase = boto3.client(
            'iam',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Policy to allow reading from portal artifacts bucket
        policy_document = {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Sid': 'AllowPortalArtifactAccess',
                    'Effect': 'Allow',
                    'Action': ['s3:GetObject'],
                    'Resource': [
                        f'arn:aws:s3:::{PORTAL_ARTIFACTS_BUCKET}/shared-components/*'
                    ]
                }
            ]
        }
        
        # Create or update the policy
        policy_name = 'DDAPortalSharedComponentsAccess'
        
        try:
            # Try to create the policy
            response = iam_usecase.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_document),
                Description='Allows Greengrass devices to access DDA Portal shared component artifacts'
            )
            policy_arn = response['Policy']['Arn']
            logger.info(f"Created policy {policy_name} in account {usecase_account_id}")
            
        except iam_usecase.exceptions.EntityAlreadyExistsException:
            # Policy exists, get its ARN
            policy_arn = f"arn:aws:iam::{usecase_account_id}:policy/{policy_name}"
            logger.info(f"Policy {policy_name} already exists in account {usecase_account_id}")
        
        # Attach policy to Greengrass device role
        # The role name follows the convention from usecase-account-stack.ts
        greengrass_role_name = 'GreengrassV2TokenExchangeRole'
        
        try:
            iam_usecase.attach_role_policy(
                RoleName=greengrass_role_name,
                PolicyArn=policy_arn
            )
            logger.info(f"Attached {policy_name} to {greengrass_role_name}")
        except iam_usecase.exceptions.NoSuchEntityException:
            logger.warning(f"Role {greengrass_role_name} not found in account {usecase_account_id}")
            # Try alternative role name
            try:
                iam_usecase.attach_role_policy(
                    RoleName='DDAGreengrassDeviceRole',
                    PolicyArn=policy_arn
                )
                logger.info(f"Attached {policy_name} to DDAGreengrassDeviceRole")
            except Exception as e:
                logger.warning(f"Could not attach policy to device role: {str(e)}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error updating usecase bucket policy: {str(e)}")
        return False


def handler(event: Dict, context: Any) -> Dict:
    """
    Lambda handler for shared components management.
    
    POST /api/v1/shared-components/provision
        Provision shared components for a usecase (called during onboarding)
    
    GET /api/v1/shared-components
        List shared components for a usecase
    
    GET /api/v1/shared-components/available
        List available shared components from portal
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        
        if http_method == 'POST' and '/provision' in path:
            return provision_components(event, user)
        elif http_method == 'GET' and '/available' in path:
            return list_available_components(event, user)
        elif http_method == 'GET':
            return list_shared_components(event, user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def provision_components(event: Dict, user: Dict) -> Dict:
    """
    Provision shared components for a usecase.
    Called during usecase onboarding.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        
        required_fields = ['usecase_id']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        component_version = body.get('component_version', '1.0.0')
        
        # Get usecase details
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Usecase not found'})
        
        usecase = response['Item']
        
        # Check user has permission
        if not check_user_access(user['user_id'], usecase_id, 'UseCaseAdmin'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # First, update bucket policy for artifact access
        policy_updated = update_usecase_bucket_policy(
            usecase_account_id=usecase['account_id'],
            cross_account_role_arn=usecase['cross_account_role_arn'],
            external_id=usecase['external_id']
        )
        
        # Provision shared components
        results = provision_shared_components_for_usecase(
            usecase_id=usecase_id,
            usecase_account_id=usecase['account_id'],
            cross_account_role_arn=usecase['cross_account_role_arn'],
            external_id=usecase['external_id'],
            user_id=user['user_id'],
            component_version=component_version
        )
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='provision_shared_components',
            resource_type='usecase',
            resource_id=usecase_id,
            result='success',
            details={
                'components': [r['component_name'] for r in results],
                'policy_updated': policy_updated
            }
        )
        
        success_count = len([r for r in results if r.get('status') == 'shared'])
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'components': results,
            'policy_updated': policy_updated,
            'message': f'Provisioned {success_count} shared component(s)'
        })
        
    except Exception as e:
        logger.error(f"Error provisioning components: {str(e)}")
        return create_response(500, {'error': f'Failed to provision components: {str(e)}'})


def list_available_components(event: Dict, user: Dict) -> Dict:
    """List available shared components from portal."""
    try:
        components = []
        
        for platform, config in DDA_LOCAL_SERVER_COMPONENTS.items():
            components.append({
                'component_name': config['name'],
                'description': config['description'],
                'platform': platform,
                'platforms': config['platforms'],
                'source': 'portal',
                'latest_version': '1.0.0'  # TODO: Get from portal
            })
        
        return create_response(200, {
            'components': components,
            'count': len(components)
        })
        
    except Exception as e:
        logger.error(f"Error listing available components: {str(e)}")
        return create_response(500, {'error': 'Failed to list available components'})


def list_shared_components(event: Dict, user: Dict) -> Dict:
    """List shared components for a usecase."""
    try:
        query_params = event.get('queryStringParameters') or {}
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check user access
        if not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Query shared components table
        table = dynamodb.Table(SHARED_COMPONENTS_TABLE)
        response = table.query(
            KeyConditionExpression='usecase_id = :uid',
            ExpressionAttributeValues={':uid': usecase_id}
        )
        
        components = response.get('Items', [])
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'components': components,
            'count': len(components)
        })
        
    except Exception as e:
        logger.error(f"Error listing shared components: {str(e)}")
        return create_response(500, {'error': 'Failed to list shared components'})
