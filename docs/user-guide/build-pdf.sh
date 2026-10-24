#!/usr/bin/env bash
# Render the user guide (README.md + NN-*.md with their images) into one PDF.
#
# Needs Docker only: markdown-it and Chromium run inside the Playwright
# container, so nothing is installed on the host. The npm packages are cached
# under ~/.cache/dda-user-guide-pdf between runs.
#
#   docs/user-guide/build-pdf.sh                        # writes docs/user-guide/DDA-User-Guide.pdf
#   docs/user-guide/build-pdf.sh ~/Desktop/guide.pdf    # writes elsewhere (not under /tmp: Docker
#                                                       # may see a different /tmp than your shell)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/DDA-User-Guide.pdf}"
mkdir -p "$(dirname "$out")"
out_dir="$(cd "$(dirname "$out")" && pwd)"
out_name="$(basename "$out")"
work="${XDG_CACHE_HOME:-$HOME/.cache}/dda-user-guide-pdf"
mkdir -p "$work"
cp "$here/build-pdf.js" "$work/"
image="mcr.microsoft.com/playwright:v1.63.0-noble"
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/work -e npm_config_cache=/work/.npm \
  -v "$work:/work" -w /work -v "$here:/guide" -v "$out_dir:/out" "$image" \
  bash -c 'npm install --no-audit --no-fund playwright@1.63.0 markdown-it@14.1.0 markdown-it-anchor@9.2.0 2>&1 | grep -v "^npm notice" || true; node build-pdf.js /guide "/out/'"$out_name"'"'
echo "wrote $out"
