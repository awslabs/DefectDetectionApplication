#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Generate manifest files for Alien Dataset

Supports both classification and segmentation tasks.

Usage:
    # Classification manifest (no masks)
    python3 generate_manifest.py s3://bucket/aliens/ --task classification

    # Segmentation manifest (with pixel-level masks)
    python3 generate_manifest.py s3://bucket/aliens/ --task segmentation
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
    base_path = "aliens"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    
    logger.info("Generating classification manifest from %s", script_dir)
    
    # Process normal images
    normal_dir = script_dir / "normal"
    if normal_dir.exists():
        for img_file in sorted(normal_dir.iterdir()):
            if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                continue
            
            s3_image_uri = f"s3://{bucket_name}/{base_path}/normal/{img_file.name}"
            
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": "normal",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "anomaly-label": 0
            }
            
            manifest_lines.append(json.dumps(entry))
    else:
        logger.warning("Normal images directory not found: %s", normal_dir)
    
    # Process anomaly images
    anomaly_dir = script_dir / "anomaly"
    if anomaly_dir.exists():
        for img_file in sorted(anomaly_dir.iterdir()):
            if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                continue
            
            s3_image_uri = f"s3://{bucket_name}/{base_path}/anomaly/{img_file.name}"
            
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": "anomaly",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "anomaly-label": 1
            }
            
            manifest_lines.append(json.dumps(entry))
    else:
        logger.warning("Anomaly images directory not found: %s", anomaly_dir)
    
    if not manifest_lines:
        logger.error("No images found in normal/ or anomaly/ directories")
        return 0
    
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
    
    Note: Alien dataset typically doesn't have masks. This creates dummy masks
    for normal images and actual masks for anomalies if they exist.
    
    Args:
        bucket_name: S3 bucket name
        output_file: Output manifest filename
    """
    base_path = "aliens"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    mask_dir = script_dir / "masks"
    
    logger.info("Generating segmentation manifest from %s", script_dir)
    logger.info("Looking for masks in: %s", mask_dir)
    
    # Process normal images (use dummy mask)
    normal_dir = script_dir / "normal"
    if normal_dir.exists():
        for img_file in sorted(normal_dir.iterdir()):
            if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                continue
            
            s3_image_uri = f"s3://{bucket_name}/{base_path}/normal/{img_file.name}"
            
            # For normal images, use a dummy mask (all background)
            s3_mask_uri = f"s3://{bucket_name}/{base_path}/masks/dummy_normal_mask.png"
            
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": "normal",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "anomaly-label": 0,
                "anomaly-mask-ref-metadata": {
                    "internal-color-map": {
                        "0": {
                            "class-name": "BACKGROUND",
                            "hex-color": "#ffffff"
                        }
                    },
                    "job-name": "labeling-job/dummy-mask",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/semantic-segmentation"
                },
                "anomaly-mask-ref": s3_mask_uri
            }
            
            manifest_lines.append(json.dumps(entry))
    else:
        logger.warning("Normal images directory not found: %s", normal_dir)
    
    # Process anomaly images
    anomaly_dir = script_dir / "anomaly"
    if anomaly_dir.exists():
        for img_file in sorted(anomaly_dir.iterdir()):
            if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                continue
            
            s3_image_uri = f"s3://{bucket_name}/{base_path}/anomaly/{img_file.name}"
            
            # Check if corresponding mask exists
            mask_file = img_file.stem + ".png"
            mask_path = mask_dir / mask_file if mask_dir.exists() else None
            
            if mask_path and mask_path.exists():
                s3_mask_uri = f"s3://{bucket_name}/{base_path}/masks/{mask_file}"
                logger.debug("Using actual mask for %s", img_file.name)
            else:
                # Use dummy mask if no actual mask exists
                s3_mask_uri = f"s3://{bucket_name}/{base_path}/masks/dummy_anomaly_mask.png"
                logger.debug("Using dummy mask for %s", img_file.name)
            
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": "anomaly",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "anomaly-label": 1,
                "anomaly-mask-ref-metadata": {
                    "internal-color-map": {
                        "0": {
                            "class-name": "defect",
                            "hex-color": "#FF0000"
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
    else:
        logger.warning("Anomaly images directory not found: %s", anomaly_dir)
    
    if not manifest_lines:
        logger.error("No images found in normal/ or anomaly/ directories")
        return 0
    
    # Write manifest file
    output_path = script_dir / output_file
    
    with open(output_path, "w") as f:
        f.write("\n".join(manifest_lines))
    
    logger.info("Generated segmentation manifest with %d entries", len(manifest_lines))
    logger.info("Manifest saved to: %s", output_path)
    
    return len(manifest_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate Alien Dataset manifest files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Classification (images only)
  python3 generate_manifest.py s3://my-bucket/aliens/ --task classification

  # Segmentation (images with masks)
  python3 generate_manifest.py s3://my-bucket/aliens/ --task segmentation

  # Both
  python3 generate_manifest.py s3://my-bucket/aliens/ --task both
        """
    )
    parser.add_argument(
        "bucket_name",
        help="S3 bucket name (e.g., dda-alien-dataset-123456789)"
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
    print("Alien Dataset Manifest Generator")
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
