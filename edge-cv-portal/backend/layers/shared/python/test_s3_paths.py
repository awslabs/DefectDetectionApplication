#!/usr/bin/env python3
"""
Simple test script for S3 path management utilities
"""
import sys
import os

# Add the current directory to Python path for imports
sys.path.insert(0, os.path.dirname(__file__))

from shared_utils import S3PathBuilder, PathResolver, create_s3_path_builder, create_path_resolver


def test_s3_path_builder():
    """Test S3PathBuilder functionality"""
    print("Testing S3PathBuilder...")
    
    # Test basic path building
    builder = S3PathBuilder("test-bucket", "test-prefix")
    
    # Test training paths
    training_path = builder.training_output_path("my-training-job-123")
    expected = "test-prefix/models/training/my-training-job-123"
    assert training_path == expected, f"Expected {expected}, got {training_path}"
    print(f"✓ Training path: {training_path}")
    
    # Test compilation paths
    compilation_path = builder.compilation_output_path("my-compilation-job", "jetson-xavier")
    expected = "test-prefix/models/compilation/my-compilation-job/jetson-xavier"
    assert compilation_path == expected, f"Expected {expected}, got {compilation_path}"
    print(f"✓ Compilation path: {compilation_path}")
    
    # Test dataset paths
    dataset_path = builder.dataset_path("raw")
    expected = "test-prefix/datasets/raw"
    assert dataset_path == expected, f"Expected {expected}, got {dataset_path}"
    print(f"✓ Dataset path: {dataset_path}")
    
    # Test deployment paths
    deployment_path = builder.deployment_path("my-deployment")
    expected = "test-prefix/deployments/my-deployment"
    assert deployment_path == expected, f"Expected {expected}, got {deployment_path}"
    print(f"✓ Deployment path: {deployment_path}")
    
    # Test S3 URI generation
    uri = builder.get_training_output_uri("test-job")
    expected = "s3://test-bucket/test-prefix/models/training/test-job"
    assert uri == expected, f"Expected {expected}, got {uri}"
    print(f"✓ S3 URI: {uri}")


def test_path_validation():
    """Test path validation"""
    print("\nTesting path validation...")
    
    builder = S3PathBuilder("test-bucket")
    
    # Test empty job name
    try:
        builder.training_output_path("")
        assert False, "Should have raised ValueError for empty job name"
    except ValueError:
        print("✓ Empty job name validation works")
    
    # Test job name sanitization
    path = builder.training_output_path("job with spaces & special chars!")
    expected = "models/training/job-with-spaces---special-chars-"
    assert path == expected, f"Expected {expected}, got {path}"
    print(f"✓ Job name sanitization: {path}")


def test_path_resolver():
    """Test PathResolver functionality"""
    print("\nTesting PathResolver...")
    
    builder = S3PathBuilder("test-bucket", "test-prefix")
    resolver = PathResolver(builder)
    
    # Test legacy path detection
    legacy_paths = [
        "s3://test-bucket/datasets//training-output/job-123/output/model.tar.gz",
        "s3://test-bucket/datasets/prefix/compilation-output/job-456/output/model.tar.gz",
        "s3://test-bucket/training-output/job-789/model.tar.gz"
    ]
    
    for path in legacy_paths:
        assert resolver.is_legacy_path(path), f"Should detect {path} as legacy"
        print(f"✓ Detected legacy path: {path}")
    
    # Test new path (should not be legacy)
    new_path = "s3://test-bucket/test-prefix/models/training/job-123/model.tar.gz"
    assert not resolver.is_legacy_path(new_path), f"Should not detect {new_path} as legacy"
    print(f"✓ New path correctly identified: {new_path}")
    
    # Test legacy path conversion
    legacy = "s3://test-bucket/datasets/prefix/training-output/my-job/output/model.tar.gz"
    converted = resolver.convert_legacy_path(legacy)
    expected = "s3://test-bucket/test-prefix/models/training/my-job"
    assert converted == expected, f"Expected {expected}, got {converted}"
    print(f"✓ Legacy path conversion: {legacy} -> {converted}")


def test_no_prefix():
    """Test path building without prefix"""
    print("\nTesting without prefix...")
    
    builder = S3PathBuilder("test-bucket")
    
    training_path = builder.training_output_path("job-123")
    expected = "models/training/job-123"
    assert training_path == expected, f"Expected {expected}, got {training_path}"
    print(f"✓ No prefix training path: {training_path}")
    
    uri = builder.get_training_output_uri("job-123")
    expected = "s3://test-bucket/models/training/job-123"
    assert uri == expected, f"Expected {expected}, got {uri}"
    print(f"✓ No prefix URI: {uri}")


def test_factory_functions():
    """Test factory functions"""
    print("\nTesting factory functions...")
    
    builder = create_s3_path_builder("factory-bucket", "factory-prefix")
    path = builder.training_output_path("factory-job")
    expected = "factory-prefix/models/training/factory-job"
    assert path == expected, f"Expected {expected}, got {path}"
    print(f"✓ Factory S3PathBuilder: {path}")
    
    resolver = create_path_resolver("factory-bucket", "factory-prefix")
    legacy = "s3://factory-bucket/datasets/training-output/factory-job/model.tar.gz"
    converted = resolver.convert_legacy_path(legacy)
    expected = "s3://factory-bucket/factory-prefix/models/training/factory-job"
    assert converted == expected, f"Expected {expected}, got {converted}"
    print(f"✓ Factory PathResolver: {converted}")


if __name__ == "__main__":
    try:
        test_s3_path_builder()
        test_path_validation()
        test_path_resolver()
        test_no_prefix()
        test_factory_functions()
        print("\n🎉 All tests passed!")
    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        sys.exit(1)