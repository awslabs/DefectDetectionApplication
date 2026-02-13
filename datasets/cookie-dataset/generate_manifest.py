#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Generate manifest files for Cookie Dataset

Supports both classification and segmentation tasks.

Usage:
    # Classification manifest (no masks)
    python3 generate_manifest.py s3://bucket/cookies/ --task classification

    # Segmentation manifest (with pixel-level masks)
    python3 generate_manifest.py s3://bucket/cookies/ --task segmentation
"""

import logging
import argparse
import sys
import json
import os
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def generate_classification_manifest(bucket_name, output_file="train_class.manifest"):
    """
    Generate classification manifest (images only, no masks).
    
    Args:
        bucket_name: S3 bucket name
        output_file: Output manifest filename
    """
    base_path = "cookies"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    training_dir = script_dir / "dataset-files" / "training-images"
    
    logger.info("Generating classification manifest from %s", training_dir)
    
    if not training_dir.exists():
        logger.error("Training images directory not found: %s", training_dir)
        return 0
    
    # Process training images
    for img_file in sorted(training_dir.iterdir()):
        if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
            continue
        
        # Determine class based on filename
        if "anomaly" in img_file.name.lower():
            class_label = "anomaly"
            anomaly_label = 1
        else:
            class_label = "normal"
            anomaly_label = 0
        
        # Build S3 URI
        s3_image_uri = f"s3://{bucket_name}/{base_path}/training-images/{img_file.name}"
        
        # Build manifest entry
        entry = {
            "source-ref": s3_image_uri,
            "anomaly-label-metadata": {
                "job-name": "anomaly-label",
                "class-name": class_label,
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/image-classification"
            },
            "anomaly-label": anomaly_label
        }
        
        manifest_lines.append(json.dumps(entry))
    
    # Write manifest file
    output_path = script_dir / output_file
    
    with open(output_path, "w") as f:
        f.write("\n".join(manifest_lines))
    
    logger.info("Generated classification manifest with %d entries", len(manifest_lines))
    logger.info("Manifest saved to: %s", output_path)
    
    return len(manifest_lines)


def generate_segmentation_manifest(bucket_name, output_file="train_segmentation.manifest"):
    """
    Generate segmentation manifest (images with pixel-level masks).
    
    Args:
        bucket_name: S3 bucket name
        output_file: Output manifest filename
    """
    base_path = "cookies"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    training_dir = script_dir / "dataset-files" / "training-images"
    mask_dir = script_dir / "dataset-files" / "mask-images"
    
    logger.info("Generating segmentation manifest from %s", training_dir)
    
    if not training_dir.exists() or not mask_dir.exists():
        logger.error("Training images or mask images directory not found")
        return 0
    
    # Process training images
    for img_file in sorted(training_dir.iterdir()):
        if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
            continue
        
        # Determine class based on filename
        if "anomaly" in img_file.name.lower():
            class_label = "anomaly"
            anomaly_label = 1
        else:
            class_label = "normal"
            anomaly_label = 0
        
        # Build S3 URIs
        s3_image_uri = f"s3://{bucket_name}/{base_path}/training-images/{img_file.name}"
        
        # Check if corresponding mask exists
        mask_file = img_file.stem + ".png"
        mask_path = mask_dir / mask_file
        
        if not mask_path.exists():
            logger.warning("Mask not found for %s, skipping", img_file.name)
            continue
        
        s3_mask_uri = f"s3://{bucket_name}/{base_path}/mask-images/{mask_file}"
        
        # Build manifest entry with segmentation metadata
        entry = {
            "source-ref": s3_image_uri,
            "anomaly-label-metadata": {
                "job-name": "anomaly-label",
                "class-name": class_label,
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/image-classification"
            },
            "anomaly-label": anomaly_label,
            "anomaly-mask-ref-metadata": {
                "internal-color-map": {
                    "0": {
                        "class-name": "cracked",
                        "hex-color": "#23A436"
                    }
                },
                "job-name": "labeling-job/object-mask-ref",
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/semantic-segmentation"
            },
            "anomaly-mask-ref": s3_mask_uri
        }
        
        manifest_lines.append(json.dumps(entry))
    
    # Write manifest file
    output_path = script_dir / output_file
    
    with open(output_path, "w") as f:
        f.write("\n".join(manifest_lines))
    
    logger.info("Generated segmentation manifest with %d entries", len(manifest_lines))
    logger.info("Manifest saved to: %s", output_path)
    
    return len(manifest_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate Cookie Dataset manifest files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Classification (images only)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task classification

  # Segmentation (images with masks)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task segmentation

  # Both
  python3 generate_manifest.py s3://my-bucket/cookies/ --task both
        """
    )
    parser.add_argument(
        "bucket_name",
        help="S3 bucket name (e.g., dda-cookie-dataset-123456789)"
    )
    parser.add_argument(
        "--task",
        choices=["classification", "segmentation", "both"],
        default="both",
        help="Type of manifest to generate (default: both)"
    )

    args = parser.parse_args()
    bucket_name = args.bucket_name

    print(f"\n{'='*60}")
    print("Cookie Dataset Manifest Generator")
    print(f"{'='*60}")
    print(f"Bucket: {bucket_name}")
    print(f"Task: {args.task}")
    print(f"{'='*60}\n")

    try:
        if args.task in ["classification", "both"]:
            count = generate_classification_manifest(bucket_name)
            if count == 0:
                logger.error("Failed to generate classification manifest")
                sys.exit(1)
            print(f"✓ Classification manifest: {count} entries\n")

        if args.task in ["segmentation", "both"]:
            count = generate_segmentation_manifest(bucket_name)
            if count == 0:
                logger.error("Failed to generate segmentation manifest")
                sys.exit(1)
            print(f"✓ Segmentation manifest: {count} entries\n")

        print(f"{'='*60}")
        print("✓ Manifest generation complete!")
        print(f"{'='*60}\n")

    except Exception as e:
        logger.error("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
