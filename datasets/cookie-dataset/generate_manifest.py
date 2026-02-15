#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Generate manifest files for Cookie Dataset

Supports both classification and segmentation tasks in Ground Truth or DDA format.

Usage:
    # Classification manifest in Ground Truth format (default - for testing transformation)
    python3 generate_manifest.py s3://bucket/cookies/ --task classification

    # Segmentation manifest in Ground Truth format (default - for testing transformation)
    python3 generate_manifest.py s3://bucket/cookies/ --task segmentation

    # Generate DDA format (ready for training without transformation)
    python3 generate_manifest.py s3://bucket/cookies/ --task both --format dda
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


def generate_classification_manifest(bucket_name, output_file="train_class.manifest", format_type="ground-truth"):
    """
    Generate classification manifest (images only, no masks).
    
    Args:
        bucket_name: S3 bucket name or S3 URI (e.g., "my-bucket" or "s3://my-bucket/cookies/")
        output_file: Output manifest filename
        format_type: "ground-truth" for Ground Truth format, "dda" for DDA format
    """
    # Normalize bucket name - remove s3:// prefix and trailing slashes
    normalized_bucket = bucket_name.replace("s3://", "").rstrip("/")
    
    # Extract just the bucket name (first part before any /)
    bucket_only = normalized_bucket.split("/")[0]
    
    base_path = "cookies/dataset-files"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    training_dir = script_dir / "dataset-files" / "training-images"
    manifest_dir = script_dir / "dataset-files" / "manifests"
    
    logger.info("Generating classification manifest from %s (format: %s)", training_dir, format_type)
    logger.info("Using bucket: %s", bucket_only)
    
    if not training_dir.exists():
        logger.error("Training images directory not found: %s", training_dir)
        return 0
    
    # Create manifests directory if it doesn't exist
    manifest_dir.mkdir(parents=True, exist_ok=True)
    
    # Process training images
    for img_file in sorted(training_dir.iterdir()):
        if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
            continue
        
        # Determine class based on filename
        if "anomaly" in img_file.name.lower():
            class_label = "anomaly"
            label_value = 1
        else:
            class_label = "normal"
            label_value = 0
        
        # Build S3 URI with normalized bucket name
        s3_image_uri = f"s3://{bucket_only}/{base_path}/training-images/{img_file.name}"
        
        # Build manifest entry based on format type
        if format_type == "ground-truth":
            # Ground Truth format with job-specific attribute names
            entry = {
                "source-ref": s3_image_uri,
                "cookie-classification": label_value,
                "cookie-classification-metadata": {
                    "job-name": "cookie-classification",
                    "class-name": class_label,
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                }
            }
        else:  # dda format
            # DDA format with standardized attribute names
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label": label_value,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": class_label,
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                }
            }
        
        manifest_lines.append(json.dumps(entry))
    
    # Write manifest file to manifests directory
    output_path = manifest_dir / output_file
    
    with open(output_path, "w") as f:
        f.write("\n".join(manifest_lines))
    
    logger.info("Generated classification manifest with %d entries", len(manifest_lines))
    logger.info("Manifest saved to: %s", output_path)
    
    return len(manifest_lines)


