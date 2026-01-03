# Greengrass Component Workflow Alignment Requirements

## Introduction

This specification analyzes the alignment between the DDA Portal's automated Greengrass component creation workflow and the reference implementation in `DDA_Greengrass_Component_Creator.ipynb`. The goal is to ensure the portal follows the same proven steps and maintains compatibility with the DDA ecosystem.

## Glossary

- **DDA Portal**: The Edge CV Admin Portal's automated workflow for creating Greengrass components
- **Reference Notebook**: The `DDA_Greengrass_Component_Creator.ipynb` manual implementation
- **Component Recipe**: The Greengrass component configuration defining lifecycle, dependencies, and artifacts
- **Model Artifacts**: Packaged model files including trained models, compiled models, and manifests
- **DDA LocalServer**: The platform-specific Greengrass component that provides DDA runtime services

## Requirements

### Requirement 1: Workflow Phase Alignment

**User Story:** As a system architect, I want the portal's automated workflow to follow the same three-phase approach as the reference notebook, so that components are created consistently and reliably.

#### Acceptance Criteria

1. WHEN the portal creates Greengrass components THEN it SHALL follow Phase 1 (Model Artifact Preparation) from the reference notebook
2. WHEN the portal processes model artifacts THEN it SHALL follow Phase 2 (Directory Structure Setup) from the reference notebook  
3. WHEN the portal publishes components THEN it SHALL follow Phase 3 (Component Creation) from the reference notebook
4. WHEN each phase completes THEN the portal SHALL produce the same intermediate artifacts as the reference notebook
5. WHEN the workflow encounters errors THEN it SHALL provide the same level of diagnostic information as the reference notebook

### Requirement 2: Model Artifact Processing Consistency

**User Story:** As a data scientist, I want the portal to process model artifacts identically to the reference notebook, so that my models work correctly on edge devices.

#### Acceptance Criteria

1. WHEN the portal downloads trained models THEN it SHALL extract config.yaml and manifest.json files as the reference notebook does
2. WHEN the portal processes model configuration THEN it SHALL read image dimensions from config.yaml as the reference notebook does
3. WHEN the portal creates DDA manifests THEN it SHALL generate the same manifest structure as the reference notebook
4. WHEN the portal locates PyTorch models THEN it SHALL find .pt files in export_artifacts directory as the reference notebook does
5. WHEN the portal extracts input shapes THEN it SHALL use the same fallback logic as the reference notebook

### Requirement 3: Directory Structure Compatibility

**User Story:** As a DevOps engineer, I want the portal to create the same directory structure as the reference notebook, so that Greengrass components deploy correctly.

#### Acceptance Criteria

1. WHEN the portal organizes compiled models THEN it SHALL create model_artifacts/{model_name} directory structure as the reference notebook does
2. WHEN the portal packages components THEN it SHALL place manifest.json at model_artifacts/manifest.json as the reference notebook does
3. WHEN the portal creates ZIP archives THEN it SHALL use the same UUID-based naming convention as the reference notebook
4. WHEN the portal uploads to S3 THEN it SHALL use the same S3 key structure as the reference notebook
5. WHEN the portal processes multiple targets THEN it SHALL maintain consistent directory structure for each target

### Requirement 4: Component Recipe Equivalence

**User Story:** As a system administrator, I want the portal to generate identical component recipes to the reference notebook, so that components have the same runtime behavior.

#### Acceptance Criteria

1. WHEN the portal creates component recipes THEN it SHALL use the same RecipeFormatVersion as the reference notebook
2. WHEN the portal sets component dependencies THEN it SHALL use platform-specific DDA LocalServer components as the reference notebook does
3. WHEN the portal defines lifecycle scripts THEN it SHALL use identical startup and shutdown commands as the reference notebook
4. WHEN the portal configures artifacts THEN it SHALL use the same unarchive and permission settings as the reference notebook
5. WHEN the portal sets component configuration THEN it SHALL include the same DefaultConfiguration parameters as the reference notebook

### Requirement 5: Platform Mapping Accuracy

**User Story:** As a deployment engineer, I want the portal to map compilation targets to platforms correctly, so that components deploy to the right device architectures.

#### Acceptance Criteria

1. WHEN the portal processes jetson-xavier targets THEN it SHALL map to aarch64 platform and arm64 LocalServer dependency
2. WHEN the portal processes x86_64-cpu targets THEN it SHALL map to amd64 platform and amd64 LocalServer dependency
3. WHEN the portal processes arm64-cpu targets THEN it SHALL map to aarch64 platform and arm64 LocalServer dependency
4. WHEN the portal encounters unknown targets THEN it SHALL default to amd64 platform as the reference notebook does
5. WHEN the portal creates multiple components THEN it SHALL apply correct platform mapping for each target independently

### Requirement 6: Component Validation and Monitoring

**User Story:** As a quality assurance engineer, I want the portal to validate components the same way as the reference notebook, so that only working components are deployed.

#### Acceptance Criteria

1. WHEN the portal creates components THEN it SHALL validate component names start with "model-" as the reference notebook does
2. WHEN the portal sets component versions THEN it SHALL validate x.0.0 format as the reference notebook does
3. WHEN the portal monitors component status THEN it SHALL wait for DEPLOYABLE state as the reference notebook does
4. WHEN the portal detects component failures THEN it SHALL capture error messages as the reference notebook does
5. WHEN the portal completes component creation THEN it SHALL provide the same success indicators as the reference notebook

### Requirement 7: Error Handling Parity

**User Story:** As a support engineer, I want the portal to handle errors the same way as the reference notebook, so that troubleshooting is consistent.

#### Acceptance Criteria

1. WHEN the portal encounters missing model files THEN it SHALL report the same error messages as the reference notebook
2. WHEN the portal fails to extract archives THEN it SHALL provide the same diagnostic information as the reference notebook
3. WHEN the portal cannot create components THEN it SHALL capture AWS API errors as the reference notebook does
4. WHEN the portal times out waiting for component status THEN it SHALL handle timeouts gracefully as the reference notebook does
5. WHEN the portal encounters permission errors THEN it SHALL provide actionable error messages as the reference notebook does

### Requirement 8: Metadata and Tagging Consistency

**User Story:** As a system operator, I want the portal to tag components consistently with the reference notebook approach, so that components are properly tracked and managed.

#### Acceptance Criteria

1. WHEN the portal creates components THEN it SHALL include CreatedBy tags indicating DDA Portal origin
2. WHEN the portal publishes components THEN it SHALL tag with UseCase, TrainingJob, and ModelName metadata
3. WHEN the portal sets component publishers THEN it SHALL use "Amazon Lookout for Vision" as the reference notebook does
4. WHEN the portal creates model records THEN it SHALL store component ARNs for deployment tracking
5. WHEN the portal completes workflows THEN it SHALL maintain audit trails as comprehensive as the reference notebook