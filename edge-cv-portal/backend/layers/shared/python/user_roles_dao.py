"""
Data Access Object (DAO) for UserRoles table operations.

This module provides functions for managing user-to-use-case assignments
and role mappings in DynamoDB.
"""

import logging
from typing import Dict, List, Optional, Set
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

class UserRolesDAO:
    """Data Access Object for UserRoles table operations."""
    
    def __init__(self, table_name: str):
        """Initialize DAO with DynamoDB table name."""
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(table_name)
    
    def assign_user_to_usecase(self, user_id: str, usecase_id: str, role: str, 
                              assigned_by: str) -> bool:
        """
        Assign a user to a use case with a specific role.
        
        Args:
            user_id: User identifier
            usecase_id: Use case identifier
            role: Role name (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer)
            assigned_by: User ID of the person making the assignment
            
        Returns:
            True if successful, False otherwise
        """
        try:
            timestamp = int(datetime.utcnow().timestamp())
            
            self.table.put_item(
                Item={
                    'user_id': user_id,
                    'usecase_id': usecase_id,
                    'role': role,
                    'assigned_at': timestamp,
                    'assigned_by': assigned_by,
                }
            )
            
            logger.info(f"Assigned user {user_id} to use case {usecase_id} with role {role}")
            return True
            
        except ClientError as e:
            logger.error(f"Error assigning user to use case: {str(e)}")
            return False
    
    def remove_user_from_usecase(self, user_id: str, usecase_id: str) -> bool:
        """
        Remove a user's assignment from a use case.
        
        Args:
            user_id: User identifier
            usecase_id: Use case identifier
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.table.delete_item(
                Key={
                    'user_id': user_id,
                    'usecase_id': usecase_id
                }
            )
            
            logger.info(f"Removed user {user_id} from use case {usecase_id}")
            return True
            
        except ClientError as e:
            logger.error(f"Error removing user from use case: {str(e)}")
            return False
    
    def get_user_usecases(self, user_id: str) -> List[Dict]:
        """
        Get all use cases assigned to a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            List of use case assignments with roles
        """
        try:
            response = self.table.query(
                KeyConditionExpression='user_id = :user_id',
                ExpressionAttributeValues={':user_id': user_id}
            )
            
            return response['Items']
            
        except ClientError as e:
            logger.error(f"Error querying user use cases: {str(e)}")
            return []
    
    def get_usecase_users(self, usecase_id: str) -> List[Dict]:
        """
        Get all users assigned to a use case.
        
        Args:
            usecase_id: Use case identifier
            
        Returns:
            List of user assignments with roles
        """
        try:
            response = self.table.query(
                IndexName='usecase-users-index',
                KeyConditionExpression='usecase_id = :usecase_id',
                ExpressionAttributeValues={':usecase_id': usecase_id}
            )
            
            return response['Items']
            
        except ClientError as e:
            logger.error(f"Error querying use case users: {str(e)}")
            return []
    
    def update_user_role(self, user_id: str, usecase_id: str, new_role: str, 
                        updated_by: str) -> bool:
        """
        Update a user's role for a specific use case.
        
        Args:
            user_id: User identifier
            usecase_id: Use case identifier
            new_role: New role name
            updated_by: User ID of the person making the update
            
        Returns:
            True if successful, False otherwise
        """
        try:
            timestamp = int(datetime.utcnow().timestamp())
            
            self.table.update_item(
                Key={
                    'user_id': user_id,
                    'usecase_id': usecase_id
                },
                UpdateExpression='SET #role = :role, assigned_at = :timestamp, assigned_by = :updated_by',
                ExpressionAttributeNames={
                    '#role': 'role'  # 'role' is a reserved word in DynamoDB
                },
                ExpressionAttributeValues={
                    ':role': new_role,
                    ':timestamp': timestamp,
                    ':updated_by': updated_by
                }
            )
            
            logger.info(f"Updated user {user_id} role to {new_role} for use case {usecase_id}")
            return True
            
        except ClientError as e:
            logger.error(f"Error updating user role: {str(e)}")
            return False
    
    def get_user_role_for_usecase(self, user_id: str, usecase_id: str) -> Optional[str]:
        """
        Get a user's role for a specific use case.
        
        Args:
            user_id: User identifier
            usecase_id: Use case identifier
            
        Returns:
            Role name if found, None otherwise
        """
        try:
            response = self.table.get_item(
                Key={
                    'user_id': user_id,
                    'usecase_id': usecase_id
                }
            )
            
            item = response.get('Item')
            return item['role'] if item else None
            
        except ClientError as e:
            logger.error(f"Error getting user role: {str(e)}")
            return None
    
    def bulk_assign_users(self, assignments: List[Dict]) -> Dict[str, List[str]]:
        """
        Bulk assign multiple users to use cases.
        
        Args:
            assignments: List of assignment dictionaries with keys:
                        user_id, usecase_id, role, assigned_by
                        
        Returns:
            Dictionary with 'success' and 'failed' lists of user_id:usecase_id
        """
        results = {'success': [], 'failed': []}
        
        for assignment in assignments:
            success = self.assign_user_to_usecase(
                assignment['user_id'],
                assignment['usecase_id'],
                assignment['role'],
                assignment['assigned_by']
            )
            
            key = f"{assignment['user_id']}:{assignment['usecase_id']}"
            if success:
                results['success'].append(key)
            else:
                results['failed'].append(key)
        
        return results
    
    def remove_all_user_assignments(self, user_id: str) -> bool:
        """
        Remove all use case assignments for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # First get all assignments for the user
            assignments = self.get_user_usecases(user_id)
            
            # Delete each assignment
            for assignment in assignments:
                self.table.delete_item(
                    Key={
                        'user_id': user_id,
                        'usecase_id': assignment['usecase_id']
                    }
                )
            
            logger.info(f"Removed all assignments for user {user_id}")
            return True
            
        except ClientError as e:
            logger.error(f"Error removing all user assignments: {str(e)}")
            return False
    
    def remove_all_usecase_assignments(self, usecase_id: str) -> bool:
        """
        Remove all user assignments for a use case.
        
        Args:
            usecase_id: Use case identifier
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # First get all assignments for the use case
            assignments = self.get_usecase_users(usecase_id)
            
            # Delete each assignment
            for assignment in assignments:
                self.table.delete_item(
                    Key={
                        'user_id': assignment['user_id'],
                        'usecase_id': usecase_id
                    }
                )
            
            logger.info(f"Removed all assignments for use case {usecase_id}")
            return True
            
        except ClientError as e:
            logger.error(f"Error removing all use case assignments: {str(e)}")
            return False
    
    def list_all_assignments(self, limit: Optional[int] = None) -> List[Dict]:
        """
        List all user-to-use-case assignments.
        
        Args:
            limit: Maximum number of items to return
            
        Returns:
            List of all assignments
        """
        try:
            scan_kwargs = {}
            if limit:
                scan_kwargs['Limit'] = limit
            
            response = self.table.scan(**scan_kwargs)
            return response['Items']
            
        except ClientError as e:
            logger.error(f"Error listing all assignments: {str(e)}")
            return []
    
    def get_users_by_role(self, role: str) -> List[Dict]:
        """
        Get all users with a specific role across all use cases.
        
        Args:
            role: Role name to filter by
            
        Returns:
            List of user assignments with the specified role
        """
        try:
            response = self.table.scan(
                FilterExpression='#role = :role',
                ExpressionAttributeNames={
                    '#role': 'role'
                },
                ExpressionAttributeValues={
                    ':role': role
                }
            )
            
            return response['Items']
            
        except ClientError as e:
            logger.error(f"Error getting users by role: {str(e)}")
            return []