# Design Document

## Overview

The Defect Detection Application (DDA) Admin Portal is a cloud-native, multi-tenant web application that provides centralized management of defect detection workloads across multiple AWS accounts. The system follows a hub-and-spoke architecture where a central Portal Account orchestrates operations across multiple UseCase Accounts containing edge devices, datasets, and Ground Truth labeling jobs.

### Key Design Principles

1. **Separation of Concerns**: Portal Account handles orchestration and UI; UseCase Accounts own data and devices
2. **Least Privilege**: Cross-account access uses STS AssumeRole with ExternalID and minimal IAM permissions
3. **Serverless-First**: Leverage AWS managed services (Lambda, Step Functions, API Gateway) for scalability and reduced operational overhead
4. **Event-Driven**: Use EventBridge and SNS for asynchronous workflows and notifications
5. **Multi-Tenancy**: Use case isolation enforced at data, API, and UI layers
6. **Auditability**: Comprehensive logging of all user actions and system events

## Architecture

### High-Level System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Customer Identity Provider                   │
│              (Okta, Azure AD, Auth0, Google, etc.)              │
│                      (SAML 2.0 / OIDC)                          │
└────────────────────────────────┬────────────────────────────────┘
                                 │ Authentication
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Portal Account                           │
│                                                                  │
│  ┌──────────────┐    ┌─────────────────────────────────────┐  │
│  │   CloudFront │───▶│  S3 (Static Web Hosting)            │  │
│  │   + WAF      │    │  React SPA                          │  │
│  └──────────────┘    └─────────────────────────────────────┘  │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │         Authentication Layer (Flexible Options)           │  │
│  │  Option 1: Cognito User Pool (federated with IdP)        │  │
│  │  Option 2: Direct IdP integration (custom authorizer)    │  │
│  │  Option 3: API Gateway JWT authorizer (IdP tokens)       │  │
│  └──────────────────────────────────────────────────────────┘  │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              API Gateway (REST + WebSocket)               │  │
│  │         (JWT Authorizer - validates IdP tokens)           │  │
│  └────────┬──────────────────────────────────┬──────────────┘  │
│           │                                   │                 │
│           ▼                                   ▼                 │
│  ┌─────────────────┐              ┌──────────────────────┐    │
│  │  Lambda Functions│              │  Step Functions      │    │
│  │  - API Handlers  │              │  - Training Pipeline │    │
│  │  - Orchestration │              │  - Compile Pipeline  │    │
│  │  - Auth/RBAC     │              │  - Publish Pipeline  │    │
│  └────────┬─────────┘              └──────────┬───────────┘    │
│           │                                   │                 │
│           ▼                                   ▼                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    DynamoDB Tables                        │  │
│  │  - UseCases  - Devices  - LabelingJobs                   │  │
│  │  - TrainingJobs  - Models  - Deployments                 │  │
│  │  - AuditLog  - UserRoles                                 │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              CloudWatch + X-Ray + CloudTrail              │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │         Cross-Account Role Assumption (STS)               │  │
│  └────────┬──────────────────────────────────┬──────────────┘  │
└───────────┼──────────────────────────────────┼─────────────────┘
            │                                   │
            ▼                                   ▼
┌───────────────────────────────────────────────────────────────┐
│                    UseCase Account(s)                          │
│                                                                │
│  ┌──────────────────────┐    ┌──────────────────────┐        │
│  │  S3 Buckets          │    │  Ground Truth        │        │
│  │  - Raw Images        │───▶│  - Labeling Jobs     │        │
│  │  - Labeled Data      │◀───│  - Manifests         │        │
│  │  - Model Artifacts   │    │  - Output Data       │        │
│  └──────────────────────┘    └──────────────────────┘        │
│                                                                │
│  ┌──────────────────────┐                                     │
│  │  SageMaker           │                                     │
│  │  - Training Jobs     │                                     │
│  │  - Compilation Jobs  │                                     │
│  └──────────────────────┘                                     │
                                  │                            │
                                  │  ┌──────────────────────┐ │
                                  │  │  Greengrass          │ │
                                  │  │  - Component Registry│ │
                                  │  │  - Deployments       │ │
                                  │  └──────────────────────┘ │
                                  │                            │
                                  │  ┌──────────────────────┐ │
                                  │  │  IoT Core            │ │
                                  │  │  - Thing Registry    │ │
                                  │  │  - Thing Shadows     │ │
                                  │  │  - Jobs              │ │
                                  │  └──────────────────────┘ │
                                  └────────────┬───────────────┘
                                               │
                                               ▼
                                  ┌────────────────────────────┐
                                  │    Edge Devices            │
                                  │                            │
                                  │  ┌──────────────────────┐ │
                                  │  │  Greengrass Core     │ │
                                  │  └──────────────────────┘ │
                                  │  ┌──────────────────────┐ │
                                  │  │  DDA Application     │ │
                                  │  └──────────────────────┘ │
                                  │  ┌──────────────────────┐ │
                                  │  │  Management Agent    │ │
                                  │  │  - File Browser      │ │
                                  │  │  - Log Streamer      │ │
                                  │  │  - Control API       │ │
                                  │  └──────────────────────┘ │
                                  └────────────────────────────┘
