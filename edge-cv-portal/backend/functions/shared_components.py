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

# DDA LocalServer component base configurations (versions discovered dynamically)
DDA_LOCAL_SERVER_COMPONENTS = {
    'arm64': {
        'name': 'aws.edgeml.dda.LocalServer.arm64',
        'description': 'DDA LocalServer for ARM64 devices (Jetson, Raspberry Pi)',
        'platforms': ['aarch64'],
        'arch_suffix': 'aarch64'  # Used in artifact filename
    },
    'amd64': {
        'name': 'aws.edgeml.dda.LocalServer.amd64', 
        'description': 'DDA LocalServer for AMD64 devices (x86_64)',
        'platforms': ['amd64', 'x86_64'],
        'arch_suffix': 'x86_64'  # Used in artifact filename
    }
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


def discover_artifact_key(component_name: str, version: str) -> Optional[str]:
    """
    Discover the artifact key in S3 for a component version.
    Lists the S3 bucket to find the actual artifact file.
    """
    try:
        prefix = f"{component_name}/{version}/"
        response = s3.list_objects_v2(
            Bucket=COMPONENT_BUCKET,
            Prefix=prefix
        )
        
        for obj in response.get('Contents', []):
            key = obj['Key']
            if key.endswith('.zip'):
                logger.info(f"Discovered artifact for {component_name} v{version}: {key}")
                return key
        
        logger.warning(f"No artifact found for {component_name} v{version} in {COMPONENT_BUCKET}")
        return None
        
    except ClientError as e:
        logger.error(f"Error discovering artifact: {str(e)}")
        return None


def get_component_info(platform: str) -> Optional[Dict]:
    """
    Get complete component info including dynamically discovered version and artifact.
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
    
    # Discover artifact key from S3
    artifact_key = discover_artifact_key(component_name, version)
    if not artifact_key:
        # Fallback: construct expected key based on naming convention
        artifact_key = f"{component_name}/{version}/{component_name}-{config['arch_suffix']}.zip"
        logger.warning(f"Using fallback artifact key: {artifact_key}")
    
    result = {
        **config,
        'version': version,
        'artifact_key': artifact_key
    }
    
    _component_cache[cache_key] = result
    return result


def get_all_component_info() -> Dict[str, Dict]:
    """
    Get info for all DDA LocalServer components with dynamically discovered versions.
    """
    result = {}
    for platform in DDA_LOCAL_SERVER_COMPONENTS.keys():
        info = get_component_info(platform)
        if info:
            result[platform] = info
    return result


# For backward compatibility - get a default version for display
def get_default_version() -> str:
    """Get the latest version from arm64 component for display purposes."""
    info = get_component_info('arm64')
    return info.get('version', '1.0.0') if info else '1.0.0'


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
        
        # Get dynamically discovered component info (includes version and artifact)
        config = get_component_info(platform)
        if not config:
            raise ValueError(f"Could not get component info for platform: {platform}")
        
        # Build artifact S3 URI using discovered artifact key
        artifact_s3_uri = f"s3://{COMPONENT_BUCKET}/{config['artifact_key']}"
        
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
            component_arn = f"arn:aws:greengrass:{AWS_REGION}:{usecase_account_id}:components:{component_name}:versions:{component_version}"
            status = 'already_exists'
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'ConflictException' or 'already exists' in str(e).lower():
                # Component already exists - this is OK, treat as success
                logger.info(f"Component {component_name} v{component_version} already exists in account {usecase_account_id}")
                component_arn = f"arn:aws:greengrass:{AWS_REGION}:{usecase_account_id}:components:{component_name}:versions:{component_version}"
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
    Provision all shared components (dda-LocalServer variants) for a new usecase.
    Called during usecase onboarding.
    
    Dynamically discovers the latest version and artifact for each component
    from the portal account's Greengrass and S3.
    
    Returns list of provisioned components.
    """
    results = []
    
    # Get dynamically discovered component info
    components = get_all_component_info()
    
    for platform, config in components.items():
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
        
        # Policy to allow reading from portal component bucket
        policy_document = {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Sid': 'AllowPortalComponentAccess',
                    'Effect': 'Allow',
                    'Action': ['s3:GetObject'],
                    'Resource': [
                        f'arn:aws:s3:::{COMPONENT_BUCKET}/*'
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


def add_usecase_to_component_bucket_policy(cross_account_role_arn: str) -> bool:
    """
    Add a usecase account's role to the GDK component bucket policy.
    This is called during usecase onboarding to grant the usecase account
    access to download component artifacts from the portal's S3 bucket.
    
    This runs in the Portal Account context (not cross-account).
    """
    try:
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
        
        # Find or create the cross-account access statement
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
            
            # Add new role if not already present
            if cross_account_role_arn not in aws_principals:
                aws_principals.append(cross_account_role_arn)
                existing_statement['Principal'] = {'AWS': aws_principals}
                current_policy['Statement'][statement_index] = existing_statement
                logger.info(f"Added {cross_account_role_arn} to existing bucket policy")
            else:
                logger.info(f"Role {cross_account_role_arn} already in bucket policy")
                return True
        else:
            # Create new statement
            new_statement = {
                'Sid': statement_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': [cross_account_role_arn]
                },
                'Action': ['s3:GetObject', 's3:GetObjectVersion'],
                'Resource': f'arn:aws:s3:::{COMPONENT_BUCKET}/*'
            }
            current_policy['Statement'].append(new_statement)
            logger.info(f"Created new bucket policy statement with {cross_account_role_arn}")
        
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
        
        # Step 2: Update the usecase account's IAM policy for Greengrass device access
        policy_updated = update_usecase_bucket_policy(
            usecase_account_id=usecase['account_id'],
            cross_account_role_arn=usecase['cross_account_role_arn'],
            external_id=usecase['external_id']
        )
        
        # Step 3: Provision shared components in the usecase account
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
                'bucket_policy_updated': bucket_policy_updated,
                'usecase_policy_updated': policy_updated
            }
        )
        
        success_count = len([r for r in results if r.get('status') in ('shared', 'already_exists')])
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'components': results,
            'bucket_policy_updated': bucket_policy_updated,
            'usecase_policy_updated': policy_updated,
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
        
        for platform, config in all_components.items():
            components.append({
                'component_name': config['name'],
                'description': config['description'],
                'platform': platform,
                'platforms': config['platforms'],
                'source': 'portal',
                'latest_version': config['version']
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
