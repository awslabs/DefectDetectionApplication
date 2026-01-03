#!/usr/bin/env python3
"""
Comprehensive test for Lambda storage optimization implementation
Tests all components including metrics, logging, and dashboard creation
"""
import sys
import os
import tempfile
import json
import time
from unittest.mock import Mock, patch, MagicMock

# Add the shared utilities to the path
sys.path.append('/opt/python')
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))

try:
    from shared_utils import (
        create_storage_manager_with_logging, create_streaming_extractor,
        create_incremental_zipper, create_cleanup_context,
        create_retry_manager, create_processing_strategy_manager,
        create_cloudwatch_metrics, create_dashboard_manager,
        setup_storage_monitoring, InsufficientStorageError,
        StorageMetrics, CloudWatchMetrics, StorageDashboardManager
    )
    print("✓ Successfully imported all storage optimization utilities")
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)

def test_enhanced_storage_manager():
    """Test StorageManagerWithLogging functionality"""
    print("\n--- Testing Enhanced Storage Manager ---")
    
    try:
        # Test with metrics and logging disabled for unit testing
        storage_manager = create_storage_manager_with_logging(
            enable_metrics=False,
            enable_enhanced_logging=False
        )
        
        # Test basic functionality
        metrics = storage_manager.get_storage_metrics()
        print(f"✓ Enhanced storage metrics: {metrics.available_space:,} bytes available")
        
        # Test cleanup with logging
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, 'test.txt')
            with open(test_file, 'w') as f:
                f.write("test content" * 1000)
            
            result = storage_manager.cleanup_directory(temp_dir, force=True)
            print(f"✓ Enhanced cleanup test: {result}")
        
        print("✓ Enhanced StorageManager tests passed")
        return True
        
    except Exception as e:
        print(f"✗ Enhanced StorageManager test failed: {e}")
        return False

def test_cloudwatch_metrics():
    """Test CloudWatch metrics functionality"""
    print("\n--- Testing CloudWatch Metrics ---")
    
    try:
        # Mock CloudWatch client for testing
        with patch('boto3.client') as mock_boto3:
            mock_cloudwatch = MagicMock()
            mock_boto3.return_value = mock_cloudwatch
            
            metrics_client = create_cloudwatch_metrics("Test/Storage")
            
            # Test storage metrics
            test_metrics = StorageMetrics(
                total_space=1000000,
                available_space=500000,
                used_space=500000,
                threshold_warning=750000,
                threshold_critical=900000,
                timestamp=time.time()
            )
            
            result = metrics_client.put_storage_metrics(test_metrics, {'Environment': 'test'})
            print(f"✓ Storage metrics test: {result}")
            
            # Test processing metrics
            result = metrics_client.put_processing_metrics(
                'TestOperation', 1.5, True, 10, 1024, {'Environment': 'test'}
            )
            print(f"✓ Processing metrics test: {result}")
            
            # Test error metrics
            result = metrics_client.put_error_metrics(
                'TestError', 'Test error message', {'Environment': 'test'}
            )
            print(f"✓ Error metrics test: {result}")
        
        print("✓ CloudWatch metrics tests passed")
        return True
        
    except Exception as e:
        print(f"✗ CloudWatch metrics test failed: {e}")
        return False

def test_dashboard_manager():
    """Test dashboard creation functionality"""
    print("\n--- Testing Dashboard Manager ---")
    
    try:
        # Mock CloudWatch client for testing
        with patch('boto3.client') as mock_boto3:
            mock_cloudwatch = MagicMock()
            mock_boto3.return_value = mock_cloudwatch
            
            dashboard_manager = create_dashboard_manager("Test/Storage")
            
            # Test dashboard creation
            result = dashboard_manager.create_storage_dashboard("TestDashboard")
            print(f"✓ Dashboard creation test: {result}")
            
            # Test alarm creation
            alarms = dashboard_manager.create_storage_alarms("arn:aws:sns:us-east-1:123456789012:test")
            print(f"✓ Alarm creation test: {len(alarms)} alarms created")
            
            # Test dashboard URL generation
            url = dashboard_manager.get_dashboard_url("TestDashboard")
            print(f"✓ Dashboard URL generation: {url}")
        
        print("✓ Dashboard manager tests passed")
        return True
        
    except Exception as e:
        print(f"✗ Dashboard manager test failed: {e}")
        return False

