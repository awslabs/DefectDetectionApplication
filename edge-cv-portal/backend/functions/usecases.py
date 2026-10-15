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
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import (
    ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError,
)
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
    # Portal authorization and IAM scoping (design I1): the registry error
    # the creation permission check can raise, the account grammar, the
    # trusted-account check, and the one component bucket name and policy
    # bound both component bucket policy writers share.
    RegistryUnavailable, validate_account_id, validate_role_arn,
    account_trusted, TrustedAccountsNotConfigured,
    component_bucket_name, COMPONENT_BUCKET_POLICY_MAX_BYTES,
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

# Portal authorization and IAM scoping (design I1). The role names the
# account templates create (the only ones the account grammar accepts), the
# Proof_Of_Control session, the STS error codes that mean "unavailable"
# rather than "denied", and the seven account fields the PUT rules are keyed
# on (Account_Field_Changes).
USECASE_ACCESS_ROLE_NAME = 'DDAPortalAccessRole'
DATA_ACCESS_ROLE_NAME = 'DDAPortalDataAccessRole'
VERIFY_CONTROL_SESSION_NAME = 'dda-portal-verify-control'
VERIFY_CONTROL_DURATION_SECONDS = 900
STS_UNAVAILABLE_CODES = frozenset({'Throttling', 'ThrottlingException', 'RequestLimitExceeded'})
STS_UNAVAILABLE_MESSAGE = 'STS is temporarily unavailable; retry'
ACCOUNT_FIELDS = ('account_id', 'cross_account_role_arn', 'external_id', 'data_account_id',
                  'data_account_role_arn', 'data_account_external_id',
                  'sagemaker_execution_role_arn')
DATA_ACCOUNT_FIELDS = frozenset({'data_account_id', 'data_account_role_arn',
                                 'data_account_external_id'})
USECASE_ACCOUNT_FIELDS = frozenset({'account_id', 'cross_account_role_arn', 'external_id'})
REMEDY_ACCOUNT_NOT_TRUSTED = 'Add the account to trustedUseCaseAccountIds and redeploy the portal.'
REMEDY_ACCOUNT_CONTROL = ('Deploy DDAPortalUseCaseAccountStack in the use-case account and supply '
                          'its external id.')
# Statement ids of the per-account grants on the component bucket policy and
# the default event bus (update_component_bucket_policy_for_greengrass,
# configure_eventbridge_permission and the shared-components writer).
GREENGRASS_DEVICE_READ_SID = 'AllowGreengrassDeviceRead-{account}'
GREENGRASS_DEVICE_LIST_SID = 'AllowGreengrassDeviceList-{account}'
SHARED_COMPONENTS_ROLES_SID = 'AllowUseCaseAccountsGreengrassAccess'
SHARED_COMPONENTS_SERVICE_SID = 'AllowGreengrassServiceAccess'
EVENTBRIDGE_STATEMENT_ID = 'AllowUseCaseAccount-{account}'


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