```

## Components and Interfaces

### 1. Frontend (React SPA)

**Technology Stack:**
- React 18+ with TypeScript
- AWS Amplify for authentication integration
- CloudScape Design System (AWS UI components)
- React Query for data fetching and caching
- WebSocket client for real-time updates

**Key Pages:**
- Login/Authentication
- Dashboard
- Use Case Management
- Labeling Workflow
- Dataset Management (manifest discovery and validation)
- Training & Compilation
- Model Registry
- Deployments
- Device Management
- Settings & Admin
- Audit Logs

**State Management:**
- React Context for global state (user, current use case)
- React Query for server state
- Local storage for user preferences


### 2. API Gateway

**Configuration:**
- REST API for CRUD operations
- WebSocket API for real-time updates (job progress, device status)
- JWT Authorizer using Cognito tokens
- Request validation using JSON Schema
- Rate limiting and throttling per user/use case
- CORS configuration for CloudFront origin

**API Routes Structure:**
```
/api/v1/
  /auth
    GET  /me                    # Current user info
    POST /refresh               # Refresh token
  /usecases
    GET    /                    # List use cases
    POST   /                    # Create use case
    GET    /{id}                # Get use case details
    PUT    /{id}                # Update use case
    DELETE /{id}                # Delete use case
  /labeling
    GET    /                    # List labeling jobs
    POST   /                    # Create labeling job
    GET    /{id}                # Get job status
    GET    /{id}/manifest       # Download manifest
  /datasets
    GET    /manifests           # List available manifest files
    POST   /validate-manifest   # Validate manifest format
  /training
    GET    /                    # List training jobs
    POST   /                    # Start training job
    GET    /{id}                # Get training status
    GET    /{id}/logs           # Get training logs
    POST   /{id}/compile        # Trigger compilation
  /compile
    GET    /{id}                # Get compile status
    GET    /{id}/logs           # Get compile logs
  /models
    GET    /                    # List models
    GET    /{id}                # Get model details
    PUT    /{id}/stage          # Promote model stage
  /components
    POST   /publish             # Publish component
    GET    /{arn}               # Get component details
  /deployments
    GET    /                    # List deployments
    POST   /                    # Create deployment
    GET    /{id}                # Get deployment status
    POST   /{id}/rollback       # Rollback deployment
  /devices
    GET    /                    # List devices
    GET    /{id}                # Get device details
    POST   /{id}/restart        # Restart Greengrass
    POST   /{id}/reboot         # Reboot device
    GET    /{id}/browse         # Browse files
    GET    /{id}/file           # Download file
    GET    /{id}/logs           # Tail logs
    PUT    /{id}/config         # Update config
  /audit
    GET    /                    # Query audit logs
  /settings
    GET    /targets             # Get compilation targets config
    PUT    /targets             # Update compilation targets
```

### 3. Lambda Functions

**Function Organization:**
- One Lambda per API route group (microservice pattern)
- Shared layer for common utilities (auth, DynamoDB, cross-account)
- Environment variables for configuration

**Key Functions:**

**AuthHandler:**
- Validates JWT tokens
- Enforces RBAC based on user roles and use case assignments
- Checks PortalAdmin super user privileges

**UseCaseHandler:**
- CRUD operations for use cases
- Validates cross-account role accessibility
- Manages user-to-use-case assignments

**LabelingHandler:**
- Lists S3 datasets via cross-account access
- Creates Ground Truth jobs in UseCase Account
- Generates manifest files in UseCase Account S3
- Polls job status
- Passes Ground Truth execution role for job creation

**DatasetHandler:**
- Lists available manifest files from S3 (Ground Truth outputs and pre-labeled)
- Validates manifest file format and content
- Provides manifest metadata (image count, label categories, format)
- Supports discovery of manifests from multiple sources

**TrainingHandler:**
- Starts SageMaker training jobs
- Triggers Step Functions workflow
- Fetches CloudWatch logs
- Updates Model Registry
- Supports multiple dataset source types (Ground Truth jobs, pre-labeled manifests, manual URIs)

**CompileHandler:**
- Starts SageMaker Neo compilation
- Packages compiled artifacts
- Creates Greengrass component structure

**ComponentHandler:**
- Publishes components to Greengrass registry
- Manages component versions
- Cross-account component publication

**DeploymentHandler:**
- Creates Greengrass deployments
- Monitors deployment status
- Handles rollbacks

**DeviceHandler:**
- Queries IoT Thing registry
- Reads Thing Shadows
- Creates IoT Jobs for device control
- Proxies requests to Management Agent

**AuditHandler:**
- Writes audit events to DynamoDB
- Queries audit logs with filtering

### 4. Step Functions Workflows

**Training-to-Deployment Pipeline:**

```
StartTrainingWorkflow
  ├─ AssumeUseCaseRole
  ├─ ValidateDataset
  ├─ StartSageMakerTraining
  ├─ WaitForTrainingCompletion (EventBridge integration)
  ├─ OnTrainingSuccess
  │   ├─ UpdateModelRegistry
  │   ├─ ParallelCompilation (for each target)
  │   │   ├─ StartCompilation
  │   │   ├─ WaitForCompilationCompletion
  │   │   └─ PackageComponent
  │   ├─ PublishComponents (for each compiled artifact)
  │   └─ SendSuccessNotification
  └─ OnTrainingFailure
      └─ SendFailureAlert
```

**Labeling Workflow:**

```
CreateLabelingJob
  ├─ AssumeUseCaseRole
  ├─ ListS3Images
  ├─ GenerateManifest
  ├─ UploadManifestToUseCaseS3
  ├─ CreateGroundTruthJob (with GroundTruthExecutionRole)
  └─ UpdateJobMetadata
```

**Dataset Selection Workflow:**

The Portal supports three methods for selecting training datasets:

1. **From Ground Truth Job (Recommended)**
```
User selects completed labeling job
  ├─ Portal fetches job metadata
  ├─ Auto-populates manifest S3 URI
  ├─ Displays job info (date, image count, labels)
  └─ Optional: Auto-split train/validation
```

2. **From Pre-Labeled Manifest**
```
User browses available manifests
  ├─ Portal scans S3 for .manifest files
  ├─ Displays manifest metadata
  ├─ User selects manifest
  └─ Portal validates manifest format
```

3. **Manual S3 URI**
```
User enters S3 URI directly
  ├─ Portal validates URI format
  ├─ Optional: Validate manifest content
  └─ User confirms selection
```

**Manifest Discovery:**
```
ListManifests
  ├─ AssumeUseCaseRole
  ├─ Scan S3 prefixes (manifests/, labeled/, training-data/)
  ├─ For each .manifest file:
  │   ├─ Parse manifest to count images
  │   ├─ Extract label categories
  │   ├─ Detect source (Ground Truth vs external)
  │   └─ Collect metadata
  └─ Return sorted list with metadata