def generate_segmentation_manifest(bucket_name, output_file="train_segmentation.manifest", format_type="ground-truth"):
    """
    Generate segmentation manifest (images with pixel-level masks).
    
    For normal images without masks, uses dummy_anomaly_mask.png as placeholder.
    
    Args:
        bucket_name: S3 bucket name or S3 URI (e.g., "my-bucket" or "s3://my-bucket/cookies/")
        output_file: Output manifest filename
        format_type: "ground-truth" for Ground Truth format, "dda" for DDA format
    """
    # Normalize bucket name - remove s3:// prefix and trailing slashes
    normalized_bucket = bucket_name.replace("s3://", "").rstrip("/")
    
    # Extract just the bucket name (first part before any /)
    bucket_only = normalized_bucket.split("/")[0]
    
    base_path = "cookies/dataset-files"
    manifest_lines = []
    
    script_dir = Path(__file__).parent
    training_dir = script_dir / "dataset-files" / "training-images"
    mask_dir = script_dir / "dataset-files" / "mask-images"
    manifest_dir = script_dir / "dataset-files" / "manifests"
    
    logger.info("Generating segmentation manifest from %s (format: %s)", training_dir, format_type)
    logger.info("Using bucket: %s", bucket_only)
    
    if not training_dir.exists() or not mask_dir.exists():
        logger.error("Training images or mask images directory not found")
        return 0
    
    # Create manifests directory if it doesn't exist
    manifest_dir.mkdir(parents=True, exist_ok=True)
    
    # Process training images
    for img_file in sorted(training_dir.iterdir()):
        if not img_file.is_file() or not img_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
            continue
        
        # Determine class based on filename
        if "anomaly" in img_file.name.lower():
            class_label = "anomaly"
            label_value = 1
        else:
            class_label = "normal"
            label_value = 0
        
        # Build S3 URIs with normalized bucket name
        s3_image_uri = f"s3://{bucket_only}/{base_path}/training-images/{img_file.name}"
        
        # Check if corresponding mask exists
        mask_file = img_file.stem + ".png"
        mask_path = mask_dir / mask_file
        
        # For normal images without masks, use dummy mask
        if not mask_path.exists():
            if class_label == "normal":
                logger.debug("Using dummy mask for normal image %s", img_file.name)
                s3_mask_uri = f"s3://{bucket_only}/{base_path}/mask-images/dummy_anomaly_mask.png"
            else:
                logger.warning("Mask not found for anomaly image %s, skipping", img_file.name)
                continue
        else:
            s3_mask_uri = f"s3://{bucket_only}/{base_path}/mask-images/{mask_file}"
        
        # Build manifest entry based on format type
        if format_type == "ground-truth":
            # Ground Truth format with job-specific attribute names
            entry = {
                "source-ref": s3_image_uri,
                "cookie-classification": label_value,
                "cookie-classification-metadata": {
                    "job-name": "cookie-classification",
                    "class-name": class_label,
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "cookie-segmentation-ref": s3_mask_uri,
                "cookie-segmentation-ref-metadata": {
                    "internal-color-map": {
                        "0": {
                            "class-name": "cracked" if class_label == "anomaly" else "BACKGROUND",
                            "hex-color": "#23A436" if class_label == "anomaly" else "#ffffff"
                        }
                    },
                    "job-name": "cookie-segmentation-ref",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/semantic-segmentation"
                }
            }
        else:  # dda format
            # DDA format with standardized attribute names
            entry = {
                "source-ref": s3_image_uri,
                "anomaly-label": label_value,
                "anomaly-label-metadata": {
                    "job-name": "anomaly-label",
                    "class-name": class_label,
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/image-classification"
                },
                "anomaly-mask-ref": s3_mask_uri,
                "anomaly-mask-ref-metadata": {
                    "internal-color-map": {
                        "0": {
                            "class-name": "cracked" if class_label == "anomaly" else "BACKGROUND",
                            "hex-color": "#23A436" if class_label == "anomaly" else "#ffffff"
                        }
                    },
                    "job-name": "anomaly-mask-ref",
                    "human-annotated": "yes",
                    "creation-date": datetime.utcnow().isoformat() + "Z",
                    "type": "groundtruth/semantic-segmentation"
                }
            }
        
        manifest_lines.append(json.dumps(entry))
    
    # Write manifest file to manifests directory
    output_path = manifest_dir / output_file
    
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
  # Classification in Ground Truth format (default - for testing transformation in portal)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task classification

  # Segmentation in Ground Truth format (default - for testing transformation in portal)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task segmentation

  # Both tasks in Ground Truth format (default)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task both

  # Generate DDA format (ready for training without transformation)
  python3 generate_manifest.py s3://my-bucket/cookies/ --task both --format dda
        """
    )
    parser.add_argument(
        "bucket_name",
        help="S3 bucket name or S3 URI (e.g., 'dda-cookie-dataset-123456789' or 's3://dda-cookie-dataset-123456789/cookies/')"
    )
    parser.add_argument(
        "--task",
        choices=["classification", "segmentation", "both"],
        default="both",
        help="Type of manifest to generate (default: both)"
    )
    parser.add_argument(
        "--format",
        choices=["ground-truth", "dda"],
        default="ground-truth",
        help="Manifest format: 'ground-truth' (default) for Ground Truth format with job-specific names (for testing transformation in portal), 'dda' for DDA format with standardized names (ready for training)"
    )

    args = parser.parse_args()
    bucket_name = args.bucket_name
    format_type = args.format

    print(f"\n{'='*60}")
    print("Cookie Dataset Manifest Generator")
    print(f"{'='*60}")
    print(f"Bucket: {bucket_name}")
    print(f"Task: {args.task}")
    print(f"Format: {format_type}")
    if format_type == "ground-truth":
        print("📝 Generating Ground Truth format manifests")
        print("   These can be transformed to DDA format in the portal")
    else:
        print("✓ Generating DDA format manifests")
        print("   Ready for training without transformation")
    print(f"{'='*60}\n")

    try:
        if args.task in ["classification", "both"]:
            count = generate_classification_manifest(bucket_name, format_type=format_type)
            if count == 0:
                logger.error("Failed to generate classification manifest")
                sys.exit(1)
            print(f"✓ Classification manifest ({format_type}): {count} entries\n")

        if args.task in ["segmentation", "both"]:
            count = generate_segmentation_manifest(bucket_name, format_type=format_type)
            if count == 0:
                logger.error("Failed to generate segmentation manifest")
                sys.exit(1)
            print(f"✓ Segmentation manifest ({format_type}): {count} entries\n")

        print(f"{'='*60}")
        print("✓ Manifest generation complete!")
        if format_type == "ground-truth":
            print("\n📋 Next steps:")
            print("1. Upload manifests to S3")
            print("2. Register as pre-labeled dataset in portal")
            print("3. Portal will detect Ground Truth format")
            print("4. Click 'Transform Manifest Now' to convert to DDA format")
            print("5. Use transformed manifest for training")
        else:
            print("\n✓ Manifests are ready for training!")
        print(f"{'='*60}\n")

    except Exception as e:
        logger.error("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
