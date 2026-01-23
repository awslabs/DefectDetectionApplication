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
PORTAL_ACCOUNT_ID = os.environ.get('PORTAL_ACCOUNT_ID')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Component bucket follows GDK naming convention: {bucket_prefix}-{region}-{account_id}
COMPONENT_BUCKET_PREFIX = os.environ.get('COMPONENT_BUCKET_PREFIX', 'dda-component')
COMPONENT_BUCKET = os.environ.get('COMPONENT_BUCKET', f'{COMPONENT_BUCKET_PREFIX}-{AWS_REGION}-{PORTAL_ACCOUNT_ID}')

# DDA shared components (versions discovered dynamically from Greengrass)
DDA_LOCAL_SERVER_COMPONENTS = {
    'arm64': {
        'name': 'aws.edgeml.dda.LocalServer.arm64',
        'description': 'DDA LocalServer for ARM64 devices (Jetson, Raspberry Pi)',
        'platforms': ['linux/arm64']
    },
    'amd64': {
        'name': 'aws.edgeml.dda.LocalServer.amd64', 
        'description': 'DDA LocalServer for AMD64 devices (x86_64)',
        'platforms': ['linux/amd64', 'linux/x86_64']
    }
}

# DDA InferenceUploader component (platform-independent)
DDA_INFERENCE_UPLOADER_COMPONENT = {
    'name': 'aws.edgeml.dda.InferenceUploader',
    'description': 'Uploads inference results (images and metadata) from edge devices to S3',
    'platforms': ['linux']  # Works on all platforms
}

# Cache for discovered component info (refreshed per Lambda invocation)
_component_cache = {}


def get_latest_component_version(component_name: str) -> Optional[str]:
    """
    Query the portal account's Greengrass to get the latest version of a component.
    Returns the latest version string or None if component not found.
    """
    try:
        response = greengrass_portal.list_component_versions(
            arn=f"arn:aws:greengrass:{AWS_REGION}:{PORTAL_ACCOUNT_ID}:components:{component_name}"
        )
        
        versions = response.get('componentVersions', [])
        if versions:
            # Versions are returned in descending order (latest first)
            latest = versions[0].get('componentVersion')
            logger.info(f"Found latest version for {component_name}: {latest}")
            return latest
        
        logger.warning(f"No versions found for component {component_name}")
        return None
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            logger.warning(f"Component {component_name} not found in portal account")
        else:
            logger.error(f"Error getting component versions: {str(e)}")
        return None


def get_component_info(platform: str) -> Optional[Dict]:
    """
    Get component info including dynamically discovered version.
    Uses caching to avoid repeated API calls within the same Lambda invocation.
    """
    cache_key = f"component_{platform}"
    
    if cache_key in _component_cache:
        return _component_cache[cache_key]
    
    config = DDA_LOCAL_SERVER_COMPONENTS.get(platform)
    if not config:
        return None
    
    component_name = config['name']
    
    # Get latest version from Greengrass
    version = get_latest_component_version(component_name)
    if not version:
        logger.error(f"Could not determine version for {component_name}")
        return None
    
    result = {
        **config,
        'version': version
    }
    
    _component_cache[cache_key] = result
    return result


def get_all_component_info() -> Dict[str, Dict]:
    """
    Get info for all DDA shared components with dynamically discovered versions.
    Returns both LocalServer (platform-specific) and InferenceUploader (platform-independent).
    """
    result = {}
    
    # Add LocalServer components (platform-specific)
    for platform in DDA_LOCAL_SERVER_COMPONENTS.keys():
        info = get_component_info(platform)
        if info:
            result[f'localserver_{platform}'] = info
    
    # Add InferenceUploader component (platform-independent)
    uploader_version = get_latest_component_version(DDA_INFERENCE_UPLOADER_COMPONENT['name'])
    if uploader_version:
        result['inference_uploader'] = {
            **DDA_INFERENCE_UPLOADER_COMPONENT,
            'version': uploader_version
        }
    
    return result


# For backward compatibility - get a default version for display
def get_default_version() -> str:
    """Get the latest version from arm64 component for display purposes."""
    info = get_component_info('arm64')
    return info.get('version', '1.0.0') if info else '1.0.0'


