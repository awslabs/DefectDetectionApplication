#!/bin/bash
# Build script for the video Lambda Layer (static-camera-video-loop).
#
# Usage: build.sh <output-dir>
#
# Installs the pinned OpenCV + numpy wheels (requirements.txt) for the
# Lambda runtime (Python 3.12, x86_64 manylinux) into <output-dir>/python,
# without OpenCV's bundled Haar cascades (cv2/data, unused by the video
# validation). The ComputeStack's VideoLayer runs this at synth time as its
# local bundling step.
#
# The installed tree (about 190 MB) is cached per requirements.txt content
# under ${DDA_VIDEO_LAYER_CACHE:-~/.cache/dda-video-layer}, and each synth
# hardlinks it into the output (a plain copy when the output is on another
# filesystem), so repeated synths — every infra test synthesizes the
# ComputeStack — do not reinstall 190 MB each time.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <output-dir>" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$1"
requirements="$here/requirements.txt"
key="$(sha256sum "$requirements" | cut -c1-16)"
cache_root="${DDA_VIDEO_LAYER_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/dda-video-layer}"
cache="$cache_root/$key"

if [ ! -f "$cache/.complete" ]; then
  mkdir -p "$cache_root"
  staging="$(mktemp -d "$cache_root/.build-XXXXXX")"
  trap 'rm -rf "$staging"' EXIT
  pip install -r "$requirements" -t "$staging/python" \
      --platform manylinux2014_x86_64 \
      --implementation cp \
      --python-version 3.12 \
      --only-binary=:all: \
      --no-compile \
      --quiet
  rm -rf "$staging/python/cv2/data"
  touch "$staging/.complete"
  # The cache directory only ever appears through this atomic rename of a
  # complete tree. Concurrent builds race benignly: the first rename wins,
  # later ones find the directory in place and discard their own tree.
  mv -T "$staging" "$cache" 2>/dev/null || true
  rm -rf "$staging"
  trap - EXIT
fi
if [ ! -f "$cache/.complete" ]; then
  echo "video layer cache $cache is incomplete; remove it and retry" >&2
  exit 1
fi

mkdir -p "$out"
rm -rf "$out/python"
cp -al "$cache/python" "$out/python" 2>/dev/null || cp -a "$cache/python" "$out/python"
echo "Video Lambda Layer ready in $out/python"
