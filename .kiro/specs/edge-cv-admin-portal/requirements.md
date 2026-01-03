# Requirements Document

## Introduction

The Defect Detection Application (DDA) Admin Portal is a centralized multi-tenant web application that manages the complete lifecycle of defect detection workloads across multiple AWS accounts. The Portal enables administrators, data scientists, and operators to manage labeling workflows, train and compile ML models, publish them as AWS Greengrass components, deploy to edge devices, and monitor device health—all through a unified interface with SSO-based authentication and cross-account access controls.

## Glossary

- **Portal**: The centralized web application deployed in the Portal Account
- **Portal Account**: The AWS account where the Portal application is deployed
- **UseCase Account**: An AWS account containing edge devices, S3 datasets, and IoT resources for a specific computer vision use case
- **Labeling Account**: A centralized AWS account where SageMaker Ground Truth labeling jobs are executed
- **Edge Device**: A physical device running AWS Greengrass Core and the DDA application
- **DDA Application**: The Defect Detection Application running on edge devices as a Greengrass component
- **Management Agent**: A lightweight service on edge devices that exposes file browsing, log access, and control endpoints
- **Ground Truth**: AWS SageMaker Ground Truth service for data labeling
- **Component**: An AWS Greengrass component containing application code or ML models
- **Deployment**: A Greengrass deployment that installs or updates components on target devices
- **SSO**: Single Sign-On authentication system
- **RBAC**: Role-Based Access Control

## Requirements

### Requirement 1

**User Story:** As a Portal Administrator, I want to authenticate users via company SSO, so that access is centrally managed and secure

#### Acceptance Criteria

1. WHEN a user navigates to the Portal login page, THE Portal SHALL redirect the user to the company SSO identity provider
2. WHEN the SSO identity provider successfully authenticates a user, THE Portal SHALL create a session with JWT tokens
3. THE Portal SHALL map SSO group memberships to RBAC roles (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer)
4. WHEN a user session expires, THE Portal SHALL require re-authentication through SSO
5. THE Portal SHALL enforce role-based permissions on all API endpoints and UI actions

### Requirement 2

**User Story:** As a Portal Administrator, I want to create and manage use cases, so that I can organize resources by business context and AWS account

#### Acceptance Criteria

1. WHEN a PortalAdmin creates a use case, THE Portal SHALL store the use case metadata including account ID, S3 bucket, cross-account role ARN, and owner information
2. THE Portal SHALL validate that the cross-account role ARN is accessible via STS AssumeRole with ExternalID
3. WHEN a PortalAdmin assigns users to a use case, THE Portal SHALL restrict those users' access to only assigned use cases
4. THE Portal SHALL display a list of all use cases with filtering and search capabilities
5. WHEN a PortalAdmin deletes a use case, THE Portal SHALL prevent deletion if active jobs or deployments exist

### Requirement 3

**User Story:** As a Portal Super User, I want automatic access to all use cases, so that I can manage and troubleshoot any use case without explicit assignment

#### Acceptance Criteria

1. WHEN a user with PortalAdmin role logs in, THE Portal SHALL grant access to all use cases regardless of explicit assignments
2. THE Portal SHALL display all use cases in the use case selector for PortalAdmin users
3. THE Portal SHALL allow PortalAdmin users to perform all operations on any use case
4. THE Portal SHALL log all PortalAdmin actions with the super user designation in audit trails
5. THE Portal SHALL display a visual indicator when a PortalAdmin is operating with super user privileges

### Requirement 4

**User Story:** As a Data Scientist, I want to create Ground Truth labeling jobs from S3 datasets, so that I can generate labeled training data

#### Acceptance Criteria

1. WHEN a Data Scientist selects a use case, THE Portal SHALL list available S3 prefixes and image counts from the UseCase Account
2. WHEN a Data Scientist creates a labeling job, THE Portal SHALL generate a manifest file and create a Ground Truth job in the Labeling Account
3. THE Portal SHALL use cross-account IAM roles with ExternalID to access S3 data in the UseCase Account
4. THE Portal SHALL display labeling job progress including percentage complete and worker metrics
5. WHEN a labeling job completes, THE Portal SHALL provide a download link for the labeled manifest file

### Requirement 4A

**User Story:** As a Data Scientist, I want to choose between creating a new labeling job or using existing pre-labeled data, so that I can skip the labeling step when I already have labeled datasets

#### Acceptance Criteria

