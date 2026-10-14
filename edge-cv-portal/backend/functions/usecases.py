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
    validate_usecase_access,
    # Use-case bucket configuration (design §5): the allowed-set helpers, the
    # attach validator and the error / audit shapes every record writer shares.
    allowed_s3_buckets, portal_owned_buckets, check_bucket_attachable,
    audit_bucket_allowed, read_bucket_tags, is_membership_name,
    is_valid_bucket_name, caller_is_portal_admin, is_cross_account_setup,
    BucketConfigError, BucketNotEligible, InvalidBucketName,
    bucket_config_error_response, EligibilityResult,
    PORTAL_MANAGED_TAG, PORTAL_CREATED_BY_TAG, PORTAL_USECASE_ID_TAG,
    PORTAL_CREATED_BY_VALUE, REMEDY_FOR_REASON, REMEDY_ACCOUNT,
    REMEDY_OTHER_USECASE, REMEDY_PORTAL_BUCKET, REMEDY_TOO_MANY,
    _usecases_configuring_bucket,
)
# Use_Case settings for VLM/LLM Anomaly Tuning Sample_Export (spec:
# quality-prompt-tuning, Requirements 2.1, 2.9). Pure module, shared with
# deployments.py (which delivers the configuration and the device grant)
# so the accepted values and the delivered ones can never diverge.
import tuning_settings

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# Use-case bucket configuration (design §5.1, §5.3): the additional-bucket
# list is capped at 100 entries per use case and at 10 names per request
# (the probe latency bound of §1.6 — every add is probed).
MAX_ADDITIONAL_BUCKETS = 100
MAX_BUCKET_NAMES_PER_REQUEST = 10
# Remedy for a portal-created bucket whose auto-add did not happen (§5.5
# step 6) and for a name the account already owns (§5.5 step 3).
REMEDY_ADD_LATER = ("The bucket exists and is tagged; a UseCaseAdmin can allow it "
                    "from the use case's Additional S3 buckets.")
REMEDY_BUCKET_ALREADY_EXISTS = ("Select the existing bucket instead; if it is not tagged "
                                "for the portal an AWS administrator must tag it.")
REMEDY_TAGGING_FAILED = ("The bucket was created but could not be tagged; an AWS "
                         "administrator must tag it before it can be allowed.")


class InvalidField(ValueError):
    """A request field of the wrong shape (not a list, more names than the
    per-request cap): answered 400 `invalid_field`."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message

    def to_body(self) -> dict:
        return {'error': self.message, 'error_code': 'invalid_field', 'field': self.field}


def _is_clear(value) -> bool:
    """A Clear of an optional bucket field (design §5.3 step 4): None, '' or
    whitespace-only. Absent, None and '' compare equal when deciding whether
    a value changed."""
    return value is None or (isinstance(value, str) and not value.strip())


def _bucket_value(value):
    """The comparable form of a configured bucket value: None for a Clear,
    the value itself otherwise (no stripping, no lowercasing — §1.1)."""
    return None if _is_clear(value) else value


def _bucket_name_list(value, field: str, *, usecase_id: str = None, purpose: str) -> list:
    """Validate an optional list of bucket names from the request body
    (design §5.3 step 2, §5.4): absent/None -> []; must be a list of at most
    MAX_BUCKET_NAMES_PER_REQUEST strings matching MEMBERSHIP_NAME_RE, else
    InvalidField / InvalidBucketName naming the first offender. Duplicates
    are dropped, order kept."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidField(field, f"{field} must be a list of bucket names")
    if len(value) > MAX_BUCKET_NAMES_PER_REQUEST:
        raise InvalidField(
            field, f"{field} accepts at most {MAX_BUCKET_NAMES_PER_REQUEST} bucket names per request")
    for name in value:
        if not is_membership_name(name):
            raise InvalidBucketName("Invalid S3 bucket name", bucket=name,
                                    usecase_id=usecase_id, purpose=purpose)
    return list(dict.fromkeys(value))


def _rejection(bucket, error: BucketConfigError) -> dict:
    """One `rejected` entry per failing name (design §5.3 step 7)."""
    return {'bucket': bucket if isinstance(bucket, str) else None,
            'error_code': error.error_code, 'reason': error.reason}


def _refused_response(first_error: BucketConfigError, rejected: list) -> dict:
    body = first_error.to_body()
    body['rejected'] = rejected
    return create_response(400, body)


def _usecase_not_found(**extra) -> dict:
    return create_response(404, {'error': 'Use case not found',
                                 'error_code': 'usecase_not_found', **extra})


def _add_additional_bucket(table, usecase_id: str, name: str) -> str:
    """Append `name` to the use case's additional_s3_buckets list (design
    §5.1). 'added' | 'already_present' | 'usecase_not_found'.

    The `attribute_exists(usecase_id)` conjunct keeps the update from
    materialising a phantom record when the use case was deleted between the
    permission check and the write; `ReturnValuesOnConditionCheckFailure`
    tells "item gone" (no Item in the failure) from "name already present"
    (the old item) without a second read.
    """
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    try:
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression='SET #a = list_append(if_not_exists(#a, :empty), :one), updated_at = :t',
            ConditionExpression='attribute_exists(usecase_id) AND (attribute_not_exists(#a) OR NOT contains(#a, :b))',
            ExpressionAttributeNames={'#a': 'additional_s3_buckets'},
            ExpressionAttributeValues={':empty': [], ':one': [name], ':b': name, ':t': now_ms},
            ReturnValuesOnConditionCheckFailure='ALL_OLD')
        return 'added'
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return 'usecase_not_found' if not e.response.get('Item') else 'already_present'


