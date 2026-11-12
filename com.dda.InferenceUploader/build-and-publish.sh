#!/bin/bash
set -e

COMPONENT_NAME="com.dda.InferenceUploader"
COMPONENT_VERSION="1.0.0"

# The component bucket name is DERIVED from the account id and region, so
# every S3 call against it asserts ExpectedBucketOwner (see the guard's header
# for the bucket-squatting threat this closes).
. "$(dirname "$0")/../scripts/s3-bucket-owner-guard.sh"

echo "Building and publishing ${COMPONENT_NAME} v${COMPONENT_VERSION}"

# Get account and region info
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -z "$ACCOUNT_ID" ] || [ "$ACCOUNT_ID" = "None" ]; then
    echo "ERROR: Could not resolve the AWS account id (aws sts get-caller-identity)." >&2
    exit 1
fi
REGION=$(aws configure get region || echo "us-east-1")
BUCKET_NAME="$(dda_component_bucket_name "$REGION" "$ACCOUNT_ID")" || exit 1

echo "Using S3 bucket: $BUCKET_NAME"
echo "Account ID: $ACCOUNT_ID"
echo "Region: $REGION"

# Verify this account owns the bucket (creating it only when S3 reports it
# absent, then re-verifying). A bucket of this name under another account is
# a hard error: the guard prints the conflict and we stop before any upload.
dda_ensure_owned_bucket "$BUCKET_NAME" "$REGION" "$ACCOUNT_ID" || exit 1

# Upload artifacts to S3 with ExpectedBucketOwner asserted on each PutObject.
ARTIFACT_PREFIX="${COMPONENT_NAME}/${COMPONENT_VERSION}"
echo "Uploading artifacts to s3://${BUCKET_NAME}/${ARTIFACT_PREFIX}/"

dda_s3_put_object "${BUCKET_NAME}" "${ARTIFACT_PREFIX}/inference_uploader.py" \
    artifacts/inference_uploader.py "${REGION}" "${ACCOUNT_ID}" || exit 1
dda_s3_put_object "${BUCKET_NAME}" "${ARTIFACT_PREFIX}/requirements.txt" \
    artifacts/requirements.txt "${REGION}" "${ACCOUNT_ID}" || exit 1

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
