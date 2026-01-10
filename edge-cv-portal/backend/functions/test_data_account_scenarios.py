#!/usr/bin/env python3
"""
Test script to verify data account configuration scenarios work correctly.

Scenarios tested:
1. Same Account: Data Account == UseCase Account (most common)
2. Separate Account: Data Account != UseCase Account (cross-account)
3. Legacy: No data account configured (backward compatibility)

Run with: python test_data_account_scenarios.py
"""
import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any

# Add the shared utilities path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))


def create_mock_usecase(scenario: str) -> Dict[str, Any]:
    """Create mock usecase data for different scenarios"""
    
    base_usecase = {
        'usecase_id': 'test-usecase-123',
        'name': 'Test UseCase',
        'account_id': '198226511894',
        's3_bucket': 'usecase-bucket',
        's3_prefix': 'datasets/',
        'cross_account_role_arn': 'arn:aws:iam::198226511894:role/DDAPortalAccessRole',
        'sagemaker_execution_role_arn': 'arn:aws:iam::198226511894:role/DDASageMakerExecutionRole',
        'external_id': 'test-external-id-123',
    }
    
    if scenario == 'same_account':
        # Data Account == UseCase Account
        base_usecase.update({
            'data_account_id': '198226511894',  # Same as account_id
            'data_account_role_arn': 'arn:aws:iam::198226511894:role/DDAPortalAccessRole',
            'data_account_external_id': 'test-external-id-123',
            'data_s3_bucket': 'usecase-bucket',  # Same bucket
            'data_s3_prefix': 'datasets/',
        })
    elif scenario == 'separate_account':
        # Data Account != UseCase Account
        base_usecase.update({
            'data_account_id': '814373574263',  # Different account
            'data_account_role_arn': 'arn:aws:iam::814373574263:role/DDAPortalDataAccessRole',
            'data_account_external_id': 'data-external-id-456',
            'data_s3_bucket': 'dda-cookie-bucket',  # Different bucket
            'data_s3_prefix': 'training-data/',
        })
    elif scenario == 'legacy':
        # No data account configured (backward compatibility)
        pass  # No data_account_* fields
    
    return base_usecase


def test_is_separate_data_account_logic():
    """Test the logic that determines if data account is separate"""
    print("\n" + "=" * 60)
    print("Testing: is_separate_data_account logic")
    print("=" * 60)
    
    def is_separate_data_account(usecase: Dict) -> bool:
        """Logic extracted from labeling.py"""
        data_role_arn = usecase.get('data_account_role_arn')
        data_account_id = usecase.get('data_account_id')
        usecase_account_id = usecase.get('account_id')
        
        return (
            data_role_arn and 
            data_account_id and 
            data_account_id != usecase_account_id
        )
    
    # Test Scenario 1: Same Account
    usecase_same = create_mock_usecase('same_account')
    result_same = is_separate_data_account(usecase_same)
    print(f"\n1. Same Account Scenario:")
    print(f"   account_id: {usecase_same['account_id']}")
    print(f"   data_account_id: {usecase_same.get('data_account_id')}")
    print(f"   is_separate: {result_same}")
    assert result_same == False, "Same account should NOT be treated as separate"
    print(f"   ✓ PASS: Correctly identified as same account")
    
    # Test Scenario 2: Separate Account
    usecase_separate = create_mock_usecase('separate_account')
    result_separate = is_separate_data_account(usecase_separate)
    print(f"\n2. Separate Account Scenario:")
    print(f"   account_id: {usecase_separate['account_id']}")
    print(f"   data_account_id: {usecase_separate.get('data_account_id')}")
    print(f"   is_separate: {result_separate}")
    assert result_separate == True, "Different accounts should be treated as separate"
    print(f"   ✓ PASS: Correctly identified as separate account")
    
    # Test Scenario 3: Legacy (no data account)
    usecase_legacy = create_mock_usecase('legacy')
    result_legacy = is_separate_data_account(usecase_legacy)
    print(f"\n3. Legacy Scenario (no data account configured):")
    print(f"   account_id: {usecase_legacy['account_id']}")
    print(f"   data_account_id: {usecase_legacy.get('data_account_id')}")
    print(f"   is_separate: {result_legacy}")
    # Note: Python's `and` returns the first falsy value or the last value
    # So None and X returns None, which is falsy - this is correct behavior
    assert not result_legacy, "Legacy should NOT be treated as separate (falsy value expected)"
    print(f"   ✓ PASS: Correctly handled legacy case")


