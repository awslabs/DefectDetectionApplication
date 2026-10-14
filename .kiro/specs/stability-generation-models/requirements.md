# Requirements Document

## Introduction

This feature makes the synthetic defect data generation workspace (spec: `synthetic-defect-data-generation`) work again by adding support for the Stability AI inpainting model on Bedrock. This is no longer an addition alongside Amazon Nova Canvas — it is the only path to a working generation pipeline on the portal account.

Verified current state on account 164152369890, us-east-1 (hit live in session e8a08d11; the S3 permission and dataset-discovery fixes from the companion bugfix specs are already deployed):

- **Nova Canvas is unusable.** `amazon.nova-canvas-v1:0` has lifecycle status LEGACY in us-east-1, and Bedrock rejects `InvokeModel` with a ResourceNotFoundException stating the model is "marked by provider as Legacy and you have not been actively using the model in the last 30 days". The current pipeline therefore has zero usable models: every generation task fails.
- **Every ACTIVE image model in us-east-1 is a Stability task-specific model**: stable-image-inpaint, outpaint, erase-object, search-replace, search-recolor, control-sketch, control-structure, style-guide, style-transfer, remove-background, and three upscalers. No general text-to-image model and no image-variation model is ACTIVE in-region.
- **Invocation requires inference profiles.** Invoking `stability.stable-image-inpaint-v1:0` directly fails with "on-demand throughput isn't supported. Retry with the ID or ARN of an inference profile". Invoking the inference profile `us.stability.stable-image-inpaint-v1:0` passes the access gate (it fails only on request schema validation: "'image' is required"), proving the account can invoke it with no model-access grant needed. Inference profiles exist as `us.` + model id for all 13 Stability models, and the handler role's existing Bedrock grant already covers inference-profile ARNs — no IAM work is needed.
- `bedrock:ListFoundationModels` returns each model's lifecycle status (`modelLifecycle.status`: ACTIVE or LEGACY), so the availability filter can distinguish live models from dead ones.

Scope decisions:

- **v1 model scope**: exactly one new model — `stability.stable-image-inpaint-v1:0`, invoked via the inference profile `us.stability.stable-image-inpaint-v1:0`. Stability's text-to-image models (`stability.sd3-5-large-v1:0`, `stability.stable-image-core-v1:1`, `stability.stable-image-ultra-v1:1`) are offered only in us-west-2 and remain out of scope.
- **Other Stability editing models are out of scope in v1.** The remaining ACTIVE task-specific models (notably `stability.stable-outpaint-v1:0` and `stability.stable-image-search-replace-v1:0`) are candidate future additions but are not added to the Model_Catalog in this feature; the per-Provider adapter and Invocation_Identifier mechanism this feature introduces is what would make adding them later straightforward.
- **Region scope (v1)**: generation invokes Bedrock only in the portal region. No cross-region inference paths are introduced.
- **Nova Canvas catalog entry stays.** The `amazon.nova-canvas-v1:0` entry remains in the Model_Catalog and the amazon adapter path stays byte-identical, so Nova Canvas returns to the dropdown automatically if Amazon reactivates it or the account regains active-usage status. The Availability_Filter (not catalog removal) is what hides it.
- **Titan is out of scope**: `amazon.titan-image-generator-v2:0` remains in the catalog but is not offered in-region; the availability filter already excludes it.

## Glossary

