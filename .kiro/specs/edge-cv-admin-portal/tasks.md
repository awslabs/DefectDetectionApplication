# Implementation Plan

- [x] 1. Set up project structure and infrastructure foundation
  - Create monorepo structure with frontend and backend directories
  - Initialize AWS CDK project for infrastructure as code
  - Set up CI/CD pipeline configuration files
  - Configure development environment and tooling
  - _Requirements: 19.1, 19.2, 19.3_

- [ ] 2. Implement authentication and authorization system
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.1, 3.2, 3.3, 3.4, 3.5_

- [-] 2.1 Deploy Cognito User Pool with SSO federation
  - Create CDK stack for Cognito User Pool
  - Configure SAML/OIDC identity provider integration
  - Set up attribute mapping for SSO groups to roles
  - Configure JWT token settings and expiration
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2.2 Implement API Gateway JWT authorizer
  - Create Lambda authorizer function for JWT validation
  - Configure API Gateway to use JWT authorizer
  - Implement token refresh endpoint
  - _Requirements: 1.4_

- [x] 2.3 Build RBAC authorization logic
  - Create Lambda layer for shared authorization utilities
  - Implement role-based permission checking (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer)
  - Implement super user access logic for PortalAdmin role
  - Create middleware for use case access validation
  - _Requirements: 1.5, 3.1, 3.2, 3.3_

- [ ] 2.4 Create DynamoDB UserRoles table and access patterns
  - Define UserRoles table schema with GSIs
  - Implement user-to-use-case assignment functions
  - Create queries for fetching user roles and permissions
  - _Requirements: 1.5, 3.1_

- [ ] 2.5 Write unit tests for authentication and authorization
  - Test JWT validation logic
  - Test RBAC permission checks for all roles
  - Test super user access scenarios
  - _Requirements: 1.1, 1.5, 3.1_

- [ ] 3. Build use case management system
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 3.1 Create DynamoDB UseCases table
  - Define table schema with attributes and GSIs
  - Implement data access layer for CRUD operations
  - _Requirements: 2.1_

- [x] 3.2 Implement use case API endpoints
  - Create Lambda handler for GET /api/v1/usecases (list)
  - Create Lambda handler for POST /api/v1/usecases (create)
  - Create Lambda handler for GET /api/v1/usecases/{id} (get details)
  - Create Lambda handler for PUT /api/v1/usecases/{id} (update)
  - Create Lambda handler for DELETE /api/v1/usecases/{id} (delete with validation)
  - _Requirements: 2.1, 2.3, 2.4, 2.5_

- [ ] 3.3 Implement cross-account role validation
  - Create utility function to assume cross-account role with ExternalID
  - Implement STS AssumeRole validation during use case creation
  - Add error handling for invalid role ARNs or ExternalIDs
  - _Requirements: 2.2, 12.1, 12.2_

- [ ] 3.4 Write unit tests for use case management
  - Test CRUD operations
  - Test cross-account role validation
  - Test deletion prevention with active resources
  - _Requirements: 2.1, 2.2, 2.5_

- [ ] 4. Implement data labeling workflow
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 4.1 Create DynamoDB LabelingJobs table
  - Define table schema with status tracking and GSIs
  - Implement data access layer
  - _Requirements: 4.4_

- [x] 4.2 Build S3 dataset listing functionality
  - Create Lambda function to list S3 prefixes via cross-account access
  - Implement image counting logic
  - Add caching for performance
  - _Requirements: 4.1_

- [x] 4.3 Implement Ground Truth job creation
  - Create Lambda function to generate manifest files
  - Implement Step Functions workflow for labeling job creation
  - Add logic to assume UseCase Account role and create Ground Truth job
  - Pass GroundTruthExecutionRole ARN when creating labeling job
  - Store job metadata in DynamoDB
  - _Requirements: 4.2, 4.3_

- [x] 4.4 Build labeling job monitoring
  - Create Lambda function to poll Ground Truth job status
  - Implement EventBridge rule for job completion events
  - Update job progress in DynamoDB
  - _Requirements: 4.4_

- [x] 4.5 Implement labeling API endpoints
  - Create GET /api/v1/labeling (list jobs)
  - Create POST /api/v1/labeling (create job)
  - Create GET /api/v1/labeling/{id} (get status)
  - Create GET /api/v1/labeling/{id}/manifest (download manifest)
  - _Requirements: 4.1, 4.2, 4.4, 4.5_

- [ ] 4.6 Write unit tests for labeling workflow
  - Test manifest generation
  - Test Ground Truth job creation
  - Test status polling logic
  - _Requirements: 4.2, 4.3, 4.4_

- [ ] 4.7 Implement dataset selection and validation
  - _Requirements: 4.1, 4.5, 5.1_

- [ ] 4.7.1 Build manifest discovery functionality
  - Create Lambda function to scan S3 for manifest files
  - Scan common prefixes (manifests/, labeled/, training-data/)
  - Parse manifest files to extract metadata (image count, labels)
  - Detect manifest source (Ground Truth vs external)
  - Return sorted list with metadata
  - _Requirements: 4.1, 4.5_

