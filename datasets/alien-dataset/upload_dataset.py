#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Upload Alien Dataset to S3

This script uploads all dataset files (normal images, anomaly images, and optional masks) to an S3 bucket.

Usage:
    python3 upload_dataset.py s3://your-bucket-name/aliens/

The script uses your default AWS credentials from:
- Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
- ~/.aws/credentials file
- IAM role (if running on EC2 or Lambda)
"""

import logging
import argparse
import sys
import os
from pathlib import Path
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging with timestamps and levels."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )


def upload_folder_to_s3(local_path, s3_path):
    """
    Uploads a local folder to S3.
    
    Args:
        local_path: Local folder path
        s3_path: S3 destination path
    """
    logger.info("Uploading folder %s to %s", local_path, s3_path)

    try:
        # Use default AWS credentials
        s3_client = boto3.client('s3')
        
        # Parse S3 path
        if not s3_path.endswith("/"):
            s3_path = s3_path + "/"
        
        bucket_name = s3_path.replace("s3://", "").split("/")[0]
        s3_prefix = s3_path.replace(f"s3://{bucket_name}/", "")

        # Verify bucket exists
        try:
            s3_client.head_bucket(Bucket=bucket_name)
            logger.info("Verified S3 bucket: %s", bucket_name)
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                raise FileNotFoundError(f"S3 bucket not found: {bucket_name}")
            raise

        # Upload all files
        uploaded_count = 0
        local_path = Path(local_path)
        
        for root, dirs, files in os.walk(local_path):
            for file in files:
                # Skip hidden files
                if file.startswith("."):
                    logger.debug("Skipping hidden file: %s", file)
                    continue

                full_local_path = Path(root) / file
                relative_path = full_local_path.relative_to(local_path)
                s3_key = s3_prefix + str(relative_path).replace("\\", "/")

                try:
                    s3_client.upload_file(
                        str(full_local_path),
                        bucket_name,
                        s3_key
                    )
                    logger.info("Uploaded: s3://%s/%s", bucket_name, s3_key)
                    uploaded_count += 1
                except ClientError as e:
                    logger.error("Failed to upload %s: %s", file, e)
                    raise

        logger.info("Successfully uploaded %d files to S3", uploaded_count)
        return uploaded_count

    except NoCredentialsError:
        logger.error("AWS credentials not found. Please configure your AWS credentials:")
        logger.error("  1. Set environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        logger.error("  2. Or run: aws configure")
        logger.error("  3. Or use an IAM role if running on EC2/Lambda")
        raise


def main():
    """Main entry point."""
    setup_logging()
    
    parser = argparse.ArgumentParser(
        description="Upload Alien Dataset to S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 upload_dataset.py s3://my-bucket/aliens/
  python3 upload_dataset.py s3://dda-alien-dataset-123456789/aliens/
        """
    )
    parser.add_argument(
        "s3_path",
        help="S3 destination path (e.g., s3://bucket-name/aliens/)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be uploaded without actually uploading"
    )

    args = parser.parse_args()
    s3_path = args.s3_path

    # Get script directory
    script_dir = Path(__file__).parent

    print(f"\n{'='*60}")
    print("Alien Dataset Upload Tool")
    print(f"{'='*60}")
    print(f"Source: {script_dir}")
    print(f"Destination: {s3_path}")
    print(f"{'='*60}\n")

    try:
        # Upload files
        if args.dry_run:
            logger.info("DRY RUN: Would upload dataset files to %s", s3_path)
            logger.info("Run without --dry-run to actually upload")
        else:
            upload_folder_to_s3(script_dir, s3_path)

        # Print success message
        print(f"\n{'='*60}")
        print("✓ Upload Complete!")
        print(f"{'='*60}")
        print(f"Destination: {s3_path}")
        print(f"\nNext steps:")
        print(f"1. Generate manifests:")
        print(f"   python3 generate_manifest.py {s3_path} --task both")
        print(f"\n2. Register in DDA Portal:")
        print(f"   Data Management → Pre-Labeled Datasets → Register Dataset")
        print(f"   S3 Manifest URI: {s3_path}train_class.manifest")
        print(f"{'='*60}\n")

    except FileNotFoundError as e:
        logger.error("File error: %s", e)
        sys.exit(1)
    except NoCredentialsError as e:
        logger.error("Credentials error: %s", e)
        sys.exit(1)
    except ClientError as e:
        logger.error("AWS error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