```

**Manifest Validation:**
```
ValidateManifest
  ├─ AssumeUseCaseRole
  ├─ Download manifest from S3
  ├─ Parse JSON lines
  ├─ Detect format (object detection, classification, segmentation)
  ├─ Validate required fields
  ├─ Extract label categories
  ├─ Check for issues (missing images, invalid format)
  └─ Return validation result with metadata
```

### 5. DynamoDB Tables

**UseCases Table:**
```
PK: usecase_id (String)
Attributes:
  - name (String)
  - account_id (String)
  - s3_bucket (String)
  - s3_prefix (String)
  - cross_account_role_arn (String)
  - external_id (String)
  - owner (String)
  - cost_center (String)
  - default_device_group (String)
  - created_at (Number - timestamp)
  - updated_at (Number - timestamp)
  - tags (Map)

GSI: owner-index (owner)
```

**UserRoles Table:**
```
PK: user_id (String)
SK: usecase_id (String)
Attributes:
  - role (String) # PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer
  - assigned_at (Number - timestamp)
  - assigned_by (String)

GSI: usecase-users-index (usecase_id, user_id)
```

**Devices Table:**
```
PK: device_id (String)
Attributes:
  - usecase_id (String)
  - thing_name (String)
  - status (String) # online, offline, error
  - last_heartbeat (Number - timestamp)
  - components (List<Map>) # [{name, version, status}]
  - storage_used (Number)
  - storage_total (Number)
  - camera_status (String)
  - greengrass_version (String)
  - metadata (Map)
  - created_at (Number - timestamp)
  - updated_at (Number - timestamp)

GSI: usecase-devices-index (usecase_id, last_heartbeat)
GSI: status-index (status, last_heartbeat)
```


**LabelingJobs Table:**
```
PK: job_id (String)
Attributes:
  - usecase_id (String)
  - name (String)
  - manifest_s3 (String)
  - output_s3 (String)
  - task_type (String) # ObjectDetection, Classification, Segmentation
  - images_count (Number)
  - labeled_count (Number)
  - status (String) # pending, in_progress, completed, failed
  - progress_percent (Number)
  - ground_truth_job_arn (String)
  - workforce_type (String)
  - created_by (String)
  - created_at (Number - timestamp)
  - completed_at (Number - timestamp)

GSI: usecase-jobs-index (usecase_id, created_at)
GSI: status-index (status, created_at)
```

**TrainingJobs Table:**
```
PK: training_id (String)
Attributes:
  - usecase_id (String)
  - model_name (String)
  - model_version (String)
  - dataset_manifest_s3 (String)
  - algorithm_uri (String)
  - hyperparameters (Map)
  - instance_type (String)
  - training_job_arn (String)
  - status (String) # pending, in_progress, completed, failed
  - metrics (Map) # accuracy, loss, etc.
  - artifact_s3 (String)
  - compiled_targets (List<String>)
  - component_versions (Map) # {target: component_arn}
  - created_by (String)
  - created_at (Number - timestamp)
  - completed_at (Number - timestamp)
  - logs_url (String)

GSI: usecase-training-index (usecase_id, created_at)
GSI: model-index (model_name, model_version)
```

**Models Table:**
```
PK: model_id (String)
Attributes:
  - usecase_id (String)
  - name (String)
  - version (String)
  - stage (String) # candidate, staging, production
  - training_job_id (String)
  - dataset_manifest_id (String)
  - metrics (Map)
  - component_arns (Map) # {target: arn}
  - deployed_devices (List<String>)
  - created_by (String)
  - created_at (Number - timestamp)
  - promoted_at (Number - timestamp)
  - promoted_by (String)

GSI: usecase-models-index (usecase_id, created_at)
GSI: stage-index (stage, usecase_id)
```

**Deployments Table:**
```
PK: deployment_id (String)
Attributes:
  - usecase_id (String)
  - component_arn (String)
  - component_version (String)
  - target_devices (List<String>)
  - target_groups (List<String>)
  - rollout_strategy (String) # all-at-once, canary, percentage
  - rollout_config (Map)
  - status (String) # pending, in_progress, completed, failed, rolled_back
  - device_statuses (Map) # {device_id: status}
  - greengrass_deployment_id (String)
  - created_by (String)
  - created_at (Number - timestamp)
  - completed_at (Number - timestamp)

GSI: usecase-deployments-index (usecase_id, created_at)
GSI: status-index (status, created_at)
```

**AuditLog Table:**
```
PK: event_id (String)
SK: timestamp (Number)
Attributes:
  - user_id (String)
  - usecase_id (String)
  - action (String) # create_job, deploy, publish, etc.
  - resource_type (String)
  - resource_id (String)
  - result (String) # success, failure
  - details (Map)
  - ip_address (String)
  - user_agent (String)
  - is_super_user (Boolean)

GSI: user-actions-index (user_id, timestamp)
GSI: usecase-actions-index (usecase_id, timestamp)
GSI: action-type-index (action, timestamp)
```

**Settings Table:**
```
PK: setting_key (String)
Attributes:
  - value (Map or String)
  - updated_by (String)
  - updated_at (Number - timestamp)

Example keys:
  - compilation_targets: {targets: [{name, platform, arch}]}
  - alert_config: {sns_topic_arn, email_list}
  - labeling_account: {account_id, role_arn}
```

### 6. Cross-Account IAM Design

**Portal Account - PortalRole:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::*:role/PortalAccessRole"
    }
  ]
}
```

**UseCase Account - PortalAccessRole:**

