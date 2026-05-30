#!/bin/bash
set -e
set -o pipefail

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

# Step tracking
STEP=0
TOTAL_STEPS=8
START_TIME=$(date +%s)

print_step() {
    STEP=$((STEP + 1))
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "[$STEP/$TOTAL_STEPS] $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Usage: ./gdk-component-build-and-publish.sh [ARCH] [JETPACK]
#   ARCH:    x86_64 or aarch64 (default: auto-detect from host)
#   JETPACK: 4 or 5 (required for aarch64 builds)
#
# Supported configurations:
#   x86_64           -> aws.edgeml.dda.LocalServer.amd64      (Ubuntu 20.04)
#   aarch64 + JP4    -> aws.edgeml.dda.LocalServer.arm64      (Ubuntu 18.04, L4T r32.x)
#   aarch64 + JP5    -> aws.edgeml.dda.LocalServer.arm64JP5   (Ubuntu 20.04, L4T r35.x)
#
# Examples:
#   ./gdk-component-build-and-publish.sh                 # auto-detect arch (x86_64)
#   ./gdk-component-build-and-publish.sh aarch64 4       # ARM64 JetPack 4.6
#   ./gdk-component-build-and-publish.sh aarch64 5       # ARM64 JetPack 5
#
# Argument parsing is order-independent and accepts both the positional JetPack
# number (4|5) and the --jp4/--jp5 flags (kept as backward-compatible aliases).
ARCH=""
JETPACK=""
for arg in "$@"; do
    case "$arg" in
        x86_64|amd64)        ARCH="x86_64" ;;
        aarch64|arm64)       ARCH="aarch64" ;;
        4|jp4|JP4|--jp4)     JETPACK="4" ;;
        5|jp5|JP5|--jp5)     JETPACK="5" ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [x86_64|aarch64] [4|5]"
            exit 1
            ;;
    esac
done

# Default ARCH to the host architecture when not supplied.
if [ -z "$ARCH" ]; then
    ARCH=$(uname -m)
fi

# Determine recipe file and component name.
case $ARCH in
    x86_64)
        RECIPE_FILE="recipe-amd64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.amd64"
        ;;
    aarch64)
        # JetPack version is required for aarch64 so we never silently publish
        # the wrong component (passing nothing previously defaulted to JP4 and
        # produced aws.edgeml.dda.LocalServer.arm64 even when JP5 was intended).
        if [ -z "$JETPACK" ]; then
            echo "ERROR: JetPack version is required for aarch64 builds."
            echo "Usage: $0 aarch64 <4|5>"
            echo "  4 = JetPack 4.6 (Ubuntu 18.04, L4T r32.x)  -> aws.edgeml.dda.LocalServer.arm64"
            echo "  5 = JetPack 5   (Ubuntu 20.04, L4T r35.x)  -> aws.edgeml.dda.LocalServer.arm64JP5"
            exit 1
        fi
        if [ "$JETPACK" = "5" ]; then
            RECIPE_FILE="recipe-arm64-jp5.yaml"
            COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64JP5"
        else
            RECIPE_FILE="recipe-arm64.yaml"
            COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64"
        fi
        ;;
    *)
        echo "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

print_step "Detecting architecture and preparing configuration"
echo "Architecture: $ARCH"
echo "JetPack version: ${JETPACK:-n/a}"
echo "Component name: $COMPONENT_NAME"
echo "Recipe file: $RECIPE_FILE"

# Use architecture-specific recipe
cp $RECIPE_FILE recipe.yaml

print_step "Creating GDK configuration"

# Use the configured AWS region (fall back to env vars; aws configure get
# returns exit 1 when unset, which would abort the script under `set -e`)
GDK_REGION=$(aws configure get region 2>/dev/null || true)
if [ -z "$GDK_REGION" ]; then
    GDK_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
if [ -z "$GDK_REGION" ]; then
    echo "❌ ERROR: No AWS region configured."
    echo "   Run: aws configure set region <your-region>"
    echo "   Or:  export AWS_REGION=<your-region>"
    exit 1
fi
echo "Using region: $GDK_REGION"

