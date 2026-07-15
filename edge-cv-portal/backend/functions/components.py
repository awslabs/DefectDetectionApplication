import json
import boto3
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from botocore.exceptions import ClientError

# Import shared utilities
import sys
sys.path.append('/opt/python')


def _timestamp_sort_key(component: Dict[str, Any]) -> float:
    """
    Return a comparable epoch-seconds value for a component's creation timestamp.

    boto3 returns timezone-aware datetimes, but timestamps may also be missing
    (None), ISO strings, or numeric. Normalizing everything to a float avoids
    "can't compare offset-naive and offset-aware datetimes" errors during sort.
    """
    value = component.get('creation_timestamp')
    if value is None:
        return float('-inf')
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return float('-inf')
    return float('-inf')
from shared_utils import (
    get_user_from_event, 
    assume_cross_account_role,
    cors_headers,
    handle_error,
    check_user_access,
    create_boto3_client,
    get_usecase_region
)


def _version_key(version_str):
    """Convert a semantic version string to a comparable tuple so versions sort
    numerically (1.0.115 > 1.0.63), not lexicographically. Non-numeric parts sort
    last."""
    parts = []
    for part in str(version_str).split('.'):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def _resolve_latest_component_version(greengrass, base_arn):
    """Return the highest semantic version of a component, or None if it has none.

    Pages through every version and selects the max by semver. We deliberately do
    NOT trust a version embedded in a discovery tag: a stale version-level tag
    (e.g. an old shared-component mirror at 1.0.63) must never mask the component's
    true latest version (e.g. 1.0.115)."""
    versions = []
    next_token = None
    try:
        while True:
            params = {'arn': base_arn}
            if next_token:
                params['nextToken'] = next_token
            resp = greengrass.list_component_versions(**params)
            for cv in resp.get('componentVersions', []):
                v = cv.get('componentVersion')
                if v:
                    versions.append(v)
            next_token = resp.get('nextToken')
            if not next_token:
                break
    except ClientError as e:
        print(f"Warning: could not list versions for {base_arn}: {e}")
        return None
    return max(versions, key=_version_key) if versions else None


# ---------------------------------------------------------------------------
# Plugin_Component listing (custom-node-designer, Requirement 16.2)
#
# Node Designer plugins are auto-packaged as Greengrass components named
# `dda.plugin.{pluginId}` and tagged with `dda-portal:plugin-id` /
# `dda-portal:plugin-version` (see plugin_components.py registry_tags in the
# node-designer Lambda bundle). The deployment screen listing joins them with
# their backing Plugin_Record (PLUGIN_RECORDS_TABLE) to show the record's
# Lifecycle_State, and derives the supported Target_Architectures from the
# recipe's platform manifests.

# Keep in sync with plugin_components.py PLUGIN_COMPONENT_PREFIX.
PLUGIN_COMPONENT_PREFIX = 'dda.plugin.'

TAG_PLUGIN_ID = 'dda-portal:plugin-id'
TAG_PLUGIN_VERSION = 'dda-portal:plugin-version'


def is_plugin_component(component_name: str) -> bool:
    """True when a component is a Node Designer Plugin_Component (16.2)"""
    return str(component_name).startswith(PLUGIN_COMPONENT_PREFIX)


def plugin_version_from_component_version(component_version: Any) -> Optional[int]:
    """
    The backing Plugin_Record version of a Plugin_Component version.

    Plugin_Component versions are '{pluginVersion}.0.0' (the inverse of
    plugin_components.component_version_for), so the Plugin_Record version is
    the major part. Returns None when the version doesn't parse.
    """
    try:
        return int(str(component_version).split('.')[0])
    except (ValueError, AttributeError):
        return None


def target_architectures_from_platforms(platforms) -> List[str]:
    """
    Derive the DDA Target_Architectures a Plugin_Component supports from its
    recipe's platform manifests (16.2). This is the inverse of
    plugin_components.platform_for:

      - architecture aarch64  -> the JetPack arch named by the 'variant'
                                 attribute (arm64_jp4 / arm64_jp5 / arm64_jp6)
      - architecture amd64 + 'runtime: nvidia' -> x86_64_nvidia
      - architecture amd64 (no runtime)        -> x86_64

    Accepts either recipe Manifest 'Platform' blocks (flat dicts) or the
    describe_component API shape ({'name': ..., 'attributes': {...}}).
    Pure over its input so it is fixture-testable without AWS (task 10.9).
    """
    architectures: List[str] = []
    for platform in platforms or []:
        if not isinstance(platform, dict):
            continue
        attributes = platform.get('attributes', platform)
        if not isinstance(attributes, dict):
            continue
        gg_arch = attributes.get('architecture')
        if gg_arch == 'aarch64':
            derived = attributes.get('variant')
        elif gg_arch == 'amd64':
            derived = 'x86_64_nvidia' if attributes.get('runtime') == 'nvidia' else 'x86_64'
        else:
            derived = None
        if derived and derived not in architectures:
            architectures.append(derived)
    return architectures


