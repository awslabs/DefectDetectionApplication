# Data Account S3 Management Feature

## Overview
Enable the Edge CV Portal to manage S3 buckets for training data storage. The data bucket can be:
1. **Same Account** - In the UseCase Account (default, uses existing role)
2. **Separate Account** - In a dedicated Data Account (requires additional role)

This flexibility allows customers to either keep everything in one account or use a centralized data lake.

## User Stories

### US-1: Configure Data Storage Location
**As a** UseCase Admin  
**I want to** configure where training data is stored  
**So that** I can use our existing data infrastructure

**Acceptance Criteria:**
- [ ] Can choose "Same as UseCase Account" or "Separate Data Account"
- [ ] If separate account: can specify Data Account ID and role ARN
- [ ] If same account: uses existing UseCase Account role
- [ ] Portal validates access before saving

### US-2: Create S3 Bucket in Data Account
**As a** UseCase Admin  
**I want to** create a new S3 bucket in the Data Account via the portal  
**So that** I don't need to manually create buckets in AWS Console

**Acceptance Criteria:**
- [ ] Can specify a bucket name with validation (DNS-compliant, unique)
- [ ] Can select the AWS region for the bucket
- [ ] Bucket is created with recommended settings (versioning, encryption)
- [ ] Appropriate bucket policy is applied for cross-account access
- [ ] Error handling for bucket creation failures (name taken, permissions)

### US-3: Browse and Select Folder in Data Account Bucket
**As a** Data Scientist  
**I want to** browse folders in the Data Account S3 bucket  
**So that** I can select the correct location for my training data

**Acceptance Criteria:**
- [ ] Can list buckets in the Data Account
- [ ] Can browse folder structure within a bucket
- [ ] Can select a folder as the data source
- [ ] Can create new folders within the bucket

### US-4: Upload Images from Local to Data Account
**As a** Data Scientist  
**I want to** upload images from my local machine to a folder in the bucket  
**So that** I can add training data without using AWS Console

**Acceptance Criteria:**
- [ ] Can browse and select local files/folders
- [ ] Can select or create destination folder in bucket
- [ ] Progress indicator for upload operation
- [ ] Support for batch uploads (multiple files)
- [ ] Handles large files with multipart upload
- [ ] Shows upload status and errors

### US-5: Use Data Account Bucket for Training
**As a** Data Scientist  
**I want to** use images from the Data Account bucket for training  
**So that** I can train models on centralized data

**Acceptance Criteria:**
- [ ] Training job can read from Data Account bucket
- [ ] SageMaker execution role has cross-account access to Data Account
- [ ] Manifest files reference Data Account S3 URIs correctly

## Architecture

### Account Structure Options

**Option A: Same Account (Default)**
```
┌─────────────────┐     ┌─────────────────────────────┐
│  Portal Account │     │      UseCase Account        │
│                 │     │                             │
│  - Portal UI    │────▶│  - SageMaker                │
│  - Lambda       │     │  - Greengrass               │
│  - DynamoDB     │     │  - S3 Buckets (data+models) │
└─────────────────┘     └─────────────────────────────┘
```

**Option B: Separate Data Account**
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Portal Account │     │ UseCase Account │     │  Data Account   │
│                 │     │                 │     │                 │
│  - Portal UI    │────▶│  - SageMaker    │────▶│  - S3 Buckets   │
│  - Lambda       │     │  - Greengrass   │     │  - Training Data│
│  - DynamoDB     │     │  - Models       │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

### IAM Role Structure
1. **DDAPortalAccessRole** (in UseCase Account) - existing
   - Used when data is in same account
   - Already has S3 permissions
   
2. **DDAPortalDataAccountRole** (in Data Account) - new, optional
   - Only needed when using separate Data Account
   - Trusted by Portal Account
   - Permissions: S3 bucket creation, object read/write, list

### Data Model Changes
```typescript
interface UseCase {
  // Existing fields...
  account_id: string;
  s3_bucket: string;
  cross_account_role_arn: string;
  
  // New Data Account fields (optional)
  data_account_id?: string;           // If different from account_id
  data_account_role_arn?: string;     // Role in data account
  data_account_external_id?: string;  // External ID for data account role
  data_s3_bucket?: string;            // Bucket in data account
  data_s3_prefix?: string;            // Prefix in data bucket
}
```

## API Endpoints

### Data Account Management
- `POST /api/v1/usecases/{id}/data-account` - Configure data account
- `GET /api/v1/usecases/{id}/data-account/verify` - Verify data account access

### S3 Bucket Management
- `POST /api/v1/usecases/{id}/data-account/buckets` - Create bucket
- `GET /api/v1/usecases/{id}/data-account/buckets` - List buckets
- `GET /api/v1/usecases/{id}/data-account/buckets/{bucket}/folders` - List folders
- `POST /api/v1/usecases/{id}/data-account/buckets/{bucket}/folders` - Create folder

### Data Copy Operations
- `POST /api/v1/usecases/{id}/data-account/copy` - Start copy operation
- `GET /api/v1/usecases/{id}/data-account/copy/{job_id}` - Get copy status

## Security Considerations
- External ID required for all cross-account role assumptions
- Bucket policies restrict access to specific accounts only
- All operations logged to audit trail
- Encryption at rest (SSE-S3 or SSE-KMS) required for new buckets

## Out of Scope (Future)
- Data Account bucket lifecycle policies
- Cross-region replication
- Data Account cost allocation
- Bulk data import from on-premises