Trust Policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<PORTAL_ACCOUNT_ID>:role/PortalRole"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "<UNIQUE_EXTERNAL_ID_PER_USECASE>"
        }
      }
    }
  ]
}
```

Permissions Policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::usecase-bucket",
        "arn:aws:s3:::usecase-bucket/*"
      ]
    },
    {
      "Sid": "SageMakerAccess",
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateTrainingJob",
        "sagemaker:DescribeTrainingJob",
        "sagemaker:CreateCompilationJob",
        "sagemaker:DescribeCompilationJob",
        "sagemaker:ListTrainingJobs"
      ],
      "Resource": "*"
    },
    {
      "Sid": "GreengrassAccess",
      "Effect": "Allow",
      "Action": [
        "greengrass:CreateComponentVersion",
        "greengrass:DescribeComponent",
        "greengrass:ListComponents",
        "greengrass:CreateDeployment",
        "greengrass:GetDeployment",
        "greengrass:ListDeployments",
        "greengrass:CancelDeployment"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IoTAccess",
      "Effect": "Allow",
      "Action": [
        "iot:DescribeThing",
        "iot:ListThings",
        "iot:UpdateThingShadow",
        "iot:GetThingShadow",
        "iot:CreateJob",
        "iot:DescribeJob",
        "iot:CancelJob"
      ],
      "Resource": "*"
    },
    {
      "Sid": "KMSAccess",
      "Effect": "Allow",
      "Action": [
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "arn:aws:kms:<REGION>:<USECASE_ACCOUNT_ID>:key/<KEY_ID>"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateLabelingJob",
        "sagemaker:DescribeLabelingJob",
        "sagemaker:ListLabelingJobs",
        "sagemaker:StopLabelingJob"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<USECASE_ACCOUNT_ID>:role/GroundTruthExecutionRole",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "sagemaker.amazonaws.com"
        }
      }
    }
  ]
}
```

**UseCase Account - GroundTruthExecutionRole:**

This role is used by Ground Truth to access S3 data within the UseCase Account.

Trust Policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "sagemaker.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::<USECASE_BUCKET>",
        "arn:aws:s3:::<USECASE_BUCKET>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
    }
  ]
}
```


### 7. Authentication & Authorization

**Authentication Options:**

The Portal supports flexible authentication to integrate with customer Identity Providers (IdPs):

**Option 1: Cognito Federation (Recommended for AWS-native)**
```
Customer IdP (Okta/Azure AD/etc.)
  ↓ SAML 2.0 / OIDC
Cognito User Pool
  ↓ JWT tokens
API Gateway JWT Authorizer
```

**Benefits:**
- AWS-managed token lifecycle
- Built-in token refresh
- User pool for backup authentication
- Attribute mapping from IdP
- MFA support

**Configuration:**
```typescript
// Cognito User Pool with IdP federation
const userPool = new cognito.UserPool(this, 'UserPool', {
  userPoolName: 'EdgeCVPortal',
  selfSignUpEnabled: false,
  signInAliases: { email: true },
});

// Add SAML IdP
const samlProvider = new cognito.UserPoolIdentityProviderSaml(this, 'SamlProvider', {
  userPool,
  name: 'CustomerIdP',
  metadata: cognito.UserPoolIdentityProviderSamlMetadata.url(
    'https://customer-idp.com/metadata.xml'
  ),
  attributeMapping: {
    email: cognito.ProviderAttribute.SAML_EMAIL,
    givenName: cognito.ProviderAttribute.SAML_GIVEN_NAME,
    familyName: cognito.ProviderAttribute.SAML_FAMILY_NAME,
    custom: {
      groups: cognito.ProviderAttribute.other('groups'),
      role: cognito.ProviderAttribute.other('role'),
    },
  },
});
```

**Option 2: Direct IdP Integration (Custom Authorizer)**
```
Customer IdP (Okta/Azure AD/etc.)
  ↓ JWT tokens
API Gateway Custom Authorizer Lambda
  ↓ Validates token with IdP
Lambda functions
```

**Benefits:**
- No Cognito dependency
- Direct token validation
- Full control over auth logic
- Support any IdP

**Custom Authorizer Lambda:**
```python
import jwt
import requests
from functools import lru_cache

@lru_cache(maxsize=1)
def get_jwks(jwks_url):
    """Cache JWKS keys from IdP."""
    response = requests.get(jwks_url)
    return response.json()

def handler(event, context):
    """Validate JWT from customer IdP."""
    token = event['authorizationToken'].replace('Bearer ', '')
    
    # Get JWKS from IdP
    jwks_url = os.environ['IDP_JWKS_URL']
    jwks = get_jwks(jwks_url)
    
    try:
        # Validate token
        header = jwt.get_unverified_header(token)
        key = find_key(jwks, header['kid'])
        
        claims = jwt.decode(
            token,
            key,
            algorithms=['RS256'],
            audience=os.environ['IDP_AUDIENCE'],
            issuer=os.environ['IDP_ISSUER']
        )
        
        # Extract user info
        user_id = claims.get('sub')
        email = claims.get('email')
        groups = claims.get('groups', [])
        
        # Generate IAM policy
        return {
            'principalId': user_id,
            'policyDocument': {
                'Version': '2012-10-17',
                'Statement': [{
                    'Action': 'execute-api:Invoke',
                    'Effect': 'Allow',
                    'Resource': event['methodArn']
                }]
            },
            'context': {
                'userId': user_id,
                'email': email,
                'groups': ','.join(groups)
            }
        }
    except Exception as e:
        raise Exception('Unauthorized')
```

**Option 3: API Gateway JWT Authorizer (Simplest)**
```
Customer IdP (Okta/Azure AD/etc.)
  ↓ JWT tokens
API Gateway JWT Authorizer (native)
  ↓ Validates against IdP JWKS
Lambda functions
```

**Benefits:**
- No custom code needed
- AWS-managed validation
- High performance
- Automatic JWKS caching

**Configuration:**
```typescript
const authorizer = new apigateway.HttpJwtAuthorizer('JwtAuthorizer', {
  jwtAudience: ['https://portal.customer.com'],
  jwtIssuer: 'https://customer-idp.com',
  identitySource: ['$request.header.Authorization'],
});

api.addRoutes({
  path: '/api/{proxy+}',
  authorizer,
  integration: new HttpLambdaIntegration('ApiIntegration', handler),
});
```

**Supported Identity Providers:**
- Okta (SAML 2.0, OIDC)
- Azure Active Directory (SAML 2.0, OIDC)
- Google Workspace (OIDC)
- Auth0 (OIDC)
- Ping Identity (SAML 2.0, OIDC)
- OneLogin (SAML 2.0, OIDC)
- Any SAML 2.0 or OIDC compliant IdP

**Frontend Integration:**

```typescript
// config.ts - Customer-specific configuration
export interface AuthConfig {
  type: 'cognito' | 'oidc' | 'saml';
  