- [ ] 4.7.2 Implement manifest validation
  - Create Lambda function to validate manifest format
  - Support Ground Truth formats (object detection, classification, segmentation)
  - Validate required fields (source-ref, labels, metadata)
  - Extract label categories and statistics
  - Check for common issues (missing images, invalid JSON)
  - Return validation result with detailed feedback
  - _Requirements: 4.5, 5.1_

- [ ] 4.7.3 Add dataset selection API endpoints
  - Create GET /api/v1/datasets/manifests (list available manifests)
  - Create POST /api/v1/datasets/validate-manifest (validate manifest)
  - Add filtering by source type (ground_truth, external, all)
  - Include pagination for large manifest lists
  - _Requirements: 4.1, 4.5_

- [ ] 4.7.4 Build DatasetSelector React component
  - Create reusable component with three selection modes
  - Mode 1: Select from completed Ground Truth jobs
  - Mode 2: Browse and select pre-labeled manifests
  - Mode 3: Manual S3 URI input
  - Add manifest validation UI with feedback
  - Display manifest metadata (image count, labels, date)
  - Add optional train/validation split checkbox
  - _Requirements: 4.1, 4.5, 5.1_

- [ ] 4.7.5 Integrate DatasetSelector into training workflow
  - Update CreateTraining page to use DatasetSelector
  - Replace manual textarea with dataset selection component
  - Add validation before training job submission
  - Show manifest preview and statistics
  - _Requirements: 5.1_

- [ ]* 4.7.6 Write unit tests for dataset selection
  - Test manifest discovery logic
  - Test manifest validation rules
  - Test DatasetSelector component interactions
  - Test API endpoint responses
  - _Requirements: 4.1, 4.5_

- [x] 4.8 Add pre-labeled dataset option to Labeling page
  - _Requirements: 4A.1, 4A.2_

- [x] 4.8.1 Update Labeling page header with dataset option button
  - Add "Use Pre-Labeled Dataset" button next to "Create Labeling Job" button
  - Configure button to navigate to /labeling/pre-labeled route
  - Update button styling to use normal variant (not primary)
  - _Requirements: 4A.1, 4A.2_

- [x] 4.8.2 Create PreLabeledDatasets page component
  - Build page layout with header and action buttons
  - Create table to display existing pre-labeled datasets
  - Add columns: name, task type, image count, label distribution, created date, actions
  - Implement dataset deletion with confirmation
  - Add "Back to Labeling" and "Add Pre-Labeled Dataset" buttons
  - _Requirements: 4A.2, 4A.6_

- [x] 4.8.3 Build dataset upload modal
  - Create modal with form for dataset name, description
  - Add upload method selector (file upload vs S3 URI)
  - Implement file upload component for JSONL manifest files
  - Add S3 URI input field with validation
  - Include "Validate Manifest" button to check format before creation
  - Display validation results with statistics and errors
  - _Requirements: 4A.3, 4A.4, 4A.5_

- [x] 4.8.4 Implement backend API for pre-labeled datasets
  - Create DynamoDB table for pre-labeled dataset metadata
  - Add GSI on usecase_id for querying datasets by use case
  - Create Lambda function for dataset CRUD operations
  - Implement POST /datasets/validate-manifest endpoint
  - Implement POST /datasets/pre-labeled endpoint (create)
  - Implement GET /datasets/pre-labeled endpoint (list)
  - Implement GET /datasets/pre-labeled/{id} endpoint (get details)
  - Implement DELETE /datasets/pre-labeled/{id} endpoint
  - _Requirements: 4A.3, 4A.4, 4A.5, 4A.6_

- [x] 4.8.5 Add manifest validation logic
  - Parse JSONL manifest files line by line
  - Validate required fields (source-ref, label fields, metadata)
  - Detect task type (classification, segmentation, detection)
  - Extract label distribution statistics
  - Count total images in manifest
  - Check for common issues (missing images, invalid format)
  - Return validation result with errors, warnings, and statistics
  - _Requirements: 4A.5_

- [x] 4.8.6 Integrate pre-labeled datasets into training workflow
  - Update DatasetSelector component to include pre-labeled datasets
  - Add pre-labeled datasets as a source option alongside Ground Truth jobs
  - Display dataset metadata when selected
  - Pass dataset manifest S3 URI to training job creation
  - _Requirements: 4A.7_

- [x] 4.8.7 Add route for PreLabeledDatasets page
  - Register /labeling/pre-labeled route in App.tsx
  - Import and configure PreLabeledDatasets component
  - Ensure route is protected with authentication
  - _Requirements: 4A.2_

- [ ]* 4.8.8 Write unit tests for pre-labeled dataset feature
  - Test manifest validation logic
  - Test dataset CRUD operations
  - Test PreLabeledDatasets component rendering
  - Test upload modal interactions
  - Test integration with training workflow
  - _Requirements: 4A.3, 4A.4, 4A.5, 4A.6_

- [ ] 5. Implement model training pipeline
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 15.1, 15.2, 15.3, 15.4, 15.5_
  - _Note: Training workflow is based on DDA_SageMaker_Model_Training_and_Compilation.ipynb_

- [x] 5.1 Create DynamoDB TrainingJobs table
  - Define table schema with metrics and artifact tracking
  - Implement data access layer with GSIs
  - _Requirements: 5.4_

