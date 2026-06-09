#!/usr/bin/env python3
"""
Script to add region field to an existing use case in DynamoDB.
This fixes the region mismatch issue where SageMaker tries to access S3 buckets in the wrong region.
"""

import boto3
import sys
from datetime import datetime

def add_region_to_usecase(usecase_id: str, region: str):
    """
    Add region field to an existing use case
    
    Args:
        usecase_id: The use case ID to update
        region: The AWS region where the use case resources are located (e.g., 'us-east-2')
    """
    dynamodb = boto3.resource('dynamodb')
    table_name = 'dda-portal-usecases'
    
    try:
        table = dynamodb.Table(table_name)
        
        # Get current use case
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            print(f"❌ Use case '{usecase_id}' not found in table '{table_name}'")
            return False
        
        usecase = response['Item']
        current_region = usecase.get('region')
        
        if current_region:
            print(f"ℹ️  Use case '{usecase_id}' already has region: {current_region}")
            if current_region == region:
                print(f"✅ Region is already set to '{region}', no update needed")
                return True
            else:
                print(f"⚠️  Updating region from '{current_region}' to '{region}'")
        else:
            print(f"📝 Adding region '{region}' to use case '{usecase_id}'")
        
        # Update the use case with region
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression='SET #region = :region, updated_at = :updated_at',
            ExpressionAttributeNames={
                '#region': 'region'  # 'region' is a reserved word in DynamoDB
            },
            ExpressionAttributeValues={
                ':region': region,
                ':updated_at': int(datetime.utcnow().timestamp() * 1000)
            }
        )
        
        print(f"✅ Successfully updated use case '{usecase_id}' with region '{region}'")
        return True
        
    except Exception as e:
        print(f"❌ Error updating use case: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python3 add_region_to_usecase.py <usecase_id> <region>")
        print("Example: python3 add_region_to_usecase.py sleep-me us-east-2")
        sys.exit(1)
    
    usecase_id = sys.argv[1]
    region = sys.argv[2]
    
    print(f"Adding region '{region}' to use case '{usecase_id}'...")
    success = add_region_to_usecase(usecase_id, region)
    
    if success:
        print("\n✅ Done! You can now create training jobs in the correct region.")
        sys.exit(0)
    else:
        print("\n❌ Failed to update use case")
        sys.exit(1)
