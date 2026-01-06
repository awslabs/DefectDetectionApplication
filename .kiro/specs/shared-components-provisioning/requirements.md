# Requirements Document

## Introduction

This document specifies the requirements for the dda-LocalServer Shared Components feature in the Edge CV Portal. The feature enables the portal account to create and manage Greengrass components (specifically dda-LocalServer) that are shared read-only to usecase accounts during onboarding. This ensures consistent deployment of the DDA LocalServer across all usecase accounts while preventing unauthorized modifications.

## Glossary

- **Portal_Account**: The central AWS account that hosts the Edge CV Portal and manages shared components
- **Usecase_Account**: Customer AWS accounts that are onboarded to use the DDA Portal for edge ML deployments
- **Shared_Component**: A Greengrass component (dda-LocalServer) created in the portal account and mirrored to usecase accounts
- **Component_Mirroring**: The process of creating a component in a usecase account with artifacts pointing to the portal's S3 bucket
- **Portal_Artifacts_Bucket**: S3 bucket in the portal account that stores shared component artifacts
- **Cross_Account_Role**: IAM role in usecase accounts that allows the portal to perform operations

## Requirements

### Requirement 1: Portal Artifacts Storage

**User Story:** As a portal administrator, I want to store dda-LocalServer artifacts in a centralized S3 bucket, so that all usecase accounts can access the same component versions.

#### Acceptance Criteria

1. THE Portal_Account SHALL have an S3 bucket named `dda-portal-artifacts-{account}-{region}` for storing shared component artifacts
2. THE Portal_Artifacts_Bucket SHALL have versioning enabled for artifact version management
3. THE Portal_Artifacts_Bucket SHALL have a bucket policy allowing cross-account read access for Greengrass device roles
4. THE Portal_Artifacts_Bucket SHALL store artifacts under the path `shared-components/dda-localserver/{platform}/`
5. WHEN a Greengrass device role from any account requests artifact access, THE Portal_Artifacts_Bucket SHALL allow s3:GetObject for the shared-components prefix

### Requirement 2: Shared Components Table

**User Story:** As a portal administrator, I want to track which shared components have been provisioned to which usecase accounts, so that I can manage component versions across accounts.

#### Acceptance Criteria

1. THE Portal_Account SHALL have a DynamoDB table named `dda-portal-shared-components` for tracking shared component provisioning
2. THE SharedComponents_Table SHALL use `usecase_id` as partition key and `component_name` as sort key
3. WHEN a shared component is provisioned, THE System SHALL record usecase_id, component_name, component_version, component_arn, platform, shared_by, and shared_at
4. THE SharedComponents_Table SHALL have a GSI on `component_name` to query all usecases with a specific component

### Requirement 3: Component Provisioning During Onboarding

**User Story:** As a portal user, I want shared components to be automatically provisioned when I onboard a new usecase, so that the usecase account is ready for deployments.

#### Acceptance Criteria

1. WHEN a new usecase is created via POST /api/v1/usecases, THE System SHALL automatically provision shared components to the usecase account
2. THE System SHALL create both arm64 and amd64 variants of dda-LocalServer in the usecase account
3. WHEN provisioning shared components, THE System SHALL assume the cross-account role in the usecase account
4. THE System SHALL tag shared components with `dda-portal:shared-component=true` and `dda-portal:read-only=true`
5. IF shared component provisioning fails, THEN THE System SHALL still complete usecase creation and log the failure
6. THE System SHALL update the usecase record with `shared_components_provisioned` status

### Requirement 4: Component Recipe Generation

**User Story:** As a portal administrator, I want component recipes to be generated with correct artifact URIs, so that Greengrass devices can download artifacts from the portal bucket.

#### Acceptance Criteria

1. WHEN generating a component recipe, THE System SHALL set the artifact URI to point to the Portal_Artifacts_Bucket
2. THE System SHALL generate recipes for both arm64 (aarch64) and amd64 (x86_64) platforms
3. THE Component_Recipe SHALL include default configuration for ServerPort, ModelPath, and LogLevel
4. THE Component_Recipe SHALL specify lifecycle scripts for Install, Run, and Shutdown phases
5. THE Component_Recipe SHALL set ComponentPublisher to 'AWS Edge ML - DDA Portal'

### Requirement 5: Read-Only Protection

**User Story:** As a portal administrator, I want shared components to be protected from modification in usecase accounts, so that component integrity is maintained.

#### Acceptance Criteria

1. THE Usecase_Account_Role SHALL include an IAM deny policy for modifying shared components
2. WHEN a component has tag `dda-portal:shared-component=true`, THE System SHALL deny greengrass:DeleteComponent action
3. WHEN a component has tag `dda-portal:shared-component=true`, THE System SHALL deny greengrass:CreateComponentVersion action
4. THE Deny_Policy SHALL apply to components matching `aws.edgeml.dda.LocalServer*` pattern

### Requirement 6: Greengrass Device Artifact Access

**User Story:** As a Greengrass device operator, I want devices to be able to download shared component artifacts, so that deployments can succeed.

#### Acceptance Criteria

1. WHEN a usecase is onboarded, THE System SHALL create an IAM policy allowing s3:GetObject from the Portal_Artifacts_Bucket
2. THE System SHALL attach the artifact access policy to the Greengrass device role in the usecase account
3. THE Policy SHALL restrict access to the `shared-components/*` prefix only
4. IF the standard Greengrass role name is not found, THEN THE System SHALL try alternative role names

### Requirement 7: Shared Components API

**User Story:** As a portal user, I want API endpoints to manage shared components, so that I can view and provision components as needed.

#### Acceptance Criteria

1. THE System SHALL provide GET /api/v1/shared-components/available to list available shared components from the portal
2. THE System SHALL provide GET /api/v1/shared-components?usecase_id={id} to list shared components for a specific usecase
3. THE System SHALL provide POST /api/v1/shared-components/provision to manually provision shared components
4. WHEN listing available components, THE System SHALL return component_name, description, platform, and latest_version
5. WHEN provisioning components, THE System SHALL require UseCaseAdmin permission

### Requirement 8: Multi-Platform Support

**User Story:** As a device operator, I want dda-LocalServer to be available for both ARM64 and AMD64 devices, so that I can deploy to various hardware platforms.

#### Acceptance Criteria

1. THE System SHALL support `aws.edgeml.dda.LocalServer.arm64` for ARM64 devices (Jetson, Raspberry Pi)
2. THE System SHALL support `aws.edgeml.dda.LocalServer.amd64` for AMD64 devices (x86_64)
3. WHEN provisioning to a usecase, THE System SHALL create both platform variants
4. THE System SHALL store platform-specific artifacts in separate S3 paths

### Requirement 9: Audit Logging

**User Story:** As a portal administrator, I want shared component operations to be logged, so that I can track provisioning activities.

#### Acceptance Criteria

1. WHEN shared components are provisioned, THE System SHALL log an audit event with action 'provision_shared_components'
2. THE Audit_Event SHALL include user_id, usecase_id, component names, and provisioning result
3. THE Audit_Event SHALL include whether the IAM policy was successfully updated