  // For Cognito
  cognitoUserPoolId?: string;
  cognitoClientId?: string;
  cognitoRegion?: string;
  
  // For direct OIDC
  oidcIssuer?: string;
  oidcClientId?: string;
  oidcRedirectUri?: string;
  
  // For SAML (via Cognito or direct)
  samlLoginUrl?: string;
}

// AuthContext.tsx - Flexible auth implementation
const authConfig = getConfig().auth;

if (authConfig.type === 'cognito') {
  // Use AWS Amplify with Cognito
  Amplify.configure({
    Auth: {
      userPoolId: authConfig.cognitoUserPoolId,
      userPoolWebClientId: authConfig.cognitoClientId,
      region: authConfig.cognitoRegion,
    },
  });
} else if (authConfig.type === 'oidc') {
  // Use OIDC client library
  const oidcClient = new OidcClient({
    authority: authConfig.oidcIssuer,
    client_id: authConfig.oidcClientId,
    redirect_uri: authConfig.oidcRedirectUri,
  });
}
```

**Role Mapping:**

Map IdP groups/roles to Portal roles:

```json
{
  "roleMapping": {
    "idpGroups": {
      "portal-admins": "PortalAdmin",
      "cv-data-scientists": "DataScientist",
      "cv-operators": "Operator",
      "cv-viewers": "Viewer"
    }
  }
}
```

### 8. Device Management Agent

**Purpose:**
Lightweight service running on edge devices to enable remote management capabilities that Greengrass doesn't natively provide.

**Technology:**
- Python 3.9+ or Go
- Runs as systemd service
- Communicates via IoT Core MQTT or Jobs

**Capabilities:**

1. **File Browser API:**
   - List files in configured directories
   - Return file metadata (size, mtime, permissions)
   - Stream file downloads

2. **Log Streamer:**
   - Tail application logs
   - Stream logs over MQTT or WebSocket
   - Support filtering by keyword/time

3. **Control API:**
   - Restart Greengrass service
   - Reboot device (with confirmation)
   - Run diagnostic commands
   - Export images to S3

4. **Security:**
   - Certificate-based authentication using device certificates
   - Whitelist of allowed directories for browsing
   - Command authorization based on IoT policies
   - Rate limiting to prevent abuse

**Communication Pattern:**

```
Portal → IoT Core (CreateJob) → Device Agent → Execute Command → Publish Result → IoT Core → Portal
```

**Agent Configuration File (`/etc/dda-agent/config.json`):**
```json
{
  "allowed_directories": [
    "/var/log/greengrass",
    "/var/log/dda",
    "/opt/dda/captured-images"
  ],
  "mqtt_topic_prefix": "dda/devices/{device_id}/agent",
  "log_files": [
    "/var/log/dda/application.log",
    "/var/log/greengrass/greengrass.log"
  ],
  "max_file_size_mb": 100,
  "rate_limit_requests_per_minute": 30
}
```

### 8. Authentication & Authorization Flow

**Login Flow:**

```
1. User navigates to Portal URL
2. CloudFront serves React SPA
3. User clicks "Login with SSO"
4. React redirects to Cognito Hosted UI
5. Cognito redirects to Company SSO (SAML/OIDC)
6. User authenticates with company credentials
7. SSO returns SAML assertion/OIDC tokens to Cognito
8. Cognito issues JWT tokens (ID token, Access token, Refresh token)
9. React stores tokens in memory/session storage
10. React fetches user profile and role mappings from /api/auth/me
11. Portal displays dashboard with use case selector
```

**Authorization Flow:**

```
1. React makes API request with JWT in Authorization header
2. API Gateway JWT Authorizer validates token with Cognito
3. Lambda receives decoded JWT with user claims
4. Lambda queries UserRoles table for user's use cases and roles
5. Lambda checks if user has PortalAdmin role (super user)
6. If PortalAdmin: grant access to all use cases
7. If not: check if user has access to requested use case
8. Enforce action permissions based on role:
   - PortalAdmin: all actions
   - UseCaseAdmin: all actions within assigned use cases
   - DataScientist: labeling, training, model registry
   - Operator: deployments, device management
   - Viewer: read-only access
9. Log action to AuditLog table
10. Execute business logic
11. Return response
```

**SSO Group Mapping:**

Cognito User Pool configured with SAML/OIDC attribute mapping:
```
SSO Group → Cognito Custom Attribute → Portal Role
"portal-admins" → custom:role → "PortalAdmin"
"cv-data-scientists" → custom:role → "DataScientist"
"cv-operators" → custom:role → "Operator"
```

## Data Models

### UseCase Model
```typescript
interface UseCase {
  usecaseId: string;
  name: string;
  accountId: string;
  s3Bucket: string;
  s3Prefix: string;
  crossAccountRoleArn: string;
  externalId: string;
  owner: string;
  costCenter: string;
  defaultDeviceGroup: string;
  createdAt: number;
  updatedAt: number;
  tags: Record<string, string>;
}
```

### Device Model
```typescript
interface Device {
  deviceId: string;
  usecaseId: string;
  thingName: string;
  status: 'online' | 'offline' | 'error';
  lastHeartbeat: number;
  components: ComponentInfo[];
  storageUsed: number;
  storageTotal: number;
  cameraStatus: string;
  greengrassVersion: string;
  metadata: Record<string, any>;
  createdAt: number;
  updatedAt: number;
}

