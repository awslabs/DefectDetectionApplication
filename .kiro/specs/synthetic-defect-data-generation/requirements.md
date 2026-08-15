# Requirements Document

## Introduction

This feature adds synthetic defect data generation to the edge-cv-portal. Data scientists select an image generation model, tune prompt templates scoped per object type and defect type, and generate synthetic defect images from either existing defect images or normal (non-defective) source images. A thumbnail preview review workspace lets the user iterate on prompts and regenerate previews, then choose how many variations to produce and which generated images to keep. On approval, the portal auto-annotates the approved images, appends them to the dataset's Ground Truth style data manifest, and enables immediate retraining through the existing SageMaker training plumbing.

The generation concepts (variation counts, prompt-driven randomization, structured outputs with annotations) take inspiration from NVIDIA Isaac Sim / Omniverse Replicator synthetic data workflows, but this is a cloud/portal implementation using hosted image generation models. There is no Isaac Sim integration and no edge/device component.

**Delivery constraint (not a functional requirement):** implementation work lands on a new git branch based off `integration/all-specs`. This is a portal-only feature; no changes are made to edge or device code.

## Glossary

- **Portal**: The edge-cv-portal web application (React frontend and Lambda backend) used to manage datasets, training, and deployments.
- **Synthetic_Data_Generator**: The portal subsystem introduced by this feature that produces synthetic defect images from source images using image generation models.
- **Generation_Model**: An image generation or inpainting model (for example, an Amazon Bedrock image model) that the Synthetic_Data_Generator can invoke to produce synthetic images.
- **Model_Catalog**: The list of Generation_Models available for selection in the Portal, including each model's identifier, display name, and capability flags (for example, inpainting support).
- **Prompt_Template**: A stored, editable text template that guides a Generation_Model, scoped to an Object_Type and Defect_Type pair, with placeholder variables resolved at generation time.
- **Object_Type**: A category of inspected part or product represented in a dataset (for example, "metal casting" or "PCB").
- **Defect_Type**: A category of defect to synthesize (for example, "scratch", "dent", "solder bridge").
- **Source_Image**: An image selected from an existing dataset in the Use_Case data bucket that serves as the base for synthetic generation. A Source_Image is either a Defect_Image or a Normal_Image.
- **Defect_Image**: A Source_Image that already contains one or more defects.
- **Normal_Image**: A Source_Image that contains no defects.
- **Generation_Session**: A persisted unit of work grouping the selected Generation_Model, Prompt_Template values, Source_Images, generation parameters, Preview_Images, and approval state.
- **Preview_Image**: A generated image rendered as a thumbnail in the review workspace, pending user approval or rejection.
- **Variation**: One distinct generated output image produced from a Source_Image and prompt combination; the Variation_Count is the number of Variations requested per Source_Image.
- **Auto_Annotator**: The portal subsystem that produces annotations (class labels and bounding boxes for the injected defect) for approved synthetic images without manual labeling.
- **Data_Manifest**: The Ground Truth style augmented manifest file (JSON Lines, one record per image with source-ref and label attributes) referenced by training jobs as dataset_manifest_s3.
- **Training_Subsystem**: The existing portal training plumbing that creates SageMaker training jobs from a Data_Manifest using the Use_Case cross-account SageMaker client and execution role.
- **Use_Case**: An onboarded tenant in the Portal with its own AWS account linkage, data bucket, and per-user role assignments.
- **Data_Scientist_Access**: Authorization satisfied by the DataScientist, UseCaseAdmin, or PortalAdmin role for the target Use_Case, as evaluated by the existing portal RBAC checks.

## Requirements

### Requirement 1: Generation Model Selection

**User Story:** As a data scientist, I want to select which image generation model to use for synthetic data generation, so that I can pick the model best suited to my object and defect types.

#### Acceptance Criteria

1. WHEN a user opens the synthetic data generation workspace, THE Portal SHALL display the Model_Catalog with each Generation_Model's display name and capability flags.
2. WHEN a user selects a Generation_Model from the Model_Catalog, THE Synthetic_Data_Generator SHALL use the selected Generation_Model for all subsequent generation requests in the Generation_Session.
3. IF the Model_Catalog contains no available Generation_Models, THEN THE Portal SHALL display a message identifying the configuration needed to enable at least one Generation_Model.
4. IF a generation request to the selected Generation_Model fails, THEN THE Synthetic_Data_Generator SHALL record the failure reason on the Generation_Session and display the failure reason to the user.

### Requirement 2: Prompt Templates per Object Type and Defect Type

**User Story:** As a data scientist, I want adjustable prompt templates scoped to each object type and defect type, so that generation prompts capture domain-specific defect appearance without rewriting prompts from scratch.

#### Acceptance Criteria