- [x] 5.2 Build training job submission logic
  - Create Lambda function to start SageMaker training jobs following DDA_SageMaker_Model_Training_and_Compilation.ipynb workflow
  - Implement support for AWS Marketplace defect detection algorithm (classification and segmentation models)
  - Validate AWS Marketplace subscription status before starting training jobs
  - Algorithm ARN: arn:aws:sagemaker:us-east-1:865070037744:algorithm/computer-vision-defect-detection
  - Marketplace URL: https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6
  - Support both model types: 'classification' and 'segmentation' (or 'classification-robust', 'segmentation-robust')
  - Configure training with AugmentedManifestFile data source (S3DataType)
  - Set attribute names based on model type:
    - Classification: source-ref, anomaly-label-metadata, anomaly-label
    - Segmentation: source-ref, anomaly-label-metadata, anomaly-label, anomaly-mask-ref-metadata, anomaly-mask-ref
  - Use ml.g4dn.2xlarge instance type for GPU training (configurable)
  - Set MaxRuntimeInSeconds to 7200 (2 hours, configurable)
  - Enable network isolation for security (EnableNetworkIsolation=True)
  - Store training job metadata in DynamoDB
  - Reference: Steps 6-7 in DDA_SageMaker_Model_Training_and_Compilation.ipynb
  - _Requirements: 5.1, 5.2_

- [x] 5.3 Implement training monitoring and logs
  - Create Lambda function to fetch CloudWatch logs
  - Implement EventBridge integration for training job state changes
  - Update job status and metrics in DynamoDB
  - _Requirements: 5.3, 5.4_

- [x] 5.4 Build Step Functions training workflow
  - Create state machine for training orchestration
  - Implement AssumeUseCaseRole step
  - Implement StartSageMakerTraining step
  - Implement WaitForTrainingCompletion with EventBridge integration
  - Implement UpdateModelRegistry step
  - Add error handling and failure notifications
  - _Requirements: 15.1, 15.2, 15.3, 15.4_

- [ ] 5.5 Implement training API endpoints
  - Create GET /api/v1/training (list jobs)
  - Create POST /api/v1/training (start training)
  - Create GET /api/v1/training/{id} (get status)
  - Create GET /api/v1/training/{id}/logs (fetch logs)
  - _Requirements: 5.1, 5.3_

- [ ] 5.6 Implement alert notifications for training failures
  - Create SNS topic for training alerts
  - Add SNS publish logic to Step Functions failure handler
  - _Requirements: 5.5, 14.3_

- [ ] 5.7 Write unit tests for training pipeline
  - Test SageMaker job submission
  - Test log fetching logic
  - Test Step Functions workflow transitions
  - _Requirements: 5.1, 5.2, 5.3_

- [ ] 6. Implement model compilation and component publishing
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 15.1, 15.2_
  - _Note: Compilation workflow is based on DDA_SageMaker_Model_Training_and_Compilation.ipynb_
  - _Note: Component creation workflow is based on DDA_Greengrass_Component_Creator.ipynb (3 phases: artifact prep, directory setup, component creation)_

- [x] 6.1 Create DynamoDB Settings table and load compilation targets
  - Define Settings table schema
  - Create default compilation_targets configuration for x86_64, ARM64, and Jetson Xavier
  - Implement function to load targets from Settings table
  - Reference: Step 8 in DDA_SageMaker_Model_Training_and_Compilation.ipynb (compilation targets)
  - _Requirements: 6.2_

- [x] 6.2 Build compilation job submission
  - Create Lambda function to start SageMaker Neo compilation jobs following DDA notebook workflow
  - Extract and repackage trained model (mochi.pt) from model.tar.gz before compilation
  - Extract input_shape from mochi.json to build DataInputConfig: {"input_shape": [1, 3, height, width]}
  - Implement target selection from configuration with platform-specific compiler options:
    - **Jetson Xavier (ARM64 + GPU)**: Os=LINUX, Arch=ARM64, Accelerator=NVIDIA
      - CompilerOptions: cuda-ver=10.2, gpu-code=sm_72, trt-ver=8.2.1, max-workspace-size=2147483648, precision-mode=fp16, jetson-platform=xavier
    - **x86_64 CPU**: Os=LINUX, Arch=X86_64 (no accelerator)
    - **x86_64 + CUDA**: Os=LINUX, Arch=X86_64, Accelerator=NVIDIA
      - CompilerOptions: cuda-ver=10.2, gpu-code=sm_75, trt-ver=8.2.1, max-workspace-size=2147483648, precision-mode=fp16
    - **ARM64 CPU**: Os=LINUX, Arch=ARM64 (no accelerator)
  - Add parallel compilation support for multiple targets
  - Use Framework=PYTORCH, FrameworkVersion=1.8
  - Set MaxRuntimeInSeconds to 3600 (1 hour)
  - Monitor compilation status with polling (INPROGRESS, STARTING, COMPLETED, FAILED)
  - Reference: Step 8 in DDA_SageMaker_Model_Training_and_Compilation.ipynb
  - _Requirements: 6.1, 6.2_

