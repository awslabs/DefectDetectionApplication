"""
YOLO Format Converter

This module provides functions to convert Lookout for Vision annotations
to YOLO format for both object detection and instance segmentation.

Functions:
    - extract_bounding_boxes: Extract bounding boxes from segmentation masks
    - normalize_coordinates: Normalize pixel coordinates to YOLO format [0, 1]
    - convert_to_yolo_format: Convert bounding box to YOLO detection format
    - extract_polygons: Extract polygon coordinates from segmentation masks
    - approximate_polygon: Simplify polygon using Douglas-Peucker algorithm
    - convert_to_yolo_segment_format: Convert polygon to YOLO segmentation format
    - read_manifest: Parse Lookout for Vision JSON Lines manifest
    - write_yolo_annotations: Write YOLO annotation files
    - create_data_yaml: Generate YOLO dataset configuration file
"""

import cv2
import numpy as np
import json
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import yaml


def extract_bounding_boxes(mask_path: str) -> List[Tuple[int, int, int, int]]:
    """
    Extract bounding boxes from a segmentation mask using OpenCV contours.
    
    This function loads a segmentation mask, applies binary thresholding to isolate
    defect regions, finds contours, and computes bounding rectangles for each contour.
    
    Args:
        mask_path: Path to the segmentation mask image file
        
    Returns:
        List of bounding boxes in format (x, y, width, height) where:
            - x, y: top-left corner coordinates in pixels
            - width, height: box dimensions in pixels
        Returns empty list if no contours are found.
        
    Raises:
        FileNotFoundError: If mask_path does not exist
        ValueError: If the image cannot be loaded or is invalid
        
    Algorithm:
        1. Load segmentation mask as grayscale image
        2. Apply binary threshold to isolate defect regions (threshold=127)
        3. Find contours in the binary mask
        4. For each contour, compute bounding rectangle
        5. Return list of bounding boxes
        
    Example:
        >>> boxes = extract_bounding_boxes('mask.png')
        >>> print(boxes)  # [(10, 20, 50, 60), (100, 150, 30, 40)]
    """
    # Load mask as grayscale
    mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    
    if mask is None:
        raise ValueError(
            f"Failed to load mask image: {mask_path}\\n"
            f"The file may not exist or may not be a valid image format."
        )
    
    # Apply binary threshold to isolate defect regions
    # Pixels > 127 become 255 (white), others become 0 (black)
    _, binary_mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    
    # Find contours in the binary mask
    contours, _ = cv2.findContours(
        binary_mask,
        cv2.RETR_EXTERNAL,  # Only external contours
        cv2.CHAIN_APPROX_SIMPLE  # Compress horizontal/vertical/diagonal segments
    )
    
    # Extract bounding boxes from contours
    bounding_boxes = []
    for contour in contours:
        # Get bounding rectangle for this contour
        x, y, w, h = cv2.boundingRect(contour)
        bounding_boxes.append((x, y, w, h))
    
    return bounding_boxes


def normalize_coordinates(
    bbox: Tuple[int, int, int, int],
    img_width: int,
    img_height: int
) -> Tuple[float, float, float, float]:
    """
    Normalize pixel coordinates to YOLO format [0, 1] range.
    
    Converts bounding box from pixel coordinates to normalized coordinates
    where all values are in the range [0, 1] relative to image dimensions.
    Also converts from (x, y, width, height) format to YOLO's
    (center_x, center_y, width, height) format.
    
    Args:
        bbox: Bounding box in format (x, y, width, height) in pixels
            - x, y: top-left corner coordinates
            - width, height: box dimensions
        img_width: Image width in pixels
        img_height: Image height in pixels
        
    Returns:
        Normalized bounding box in YOLO format (center_x, center_y, width, height)
        where all values are in range [0, 1]:
            - center_x: normalized x-coordinate of box center
            - center_y: normalized y-coordinate of box center
            - width: normalized box width
            - height: normalized box height
            
    Raises:
        ValueError: If img_width or img_height is <= 0
        
    Algorithm:
        1. Extract x, y, width, height from bbox
        2. Calculate center coordinates: center_x = x + width/2, center_y = y + height/2
        3. Normalize all values by dividing by image dimensions
        4. Return (center_x_norm, center_y_norm, width_norm, height_norm)
        
    Example:
        >>> bbox = (100, 200, 50, 60)  # x, y, w, h in pixels
        >>> normalized = normalize_coordinates(bbox, 640, 480)
        >>> print(normalized)  # (0.1953, 0.4792, 0.0781, 0.125)
    """
    if img_width <= 0 or img_height <= 0:
        raise ValueError(
            f"Image dimensions must be positive: "
            f"width={img_width}, height={img_height}"
        )
    
    # Extract bbox components
    x, y, width, height = bbox
    
    # Calculate center coordinates
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    
    # Normalize by image dimensions
    center_x_norm = center_x / img_width
    center_y_norm = center_y / img_height
    width_norm = width / img_width
    height_norm = height / img_height
    
    return (center_x_norm, center_y_norm, width_norm, height_norm)