interface ComponentInfo {
  name: string;
  version: string;
  status: string;
}
```

### LabelingJob Model
```typescript
interface LabelingJob {
  jobId: string;
  usecaseId: string;
  name: string;
  manifestS3: string;
  outputS3: string;
  taskType: 'ObjectDetection' | 'Classification' | 'Segmentation';
  imagesCount: number;
  labeledCount: number;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  progressPercent: number;
  groundTruthJobArn: string;
  workforceType: string;
  createdBy: string;
  createdAt: number;
  completedAt?: number;
}
```

### PreLabeledDataset Model
```typescript
interface PreLabeledDataset {
  datasetId: string;
  usecaseId: string;
  name: string;
  description: string;
  manifestS3Uri: string;
  taskType: 'classification' | 'segmentation' | 'detection' | 'unknown';
  labelAttribute: string;
  imageCount: number;
  labelStats: Record<string, number>; // e.g., {"anomaly": 32, "normal": 31}
  createdBy: string;
  createdAt: number;
  updatedAt: number;
}
```

**Pre-Labeled Dataset Workflow:**

The Portal supports using existing labeled datasets (manifest files) without creating new Ground Truth labeling jobs. This is useful when:
- Users already have labeled data from previous projects
- Data was labeled using external tools
- Reusing datasets across multiple training experiments

**Supported Manifest Formats:**
- SageMaker Ground Truth output format (JSONL)
- Required fields: `source-ref`, label fields, metadata
- Task types: classification, segmentation, object detection

**Example Classification Manifest Entry:**
```json
{
  "source-ref": "s3://bucket/images/image001.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {
    "confidence": 0.95,
    "job-name": "labeling-job-001",
    "class-name": "anomaly",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:00.000Z",
    "type": "groundtruth/image-classification"
  }
}
```

**Validation Rules:**
1. File must be valid JSONL (one JSON object per line)
2. Each entry must have `source-ref` field
3. Label fields must follow Ground Truth naming convention
4. Metadata fields should include task type information
5. S3 URIs in `source-ref` should be accessible

**Integration with Training:**
Pre-labeled datasets appear alongside Ground Truth jobs in the dataset selector during training job creation. Users can:
- Browse available pre-labeled datasets
- View dataset statistics (image count, label distribution)
- Select dataset as training input
- Use same workflow as Ground Truth outputs

### TrainingJob Model
```typescript
interface TrainingJob {
  trainingId: string;
  usecaseId: string;
  modelName: string;
  modelVersion: string;
  datasetManifestS3: string;
  algorithmUri: string;
  hyperparameters: Record<string, any>;
  instanceType: string;
  trainingJobArn: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  metrics: Record<string, number>;
  artifactS3: string;
  compiledTargets: string[];
  componentVersions: Record<string, string>;
  createdBy: string;
  createdAt: number;
  completedAt?: number;
  logsUrl: string;
}
```

### Model Model
```typescript
interface Model {
  modelId: string;
  usecaseId: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  trainingJobId: string;
  datasetManifestId: string;
  metrics: Record<string, number>;
  componentArns: Record<string, string>; // target -> arn
  deployedDevices: string[];
  createdBy: string;
  createdAt: number;
  promotedAt?: number;
  promotedBy?: string;
}
```

### Deployment Model
```typescript
interface Deployment {
  deploymentId: string;
  usecaseId: string;
  componentArn: string;
  componentVersion: string;
  targetDevices: string[];
  targetGroups: string[];
  rolloutStrategy: 'all-at-once' | 'canary' | 'percentage';
  rolloutConfig: RolloutConfig;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'rolled_back';
  deviceStatuses: Record<string, string>;
  greengrassDeploymentId: string;
  createdBy: string;
  createdAt: number;
  completedAt?: number;
}

interface RolloutConfig {
  canarySize?: number;
  canaryPercentage?: number;
  failureThreshold?: number;
}
```

## Error Handling

### Error Categories

1. **Authentication Errors (401):**
   - Invalid or expired JWT
   - SSO authentication failure
   - Response: Redirect to login

2. **Authorization Errors (403):**
   - Insufficient permissions for action
   - Use case access denied
   - Response: Display error message with required role

3. **Validation Errors (400):**
   - Invalid request parameters
   - Schema validation failure
   - Response: Return detailed validation errors

4. **Resource Not Found (404):**
   - Use case, device, job, or model not found
   - Response: Display "Resource not found" message

5. **Cross-Account Errors (500):**
   - STS AssumeRole failure
   - Invalid ExternalId
   - Response: Log error, alert admin, display generic error

6. **AWS Service Errors (500/503):**
   - SageMaker job failure
   - Greengrass API errors
   - IoT Core errors
   - Response: Log error, retry if transient, alert if persistent

7. **Timeout Errors (504):**
   - Long-running operations
   - Device not responding
   - Response: Display timeout message, suggest retry

### Error Response Format

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request parameters",
    "details": [
      {
        "field": "instanceType",
        "message": "Must be one of: ml.m5.large, ml.m5.xlarge"
      }
    ],
    "requestId": "abc-123-def"
  }
}
```

### Retry Strategy

- Exponential backoff for transient AWS service errors
- Max 3 retries for API calls
- Circuit breaker pattern for cross-account calls
- Dead letter queue for failed async operations


## Testing Strategy

### Unit Tests

**Backend (Lambda Functions):**
- Test each Lambda handler in isolation
- Mock AWS SDK calls (DynamoDB, SageMaker, Greengrass, IoT)
- Test RBAC logic with different user roles
- Test cross-account role assumption logic
- Coverage target: 80%+

**Frontend (React Components):**
- Test component rendering with different props
- Test user interactions (button clicks, form submissions)
- Test state management logic
- Mock API calls
- Coverage target: 70%+

### Integration Tests

**API Integration:**
- Test complete API flows (create use case → create job → deploy)
- Test authentication and authorization flows
- Test cross-account operations with test AWS accounts
- Test WebSocket real-time updates

**Step Functions:**
- Test training pipeline with mock SageMaker
- Test error handling and retry logic
- Test parallel compilation flows

### End-to-End Tests

**Critical User Journeys:**
1. Login → Select use case → Create labeling job → Monitor progress
2. Login → Start training → Compile → Publish component
3. Login → Create deployment → Monitor device status
4. Login → Browse device files → Download log

**Tools:**
- Cypress or Playwright for browser automation
- Test against staging environment
- Run on PR and before production deployment

### Load Testing

**Scenarios:**
- 100 concurrent users browsing devices
- 50 concurrent training jobs
- 1000 devices reporting status simultaneously

