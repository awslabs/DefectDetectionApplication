#!/usr/bin/env python3
"""
DDA Dataset Evaluation Agent
Validates augmented manifest readiness before transformation or training.

Supports two manifest formats:
  - Raw SageMaker Ground Truth output (job-specific key names)
  - DDA-transformed format (anomaly-label / anomaly-mask-ref keys)

Role: Read-only evaluation — no modifications to data, artifacts, or manifests.

Usage:
    python3 evaluate_manifest.py <manifest_file> [--profile <aws_profile>]

Examples:
    python3 evaluate_manifest.py output.manifest --profile my-profile
    python3 evaluate_manifest.py output-dda.ndjson --profile my-profile
"""
import json
import sys
import argparse
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone


# DDA required keys for segmentation training
DDA_REQUIRED_KEYS = [
    "source-ref",
    "anomaly-label",
    "anomaly-label-metadata",
    "anomaly-mask-ref",
    "anomaly-mask-ref-metadata",
]

# Known Ground Truth Unicode substitutions
GT_SUBSTITUTIONS = {
    "\uf03a": "U+F03A (Ground Truth colon substitute for ':')",
}


def parse_s3_uri(uri: str):
    """Parse s3://bucket/key into (bucket, key)."""
    if not uri.startswith("s3://"):
        return None, None
    path = uri[5:]
    slash = path.find("/")
    if slash == -1:
        return path, ""
    return path[:slash], path[slash + 1:]


