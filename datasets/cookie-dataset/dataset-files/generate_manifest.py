#!/usr/bin/env python3
"""
Generate manifest file for cookie dataset.
Reads images from training-images/ and mask-images/ directories.
"""

import os
import json
import sys

def generate_manifest(bucket_name, output_file="train.manifest"):
    """
    Generate manifest file from dataset structure.
    
    Args:
        bucket_name: S3 bucket name (e.g., dda-cookie-dataset-123456789)
        output_file: Output manifest filename
    """
    
    base_path = "cookies"
    manifest_lines = []
    
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    training_dir = os.path.join(script_dir, "training-images")
    mask_dir = os.path.join(script_dir, "mask-images")
    
    print(f"Looking for images in: {training_dir}")
    print(f"Looking for masks in: {mask_dir}")
    
    if not os.path.exists(training_dir):
        print(f"ERROR: training-images directory not found at {training_dir}")
        return 0
    
    # Process training images
    for img_file in sorted(os.listdir(training_dir)):
        if not img_file.endswith(('.jpg', '.jpeg', '.png')):
            continue
        
        # Determine class based on filename
        if "anomaly" in img_file.lower():
            class_label = "anomaly"
            anomaly_label = 1
        else:
            class_label = "normal"
            anomaly_label = 0
        
        # Build S3 URI for image
        s3_image_uri = f"s3://{bucket_name}/{base_path}/training-images/{img_file}"
        
        # Check if corresponding mask exists
        mask_file = img_file.replace('.jpg', '.png').replace('.jpeg', '.png')
        mask_path = os.path.join(mask_dir, mask_file)
        
        # Build manifest entry
        entry = {
            "source-ref": s3_image_uri,
            "anomaly-label-metadata": {
                "job-name": "anomaly-label",
                "class-name": class_label,
                "human-annotated": "yes",
                "type": "groundtruth/image-classification"
            },
            "anomaly-label": anomaly_label
        }
        
        # Add mask reference if it exists
        if os.path.exists(mask_path):
            s3_mask_uri = f"s3://{bucket_name}/{base_path}/mask-images/{mask_file}"
            entry["anomaly-mask-ref-metadata"] = {
                "internal-color-map": {
                    "0": {
                        "class-name": "cracked",
                        "hex-color": "#23A436"
                    }
                },
                "job-name": "labeling-job/object-mask-ref",
                "human-annotated": "yes",
                "type": "groundtruth/semantic-segmentation"
            }
            entry["anomaly-mask-ref"] = s3_mask_uri
        
        manifest_lines.append(json.dumps(entry))
    
    # Write manifest file
    output_path = os.path.join(script_dir, output_file)
    with open(output_path, "w") as f:
        f.write("\n".join(manifest_lines))
    
    print(f"Generated manifest with {len(manifest_lines)} entries")
    print(f"Manifest saved to: {output_path}")
    
    return len(manifest_lines)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 generate_manifest.py <bucket_name> [output_file]")
        print("Example: python3 generate_manifest.py dda-cookie-dataset-123456789")
        sys.exit(1)
    
    bucket_name = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "train.manifest"
    
    count = generate_manifest(bucket_name, output_file)
    if count == 0:
        sys.exit(1)
