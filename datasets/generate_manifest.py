#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Universal manifest generator for DDA datasets.

Scans a local image directory, generates Ground Truth format manifests,
and optionally transforms to DDA format using the portal's transformer.

Works with any dataset (cookie, alien, custom) — just point it at a directory
with 'anomaly' and 'normal' subfolders, or images with anomaly/normal in filenames.

Usage:
    # From image directory with anomaly/normal subfolders
    python3 generate_manifest.py s3://bucket/prefix/ --images-dir ./training-images/

    # From flat directory (classifies by filename containing 'anomaly' or 'normal')
    python3 generate_manifest.py s3://bucket/prefix/ --images-dir ./images/ --flat

    # With masks for segmentation
    python3 generate_manifest.py s3://bucket/prefix/ --images-dir ./training-images/ \\
        --masks-dir ./mask-images/ --task both

    # Generate DDA format (transforms GT using portal's transformer)
    python3 generate_manifest.py s3://bucket/prefix/ --images-dir ./images/ --format dda

    # Specify job name (used in GT field names)
    python3 generate_manifest.py s3://bucket/prefix/ --images-dir ./images/ --job-name cookie-classification
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add the manifest_transformer from the portal's shared layer
sys.path.insert(0, str(Path(__file__).parent.parent / 'edge-cv-portal' / 'backend' / 'layers' / 'shared' / 'python'))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}


def classify_image(filepath: Path, flat: bool = False) -> str:
    """Determine if an image is anomaly or normal based on path or filename."""
    name = filepath.name.lower()
    parent = filepath.parent.name.lower()

    if not flat and parent in ('anomaly', 'anomalous', 'defect', 'defects'):
        return 'anomaly'
    if not flat and parent in ('normal', 'good', 'ok'):
        return 'normal'
    # Fallback to filename
    if 'anomaly' in name or 'defect' in name:
        return 'anomaly'
    return 'normal'