**Tools:**
- Artillery or k6 for load generation
- Monitor CloudWatch metrics during tests
- Identify bottlenecks and scaling limits

### Security Testing

**Checks:**
- OWASP Top 10 vulnerabilities
- JWT token validation
- Cross-account access controls
- SQL injection (if using RDS)
- XSS prevention in React components

**Tools:**
- OWASP ZAP for automated scanning
- Manual penetration testing
- AWS IAM Access Analyzer for policy validation

## Deployment Architecture

### Infrastructure as Code

**Technology:** AWS CDK (TypeScript) or Terraform

**Stacks:**

1. **Network Stack:**
   - VPC (if using private resources)
   - Security groups
   - VPC endpoints for AWS services

2. **Auth Stack:**
   - Cognito User Pool
   - Cognito Identity Pool
   - SSO integration configuration

3. **Storage Stack:**
   - DynamoDB tables with GSIs
   - S3 buckets for static hosting
   - S3 buckets for artifacts

4. **Compute Stack:**
   - Lambda functions with layers
   - API Gateway REST and WebSocket APIs
   - Step Functions state machines

5. **Monitoring Stack:**
   - CloudWatch dashboards
   - CloudWatch alarms
   - SNS topics for alerts
   - X-Ray tracing

6. **Frontend Stack:**
   - CloudFront distribution
   - S3 bucket for React build
   - WAF rules

### CI/CD Pipeline

**Stages:**

1. **Source:**
   - GitHub/GitLab repository
   - Branch protection rules
   - PR required for main branch

2. **Build:**
   - Install dependencies
   - Run linters (ESLint, Pylint)
   - Run unit tests
   - Build React app
   - Package Lambda functions

3. **Test:**
   - Run integration tests
   - Run security scans
   - Generate coverage reports

4. **Deploy to Staging:**
   - Deploy IaC to staging account
   - Run smoke tests
   - Run E2E tests

5. **Manual Approval:**
   - Review test results
   - Approve production deployment

6. **Deploy to Production:**
   - Blue/green deployment for Lambda
   - CloudFront cache invalidation
   - Monitor metrics and alarms

**Tools:**
- AWS CodePipeline + CodeBuild
- Or GitHub Actions
- Or GitLab CI/CD

### Multi-Region Considerations

**Primary Region:** us-east-1 (or customer preference)

**DR Strategy:**
- DynamoDB Global Tables for multi-region replication
- S3 Cross-Region Replication for artifacts
- CloudFront for global edge caching
- Route 53 health checks and failover

**Deployment:**
- Deploy Portal to primary region
- UseCase Accounts can be in any region
- Cross-region API calls handled by Portal

## Monitoring and Observability

### CloudWatch Dashboards

**Portal Health Dashboard:**
- API Gateway request count, latency, errors
- Lambda invocation count, duration, errors
- DynamoDB read/write capacity, throttles
- Step Functions execution count, success rate
- Cognito authentication success rate

**Jobs Dashboard:**
- Active training jobs by use case
- Active labeling jobs by use case
- Compilation jobs status
- Average job duration

**Device Dashboard:**
- Total devices by use case
- Online vs offline devices
- Devices with errors
- Deployment success rate

### CloudWatch Alarms

**Critical Alarms:**
- API Gateway 5xx error rate > 5%
- Lambda error rate > 10%
- DynamoDB throttling events
- Training job failure
- Device offline > 1 hour
- Deployment failure rate > 20%

**Warning Alarms:**
- API latency > 2 seconds
- Lambda duration approaching timeout
- DynamoDB capacity approaching limit

### Logging Strategy

**Log Levels:**
- ERROR: Failures requiring immediate attention
- WARN: Potential issues, degraded performance
- INFO: Important business events (job created, deployment started)
- DEBUG: Detailed diagnostic information

**Log Aggregation:**
- All Lambda logs to CloudWatch Logs
- Structured JSON logging for easy parsing
- Log retention: 30 days for INFO, 90 days for ERROR

**Log Analysis:**
- CloudWatch Logs Insights for querying
- Example queries:
  - Failed API calls by endpoint
  - Slow Lambda executions
  - Cross-account errors

### Distributed Tracing

**AWS X-Ray:**
- Trace API Gateway → Lambda → DynamoDB
- Trace Step Functions workflows
- Trace cross-account AWS SDK calls
- Identify bottlenecks and latency sources

### Metrics

**Custom Metrics:**
- Training jobs per use case per day
- Labeling throughput (images/hour)
- Deployment success rate by use case
- Device health score
- API usage by user/role

**Cost Metrics:**
- Training cost per use case
- Labeling cost per use case
- Storage cost per use case
- Total Portal operational cost

## Security Considerations

### Data Encryption

**At Rest:**
- DynamoDB: AWS managed keys or customer managed KMS keys
- S3: SSE-S3 or SSE-KMS
- Lambda environment variables: KMS encryption

**In Transit:**
- TLS 1.2+ for all API calls
- HTTPS only for CloudFront
- Certificate pinning for device agent

### Secrets Management

**AWS Secrets Manager:**
- Store cross-account role ExternalIds
- Store API keys for third-party services
- Rotate secrets automatically

**Lambda Access:**
- Grant Lambda execution role permission to read secrets
- Cache secrets in Lambda for performance

### Network Security

**API Gateway:**
- WAF rules to block common attacks
- Rate limiting per user/IP
- Request validation

**Lambda:**
- Deploy in VPC if accessing private resources
- Security groups for egress control
- VPC endpoints for AWS services

### Compliance

**Audit Requirements:**
- All user actions logged to AuditLog table
- CloudTrail enabled in all accounts
- Log retention per compliance requirements
- Regular access reviews

**Data Residency:**
- Ensure data stays in required regions
- Document cross-region data flows
- Implement data classification

## Scalability Considerations

### API Gateway

- Default limit: 10,000 requests/second
- Request throttling per user/use case
- Caching for read-heavy endpoints

### Lambda

- Concurrent execution limit: 1,000 (default)
- Request limit increase if needed
- Provisioned concurrency for critical functions