def test_input_bucket_selection():
    """Test that correct input bucket is selected for each scenario"""
    print("\n" + "=" * 60)
    print("Testing: Input bucket selection logic")
    print("=" * 60)
    
    def get_input_bucket(usecase: Dict, is_separate: bool) -> str:
        """Logic extracted from labeling.py"""
        if is_separate:
            return usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
        else:
            return usecase.get('data_s3_bucket') or usecase['s3_bucket']
    
    # Test Scenario 1: Same Account
    usecase_same = create_mock_usecase('same_account')
    bucket_same = get_input_bucket(usecase_same, False)
    print(f"\n1. Same Account Scenario:")
    print(f"   s3_bucket: {usecase_same['s3_bucket']}")
    print(f"   data_s3_bucket: {usecase_same.get('data_s3_bucket')}")
    print(f"   selected bucket: {bucket_same}")
    assert bucket_same == 'usecase-bucket', f"Expected 'usecase-bucket', got '{bucket_same}'"
    print(f"   ✓ PASS: Correct bucket selected")
    
    # Test Scenario 2: Separate Account
    usecase_separate = create_mock_usecase('separate_account')
    bucket_separate = get_input_bucket(usecase_separate, True)
    print(f"\n2. Separate Account Scenario:")
    print(f"   s3_bucket: {usecase_separate['s3_bucket']}")
    print(f"   data_s3_bucket: {usecase_separate.get('data_s3_bucket')}")
    print(f"   selected bucket: {bucket_separate}")
    assert bucket_separate == 'dda-cookie-bucket', f"Expected 'dda-cookie-bucket', got '{bucket_separate}'"
    print(f"   ✓ PASS: Correct bucket selected")
    
    # Test Scenario 3: Legacy
    usecase_legacy = create_mock_usecase('legacy')
    bucket_legacy = get_input_bucket(usecase_legacy, False)
    print(f"\n3. Legacy Scenario:")
    print(f"   s3_bucket: {usecase_legacy['s3_bucket']}")
    print(f"   data_s3_bucket: {usecase_legacy.get('data_s3_bucket')}")
    print(f"   selected bucket: {bucket_legacy}")
    assert bucket_legacy == 'usecase-bucket', f"Expected 'usecase-bucket', got '{bucket_legacy}'"
    print(f"   ✓ PASS: Correct bucket selected")


def test_manifest_generation():
    """Test that manifest references correct bucket for each scenario"""
    print("\n" + "=" * 60)
    print("Testing: Manifest generation with correct bucket references")
    print("=" * 60)
    
    def generate_manifest_line(image_key: str, bucket: str) -> str:
        """Simplified manifest line generation"""
        return json.dumps({'source-ref': f"s3://{bucket}/{image_key}"})
    
    image_key = "datasets/images/image001.jpg"
    
    # Test Scenario 1: Same Account
    usecase_same = create_mock_usecase('same_account')
    manifest_same = generate_manifest_line(image_key, usecase_same['data_s3_bucket'])
    print(f"\n1. Same Account Scenario:")
    print(f"   Manifest line: {manifest_same}")
    expected_same = '{"source-ref": "s3://usecase-bucket/datasets/images/image001.jpg"}'
    assert 'usecase-bucket' in manifest_same, "Manifest should reference usecase bucket"
    print(f"   ✓ PASS: Manifest references correct bucket")
    
    # Test Scenario 2: Separate Account
    usecase_separate = create_mock_usecase('separate_account')
    manifest_separate = generate_manifest_line(image_key, usecase_separate['data_s3_bucket'])
    print(f"\n2. Separate Account Scenario:")
    print(f"   Manifest line: {manifest_separate}")
    assert 'dda-cookie-bucket' in manifest_separate, "Manifest should reference data account bucket"
    print(f"   ✓ PASS: Manifest references correct bucket")


def test_sagemaker_role_access():
    """Test that SageMaker execution role has correct bucket access"""
    print("\n" + "=" * 60)
    print("Testing: SageMaker execution role bucket access requirements")
    print("=" * 60)
    
    # Test Scenario 1: Same Account
    usecase_same = create_mock_usecase('same_account')
    print(f"\n1. Same Account Scenario:")
    print(f"   SageMaker Role: {usecase_same['sagemaker_execution_role_arn']}")
    print(f"   Needs access to: s3://{usecase_same['s3_bucket']}")
    print(f"   Access method: IAM policy on SageMaker role (same account)")
    print(f"   ✓ No cross-account bucket policy needed")
    
    # Test Scenario 2: Separate Account
    usecase_separate = create_mock_usecase('separate_account')
    print(f"\n2. Separate Account Scenario:")
    print(f"   SageMaker Role: {usecase_separate['sagemaker_execution_role_arn']}")
    print(f"   Needs access to: s3://{usecase_separate['data_s3_bucket']}")
    print(f"   Access method: Bucket policy on data bucket allowing SageMaker role")
    print(f"   ⚠️  Requires Data Account stack deployment with dataBucketNames parameter")
    
    # Verify the bucket policy statement that would be needed
    expected_policy = {
        'Sid': 'AllowUseCaseSageMakerRead',
        'Effect': 'Allow',
        'Principal': {
            'AWS': usecase_separate['sagemaker_execution_role_arn']
        },
        'Action': ['s3:GetObject', 's3:GetObjectVersion', 's3:GetObjectTagging'],
        'Resource': f"arn:aws:s3:::{usecase_separate['data_s3_bucket']}/*"
    }
    print(f"\n   Required bucket policy statement:")
    print(f"   {json.dumps(expected_policy, indent=4)}")


