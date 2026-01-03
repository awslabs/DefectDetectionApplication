# Data Account S3 Management - Design

## Overview
Add support for managing S3 buckets in a Data Account (which can be the same as or different from the UseCase Account), including bucket creation, folder management, and file uploads.

## Architecture

### Data Flow
```
User Browser                Portal Lambda              Data Account
    │                            │                          │
    │  1. Upload files           │                          │
    │ ─────────────────────────► │                          │
    │                            │  2. Get presigned URL    │
    │                            │ ────────────────────────►│
    │                            │  3. Return presigned URL │
    │                            │ ◄────────────────────────│
    │  4. Presigned URL          │                          │
    │ ◄───────────────────────── │                          │
    │                            │                          │
    │  5. Direct upload to S3    │                          │
    │ ─────────────────────────────────────────────────────►│
```

### Account Configuration Options

**Option A: Same Account (Default)**
- `data_account_id` = `account_id` (or null)
- Uses existing `cross_account_role_arn`
- No additional IAM setup needed

**Option B: Separate Data Account**
- `data_account_id` = different account ID
- `data_account_role_arn` = role in data account
- `data_account_external_id` = external ID for data account role
- Requires IAM role in Data Account trusting Portal Account

## API Design

### 1. Create S3 Bucket
```
POST /api/v1/usecases/{usecase_id}/data/buckets
```

**Request:**
```json
{
  "bucket_name": "my-training-data-bucket",
  "region": "us-east-1",
  "enable_versioning": true,
  "encryption": "AES256"
}
```

**Response:**
```json
{
  "bucket_name": "my-training-data-bucket",
  "region": "us-east-1",
  "arn": "arn:aws:s3:::my-training-data-bucket",
  "created": true
}
```

### 2. List Buckets
```
GET /api/v1/usecases/{usecase_id}/data/buckets
```

**Response:**
```json
{
  "buckets": [
    {
      "name": "my-training-data-bucket",
      "creation_date": "2024-01-15T10:30:00Z",
      "region": "us-east-1"
    }
  ]
}
```

### 3. List Folders
```
GET /api/v1/usecases/{usecase_id}/data/folders?bucket={bucket}&prefix={prefix}
```

**Response:**
```json
{
  "bucket": "my-training-data-bucket",
  "prefix": "datasets/",
  "folders": [
    {"name": "raw/", "path": "datasets/raw/"},
    {"name": "labeled/", "path": "datasets/labeled/"}
  ],
  "files": [
    {"name": "readme.txt", "size": 1024, "last_modified": "2024-01-15T10:30:00Z"}
  ]
}
```

### 4. Create Folder
```
POST /api/v1/usecases/{usecase_id}/data/folders
```

**Request:**
```json
{
  "bucket": "my-training-data-bucket",
  "folder_path": "datasets/new-project/"
}
```

### 5. Get Upload URL (Presigned)
```
POST /api/v1/usecases/{usecase_id}/data/upload-url
```

**Request:**
```json
{
  "bucket": "my-training-data-bucket",
  "key": "datasets/images/image001.jpg",
  "content_type": "image/jpeg"
}
```

**Response:**
```json
{
  "upload_url": "https://s3.amazonaws.com/...",
  "expires_in": 3600,
  "key": "datasets/images/image001.jpg"
}
```

### 6. Batch Upload URLs
```
POST /api/v1/usecases/{usecase_id}/data/batch-upload-urls
```

**Request:**
```json
{
  "bucket": "my-training-data-bucket",
  "prefix": "datasets/images/",
  "files": [
    {"filename": "image001.jpg", "content_type": "image/jpeg"},
    {"filename": "image002.jpg", "content_type": "image/jpeg"}
  ]
}
```

**Response:**
```json
{
  "uploads": [
    {"filename": "image001.jpg", "key": "datasets/images/image001.jpg", "upload_url": "..."},
    {"filename": "image002.jpg", "key": "datasets/images/image002.jpg", "upload_url": "..."}
  ],
  "expires_in": 3600
}
```

## Data Model Changes

### UseCase Table Updates
```python
{
  # Existing fields...
  "usecase_id": "uuid",
  "account_id": "123456789012",
  "s3_bucket": "usecase-bucket",
  "cross_account_role_arn": "arn:aws:iam::123456789012:role/DDAPortalAccessRole",
  
  # New Data Account fields (optional)
  "data_account_id": "987654321098",           # If different from account_id
  "data_account_role_arn": "arn:aws:iam::987654321098:role/DDADataAccessRole",
  "data_account_external_id": "uuid",
  "data_s3_bucket": "data-bucket",             # Primary data bucket
  "data_s3_prefix": "datasets/"                # Default prefix
}
```

## IAM Requirements

### Data Account Role (when using separate account)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:PutBucketVersioning",
        "s3:PutBucketEncryption",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListAllMyBuckets"
      ],
      "Resource": "*"
    }
  ]
}
```

### SageMaker Cross-Account Access
The SageMaker execution role needs to access the Data Account bucket:
```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::data-bucket",
    "arn:aws:s3:::data-bucket/*"
  ]
}
```

And the Data Account bucket needs a bucket policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::USECASE_ACCOUNT:role/DDASageMakerExecutionRole"
      },
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::data-bucket",
        "arn:aws:s3:::data-bucket/*"
      ]
    }
  ]
}
```

## Implementation Files

1. `backend/functions/data_management.py` - New Lambda for data account operations
2. `backend/functions/usecases.py` - Update to handle data account fields
3. `infrastructure/lib/compute-stack.ts` - Add new Lambda and API routes
4. `frontend/src/pages/DataManagement.tsx` - New page for data management
5. `frontend/src/services/api.ts` - Add new API methods