def get_portal_component_recipe(component_name: str, version: str) -> Optional[Dict]:
    """
    Get the recipe for a portal-managed component.
    This retrieves the component recipe from the portal account using get_component API.
    """
    region = os.environ.get('AWS_REGION', 'us-east-1')
    component_arn = f"arn:aws:greengrass:{region}:{PORTAL_ACCOUNT_ID}:components:{component_name}:versions:{version}"
    
    logger.info(f"Fetching recipe for component ARN: {component_arn}")
    
    try:
        # Use get_component to retrieve the recipe (returns base64-encoded recipe)
        response = greengrass_portal.get_component(
            arn=component_arn,
            recipeOutputFormat='JSON'
        )
        
        # Recipe is returned as base64-encoded bytes
        recipe_data = response.get('recipe')
        if not recipe_data:
            logger.error(f"No recipe returned for {component_arn}")
            return None
        
        # Decode the recipe (it's a bytes object containing the recipe)
        if isinstance(recipe_data, bytes):
            recipe_str = recipe_data.decode('utf-8')
        else:
            recipe_str = str(recipe_data)
        
        logger.info(f"Successfully fetched recipe for {component_name} v{version}, length: {len(recipe_str)}")
        
        # Parse as JSON (we requested JSON format)
        return json.loads(recipe_str)
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        error_msg = e.response.get('Error', {}).get('Message', '')
        logger.error(f"ClientError getting recipe for {component_arn}: {error_code} - {error_msg}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing recipe JSON for {component_arn}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error getting recipe for {component_arn}: {type(e).__name__} - {str(e)}")
        return None



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
        elif 'InferenceUploader' in component_name:
            platform = 'all'  # Platform-independent
        else:
            raise ValueError(f"Cannot determine platform from component name: {component_name}")
        
        # Fetch the existing recipe from the portal account
        # This ensures the usecase account gets the exact same recipe that was published by GDK
        recipe = get_portal_component_recipe(component_name, component_version)
        
        if not recipe:
            raise ValueError(
                f"Could not fetch recipe for {component_name} v{component_version} from portal account. "
                f"Ensure the component has been built and published using gdk-component-build-and-publish.sh"
            )
        
        logger.info(f"Using existing recipe from portal account for {component_name} v{component_version}")
        
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
        
        component_arn = f"arn:aws:greengrass:{AWS_REGION}:{usecase_account_id}:components:{component_name}:versions:{component_version}"
        
        # Try to create component in usecase account
        try:
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
            status = 'shared'
            
        except greengrass_usecase.exceptions.ConflictException:
            # Component already exists - this is OK, treat as success
            logger.info(f"Component {component_name} v{component_version} already exists in account {usecase_account_id}")
            status = 'already_exists'
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'ConflictException' or 'already exists' in str(e).lower():
                # Component already exists - this is OK, treat as success
                logger.info(f"Component {component_name} v{component_version} already exists in account {usecase_account_id}")
                status = 'already_exists'
            else:
                raise
        
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
            'status': status
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
    component_version: str = None  # Deprecated - versions discovered dynamically
) -> List[Dict]:
    """
    Provision all shared components for a new usecase.
    Called during usecase onboarding.
    
    Provisions:
    - DDA LocalServer (arm64 and amd64 variants)
    - DDA InferenceUploader (platform-independent)
    
    Dynamically discovers the latest version for each component.
    
    Returns list of provisioned components.
    """
    results = []
    
    # Get dynamically discovered component info
    components = get_all_component_info()
    
    # Provision LocalServer components (platform-specific)
    for platform in DDA_LOCAL_SERVER_COMPONENTS.keys():
        component_key = f'localserver_{platform}'
        if component_key not in components:
            logger.warning(f"LocalServer component for {platform} not found")
            continue
            
        config = components[component_key]
        try:
            platform_version = config['version']
            
            result = share_component_to_usecase(
                usecase_id=usecase_id,
                usecase_account_id=usecase_account_id,
                cross_account_role_arn=cross_account_role_arn,
                external_id=external_id,
                component_name=config['name'],
                component_version=platform_version,
                user_id=user_id
            )
            results.append(result)
            logger.info(f"Provisioned {config['name']} v{platform_version} for usecase {usecase_id}")
            
        except Exception as e:
            logger.error(f"Failed to provision {config['name']} for usecase {usecase_id}: {str(e)}")
            results.append({
                'component_name': config['name'],
                'platform': platform,
                'status': 'failed',
                'error': str(e)
            })
    
    # Provision InferenceUploader component (platform-independent)
    if 'inference_uploader' in components:
        config = components['inference_uploader']
        try:
            result = share_component_to_usecase(
                usecase_id=usecase_id,
                usecase_account_id=usecase_account_id,
                cross_account_role_arn=cross_account_role_arn,
                external_id=external_id,
                component_name=config['name'],
                component_version=config['version'],
                user_id=user_id
            )
            # Mark as platform-independent
            result['platform'] = 'all'
            results.append(result)
            logger.info(f"Provisioned {config['name']} v{config['version']} for usecase {usecase_id}")
            
        except Exception as e:
            logger.error(f"Failed to provision {config['name']} for usecase {usecase_id}: {str(e)}")
            results.append({
                'component_name': config['name'],
                'platform': 'all',
                'status': 'failed',
                'error': str(e)
            })
    
    return results


