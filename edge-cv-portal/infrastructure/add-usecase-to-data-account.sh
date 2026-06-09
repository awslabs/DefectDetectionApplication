#!/bin/bash
set -e

# Script to add a new UseCase Account to Data Account S3 bucket policy
# This allows incremental onboarding without redeploying the CDK stack

echo "=========================================="
echo "Add UseCase Account to Data Account"
echo "=========================================="
echo ""

# Get Data Account ID
read -p "Enter Data Account ID: " DATA_ACCOUNT_ID

# Get UseCase Account ID to add
read -p "Enter UseCase Account ID to add: " USECASE_ACCOUNT_ID

# Get bucket name(s)
read -p "Enter S3 bucket name(s) in Data Account (comma-separated): " BUCKET_NAMES

# Convert comma-separated buckets to array
IFS=',' read -ra BUCKETS <<< "$BUCKET_NAMES"

echo ""
echo "Configuration:"
echo "  Data Account: $DATA_ACCOUNT_ID"
echo "  UseCase Account: $USECASE_ACCOUNT_ID"
echo "  Buckets: ${BUCKETS[@]}"
echo ""
read -p "Continue? (y/n): " CONFIRM

if [ "$CONFIRM" != "y" ]; then
    echo "Aborted."
    exit 0
fi

# Function to update bucket policy
update_bucket_policy() {
    local BUCKET=$1
    
    echo ""
    echo "Updating bucket policy for: $BUCKET"
    
    # Get current bucket policy
    CURRENT_POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text 2>/dev/null || echo "")
    
    if [ -z "$CURRENT_POLICY" ]; then
        echo "No existing policy found. Creating new policy..."
        
        # Create new policy
        NEW_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowUseCaseAccountAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::${USECASE_ACCOUNT_ID}:role/DDASageMakerExecutionRole"
      },
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:GetObjectTagging",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:PutObject",
        "s3:PutObjectTagging"
      ],
      "Resource": [
        "arn:aws:s3:::${BUCKET}",
        "arn:aws:s3:::${BUCKET}/*"
      ]
    }
  ]
}
EOF
)
    else
        echo "Existing policy found. Updating..."
        
        # Parse existing policy and add new account
        NEW_POLICY=$(echo "$CURRENT_POLICY" | jq --arg account "$USECASE_ACCOUNT_ID" '
          .Statement[] |= 
          if .Sid == "AllowUseCaseAccountAccess" then
            .Principal.AWS |= 
            if type == "string" then
              [., "arn:aws:iam::\($account):role/DDASageMakerExecutionRole"] | unique
            else
              . + ["arn:aws:iam::\($account):role/DDASageMakerExecutionRole"] | unique
            end
          else
            .
          end
        ')
        
        # If statement doesn't exist, add it
        if ! echo "$CURRENT_POLICY" | jq -e '.Statement[] | select(.Sid == "AllowUseCaseAccountAccess")' > /dev/null; then
            NEW_POLICY=$(echo "$CURRENT_POLICY" | jq --arg account "$USECASE_ACCOUNT_ID" --arg bucket "$BUCKET" '
              .Statement += [{
                "Sid": "AllowUseCaseAccountAccess",
                "Effect": "Allow",
                "Principal": {
                  "AWS": "arn:aws:iam::\($account):role/DDASageMakerExecutionRole"
                },
                "Action": [
                  "s3:GetObject",
                  "s3:GetObjectVersion",
                  "s3:GetObjectTagging",
                  "s3:ListBucket",
                  "s3:GetBucketLocation",
                  "s3:PutObject",
                  "s3:PutObjectTagging"
                ],
                "Resource": [
                  "arn:aws:s3:::\($bucket)",
                  "arn:aws:s3:::\($bucket)/*"
                ]
              }]
            ')
        fi
    fi
    
    # Apply the policy
    echo "$NEW_POLICY" > /tmp/bucket-policy.json
    aws s3api put-bucket-policy --bucket "$BUCKET" --policy file:///tmp/bucket-policy.json
    rm /tmp/bucket-policy.json
    
    echo "✓ Bucket policy updated for $BUCKET"
}

# Update policy for each bucket
for BUCKET in "${BUCKETS[@]}"; do
    # Trim whitespace
    BUCKET=$(echo "$BUCKET" | xargs)
    update_bucket_policy "$BUCKET"
done

echo ""
echo "=========================================="
echo "✓ UseCase Account Added Successfully"
echo "=========================================="
echo ""
echo "UseCase Account $USECASE_ACCOUNT_ID now has access to:"
for BUCKET in "${BUCKETS[@]}"; do
    BUCKET=$(echo "$BUCKET" | xargs)
    echo "  - s3://$BUCKET"
done
echo ""
echo "Next steps:"
echo "1. Onboard the usecase in the DDA Portal"
echo "2. Configure the usecase to use Data Account buckets"
echo ""