- [x] 6.3 Implement component packaging logic
  - Create Lambda function to package compiled artifacts following DDA_Greengrass_Component_Creator.ipynb Phase 2
  - **Phase 1: Model Artifact Preparation (Steps 1.1-1.6)**
    - Download trained model artifacts (model.tar.gz) from S3
    - Extract model archive to access config.yaml and export_artifacts/manifest.json
    - Read image dimensions from config.yaml (dataset.image_width, dataset.image_height)
    - Read model metadata from manifest.json (model_graph, input_shape)
    - Locate PyTorch model file (.pt) in export_artifacts directory
    - Create DDA-compatible manifest with structure:
      ```json
      {
        "model_graph": <from original manifest>,
        "compilable_models": [{
          "filename": "<model>.pt",
          "data_input_config": {"input": <input_shape>},
          "framework": "PYTORCH"
        }],
        "dataset": {
          "image_width": <width>,
          "image_height": <height>
        }
      }
      ```
  - **Phase 2: Directory Structure Setup (Steps 2.1-2.6)**
    - Download compiled model artifacts from SageMaker Neo compilation output
    - Extract compiled model to model_artifacts/<model_name>/ directory
    - Copy DDA-compatible manifest.json to model_artifacts/ root
    - Create directory structure:
      ```
      model_artifacts/
        ├── manifest.json (DDA-compatible)
        └── <model_name>/
            └── <compiled model files>
      ```
    - Package complete structure as ZIP archive with unique UUID: <uuid>_greengrass_model_component.zip
    - Upload packaged ZIP to S3: s3://<bucket>/model_artifacts/model-<uuid>/<uuid>_greengrass_model_component.zip
  - Reference: DDA_Greengrass_Component_Creator.ipynb Phases 1-2
  - _Requirements: 6.4_

- [x] 6.4 Build component publishing to Greengrass
  - Create Lambda function to generate Greengrass component recipe following DDA_Greengrass_Component_Creator.ipynb Phase 3
  - **Phase 3: Component Creation (Steps 3.1-3.3)**
    - Validate component name format: must start with "model-" (e.g., model-defect-classifier)
    - Validate component version format: x.0.0 (e.g., 1.0.0, 2.0.0)
    - Determine target platform: aarch64 or amd64
    - Set platform-specific DDA LocalServer dependency:
      - aarch64 → aws.edgeml.dda.LocalServer.arm64
      - amd64 → aws.edgeml.dda.LocalServer.amd64
    - Generate Greengrass component recipe with structure:
      ```json
      {
        "RecipeFormatVersion": "2020-01-25",
        "ComponentName": "<component_name>",
        "ComponentVersion": "<version>",
        "ComponentType": "aws.greengrass.generic",
        "ComponentPublisher": "Amazon Lookout for Vision",
        "ComponentConfiguration": {
          "DefaultConfiguration": {
            "Autostart": false,
            "PYTHONPATH": "/usr/bin/python3.9",
            "ModelName": "<friendly_name>"
          }
        },
        "ComponentDependencies": {
          "<local_server_component>": {
            "VersionRequirement": "^1.0.0",
            "DependencyType": "HARD"
          }
        },
        "Manifests": [{
          "Platform": {"os": "linux", "architecture": "<platform>"},
          "Lifecycle": {
            "Startup": {
              "Script": "python3 /aws_dda/model_convertor.py --unarchived_model_path {artifacts:decompressedPath}/<model_path>/ --model_version <version> --model_name <component_name>",
              "Timeout": 900,
              "requiresPrivilege": true,
              "runWith": {"posixUser": "root"}
            },
            "Shutdown": {
              "Script": "python3 /aws_dda/convert_model_cleanup.py --model_name <component_name>",
              "Timeout": 900,
              "requiresPrivilege": true,
              "runWith": {"posixUser": "root"}
            }
          },
          "Artifacts": [{
            "Uri": "<s3_model_artifacts_uri>",
            "Digest": "",
            "Algorithm": "SHA-256",
            "Unarchive": "ZIP",
            "Permission": {"Read": "ALL", "Execute": "ALL"}
          }]
        }],
        "Lifecycle": {}
      }
      ```
    - Create component via Greengrass API: CreateComponentVersion(inlineRecipe=json.dumps(recipe))
    - Monitor component status until DEPLOYABLE (poll describe_component)
    - Implement cross-account component publication via assumed role
    - Store component ARN and version in DynamoDB Models table
  - Reference: DDA_Greengrass_Component_Creator.ipynb Phase 3 (Steps 3.1-3.3)
  - _Requirements: 6.5_

- [x] 6.5 Extend Step Functions workflow for compilation and publishing
  - Add ParallelCompilation step to training workflow
  - Add StartCompilation and WaitForCompilationCompletion steps
  - Add PackageComponent and PublishComponents steps
  - Implement automatic triggering after training completion
  - _Requirements: 15.1, 15.2, 6.3_

- [ ] 6.6 Implement compilation API endpoints
  - Create POST /api/v1/training/{id}/compile (trigger compilation)
  - Create GET /api/v1/compile/{id} (get status)
  - Create GET /api/v1/compile/{id}/logs (fetch logs)
  - Create POST /api/v1/components/publish (manual publish)
  - _Requirements: 6.1, 6.3_

- [ ] 6.7 Write unit tests for compilation and publishing
  - Test compilation job creation
  - Test component packaging logic
  - Test Greengrass publishing
  - _Requirements: 6.1, 6.4, 6.5_

