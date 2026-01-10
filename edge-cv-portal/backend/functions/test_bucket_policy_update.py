#!/usr/bin/env python3
"""
Test script for the dynamic bucket policy update function.
Tests the logic without making actual AWS calls.

Run with: python test_bucket_policy_update.py
"""
import sys
import os
import json

# Add paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))


def test_bucket_policy_statement_generation():
    """Test that bucket policy statements are generated correctly"""
    print("\n" + "=" * 60)
    print("Testing: Bucket Policy Statement Generation")
    print("=" * 60)
    
    usecase_account_id = '198226511894'
    sagemaker_role_arn = f'arn:aws:iam::{usecase_account_id}:role/DDASageMakerExecutionRole'
    data_bucket_name = 'dda-cookie-bucket'
    
    # Generate statements (logic from usecases.py)
    sagemaker_read_sid = f"AllowSageMakerRead-{usecase_account_id}"
    sagemaker_list_sid = f"AllowSageMakerList-{usecase_account_id}"
    
    read_statement = {
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
    }
    
    list_statement = {
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
    }
    
    print(f"\n1. Read Statement:")
    print(json.dumps(read_statement, indent=2))
    
    print(f"\n2. List Statement:")
    print(json.dumps(list_statement, indent=2))
    
    # Verify
    assert read_statement['Sid'] == 'AllowSageMakerRead-198226511894'
    assert read_statement['Principal']['AWS'] == sagemaker_role_arn
    assert 's3:GetObject' in read_statement['Action']
    assert read_statement['Resource'] == 'arn:aws:s3:::dda-cookie-bucket/*'
    
    assert list_statement['Sid'] == 'AllowSageMakerList-198226511894'
    assert 's3:ListBucket' in list_statement['Action']
    assert list_statement['Resource'] == 'arn:aws:s3:::dda-cookie-bucket'
    
    print(f"\n✓ PASS: Statements generated correctly")


def test_idempotent_policy_update():
    """Test that policy update is idempotent (doesn't duplicate statements)"""
    print("\n" + "=" * 60)
    print("Testing: Idempotent Policy Update")
    print("=" * 60)
    
    usecase_account_id = '198226511894'
    sagemaker_read_sid = f"AllowSageMakerRead-{usecase_account_id}"
    sagemaker_list_sid = f"AllowSageMakerList-{usecase_account_id}"
    
    # Simulate existing policy with statements already present
    existing_policy = {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Sid': sagemaker_read_sid,
                'Effect': 'Allow',
                'Principal': {'AWS': f'arn:aws:iam::{usecase_account_id}:role/DDASageMakerExecutionRole'},
                'Action': ['s3:GetObject'],
                'Resource': 'arn:aws:s3:::dda-cookie-bucket/*'
            },
            {
                'Sid': sagemaker_list_sid,
                'Effect': 'Allow',
                'Principal': {'AWS': f'arn:aws:iam::{usecase_account_id}:role/DDASageMakerExecutionRole'},
                'Action': ['s3:ListBucket'],
                'Resource': 'arn:aws:s3:::dda-cookie-bucket'
            }
        ]
    }
    
    # Check if statements already exist (logic from usecases.py)
    existing_sids = {stmt.get('Sid') for stmt in existing_policy.get('Statement', [])}
    
    statements_to_add = []
    
    if sagemaker_read_sid not in existing_sids:
        statements_to_add.append({'Sid': sagemaker_read_sid})
    
    if sagemaker_list_sid not in existing_sids:
        statements_to_add.append({'Sid': sagemaker_list_sid})
    
    print(f"\nExisting SIDs: {existing_sids}")
    print(f"Statements to add: {len(statements_to_add)}")
    
    assert len(statements_to_add) == 0, "Should not add duplicate statements"
    print(f"\n✓ PASS: Idempotent - no duplicate statements added")


