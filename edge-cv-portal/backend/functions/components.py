import json
import boto3
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from botocore.exceptions import ClientError

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    get_user_from_event, 
    assume_cross_account_role,
    cors_headers,
    handle_error,
    check_user_access,
    create_boto3_client
)

def lambda_handler(event, context):
    """
    Handle Greengrass component management requests
    """
    try:
        # Apply CORS headers
        headers = cors_headers()
        
        # Handle CORS preflight
        if event.get('httpMethod') == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': headers,
                'body': ''
            }
        
        # Get user info from event (set by API Gateway authorizer)
        user_info = get_user_from_event(event)
        if not user_info or user_info.get('user_id') == 'unknown':
            return {
                'statusCode': 401,
                'headers': headers,
                'body': json.dumps({'error': 'Unauthorized'})
            }
        
        # Get HTTP method and path
        method = event['httpMethod']
        path = event['path']
        path_params = event.get('pathParameters') or {}
        query_params = event.get('queryStringParameters') or {}
        
        # Route to appropriate handler
        if method == 'GET' and path == '/components':
            return list_components(user_info, query_params, headers)
        elif method == 'GET' and path.startswith('/components/') and path_params.get('id'):
            component_arn = path_params.get('id')
            return get_component_details(user_info, component_arn, query_params, headers)
        elif method == 'DELETE' and path.startswith('/components/') and path_params.get('id'):
            component_arn = path_params.get('id')
            return delete_component(user_info, component_arn, query_params, headers)
        else:
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Not found'})
            }
            
    except Exception as e:
        return handle_error(e, headers)

def list_components(user_info: Dict, query_params: Dict, headers: Dict) -> Dict:
    """
    List Greengrass components for the current use case.
    Supports both PRIVATE (portal-managed) and PUBLIC (AWS-provided) components.
    """
    try:
        # Get current use case
        current_use_case = query_params.get('usecase_id')
        if not current_use_case:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'usecase_id parameter required'})
            }
        
        # Check permissions
        user_id = user_info.get('user_id')
        if not check_user_access(user_id, current_use_case):
            return {
                'statusCode': 403,
                'headers': headers,
                'body': json.dumps({'error': 'Insufficient permissions'})
            }
        
        # Get scope parameter - PRIVATE (portal-managed) or PUBLIC (AWS-provided)
        scope = query_params.get('scope', 'PRIVATE').upper()
        
        # Get use case details from DynamoDB
        dynamodb = boto3.resource('dynamodb')
        usecases_table = dynamodb.Table(os.environ['USECASES_TABLE'])
        
        response = usecases_table.get_item(Key={'usecase_id': current_use_case})
        if 'Item' not in response:
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Use case not found'})
            }
        
        use_case = response['Item']
        
        # Assume cross-account role
        cross_account_role_arn = use_case['cross_account_role_arn']
        external_id = use_case['external_id']
        
        credentials = assume_cross_account_role(cross_account_role_arn, external_id)
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        components = []
        
        if scope == 'PUBLIC':
            # List AWS public components using Greengrass API
            components = list_public_components(credentials, region, query_params)
        else:
            # List portal-managed private components using Resource Groups Tagging API
            components = list_private_components(credentials, region, query_params)
        
        # Apply additional filters
        if query_params.get('search'):
            search_term = query_params['search'].lower()
            components = [
                c for c in components 
                if search_term in c['component_name'].lower() or 
                   search_term in c.get('description', '').lower() or
                   search_term in c.get('model_name', '').lower()
            ]
        
        # Sort components
        sort_by = query_params.get('sort_by', 'component_name')
        reverse = query_params.get('sort_order', 'asc') == 'desc'
        
        if sort_by == 'component_name':
            components.sort(key=lambda x: x['component_name'], reverse=reverse)
        elif sort_by == 'creation_timestamp':
            components.sort(
                key=lambda x: x.get('creation_timestamp') or datetime.min, 
                reverse=reverse
            )
        
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({
                'components': components,
                'total_count': len(components)
            }, default=str)
        }
        
    except Exception as e:
        return handle_error(e, headers)


