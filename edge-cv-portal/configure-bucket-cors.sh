#!/bin/bash
# Configure CORS for S3 buckets to allow portal uploads
# Run this script to enable browser-based file uploads from the DDA Portal

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

printf "${GREEN}========================================${NC}\n"
printf "${GREEN}DDA Portal - S3 Bucket CORS Configuration${NC}\n"
printf "${GREEN}========================================${NC}\n"
echo ""

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    printf "${RED}Error: AWS CLI not found${NC}\n"
    exit 1
fi

# Get bucket name
if [ -n "$1" ]; then
    BUCKET_NAME="$1"
else
    read -p "Enter S3 bucket name: " BUCKET_NAME
fi

if [ -z "$BUCKET_NAME" ]; then
    printf "${RED}Error: Bucket name required${NC}\n"
    exit 1
fi

# Get CloudFront domain
if [ -n "$2" ]; then
    CLOUDFRONT_DOMAIN="$2"
else
    read -p "Enter Portal CloudFront domain (e.g., d3qeryypza4i9i.cloudfront.net): " CLOUDFRONT_DOMAIN
fi

if [ -z "$CLOUDFRONT_DOMAIN" ]; then
    printf "${RED}Error: CloudFront domain required${NC}\n"
    exit 1
fi

# Verify bucket exists
printf "${BLUE}Checking bucket...${NC}\n"
if ! aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
    printf "${RED}Error: Bucket '$BUCKET_NAME' not found or access denied${NC}\n"
    exit 1
fi

printf "${GREEN}✓ Bucket found${NC}\n"
echo ""

# Show current CORS (if any)
printf "${BLUE}Current CORS configuration:${NC}\n"
CURRENT_CORS=$(aws s3api get-bucket-cors --bucket "$BUCKET_NAME" 2>/dev/null)
if [ -n "$CURRENT_CORS" ]; then
    echo "$CURRENT_CORS"
else
    echo "  (none)"
fi
echo ""

# Create CORS configuration
CORS_CONFIG=$(cat <<CORS
{
    "CORSRules": [
        {
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "PUT", "POST", "HEAD", "DELETE"],
            "AllowedOrigins": ["https://${CLOUDFRONT_DOMAIN}"],
            "ExposeHeaders": ["ETag", "x-amz-meta-custom-header"],
            "MaxAgeSeconds": 3000
        }
    ]
}
CORS
)

printf "${YELLOW}New CORS configuration:${NC}\n"
echo "$CORS_CONFIG"
echo ""

read -p "Apply this CORS configuration? [y/N]: " CONFIRM

if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    # Write CORS config to temp file (more reliable than stdin)
    TEMP_FILE=$(mktemp)
    printf '%s' "$CORS_CONFIG" > "$TEMP_FILE"
    
    if aws s3api put-bucket-cors --bucket "$BUCKET_NAME" --cors-configuration "file://$TEMP_FILE"; then
        printf "${GREEN}✓ CORS configured successfully!${NC}\n"
        echo ""
        echo "The bucket '$BUCKET_NAME' now allows uploads from:"
        printf "  ${GREEN}https://${CLOUDFRONT_DOMAIN}${NC}\n"
    else
        printf "${RED}✗ Failed to configure CORS${NC}\n"
        rm -f "$TEMP_FILE"
        exit 1
    fi
    
    rm -f "$TEMP_FILE"
else
    printf "${YELLOW}Cancelled${NC}\n"
    exit 0
fi

echo ""

# Also ensure bucket is tagged for portal access
printf "${BLUE}Checking portal access tag...${NC}\n"
CURRENT_TAGS=$(aws s3api get-bucket-tagging --bucket "$BUCKET_NAME" 2>/dev/null)

if echo "$CURRENT_TAGS" | grep -q "dda-portal:managed"; then
    printf "${GREEN}✓ Bucket already tagged for portal access${NC}\n"
else
    read -p "Tag bucket for portal access (dda-portal:managed=true)? [y/N]: " TAG_CONFIRM
    
    if [ "$TAG_CONFIRM" = "y" ] || [ "$TAG_CONFIRM" = "Y" ]; then
        if aws s3api put-bucket-tagging --bucket "$BUCKET_NAME" --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'; then
            printf "${GREEN}✓ Bucket tagged for portal access${NC}\n"
        else
            printf "${YELLOW}⚠ Failed to tag bucket (may need to preserve existing tags)${NC}\n"
        fi
    fi
fi

echo ""
printf "${GREEN}========================================${NC}\n"
printf "${GREEN}Configuration Complete!${NC}\n"
printf "${GREEN}========================================${NC}\n"
echo ""
echo "You can now upload files to this bucket from the DDA Portal."
echo ""
