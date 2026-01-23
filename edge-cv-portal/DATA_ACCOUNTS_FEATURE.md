# Data Accounts Management Feature

## Overview

This feature allows Portal Admins to register and manage Data Accounts centrally, making it easy for users to create usecases with cross-account data access.

## Implementation Status

### ✅ Completed
1. **DynamoDB Table**: `dda-portal-data-accounts` added to storage-stack.ts
2. **Backend API**: `data_accounts.py` created with full CRUD operations
3. **Auto-configuration**: Usecase onboarding automatically updates Data Account bucket policies

### 🚧 To Complete

#### 1. Update Compute Stack
Add Lambda function and API Gateway route for Data Accounts:

**File**: `infrastructure/lib/compute-stack.ts`

```typescript
// Add Data Accounts Lambda
const dataAccountsFunction = new lambda.Function(this, 'DataAccountsFunction', {
  functionName: 'EdgeCVPortal-DataAccounts',
  runtime: lambda.Runtime.PYTHON_3_11,
  handler: 'data_accounts.handler',
  code: lambda.Code.fromAsset('backend/functions'),
  layers: [sharedLayer],
  environment: {
    DATA_ACCOUNTS_TABLE: props.dataAccountsTable.tableName,
    AUDIT_LOG_TABLE: props.auditLogTable.tableName,
  },
  timeout: cdk.Duration.seconds(30),
});

// Grant permissions
props.dataAccountsTable.grantReadWriteData(dataAccountsFunction);
props.auditLogTable.grantWriteData(dataAccountsFunction);

// Add API routes
api.root.addResource('data-accounts')
  .addMethod('GET', new apigateway.LambdaIntegration(dataAccountsFunction), { authorizer });
api.root.addResource('data-accounts')
  .addMethod('POST', new apigateway.LambdaIntegration(dataAccountsFunction), { authorizer });
// ... add other routes
```

#### 2. Create Frontend - Settings Page

**File**: `frontend/src/pages/Settings.tsx`

```typescript
import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Tabs,
  Table,
  Button,
  Modal,
  Form,
  FormField,
  Input,
  Textarea,
  SpaceBetween,
  Alert,
  Badge,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

export default function Settings() {
  const [activeTab, setActiveTab] = useState('data-accounts');
  const [dataAccounts, setDataAccounts] = useState([]);
  const [showAddModal, setShowAddModal] = useState(false);
  
  // ... implementation
  
  return (
    <Container>
      <Header variant="h1">Portal Settings</Header>
      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          {
            id: 'data-accounts',
            label: 'Data Accounts',
            content: <DataAccountsTab />
          },
          {
            id: 'general',
            label: 'General',
            content: <GeneralSettingsTab />
          }
        ]}
      />
    </Container>
  );
}
```

#### 3. Update Create UseCase Form

**File**: `frontend/src/pages/CreateUseCase.tsx`

Add Data Account dropdown:

```typescript
const [dataAccounts, setDataAccounts] = useState([]);
const [selectedDataAccount, setSelectedDataAccount] = useState(null);

// Load Data Accounts
useEffect(() => {
  apiService.get('/data-accounts').then(response => {
    setDataAccounts(response.data_accounts);
  });
}, []);

// In the form:
<FormField label="Data Storage">
  <RadioGroup
    value={dataStorageType}
    onChange={({ detail }) => setDataStorageType(detail.value)}
    items={[
      { value: 'usecase', label: 'Use UseCase Account' },
      { value: 'data', label: 'Use Separate Data Account' }
    ]}
  />
</FormField>

{dataStorageType === 'data' && (
  <FormField label="Data Account">
    <Select
      selectedOption={selectedDataAccount}
      onChange={({ detail }) => setSelectedDataAccount(detail.selectedOption)}
      options={dataAccounts.map(da => ({
        label: da.name,
        value: da.data_account_id,
        description: `${da.default_bucket} (${da.region})`
      }))}
      placeholder="Select a Data Account"
    />
  </FormField>
)}
```

## User Workflow

### Portal Admin: Register Data Account

1. Navigate to **Settings → Data Accounts**
2. Click **"Add Data Account"**
3. Fill in form:
   - Name: "Production Data Account"
   - Account ID: 619071348270
   - Role ARN: arn:aws:iam::619071348270:role/DDAPortalAccessRole
   - External ID: (from deployment)
   - Default Bucket: dda-training-data
   - Region: us-east-1
4. Click **"Test Connection"** (validates access)
5. Click **"Register"**

### User: Create UseCase with Data Account

1. Navigate to **UseCases → Create UseCase**
2. Fill in basic info
3. Under "Data Storage", select **"Use Separate Data Account"**
4. Select from dropdown: **"Production Data Account"**
5. Click **"Create"**

**Behind the scenes:**
- Portal automatically updates Data Account bucket policy
- Grants UseCase Account's SageMaker role read access
- No manual configuration needed!

## API Endpoints

| Method | Endpoint | Description | Access |
|--------|----------|-------------|--------|
| GET | `/api/v1/data-accounts` | List Data Accounts | PortalAdmin |
| POST | `/api/v1/data-accounts` | Register Data Account | PortalAdmin |
| GET | `/api/v1/data-accounts/{id}` | Get details | PortalAdmin |
| PUT | `/api/v1/data-accounts/{id}` | Update | PortalAdmin |
| DELETE | `/api/v1/data-accounts/{id}` | Delete | PortalAdmin |
| POST | `/api/v1/data-accounts/{id}/test` | Test connection | PortalAdmin |

## Data Model

### DataAccounts Table

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
  "tags": {
    "environment": "production",
    "cost_center": "ml-ops"
  },
  "connection_test": {
    "status": "success",
    "message": "Successfully connected to Data Account",
    "bucket_region": "us-east-1"
  },
  "last_tested_at": 1737504000000
}
```

## Deployment Steps

1. **Deploy infrastructure** (adds DynamoDB table):
   ```bash
   cd edge-cv-portal/infrastructure
   npm run build
   rm -rf cdk.out
   cdk deploy EdgeCVPortalStack-StorageStack
   ```

2. **Deploy backend** (adds Lambda function):
   ```bash
   cdk deploy EdgeCVPortalStack-ComputeStack
   ```

3. **Deploy frontend** (adds Settings page):
   ```bash
   ./deploy-frontend.sh
   ```

## Benefits

✅ **No manual configuration**: Portal handles bucket policy updates automatically
✅ **Centralized management**: Admins register Data Accounts once
✅ **Easy onboarding**: Users select from dropdown, no need to remember account IDs
✅ **Connection validation**: Test connection before registering
✅ **Audit trail**: All changes logged
✅ **Scalable**: Add unlimited Data Accounts

## Security

- Only PortalAdmin can manage Data Accounts
- External ID required for all cross-account access
- Connection tested before registration
- Credentials never stored (only role ARN + external ID)
- Bucket policies use least-privilege (read-only for SageMaker)

## Future Enhancements

1. **Multi-bucket support**: Allow multiple buckets per Data Account
2. **Usage tracking**: Show which usecases use each Data Account
3. **Health monitoring**: Periodic connection tests
4. **Cost allocation**: Track data transfer costs per usecase
5. **Bucket browser**: Browse Data Account buckets from portal
