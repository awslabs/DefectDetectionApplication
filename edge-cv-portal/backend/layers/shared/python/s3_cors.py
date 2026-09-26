"""Bucket CORS for browser uploads, shared by every presigned-PUT route.

Moved verbatim from functions/data_management.py (detector-checkpoint-import
task 5.2), so that model_converter.get_model_upload_url configures a use case
bucket exactly as data_management.get_upload_url does (Requirement 3.5).
data_management.py re-imports it, and its behaviour is unchanged.
"""
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()


def ensure_bucket_cors(s3_client: boto3.client, bucket: str) -> None:
    """
    Ensure S3 bucket has CORS configuration that includes the current CloudFront domain.
    This allows the frontend to make PUT requests directly to S3.
    
    If CORS already exists but doesn't include the current CloudFront domain,
    we add a new rule for it (preserving existing rules).
    """
    cloudfront_domain = os.environ.get('CLOUDFRONT_DOMAIN', '')
    current_origin = f"https://{cloudfront_domain}" if cloudfront_domain else None
    
    try:
        # Check if CORS is already configured
        existing_rules = []
        try:
            existing_cors = s3_client.get_bucket_cors(Bucket=bucket)
            existing_rules = existing_cors.get('CORSRules', [])
            logger.info(f"Bucket {bucket} has {len(existing_rules)} existing CORS rule(s)")
            
            # Check if current CloudFront domain is already allowed
            if current_origin:
                for rule in existing_rules:
                    allowed_origins = rule.get('AllowedOrigins', [])
                    if current_origin in allowed_origins or '*' in allowed_origins:
                        logger.info(f"Bucket {bucket} CORS already allows {current_origin}")
                        return
                
                # Current domain not found - add it
                logger.info(f"Adding {current_origin} to CORS for bucket {bucket}")
                existing_rules.append({
                    'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
                    'AllowedOrigins': [current_origin],
                    'AllowedHeaders': ['*'],
                    'ExposeHeaders': ['ETag', 'x-amz-version-id', 'x-amz-request-id'],
                    'MaxAgeSeconds': 3000
                })
                s3_client.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRules': existing_rules})
                logger.info(f"CORS updated for bucket {bucket} with origin {current_origin}")
                return
            else:
                # No CloudFront domain configured - check if wildcard exists
                for rule in existing_rules:
                    if '*' in rule.get('AllowedOrigins', []):
                        logger.info(f"Bucket {bucket} has wildcard CORS origin, OK")
                        return
                # No wildcard and no domain - existing CORS may be stale but we can't fix without knowing the domain
                logger.warning(f"Bucket {bucket} has CORS but no CLOUDFRONT_DOMAIN env var set. CORS may be stale.")
                return
                
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchCORSConfiguration':
                logger.warning(f"Error checking existing CORS: {str(e)}")
                raise
        
        # No CORS at all - create fresh config
        origins = [current_origin] if current_origin else ['*']
        cors_config = {
            'CORSRules': [
                {
                    'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
                    'AllowedOrigins': origins,
                    'AllowedHeaders': ['*'],
                    'ExposeHeaders': ['ETag', 'x-amz-version-id', 'x-amz-request-id'],
                    'MaxAgeSeconds': 3000
                }
            ]
        }
        
        s3_client.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors_config)
        logger.info(f"CORS configuration applied to bucket {bucket} with origins {origins}")
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_msg = e.response.get('Error', {}).get('Message', str(e))
        
        if 'AccessDenied' in error_code or 'Forbidden' in error_code:
            logger.warning(f"Insufficient permissions to configure CORS for bucket {bucket}. "
                          f"Please ensure the Lambda execution role has s3:PutBucketCors permission. "
                          f"Error: {error_msg}")
        else:
            logger.warning(f"Could not configure CORS for bucket {bucket}: {error_code} - {error_msg}")
    except Exception as e:
        logger.warning(f"Unexpected error configuring CORS: {str(e)}")