# Create gdk-config.json with architecture-specific component name
cat > gdk-config.json << EOF
{
  "component": {
    "${COMPONENT_NAME}": {
      "author": "Amazon",
      "version": "NEXT_PATCH",
      "build": {
        "build_system": "custom",
        "custom_build_command": [
          "bash",
          "build-custom.sh",
          "${COMPONENT_NAME}",
          "NEXT_PATCH"
        ]
      },
      "publish": {
        "bucket": "dda-component",
        "region": "${GDK_REGION}"
      }
    }
  },
  "gdk_version": "1.0.0"
}
EOF
echo "✓ GDK configuration created"

print_step "Cleaning build directories"
# Clean GDK cache and build directories
rm -rf greengrass-build/
rm -rf .gdk/
echo "✓ Build directories cleaned"

print_step "Building LocalServer component"
# Build and publish component
BUILD_LOG="/tmp/gdk-build-$(date +%s).log"
echo "Build log: $BUILD_LOG"
echo ""

# Run build with real-time output and log capture
if gdk component build 2>&1 | tee "$BUILD_LOG"; then
    echo ""
    echo "✓ Component built successfully"
else
    BUILD_EXIT_CODE=${PIPESTATUS[0]}
    echo ""
    echo "✗ Component build failed (exit code: $BUILD_EXIT_CODE)"
    echo ""
    echo "Last 50 lines of build log:"
    echo "---"
    tail -50 "$BUILD_LOG"
    echo "---"
    echo ""
    echo "Full log saved to: $BUILD_LOG"
    exit 1
fi

print_step "Publishing LocalServer component"
PUBLISH_LOG="/tmp/gdk-publish-$(date +%s).log"
echo "Publish log: $PUBLISH_LOG"
echo ""

# Resolve account/region up front (needed for the ECR path and tagging).
PUB_REGION=$(aws configure get region 2>/dev/null || true)
if [ -z "$PUB_REGION" ]; then
    PUB_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
PUB_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)

