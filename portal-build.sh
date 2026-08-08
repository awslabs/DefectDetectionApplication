#!/bin/bash
set -e
set -o pipefail

# portal-build.sh — non-interactive build + publish entry point for the portal
# build fleet (refactor of gdk-component-build-and-publish.sh).
#
# Differences from the interactive script:
#   * Fully non-interactive: the InferenceUploader prompt is removed. Set
#     BUILD_INFERENCE_UPLOADER=1 to also build/publish the InferenceUploader
#     component after the LocalServer publish (default: skipped).
#   * Emits a `phase=publishing` event (source `dda.portal.builds`) between the
#     build and publish steps via `aws events put-events` when EVENT_BUS is set
#     (degrades gracefully to a log line when it is not).
#   * On success prints a machine-readable result line:
#       PORTAL_BUILD_RESULT {"component_name":...,"published_version":...,"pushed_image_refs":[...]}
#   * Accepts `x86_64_nvidia` as an ARCH value mapping to
#     aws.edgeml.dda.LocalServer.amd64Nvidia / recipe-amd64-nvidia.yaml.
#
# Usage: ./portal-build.sh [ARCH] [JETPACK]
#   ARCH:    x86_64, x86_64_nvidia, or aarch64 (default: auto-detect from host)
#   JETPACK: 4, 5, or 6 (required for aarch64 builds)
#
# Supported configurations:
#   x86_64           -> aws.edgeml.dda.LocalServer.amd64        (Ubuntu 20.04)
#   x86_64_nvidia    -> aws.edgeml.dda.LocalServer.amd64Nvidia  (x86 + NVIDIA GPU)
#   aarch64 + JP4    -> aws.edgeml.dda.LocalServer.arm64        (Ubuntu 18.04, L4T r32.x)
#   aarch64 + JP5    -> aws.edgeml.dda.LocalServer.arm64JP5     (Ubuntu 20.04, L4T r35.x)
#   aarch64 + JP6    -> aws.edgeml.dda.LocalServer.arm64JP6     (Ubuntu 22.04, L4T r36.x)
#
# Environment:
#   EVENT_BUS                 EventBridge bus name/ARN for phase events (optional)
#   BUILD_JOB_ID              Portal Build_Job id included in phase events (optional)
#   ATTEMPT_ID                Execution-attempt identity included in phase
#                             events when set (optional, additive; exported
#                             by portal-build-agent.sh — task 7.2)
#   SKIP_BUILD=1              Re-use existing greengrass-build/ artifacts, publish only
#   BUILD_INFERENCE_UPLOADER=1  Also build the InferenceUploader component (optional)
#
# Examples:
#   ./portal-build.sh                     # auto-detect arch (x86_64)
#   ./portal-build.sh x86_64_nvidia       # x86_64 + NVIDIA GPU runtime
#   ./portal-build.sh aarch64 5           # ARM64 JetPack 5
#   ./portal-build.sh aarch64 6           # ARM64 JetPack 6

# ── Phase event emission (source dda.portal.builds) ─────────────────────────
# Non-fatal by design: a missing EVENT_BUS, missing python3, or a PutEvents
# failure must never break the build/publish itself.
emit_phase_event() {
    local phase="$1"
    if [ -z "${EVENT_BUS:-}" ]; then
        echo "ℹ EVENT_BUS not set — skipping phase=${phase} event emission"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "⚠ Warning: python3 not available — cannot emit phase=${phase} event"
        return 0
    fi
    local entries
    entries=$(python3 -c '
import json, sys
bus, job_id, phase, component, attempt_id = sys.argv[1:6]
detail = {"build_job_id": job_id, "phase": phase, "component_name": component}
if attempt_id:
    # Correlated execution-attempt identity (build-fleet-execution-
    # failures task 7.2): additive only — absent for legacy invocations.
    detail["attempt_id"] = attempt_id
print(json.dumps([{
    "Source": "dda.portal.builds",
    "DetailType": "BuildPhaseChange",
    "EventBusName": bus,
    "Detail": json.dumps(detail),
}]))' "$EVENT_BUS" "${BUILD_JOB_ID:-}" "$phase" "${COMPONENT_NAME:-}" "${ATTEMPT_ID:-}") || {
        echo "⚠ Warning: failed to serialize phase=${phase} event"
        return 0
    }
    if aws events put-events --entries "$entries" >/dev/null 2>&1; then
        echo "✓ Emitted phase=${phase} event to bus ${EVENT_BUS}"
    else
        echo "⚠ Warning: PutEvents for phase=${phase} failed (non-fatal)"
    fi
    return 0
}

# ── Pre-flight: verify AWS credentials before the (long) build ──────────────
# Publishing needs valid credentials. Check them up front so an expired session
# fails in seconds rather than after a full image build + packaging (which can
# take 10-15+ minutes). On the build fleet the instance profile satisfies this.
echo "Checking AWS credentials..."
if ! CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1); then
    echo ""
    echo "❌ ERROR: AWS credentials are not valid or have expired."
    echo "   ${CALLER_IDENTITY}"
    echo ""
    echo "   Re-authenticate before building/publishing, e.g.:"
    echo "     aws sso login --profile <name>"
    echo "     aws login"
    echo "     export AWS_PROFILE=<name>"
    echo ""
    echo "   (Credentials are required to publish the Greengrass component.)"
    exit 1