def add_usecase_to_component_bucket_policy(cross_account_role_arn: str) -> bool:
    """
    Add a usecase account's roles to the GDK component bucket policy.
    This is called during usecase onboarding to grant the usecase account
    access to download component artifacts from the portal's S3 bucket.
    
    Adds:
    1. DDAPortalAccessRole and GreengrassV2TokenExchangeRole for device access
    2. Greengrass service principal for component creation validation
    
    This runs in the Portal Account context (not cross-account).
    """
    try:
        # Extract account ID from the cross_account_role_arn
        # Format: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME
        account_id = cross_account_role_arn.split(':')[4]
        
        # Build list of role ARNs to add
        # Include both the portal access role and the Greengrass device role
        role_arns_to_add = [
            cross_account_role_arn,  # DDAPortalAccessRole
            f'arn:aws:iam::{account_id}:role/GreengrassV2TokenExchangeRole'  # Greengrass device role
        ]
        
        # Get current bucket policy
        try:
            response = s3.get_bucket_policy(Bucket=COMPONENT_BUCKET)
            current_policy = json.loads(response['Policy'])
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucketPolicy':
                # No policy exists, create a new one
                current_policy = {
                    'Version': '2012-10-17',
                    'Statement': []
                }
            else:
                raise
        
        # Statement 1: Cross-account IAM role access
        statement_sid = 'AllowUseCaseAccountsGreengrassAccess'
        existing_statement = None
        statement_index = None
        
        for i, stmt in enumerate(current_policy.get('Statement', [])):
            if stmt.get('Sid') == statement_sid:
                existing_statement = stmt
                statement_index = i
                break
        
        if existing_statement:
            # Get existing principals
            principal = existing_statement.get('Principal', {})
            if isinstance(principal, dict):
                aws_principals = principal.get('AWS', [])
                if isinstance(aws_principals, str):
                    aws_principals = [aws_principals]
            else:
                aws_principals = []
            
            # Add new roles if not already present
            for role_arn in role_arns_to_add:
                if role_arn not in aws_principals:
                    aws_principals.append(role_arn)
            
            # Update the statement with correct actions and resources
            existing_statement['Principal'] = {'AWS': aws_principals}
            existing_statement['Action'] = ['s3:GetObject', 's3:GetObjectVersion', 's3:GetBucketLocation']
            existing_statement['Resource'] = [
                f'arn:aws:s3:::{COMPONENT_BUCKET}',
                f'arn:aws:s3:::{COMPONENT_BUCKET}/*'
            ]
            current_policy['Statement'][statement_index] = existing_statement
            logger.info(f"Updated bucket policy with roles from account {account_id}")
        else:
            # Create new statement with both roles
            new_statement = {
                'Sid': statement_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': role_arns_to_add
                },
                'Action': ['s3:GetObject', 's3:GetObjectVersion', 's3:GetBucketLocation'],
                'Resource': [
                    f'arn:aws:s3:::{COMPONENT_BUCKET}',
                    f'arn:aws:s3:::{COMPONENT_BUCKET}/*'
                ]
            }
            current_policy['Statement'].append(new_statement)
            logger.info(f"Created new bucket policy statement with roles from account {account_id}")
        
        # Statement 2: Greengrass service principal access (for component creation validation)
        service_statement_sid = 'AllowGreengrassServiceAccess'
        service_statement_exists = False
        service_statement_index = None
        
        for i, stmt in enumerate(current_policy.get('Statement', [])):
            if stmt.get('Sid') == service_statement_sid:
                service_statement_exists = True
                service_statement_index = i
                # Update the condition to include the new account
                existing_condition = stmt.get('Condition', {})
                source_accounts = existing_condition.get('StringEquals', {}).get('aws:SourceAccount', [])
                if isinstance(source_accounts, str):
                    source_accounts = [source_accounts]
                if account_id not in source_accounts:
                    source_accounts.append(account_id)
                stmt['Condition'] = {
                    'StringEquals': {
                        'aws:SourceAccount': source_accounts
                    }
                }
                current_policy['Statement'][i] = stmt
                logger.info(f"Updated Greengrass service statement with account {account_id}")
                break
        
        if not service_statement_exists:
            # Get portal account ID for the condition
            portal_account_id = os.environ.get('AWS_ACCOUNT_ID', '')
            if not portal_account_id:
                # Try to get from STS
                try:
                    sts = boto3.client('sts')
                    portal_account_id = sts.get_caller_identity()['Account']
                except:
                    portal_account_id = COMPONENT_BUCKET.split('-')[-1]  # Fallback: extract from bucket name
            
            service_statement = {
                'Sid': service_statement_sid,
                'Effect': 'Allow',
                'Principal': {
                    'Service': 'greengrass.amazonaws.com'
                },
                'Action': ['s3:GetObject', 's3:GetBucketLocation'],
                'Resource': [
                    f'arn:aws:s3:::{COMPONENT_BUCKET}',
                    f'arn:aws:s3:::{COMPONENT_BUCKET}/*'
                ],
                'Condition': {
                    'StringEquals': {
                        'aws:SourceAccount': [portal_account_id, account_id]
                    }
                }
            }
            current_policy['Statement'].append(service_statement)
            logger.info(f"Created Greengrass service statement for accounts {portal_account_id}, {account_id}")
        
        # Apply updated policy
        s3.put_bucket_policy(
            Bucket=COMPONENT_BUCKET,
            Policy=json.dumps(current_policy)
        )
        
        logger.info(f"Successfully updated bucket policy for {COMPONENT_BUCKET}")
        return True
        
    except ClientError as e:
        logger.error(f"Error updating component bucket policy: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error updating component bucket policy: {str(e)}")
        return False