# Measure total built-artifact size. Greengrass rejects single artifacts over
# 2 GB, so when the packaged zip exceeds that we publish the Docker images to
# ECR and ship only the scripts/compose via S3 (the >2GB path re-integrated
# from main, adapted to this branch's single-zip layout).
GG_LIMIT=2147483648
ARTIFACT_DIR="greengrass-build/artifacts/${COMPONENT_NAME}/NEXT_PATCH"
TOTAL_SIZE=0
for z in "$ARTIFACT_DIR"/*.zip; do
    [ -f "$z" ] || continue
    sz=$(stat --format=%s "$z" 2>/dev/null || echo 0)
    TOTAL_SIZE=$((TOTAL_SIZE + sz))
done
echo "Total artifact size: $(numfmt --to=iec "$TOTAL_SIZE" 2>/dev/null || echo "${TOTAL_SIZE} bytes")"

if [ "$TOTAL_SIZE" -gt "$GG_LIMIT" ]; then
    echo ""
    echo "Artifacts exceed the Greengrass 2GB limit — using ECR for Docker images + S3 for scripts."
    echo ""

    if [ -z "$PUB_REGION" ] || [ -z "$PUB_ACCOUNT_ID" ] || [ "$PUB_ACCOUNT_ID" = "None" ]; then
        echo "✗ ECR publish requires a resolvable AWS region and account."
        echo "  region='$PUB_REGION' account='$PUB_ACCOUNT_ID'"
        exit 1
    fi

    ECR_REGISTRY="${PUB_ACCOUNT_ID}.dkr.ecr.${PUB_REGION}.amazonaws.com"
    ECR_REPO_BACKEND="${ECR_REGISTRY}/dda/flask-app"
    ECR_REPO_FRONTEND="${ECR_REGISTRY}/dda/react-webapp"
    S3_BUCKET="dda-component-${PUB_REGION}-${PUB_ACCOUNT_ID}"

    # Determine the next component version (bump patch; start at 1.0.0).
    LATEST_VERSION=$(aws greengrassv2 list-component-versions \
        --arn "arn:aws:greengrass:${PUB_REGION}:${PUB_ACCOUNT_ID}:components:${COMPONENT_NAME}" \
        --query 'componentVersions[0].componentVersion' --output text 2>/dev/null || echo "None")
    if [ "$LATEST_VERSION" = "None" ] || [ -z "$LATEST_VERSION" ]; then
        COMPONENT_VERSION="1.0.0"
    else
        V_MAJOR=$(echo "$LATEST_VERSION" | cut -d. -f1)
        V_MINOR=$(echo "$LATEST_VERSION" | cut -d. -f2)
        V_PATCH=$(echo "$LATEST_VERSION" | cut -d. -f3)
        COMPONENT_VERSION="${V_MAJOR}.${V_MINOR}.$((V_PATCH + 1))"
    fi
    echo "Publishing version: $COMPONENT_VERSION"

    # Authenticate to ECR and ensure the repositories exist.
    aws ecr get-login-password --region "$PUB_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
    aws ecr describe-repositories --repository-names dda/flask-app --region "$PUB_REGION" >/dev/null 2>&1 || \
        aws ecr create-repository --repository-name dda/flask-app --region "$PUB_REGION" >/dev/null
    aws ecr describe-repositories --repository-names dda/react-webapp --region "$PUB_REGION" >/dev/null 2>&1 || \
        aws ecr create-repository --repository-name dda/react-webapp --region "$PUB_REGION" >/dev/null

    # Push the locally-built images (left tagged flask-app/react-webapp by build-custom.sh).
    echo "Pushing flask-app to ECR..."
    docker tag flask-app:latest "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"
    docker push "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"
    echo "Pushing react-webapp to ECR..."
    docker tag react-webapp:latest "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"
    docker push "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"

    # Repackage a scripts-only zip with the SAME name/layout as the full zip
    # (minus the image tars) so the recipe's decompressedPath references stay valid.
    STAGE_DIR="custom-build/${COMPONENT_NAME}"
    if [ ! -d "$STAGE_DIR" ]; then
        echo "✗ Expected staging dir $STAGE_DIR not found (build-custom.sh output)."
        exit 1
    fi
    rm -f "${STAGE_DIR}/flask-app.tar" "${STAGE_DIR}/react-webapp.tar" "${STAGE_DIR}"/.tmp-* 2>/dev/null || true
    APP_ZIP="custom-build/${COMPONENT_NAME}-${ARCH}.zip"
    rm -f "$APP_ZIP"
    zip -r -X "$APP_ZIP" "custom-build/${COMPONENT_NAME}" -x '*/.tmp-*'

    S3_KEY="${COMPONENT_NAME}/${COMPONENT_VERSION}/${COMPONENT_NAME}-${ARCH}.zip"
    S3_URI="s3://${S3_BUCKET}/${S3_KEY}"
    echo "Uploading scripts artifact to ${S3_URI} ($(numfmt --to=iec "$(stat --format=%s "$APP_ZIP")" 2>/dev/null || echo "?"))..."
    aws s3 cp "$APP_ZIP" "$S3_URI" --region "$PUB_REGION"

    # Rewrite the recipe: docker: image artifacts + S3 scripts artifact, add the
    # Docker/TES dependencies, and swap the in-Install `docker load -i ...tar`
    # commands for `docker tag <ecr> ...:latest` (preserving CSI/host_scripts steps).
    python3 -c "import yaml" 2>/dev/null || pip3 install --user pyyaml >/dev/null 2>&1 || true
    ECR_RECIPE="greengrass-build/recipes/recipe-ecr.yaml"
    mkdir -p greengrass-build/recipes
    python3 - "recipe.yaml" "$ECR_RECIPE" "$ECR_REPO_BACKEND" "$ECR_REPO_FRONTEND" "$COMPONENT_VERSION" "$S3_URI" <<'PYEOF'
import re, sys, yaml

src, out, ecr_backend, ecr_frontend, version, s3_uri = sys.argv[1:7]

with open(src) as f:
    recipe = yaml.safe_load(f)

recipe['ComponentVersion'] = version

deps = recipe.setdefault('ComponentDependencies', {})
deps['aws.greengrass.DockerApplicationManager'] = {'VersionRequirement': '~2.0.0'}
deps['aws.greengrass.TokenExchangeService'] = {'VersionRequirement': '~2.0.0'}

def rewrite_install(script: str) -> str:
    # Replace the tar-load commands with ECR retags so docker-compose still
    # finds the flask-app / react-webapp image names at Run time.
    script = re.sub(r'docker load -i \S*flask-app\.tar',
                    f'docker tag {ecr_backend}:{version} flask-app:latest', script)
    script = re.sub(r'docker load -i \S*react-webapp\.tar(?:\.gz)?',
                    f'docker tag {ecr_frontend}:{version} react-webapp:latest', script)
    return script

