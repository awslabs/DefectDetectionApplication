#!/bin/bash
set -e

# Build and publish InferenceUploader Greengrass component
# This component enables edge devices to upload inference results to S3

COMPONENT_NAME="aws.edgeml.dda.InferenceUploader"

echo "Building and publishing ${COMPONENT_NAME}"
echo ""

# Get account and region info
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region 2>/dev/null)
if [ -z "$REGION" ]; then
    echo "❌ ERROR: No AWS region configured."
    echo "   Run: aws configure set region <your-region>"
    exit 1
fi
BUCKET_NAME="dda-component-${REGION}-${ACCOUNT_ID}"

echo "Using S3 bucket: $BUCKET_NAME"
echo ""

# Create bucket if it doesn't exist
if ! aws s3 ls "s3://${BUCKET_NAME}" 2>/dev/null; then
    echo "Creating S3 bucket: ${BUCKET_NAME}"
    aws s3 mb "s3://${BUCKET_NAME}" --region "${REGION}"
fi

# Determine the next available component version.
# Find the latest published version and bump the minor number; if none exists, start at 1.0.0.
echo "Determining component version..."
COMPONENT_ARN="arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}"
LATEST_VERSION=$(aws greengrassv2 list-component-versions \
    --arn "${COMPONENT_ARN}" \
    --region "${REGION}" \
    --query 'componentVersions[0].componentVersion' \
    --output text 2>/dev/null || echo "None")

if [ "$LATEST_VERSION" = "None" ] || [ -z "$LATEST_VERSION" ]; then
    COMPONENT_VERSION="1.0.0"
else
    MAJOR=$(echo "$LATEST_VERSION" | cut -d. -f1)
    MINOR=$(echo "$LATEST_VERSION" | cut -d. -f2)
    COMPONENT_VERSION="${MAJOR}.$((MINOR + 1)).0"
fi
echo "Publishing version: ${COMPONENT_VERSION}"
echo ""

# Publish helper: uploads artifacts for a given version and creates the component version.
# Returns 0 on success, 10 on version conflict (so the caller can retry with a higher version).
publish_version() {
    local version="$1"
    local artifact_prefix="${COMPONENT_NAME}/${version}"

    echo "Uploading artifacts to s3://${BUCKET_NAME}/${artifact_prefix}/"
    aws s3 cp inference-uploader/artifacts/inference_uploader.py "s3://${BUCKET_NAME}/${artifact_prefix}/inference_uploader.py"
    aws s3 cp inference-uploader/artifacts/requirements.txt "s3://${BUCKET_NAME}/${artifact_prefix}/requirements.txt"
    echo "Artifacts uploaded successfully"
    echo ""

    # Render the recipe with the bucket name and version
    local recipe_file="recipe_processed.yaml"
    cat inference-uploader/recipe.yaml | \
        sed "s|BUCKET_NAME|${BUCKET_NAME}|g" | \
        sed "s|COMPONENT_VERSION|${version}|g" | \
        sed "s|ComponentVersion: '[^']*'|ComponentVersion: '${version}'|g" > "${recipe_file}"

    echo "Creating component version ${version} in Greengrass..."
    local create_output
    if create_output=$(aws greengrassv2 create-component-version \
        --inline-recipe fileb://"${recipe_file}" \
        --region "${REGION}" \
        --tags "dda-portal:managed=true,dda-portal:component-type=inference-uploader,dda-portal:shared-component=true" 2>&1); then
        rm -f "${recipe_file}"
        return 0
    else
        rm -f "${recipe_file}"
        if echo "$create_output" | grep -q "ConflictException"; then
            echo "⚠ Version ${version} already exists, will retry with a higher version..."
            return 10
        fi
        echo "❌ ERROR creating component version:"
        echo "$create_output"
        return 1
    fi
}

# Try to publish, bumping the minor version on conflict (up to 10 attempts).
MAX_ATTEMPTS=10
attempt=0
while [ $attempt -lt $MAX_ATTEMPTS ]; do
    set +e
    publish_version "${COMPONENT_VERSION}"
    rc=$?
    set -e

    if [ $rc -eq 0 ]; then
        break
    elif [ $rc -eq 10 ]; then
        MAJOR=$(echo "$COMPONENT_VERSION" | cut -d. -f1)
        MINOR=$(echo "$COMPONENT_VERSION" | cut -d. -f2)
        COMPONENT_VERSION="${MAJOR}.$((MINOR + 1)).0"
        attempt=$((attempt + 1))
    else
        exit 1
    fi
done

if [ $attempt -ge $MAX_ATTEMPTS ]; then
    echo "❌ ERROR: Could not publish after ${MAX_ATTEMPTS} version-bump attempts."
    exit 1
fi

echo ""

# Tag the component to ensure it's discoverable
echo "Tagging component for portal discovery..."

COMPONENT_ARN=$(aws greengrassv2 list-components \
    --scope PRIVATE \
    --region "${REGION}" \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" \
    --output text 2>/dev/null)

if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    echo "Found component ARN: $COMPONENT_ARN"

    if aws greengrassv2 tag-resource \
        --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" \
        --region "${REGION}" 2>/dev/null; then
        echo "✓ Component tagged successfully"
    else
        echo "⚠ Warning: Could not tag component (this is non-critical)"
    fi
else
    echo "⚠ Warning: Could not find component ARN for tagging (this is non-critical)"
fi

echo ""
echo "=========================================="
echo "✅ Component Published Successfully!"
echo "=========================================="
echo ""
echo "Component Name: ${COMPONENT_NAME}"
echo "Version: ${COMPONENT_VERSION}"
echo "Region: ${REGION}"
echo "S3 Bucket: ${BUCKET_NAME}"
echo ""
echo "=== Next Steps ==="
echo "1. Deploy infrastructure to create S3 bucket:"
echo "   cd edge-cv-portal/infrastructure && npm run build && rm -rf cdk.out"
echo "   cdk deploy EdgeCVPortalStack-UseCaseAccountStack"
echo ""
echo "2. Provision to usecase accounts (automatic for new usecases):"
echo "   - New usecases: Component auto-provisions during onboarding"
echo "   - Existing usecases: Use 'Update All Usecases' button in portal"
echo ""
echo "3. Deploy to devices via portal with S3 configuration:"
echo "   - s3Bucket: dda-inference-results-{account-id}"
echo "   - s3Prefix: {usecase-id}/{device-id}"
echo "   - uploadIntervalSeconds: 300"
echo ""