def handler(event: Dict, context: Any) -> Dict:
    """
    Lambda handler for shared components management.
    
    POST /api/v1/shared-components/provision
        Provision shared components for a usecase (called during onboarding)
    
    POST /api/v1/shared-components/update-all
        Update shared components for all usecases to latest version (portal admin only)
    
    GET /api/v1/shared-components
        List shared components for a usecase
    
    GET /api/v1/shared-components/available
        List available shared components from portal
    
    GET /api/v1/shared-components/status
        Get update status for all usecases (portal admin only)
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
        elif http_method == 'POST' and '/update-all' in path:
            return update_all_usecases(event, user)
        elif http_method == 'GET' and '/available' in path:
            return list_available_components(event, user)
        elif http_method == 'GET' and '/status' in path:
            return get_update_status(event, user)
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
        # component_version is deprecated - versions are now discovered dynamically per platform
        
        # Get usecase details
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Usecase not found'})
        
        usecase = response['Item']
        
        # Check user has permission (pass user dict for JWT role lookup)
        if not check_user_access(user['user_id'], usecase_id, 'UseCaseAdmin', user):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Step 1: Add usecase role to the GDK component bucket policy (Portal Account)
        # This allows the usecase account to download component artifacts
        bucket_policy_updated = add_usecase_to_component_bucket_policy(
            cross_account_role_arn=usecase['cross_account_role_arn']
        )
        
        if not bucket_policy_updated:
            logger.warning(f"Failed to update component bucket policy for usecase {usecase_id}")
        
        # Note: Device IAM policy (DDAPortalComponentAccessPolicy) is created by UseCaseAccountStack CDK
        # and attached to GreengrassV2TokenExchangeRole by setup_station.sh during device provisioning
        
        # Step 2: Provision shared components in the usecase account
        # Each platform uses its own version from DDA_LOCAL_SERVER_COMPONENTS config
        results = provision_shared_components_for_usecase(
            usecase_id=usecase_id,
            usecase_account_id=usecase['account_id'],
            cross_account_role_arn=usecase['cross_account_role_arn'],
            external_id=usecase['external_id'],
            user_id=user['user_id']
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
                'bucket_policy_updated': bucket_policy_updated
            }
        )
        
        success_count = len([r for r in results if r.get('status') in ('shared', 'already_exists')])
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'components': results,
            'bucket_policy_updated': bucket_policy_updated,
            'message': f'Provisioned {success_count} shared component(s)'
        })
        
    except Exception as e:
        logger.error(f"Error provisioning components: {str(e)}")
        return create_response(500, {'error': f'Failed to provision components: {str(e)}'})


def list_available_components(event: Dict, user: Dict) -> Dict:
    """List available shared components from portal with dynamically discovered version info."""
    try:
        components = []
        
        # Get dynamically discovered component info
        all_components = get_all_component_info()
        
        # Add LocalServer components
        for platform in DDA_LOCAL_SERVER_COMPONENTS.keys():
            component_key = f'localserver_{platform}'
            if component_key in all_components:
                config = all_components[component_key]
                components.append({
                    'component_name': config['name'],
                    'description': config['description'],
                    'platform': platform,
                    'platforms': config.get('platforms', []),
                    'source': 'portal',
                    'latest_version': config['version'],
                    'component_type': 'local-server'
                })
        
        # Add InferenceUploader component
        if 'inference_uploader' in all_components:
            config = all_components['inference_uploader']
            components.append({
                'component_name': config['name'],
                'description': config['description'],
                'platform': 'all',
                'platforms': config.get('platforms', []),
                'source': 'portal',
                'latest_version': config['version'],
                'component_type': 'inference-uploader'
            })
        
        # Get default version for display
        default_version = get_default_version()
        
        return create_response(200, {
            'components': components,
            'count': len(components),
            'latest_version': default_version
        })
        
    except Exception as e:
        logger.error(f"Error listing available components: {str(e)}")
        return create_response(500, {'error': 'Failed to list available components'})


def list_shared_components(event: Dict, user: Dict) -> Dict:
    """List shared components for a usecase with version status."""
    try:
        query_params = event.get('queryStringParameters') or {}
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check user access (pass user dict for JWT role lookup)
        if not check_user_access(user['user_id'], usecase_id, None, user):
            return create_response(403, {'error': 'Access denied'})
        
        # Query shared components table
        table = dynamodb.Table(SHARED_COMPONENTS_TABLE)
        response = table.query(
            KeyConditionExpression='usecase_id = :uid',
            ExpressionAttributeValues={':uid': usecase_id}
        )
        
        components = response.get('Items', [])
        
        # Get dynamically discovered component info for version comparison
        all_component_info = get_all_component_info()
        default_version = get_default_version()
        
        # Add update_available flag by comparing versions
        for comp in components:
            current_version = comp.get('component_version', '0.0.0')
            platform = comp.get('platform', 'arm64')
            
            # Get latest version for this specific platform
            platform_info = all_component_info.get(platform, {})
            latest_version = platform_info.get('version', default_version)
            
            comp['update_available'] = current_version != latest_version
            comp['latest_version'] = latest_version
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'components': components,
            'count': len(components),
            'latest_version': default_version
        })
        
    except Exception as e:
        logger.error(f"Error listing shared components: {str(e)}")
        return create_response(500, {'error': 'Failed to list shared components'})


def get_update_status(event: Dict, user: Dict) -> Dict:
    """
    Get shared component update status for all usecases.
    Portal admin only - shows which usecases need updates.
    """
    try:
        # Check if user is portal admin
        if user.get('role') != 'PortalAdmin':
            return create_response(403, {'error': 'Portal admin access required'})
        
        # Get dynamically discovered component info
        all_component_info = get_all_component_info()
        default_version = get_default_version()
        
        # Get all usecases
        usecases_table = dynamodb.Table(USECASES_TABLE)
        usecases_response = usecases_table.scan()
        usecases = usecases_response.get('Items', [])
        
        # Get shared components for each usecase
        components_table = dynamodb.Table(SHARED_COMPONENTS_TABLE)
        
        status_list = []
        usecases_needing_update = 0
        
        for usecase in usecases:
            usecase_id = usecase['usecase_id']
            
            # Query components for this usecase
            comp_response = components_table.query(
                KeyConditionExpression='usecase_id = :uid',
                ExpressionAttributeValues={':uid': usecase_id}
            )
            components = comp_response.get('Items', [])
            
            # Check if any component needs update
            needs_update = False
            component_versions = []
            
            for comp in components:
                current_version = comp.get('component_version', '0.0.0')
                platform = comp.get('platform', 'arm64')
                
                # Get latest version for this specific platform
                platform_info = all_component_info.get(platform, {})
                latest_version = platform_info.get('version', default_version)
                
                update_available = current_version != latest_version
                if update_available:
                    needs_update = True
                component_versions.append({
                    'component_name': comp.get('component_name'),
                    'current_version': current_version,
                    'latest_version': latest_version,
                    'update_available': update_available,
                    'status': comp.get('status', 'unknown')
                })
            
            if needs_update:
                usecases_needing_update += 1
            
            status_list.append({
                'usecase_id': usecase_id,
                'usecase_name': usecase.get('name', 'Unknown'),
                'account_id': usecase.get('account_id'),
                'needs_update': needs_update,
                'components': component_versions,
                'shared_components_provisioned': usecase.get('shared_components_provisioned', False)
            })
        
        return create_response(200, {
            'usecases': status_list,
            'total_usecases': len(usecases),
            'usecases_needing_update': usecases_needing_update,
            'latest_version': default_version
        })
        
    except Exception as e:
        logger.error(f"Error getting update status: {str(e)}")
        return create_response(500, {'error': 'Failed to get update status'})


def update_all_usecases(event: Dict, user: Dict) -> Dict:
    """
    Update shared components for all usecases to the latest version.
    Portal admin only.
    """
    try:
        # Check if user is portal admin
        if user.get('role') != 'PortalAdmin':
            return create_response(403, {'error': 'Portal admin access required'})
        
        body = json.loads(event.get('body', '{}'))
        # target_version is deprecated - versions are now discovered dynamically per platform
        target_version = body.get('version', get_default_version())
        usecase_ids = body.get('usecase_ids')  # Optional: specific usecases to update
        
        # Get usecases to update
        usecases_table = dynamodb.Table(USECASES_TABLE)
        
        if usecase_ids:
            # Update specific usecases
            usecases = []
            for uid in usecase_ids:
                response = usecases_table.get_item(Key={'usecase_id': uid})
                if 'Item' in response:
                    usecases.append(response['Item'])
        else:
            # Update all usecases
            response = usecases_table.scan()
            usecases = response.get('Items', [])
        
        results = []
        success_count = 0
        failed_count = 0
        
        for usecase in usecases:
            usecase_id = usecase['usecase_id']
            
            try:
                # Provision/update shared components (each platform uses its own version)
                provision_results = provision_shared_components_for_usecase(
                    usecase_id=usecase_id,
                    usecase_account_id=usecase['account_id'],
                    cross_account_role_arn=usecase['cross_account_role_arn'],
                    external_id=usecase['external_id'],
                    user_id=user['user_id']
                )
                
                # Check results - both 'shared' and 'already_exists' are success
                usecase_success = all(r.get('status') in ('shared', 'already_exists') for r in provision_results)
                
                if usecase_success:
                    success_count += 1
                else:
                    failed_count += 1
                
                results.append({
                    'usecase_id': usecase_id,
                    'usecase_name': usecase.get('name'),
                    'status': 'success' if usecase_success else 'partial_failure',
                    'components': provision_results
                })
                
            except Exception as e:
                failed_count += 1
                results.append({
                    'usecase_id': usecase_id,
                    'usecase_name': usecase.get('name'),
                    'status': 'failed',
                    'error': str(e)
                })
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='bulk_update_shared_components',
            resource_type='shared_components',
            resource_id='all',
            result='success' if failed_count == 0 else 'partial',
            details={
                'target_version': target_version,
                'total_usecases': len(usecases),
                'success_count': success_count,
                'failed_count': failed_count
            }
        )
        
        return create_response(200, {
            'message': f'Updated {success_count} usecase(s), {failed_count} failed',
            'target_version': target_version,
            'results': results,
            'success_count': success_count,
            'failed_count': failed_count
        })
        
    except Exception as e:
        logger.error(f"Error updating all usecases: {str(e)}")
        return create_response(500, {'error': f'Failed to update usecases: {str(e)}'})