### DynamoDB

- On-demand capacity mode for variable workload
- Or provisioned capacity with auto-scaling
- GSIs for efficient queries
- DynamoDB Streams for change data capture

### Step Functions

- Standard workflows for long-running jobs
- Express workflows for high-throughput operations
- Limit: 1 million open executions

### S3

- Unlimited storage
- Request rate: 3,500 PUT/5,500 GET per prefix per second
- Use prefixes to distribute load

### Device Scale

- Support 1,000+ devices per use case
- IoT Core: 500,000 concurrent connections per account
- Thing Shadow updates: 100 per second per thing
- Jobs: 100 concurrent jobs per account

## Configuration Management

### Settings Configuration File

**Compilation Targets (`settings/compilation_targets.json`):**
```json
{
  "targets": [
    {
      "name": "Jetson Nano",
      "platform": "jetson",
      "arch": "aarch64",
      "compiler": "neo",
      "compiler_options": {
        "target_platform": "jetson_nano"
      }
    },
    {
      "name": "x86 CPU",
      "platform": "x86",
      "arch": "x86_64",
      "compiler": "neo",
      "compiler_options": {
        "target_platform": "ml_c5"
      }
    },
    {
      "name": "ARM64 CPU",
      "platform": "arm",
      "arch": "aarch64",
      "compiler": "neo",
      "compiler_options": {
        "target_platform": "ml_m6g"
      }
    }
  ]
}
```

**Admin UI for Configuration:**
- Settings page to edit compilation targets
- Validate JSON schema before saving
- Store in DynamoDB Settings table
- Audit changes

### Environment Variables

**Lambda Environment Variables:**
```
DYNAMODB_USECASES_TABLE=portal-usecases
DYNAMODB_DEVICES_TABLE=portal-devices
DYNAMODB_JOBS_TABLE=portal-jobs
DYNAMODB_MODELS_TABLE=portal-models
DYNAMODB_DEPLOYMENTS_TABLE=portal-deployments
DYNAMODB_AUDIT_TABLE=portal-audit
DYNAMODB_SETTINGS_TABLE=portal-settings
PORTAL_ROLE_ARN=arn:aws:iam::123456789012:role/PortalRole
LABELING_ACCOUNT_ID=987654321098
LABELING_ROLE_ARN=arn:aws:iam::987654321098:role/LabelingPortalRole
SNS_ALERT_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:portal-alerts
LOG_LEVEL=INFO
```

## Future Enhancements

### Phase 2 Features

1. **Advanced Analytics:**
   - Model performance tracking over time
   - Device health trends
   - Cost optimization recommendations

2. **Automated Retraining:**
   - Trigger retraining based on model drift
   - Scheduled retraining pipelines
   - A/B testing for model versions

3. **Enhanced Device Management:**
   - Remote shell access (secure)
   - Bulk device operations
   - Device grouping and tagging

4. **Collaboration Features:**
   - Comments on labeling jobs
   - Model review workflow
   - Shared dashboards

5. **Integration Ecosystem:**
   - Webhook notifications
   - REST API for external systems
   - Export data to data lakes

### Extensibility Points

**Plugin Architecture:**
- Custom training algorithms
- Custom compilation targets
- Custom device management actions
- Custom metrics and dashboards

**API Versioning:**
- Support multiple API versions
- Deprecation policy for old versions
- Backward compatibility guarantees

## Appendix

### Technology Stack Summary

**Frontend:**
- React 18+
- TypeScript
- CloudScape Design System
- React Query
- AWS Amplify

**Backend:**
- AWS Lambda (Python 3.11 or Node.js 18)
- API Gateway
- Step Functions
- DynamoDB
- S3
- Cognito

**Infrastructure:**
- AWS CDK or Terraform
- CloudFormation

**CI/CD:**
- AWS CodePipeline or GitHub Actions
- CodeBuild
- CodeDeploy

**Monitoring:**
- CloudWatch
- X-Ray
- CloudTrail

### Key AWS Services

- **Compute:** Lambda, Step Functions
- **Storage:** S3, DynamoDB
- **Networking:** API Gateway, CloudFront, Route 53
- **Security:** Cognito, IAM, KMS, Secrets Manager, WAF
- **ML:** SageMaker (Training, Neo, Ground Truth)
- **IoT:** IoT Core, Greengrass
- **Monitoring:** CloudWatch, X-Ray, CloudTrail, SNS

### Estimated Costs

**Monthly Operational Costs (approximate):**
- API Gateway: $3.50 per million requests
- Lambda: $0.20 per million requests (1GB memory)
- DynamoDB: $0.25 per GB storage + $1.25 per million read/write
- S3: $0.023 per GB storage + $0.005 per 1000 PUT
- CloudFront: $0.085 per GB transfer
- Cognito: $0.0055 per MAU (after free tier)

**Variable Costs:**
- SageMaker Training: $0.50-$5.00 per hour (instance dependent)
- SageMaker Ground Truth: $0.08 per object labeled
- IoT Core: $1.00 per million messages

**Example Monthly Cost (10 use cases, 100 devices, 50 training jobs):**
- Portal infrastructure: ~$200
- Training: ~$500
- Labeling: ~$400
- Total: ~$1,100 + data transfer

### Glossary of AWS Services

- **API Gateway:** Managed service for creating REST and WebSocket APIs
- **Lambda:** Serverless compute service
- **Step Functions:** Workflow orchestration service
- **DynamoDB:** NoSQL database service
- **S3:** Object storage service
- **Cognito:** User authentication and authorization service
- **SageMaker:** Machine learning platform
- **Ground Truth:** Data labeling service
- **Neo:** Model compilation service
- **Greengrass:** Edge runtime for IoT devices
- **IoT Core:** Managed IoT messaging service
- **CloudWatch:** Monitoring and logging service
- **X-Ray:** Distributed tracing service
- **CloudTrail:** API audit logging service
- **KMS:** Key management service
- **Secrets Manager:** Secrets storage and rotation service
- **WAF:** Web application firewall
- **CloudFront:** Content delivery network
- **Route 53:** DNS service
