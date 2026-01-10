#!/bin/bash
set -e

# ── Pre-flight: verify AWS credentials ────────────────────────────────────
echo "Checking AWS credentials..."
CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1) || {
    echo ""
    echo "ERROR: AWS credentials not configured or expired."
    echo ""
    echo "Please configure credentials before building:"
    echo "  aws configure                    # for long-term credentials"
    echo "  aws sso login --profile <name>   # for SSO"
    echo "  export AWS_PROFILE=<name>        # to select a profile"
    echo ""
    echo "The build requires valid credentials to publish the Greengrass component."
    exit 1
}

AWS_ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
AWS_ARN=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
AWS_REGION=$(aws configure get region 2>/dev/null || echo "${AWS_DEFAULT_REGION:-}")

if [ -z "$AWS_REGION" ]; then
    echo ""
    echo "ERROR: No AWS region configured."
    echo ""
    echo "Set a region before building:"
    echo "  aws configure set region us-east-2"
    echo "  export AWS_DEFAULT_REGION=us-east-2"
    exit 1
fi

export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

echo "  Account: $AWS_ACCOUNT_ID"
echo "  Role/User: $AWS_ARN"
echo "  Region: $AWS_REGION"
echo ""

# Usage: ./gdk-component-build-and-publish.sh [ARCH] [JETPACK]
# ARCH: x86_64 or aarch64 (default: auto-detect from host)
# JETPACK: 4 or 5 (required for aarch64 builds)
#
# Supported configurations:
#   x86_64              -> aws.edgeml.dda.LocalServer.amd64      (Ubuntu 20.04)
#   aarch64 + JP4       -> aws.edgeml.dda.LocalServer.arm64      (Ubuntu 18.04, L4T r32.x)
#   aarch64 + JP5       -> aws.edgeml.dda.LocalServer.arm64JP5   (Ubuntu 20.04, L4T r35.x)
#
# Examples:
#   ./gdk-component-build-and-publish.sh                  # x86_64 (auto-detect)
#   ./gdk-component-build-and-publish.sh aarch64 4        # ARM64 JetPack 4.6
#   ./gdk-component-build-and-publish.sh aarch64 5        # ARM64 JetPack 5

# Get architecture
ARCH="${1:-$(arch)}"
echo "Architecture: $ARCH"

# Determine JetPack version for aarch64
if [ "$ARCH" = "aarch64" ]; then
    if [ -z "$2" ]; then
        echo "ERROR: JetPack version is required for aarch64 builds."
        echo "Usage: $0 aarch64 <4|5>"
        echo "  4 = JetPack 4.6 (Ubuntu 18.04, L4T r32.x)"
        echo "  5 = JetPack 5   (Ubuntu 20.04, L4T r35.x)"
        exit 1
    fi
    JETPACK="$2"
    if [ "$JETPACK" != "4" ] && [ "$JETPACK" != "5" ]; then
        echo "ERROR: JETPACK must be 4 or 5, got: $JETPACK"
        exit 1
    fi
    echo "JetPack version: $JETPACK"
fi

# Determine recipe file and component name
case "$ARCH" in
    x86_64)
        RECIPE_FILE="recipe-amd64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.amd64"
        ;;
    aarch64)
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

echo "Building component for architecture: $ARCH"
echo "Component name: $COMPONENT_NAME"
echo "Using recipe: $RECIPE_FILE"

# Get account and region info
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")
BUCKET_NAME="dda-component-${REGION}-${ACCOUNT_ID}"

echo "Using S3 bucket: $BUCKET_NAME"

# Use architecture-specific recipe
cp $RECIPE_FILE recipe.yaml

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
          "NEXT_PATCH",
          "${ARCH}"
        ]
      },
      "publish": {
        "bucket": "dda-component-${AWS_REGION}-${AWS_ACCOUNT_ID}",
        "region": "${AWS_REGION}"
      }
    }
  },
  "gdk_version": "1.0.0"
}
EOF

# Clean GDK cache and build directories
rm -rf greengrass-build/
rm -rf .gdk/

# Build component
echo "Building component..."
gdk component build

