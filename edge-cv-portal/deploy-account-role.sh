#!/bin/bash

# Deploy cross-account role for Data Account access
# Creates IAM role that allows Portal Account to access Data Account resources

set -e

DATA_ACCOUNT_ID=${1:-}
PORTAL_ACCOUNT_ID=${2:-}

if [ -z "$DATA_ACCOUNT_ID" ] || [ -z "$PORTAL_ACCOUNT_ID" ]; then
    echo "Usage: $0 <data-account-id> <portal-account-id>"
    exit 1
fi

echo "Deploying cross-account role..."
echo "Data Account: $DATA_ACCOUNT_ID"
echo "Portal Account: $PORTAL_ACCOUNT_ID"

# Create trust policy
TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::$PORTAL_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)

# Create role
aws iam create-role \
    --role-name DDAPortalAccessRole \
    --assume-role-policy-document "$TRUST_POLICY" || true

# Attach policies
aws iam attach-role-policy \
    --role-name DDAPortalAccessRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

aws iam attach-role-policy \
    --role-name DDAPortalAccessRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonSageMakerFullAccess

echo "Cross-account role deployed successfully!"
