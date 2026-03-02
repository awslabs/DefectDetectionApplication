#!/usr/bin/env python3
"""
Fix S3 object keys containing spaces by renaming them with underscores.

This script:
1. Lists all objects in a specified S3 prefix
2. Identifies objects with spaces in their keys
3. Copies them to new keys with spaces replaced by underscores
4. Optionally deletes the original objects

Usage:
    python fix_s3_spaces.py s3://bucket/prefix/ [--profile PROFILE] [--dry-run] [--delete-original]
"""
import sys
import argparse
import boto3
from urllib.parse import urlparse


def parse_s3_uri(uri):
    """Parse s3://bucket/prefix into (bucket, prefix)."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def list_objects_with_spaces(s3, bucket, prefix):
    """List all objects under prefix that contain spaces in their keys."""
    objects_with_spaces = []
    paginator = s3.get_paginator("list_objects_v2")
    
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if "Contents" not in page:
            continue
        
        for obj in page["Contents"]:
            key = obj["Key"]
            if " " in key:
                objects_with_spaces.append({
                    "original_key": key,
                    "new_key": key.replace(" ", "_"),
                    "size": obj["Size"],
                })
    
    return objects_with_spaces


def fix_object_spaces(s3, bucket, obj_info, dry_run=False, delete_original=False):
    """Rename an S3 object by copying to new key and optionally deleting original."""
    original = obj_info["original_key"]
    new = obj_info["new_key"]
    
    if dry_run:
        print(f"  [DRY-RUN] Would rename: {original} -> {new}")
        return True
    
    try:
        # Copy to new key
        copy_source = {"Bucket": bucket, "Key": original}
        s3.copy_object(
            CopySource=copy_source,
            Bucket=bucket,
            Key=new,
        )
        print(f"  ✓ Copied: {original} -> {new}")
        
        # Delete original if requested
        if delete_original:
            s3.delete_object(Bucket=bucket, Key=original)
            print(f"  ✓ Deleted original: {original}")
        else:
            print(f"  ⚠ Original kept: {original} (use --delete-original to remove)")
        
        return True
    
    except Exception as e:
        print(f"  ✗ Failed to rename {original}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Fix S3 object keys containing spaces by replacing with underscores"
    )
    parser.add_argument(
        "s3_uri",
        help="S3 URI to scan (e.g., s3://bucket/prefix/)",
    )
    parser.add_argument(
        "--profile",
        help="AWS profile name to use",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be renamed without making changes",
    )
    parser.add_argument(
        "--delete-original",
        action="store_true",
        help="Delete original objects after copying (default: keep both)",
    )
    
    args = parser.parse_args()
    
    # Parse S3 URI
    try:
        bucket, prefix = parse_s3_uri(args.s3_uri)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    # Initialize S3 client
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    s3 = session.client("s3")
    
    print(f"Scanning s3://{bucket}/{prefix}")
    if args.dry_run:
        print("DRY-RUN MODE: No changes will be made")
    print()
    
    # Find objects with spaces
    objects_with_spaces = list_objects_with_spaces(s3, bucket, prefix)
    
    if not objects_with_spaces:
        print("✓ No objects with spaces found")
        return
    
    print(f"Found {len(objects_with_spaces)} object(s) with spaces in their keys:")
    print()
    
    # Show summary
    total_size = sum(obj["size"] for obj in objects_with_spaces)
    print(f"Total size: {total_size:,} bytes ({total_size / (1024**2):.2f} MB)")
    print()
    
    # Process each object
    success_count = 0
    for obj_info in objects_with_spaces:
        if fix_object_spaces(s3, bucket, obj_info, args.dry_run, args.delete_original):
            success_count += 1
    
    print()
    print("=" * 70)
    if args.dry_run:
        print(f"DRY-RUN: Would rename {success_count}/{len(objects_with_spaces)} objects")
    else:
        print(f"Successfully renamed {success_count}/{len(objects_with_spaces)} objects")
        if not args.delete_original:
            print("Original objects were kept (use --delete-original to remove them)")
    print("=" * 70)


if __name__ == "__main__":
    main()