fi
echo "✓ AWS credentials valid"

# ── Propagate resolved credentials to GDK ───────────────────────────────────
# The AWS CLI v2 bundles a modern botocore and can resolve credentials from SSO
# / login-helper sources (e.g. an `~/.aws/config` with only `region` +
# `login_session` and no static `~/.aws/credentials`). However, `gdk component
# publish` runs on its own older botocore (1.26.x under Python 3.6) that cannot
# read those sources and fails with `NoCredentialsError: Unable to locate
# credentials` even though `aws` works. Materialize the already-resolved session
# into standard env vars (highest-priority in every SDK's credential chain) so
# GDK's old botocore can authenticate. No-op if the running CLI predates
# `export-credentials`.
if _CREDS_ENV=$(aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION); then
    eval "$_CREDS_ENV"
    unset _CREDS_ENV
    echo "✓ Exported resolved credentials to the environment for GDK"
else
    echo "ℹ AWS CLI does not support 'export-credentials'; relying on the ambient"
    echo "  credential chain. If 'gdk component publish' reports NoCredentialsError,"
    echo "  export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN manually."
fi

# Step tracking
STEP=0
TOTAL_STEPS=7
START_TIME=$(date +%s)

print_step() {
    STEP=$((STEP + 1))
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "[$STEP/$TOTAL_STEPS] $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Argument parsing is order-independent and accepts both the positional JetPack
# number (4|5|6) and the --jp4/--jp5/--jp6 flags (kept as backward-compatible aliases).
ARCH=""
JETPACK=""
for arg in "$@"; do
    case "$arg" in
        x86_64_nvidia|amd64_nvidia|x86_64-nvidia) ARCH="x86_64_nvidia" ;;
        x86_64|amd64)        ARCH="x86_64" ;;
        aarch64|arm64)       ARCH="aarch64" ;;
        4|jp4|JP4|--jp4)     JETPACK="4" ;;
        5|jp5|JP5|--jp5)     JETPACK="5" ;;
        6|jp6|JP6|--jp6)     JETPACK="6" ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [x86_64|x86_64_nvidia|aarch64] [4|5|6]"
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
    x86_64_nvidia)
        RECIPE_FILE="recipe-amd64-nvidia.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.amd64Nvidia"
        ;;
    aarch64)
        # JetPack version is required for aarch64 so we never silently publish
        # the wrong component (passing nothing previously defaulted to JP4 and
        # produced aws.edgeml.dda.LocalServer.arm64 even when JP5 was intended).
        if [ -z "$JETPACK" ]; then
            echo "ERROR: JetPack version is required for aarch64 builds."
            echo "Usage: $0 aarch64 <4|5|6>"
            echo "  4 = JetPack 4.6 (Ubuntu 18.04, L4T r32.x)  -> aws.edgeml.dda.LocalServer.arm64"
            echo "  5 = JetPack 5   (Ubuntu 20.04, L4T r35.x)  -> aws.edgeml.dda.LocalServer.arm64JP5"
            echo "  6 = JetPack 6   (Ubuntu 22.04, L4T r36.x)  -> aws.edgeml.dda.LocalServer.arm64JP6"
            exit 1
        fi
        if [ "$JETPACK" = "6" ]; then
            RECIPE_FILE="recipe-arm64-jp6.yaml"
            COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64JP6"
        elif [ "$JETPACK" = "5" ]; then
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

if [ ! -f "$RECIPE_FILE" ]; then
    echo "❌ ERROR: Recipe file '$RECIPE_FILE' not found."
    exit 1
fi

# Use architecture-specific recipe
cp "$RECIPE_FILE" recipe.yaml

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

