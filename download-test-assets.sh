#!/bin/bash
#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# =============================================================================
# download-test-assets.sh
#
# Fetches sample test IMAGES (and, optionally, sample MODELS) for quickly trying
# object detection (YOLO and RF-DETR) end-to-end in DDA. These assets are large
# and/or third-party, so they are NOT committed to this repository — this script
# pulls them from their upstream sources on demand.
#
# Everything is written under ./test-assets/ (git-ignored) by default.
#
#   Images (always, unless --no-images):
#     test-assets/images/{bus,zidane,dog,horses,eagle}.jpg
#     COCO-class photos that work as-is for BOTH YOLO and RF-DETR (both are
#     COCO-pretrained), so the same folder can drive either model.
#
#   Models (only with --models):
#     test-assets/models/yolo/yolov8n.onnx            (direct download)
#     test-assets/models/rf-detr/inference_model.onnx (exported locally)
#
# YOLO ships as a ready-to-import ONNX. RF-DETR has no canonical prebuilt ONNX,
# so this script exports one locally via the official `rfdetr[onnx]` package
# (installs into an isolated venv and downloads the pretrained checkpoint).
#
# Usage:
#   ./download-test-assets.sh                 # images only
#   ./download-test-assets.sh --models        # images + YOLO + RF-DETR models
#   ./download-test-assets.sh --models --yolo # images + YOLO model only
#   ./download-test-assets.sh --models --rfdetr --no-images   # RF-DETR model only
#   ./download-test-assets.sh --dest /data/dda-test --force
#
# Options:
#   --models         Also fetch sample models (YOLO + RF-DETR unless narrowed).
#   --yolo           With --models: fetch only the YOLO model.
#   --rfdetr         With --models: fetch only the RF-DETR model.
#   --no-images      Skip the sample images (e.g. when you only want models).
#   --dest DIR       Output base directory (default: ./test-assets).
#   --force          Re-download / re-export even if the file already exists.
#   -v, --version    Print the script version and exit.
#   -h, --help       Print this help and exit.
#
# Environment overrides:
#   RFDETR_VARIANT   RF-DETR variant class suffix to export (default: Nano).
#                    One of: Nano | Small | Medium | Base | Large.
# =============================================================================

set -euo pipefail

SCRIPT_VERSION="1.0.0"
SCRIPT_NAME="$(basename "$0")"

# ── Upstream sources (verified reachable at authoring time) ──────────────────
# COCO-class sample images (shared by YOLO and RF-DETR).
IMAGE_URLS=(
  "https://ultralytics.com/images/bus.jpg"
  "https://ultralytics.com/images/zidane.jpg"
  "https://raw.githubusercontent.com/pjreddie/darknet/master/data/dog.jpg"
  "https://raw.githubusercontent.com/pjreddie/darknet/master/data/horses.jpg"
  "https://raw.githubusercontent.com/pjreddie/darknet/master/data/eagle.jpg"
)
# Prebuilt YOLOv8n ONNX (COCO, input [1,3,640,640] -> output [1,84,8400]).
YOLO_ONNX_URL="https://github.com/shoz-f/onnx_interp/releases/download/models/yolov8n.onnx"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEST="./test-assets"
WANT_IMAGES=1
WANT_MODELS=0
WANT_YOLO=0        # only meaningful when WANT_MODELS=1
WANT_RFDETR=0      # only meaningful when WANT_MODELS=1
NARROWED_MODELS=0  # set when --yolo or --rfdetr is passed
FORCE=0
RFDETR_VARIANT="${RFDETR_VARIANT:-Nano}"

log()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
}

# ── Argument parsing ─────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --models)    WANT_MODELS=1 ;;
    --yolo)      WANT_YOLO=1; NARROWED_MODELS=1 ;;
    --rfdetr)    WANT_RFDETR=1; NARROWED_MODELS=1 ;;
    --no-images) WANT_IMAGES=0 ;;
    --dest)      shift; [ $# -gt 0 ] || { err "--dest requires a directory argument"; exit 2; }; DEST="$1" ;;
    --force)     FORCE=1 ;;
    -v|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
    -h|--help)   usage; exit 0 ;;
    *) err "Unknown option: $1"; echo "Run '${SCRIPT_NAME} --help' for usage." >&2; exit 2 ;;
  esac
  shift
done