def convert_to_yolo_format(
    bbox: Tuple[float, float, float, float],
    class_id: int
) -> str:
    """
    Convert normalized bounding box to YOLO detection annotation format.
    
    Creates a YOLO format annotation line with class ID and normalized coordinates.
    
    Args:
        bbox: Normalized bounding box (center_x, center_y, width, height)
            All values should be in range [0, 1]
        class_id: Integer class identifier (e.g., 0 for normal, 1 for anomaly)
        
    Returns:
        YOLO format annotation string: "class_id center_x center_y width height"
        Values are space-separated with 6 decimal places of precision.
        
    Format:
        <class_id> <center_x> <center_y> <width> <height>
        
    Example:
        >>> bbox = (0.5, 0.6, 0.1, 0.15)
        >>> annotation = convert_to_yolo_format(bbox, 1)
        >>> print(annotation)  # "1 0.500000 0.600000 0.100000 0.150000"
    """
    center_x, center_y, width, height = bbox
    
    # Format: class_id center_x center_y width height
    # Use 6 decimal places for precision
    return f"{class_id} {center_x:.6f} {center_y:.6f} {width:.6f} {height:.6f}"


def extract_polygons(mask_path: str) -> List[np.ndarray]:
    """
    Extract polygon coordinates from a segmentation mask using OpenCV contours.
    
    This function loads a segmentation mask, applies binary thresholding to isolate
    defect regions, finds contours, and returns the polygon coordinates for each contour.
    
    Args:
        mask_path: Path to the segmentation mask image file
        
    Returns:
        List of polygon contours as numpy arrays. Each array has shape (N, 1, 2)
        where N is the number of points in the polygon, and each point is (x, y).
        Returns empty list if no contours are found.
        
    Raises:
        FileNotFoundError: If mask_path does not exist
        ValueError: If the image cannot be loaded or is invalid
        
    Algorithm:
        1. Load segmentation mask as grayscale image
        2. Apply binary threshold to isolate defect regions (threshold=127)
        3. Find contours in the binary mask
        4. Return list of contour polygons
        
    Example:
        >>> polygons = extract_polygons('mask.png')
        >>> print(len(polygons))  # 2 (two defect regions)
        >>> print(polygons[0].shape)  # (25, 1, 2) - 25 points
    """
    # Load mask as grayscale
    mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    
    if mask is None:
        raise ValueError(
            f"Failed to load mask image: {mask_path}\n"
            f"The file may not exist or may not be a valid image format."
        )
    
    # Apply binary threshold to isolate defect regions
    # Pixels > 127 become 255 (white), others become 0 (black)
    _, binary_mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    
    # Find contours in the binary mask
    contours, _ = cv2.findContours(
        binary_mask,
        cv2.RETR_EXTERNAL,  # Only external contours
        cv2.CHAIN_APPROX_SIMPLE  # Compress horizontal/vertical/diagonal segments
    )
    
    return list(contours)


