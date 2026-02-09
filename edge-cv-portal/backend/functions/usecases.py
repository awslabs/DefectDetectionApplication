"""
Use Cases handler for Edge CV Portal
"""
import json
import logging
import os
from datetime import datetime
import uuid
import boto3
import requests
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, validate_required_fields,
    rbac_manager, Role, Permission, require_permission, require_super_user,
    validate_usecase_access
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')
USECASES_TABLE = os.environ.get('USECASES_TABLE')


def provision_shared_components_via_api(
    usecase_id: str,
    usecase_account_id: str,
    cross_account_role_arn: str,
    external_id: str,
    user_id: str,
    auth_token: str = None
) -> dict:
    """
    Call the SharedComponentsHandler Lambda via API Gateway to provision shared components.
    
    This avoids the need to import the shared_components module, which may not be available
    in the Lambda layer. Instead, we call the provisioning API endpoint which routes to
    the SharedComponentsHandler Lambda function.
    
    For internal Lambda-to-Lambda calls, we use AWS SigV4 signing with the Lambda's IAM role
    instead of JWT tokens. This allows the call to pass through the Cognito authorizer.
    
    Args:
        usecase_id: UUID of the usecase
        usecase_account_id: AWS Account ID of the usecase
        cross_account_role_arn: IAM role ARN for cross-account access
        external_id: External ID for role assumption
        user_id: User ID making the request
        auth_token: Optional auth token for API call (not used for internal calls)
    
    Returns:
        dict with provisioning results
    """
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        
        # Get the Portal API URL from environment
        portal_api_url = os.environ.get('PORTAL_API_URL')
        logger.info(f"provision_shared_components_via_api called for usecase {usecase_id}")
        logger.info(f"PORTAL_API_URL from environment: {portal_api_url}")
        
        if not portal_api_url:
            logger.warning("PORTAL_API_URL not configured, skipping shared components provisioning")
            return {'status': 'skipped', 'reason': 'PORTAL_API_URL not configured'}
        
        # Prepare the provisioning request
        provision_url = f"{portal_api_url}/shared-components/provision"
        
        payload = {
            'usecase_id': usecase_id,
            'user_id': user_id,  # Pass user context for authorization in SharedComponentsHandler
            'account_id': usecase_account_id
        }
        
        logger.info(f"Calling provisioning API: {provision_url} for usecase {usecase_id}")
        logger.info(f"Provisioning payload: {payload}")
        
        # Prepare request body
        body = json.dumps(payload)
        
        # Create an AWS request for SigV4 signing
        # This allows the Lambda's IAM role to authenticate with API Gateway
        request = AWSRequest(
            method='POST',
            url=provision_url,
            data=body,
            headers={'Content-Type': 'application/json'}
        )
        
        # Get credentials and sign the request
        credentials = boto3.Session().get_credentials()
        region = os.environ.get('AWS_REGION', 'us-east-1')
        logger.info(f"Signing request with region: {region}")
        
        SigV4Auth(credentials, 'execute-api', region).add_auth(request)
        
        # Log the signed headers for debugging
        logger.info(f"Request headers after signing: Authorization header present: {'Authorization' in request.headers}")
        
        # Make the signed request
        response = requests.post(
            provision_url,
            headers=dict(request.headers),
            data=request.body,
            timeout=120  # 2 minutes timeout for provisioning
        )
        
        logger.info(f"Provisioning API response status: {response.status_code}")
        
        # Check response status
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Provisioning API returned success: {result}")
            return result
        elif response.status_code == 202:
            # Accepted - provisioning is async
            result = response.json()
            logger.info(f"Provisioning API accepted request (async): {result}")
            return result
        else:
            error_msg = f"Provisioning API returned status {response.status_code}"
            try:
                error_detail = response.json()
                logger.error(f"{error_msg}: {error_detail}")
                return {'status': 'failed', 'error': error_detail}
            except:
                logger.error(f"{error_msg}: {response.text}")
                return {'status': 'failed', 'error': error_msg}
    
    except requests.exceptions.Timeout:
        error_msg = "Provisioning API call timed out"
        logger.error(error_msg)
        return {'status': 'failed', 'error': error_msg}
    
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Failed to connect to provisioning API: {str(e)}"
        logger.error(error_msg)
        return {'status': 'failed', 'error': error_msg}
    
    except Exception as e:
        error_msg = f"Unexpected error calling provisioning API: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {'status': 'failed', 'error': error_msg}


def assume_role(role_arn: str, external_id: str, session_name: str) -> dict:
    """Assume a cross-account role and return credentials
    
    Args:
        role_arn: ARN of the role to assume
        external_id: External ID for role assumption (can be empty/None if role doesn't require it)
        session_name: Name for the assumed role session
    """
    try:
        # Build assume role parameters
        assume_params = {
            'RoleArn': role_arn,
            'RoleSessionName': session_name,
            'DurationSeconds': 900  # 15 minutes is enough for bucket policy update
        }
        
        # Only include ExternalId if provided (some roles don't require it)
        if external_id:
            assume_params['ExternalId'] = external_id
        
        response = sts.assume_role(**assume_params)
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Failed to assume role {role_arn}: {str(e)}")
        raise