# --yolo/--rfdetr imply --models. If neither narrowing flag was given with
# --models, fetch both.
if [ "$NARROWED_MODELS" -eq 1 ]; then WANT_MODELS=1; fi
if [ "$WANT_MODELS" -eq 1 ] && [ "$NARROWED_MODELS" -eq 0 ]; then WANT_YOLO=1; WANT_RFDETR=1; fi

IMAGES_DIR="${DEST}/images"
YOLO_DIR="${DEST}/models/yolo"
RFDETR_DIR="${DEST}/models/rf-detr"

# ── Download helper: curl (preferred) or wget, with retries + atomic write ────
have() { command -v "$1" >/dev/null 2>&1; }

download() {
  # download <url> <dest-file>
  local url="$1" out="$2" tmp
  if [ -f "$out" ] && [ "$FORCE" -eq 0 ]; then
    info "exists, skipping: ${out#"$DEST"/}  (use --force to re-download)"
    return 0
  fi
  mkdir -p "$(dirname "$out")"
  tmp="${out}.part"
  info "downloading: $(basename "$out")"
  if have curl; then
    curl -fL -sS --retry 3 --retry-delay 2 --connect-timeout 30 -o "$tmp" "$url"
  elif have wget; then
    wget -q --tries=3 --timeout=30 -O "$tmp" "$url"
  else
    err "neither curl nor wget is installed; cannot download."
    return 1
  fi
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    err "downloaded file is empty: $url"
    return 1
  fi
  mv -f "$tmp" "$out"
}

# ── Images ───────────────────────────────────────────────────────────────────
fetch_images() {
  log ""
  log "Fetching sample images -> ${IMAGES_DIR}"
  local url name
  for url in "${IMAGE_URLS[@]}"; do
    name="$(basename "$url")"
    download "$url" "${IMAGES_DIR}/${name}"
  done
}

# ── YOLO model ────────────────────────────────────────────────────────────────
fetch_yolo() {
  log ""
  log "Fetching YOLO model -> ${YOLO_DIR}"
  download "$YOLO_ONNX_URL" "${YOLO_DIR}/yolov8n.onnx"
  cat > "${YOLO_DIR}/IMPORT_NOTES.txt" <<'EOF'
YOLOv8n (COCO, 80 classes) — Smart Import settings in the DDA portal:
  Model type:              Object Detection
  Runtime / export format: ONNX Runtime
  Detection architecture:  YOLO
  Input image size:        640
  Number of classes:       80  (COCO; leave class names blank to use defaults)
Input : [1, 3, 640, 640]
Output: [1, 84, 8400]
EOF
  info "wrote ${YOLO_DIR#"$DEST"/}/IMPORT_NOTES.txt"
}

