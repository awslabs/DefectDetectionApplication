#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Universal dataset uploader for DDA.

Uploads a local directory of images (and optional masks) to S3.
Works with any dataset — cookie, alien, or custom.

Usage:
    # Upload cookie dataset
    python3 upload_dataset.py datasets/cookie-dataset/dataset-files/training-images/ \
        s3://dda-cookie-bucket/cookies/training-images/

    # Upload alien dataset
    python3 upload_dataset.py datasets/alien-dataset/ s3://dda-alien-bucket/aliens/

    # Upload with masks
    python3 upload_dataset.py datasets/cookie-dataset/dataset-files/training-images/ \
        s3://bucket/cookies/training-images/ \
        --masks-dir datasets/cookie-dataset/dataset-files/mask-images/ \
        --masks-prefix cookies/mask-images/

    # Dry run
    python3 upload_dataset.py ./my-images/ s3://bucket/prefix/ --dry-run
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def upload_directory(local_dir: Path, bucket: str, prefix: str, dry_run: bool = False) -> int:
    """Upload all files in a directory to S3."""
    s3 = boto3.client('s3')
    count = 0

    for root, _, files in os.walk(local_dir):
        for f in sorted(files):
            if f.startswith('.'):
                continue
            local_path = Path(root) / f
            rel = local_path.relative_to(local_dir)
            s3_key = f"{prefix}{rel}".replace("\\", "/")

            if dry_run:
                print(f"  [dry-run] {local_path} → s3://{bucket}/{s3_key}")
            else:
                s3.upload_file(str(local_path), bucket, s3_key)
                logger.info("Uploaded: s3://%s/%s", bucket, s3_key)
            count += 1

    return count


def parse_s3_uri(s3_uri: str):
    """Parse s3://bucket/prefix/ into (bucket, prefix)."""
    path = s3_uri.replace("s3://", "")
    parts = path.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def main():
    parser = argparse.ArgumentParser(
        description="Upload dataset images and masks to S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("local_dir", help="Local directory containing images to upload")
    parser.add_argument("s3_destination", help="S3 destination (e.g., s3://bucket/prefix/)")
    parser.add_argument("--masks-dir", help="Local directory containing mask images")
    parser.add_argument("--masks-prefix", help="S3 prefix for masks (default: same level as images)")
    parser.add_argument("--manifest", help="Also upload a manifest file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded")
    args = parser.parse_args()

    local_dir = Path(args.local_dir)
    if not local_dir.exists():
        logger.error("Directory not found: %s", local_dir)
        sys.exit(1)

    bucket, prefix = parse_s3_uri(args.s3_destination)

    # Verify bucket
    if not args.dry_run:
        try:
            boto3.client('s3').head_bucket(Bucket=bucket)
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                logger.error("Bucket not found: %s", bucket)
                sys.exit(1)
            raise
        except NoCredentialsError:
            logger.error("AWS credentials not configured. Run: aws configure")
            sys.exit(1)

    print(f"\n{'='*60}")
    print("DDA Dataset Uploader")
    print(f"{'='*60}")
    print(f"Images:      {local_dir}")
    print(f"Destination: s3://{bucket}/{prefix}")
    if args.masks_dir:
        print(f"Masks:       {args.masks_dir}")
    print(f"{'='*60}\n")

    total = 0

    # Upload images
    count = upload_directory(local_dir, bucket, prefix, args.dry_run)
    total += count
    print(f"✓ Images: {count} files {'(dry-run)' if args.dry_run else 'uploaded'}")

    # Upload masks
    if args.masks_dir:
        masks_dir = Path(args.masks_dir)
        masks_prefix = args.masks_prefix or prefix.rstrip('/').rsplit('/', 1)[0] + '/mask-images/'
        count = upload_directory(masks_dir, bucket, masks_prefix, args.dry_run)
        total += count
        print(f"✓ Masks: {count} files {'(dry-run)' if args.dry_run else 'uploaded'}")

    # Upload manifest
    if args.manifest:
        manifest = Path(args.manifest)
        if manifest.exists():
            s3_key = f"{prefix}{manifest.name}"
            if not args.dry_run:
                boto3.client('s3').upload_file(str(manifest), bucket, s3_key)
            total += 1
            print(f"✓ Manifest: s3://{bucket}/{s3_key}")

    print(f"\n{'='*60}")
    print(f"✓ Total: {total} files {'(dry-run)' if args.dry_run else 'uploaded'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
