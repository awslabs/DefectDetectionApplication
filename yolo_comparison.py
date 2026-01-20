"""
YOLO Model Comparison Helper Functions

This module provides functions for comparing YOLO model results with Lookout for Vision baseline.
Supports both detection (bounding boxes) and segmentation (polygon masks) comparisons.
"""

import os
import cv2
import numpy as np
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
import matplotlib.pyplot as plt
import matplotlib.patches as patches


def load_test_images(test_dir: str) -> List[Tuple[str, np.ndarray]]:
    """
    Load test images from a directory.
    
    Args:
        test_dir: Path to directory containing test images
        
    Returns:
        List of tuples (filename, image_array) for each loaded image
        
    Raises:
        FileNotFoundError: If test_dir doesn't exist
        ValueError: If no valid images found in directory
    """
    if not os.path.exists(test_dir):
        raise FileNotFoundError(
            f"Test directory not found: {test_dir}\n"
            f"Expected location: {os.path.abspath(test_dir)}"
        )
    
    # Supported image extensions
    valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp'}
    
    # Load all images from directory
    images = []
    test_path = Path(test_dir)
    
    for img_file in sorted(test_path.iterdir()):
        if img_file.suffix.lower() in valid_extensions:
            # Read image
            img = cv2.imread(str(img_file))
            
            if img is not None:
                # Convert BGR to RGB for display
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                images.append((img_file.name, img_rgb))
            else:
                print(f"Warning: Failed to load image: {img_file.name}")
    
    if not images:
        raise ValueError(
            f"No valid images found in directory: {test_dir}\n"
            f"Supported formats: {', '.join(valid_extensions)}"
        )
    
    return images


def run_yolo_inference(
    model_path: str,
    images: List[Tuple[str, np.ndarray]],
    task: str = 'detect',
    conf_threshold: float = 0.25
) -> Dict[str, Any]:
    """
    Run YOLO inference on a list of images.
    
    Args:
        model_path: Path to YOLO model file (.pt)
        images: List of (filename, image_array) tuples
        task: Task type - 'detect' or 'segment'
        conf_threshold: Confidence threshold for predictions
        
    Returns:
        Dictionary with predictions for each image:
        {
            'image_filename': {
                'boxes': [[x1, y1, x2, y2], ...],  # For detection
                'classes': [class_id, ...],
                'confidences': [conf, ...],
                'masks': [mask_array, ...]  # For segmentation
            }
        }
        
    Raises:
        FileNotFoundError: If model_path doesn't exist
        ValueError: If task is not 'detect' or 'segment'
    """
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"Model file not found: {model_path}\n"
            f"Expected location: {os.path.abspath(model_path)}"
        )
    
    if task not in ['detect', 'segment']:
        raise ValueError(
            f"Invalid task: {task}\n"
            f"Supported tasks: 'detect', 'segment'"
        )
    
    try:
        from ultralytics import YOLO
    except ImportError:
        raise ImportError(
            "Ultralytics YOLO library not found.\n"
            "Install with: pip install ultralytics"
        )
    
    # Load YOLO model
    model = YOLO(model_path)
    
    # Run inference on all images
    predictions = {}
    
    for filename, img in images:
        # Run prediction
        results = model.predict(
            img,
            conf=conf_threshold,
            verbose=False
        )
        
        # Extract predictions from results
        result = results[0]  # Single image result
        
        pred_dict = {
            'boxes': [],
            'classes': [],
            'confidences': []
        }
        
        # Extract bounding boxes and classes
        if len(result.boxes) > 0:
            boxes = result.boxes.xyxy.cpu().numpy()  # x1, y1, x2, y2 format
            classes = result.boxes.cls.cpu().numpy().astype(int)
            confidences = result.boxes.conf.cpu().numpy()
            
            pred_dict['boxes'] = boxes.tolist()
            pred_dict['classes'] = classes.tolist()
            pred_dict['confidences'] = confidences.tolist()
        
        # Extract segmentation masks if available
        if task == 'segment' and hasattr(result, 'masks') and result.masks is not None:
            masks = result.masks.data.cpu().numpy()
            pred_dict['masks'] = masks.tolist()
        
        predictions[filename] = pred_dict
    
    return predictions