# ── RF-DETR model (exported locally via the official rfdetr package) ──────────
fetch_rfdetr() {
  log ""
  log "Preparing RF-DETR model -> ${RFDETR_DIR}"
  local out_onnx="${RFDETR_DIR}/inference_model.onnx"
  if [ -f "$out_onnx" ] && [ "$FORCE" -eq 0 ]; then
    info "exists, skipping: ${out_onnx#"$DEST"/}  (use --force to re-export)"
    return 0
  fi

  if ! have python3; then
    err "python3 is required to export the RF-DETR model but was not found."
    warn "Skipping RF-DETR. Install Python 3, or export manually (see below), then re-run."
    _rfdetr_manual_notes
    return 1
  fi

  warn "RF-DETR has no prebuilt ONNX to download."
  warn "This will create a Python venv, 'pip install \"rfdetr[onnx]\"' (large: torch, etc.),"
  warn "download the pretrained RF-DETR ${RFDETR_VARIANT} checkpoint, and export it to ONNX."
  warn "This needs internet + several minutes + a few GB of disk."

  local venv="${DEST}/.rfdetr-venv"
  local stage="${RFDETR_DIR}/.export"
  mkdir -p "$RFDETR_DIR"
  rm -rf "$stage"; mkdir -p "$stage"

  info "creating venv: ${venv#"$DEST"/}"
  if ! python3 -m venv "$venv"; then
    err "failed to create Python venv (is the python3-venv package installed?)."
    _rfdetr_manual_notes
    return 1
  fi
  # shellcheck disable=SC1091
  . "$venv/bin/activate"

  info "installing rfdetr[onnx] (this can take a while)..."
  if ! pip install --quiet --upgrade pip >/dev/null 2>&1 \
     || ! pip install --quiet "rfdetr[onnx]"; then
    err "failed to install rfdetr[onnx]."
    deactivate || true
    _rfdetr_manual_notes
    return 1
  fi

  info "exporting RF-DETR ${RFDETR_VARIANT} to ONNX (downloads pretrained weights)..."
  if ! RFDETR_VARIANT="$RFDETR_VARIANT" STAGE_DIR="$stage" python3 - <<'PY'
import os, sys
variant = os.environ["RFDETR_VARIANT"]
stage = os.environ["STAGE_DIR"]
try:
    import rfdetr
    cls = getattr(rfdetr, f"RFDETR{variant}")
except Exception as e:  # noqa: BLE001
    print(f"could not resolve RFDETR{variant} from rfdetr package: {e}", file=sys.stderr)
    sys.exit(3)
# No pretrain_weights -> the package downloads the default pretrained checkpoint.
model = cls()
model.export(output_dir=stage)
PY
  then
    err "RF-DETR export failed."
    deactivate || true
    _rfdetr_manual_notes
    return 1
  fi
  deactivate || true

  # The package writes 'inference_model.onnx' into the output dir.
  if [ -f "${stage}/inference_model.onnx" ]; then
    mv -f "${stage}/inference_model.onnx" "$out_onnx"
    rm -rf "$stage"
    info "wrote ${out_onnx#"$DEST"/}"
    cat > "${RFDETR_DIR}/IMPORT_NOTES.txt" <<EOF
RF-DETR ${RFDETR_VARIANT} (COCO, 80 classes) — Smart Import settings in the DDA portal:
  Model type:              Object Detection
  Runtime / export format: ONNX Runtime
  Detection architecture:  RF-DETR
  Input image size:        560   (RF-DETR default square input; confirm per variant)
  Number of classes:       80    (COCO; leave class names blank to use defaults)
Outputs: two tensors — boxes [1, Q, 4] and logits [1, Q, C] (order handled by shape).

For the SEGMENTATION variant instead of detection, export a seg class from the
rfdetr package (e.g. the RFDETRSeg* preview classes) rather than RFDETR${RFDETR_VARIANT}.
EOF
    info "wrote ${RFDETR_DIR#"$DEST"/}/IMPORT_NOTES.txt"
  else
    err "export finished but ${stage}/inference_model.onnx was not produced."
    _rfdetr_manual_notes
    return 1
  fi
}

_rfdetr_manual_notes() {
  cat >&2 <<'EOF'
--------------------------------------------------------------------------------
To export an RF-DETR ONNX manually:
    python3 -m venv rfdetr-venv && . rfdetr-venv/bin/activate
    pip install "rfdetr[onnx]"
    python3 -c "from rfdetr import RFDETRNano; RFDETRNano().export()"
    # -> output/inference_model.onnx
Then import it in the DDA portal (Object Detection, ONNX Runtime, arch RF-DETR).
--------------------------------------------------------------------------------
EOF
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
  log "=============================================="
  log "DDA test-asset downloader  (v${SCRIPT_VERSION})"
  log "=============================================="
  log "Destination: ${DEST}"
  mkdir -p "$DEST"

  local rfdetr_failed=0

  [ "$WANT_IMAGES" -eq 1 ] && fetch_images
  if [ "$WANT_MODELS" -eq 1 ]; then
    [ "$WANT_YOLO" -eq 1 ] && fetch_yolo
    if [ "$WANT_RFDETR" -eq 1 ]; then
      fetch_rfdetr || rfdetr_failed=1
    fi
  fi

  if [ "$WANT_IMAGES" -eq 0 ] && [ "$WANT_MODELS" -eq 0 ]; then
    warn "Nothing selected (used --no-images without --models). Nothing to do."
  fi

  log ""
  log "Done. Assets are under: ${DEST}"
  [ "$WANT_IMAGES" -eq 1 ] && log "  images: ${IMAGES_DIR}"
  if [ "$WANT_MODELS" -eq 1 ]; then
    [ "$WANT_YOLO" -eq 1 ]   && log "  YOLO:   ${YOLO_DIR}/yolov8n.onnx"
    [ "$WANT_RFDETR" -eq 1 ] && [ "$rfdetr_failed" -eq 0 ] && log "  RF-DETR: ${RFDETR_DIR}/inference_model.onnx"
  fi
  if [ "$rfdetr_failed" -eq 1 ]; then
    log ""
    warn "RF-DETR model was not produced (see messages above). Images/YOLO (if requested) are unaffected."
    exit 1
  fi
}

main "$@"