def approximate_polygon(contour: np.ndarray, epsilon: float = 0.01) -> np.ndarray:
    """
    Approximate a polygon using the Douglas-Peucker algorithm.
    
    This function simplifies a polygon by reducing the number of points while
    preserving the overall shape. The epsilon parameter controls the approximation
    accuracy - smaller values preserve more detail.
    
    Args:
        contour: Input contour as numpy array with shape (N, 1, 2)
        epsilon: Approximation accuracy parameter (default: 0.01)
            - Smaller values (e.g., 0.001) preserve more detail
            - Larger values (e.g., 0.05) create simpler polygons
            - Typically expressed as fraction of contour perimeter
            
    Returns:
        Approximated polygon as numpy array with shape (M, 1, 2) where M <= N
        
    Algorithm:
        Uses OpenCV's approxPolyDP which implements Douglas-Peucker algorithm:
        1. Calculate contour perimeter
        2. Set epsilon as fraction of perimeter (epsilon * perimeter)
        3. Recursively simplify the polygon by removing points that deviate
           less than epsilon from the line segment between their neighbors
           
    Example:
        >>> contour = np.array([[[10, 10]], [[20, 10]], [[20, 20]], [[10, 20]]])
        >>> simplified = approximate_polygon(contour, epsilon=0.01)
        >>> print(simplified.shape)  # (4, 1, 2) - rectangle preserved
    """
    # Calculate perimeter of the contour
    perimeter = cv2.arcLength(contour, closed=True)
    
    # Approximate polygon using Douglas-Peucker algorithm
    # epsilon is expressed as a fraction of the perimeter
    approx = cv2.approxPolyDP(contour, epsilon * perimeter, closed=True)
    
    return approx


def convert_to_yolo_segment_format(
    polygon: np.ndarray,
    class_id: int,
    img_width: int,
    img_height: int
) -> str:
    """
    Convert polygon coordinates to YOLO segmentation annotation format.
    
    Creates a YOLO segmentation format annotation line with class ID and normalized
    polygon coordinates. All coordinates are normalized to [0, 1] range.
    
    Args:
        polygon: Polygon contour as numpy array with shape (N, 1, 2)
            where N is the number of points and each point is (x, y) in pixels
        class_id: Integer class identifier (e.g., 0 for normal, 1 for anomaly)
        img_width: Image width in pixels (for normalization)
        img_height: Image height in pixels (for normalization)
        
    Returns:
        YOLO segmentation format annotation string:
        "class_id x1 y1 x2 y2 x3 y3 ... xn yn"
        All coordinates are normalized to [0, 1] range with 6 decimal places.
        
    Raises:
        ValueError: If img_width or img_height is <= 0
        
    Format:
        <class_id> <x1> <y1> <x2> <y2> <x3> <y3> ... <xn> <yn>
        
    Algorithm:
        1. Extract (x, y) coordinates from polygon array
        2. Normalize each coordinate by image dimensions
        3. Flatten to single line with class_id prefix
        
    Example:
        >>> polygon = np.array([[[100, 200]], [[150, 200]], [[150, 250]], [[100, 250]]])
        >>> annotation = convert_to_yolo_segment_format(polygon, 1, 640, 480)
        >>> print(annotation)
        # "1 0.156250 0.416667 0.234375 0.416667 0.234375 0.520833 0.156250 0.520833"
    """
    if img_width <= 0 or img_height <= 0:
        raise ValueError(
            f"Image dimensions must be positive: "
            f"width={img_width}, height={img_height}"
        )
    
    # Extract coordinates and normalize
    normalized_coords = []
    
    for point in polygon:
        # Extract x, y from shape (1, 2)
        x, y = point[0]
        
        # Normalize by image dimensions
        x_norm = x / img_width
        y_norm = y / img_height
        
        # Add to list
        normalized_coords.append(f"{x_norm:.6f}")
        normalized_coords.append(f"{y_norm:.6f}")
    
    # Format: class_id x1 y1 x2 y2 ... xn yn
    coords_str = " ".join(normalized_coords)
    return f"{class_id} {coords_str}"


