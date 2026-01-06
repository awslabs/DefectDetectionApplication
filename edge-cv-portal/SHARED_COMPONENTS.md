# DDA Portal Shared Components

This document describes how the DDA Portal shares Greengrass components (like `dda-LocalServer`) from the portal account to usecase accounts.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PORTAL ACCOUNT                                │
│  ┌─────────────────────┐    ┌──────────────────────────────────┐   │
│  │  Portal Artifacts   │    │  Greengrass Components           │   │
│  │  S3 Bucket          │    │  - aws.edgeml.dda.LocalServer.arm64 │
│  │  /shared-components/│    │  - aws.edgeml.dda.LocalServer.amd64 │
│  └─────────────────────┘    └──────────────────────────────────┘   │
│           │                              │                          │
│           │ Cross-account S3 access      │ Recipe template          │
│           ▼                              ▼                          │
└───────────┼──────────────────────────────┼──────────────────────────┘
            │                              │
            │                              │
┌───────────┼──────────────────────────────┼──────────────────────────┐
│           │     USECASE ACCOUNT          │                          │
│           │                              │                          │
│  ┌────────▼────────┐    ┌────────────────▼─────────────────────┐   │
│  │ Greengrass      │    │  Shared Components (Read-Only)       │   │
│  │ Device Role     │    │  - aws.edgeml.dda.LocalServer.arm64  │   │
│  │ (s3:GetObject)  │    │  - aws.edgeml.dda.LocalServer.amd64  │   │
│  └─────────────────┘    │  (Tagged: dda-portal:shared-component)│   │
│                         └──────────────────────────────────────┘   │
│                                          │                          │
│                                          ▼                          │
│                         ┌──────────────────────────────────────┐   │
│                         │  Greengrass Core Devices             │   │
│                         │  (Deploy shared + model components)  │   │
│                         └──────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## How It Works

### 1. Component Storage (Portal Account)

The portal account stores:
- **Component artifacts** in the `dda-portal-artifacts-{account}-{region}` S3 bucket
- **Component recipes** as Greengrass components

### 2. Usecase Onboarding

When a new usecase is onboarded via the portal:

1. **Bucket Policy Setup**: The portal's artifacts bucket allows cross-account read access for Greengrass device roles
2. **Component Creation**: The portal creates the `dda-LocalServer` components in the usecase account
3. **Read-Only Tags**: Components are tagged with `dda-portal:shared-component=true` and `dda-portal:read-only=true`
4. **IAM Policy**: The usecase account's Greengrass device role gets `s3:GetObject` permission for the portal's artifacts bucket

### 3. Component Protection

Shared components are protected from modification in usecase accounts:

```typescript
// IAM Deny policy in usecase-account-stack.ts
{
  "Effect": "Deny",
  "Action": [
    "greengrass:DeleteComponent",
    "greengrass:CreateComponentVersion"
  ],
  "Resource": "arn:aws:greengrass:*:*:components:aws.edgeml.dda.LocalServer*",
  "Condition": {
    "StringEquals": {
      "aws:ResourceTag/dda-portal:shared-component": "true"
    }
  }
}
```

## Shared Components

| Component Name | Platform | Description |
|---------------|----------|-------------|
| `aws.edgeml.dda.LocalServer.arm64` | ARM64 (aarch64) | DDA LocalServer for Jetson, Raspberry Pi |
| `aws.edgeml.dda.LocalServer.amd64` | AMD64 (x86_64) | DDA LocalServer for x86 devices |

## API Endpoints

### Provision Shared Components
```
POST /api/v1/shared-components/provision
{
  "usecase_id": "uuid",
  "component_version": "1.0.0"  // optional, defaults to latest
}
```

### List Available Shared Components
```
GET /api/v1/shared-components/available
```

### List Shared Components for Usecase
```
GET /api/v1/shared-components?usecase_id={usecase_id}
```

## Deployment Flow

1. User creates a deployment in the portal
2. Portal selects model component + dda-LocalServer dependency
3. Deployment is created in usecase account via cross-account role
4. Greengrass device downloads:
   - Model artifacts from usecase account's S3
   - dda-LocalServer artifacts from portal account's S3

## Updating Shared Components

To update the dda-LocalServer component:

1. Upload new artifacts to portal's S3 bucket
2. Create new component version in portal account
3. Run migration script to update all usecase accounts:

```bash
python backend/scripts/update_shared_components.py --version 1.1.0
```

## Future Enhancement: Automatic Version Updates

**Status: Not Yet Implemented**

Currently, when a new version of dda-LocalServer is published in the portal account, usecase accounts must be manually updated. A future enhancement should implement:

### Proposed API Endpoint
```
POST /shared-components/update-all
{
  "component_name": "aws.edgeml.dda.LocalServer.arm64",
  "new_version": "1.1.0",
  "trigger_redeployment": false  // optional: trigger device redeployments
}
```

### Proposed Functionality
1. Query `SharedComponentsTable` for all usecases with the component
2. For each usecase:
   - Assume cross-account role
   - Create new component version in usecase account
   - Update `SharedComponentsTable` with new version
3. Optionally trigger redeployments to devices running the old version
4. Return summary of updated usecases

### Alternative: Version Check UI
- Add a "Check for Updates" button in the portal UI
- Show which usecases have outdated shared components
- Allow bulk or individual updates

---

## Troubleshooting

### Component Not Found in Usecase Account
- Check if usecase was onboarded after shared components feature was added
- Run: `POST /api/v1/shared-components/provision` to provision manually

### Artifact Download Failed
- Verify portal artifacts bucket policy allows cross-account access
- Check Greengrass device role has `s3:GetObject` permission
- Verify artifact exists in portal bucket: `s3://dda-portal-artifacts-{account}-{region}/shared-components/`

### Cannot Delete Shared Component
- This is expected behavior - shared components are read-only
- Contact portal admin if component needs to be removed
