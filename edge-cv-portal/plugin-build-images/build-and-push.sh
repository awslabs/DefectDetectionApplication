#!/usr/bin/env bash
# Build and push the per-architecture dda-plugin-build images (custom-node-designer).
#
# Usage:
#   ./build-and-push.sh                 # build + push all five architectures
#   ./build-and-push.sh arm64_jp5       # build + push one architecture
#
# The image tag is the architecture name, matching the default tag the
# CodeBuild projects in node-designer-stack.ts pull (override the tag
# suffix there with the `pluginBuildImageTag` CDK context and here with
# TAG_SUFFIX=<suffix>).
#
# x86_64 images target linux/amd64 (emulated via qemu on an arm64 host);
# the JetPack images are native linux/arm64 builds from NVIDIA L4T bases.
set -euo pipefail
cd "$(dirname "$0")"

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-164152369890}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-dda-plugin-build}"
TAG_SUFFIX="${TAG_SUFFIX:-}"
REPO_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO"

ALL_ARCHES=(x86_64 x86_64_nvidia arm64_jp4 arm64_jp5 arm64_jp6)

platform_for() {
  case "$1" in
    x86_64|x86_64_nvidia) echo linux/amd64 ;;
    arm64_*)              echo linux/arm64 ;;
    *) echo "unknown architecture: $1" >&2; return 1 ;;
  esac
}

ARCHES=("${ALL_ARCHES[@]}")
if [ "$#" -ge 1 ]; then
  ARCHES=("$1")
  platform_for "$1" > /dev/null
fi

echo "Logging in to $REPO_URI"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
      "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

for arch in "${ARCHES[@]}"; do
  platform="$(platform_for "$arch")"
  tag="$arch${TAG_SUFFIX:+-$TAG_SUFFIX}"
  echo "=== Building $REPO_URI:$tag ($platform) ==="
  docker buildx build \
    --platform "$platform" \
    -f "Dockerfile.$arch" \
    -t "$REPO_URI:$tag" \
    --provenance=false \
    --push \
    .
  echo "=== Pushed $REPO_URI:$tag ==="
done
