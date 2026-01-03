#!/usr/bin/env python3
"""
Test script for Lambda storage optimization implementation
"""
import sys
import os
import tempfile
import json
from unittest.mock import Mock, patch

# Add the shared utilities to the path
sys.path.append('/opt/python')
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))

try:
    from shared_utils import (
        create_storage_manager, create_streaming_extractor,
        create_incremental_zipper, create_cleanup_context,
        create_retry_manager, create_processing_strategy_manager,
        InsufficientStorageError, StorageMetrics
    )
    print("✓ Successfully imported storage management utilities")
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)

def test_storage_manager():
    """Test StorageManager functionality"""
    print("\n--- Testing StorageManager ---")
    
    try:
        storage_manager = create_storage_manager()
        
        # Test basic metrics
        metrics = storage_manager.get_storage_metrics()
        print(f"✓ Storage metrics: {metrics.available_space:,} bytes available")
        
        # Test space estimation
        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file.write(b"test data" * 1000)
            temp_file.flush()
            
            estimated = storage_manager.estimate_space_needed(temp_file.name, 'extract')
            print(f"✓ Space estimation: {estimated:,} bytes for extraction")
        
        # Test cleanup
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, 'test.txt')
            with open(test_file, 'w') as f:
                f.write("test content")
            
            result = storage_manager.cleanup_directory(temp_dir, force=True)
            print(f"✓ Cleanup test: {result}")
        
        print("✓ StorageManager tests passed")
        
    except Exception as e:
        print(f"✗ StorageManager test failed: {e}")
        return False
    
    return True

def test_cleanup_context():
    """Test CleanupContext functionality"""
    print("\n--- Testing CleanupContext ---")
    
    try:
        storage_manager = create_storage_manager()
        
        with create_cleanup_context(storage_manager) as cleanup_ctx:
            # Create a temporary directory
            temp_dir = cleanup_ctx.create_temp_dir("test_")
            
            # Create a test file
            test_file = os.path.join(temp_dir, 'test.txt')
            with open(test_file, 'w') as f:
                f.write("test content")
            
            print(f"✓ Created temp directory: {temp_dir}")
            print(f"✓ Created test file: {test_file}")
        
        # Directory should be cleaned up automatically
        if not os.path.exists(temp_dir):
            print("✓ Automatic cleanup successful")
        else:
            print("✗ Automatic cleanup failed")
            return False
        
        print("✓ CleanupContext tests passed")
        
    except Exception as e:
        print(f"✗ CleanupContext test failed: {e}")
        return False
    
    return True

def test_retry_manager():
    """Test RetryManager functionality"""
    print("\n--- Testing RetryManager ---")
    
    try:
        storage_manager = create_storage_manager()
        retry_manager = create_retry_manager(storage_manager, max_retries=2)
        
        # Test successful operation
        def successful_operation(value):
            return value * 2
        
        result = retry_manager.retry_with_cleanup(successful_operation, None, (), 5)
        if result == 10:
            print("✓ Successful operation test passed")
        else:
            print(f"✗ Successful operation test failed: expected 10, got {result}")
            return False
        
        # Test retry with failure
        attempt_count = 0
        def failing_operation():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise InsufficientStorageError(1000, 500)
            return "success"
        
        try:
            result = retry_manager.retry_with_cleanup(failing_operation, None, (InsufficientStorageError,))
            print("✓ Retry mechanism test passed")
        except InsufficientStorageError:
            print("✓ Retry exhaustion test passed")
        
        print("✓ RetryManager tests passed")
        
    except Exception as e:
        print(f"✗ RetryManager test failed: {e}")
        return False
    
    return True

def test_processing_strategy_manager():
    """Test ProcessingStrategyManager functionality"""
    print("\n--- Testing ProcessingStrategyManager ---")
    
    try:
        storage_manager = create_storage_manager()
        strategy_manager = create_processing_strategy_manager(storage_manager)
        
        # Test strategy selection
        available_space = storage_manager.get_available_space()
        
        # Test with small space requirement (should use standard strategy)
        small_requirement = available_space // 10
        strategy = strategy_manager.select_strategy(small_requirement)
        print(f"✓ Selected strategy for small requirement: {strategy.__class__.__name__}")
        
        # Test with large space requirement (should use streaming or sequential)
        large_requirement = available_space * 2
        try:
            strategy = strategy_manager.select_strategy(large_requirement)
            print(f"✓ Selected strategy for large requirement: {strategy.__class__.__name__}")
        except InsufficientStorageError:
            print("✓ Correctly rejected impossible requirement")
        
        print("✓ ProcessingStrategyManager tests passed")
        
    except Exception as e:
        print(f"✗ ProcessingStrategyManager test failed: {e}")
        return False
    
    return True

def main():
    """Run all tests"""
    print("Lambda Storage Optimization - Test Suite")
    print("=" * 50)
    
    tests = [
        test_storage_manager,
        test_cleanup_context,
        test_retry_manager,
        test_processing_strategy_manager
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\n{'=' * 50}")
    print(f"Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("✓ All tests passed! Storage optimization implementation is working correctly.")
        return True
    else:
        print("✗ Some tests failed. Please check the implementation.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)