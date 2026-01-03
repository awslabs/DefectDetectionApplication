# Requirements Document

## Introduction

The Defect Detection Application (DDA) Portal currently has compilation functionality in the backend that automatically triggers when training jobs complete, but users cannot see the status, progress, or results of these compilation jobs in the frontend. Users need visibility into compilation job status, error messages, and the resulting Greengrass components to effectively manage their ML model deployment pipeline.

## Glossary

- **Portal**: The centralized web application for managing ML workflows
- **Training Job**: A SageMaker training job that produces a trained ML model
- **Compilation Job**: A SageMaker Neo compilation job that optimizes models for specific hardware targets
- **Target Architecture**: Hardware platform for compilation (e.g., jetson-xavier, x86_64-cpu, arm64-cpu)
- **Greengrass Component**: AWS IoT Greengrass component containing compiled model and inference code
- **Packaging**: Process of creating Greengrass components from compiled models
- **Model Artifact**: S3 location containing the compiled model files

## Requirements

### Requirement 1

**User Story:** As a Data Scientist, I want to see the status of compilation jobs triggered by my training jobs, so that I can monitor the model optimization process and identify any issues

#### Acceptance Criteria

1. WHEN a training job completes and triggers compilation, THE Portal SHALL display compilation job status for each target architecture
2. WHEN compilation jobs are running, THE Portal SHALL show real-time progress indicators and status updates
3. WHEN compilation jobs complete successfully, THE Portal SHALL display the S3 location of compiled model artifacts
4. WHEN compilation jobs fail, THE Portal SHALL display detailed error messages and failure reasons
5. THE Portal SHALL refresh compilation status automatically every 30 seconds while jobs are in progress

### Requirement 2

**User Story:** As a Data Scientist, I want to manually trigger compilation for additional target architectures, so that I can optimize my models for different hardware platforms as needed

#### Acceptance Criteria

1. WHEN viewing a completed training job, THE Portal SHALL provide a button to start compilation for selected target architectures
2. WHEN starting manual compilation, THE Portal SHALL allow selection of multiple target architectures from available options
3. THE Portal SHALL prevent duplicate compilation jobs for the same training job and target combination
4. WHEN manual compilation is triggered, THE Portal SHALL immediately update the UI to show the new compilation jobs
5. THE Portal SHALL validate that the training job is in completed status before allowing compilation

### Requirement 3

**User Story:** As a Data Scientist, I want to see compilation job details in a dedicated section of the training job interface, so that I can easily access compilation information without cluttering the main training view

#### Acceptance Criteria

1. WHEN viewing training job details, THE Portal SHALL provide a "Compilation" tab alongside Overview and Logs tabs
2. THE Compilation tab SHALL display a table showing all compilation jobs with target, status, duration, and artifact location
3. WHEN compilation jobs exist, THE Portal SHALL show a compilation status indicator in the main training job list
4. THE Portal SHALL organize compilation information by target architecture for easy comparison
5. THE Portal SHALL provide direct links to download compiled model artifacts when available

### Requirement 4

**User Story:** As a Data Scientist, I want to see the Greengrass components created from my compiled models, so that I can track the complete pipeline from training to deployment-ready components

#### Acceptance Criteria

1. WHEN compilation and packaging complete, THE Portal SHALL display Greengrass component information including component name, version, and ARN
2. THE Portal SHALL show the relationship between compiled models and their corresponding Greengrass components
3. WHEN Greengrass components are created, THE Portal SHALL provide links to view component details in the AWS console
4. THE Portal SHALL display component creation status and any packaging errors
5. THE Portal SHALL show which devices currently have each component deployed

### Requirement 5

**User Story:** As a Data Scientist, I want to understand why compilation jobs fail, so that I can troubleshoot model or configuration issues

#### Acceptance Criteria

1. WHEN compilation jobs fail, THE Portal SHALL display the specific SageMaker error message and failure reason
2. THE Portal SHALL provide guidance on common compilation failure causes and resolution steps
3. THE Portal SHALL allow downloading of compilation logs when available
4. THE Portal SHALL show compilation job duration and resource usage information
5. THE Portal SHALL maintain a history of all compilation attempts including failed ones

### Requirement 6

**User Story:** As an Operator, I want to see compilation status in the training job overview, so that I can quickly identify which models are ready for deployment

#### Acceptance Criteria

1. WHEN viewing the training jobs list, THE Portal SHALL show compilation status indicators for each job
2. THE Portal SHALL use clear visual indicators to distinguish between not compiled, compiling, compiled successfully, and compilation failed states
3. THE Portal SHALL show the number of successful compilations vs total target architectures
4. THE Portal SHALL allow filtering training jobs by compilation status
5. THE Portal SHALL provide tooltips showing compilation details on hover in the list view