def get_plugin_record_lifecycle_state(plugin_id: Optional[str],
                                      plugin_version: Optional[Any]) -> Optional[str]:
    """
    The backing Plugin_Record's Lifecycle_State for one Plugin_Component
    version (16.2). Returns None when the record (or the PLUGIN_RECORDS_TABLE
    configuration) is unavailable — the listing still shows the component, it
    just cannot attribute a lifecycle state.
    """
    table_name = os.environ.get('PLUGIN_RECORDS_TABLE')
    if not table_name or not plugin_id or plugin_version is None:
        return None
    try:
        table = boto3.resource('dynamodb').Table(table_name)
        response = table.get_item(
            Key={'plugin_id': plugin_id, 'version': int(plugin_version)})
        item = response.get('Item')
        return item.get('lifecycle_state') if item else None
    except (ClientError, ValueError, TypeError) as e:
        print(f"Warning: could not read Plugin_Record {plugin_id} "
              f"v{plugin_version}: {e}")
        return None


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
        region = get_usecase_region(use_case)
        
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
            components.sort(key=_timestamp_sort_key, reverse=reverse)
        
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
        
        pagination_token = ''  # nosec B105 — empty pagination cursor, not a secret.
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
        
        # Collect the set of portal-managed components. A component may be tagged at
        # the component level (arn:...:components:name) and/or at specific version
        # levels (arn:...:components:name:versions:x.y.z). We only use the tags to
        # discover WHICH components are portal-managed; the latest version is always
        # resolved from Greengrass below so a stale version-level tag cannot pin the
        # displayed version to an old release.
        component_base = {}  # component_name -> {'arn': base_arn, 'tags': {...}}

        for resource in tagged_resources:
            component_arn = resource['ResourceARN']
            tags = {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}

            # ARN format: arn:aws:greengrass:region:account:components:name[:versions:version]
            # Index:      0   1   2          3      4       5          6    [7]      [8]
            arn_parts = component_arn.split(':')
            if len(arn_parts) < 7:
                print(f"[DEBUG] ERROR: Unexpected ARN format with {len(arn_parts)} parts: {component_arn}")
                continue

            component_name = arn_parts[6]
            base_arn = ':'.join(arn_parts[:7])  # strip any ':versions:<v>' suffix

            entry = component_base.setdefault(component_name, {'arn': base_arn, 'tags': {}})
            entry['arn'] = base_arn
            entry['tags'].update(tags)  # merge tags across component-/version-level entries

        # Build component list. Create Greengrass client to resolve the latest
        # version and fetch component details.
        greengrass = create_boto3_client('greengrassv2', credentials, region)
        
        components = []
        for component_name, comp_data in component_base.items():
            # Fetch component details to get platforms information
            platforms = []
            description = ''
            creation_timestamp = None
            base_arn = comp_data['arn']

            # Always resolve the true latest version from Greengrass (semver-aware),
            # ignoring any version pinned in a discovery tag.
            final_version = _resolve_latest_component_version(greengrass, base_arn)
            print(f"[DEBUG] Resolved latest version for {component_name}: {final_version}")

            try:
                if final_version:
                    arn_with_version = f"{base_arn}:versions:{final_version}"
                    print(f"[DEBUG] Describing component with full ARN: {arn_with_version}")
                    component_details = greengrass.describe_component(arn=arn_with_version)
                else:
                    print(f"[DEBUG] Describing component with ARN: {base_arn}")
                    component_details = greengrass.describe_component(arn=base_arn)
                
                platforms = component_details.get('platforms', [])
                description = component_details.get('description', '')
                creation_timestamp = component_details.get('creationTimestamp')
                
                print(f"[DEBUG] Successfully described {component_name}: version={final_version}, platforms={len(platforms)}")
                
            except ClientError as e:
                print(f"Warning: Could not fetch details for {component_name} with ARN {base_arn}: {e}")
                # Continue with empty platforms if describe fails
            
            enriched_component = {
                'arn': base_arn,
                'component_name': component_name,
                'latest_version': {
                    'componentVersion': final_version or '0.0.0',
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

            # Plugin_Component listing (custom-node-designer, 16.2): join
            # dda.plugin.* components with their backing Plugin_Record via the
            # registry tags and expose the record's Lifecycle_State plus the
            # supported Target_Architectures derived from the recipe's
            # platform manifests for the deployment screen.
            if is_plugin_component(component_name):
                plugin_id = comp_data['tags'].get(TAG_PLUGIN_ID)
                # The listed version is the resolved latest component version;
                # its major part IS the backing Plugin_Record version. The
                # version tag is only a fallback (tags are merged across
                # version-level entries, so it may name an older version).
                plugin_version = plugin_version_from_component_version(final_version)
                if plugin_version is None:
                    plugin_version = comp_data['tags'].get(TAG_PLUGIN_VERSION)
                enriched_component.update({
                    'is_plugin_component': True,
                    'plugin_id': plugin_id,
                    'plugin_version': plugin_version,
                    'lifecycle_state': get_plugin_record_lifecycle_state(
                        plugin_id, plugin_version),
                    'supported_architectures':
                        target_architectures_from_platforms(platforms),
                })

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
        region = get_usecase_region(use_case)
        
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
        region = get_usecase_region(use_case)
        
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