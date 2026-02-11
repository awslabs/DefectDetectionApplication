#!/bin/bash

# Build and publish InferenceUploader Greengrass component
# This component enables edge devices to upload inference results to S3

set -e

VERBOSE="${VERBOSE:-0}"
LOG_FILE="${LOG_FILE:-/tmp/inference-uploader-build-$(date +%s).log}"
ERRORS=()

# Export LOG_FILE for consistency
export LOG_FILE

COMPONENT_NAME="aws.edgeml.dda.InferenceUploader"
COMPONENT_VERSION="1.0.0"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper function to run commands with logging
run_cmd() {
    local cmd="$@"
    if [ "$VERBOSE" = "1" ]; then
        echo "[RUN] $cmd"
        eval "$cmd" | tee -a "$LOG_FILE"
    else
        if ! eval "$cmd" >> "$LOG_FILE" 2>&1; then
            ERRORS+=("Failed: $cmd")
            return 1
        fi
    fi
}

# Trap errors and show summary
trap 'show_error_summary' EXIT

show_error_summary() {
    if [ ${#ERRORS[@]} -gt 0 ]; then
        echo ""
        echo -e "${RED}❌ ERRORS ENCOUNTERED:${NC}"
        printf '%s\n' "${ERRORS[@]}"
        echo ""
        echo "📋 Full log: $LOG_FILE"
        return 1
    fi
}

echo -e "${BLUE}Building and publishing ${COMPONENT_NAME} v${COMPONENT_VERSION}${NC}"
echo ""

# Get account and region info
echo "▶ Getting AWS account and region info..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")
BUCKET_NAME="dda-component-${REGION}-${ACCOUNT_ID}"

echo "  Account ID: $ACCOUNT_ID"
echo "  Region: $REGION"
echo "  S3 Bucket: $BUCKET_NAME"
echo ""

# Create bucket if it doesn't exist
echo "▶ Checking S3 bucket..."
if ! aws s3 ls "s3://${BUCKET_NAME}" 2>/dev/null; then
    echo "  Creating S3 bucket: ${BUCKET_NAME}"
    if run_cmd "aws s3 mb s3://${BUCKET_NAME} --region ${REGION}"; then
        echo "✓ S3 bucket created"
    else
        ERRORS+=("Failed to create S3 bucket")
        exit 1
    fi
else
    echo "✓ S3 bucket exists"
fi
echo ""

# Upload artifacts to S3
echo "▶ Uploading artifacts to S3..."
ARTIFACT_PREFIX="${COMPONENT_NAME}/${COMPONENT_VERSION}"

if run_cmd "aws s3 cp inference-uploader/artifacts/inference_uploader.py s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/inference_uploader.py"; then
    echo "✓ inference_uploader.py uploaded"
else
    ERRORS+=("Failed to upload inference_uploader.py")
    exit 1
fi

if run_cmd "aws s3 cp inference-uploader/artifacts/requirements.txt s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/requirements.txt"; then
    echo "✓ requirements.txt uploaded"
else
    ERRORS+=("Failed to upload requirements.txt")
    exit 1
fi
echo ""

# Update recipe with actual bucket name and version
echo "▶ Preparing component recipe..."
RECIPE_FILE="recipe_processed.yaml"
cat inference-uploader/recipe.yaml | \
  sed "s|BUCKET_NAME|${BUCKET_NAME}|g" | \
  sed "s|COMPONENT_VERSION|${COMPONENT_VERSION}|g" > "${RECIPE_FILE}"
echo "✓ Recipe prepared"
echo ""

# Create component version in Greengrass
echo "▶ Creating component version in Greengrass..."
if run_cmd "aws greengrassv2 create-component-version --inline-recipe fileb://${RECIPE_FILE} --region ${REGION} --tags dda-portal:managed=true,dda-portal:component-type=inference-uploader,dda-portal:shared-component=true"; then
    echo "✓ Component version created"
else
    ERRORS+=("Failed to create component version")
    rm -f "${RECIPE_FILE}"
    exit 1
fi

# Clean up temporary file
rm -f "${RECIPE_FILE}"
echo ""

# Tag the component to ensure it's discoverable
echo "▶ Tagging component for portal discovery..."
COMPONENT_ARN=$(aws greengrassv2 list-components \
    --scope PRIVATE \
    --region "${REGION}" \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" \
    --output text 2>/dev/null)

if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    echo "  Found component ARN: $COMPONENT_ARN"
    
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

if [ ${#ERRORS[@]} -eq 0 ]; then
    echo -e "${GREEN}=========================================="
    echo "✅ Component Published Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "Component Name: ${COMPONENT_NAME}"
    echo "Version: ${COMPONENT_VERSION}"
    echo "Region: ${REGION}"
    echo "S3 Bucket: ${BUCKET_NAME}"
    echo ""
    echo -e "${BLUE}=== Next Steps ===${NC}"
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
fi
