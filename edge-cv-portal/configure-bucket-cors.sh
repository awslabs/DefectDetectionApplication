#!/bin/bash

# Configure CORS on S3 bucket for portal access
# Allows browser uploads from the portal frontend

set -e

BUCKET_NAME=${1:-}
CLOUDFRONT_DOMAIN=${2:-}

if [ -z "$BUCKET_NAME" ] || [ -z "$CLOUDFRONT_DOMAIN" ]; then
    echo "Usage: $0 <bucket-name> <cloudfront-domain>"
    exit 1
fi

echo "Configuring CORS for bucket: $BUCKET_NAME"

# Build CORS configuration
CORS_CONFIG=$(cat <<EOF
{
  "CORSRules": [
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
      "AllowedOrigins": ["https://$CLOUDFRONT_DOMAIN"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    },
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedOrigins": ["*"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
}
EOF
)

# Apply CORS configuration
aws s3api put-bucket-cors \
    --bucket "$BUCKET_NAME" \
    --cors-configuration "$CORS_CONFIG"

echo "CORS configured successfully!"