1. WHEN a Data Scientist navigates to the Labeling page, THE Portal SHALL display options to either create a new labeling job or use pre-labeled datasets
2. WHEN a Data Scientist selects the pre-labeled dataset option, THE Portal SHALL navigate to a dataset management interface
3. THE Portal SHALL allow upload of manifest files in SageMaker Ground Truth format (JSONL)
4. THE Portal SHALL allow specification of S3 URIs pointing to existing manifest files
5. THE Portal SHALL validate manifest file format and display statistics including image count and label distribution
6. THE Portal SHALL store pre-labeled dataset metadata for reuse in training workflows
7. WHEN a Data Scientist creates a training job, THE Portal SHALL allow selection from both Ground Truth labeling jobs and pre-labeled datasets

### Requirement 5

**User Story:** As a Data Scientist, I want to train ML models using labeled datasets, so that I can create models for edge deployment

#### Acceptance Criteria

1. WHEN a Data Scientist submits a training job, THE Portal SHALL start a SageMaker training job with the specified algorithm, hyperparameters, and compute configuration
2. THE Portal SHALL support both AWS Marketplace algorithms and custom container URIs
3. THE Portal SHALL display real-time training logs and metrics from CloudWatch
4. WHEN a training job completes successfully, THE Portal SHALL store the model artifact location and metadata in the Model Registry
5. IF a training job fails, THEN THE Portal SHALL send an alert notification and display error logs

### Requirement 6

**User Story:** As a Data Scientist, I want to compile trained models for specific edge targets, so that models run efficiently on edge hardware

#### Acceptance Criteria

1. WHEN a Data Scientist selects a trained model for compilation, THE Portal SHALL trigger a SageMaker Neo compilation job for the specified target architecture
2. THE Portal SHALL load available compilation target architectures from an administrator-configured configuration file
3. THE Portal SHALL display compilation progress and logs
4. WHEN compilation completes, THE Portal SHALL package the compiled artifact with an inference runner into a Greengrass component structure
5. THE Portal SHALL publish the Greengrass component to the component registry in the UseCase Account via assumed role

### Requirement 7

**User Story:** As a Data Scientist, I want to maintain a model registry, so that I can track model versions, metrics, and deployment status

#### Acceptance Criteria

1. THE Portal SHALL store model metadata including name, version, use case, training job ID, metrics, dataset manifest ID, and component ARN
2. THE Portal SHALL display a searchable and filterable list of all models
3. WHEN a Data Scientist promotes a model, THE Portal SHALL update the model stage (candidate, staging, production)
4. THE Portal SHALL display which devices currently have each model version deployed
5. THE Portal SHALL prevent deletion of models that are actively deployed

### Requirement 8

**User Story:** As an Operator, I want to deploy Greengrass components to edge devices, so that I can update applications and models

#### Acceptance Criteria

1. WHEN an Operator creates a deployment, THE Portal SHALL allow selection of one or more Greengrass components and target devices or device groups
2. THE Portal SHALL support rollout strategies including all-at-once, canary, and percentage-based deployments
3. THE Portal SHALL create the Greengrass deployment via the Greengrass API in the UseCase Account using assumed role
4. THE Portal SHALL display per-device deployment status with real-time updates
5. WHEN a deployment fails on any device, THE Portal SHALL provide rollback capability and display error details

### Requirement 9

**User Story:** As an Operator, I want to view device inventory and health status, so that I can monitor the edge fleet

#### Acceptance Criteria

1. THE Portal SHALL display a list of all devices per use case including device ID, status, last heartbeat, installed components, and storage usage
2. THE Portal SHALL update device status based on IoT Thing Shadow and Greengrass deployment status
3. THE Portal SHALL provide filtering and search capabilities on the device list
4. WHEN a device goes offline for more than a threshold period, THE Portal SHALL send an alert notification
5. THE Portal SHALL display device health metrics including uptime, error counts, and camera status

### Requirement 10

**User Story:** As an Operator, I want to browse device files and view logs, so that I can troubleshoot issues remotely

#### Acceptance Criteria

1. WHEN an Operator views a device detail page, THE Portal SHALL display a file browser showing configured directories
2. THE Portal SHALL communicate with the Management Agent on the device via IoT Core to retrieve file listings
3. WHEN an Operator selects a file, THE Portal SHALL provide a download capability through the Management Agent
4. THE Portal SHALL display a log viewer that tails DDA application logs and Greengrass logs in real-time
5. THE Portal SHALL provide text search and time-based filtering on log entries

### Requirement 11

**User Story:** As an Operator, I want to restart Greengrass or reboot devices remotely, so that I can resolve issues without physical access

#### Acceptance Criteria

1. WHEN an Operator triggers a Greengrass restart, THE Portal SHALL send an IoT Job or command to the Management Agent
2. THE Portal SHALL require confirmation before executing restart or reboot actions
3. THE Portal SHALL display the action status and update device status when the action completes
4. THE Portal SHALL log all restart and reboot actions in the audit trail
5. IF a device does not respond to a restart command within a timeout period, THEN THE Portal SHALL display a timeout error

### Requirement 12

