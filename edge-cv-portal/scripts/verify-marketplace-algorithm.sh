#!/bin/bash

# Script to verify AWS Marketplace algorithm subscription
# This helps confirm the algorithm is available before creating training jobs

set -e

echo "========================================="
echo "AWS Marketplace Algorithm Verification"
echo "========================================="
echo ""

# Check if AWS CLI is configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo "❌ Error: AWS CLI is not configured or credentials are invalid"
    echo "   Please run 'aws configure' first"
    exit 1
fi

# Get current account info
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
REGION=$(aws configure get region || echo "us-east-1")

echo "Current Account: $ACCOUNT_ID"
echo "Current Region: $REGION"
echo ""

# Search for the marketplace algorithm
echo "Searching for marketplace algorithm..."
echo ""

ALGORITHMS=$(aws sagemaker list-algorithms \
    --name-contains "lfv-public-algorithm" \
    --region "$REGION" \
    --output json 2>&1)

if [ $? -ne 0 ]; then
    echo "❌ Error querying SageMaker algorithms:"
    echo "$ALGORITHMS"
    exit 1
fi

# Parse and display results
ALGO_COUNT=$(echo "$ALGORITHMS" | jq -r '.AlgorithmSummaryList | length')

if [ "$ALGO_COUNT" -eq 0 ]; then
    echo "❌ No marketplace algorithm found!"
    echo ""
    echo "This means the account has NOT subscribed to the algorithm."
    echo ""
    echo "To fix this:"
    echo "1. Visit: https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6"
    echo "2. Click 'Continue to Subscribe'"
    echo "3. Accept the offer"
    echo "4. Wait 2-5 minutes for activation"
    echo "5. Run this script again"
    echo ""
    exit 1
fi

echo "✅ Found $ALGO_COUNT marketplace algorithm(s):"
echo ""

# Display each algorithm
echo "$ALGORITHMS" | jq -r '.AlgorithmSummaryList[] | 
    "Algorithm ARN: \(.AlgorithmArn)\n" +
    "Status: \(.AlgorithmStatus)\n" +
    "Created: \(.CreationTime)\n"'

# Get the first algorithm ARN
ALGO_ARN=$(echo "$ALGORITHMS" | jq -r '.AlgorithmSummaryList[0].AlgorithmArn')

echo "========================================="
echo "✅ Verification Complete!"
echo "========================================="
echo ""
echo "The marketplace algorithm is available and ready to use."
echo ""
echo "Algorithm ARN:"
echo "$ALGO_ARN"
echo ""
echo "You can now create training jobs in the DDA Portal."
echo ""
