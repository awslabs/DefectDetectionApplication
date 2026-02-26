#!/usr/bin/env python3
"""
Script to update use case bucket and region in DynamoDB.
"""

import boto3
import sys
from datetime import datetime

def update_usecase(usecase_id: str, bucket: str, region: str):
    """
    Update use case bucket and region
    
    Args:
        usecase_id: The use case ID to update
        bucket: The S3 bucket name (e.g., 'dda-sleepme-useast1')
        region: The AWS region (e.g., 'us-east-1')
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
        print(f"\nCurrent use case configuration:")
        print(f"  Bucket: {usecase.get('bucket', 'NOT SET')}")
        print(f"  Region: {usecase.get('region', 'NOT SET')}")
        
        # Update the use case
        print(f"\nUpdating to:")
        print(f"  Bucket: {bucket}")
        print(f"  Region: {region}")
        
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression='SET bucket = :bucket, #region = :region, updated_at = :updated_at',
            ExpressionAttributeNames={
                '#region': 'region'  # 'region' is a reserved word in DynamoDB
            },
            ExpressionAttributeValues={
                ':bucket': bucket,
                ':region': region,
                ':updated_at': int(datetime.utcnow().timestamp() * 1000)
            }
        )
        
        print(f"\n✅ Successfully updated use case '{usecase_id}'")
        return True
        
    except Exception as e:
        print(f"❌ Error updating use case: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("Usage: python3 update_usecase_bucket.py <usecase_id> <bucket> <region>")
        print("Example: python3 update_usecase_bucket.py sleep-me dda-sleepme-useast1 us-east-1")
        sys.exit(1)
    
    usecase_id = sys.argv[1]
    bucket = sys.argv[2]
    region = sys.argv[3]
    
    success = update_usecase(usecase_id, bucket, region)
    
    if success:
        print("\n✅ Done! Create a new training job to use the updated configuration.")
        sys.exit(0)
    else:
        print("\n❌ Failed to update use case")
        sys.exit(1)
