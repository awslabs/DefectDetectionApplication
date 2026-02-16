#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Create a background-only mask for normal images in segmentation training.

This creates a single-channel PNG where all pixels are 0 (background class).
This mask is used for normal images that don't have defects.
"""

from PIL import Image
import numpy as np
from pathlib import Path

def create_background_mask(width=224, height=224, output_path="background_mask.png"):
    """
    Create a background-only mask (all pixels = 0).
    
    Args:
        width: Image width in pixels
        height: Image height in pixels
        output_path: Where to save the mask
    """
    # Create array of zeros (all background)
    mask_array = np.zeros((height, width), dtype=np.uint8)
    
    # Convert to PIL Image and save
    mask_image = Image.fromarray(mask_array, mode='L')
    mask_image.save(output_path)
    
    print(f"✓ Created background mask: {output_path}")
    print(f"  Size: {width}x{height}")
    print(f"  All pixels: 0 (BACKGROUND class)")

if __name__ == "__main__":
    script_dir = Path(__file__).parent
    mask_dir = script_dir / "dataset-files" / "mask-images"
    mask_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = mask_dir / "background_mask.png"
    create_background_mask(output_path=str(output_path))
    
    print(f"\n📋 Next steps:")
    print(f"1. Upload this mask to S3:")
    print(f"   aws s3 cp {output_path} s3://YOUR-BUCKET/cookies/dataset-files/mask-images/")
    print(f"2. Regenerate the manifest with the updated script")