def verify_account_control(role_arn: str, external_id, expected_account_id: str) -> dict:
    """Proof_Of_Control (design I1 D2): one sts:AssumeRole on the use case's
    DDAPortalAccessRole with the supplied external id, shared by
    create_usecase, update_usecase and verify_role.

    Called only after the account passed the trusted-account check, so a
    denial means the role or the external id in that account is wrong.
    The temporary credentials are never logged or returned.

    Returns:
        {'ok': True, 'assumed_role_arn': <AssumedRoleUser.Arn>} or
        {'ok': False, 'reason': 'access_denied' | 'no_such_entity' |
         'account_mismatch' | 'sts_unavailable', 'detail': '<Code>: <Message>'}.
    """
    params = {
        'RoleArn': role_arn,
        'RoleSessionName': VERIFY_CONTROL_SESSION_NAME,
        'DurationSeconds': VERIFY_CONTROL_DURATION_SECONDS,
    }
    if external_id:
        params['ExternalId'] = external_id
    try:
        response = boto3.client('sts').assume_role(**params)
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', 'Unknown')
        detail = f"{code}: {error.get('Message', '')}"
        if code in STS_UNAVAILABLE_CODES:
            logger.error(f"STS unavailable while assuming {role_arn}: {code}")
            return {'ok': False, 'reason': 'sts_unavailable', 'detail': detail}
        reason = 'no_such_entity' if code == 'NoSuchEntity' else 'access_denied'
        logger.warning(f"Proof of control failed for {role_arn}: {code}")
        return {'ok': False, 'reason': reason, 'detail': detail}
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as e:
        logger.error(f"STS unreachable while assuming {role_arn}: {type(e).__name__}")
        return {'ok': False, 'reason': 'sts_unavailable', 'detail': f"{type(e).__name__}: {e}"}
    assumed_role_arn = response['AssumedRoleUser']['Arn']
    assumed_account = assumed_role_arn.split(':')[4]
    if assumed_account != expected_account_id:
        logger.warning(f"Proof of control for {role_arn} assumed a role in account "
                       f"{assumed_account}, expected {expected_account_id}")
        return {'ok': False, 'reason': 'account_mismatch',
                'detail': (f"AccountMismatch: the assumed role is in account {assumed_account}, "
                           f"not {expected_account_id}")}
    return {'ok': True, 'assumed_role_arn': assumed_role_arn}


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
        component_bucket_name: S3 bucket name in Portal Account (component_bucket_name(), e.g.
            dda-component-<region>-<portal account id>)
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
        
        # Define the Greengrass device access statements (remove_account_grants
        # removes them by the same statement ids)
        greengrass_read_sid = GREENGRASS_DEVICE_READ_SID.format(account=device_account_id)
        greengrass_list_sid = GREENGRASS_DEVICE_LIST_SID.format(account=device_account_id)
        
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

        # Bounded growth (Requirement 3.3): refuse a policy S3 would reject
        # rather than attempt the PutBucketPolicy.
        policy_document = json.dumps(existing_policy)
        if len(policy_document.encode()) > COMPONENT_BUCKET_POLICY_MAX_BYTES:
            logger.warning(
                f"Component bucket policy for {component_bucket_name} would exceed "
                f"{COMPONENT_BUCKET_POLICY_MAX_BYTES} bytes with device account "
                f"{device_account_id}; not written")
            return {
                'status': 'failed',
                'error': 'component bucket policy would exceed the size limit',
                'bucket': component_bucket_name
            }
        
        # Put updated bucket policy
        s3_component.put_bucket_policy(
            Bucket=component_bucket_name,
            Policy=policy_document
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
        statement_id = EVENTBRIDGE_STATEMENT_ID.format(account=usecase_account_id)
        
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


def add_account_grants(account_id: str) -> dict:
    """The grants creation writes for a use case's account, for an account
    move on PUT /usecases/{id} (design I1): the component bucket statements
    for the account's device role and the EventBridge PutEvents permission.
    Best-effort; both writers report their own failures."""
    return {
        'component_bucket_policy': update_component_bucket_policy_for_greengrass(
            component_bucket_name=component_bucket_name(), device_account_id=account_id),
        'eventbridge': configure_eventbridge_permission(account_id),
    }


def _usecases_in_account(table, account_id: str) -> set:
    """Ids of the use case records whose account_id is `account_id`."""
    usecase_ids = set()
    scan_args = {'FilterExpression': Attr('account_id').eq(account_id),
                 'ProjectionExpression': 'usecase_id'}
    while True:
        page = table.scan(**scan_args)
        usecase_ids.update(item['usecase_id'] for item in page.get('Items', []))
        if 'LastEvaluatedKey' not in page:
            return usecase_ids
        scan_args['ExclusiveStartKey'] = page['LastEvaluatedKey']


def _principal_account(principal) -> str:
    """The account of an IAM principal in a bucket policy: a bare account id,
    or the account segment of an IAM ARN; None for anything else."""
    if not isinstance(principal, str):
        return None
    if principal.isdigit():
        return principal
    parts = principal.split(':')
    return parts[4] if principal.startswith('arn:') and len(parts) > 5 else None


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _remove_component_bucket_grants(bucket: str, account_id: str) -> dict:
    """Remove an account's grants from the component bucket policy: the two
    AllowGreengrassDevice{Read,List}-<account> statements, the account's
    principals in AllowUseCaseAccountsGreengrassAccess and the account in
    the aws:SourceAccount condition of AllowGreengrassServiceAccess. A
    statement left without principals or source accounts is dropped. S3
    stores no policy without statements, so a removal that would empty the
    policy is reported failed and nothing is written."""
    s3_component = boto3.client('s3')
    try:
        try:
            policy = json.loads(s3_component.get_bucket_policy(Bucket=bucket)['Policy'])
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'NoSuchBucketPolicy':
                return {'status': 'not_present', 'bucket': bucket}
            raise
        device_sids = {GREENGRASS_DEVICE_READ_SID.format(account=account_id),
                       GREENGRASS_DEVICE_LIST_SID.format(account=account_id)}
        kept = []
        changed = False
        for statement in policy.get('Statement', []):
            sid = statement.get('Sid')
            if sid in device_sids:
                changed = True
                continue
            if sid == SHARED_COMPONENTS_ROLES_SID and isinstance(statement.get('Principal'), dict):
                principals = _as_list(statement['Principal'].get('AWS'))
                remaining = [p for p in principals if _principal_account(p) != account_id]
                if len(remaining) != len(principals):
                    changed = True
                    if not remaining:
                        continue
                    statement['Principal'] = {**statement['Principal'], 'AWS': remaining}
            if sid == SHARED_COMPONENTS_SERVICE_SID:
                string_equals = (statement.get('Condition') or {}).get('StringEquals') or {}
                accounts = _as_list(string_equals.get('aws:SourceAccount'))
                remaining = [a for a in accounts if a != account_id]
                if len(remaining) != len(accounts):
                    changed = True
                    if not remaining:
                        continue
                    string_equals['aws:SourceAccount'] = remaining
            kept.append(statement)
        if not changed:
            return {'status': 'not_present', 'bucket': bucket}
        if not kept:
            logger.warning(f"Removing account {account_id} would leave the policy of {bucket} "
                           f"without statements; not written")
            return {'status': 'failed', 'bucket': bucket,
                    'error': 'removing the account would leave the component bucket policy empty'}
        policy['Statement'] = kept
        s3_component.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
        return {'status': 'success', 'bucket': bucket}
    except ClientError as e:
        error = e.response.get('Error', {})
        logger.warning(f"Could not remove account {account_id} from the policy of {bucket}: "
                       f"{error.get('Code')}")
        return {'status': 'failed', 'bucket': bucket,
                'error': f"{error.get('Code')}: {error.get('Message')}"}
    except Exception as e:
        logger.warning(f"Could not remove account {account_id} from the policy of {bucket}: {e}")
        return {'status': 'failed', 'bucket': bucket, 'error': str(e)}


def _remove_eventbridge_permission(account_id: str) -> dict:
    """Remove the account's PutEvents permission on the default event bus."""
    statement_id = EVENTBRIDGE_STATEMENT_ID.format(account=account_id)
    try:
        boto3.client('events').remove_permission(EventBusName='default', StatementId=statement_id)
        return {'status': 'success', 'statement_id': statement_id}
    except ClientError as e:
        error = e.response.get('Error', {})
        if error.get('Code') == 'ResourceNotFoundException':
            return {'status': 'not_present', 'statement_id': statement_id}
        logger.warning(f"Could not remove EventBridge permission {statement_id}: {error.get('Code')}")
        return {'status': 'failed', 'statement_id': statement_id,
                'error': f"{error.get('Code')}: {error.get('Message')}"}
    except Exception as e:
        logger.warning(f"Could not remove EventBridge permission {statement_id}: {e}")
        return {'status': 'failed', 'statement_id': statement_id, 'error': str(e)}


def remove_account_grants(account_id: str, record: dict) -> dict:
    """Remove the grants creation wrote for a use case's account once no use
    case references that account any more (Requirement 3.4): on DELETE
    /usecases/{id} and on PUT /usecases/{id} when the use case moves to
    another account. Best-effort; returned as `grants_removed`.

    Skipped for a Single_Account_Record and for the portal account itself:
    the portal never removes its own account's statements.
    """
    if not is_cross_account_setup(record):
        return {'status': 'skipped', 'reason': 'single_account', 'account_id': account_id}
    if account_id == os.environ.get('PORTAL_ACCOUNT_ID'):
        return {'status': 'skipped', 'reason': 'portal_account', 'account_id': account_id}
    try:
        others = _usecases_in_account(dynamodb.Table(USECASES_TABLE), account_id)
    except Exception as e:
        logger.warning(f"Could not check which use cases reference account {account_id}: {e}")
        return {'status': 'failed', 'account_id': account_id, 'error': str(e)}
    others.discard(record.get('usecase_id'))
    if others:
        return {'status': 'skipped', 'reason': 'account_still_referenced', 'account_id': account_id}
    return {
        'status': 'completed',
        'account_id': account_id,
        'component_bucket_policy': _remove_component_bucket_grants(component_bucket_name(), account_id),
        'eventbridge': _remove_eventbridge_permission(account_id),
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
        elif http_method == 'POST' and path.endswith('/verify-role'):
            # Ahead of the generic POST, which would otherwise take it.
            return verify_role(event, user)
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


# ------------------------------------------------------------------------
# Account grammar, trusted-account check and Proof_Of_Control (design I1).
# ------------------------------------------------------------------------

def _account_value(value):
    """The comparable value of an account field: None for None, '' or
    whitespace (absent), the value itself otherwise."""
    return None if _is_clear(value) else value


def _usecase_account_grammar(account_id, cross_account_role_arn):
    """(error_code, message) when account_id or the pinned
    DDAPortalAccessRole ARN in it is malformed, else None (Requirement 3.1)."""
    if not validate_account_id(account_id):
        return 'INVALID_ACCOUNT_ID', 'account_id must be a twelve-digit AWS account id'
    if not validate_role_arn(cross_account_role_arn, account_id, role_name=USECASE_ACCESS_ROLE_NAME):
        return ('INVALID_ROLE_ARN',
                f'cross_account_role_arn must be arn:aws:iam::{account_id}:role/{USECASE_ACCESS_ROLE_NAME}')
    return None


def _sagemaker_role_grammar(sagemaker_role_arn, account_id, *, move=False):
    """(error_code, message) when the SageMaker execution role is not an IAM
    role ARN in account_id, else None. On an account move the message names
    the remedy: the role must be supplied in the new account."""
    if validate_role_arn(sagemaker_role_arn, account_id):
        return None
    if move:
        return 'INVALID_ROLE_ARN', f'supply sagemaker_execution_role_arn in account {account_id}'
    return ('INVALID_ROLE_ARN',
            f'sagemaker_execution_role_arn must be an IAM role ARN in account {account_id}')


def _data_account_grammar(account_id, data_account_id, data_account_role_arn):
    """(error_code, message) for a separate data account (data_account_id set
    and different from account_id) whose id is malformed or whose role ARN is
    missing or not the pinned DDAPortalDataAccessRole, else None."""
    if _account_value(data_account_id) is None or data_account_id == account_id:
        return None
    if not validate_account_id(data_account_id):
        return 'INVALID_ACCOUNT_ID', 'data_account_id must be a twelve-digit AWS account id'
    if _account_value(data_account_role_arn) is None:
        return 'INVALID_ROLE_ARN', 'supply data_account_role_arn for the separate data account'
    if not validate_role_arn(data_account_role_arn, data_account_id, role_name=DATA_ACCESS_ROLE_NAME):
        return ('INVALID_ROLE_ARN',
                f'data_account_role_arn must be arn:aws:iam::{data_account_id}:role/{DATA_ACCESS_ROLE_NAME}')
    return None


def _grammar_refusal(error) -> dict:
    code, message = error
    logger.warning(f"Account grammar refused ({code}): {message}")
    return create_response(400, {'error': message, 'error_code': code})


def _account_not_trusted_message(account_id) -> str:
    return (f"account {account_id} is not in the portal's trusted use-case accounts; "
            "add it to trustedUseCaseAccountIds and redeploy the portal")


def _account_control_refusal(user, event, action: str, resource_id: str, *,
                             account_id: str, role_arn: str, external_id) -> dict:
    """The trusted-account check, then Proof_Of_Control, for a multi-account
    configuration about to be written (create_usecase, update_usecase).
    None when both pass; otherwise the refusal to answer, audited as an
    `action` failure. No STS call is made for an untrusted account."""
    try:
        trusted = account_trusted(account_id)
    except TrustedAccountsNotConfigured as e:
        logger.error(f"{action}: {e}")
        return create_response(500, {'error': str(e)})
    if not trusted:
        logger.warning(f"{action}: account {account_id} is not a trusted use-case account")
        log_audit_event(user['user_id'], action, 'usecase', resource_id, 'failure',
                        {'reason': 'account_not_trusted', 'account_id': account_id}, event=event)
        return create_response(400, {
            'error': _account_not_trusted_message(account_id),
            'error_code': 'ACCOUNT_NOT_TRUSTED',
            'remedy': REMEDY_ACCOUNT_NOT_TRUSTED,
        })
    proof = verify_account_control(role_arn, external_id, account_id)
    if proof['ok']:
        return None
    if proof['reason'] == 'sts_unavailable':
        log_audit_event(user['user_id'], action, 'usecase', resource_id, 'failure',
                        {'reason': 'sts_unavailable', 'account_id': account_id}, event=event)
        return create_response(503, {'error': STS_UNAVAILABLE_MESSAGE, 'error_code': 'STS_UNAVAILABLE'})
    log_audit_event(user['user_id'], action, 'usecase', resource_id, 'failure',
                    {'reason': 'account_control_not_verified', 'account_id': account_id},
                    event=event)
    return create_response(400, {
        'error': (f"The portal could not assume {role_arn} in account {account_id} with the "
                  "supplied external id. Deploy DDAPortalUseCaseAccountStack in that account "
                  "and supply its external id."),
        'error_code': 'ACCOUNT_CONTROL_NOT_VERIFIED',
        'remedy': REMEDY_ACCOUNT_CONTROL,
    })


def _account_rules(usecase_id: str, body: dict, record: dict, user, event):
    """The PUT /usecases/{id} rules on the seven account fields (design I1
    "update_usecase rule"), evaluated on the Account_Field_Changes: a field
    whose normalized body value differs from the stored one, never key
    presence (the Edit modal echoes the stored account fields on every save).

    Runs after the record fetch and before the attach validation, so an
    account change that fails here makes no eligibility probe. The portal
    account is PORTAL_ACCOUNT_ID from the environment, so the paths that
    skip or refuse make no STS call of any kind.

    Returns (refusal, move): the response to answer, or None; and
    {'old_account_id', 'new_account_id'} when the body moves the use case to
    another account (proven), else None.
    """
    changes = {field for field in ACCOUNT_FIELDS
               if field in body and _account_value(body[field]) != _account_value(record.get(field))}
    if not changes:
        return None, None
    merged = {field: _account_value(body[field] if field in body else record.get(field))
              for field in ACCOUNT_FIELDS}
    portal_account = os.environ.get('PORTAL_ACCOUNT_ID') or None
    new_account = merged['account_id']
    # An absent account_id counts as the portal account.
    to_portal_account = new_account is None or new_account == portal_account
    moving = 'account_id' in changes and not to_portal_account

    if not is_cross_account_setup(record):
        if not moving:
            # The record stays single-account: no trusted check and no proof.
            if 'cross_account_role_arn' in changes:
                return _grammar_refusal((
                    'INVALID_ROLE_ARN',
                    'a single-account use case does not carry a cross-account role; '
                    'supply account_id to move it to another account')), None
            error = None
            if changes & DATA_ACCOUNT_FIELDS:
                error = _data_account_grammar(new_account or portal_account,
                                              merged['data_account_id'],
                                              merged['data_account_role_arn'])
            if error is None and 'sagemaker_execution_role_arn' in changes:
                error = _sagemaker_role_grammar(merged['sagemaker_execution_role_arn'],
                                                portal_account)
            return (_grammar_refusal(error) if error else None), None
        # Single-account -> multi-account: the body names the new account's role.
        if 'cross_account_role_arn' not in changes or merged['cross_account_role_arn'] is None:
            return _grammar_refusal((
                'INVALID_ROLE_ARN',
                'a cross-account role ARN is required to move a use case to another account')), None
    elif 'account_id' in changes and to_portal_account:
        return _grammar_refusal((
            'INVALID_ACCOUNT_ID',
            'a multi-account use case cannot be converted to single-account; '
            'create a new use case')), None

    proving = moving or bool(changes & USECASE_ACCOUNT_FIELDS)
    error = None
    if proving:
        error = (_usecase_account_grammar(new_account, merged['cross_account_role_arn'])
                 or _sagemaker_role_grammar(merged['sagemaker_execution_role_arn'], new_account,
                                            move=moving))
    elif 'sagemaker_execution_role_arn' in changes:
        error = _sagemaker_role_grammar(merged['sagemaker_execution_role_arn'], new_account)
    if error is None and changes & DATA_ACCOUNT_FIELDS:
        error = _data_account_grammar(new_account, merged['data_account_id'],
                                      merged['data_account_role_arn'])
    if error is not None:
        return _grammar_refusal(error), None
    if not proving:
        # A change confined to the data-account triple or to the SageMaker
        # execution role: grammar only, no STS.
        return None, None
    refusal = _account_control_refusal(
        user, event, 'update_usecase', usecase_id, account_id=new_account,
        role_arn=merged['cross_account_role_arn'], external_id=merged['external_id'])
    if refusal is not None:
        return refusal, None
    if moving:
        return None, {'old_account_id': record.get('account_id'), 'new_account_id': new_account}
    return None, None


def verify_role(event, user):
    """Verify that the portal can assume a use-case account's
    DDAPortalAccessRole (the onboarding wizard's Verify step).

    Gated like creation (CREATE_USECASES at the global scope); the role ARN
    must be the pinned DDAPortalAccessRole of a trusted account, and the
    optional account_id must name the same account, before the one
    Proof_Of_Control STS call. The 200 body and the STS-denial 400 body
    keep their existing shapes; every other refusal carries `status:
    'failed'`, `error` and `error_code`.
    """
    try:
        try:
            allowed = rbac_manager.has_permission(
                user['user_id'], 'global', Permission.CREATE_USECASES, user)
        except RegistryUnavailable as e:
            logger.error(f"Authorization check for verify_role failed: {e}")
            log_audit_event(user['user_id'], 'verify_role', 'usecase', 'new', 'failure',
                            {'reason': 'registry_unavailable'}, event=event)
            return create_response(500, {'error': 'Authorization check failed'})
        if not allowed:
            log_audit_event(
                user['user_id'], 'unauthorized_access', 'api_endpoint',
                event.get('path', 'unknown'), 'denied',
                details={'required_permission': Permission.CREATE_USECASES.value}, event=event)
            return create_response(403, {
                'status': 'failed',
                'error': 'Insufficient permissions to verify roles for use-case onboarding',
                'error_code': 'INSUFFICIENT_PERMISSIONS',
            })

        body = json.loads(event.get('body', '{}'))
        role_arn = body.get('role_arn')
        external_id = body.get('external_id')

        if not role_arn:
            return create_response(400, {'error': 'role_arn is required'})

        parts = role_arn.split(':') if isinstance(role_arn, str) else []
        expected_account_id = parts[4] if len(parts) > 5 else None
        if not validate_role_arn(role_arn, expected_account_id, role_name=USECASE_ACCESS_ROLE_NAME):
            logger.warning("verify-role: role_arn is not a DDAPortalAccessRole ARN")
            return create_response(400, {
                'status': 'failed',
                'error': f'role_arn must be arn:aws:iam::<account>:role/{USECASE_ACCESS_ROLE_NAME}',
                'error_code': 'INVALID_ROLE_ARN',
            })
        body_account_id = body.get('account_id')
        if not _is_clear(body_account_id) and body_account_id != expected_account_id:
            logger.warning("verify-role: account_id does not match role_arn")
            return create_response(400, {
                'status': 'failed',
                'error': 'account_id does not match role_arn',
                'error_code': 'INVALID_ACCOUNT_ID',
            })
        try:
            trusted = account_trusted(expected_account_id)
        except TrustedAccountsNotConfigured as e:
            logger.error(f"verify_role: {e}")
            return create_response(500, {'status': 'failed', 'error': str(e)})
        if not trusted:
            logger.warning(f"verify-role: account {expected_account_id} is not a trusted use-case account")
            log_audit_event(user['user_id'], 'verify_role', 'usecase', 'new', 'failure',
                            {'reason': 'account_not_trusted', 'account_id': expected_account_id},
                            event=event)
            return create_response(400, {
                'status': 'failed',
                'error': _account_not_trusted_message(expected_account_id),
                'error_code': 'ACCOUNT_NOT_TRUSTED',
            })

        proof = verify_account_control(role_arn, external_id, expected_account_id)
        if proof['ok']:
            return create_response(200, {
                'status': 'success',
                'account_id': expected_account_id,
                'assumed_role': proof['assumed_role_arn']
            })
        if proof['reason'] == 'sts_unavailable':
            return create_response(503, {
                'status': 'failed',
                'error': STS_UNAVAILABLE_MESSAGE,
                'error_code': 'STS_UNAVAILABLE',
            })
        logger.warning(f"Role verification failed: {proof['detail']}")
        return create_response(400, {
            'status': 'failed',
            'error': proof['detail']
        })
    except Exception as e:
        logger.error(f"Error verifying role: {str(e)}")
        return create_response(500, {'error': 'Failed to verify role'})


def create_usecase(event, user):
    """Create a use case (POST /usecases).

    Requires Permission.CREATE_USECASES at the global scope, resolved by
    rbac_manager in both registry enforcement states (PortalAdmin holds it
    in the role matrix); the creator is then assigned UseCaseAdmin on the
    new use case. A multi-account body (one naming account_id) must pass the
    account grammar, name an account in the portal's trusted list and pass
    Proof_Of_Control before the record is written or any account grant is
    made (design I1). A single-account body takes the account from
    sts:GetCallerIdentity, with no trusted check and no proof.
    """
    try:
        try:
            allowed = rbac_manager.has_permission(
                user['user_id'], 'global', Permission.CREATE_USECASES, user)
        except RegistryUnavailable as e:
            logger.error(f"Authorization check for create_usecase failed: {e}")
            log_audit_event(user['user_id'], 'create_usecase', 'usecase', 'new', 'failure',
                            {'reason': 'registry_unavailable'}, event=event)
            return create_response(500, {'error': 'Authorization check failed'})
        if not allowed:
            log_audit_event(
                user['user_id'], 'unauthorized_access', 'api_endpoint',
                event.get('path', 'unknown'), 'denied',
                details={'required_permission': Permission.CREATE_USECASES.value}, event=event)
            return create_response(403, {'error': 'Insufficient permissions to create use cases',
                                         'error_code': 'INSUFFICIENT_PERMISSIONS'})

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

        # Account grammar (Requirement 3.1), before any STS call: the pinned
        # role names in the named account, the SageMaker execution role in
        # the same account, and the data-account triple of a separate data
        # account. A single-account body names no account of its own; its
        # data-account triple is checked once the account is known (below).
        if not is_single_account:
            grammar_error = (
                _usecase_account_grammar(body['account_id'], body['cross_account_role_arn'])
                or _sagemaker_role_grammar(body['sagemaker_execution_role_arn'], body['account_id'])
                or _data_account_grammar(body['account_id'], body.get('data_account_id'),
                                         body.get('data_account_role_arn')))
            if grammar_error:
                return _grammar_refusal(grammar_error)

        # Workflow tuning Sample_Export settings (spec:
        # quality-prompt-tuning, Requirements 2.1, 2.9): optional at
        # creation, validated with exactly the same rules as an update.
        settings_error = tuning_settings.validate_settings(body)
        if settings_error:
            return create_response(400, {'error': settings_error})

        # Trusted-account check, then Proof_Of_Control (design I1 D2), before
        # anything is written and before any account grant.
        if not is_single_account:
            refusal = _account_control_refusal(
                user, event, 'create_usecase', 'new', account_id=body['account_id'],
                role_arn=body['cross_account_role_arn'], external_id=body.get('external_id'))
            if refusal is not None:
                return refusal
        
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
            grammar_error = _data_account_grammar(account_id, body.get('data_account_id'),
                                                  body.get('data_account_role_arn'))
            if grammar_error:
                return _grammar_refusal(grammar_error)
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
        # COMPONENT_BUCKET when set, else the {prefix}-{region}-{account}
        # name the SharedComponents function resolves too (Requirement 3.5).
        component_bucket = component_bucket_name()
        
        logger.info(f"Updating component bucket policy for devices in usecase account {account_id}")
        
        try:
            component_bucket_policy_result = update_component_bucket_policy_for_greengrass(
                component_bucket_name=component_bucket,
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

        # Account rules (design I1 "update_usecase rule"), keyed on the
        # Account_Field_Changes: grammar, trusted check and Proof_Of_Control
        # before any eligibility probe and before update_item.
        account_refusal, account_move = _account_rules(usecase_id, body, record, user, event)
        if account_refusal is not None:
            return account_refusal
        
        updatable_fields = [
            'name', 'description', 's3_bucket', 'region', 'owner', 'default_device_group',
            'cross_account_role_arn', 'account_id', 'external_id',
            # Data Account fields
            'data_account_id', 'data_account_role_arn', 'data_account_external_id',
            'data_s3_bucket',
            # Travels with an account move (design I1)
            'sagemaker_execution_role_arn',
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

        # Grant maintenance on an account move (design I1): the previous
        # account's grants are removed when no other use case references it,
        # then the new account gets the grants creation writes. Best-effort,
        # reported beside the other keys of the 200.
        grant_reports = {}
        if account_move is not None:
            grant_reports['grants_removed'] = remove_account_grants(
                account_move['old_account_id'], record)
            grant_reports['grants_added'] = add_account_grants(account_move['new_account_id'])

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
            **grant_reports,
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
        record = table.get_item(Key={'usecase_id': usecase_id}).get('Item')
        
        # TODO: Check if use case has active resources before deletion
        
        table.delete_item(Key={'usecase_id': usecase_id})

        # The account's grants go once no other use case references the
        # account (Requirement 3.4); best-effort, the deletion stands.
        if record is None:
            grants_removed = {'status': 'skipped', 'reason': 'usecase_not_found'}
        else:
            grants_removed = remove_account_grants(record.get('account_id'), record)
        
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'success'
        )
        
        return create_response(200, {'message': 'Use case deleted successfully',
                                     'grants_removed': grants_removed})
        
    except Exception as e:
        logger.error(f"Error deleting use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to delete use case'})
