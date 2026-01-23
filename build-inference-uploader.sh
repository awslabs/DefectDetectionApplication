#!/bin/bash
set -e

COMPONENT_NAME="aws.edgeml.dda.InferenceUploader"
COMPONENT_VERSION="1.0.0"

echo "Building and publishing ${COMPONENT_NAME} v${COMPONENT_VERSION}"

# Get account and region info
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")
BUCKET_NAME="dda-component-${REGION}-${ACCOUNT_ID}"

echo "Using S3 bucket: $BUCKET_NAME"

# Create bucket if it doesn't exist
if ! aws s3 ls "s3://${BUCKET_NAME}" 2>/dev/null; then
    echo "Creating S3 bucket: ${BUCKET_NAME}"
    aws s3 mb "s3://${BUCKET_NAME}" --region "${REGION}"
fi

# Upload artifacts to S3
ARTIFACT_PREFIX="${COMPONENT_NAME}/${COMPONENT_VERSION}"
echo "Uploading artifacts to s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/"

aws s3 cp inference-uploader/artifacts/inference_uploader.py "s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/inference_uploader.py"
aws s3 cp inference-uploader/artifacts/requirements.txt "s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/requirements.txt"

echo "Artifacts uploaded successfully"

# Update recipe with actual bucket name and version
RECIPE_FILE="recipe_processed.yaml"
cat inference-uploader/recipe.yaml | \
  sed "s|BUCKET_NAME|${BUCKET_NAME}|g" | \
  sed "s|COMPONENT_VERSION|${COMPONENT_VERSION}|g" > "${RECIPE_FILE}"

# Create component version in Greengrass
echo "Creating component version in Greengrass..."

aws greengrassv2 create-component-version \
  --inline-recipe fileb://"${RECIPE_FILE}" \
  --region "${REGION}" \
  --tags "dda-portal:managed=true,dda-portal:component-type=inference-uploader,dda-portal:shared-component=true"

# Clean up temporary file
rm -f "${RECIPE_FILE}"

COMPONENT_ARN="arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}:versions:${COMPONENT_VERSION}"

echo ""
echo "=== Component Published Successfully ==="
echo "Component Name: ${COMPONENT_NAME}"
echo "Version: ${COMPONENT_VERSION}"
echo "ARN: ${COMPONENT_ARN}"
echo "Bucket: ${BUCKET_NAME}"
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