- [ ] 7. Build model registry
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

- [x] 7.1 Create DynamoDB Models table
  - Define table schema with stage tracking and GSIs
  - Implement data access layer
  - _Requirements: 7.1_

- [ ] 7.2 Implement model registration logic
  - Create Lambda function to register models after training
  - Store model metadata, metrics, and component ARNs
  - Link to training job and dataset manifest
  - _Requirements: 7.1_

- [ ] 7.3 Build model promotion functionality
  - Create Lambda function to promote model stages
  - Implement validation to prevent deletion of deployed models
  - Track promotion history
  - _Requirements: 7.3, 7.5_

- [ ] 7.4 Implement model registry API endpoints
  - Create GET /api/v1/models (list with filtering)
  - Create GET /api/v1/models/{id} (get details)
  - Create PUT /api/v1/models/{id}/stage (promote stage)
  - _Requirements: 7.2, 7.3_

- [ ] 7.5 Write unit tests for model registry
  - Test model registration
  - Test stage promotion logic
  - Test deletion prevention
  - _Requirements: 7.1, 7.3, 7.5_


- [ ] 8. Implement deployment management
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 8.1 Create DynamoDB Deployments table
  - Define table schema with device status tracking and GSIs
  - Implement data access layer
  - _Requirements: 8.4_

- [ ] 8.2 Build deployment creation logic
  - Create Lambda function to create Greengrass deployments
  - Implement component and target device selection
  - Support rollout strategies (all-at-once, canary, percentage)
  - Assume cross-account role to call Greengrass API
  - _Requirements: 8.1, 8.2, 8.3_

- [ ] 8.3 Implement deployment monitoring
  - Create Lambda function to poll Greengrass deployment status
  - Update per-device status in DynamoDB
  - Implement EventBridge integration for deployment events
  - _Requirements: 8.4_

- [ ] 8.4 Build deployment rollback functionality
  - Create Lambda function to rollback deployments
  - Implement previous version tracking
  - Update deployment status on rollback
  - _Requirements: 8.4_

- [ ] 8.5 Implement deployment API endpoints
  - Create GET /api/v1/deployments (list deployments)
  - Create POST /api/v1/deployments (create deployment)
  - Create GET /api/v1/deployments/{id} (get status)
  - Create POST /api/v1/deployments/{id}/rollback (rollback)
  - _Requirements: 8.1, 8.4, 8.5_

- [ ] 8.6 Add deployment audit logging
  - Log deployment creation with user identity
  - Log rollback actions
  - Track deployment history per device
  - _Requirements: 8.5, 13.1, 13.2_

- [ ] 8.7 Write unit tests for deployment management
  - Test deployment creation logic
  - Test rollout strategy configurations
  - Test rollback functionality
  - _Requirements: 8.1, 8.2, 8.4_

- [ ] 9. Build device management system
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 9.1 Create DynamoDB Devices table
  - Define table schema with component tracking and GSIs
  - Implement data access layer with use case filtering
  - _Requirements: 9.1_

- [ ] 9.2 Implement device inventory sync
  - Create Lambda function to query IoT Thing registry
  - Fetch Thing Shadows for device status
  - Update Devices table with current state
  - Schedule periodic sync via EventBridge
  - _Requirements: 9.1, 9.2_

- [ ] 9.3 Build device health monitoring
  - Create Lambda function to process device heartbeats
  - Implement offline detection logic
  - Update device status and metrics in DynamoDB
  - _Requirements: 9.2, 9.4_

- [ ] 9.4 Implement device alert system
  - Create SNS topic for device alerts
  - Add logic to send alerts when devices go offline
  - Implement configurable alert thresholds
  - _Requirements: 9.4, 14.4_

- [ ] 9.5 Implement device inventory API endpoints
  - Create GET /api/v1/devices (list with filtering)
  - Create GET /api/v1/devices/{id} (get details)
  - Add pagination and search capabilities
  - _Requirements: 9.1, 9.3, 9.5_

- [ ] 9.6 Write unit tests for device management
  - Test device sync logic
  - Test offline detection
  - Test alert triggering
  - _Requirements: 9.1, 9.2, 9.4_

- [ ] 10. Implement device control and file management
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 10.1 Build IoT Jobs integration for device control
  - Create Lambda function to create IoT Jobs
  - Implement job templates for restart, reboot, diagnostics
  - Add job status tracking
  - _Requirements: 11.1, 11.2, 11.3_

- [ ] 10.2 Implement file browsing via Management Agent
  - Create Lambda function to proxy file browse requests to device
  - Send IoT Job to Management Agent for file listing
  - Parse and return file metadata
  - _Requirements: 10.1, 10.2_

- [ ] 10.3 Build file download functionality
  - Create Lambda function to request file from Management Agent
  - Implement secure file transfer via S3 or direct download
  - Add file size limits and validation
  - _Requirements: 10.3_

- [ ] 10.4 Implement log streaming
  - Create Lambda function to tail device logs via Management Agent
  - Support real-time log streaming via WebSocket
  - Add log filtering by keyword and time
  - _Requirements: 10.4, 10.5_