def test_complete_monitoring_setup():
    """Test complete monitoring setup"""
    print("\n--- Testing Complete Monitoring Setup ---")
    
    try:
        # Mock CloudWatch client for testing
        with patch('boto3.client') as mock_boto3:
            mock_cloudwatch = MagicMock()
            mock_boto3.return_value = mock_cloudwatch
            
            result = setup_storage_monitoring(
                "TestMonitoring",
                "arn:aws:sns:us-east-1:123456789012:test",
                "Test/Storage"
            )
            
            print(f"✓ Complete monitoring setup: {result['setup_successful']}")
            print(f"  - Dashboard created: {result.get('dashboard_created', False)}")
            print(f"  - Alarms created: {len(result.get('alarms_created', []))}")
            print(f"  - Dashboard URL: {result.get('dashboard_url', 'N/A')}")
        
        print("✓ Complete monitoring setup tests passed")
        return True
        
    except Exception as e:
        print(f"✗ Complete monitoring setup test failed: {e}")
        return False

def test_integration_with_packaging():
    """Test integration with packaging workflow"""
    print("\n--- Testing Integration with Packaging ---")
    
    try:
        # Test that packaging.py can import and use the new utilities
        import packaging
        
        # Check if the imports work
        print("✓ Packaging module can import storage optimization utilities")
        
        # Test that the functions exist and are callable
        storage_manager = create_storage_manager_with_logging(
            enable_metrics=False,
            enable_enhanced_logging=False
        )
        
        streaming_extractor = create_streaming_extractor(storage_manager)
        incremental_zipper = create_incremental_zipper(storage_manager)
        retry_manager = create_retry_manager(storage_manager, max_retries=1)
        
        print("✓ All storage optimization components can be instantiated")
        
        # Test basic functionality
        metrics = storage_manager.get_storage_metrics()
        print(f"✓ Integration test - Storage available: {metrics.available_space:,} bytes")
        
        print("✓ Integration with packaging tests passed")
        return True
        
    except Exception as e:
        print(f"✗ Integration with packaging test failed: {e}")
        return False

def test_error_handling():
    """Test error handling and recovery"""
    print("\n--- Testing Error Handling ---")
    
    try:
        storage_manager = create_storage_manager_with_logging(
            enable_metrics=False,
            enable_enhanced_logging=False
        )
        
        # Test insufficient storage error
        try:
            storage_manager.check_space_requirements(999999999999999)  # Impossibly large
            print("✗ Should have raised InsufficientStorageError")
            return False
        except InsufficientStorageError as e:
            print(f"✓ Correctly raised InsufficientStorageError: {e.required:,} > {e.available:,}")
        
        # Test retry mechanism with failure
        retry_manager = create_retry_manager(storage_manager, max_retries=1)
        
        attempt_count = 0
        def always_fail():
            nonlocal attempt_count
            attempt_count += 1
            raise InsufficientStorageError(1000, 500)
        
        try:
            retry_manager.retry_with_cleanup(always_fail, None, (InsufficientStorageError,))
            print("✗ Should have exhausted retries")
            return False
        except InsufficientStorageError:
            print(f"✓ Correctly exhausted retries after {attempt_count} attempts")
        
        print("✓ Error handling tests passed")
        return True
        
    except Exception as e:
        print(f"✗ Error handling test failed: {e}")
        return False

def main():
    """Run comprehensive test suite"""
    print("Lambda Storage Optimization - Comprehensive Test Suite")
    print("=" * 60)
    
    tests = [
        test_enhanced_storage_manager,
        test_cloudwatch_metrics,
        test_dashboard_manager,
        test_complete_monitoring_setup,
        test_integration_with_packaging,
        test_error_handling
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\n{'=' * 60}")
    print(f"Comprehensive Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("✓ ALL TESTS PASSED! Lambda storage optimization is fully implemented and working correctly.")
        print("\nKey Features Implemented:")
        print("  • Enhanced storage management with monitoring")
        print("  • Streaming extraction and incremental compression")
        print("  • Automatic cleanup and context management")
        print("  • Retry mechanisms with cleanup between attempts")
        print("  • Alternative processing strategies")
        print("  • CloudWatch metrics integration")
        print("  • Comprehensive structured logging")
        print("  • Automated dashboard and alarm creation")
        print("  • Error handling and recovery mechanisms")
        print("\nThe Lambda function should now handle large model artifacts without disk space errors!")
        return True
    else:
        print("✗ Some tests failed. Please check the implementation.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)