def _remove_additional_bucket(table, usecase_id: str, name: str) -> str:
    """Remove `name` from the use case's additional_s3_buckets list (design
    §5.1). 'removed' | 'not_present' | 'usecase_not_found' |
    'concurrent_update' (the list changed under us twice)."""
    for _attempt in range(2):
        record = table.get_item(Key={'usecase_id': usecase_id}).get('Item')
        if record is None:
            return 'usecase_not_found'
        current = list(record.get('additional_s3_buckets') or [])
        if name not in current:
            return 'not_present'
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        try:
            table.update_item(
                Key={'usecase_id': usecase_id},
                UpdateExpression='SET #a = :filtered, updated_at = :t',
                ConditionExpression='attribute_exists(usecase_id) AND #a = :expected',
                ExpressionAttributeNames={'#a': 'additional_s3_buckets'},
                ExpressionAttributeValues={':filtered': [b for b in current if b != name],
                                           ':expected': current, ':t': now_ms})
            return 'removed'
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            logger.warning("additional_s3_buckets of use case %s changed during a remove; re-reading",
                           usecase_id)
    return 'concurrent_update'


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
        
        # Get the Portal API URL from environment or construct it
        portal_api_url = os.environ.get('PORTAL_API_URL')
        
        # If not explicitly set, construct from AWS environment variables
        if not portal_api_url:
            # AWS Lambda always has AWS_REGION available
            region = os.environ.get('AWS_REGION', 'us-east-1')
            # Get API Gateway ID from the API_GATEWAY_ID environment variable
            api_id = os.environ.get('API_GATEWAY_ID')
            
            if api_id:
                portal_api_url = f"https://{api_id}.execute-api.{region}.amazonaws.com/v1"
                logger.info(f"Constructed PORTAL_API_URL from API_GATEWAY_ID: {portal_api_url}")
            else:
                logger.warning("Neither PORTAL_API_URL nor API_GATEWAY_ID configured, skipping shared components provisioning")
                return {'status': 'skipped', 'reason': 'API URL not configured'}
        
        logger.info(f"provision_shared_components_via_api called for usecase {usecase_id}")
        logger.info(f"Using PORTAL_API_URL: {portal_api_url}")
        
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
    Confirm that a Data Account bucket carries the portal tag
    (dda-portal:managed=true) so it appears in the portal.
    
    Called during usecase onboarding for the configured data bucket. Read-only:
    the portal writes no tags on buckets it did not create (design §5.4) — the
    attach validator has already required the tag before the use case was
    written, so on the success path the bucket is always `already_configured`;
    a bucket without the tag is reported `failed` with the tagging remedy for
    an AWS administrator.
    
    Args:
        role_arn: Role ARN to assume for bucket access
        external_id: External ID for assuming the role
        bucket_name: S3 bucket name to check
        usecase_id: UseCase ID the bucket is configured on (logged only)
    
    Returns:
        dict with status and details (keys unchanged from the former writer)
    """
    try:
        logger.info(f"Checking the portal tag on bucket {bucket_name} for use case {usecase_id}")
        
        # Assume role
        credentials = assume_role(role_arn, external_id, 'tag-bucket')
        
        # Create S3 client with credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Read the tags; the managed tag must already be there.
        try:
            response = s3.get_bucket_tagging(Bucket=bucket_name)
            for tag in response.get('TagSet', []):
                if tag.get('Key') == PORTAL_MANAGED_TAG and tag.get('Value') == 'true':
                    logger.info(f"Bucket {bucket_name} is tagged for the portal")
                    return {
                        'status': 'already_configured',
                        'bucket': bucket_name
                    }
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchTagSet':
                raise
        
        logger.warning(f"Bucket {bucket_name} does not carry {PORTAL_MANAGED_TAG}=true")
        return {
            'status': 'failed',
            'error': f"Bucket is not tagged {PORTAL_MANAGED_TAG}=true. {REMEDY_FOR_REASON['tag_missing']}",
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


def update_component_bucket_policy_for_greengrass(
    component_bucket_name: str,
    device_account_id: str
) -> dict:
    """
    Update the component bucket policy to allow devices in a usecase account to download components.
    
    This is called during usecase onboarding. The component bucket is in the Portal Account,
    and we need to allow the Greengrass device role from the usecase account to access it.
    
    Args:
        component_bucket_name: S3 bucket name in Portal Account (e.g., dda-component-us-east-1-164152369890)
        device_account_id: AWS Account ID where devices will run (the usecase account)
    
    Returns:
        dict with status and details
    """
    try:
        logger.info(f"Updating component bucket policy for {component_bucket_name} to allow device account {device_account_id}")
        
        # Create S3 client in Portal Account (we're already in Portal Account context)
        s3_component = boto3.client('s3')
        
        # Get existing bucket policy (if any)
        existing_policy = None
        try:
            response = s3_component.get_bucket_policy(Bucket=component_bucket_name)
            existing_policy = json.loads(response['Policy'])
            logger.info(f"Found existing bucket policy with {len(existing_policy.get('Statement', []))} statements")
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucketPolicy':
                logger.info(f"No existing bucket policy for {component_bucket_name}, creating new one")
                existing_policy = {
                    'Version': '2012-10-17',
                    'Statement': []
                }
            else:
                raise
        
        # Define the Greengrass device access statements
        greengrass_read_sid = f"AllowGreengrassDeviceRead-{device_account_id}"
        greengrass_list_sid = f"AllowGreengrassDeviceList-{device_account_id}"
        
        # Check if statements already exist for this device account
        existing_sids = {stmt.get('Sid') for stmt in existing_policy.get('Statement', [])}
        
        statements_to_add = []
        
        if greengrass_read_sid not in existing_sids:
            statements_to_add.append({
                'Sid': greengrass_read_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': f'arn:aws:iam::{device_account_id}:role/GreengrassV2TokenExchangeRole'
                },
                'Action': [
                    's3:GetObject',
                    's3:GetObjectVersion'
                ],
                'Resource': f'arn:aws:s3:::{component_bucket_name}/*'
            })
        
        if greengrass_list_sid not in existing_sids:
            statements_to_add.append({
                'Sid': greengrass_list_sid,
                'Effect': 'Allow',
                'Principal': {
                    'AWS': f'arn:aws:iam::{device_account_id}:role/GreengrassV2TokenExchangeRole'
                },
                'Action': [
                    's3:ListBucket',
                    's3:GetBucketLocation'
                ],
                'Resource': f'arn:aws:s3:::{component_bucket_name}'
            })
        
        if not statements_to_add:
            logger.info(f"Bucket policy already has Greengrass access for device account {device_account_id}")
            return {
                'status': 'already_configured',
                'bucket': component_bucket_name,
                'device_account_id': device_account_id
            }
        
        # Add new statements to policy
        existing_policy['Statement'].extend(statements_to_add)
        
        # Put updated bucket policy
        s3_component.put_bucket_policy(
            Bucket=component_bucket_name,
            Policy=json.dumps(existing_policy)
        )
        
        logger.info(f"Successfully updated component bucket policy for {component_bucket_name}")
        
        return {
            'status': 'success',
            'bucket': component_bucket_name,
            'device_account_id': device_account_id,
            'statements_added': len(statements_to_add)
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"Failed to update component bucket policy: {error_code} - {error_message}")
        
        if error_code == 'AccessDenied':
            return {
                'status': 'failed',
                'error': 'Access denied. Portal account needs s3:GetBucketPolicy and s3:PutBucketPolicy permissions.',
                'bucket': component_bucket_name
            }
        elif error_code == 'NoSuchBucket':
            return {
                'status': 'failed',
                'error': f"Bucket '{component_bucket_name}' does not exist in Portal Account.",
                'bucket': component_bucket_name
            }
        else:
            return {
                'status': 'failed',
                'error': f"{error_code}: {error_message}",
                'bucket': component_bucket_name
            }
    except Exception as e:
        logger.error(f"Unexpected error updating component bucket policy: {str(e)}")
        return {
            'status': 'failed',
            'error': str(e),
            'bucket': component_bucket_name
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


def _portal_tagged_buckets() -> dict:
    """{bucket name: tags} for every bucket in this account and region tagged
    dda-portal:managed=true — one paginated Resource Groups Tagging API query
    (design §5.5, `list_s3_buckets`). GetResources is regional while
    ListBuckets is global, so the marking is advisory."""
    tagging = boto3.client('resourcegroupstaggingapi')
    tagged = {}
    paginator = tagging.get_paginator('get_resources')
    for page in paginator.paginate(TagFilters=[{'Key': PORTAL_MANAGED_TAG, 'Values': ['true']}],
                                   ResourceTypeFilters=['s3:bucket']):
        for resource in page.get('ResourceTagMappingList', []):
            # ARN format: arn:aws:s3:::bucket-name
            bucket_name = resource.get('ResourceARN', '').split(':::')[-1]
            if bucket_name:
                tagged[bucket_name] = {tag['Key']: tag.get('Value') for tag in resource.get('Tags', [])}
    return tagged


def list_s3_buckets(user):
    """List S3 buckets in the current (portal) AWS account.

    Used by the 'Onboard New Use Case' flow so the user can browse and pick a
    bucket instead of typing its name. Runs in the portal account with the
    Lambda execution role (no cross-account assume-role) and returns each
    bucket's name, creation date, and region, plus the advisory marks
    `eligible` (tagged dda-portal:managed=true in this region, from one
    Resource Groups Tagging API query) and `portal_created` (design §5.5).
    The attach probe at use-case creation is the authority, not these marks.
    """
    try:
        s3 = boto3.client('s3')
        response = s3.list_buckets()
        tagged = _portal_tagged_buckets()

        buckets = []
        for b in response.get('Buckets', []):
            name = b.get('Name')
            created = b.get('CreationDate')
            region = None
            try:
                loc = s3.get_bucket_location(Bucket=name)
                # us-east-1 is returned as None/empty by the API
                region = loc.get('LocationConstraint') or 'us-east-1'
            except ClientError as e:
                logger.warning(f"Could not get location for bucket {name}: {str(e)}")
                region = 'unknown'

            buckets.append({
                'name': name,
                'creation_date': created.isoformat() if hasattr(created, 'isoformat') else None,
                'region': region,
                'eligible': name in tagged,
                'portal_created': tagged.get(name, {}).get(PORTAL_CREATED_BY_TAG) == PORTAL_CREATED_BY_VALUE,
            })

        buckets.sort(key=lambda x: (x['name'] or '').lower())

        log_audit_event(
            user.get('user_id', 'unknown'), 'list_s3_buckets', 'usecase', 'current-account',
            'success', {'bucket_count': len(buckets)}
        )

        return create_response(200, {'buckets': buckets, 'count': len(buckets)})
    except ClientError as e:
        logger.error(f"AWS error listing S3 buckets: {str(e)}")
        return create_response(500, {'error': f"Failed to list S3 buckets: {str(e)}"})
    except Exception as e:
        logger.error(f"Error listing S3 buckets: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def _portal_bucket_tags(usecase_id):
    """The tags a portal-created bucket carries (design §5.5 step 3)."""
    tags = [{'Key': PORTAL_MANAGED_TAG, 'Value': 'true'},
            {'Key': PORTAL_CREATED_BY_TAG, 'Value': PORTAL_CREATED_BY_VALUE}]
    if usecase_id:
        tags.append({'Key': PORTAL_USECASE_ID_TAG, 'Value': usecase_id})
    return tags


def _portal_tags_confirmed(read_tags: dict, tags: list) -> bool:
    """True when every tag the portal sent came back from GetBucketTagging."""
    return all(read_tags.get(tag['Key']) == tag['Value'] for tag in tags)


def _read_created_bucket_tags(s3, name: str) -> dict:
    """Read-after-write of the tags sent with CreateBucket (design §5.5 step
    5): the shared bounded retry on NoSuchTagSet / a tag set lacking the
    keys; any other read error counts as "keys missing" so the fallback
    runs."""
    try:
        read_tags, _reason = read_bucket_tags(s3, name, retry=True)
        return read_tags
    except ClientError as e:
        logger.warning(f"Could not read back the tags of bucket {name}: {str(e)}")
        return {}


def _audit_bucket_refused_before_create(user, usecase_id, name, reason, event):
    log_audit_event(
        user['user_id'], 's3_bucket_refused', 'usecase', usecase_id or 'none', 'denied',
        {'bucket': name, 'purpose': 'create-s3-bucket', 'reason': reason}, event=event)


def create_s3_bucket(event, user):
    """Create a portal-managed S3 bucket in the current (portal) account
    (design §5.5, Requirement 9).

    body: {name: str, region?: str, usecase_id?: str}

    Who: with `usecase_id`, a holder of UPDATE_USECASES on that single-account
    use case (the bucket is then auto-added to its additional buckets);
    without, a PortalAdmin (the onboarding wizard, which has no use case yet).
    Before any AWS call the name must be free of the portal's own buckets and
    not configured on another use case. The dda-portal:* tags travel in the
    CreateBucket request (CreateBucketConfiguration.Tags, built for us-east-1
    too), ABAC is enabled, the tags are read back with the bounded retry and,
    only if S3 did not return them, written once more with the S3 Control
    TagResource API; a bucket whose tags cannot be confirmed is never
    attached (500 `bucket_tagging_failed`).
    """
    try:
        body = json.loads(event.get('body', '{}'))
        name = body.get('name')
        name = name.strip() if isinstance(name, str) else name
        region = (body.get('region') or os.environ.get('AWS_REGION', 'us-east-1')).strip()
        usecase_id = body.get('usecase_id') or None

        if not name:
            return create_response(400, {'error': 'Bucket name is required'})
        if usecase_id is not None and not isinstance(usecase_id, str):
            return create_response(400, InvalidField('usecase_id', 'usecase_id must be a string').to_body())
        # 1. Strict S3 naming: this is a new bucket.
        if not is_valid_bucket_name(name):
            return bucket_config_error_response(InvalidBucketName(
                "Invalid S3 bucket name", bucket=name, usecase_id=usecase_id,
                purpose='create-s3-bucket'))

        # 2. Authorization.
        table = dynamodb.Table(USECASES_TABLE)
        record = None
        if usecase_id:
            record = table.get_item(Key={'usecase_id': usecase_id}).get('Item')
            if record is None:
                return _usecase_not_found()
            if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.UPDATE_USECASES, user):
                log_audit_event(
                    user['user_id'], 'create_s3_bucket', 'usecase', usecase_id,
                    'failure', {'reason': 'insufficient_permissions', 'bucket': name})
                return create_response(403, {'error': 'Insufficient permissions to update use case'})
            if is_cross_account_setup(record):
                return bucket_config_error_response(BucketNotEligible(
                    "Buckets created in the portal account cannot be allowed for a multi-account use case",
                    bucket=name, usecase_id=usecase_id, purpose='create-s3-bucket',
                    reason='account_mismatch', remedy=REMEDY_ACCOUNT))
            if len(record.get('additional_s3_buckets') or []) >= MAX_ADDITIONAL_BUCKETS:
                return bucket_config_error_response(BucketNotEligible(
                    "The use case already has the maximum number of additional buckets",
                    bucket=name, usecase_id=usecase_id, purpose='create-s3-bucket',
                    reason='too_many_buckets', remedy=REMEDY_TOO_MANY))
        elif not caller_is_portal_admin(user):
            log_audit_event(
                user['user_id'], 'create_s3_bucket', 'usecase', name,
                'failure', {'reason': 'insufficient_permissions'})
            return create_response(403, {'error': 'Only a PortalAdmin can create a bucket without a use case'})

        # 2b. Pre-create Unclaimed check, before any AWS call: the portal does
        # not re-create its own buckets, and a name another use case already
        # configures is created only by a PortalAdmin (who may share it).
        if name in portal_owned_buckets():
            _audit_bucket_refused_before_create(user, usecase_id, name, 'portal_bucket', event)
            return bucket_config_error_response(BucketNotEligible(
                "S3 bucket is not eligible for this use case", bucket=name, usecase_id=usecase_id,
                purpose='create-s3-bucket', reason='portal_bucket', remedy=REMEDY_PORTAL_BUCKET))
        others = _usecases_configuring_bucket(name)
        others.discard(usecase_id)
        if others and not caller_is_portal_admin(user):
            _audit_bucket_refused_before_create(user, usecase_id, name,
                                                'bucket_configured_on_other_usecase', event)
            return bucket_config_error_response(BucketNotEligible(
                "S3 bucket is not eligible for this use case", bucket=name, usecase_id=usecase_id,
                purpose='create-s3-bucket', reason='bucket_configured_on_other_usecase',
                remedy=REMEDY_OTHER_USECASE))

        # 3. Create with the tags in the request. The configuration dict is
        # built for us-east-1 too; only the LocationConstraint is omitted
        # there (us-east-1 rejects an explicit one).
        tags = _portal_bucket_tags(usecase_id)
        tag_keys = [tag['Key'] for tag in tags]
        s3 = boto3.client('s3', region_name=region)
        create_args = {'Bucket': name, 'CreateBucketConfiguration': {'Tags': tags}}
        if region != 'us-east-1':
            create_args['CreateBucketConfiguration']['LocationConstraint'] = region

        try:
            s3.create_bucket(**create_args)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code == 'BucketAlreadyOwnedByYou':
                # Not idempotent any more: an existing bucket goes through
                # "select existing" so an untagged one is never attached.
                logger.info(f"Bucket {name} already owned by this account")
                return create_response(409, {
                    'error': f"Bucket '{name}' already exists in this account",
                    'error_code': 'bucket_already_exists',
                    'bucket': name,
                    'remedy': REMEDY_BUCKET_ALREADY_EXISTS,
                })
            elif code == 'BucketAlreadyExists':
                return create_response(409, {
                    'error': f"Bucket name '{name}' is already taken (S3 bucket names are globally unique). Try another name."
                })
            else:
                logger.error(f"Failed to create bucket {name}: {str(e)}")
                return create_response(400, {'error': f"Failed to create bucket: {str(e)}"})

        # 4. Enable ABAC (non-fatal) so bucket-tag conditions apply to it.
        try:
            s3.put_bucket_abac(Bucket=name, AbacStatus={'Status': 'Enabled'})
            abac = 'enabled'
        except ClientError as e:
            logger.warning(f"Could not enable ABAC on bucket {name}: {str(e)}")
            abac = 'failed'

        # 5. Read the tags back; fall back once to the post-ABAC tag write.
        read_tags = _read_created_bucket_tags(s3, name)
        if not _portal_tags_confirmed(read_tags, tags):
            try:
                boto3.client('s3control', region_name=region).tag_resource(
                    AccountId=os.environ['PORTAL_ACCOUNT_ID'],
                    ResourceArn=f'arn:aws:s3:::{name}', Tags=tags)
                read_tags = _read_created_bucket_tags(s3, name)
            except (ClientError, KeyError) as e:
                logger.error(f"Fallback tag write on bucket {name} failed: {str(e)}")
                read_tags = {}
        if not _portal_tags_confirmed(read_tags, tags):
            logger.error(f"Bucket {name} was created but its portal tags could not be confirmed")
            log_audit_event(
                user['user_id'], 'create_s3_bucket', 'usecase', name, 'failure',
                {'region': region, 'reason': 'bucket_tagging_failed', 'abac': abac,
                 'usecase_id': usecase_id}, event=event)
            return create_response(500, {
                'error': 'The bucket was created but could not be tagged',
                'error_code': 'bucket_tagging_failed',
                'bucket': name,
                'remedy': REMEDY_TAGGING_FAILED,
            })
        evidence = EligibilityResult(
            eligible=True, reason='eligible', tags=read_tags, portal_created=True,
            claimed_by=read_tags.get(PORTAL_USECASE_ID_TAG) or None)

        # 6. Auto-add to the use case, with the read tags as the evidence.
        added_to_usecase = False
        add_error = None
        if usecase_id:
            try:
                result = check_bucket_attachable(
                    target_usecase_id=usecase_id, usecase_like=record, bucket=name,
                    user=user, event=event, purpose='auto-add-bucket', evidence=evidence)
                outcome = _add_additional_bucket(table, usecase_id, name)
                if outcome == 'usecase_not_found':
                    add_error = {'error_code': 'usecase_not_found', 'remedy': REMEDY_ADD_LATER}
                else:
                    added_to_usecase = True
                    audit_bucket_allowed(user, usecase_id, name, 'additional_s3_buckets',
                                         'auto_add', result, event)
            except BucketNotEligible as e:
                add_error = {'error_code': 'bucket_not_eligible', 'reason': e.reason, 'remedy': e.remedy}
            except ClientError as e:
                logger.error(f"Could not add bucket {name} to use case {usecase_id}: {str(e)}")
                add_error = {'error_code': 'usecase_update_failed', 'remedy': REMEDY_ADD_LATER}

        # 7. Audit and answer.
        log_audit_event(
            user['user_id'], 'create_s3_bucket', 'usecase', name, 'success',
            {'region': region, 'tags': tag_keys, 'abac': abac, 'usecase_id': usecase_id,
             'added_to_usecase': added_to_usecase}, event=event)

        bucket = {'name': name, 'region': region, 'portal_created': True, 'tags': tag_keys,
                  'abac': abac, 'added_to_usecase': added_to_usecase}
        if add_error is not None:
            bucket['add_error'] = add_error
        return create_response(201, {'bucket': bucket})
    except ClientError as e:
        logger.error(f"AWS error creating S3 bucket: {str(e)}")
        return create_response(500, {'error': f"Failed to create S3 bucket: {str(e)}"})
    except Exception as e:
        logger.error(f"Error creating S3 bucket: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


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
        
        # /s3-buckets - list (GET) or create (POST) buckets in the current account
        if path.endswith('/s3-buckets'):
            if http_method == 'GET':
                return list_s3_buckets(user)
            elif http_method == 'POST':
                return create_s3_bucket(event, user)
        
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
        elif http_method == 'POST' and path.endswith('/verify-role'):
            return verify_role(event, user)
        
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


def verify_role(event, user):
    """Verify that a cross-account role can be assumed"""
    try:
        body = json.loads(event.get('body', '{}'))
        role_arn = body.get('role_arn')
        external_id = body.get('external_id')

        if not role_arn:
            return create_response(400, {'error': 'role_arn is required'})

        try:
            assume_params = {
                'RoleArn': role_arn,
                'RoleSessionName': 'verify-role-access',
                'DurationSeconds': 900
            }
            if external_id:
                assume_params['ExternalId'] = external_id

            response = sts.assume_role(**assume_params)
            account_id = response['AssumedRoleUser']['Arn'].split(':')[4]

            return create_response(200, {
                'status': 'success',
                'account_id': account_id,
                'assumed_role': response['AssumedRoleUser']['Arn']
            })
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            logger.warning(f"Role verification failed: {error_code} - {error_msg}")
            return create_response(400, {
                'status': 'failed',
                'error': f"{error_code}: {error_msg}"
            })
    except Exception as e:
        logger.error(f"Error verifying role: {str(e)}")
        return create_response(500, {'error': 'Failed to verify role'})


def create_usecase(event, user):
    """Create a new use case - any authenticated user can create"""
    try:
        # Any authenticated user can create a usecase
        # They will be automatically assigned as UseCaseAdmin
        
        body = json.loads(event.get('body', '{}'))
        
        # For single-account setup: only name and s3_bucket are required
        # For multi-account setup: account_id, cross_account_role_arn, sagemaker_execution_role_arn are required
        
        # Check if this is single-account or multi-account setup
        is_single_account = not body.get('account_id')  # If no account_id provided, assume single-account
        
        if is_single_account:
            # Single-account setup: only name and s3_bucket required
            required_fields = ['name', 's3_bucket']
        else:
            # Multi-account setup: all fields required
            required_fields = ['name', 'account_id', 's3_bucket', 'cross_account_role_arn', 'sagemaker_execution_role_arn']
        
        validation_error = validate_required_fields(body, required_fields)
        if validation_error:
            return create_response(400, {'error': validation_error})

        # Workflow tuning Sample_Export settings (spec:
        # quality-prompt-tuning, Requirements 2.1, 2.9): optional at
        # creation, validated with exactly the same rules as an update.
        settings_error = tuning_settings.validate_settings(body)
        if settings_error:
            return create_response(400, {'error': settings_error})
        
        table = dynamodb.Table(USECASES_TABLE)
        usecase_id = str(uuid.uuid4())
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        # For single-account setup, auto-detect account ID from STS
        if is_single_account:
            try:
                sts_client = boto3.client('sts')
                account_id = sts_client.get_caller_identity()['Account']
                
                # Use DDASageMakerExecutionRole for single-account setup
                # This role must be pre-created using: ./deploy-account-role.sh single-account
                sagemaker_role_arn = f'arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole'
                cross_account_role_arn = f'arn:aws:iam::{account_id}:root'
            except Exception as e:
                logger.error(f"Failed to auto-detect account ID: {str(e)}")
                return create_response(500, {'error': 'Failed to auto-detect AWS account ID'})
        else:
            account_id = body['account_id']
            sagemaker_role_arn = body['sagemaker_execution_role_arn']
            cross_account_role_arn = body['cross_account_role_arn']
        
        item = {
            'usecase_id': usecase_id,
            'name': body['name'],
            'description': body.get('description', ''),
            'account_id': account_id,
            'region': body.get('region', os.environ.get('AWS_REGION', 'us-east-1')),
            's3_bucket': body['s3_bucket'],
            'cross_account_role_arn': cross_account_role_arn,
            'sagemaker_execution_role_arn': sagemaker_role_arn,
            'external_id': body.get('external_id', str(uuid.uuid4())),
            'owner': body.get('owner', user['email']),
            'cost_center': body.get('cost_center', ''),
            'default_device_group': body.get('default_device_group', ''),
            'created_at': timestamp,
            'updated_at': timestamp,
            'tags': body.get('tags', {}),
            'shared_components_provisioned': False,
            'setup_type': 'single-account' if is_single_account else 'multi-account'
        }
        
        # Validate Data Account configuration - external_id is required for security
        if body.get('data_account_id') and body.get('data_account_id') != account_id:
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
        # For single-account setups (data_account_id == account_id), don't store role/external_id
        is_data_account_separate = body.get('data_account_id') and body.get('data_account_id') != account_id
        
        if body.get('data_account_id'):
            item['data_account_id'] = body['data_account_id']
        
        # Only store data_account_role_arn for multi-account setups
        if body.get('data_account_role_arn') and is_data_account_separate:
            item['data_account_role_arn'] = body['data_account_role_arn']
        
        # Only store data_account_external_id for multi-account setups
        if body.get('data_account_external_id') and not is_single_account:
            item['data_account_external_id'] = body['data_account_external_id']
        
        if body.get('data_s3_bucket'):
            item['data_s3_bucket'] = body['data_s3_bucket']

        # Sample_Export settings, stored only when the caller set them, so a
        # Use_Case created without them is byte-identical to pre-feature
        # (Requirement 11.3).
        for field in (tuning_settings.SAMPLE_EXPORT_FIELD,
                      tuning_settings.SAMPLE_RETENTION_FIELD):
            if field in body:
                item[field] = body[field]

        # Use-case bucket configuration (design §5.4): every bucket the new
        # record names must be Eligible and Unclaimed before anything is
        # written. The two configured buckets opt into the tag
        # read-after-write retry (the wizard may have created and tagged
        # them seconds earlier); the optional additional list (<= 10 names)
        # names pre-existing buckets and is probed without retry.
        try:
            additional = _bucket_name_list(
                body.get('additional_s3_buckets'), 'additional_s3_buckets',
                usecase_id=usecase_id, purpose='allow-additional-bucket')
        except InvalidField as e:
            return create_response(400, e.to_body())
        except BucketConfigError as e:
            return bucket_config_error_response(e)
        if additional:
            item['additional_s3_buckets'] = additional
        attach_checks = [('s3_bucket', item['s3_bucket'], 'configure-s3-bucket', True)]
        if not _is_clear(item.get('data_s3_bucket')):
            attach_checks.append(('data_s3_bucket', item['data_s3_bucket'],
                                  'configure-data-s3-bucket', True))
        attach_checks.extend(('additional_s3_buckets', name, 'allow-additional-bucket', False)
                             for name in additional)
        attached = []          # (attribute, bucket, EligibilityResult) for the audit rows
        rejected = []
        first_error = None
        for attribute, bucket, purpose, retry in attach_checks:
            try:
                attached.append((attribute, bucket, check_bucket_attachable(
                    target_usecase_id=usecase_id, usecase_like=item, bucket=bucket,
                    user=user, event=event, purpose=purpose, retry=retry)))
            except BucketConfigError as e:
                rejected.append(_rejection(bucket, e))
                first_error = first_error or e
        if rejected:
            log_audit_event(
                user['user_id'], 'create_usecase', 'usecase', usecase_id, 'failure',
                {'reason': 'bucket_not_eligible', 'bucket': first_error.bucket,
                 'eligibility_reason': first_error.reason}, event=event)
            return _refused_response(first_error, rejected)

        table.put_item(Item=item)

        # The attach audit row, once per bucket the record names (design §1.6).
        for attribute, bucket, result in attached:
            audit_bucket_allowed(user, usecase_id, bucket, attribute, 'create', result, event)
        
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
            body.get('data_account_id') != account_id):
            
            logger.info(f"Separate Data Account detected, updating bucket policy for SageMaker access")
            
            try:
                data_bucket_policy_result = update_data_bucket_policy_for_sagemaker(
                    data_account_role_arn=body['data_account_role_arn'],
                    data_account_external_id=body['data_account_external_id'],  # Required for production
                    data_bucket_name=body['data_s3_bucket'],
                    usecase_account_id=account_id,
                    sagemaker_role_arn=sagemaker_role_arn
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
            body.get('data_account_id') != account_id and
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
            body.get('data_account_id') != account_id):
            
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
        
        # Update component bucket policy to allow Greengrass devices to download components
        # Component bucket is in Portal Account, devices are in UseCase Account
        component_bucket_policy_result = None
        component_bucket_name = os.environ.get('COMPONENT_BUCKET', 'dda-component-us-east-1-164152369890')
        
        logger.info(f"Updating component bucket policy for devices in usecase account {account_id}")
        
        try:
            component_bucket_policy_result = update_component_bucket_policy_for_greengrass(
                component_bucket_name=component_bucket_name,
                device_account_id=account_id  # Devices will be in the usecase account
            )
            
            # Update usecase with component bucket policy status
            table.update_item(
                Key={'usecase_id': usecase_id},
                UpdateExpression='SET component_bucket_policy_configured = :configured, component_bucket_policy_result = :result',
                ExpressionAttributeValues={
                    ':configured': component_bucket_policy_result.get('status') in ['success', 'already_configured'],
                    ':result': component_bucket_policy_result
                }
            )
            item['component_bucket_policy_configured'] = component_bucket_policy_result.get('status') in ['success', 'already_configured']
            item['component_bucket_policy_result'] = component_bucket_policy_result
            
            logger.info(f"Component bucket policy update result: {component_bucket_policy_result.get('status')}")
            
        except Exception as e:
            logger.warning(f"Failed to update component bucket policy for usecase {usecase_id}: {str(e)}")
            component_bucket_policy_result = {'status': 'failed', 'error': str(e)}
        
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
                    usecase_account_id=account_id,
                    cross_account_role_arn=cross_account_role_arn,
                    external_id=item['external_id'],
                    user_id=user['user_id'],
                    auth_token=None  # Don't pass auth token for internal calls
                )
                
                # Check if provisioning was accepted (202) or completed (200)
                # 202 means async provisioning started - the database will be updated by the background thread
                # 200 means provisioning completed synchronously (fallback case)
                if shared_components_result.get('status') in ['accepted', 'success', 'completed']:
                    logger.info(f"Provisioning accepted/completed with status: {shared_components_result.get('status')}")
                    
                    # For async provisioning (202 Accepted), don't update the database here
                    # The background thread will update it when provisioning completes
                    # For now, leave shared_components_provisioned as False
                    if shared_components_result.get('status') == 'accepted':
                        logger.info(f"Async provisioning started for usecase {usecase_id} - will be updated by background thread")
                        item['shared_components_provisioned'] = False
                        item['shared_components'] = {'status': 'provisioning', 'message': 'Provisioning in progress'}
                    else:
                        # Synchronous completion (fallback case)
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
            eventbridge_result = configure_eventbridge_permission(account_id)
            
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
        
        # Use-case bucket configuration (design §5.2): the computed allowed
        # set (the portal's own buckets are never in it, even when the raw
        # record names one) and the additional list, absent ≡ [].
        item = dict(response['Item'])
        item['allowed_s3_buckets'] = sorted(allowed_s3_buckets(item))
        item['additional_s3_buckets'] = item.get('additional_s3_buckets') or []
        return create_response(200, {'usecase': item})
        
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

        # Workflow tuning Sample_Export settings (spec:
        # quality-prompt-tuning, Requirements 2.1, 2.9). Validated and
        # normalized before anything is written: an invalid value is a 400
        # rather than a Use_Case whose devices export to nowhere.
        # deployments.py turns tuning_sample_export into the LocalServer
        # `workflowTuning` component configuration plus the device role
        # grant; tuning_sample_retention_days drives the Sample_Store
        # lifecycle expiry.
        settings_error = tuning_settings.validate_settings(body)
        if settings_error:
            log_audit_event(
                user['user_id'], 'update_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'invalid_setting',
                            'error': settings_error}
            )
            return create_response(400, {'error': settings_error})

        # Use-case bucket configuration (design §5.3). The additional list is
        # changed only through add / remove deltas; every bucket entering the
        # record (a changed configured bucket, each add) must be Eligible and
        # Unclaimed, and nothing is written while any of them is refused.
        if 'additional_s3_buckets' in body:
            return create_response(400, {
                'error': 'additional_s3_buckets is changed through additional_s3_buckets_add '
                         'and additional_s3_buckets_remove',
                'error_code': 'unsupported_field', 'field': 'additional_s3_buckets'})
        try:
            adds = _bucket_name_list(body.pop('additional_s3_buckets_add', None),
                                     'additional_s3_buckets_add', usecase_id=usecase_id,
                                     purpose='allow-additional-bucket')
            removes = _bucket_name_list(body.pop('additional_s3_buckets_remove', None),
                                        'additional_s3_buckets_remove', usecase_id=usecase_id,
                                        purpose='remove-additional-bucket')
        except InvalidField as e:
            return create_response(400, e.to_body())
        except BucketConfigError as e:
            return bucket_config_error_response(e)

        record = table.get_item(Key={'usecase_id': usecase_id}).get('Item')
        if record is None:
            return _usecase_not_found()
        
        updatable_fields = [
            'name', 'description', 's3_bucket', 'region', 'owner', 'default_device_group',
            'cross_account_role_arn', 'account_id',
            # Data Account fields
            'data_account_id', 'data_account_role_arn', 'data_account_external_id',
            'data_s3_bucket',
            # Workflow tuning Sample_Export settings (quality-prompt-tuning
            # 2.1, 2.9) — validated above
            tuning_settings.SAMPLE_EXPORT_FIELD,
            tuning_settings.SAMPLE_RETENTION_FIELD
        ]
        # The record as it will be after the SET (role fields included), so
        # the probe uses the credentials the portal will later use.
        effective = dict(record)
        effective.update({field: body[field] for field in updatable_fields if field in body})

        # Clear semantics: data_s3_bucket None / '' / whitespace is a Clear
        # (stored as today, never probed); s3_bucket is required.
        if 's3_bucket' in body and _is_clear(body['s3_bucket']):
            return bucket_config_error_response(InvalidBucketName(
                "s3_bucket is required and cannot be cleared", usecase_id=usecase_id,
                purpose='configure-s3-bucket'))
        configured_changes = []        # (attribute, bucket, purpose) for buckets that change
        for attribute, purpose in (('s3_bucket', 'configure-s3-bucket'),
                                   ('data_s3_bucket', 'configure-data-s3-bucket')):
            if attribute in body and not _is_clear(body[attribute]) and \
                    _bucket_value(body[attribute]) != _bucket_value(record.get(attribute)):
                configured_changes.append((attribute, body[attribute], purpose))

        # Cap on the additional list, before any probe.
        current = list(record.get('additional_s3_buckets') or [])
        if len(set(current) | set(adds)) - len(set(removes) & set(current)) > MAX_ADDITIONAL_BUCKETS:
            return bucket_config_error_response(BucketNotEligible(
                "The use case would exceed the maximum number of additional buckets",
                usecase_id=usecase_id, purpose='allow-additional-bucket',
                reason='too_many_buckets', remedy=REMEDY_TOO_MANY))

        # Every bucket entering the record is validated; every refusal is
        # reported, nothing is written while any remains.
        configured_results = {}        # attribute -> (bucket, EligibilityResult)
        add_results = {}               # bucket -> EligibilityResult
        rejected = []
        first_error = None
        for attribute, bucket, purpose in configured_changes:
            try:
                configured_results[attribute] = (bucket, check_bucket_attachable(
                    target_usecase_id=usecase_id, usecase_like=effective, bucket=bucket,
                    user=user, event=event, purpose=purpose, retry=False))
            except BucketConfigError as e:
                rejected.append(_rejection(bucket, e))
                first_error = first_error or e
        for name in adds:
            if name in current:
                continue
            try:
                add_results[name] = check_bucket_attachable(
                    target_usecase_id=usecase_id, usecase_like=effective, bucket=name,
                    user=user, event=event, purpose='allow-additional-bucket', retry=False)
            except BucketConfigError as e:
                rejected.append(_rejection(name, e))
                first_error = first_error or e
        if rejected:
            return _refused_response(first_error, rejected)

        # Build update expression with expression attribute names for reserved keywords
        update_expr = "SET updated_at = :updated_at"
        expr_values = {':updated_at': int(datetime.utcnow().timestamp() * 1000)}
        expr_names = {}
        for field in updatable_fields:
            if field in body:
                alias = f"#{field}"
                expr_names[alias] = field
                update_expr += f", {alias} = :{field}"
                expr_values[f":{field}"] = body[field]
        
        update_params = {
            'Key': {'usecase_id': usecase_id},
            'UpdateExpression': update_expr,
            'ExpressionAttributeValues': expr_values,
            # Never materialise a record: a PUT on a deleted use case is a 404.
            'ConditionExpression': 'attribute_exists(usecase_id)',
        }
        if expr_names:
            update_params['ExpressionAttributeNames'] = expr_names
        
        try:
            table.update_item(**update_params)
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            logger.warning(f"Use case {usecase_id} disappeared before the update was written")
            return _usecase_not_found()
        for attribute, (bucket, result) in configured_results.items():
            audit_bucket_allowed(user, usecase_id, bucket, attribute, 'api', result, event)

        # Adds, then removes (design §5.1); each change is independently
        # valid, so a write failure mid-sequence reports what was applied.
        added, already_present, removed, not_present = [], [], [], []
        applied = {'added': added, 'removed': removed}
        try:
            for name in adds:
                if name in current:
                    already_present.append(name)
                    continue
                outcome = _add_additional_bucket(table, usecase_id, name)
                if outcome == 'added':
                    added.append(name)
                    audit_bucket_allowed(user, usecase_id, name, 'additional_s3_buckets',
                                         'api', add_results[name], event)
                elif outcome == 'already_present':
                    already_present.append(name)
                else:
                    return _usecase_not_found(applied=applied)
            for name in removes:
                outcome = _remove_additional_bucket(table, usecase_id, name)
                if outcome == 'removed':
                    removed.append(name)
                    log_audit_event(
                        user['user_id'], 's3_bucket_removed', 'usecase', usecase_id, 'success',
                        {'bucket': name, 'attribute': 'additional_s3_buckets', 'source': 'api'},
                        event=event)
                elif outcome == 'not_present':
                    not_present.append(name)
                elif outcome == 'usecase_not_found':
                    return _usecase_not_found(applied=applied)
                else:
                    return create_response(409, {
                        'error': 'The additional buckets changed concurrently; retry the request',
                        'error_code': 'concurrent_update', 'bucket': name, 'applied': applied})
        except ClientError as e:
            logger.error(f"Error writing the additional buckets of use case {usecase_id}: {str(e)}")
            log_audit_event(
                user['user_id'], 'update_usecase', 'usecase', usecase_id, 'failure',
                {'reason': 'write_failed', 'applied': applied})
            return create_response(500, {'error': 'Failed to update use case', 'applied': applied})
        
        log_audit_event(
            user['user_id'], 'update_usecase', 'usecase', usecase_id,
            'success', {'updated_fields': list(body.keys())}
        )
        
        reloaded = table.get_item(Key={'usecase_id': usecase_id}).get('Item') or {}
        return create_response(200, {
            'message': 'Use case updated successfully',
            'added': added, 'already_present': already_present,
            'removed': removed, 'not_present': not_present,
            'allowed_s3_buckets': sorted(allowed_s3_buckets(reloaded)),
        })
        
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