def list_public_components(credentials: Dict, region: str, query_params: Dict) -> List[Dict]:
    """
    List AWS public Greengrass components.
    These are AWS-provided components like aws.greengrass.Nucleus, aws.greengrass.Cli, etc.
    """
    try:
        # Create Greengrass client with assumed role (or default credentials for single-account)
        greengrass = create_boto3_client('greengrassv2', credentials, region)
        
        components = []
        next_token = None
        
        while True:
            params = {
                'scope': 'PUBLIC',
                'maxResults': 100
            }
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass.list_components(**params)
            
            for comp in response.get('components', []):
                component_name = comp.get('componentName', '')
                latest_version = comp.get('latestVersion', {})
                
                components.append({
                    'arn': comp.get('arn', ''),
                    'component_name': component_name,
                    'latest_version': {
                        'componentVersion': latest_version.get('componentVersion', 'unknown'),
                        'arn': latest_version.get('arn', ''),
                        'creationTimestamp': latest_version.get('creationTimestamp'),
                        'description': latest_version.get('description', ''),
                        'publisher': latest_version.get('publisher', 'AWS'),
                        'platforms': latest_version.get('platforms', [])
                    },
                    'description': latest_version.get('description', ''),
                    'publisher': latest_version.get('publisher', 'AWS'),
                    'creation_timestamp': latest_version.get('creationTimestamp'),
                    'status': 'DEPLOYABLE',
                    'platforms': latest_version.get('platforms', []),
                    'tags': {},
                    'model_name': '',
                    'training_job_id': '',
                    'created_by_portal': False,
                    'scope': 'PUBLIC',
                    'deployment_info': {
                        'total_deployments': 0,
                        'active_deployments': 0,
                        'deployed_devices': [],
                        'device_count': 0
                    }
                })
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        print(f"Found {len(components)} public AWS components")
        return components
        
    except ClientError as e:
        print(f"Error listing public components: {e}")
        raise e