def find_images(images_dir: Path, flat: bool = False):
    """Find all images and classify them."""
    images = []
    if flat:
        for f in sorted(images_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                images.append((f, classify_image(f, flat=True)))
    else:
        # Look in subdirectories (anomaly/, normal/)
        for subdir in sorted(images_dir.iterdir()):
            if subdir.is_dir():
                for f in sorted(subdir.iterdir()):
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                        images.append((f, classify_image(f)))
        # Also check root level
        for f in sorted(images_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                images.append((f, classify_image(f, flat=True)))
    return images


def build_s3_uri(bucket_prefix: str, images_dir: Path, filepath: Path) -> str:
    """Build S3 URI for a file relative to images_dir."""
    rel = filepath.relative_to(images_dir)
    prefix = bucket_prefix.rstrip('/')
    return f"s3://{prefix}/{rel}"


def generate_classification_manifest(
    bucket_prefix: str, images_dir: Path, job_name: str, flat: bool = False
) -> list:
    """Generate classification manifest lines in Ground Truth format."""
    images = find_images(images_dir, flat)
    lines = []
    for filepath, label in images:
        entry = {
            "source-ref": build_s3_uri(bucket_prefix, images_dir, filepath),
            job_name: 1 if label == 'anomaly' else 0,
            f"{job_name}-metadata": {
                "job-name": job_name,
                "class-name": label,
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/image-classification"
            }
        }
        lines.append(json.dumps(entry))
    return lines


def generate_segmentation_manifest(
    bucket_prefix: str, images_dir: Path, masks_dir: Path,
    job_name: str, mask_job_name: str, flat: bool = False,
    background_mask: str = "background_mask.png"
) -> list:
    """Generate segmentation manifest lines in Ground Truth format."""
    images = find_images(images_dir, flat)
    lines = []
    for filepath, label in images:
        label_value = 1 if label == 'anomaly' else 0
        s3_image = build_s3_uri(bucket_prefix, images_dir, filepath)

        # Find mask
        mask_file = filepath.stem + ".png"
        mask_path = masks_dir / mask_file if masks_dir else None

        if mask_path and mask_path.exists():
            s3_mask = build_s3_uri(bucket_prefix, masks_dir, mask_path)
        elif label == 'normal' and masks_dir:
            s3_mask = f"s3://{bucket_prefix.rstrip('/')}/{background_mask}"
        else:
            continue  # Skip anomaly images without masks

        color_map = {"0": {"class-name": "BACKGROUND", "hex-color": "#ffffff", "confidence": 0.5}}
        if label_value == 1:
            color_map["1"] = {"class-name": "DEFECT", "hex-color": "#23A436", "confidence": 0.5}

        entry = {
            "source-ref": s3_image,
            job_name: label_value,
            f"{job_name}-metadata": {
                "job-name": job_name,
                "class-name": label,
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/image-classification"
            },
            f"{mask_job_name}-ref": s3_mask,
            f"{mask_job_name}-ref-metadata": {
                "internal-color-map": color_map,
                "job-name": f"{mask_job_name}-ref",
                "human-annotated": "yes",
                "creation-date": datetime.utcnow().isoformat() + "Z",
                "type": "groundtruth/semantic-segmentation"
            }
        }
        lines.append(json.dumps(entry))
    return lines


def transform_to_dda(lines: list, task_type: str) -> list:
    """Transform GT manifest lines to DDA format using the portal's transformer."""
    try:
        from manifest_transformer import transform_manifest_lines
    except ImportError:
        logger.error("Could not import manifest_transformer from edge-cv-portal/backend/layers/shared/python/")
        raise
    result = transform_manifest_lines(lines, task_type)
    if not result['transformed_lines']:
        raise RuntimeError(f"Transformation failed: {result['errors']}")
    logger.info("Transformed %d entries", result['stats']['transformed'])
    return result['transformed_lines']


def main():
    parser = argparse.ArgumentParser(
        description="Generate or transform DDA manifest files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Generate subcommand
    gen = subparsers.add_parser("generate", help="Generate manifest from image directory")
    gen.add_argument("bucket_prefix", help="S3 bucket/prefix for image URIs")
    gen.add_argument("--images-dir", required=True, help="Local directory containing images")
    gen.add_argument("--masks-dir", default=None, help="Local directory containing mask images")
    gen.add_argument("--output", "-o", default="train.manifest", help="Output manifest filename")
    gen.add_argument("--task", choices=["classification", "segmentation", "both"], default="classification")
    gen.add_argument("--format", choices=["ground-truth", "dda"], default="ground-truth")
    gen.add_argument("--job-name", default="dataset-classification", help="GT job name for field naming")
    gen.add_argument("--mask-job-name", default="dataset-segmentation", help="GT mask job name")
    gen.add_argument("--flat", action="store_true", help="Flat directory (no anomaly/normal subfolders)")

    # Transform subcommand
    xform = subparsers.add_parser("transform", help="Transform existing GT manifest to DDA format")
    xform.add_argument("input", help="Input manifest file (Ground Truth format)")
    xform.add_argument("--output", "-o", help="Output manifest file (default: input with -dda suffix)")
    xform.add_argument("--task", choices=["classification", "segmentation"], default="classification")

    args = parser.parse_args()

    if args.command == "transform":
        input_path = Path(args.input)
        if not input_path.exists():
            logger.error("Input manifest not found: %s", input_path)
            sys.exit(1)
        
        with open(input_path) as f:
            lines = f.read().strip().split('\n')
        
        transformed = transform_to_dda(lines, args.task)
        
        out_path = Path(args.output) if args.output else input_path.with_stem(input_path.stem + '-dda')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as f:
            f.write('\n'.join(transformed))
        print(f"✓ Transformed {len(transformed)} entries → {out_path}")

    elif args.command == "generate":
        images_dir = Path(args.images_dir)
        masks_dir = Path(args.masks_dir) if args.masks_dir else None
        output = Path(args.output)

        if not images_dir.exists():
            logger.error("Images directory not found: %s", images_dir)
            sys.exit(1)

        bucket_prefix = args.bucket_prefix.replace("s3://", "").rstrip("/")

        for task in (['classification', 'segmentation'] if args.task == 'both' else [args.task]):
            if task == 'classification':
                lines = generate_classification_manifest(bucket_prefix, images_dir, args.job_name, args.flat)
                out_file = output.with_stem(output.stem + '_class') if args.task == 'both' else output
            else:
                if not masks_dir:
                    logger.error("--masks-dir required for segmentation")
                    sys.exit(1)
                lines = generate_segmentation_manifest(
                    bucket_prefix, images_dir, masks_dir, args.job_name, args.mask_job_name, args.flat
                )
                out_file = output.with_stem(output.stem + '_segmentation') if args.task == 'both' else output

            if args.format == 'dda':
                lines = transform_to_dda(lines, task)

            out_file.parent.mkdir(parents=True, exist_ok=True)
            with open(out_file, 'w') as f:
                f.write('\n'.join(lines))
            print(f"✓ {task} ({args.format}): {len(lines)} entries → {out_file}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