- [ ] 10.5 Implement device control API endpoints
  - Create POST /api/v1/devices/{id}/restart (restart Greengrass)
  - Create POST /api/v1/devices/{id}/reboot (reboot device)
  - Create GET /api/v1/devices/{id}/browse (list files)
  - Create GET /api/v1/devices/{id}/file (download file)
  - Create GET /api/v1/devices/{id}/logs (tail logs)
  - _Requirements: 11.1, 11.2, 10.1, 10.3, 10.4_

- [ ] 10.6 Write unit tests for device control
  - Test IoT Job creation
  - Test file browse request handling
  - Test log streaming logic
  - _Requirements: 10.1, 10.2, 10.4_

- [ ] 11. Implement device configuration management
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5_

- [ ] 11.1 Build Thing Shadow update logic
  - Create Lambda function to update device configuration via Thing Shadow
  - Implement configuration schema validation
  - Track desired vs reported state
  - _Requirements: 16.1, 16.2_

- [ ] 11.2 Implement configuration history tracking
  - Store configuration changes in DynamoDB
  - Track user identity and timestamp for each change
  - _Requirements: 16.4_

- [ ] 11.3 Build configuration API endpoints
  - Create PUT /api/v1/devices/{id}/config (update configuration)
  - Create GET /api/v1/devices/{id}/config (get current config)
  - Create GET /api/v1/devices/{id}/config/history (get change history)
  - _Requirements: 16.1, 16.3, 16.4_

- [ ] 11.4 Write unit tests for configuration management
  - Test Thing Shadow updates
  - Test configuration validation
  - Test history tracking
  - _Requirements: 16.1, 16.2, 16.4_

- [ ] 12. Build audit logging and monitoring
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 14.1, 14.2, 14.3, 14.4, 14.5_

- [ ] 12.1 Create DynamoDB AuditLog table
  - Define table schema with user and action tracking
  - Implement GSIs for querying by user, use case, action type
  - _Requirements: 13.1, 13.2_

- [ ] 12.2 Implement audit logging middleware
  - Create Lambda layer for audit logging utilities
  - Add middleware to log all API actions
  - Capture user identity, action, resource, and result
  - Flag super user actions
  - _Requirements: 13.1, 13.2, 3.4_

- [ ] 12.3 Build audit log query API
  - Create GET /api/v1/audit (query logs with filtering)
  - Support filtering by user, action type, time range, use case
  - Implement pagination for large result sets
  - _Requirements: 13.3_

- [ ] 12.4 Create CloudWatch dashboards
  - Define dashboard for Portal health metrics
  - Define dashboard for jobs (training, labeling, deployments)
  - Define dashboard for device health
  - Deploy dashboards via CDK
  - _Requirements: 14.1, 14.2_

- [ ] 12.5 Implement CloudWatch alarms
  - Create alarms for API errors, Lambda failures, DynamoDB throttling
  - Create alarms for training job failures
  - Create alarms for device offline events
  - Create alarms for deployment failures
  - Configure SNS notifications for alarms
  - _Requirements: 14.3, 14.4, 14.5_

- [ ] 12.6 Set up X-Ray tracing
  - Enable X-Ray for API Gateway
  - Enable X-Ray for Lambda functions
  - Enable X-Ray for Step Functions
  - Add custom segments for cross-account calls
  - _Requirements: 14.1_

- [ ] 12.7 Write unit tests for audit logging
  - Test audit event creation
  - Test log query filtering
  - Test super user flag logic
  - _Requirements: 13.1, 13.2_

- [ ] 13. Implement cost tracking
  - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

- [ ] 13.1 Add resource tagging logic
  - Implement tagging for SageMaker training jobs
  - Implement tagging for S3 objects
  - Implement tagging for Greengrass components
  - Tag all resources with use case ID and cost center
  - _Requirements: 18.1_

- [ ] 13.2 Build cost estimation functionality
  - Create Lambda function to estimate training job costs
  - Calculate costs based on instance type and duration
  - Display estimates in UI before job submission
  - _Requirements: 18.3_

- [ ] 13.3 Implement quota enforcement
  - Add validation for instance type limits per use case
  - Implement concurrent job limits
  - Store quota configuration in Settings table
  - _Requirements: 18.4_

- [ ] 13.4 Create cost tracking API endpoints
  - Create GET /api/v1/usecases/{id}/costs (get cost links)
  - Create GET /api/v1/training/{id}/cost-estimate (estimate cost)
  - _Requirements: 18.2, 18.3_

- [ ] 13.5 Write unit tests for cost tracking
  - Test cost estimation calculations
  - Test quota validation
  - Test resource tagging
  - _Requirements: 18.1, 18.3, 18.4_

- [ ] 14. Build settings and configuration management
  - _Requirements: 6.2_

- [ ] 14.1 Implement compilation targets configuration UI
  - Create settings page in React app
  - Build form to add/edit/delete compilation targets
  - Implement JSON schema validation
  - _Requirements: 6.2_

- [ ] 14.2 Build settings API endpoints
  - Create GET /api/v1/settings/targets (get compilation targets)
  - Create PUT /api/v1/settings/targets (update targets)
  - Add audit logging for settings changes
  - _Requirements: 6.2_

- [ ] 14.3 Write unit tests for settings management
  - Test settings CRUD operations
  - Test JSON validation
  - Test audit logging
  - _Requirements: 6.2_