def read_manifest(manifest_path: str) -> List[Dict[str, Any]]:
    """
    Parse Lookout for Vision JSON Lines manifest file.
    
    Reads a manifest file in JSON Lines format where each line is a separate
    JSON object containing image metadata and annotations.
    
    Args:
        manifest_path: Path to the manifest file (.json or .jsonl)
        
    Returns:
        List of dictionaries, one per line in the manifest file.
        Each dictionary contains the parsed JSON object from that line.
        
    Raises:
        FileNotFoundError: If manifest_path does not exist
        json.JSONDecodeError: If a line contains invalid JSON
        
    Format:
        Each line in the manifest should be a valid JSON object with fields like:
        - source-ref: S3 URI or local path to the image
        - anomaly-label: Classification label (0=normal, 1=anomaly)
        - anomaly-label-metadata: Additional metadata
        
    Example:
        >>> records = read_manifest('manifest.jsonl')
        >>> print(len(records))  # 63
        >>> print(records[0]['source-ref'])  # 's3://bucket/image.jpg'
    """
    records = []
    
    with open(manifest_path, 'r') as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                # Skip empty lines
                continue
            
            try:
                record = json.loads(line)
                records.append(record)
            except json.JSONDecodeError as e:
                raise json.JSONDecodeError(
                    f"Invalid JSON on line {line_num}: {e.msg}",
                    e.doc,
                    e.pos
                )
    
    return records


def write_yolo_annotations(
    annotations: Dict[str, List[str]],
    output_dir: str
) -> None:
    """
    Write YOLO annotation files to disk.
    
    Creates .txt annotation files for each image in the YOLO format.
    Each annotation file contains one line per object in the image.
    
    Args:
        annotations: Dictionary mapping image filenames to lists of annotation lines
            - Key: image filename (e.g., 'image001.jpg')
            - Value: list of YOLO format annotation strings
        output_dir: Directory where annotation files will be written
        
    Returns:
        None
        
    Side Effects:
        - Creates output_dir if it doesn't exist
        - Writes one .txt file per image in annotations
        - Each .txt file has the same base name as the image
        
    File Format:
        Each line in the .txt file:
        <class_id> <center_x> <center_y> <width> <height>
        
    Example:
        >>> annotations = {
        ...     'image001.jpg': ['1 0.5 0.6 0.1 0.15', '1 0.3 0.4 0.08 0.12'],
        ...     'image002.jpg': ['0 0.5 0.5 0.2 0.2']
        ... }
        >>> write_yolo_annotations(annotations, 'labels/train')
        # Creates: labels/train/image001.txt and labels/train/image002.txt
    """
    # Create output directory if it doesn't exist
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Write annotation file for each image
    for image_filename, annotation_lines in annotations.items():
        # Get base filename without extension
        base_name = Path(image_filename).stem
        
        # Create annotation filename
        annotation_filename = f"{base_name}.txt"
        annotation_path = output_path / annotation_filename
        
        # Write annotations to file
        with open(annotation_path, 'w') as f:
            for line in annotation_lines:
                f.write(line + '\\n')


def create_data_yaml(
    class_names: List[str],
    output_path: str,
    train_path: str = 'images/train',
    val_path: str = 'images/val'
) -> None:
    """
    Generate YOLO dataset configuration file (data.yaml).
    
    Creates a YAML configuration file that specifies dataset paths and class names
    for YOLO training.
    
    Args:
        class_names: List of class names in order (index = class ID)
            Example: ['normal', 'anomaly']
        output_path: Path where data.yaml will be written
        train_path: Relative path to training images (default: 'images/train')
        val_path: Relative path to validation images (default: 'images/val')
        
    Returns:
        None
        
    Side Effects:
        - Creates output_path file
        - Overwrites file if it already exists
        
    YAML Format:
        path: /opt/ml/input/data/training  # Dataset root (set at runtime)
        train: images/train  # Train images relative to path
        val: images/val      # Val images relative to path
        
        # Classes
        names:
          0: normal
          1: anomaly
          
    Example:
        >>> create_data_yaml(['normal', 'anomaly'], 'data.yaml')
        # Creates data.yaml with 2 classes
    """
    # Create configuration dictionary
    config = {
        'path': '/opt/ml/input/data/training',  # Will be set by SageMaker
        'train': train_path,
        'val': val_path,
        'names': {i: name for i, name in enumerate(class_names)}
    }
    
    # Write YAML file
    with open(output_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
