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
    check_user_access
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
    Uses Resource Groups Tagging API to efficiently filter portal-created components.
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
        
        # Create Resource Groups Tagging API client with assumed role
        # This allows efficient filtering of portal-created components by tag
        tagging_client = boto3.client(
            'resourcegroupstaggingapi',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=region
        )
        
        components = []
        
        try:
            # Use Resource Groups Tagging API to find portal-created components
            # Single API call with tag filter - much faster than N+1 describe calls
            pagination_token = ''
            tagged_resources = []
            
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
                
                # Filter to only Greengrass components by ARN pattern
                for resource in tag_response.get('ResourceTagMappingList', []):
                    arn = resource.get('ResourceARN', '')
                    if ':greengrass:' in arn and ':components:' in arn:
                        tagged_resources.append(resource)
                
                pagination_token = tag_response.get('PaginationToken', '')
                if not pagination_token:
                    break
            
            print(f"Found {len(tagged_resources)} portal-created components via tagging API")
            
            # Build component list from tagged resources
            for resource in tagged_resources:
                component_arn = resource['ResourceARN']
                tags = {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}
                
                # Extract component name from ARN
                # ARN format: arn:aws:greengrass:region:account:components:name:versions:version
                # Index:      0   1   2          3      4       5          6    7        8
                arn_parts = component_arn.split(':')
                component_name = arn_parts[6] if len(arn_parts) > 6 else 'unknown'
                
                # Get version from ARN if present (index 8)
                latest_version = arn_parts[8] if len(arn_parts) > 8 else 'unknown'
                
                # Build component info from tags (no extra API calls needed)
                enriched_component = {
                    'arn': component_arn,
                    'component_name': component_name,
                    'latest_version': {'componentVersion': latest_version},
                    'description': '',
                    'publisher': 'DDA Portal',
                    'creation_timestamp': None,
                    'status': 'DEPLOYABLE',
                    'platforms': [],
                    'tags': tags,
                    'model_name': tags.get('dda-portal:model-name', ''),
                    'training_job_id': tags.get('dda-portal:training-id', ''),
                    'created_by_portal': True,
                    'deployment_info': {
                        'total_deployments': 0,
                        'active_deployments': 0,
                        'deployed_devices': [],
                        'device_count': 0
                    }
                }
                
                components.append(enriched_component)
                
        except ClientError as e:
            print(f"Error listing components: {e}")
            return {
                'statusCode': 500,
                'headers': headers,
                'body': json.dumps({'error': f'Failed to list components: {str(e)}'})
            }
        
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
        
        # Create Greengrass client with assumed role
        greengrass = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )
        
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
        
        # Create Greengrass client with assumed role
        greengrass = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )
        
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