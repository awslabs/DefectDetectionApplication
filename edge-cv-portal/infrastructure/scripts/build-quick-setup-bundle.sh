#!/usr/bin/env bash
#
# build-quick-setup-bundle.sh — Station Quick Setup bundle packager.
#
# Runs during CDK asset bundling (see compute-stack.ts QuickSetupBundle
# BucketDeployment). It packages the repository's station_install/ tree
# (including quick_setup/) into a single self-contained installer artifact and
# emits SHA-256 sidecars computed over the EXACT bytes that are uploaded, so
# the station can verify integrity end to end (Requirements 4.3, 4.4, 4.5).
#
# Outputs written to OUTPUT_DIR (uploaded verbatim under quick-setup/current/):
#   setup-bundle.tar.gz          the full station_install tree
#   setup-bundle.tar.gz.sha256   `sha256sum -c`-compatible sidecar
#   bootstrap.sh                 the small, static station bootstrap script
#   bootstrap.sh.sha256          `sha256sum -c`-compatible sidecar
#   manifest.json                machine-readable index of the above + hashes
#
# Usage:
#   build-quick-setup-bundle.sh [INPUT_DIR] [OUTPUT_DIR]
#
# When invoked by CDK's Docker bundler the defaults match the mounted volumes
# (/asset-input, /asset-output); the local bundler passes them explicitly.
set -euo pipefail

INPUT_DIR="${1:-/asset-input}"
OUTPUT_DIR="${2:-/asset-output}"

if [[ ! -d "${INPUT_DIR}" ]]; then
  echo "build-quick-setup-bundle: input dir not found: ${INPUT_DIR}" >&2
  exit 1
fi
if [[ ! -f "${INPUT_DIR}/setup_station.sh" ]]; then
  echo "build-quick-setup-bundle: ${INPUT_DIR} does not look like station_install/ (missing setup_station.sh)" >&2
  exit 1
fi
if [[ ! -f "${INPUT_DIR}/quick_setup/bootstrap.sh" ]]; then
  echo "build-quick-setup-bundle: missing quick_setup/bootstrap.sh under ${INPUT_DIR}" >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"

BUNDLE_NAME="setup-bundle.tar.gz"
BOOTSTRAP_NAME="bootstrap.sh"

# Stage a clean copy of station_install/ so build/test cruft never ships in the
# bundle (the station must provision from these bytes with no repo access).
STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

cp -R "${INPUT_DIR}/." "${STAGING}/"

# Drop artifacts that must not reach the station.
find "${STAGING}" \
  \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.hypothesis' \
     -o -name '.mypy_cache' -o -name 'node_modules' -o -name '.git' \
     -o -name 'tests' \) \
  -prune -exec rm -rf {} + 2>/dev/null || true
find "${STAGING}" -name '*.pyc' -delete 2>/dev/null || true
# The bundling script itself is never part of the shipped tree.
find "${STAGING}" -name 'build-quick-setup-bundle.sh' -delete 2>/dev/null || true

# Produce a reproducible tarball: stable file ordering, zeroed timestamps and
# ownership, and a timestamp-free gzip stream. Reproducible bytes mean the
# SHA-256 only changes when the station_install contents actually change, and
# the checksum below is computed over the exact file we upload.
tar --sort=name \
    --mtime='UTC 1970-01-01' \
    --owner=0 --group=0 --numeric-owner \
    -C "${STAGING}" -cf - . \
  | gzip -n -9 > "${OUTPUT_DIR}/${BUNDLE_NAME}"

# Copy the standalone bootstrap script served by the token-free /bootstrap
# route. It travels alongside the bundle (not only inside it) because the
# station fetches it before it has the bundle.
cp "${INPUT_DIR}/quick_setup/bootstrap.sh" "${OUTPUT_DIR}/${BOOTSTRAP_NAME}"

# Compute SHA-256 sidecars over the exact output bytes. Emit the standard
# `sha256sum` two-field format so the station can verify with `sha256sum -c`.
compute_sha256() {
  # prints the 64-char lowercase hex digest of "$1" to stdout
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    # macOS / BSD fallback used by the local bundler on developer machines
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

BUNDLE_SHA256="$(compute_sha256 "${OUTPUT_DIR}/${BUNDLE_NAME}")"
BOOTSTRAP_SHA256="$(compute_sha256 "${OUTPUT_DIR}/${BOOTSTRAP_NAME}")"

printf '%s  %s\n' "${BUNDLE_SHA256}" "${BUNDLE_NAME}" > "${OUTPUT_DIR}/${BUNDLE_NAME}.sha256"
printf '%s  %s\n' "${BOOTSTRAP_SHA256}" "${BOOTSTRAP_NAME}" > "${OUTPUT_DIR}/${BOOTSTRAP_NAME}.sha256"

# Machine-readable manifest indexing the artifacts and their digests.
cat > "${OUTPUT_DIR}/manifest.json" <<EOF
{
  "bundle": "${BUNDLE_NAME}",
  "bundle_sha256": "${BUNDLE_SHA256}",
  "bootstrap": "${BOOTSTRAP_NAME}",
  "bootstrap_sha256": "${BOOTSTRAP_SHA256}"
}
EOF

echo "build-quick-setup-bundle: wrote ${BUNDLE_NAME} (sha256=${BUNDLE_SHA256})" >&2
echo "build-quick-setup-bundle: wrote ${BOOTSTRAP_NAME} (sha256=${BOOTSTRAP_SHA256})" >&2
