#!/usr/bin/env python3
"""
Test script for Greengrass Component Discovery functionality
"""
import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Add the shared utilities to the path
sys.path.append('/opt/python')
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'layers', 'shared', 'python'))

try:
    from shared_utils import (
        ComponentDiscoveryService, ComponentRegistry, ComponentMetadata,
        ComponentType, ComponentStatus, ComponentFilter,
        create_component_discovery_service, create_component_registry
    )
    print("✓ Successfully imported component discovery utilities")
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)

def test_component_metadata():
    """Test ComponentMetadata data class"""
    print("\n--- Testing ComponentMetadata ---")
    
    try:
        # Create test component metadata
        metadata = ComponentMetadata(
            component_name="test-component",
            component_arn="arn:aws:greengrass:us-east-1:123456789012:components:test-component:versions:1.0.0",
            version="1.0.0",
            description="Test component for unit testing",
            publisher="Test Publisher",
            creation_date=datetime.utcnow(),
            component_type=ComponentType.UTILITY,
            platform_compatibility=["linux-x86_64"],
            resource_requirements={"memory": 128, "cpu": 0.1},
            dependencies=["aws.greengrass.Nucleus"],
            tags={"Environment": "test"},
            status=ComponentStatus.ACTIVE
        )
        
        # Test serialization
        metadata_dict = metadata.to_dict()
        assert metadata_dict['component_name'] == "test-component"
        assert metadata_dict['component_type'] == "utility"
        assert metadata_dict['status'] == "active"
        print("✓ Component metadata serialization works")
        
        # Test deserialization
        restored_metadata = ComponentMetadata.from_dict(metadata_dict)
        assert restored_metadata.component_name == metadata.component_name
        assert restored_metadata.component_type == metadata.component_type
        print("✓ Component metadata deserialization works")
        
        print("✓ ComponentMetadata tests passed")
        return True
        
    except Exception as e:
        print(f"✗ ComponentMetadata test failed: {e}")
        return False

def test_component_discovery_service():
    """Test ComponentDiscoveryService functionality"""
    print("\n--- Testing ComponentDiscoveryService ---")
    
    try:
        # Mock AWS clients
        with patch('boto3.client') as mock_boto3:
            mock_greengrass = MagicMock()
            mock_sts = MagicMock()
            
            # Configure mock responses
            mock_boto3.side_effect = lambda service, **kwargs: {
                'greengrassv2': mock_greengrass,
                'sts': mock_sts
            }[service]
            
            mock_sts.get_caller_identity.return_value = {'Account': '123456789012'}
            
            # Mock component list response
            mock_greengrass.get_paginator.return_value.paginate.return_value = [
                {
                    'components': [
                        {
                            'componentName': 'test-component',
                            'latestVersion': {'componentVersion': '1.0.0'}
                        }
                    ]
                }
            ]
            
            # Mock component details response
            mock_greengrass.describe_component.return_value = {
                'arn': 'arn:aws:greengrass:us-east-1:123456789012:components:test-component:versions:1.0.0',
                'description': 'Test component',
                'publisher': 'Test Publisher',
                'creationTimestamp': datetime.utcnow(),
                'tags': {'Environment': 'test'},
                'recipe': json.dumps({
                    'RecipeFormatVersion': '2020-01-25',
                    'ComponentName': 'test-component',
                    'ComponentVersion': '1.0.0',
                    'ComponentDescription': 'Test component',
                    'Manifests': [{
                        'Platform': {'os': 'linux', 'architecture': 'x86_64'},
                        'Lifecycle': {'Run': 'echo "Hello World"'}
                    }]
                })
            }
            
            # Test component discovery
            discovery_service = create_component_discovery_service()
            components = discovery_service.discover_components()
            
            assert len(components) > 0, "Should discover at least one component"
            assert components[0].component_name == 'test-component'
            print("✓ Component discovery works")
            
            # Test component metadata retrieval
            metadata = discovery_service.get_component_metadata(
                'arn:aws:greengrass:us-east-1:123456789012:components:test-component:versions:1.0.0'
            )
            assert metadata is not None
            assert metadata.component_name == 'test-component'
            print("✓ Component metadata retrieval works")
        
        print("✓ ComponentDiscoveryService tests passed")
        return True
        
    except Exception as e:
        print(f"✗ ComponentDiscoveryService test failed: {e}")
        return False

