#!/usr/bin/env python3
"""
Test script to verify training progress tracking fix
"""
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock

# Add the functions directory to the path
sys.path.append(os.path.dirname(__file__))

def test_training_progress_update():
    """Test that training progress is updated correctly based on job status"""
    print("Testing training progress update fix...")
    
    # Mock the training_events module
    with patch('boto3.resource') as mock_dynamodb, \
         patch('boto3.client') as mock_boto3:
        
        # Mock DynamoDB table
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        
        # Mock scan response to find training job
        mock_table.scan.return_value = {
            'Items': [{
                'training_id': 'test-training-123',
                'training_job_name': 'test-job',
                'usecase_id': 'test-usecase',
                'model_name': 'test-model'
            }]
        }
        
        # Import after mocking
        from training_events import handle_training_state_change
        
        # Test completed training job
        completed_event = {
            'detail': {
                'TrainingJobName': 'test-job',
                'TrainingJobStatus': 'Completed',
                'TrainingJobArn': 'arn:aws:sagemaker:us-east-1:123456789012:training-job/test-job',
                'ModelArtifacts': {
                    'S3ModelArtifacts': 's3://bucket/model.tar.gz'
                }
            }
        }
        
        result = handle_training_state_change(completed_event, {})
        
        # Verify the update was called with correct progress
        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args
        
        # Check that progress was set to 100 for completed job
        expr_values = call_args[1]['ExpressionAttributeValues']
        assert expr_values[':progress'] == 100, f"Expected progress 100, got {expr_values[':progress']}"
        assert expr_values[':status'] == 'Completed', f"Expected status Completed, got {expr_values[':status']}"
        
        print("✓ Completed training job progress set to 100%")
        
        # Reset mock
        mock_table.reset_mock()
        
        # Test failed training job
        failed_event = {
            'detail': {
                'TrainingJobName': 'test-job',
                'TrainingJobStatus': 'Failed',
                'TrainingJobArn': 'arn:aws:sagemaker:us-east-1:123456789012:training-job/test-job',
                'FailureReason': 'Test failure'
            }
        }
        
        result = handle_training_state_change(failed_event, {})
        
        # Verify the update was called with correct progress for failed job
        call_args = mock_table.update_item.call_args
        expr_values = call_args[1]['ExpressionAttributeValues']
        assert expr_values[':progress'] == 0, f"Expected progress 0 for failed job, got {expr_values[':progress']}"
        assert expr_values[':status'] == 'Failed', f"Expected status Failed, got {expr_values[':status']}"
        
        print("✓ Failed training job progress set to 0%")
        
        # Reset mock
        mock_table.reset_mock()
        
        # Test in-progress training job
        inprogress_event = {
            'detail': {
                'TrainingJobName': 'test-job',
                'TrainingJobStatus': 'InProgress',
                'TrainingJobArn': 'arn:aws:sagemaker:us-east-1:123456789012:training-job/test-job'
            }
        }
        
        result = handle_training_state_change(inprogress_event, {})
        
        # Verify the update was called with correct progress for in-progress job
        call_args = mock_table.update_item.call_args
        expr_values = call_args[1]['ExpressionAttributeValues']
        assert expr_values[':progress'] == 50, f"Expected progress 50 for in-progress job, got {expr_values[':progress']}"
        assert expr_values[':status'] == 'InProgress', f"Expected status InProgress, got {expr_values[':status']}"
        
        print("✓ In-progress training job progress set to 50%")
        
        print("✓ All training progress update tests passed!")
        return True

def main():
    """Run the test"""
    print("Training Progress Fix - Test Suite")
    print("=" * 40)
    
    try:
        success = test_training_progress_update()
        
        if success:
            print("\n✓ Training progress tracking fix is working correctly!")
            print("  - Completed jobs will show 100% progress")
            print("  - Failed/Stopped jobs will show 0% progress") 
            print("  - In-progress jobs will show 50% progress")
            print("\nThe training job progress should now update properly when the job completes.")
            return True
        else:
            print("\n✗ Training progress fix test failed")
            return False
            
    except Exception as e:
        print(f"\n✗ Test failed with error: {str(e)}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)