"""
Example Lambda function showing how to use RBAC middleware.

This demonstrates how to protect API endpoints with authentication
and authorization using the RBAC utilities.
"""

import json
import logging
import os
from typing import Dict, Any

# Import shared utilities
import sys
sys.path.append('/opt/python')

from auth_middleware import (
    auth_required, require_permission, data_scientist_required,
    create_response, handle_cors_preflight, log_request
)
from rbac_utils import Permission

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

@handle_cors_preflight
@log_request
@auth_required
@require_permission(Permission.CREATE_LABELING_JOB, 'usecase_id')
def create_labeling_job_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Example handler for creating a labeling job.
    
    Requires:
    - Authentication (valid JWT)
    - CREATE_LABELING_JOB permission
    - Access to the specified use case
    """
    try:
        user_context = event['user_context']
        usecase_id = event['pathParameters']['usecase_id']
        body = json.loads(event['body'])
        
        # Your business logic here
        job_data = {
            'job_id': 'job-123',
            'usecase_id': usecase_id,
            'name': body['name'],
            'created_by': user_context.user_id,
            'status': 'pending'
        }
        
        logger.info(f"Created labeling job {job_data['job_id']} for use case {usecase_id}")
        
        return create_response(201, {
            'message': 'Labeling job created successfully',
            'job': job_data
        })
        
    except json.JSONDecodeError:
        return create_response(400, {
            'error': 'Invalid JSON',
            'message': 'Request body must be valid JSON'
        })
    except KeyError as e:
        return create_response(400, {
            'error': 'Missing required field',
            'message': f'Required field missing: {str(e)}'
        })
    except Exception as e:
        logger.error(f"Error creating labeling job: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@handle_cors_preflight
@log_request
@data_scientist_required('usecase_id')
def get_training_jobs_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Example handler for listing training jobs.
    
    Uses convenience decorator that combines:
    - Authentication
    - DataScientist role or higher
    - Use case access
    """
    try:
        user_context = event['user_context']
        usecase_id = event['pathParameters']['usecase_id']
        
        # Your business logic here
        training_jobs = [
            {
                'training_id': 'train-123',
                'usecase_id': usecase_id,
                'model_name': 'defect-classifier',
                'status': 'completed',
                'created_by': user_context.user_id
            }
        ]
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'training_jobs': training_jobs
        })
        
    except Exception as e:
        logger.error(f"Error getting training jobs: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@handle_cors_preflight
@log_request
@auth_required
def get_user_profile_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Example handler for getting user profile.
    
    Only requires authentication - users can view their own profile.
    """
    try:
        user_context = event['user_context']
        
        profile = {
            'user_id': user_context.user_id,
            'email': user_context.email,
            'roles': [role.value for role in user_context.roles],
            'assigned_usecases': list(user_context.assigned_usecases),
            'is_super_user': user_context.is_super_user
        }
        
        return create_response(200, profile)
        
    except Exception as e:
        logger.error(f"Error getting user profile: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

# Example of how to route different HTTP methods
@handle_cors_preflight
@log_request
@auth_required
def multi_method_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Example handler that routes different HTTP methods with different permissions.
    """
    try:
        method = event['httpMethod']
        
        if method == 'GET':
            # Anyone authenticated can read
            return get_resource(event, context)
        elif method == 'POST':
            # Need create permission
            return create_resource(event, context)
        elif method == 'PUT':
            # Need update permission
            return update_resource(event, context)
        elif method == 'DELETE':
            # Need delete permission
            return delete_resource(event, context)
        else:
            return create_response(405, {
                'error': 'Method not allowed',
                'message': f'Method {method} not supported'
            })
            
    except Exception as e:
        logger.error(f"Error in multi-method handler: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

def get_resource(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle GET request - read access."""
    return create_response(200, {'message': 'Resource retrieved'})

@require_permission(Permission.CREATE_LABELING_JOB, 'usecase_id')
def create_resource(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle POST request - create permission required."""
    return create_response(201, {'message': 'Resource created'})

@require_permission(Permission.UPDATE_USECASE, 'usecase_id')
def update_resource(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle PUT request - update permission required."""
    return create_response(200, {'message': 'Resource updated'})

@require_permission(Permission.DELETE_LABELING_JOB, 'usecase_id')
def delete_resource(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle DELETE request - delete permission required."""
    return create_response(200, {'message': 'Resource deleted'})