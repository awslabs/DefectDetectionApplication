#!/usr/bin/env python3
"""
Test script to verify training function generates correct S3 paths
"""
import sys
import os

# Add the shared utilities path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))

from shared_utils import create_s3_path_builder


def test_training_path_generation():
    """Test that training function would generate correct paths"""
    print("Testing training path generation...")
    
    # Simulate use case configuration
    usecase = {
        's3_bucket': 'manufacturing-line-1',
        's3_prefix': 'datasets'  # This is the problematic current prefix
    }
    
    # Test with current problematic prefix
    path_builder = create_s3_path_builder(
        bucket=usecase['s3_bucket'],
        prefix=usecase.get('s3_prefix', '')
    )
    
    training_job_name = "cookie-classification-20251211-042027"
    training_output_uri = path_builder.get_training_output_uri(training_job_name)
    
    print(f"Old problematic path would have been:")
    print(f"  s3://manufacturing-line-1/datasets//training-output/{training_job_name}/")
    print(f"")
    print(f"New organized path will be:")
    print(f"  {training_output_uri}")
    
    # Verify the new path structure
    expected = f"s3://manufacturing-line-1/datasets/models/training/{training_job_name}"
    assert training_output_uri == expected, f"Expected {expected}, got {training_output_uri}"
    
    print(f"✓ Path generation works correctly!")
    
    # Test with no prefix (ideal case)
    path_builder_clean = create_s3_path_builder(
        bucket=usecase['s3_bucket'],
        prefix=""
    )
    
    clean_uri = path_builder_clean.get_training_output_uri(training_job_name)
    expected_clean = f"s3://manufacturing-line-1/models/training/{training_job_name}"
    
    print(f"")
    print(f"With clean prefix (recommended):")
    print(f"  {clean_uri}")
    
    assert clean_uri == expected_clean, f"Expected {expected_clean}, got {clean_uri}"
    print(f"✓ Clean path generation works correctly!")


def test_path_comparison():
    """Compare old vs new path structures"""
    print("\nPath structure comparison:")
    print("=" * 60)
    
    job_name = "cookie-classification-compilation-clone-20251211-042027"
    
    # Old problematic structure
    old_path = f"s3://manufacturing-line-1/datasets//training-output/{job_name}/output/model.tar.gz"
    
    # New organized structure  
    path_builder = create_s3_path_builder("manufacturing-line-1", "")
    new_base = path_builder.get_training_output_uri(job_name)
    new_path = f"{new_base}/output/model.tar.gz"
    
    print(f"OLD: {old_path}")
    print(f"NEW: {new_path}")
    print("")
    print("Benefits of new structure:")
    print("  ✓ No double slashes")
    print("  ✓ Logical separation (models/ vs datasets/)")
    print("  ✓ Consistent naming (training/ not training-output/)")
    print("  ✓ Job-based organization")


if __name__ == "__main__":
    try:
        test_training_path_generation()
        test_path_comparison()
        print("\n🎉 All training path tests passed!")
    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        sys.exit(1)