def update_data_bucket_policy_for_sagemaker(
    data_account_role_arn: str,
    data_account_external_id: str,
    data_bucket_name: str,
    usecase_account_id: str,
    sagemaker_role_arn: str
) -> dict:
    """
    Update the Data Account bucket policy to allow UseCase Account's SageMaker role to read.
    
    This is called during usecase onboarding when a separate Data Account is configured.
    It adds a policy statement allowing the SageMaker execution role to read from the bucket.
    
    Args:
        data_account_role_arn: Role ARN in Data Account that Portal can assume
        data_account_external_id: External ID for assuming the Data Account role
        data_bucket_name: S3 bucket name in Data Account
        usecase_account_id: UseCase Account ID
        sagemaker_role_arn: SageMaker execution role ARN in UseCase Account
    
    Returns:
        dict with status and details
    """
    try:
        logger.info(f"Updating bucket policy for {data_bucket_name} to allow {sagemaker_role_arn}")
        
        # Assume Data Account role
        credentials = assume_role(
            data_account_role_arn,
            data_account_external_id,
            'update-bucket-policy'
        )
        
        # Create S3 client with Data Account credentials
        s3_data = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Get existing bucket policy (if any)
        existing_policy = None
        try:
            response = s3_data.get_bucket_policy(Bucket=data_bucket_name)
            existing_policy = json.loads(response['Policy'])
            logger.info(f"Found existing bucket policy with {len(existing_policy.get('Statement', []))} statements")
            
            # Validate and sanitize existing statements
            # Remove any statements with invalid principals that would cause MalformedPolicy error
            valid_statements = []
            for stmt in existing_policy.get('Statement', []):
                try:
                    # Check if Principal is valid
                    principal = stmt.get('Principal')
                    if principal:
                        # Principal can be a string "*" or a dict with "AWS", "Service", etc.
                        if isinstance(principal, str):
                            # Valid: "*"
                            valid_statements.append(stmt)
                        elif isinstance(principal, dict):
                            # Check if it has valid keys
                            valid_keys = {'AWS', 'Service', 'Federated', 'CanonicalUser'}
                            if any(key in principal for key in valid_keys):
                                # Validate AWS principals are ARNs or account IDs
                                aws_principals = principal.get('AWS', [])
                                if isinstance(aws_principals, str):
                                    aws_principals = [aws_principals]
                                
                                # Filter out invalid principals
                                valid_aws = [p for p in aws_principals if p and (p.startswith('arn:') or p.isdigit())]
                                if valid_aws:
                                    principal['AWS'] = valid_aws
                                    valid_statements.append(stmt)
                                else:
                                    logger.warning(f"Skipping statement with invalid AWS principals: {aws_principals}")
                            else:
                                logger.warning(f"Skipping statement with invalid principal keys: {principal.keys()}")
                        else:
                            logger.warning(f"Skipping statement with invalid principal type: {type(principal)}")
                    else:
                        # No principal, skip
                        logger.warning(f"Skipping statement without principal: {stmt.get('Sid')}")
                except Exception as e:
                    logger.warning(f"Error validating statement {stmt.get('Sid')}: {str(e)}")
            
            existing_policy['Statement'] = valid_statements
            logger.info(f"After validation: {len(valid_statements)} valid statements")
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucketPolicy':
                logger.info(f"No existing bucket policy for {data_bucket_name}, creating new one")
                existing_policy = {
                    'Version': '2012-10-17',
                    'Statement': []
                }
            else:
                raise
        
        # Define the SageMaker access statements
        sagemaker_read_sid = f"AllowSageMakerRead-{usecase_account_id}"
        sagemaker_list_sid = f"AllowSageMakerList-{usecase_account_id}"
        
        # Check if statements already exist for this UseCase Account
        existing_sids = {stmt.get('Sid') for stmt in existing_policy.get('Statement', [])}
        
        statements_to_add = []
        
        if sagemaker_read_sid not in existing_sids:
            statements_to_add.append({
                'Sid': sagemaker_read_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': sagemaker_role_arn
                },
                'Action': [
                    's3:GetObject',
                    's3:GetObjectVersion',
                    's3:GetObjectTagging'
                ],
                'Resource': f'arn:aws:s3:::{data_bucket_name}/*'
            })
        
        if sagemaker_list_sid not in existing_sids:
            statements_to_add.append({
                'Sid': sagemaker_list_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': sagemaker_role_arn
                },
                'Action': [
                    's3:ListBucket',
                    's3:GetBucketLocation'
                ],
                'Resource': f'arn:aws:s3:::{data_bucket_name}'
            })
        
        if not statements_to_add:
            logger.info(f"Bucket policy already has SageMaker access for UseCase {usecase_account_id}")
            return {
                'status': 'already_configured',
                'bucket': data_bucket_name,
                'sagemaker_role': sagemaker_role_arn
            }
        
        # Add new statements to policy
        existing_policy['Statement'].extend(statements_to_add)
        
        # Put updated bucket policy
        s3_data.put_bucket_policy(
            Bucket=data_bucket_name,
            Policy=json.dumps(existing_policy)
        )
        
        logger.info(f"Successfully updated bucket policy for {data_bucket_name}")
        
        return {
            'status': 'success',
            'bucket': data_bucket_name,
            'sagemaker_role': sagemaker_role_arn,
            'statements_added': len(statements_to_add)
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"Failed to update bucket policy: {error_code} - {error_message}")
        
        if error_code == 'AccessDenied':
            return {
                'status': 'failed',
                'error': 'Access denied. The Data Account role needs s3:GetBucketPolicy and s3:PutBucketPolicy permissions.',
                'bucket': data_bucket_name
            }
        elif error_code == 'NoSuchBucket':
            return {
                'status': 'failed',
                'error': f"Bucket '{data_bucket_name}' does not exist in the Data Account.",
                'bucket': data_bucket_name
            }
        else:
            return {
                'status': 'failed',
                'error': f"{error_code}: {error_message}",
                'bucket': data_bucket_name
            }
    except Exception as e:
        logger.error(f"Unexpected error updating bucket policy: {str(e)}")
        return {
            'status': 'failed',
            'error': str(e),
            'bucket': data_bucket_name
        }


