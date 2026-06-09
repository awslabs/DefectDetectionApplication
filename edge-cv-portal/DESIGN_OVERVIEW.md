# DDA Portal - Design Overview

## Executive Summary

The Defect Detection Application (DDA) Portal is a multi-tenant web application for managing computer vision defect detection workloads on AWS edge devices. It provides end-to-end ML lifecycle management from data labeling to edge deployment.

## Problem Statement

Manufacturing teams need to:
- Train and deploy defect detection models to edge devices
- Manage multiple production lines (use cases) across AWS accounts
- Enable data scientists and operators with different access levels
- Monitor edge device fleet health and deployments

## Solution Architecture

### Multi-Account Model

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           PORTAL ACCOUNT                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │
│  │ CloudFront  │  │ API Gateway │  │  Cognito    │  │  DynamoDB   │   │
│  │ + React SPA │  │ + Lambda    │  │  (SSO/IdP)  │  │  (Metadata) │   │
│  └─────────────┘  └──────┬──────┘  └─────────────┘  └─────────────┘   │
│                          │ STS AssumeRole                              │
└──────────────────────────┼─────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ USECASE ACCT 1│  │ USECASE ACCT 2│  │ DATA ACCOUNT  │
│               │  │               │  │  (Optional)   │
│ • SageMaker   │  │ • SageMaker   │  │               │
│ • Greengrass  │  │ • Greengrass  │  │ • S3 Buckets  │
│ • IoT Core    │  │ • IoT Core    │  │ • Training    │
│ • S3 Data     │  │ • S3 Data     │  │   Data        │
│ • Edge Devices│  │ • Edge Devices│  │               │
└───────────────┘  └───────────────┘  └───────────────┘
```

### Account Responsibilities

| Account | Purpose | Resources |
|---------|---------|-----------|
| Portal | Orchestration & UI | CloudFront, API Gateway, Lambda, DynamoDB, Cognito |
| UseCase | ML Workloads & Devices | SageMaker, Greengrass, IoT Core, S3, Edge Devices |
| Data (Optional) | Centralized Training Data | S3 buckets shared across use cases |

## Core Capabilities

### 1. Data Management & Labeling
- Browse/upload training images to S3
- Create SageMaker Ground Truth bounding box jobs
- Import pre-labeled datasets (manifest files)
- Cross-account S3 access via assumed roles

### 2. Model Training & Compilation
- SageMaker training with AWS Marketplace algorithm
- Multi-target compilation (x86-64, ARM64, Jetson)
- Automatic model packaging as Greengrass components
- Training logs and metrics visualization

### 3. Model Registry
- Track model versions and lineage
- Stage promotion (candidate → staging → production)
- Deployment status per model

### 4. Edge Deployment
- Greengrass component management
- Device/group targeting
- Deployment monitoring and rollback

### 5. Device Management
- Fleet inventory and health monitoring
- Component logs via CloudWatch
- Device status (online/offline/healthy)

## Security Model

### Authentication
- Cognito User Pool with IdP federation (SAML/OIDC)
- Supports: Okta, Azure AD, Google Workspace, Auth0
- JWT-based API authorization

### Role-Based Access Control (RBAC)

| Role | Scope | Permissions |
|------|-------|-------------|
| PortalAdmin | Global | All operations, all use cases, settings, audit logs |
| UseCaseAdmin | Per UseCase | Full control within assigned use cases, team management |
| DataScientist | Per UseCase | Labeling, training, models, compilation |
| Operator | Per UseCase | Deployments, devices, monitoring |
| Viewer | Per UseCase | Read-only access |

### Cross-Account Access
- STS AssumeRole with ExternalID per use case
- Least-privilege IAM policies
- Separate roles for UseCase and Data accounts

## Technology Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 18, TypeScript, CloudScape Design System |
| API | API Gateway REST, Lambda (Python 3.11) |
| Auth | Cognito User Pool, JWT Authorizer |
| Database | DynamoDB (8 tables) |
| ML | SageMaker Training, Neo Compilation |
| Edge | Greengrass V2, IoT Core |
| IaC | AWS CDK (TypeScript) |
| Hosting | CloudFront + S3 |

## Data Model (Key Tables)

| Table | Purpose | Key |
|-------|---------|-----|
| UseCases | Use case configuration | usecase_id |
| UserRoles | RBAC assignments | user_id + usecase_id |
| TrainingJobs | Training job metadata | training_id |
| Models | Model registry | model_id |
| Deployments | Deployment tracking | deployment_id |
| Devices | Device inventory | device_id |
| AuditLog | User action audit | event_id + timestamp |

## Key Workflows

### Training-to-Deployment Pipeline
```
Dataset Selection → Training Job → Compilation → Packaging → Component Publish → Deployment
        │               │              │            │              │              │
        ▼               ▼              ▼            ▼              ▼              ▼
   S3 Manifest    SageMaker      SageMaker     Lambda       Greengrass      Greengrass
                  Training         Neo                       Registry        Deployment
```

### Use Case Onboarding
```
1. Deploy UseCase Stack (CDK) in target account
2. Create use case in Portal with Role ARN + External ID
3. Portal auto-configures: bucket policy, CORS, tagging
4. Provision shared components (dda-LocalServer)
5. Ready for labeling/training/deployment
```

## Deployment Architecture

```
Portal Account Deployment:
├── AuthStack (Cognito)
├── StorageStack (DynamoDB, S3)
├── ComputeStack (API Gateway, Lambda)
└── FrontendStack (CloudFront, S3)

UseCase Account Deployment:
├── UseCaseAccountStack (IAM roles, S3 bucket)
└── (Optional) DataAccountStack (shared data bucket)
```

## API Structure

```
/api/v1/
├── /auth           # Authentication
├── /usecases       # Use case CRUD
├── /datasets       # Dataset browsing
├── /labeling       # Ground Truth jobs
├── /training       # SageMaker training
├── /models         # Model registry
├── /components     # Greengrass components
├── /deployments    # Edge deployments
├── /devices        # Device management
├── /audit-logs     # Audit trail
└── /users          # Team management
```

## Future Considerations

- Real-time device log streaming (WebSocket)
- Model A/B testing on edge
- Automated retraining pipelines
- Cost allocation dashboards
- Multi-region deployment

---

*Document Version: 1.0 | Last Updated: January 2026*
