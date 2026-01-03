# S3 Structure Reorganization Design Document

## Overview

This design document outlines the reorganization of the S3 bucket structure for the Edge CV Portal to provide logical separation of artifacts, consistent naming conventions, and improved maintainability. The solution includes a migration strategy to transition from the current inconsistent structure to a well-organized hierarchy without disrupting existing functionality.

## Architecture

### Current Structure Problems
- Mixed purposes: `datasets/` prefix used for both datasets AND model outputs
- Inconsistent naming: `training-output` vs `compilation-output`
- Path construction issues: Double slashes (`datasets//training-output`)
- No logical grouping of related artifacts

### New Structure Design
```
s3://{bucket-name}/
├── datasets/                    # Raw datasets and manifests
│   ├── raw/                    # Original uploaded images
│   ├── manifests/              # Training manifest files
│   └── labeled/                # Ground Truth labeled datasets
├── models/                     # All model-related artifacts
│   ├── training/               # Training job outputs
│   │   └── {training-job-name}/
│   │       ├── model.tar.gz    # Original trained model
│   │       └── metadata.json   # Training metadata
│   └── compilation/            # Compilation job outputs
│       └── {compilation-job-name}/
│           ├── {target}/       # Per-target compiled models
│           │   └── model.tar.gz
│           └── metadata.json   # Compilation metadata
└── deployments/               # Deployment-ready artifacts
    └── {deployment-name}/
        ├── model.tar.gz       # Packaged model
        ├── config.json        # Deployment configuration
        └── component.zip      # Greengrass component
```

## Components and Interfaces

### S3 Path Builder Service
A centralized service for constructing S3 paths consistently across all Lambda functions.

```python
class S3PathBuilder:
    def __init__(self, bucket: str, prefix: str = ""):
        self.bucket = bucket
        self.prefix = prefix.rstrip('/')
    
    def training_output_path(self, job_name: str) -> str
    def compilation_output_path(self, job_name: str, target: str = None) -> str
    def dataset_path(self, dataset_type: str) -> str
    def deployment_path(self, deployment_name: str) -> str
```

### Migration Service
Handles the transition from old to new S3 structure.

```python
class S3MigrationService:
    def migrate_training_artifacts(self, training_id: str) -> bool
    def migrate_compilation_artifacts(self, training_id: str) -> bool
    def validate_migration(self) -> MigrationReport
    def rollback_migration(self, backup_manifest: str) -> bool
```

### Path Resolution Service
Provides backward compatibility during migration period.

```python
class PathResolver:
    def resolve_artifact_path(self, artifact_type: str, job_id: str) -> str
    def is_legacy_path(self, s3_uri: str) -> bool
    def convert_legacy_path(self, legacy_path: str) -> str
```

## Data Models

### S3 Path Configuration
```python
@dataclass
class S3PathConfig:
    bucket: str
    prefix: str
    training_folder: str = "models/training"
    compilation_folder: str = "models/compilation"
    dataset_folder: str = "datasets"
    deployment_folder: str = "deployments"
```