- **Portal**: The edge-cv-portal web application (React frontend and Lambda backend).
- **Portal_Region**: The single AWS region in which the Portal's backend runs and invokes Bedrock (currently us-east-1).
- **Synthetic_Data_Generator**: The portal subsystem that produces synthetic defect images from source images using image generation models (introduced by the `synthetic-defect-data-generation` spec).
- **Generation_Model**: An image generation or editing model invokable through Bedrock `InvokeModel` that the Synthetic_Data_Generator can use.
- **Model_Catalog**: The static in-code list (`MODEL_CATALOG` in `synthetic_core.py`) of Generation_Models, each with a model identifier, display name, capability flags, `max_images_per_call`, and `randomization_defaults`.
- **Invocation_Identifier**: The identifier a Model_Catalog entry designates for Bedrock `InvokeModel` calls. For models requiring inference-profile invocation this is the Inference_Profile id; for models supporting direct on-demand invocation it is the bare model id.
- **Inference_Profile**: The Bedrock cross-region inference profile through which a model must be invoked when on-demand throughput on the bare model id is not supported; for Stability models in us-east-1 the profile id is `us.` + model id (for example `us.stability.stable-image-inpaint-v1:0`).
- **Lifecycle_Status**: The `modelLifecycle.status` value (ACTIVE or LEGACY) returned by `bedrock:ListFoundationModels` for each foundation model.
- **Availability_Filter**: The logic (`filter_available_models` intersected with `bedrock:ListFoundationModels`, IMAGE output modality, in the Portal_Region) that determines which Model_Catalog entries appear in the model dropdown.
- **Provider**: The vendor family of a Generation_Model, identified by the model id prefix (`amazon.` or `stability.`), which determines the Bedrock request and response JSON schema.
- **Request_Adapter**: The per-Provider logic that maps a generation task (source image, mask input, resolved prompt, seed, randomization parameters) to the Provider's Bedrock `InvokeModel` request body and extracts the generated image from the Provider's response body.
- **Stability_Inpaint_Model**: `stability.stable-image-inpaint-v1:0`, the Stability task-specific inpainting model that is ACTIVE in us-east-1, invoked via its Inference_Profile.
- **Generation_Method**: The operation used for one generation task: inpainting (primary path, used for Normal_Image sources when supported) or image variation (fallback).
- **Mask_Image**: A binary mask image (PNG) delimiting the region to inpaint, required by Stability inpainting; distinct from Amazon's text `maskPrompt`.
- **Mask_Region**: The rectangular region (left, top, width, height) recorded on a Preview_Image when generation constrained the defect region; the Auto_Annotator derives bounding boxes from it.
- **Auto_Annotator**: The portal subsystem that produces annotations (class labels and bounding boxes) for approved synthetic images without manual labeling.
- **Normal_Image**: A source image classified as defect-free; the inpainting path injects a synthetic defect into a Normal_Image.
- **Defect_Image**: A source image classified as already containing a defect; the image-variation path (unsupported by the Stability_Inpaint_Model) produces variations of a Defect_Image.
- **Data_Manifest**: The Ground Truth style JSON Lines manifest into which approved synthetic images are integrated for training.
- **Generation_Session**: The persisted unit of work grouping model selection, prompts, source images, generation plan, and preview state.
- **Preview_Image**: A generated image record in a Generation_Session, carrying the generation model id, per-task seed, resolved prompt, and status.
- **Task_Seed**: The deterministic per-task seed produced by `derive_task_seed(base_seed, task_index)`, with values in the range 0 to 858,993,459.

## Requirements

### Requirement 1: Stability Inpaint Model in the Model_Catalog

**User Story:** As a data scientist, I want the Stability inpainting model to appear in the generation model dropdown, so that I have a working model for synthetic defect generation now that Nova Canvas is unusable.

#### Acceptance Criteria

1. THE Model_Catalog SHALL contain an entry for the Stability_Inpaint_Model with capability flags declaring inpainting support as true, seed support as true, text-to-image support as false, image-variation support as false, and cfg_scale support as false.
2. THE Model_Catalog entry for the Stability_Inpaint_Model SHALL carry the Invocation_Identifier `us.stability.stable-image-inpaint-v1:0` distinct from the model id `stability.stable-image-inpaint-v1:0`.
3. WHEN `GET /synthetic/models` is invoked and the Availability_Filter admits the Stability_Inpaint_Model, THE Portal SHALL include the Stability_Inpaint_Model entry in the response with its display name and capability flags.
4. WHEN a user selects the Stability_Inpaint_Model from the dropdown, THE Synthetic_Data_Generator SHALL use the Stability_Inpaint_Model for all subsequent generation requests in the Generation_Session.
5. WHERE a selected Generation_Model's capability flags exclude a randomization parameter (for example `cfg_scale`), THE Portal SHALL omit the corresponding generation control for that model.

### Requirement 2: Per-Provider Request and Response Adapter

**User Story:** As a data scientist, I want generation requests shaped correctly for whichever provider's model I select, so that the Stability inpainting model produces images through the same pipeline Amazon models used.

#### Acceptance Criteria

