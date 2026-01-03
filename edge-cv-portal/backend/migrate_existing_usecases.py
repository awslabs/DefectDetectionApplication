#!/usr/bin/env python3
"""
Migration script to assign existing use case owners to their use cases in the RBAC system.
This script should be run after deploying the RBAC system to ensure existing users can access their use cases.
"""

import boto3
import os
import sys
from datetime import datetime

def migrate_usecase_owners():
    """
    Migrate existing use case owners to have UseCaseAdmin role for their use cases
    """
    dynamodb = boto3.resource('dynamodb')
    
    # Table names - update these if your table names are different
    usecases_table_name = os.environ.get('USECASES_TABLE', 'dda-portal-usecases')
    user_roles_table_name = os.environ.get('USER_ROLES_TABLE', 'dda-portal-user-roles')
    
    try:
        usecases_table = dynamodb.Table(usecases_table_name)
        user_roles_table = dynamodb.Table(user_roles_table_name)
        
        print(f"Scanning use cases from table: {usecases_table_name}")
        
        # Scan all use cases
        response = usecases_table.scan()
        usecases = response.get('Items', [])
        
        print(f"Found {len(usecases)} use cases to migrate")
        
        migrated_count = 0
        
        for usecase in usecases:
            usecase_id = usecase.get('usecase_id')
            owner_email = usecase.get('owner')
            
            if not usecase_id or not owner_email:
                print(f"Skipping use case with missing ID or owner: {usecase}")
                continue
            
            # Check if role assignment already exists
            try:
                existing_role = user_roles_table.get_item(
                    Key={
                        'user_id': owner_email,
                        'usecase_id': usecase_id
                    }
                )
                
                if 'Item' in existing_role:
                    print(f"Role assignment already exists for {owner_email} -> {usecase_id}")
                    continue
                    
            except Exception as e:
                print(f"Error checking existing role for {owner_email} -> {usecase_id}: {e}")
                continue
            
            # Create role assignment
            timestamp = int(datetime.utcnow().timestamp() * 1000)
            
            role_item = {
                'user_id': owner_email,
                'usecase_id': usecase_id,
                'role': 'UseCaseAdmin',  # Give owners admin role for their use cases
                'assigned_by': 'migration_script',
                'assigned_at': timestamp,
                'created_at': timestamp,
                'updated_at': timestamp
            }
            
            try:
                user_roles_table.put_item(Item=role_item)
                print(f"✅ Assigned UseCaseAdmin role: {owner_email} -> {usecase_id} ({usecase.get('name', 'Unknown')})")
                migrated_count += 1
                
            except Exception as e:
                print(f"❌ Error assigning role for {owner_email} -> {usecase_id}: {e}")
        
        print(f"\n🎉 Migration completed! Assigned roles for {migrated_count} use cases.")
        
        if migrated_count > 0:
            print("\nUsers should now be able to see their use cases in the dropdown.")
            print("If you need to assign additional users to use cases, use the user management API.")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        sys.exit(1)


def assign_portal_admin(email):
    """
    Assign PortalAdmin role to a specific user (for global admin access)
    """
    dynamodb = boto3.resource('dynamodb')
    user_roles_table_name = os.environ.get('USER_ROLES_TABLE', 'dda-portal-user-roles')
    
    try:
        user_roles_table = dynamodb.Table(user_roles_table_name)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        role_item = {
            'user_id': email,
            'usecase_id': 'global',  # Special usecase_id for global roles
            'role': 'PortalAdmin',
            'assigned_by': 'migration_script',
            'assigned_at': timestamp,
            'created_at': timestamp,
            'updated_at': timestamp
        }
        
        user_roles_table.put_item(Item=role_item)
        print(f"✅ Assigned PortalAdmin role to: {email}")
        
    except Exception as e:
        print(f"❌ Error assigning PortalAdmin role to {email}: {e}")


if __name__ == "__main__":
    print("🚀 Starting RBAC migration for existing use cases...")
    
    # Check if we have AWS credentials
    try:
        boto3.client('sts').get_caller_identity()
    except Exception as e:
        print(f"❌ AWS credentials not configured: {e}")
        sys.exit(1)
    
    # Run the migration
    migrate_usecase_owners()
    
    # Optionally assign PortalAdmin role
    if len(sys.argv) > 1:
        admin_email = sys.argv[1]
        print(f"\n🔑 Assigning PortalAdmin role to: {admin_email}")
        assign_portal_admin(admin_email)
    else:
        print("\n💡 To assign PortalAdmin role to a user, run:")
        print("   python migrate_existing_usecases.py your-email@example.com")