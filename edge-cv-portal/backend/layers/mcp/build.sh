#!/bin/bash
# Build script for the MCP Lambda Layer (Portal_MCP_Server).
#
# Usage: build.sh <output-dir>
#
# Installs every wheel pinned in requirements.lock.txt (all transitive
# dependencies, with hashes) into <output-dir>/python for the python3.11
# Lambda runtime on x86_64: binary wheels only, resolved for CPython 3.11
# whatever interpreter runs pip, and no dependency resolution at build time
# (--no-deps; the lock is complete). The McpStack's McpLayer runs this script
# at CDK synth as its local bundling step, and inside the python3.11 bundling
# image as the Docker fallback, so every synth and deploy path builds the
# same layer.
#
# Fails with a non-zero exit when pip fails (a missing wheel, a hash
# mismatch, no index access) and when any pinned distribution is absent from
# the output afterwards, so a synth never stages an empty layer. Exit code 3
# means this host has no pip; the local bundler then falls back to Docker.
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "usage: $0 <output-dir>" >&2
  exit 2
fi
if ! command -v pip >/dev/null 2>&1; then
  echo "build.sh: pip not found on PATH" >&2
  exit 3
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="$here/requirements.lock.txt"
target="$1/python"
rm -rf "$target"
mkdir -p "$target"
if ! pip install \
    --platform manylinux2014_x86_64 \
    --python-version 3.11 \
    --implementation cp \
    --only-binary=:all: \
    --no-deps \
    --require-hashes \
    --no-compile \
    --disable-pip-version-check \
    --quiet \
    -r "$lock" \
    -t "$target"; then
  echo "build.sh: pip could not install the wheels pinned in $lock" >&2
  exit 1
fi
# Every pin must have landed as <name>-<version>.dist-info (names compared
# case-insensitively with '-' and '.' folded to '_', as wheels spell them).
pins="$(sed -n 's/^\([A-Za-z0-9][A-Za-z0-9._-]*==[^ ;\\]*\).*/\1/p' "$lock")"
if [ -z "$pins" ]; then
  echo "build.sh: no pins found in $lock" >&2
  exit 1
fi
missing=0
while IFS= read -r pin; do
  name="${pin%%==*}"
  version="${pin#*==}"
  want="$(printf '%s' "$name" | tr 'A-Z.-' 'a-z__')"
  found=0
  for dist in "$target"/*-"$version".dist-info; do
    [ -d "$dist" ] || continue
    have="$(basename "$dist" "-$version.dist-info" | tr 'A-Z.-' 'a-z__')"
    if [ "$have" = "$want" ]; then
      found=1
      break
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "build.sh: $name==$version is missing from $target" >&2
    missing=1
  fi
done <<< "$pins"
if [ "$missing" -ne 0 ]; then
  exit 1
fi
echo "MCP Lambda Layer ready in $target ($(printf '%s\n' "$pins" | wc -l) pinned distributions)"
