#!/usr/bin/env python3
"""
Transform raw SageMaker Ground Truth segmentation output to DDA/LFV format.

Ground Truth segmentation output has:
  - source-ref
  - {job}-ref          (mask S3 URI)
  - {job}-ref-metadata (mask metadata)

DDA/LFV segmentation training requires:
  - source-ref
  - anomaly-label              (int: 0=normal, 1=anomaly)
  - anomaly-label-metadata     (classification metadata)
  - anomaly-mask-ref           (mask S3 URI)
  - anomaly-mask-ref-metadata  (mask metadata)

This script:
  1. Detects the GT job-specific key names
  2. Remaps mask keys to anomaly-mask-ref / anomaly-mask-ref-metadata
  3. Infers anomaly-label from the source image filename convention
  4. Constructs anomaly-label-metadata with correct type

Usage:
    # Preview (no file written)
    python3 transform_gt_to_dda.py output.manifest

    # Write transformed manifest
    python3 transform_gt_to_dda.py output.manifest output-dda.ndjson
"""
import json
import sys
from datetime import datetime, timezone


def detect_gt_keys(records):
    """Find the GT job-specific -ref and -ref-metadata keys."""
    skip = {"source-ref"}
    for _, rec in records:
        for k in rec.keys():
            if k.endswith("-ref-metadata") and k not in skip:
                ref_meta = k
                ref = ref_meta.replace("-metadata", "")
                if ref in rec:
                    return ref, ref_meta
    return None, None


def infer_label(source_ref: str) -> int:
    """Infer anomaly-label from the source image filename.

    Convention:
      anomaly_*  -> 1
      normal_*   -> 0
      (no anomaly_ prefix) -> 0

    Override this function for datasets with different naming conventions.
    """
    filename = source_ref.rsplit("/", 1)[-1].lower()
    if filename.startswith("anomaly"):
        return 1
    return 0


def transform_entry(rec, gt_ref_key, gt_ref_meta_key):
    """Transform a single GT record to DDA format."""
    source_ref = rec.get("source-ref", "")
    mask_ref = rec.get(gt_ref_key)
    mask_meta = rec.get(gt_ref_meta_key)

    # Skip records that weren't labeled (no mask ref)
    if not mask_ref or not isinstance(mask_meta, dict):
        return None, "unlabeled"

    # Skip failed records
    if "failure-reason" in (mask_meta or {}):
        return None, "failed"

    label = infer_label(source_ref)
    class_name = "anomaly" if label == 1 else "normal"

    creation_date = mask_meta.get(
        "creation-date", datetime.now(timezone.utc).isoformat()
    )

    transformed = {
        "source-ref": source_ref,
        "anomaly-label": label,
        "anomaly-label-metadata": {
            "class-name": class_name,
            "confidence": 1.0,
            "type": "groundtruth/image-classification",
            "job-name": "anomaly-label",
            "human-annotated": "yes",
            "creation-date": creation_date,
        },
        "anomaly-mask-ref": mask_ref,
        "anomaly-mask-ref-metadata": {
            "internal-color-map": mask_meta.get("internal-color-map", {}),
            "type": "groundtruth/semantic-segmentation",
            "human-annotated": mask_meta.get("human-annotated", "yes"),
            "creation-date": creation_date,
            "job-name": "anomaly-mask-ref",
        },
    }
    return transformed, None


def transform_manifest(input_path, output_path=None):
    """Transform a GT manifest file to DDA format."""
    with open(input_path) as f:
        lines = [l.strip() for l in f if l.strip()]

    records = []
    parse_errors = []
    for i, line in enumerate(lines, 1):
        try:
            records.append((i, json.loads(line)))
        except json.JSONDecodeError as e:
            parse_errors.append(f"Line {i}: {e}")

    if not records:
        print(f"FATAL: No records parsed from {input_path}")
        return

    gt_ref, gt_ref_meta = detect_gt_keys(records)
    if not gt_ref:
        print("FATAL: Could not detect Ground Truth -ref/-ref-metadata keys")
        return

    print(f"Detected GT keys: {gt_ref} / {gt_ref_meta}")

    transformed = []
    skipped_unlabeled = 0
    skipped_failed = 0
    normal_count = 0
    anomaly_count = 0

    for line_num, rec in records:
        entry, skip_reason = transform_entry(rec, gt_ref, gt_ref_meta)
        if entry:
            transformed.append(entry)
            if entry["anomaly-label"] == 0:
                normal_count += 1
            else:
                anomaly_count += 1
        elif skip_reason == "unlabeled":
            skipped_unlabeled += 1
        elif skip_reason == "failed":
            skipped_failed += 1

    print(f"\n=== TRANSFORMATION RESULTS ===")
    print(f"  Input records:     {len(records)}")
    print(f"  Transformed:       {len(transformed)}")
    print(f"  Skipped unlabeled: {skipped_unlabeled}")
    print(f"  Skipped failed:    {skipped_failed}")
    print(f"  Normal (label=0):  {normal_count}")
    print(f"  Anomaly (label=1): {anomaly_count}")

    if parse_errors:
        print(f"  Parse errors:      {len(parse_errors)}")

    if output_path:
        with open(output_path, "w") as f:
            for entry in transformed:
                f.write(json.dumps(entry) + "\n")
        print(f"\n  Written to: {output_path}")
    else:
        print(f"\n=== FIRST 3 ENTRIES ===")
        for i, entry in enumerate(transformed[:3]):
            print(f"\n--- Entry {i+1} (label={entry['anomaly-label']}) ---")
            print(json.dumps(entry, indent=2))

        for entry in reversed(transformed):
            if entry["anomaly-label"] == 0:
                print(f"\n--- Sample normal entry (label=0) ---")
                print(json.dumps(entry, indent=2))
                break

    return transformed


if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else "output.manifest"
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    transform_manifest(input_path, output_path)
