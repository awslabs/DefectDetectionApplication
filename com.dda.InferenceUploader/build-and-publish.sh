#!/bin/bash
set -e

COMPONENT_NAME="com.dda.InferenceUploader"
COMPONENT_VERSION="1.0.0"

echo "Building and publishing ${COMPONENT_NAME} v${COMPONENT_VERSION}"

# Get account and region info
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")
BUCKET_NAME="dda-component-${REGION}-${ACCOUNT_ID}"

echo "Using S3 bucket: $BUCKET_NAME"
echo "Account ID: $ACCOUNT_ID"
echo "Region: $REGION"

# Create bucket if it doesn't exist
if ! aws s3 ls "s3://${BUCKET_NAME}" 2>/dev/null; then
    echo "Creating S3 bucket: ${BUCKET_NAME}"
    aws s3 mb "s3://${BUCKET_NAME}" --region "${REGION}"
fi

# Upload artifacts to S3
ARTIFACT_PREFIX="${COMPONENT_NAME}/${COMPONENT_VERSION}"
echo "Uploading artifacts to s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/"

aws s3 cp artifacts/inference_uploader.py "s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/inference_uploader.py"
aws s3 cp artifacts/requirements.txt "s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/requirements.txt"

echo "Artifacts uploaded successfully"

# Update recipe with actual bucket name
RECIPE_FILE="recipe.yaml"
cp recipe.yaml "${RECIPE_FILE}.tmp"
sed "s|BUCKET_NAME|${BUCKET_NAME}|g" "${RECIPE_FILE}.tmp" > "${RECIPE_FILE}.processed"
rm "${RECIPE_FILE}.tmp"

# Create component version in Greengrass
echo "Creating component version in Greengrass..."

RECIPE_CONTENT=$(cat "${RECIPE_FILE}.processed")

aws greengrassv2 create-component-version \
  --inline-recipe "${RECIPE_CONTENT}" \
  --region "${REGION}" \
  --tags "dda-portal:managed=true" \
         "dda-portal:component-type=inference-uploader" \
         "dda-portal:version=${COMPONENT_VERSION}"

rm "${RECIPE_FILE}.processed"

COMPONENT_ARN="arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}:versions:${COMPONENT_VERSION}"

echo ""
echo "=== Component Published Successfully ==="
echo "Component Name: ${COMPONENT_NAME}"
echo "Version: ${COMPONENT_VERSION}"
echo "ARN: ${COMPONENT_ARN}"
echo "Bucket: ${BUCKET_NAME}"
echo "Artifact Path: ${ARTIFACT_PREFIX}/"
echo ""
echo "=== Next Steps ==="
echo "1. Deploy this component to devices via the DDA Portal"
echo "2. Configure the component with:"
echo "   - s3Bucket: dda-inference-results-${ACCOUNT_ID}"
echo "   - s3Prefix: {usecase-id}/{device-id}"
echo "   - uploadIntervalSeconds: 300 (5 minutes)"
echo "3. Ensure GreengrassV2TokenExchangeRole has DDAPortalComponentAccessPolicy attached"
echo ""
