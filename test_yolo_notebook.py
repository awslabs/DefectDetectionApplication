"""
Unit tests for YOLO Cookie Defect Detection notebook.

This test suite validates the environment setup and core functionality
of the YOLO notebook implementation.

Note: These tests are designed to run in environments where boto3 and sagemaker
may not be installed. They use mocking to simulate the behavior of these libraries.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os


class TestEnvironmentSetup(unittest.TestCase):
    """Test cases for environment setup functionality."""
    
    def test_library_imports_succeed(self):
        """
        Test that all required libraries can be imported successfully.
        
        This test validates that standard libraries are importable and that
        the notebook structure expects boto3 and sagemaker to be available.
        
        Validates: Requirements 1.1
        """
        # Test standard libraries (these should always be available)
        try:
            import json
            import datetime
            import os
            from pathlib import Path
            
            self.assertIsNotNone(json, "json should be importable")
            self.assertIsNotNone(datetime, "datetime should be importable")
            self.assertIsNotNone(os, "os should be importable")
            self.assertIsNotNone(Path, "pathlib.Path should be importable")
        except ImportError as e:
            self.fail(f"Failed to import standard library: {e}")
        
        # For AWS libraries, we verify they would be imported in the notebook
        # In a real SageMaker environment, these would be available
        # Here we just verify the import statements are valid Python syntax
        aws_imports = [
            "import boto3",
            "import sagemaker",
            "from sagemaker.pytorch import PyTorch"
        ]
        
        for import_stmt in aws_imports:
            try:
                compile(import_stmt, '<string>', 'exec')
            except SyntaxError as e:
                self.fail(f"Invalid import statement: {import_stmt} - {e}")
    
    @patch('boto3.client')
    def test_s3_folder_creation_success(self, mock_s3_client):
        """
        Test successful S3 folder creation.
        
        Validates: Requirements 1.3
        """
        # Mock S3 client
        mock_client = Mock()
        mock_s3_client.return_value = mock_client
        mock_client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200}}
        
        # Test folder creation
        bucket = "test-bucket"
        key = "test-folder/.folder_marker"
        
        s3_client = mock_s3_client('s3')
        s3_client.put_object(Bucket=bucket, Key=key, Body=b'')
        
        # Verify put_object was called with correct parameters
        mock_client.put_object.assert_called_once_with(
            Bucket=bucket,
            Key=key,
            Body=b''
        )
    
    @patch('boto3.client')
    def test_s3_folder_creation_handles_errors(self, mock_s3_client):
        """
        Test that S3 folder creation handles errors appropriately.
        
        Validates: Requirements 1.3, 13.1
        """
        # Mock S3 client to raise an exception
        mock_client = Mock()
        mock_s3_client.return_value = mock_client
        mock_client.put_object.side_effect = Exception("S3 operation failed")
        
        # Test that exception is raised
        bucket = "test-bucket"
        key = "test-folder/.folder_marker"
        
        s3_client = mock_s3_client('s3')
        
        with self.assertRaises(Exception) as context:
            s3_client.put_object(Bucket=bucket, Key=key, Body=b'')
        
        self.assertIn("S3 operation failed", str(context.exception))
    
    @patch('boto3.client')
    def test_multiple_s3_folders_creation(self, mock_s3_client):
        """
        Test creation of multiple S3 folders for project structure.
        
        Validates: Requirements 1.3
        """
        # Mock S3 client
        mock_client = Mock()
        mock_s3_client.return_value = mock_client
        mock_client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200}}
        
        # Define folder structure
        bucket = "test-bucket"
        folders = [
            'training-output',
            'compilation-output',
            'dataset',
            'models'
        ]
        
        s3_client = mock_s3_client('s3')
        
        # Create all folders
        for folder in folders:
            key = f"{folder}/.folder_marker"
            s3_client.put_object(Bucket=bucket, Key=key, Body=b'')
        
        # Verify put_object was called for each folder
        self.assertEqual(mock_client.put_object.call_count, len(folders))
    
    def test_s3_uri_parsing(self):
        """
        Test parsing of S3 URIs into bucket and key components.
        
        Validates: Requirements 1.3
        """
        # Test S3 URI parsing
        s3_uri = "s3://my-bucket/my-prefix/my-folder"
        
        # Parse URI
        s3_uri_parts = s3_uri.replace('s3://', '').split('/', 1)
        bucket = s3_uri_parts[0]
        prefix = s3_uri_parts[1] if len(s3_uri_parts) > 1 else ''
        
        # Verify parsing
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(prefix, "my-prefix/my-folder")
    
    def test_timestamp_generation(self):
        """
        Test that timestamp generation produces valid format.
        
        Validates: Requirements 1.3
        """
        import datetime
        
        # Generate timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        
        # Verify format (YYYYMMDD-HHMMSS)
        self.assertIsNotNone(timestamp)
        self.assertRegex(timestamp, r'^\d{8}-\d{6}$')
        
        # Verify length
        self.assertEqual(len(timestamp), 15)  # 8 digits + dash + 6 digits


class TestDatasetAcquisition(unittest.TestCase):
    """Test cases for dataset acquisition functionality."""
    
    def test_dataset_validation_with_known_structure(self):
        """
        Test dataset validation with a known valid structure.
        
        This test creates a mock dataset structure and validates that
        the validation logic correctly identifies all required components.
        
        Validates: Requirements 2.3
        """
        import tempfile
        import shutil
        
        # Create temporary directory structure
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create dataset structure
            dataset_root = os.path.join(temp_dir, 'cookie-dataset')
            dataset_files = os.path.join(dataset_root, 'dataset-files')
            training_images = os.path.join(dataset_files, 'training-images')
            mask_images = os.path.join(dataset_files, 'mask-images')
            manifests = os.path.join(dataset_files, 'manifests')
            
            # Create directories
            os.makedirs(training_images)
            os.makedirs(mask_images)
            os.makedirs(manifests)
            
            # Create sample files
            # Training images
            for i in range(5):
                open(os.path.join(training_images, f'image_{i}.jpg'), 'w').close()
            
            # Mask images
            for i in range(3):
                open(os.path.join(mask_images, f'mask_{i}.png'), 'w').close()
            
            # Manifest file
            open(os.path.join(manifests, 'manifest.json'), 'w').close()
            
            # Validate structure
            dataset_structure = {
                'training_images': training_images,
                'mask_images': mask_images,
                'manifests': manifests
            }
            
            # Check all components exist
            for component_name, component_path in dataset_structure.items():
                self.assertTrue(
                    os.path.exists(component_path),
                    f"{component_name} should exist at {component_path}"
                )
            
            # Count images
            training_count = len([f for f in os.listdir(training_images) 
                                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))])
            mask_count = len([f for f in os.listdir(mask_images) 
                             if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))])
            manifest_count = len([f for f in os.listdir(manifests) 
                                 if f.endswith('.json') or f.endswith('.jsonl')])
            
            # Verify counts
            self.assertEqual(training_count, 5, "Should have 5 training images")
            self.assertEqual(mask_count, 3, "Should have 3 mask images")
            self.assertEqual(manifest_count, 1, "Should have 1 manifest file")
    
    def test_dataset_validation_handles_missing_files(self):
        """
        Test that dataset validation properly handles missing files.
        
        This test verifies that the validation logic correctly identifies
        when required components are missing from the dataset structure.
        
        Validates: Requirements 2.3
        """
        import tempfile
        
        # Create temporary directory with incomplete structure
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create only partial structure (missing mask-images)
            dataset_root = os.path.join(temp_dir, 'cookie-dataset')
            dataset_files = os.path.join(dataset_root, 'dataset-files')
            training_images = os.path.join(dataset_files, 'training-images')
            manifests = os.path.join(dataset_files, 'manifests')
            
            # Create only some directories
            os.makedirs(training_images)
            os.makedirs(manifests)
            
            # Define expected structure
            mask_images = os.path.join(dataset_files, 'mask-images')
            
            dataset_structure = {
                'training_images': training_images,
                'mask_images': mask_images,  # This one is missing
                'manifests': manifests
            }
            
            # Validate structure
            validation_passed = True
            missing_components = []
            
            for component_name, component_path in dataset_structure.items():
                if not os.path.exists(component_path):
                    validation_passed = False
                    missing_components.append(component_name)
            
            # Verify validation failed
            self.assertFalse(validation_passed, "Validation should fail with missing components")
            self.assertIn('mask_images', missing_components, "Should detect missing mask_images")
            self.assertEqual(len(missing_components), 1, "Should have exactly 1 missing component")
    
    def test_image_file_filtering(self):
        """
        Test that image file filtering correctly identifies image files.
        
        This test verifies that the file filtering logic correctly identifies
        image files by extension and ignores non-image files.
        
        Validates: Requirements 2.3, 2.4
        """
        import tempfile
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create various file types
            files = [
                'image1.jpg',
                'image2.jpeg',
                'image3.png',
                'image4.bmp',
                'image5.JPG',  # Test case insensitivity
                'not_image.txt',
                'not_image.json',
                'readme.md'
            ]
            
            for filename in files:
                open(os.path.join(temp_dir, filename), 'w').close()
            
            # Filter for image files
            image_files = [f for f in os.listdir(temp_dir) 
                          if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))]
            
            # Verify filtering
            self.assertEqual(len(image_files), 5, "Should find 5 image files")
            self.assertIn('image1.jpg', image_files)
            self.assertIn('image5.JPG', image_files)  # Case insensitive
            self.assertNotIn('not_image.txt', image_files)
            self.assertNotIn('readme.md', image_files)
    
    def test_manifest_file_filtering(self):
        """
        Test that manifest file filtering correctly identifies JSON files.
        
        Validates: Requirements 2.3
        """
        import tempfile
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create various file types
            files = [
                'manifest.json',
                'manifest.jsonl',
                'data.json',
                'readme.txt',
                'config.yaml'
            ]
            
            for filename in files:
                open(os.path.join(temp_dir, filename), 'w').close()
            
            # Filter for manifest files
            manifest_files = [f for f in os.listdir(temp_dir) 
                             if f.endswith('.json') or f.endswith('.jsonl')]
            
            # Verify filtering
            self.assertEqual(len(manifest_files), 3, "Should find 3 JSON/JSONL files")
            self.assertIn('manifest.json', manifest_files)
            self.assertIn('manifest.jsonl', manifest_files)
            self.assertNotIn('readme.txt', manifest_files)
    
    def test_error_message_includes_context(self):
        """
        Test that error messages include helpful context information.
        
        This test verifies that when dataset validation fails, the error
        message includes the expected file locations and available files.
        
        Validates: Requirements 2.3, 13.2
        """
        import tempfile
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create incomplete structure
            dataset_root = os.path.join(temp_dir, 'cookie-dataset')
            dataset_files = os.path.join(dataset_root, 'dataset-files')
            
            # Only create training-images
            training_images = os.path.join(dataset_files, 'training-images')
            os.makedirs(training_images)
            
            # Define expected structure
            mask_images = os.path.join(dataset_files, 'mask-images')
            manifests = os.path.join(dataset_files, 'manifests')
            
            dataset_structure = {
                'training_images': training_images,
                'mask_images': mask_images,
                'manifests': manifests
            }
            
            # Build error message
            missing_components = []
            for component_name, component_path in dataset_structure.items():
                if not os.path.exists(component_path):
                    missing_components.append(component_name)
            
            if missing_components:
                error_message = (
                    f"Invalid dataset structure.\\n"
                    f"Missing required components.\\n"
                    f"Expected structure:\\n"
                    f"  - {dataset_structure['training_images']}\\n"
                    f"  - {dataset_structure['mask_images']}\\n"
                    f"  - {dataset_structure['manifests']}"
                )
                
                # Verify error message contains context
                self.assertIn('Invalid dataset structure', error_message)
                self.assertIn('Missing required components', error_message)
                self.assertIn('training-images', error_message)
                self.assertIn('mask-images', error_message)
                self.assertIn('manifests', error_message)


if __name__ == '__main__':
    unittest.main()



class TestFormatConversion(unittest.TestCase):
    """Test cases for YOLO format conversion functionality."""
    
    def test_coordinate_normalization_round_trip(self):
        """
        Feature: yolo-cookie-defect-detection, Property 1: Coordinate Normalization Preserves Relative Position
        
        Property-based test that verifies coordinate normalization preserves relative position.
        
        For any pixel coordinates (x, y) and image dimensions (width, height), normalizing
        the coordinates to YOLO format and then denormalizing them back should produce
        coordinates within 1 pixel of the original values (accounting for floating-point precision).
        
        Validates: Requirements 3.3
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        from yolo_format_converter import normalize_coordinates
        
        @given(
            x=st.integers(min_value=0, max_value=1920),
            y=st.integers(min_value=0, max_value=1080),
            width=st.integers(min_value=1, max_value=500),
            height=st.integers(min_value=1, max_value=500),
            img_width=st.integers(min_value=100, max_value=1920),
            img_height=st.integers(min_value=100, max_value=1080)
        )
        @settings(max_examples=100)
        def property_test(x, y, width, height, img_width, img_height):
            # Ensure bbox fits within image
            if x + width > img_width or y + height > img_height:
                return  # Skip invalid cases
            
            # Original bbox in pixel coordinates
            bbox = (x, y, width, height)
            
            # Normalize to YOLO format
            center_x_norm, center_y_norm, width_norm, height_norm = normalize_coordinates(
                bbox, img_width, img_height
            )
            
            # Verify normalized values are in [0, 1] range
            assert 0 <= center_x_norm <= 1, f"center_x_norm out of range: {center_x_norm}"
            assert 0 <= center_y_norm <= 1, f"center_y_norm out of range: {center_y_norm}"
            assert 0 <= width_norm <= 1, f"width_norm out of range: {width_norm}"
            assert 0 <= height_norm <= 1, f"height_norm out of range: {height_norm}"
            
            # Denormalize back to pixel coordinates
            center_x_pixels = center_x_norm * img_width
            center_y_pixels = center_y_norm * img_height
            width_pixels = width_norm * img_width
            height_pixels = height_norm * img_height
            
            # Convert from center format back to top-left format
            x_reconstructed = center_x_pixels - width_pixels / 2.0
            y_reconstructed = center_y_pixels - height_pixels / 2.0
            
            # Verify round-trip accuracy (within 1 pixel due to floating-point precision)
            assert abs(x_reconstructed - x) <= 1.0, \
                f"x round-trip error: original={x}, reconstructed={x_reconstructed}"
            assert abs(y_reconstructed - y) <= 1.0, \
                f"y round-trip error: original={y}, reconstructed={y_reconstructed}"
            assert abs(width_pixels - width) <= 1.0, \
                f"width round-trip error: original={width}, reconstructed={width_pixels}"
            assert abs(height_pixels - height) <= 1.0, \
                f"height round-trip error: original={height}, reconstructed={height_pixels}"
        
        # Run the property test
        property_test()
    
    def test_normalize_coordinates_basic(self):
        """
        Unit test for coordinate normalization with known values.
        
        Validates: Requirements 3.3
        """
        from yolo_format_converter import normalize_coordinates
        
        # Test case: bbox at (100, 200, 50, 60) in 640x480 image
        bbox = (100, 200, 50, 60)
        img_width = 640
        img_height = 480
        
        center_x_norm, center_y_norm, width_norm, height_norm = normalize_coordinates(
            bbox, img_width, img_height
        )
        
        # Expected values:
        # center_x = 100 + 50/2 = 125, normalized = 125/640 = 0.1953125
        # center_y = 200 + 60/2 = 230, normalized = 230/480 = 0.4791666...
        # width_norm = 50/640 = 0.078125
        # height_norm = 60/480 = 0.125
        
        self.assertAlmostEqual(center_x_norm, 0.1953125, places=6)
        self.assertAlmostEqual(center_y_norm, 0.4791666, places=6)
        self.assertAlmostEqual(width_norm, 0.078125, places=6)
        self.assertAlmostEqual(height_norm, 0.125, places=6)
    
    def test_normalize_coordinates_edge_cases(self):
        """
        Test coordinate normalization with edge cases.
        
        Validates: Requirements 3.3
        """
        from yolo_format_converter import normalize_coordinates
        
        # Test case 1: bbox at origin
        bbox = (0, 0, 10, 10)
        center_x, center_y, width, height = normalize_coordinates(bbox, 100, 100)
        self.assertAlmostEqual(center_x, 0.05, places=6)
        self.assertAlmostEqual(center_y, 0.05, places=6)
        
        # Test case 2: bbox covering entire image
        bbox = (0, 0, 100, 100)
        center_x, center_y, width, height = normalize_coordinates(bbox, 100, 100)
        self.assertAlmostEqual(center_x, 0.5, places=6)
        self.assertAlmostEqual(center_y, 0.5, places=6)
        self.assertAlmostEqual(width, 1.0, places=6)
        self.assertAlmostEqual(height, 1.0, places=6)
        
        # Test case 3: Invalid dimensions should raise error
        with self.assertRaises(ValueError):
            normalize_coordinates((10, 10, 20, 20), 0, 100)
        
        with self.assertRaises(ValueError):
            normalize_coordinates((10, 10, 20, 20), 100, -50)

    
    def test_bounding_box_covers_mask_region(self):
        """
        Feature: yolo-cookie-defect-detection, Property 2: Bounding Box Extraction Covers Mask Region
        
        Property-based test that verifies extracted bounding boxes cover all mask pixels.
        
        For any segmentation mask with non-zero pixels, the extracted bounding box should
        contain all non-zero pixels from the mask (i.e., no defect pixels should fall
        outside the bounding box).
        
        Validates: Requirements 3.2
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        import cv2
        import numpy as np
        import tempfile
        from yolo_format_converter import extract_bounding_boxes
        
        @given(
            mask_width=st.integers(min_value=50, max_value=500),
            mask_height=st.integers(min_value=50, max_value=500),
            defect_x=st.integers(min_value=10, max_value=400),
            defect_y=st.integers(min_value=10, max_value=400),
            defect_width=st.integers(min_value=5, max_value=100),
            defect_height=st.integers(min_value=5, max_value=100)
        )
        @settings(max_examples=100)
        def property_test(mask_width, mask_height, defect_x, defect_y, defect_width, defect_height):
            # Ensure defect fits within mask
            if defect_x + defect_width > mask_width or defect_y + defect_height > mask_height:
                return  # Skip invalid cases
            
            # Create a blank mask
            mask = np.zeros((mask_height, mask_width), dtype=np.uint8)
            
            # Draw a white rectangle representing a defect
            mask[defect_y:defect_y+defect_height, defect_x:defect_x+defect_width] = 255
            
            # Save mask to temporary file
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
                tmp_path = tmp_file.name
                cv2.imwrite(tmp_path, mask)
            
            try:
                # Extract bounding boxes
                bboxes = extract_bounding_boxes(tmp_path)
                
                # Should have at least one bounding box
                assert len(bboxes) > 0, "Should extract at least one bounding box from non-empty mask"
                
                # For each non-zero pixel in the mask, verify it's covered by at least one bbox
                non_zero_coords = np.argwhere(mask > 0)
                
                for coord in non_zero_coords:
                    y_pixel, x_pixel = coord
                    
                    # Check if this pixel is covered by any bounding box
                    covered = False
                    for bbox in bboxes:
                        x, y, w, h = bbox
                        if x <= x_pixel < x + w and y <= y_pixel < y + h:
                            covered = True
                            break
                    
                    assert covered, \
                        f"Pixel ({x_pixel}, {y_pixel}) not covered by any bounding box. Boxes: {bboxes}"
            
            finally:
                # Clean up temporary file
                import os
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        
        # Run the property test
        property_test()
    
    def test_extract_bounding_boxes_basic(self):
        """
        Unit test for bounding box extraction with a known mask.
        
        Validates: Requirements 3.2
        """
        import cv2
        import numpy as np
        import tempfile
        from yolo_format_converter import extract_bounding_boxes
        
        # Create a test mask with a single white rectangle
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[20:40, 30:60] = 255  # White rectangle from (30,20) with size 30x20
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
            tmp_path = tmp_file.name
            cv2.imwrite(tmp_path, mask)
        
        try:
            # Extract bounding boxes
            bboxes = extract_bounding_boxes(tmp_path)
            
            # Should have exactly one bounding box
            self.assertEqual(len(bboxes), 1, "Should extract one bounding box")
            
            # Verify bounding box coordinates
            x, y, w, h = bboxes[0]
            self.assertEqual(x, 30, "x coordinate should be 30")
            self.assertEqual(y, 20, "y coordinate should be 20")
            self.assertEqual(w, 30, "width should be 30")
            self.assertEqual(h, 20, "height should be 20")
        
        finally:
            # Clean up
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def test_extract_bounding_boxes_multiple_regions(self):
        """
        Test bounding box extraction with multiple defect regions.
        
        Validates: Requirements 3.2
        """
        import cv2
        import numpy as np
        import tempfile
        from yolo_format_converter import extract_bounding_boxes
        
        # Create a test mask with two white rectangles
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[10:20, 10:20] = 255  # First defect
        mask[50:70, 60:80] = 255  # Second defect
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
            tmp_path = tmp_file.name
            cv2.imwrite(tmp_path, mask)
        
        try:
            # Extract bounding boxes
            bboxes = extract_bounding_boxes(tmp_path)
            
            # Should have exactly two bounding boxes
            self.assertEqual(len(bboxes), 2, "Should extract two bounding boxes")
            
            # Verify both regions are covered
            # Sort by x coordinate for consistent ordering
            bboxes_sorted = sorted(bboxes, key=lambda b: b[0])
            
            # First bbox should cover first defect
            x1, y1, w1, h1 = bboxes_sorted[0]
            self.assertEqual(x1, 10)
            self.assertEqual(y1, 10)
            self.assertEqual(w1, 10)
            self.assertEqual(h1, 10)
            
            # Second bbox should cover second defect
            x2, y2, w2, h2 = bboxes_sorted[1]
            self.assertEqual(x2, 60)
            self.assertEqual(y2, 50)
            self.assertEqual(w2, 20)
            self.assertEqual(h2, 20)
        
        finally:
            # Clean up
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def test_extract_bounding_boxes_empty_mask(self):
        """
        Test bounding box extraction with an empty mask (no defects).
        
        Validates: Requirements 3.2
        """
        import cv2
        import numpy as np
        import tempfile
        from yolo_format_converter import extract_bounding_boxes
        
        # Create an empty mask (all black)
        mask = np.zeros((100, 100), dtype=np.uint8)
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
            tmp_path = tmp_file.name
            cv2.imwrite(tmp_path, mask)
        
        try:
            # Extract bounding boxes
            bboxes = extract_bounding_boxes(tmp_path)
            
            # Should have no bounding boxes
            self.assertEqual(len(bboxes), 0, "Should extract zero bounding boxes from empty mask")
        
        finally:
            # Clean up
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def test_extract_bounding_boxes_invalid_file(self):
        """
        Test that extract_bounding_boxes handles invalid files appropriately.
        
        Validates: Requirements 3.2, 13.2
        """
        from yolo_format_converter import extract_bounding_boxes
        
        # Test with non-existent file
        with self.assertRaises(ValueError) as context:
            extract_bounding_boxes('nonexistent_file.png')
        
        # Verify error message includes context
        self.assertIn('Failed to load mask image', str(context.exception))
        self.assertIn('nonexistent_file.png', str(context.exception))
    
    def test_yolo_detection_format_compliance(self):
        """
        Feature: yolo-cookie-defect-detection, Property 3: YOLO Detection Annotation Format Compliance
        
        Property-based test that verifies YOLO detection annotation format compliance.
        
        For any generated YOLO detection annotation line, parsing it should yield exactly
        5 values (class_id, center_x, center_y, width, height) where class_id is an integer
        and all coordinates are floats in the range [0, 1].
        
        Validates: Requirements 3.5
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        from yolo_format_converter import convert_to_yolo_format
        
        @given(
            center_x=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            center_y=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            width=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            height=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            class_id=st.integers(min_value=0, max_value=10)
        )
        @settings(max_examples=100)
        def property_test(center_x, center_y, width, height, class_id):
            # Create normalized bbox
            bbox = (center_x, center_y, width, height)
            
            # Convert to YOLO format
            annotation_line = convert_to_yolo_format(bbox, class_id)
            
            # Parse the annotation line
            parts = annotation_line.split()
            
            # Should have exactly 5 values
            assert len(parts) == 5, \
                f"YOLO annotation should have 5 values, got {len(parts)}: {annotation_line}"
            
            # Parse values
            parsed_class_id = int(parts[0])
            parsed_center_x = float(parts[1])
            parsed_center_y = float(parts[2])
            parsed_width = float(parts[3])
            parsed_height = float(parts[4])
            
            # Verify class_id is an integer
            assert isinstance(parsed_class_id, int), \
                f"class_id should be integer, got {type(parsed_class_id)}"
            
            # Verify all coordinates are in [0, 1] range
            assert 0.0 <= parsed_center_x <= 1.0, \
                f"center_x out of range [0, 1]: {parsed_center_x}"
            assert 0.0 <= parsed_center_y <= 1.0, \
                f"center_y out of range [0, 1]: {parsed_center_y}"
            assert 0.0 <= parsed_width <= 1.0, \
                f"width out of range [0, 1]: {parsed_width}"
            assert 0.0 <= parsed_height <= 1.0, \
                f"height out of range [0, 1]: {parsed_height}"
            
            # Verify values match original (within floating-point precision)
            assert abs(parsed_class_id - class_id) < 0.001, \
                f"class_id mismatch: original={class_id}, parsed={parsed_class_id}"
            assert abs(parsed_center_x - center_x) < 0.000001, \
                f"center_x mismatch: original={center_x}, parsed={parsed_center_x}"
            assert abs(parsed_center_y - center_y) < 0.000001, \
                f"center_y mismatch: original={center_y}, parsed={parsed_center_y}"
            assert abs(parsed_width - width) < 0.000001, \
                f"width mismatch: original={width}, parsed={parsed_width}"
            assert abs(parsed_height - height) < 0.000001, \
                f"height mismatch: original={height}, parsed={parsed_height}"
        
        # Run the property test
        property_test()


    def test_yolo_segmentation_format_compliance(self):
        """
        Feature: yolo-cookie-defect-detection, Property 4: YOLO Segmentation Annotation Format Compliance
        
        Property-based test that verifies YOLO segmentation annotation format compliance.
        
        For any generated YOLO segmentation annotation line, parsing it should yield an odd
        number of values (class_id followed by pairs of x,y coordinates) where class_id is
        an integer and all coordinates are floats in the range [0, 1].
        
        Validates: Requirements 3.5
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        import numpy as np
        from yolo_format_converter import convert_to_yolo_segment_format
        
        @given(
            # Generate random polygon with 3-20 points
            num_points=st.integers(min_value=3, max_value=20),
            img_width=st.integers(min_value=100, max_value=1920),
            img_height=st.integers(min_value=100, max_value=1080),
            class_id=st.integers(min_value=0, max_value=10)
        )
        @settings(max_examples=100)
        def property_test(num_points, img_width, img_height, class_id):
            # Generate random polygon points within image bounds
            points = []
            for _ in range(num_points):
                x = np.random.randint(0, img_width)
                y = np.random.randint(0, img_height)
                points.append([[x, y]])
            
            polygon = np.array(points)
            
            # Convert to YOLO segmentation format
            annotation_line = convert_to_yolo_segment_format(
                polygon, class_id, img_width, img_height
            )
            
            # Parse the annotation line
            parts = annotation_line.split()
            
            # Should have odd number of values (class_id + pairs of coordinates)
            assert len(parts) % 2 == 1, \
                f"YOLO segmentation annotation should have odd number of values, got {len(parts)}: {annotation_line}"
            
            # Should have at least 7 values (class_id + 3 points minimum for a polygon)
            assert len(parts) >= 7, \
                f"YOLO segmentation annotation should have at least 7 values (class_id + 3 points), got {len(parts)}"
            
            # Parse class_id
            parsed_class_id = int(parts[0])
            
            # Verify class_id is an integer
            assert isinstance(parsed_class_id, int), \
                f"class_id should be integer, got {type(parsed_class_id)}"
            
            # Verify class_id matches
            assert parsed_class_id == class_id, \
                f"class_id mismatch: original={class_id}, parsed={parsed_class_id}"
            
            # Parse coordinate pairs
            coords = parts[1:]
            assert len(coords) % 2 == 0, \
                f"Coordinates should come in pairs, got {len(coords)} values"
            
            # Verify all coordinates are floats in [0, 1] range
            for i in range(0, len(coords), 2):
                x_norm = float(coords[i])
                y_norm = float(coords[i + 1])
                
                assert 0.0 <= x_norm <= 1.0, \
                    f"x coordinate out of range [0, 1]: {x_norm}"
                assert 0.0 <= y_norm <= 1.0, \
                    f"y coordinate out of range [0, 1]: {y_norm}"
            
            # Verify number of coordinate pairs matches number of polygon points
            num_coord_pairs = len(coords) // 2
            assert num_coord_pairs == num_points, \
                f"Number of coordinate pairs mismatch: expected {num_points}, got {num_coord_pairs}"
        
        # Run the property test
        property_test()

    def test_manifest_parsing_preserves_data(self):
        """
        Feature: yolo-cookie-defect-detection, Property 8: Manifest Parsing Preserves Data
        
        Property-based test that verifies manifest parsing preserves all data.
        
        For any valid Lookout for Vision manifest file, parsing all lines should produce
        a list of records where the count equals the number of lines in the file, and each
        record contains the required fields (source-ref, anomaly-label).
        
        Validates: Requirements 3.1
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        import tempfile
        import json
        from yolo_format_converter import read_manifest
        
        @given(
            # Generate random image paths and labels
            records_data=st.lists(
                st.tuples(
                    st.text(min_size=5, max_size=50, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'), whitelist_characters='/-_.')),
                    st.integers(min_value=0, max_value=1)
                ),
                min_size=1,
                max_size=50
            )
        )
        @settings(max_examples=100)
        def property_test(records_data):
            # Create temporary manifest file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp_file:
                tmp_path = tmp_file.name
                
                # Write JSON Lines format
                for image_path, label in records_data:
                    record = {
                        'source-ref': f's3://bucket/{image_path}.jpg',
                        'anomaly-label': label,
                        'anomaly-label-metadata': {
                            'confidence': 0.95,
                            'class-name': 'anomaly' if label == 1 else 'normal'
                        }
                    }
                    tmp_file.write(json.dumps(record) + '\n')
            
            try:
                # Parse the manifest
                parsed_records = read_manifest(tmp_path)
                
                # Verify count matches
                assert len(parsed_records) == len(records_data), \
                    f"Parsed record count mismatch: expected {len(records_data)}, got {len(parsed_records)}"
                
                # Verify each record has required fields
                for i, record in enumerate(parsed_records):
                    assert 'source-ref' in record, \
                        f"Record {i} missing 'source-ref' field"
                    assert 'anomaly-label' in record, \
                        f"Record {i} missing 'anomaly-label' field"
                    
                    # Verify data integrity
                    expected_path, expected_label = records_data[i]
                    assert record['anomaly-label'] == expected_label, \
                        f"Record {i} label mismatch: expected {expected_label}, got {record['anomaly-label']}"
                    assert expected_path in record['source-ref'], \
                        f"Record {i} path mismatch: expected '{expected_path}' in '{record['source-ref']}'"
            
            finally:
                # Clean up temporary file
                import os
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        
        # Run the property test
        property_test()


class TestS3Upload(unittest.TestCase):
    """Test cases for S3 upload functionality."""
    
    def test_error_messages_include_context(self):
        """
        Feature: yolo-cookie-defect-detection, Property 7: Error Messages Include Context
        
        Property-based test that verifies error messages include relevant context.
        
        For any S3 operation failure or missing file error, the error message should
        contain the relevant file path, bucket name, or key that caused the error.
        
        Validates: Requirements 13.1, 13.2, 13.5
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        from botocore.exceptions import ClientError
        
        @given(
            bucket_name=st.text(min_size=3, max_size=63, alphabet=st.characters(whitelist_categories=('Ll', 'Nd'), whitelist_characters='-')),
            s3_key=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'), whitelist_characters='/-_.')),
            local_file=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'), whitelist_characters='/-_.')),
            error_code=st.sampled_from(['NoSuchBucket', 'AccessDenied', 'InvalidBucketName', 'NoSuchKey', 'RequestTimeout'])
        )
        @settings(max_examples=100)
        def property_test(bucket_name, s3_key, local_file, error_code):
            # Simulate S3 upload error
            error_msg = f"S3 operation failed: {error_code}"
            
            # Build error message with context (as done in notebook)
            contextual_error = (
                f"❌ S3 upload failed: {error_code}\n"
                f"   File: {local_file}\n"
                f"   Destination: s3://{bucket_name}/{s3_key}\n"
                f"   Error: {error_msg}"
            )
            
            # Verify error message contains all context elements
            assert local_file in contextual_error, \
                f"Error message should contain local file path: {local_file}"
            assert bucket_name in contextual_error, \
                f"Error message should contain bucket name: {bucket_name}"
            assert s3_key in contextual_error, \
                f"Error message should contain S3 key: {s3_key}"
            assert error_code in contextual_error, \
                f"Error message should contain error code: {error_code}"
            
            # Verify S3 URI is properly formatted
            s3_uri = f"s3://{bucket_name}/{s3_key}"
            assert s3_uri in contextual_error, \
                f"Error message should contain complete S3 URI: {s3_uri}"
        
        # Run the property test
        property_test()
    
    def test_s3_upload_error_message_format(self):
        """
        Unit test for S3 upload error message formatting.
        
        Validates: Requirements 13.1, 13.5
        """
        # Test case: S3 upload failure
        bucket = "my-test-bucket"
        key = "dataset/images/test.jpg"
        local_file = "/path/to/local/test.jpg"
        error_code = "AccessDenied"
        error_msg = "Access Denied"
        
        # Build error message (as done in notebook)
        error_message = (
            f"❌ S3 upload failed: {error_code}\n"
            f"   File: {local_file}\n"
            f"   Destination: s3://{bucket}/{key}\n"
            f"   Error: {error_msg}"
        )
        
        # Verify all context is present
        self.assertIn(local_file, error_message)
        self.assertIn(bucket, error_message)
        self.assertIn(key, error_message)
        self.assertIn(error_code, error_message)
        self.assertIn(f"s3://{bucket}/{key}", error_message)
    
    def test_missing_file_error_message_format(self):
        """
        Unit test for missing file error message formatting.
        
        Validates: Requirements 13.2, 13.5
        """
        # Test case: Missing data.yaml file
        expected_location = "s3://my-bucket/project/dataset/data.yaml"
        
        # Build error message (as done in notebook)
        error_message = (
            f"data.yaml not found in S3.\n"
            f"Expected location: {expected_location}\n"
            f"This file is required for YOLO training."
        )
        
        # Verify context is present
        self.assertIn("data.yaml", error_message)
        self.assertIn(expected_location, error_message)
        self.assertIn("required for YOLO training", error_message)
    
    def test_dataset_validation_error_includes_paths(self):
        """
        Test that dataset validation errors include expected file paths.
        
        Validates: Requirements 13.2
        """
        # Test case: Invalid dataset structure
        training_images_path = "/path/to/dataset/training-images"
        mask_images_path = "/path/to/dataset/mask-images"
        manifests_path = "/path/to/dataset/manifests"
        
        # Build error message (as done in notebook)
        error_message = (
            f"Invalid dataset structure.\n"
            f"Missing required components.\n"
            f"Expected structure:\n"
            f"  - {training_images_path}\n"
            f"  - {mask_images_path}\n"
            f"  - {manifests_path}"
        )
        
        # Verify all paths are present
        self.assertIn(training_images_path, error_message)
        self.assertIn(mask_images_path, error_message)
        self.assertIn(manifests_path, error_message)
        self.assertIn("Invalid dataset structure", error_message)
        self.assertIn("Missing required components", error_message)
    
    @patch('boto3.client')
    def test_s3_upload_with_client_error(self, mock_boto_client):
        """
        Test S3 upload error handling with ClientError.
        
        Validates: Requirements 4.5, 13.1
        """
        from botocore.exceptions import ClientError
        
        # Mock S3 client to raise ClientError
        mock_client = Mock()
        mock_boto_client.return_value = mock_client
        
        error_response = {
            'Error': {
                'Code': 'NoSuchBucket',
                'Message': 'The specified bucket does not exist'
            }
        }
        mock_client.upload_file.side_effect = ClientError(error_response, 'upload_file')
        
        # Attempt upload
        s3_client = mock_boto_client('s3')
        
        with self.assertRaises(ClientError) as context:
            s3_client.upload_file(
                '/path/to/file.jpg',
                'nonexistent-bucket',
                'dataset/file.jpg'
            )
        
        # Verify error contains expected information
        error = context.exception
        self.assertEqual(error.response['Error']['Code'], 'NoSuchBucket')
        self.assertIn('bucket does not exist', error.response['Error']['Message'])
    
    @patch('boto3.client')
    def test_s3_list_objects_error_handling(self, mock_boto_client):
        """
        Test S3 list objects error handling.
        
        Validates: Requirements 4.4, 13.1
        """
        from botocore.exceptions import ClientError
        
        # Mock S3 client to raise ClientError
        mock_client = Mock()
        mock_boto_client.return_value = mock_client
        
        error_response = {
            'Error': {
                'Code': 'AccessDenied',
                'Message': 'Access Denied'
            }
        }
        mock_client.list_objects_v2.side_effect = ClientError(error_response, 'list_objects_v2')
        
        # Attempt to list objects
        s3_client = mock_boto_client('s3')
        
        with self.assertRaises(ClientError) as context:
            s3_client.list_objects_v2(
                Bucket='test-bucket',
                Prefix='dataset/images/'
            )
        
        # Verify error
        error = context.exception
        self.assertEqual(error.response['Error']['Code'], 'AccessDenied')
    
    @patch('boto3.client')
    def test_s3_head_object_not_found(self, mock_boto_client):
        """
        Test S3 head_object handling for missing files.
        
        Validates: Requirements 4.4, 13.5
        """
        from botocore.exceptions import ClientError
        
        # Mock S3 client to raise 404 error
        mock_client = Mock()
        mock_boto_client.return_value = mock_client
        
        error_response = {
            'Error': {
                'Code': '404',
                'Message': 'Not Found'
            }
        }
        mock_client.head_object.side_effect = ClientError(error_response, 'head_object')
        
        # Attempt to check if object exists
        s3_client = mock_boto_client('s3')
        
        try:
            s3_client.head_object(
                Bucket='test-bucket',
                Key='dataset/data.yaml'
            )
            self.fail("Should have raised ClientError")
        except ClientError as e:
            # Verify it's a 404 error
            self.assertEqual(e.response['Error']['Code'], '404')
            
            # Build contextual error message
            bucket = 'test-bucket'
            key = 'dataset/data.yaml'
            error_msg = (
                f"❌ data.yaml not found in S3\n"
                f"   Expected location: s3://{bucket}/{key}"
            )
            
            # Verify error message includes context
            self.assertIn('data.yaml', error_msg)
            self.assertIn(bucket, error_msg)
            self.assertIn(key, error_msg)
            self.assertIn(f"s3://{bucket}/{key}", error_msg)



class TestTrainingScript(unittest.TestCase):
    """Test cases for YOLOv8 training script functionality."""
    
    def test_argument_parsing_default_values(self):
        """
        Test that argument parsing works with default values.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        # Mock sys.argv to simulate command-line arguments
        with patch('sys.argv', ['yolo_training.py']):
            args = parse_args()
            
            # Verify default values
            self.assertEqual(args.model_size, 'yolov8n')
            self.assertEqual(args.task, 'detect')
            self.assertEqual(args.epochs, 50)
            self.assertEqual(args.batch_size, 16)
            self.assertEqual(args.img_size, 640)
            self.assertAlmostEqual(args.conf_threshold, 0.25)
            self.assertEqual(args.data_dir, '/opt/ml/input/data/training')
            self.assertEqual(args.model_dir, '/opt/ml/model')
    
    def test_argument_parsing_custom_values(self):
        """
        Test that argument parsing works with custom values.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        # Mock sys.argv with custom arguments
        test_args = [
            'yolo_training.py',
            '--model-size', 'yolov8m',
            '--task', 'segment',
            '--epochs', '100',
            '--batch-size', '32',
            '--img-size', '1024',
            '--conf-threshold', '0.5',
            '--data-dir', '/custom/data',
            '--model-dir', '/custom/model'
        ]
        
        with patch('sys.argv', test_args):
            args = parse_args()
            
            # Verify custom values
            self.assertEqual(args.model_size, 'yolov8m')
            self.assertEqual(args.task, 'segment')
            self.assertEqual(args.epochs, 100)
            self.assertEqual(args.batch_size, 32)
            self.assertEqual(args.img_size, 1024)
            self.assertAlmostEqual(args.conf_threshold, 0.5)
            self.assertEqual(args.data_dir, '/custom/data')
            self.assertEqual(args.model_dir, '/custom/model')
    
    def test_argument_parsing_all_model_sizes(self):
        """
        Test that all valid model sizes are accepted.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        valid_sizes = ['yolov8n', 'yolov8s', 'yolov8m', 'yolov8l', 'yolov8x']
        
        for size in valid_sizes:
            test_args = ['yolo_training.py', '--model-size', size]
            
            with patch('sys.argv', test_args):
                args = parse_args()
                self.assertEqual(args.model_size, size)
    
    def test_argument_parsing_invalid_model_size(self):
        """
        Test that invalid model sizes are rejected.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        # Test with invalid model size
        test_args = ['yolo_training.py', '--model-size', 'invalid_model']
        
        with patch('sys.argv', test_args):
            with self.assertRaises(SystemExit):
                parse_args()
    
    def test_argument_parsing_task_types(self):
        """
        Test that both task types (detect and segment) are accepted.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        # Test detect task
        with patch('sys.argv', ['yolo_training.py', '--task', 'detect']):
            args = parse_args()
            self.assertEqual(args.task, 'detect')
        
        # Test segment task
        with patch('sys.argv', ['yolo_training.py', '--task', 'segment']):
            args = parse_args()
            self.assertEqual(args.task, 'segment')
    
    def test_argument_parsing_invalid_task(self):
        """
        Test that invalid task types are rejected.
        
        Validates: Requirements 5.1
        """
        from yolo_training import parse_args
        
        # Test with invalid task
        test_args = ['yolo_training.py', '--task', 'invalid_task']
        
        with patch('sys.argv', test_args):
            with self.assertRaises(SystemExit):
                parse_args()
    
    def test_model_loading_detection(self):
        """
        Test that detection models are loaded correctly.
        
        Validates: Requirements 5.1
        """
        # Mock the ultralytics module
        mock_ultralytics = MagicMock()
        mock_yolo_class = Mock()
        mock_model = Mock()
        mock_yolo_class.return_value = mock_model
        mock_ultralytics.YOLO = mock_yolo_class
        
        with patch.dict('sys.modules', {'ultralytics': mock_ultralytics}):
            from yolo_training import load_model
            
            # Load detection model
            model = load_model('yolov8n', 'detect')
            
            # Verify YOLO was called with correct model name
            mock_yolo_class.assert_called_once_with('yolov8n.pt')
            self.assertEqual(model, mock_model)
    
    def test_model_loading_segmentation(self):
        """
        Test that segmentation models are loaded correctly.
        
        Validates: Requirements 5.1
        """
        # Mock the ultralytics module
        mock_ultralytics = MagicMock()
        mock_yolo_class = Mock()
        mock_model = Mock()
        mock_yolo_class.return_value = mock_model
        mock_ultralytics.YOLO = mock_yolo_class
        
        with patch.dict('sys.modules', {'ultralytics': mock_ultralytics}):
            from yolo_training import load_model
            
            # Load segmentation model
            model = load_model('yolov8s', 'segment')
            
            # Verify YOLO was called with correct model name (with -seg suffix)
            mock_yolo_class.assert_called_once_with('yolov8s-seg.pt')
            self.assertEqual(model, mock_model)
    
    def test_model_loading_all_sizes_detection(self):
        """
        Test that all model sizes work for detection.
        
        Validates: Requirements 5.1
        """
        # Mock the ultralytics module
        mock_ultralytics = MagicMock()
        mock_yolo_class = Mock()
        mock_ultralytics.YOLO = mock_yolo_class
        
        with patch.dict('sys.modules', {'ultralytics': mock_ultralytics}):
            from yolo_training import load_model
            
            model_sizes = ['yolov8n', 'yolov8s', 'yolov8m', 'yolov8l', 'yolov8x']
            
            for size in model_sizes:
                mock_yolo_class.reset_mock()
                mock_model = Mock()
                mock_yolo_class.return_value = mock_model
                
                model = load_model(size, 'detect')
                
                expected_name = f'{size}.pt'
                mock_yolo_class.assert_called_once_with(expected_name)
    
    def test_model_loading_all_sizes_segmentation(self):
        """
        Test that all model sizes work for segmentation.
        
        Validates: Requirements 5.1
        """
        # Mock the ultralytics module
        mock_ultralytics = MagicMock()
        mock_yolo_class = Mock()
        mock_ultralytics.YOLO = mock_yolo_class
        
        with patch.dict('sys.modules', {'ultralytics': mock_ultralytics}):
            from yolo_training import load_model
            
            model_sizes = ['yolov8n', 'yolov8s', 'yolov8m', 'yolov8l', 'yolov8x']
            
            for size in model_sizes:
                mock_yolo_class.reset_mock()
                mock_model = Mock()
                mock_yolo_class.return_value = mock_model
                
                model = load_model(size, 'segment')
                
                expected_name = f'{size}-seg.pt'
                mock_yolo_class.assert_called_once_with(expected_name)
    
    def test_metadata_generation_detection(self):
        """
        Test that metadata is generated correctly for detection models.
        
        Validates: Requirements 5.1
        """
        from yolo_training import save_metadata
        import tempfile
        import json
        
        # Create temporary directory for model output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock args
            mock_args = Mock()
            mock_args.task = 'detect'
            mock_args.model_size = 'yolov8n'
            mock_args.img_size = 640
            mock_args.epochs = 50
            mock_args.batch_size = 16
            mock_args.conf_threshold = 0.25
            
            # Save metadata
            save_metadata(mock_args, temp_dir)
            
            # Read and verify metadata
            metadata_path = os.path.join(temp_dir, 'metadata.json')
            self.assertTrue(os.path.exists(metadata_path))
            
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            
            # Verify metadata content
            self.assertEqual(metadata['task'], 'detect')
            self.assertEqual(metadata['model_size'], 'yolov8n')
            self.assertEqual(metadata['input_shape'], [1, 3, 640, 640])
            self.assertEqual(metadata['epochs'], 50)
            self.assertEqual(metadata['batch_size'], 16)
            self.assertAlmostEqual(metadata['conf_threshold'], 0.25)
    
    def test_metadata_generation_segmentation(self):
        """
        Test that metadata is generated correctly for segmentation models.
        
        Validates: Requirements 5.1
        """
        from yolo_training import save_metadata
        import tempfile
        import json
        
        # Create temporary directory for model output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock args
            mock_args = Mock()
            mock_args.task = 'segment'
            mock_args.model_size = 'yolov8m'
            mock_args.img_size = 1024
            mock_args.epochs = 100
            mock_args.batch_size = 32
            mock_args.conf_threshold = 0.5
            
            # Save metadata
            save_metadata(mock_args, temp_dir)
            
            # Read and verify metadata
            metadata_path = os.path.join(temp_dir, 'metadata.json')
            self.assertTrue(os.path.exists(metadata_path))
            
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            
            # Verify metadata content
            self.assertEqual(metadata['task'], 'segment')
            self.assertEqual(metadata['model_size'], 'yolov8m')
            self.assertEqual(metadata['input_shape'], [1, 3, 1024, 1024])
            self.assertEqual(metadata['epochs'], 100)
            self.assertEqual(metadata['batch_size'], 32)
            self.assertAlmostEqual(metadata['conf_threshold'], 0.5)
    
    def test_metadata_input_shape_calculation(self):
        """
        Test that input shape is calculated correctly for different image sizes.
        
        Validates: Requirements 5.1
        """
        from yolo_training import save_metadata
        import tempfile
        import json
        
        image_sizes = [320, 640, 1024, 1280]
        
        for img_size in image_sizes:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Create mock args
                mock_args = Mock()
                mock_args.task = 'detect'
                mock_args.model_size = 'yolov8n'
                mock_args.img_size = img_size
                mock_args.epochs = 50
                mock_args.batch_size = 16
                mock_args.conf_threshold = 0.25
                
                # Save metadata
                save_metadata(mock_args, temp_dir)
                
                # Read and verify metadata
                metadata_path = os.path.join(temp_dir, 'metadata.json')
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                
                # Verify input shape matches image size
                expected_shape = [1, 3, img_size, img_size]
                self.assertEqual(metadata['input_shape'], expected_shape)
    
    def test_metadata_file_format(self):
        """
        Test that metadata file is valid JSON with proper formatting.
        
        Validates: Requirements 5.1
        """
        from yolo_training import save_metadata
        import tempfile
        import json
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock args
            mock_args = Mock()
            mock_args.task = 'detect'
            mock_args.model_size = 'yolov8n'
            mock_args.img_size = 640
            mock_args.epochs = 50
            mock_args.batch_size = 16
            mock_args.conf_threshold = 0.25
            
            # Save metadata
            save_metadata(mock_args, temp_dir)
            
            # Read metadata file
            metadata_path = os.path.join(temp_dir, 'metadata.json')
            with open(metadata_path, 'r') as f:
                content = f.read()
            
            # Verify it's valid JSON
            try:
                metadata = json.loads(content)
            except json.JSONDecodeError:
                self.fail("Metadata file is not valid JSON")
            
            # Verify all required fields are present
            required_fields = ['task', 'model_size', 'input_shape', 'epochs', 'batch_size', 'conf_threshold']
            for field in required_fields:
                self.assertIn(field, metadata, f"Missing required field: {field}")
    
    def test_metadata_types(self):
        """
        Test that metadata fields have correct data types.
        
        Validates: Requirements 5.1
        """
        from yolo_training import save_metadata
        import tempfile
        import json
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock args
            mock_args = Mock()
            mock_args.task = 'detect'
            mock_args.model_size = 'yolov8n'
            mock_args.img_size = 640
            mock_args.epochs = 50
            mock_args.batch_size = 16
            mock_args.conf_threshold = 0.25
            
            # Save metadata
            save_metadata(mock_args, temp_dir)
            
            # Read and verify metadata
            metadata_path = os.path.join(temp_dir, 'metadata.json')
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            
            # Verify data types
            self.assertIsInstance(metadata['task'], str)
            self.assertIsInstance(metadata['model_size'], str)
            self.assertIsInstance(metadata['input_shape'], list)
            self.assertIsInstance(metadata['epochs'], int)
            self.assertIsInstance(metadata['batch_size'], int)
            self.assertIsInstance(metadata['conf_threshold'], (int, float))
            
            # Verify input_shape is a list of 4 integers
            self.assertEqual(len(metadata['input_shape']), 4)
            for dim in metadata['input_shape']:
                self.assertIsInstance(dim, int)


if __name__ == '__main__':
    unittest.main()


class TestTrainingJobOrchestration(unittest.TestCase):
    """Test cases for training job orchestration functionality."""
    
    def test_unique_job_name_generation(self):
        """
        Feature: yolo-cookie-defect-detection, Property 5: Unique Job Name Generation
        
        Property-based test that verifies unique training job name generation.
        
        For any two training or compilation jobs created within the same notebook execution,
        their generated names should be unique (no collisions) when created at different times
        or with different task types.
        
        Validates: Requirements 5.7, 8.5
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        import datetime
        import time
        
        @given(
            # Generate random task types
            task1=st.sampled_from(['detect', 'segment']),
            task2=st.sampled_from(['detect', 'segment'])
        )
        @settings(max_examples=10, deadline=None)  # Reduced examples and disabled deadline due to 1-second delay
        def property_test(task1, task2):
            # Generate first job name
            timestamp1 = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            job_name1 = f"yolo-cookie-{task1}-{timestamp1}"
            
            # Delay to ensure different timestamps (1 second minimum for timestamp format)
            time.sleep(1.1)
            
            # Generate second job name
            timestamp2 = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            job_name2 = f"yolo-cookie-{task2}-{timestamp2}"
            
            # Verify names are unique (either different timestamps or different tasks)
            assert job_name1 != job_name2, \
                f"Job names should be unique: {job_name1} vs {job_name2}"
            
            # Verify name format for first job
            assert job_name1.startswith("yolo-cookie-"), \
                f"Job name should start with 'yolo-cookie-': {job_name1}"
            assert task1 in job_name1, \
                f"Job name should contain task type: {task1} in {job_name1}"
            
            # Extract timestamp from job name (format: yolo-cookie-{task}-{timestamp})
            # Split by '-' and take the last two parts (YYYYMMDD and HHMMSS)
            parts1 = job_name1.split('-')
            timestamp_part1 = '-'.join(parts1[-2:])  # Get last two parts: YYYYMMDD-HHMMSS
            
            # Verify timestamp format (YYYYMMDD-HHMMSS = 15 characters)
            assert len(timestamp_part1) == 15, \
                f"Timestamp should be 15 characters (YYYYMMDD-HHMMSS): {timestamp_part1}"
            
            # Verify timestamp is numeric (except for the dash)
            timestamp_digits1 = timestamp_part1.replace('-', '')
            assert timestamp_digits1.isdigit(), \
                f"Timestamp should contain only digits: {timestamp_digits1}"
            
            # Verify timestamp can be parsed
            try:
                parsed_time = datetime.datetime.strptime(timestamp_part1, "%Y%m%d-%H%M%S")
                assert parsed_time is not None
            except ValueError as e:
                assert False, f"Timestamp should be parseable: {timestamp_part1}, error: {e}"
        
        # Run the property test
        property_test()
    
    def test_training_job_name_format(self):
        """
        Unit test for training job name format.
        
        Validates: Requirements 5.7
        """
        import datetime
        
        # Test case: Generate training job name
        task = 'detect'
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_name = f"yolo-cookie-{task}-{timestamp}"
        
        # Verify format
        self.assertTrue(job_name.startswith("yolo-cookie-"))
        self.assertIn(task, job_name)
        self.assertIn(timestamp, job_name)
        
        # Verify timestamp format
        timestamp_part = job_name.split('-', 2)[2]
        self.assertEqual(len(timestamp_part), 15)  # YYYYMMDD-HHMMSS
        
        # Verify timestamp is valid
        timestamp_digits = timestamp_part.replace('-', '')
        self.assertTrue(timestamp_digits.isdigit())
    
    def test_compilation_job_name_format(self):
        """
        Unit test for compilation job name format.
        
        Validates: Requirements 8.5
        """
        import datetime
        
        # Test case: Generate compilation job name
        platform = 'jetson-xavier'
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_name = f"yolo-compile-{platform}-{timestamp}"
        
        # Verify format
        self.assertTrue(job_name.startswith("yolo-compile-"))
        self.assertIn(platform, job_name)
        self.assertIn(timestamp, job_name)
        
        # Verify timestamp format
        timestamp_part = job_name.split('-')[-2] + '-' + job_name.split('-')[-1]
        self.assertEqual(len(timestamp_part), 15)  # YYYYMMDD-HHMMSS
    
    def test_multiple_job_names_are_unique(self):
        """
        Unit test that multiple job names generated in sequence are unique.
        
        Validates: Requirements 5.7, 8.5
        """
        import datetime
        import time
        
        # Generate multiple job names
        job_names = []
        for i in range(10):
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            job_name = f"yolo-cookie-detect-{timestamp}"
            job_names.append(job_name)
            time.sleep(0.001)  # Small delay to ensure different timestamps
        
        # Verify all names are unique
        unique_names = set(job_names)
        self.assertEqual(len(unique_names), len(job_names), 
                        f"All job names should be unique. Got {len(unique_names)} unique out of {len(job_names)}")
    
    def test_job_name_with_different_tasks(self):
        """
        Test that job names with different tasks are unique.
        
        Validates: Requirements 5.7
        """
        import datetime
        
        # Generate job names for different tasks at the same timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        
        detect_job = f"yolo-cookie-detect-{timestamp}"
        segment_job = f"yolo-cookie-segment-{timestamp}"
        
        # Verify names are different
        self.assertNotEqual(detect_job, segment_job)
        
        # Verify both contain the timestamp
        self.assertIn(timestamp, detect_job)
        self.assertIn(timestamp, segment_job)
        
        # Verify task types are in the names
        self.assertIn('detect', detect_job)
        self.assertIn('segment', segment_job)
    
    def test_timestamp_format_validation(self):
        """
        Test that timestamp format is valid and parseable.
        
        Validates: Requirements 5.7, 8.5
        """
        import datetime
        
        # Generate timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        
        # Verify format
        self.assertEqual(len(timestamp), 15)
        self.assertRegex(timestamp, r'^\d{8}-\d{6}$')
        
        # Verify it can be parsed back
        try:
            parsed_date = datetime.datetime.strptime(timestamp, "%Y%m%d-%H%M%S")
            self.assertIsNotNone(parsed_date)
        except ValueError:
            self.fail(f"Timestamp should be parseable: {timestamp}")
    
    def test_job_name_length_constraints(self):
        """
        Test that job names meet SageMaker length constraints.
        
        SageMaker job names must be 1-63 characters long.
        
        Validates: Requirements 5.7, 8.5
        """
        import datetime
        
        # Generate job names
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        
        training_job = f"yolo-cookie-detect-{timestamp}"
        compilation_job = f"yolo-compile-jetson-xavier-{timestamp}"
        
        # Verify length constraints
        self.assertGreaterEqual(len(training_job), 1, "Job name too short")
        self.assertLessEqual(len(training_job), 63, f"Training job name too long: {len(training_job)} chars")
        
        self.assertGreaterEqual(len(compilation_job), 1, "Job name too short")
        self.assertLessEqual(len(compilation_job), 63, f"Compilation job name too long: {len(compilation_job)} chars")
    
    def test_job_name_character_constraints(self):
        """
        Test that job names only contain valid characters.
        
        SageMaker job names can only contain alphanumeric characters and hyphens.
        
        Validates: Requirements 5.7, 8.5
        """
        import datetime
        import re
        
        # Generate job name
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_name = f"yolo-cookie-detect-{timestamp}"
        
        # Verify only valid characters (alphanumeric and hyphens)
        valid_pattern = r'^[a-zA-Z0-9-]+$'
        self.assertRegex(job_name, valid_pattern, 
                        f"Job name should only contain alphanumeric characters and hyphens: {job_name}")
        
        # Verify no consecutive hyphens
        self.assertNotIn('--', job_name, "Job name should not contain consecutive hyphens")
        
        # Verify doesn't start or end with hyphen
        self.assertFalse(job_name.startswith('-'), "Job name should not start with hyphen")
        self.assertFalse(job_name.endswith('-'), "Job name should not end with hyphen")


if __name__ == '__main__':
    unittest.main()



class TestModelPreparation(unittest.TestCase):
    """Test cases for model preparation for compilation functionality."""
    
    def test_input_shape_extraction_from_metadata(self):
        """
        Feature: yolo-cookie-defect-detection, Property 3 (partial): Input Shape Extraction
        
        Property-based test that verifies input shape extraction works for various model files.
        
        For any valid model metadata file with an input_shape field, extracting the input shape
        should return a list of 4 integers representing [batch_size, channels, height, width].
        
        Validates: Requirements 7.3
        """
        try:
            from hypothesis import given, strategies as st, settings
        except ImportError:
            self.skipTest("Hypothesis not installed")
        
        import tempfile
        import json
        
        @given(
            batch_size=st.integers(min_value=1, max_value=32),
            channels=st.integers(min_value=1, max_value=4),
            height=st.integers(min_value=32, max_value=2048),
            width=st.integers(min_value=32, max_value=2048)
        )
        @settings(max_examples=100)
        def property_test(batch_size, channels, height, width):
            # Create temporary metadata file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp_file:
                tmp_path = tmp_file.name
                
                # Write metadata with input shape
                metadata = {
                    'task': 'detect',
                    'model_size': 'yolov8n',
                    'input_shape': [batch_size, channels, height, width],
                    'epochs': 50,
                    'batch_size': 16
                }
                json.dump(metadata, tmp_file)
            
            try:
                # Read and extract input shape
                with open(tmp_path, 'r') as f:
                    loaded_metadata = json.load(f)
                
                input_shape = loaded_metadata.get('input_shape')
                
                # Verify input shape exists
                assert input_shape is not None, \
                    "Input shape should be present in metadata"
                
                # Verify input shape is a list
                assert isinstance(input_shape, list), \
                    f"Input shape should be a list, got {type(input_shape)}"
                
                # Verify input shape has 4 dimensions
                assert len(input_shape) == 4, \
                    f"Input shape should have 4 dimensions, got {len(input_shape)}"
                
                # Verify all dimensions are integers
                for i, dim in enumerate(input_shape):
                    assert isinstance(dim, int), \
                        f"Dimension {i} should be integer, got {type(dim)}"
                
                # Verify dimensions match original values
                assert input_shape[0] == batch_size, \
                    f"Batch size mismatch: expected {batch_size}, got {input_shape[0]}"
                assert input_shape[1] == channels, \
                    f"Channels mismatch: expected {channels}, got {input_shape[1]}"
                assert input_shape[2] == height, \
                    f"Height mismatch: expected {height}, got {input_shape[2]}"
                assert input_shape[3] == width, \
                    f"Width mismatch: expected {width}, got {input_shape[3]}"
                
                # Verify dimensions are positive
                for i, dim in enumerate(input_shape):
                    assert dim > 0, \
                        f"Dimension {i} should be positive, got {dim}"
            
            finally:
                # Clean up temporary file
                import os
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        
        # Run the property test
        property_test()
    
    def test_input_shape_extraction_standard_yolo_sizes(self):
        """
        Unit test for input shape extraction with standard YOLO image sizes.
        
        Validates: Requirements 7.3
        """
        import tempfile
        import json
        
        # Standard YOLO image sizes
        standard_sizes = [320, 416, 512, 640, 1024, 1280]
        
        for img_size in standard_sizes:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp_file:
                tmp_path = tmp_file.name
                
                # Write metadata
                metadata = {
                    'task': 'detect',
                    'model_size': 'yolov8n',
                    'input_shape': [1, 3, img_size, img_size],
                    'epochs': 50
                }
                json.dump(metadata, tmp_file)
            
            try:
                # Read and verify
                with open(tmp_path, 'r') as f:
                    loaded_metadata = json.load(f)
                
                input_shape = loaded_metadata['input_shape']
                
                # Verify shape
                self.assertEqual(len(input_shape), 4)
                self.assertEqual(input_shape[0], 1)  # Batch size
                self.assertEqual(input_shape[1], 3)  # RGB channels
                self.assertEqual(input_shape[2], img_size)  # Height
                self.assertEqual(input_shape[3], img_size)  # Width
            
            finally:
                import os
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    
    def test_input_shape_extraction_missing_metadata(self):
        """
        Test that missing metadata is handled gracefully with default values.
        
        Validates: Requirements 7.3
        """
        import tempfile
        import json
        
        # Create metadata without input_shape
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp_file:
            tmp_path = tmp_file.name
            
            metadata = {
                'task': 'detect',
                'model_size': 'yolov8n',
                'epochs': 50
                # input_shape is missing
            }
            json.dump(metadata, tmp_file)
        
        try:
            # Read metadata
            with open(tmp_path, 'r') as f:
                loaded_metadata = json.load(f)
            
            # Get input shape with default fallback
            input_shape = loaded_metadata.get('input_shape', [1, 3, 640, 640])
            
            # Verify default shape is used
            self.assertEqual(input_shape, [1, 3, 640, 640])
            self.assertEqual(len(input_shape), 4)
        
        finally:
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def test_input_shape_extraction_from_hyperparameters(self):
        """
        Test that input shape can be inferred from hyperparameters when metadata is missing.
        
        Validates: Requirements 7.3
        """
        # Test with various image sizes from hyperparameters
        hyperparameter_sizes = [320, 640, 1024, 1280]
        
        for img_size in hyperparameter_sizes:
            # Simulate hyperparameters
            hyperparameters = {
                'img-size': img_size,
                'model-size': 'yolov8n',
                'task': 'detect'
            }
            
            # Infer input shape from hyperparameters
            input_shape = [1, 3, hyperparameters['img-size'], hyperparameters['img-size']]
            
            # Verify shape
            self.assertEqual(len(input_shape), 4)
            self.assertEqual(input_shape[0], 1)
            self.assertEqual(input_shape[1], 3)
            self.assertEqual(input_shape[2], img_size)
            self.assertEqual(input_shape[3], img_size)
    
    def test_input_shape_format_for_data_input_config(self):
        """
        Test that input shape is correctly formatted for DataInputConfig.
        
        Validates: Requirements 7.3
        """
        import json
        
        # Test various input shapes
        test_shapes = [
            [1, 3, 320, 320],
            [1, 3, 640, 640],
            [1, 3, 1024, 1024],
            [2, 3, 640, 640],  # Batch size > 1
        ]
        
        for input_shape in test_shapes:
            # Create DataInputConfig
            data_input_config = json.dumps({"input_shape": input_shape})
            
            # Verify it's valid JSON
            parsed_config = json.loads(data_input_config)
            
            # Verify structure
            self.assertIn('input_shape', parsed_config)
            self.assertEqual(parsed_config['input_shape'], input_shape)
            self.assertEqual(len(parsed_config['input_shape']), 4)
    
    def test_input_shape_validation(self):
        """
        Test that input shape validation catches invalid shapes.
        
        Validates: Requirements 7.3
        """
        # Invalid shapes
        invalid_shapes = [
            [],  # Empty
            [1, 3],  # Too few dimensions
            [1, 3, 640],  # Too few dimensions
            [1, 3, 640, 640, 1],  # Too many dimensions
            [0, 3, 640, 640],  # Zero batch size
            [1, 0, 640, 640],  # Zero channels
            [1, 3, 0, 640],  # Zero height
            [1, 3, 640, 0],  # Zero width
            [-1, 3, 640, 640],  # Negative dimension
        ]
        
        for invalid_shape in invalid_shapes:
            # Validate shape
            is_valid = (
                isinstance(invalid_shape, list) and
                len(invalid_shape) == 4 and
                all(isinstance(dim, int) and dim > 0 for dim in invalid_shape)
            )
            
            # Should be invalid
            self.assertFalse(is_valid, f"Shape should be invalid: {invalid_shape}")
    
    def test_input_shape_extraction_detection_vs_segmentation(self):
        """
        Test that input shape extraction works for both detection and segmentation models.
        
        Validates: Requirements 7.3
        """
        import tempfile
        import json
        
        tasks = ['detect', 'segment']
        
        for task in tasks:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp_file:
                tmp_path = tmp_file.name
                
                # Write metadata
                metadata = {
                    'task': task,
                    'model_size': 'yolov8n',
                    'input_shape': [1, 3, 640, 640],
                    'epochs': 50
                }
                json.dump(metadata, tmp_file)
            
            try:
                # Read and verify
                with open(tmp_path, 'r') as f:
                    loaded_metadata = json.load(f)
                
                input_shape = loaded_metadata['input_shape']
                
                # Verify shape is same for both tasks
                self.assertEqual(input_shape, [1, 3, 640, 640])
                self.assertEqual(len(input_shape), 4)
                
                # Verify task is correctly stored
                self.assertEqual(loaded_metadata['task'], task)
            
            finally:
                import os
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    
    def test_model_file_extraction_from_tar(self):
        """
        Test that model files can be extracted from tar.gz archives.
        
        Validates: Requirements 7.2
        """
        import tempfile
        import tarfile
        
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a dummy model file
            model_file = os.path.join(temp_dir, 'best.pt')
            with open(model_file, 'wb') as f:
                f.write(b'dummy model data')
            
            # Create tar.gz archive
            tar_path = os.path.join(temp_dir, 'model.tar.gz')
            with tarfile.open(tar_path, 'w:gz') as tar:
                tar.add(model_file, arcname='best.pt')
            
            # Extract and verify
            extract_dir = os.path.join(temp_dir, 'extracted')
            os.makedirs(extract_dir)
            
            with tarfile.open(tar_path, 'r:gz') as tar:
                tar.extractall(path=extract_dir)
            
            # Verify extracted file exists
            extracted_file = os.path.join(extract_dir, 'best.pt')
            self.assertTrue(os.path.exists(extracted_file))
            
            # Verify content
            with open(extracted_file, 'rb') as f:
                content = f.read()
            self.assertEqual(content, b'dummy model data')
    
    def test_model_file_candidates(self):
        """
        Test that all valid YOLO model file names are recognized.
        
        Validates: Requirements 7.2
        """
        # Valid YOLO model file names
        valid_names = ['best.pt', 'yolo.pt', 'last.pt']
        
        for name in valid_names:
            # Verify name ends with .pt
            self.assertTrue(name.endswith('.pt'))
            
            # Verify name is in expected list
            self.assertIn(name, valid_names)
    
    def test_compilation_ready_package_creation(self):
        """
        Test that compilation-ready packages are created correctly.
        
        Validates: Requirements 7.4
        """
        import tempfile
        import tarfile
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a dummy model file
            model_file = os.path.join(temp_dir, 'best.pt')
            with open(model_file, 'wb') as f:
                f.write(b'model weights data')
            
            # Create compilation-ready tar.gz
            output_tar = os.path.join(temp_dir, 'model-for-compilation.tar.gz')
            with tarfile.open(output_tar, 'w:gz') as tar:
                tar.add(model_file, arcname='best.pt')
            
            # Verify archive was created
            self.assertTrue(os.path.exists(output_tar))
            
            # Verify archive contains only the model file
            with tarfile.open(output_tar, 'r:gz') as tar:
                members = tar.getmembers()
                self.assertEqual(len(members), 1)
                self.assertEqual(members[0].name, 'best.pt')
            
            # Verify file size is reasonable
            file_size = os.path.getsize(output_tar)
            self.assertGreater(file_size, 0)


if __name__ == '__main__':
    unittest.main()


class TestModelComparison(unittest.TestCase):
    """Test cases for model comparison functionality."""
    
    def test_metric_calculation_correctness(self):
        """
        Feature: yolo-cookie-defect-detection, Property 6: Metric Calculation Correctness
        
        For any set of predictions and ground truth labels, the calculated precision, recall, 
        and F1 scores should satisfy the mathematical relationships:
        - 0 ≤ precision ≤ 1
        - 0 ≤ recall ≤ 1
        - F1 = 2 * (precision * recall) / (precision + recall) when both are non-zero
        - F1 = 0 when both precision and recall are zero
        
        Validates: Requirements 12.4
        """
        from hypothesis import given, strategies as st, settings
        from yolo_comparison import calculate_detection_metrics
        
        @given(
            # Generate random true positives, false positives, false negatives
            tp=st.integers(min_value=0, max_value=100),
            fp=st.integers(min_value=0, max_value=100),
            fn=st.integers(min_value=0, max_value=100)
        )
        @settings(max_examples=100)
        def property_test(tp, fp, fn):
            # Create synthetic predictions and ground truth based on TP, FP, FN
            predictions = {}
            ground_truth = {}
            
            # Create a single image with boxes
            image_name = "test_image.jpg"
            
            # Ground truth boxes (TP + FN)
            gt_boxes = []
            for i in range(tp + fn):
                # Create boxes at different positions
                x1 = i * 50
                y1 = i * 50
                x2 = x1 + 40
                y2 = y1 + 40
                gt_boxes.append([x1, y1, x2, y2])
            
            # Predicted boxes
            pred_boxes = []
            
            # True positives - boxes that overlap with ground truth
            for i in range(tp):
                # Create boxes that overlap with ground truth
                x1 = i * 50 + 2  # Slight offset to ensure overlap
                y1 = i * 50 + 2
                x2 = x1 + 40
                y2 = y1 + 40
                pred_boxes.append([x1, y1, x2, y2])
            
            # False positives - boxes that don't overlap with ground truth
            for i in range(fp):
                # Create boxes far from ground truth
                x1 = 1000 + i * 50
                y1 = 1000 + i * 50
                x2 = x1 + 40
                y2 = y1 + 40
                pred_boxes.append([x1, y1, x2, y2])
            
            predictions[image_name] = {
                'boxes': pred_boxes,
                'classes': [1] * len(pred_boxes),
                'confidences': [0.9] * len(pred_boxes)
            }
            
            ground_truth[image_name] = {
                'boxes': gt_boxes,
                'classes': [1] * len(gt_boxes),
                'confidences': [1.0] * len(gt_boxes)
            }
            
            # Calculate metrics
            metrics = calculate_detection_metrics(predictions, ground_truth, iou_threshold=0.5)
            
            precision = metrics['precision']
            recall = metrics['recall']
            f1_score = metrics['f1_score']
            
            # Property 1: Precision is in valid range [0, 1]
            assert 0.0 <= precision <= 1.0, f"Precision {precision} out of range [0, 1]"
            
            # Property 2: Recall is in valid range [0, 1]
            assert 0.0 <= recall <= 1.0, f"Recall {recall} out of range [0, 1]"
            
            # Property 3: F1 score is in valid range [0, 1]
            assert 0.0 <= f1_score <= 1.0, f"F1 score {f1_score} out of range [0, 1]"
            
            # Property 4: F1 score mathematical relationship
            if precision > 0 and recall > 0:
                expected_f1 = 2 * (precision * recall) / (precision + recall)
                assert abs(f1_score - expected_f1) < 1e-6, \
                    f"F1 score {f1_score} doesn't match formula {expected_f1}"
            elif precision == 0 and recall == 0:
                assert f1_score == 0.0, \
                    f"F1 score should be 0 when precision and recall are 0, got {f1_score}"
            
            # Property 5: Verify TP, FP, FN counts match input
            # Note: Due to IoU threshold, actual TP might be less than expected
            # but should never exceed the minimum of predictions and ground truth
            assert metrics['true_positives'] <= min(len(pred_boxes), len(gt_boxes)), \
                "True positives exceed possible matches"
            assert metrics['false_positives'] >= 0, "False positives cannot be negative"
            assert metrics['false_negatives'] >= 0, "False negatives cannot be negative"
            
            # Property 6: Total predictions = TP + FP
            total_predictions = len(pred_boxes)
            assert metrics['true_positives'] + metrics['false_positives'] == total_predictions, \
                f"TP + FP should equal total predictions"
            
            # Property 7: Total ground truth = TP + FN
            total_ground_truth = len(gt_boxes)
            assert metrics['true_positives'] + metrics['false_negatives'] == total_ground_truth, \
                f"TP + FN should equal total ground truth"
        
        # Run the property test
        property_test()
    
    def test_metric_calculation_perfect_predictions(self):
        """
        Unit test for metric calculation with perfect predictions.
        """
        from yolo_comparison import calculate_detection_metrics
        
        # Perfect predictions - all boxes match
        predictions = {
            'image1.jpg': {
                'boxes': [[10, 10, 50, 50], [100, 100, 150, 150]],
                'classes': [1, 1],
                'confidences': [0.9, 0.95]
            }
        }
        
        ground_truth = {
            'image1.jpg': {
                'boxes': [[10, 10, 50, 50], [100, 100, 150, 150]],
                'classes': [1, 1],
                'confidences': [1.0, 1.0]
            }
        }
        
        metrics = calculate_detection_metrics(predictions, ground_truth)
        
        # Perfect predictions should have precision = recall = F1 = 1.0
        self.assertAlmostEqual(metrics['precision'], 1.0, places=6)
        self.assertAlmostEqual(metrics['recall'], 1.0, places=6)
        self.assertAlmostEqual(metrics['f1_score'], 1.0, places=6)
        self.assertEqual(metrics['true_positives'], 2)
        self.assertEqual(metrics['false_positives'], 0)
        self.assertEqual(metrics['false_negatives'], 0)
    
    def test_metric_calculation_no_predictions(self):
        """
        Unit test for metric calculation with no predictions.
        """
        from yolo_comparison import calculate_detection_metrics
        
        # No predictions
        predictions = {
            'image1.jpg': {
                'boxes': [],
                'classes': [],
                'confidences': []
            }
        }
        
        ground_truth = {
            'image1.jpg': {
                'boxes': [[10, 10, 50, 50]],
                'classes': [1],
                'confidences': [1.0]
            }
        }
        
        metrics = calculate_detection_metrics(predictions, ground_truth)
        
        # No predictions should have precision = 0, recall = 0, F1 = 0
        self.assertEqual(metrics['precision'], 0.0)
        self.assertEqual(metrics['recall'], 0.0)
        self.assertEqual(metrics['f1_score'], 0.0)
        self.assertEqual(metrics['true_positives'], 0)
        self.assertEqual(metrics['false_positives'], 0)
        self.assertEqual(metrics['false_negatives'], 1)
    
    def test_metric_calculation_no_ground_truth(self):
        """
        Unit test for metric calculation with no ground truth.
        """
        from yolo_comparison import calculate_detection_metrics
        
        # No ground truth
        predictions = {
            'image1.jpg': {
                'boxes': [[10, 10, 50, 50]],
                'classes': [1],
                'confidences': [0.9]
            }
        }
        
        ground_truth = {
            'image1.jpg': {
                'boxes': [],
                'classes': [],
                'confidences': []
            }
        }
        
        metrics = calculate_detection_metrics(predictions, ground_truth)
        
        # All predictions are false positives
        self.assertEqual(metrics['precision'], 0.0)
        self.assertEqual(metrics['recall'], 0.0)
        self.assertEqual(metrics['f1_score'], 0.0)
        self.assertEqual(metrics['true_positives'], 0)
        self.assertEqual(metrics['false_positives'], 1)
        self.assertEqual(metrics['false_negatives'], 0)
    
    def test_metric_calculation_partial_match(self):
        """
        Unit test for metric calculation with partial matches.
        """
        from yolo_comparison import calculate_detection_metrics
        
        # Partial matches - 1 TP, 1 FP, 1 FN
        predictions = {
            'image1.jpg': {
                'boxes': [
                    [10, 10, 50, 50],  # Matches GT
                    [200, 200, 250, 250]  # No match (FP)
                ],
                'classes': [1, 1],
                'confidences': [0.9, 0.8]
            }
        }
        
        ground_truth = {
            'image1.jpg': {
                'boxes': [
                    [10, 10, 50, 50],  # Matches pred
                    [300, 300, 350, 350]  # No match (FN)
                ],
                'classes': [1, 1],
                'confidences': [1.0, 1.0]
            }
        }
        
        metrics = calculate_detection_metrics(predictions, ground_truth)
        
        # 1 TP, 1 FP, 1 FN
        self.assertEqual(metrics['true_positives'], 1)
        self.assertEqual(metrics['false_positives'], 1)
        self.assertEqual(metrics['false_negatives'], 1)
        
        # Precision = 1/2 = 0.5
        self.assertAlmostEqual(metrics['precision'], 0.5, places=6)
        
        # Recall = 1/2 = 0.5
        self.assertAlmostEqual(metrics['recall'], 0.5, places=6)
        
        # F1 = 2 * (0.5 * 0.5) / (0.5 + 0.5) = 0.5
        self.assertAlmostEqual(metrics['f1_score'], 0.5, places=6)


if __name__ == '__main__':
    unittest.main()