def list_private_components(credentials: Dict, region: str, query_params: Dict) -> List[Dict]:
    """
    List portal-managed private components using Resource Groups Tagging API.
    These are components created by the DDA Portal (model components, etc.)
    Only returns the latest version of each component.
    """
    try:
        # Create Resource Groups Tagging API client with assumed role (or default credentials for single-account)
        tagging_client = create_boto3_client('resourcegroupstaggingapi', credentials, region)
        
        pagination_token = ''
        tagged_resources = []
        
        print(f"[DEBUG] Listing private components with credentials: is_default={credentials.get('is_default_credentials')}")
        
        while True:
            tag_params = {
                'TagFilters': [
                    {
                        'Key': 'dda-portal:managed',
                        'Values': ['true']
                    }
                ],
                # Don't use ResourceTypeFilters - it doesn't work reliably for Greengrass
                # We'll filter by ARN pattern instead
                'ResourcesPerPage': 100
            }
            
            if pagination_token:
                tag_params['PaginationToken'] = pagination_token
            
            tag_response = tagging_client.get_resources(**tag_params)
            
            print(f"[DEBUG] Tag response: {len(tag_response.get('ResourceTagMappingList', []))} resources found")
            
            # Filter to only Greengrass components by ARN pattern
            for resource in tag_response.get('ResourceTagMappingList', []):
                arn = resource.get('ResourceARN', '')
                print(f"[DEBUG] Checking resource: {arn}")
                if ':greengrass:' in arn and ':components:' in arn:
                    tagged_resources.append(resource)
                    print(f"[DEBUG] Added Greengrass component: {arn}")
            
            pagination_token = tag_response.get('PaginationToken', '')
            if not pagination_token:
                break
        
        print(f"Found {len(tagged_resources)} portal-created component versions via tagging API")
        
        # Deduplicate by component name, keeping only the latest version
        # Use a dict to track the latest version per component
        component_map = {}
        
        for resource in tagged_resources:
            component_arn = resource['ResourceARN']
            tags = {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}
            
            # Extract component name and version from ARN
            # ARN format: arn:aws:greengrass:region:account:components:name:versions:version
            # Index:      0   1   2          3      4       5          6    7        8
            arn_parts = component_arn.split(':')
            
            # Debug: print the ARN structure
            print(f"[DEBUG] Full ARN: {component_arn}")
            print(f"[DEBUG] ARN parts: {arn_parts}")
            print(f"[DEBUG] ARN parts count: {len(arn_parts)}")
            
            # Handle both formats:
            # 1. Full ARN with version: arn:aws:greengrass:region:account:components:name:versions:version
            # 2. Component ARN without version: arn:aws:greengrass:region:account:components:name
            
            if len(arn_parts) >= 9 and arn_parts[7] == 'versions':
                # Full ARN with version
                component_name = arn_parts[6]
                version_str = arn_parts[8]
                print(f"[DEBUG] Parsed as full ARN: component={component_name}, version={version_str}")
            elif len(arn_parts) >= 7:
                # Component ARN without version - need to query Greengrass for latest version
                component_name = arn_parts[6]
                version_str = '0.0.0'  # Default, will be updated from Greengrass
                print(f"[DEBUG] Parsed as component ARN without version: component={component_name}")
            else:
                print(f"[DEBUG] ERROR: Unexpected ARN format with {len(arn_parts)} parts")
                continue
            
            # Parse version for comparison (handle semver like 1.0.62)
            try:
                version_parts = [int(x) for x in version_str.split('.')]
                version_tuple = tuple(version_parts + [0] * (3 - len(version_parts)))  # Pad to 3 parts
            except ValueError:
                version_tuple = (0, 0, 0)
            
            # Check if this is a newer version than what we have
            if component_name not in component_map or version_tuple > component_map[component_name]['version_tuple']:
                component_map[component_name] = {
                    'arn': component_arn,
                    'version_str': version_str,
                    'version_tuple': version_tuple,
                    'tags': tags
                }
                print(f"[DEBUG] Updated component_map[{component_name}] = version {version_str}")
        
        # Build component list from deduplicated map
        # Create Greengrass client to fetch component details
        greengrass = create_boto3_client('greengrassv2', credentials, region)
        
        components = []
        for component_name, comp_data in component_map.items():
            # Fetch component details to get platforms information
            platforms = []
            description = ''
            creation_timestamp = None
            final_version = comp_data['version_str']
            
            try:
                # If version is 0.0.0, we need to query Greengrass for the latest version
                if final_version == '0.0.0':
                    print(f"[DEBUG] Version is 0.0.0 for {component_name}, querying Greengrass for latest version")
                    # List component versions to find the latest
                    versions_response = greengrass.list_component_versions(
                        arn=comp_data['arn'],
                        maxResults=1
                    )
                    if versions_response.get('componentVersions'):
                        latest_version_info = versions_response['componentVersions'][0]
                        final_version = latest_version_info.get('componentVersion', '0.0.0')
                        print(f"[DEBUG] Found latest version from Greengrass: {final_version}")
                
                # Now describe the component with the correct version
                # Build the full ARN with version if we have it
                if final_version != '0.0.0':
                    # Construct full ARN with version
                    arn_with_version = f"{comp_data['arn']}:versions:{final_version}"
                    print(f"[DEBUG] Describing component with full ARN: {arn_with_version}")
                    component_details = greengrass.describe_component(arn=arn_with_version)
                else:
                    # Use the ARN as-is
                    print(f"[DEBUG] Describing component with ARN: {comp_data['arn']}")
                    component_details = greengrass.describe_component(arn=comp_data['arn'])
                
                platforms = component_details.get('platforms', [])
                description = component_details.get('description', '')
                creation_timestamp = component_details.get('creationTimestamp')
                
                print(f"[DEBUG] Successfully described {component_name}: version={final_version}, platforms={len(platforms)}")
                
            except ClientError as e:
                print(f"Warning: Could not fetch details for {component_name} with ARN {comp_data['arn']}: {e}")
                # Continue with empty platforms if describe fails
            
            enriched_component = {
                'arn': comp_data['arn'],
                'component_name': component_name,
                'latest_version': {
                    'componentVersion': final_version,
                    'platforms': platforms
                },
                'description': description,
                'publisher': 'DDA Portal',
                'creation_timestamp': creation_timestamp,
                'status': 'DEPLOYABLE',
                'platforms': platforms,
                'tags': comp_data['tags'],
                'model_name': comp_data['tags'].get('dda-portal:model-name', ''),
                'training_job_id': comp_data['tags'].get('dda-portal:training-id', ''),
                'created_by_portal': True,
                'scope': 'PRIVATE',
                'deployment_info': {
                    'total_deployments': 0,
                    'active_deployments': 0,
                    'deployed_devices': [],
                    'device_count': 0
                }
            }
            components.append(enriched_component)
        
        print(f"Returning {len(components)} unique components (latest versions only)")
        return components
        
    except ClientError as e:
        print(f"Error listing private components: {e}")
        raise e


