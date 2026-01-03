# Requirements Document

## Introduction

The Edge CV Portal currently has an inconsistent and confusing S3 bucket structure that mixes datasets with model outputs and uses inconsistent naming conventions. This feature will reorganize the S3 structure to provide clear separation of concerns, consistent naming, and better organization for datasets, models, and deployment artifacts.

## Glossary

- **S3_Bucket**: The AWS S3 bucket used by a use case for storing all artifacts
- **S3_Prefix**: The root prefix/folder within the S3 bucket for organizing content
- **Training_Output**: Model artifacts produced by SageMaker training jobs
- **Compilation_Output**: Optimized model artifacts produced by SageMaker Neo compilation jobs
- **Dataset**: Raw images and manifest files used for training and labeling
- **Deployment_Artifact**: Packaged models ready for edge device deployment
- **Migration_Strategy**: Process for moving existing data to new structure without breaking existing functionality
- **Path_Convention**: Standardized naming pattern for S3 object keys

## Requirements

### Requirement 1

**User Story:** As a system administrator, I want a logical S3 bucket structure, so that I can easily locate and manage different types of artifacts.

#### Acceptance Criteria

1. WHEN organizing S3 content THEN the system SHALL separate datasets, models, and deployments into distinct top-level folders
2. WHEN storing training outputs THEN the system SHALL place them under models/training/{job-name}/ path structure
3. WHEN storing compilation outputs THEN the system SHALL place them under models/compilation/{job-name}/ path structure
4. WHEN storing deployment artifacts THEN the system SHALL place them under deployments/{deployment-name}/ path structure
5. WHEN storing datasets THEN the system SHALL place them under datasets/ path structure

### Requirement 2

**User Story:** As a developer, I want consistent S3 path naming conventions, so that I can predictably locate artifacts programmatically.

#### Acceptance Criteria

1. WHEN generating S3 paths THEN the system SHALL use consistent naming patterns across all artifact types
2. WHEN creating folder structures THEN the system SHALL avoid double slashes or empty path segments
3. WHEN naming job-specific folders THEN the system SHALL use the job name as the folder identifier
4. WHEN organizing by artifact type THEN the system SHALL use singular nouns for folder names (model, not models)
5. WHEN constructing paths THEN the system SHALL ensure all paths are valid S3 object keys

### Requirement 3

**User Story:** As a data scientist, I want training and compilation outputs organized by job, so that I can easily find artifacts for specific training runs.

#### Acceptance Criteria

1. WHEN a training job completes THEN the system SHALL store outputs in models/training/{training-job-name}/
2. WHEN a compilation job completes THEN the system SHALL store outputs in models/compilation/{compilation-job-name}/
3. WHEN multiple compilation targets exist THEN the system SHALL organize them by target platform within the job folder
4. WHEN accessing job artifacts THEN the system SHALL provide the complete S3 path in API responses
5. WHEN listing job outputs THEN the system SHALL return organized paths that reflect the job hierarchy

### Requirement 4

**User Story:** As a system operator, I want to migrate existing data to the new structure, so that current deployments continue working while adopting the improved organization.

#### Acceptance Criteria

1. WHEN migrating existing data THEN the system SHALL preserve all existing artifacts without data loss
2. WHEN updating path references THEN the system SHALL update DynamoDB records to reflect new S3 locations
3. WHEN migration is in progress THEN the system SHALL maintain backward compatibility for existing references
4. WHEN migration completes THEN the system SHALL validate that all artifacts are accessible at new locations
5. WHEN rollback is needed THEN the system SHALL provide a mechanism to revert to previous structure

### Requirement 5

**User Story:** As a backend developer, I want updated Lambda functions that use the new S3 structure, so that new jobs automatically use the improved organization.

#### Acceptance Criteria

1. WHEN creating training jobs THEN the system SHALL configure S3 output paths using the new structure
2. WHEN creating compilation jobs THEN the system SHALL configure S3 output paths using the new structure
3. WHEN packaging deployments THEN the system SHALL use the new deployment artifact paths
4. WHEN querying job status THEN the system SHALL return S3 paths using the new structure
5. WHEN handling legacy paths THEN the system SHALL gracefully handle both old and new path formats during transition

### Requirement 6

**User Story:** As a quality assurance engineer, I want validation of the S3 structure, so that I can verify the reorganization was successful.

#### Acceptance Criteria

1. WHEN validating S3 structure THEN the system SHALL provide a script to verify path consistency
2. WHEN checking artifact accessibility THEN the system SHALL confirm all artifacts are reachable via new paths
3. WHEN auditing the migration THEN the system SHALL report any orphaned or missing artifacts
4. WHEN testing functionality THEN the system SHALL verify that training, compilation, and deployment workflows work with new paths
5. WHEN documenting changes THEN the system SHALL provide clear documentation of the new structure and migration process