1. WHEN the generation worker invokes a Generation_Model, THE Synthetic_Data_Generator SHALL select the Request_Adapter by the model id's Provider prefix.
2. WHEN the Request_Adapter builds a request for an `amazon.` Generation_Model, THE Request_Adapter SHALL produce a request body byte-identical to the body the current implementation produces for the same task inputs.
3. WHEN the Request_Adapter builds an inpainting request for the Stability_Inpaint_Model, THE Request_Adapter SHALL produce a request body containing the base64-encoded source image in the `image` field, the base64-encoded Mask_Image in the `mask` field, the resolved prompt in the `prompt` field, the Task_Seed in the `seed` field, and the output format in the `output_format` field, conforming to the Bedrock Stability inpaint request schema (`image`, `mask`, `prompt`, `negative_prompt`, `seed`, `output_format`).
4. WHERE a Generation_Model's capability flags exclude a parameter, THE Request_Adapter SHALL omit that parameter from the request body.
5. WHEN the Stability_Inpaint_Model returns a response, THE Request_Adapter SHALL extract the generated image from the Stability response schema (`images` list of base64 strings, with `seeds` and `finish_reasons`) and return image bytes to the generation worker in the same form the Amazon path returns them.
6. IF a Stability response contains no image or reports a content-filtered or error value in `finish_reasons`, THEN THE Request_Adapter SHALL raise a task failure whose message includes the reason reported by the model.

### Requirement 3: Mask_Image Synthesis and Inpainting

**User Story:** As a data scientist, I want the inpainting path to supply the binary mask the Stability model requires and to keep results reproducible, so that I can inject defects into normal source images and the auto-annotator can localize them.

#### Acceptance Criteria

1. WHEN a Generation_Session with Normal_Image sources uses a Generation_Model whose capability flags declare inpainting support, THE Synthetic_Data_Generator SHALL select inpainting as the Generation_Method.
2. WHEN an inpainting task targets the Stability_Inpaint_Model, THE Synthetic_Data_Generator SHALL synthesize a binary Mask_Image (PNG) delimiting the defect region to inpaint.
3. WHEN the Synthetic_Data_Generator synthesizes a Mask_Image for a task, THE Synthetic_Data_Generator SHALL derive the mask placement deterministically from the Task_Seed, so that identical task inputs produce an identical Mask_Image.
4. WHEN an inpainting task for the Stability_Inpaint_Model completes, THE Synthetic_Data_Generator SHALL record on the Preview_Image the Mask_Region rectangle corresponding to the supplied Mask_Image, so that the Auto_Annotator derives the bounding box from the Mask_Region.
5. IF a generation request's source classification requires a Generation_Method that the selected Generation_Model's capability flags do not support (for example image variation for Defect_Image sources on the Stability_Inpaint_Model), THEN THE Synthetic_Data_Generator SHALL reject the generation request with a message identifying the missing capability.

### Requirement 4: Inference-Profile Invocation

**User Story:** As a portal operator, I want the generation worker to invoke Stability models through their inference profiles, so that invocations succeed despite Bedrock rejecting direct on-demand invocation of the bare model ids.

#### Acceptance Criteria

1. THE Model_Catalog SHALL support an Invocation_Identifier on each entry distinct from the entry's model id.
2. WHEN the generation worker invokes a Generation_Model whose Model_Catalog entry carries an Invocation_Identifier, THE Synthetic_Data_Generator SHALL pass the Invocation_Identifier as the model id in the Bedrock `InvokeModel` call.
3. WHEN the Availability_Filter evaluates a Model_Catalog entry, THE Availability_Filter SHALL match the entry's bare model id against the model ids returned by `bedrock:ListFoundationModels`, independent of the entry's Invocation_Identifier.
4. WHEN a Model_Catalog entry carries no Invocation_Identifier, THE Synthetic_Data_Generator SHALL invoke Bedrock with the entry's bare model id, preserving the current Amazon invocation behavior.

### Requirement 5: LEGACY Model Exclusion from Availability

**User Story:** As a data scientist, I want models the account can no longer invoke to disappear from the dropdown, so that I am not offered Nova Canvas as a selectable dead end that fails on every task.

#### Acceptance Criteria