def configure_bucket_cors(
    data_account_role_arn: str,
    data_account_external_id: str,
    data_bucket_name: str,
    cloudfront_domain: str
) -> dict:
    """
    Configure CORS on the Data Account bucket to allow browser uploads from the portal.
    
    This is called during usecase onboarding when a separate Data Account is configured.
    It adds CORS rules allowing the CloudFront domain to upload files.
    
    Args:
        data_account_role_arn: Role ARN in Data Account that Portal can assume
        data_account_external_id: External ID for assuming the Data Account role
        data_bucket_name: S3 bucket name in Data Account
        cloudfront_domain: CloudFront domain of the portal frontend
    
    Returns:
        dict with status and details
    """
    try:
        logger.info(f"Configuring CORS for {data_bucket_name} to allow {cloudfront_domain}")
        
        # Assume Data Account role
        credentials = assume_role(
            data_account_role_arn,
            data_account_external_id,
            'configure-bucket-cors'
        )
        
        # Create S3 client with Data Account credentials
        s3_data = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Build the allowed origin (ensure https://)
        if not cloudfront_domain.startswith('http'):
            allowed_origin = f'https://{cloudfront_domain}'
        else:
            allowed_origin = cloudfront_domain
        
        # Check existing CORS configuration
        existing_cors = None
        try:
            response = s3_data.get_bucket_cors(Bucket=data_bucket_name)
            existing_cors = response.get('CORSRules', [])
            logger.info(f"Found existing CORS with {len(existing_cors)} rules")
            
            # Check if our origin is already configured
            for rule in existing_cors:
                origins = rule.get('AllowedOrigins', [])
                if allowed_origin in origins or '*' in origins:
                    logger.info(f"CORS already configured for {allowed_origin}")
                    return {
                        'status': 'already_configured',
                        'bucket': data_bucket_name,
                        'origin': allowed_origin
                    }
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchCORSConfiguration':
                logger.info(f"No existing CORS for {data_bucket_name}, creating new one")
                existing_cors = []
            else:
                raise
        
        # Add our CORS rules
        # Rule 1: Portal uploads from CloudFront
        portal_rule = {
            'AllowedHeaders': ['*'],
            'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
            'AllowedOrigins': [allowed_origin],
            'ExposeHeaders': ['ETag'],
            'MaxAgeSeconds': 3000
        }
        
        # Rule 2: Ground Truth labeling UI (requires * origin for image display)
        ground_truth_rule = {
            'AllowedHeaders': ['*'],
            'AllowedMethods': ['GET', 'HEAD'],
            'AllowedOrigins': ['*'],
            'ExposeHeaders': ['ETag', 'x-amz-meta-custom-header'],
            'MaxAgeSeconds': 3000
        }
        
        # Check if Ground Truth rule already exists
        has_ground_truth_rule = False
        for rule in existing_cors:
            origins = rule.get('AllowedOrigins', [])
            methods = rule.get('AllowedMethods', [])
            if '*' in origins and 'GET' in methods:
                has_ground_truth_rule = True
                break
        
        # Append rules as needed
        existing_cors.append(portal_rule)
        if not has_ground_truth_rule:
            existing_cors.append(ground_truth_rule)
        
        # Put CORS configuration
        s3_data.put_bucket_cors(
            Bucket=data_bucket_name,
            CORSConfiguration={'CORSRules': existing_cors}
        )
        
        logger.info(f"Successfully configured CORS for {data_bucket_name}")
        
        return {
            'status': 'success',
            'bucket': data_bucket_name,
            'origin': allowed_origin
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"Failed to configure CORS: {error_code} - {error_message}")
        
        if error_code == 'AccessDenied':
            return {
                'status': 'failed',
                'error': 'Access denied. The Data Account role needs s3:GetBucketCors and s3:PutBucketCors permissions.',
                'bucket': data_bucket_name
            }
        else:
            return {
                'status': 'failed',
                'error': f"{error_code}: {error_message}",
                'bucket': data_bucket_name
            }
    except Exception as e:
        logger.error(f"Unexpected error configuring CORS: {str(e)}")
        return {
            'status': 'failed',
            'error': str(e),
            'bucket': data_bucket_name
        }


def tag_bucket_for_portal(
    role_arn: str,
    external_id: str,
    bucket_name: str,
    usecase_id: str
) -> dict:
    """
    Tag a bucket with dda-portal:managed=true so it appears in the portal.
    
    This is called during usecase onboarding to automatically tag the configured bucket.
    
    Args:
        role_arn: Role ARN to assume for bucket access
        external_id: External ID for assuming the role
        bucket_name: S3 bucket name to tag
        usecase_id: UseCase ID to associate with the bucket
    
    Returns:
        dict with status and details
    """
    try:
        logger.info(f"Tagging bucket {bucket_name} for portal access")
        
        # Assume role
        credentials = assume_role(role_arn, external_id, 'tag-bucket')
        
        # Create S3 client with credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Get existing tags
        existing_tags = []
        try:
            response = s3.get_bucket_tagging(Bucket=bucket_name)
            existing_tags = response.get('TagSet', [])
            
            # Check if already tagged
            for tag in existing_tags:
                if tag.get('Key') == 'dda-portal:managed' and tag.get('Value') == 'true':
                    logger.info(f"Bucket {bucket_name} already tagged for portal")
                    return {
                        'status': 'already_configured',
                        'bucket': bucket_name
                    }
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchTagSet':
                existing_tags = []
            else:
                raise
        
        # Add portal tag
        new_tags = [t for t in existing_tags if t.get('Key') not in ['dda-portal:managed', 'dda-portal:usecase-id']]
        new_tags.append({'Key': 'dda-portal:managed', 'Value': 'true'})
        new_tags.append({'Key': 'dda-portal:usecase-id', 'Value': usecase_id})
        
        # Put tags
        s3.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={'TagSet': new_tags}
        )
        
        logger.info(f"Successfully tagged bucket {bucket_name} for portal")
        
        return {
            'status': 'success',
            'bucket': bucket_name
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"Failed to tag bucket: {error_code} - {error_message}")
        
        return {
            'status': 'failed',
            'error': f"{error_code}: {error_message}",
            'bucket': bucket_name
        }
    except Exception as e:
        logger.error(f"Unexpected error tagging bucket: {str(e)}")
        return {
            'status': 'failed',
            'error': str(e),
            'bucket': bucket_name
        }


