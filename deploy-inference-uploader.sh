#!/bin/bash
set -e

# Configuration
DEVICE_NAME="dda_edge_server_2"
USECASE_ACCOUNT="198226511894"
REGION="us-east-1"
USECASE_ID="cookie-factory"

# Component versions (update these as needed)
NUCLEUS_VERSION="2.13.0"
LOCALSERVER_VERSION="1.0.63"
INFERENCE_UPLOADER_VERSION="1.0.0"
MODEL_COMPONENT="model-cookie-defect-detection-arm64-cpu"
MODEL_VERSION="1.0.0"

# S3 bucket for inference results
S3_BUCKET="dda-inference-results-${USECASE_ACCOUNT}"
S3_PREFIX="${USECASE_ID}/${DEVICE_NAME}"

echo "Deploying InferenceUploader to device: ${DEVICE_NAME}"
echo "S3 Bucket: ${S3_BUCKET}"
echo "S3 Prefix: ${S3_PREFIX}"
echo ""

# Create deployment
aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:${REGION}:${USECASE_ACCOUNT}:thing/${DEVICE_NAME}" \
  --components "{
    \"aws.greengrass.Nucleus\": {
      \"componentVersion\": \"${NUCLEUS_VERSION}\"
    },
    \"aws.greengrass.LogManager\": {
      \"componentVersion\": \"2.3.9\"
    },
    \"aws.edgeml.dda.LocalServer.arm64\": {
      \"componentVersion\": \"${LOCALSERVER_VERSION}\"
    },
    \"aws.edgeml.dda.InferenceUploader\": {
      \"componentVersion\": \"${INFERENCE_UPLOADER_VERSION}\",
      \"configurationUpdate\": {
        \"merge\": \"{\\\"s3Bucket\\\":\\\"${S3_BUCKET}\\\",\\\"s3Prefix\\\":\\\"${S3_PREFIX}\\\",\\\"uploadIntervalSeconds\\\":300,\\\"batchSize\\\":100,\\\"localRetentionDays\\\":7,\\\"uploadImages\\\":true,\\\"uploadMetadata\\\":true,\\\"inferenceResultsPath\\\":\\\"/aws_dda/inference-results\\\",\\\"awsRegion\\\":\\\"${REGION}\\\"}\"
      }
    },
    \"${MODEL_COMPONENT}\": {
      \"componentVersion\": \"${MODEL_VERSION}\"
    }
  }" \
  --deployment-name "DDA-with-InferenceUploader-$(date +%Y%m%d-%H%M%S)" \
  --region "${REGION}"

echo ""
echo "Deployment created successfully!"
echo ""
echo "Monitor deployment status:"
echo "  aws greengrassv2 list-effective-deployments --core-device-thing-name ${DEVICE_NAME} --region ${REGION}"
echo ""
echo "Check component logs on device:"
echo "  sudo tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.InferenceUploader.log"
echo ""