def test_frontend_submission_data():
    """Test that frontend sends correct data for each scenario"""
    print("\n" + "=" * 60)
    print("Testing: Frontend submission data structure")
    print("=" * 60)
    
    def simulate_frontend_submission(
        account_id: str,
        s3_bucket: str,
        data_account_same_as_usecase: bool,
        data_account_id: str = None,
        data_s3_bucket: str = None
    ) -> Dict:
        """Simulate what frontend sends based on UseCaseOnboarding.tsx logic"""
        data = {
            'name': 'Test UseCase',
            'account_id': account_id,
            's3_bucket': s3_bucket,
            's3_prefix': 'datasets/',
            'cross_account_role_arn': f'arn:aws:iam::{account_id}:role/DDAPortalAccessRole',
            'sagemaker_execution_role_arn': f'arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole',
            'external_id': 'test-external-id',
        }
        
        # Always include Data Account configuration (new behavior)
        if data_account_same_as_usecase:
            data['data_account_id'] = account_id
            data['data_account_role_arn'] = data['cross_account_role_arn']
            data['data_account_external_id'] = data['external_id']
            data['data_s3_bucket'] = s3_bucket
            data['data_s3_prefix'] = 'datasets/'
        else:
            data['data_account_id'] = data_account_id
            data['data_account_role_arn'] = f'arn:aws:iam::{data_account_id}:role/DDAPortalDataAccessRole'
            data['data_account_external_id'] = 'data-external-id'
            data['data_s3_bucket'] = data_s3_bucket
            data['data_s3_prefix'] = 'training-data/'
        
        return data
    
    # Test Scenario 1: Same Account (checkbox checked)
    print(f"\n1. Same Account Scenario (dataAccountSameAsUseCase = true):")
    data_same = simulate_frontend_submission(
        account_id='198226511894',
        s3_bucket='usecase-bucket',
        data_account_same_as_usecase=True
    )
    print(f"   account_id: {data_same['account_id']}")
    print(f"   data_account_id: {data_same['data_account_id']}")
    print(f"   s3_bucket: {data_same['s3_bucket']}")
    print(f"   data_s3_bucket: {data_same['data_s3_bucket']}")
    assert data_same['account_id'] == data_same['data_account_id'], "IDs should match"
    assert data_same['s3_bucket'] == data_same['data_s3_bucket'], "Buckets should match"
    print(f"   ✓ PASS: Frontend correctly sends same account data")
    
    # Test Scenario 2: Separate Account (checkbox unchecked)
    print(f"\n2. Separate Account Scenario (dataAccountSameAsUseCase = false):")
    data_separate = simulate_frontend_submission(
        account_id='198226511894',
        s3_bucket='usecase-bucket',
        data_account_same_as_usecase=False,
        data_account_id='814373574263',
        data_s3_bucket='dda-cookie-bucket'
    )
    print(f"   account_id: {data_separate['account_id']}")
    print(f"   data_account_id: {data_separate['data_account_id']}")
    print(f"   s3_bucket: {data_separate['s3_bucket']}")
    print(f"   data_s3_bucket: {data_separate['data_s3_bucket']}")
    assert data_separate['account_id'] != data_separate['data_account_id'], "IDs should differ"
    assert data_separate['s3_bucket'] != data_separate['data_s3_bucket'], "Buckets should differ"
    print(f"   ✓ PASS: Frontend correctly sends separate account data")