def configure_eventbridge_permission(usecase_account_id: str) -> dict:
    """
    Configure EventBridge permission to allow UseCase Account to send events to Portal Account.
    
    This is called during usecase onboarding to enable cross-account EventBridge forwarding.
    The UseCase Account's EventBridge rules (created by UseCaseAccountStack) will forward
    SageMaker training/compilation job state changes to the Portal Account.
    
    Args:
        usecase_account_id: AWS Account ID of the UseCase Account
    
    Returns:
        dict with status and details
    """
    try:
        logger.info(f"Configuring EventBridge permission for UseCase Account {usecase_account_id}")
        
        events_client = boto3.client('events')
        statement_id = f"AllowUseCaseAccount-{usecase_account_id}"
        
        # Check if permission already exists
        try:
            # Try to describe the event bus policy
            response = events_client.describe_event_bus(Name='default')
            policy = response.get('Policy')
            
            if policy:
                policy_doc = json.loads(policy)
                for statement in policy_doc.get('Statement', []):
                    if statement.get('Sid') == statement_id:
                        logger.info(f"EventBridge permission already exists for {usecase_account_id}")
                        return {
                            'status': 'already_configured',
                            'usecase_account_id': usecase_account_id,
                            'statement_id': statement_id
                        }
        except ClientError as e:
            # Policy might not exist yet, which is fine
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                logger.warning(f"Error checking EventBridge policy: {e}")
        
        # Add permission for UseCase Account to send events
        events_client.put_permission(
            EventBusName='default',
            Action='events:PutEvents',
            Principal=usecase_account_id,
            StatementId=statement_id
        )
        
        logger.info(f"Successfully configured EventBridge permission for {usecase_account_id}")
        
        return {
            'status': 'success',
            'usecase_account_id': usecase_account_id,
            'statement_id': statement_id,
            'message': 'EventBridge permission granted. UseCase Account can now forward SageMaker events.'
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"Failed to configure EventBridge permission: {error_code} - {error_message}")
        
        if 'already exists' in error_message.lower():
            return {
                'status': 'already_configured',
                'usecase_account_id': usecase_account_id
            }
        
        return {
            'status': 'failed',
            'error': f"{error_code}: {error_message}",
            'usecase_account_id': usecase_account_id
        }
    except Exception as e:
        logger.error(f"Unexpected error configuring EventBridge: {str(e)}")
        return {
            'status': 'failed',
            'error': str(e),
            'usecase_account_id': usecase_account_id
        }


