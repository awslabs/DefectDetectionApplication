#!/bin/bash
# Fix S3 object keys containing spaces by renaming them with underscores
#
# Usage:
#   ./fix_s3_spaces.sh s3://bucket/prefix/ [--profile PROFILE] [--dry-run] [--delete-original]
#
# Examples:
#   ./fix_s3_spaces.sh s3://my-bucket/images/ --dry-run
#   ./fix_s3_spaces.sh s3://my-bucket/images/ --profile my-profile --delete-original

set -euo pipefail

# Parse arguments
S3_URI=""
PROFILE=""
DRY_RUN=false
DELETE_ORIGINAL=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --delete-original)
            DELETE_ORIGINAL=true
            shift
            ;;
        s3://*)
            S3_URI="$1"
            shift
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 s3://bucket/prefix/ [--profile PROFILE] [--dry-run] [--delete-original]"
            exit 1
            ;;
    esac
done

if [[ -z "$S3_URI" ]]; then
    echo "Error: S3 URI required"
    echo "Usage: $0 s3://bucket/prefix/ [--profile PROFILE] [--dry-run] [--delete-original]"
    exit 1
fi

# Build AWS CLI command prefix
AWS_CMD="aws s3"
if [[ -n "$PROFILE" ]]; then
    AWS_CMD="$AWS_CMD --profile $PROFILE"
fi

echo "Scanning $S3_URI"
if [[ "$DRY_RUN" == true ]]; then
    echo "DRY-RUN MODE: No changes will be made"
fi
echo ""

# List all objects and filter for those with spaces
OBJECTS_WITH_SPACES=$(eval "$AWS_CMD ls --recursive $S3_URI" | awk '{$1=$2=$3=""; print $0}' | sed 's/^[ \t]*//' | grep ' ')

if [[ -z "$OBJECTS_WITH_SPACES" ]]; then
    echo "✓ No objects with spaces found"
    exit 0
fi

# Count objects
OBJECT_COUNT=$(echo "$OBJECTS_WITH_SPACES" | wc -l | tr -d ' ')
echo "Found $OBJECT_COUNT object(s) with spaces in their keys:"
echo ""

# Extract bucket and prefix from S3 URI
BUCKET=$(echo "$S3_URI" | sed 's|s3://||' | cut -d'/' -f1)
PREFIX=$(echo "$S3_URI" | sed 's|s3://||' | cut -d'/' -f2-)

SUCCESS_COUNT=0

# Process each object
while IFS= read -r RELATIVE_KEY; do
    ORIGINAL_KEY="${PREFIX}${RELATIVE_KEY}"
    NEW_KEY=$(echo "$ORIGINAL_KEY" | tr ' ' '_')
    
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [DRY-RUN] Would rename: $ORIGINAL_KEY -> $NEW_KEY"
        ((SUCCESS_COUNT++))
    else
        # Copy to new key
        if eval "$AWS_CMD cp s3://$BUCKET/$ORIGINAL_KEY s3://$BUCKET/$NEW_KEY" > /dev/null 2>&1; then
            echo "  ✓ Copied: $ORIGINAL_KEY -> $NEW_KEY"
            
            # Delete original if requested
            if [[ "$DELETE_ORIGINAL" == true ]]; then
                if eval "$AWS_CMD rm s3://$BUCKET/$ORIGINAL_KEY" > /dev/null 2>&1; then
                    echo "  ✓ Deleted original: $ORIGINAL_KEY"
                else
                    echo "  ✗ Failed to delete original: $ORIGINAL_KEY"
                fi
            else
                echo "  ⚠ Original kept: $ORIGINAL_KEY (use --delete-original to remove)"
            fi
            
            ((SUCCESS_COUNT++))
        else
            echo "  ✗ Failed to rename: $ORIGINAL_KEY"
        fi
    fi
done <<< "$OBJECTS_WITH_SPACES"

echo ""
echo "======================================================================"
if [[ "$DRY_RUN" == true ]]; then
    echo "DRY-RUN: Would rename $SUCCESS_COUNT/$OBJECT_COUNT objects"
else
    echo "Successfully renamed $SUCCESS_COUNT/$OBJECT_COUNT objects"
    if [[ "$DELETE_ORIGINAL" == false ]]; then
        echo "Original objects were kept (use --delete-original to remove them)"
    fi
fi
echo "======================================================================"