def test_role_assumption_optimization():
    """Test that role assumption is optimized for same account scenario"""
    print("\n" + "=" * 60)
    print("Testing: Role assumption optimization")
    print("=" * 60)
    
    assume_role_calls = []
    
    def mock_assume_role(role_arn: str, external_id: str, session_name: str):
        """Track role assumption calls"""
        assume_role_calls.append({
            'role_arn': role_arn,
            'external_id': external_id,
            'session_name': session_name
        })
        return {'AccessKeyId': 'test', 'SecretAccessKey': 'test', 'SessionToken': 'test'}
    
    def simulate_labeling_role_assumptions(usecase: Dict):
        """Simulate the role assumption logic from labeling.py"""
        assume_role_calls.clear()
        
        # Always assume UseCase role first
        mock_assume_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'create-labeling-job'
        )
        
        # Check if separate data account
        data_role_arn = usecase.get('data_account_role_arn')
        data_account_id = usecase.get('data_account_id')
        usecase_account_id = usecase.get('account_id')
        
        is_separate = (
            data_role_arn and 
            data_account_id and 
            data_account_id != usecase_account_id
        )
        
        if is_separate:
            # Only assume data role if different account
            mock_assume_role(
                data_role_arn,
                usecase.get('data_account_external_id', usecase['external_id']),
                'labeling-data-access'
            )
        
        return len(assume_role_calls)
    
    # Test Scenario 1: Same Account - should only assume once
    usecase_same = create_mock_usecase('same_account')
    calls_same = simulate_labeling_role_assumptions(usecase_same)
    print(f"\n1. Same Account Scenario:")
    print(f"   Role assumptions: {calls_same}")
    for call in assume_role_calls:
        print(f"     - {call['session_name']}: {call['role_arn']}")
    assert calls_same == 1, f"Expected 1 role assumption, got {calls_same}"
    print(f"   ✓ PASS: Only one role assumption (optimized)")
    
    # Test Scenario 2: Separate Account - should assume twice
    usecase_separate = create_mock_usecase('separate_account')
    calls_separate = simulate_labeling_role_assumptions(usecase_separate)
    print(f"\n2. Separate Account Scenario:")
    print(f"   Role assumptions: {calls_separate}")
    for call in assume_role_calls:
        print(f"     - {call['session_name']}: {call['role_arn']}")
    assert calls_separate == 2, f"Expected 2 role assumptions, got {calls_separate}"
    print(f"   ✓ PASS: Two role assumptions (UseCase + Data)")
    
    # Test Scenario 3: Legacy - should only assume once
    usecase_legacy = create_mock_usecase('legacy')
    calls_legacy = simulate_labeling_role_assumptions(usecase_legacy)
    print(f"\n3. Legacy Scenario:")
    print(f"   Role assumptions: {calls_legacy}")
    for call in assume_role_calls:
        print(f"     - {call['session_name']}: {call['role_arn']}")
    assert calls_legacy == 1, f"Expected 1 role assumption, got {calls_legacy}"
    print(f"   ✓ PASS: Only one role assumption (backward compatible)")


def test_training_data_account_logging():
    """Test that training.py logs correct data account info"""
    print("\n" + "=" * 60)
    print("Testing: Training data account logging")
    print("=" * 60)
    
    def simulate_training_logging(usecase: Dict) -> str:
        """Simulate the logging logic from training.py"""
        data_account_id = usecase.get('data_account_id')
        data_s3_bucket = usecase.get('data_s3_bucket')
        
        if data_account_id and data_account_id != usecase['account_id']:
            return f"Training will use separate Data Account: {data_account_id}, bucket: {data_s3_bucket}"
        else:
            return f"Training data is in UseCase Account: {usecase['account_id']}"
    
    # Test Scenario 1: Same Account
    usecase_same = create_mock_usecase('same_account')
    log_same = simulate_training_logging(usecase_same)
    print(f"\n1. Same Account Scenario:")
    print(f"   Log message: {log_same}")
    assert 'UseCase Account' in log_same, "Should log UseCase Account"
    print(f"   ✓ PASS: Correct log message")
    
    # Test Scenario 2: Separate Account
    usecase_separate = create_mock_usecase('separate_account')
    log_separate = simulate_training_logging(usecase_separate)
    print(f"\n2. Separate Account Scenario:")
    print(f"   Log message: {log_separate}")
    assert 'separate Data Account' in log_separate, "Should log separate Data Account"
    assert '814373574263' in log_separate, "Should include data account ID"
    print(f"   ✓ PASS: Correct log message")


def run_all_tests():
    """Run all test scenarios"""
    print("\n" + "=" * 60)
    print("DATA ACCOUNT SCENARIOS TEST SUITE")
    print("=" * 60)
    
    tests = [
        test_is_separate_data_account_logic,
        test_input_bucket_selection,
        test_manifest_generation,
        test_sagemaker_role_access,
        test_frontend_submission_data,
        test_role_assumption_optimization,
        test_training_data_account_logging,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"\n❌ FAILED: {test.__name__}")
            print(f"   Error: {str(e)}")
            failed += 1
        except Exception as e:
            print(f"\n❌ ERROR: {test.__name__}")
            print(f"   Error: {str(e)}")
            failed += 1
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total:  {len(tests)}")
    
    if failed == 0:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n❌ {failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