- [ ] 15. Build React frontend application
  - _Requirements: 1.1, 2.4, 4.1, 5.3, 7.2, 8.4, 9.1, 10.1, 10.4, 16.1, 17.1, 17.2, 17.3, 17.4, 17.5_

- [ ] 15.1 Set up React project structure
  - Initialize React app with TypeScript
  - Configure CloudScape Design System
  - Set up React Query for data fetching
  - Configure routing with React Router
  - Set up build configuration for CloudFront deployment
  - _Requirements: 17.1_

- [x] 15.2 Implement authentication UI
  - Create login page with SSO redirect
  - Integrate AWS Amplify for Cognito authentication
  - Implement token storage and refresh logic
  - Create protected route wrapper
  - _Requirements: 1.1, 1.4_

- [x] 15.3 Build dashboard page
  - Create dashboard layout with metrics cards
  - Display use case count, online devices, active jobs
  - Show recent events timeline
  - Add use case selector component
  - _Requirements: 17.1_

- [x] 15.4 Implement use case management UI
  - Create use case list page with table
  - Build use case creation form
  - Build use case edit form
  - Add delete confirmation modal
  - _Requirements: 2.4_

- [x] 15.5 Build labeling workflow UI
  - Create dataset browser page
  - Build labeling job creation wizard (multi-step form)
  - Create labeling job list page with progress indicators
  - Build job detail page with manifest download
  - _Requirements: 4.1, 4.4, 4.5_

- [x] 15.6 Implement training workflow UI
  - Create training job list page
  - Build training wizard (dataset selection, algorithm, hyperparameters, compute)
  - Create training job detail page with logs viewer
  - Add real-time progress updates via WebSocket or polling
  - _Requirements: 5.3, 5.4_

- [ ] 15.7 Build model registry UI
  - Create model list page with filtering
  - Build model detail page with metrics display
  - Implement stage promotion UI with confirmation
  - Show deployed devices for each model
  - _Requirements: 7.2, 7.3_

- [ ] 15.8 Implement deployment management UI
  - Create deployment list page
  - Build deployment creation form (component selection, targets, rollout strategy)
  - Create deployment detail page with per-device status
  - Add rollback button with confirmation
  - _Requirements: 8.4, 8.5_

- [ ] 15.9 Build device management UI
  - Create device inventory page with filtering and search
  - Build device detail page with tabs (overview, components, logs, files)
  - Implement log viewer with tail and filtering
  - Build file browser with download capability
  - Add device action buttons (restart, reboot) with confirmation modals
  - _Requirements: 9.1, 9.5, 10.1, 10.3, 10.4, 11.1_

- [x] 15.10 Implement settings and admin UI
  - Create settings page for compilation targets configuration
  - Build audit log viewer with filtering
  - Add role ARN configuration forms
  - _Requirements: 6.2, 13.3_

- [ ] 15.11 Implement WebSocket integration for real-time updates
  - Set up WebSocket client connection
  - Subscribe to job status updates
  - Subscribe to device status updates
  - Update UI components on received messages
  - _Requirements: 17.2_

- [ ] 15.12 Add loading states and error handling
  - Implement loading spinners for async operations
  - Create error boundary components
  - Add toast notifications for success/error messages
  - Implement retry logic for failed requests
  - _Requirements: 17.3_

- [ ] 15.13 Implement pagination and performance optimizations
  - Add pagination to all list pages
  - Implement virtual scrolling for large lists
  - Add React Query caching configuration
  - Optimize bundle size with code splitting
  - _Requirements: 17.4_

- [x] 15.14 Build confirmation modals for destructive actions
  - Create reusable confirmation modal component
  - Add confirmations for delete, restart, reboot, rollback actions
  - _Requirements: 17.5_

- [ ] 15.15 Write unit tests for React components
  - Test authentication flow
  - Test form validation
  - Test user interactions
  - Test error handling
  - _Requirements: 1.1, 17.3_

- [ ] 16. Implement WebSocket API for real-time updates
  - _Requirements: 17.2_

- [ ] 16.1 Create WebSocket API Gateway
  - Define WebSocket API with connect, disconnect, and message routes
  - Configure JWT authorizer for WebSocket connections
  - _Requirements: 17.2_

- [ ] 16.2 Build WebSocket connection management
  - Create Lambda for $connect route to validate and store connection
  - Create Lambda for $disconnect route to clean up connection
  - Store connection IDs in DynamoDB with user and use case mapping
  - _Requirements: 17.2_

- [ ] 16.3 Implement message broadcasting
  - Create utility function to send messages to specific connections
  - Add logic to broadcast job status updates to subscribed users
  - Add logic to broadcast device status updates
  - _Requirements: 17.2_

- [ ] 16.4 Integrate WebSocket notifications into workflows
  - Add WebSocket broadcast to training Step Functions
  - Add WebSocket broadcast to deployment monitoring
  - Add WebSocket broadcast to device status updates
  - _Requirements: 17.2_

- [ ] 16.5 Write unit tests for WebSocket functionality
  - Test connection management
  - Test message broadcasting
  - Test subscription filtering
  - _Requirements: 17.2_