changed = False
for manifest in recipe.get('Manifests', []):
    lifecycle = manifest.get('Lifecycle', {})
    install = lifecycle.get('Install')
    if isinstance(install, dict) and 'Script' in install:
        new_script = rewrite_install(install['Script'])
        if new_script != install['Script']:
            changed = True
        install['Script'] = new_script
    manifest['Artifacts'] = [
        {'URI': f'docker:{ecr_backend}:{version}'},
        {'URI': f'docker:{ecr_frontend}:{version}'},
        {'URI': s3_uri, 'Unarchive': 'ZIP'},
    ]

if not changed:
    sys.stderr.write('ERROR: did not find docker load commands to rewrite in Install; recipe format may have changed.\n')
    sys.exit(2)

with open(out, 'w') as f:
    yaml.dump(recipe, f, default_flow_style=False, sort_keys=False)
print(f'Wrote ECR recipe: {out}')
PYEOF

    echo "Creating component version via API..."
    if aws greengrassv2 create-component-version \
        --inline-recipe fileb://"$ECR_RECIPE" \
        --region "$PUB_REGION" 2>&1 | tee "$PUBLISH_LOG"; then
        echo ""
        echo "✓ Component published successfully (ECR + S3): ${COMPONENT_NAME} v${COMPONENT_VERSION}"
    else
        echo ""
        echo "✗ ECR component publish failed"
        echo "Full log saved to: $PUBLISH_LOG"
        exit 1
    fi
else
    # Under 2GB — standard GDK publish.
    if gdk component publish 2>&1 | tee "$PUBLISH_LOG"; then
        echo ""
        echo "✓ Component published successfully"
    else
        PUBLISH_EXIT_CODE=${PIPESTATUS[0]}
        echo ""
        echo "✗ Component publish failed (exit code: $PUBLISH_EXIT_CODE)"
        echo ""
        echo "Last 50 lines of publish log:"
        echo "---"
        tail -50 "$PUBLISH_LOG"
        echo "---"
        echo ""
        echo "Full log saved to: $PUBLISH_LOG"
        exit 1
    fi
fi

print_step "Tagging component for portal discovery"
# Tag the published component with dda-portal:managed=true

REGION=$(aws configure get region 2>/dev/null || true)
if [ -z "$REGION" ]; then
    REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)

COMPONENT_ARN=$(aws greengrassv2 list-components \
    --scope PRIVATE \
    --region $REGION \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" \
    --output text 2>/dev/null || true)

if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    echo "Found component ARN: $COMPONENT_ARN"
    
    if aws greengrassv2 tag-resource \
        --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" \
        --region $REGION 2>/dev/null; then
        echo "✓ Component tagged successfully"
    else
        echo "⚠ Warning: Could not tag component (this is non-critical)"
    fi
else
    echo "⚠ Warning: Could not find component ARN for tagging (this is non-critical)"
fi

print_step "Optional: Build InferenceUploader component"
# Ask if user wants to build InferenceUploader component
echo ""
echo "The InferenceUploader component enables edge devices to automatically"
echo "upload inference results (images and metadata) to S3 for centralized storage."
echo ""
read -p "Build and publish InferenceUploader component now? (y/n): " BUILD_INFERENCE_UPLOADER

if [ "$BUILD_INFERENCE_UPLOADER" = "y" ] || [ "$BUILD_INFERENCE_UPLOADER" = "Y" ]; then
    echo ""
    INFERENCE_LOG="/tmp/inference-uploader-build-$(date +%s).log"
    echo "Build log: $INFERENCE_LOG"
    echo ""
    
    if bash build-inference-uploader.sh 2>&1 | tee "$INFERENCE_LOG"; then
        echo ""
        echo "✅ InferenceUploader component built and published successfully!"
    else
        INFERENCE_EXIT_CODE=${PIPESTATUS[0]}
        echo ""
        echo "✗ InferenceUploader build failed (exit code: $INFERENCE_EXIT_CODE)"
        echo ""
        echo "Last 50 lines of build log:"
        echo "---"
        tail -50 "$INFERENCE_LOG"
        echo "---"
        echo ""
        echo "Full log saved to: $INFERENCE_LOG"
        echo ""
        echo "You can run ./build-inference-uploader.sh later to retry"
    fi
else
    echo ""
    echo "ℹ You can build the InferenceUploader component later by running:"
    echo "  ./build-inference-uploader.sh"
fi

print_step "Build and publish complete"
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
echo "✅ All components built and published successfully!"
echo "Total time: ${ELAPSED}s"
echo ""