def calculate_detection_metrics(
    predictions: Dict[str, Any],
    ground_truth: Dict[str, Any],
    iou_threshold: float = 0.5
) -> Dict[str, float]:
    """
    Calculate detection metrics (precision, recall, F1) for YOLO predictions.
    
    Args:
        predictions: Dictionary of predictions per image
        ground_truth: Dictionary of ground truth annotations per image
        iou_threshold: IoU threshold for considering a detection as correct
        
    Returns:
        Dictionary with metrics:
        {
            'precision': float,
            'recall': float,
            'f1_score': float,
            'true_positives': int,
            'false_positives': int,
            'false_negatives': int
        }
    """
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    
    # Process each image
    for filename in predictions.keys():
        pred = predictions.get(filename, {})
        gt = ground_truth.get(filename, {})
        
        pred_boxes = pred.get('boxes', [])
        gt_boxes = gt.get('boxes', [])
        
        # Track which ground truth boxes have been matched
        gt_matched = [False] * len(gt_boxes)
        
        # For each prediction, find best matching ground truth
        for pred_box in pred_boxes:
            best_iou = 0
            best_gt_idx = -1
            
            for gt_idx, gt_box in enumerate(gt_boxes):
                if gt_matched[gt_idx]:
                    continue
                
                # Calculate IoU
                iou = calculate_iou(pred_box, gt_box)
                
                if iou > best_iou:
                    best_iou = iou
                    best_gt_idx = gt_idx
            
            # Check if prediction matches ground truth
            if best_iou >= iou_threshold and best_gt_idx >= 0:
                true_positives += 1
                gt_matched[best_gt_idx] = True
            else:
                false_positives += 1
        
        # Count unmatched ground truth boxes as false negatives
        false_negatives += sum(1 for matched in gt_matched if not matched)
    
    # Calculate metrics
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else 0.0
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) > 0 else 0.0
    f1_score = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    
    return {
        'precision': precision,
        'recall': recall,
        'f1_score': f1_score,
        'true_positives': true_positives,
        'false_positives': false_positives,
        'false_negatives': false_negatives
    }


def calculate_iou(box1: List[float], box2: List[float]) -> float:
    """
    Calculate Intersection over Union (IoU) between two bounding boxes.
    
    Args:
        box1: Bounding box in format [x1, y1, x2, y2]
        box2: Bounding box in format [x1, y1, x2, y2]
        
    Returns:
        IoU value between 0 and 1
    """
    # Extract coordinates
    x1_1, y1_1, x2_1, y2_1 = box1
    x1_2, y1_2, x2_2, y2_2 = box2
    
    # Calculate intersection area
    x1_i = max(x1_1, x1_2)
    y1_i = max(y1_1, y1_2)
    x2_i = min(x2_1, x2_2)
    y2_i = min(y2_1, y2_2)
    
    if x2_i < x1_i or y2_i < y1_i:
        return 0.0
    
    intersection_area = (x2_i - x1_i) * (y2_i - y1_i)
    
    # Calculate union area
    box1_area = (x2_1 - x1_1) * (y2_1 - y1_1)
    box2_area = (x2_2 - x1_2) * (y2_2 - y1_2)
    union_area = box1_area + box2_area - intersection_area
    
    # Calculate IoU
    iou = intersection_area / union_area if union_area > 0 else 0.0
    
    return iou


def calculate_segmentation_metrics(
    pred_masks: List[np.ndarray],
    gt_masks: List[np.ndarray]
) -> Dict[str, float]:
    """
    Calculate segmentation metrics (IoU, pixel accuracy) for predicted masks.
    
    Args:
        pred_masks: List of predicted segmentation masks
        gt_masks: List of ground truth segmentation masks
        
    Returns:
        Dictionary with metrics:
        {
            'mean_iou': float,
            'pixel_accuracy': float
        }
    """
    if len(pred_masks) == 0 or len(gt_masks) == 0:
        return {
            'mean_iou': 0.0,
            'pixel_accuracy': 0.0
        }
    
    ious = []
    total_correct_pixels = 0
    total_pixels = 0
    
    # Calculate metrics for each mask pair
    for pred_mask, gt_mask in zip(pred_masks, gt_masks):
        # Ensure masks are binary
        pred_binary = (pred_mask > 0.5).astype(np.uint8)
        gt_binary = (gt_mask > 0.5).astype(np.uint8)
        
        # Calculate intersection and union
        intersection = np.logical_and(pred_binary, gt_binary).sum()
        union = np.logical_or(pred_binary, gt_binary).sum()
        
        # Calculate IoU
        iou = intersection / union if union > 0 else 0.0
        ious.append(iou)
        
        # Calculate pixel accuracy
        correct_pixels = (pred_binary == gt_binary).sum()
        total_correct_pixels += correct_pixels
        total_pixels += pred_binary.size
    
    # Calculate mean metrics
    mean_iou = np.mean(ious) if ious else 0.0
    pixel_accuracy = total_correct_pixels / total_pixels if total_pixels > 0 else 0.0
    
    return {
        'mean_iou': mean_iou,
        'pixel_accuracy': pixel_accuracy
    }


