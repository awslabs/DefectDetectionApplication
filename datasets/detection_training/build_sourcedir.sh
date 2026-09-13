#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build the SageMaker script-mode sourcedir.tar.gz for detector training.
#
# train.py resolves the dataset converter as a SIBLING of itself
# (CODE_DIR / "manifest_to_detector_dataset.py"), and that converter imports
# dedupe_frames from its own directory. SageMaker extracts the tarball flat,
# so all four files must sit at the tarball ROOT -- not under datasets/.
# That is the whole reason this script exists: tarring the repo layout as-is
# produces a bundle that dies in dataset conversion, minutes into a GPU job.
#
# Usage:
#   ./build_sourcedir.sh [output.tar.gz]
#
# Then upload and launch -- see README.md.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATASETS="$(dirname "$HERE")"
OUT="${1:-$HERE/sourcedir.tar.gz}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp "$HERE/train.py"                            "$STAGE/"
cp "$HERE/requirements.txt"                    "$STAGE/"
cp "$DATASETS/manifest_to_detector_dataset.py" "$STAGE/"
cp "$DATASETS/dedupe_frames.py"                "$STAGE/"

tar -czf "$OUT" -C "$STAGE" \
    train.py requirements.txt \
    manifest_to_detector_dataset.py dedupe_frames.py

echo "wrote $OUT"
tar -tzf "$OUT"