def get_component_details(user_info: Dict, component_arn: str, query_params: Dict, headers: Dict) -> Dict:
    """
    Get detailed information about a specific component
    """
    from urllib.parse import unquote
    
    try:
        if not component_arn:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'Component ARN required'})
            }
        
        # URL decode the ARN (may be double-encoded from API Gateway)
        component_arn = unquote(unquote(component_arn))
        
        # Get use case ID from query parameters (required for cross-account access)
        current_use_case = query_params.get('usecase_id')
        if not current_use_case:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'usecase_id parameter required'})
            }
        
        # Check permissions
        user_id = user_info.get('user_id')
        if not check_user_access(user_id, current_use_case):
            return {
                'statusCode': 403,
                'headers': headers,
                'body': json.dumps({'error': 'Insufficient permissions'})
            }
        
        # Get use case details from DynamoDB
        dynamodb = boto3.resource('dynamodb')
        usecases_table = dynamodb.Table(os.environ['USECASES_TABLE'])
        
        response = usecases_table.get_item(Key={'usecase_id': current_use_case})
        if 'Item' not in response:
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Use case not found'})
            }
        
        use_case = response['Item']
        
        # Assume cross-account role
        cross_account_role_arn = use_case['cross_account_role_arn']
        external_id = use_case['external_id']
        
        credentials = assume_cross_account_role(cross_account_role_arn, external_id)
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Create Greengrass client with assumed role
        greengrass = create_boto3_client('greengrassv2', credentials, region)
        
        # Get component details
        component_details = greengrass.describe_component(arn=component_arn)
        
        # Get component versions
        versions_response = greengrass.list_component_versions(
            arn=component_arn,
            maxResults=50
        )
        
        # Get deployment information
        deployment_info = get_component_deployment_info(current_use_case, component_arn)
        
        # Extract status - it's an object with componentState, not a string
        status_obj = component_details.get('status', {})
        if isinstance(status_obj, dict):
            component_status = status_obj.get('componentState', 'DEPLOYABLE')
        else:
            component_status = str(status_obj) if status_obj else 'DEPLOYABLE'
        
        # Process versions to extract status string from status object
        versions = []
        for v in versions_response.get('componentVersions', []):
            version_status = v.get('status', {})
            if isinstance(version_status, dict):
                v['status'] = version_status.get('componentState', 'DEPLOYABLE')
            versions.append(v)
        
        # Parse recipe - it's returned as bytes (YAML or JSON)
        recipe_data = component_details.get('recipe')
        parsed_recipe = {}
        if recipe_data:
            try:
                # Recipe is returned as bytes
                if isinstance(recipe_data, bytes):
                    recipe_str = recipe_data.decode('utf-8')
                else:
                    recipe_str = str(recipe_data)
                
                # Try parsing as JSON first
                try:
                    parsed_recipe = json.loads(recipe_str)
                except json.JSONDecodeError:
                    # If not JSON, try YAML
                    import yaml
                    parsed_recipe = yaml.safe_load(recipe_str)
            except Exception as e:
                print(f"Error parsing recipe: {e}")
                parsed_recipe = {'raw': recipe_str if 'recipe_str' in dir() else 'Unable to parse recipe'}
        
        # Combine all information
        detailed_component = {
            'arn': component_arn,
            'component_name': component_details['componentName'],
            'description': component_details.get('description', ''),
            'publisher': component_details.get('publisher', ''),
            'creation_timestamp': component_details.get('creationTimestamp'),
            'status': component_status,
            'platforms': component_details.get('platforms', []),
            'tags': component_details.get('tags', {}),
            'component_type': component_details.get('componentType', 'aws.greengrass.generic'),
            'versions': versions,
            'deployment_info': deployment_info,
            'recipe': parsed_recipe
        }
        
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps(detailed_component, default=str)
        }
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Component not found'})
            }
        return handle_error(e, headers)
    except Exception as e:
        return handle_error(e, headers)

