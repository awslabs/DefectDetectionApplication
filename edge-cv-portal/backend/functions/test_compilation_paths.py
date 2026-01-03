#!/usr/bin/env python3
"""
Test script to verify compilation function generates correct S3 paths
"""
import sys
import os

# Add the shared utilities path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))

from shared_utils import create_s3_path_builder


def test_compilation_path_generation():
    """Test that compilation function would generate correct paths"""
    print("Testing compilation path generation...")
    
    # Simulate use case configuration
    usecase = {
        's3_bucket': 'manufacturing-line-1',
        's3_prefix': 'datasets'  # Current problematic prefix
    }
    
    path_builder = create_s3_path_builder(
        bucket=usecase['s3_bucket'],
        prefix=usecase.get('s3_prefix', '')
    )
    
    compilation_job_name = "cookie-classification-jetson-xavier-20251211-042027"
    target = "jetson-xavier"
    
    compilation_output_uri = path_builder.get_compilation_output_uri(compilation_job_name, target)
    
    print(f"Old problematic path would have been:")
    print(f"  s3://manufacturing-line-1/datasets//compilation-output/")
    print(f"")
    print(f"New organized path will be:")
    print(f"  {compilation_output_uri}")
    
    # Verify the new path structure
    expected = f"s3://manufacturing-line-1/datasets/models/compilation/{compilation_job_name}/{target}"
    assert compilation_output_uri == expected, f"Expected {expected}, got {compilation_output_uri}"
    
    print(f"✓ Compilation path generation works correctly!")
    
    # Test with no prefix (ideal case)
    path_builder_clean = create_s3_path_builder(
        bucket=usecase['s3_bucket'],
        prefix=""
    )
    
    clean_uri = path_builder_clean.get_compilation_output_uri(compilation_job_name, target)
    expected_clean = f"s3://manufacturing-line-1/models/compilation/{compilation_job_name}/{target}"
    
    print(f"")
    print(f"With clean prefix (recommended):")
    print(f"  {clean_uri}")
    
    assert clean_uri == expected_clean, f"Expected {expected_clean}, got {clean_uri}"
    print(f"✓ Clean compilation path generation works correctly!")


def test_multi_target_compilation():
    """Test compilation paths for multiple targets"""
    print("\nTesting multi-target compilation paths...")
    
    path_builder = create_s3_path_builder("manufacturing-line-1", "")
    job_name = "my-model-20251211-120000"
    
    targets = ["jetson-xavier", "x86_64-cpu", "x86_64-cuda", "arm64-cpu"]
    
    print(f"Compilation job: {job_name}")
    print(f"Target-specific paths:")
    
    for target in targets:
        uri = path_builder.get_compilation_output_uri(job_name, target)
        expected = f"s3://manufacturing-line-1/models/compilation/{job_name}/{target}"
        assert uri == expected, f"Expected {expected}, got {uri}"
        print(f"  {target}: {uri}")
    
    print(f"✓ Multi-target compilation paths work correctly!")


def test_compilation_path_comparison():
    """Compare old vs new compilation path structures"""
    print("\nCompilation path structure comparison:")
    print("=" * 70)
    
    job_name = "cookie-classification-jetson-xavier-20251211-042027"
    target = "jetson-xavier"
    
    # Old problematic structure
    old_path = f"s3://manufacturing-line-1/datasets//compilation-output/{job_name}/output/model.tar.gz"
    
    # New organized structure  
    path_builder = create_s3_path_builder("manufacturing-line-1", "")
    new_base = path_builder.get_compilation_output_uri(job_name, target)
    new_path = f"{new_base}/output/model.tar.gz"
    
    print(f"OLD: {old_path}")
    print(f"NEW: {new_path}")
    print("")
    print("Benefits of new compilation structure:")
    print("  ✓ No double slashes")
    print("  ✓ Logical separation (models/compilation/ vs datasets/)")
    print("  ✓ Consistent naming (compilation/ not compilation-output/)")
    print("  ✓ Target-specific organization")
    print("  ✓ Job-based hierarchy")


if __name__ == "__main__":
    try:
        test_compilation_path_generation()
        test_multi_target_compilation()
        test_compilation_path_comparison()
        print("\n🎉 All compilation path tests passed!")
    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        sys.exit(1)