- [ ] 17. Deploy infrastructure with AWS CDK
  - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_

- [ ] 17.1 Create CDK stacks for core infrastructure
  - Create Network stack (VPC, security groups if needed)
  - Create Auth stack (Cognito User Pool, Identity Pool)
  - Create Storage stack (DynamoDB tables, S3 buckets)
  - Create Compute stack (Lambda functions, API Gateway, Step Functions)
  - Create Monitoring stack (CloudWatch dashboards, alarms, SNS topics)
  - Create Frontend stack (CloudFront, S3 for static hosting, WAF)
  - _Requirements: 19.1, 19.2_

- [ ] 17.2 Create cross-account IAM role templates
  - Generate CloudFormation template for PortalAccessRole in UseCase Accounts
  - Generate CloudFormation template for GroundTruthExecutionRole in UseCase Accounts
  - Document ExternalID generation and usage
  - _Requirements: 19.3, 12.1, 12.2_

- [ ] 17.3 Implement CDK deployment scripts
  - Create deployment script for all stacks
  - Add environment-specific configuration (dev, staging, prod)
  - Implement stack dependency management
  - _Requirements: 19.1_

- [ ] 17.4 Create deployment documentation
  - Write step-by-step deployment guide
  - Document prerequisites and AWS account setup
  - Create runbook for onboarding new use case accounts
  - Document SSO integration steps
  - _Requirements: 19.4_

- [ ] 18. Build device-side Management Agent
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 11.1_

- [ ] 18.1 Implement Management Agent core
  - Create Python service with IoT Core MQTT client
  - Implement certificate-based authentication
  - Add configuration file parsing
  - Implement command routing logic
  - _Requirements: 10.1, 11.1_

- [ ] 18.2 Build file browsing functionality
  - Implement file listing with metadata (size, mtime, permissions)
  - Add directory whitelist validation
  - Implement file size limits
  - _Requirements: 10.1, 10.2_

- [ ] 18.3 Implement file download capability
  - Add file streaming over MQTT or S3 upload
  - Implement chunked transfer for large files
  - Add authentication and authorization checks
  - _Requirements: 10.3_

- [ ] 18.4 Build log streaming
  - Implement log file tailing
  - Add log filtering by keyword and time
  - Stream logs over MQTT
  - _Requirements: 10.4_

- [ ] 18.5 Implement control commands
  - Add Greengrass restart command
  - Add device reboot command (with safety checks)
  - Add diagnostic command execution
  - Add S3 export trigger
  - _Requirements: 11.1, 11.2_

- [ ] 18.6 Add rate limiting and security
  - Implement request rate limiting
  - Add command authorization based on IoT policies
  - Log all executed commands
  - _Requirements: 10.1_

- [ ] 18.7 Create systemd service configuration
  - Write systemd unit file for agent
  - Add auto-restart on failure
  - Configure logging to system journal
  - _Requirements: 10.1_

- [ ] 18.8 Create agent installation script
  - Write installation script for edge devices
  - Include dependency installation
  - Configure IoT certificates
  - Start and enable systemd service
  - _Requirements: 10.1_

- [ ] 18.9 Write unit tests for Management Agent
  - Test file browsing logic
  - Test command execution
  - Test log streaming
  - Test rate limiting
  - _Requirements: 10.1, 10.2, 10.4_

- [ ] 19. Set up CI/CD pipeline
  - _Requirements: 19.1, 19.5_

- [ ] 19.1 Create CI/CD configuration
  - Set up GitHub Actions or AWS CodePipeline configuration
  - Define build stage (install, lint, test, build)
  - Define test stage (unit tests, integration tests)
  - Define deploy stages (staging, production)
  - _Requirements: 19.5_

- [ ] 19.2 Implement automated testing in pipeline
  - Run backend unit tests
  - Run frontend unit tests
  - Generate coverage reports
  - Fail build on test failures
  - _Requirements: 19.5_

- [ ] 19.3 Add security scanning
  - Integrate dependency vulnerability scanning
  - Add SAST (static analysis) scanning
  - Scan Docker images if using containers
  - _Requirements: 19.5_

- [ ] 19.4 Configure deployment automation
  - Automate CDK deployment to staging on merge to main
  - Require manual approval for production deployment
  - Implement blue/green deployment for Lambda
  - Add CloudFront cache invalidation step
  - _Requirements: 19.5_

- [ ] 19.5 Set up monitoring for pipeline
  - Add notifications for build failures
  - Track deployment success rate
  - Monitor deployment duration
  - _Requirements: 19.5_

- [ ] 20. Create integration tests and demo setup
  - _Requirements: All requirements_

- [ ] 20.1 Write integration tests
  - Test end-to-end labeling workflow
  - Test end-to-end training-to-deployment pipeline
  - Test cross-account access scenarios
  - Test device management workflows
  - _Requirements: All requirements_

- [ ] 20.2 Create demo data and scripts
  - Create script to populate sample use cases
  - Generate sample S3 datasets with images
  - Create sample devices in IoT registry
  - Populate sample training jobs and models
  - _Requirements: All requirements_

- [ ] 20.3 Build demo walkthrough documentation
  - Document demo scenario steps
  - Create screenshots and videos
  - Write troubleshooting guide
  - _Requirements: All requirements_