def visualize_detections(
    image: np.ndarray,
    boxes: List[List[float]],
    classes: List[int],
    confidences: List[float],
    class_names: Optional[List[str]] = None,
    title: str = "Detections"
) -> plt.Figure:
    """
    Visualize detection results by drawing bounding boxes on the image.
    
    Args:
        image: Input image (RGB format)
        boxes: List of bounding boxes in format [[x1, y1, x2, y2], ...]
        classes: List of class IDs for each box
        confidences: List of confidence scores for each box
        class_names: Optional list of class names (default: ['normal', 'anomaly'])
        title: Title for the plot
        
    Returns:
        Matplotlib figure with visualized detections
    """
    if class_names is None:
        class_names = ['normal', 'anomaly']
    
    # Create figure
    fig, ax = plt.subplots(1, figsize=(12, 8))
    ax.imshow(image)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.axis('off')
    
    # Define colors for each class
    colors = ['green', 'red', 'blue', 'yellow', 'purple']
    
    # Draw each bounding box
    for box, cls, conf in zip(boxes, classes, confidences):
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        
        # Select color based on class
        color = colors[cls % len(colors)]
        
        # Draw rectangle
        rect = patches.Rectangle(
            (x1, y1), width, height,
            linewidth=2,
            edgecolor=color,
            facecolor='none'
        )
        ax.add_patch(rect)
        
        # Add label with class name and confidence
        label = f"{class_names[cls]}: {conf:.2f}"
        ax.text(
            x1, y1 - 5,
            label,
            color='white',
            fontsize=10,
            fontweight='bold',
            bbox=dict(facecolor=color, alpha=0.7, edgecolor='none', pad=2)
        )
    
    plt.tight_layout()
    return fig


def visualize_segmentation(
    image: np.ndarray,
    mask: np.ndarray,
    class_id: int,
    class_names: Optional[List[str]] = None,
    title: str = "Segmentation"
) -> plt.Figure:
    """
    Visualize segmentation results by overlaying the mask on the image.
    
    Args:
        image: Input image (RGB format)
        mask: Segmentation mask (binary or probability map)
        class_id: Class ID for the mask
        class_names: Optional list of class names (default: ['normal', 'anomaly'])
        title: Title for the plot
        
    Returns:
        Matplotlib figure with visualized segmentation
    """
    if class_names is None:
        class_names = ['normal', 'anomaly']
    
    # Create figure
    fig, ax = plt.subplots(1, figsize=(12, 8))
    
    # Display image
    ax.imshow(image)
    
    # Overlay mask with transparency
    if mask is not None and mask.size > 0:
        # Ensure mask is binary
        binary_mask = (mask > 0.5).astype(np.uint8)
        
        # Create colored mask
        colored_mask = np.zeros((*binary_mask.shape, 4))
        
        # Set color based on class (red for anomaly, green for normal)
        if class_id == 1:  # Anomaly
            colored_mask[binary_mask == 1] = [1, 0, 0, 0.5]  # Red with 50% transparency
        else:  # Normal
            colored_mask[binary_mask == 1] = [0, 1, 0, 0.5]  # Green with 50% transparency
        
        ax.imshow(colored_mask)
    
    # Add title with class name
    class_name = class_names[class_id] if class_id < len(class_names) else f"Class {class_id}"
    ax.set_title(f"{title} - {class_name}", fontsize=14, fontweight='bold')
    ax.axis('off')
    
    plt.tight_layout()
    return fig


def create_comparison_table(
    yolo_metrics: Dict[str, float],
    lfv_metrics: Optional[Dict[str, float]] = None
) -> str:
    """
    Create a formatted comparison table showing metrics for YOLO and LFV models.
    
    Args:
        yolo_metrics: Dictionary of YOLO model metrics
        lfv_metrics: Optional dictionary of Lookout for Vision metrics
        
    Returns:
        Formatted string table for display
    """
    # Create table header
    if lfv_metrics:
        table = "| Metric              | YOLO Model | LFV Model  | Difference |\n"
        table += "|---------------------|------------|------------|------------|\n"
    else:
        table = "| Metric              | YOLO Model |\n"
        table += "|---------------------|------------|\n"
    
    # Add metrics rows
    metric_names = {
        'precision': 'Precision',
        'recall': 'Recall',
        'f1_score': 'F1 Score',
        'mean_iou': 'Mean IoU',
        'pixel_accuracy': 'Pixel Accuracy',
        'avg_inference_time_ms': 'Avg Inference Time (ms)'
    }
    
    for metric_key, metric_label in metric_names.items():
        if metric_key in yolo_metrics:
            yolo_value = yolo_metrics[metric_key]
            
            if lfv_metrics and metric_key in lfv_metrics:
                lfv_value = lfv_metrics[metric_key]
                diff = yolo_value - lfv_value
                
                # Format values
                if metric_key == 'avg_inference_time_ms':
                    table += f"| {metric_label:<19} | {yolo_value:>10.2f} | {lfv_value:>10.2f} | {diff:>+10.2f} |\n"
                else:
                    table += f"| {metric_label:<19} | {yolo_value:>10.4f} | {lfv_value:>10.4f} | {diff:>+10.4f} |\n"
            else:
                # YOLO only
                if metric_key == 'avg_inference_time_ms':
                    table += f"| {metric_label:<19} | {yolo_value:>10.2f} |\n"
                else:
                    table += f"| {metric_label:<19} | {yolo_value:>10.4f} |\n"
    
    return table