def s3_object_exists(s3, bucket, key):
    """Return True if the S3 object exists, False otherwise."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            return False
        raise


def load_manifest(path: str):
    """Load and parse JSONL manifest. Returns list of (line_number, dict) tuples."""
    records = []
    errors = []
    with open(path, "r") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append((i, json.loads(line)))
            except json.JSONDecodeError as e:
                errors.append(f"Line {i}: invalid JSON — {e}")
    return records, errors


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_manifest_format(records):
    """Auto-detect whether this is a raw Ground Truth manifest or DDA format.

    Scans all records since intermediate manifests may have unlabeled
    entries at the start.
    """
    if not records:
        return {"format": "unknown", "all_ref_keys": ["source-ref"]}

    all_keys = set()
    for _, rec in records:
        all_keys.update(rec.keys())

    # DDA format detection
    if "anomaly-label" in all_keys and "anomaly-label-metadata" in all_keys:
        has_mask = "anomaly-mask-ref" in all_keys
        fmt = {
            "format": "dda",
            "label_key": "anomaly-label",
            "label_meta_key": "anomaly-label-metadata",
            "mask_ref_key": "anomaly-mask-ref" if has_mask else None,
            "mask_ref_meta_key": "anomaly-mask-ref-metadata" if has_mask else None,
        }
        fmt["all_ref_keys"] = ["source-ref"]
        if fmt["mask_ref_key"]:
            fmt["all_ref_keys"].append(fmt["mask_ref_key"])
        return fmt

    # Ground Truth format: look for *-ref-metadata pattern across all records
    skip = {"source-ref"}
    ref_meta_key = None
    for k in all_keys:
        if k.endswith("-ref-metadata") and k not in skip:
            ref_meta_key = k
            break

    if ref_meta_key:
        ref_key = ref_meta_key.replace("-metadata", "")
        fmt = {
            "format": "ground-truth",
            "label_key": None,
            "label_meta_key": None,
            "mask_ref_key": ref_key if ref_key in all_keys else None,
            "mask_ref_meta_key": ref_meta_key,
            "gt_job_prefix": ref_key.replace("-ref", ""),
        }
        fmt["all_ref_keys"] = ["source-ref"]
        if fmt["mask_ref_key"]:
            fmt["all_ref_keys"].append(fmt["mask_ref_key"])
        return fmt

    return {"format": "unknown", "all_ref_keys": ["source-ref"]}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_class_presence(records, fmt):
    """Verify both normal (0) and anomaly (1) classes exist."""
    findings = []
    label_key = fmt.get("label_key")

    if not label_key:
        findings.append({
            "severity": "BLOCKING",
            "check": "class-presence",
            "message": (
                "Manifest has no classification label field (e.g. 'anomaly-label'). "
                "Raw Ground Truth segmentation output does not include image-level labels. "
                "A DDA transformation is required to add anomaly-label before training."
            ),
        })
        return {
            "normal_count": 0,
            "anomaly_count": 0,
            "missing_label_count": len(records),
            "findings": findings,
        }

    normal_count = 0
    anomaly_count = 0
    missing_label = 0

    for line_num, rec in records:
        label = rec.get(label_key)
        if label is None:
            missing_label += 1
        elif label == 0:
            normal_count += 1
        elif label == 1:
            anomaly_count += 1

    if missing_label > 0:
        findings.append({
            "severity": "ERROR",
            "check": "class-presence",
            "message": f"{missing_label} record(s) missing '{label_key}' field",
        })
    if normal_count == 0:
        findings.append({
            "severity": "BLOCKING",
            "check": "class-presence",
            "message": "No normal samples (label=0). Training requires at least one.",
        })
    if anomaly_count == 0:
        findings.append({
            "severity": "BLOCKING",
            "check": "class-presence",
            "message": "No anomaly samples (label=1). Training requires at least one.",
        })

    return {
        "normal_count": normal_count,
        "anomaly_count": anomaly_count,
        "missing_label_count": missing_label,
        "findings": findings,
    }


def check_schema(records, fmt):
    """Check required keys are present based on detected format."""
    findings = []

    if fmt["format"] == "dda":
        for line_num, rec in records:
            missing = [k for k in DDA_REQUIRED_KEYS if k not in rec]
            if missing:
                source = rec.get("source-ref", f"line {line_num}")
                findings.append({
                    "severity": "ERROR",
                    "check": "schema",
                    "line": line_num,
                    "message": f"Missing DDA keys {missing} — {source}",
                })
    elif fmt["format"] == "ground-truth":
        required_gt = ["source-ref"]
        if fmt.get("mask_ref_key"):
            required_gt.append(fmt["mask_ref_key"])
        if fmt.get("mask_ref_meta_key"):
            required_gt.append(fmt["mask_ref_meta_key"])
        for line_num, rec in records:
            missing = [k for k in required_gt if k not in rec]
            if missing:
                findings.append({
                    "severity": "ERROR",
                    "check": "schema",
                    "line": line_num,
                    "message": f"Missing Ground Truth keys {missing}",
                })
    else:
        findings.append({
            "severity": "ERROR",
            "check": "schema",
            "message": "Could not determine manifest format. Expected DDA or Ground Truth structure.",
        })

    return findings


def check_s3_key_encoding(records, fmt):
    """Detect hidden/special Unicode characters in S3 URIs.

    SageMaker Ground Truth substitutes colons with U+F03A in annotation
    output keys. These invisible characters cause mismatches between
    manifest references and actual S3 object keys.
    """
    findings = []
    affected_records = 0
    ref_keys = fmt.get("all_ref_keys", ["source-ref"])

    for line_num, rec in records:
        record_dirty = False
        for ref_key in ref_keys:
            uri = rec.get(ref_key, "")
            for char, description in GT_SUBSTITUTIONS.items():
                if char in uri:
                    record_dirty = True
                    count = uri.count(char)
                    findings.append({
                        "severity": "BLOCKING",
                        "check": "s3-key-encoding",
                        "line": line_num,
                        "message": (
                            f"'{ref_key}' contains {count}x {description}. "
                            f"Known SageMaker Ground Truth artifact — S3 key will not "
                            f"resolve until these characters are removed from the actual "
                            f"S3 object key AND the manifest URI."
                        ),
                    })
            for i, c in enumerate(uri):
                if ord(c) > 127 and c not in GT_SUBSTITUTIONS:
                    record_dirty = True
                    findings.append({
                        "severity": "WARNING",
                        "check": "s3-key-encoding",
                        "line": line_num,
                        "message": (
                            f"'{ref_key}' contains non-ASCII U+{ord(c):04X} at position {i}"
                        ),
                    })
        if record_dirty:
            affected_records += 1

    return {"affected_records": affected_records, "findings": findings}


def check_artifact_existence(records, fmt, s3):
    """Verify every referenced S3 object actually exists."""
    findings = []
    checked = 0
    missing = 0
    ref_keys = fmt.get("all_ref_keys", ["source-ref"])

    for line_num, rec in records:
        for ref_key in ref_keys:
            uri = rec.get(ref_key)
            if not uri:
                continue
            bucket, key = parse_s3_uri(uri)
            if not bucket:
                findings.append({
                    "severity": "ERROR",
                    "check": "artifact-existence",
                    "line": line_num,
                    "message": f"Invalid S3 URI for '{ref_key}': {uri}",
                })
                continue
            checked += 1
            if not s3_object_exists(s3, bucket, key):
                missing += 1
                findings.append({
                    "severity": "BLOCKING",
                    "check": "artifact-existence",
                    "line": line_num,
                    "ref_key": ref_key,
                    "message": f"Object not found: {uri}",
                })

    return {"checked": checked, "missing": missing, "findings": findings}


def check_metadata_quality(records, fmt):
    """Validate metadata structure and label/class-name consistency."""
    findings = []
    label_key = fmt.get("label_key")
    mask_meta_key = fmt.get("mask_ref_meta_key")

    for line_num, rec in records:
        if label_key:
            meta = rec.get(fmt.get("label_meta_key", ""))
            if isinstance(meta, dict):
                cn = meta.get("class-name")
                lbl = rec.get(label_key)
                if lbl == 0 and cn != "normal":
                    findings.append({
                        "severity": "WARNING",
                        "check": "metadata-quality",
                        "line": line_num,
                        "message": f"label=0 but class-name='{cn}' (expected 'normal')",
                    })
                if lbl == 1 and cn != "anomaly":
                    findings.append({
                        "severity": "WARNING",
                        "check": "metadata-quality",
                        "line": line_num,
                        "message": f"label=1 but class-name='{cn}' (expected 'anomaly')",
                    })

        if mask_meta_key:
            mm = rec.get(mask_meta_key)
            if isinstance(mm, dict):
                cm = mm.get("internal-color-map")
                if not cm:
                    findings.append({
                        "severity": "WARNING",
                        "check": "metadata-quality",
                        "line": line_num,
                        "message": f"'{mask_meta_key}' missing 'internal-color-map'",
                    })
                elif isinstance(cm, dict) and "0" not in cm:
                    findings.append({
                        "severity": "WARNING",
                        "check": "metadata-quality",
                        "line": line_num,
                        "message": "internal-color-map missing class '0' (BACKGROUND)",
                    })

    return findings


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def generate_report(manifest_path, fmt, records, parse_errors,
                    class_result, schema_findings, encoding_result,
                    artifact_result, metadata_findings):
    blocking, errors, warnings = [], [], []

    all_findings = (
        class_result["findings"]
        + schema_findings
        + encoding_result["findings"]
        + artifact_result["findings"]
        + metadata_findings
    )
    for f in all_findings:
        sev = f["severity"]
        if sev == "BLOCKING":
            blocking.append(f)
        elif sev == "ERROR":
            errors.append(f)
        elif sev == "WARNING":
            warnings.append(f)

    for pe in parse_errors:
        errors.append({"severity": "ERROR", "check": "parse", "message": pe})

    passed = len(blocking) == 0 and len(errors) == 0

    r = []
    r.append("=" * 70)
    r.append("  DDA DATASET EVALUATION REPORT")
    r.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    r.append(f"  Manifest:  {manifest_path}")
    r.append(f"  Format:    {fmt['format'].upper()}")
    if fmt["format"] == "ground-truth":
        r.append(f"  GT job:    {fmt.get('gt_job_prefix', 'n/a')}")
    r.append("=" * 70)

    r.append("")
    r.append(f"  VERDICT: {'PASS' if passed else 'FAIL'}")
    r.append("")
    r.append(f"  Total records:     {len(records)}")
    r.append(f"  Normal samples:    {class_result['normal_count']}")
    r.append(f"  Anomaly samples:   {class_result['anomaly_count']}")
    r.append(f"  Artifacts checked: {artifact_result['checked']}")
    r.append(f"  Artifacts missing: {artifact_result['missing']}")
    r.append(f"  Encoding issues:   {encoding_result['affected_records']}")
    r.append("")
    r.append(f"  Blocking errors:   {len(blocking)}")
    r.append(f"  Errors:            {len(errors)}")
    r.append(f"  Warnings:          {len(warnings)}")
    r.append("")

    if blocking:
        r.append("-" * 70)
        r.append("  BLOCKING ERRORS (training cannot proceed)")
        r.append("-" * 70)
        for f in blocking[:30]:
            r.append(f"  [{f['check']}] {f['message']}")
        if len(blocking) > 30:
            r.append(f"  ... and {len(blocking) - 30} more blocking errors")
        r.append("")

    if errors:
        r.append("-" * 70)
        r.append("  ERRORS")
        r.append("-" * 70)
        for f in errors[:20]:
            r.append(f"  [{f['check']}] {f['message']}")
        if len(errors) > 20:
            r.append(f"  ... and {len(errors) - 20} more errors")
        r.append("")

    if warnings:
        r.append("-" * 70)
        r.append("  WARNINGS")
        r.append("-" * 70)
        for f in warnings[:20]:
            r.append(f"  [line {f.get('line', '?')}] {f['message']}")
        if len(warnings) > 20:
            r.append(f"  ... and {len(warnings) - 20} more warnings")
        r.append("")

    r.append("=" * 70)
    r.append("  END OF REPORT")
    r.append("=" * 70)
    return "\n".join(r)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DDA Dataset Evaluation Agent")
    parser.add_argument("manifest", help="Path to manifest file (JSONL)")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    args = parser.parse_args()

    print(f"Loading manifest: {args.manifest}")
    records, parse_errors = load_manifest(args.manifest)
    if not records and parse_errors:
        print("FATAL: Could not parse any records.")
        for e in parse_errors:
            print(f"  {e}")
        sys.exit(1)

    print(f"Parsed {len(records)} records.")

    fmt = detect_manifest_format(records)
    print(f"Detected format: {fmt['format'].upper()}")
    print("Running checks...")

    print("  [1/5] Class presence validation...")
    class_result = check_class_presence(records, fmt)

    print("  [2/5] Schema validation...")
    schema_findings = check_schema(records, fmt)

    print("  [3/5] S3 key encoding validation...")
    encoding_result = check_s3_key_encoding(records, fmt)

    print("  [4/5] Artifact existence validation (S3 head requests)...")
    session = boto3.Session(profile_name=args.profile)
    s3 = session.client("s3")
    artifact_result = check_artifact_existence(records, fmt, s3)

    print("  [5/5] Metadata quality checks...")
    metadata_findings = check_metadata_quality(records, fmt)

    report = generate_report(
        args.manifest, fmt, records, parse_errors,
        class_result, schema_findings, encoding_result,
        artifact_result, metadata_findings,
    )
    print("")
    print(report)


if __name__ == "__main__":
    main()