1. THE Portal SHALL store Prompt_Templates keyed by Use_Case, Object_Type, and Defect_Type.
2. WHEN a user selects an Object_Type and Defect_Type for which a Prompt_Template exists, THE Portal SHALL load that Prompt_Template into the prompt editor.
3. WHEN a user selects an Object_Type and Defect_Type for which no Prompt_Template exists, THE Portal SHALL load a default Prompt_Template containing placeholder variables for the Object_Type and Defect_Type.
4. WHEN a user edits a Prompt_Template and saves it, THE Portal SHALL persist the edited Prompt_Template for that Use_Case, Object_Type, and Defect_Type.
5. WHEN the Synthetic_Data_Generator submits a generation request, THE Synthetic_Data_Generator SHALL resolve all placeholder variables in the Prompt_Template before invoking the Generation_Model.
6. IF a Prompt_Template contains an unresolvable placeholder variable at generation time, THEN THE Synthetic_Data_Generator SHALL reject the generation request and display the unresolved variable name to the user.

### Requirement 3: Source Image Selection

**User Story:** As a data scientist, I want to generate synthetic data from either existing defect images or normal images in my datasets, so that I can expand coverage of rare defects regardless of what source data I have.

#### Acceptance Criteria

1. WHEN a user creates a Generation_Session, THE Portal SHALL list datasets from the Use_Case data bucket using the existing dataset discovery so the user can browse and select Source_Images.
2. WHEN a user selects Source_Images, THE Portal SHALL require the user to classify the selection as Defect_Images or Normal_Images.
3. WHEN a user classifies the selection as Normal_Images, THE Portal SHALL require the user to specify the Defect_Type to synthesize before generation can start.
4. WHEN a user classifies the selection as Defect_Images, THE Portal SHALL allow the user to specify the Defect_Type present in the Defect_Images.
5. THE Portal SHALL display thumbnail previews of the selected Source_Images using presigned URLs before generation starts.
6. IF a user starts generation with zero Source_Images selected, THEN THE Portal SHALL reject the request and display a message stating that at least one Source_Image is required.

### Requirement 4: Variation Generation Controls

**User Story:** As a data scientist, I want to control how many variations are generated per source image, so that I can balance dataset diversity against generation time and cost.

#### Acceptance Criteria

1. WHEN a user configures a Generation_Session, THE Portal SHALL provide a Variation_Count input constrained to an integer between 1 and 20 inclusive.
2. WHEN a user starts generation, THE Synthetic_Data_Generator SHALL produce the requested Variation_Count of Variations for each Source_Image in the Generation_Session.
3. WHERE the selected Generation_Model supports randomization parameters (for example, seed or guidance strength), THE Portal SHALL expose those parameters as adjustable generation controls with model-appropriate defaults.
4. IF a user submits a Variation_Count outside the range of 1 to 20, THEN THE Portal SHALL reject the input and display the valid range.
5. WHEN generation of an individual Variation fails, THE Synthetic_Data_Generator SHALL continue generating the remaining Variations and record the per-Variation failure on the Generation_Session.

### Requirement 5: Thumbnail Preview and Iterative Prompt Correction

**User Story:** As a data scientist, I want a thumbnail preview review process where I can correct the prompt and see updated results quickly, so that I can converge on realistic synthetic defects without waiting for full batch runs.

#### Acceptance Criteria

1. WHEN generation starts, THE Portal SHALL display a progress indicator for the Generation_Session within 2 seconds of the user starting generation.
2. WHEN a Variation completes generation, THE Portal SHALL display that Variation as a thumbnail Preview_Image in the review workspace without requiring a page reload.
3. WHEN a user edits the prompt in the review workspace and requests regeneration, THE Synthetic_Data_Generator SHALL submit the regeneration request using the edited prompt within 2 seconds of the user's request.
4. WHEN regenerated Preview_Images complete, THE Portal SHALL replace or append the corresponding thumbnails in the review workspace so the user can compare results against the prior prompt.
5. WHEN a user selects a Preview_Image thumbnail, THE Portal SHALL display a full-size view of that Preview_Image.
6. THE Portal SHALL retain the prompt text used to produce each Preview_Image and display that prompt text when the user inspects a Preview_Image.

### Requirement 6: Preview Approval and Inclusion Selection

**User Story:** As a data scientist, I want to choose which generated images to include in the dataset, so that only realistic synthetic images enter training data.

#### Acceptance Criteria