def handler(event, context):
    """
    Handle use case management requests
    
    GET    /api/v1/usecases       - List use cases
    POST   /api/v1/usecases       - Create use case
    GET    /api/v1/usecases/{id}  - Get use case details
    PUT    /api/v1/usecases/{id}  - Update use case
    DELETE /api/v1/usecases/{id}  - Delete use case
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        
        logger.info(f"UseCases request: {http_method} {path}")
        
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
        
        if http_method == 'GET' and not path_parameters.get('id'):
            return list_usecases(user)
        elif http_method == 'POST':
            return create_usecase(event, user)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_usecase(path_parameters['id'], user)
        elif http_method == 'PUT' and path_parameters.get('id'):
            return update_usecase(path_parameters['id'], event, user)
        elif http_method == 'DELETE' and path_parameters.get('id'):
            return delete_usecase(path_parameters['id'], user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in usecases handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_usecases(user):
    """List all use cases accessible to the user"""
    try:
        # Validate user object
        if not user:
            logger.error("User object is None")
            return create_response(401, {'error': 'Unauthorized - no user'})
        
        if 'user_id' not in user:
            logger.error(f"User object missing user_id: {user}")
            return create_response(401, {'error': 'Unauthorized - invalid user'})
        
        table = dynamodb.Table(USECASES_TABLE)
        user_id = user['user_id']
        
        logger.info(f"Listing usecases for user: {user_id}")
        
        # Get accessible use cases using RBAC manager (role from IDP)
        accessible_usecases = rbac_manager.get_accessible_usecases(user_id, user)
        
        if rbac_manager.is_portal_admin(user_id, user):
            # Super user gets all use cases
            logger.info(f"User {user_id} is portal admin, scanning all usecases")
            response = table.scan()
            usecases = response.get('Items', [])
            logger.info(f"Found {len(usecases)} usecases in table")
            
            log_audit_event(
                user_id, 'list_usecases', 'usecase', 'all',
                'success', {'is_super_user': True, 'count': len(usecases)}
            )
        else:
            # Regular users get only their assigned use cases
            logger.info(f"User {user_id} is regular user, getting assigned usecases")
            usecases = []
            for usecase_id in accessible_usecases:
                try:
                    response = table.get_item(Key={'usecase_id': usecase_id})
                    if 'Item' in response:
                        usecase = response['Item']
                        # Add user's role for this use case
                        usecase['user_role'] = user.get('role', 'Viewer')
                        usecases.append(usecase)
                except Exception as e:
                    logger.warning(f"Error getting use case {usecase_id}: {str(e)}")
            
            logger.info(f"Found {len(usecases)} assigned usecases for user {user_id}")
            
            log_audit_event(
                user_id, 'list_usecases', 'usecase', 'assigned',
                'success', {'count': len(usecases)}
            )
        
        return create_response(200, {
            'usecases': usecases,
            'count': len(usecases),
            'is_super_user': rbac_manager.is_portal_admin(user_id, user)
        })
        
    except Exception as e:
        logger.error(f"Error listing use cases: {str(e)}", exc_info=True)
        log_audit_event(
            user['user_id'] if user else 'unknown', 'list_usecases', 'usecase', 'all', 'failure'
        )
        return create_response(500, {'error': f'Failed to list use cases: {str(e)}'})


def create_usecase(event, user):
    """Create a new use case - any authenticated user can create"""
    try:
        # Any authenticated user can create a usecase
        # They will be automatically assigned as UseCaseAdmin
        
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['name', 'account_id', 's3_bucket', 'cross_account_role_arn', 'sagemaker_execution_role_arn']
        validation_error = validate_required_fields(body, required_fields)
        if validation_error:
            return create_response(400, {'error': validation_error})
        
        table = dynamodb.Table(USECASES_TABLE)
        usecase_id = str(uuid.uuid4())
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            'usecase_id': usecase_id,
            'name': body['name'],
            'account_id': body['account_id'],
            's3_bucket': body['s3_bucket'],
            's3_prefix': body.get('s3_prefix', ''),
            'cross_account_role_arn': body['cross_account_role_arn'],
            'sagemaker_execution_role_arn': body['sagemaker_execution_role_arn'],
            'external_id': body.get('external_id', str(uuid.uuid4())),
            'owner': body.get('owner', user['email']),
            'cost_center': body.get('cost_center', ''),
            'default_device_group': body.get('default_device_group', ''),
            'created_at': timestamp,
            'updated_at': timestamp,
            'tags': body.get('tags', {}),
            'shared_components_provisioned': False
        }
        
        # Validate Data Account configuration - external_id is required for security
        if body.get('data_account_id') and body.get('data_account_id') != body['account_id']:
            # Separate Data Account is being configured - require all fields
            if not body.get('data_account_role_arn'):
                return create_response(400, {
                    'error': 'data_account_role_arn is required when configuring a separate Data Account'
                })
            if not body.get('data_account_external_id'):
                return create_response(400, {
                    'error': 'data_account_external_id is required for security when configuring a separate Data Account. '
                             'This should be the external ID configured in the Data Account role trust policy.'
                })
            if not body.get('data_s3_bucket'):
                return create_response(400, {
                    'error': 'data_s3_bucket is required when configuring a separate Data Account'
                })
        
        # Add optional Data Account fields if provided
        if body.get('data_account_id'):
            item['data_account_id'] = body['data_account_id']
        if body.get('data_account_role_arn'):
            item['data_account_role_arn'] = body['data_account_role_arn']
        if body.get('data_account_external_id'):
            item['data_account_external_id'] = body['data_account_external_id']
        if body.get('data_s3_bucket'):
            item['data_s3_bucket'] = body['data_s3_bucket']
        if body.get('data_s3_prefix'):
            item['data_s3_prefix'] = body['data_s3_prefix']
        
        table.put_item(Item=item)
        
        # Auto-assign creator as UseCaseAdmin
        try:
            user_roles_table = dynamodb.Table(os.environ.get('USER_ROLES_TABLE'))
            user_roles_table.put_item(Item={
                'user_id': user['user_id'],
                'usecase_id': usecase_id,
                'role': 'UseCaseAdmin',
                'assigned_at': timestamp,
                'assigned_by': 'system',
                'reason': 'auto-assigned as creator'
            })
            logger.info(f"Auto-assigned user {user['user_id']} as UseCaseAdmin for usecase {usecase_id}")
            item['creator_role_assigned'] = True
        except Exception as e:
            logger.warning(f"Failed to auto-assign creator role: {str(e)}")
            item['creator_role_assigned'] = False
        
        # If separate Data Account is configured, update bucket policy to allow SageMaker access
        data_bucket_policy_result = None
        if (body.get('data_account_id') and 
            body.get('data_account_role_arn') and 
            body.get('data_s3_bucket') and
            body.get('data_account_id') != body['account_id']):
            
            logger.info(f"Separate Data Account detected, updating bucket policy for SageMaker access")
            
            try:
                data_bucket_policy_result = update_data_bucket_policy_for_sagemaker(
                    data_account_role_arn=body['data_account_role_arn'],
                    data_account_external_id=body['data_account_external_id'],  # Required for production
                    data_bucket_name=body['data_s3_bucket'],
                    usecase_account_id=body['account_id'],
                    sagemaker_role_arn=body['sagemaker_execution_role_arn']
                )
                
                # Update usecase with bucket policy status
                table.update_item(
                    Key={'usecase_id': usecase_id},
                    UpdateExpression='SET data_bucket_policy_configured = :configured, data_bucket_policy_result = :result',
                    ExpressionAttributeValues={
                        ':configured': data_bucket_policy_result.get('status') == 'success' or data_bucket_policy_result.get('status') == 'already_configured',
                        ':result': data_bucket_policy_result
                    }
                )
                item['data_bucket_policy_configured'] = data_bucket_policy_result.get('status') in ['success', 'already_configured']
                item['data_bucket_policy_result'] = data_bucket_policy_result
                
                logger.info(f"Data bucket policy update result: {data_bucket_policy_result.get('status')}")
                
            except Exception as e:
                logger.warning(f"Failed to update data bucket policy for usecase {usecase_id}: {str(e)}")
                data_bucket_policy_result = {'status': 'failed', 'error': str(e)}
        
        # Configure CORS on Data Account bucket for browser uploads
        data_bucket_cors_result = None
        cloudfront_domain = os.environ.get('CLOUDFRONT_DOMAIN')
        
        if (body.get('data_account_id') and 
            body.get('data_account_role_arn') and 
            body.get('data_s3_bucket') and
            body.get('data_account_id') != body['account_id'] and
            cloudfront_domain):
            
            logger.info(f"Configuring CORS for Data Account bucket")
            
            try:
                data_bucket_cors_result = configure_bucket_cors(
                    data_account_role_arn=body['data_account_role_arn'],
                    data_account_external_id=body['data_account_external_id'],  # Required for production
                    data_bucket_name=body['data_s3_bucket'],
                    cloudfront_domain=cloudfront_domain
                )
                
                # Update usecase with CORS status
                table.update_item(
                    Key={'usecase_id': usecase_id},
                    UpdateExpression='SET data_bucket_cors_configured = :configured, data_bucket_cors_result = :result',
                    ExpressionAttributeValues={
                        ':configured': data_bucket_cors_result.get('status') in ['success', 'already_configured'],
                        ':result': data_bucket_cors_result
                    }
                )
                item['data_bucket_cors_configured'] = data_bucket_cors_result.get('status') in ['success', 'already_configured']
                item['data_bucket_cors_result'] = data_bucket_cors_result
                
                logger.info(f"Data bucket CORS result: {data_bucket_cors_result.get('status')}")
                
            except Exception as e:
                logger.warning(f"Failed to configure CORS for usecase {usecase_id}: {str(e)}")
                data_bucket_cors_result = {'status': 'failed', 'error': str(e)}
        
        # Tag Data Account bucket for portal discovery
        data_bucket_tag_result = None
        if (body.get('data_account_id') and 
            body.get('data_account_role_arn') and 
            body.get('data_s3_bucket') and
            body.get('data_account_id') != body['account_id']):
            
            logger.info(f"Tagging Data Account bucket for portal discovery")
            
            try:
                data_bucket_tag_result = tag_bucket_for_portal(
                    role_arn=body['data_account_role_arn'],
                    external_id=body['data_account_external_id'],  # Required for production
                    bucket_name=body['data_s3_bucket'],
                    usecase_id=usecase_id
                )
                
                # Update usecase with tag status
                table.update_item(
                    Key={'usecase_id': usecase_id},
                    UpdateExpression='SET data_bucket_tagged = :tagged, data_bucket_tag_result = :result',
                    ExpressionAttributeValues={
                        ':tagged': data_bucket_tag_result.get('status') in ['success', 'already_configured'],
                        ':result': data_bucket_tag_result
                    }
                )
                item['data_bucket_tagged'] = data_bucket_tag_result.get('status') in ['success', 'already_configured']
                item['data_bucket_tag_result'] = data_bucket_tag_result
                
                logger.info(f"Data bucket tag result: {data_bucket_tag_result.get('status')}")
                
            except Exception as e:
                logger.warning(f"Failed to tag bucket for usecase {usecase_id}: {str(e)}")
                data_bucket_tag_result = {'status': 'failed', 'error': str(e)}
        
        # Provision shared components (dda-LocalServer) to the usecase account
        # This creates read-only copies of portal-managed components
        # We call the provisioning API endpoint instead of importing the module
        shared_components_result = None
        provision_shared = body.get('provision_shared_components', True)
        
        logger.info(f"Checking if shared components should be provisioned: provision_shared={provision_shared}")
        
        if provision_shared:
            logger.info(f"Starting shared components provisioning for usecase {usecase_id}")
            try:
                # Extract auth token from event headers if available
                # Note: We don't pass auth token for internal API calls - the provisioning endpoint
                # will use the Lambda's IAM role for authorization instead
                
                shared_components_result = provision_shared_components_via_api(
                    usecase_id=usecase_id,
                    usecase_account_id=body['account_id'],
                    cross_account_role_arn=body['cross_account_role_arn'],
                    external_id=item['external_id'],
                    user_id=user['user_id'],
                    auth_token=None  # Don't pass auth token for internal calls
                )
                
                # Check if provisioning was successful
                if shared_components_result.get('status') in ['success', 'completed']:
                    logger.info(f"Provisioning succeeded with status: {shared_components_result.get('status')}")
                    # Update usecase with provisioning status
                    table.update_item(
                        Key={'usecase_id': usecase_id},
                        UpdateExpression='SET shared_components_provisioned = :provisioned, shared_components = :components',
                        ExpressionAttributeValues={
                            ':provisioned': True,
                            ':components': shared_components_result
                        }
                    )
                    item['shared_components_provisioned'] = True
                    item['shared_components'] = shared_components_result
                    
                    logger.info(f"Provisioned shared components for usecase {usecase_id}: {shared_components_result}")
                else:
                    # Provisioning failed or was skipped, but don't fail usecase creation
                    logger.warning(f"Shared components provisioning returned status: {shared_components_result.get('status')}, full result: {shared_components_result}")
                    item['shared_components_provisioned'] = False
                    item['shared_components'] = shared_components_result
                
            except Exception as e:
                logger.warning(f"Failed to provision shared components for usecase {usecase_id}: {str(e)}", exc_info=True)
                # Don't fail usecase creation if shared component provisioning fails
                shared_components_result = {'error': str(e), 'status': 'failed'}
                item['shared_components_provisioned'] = False
                item['shared_components'] = shared_components_result
        
        # Configure cross-account EventBridge permissions
        # This allows the UseCase Account to send SageMaker events to the Portal Account
        eventbridge_result = None
        try:
            eventbridge_result = configure_eventbridge_permission(body['account_id'])
            
            # Update usecase with EventBridge status
            table.update_item(
                Key={'usecase_id': usecase_id},
                UpdateExpression='SET eventbridge_configured = :configured, eventbridge_result = :result',
                ExpressionAttributeValues={
                    ':configured': eventbridge_result.get('status') in ['success', 'already_configured'],
                    ':result': eventbridge_result
                }
            )
            item['eventbridge_configured'] = eventbridge_result.get('status') in ['success', 'already_configured']
            item['eventbridge_result'] = eventbridge_result
            
            logger.info(f"EventBridge permission result: {eventbridge_result.get('status')}")
            
        except Exception as e:
            logger.warning(f"Failed to configure EventBridge for usecase {usecase_id}: {str(e)}")
            eventbridge_result = {'status': 'failed', 'error': str(e)}
        
        log_audit_event(
            user['user_id'], 'create_usecase', 'usecase', usecase_id,
            'success', {
                'name': body['name'],
                'shared_components_provisioned': item.get('shared_components_provisioned', False),
                'data_bucket_policy_configured': item.get('data_bucket_policy_configured', False),
                'data_bucket_cors_configured': item.get('data_bucket_cors_configured', False),
                'data_bucket_tagged': item.get('data_bucket_tagged', False),
                'eventbridge_configured': item.get('eventbridge_configured', False)
            }
        )
        
        logger.info(f"Use case created: {usecase_id}")
        
        return create_response(201, {
            'usecase': item,
            'shared_components': shared_components_result,
            'data_bucket_policy': data_bucket_policy_result,
            'data_bucket_cors': data_bucket_cors_result,
            'data_bucket_tag': data_bucket_tag_result,
            'eventbridge': eventbridge_result,
            'message': 'Use case created successfully'
        })
        
    except Exception as e:
        logger.error(f"Error creating use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'create_usecase', 'usecase', 'new', 'failure'
        )
        return create_response(500, {'error': 'Failed to create use case'})


def get_usecase(usecase_id, user):
    """Get use case details"""
    try:
        # Check access using RBAC (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.VIEW_USECASES, user):
            log_audit_event(
                user['user_id'], 'get_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'access_denied', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Access denied to use case'})
        
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Use case not found'})
        
        log_audit_event(
            user['user_id'], 'get_usecase', 'usecase', usecase_id, 'success'
        )
        
        return create_response(200, {'usecase': response['Item']})
        
    except Exception as e:
        logger.error(f"Error getting use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'get_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to get use case'})


def update_usecase(usecase_id, event, user):
    """Update use case"""
    try:
        # Check permission using RBAC (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.UPDATE_USECASES, user):
            log_audit_event(
                user['user_id'], 'update_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'insufficient_permissions', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Insufficient permissions to update use case'})
        
        body = json.loads(event.get('body', '{}'))
        table = dynamodb.Table(USECASES_TABLE)
        
        # Build update expression
        update_expr = "SET updated_at = :updated_at"
        expr_values = {':updated_at': int(datetime.utcnow().timestamp() * 1000)}
        
        updatable_fields = [
            'name', 's3_bucket', 's3_prefix', 'owner', 'cost_center', 'default_device_group',
            'cross_account_role_arn', 'account_id',
            # Data Account fields
            'data_account_id', 'data_account_role_arn', 'data_account_external_id',
            'data_s3_bucket', 'data_s3_prefix'
        ]
        for field in updatable_fields:
            if field in body:
                update_expr += f", {field} = :{field}"
                expr_values[f":{field}"] = body[field]
        
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values
        )
        
        log_audit_event(
            user['user_id'], 'update_usecase', 'usecase', usecase_id,
            'success', {'updated_fields': list(body.keys())}
        )
        
        return create_response(200, {'message': 'Use case updated successfully'})
        
    except Exception as e:
        logger.error(f"Error updating use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'update_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to update use case'})


def delete_usecase(usecase_id, user):
    """Delete use case"""
    try:
        # Check permission using RBAC - only PortalAdmin can delete use cases (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.DELETE_USECASES, user):
            log_audit_event(
                user['user_id'], 'delete_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'insufficient_permissions', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Insufficient permissions to delete use case'})
        
        table = dynamodb.Table(USECASES_TABLE)
        
        # TODO: Check if use case has active resources before deletion
        
        table.delete_item(Key={'usecase_id': usecase_id})
        
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'success'
        )
        
        return create_response(200, {'message': 'Use case deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to delete use case'})