def test_component_registry():
    """Test ComponentRegistry functionality"""
    print("\n--- Testing ComponentRegistry ---")
    
    try:
        # Mock DynamoDB
        with patch('boto3.resource') as mock_dynamodb:
            mock_table = MagicMock()
            mock_dynamodb.return_value.Table.return_value = mock_table
            
            # Create test component
            test_component = ComponentMetadata(
                component_name="test-component",
                component_arn="arn:aws:greengrass:us-east-1:123456789012:components:test-component:versions:1.0.0",
                version="1.0.0",
                description="Test component",
                publisher="Test Publisher",
                creation_date=datetime.utcnow(),
                component_type=ComponentType.UTILITY,
                platform_compatibility=["linux-x86_64"],
                resource_requirements={"memory": 128},
                dependencies=[],
                tags={"Environment": "test"},
                status=ComponentStatus.ACTIVE
            )
            
            # Test component registration
            registry = create_component_registry('test-table')
            result = registry.register_component(test_component)
            
            # Verify put_item was called
            mock_table.put_item.assert_called_once()
            call_args = mock_table.put_item.call_args[1]['Item']
            assert call_args['component_name'] == 'test-component'
            assert call_args['component_type'] == 'utility'
            print("✓ Component registration works")
            
            # Mock search response
            mock_table.scan.return_value = {
                'Items': [{
                    'component_id': 'test-component#1.0.0',
                    'component_name': 'test-component',
                    'version': '1.0.0',
                    'component_arn': test_component.component_arn,
                    'description': test_component.description,
                    'publisher': test_component.publisher,
                    'creation_date': test_component.creation_date.isoformat(),
                    'component_type': 'utility',
                    'platform_compatibility': test_component.platform_compatibility,
                    'resource_requirements': test_component.resource_requirements,
                    'dependencies': test_component.dependencies,
                    'tags': test_component.tags,
                    'status': 'active'
                }]
            }
            
            # Test component search
            components = registry.search_components(query="test")
            assert len(components) == 1
            assert components[0].component_name == 'test-component'
            print("✓ Component search works")
            
            # Test component retrieval by ID
            mock_table.get_item.return_value = {
                'Item': {
                    'component_id': 'test-component#1.0.0',
                    'component_name': 'test-component',
                    'version': '1.0.0',
                    'component_arn': test_component.component_arn,
                    'description': test_component.description,
                    'publisher': test_component.publisher,
                    'creation_date': test_component.creation_date.isoformat(),
                    'component_type': 'utility',
                    'platform_compatibility': test_component.platform_compatibility,
                    'resource_requirements': test_component.resource_requirements,
                    'dependencies': test_component.dependencies,
                    'tags': test_component.tags,
                    'status': 'active'
                }
            }
            
            component = registry.get_component_by_id('test-component#1.0.0')
            assert component is not None
            assert component.component_name == 'test-component'
            print("✓ Component retrieval by ID works")
        
        print("✓ ComponentRegistry tests passed")
        return True
        
    except Exception as e:
        print(f"✗ ComponentRegistry test failed: {e}")
        return False

def test_component_filter():
    """Test ComponentFilter functionality"""
    print("\n--- Testing ComponentFilter ---")
    
    try:
        # Create component filter
        filter_obj = ComponentFilter(
            component_type=ComponentType.UTILITY,
            platform="linux-x86_64",
            publisher="Test Publisher",
            status=ComponentStatus.ACTIVE,
            search_query="test"
        )
        
        assert filter_obj.component_type == ComponentType.UTILITY
        assert filter_obj.platform == "linux-x86_64"
        assert filter_obj.search_query == "test"
        print("✓ Component filter creation works")
        
        print("✓ ComponentFilter tests passed")
        return True
        
    except Exception as e:
        print(f"✗ ComponentFilter test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("Greengrass Component Discovery - Test Suite")
    print("=" * 50)
    
    tests = [
        test_component_metadata,
        test_component_discovery_service,
        test_component_registry,
        test_component_filter
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\n{'=' * 50}")
    print(f"Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("✓ All tests passed! Component discovery infrastructure is working correctly.")
        print("\nKey Features Implemented:")
        print("  • Component metadata management with serialization")
        print("  • AWS Greengrass component discovery service")
        print("  • Component registry with search and filtering")
        print("  • DynamoDB integration for component storage")
        print("  • Component filtering and categorization")
        print("\nThe component discovery infrastructure is ready for use!")
        return True
    else:
        print("✗ Some tests failed. Please check the implementation.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)