def delete_component(user_info: Dict, component_arn: str, query_params: Dict, headers: Dict) -> Dict:
    """
    Delete a component (requires admin permissions)
    """
    from urllib.parse import unquote
    
    try:
        if not component_arn:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'Component ARN required'})
            }
        
        # URL decode the ARN (may be double-encoded from API Gateway)
        component_arn = unquote(unquote(component_arn))
        
        # Get use case ID from query parameters
        current_use_case = query_params.get('usecase_id')
        if not current_use_case:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'usecase_id parameter required'})
            }
        
        # Check admin permissions
        user_id = user_info.get('user_id')
        if not check_user_access(user_id, current_use_case, 'UseCaseAdmin'):
            return {
                'statusCode': 403,
                'headers': headers,
                'body': json.dumps({'error': 'Admin permissions required'})
            }
        
        # Check if component is deployed before deletion
        deployment_info = get_component_deployment_info(current_use_case, component_arn)
        if deployment_info['deployed_devices']:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({
                    'error': 'Cannot delete component that is currently deployed',
                    'deployed_devices': deployment_info['deployed_devices']
                })
            }
        
        # Get use case details from DynamoDB
        dynamodb = boto3.resource('dynamodb')
        usecases_table = dynamodb.Table(os.environ['USECASES_TABLE'])
        
        response = usecases_table.get_item(Key={'usecase_id': current_use_case})
        if 'Item' not in response:
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Use case not found'})
            }
        
        use_case = response['Item']
        
        # Assume cross-account role
        cross_account_role_arn = use_case['cross_account_role_arn']
        external_id = use_case['external_id']
        
        credentials = assume_cross_account_role(cross_account_role_arn, external_id)
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Create Greengrass client with assumed role
        greengrass = create_boto3_client('greengrassv2', credentials, region)
        
        # Delete component (this will delete all versions)
        try:
            greengrass.delete_component(arn=component_arn)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return {
                    'statusCode': 404,
                    'headers': headers,
                    'body': json.dumps({'error': 'Component not found'})
                }
            raise e
        
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({'message': 'Component deleted successfully'})
        }
        
    except Exception as e:
        return handle_error(e, headers)

def get_component_deployment_info(use_case_id: str, component_arn: str) -> Dict:
    """
    Get deployment information for a component
    """
    try:
        dynamodb = boto3.resource('dynamodb')
        
        # Check deployments table
        deployments_table = dynamodb.Table(os.environ.get('DEPLOYMENTS_TABLE', 'Deployments'))
        
        # Query for deployments of this component
        response = deployments_table.scan(
            FilterExpression='component_arn = :arn AND usecase_id = :usecase',
            ExpressionAttributeValues={
                ':arn': component_arn,
                ':usecase': use_case_id
            }
        )
        
        deployments = response.get('Items', [])
        
        # Get deployed devices
        deployed_devices = []
        active_deployments = 0
        
        for deployment in deployments:
            if deployment.get('status') in ['completed', 'in_progress']:
                active_deployments += 1
                deployed_devices.extend(deployment.get('target_devices', []))
        
        # Remove duplicates
        deployed_devices = list(set(deployed_devices))
        
        return {
            'total_deployments': len(deployments),
            'active_deployments': active_deployments,
            'deployed_devices': deployed_devices,
            'device_count': len(deployed_devices)
        }
        
    except Exception as e:
        print(f"Error getting deployment info: {e}")
        return {
            'total_deployments': 0,
            'active_deployments': 0,
            'deployed_devices': [],
            'device_count': 0
        }