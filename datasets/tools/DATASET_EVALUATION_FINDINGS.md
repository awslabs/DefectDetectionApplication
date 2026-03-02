# Dataset Evaluation Findings: Ground Truth to DDA Pipeline

## Summary

An end-to-end evaluation of the SageMaker Ground Truth → DDA manifest transformation pipeline
revealed two critical issues that can silently produce invalid training manifests. This document
describes the findings, the tools created to detect them, and the corrected transformation approach.

## Background

DDA segmentation training requires an augmented manifest in a specific format with five keys per record:

```json
{
  "source-ref": "s3://bucket/image.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": { "class-name": "anomaly", "type": "groundtruth/image-classification", ... },
  "anomaly-mask-ref": "s3://bucket/mask.png",
  "anomaly-mask-ref-metadata": { "internal-color-map": {...}, "type": "groundtruth/semantic-segmentation", ... }
}
```

SageMaker Ground Truth semantic segmentation jobs produce a different format with only three keys:

```json
{
  "source-ref": "s3://bucket/image.jpg",
  "{job-name}-ref": "s3://bucket/mask.png",
  "{job-name}-ref-metadata": { "internal-color-map": {...}, "type": "groundtruth/semantic-segmentation", ... }
}
```

The key difference: Ground Truth output has **no classification label** (`anomaly-label`).
A transformation step is required to bridge the two formats.

## Finding 1: Existing Transformer Produces Invalid Manifests

**Severity**: Critical (silent data corruption)

**Location**: `edge-cv-portal/backend/layers/shared/python/manifest_transformer.py`

**Issue**: The `detect_ground_truth_attributes()` function assumes the GT manifest has separate
label and mask attribute pairs. For segmentation-only GT output, there is only one attribute pair
(`{job}-ref` / `{job}-ref-metadata`). The function treats the mask ref key as both the label
attribute and the mask attribute, resulting in:

- `anomaly-label` set to an S3 URI string (the mask path) instead of an integer (0 or 1)
- `anomaly-label-metadata` containing segmentation metadata instead of classification metadata
- `anomaly-mask-ref` duplicating the same S3 URI as `anomaly-label`
- Zero errors reported — the transformer claims 100% success

**Impact**: The corrupted manifest would fail at SageMaker training time because `anomaly-label`
must be an integer. The failure is silent at transformation time, delaying detection to the
training stage.

**Reproduction**:
```bash
# Using the existing transformer on raw GT segmentation output:
python3 -c "
import sys
sys.path.insert(0, 'edge-cv-portal/backend/layers/shared/python')
from manifest_transformer import transform_manifest_lines
import json

with open('output.manifest') as f:
    lines = [l.strip() for l in f if l.strip()]

result = transform_manifest_lines(lines, 'segmentation')
# result['stats'] shows 83 transformed, 0 skipped — but all are invalid
entry = json.loads(result['transformed_lines'][0])
print(type(entry['anomaly-label']))  # <class 'str'> — should be int
print(entry['anomaly-label'][:50])   # s3://... — should be 0 or 1
"
```

## Finding 2: Ground Truth U+F03A Character Substitution

**Severity**: High (artifact mismatch)

**Issue**: SageMaker Ground Truth substitutes colon characters (`:`) with Unicode Private Use Area
character U+F03A when generating S3 keys for annotation output files. This character is invisible
in most terminal and console displays but causes S3 `GetObject` calls to fail because the actual
S3 key contains U+F03A while the manifest reference may or may not match.

**Observed behavior**:
- `aws s3 ls` displays the filename without the character (silently strips it)
- `aws s3api list-objects-v2` also strips it from display
- The S3 console shows garbled box characters in the filename
- `repr()` in Python reveals `\uf03a` in the key string

**Note**: This behavior was observed in the original GT job (`reserior-zip-ties-2`) but was
**not** reproduced in the evaluation GT job (`gt-eval-seg-20260220-011205`). The occurrence
may depend on the GT job configuration, region, or service version.

**Detection**:
```bash
python3 evaluate_manifest.py manifest.ndjson --profile my-profile
# Reports BLOCKING errors for any S3 URIs containing U+F03A
```

## Tools Created

### 1. Dataset Evaluation Agent (`evaluate_manifest.py`)

Read-only validation tool that checks manifest readiness before transformation or training.

**Checks performed**:
1. **Class presence** — verifies both normal (0) and anomaly (1) samples exist
2. **Schema validation** — checks required keys for DDA or Ground Truth format
3. **S3 key encoding** — detects U+F03A and other hidden Unicode characters
4. **Artifact existence** — HEAD requests to verify every referenced S3 object exists
5. **Metadata quality** — validates label/class-name consistency and color map structure

**Auto-detects** manifest format (DDA vs raw Ground Truth) by scanning all record keys.

```bash
# Evaluate a raw GT manifest
python3 evaluate_manifest.py output.manifest --profile my-profile

# Evaluate a DDA-transformed manifest
python3 evaluate_manifest.py output-dda.ndjson --profile my-profile
```

### 2. GT-to-DDA Transformer (`transform_gt_to_dda.py`)

Corrected transformation that properly handles GT segmentation-only output.

**Key differences from the existing transformer**:
- Recognizes that GT segmentation output has a single attribute pair (mask only)
- Maps `{job}-ref` → `anomaly-mask-ref` (not `anomaly-label`)
- Infers `anomaly-label` from the source image filename convention
- Constructs `anomaly-label-metadata` with `type: groundtruth/image-classification`
- Skips unlabeled and failed records from intermediate manifests

**Label inference**: Uses filename prefix convention:
- `anomaly_*` → label 1
- `normal_*` or any other prefix → label 0

```bash
# Preview transformation (no file written)
python3 transform_gt_to_dda.py output.manifest

# Write transformed manifest
python3 transform_gt_to_dda.py output.manifest output-dda.ndjson

# Then validate
python3 evaluate_manifest.py output-dda.ndjson --profile my-profile
```

## Recommended Workflow

```
Ground Truth Job
       │
       ▼
  output.manifest (raw GT format)
       │
       ▼
  evaluate_manifest.py ──► catches missing anomaly-label (BLOCKING)
       │                   catches U+F03A encoding (BLOCKING)
       │                   catches missing artifacts (BLOCKING)
       ▼
  transform_gt_to_dda.py ──► output-dda.ndjson (DDA format)
       │
       ▼
  evaluate_manifest.py ──► confirms PASS before training
       │
       ▼
  DDA Training Job
```

## Test Results

### Raw GT Output Evaluation
```
Format:    GROUND-TRUTH
VERDICT:   FAIL
Records:   83
Blocking:  1 (missing anomaly-label)
Errors:    0
Artifacts: 166 checked, 0 missing
```

### After Corrected Transformation
```
Format:    DDA
VERDICT:   PASS
Records:   83 (39 anomaly, 44 normal)
Blocking:  0
Errors:    0
Artifacts: 166 checked, 0 missing
```

### After Existing Transformer (for comparison)
```
anomaly-label type: str (all 83 records)
anomaly-label value: S3 URI string (should be 0 or 1)
Result: silently corrupted — would fail at training time
```
