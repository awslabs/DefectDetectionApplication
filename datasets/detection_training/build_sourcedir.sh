#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build the SageMaker script-mode sourcedir.tar.gz for detector training.
#
# The entry points (train.py for YOLO, train_rfdetr.py for RF-DETR) resolve
# _common.py and the dataset converter as SIBLINGS of themselves
# (CODE_DIR / "manifest_to_detector_dataset.py"), and that converter imports
# dedupe_frames from its own directory. SageMaker extracts the tarball flat,
# so every file must sit at the tarball ROOT -- not under datasets/.
# That is the whole reason this script exists: tarring the repo layout as-is
# produces a bundle that dies in dataset conversion, minutes into a GPU job.
#
# One arch per tarball: the chosen entry point plus ITS requirements file
# renamed to requirements.txt (the SageMaker toolkit pip-installs exactly that
# name), so the other arch's heavyweight pins are never installed. This
# mirrors detection_training.build_sourcedir_tarball, which the portal uses.
#
# Usage:
#   ./build_sourcedir.sh [--arch yolo|rf_detr] [output.tar.gz]
#
#   --arch yolo     (default) train.py + requirements.txt
#   --arch rf_detr  train_rfdetr.py + requirements-rfdetr.txt (as requirements.txt)
#
# Default output: sourcedir.tar.gz (yolo) / sourcedir-rfdetr.tar.gz (rf_detr)
# beside this script. Then upload and launch -- see README.md.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") [--arch yolo|rf_detr] [output.tar.gz]" >&2
    exit 2
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATASETS="$(dirname "$HERE")"

ARCH="yolo"
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --arch)
            [ $# -ge 2 ] || usage
            ARCH="$2"; shift 2 ;;
        --arch=*)
            ARCH="${1#--arch=}"; shift ;;
        -h|--help)
            usage ;;
        -*)
            echo "unknown option: $1" >&2; usage ;;
        *)
            [ -z "$OUT" ] || usage
            OUT="$1"; shift ;;
    esac
done

case "$ARCH" in
    yolo)
        ENTRY="train.py"
        REQS="requirements.txt"
        DEFAULT_OUT="$HERE/sourcedir.tar.gz" ;;
    rf_detr)
        ENTRY="train_rfdetr.py"
        REQS="requirements-rfdetr.txt"
        DEFAULT_OUT="$HERE/sourcedir-rfdetr.tar.gz" ;;
    *)
        echo "unknown arch: '$ARCH' (expected yolo or rf_detr)" >&2; usage ;;
esac
OUT="${OUT:-$DEFAULT_OUT}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp "$HERE/$ENTRY"                              "$STAGE/"
cp "$HERE/$REQS"                               "$STAGE/requirements.txt"
cp "$DATASETS/manifest_to_detector_dataset.py" "$STAGE/"
cp "$DATASETS/dedupe_frames.py"                "$STAGE/"
cp "$HERE/_common.py"                          "$STAGE/"

tar -czf "$OUT" -C "$STAGE" \
    "$ENTRY" requirements.txt \
    manifest_to_detector_dataset.py dedupe_frames.py \
    _common.py

echo "wrote $OUT (arch=$ARCH)"
tar -tzf "$OUT"
