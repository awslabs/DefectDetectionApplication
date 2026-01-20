#!/usr/bin/env python3
"""
YOLOv8 Training Script for SageMaker

This script trains YOLOv8 models (detection or segmentation) on SageMaker.
It supports configurable hyperparameters and saves model artifacts with metadata.
"""

import argparse
import json
import os
from pathlib import Path


def parse_args():
    """Parse command-line arguments for training configuration."""
    parser = argparse.ArgumentParser(description='Train YOLOv8 model on SageMaker')
    
    parser.add_argument(
        '--model-size',
        type=str,
        default='yolov8n',
        choices=['yolov8n', 'yolov8s', 'yolov8m', 'yolov8l', 'yolov8x'],
        help='YOLOv8 model size (n=nano, s=small, m=medium, l=large, x=xlarge)'
    )
    
    parser.add_argument(
        '--task',
        type=str,
        default='detect',
        choices=['detect', 'segment'],
        help='Task type: detect for object detection, segment for instance segmentation'
    )
    
    parser.add_argument(
        '--epochs',
        type=int,
        default=50,
        help='Number of training epochs'
    )
    
    parser.add_argument(
        '--batch-size',
        type=int,
        default=16,
        help='Training batch size'
    )
    
    parser.add_argument(
        '--img-size',
        type=int,
        default=640,
        help='Input image size (height and width)'
    )
    
    parser.add_argument(
        '--conf-threshold',
        type=float,
        default=0.25,
        help='Confidence threshold for predictions'
    )
    
    # SageMaker-specific paths
    parser.add_argument(
        '--data-dir',
        type=str,
        default='/opt/ml/input/data/training',
        help='Path to training data directory'
    )
    
    parser.add_argument(
        '--model-dir',
        type=str,
        default='/opt/ml/model',
        help='Path to save trained model'
    )
    
    return parser.parse_args()


def load_model(model_size, task):
    """
    Load YOLOv8 model based on size and task type.
    
    Args:
        model_size: Model size (yolov8n, yolov8s, etc.)
        task: Task type ('detect' or 'segment')
    
    Returns:
        YOLO model instance
    """
    # Import here to avoid requiring ultralytics for testing
    from ultralytics import YOLO
    
    if task == 'segment':
        model_name = f'{model_size}-seg.pt'
    else:
        model_name = f'{model_size}.pt'
    
    print(f"Loading model: {model_name}")
    model = YOLO(model_name)
    return model


def train_model(model, args):
    """
    Train the YOLO model using Ultralytics API.
    
    Args:
        model: YOLO model instance
        args: Parsed command-line arguments
    
    Returns:
        Training results
    """
    data_yaml_path = os.path.join(args.data_dir, 'data.yaml')
    
    if not os.path.exists(data_yaml_path):
        raise FileNotFoundError(
            f"data.yaml not found at {data_yaml_path}. "
            f"Expected YOLO dataset structure with data.yaml configuration file."
        )
    
    print(f"Training configuration:")
    print(f"  Data: {data_yaml_path}")
    print(f"  Epochs: {args.epochs}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Image size: {args.img_size}")
    print(f"  Task: {args.task}")
    
    # Train the model
    results = model.train(
        data=data_yaml_path,
        epochs=args.epochs,
        batch=args.batch_size,
        imgsz=args.img_size,
        project=args.model_dir,
        name='yolo_training',
        conf=args.conf_threshold,
        save=True,
        save_period=-1,  # Save only best and last
        verbose=True
    )
    
    return results


def save_metadata(args, model_dir):
    """
    Save model metadata for compilation and deployment.
    
    Args:
        args: Parsed command-line arguments
        model_dir: Directory where model is saved
    """
    metadata = {
        'task': args.task,
        'model_size': args.model_size,
        'input_shape': [1, 3, args.img_size, args.img_size],
        'epochs': args.epochs,
        'batch_size': args.batch_size,
        'conf_threshold': args.conf_threshold
    }
    
    metadata_path = os.path.join(model_dir, 'metadata.json')
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)
    
    print(f"Metadata saved to {metadata_path}")
    print(f"Metadata: {json.dumps(metadata, indent=2)}")


def main():
    """Main training function."""
    args = parse_args()
    
    print("=" * 60)
    print("YOLOv8 Training Script for SageMaker")
    print("=" * 60)
    
    # Load model
    model = load_model(args.model_size, args.task)
    
    # Train model
    print("\nStarting training...")
    results = train_model(model, args)
    
    # Save metadata
    print("\nSaving metadata...")
    save_metadata(args, args.model_dir)
    
    print("\n" + "=" * 60)
    print("Training completed successfully!")
    print("=" * 60)
    
    # Print training results summary
    if hasattr(results, 'results_dict'):
        print("\nTraining Results:")
        for key, value in results.results_dict.items():
            print(f"  {key}: {value}")


if __name__ == '__main__':
    main()