### Migration Manifest
```python
@dataclass
class MigrationRecord:
    old_path: str
    new_path: str
    artifact_type: str
    job_id: str
    migration_timestamp: int
    status: str  # 'pending', 'completed', 'failed'
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

<function_calls>
<invoke name="prework">
<parameter name="featureName">s3-structure-reorganization

### Property Reflection

After reviewing all testable properties from the prework analysis, I identified several areas where properties can be consolidated:

**Redundancy Analysis:**
- Properties 1.2 and 3.1 both test training output path structure - can be combined
- Properties 1.3 and 3.2 both test compilation output path structure - can be combined  
- Properties 2.1 and 2.5 both relate to path validation - can be combined into comprehensive path validation
- Properties 4.1, 4.4, and 6.2 all test artifact accessibility - can be combined into migration integrity property

**Consolidated Properties:**

Property 1: S3 path structure separation
*For any* generated S3 path, it should start with one of the expected top-level folders (datasets/, models/, or deployments/)
**Validates: Requirements 1.1, 1.5**

Property 2: Training output path structure  
*For any* training job, the output path should follow the pattern models/training/{job-name}/
**Validates: Requirements 1.2, 3.1**

Property 3: Compilation output path structure
*For any* compilation job, the output path should follow the pattern models/compilation/{job-name}/ with optional target subdirectories
**Validates: Requirements 1.3, 3.2, 3.3**

Property 4: Deployment artifact path structure
*For any* deployment, the artifact path should follow the pattern deployments/{deployment-name}/
**Validates: Requirements 1.4**

Property 5: Path validation and consistency
*For any* generated S3 path, it should be a valid S3 object key with no double slashes, empty segments, and consistent naming patterns
**Validates: Requirements 2.1, 2.2, 2.5**

Property 6: Job name inclusion in paths
*For any* job-specific path, it should contain the job name in the expected position within the path structure
**Validates: Requirements 2.3**

Property 7: API response path format
*For any* API response containing S3 paths, the paths should use the new structure format and be valid S3 URIs
**Validates: Requirements 3.4, 3.5, 5.4**

Property 8: Migration data integrity
*For any* migrated artifact, it should be accessible at the new location and the old location should be cleanly removed after migration
**Validates: Requirements 4.1, 4.4, 6.2**

Property 9: Database consistency after migration
*For any* DynamoDB record referencing S3 paths, the paths should be updated to reflect the new structure after migration
**Validates: Requirements 4.2**

Property 10: Backward compatibility during migration
*For any* legacy path reference during migration, the system should resolve it correctly while maintaining new path functionality
**Validates: Requirements 4.3, 5.5**

Property 11: Lambda function path configuration
*For any* new training or compilation job, the Lambda functions should configure S3 output paths using the new structure
**Validates: Requirements 5.1, 5.2, 5.3**

Property 12: Migration validation and audit
*For any* migration operation, the validation script should correctly identify path inconsistencies and orphaned artifacts
**Validates: Requirements 6.1, 6.3**

Property 13: End-to-end workflow functionality
*For any* training, compilation, or deployment workflow, it should complete successfully using the new path structure
**Validates: Requirements 6.4**

## Error Handling

### Migration Errors
- **Partial Migration Failure**: If some artifacts fail to migrate, maintain a rollback manifest and provide retry mechanisms
- **Path Collision**: If new paths conflict with existing objects, use versioning or alternative naming strategies
- **Permission Errors**: Ensure proper IAM permissions for cross-account S3 operations during migration

### Path Resolution Errors
- **Invalid Legacy Paths**: Gracefully handle malformed legacy paths with appropriate error messages
- **Missing Artifacts**: Provide clear error messages when referenced artifacts don't exist at expected locations
- **S3 Access Errors**: Handle S3 permission and connectivity issues with proper retry logic

### Validation Errors
- **Inconsistent Structure**: Report specific path inconsistencies with suggested corrections
- **Orphaned Artifacts**: Identify and report artifacts that don't follow the expected structure
- **Missing Metadata**: Handle cases where migration metadata is incomplete or corrupted

## Testing Strategy

### Unit Testing Approach
- Test S3PathBuilder with various input combinations to ensure correct path generation
- Test PathResolver with legacy and new path formats to verify conversion logic
- Test MigrationService with mock S3 operations to verify migration logic
- Test validation scripts with known good and bad S3 structures

### Property-Based Testing Approach
Using **Hypothesis** for Python property-based testing:
- Generate random job names, bucket names, and path components to test path building
- Generate random S3 structures to test migration and validation logic
- Generate random API responses to test path format consistency
- Each property-based test should run a minimum of 100 iterations
- Property tests will be tagged with comments referencing the design document properties

**Testing Framework**: Hypothesis (Python property-based testing library)
**Test Configuration**: Minimum 100 iterations per property test
**Test Tagging**: Each test tagged with format: `**Feature: s3-structure-reorganization, Property {number}: {property_text}**`

### Integration Testing
- Test complete migration workflow with real S3 buckets (using test accounts)
- Verify that training and compilation jobs work end-to-end with new paths
- Test backward compatibility with existing deployments during migration period
- Validate that all Lambda functions correctly use the new path structure

### Migration Testing
- Test migration with various existing S3 structures
- Verify rollback functionality restores original state
- Test partial migration scenarios and recovery
- Validate that no data is lost during migration process