**User Story:** As a Portal Administrator, I want to enforce cross-account access controls, so that the Portal operates with least privilege

#### Acceptance Criteria

1. THE Portal SHALL use STS AssumeRole with ExternalID to access resources in UseCase Accounts
2. THE Portal SHALL enforce that assumed roles have only the minimum required permissions for S3, SageMaker, Greengrass, and IoT operations
3. THE Portal SHALL encrypt all data at rest using KMS keys with appropriate key policies
4. THE Portal SHALL enforce TLS for all data in transit
5. THE Portal SHALL validate that KMS key policies allow both Portal Account and UseCase Account roles to decrypt artifacts

### Requirement 13

**User Story:** As a Portal Administrator, I want to audit all user actions and system events, so that I can maintain compliance and troubleshoot issues

#### Acceptance Criteria

1. THE Portal SHALL log all user actions including job creation, deployment, component publication, and configuration changes
2. THE Portal SHALL store audit events with timestamp, user identity, action type, resource ID, and result
3. THE Portal SHALL provide an audit log viewer with filtering by user, action type, time range, and use case
4. THE Portal SHALL integrate with CloudTrail to capture AWS API calls made by the Portal
5. THE Portal SHALL retain audit logs for a configurable retention period

### Requirement 14

**User Story:** As a Portal Administrator, I want to monitor system health and receive alerts, so that I can respond to failures quickly

#### Acceptance Criteria

1. THE Portal SHALL integrate with CloudWatch to collect metrics from training jobs, compilation jobs, and Step Functions
2. THE Portal SHALL display dashboards showing active jobs, device health, and recent events
3. WHEN a training job fails, THE Portal SHALL send an SNS notification to configured recipients
4. WHEN a device goes offline, THE Portal SHALL send an alert notification
5. THE Portal SHALL provide configurable alert thresholds for device metrics and job failures

### Requirement 15

**User Story:** As a Data Scientist, I want the Portal to orchestrate the complete training-to-deployment pipeline, so that I can deploy models with minimal manual steps

#### Acceptance Criteria

1. WHEN a training job completes successfully, THE Portal SHALL automatically trigger compilation for configured target architectures
2. WHEN compilation completes, THE Portal SHALL automatically package and publish the Greengrass component
3. THE Portal SHALL use Step Functions to orchestrate the training, compilation, and publishing workflow
4. THE Portal SHALL provide visibility into each workflow step with status and logs
5. IF any workflow step fails, THEN THE Portal SHALL halt the workflow and display the failure reason

### Requirement 16

**User Story:** As an Operator, I want to manage device configurations remotely, so that I can adjust settings without physical access

#### Acceptance Criteria

1. THE Portal SHALL allow Operators to update device configuration via IoT Thing Shadow
2. WHEN an Operator updates a configuration, THE Portal SHALL validate the configuration schema before applying
3. THE Portal SHALL display the current and desired configuration state for each device
4. THE Portal SHALL track configuration change history with timestamps and user identity
5. IF a device rejects a configuration update, THEN THE Portal SHALL display the rejection reason

### Requirement 17

**User Story:** As a Portal user, I want responsive UI with real-time updates, so that I can monitor long-running operations efficiently

#### Acceptance Criteria

1. THE Portal SHALL use pagination for all list views with configurable page size
2. THE Portal SHALL provide real-time progress updates for training jobs, labeling jobs, and deployments via WebSocket or polling
3. THE Portal SHALL display loading indicators during asynchronous operations
4. THE Portal SHALL complete page load operations within 3 seconds for typical list views
5. THE Portal SHALL provide confirmation modals for all destructive actions

### Requirement 18

**User Story:** As a Portal Administrator, I want to track costs per use case, so that I can allocate expenses appropriately

#### Acceptance Criteria

1. THE Portal SHALL tag all created AWS resources with use case ID and cost center metadata
2. THE Portal SHALL provide links to Cost Explorer filtered by use case tags
3. THE Portal SHALL display estimated costs for training jobs based on instance type and duration
4. THE Portal SHALL enforce quota limits on training job instance types per use case
5. THE Portal SHALL log resource creation events for cost tracking purposes

### Requirement 19

**User Story:** As a developer, I want the Portal to be deployed using Infrastructure as Code, so that environments are reproducible and version-controlled

#### Acceptance Criteria

1. THE Portal SHALL provide IaC templates (Terraform, CDK, or CloudFormation) for all Portal Account resources
2. THE Portal SHALL provide template IAM policies for PortalAccessRole in UseCase Accounts
3. THE Portal SHALL provide template IAM policies for LabelingPortalRole in the Labeling Account
4. THE Portal SHALL include deployment documentation with step-by-step instructions
5. THE Portal SHALL support deployment to multiple AWS regions
