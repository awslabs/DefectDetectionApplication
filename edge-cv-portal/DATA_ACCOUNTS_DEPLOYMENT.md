# Data Accounts Management - Deployment Guide

## Status: ✅ READY TO DEPLOY

All code changes are complete. The Data Accounts Management feature is ready for deployment.

## What Was Completed

### ✅ Backend
- **Lambda Function**: `data_accounts.py` - Full CRUD operations for Data Accounts
- **API Routes**: All endpoints configured in `compute-stack.ts`
  - `GET /data-accounts` - List all Data Accounts
  - `POST /data-accounts` - Register new Data Account
  - `GET /data-accounts/{id}` - Get Data Account details
  - `PUT /data-accounts/{id}` - Update Data Account
  - `DELETE /data-accounts/{id}` - Delete Data Account
  - `POST /data-accounts/{id}/test` - Test connection
- **DynamoDB Table**: `dda-portal-data-accounts` created in `storage-stack.ts`
- **Permissions**: Lambda has read/write access to Data Accounts table

### ✅ Frontend
- **Settings Page**: `frontend/src/pages/Settings.tsx` - Complete UI for managing Data Accounts
- **API Service**: Added 6 new methods to `api.ts`:
  - `listDataAccounts()`
  - `getDataAccount(accountId)`
  - `createDataAccount(data)`
  - `updateDataAccount(accountId, data)`
  - `deleteDataAccount(accountId)`
  - `testDataAccountConnection(accountId)`
- **Routing**: Settings route already configured in `App.tsx`
- **Navigation**: Settings link already in Layout (Portal Admin only)

### ✅ Infrastructure
- **Storage Stack**: DataAccounts table with GSI on status
- **Compute Stack**: Lambda function with proper environment variables
- **App Stack**: dataAccountsTable passed from storage to compute

## Deployment Steps

### 1. Deploy Infrastructure (if not already deployed)

```bash
cd edge-cv-portal/infrastructure
npm run build
rm -rf cdk.out

# Deploy storage stack (creates DynamoDB table)
cdk deploy EdgeCVPortalStack-StorageStack

# Deploy compute stack (creates Lambda function and API routes)
cdk deploy EdgeCVPortalStack-ComputeStack
```

### 2. Deploy Frontend

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

## Testing the Feature

### 1. Access Settings Page

1. Log in as **PortalAdmin**
2. Navigate to **Settings** from the left sidebar
3. Click on **Data Accounts** tab

### 2. Register a Data Account

1. Click **"Add Data Account"**
2. Fill in the form:
   - **Account ID**: `619071348270`
   - **Name**: `Production Data Account`
   - **Description**: `Centralized data storage for production usecases`
   - **Role ARN**: `arn:aws:iam::619071348270:role/DDAPortalAccessRole`
   - **External ID**: `7B1EA7C8-A279-4F44-9732-E1C912F01272`
   - **Default Bucket**: `dda-training-data`
   - **Default Prefix**: `datasets/`
   - **Region**: `us-east-1`
3. Click **"Register"**

### 3. Test Connection

1. Click the **test icon** (checkmark) next to the Data Account
2. Verify it shows "Connected" status

### 4. Create UseCase with Data Account

Currently, the UseCase onboarding still requires manual entry of Data Account details. 

**Next Enhancement** (optional): Add a dropdown in `UseCaseOnboarding.tsx` to select from registered Data Accounts instead of manual entry.

## API Endpoints

All endpoints require **PortalAdmin** role.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/data-accounts` | List all Data Accounts |
| POST | `/api/v1/data-accounts` | Register new Data Account |
| GET | `/api/v1/data-accounts/{id}` | Get Data Account details |
| PUT | `/api/v1/data-accounts/{id}` | Update Data Account |
| DELETE | `/api/v1/data-accounts/{id}` | Delete Data Account |
| POST | `/api/v1/data-accounts/{id}/test` | Test connection |

## Data Model

```json
{
  "data_account_id": "619071348270",
  "name": "Production Data Account",
  "description": "Centralized data storage for all production usecases",
  "role_arn": "arn:aws:iam::619071348270:role/DDAPortalAccessRole",
  "external_id": "7B1EA7C8-A279-4F44-9732-E1C912F01272",
  "default_bucket": "dda-training-data",
  "default_prefix": "datasets/",
  "region": "us-east-1",
  "status": "active",
  "created_at": 1737504000000,
  "created_by": "admin@example.com",
  "updated_at": 1737504000000,
  "connection_test": {
    "status": "success",
    "message": "Successfully connected to Data Account",
    "bucket_region": "us-east-1"
  },
  "last_tested_at": 1737504000000
}
```

## Benefits

✅ **Centralized Management**: Register Data Accounts once, use across multiple usecases
✅ **No Manual Entry**: Admins don't need to remember account IDs and ARNs
✅ **Connection Validation**: Test connection before registering
✅ **Audit Trail**: All changes logged in audit table
✅ **Secure**: External ID required, credentials never stored

## Future Enhancements

1. **UseCase Onboarding Dropdown**: Add Data Account selector in UseCase creation form
2. **Multi-bucket Support**: Allow multiple buckets per Data Account
3. **Usage Tracking**: Show which usecases use each Data Account
4. **Health Monitoring**: Periodic connection tests
5. **Cost Allocation**: Track data transfer costs per usecase

## Files Changed

### Backend
- `backend/functions/data_accounts.py` (already exists)

### Frontend
- `frontend/src/services/api.ts` (added 6 methods)
- `frontend/src/pages/Settings.tsx` (fixed API calls)

### Infrastructure
- `infrastructure/lib/storage-stack.ts` (table already exists)
- `infrastructure/lib/compute-stack.ts` (Lambda already exists)
- `infrastructure/bin/app.ts` (already passing dataAccountsTable)

## Verification Checklist

- [x] Backend Lambda function exists
- [x] API routes configured
- [x] DynamoDB table created
- [x] Frontend Settings page created
- [x] API service methods added
- [x] Settings route configured
- [x] Navigation link added
- [ ] Infrastructure deployed
- [ ] Frontend deployed
- [ ] Feature tested end-to-end

## Ready to Deploy!

All code is complete. Just run the deployment commands above.