1. THE Portal SHALL provide per-Preview_Image controls to mark each Preview_Image as approved or rejected.
2. THE Portal SHALL provide a control to approve or reject all Preview_Images in the Generation_Session at once.
3. WHEN a user confirms approval of the Generation_Session, THE Synthetic_Data_Generator SHALL include only the approved Preview_Images in the dataset integration workflow defined in Requirement 7.
4. WHEN a user confirms approval, THE Portal SHALL display a summary showing the count of approved images, the target dataset, and the Defect_Type before executing the dataset integration workflow.
5. IF a user confirms approval with zero approved Preview_Images, THEN THE Portal SHALL reject the confirmation and display a message stating that at least one approved image is required.
6. WHEN a Generation_Session is approved, THE Synthetic_Data_Generator SHALL delete or mark as rejected all non-approved Preview_Images so rejected images are excluded from the dataset and the Data_Manifest.

### Requirement 7: Auto-Annotation and Manifest Integration

**User Story:** As a data scientist, I want approved synthetic images automatically annotated and added to the dataset manifest, so that I can retrain quickly without a manual labeling pass.

#### Acceptance Criteria

1. WHEN a Generation_Session is approved, THE Auto_Annotator SHALL produce an annotation for each approved image containing the Defect_Type class label and a bounding box localizing the injected defect region.
2. WHERE the generation method constrains the defect region (for example, an inpainting mask or edit region), THE Auto_Annotator SHALL derive the bounding box from that region.
3. WHEN annotations are produced, THE Synthetic_Data_Generator SHALL upload each approved image to the Use_Case data bucket under the target dataset prefix.
4. WHEN approved images are uploaded, THE Synthetic_Data_Generator SHALL append one record per approved image to the target Data_Manifest in the Ground Truth augmented manifest format accepted by the Training_Subsystem, including a metadata attribute identifying the record as synthetic.
5. WHEN appending to the Data_Manifest, THE Synthetic_Data_Generator SHALL preserve all existing manifest records unchanged.
6. WHEN manifest integration completes, THE Portal SHALL display a confirmation including the updated Data_Manifest S3 URI and the count of appended records.
7. IF the upload of an image or the manifest append fails, THEN THE Synthetic_Data_Generator SHALL leave the target Data_Manifest in its pre-integration state, record the failure on the Generation_Session, and display the failure reason to the user.
8. FOR ALL manifest records written by the Synthetic_Data_Generator, parsing the updated Data_Manifest with the Training_Subsystem's existing manifest validation SHALL succeed (round-trip property).

### Requirement 8: Retraining Turnaround

**User Story:** As a data scientist, I want to start a SageMaker training job on the updated dataset directly from the approval flow, so that the turnaround from synthetic data creation to retraining is fast.

#### Acceptance Criteria

1. WHEN manifest integration completes, THE Portal SHALL offer the user an action to create a training job pre-populated with the updated Data_Manifest S3 URI.
2. WHEN the user invokes the retraining action, THE Portal SHALL create the training job through the existing Training_Subsystem using the Use_Case cross-account SageMaker client and execution role.
3. WHEN the training job is created, THE Portal SHALL record the originating Generation_Session identifier on the training job record.
4. IF training job creation fails, THEN THE Portal SHALL display the failure reason and retain the updated Data_Manifest so the user can retry.

### Requirement 9: Access Control

**User Story:** As a portal administrator, I want synthetic data generation restricted to data scientists and higher roles, so that dataset contents and training data are only changed by authorized users.

#### Acceptance Criteria

1. WHEN a user invokes any Synthetic_Data_Generator API operation, THE Portal SHALL verify the user holds Data_Scientist_Access for the target Use_Case using the existing RBAC checks before executing the operation.
2. IF a user without Data_Scientist_Access invokes a Synthetic_Data_Generator API operation, THEN THE Portal SHALL return a 403 response and log an audit event recording the denied attempt.
3. WHILE a user without Data_Scientist_Access is signed in, THE Portal frontend SHALL hide the synthetic data generation workspace entry points from navigation.
4. WHEN a Generation_Session is created, approved, or integrated into a Data_Manifest, THE Portal SHALL log an audit event recording the user, Use_Case, and Generation_Session identifier.

### Requirement 10: Generation Session Persistence

**User Story:** As a data scientist, I want my generation sessions persisted, so that I can leave and resume review work and trace which synthetic images came from which prompts and models.

#### Acceptance Criteria

1. WHEN a Generation_Session is created, THE Portal SHALL persist the Generation_Session with its Use_Case, selected Generation_Model, Object_Type, Defect_Type, Prompt_Template text, Source_Image references, and generation parameters.
2. WHEN a user returns to an existing Generation_Session, THE Portal SHALL restore the session state including generated Preview_Images and their approval marks.
3. THE Portal SHALL record on each approved image's manifest record or session record the Generation_Model identifier and the resolved prompt text used to produce that image.
4. WHEN a user lists Generation_Sessions for a Use_Case, THE Portal SHALL display each session's status (in progress, awaiting review, approved, integrated, or failed) and creation time.