# Check total artifact size
TOTAL_SIZE=0
for artifact in greengrass-build/artifacts/$COMPONENT_NAME/NEXT_PATCH/*.zip; do
    if [ -s "$artifact" ]; then
        SIZE=$(stat --format=%s "$artifact")
        TOTAL_SIZE=$((TOTAL_SIZE + SIZE))
    fi
done
echo "Total artifact size: $(numfmt --to=iec $TOTAL_SIZE)"

if [ "$TOTAL_SIZE" -gt 2147483648 ]; then
    echo ""
    echo "WARNING: Total artifacts exceed Greengrass 2GB limit ($(numfmt --to=iec $TOTAL_SIZE))."
    echo "Using ECR for Docker images + S3 for scripts/config."
    echo ""

    # Push Docker images to ECR instead
    REGION="$AWS_REGION"
    ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
    ECR_REPO_BACKEND="${ECR_REGISTRY}/dda/flask-app"
    ECR_REPO_FRONTEND="${ECR_REGISTRY}/dda/react-webapp"

    # Login to ECR
    aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR_REGISTRY

    # Create repos if they don't exist
    aws ecr describe-repositories --repository-names dda/flask-app --region $REGION 2>/dev/null || \
        aws ecr create-repository --repository-name dda/flask-app --region $REGION
    aws ecr describe-repositories --repository-names dda/react-webapp --region $REGION 2>/dev/null || \
        aws ecr create-repository --repository-name dda/react-webapp --region $REGION

    # Determine version tag
    LATEST_VERSION=$(aws greengrassv2 list-component-versions \
        --arn "arn:aws:greengrass:${REGION}:${AWS_ACCOUNT_ID}:components:${COMPONENT_NAME}" \
        --query 'componentVersions[0].componentVersion' --output text 2>/dev/null || echo "0.0.0")
    if [ "$LATEST_VERSION" = "None" ] || [ "$LATEST_VERSION" = "0.0.0" ]; then
        COMPONENT_VERSION="1.0.0"
    else
        MAJOR=$(echo $LATEST_VERSION | cut -d. -f1)
        MINOR=$(echo $LATEST_VERSION | cut -d. -f2)
        PATCH=$(echo $LATEST_VERSION | cut -d. -f3)
        COMPONENT_VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"
    fi
    echo "Component version: $COMPONENT_VERSION"

    # Tag and push images
    echo "Pushing flask-app to ECR..."
    docker tag flask-app:latest "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"
    docker push "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"

    echo "Pushing react-webapp to ECR..."
    docker tag react-webapp:latest "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"
    docker push "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"

    # Upload the small app artifact (scripts + docker-compose) to S3
    # Remove the large backend zip, keep only the app zip
    BUCKET="dda-component-${REGION}-${AWS_ACCOUNT_ID}"
    APP_ARTIFACT="${COMPONENT_NAME}-app-$(arch).zip"
    APP_ZIP="greengrass-build/artifacts/${COMPONENT_NAME}/NEXT_PATCH/${APP_ARTIFACT}"
    S3_KEY="${COMPONENT_NAME}/${COMPONENT_VERSION}/${APP_ARTIFACT}"

    if [ -f "$APP_ZIP" ] && [ -s "$APP_ZIP" ]; then
        echo "Uploading app artifact to S3 ($(numfmt --to=iec $(stat --format=%s "$APP_ZIP")))..."
        aws s3 cp "$APP_ZIP" "s3://${BUCKET}/${S3_KEY}" --region "$REGION"
    else
        echo "ERROR: App artifact not found at $APP_ZIP"
        exit 1
    fi

    # Update recipe: ECR for docker pull in Install, keep S3 artifact for scripts
    RECIPE_FILE="greengrass-build/recipes/recipe.yaml"
    S3_ARTIFACT_URI="s3://${BUCKET}/${S3_KEY}"

    python3 - "$RECIPE_FILE" "$ECR_REPO_BACKEND" "$ECR_REPO_FRONTEND" "$COMPONENT_VERSION" "$REGION" "$AWS_ACCOUNT_ID" "$S3_ARTIFACT_URI" "$APP_ARTIFACT" << 'PYEOF'
import sys, yaml

recipe_file = sys.argv[1]
ecr_backend = sys.argv[2]
ecr_frontend = sys.argv[3]
version = sys.argv[4]
region = sys.argv[5]
account_id = sys.argv[6]
s3_artifact_uri = sys.argv[7]
app_artifact_name = sys.argv[8]

with open(recipe_file) as f:
    recipe = yaml.safe_load(f)

recipe['ComponentVersion'] = version

ecr_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

# Add DockerApplicationManager and TokenExchangeService as dependencies
# These handle ECR authentication automatically
if 'ComponentDependencies' not in recipe:
    recipe['ComponentDependencies'] = {}
recipe['ComponentDependencies']['aws.greengrass.DockerApplicationManager'] = {
    'VersionRequirement': '~2.0.0'
}
recipe['ComponentDependencies']['aws.greengrass.TokenExchangeService'] = {
    'VersionRequirement': '~2.0.0'
}

# Derive the artifact decompressed path prefix from the app artifact name
# e.g. aws.edgeml.dda.LocalServer.arm64JP5-app-aarch64
app_artifact_base = app_artifact_name.replace('.zip', '')

# Update lifecycle and artifacts
for manifest in recipe.get('Manifests', []):
    lifecycle = manifest.get('Lifecycle', {})

    # Install: tag the ECR images as local names, clean dangling images
    lifecycle['Install'] = {
        'RequiresPrivilege': True,
        'Script': (
            f'docker images --quiet --filter=dangling=true | xargs --no-run-if-empty docker rmi -f ; '
            f'docker tag {ecr_backend}:{version} flask-app:latest ; '
            f'docker tag {ecr_frontend}:{version} react-webapp:latest'
        )
    }

    # Artifacts: docker: URIs for ECR images + S3 URI for scripts/compose
    manifest['Artifacts'] = [
        {'URI': f'docker:{ecr_backend}:{version}'},
        {'URI': f'docker:{ecr_frontend}:{version}'},
        {'URI': s3_artifact_uri, 'Unarchive': 'ZIP'}
    ]

with open(recipe_file, 'w') as f:
    yaml.dump(recipe, f, default_flow_style=False, sort_keys=False)

print(f"Updated recipe: docker: artifacts + S3 app artifact")
print(f"  Docker: {ecr_backend}:{version}")
print(f"  Docker: {ecr_frontend}:{version}")
print(f"  S3:     {s3_artifact_uri}")
PYEOF

    # Create component version via API
    echo "Creating component version in Greengrass..."
    aws greengrassv2 create-component-version \
        --inline-recipe fileb://"$RECIPE_FILE" \
        --region "$REGION"

    # Get the component ARN and tag it for portal visibility
    echo "Tagging component for DDA Portal visibility..."
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

    # Get the latest version of the component
    LATEST_VERSION=$(aws greengrassv2 list-component-versions \
      --arn "arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}" \
      --query 'componentVersions[0].componentVersion' \
      --output text 2>/dev/null || echo "")

    if [ -n "$LATEST_VERSION" ] && [ "$LATEST_VERSION" != "None" ]; then
      COMPONENT_ARN="arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}:versions:${LATEST_VERSION}"

      echo "Tagging component: $COMPONENT_ARN"
      aws greengrassv2 tag-resource \
        --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" \
               "dda-portal:component-type=local-server" \
               "dda-portal:architecture=${ARCH}"

      echo "Component tagged successfully!"
      echo ""
      echo "=== Component Details ==="
      echo "Component Name: $COMPONENT_NAME"
      echo "Version: $LATEST_VERSION"
      echo "Bucket: $BUCKET_NAME"
      echo "Artifact Path: ${COMPONENT_NAME}/${LATEST_VERSION}/${COMPONENT_NAME}-${ARCH}.zip"
      echo ""
      echo "=== Next Steps ==="
      echo "1. Update DDA_LOCAL_SERVER_VERSION in compute-stack.ts to: $LATEST_VERSION"
      echo "2. Deploy: cd edge-cv-portal/infrastructure && npm run build && cdk deploy EdgeCVPortalComputeStack"
      echo "3. Use 'Update All Usecases' button in portal to push to all usecase accounts"
      echo ""
      echo "NOTE: Cross-account bucket policy is automatically managed during usecase onboarding."
      echo "      New usecase accounts are added to the bucket policy when shared components are provisioned."
      echo ""
    else
      echo "Warning: Could not determine component version for tagging"
    fi

    echo "Component ${COMPONENT_NAME} v${COMPONENT_VERSION} published successfully (ECR + S3)!"
else
    # Under 2GB - use standard GDK publish
    echo "Publishing component via GDK..."
    gdk component publish
    echo "Component ${COMPONENT_NAME} built and published successfully!"
fi