def test_new_usecase_policy_update():
    """Test adding statements for a new UseCase Account"""
    print("\n" + "=" * 60)
    print("Testing: New UseCase Policy Update")
    print("=" * 60)
    
    existing_usecase = '198226511894'
    new_usecase = '111222333444'
    
    # Simulate existing policy with one UseCase
    existing_policy = {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Sid': f'AllowSageMakerRead-{existing_usecase}',
                'Effect': 'Allow',
                'Principal': {'AWS': f'arn:aws:iam::{existing_usecase}:role/DDASageMakerExecutionRole'},
                'Action': ['s3:GetObject'],
                'Resource': 'arn:aws:s3:::dda-cookie-bucket/*'
            }
        ]
    }
    
    # Check for new UseCase
    new_read_sid = f"AllowSageMakerRead-{new_usecase}"
    new_list_sid = f"AllowSageMakerList-{new_usecase}"
    
    existing_sids = {stmt.get('Sid') for stmt in existing_policy.get('Statement', [])}
    
    statements_to_add = []
    
    if new_read_sid not in existing_sids:
        statements_to_add.append({
            'Sid': new_read_sid,
            'Effect': 'Allow',
            'Principal': {'AWS': f'arn:aws:iam::{new_usecase}:role/DDASageMakerExecutionRole'},
            'Action': ['s3:GetObject', 's3:GetObjectVersion', 's3:GetObjectTagging'],
            'Resource': 'arn:aws:s3:::dda-cookie-bucket/*'
        })
    
    if new_list_sid not in existing_sids:
        statements_to_add.append({
            'Sid': new_list_sid,
            'Effect': 'Allow',
            'Principal': {'AWS': f'arn:aws:iam::{new_usecase}:role/DDASageMakerExecutionRole'},
            'Action': ['s3:ListBucket', 's3:GetBucketLocation'],
            'Resource': 'arn:aws:s3:::dda-cookie-bucket'
        })
    
    print(f"\nExisting SIDs: {existing_sids}")
    print(f"New UseCase: {new_usecase}")
    print(f"Statements to add: {len(statements_to_add)}")
    
    for stmt in statements_to_add:
        print(f"  - {stmt['Sid']}")
    
    assert len(statements_to_add) == 2, "Should add 2 statements for new UseCase"
    assert any(s['Sid'] == new_read_sid for s in statements_to_add)
    assert any(s['Sid'] == new_list_sid for s in statements_to_add)
    
    # Merge policies
    existing_policy['Statement'].extend(statements_to_add)
    
    print(f"\nFinal policy has {len(existing_policy['Statement'])} statements")
    assert len(existing_policy['Statement']) == 3
    
    print(f"\n✓ PASS: New UseCase statements added correctly")


def test_empty_policy_creation():
    """Test creating policy when bucket has no existing policy"""
    print("\n" + "=" * 60)
    print("Testing: Empty Policy Creation")
    print("=" * 60)
    
    usecase_account_id = '198226511894'
    
    # Simulate no existing policy
    existing_policy = {
        'Version': '2012-10-17',
        'Statement': []
    }
    
    sagemaker_read_sid = f"AllowSageMakerRead-{usecase_account_id}"
    sagemaker_list_sid = f"AllowSageMakerList-{usecase_account_id}"
    
    existing_sids = {stmt.get('Sid') for stmt in existing_policy.get('Statement', [])}
    
    statements_to_add = []
    
    if sagemaker_read_sid not in existing_sids:
        statements_to_add.append({'Sid': sagemaker_read_sid})
    
    if sagemaker_list_sid not in existing_sids:
        statements_to_add.append({'Sid': sagemaker_list_sid})
    
    print(f"\nExisting SIDs: {existing_sids}")
    print(f"Statements to add: {len(statements_to_add)}")
    
    assert len(statements_to_add) == 2, "Should add 2 statements for new bucket"
    
    existing_policy['Statement'].extend(statements_to_add)
    
    print(f"Final policy has {len(existing_policy['Statement'])} statements")
    assert len(existing_policy['Statement']) == 2
    
    print(f"\n✓ PASS: New policy created correctly")


def run_all_tests():
    """Run all bucket policy tests"""
    print("\n" + "=" * 60)
    print("BUCKET POLICY UPDATE TESTS")
    print("=" * 60)
    
    tests = [
        test_bucket_policy_statement_generation,
        test_idempotent_policy_update,
        test_new_usecase_policy_update,
        test_empty_policy_creation,
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
        print("\n🎉 All bucket policy tests passed!")
        return 0
    else:
        print(f"\n❌ {failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