1. WHEN the Availability_Filter evaluates a Model_Catalog entry, THE Availability_Filter SHALL exclude the entry when the model's Lifecycle_Status returned by `bedrock:ListFoundationModels` is not ACTIVE.
2. WHILE `amazon.nova-canvas-v1:0` has Lifecycle_Status LEGACY in the Portal_Region, THE Portal SHALL exclude the Nova Canvas entry from the `GET /synthetic/models` response.
3. THE Model_Catalog SHALL retain the `amazon.nova-canvas-v1:0` entry, so that the entry reappears in the dropdown through the Availability_Filter when the model's Lifecycle_Status returns to ACTIVE.

### Requirement 6: Region Scope

**User Story:** As a portal operator, I want generation to stay within the portal region, so that model availability and data movement remain predictable and no cross-region inference paths are introduced.

#### Acceptance Criteria

1. THE Synthetic_Data_Generator SHALL invoke all Generation_Models through Bedrock in the Portal_Region.
2. WHEN `GET /synthetic/models` is invoked, THE Availability_Filter SHALL exclude Model_Catalog entries that Bedrock does not offer in the Portal_Region.

### Requirement 7: Seed Determinism and Metadata Traceability

**User Story:** As a data scientist, I want every generated image traceable to its model, seed, and prompt regardless of provider, so that generation results remain reproducible and auditable.

#### Acceptance Criteria

1. FOR ALL generation tasks, THE Synthetic_Data_Generator SHALL derive the Task_Seed with the same `derive_task_seed(base_seed, task_index)` function regardless of the selected Generation_Model's Provider.
2. WHEN a generation task for the Stability_Inpaint_Model is created, THE Request_Adapter SHALL pass the Task_Seed unmodified in the Stability request schema's `seed` field, which accepts values in the range 0 to 4,294,967,294 and therefore accepts every Task_Seed value (0 to 858,993,459) without transformation.
3. WHEN a Preview_Image is produced by the Stability_Inpaint_Model, THE Synthetic_Data_Generator SHALL record on the Preview_Image the generation model id, the Task_Seed, and the resolved prompt in the same record shape used for Amazon-generated Preview_Images.
4. WHEN approved Stability-generated images are integrated into a Data_Manifest, THE Synthetic_Data_Generator SHALL record the Generation_Model identifier and resolved prompt using the same session and manifest metadata shape used today.

### Requirement 8: Nova Canvas Non-Interference

**User Story:** As a data scientist, I want the Nova Canvas request-building path to remain exactly as it is today, so that generation resumes unchanged if Amazon reactivates the model for this account.

#### Acceptance Criteria

1. WHEN a generation task targets `amazon.nova-canvas-v1:0`, THE Synthetic_Data_Generator SHALL produce a Bedrock request body byte-identical to the body produced before this feature for the same task inputs.
2. WHEN `GET /synthetic/models` is invoked, THE Portal SHALL return any admitted Amazon catalog entries with the same fields and ordering behavior as before this feature, subject to the Availability_Filter.
3. WHEN a Generation_Session uses an Amazon Generation_Model, THE Synthetic_Data_Generator SHALL persist session, Preview_Image, and Data_Manifest records with the same fields as before this feature.

### Requirement 9: Model Invocation Failure Handling

**User Story:** As a data scientist, I want clear per-image errors distinguishing access problems from lifecycle problems, so that I know whether to check Bedrock model access or model lifecycle status rather than suspect a pipeline bug.

#### Acceptance Criteria

1. IF a Bedrock invocation of a Generation_Model fails with an AccessDeniedException, THEN THE Synthetic_Data_Generator SHALL record a per-task failure reason on the Preview_Image identifying that Bedrock model access is not granted for that model id.
2. IF a Bedrock invocation of a Generation_Model fails with a ResourceNotFoundException indicating the model is marked Legacy, THEN THE Synthetic_Data_Generator SHALL record a per-task failure reason on the Preview_Image identifying the model's lifecycle status as the cause.
3. WHEN a generation task fails, THE Synthetic_Data_Generator SHALL continue executing the remaining tasks in the generation plan and record each failure on the Generation_Session.
4. WHEN a Generation_Session contains failed tasks, THE Portal SHALL display the recorded failure reasons to the user through the existing per-task error surfacing.