# SKIP_BUILD=1 re-uses the already-built artifacts in greengrass-build/ and the
# locally-tagged flask-app/react-webapp images, skipping the (long) clean +
# `gdk component build`. Use it to re-run ONLY the publish step — e.g. when a
# build succeeded but publish failed on a transient credential expiry — without
# a full ~1h rebuild. Default (unset) preserves the original clean+build+publish.
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    print_step "Reusing existing build (SKIP_BUILD=1)"
    echo "⏭  SKIP_BUILD=1 — skipping clean + gdk component build; publishing the"
    echo "   already-built artifacts in greengrass-build/ and the local images."
    if ! ls greengrass-build/artifacts/"${COMPONENT_NAME}"/NEXT_PATCH/*.zip >/dev/null 2>&1; then
        echo "✗ SKIP_BUILD=1 but no built artifact found at"
        echo "  greengrass-build/artifacts/${COMPONENT_NAME}/NEXT_PATCH/*.zip — run a full build first."
        exit 1
    fi
    echo "✓ Found existing artifact for ${COMPONENT_NAME}"
    STEP=$((STEP + 1)) # account for the skipped build step so numbering stays aligned
else
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
fi

print_step "Publishing LocalServer component"

# The build step has completed successfully — signal the transition into the
# publishing phase before any publish action runs (Req 5.1).
emit_phase_event "publishing"

PUBLISH_LOG="/tmp/gdk-publish-$(date +%s).log"
echo "Publish log: $PUBLISH_LOG"
echo ""

# The image build above can take far longer than a session token's lifetime, so
# the credentials exported during pre-flight may now be expired (gdk would fail
# with "Credentials were refreshed, but the refreshed credentials are still
# expired"). Re-resolve a fresh session immediately before publishing.
#
# IMPORTANT: strip AWS_CREDENTIAL_EXPIRATION. With it set, gdk's old botocore
# treats the env credentials as *refreshable* and, when it thinks they're past
# expiry, "refreshes" by re-reading the same env vars and then raises
# "refreshed credentials are still expired" — even when the underlying token is
# actually valid (the export can emit a stale/past expiration for SSO/login
# credential sources). Without the expiration var, botocore uses the token as a
# static credential and signs successfully.
if _CREDS_ENV=$(aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION); then
    eval "$_CREDS_ENV"
    unset _CREDS_ENV
    echo "✓ Refreshed AWS credentials for publish"
fi

# Resolve account/region up front (needed for the ECR path and tagging).
PUB_REGION=$(aws configure get region 2>/dev/null || true)
if [ -z "$PUB_REGION" ]; then
    PUB_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
PUB_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)

# Result metadata for the PORTAL_BUILD_RESULT line.
PUBLISHED_VERSION=""
PUSHED_IMAGE_REFS=()

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
    PUSHED_IMAGE_REFS+=("${ECR_REPO_BACKEND}:${COMPONENT_VERSION}")
    echo "Pushing react-webapp to ECR..."
    docker tag react-webapp:latest "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"
    docker push "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"
    PUSHED_IMAGE_REFS+=("${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}")

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
        PUBLISHED_VERSION="$COMPONENT_VERSION"
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

    # Resolve the version GDK just published. Prefer the publish log (exact),
    # fall back to the latest component version from the API.
    PUBLISHED_VERSION=$(grep -oE "version[: ]+[0-9]+\.[0-9]+\.[0-9]+" "$PUBLISH_LOG" 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | tail -1 || true)
    if [ -z "$PUBLISHED_VERSION" ] && [ -n "$PUB_REGION" ] && [ -n "$PUB_ACCOUNT_ID" ] && [ "$PUB_ACCOUNT_ID" != "None" ]; then
        PUBLISHED_VERSION=$(aws greengrassv2 list-component-versions \
            --arn "arn:aws:greengrass:${PUB_REGION}:${PUB_ACCOUNT_ID}:components:${COMPONENT_NAME}" \
            --query 'componentVersions[0].componentVersion' --output text 2>/dev/null || true)
        [ "$PUBLISHED_VERSION" = "None" ] && PUBLISHED_VERSION=""
    fi
fi

print_step "Tagging component for portal discovery"
# Tag the published component with dda-portal:managed=true

REGION=$(aws configure get region 2>/dev/null || true)
if [ -z "$REGION" ]; then
    REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi

COMPONENT_ARN=$(aws greengrassv2 list-components \
    --scope PRIVATE \
    --region "$REGION" \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" \
    --output text 2>/dev/null || true)

if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    echo "Found component ARN: $COMPONENT_ARN"

    if aws greengrassv2 tag-resource \
        --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" \
        --region "$REGION" 2>/dev/null; then
        echo "✓ Component tagged successfully"
    else
        echo "⚠ Warning: Could not tag component (this is non-critical)"
    fi
else
    echo "⚠ Warning: Could not find component ARN for tagging (this is non-critical)"
fi

# ── Optional: InferenceUploader (non-interactive) ────────────────────────────
# The interactive prompt of gdk-component-build-and-publish.sh is replaced by
# an opt-in environment variable so this script never blocks on stdin.
if [ "${BUILD_INFERENCE_UPLOADER:-0}" = "1" ]; then
    echo ""
    echo "BUILD_INFERENCE_UPLOADER=1 — building InferenceUploader component..."
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
        echo "Full log saved to: $INFERENCE_LOG"
        echo "You can run ./build-inference-uploader.sh later to retry"
    fi
else
    echo ""
    echo "ℹ Skipping InferenceUploader (set BUILD_INFERENCE_UPLOADER=1 to include it)."
fi

print_step "Build and publish complete"
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
echo "✅ All components built and published successfully!"
echo "Total time: ${ELAPSED}s"
echo ""

# ── Machine-readable result line (parsed by portal-build-agent.sh) ──────────
IMAGES_JSON="[]"
if [ ${#PUSHED_IMAGE_REFS[@]} -gt 0 ]; then
    IMAGES_JSON=$(printf '"%s",' "${PUSHED_IMAGE_REFS[@]}")
    IMAGES_JSON="[${IMAGES_JSON%,}]"
fi
echo "PORTAL_BUILD_RESULT {\"component_name\":\"${COMPONENT_NAME}\",\"published_version\":\"${PUBLISHED_VERSION}\",\"pushed_image_refs\":${IMAGES_JSON}}"
