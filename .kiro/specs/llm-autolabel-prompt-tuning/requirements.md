# Requirements Document

## Introduction

The DDA Labeling System's prompt-guided LLM auto-label family (`llm:<model_identifier>`) sends one Bedrock Converse request per dataset image, built from the Job_Creator's Detection_Prompt, and converts the model's Coordinate_Guidance JSON into modality Pre_Labels. Today the Job_Creator writes the Detection_Prompt blind: the first evidence of prompt quality arrives only after the Labeling_Job is created and per-image auto-labeling has already fanned out to labelers (or to skip-verification review). There is also no way to leverage the good/bad example images the create wizard already collects (up to 10 each, JPEG/PNG, currently used only as labeler instructions) to improve the model's output.

This feature adds two capabilities to the labeling job creation flow, both scoped to the `llm:` model family:

1. **Prompt Tuning Preview** — before the Labeling_Job is created, the Job_Creator selects a small number of sample images from the dataset prefix, runs the configured model and Detection_Prompt against them through a preview backend API, sees the resulting Pre_Labels rendered on the images (boxes, masks, or classification labels per modality), and iterates on the prompt and/or model until satisfied. Preview runs create no job records, no Task_Assignments, and no notifications.
2. **Few-Shot Examples** — an option to attach the job's good/bad example images to each model request as few-shot context alongside the Detection_Prompt. The option and the example set are persisted with the job at creation so the Auto_Labeler worker sends the same request shape at labeling time that the preview exercised, bounded by the per-model image limit of the Bedrock Converse API.

Existing behavior for the `sam` and `bedrock:` model families, and for `llm:` jobs that do not enable few-shot examples, is preserved unchanged.

## Glossary

- **DDA_Labeling_System**: The portal-native data labeling backend (see the dda-data-labeling spec), comprising job creation, task distribution, the Auto_Labeler, and the labeler/admin interfaces.
- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **Job_Creator**: A portal user authorized to create labeling jobs (e.g., DataScientist, UseCaseAdmin, or PortalAdmin) within a Use_Case.
- **Use_Case**: The existing portal tenant construct; datasets, jobs, and previews are scoped to a Use_Case.
- **Labeling_Job**: A unit of labeling work created against a dataset prefix in S3, with a modality, Label_Set, instructions, example images, and optional auto-labeling configuration.
- **Labeling_Modality**: The kind of annotation a job collects — Classification, Segmentation, or ObjectDetection.
- **Label_Set**: The ordered list of class names defined at job creation.
- **LLM_Auto_Label_Model**: An auto-label model in the `llm:<model_identifier>` family: a Bedrock model invoked with one Converse request per image carrying the image and a prompt built from the Detection_Prompt.
- **Detection_Prompt**: The Job_Creator-authored prompt (1–2000 characters, persisted character-for-character) that guides the LLM_Auto_Label_Model's detections.
- **Coordinate_Guidance**: The `{"detections": [...]}` JSON the LLM_Auto_Label_Model returns for one image, strictly parsed and validated before conversion to a Pre_Label.
- **Pre_Label**: A machine-generated candidate annotation in the job's Labeling_Modality (classification label, bounding boxes, or mask regions).
- **Prompt_Tuning_Preview**: The new capability, embedded in the labeling job creation flow, that lets the Job_Creator run the configured LLM_Auto_Label_Model and Detection_Prompt against selected sample images and inspect the resulting Pre_Labels before creating the Labeling_Job.
- **Preview_Run**: One invocation of the Preview_API: a set of selected Sample_Images evaluated with one model, one Detection_Prompt, one Labeling_Modality, one Label_Set, and one few-shot setting.
- **Preview_API**: The new backend API that executes a Preview_Run and returns per-image results without creating any Labeling_Job, Task_Assignment, or notification.
- **Sample_Image**: A dataset image under the Use_Case's dataset prefix that the Job_Creator selects for a Preview_Run.
- **Sample_Limit**: The maximum number of Sample_Images allowed in one Preview_Run: 5.
- **Few_Shot_Examples**: The job's good example images and bad example images, attached to a model request as additional image content identified to the model as good or bad examples, alongside the Detection_Prompt.
- **Few_Shot_Option**: The per-job boolean setting that enables attaching Few_Shot_Examples to every LLM_Auto_Label_Model request for the job.
- **Model_Image_Limit**: The maximum number of image blocks permitted in one Bedrock Converse request for a given model, taken from a per-model configuration with a default of 20.
- **Auto_Labeler**: The DDA_Labeling_System component (SQS worker) that generates Pre_Labels for dataset images at labeling time.
- **Preview_Result**: The per-Sample_Image outcome of a Preview_Run: either a validated Pre_Label or a failure with a categorized reason.

## Requirements

### Requirement 1: Prompt Tuning Preview Availability

**User Story:** As a Job_Creator, I want a preview step in the labeling job creation flow when I configure prompt-guided LLM auto-labeling, so that I can evaluate my Detection_Prompt against real dataset images before any labeling job exists.

#### Acceptance Criteria

1. WHILE a Job_Creator is configuring a DDA Labeling_Job with an LLM_Auto_Label_Model selected, THE Portal SHALL present the Prompt_Tuning_Preview controls within the job creation flow before job submission.
2. WHILE the selected auto-label model is `sam`, a `bedrock:` model, or no model, THE Portal SHALL hide the Prompt_Tuning_Preview controls.
3. WHEN a Job_Creator starts a Preview_Run, THE Portal SHALL send to the Preview_API the currently configured LLM_Auto_Label_Model identifier, Detection_Prompt, Labeling_Modality, Label_Set, Few_Shot_Option value, the selected Sample_Images (between 1 and the Sample_Limit of 5 images), and, WHERE the Few_Shot_Option is enabled, the uploaded example image references (at most 10 good and 10 bad example images).
4. IF a Job_Creator attempts to start a Preview_Run while the Detection_Prompt is empty after trimming, the Detection_Prompt exceeds 2000 characters, the Label_Set is invalid for the selected Labeling_Modality, zero Sample_Images are selected, or more than 5 Sample_Images are selected, THEN THE Portal SHALL reject the attempt with a validation message identifying each invalid input, SHALL invoke no Preview_API request, and SHALL retain all values the Job_Creator has entered in the job creation flow.
5. THE Portal SHALL permit the Job_Creator to submit the Labeling_Job without having started any Preview_Run.
6. THE Preview_API SHALL create no Labeling_Job, Task_Assignment, or Pre_Label record as a result of a Preview_Run.
7. WHILE a Preview_Run is in progress, THE Portal SHALL prevent the Job_Creator from starting an additional Preview_Run and SHALL retain all values entered in the job creation flow.
8. IF a started Preview_Run fails or returns no result within 120 seconds per Sample_Image, THEN THE Portal SHALL display an error message indicating the Preview_Run failed, SHALL re-enable the Preview_Run controls, and SHALL retain all values entered in the job creation flow.

### Requirement 2: Sample Image Selection

**User Story:** As a Job_Creator, I want to pick a handful of representative images from my dataset prefix, so that the preview evaluates the prompt against the same images the labeling job will process.

#### Acceptance Criteria

1. WHEN a Job_Creator opens the Sample_Image selection within the Prompt_Tuning_Preview, THE Portal SHALL list the image objects under the configured dataset prefix of the Use_Case whose object keys end in `.jpg`, `.jpeg`, or `.png` (case-insensitive) and SHALL exclude all other objects from the selectable list.
2. WHEN listing images under the dataset prefix, THE Portal SHALL display each listed image's object key together with a visual thumbnail of the image so the Job_Creator can identify which image is being selected.
3. THE Portal SHALL permit selecting between 1 and Sample_Limit (5) Sample_Images, inclusive, for one Preview_Run.
4. IF a Job_Creator attempts to start a Preview_Run with zero Sample_Images selected or with more than Sample_Limit Sample_Images selected, THEN THE Portal SHALL reject the attempt with a validation message stating the allowed range of 1 to Sample_Limit and SHALL invoke no Preview_API request.
5. IF the dataset prefix is not accessible to the Portal or the listing returns zero JPEG or PNG image objects, THEN THE Portal SHALL display an error message that identifies the dataset prefix and distinguishes an inaccessible prefix from an empty prefix, and SHALL disable starting a Preview_Run.
6. WHEN a Job_Creator refreshes or re-opens the Sample_Image selection after a listing error, THE Portal SHALL re-execute the listing under the dataset prefix and SHALL enable starting a Preview_Run when the re-listing succeeds and returns at least one JPEG or PNG image object.
7. IF the dataset prefix contains more than 100 JPEG or PNG image objects, THEN THE Portal SHALL present the listing in pages of at most 100 images and SHALL permit the Job_Creator to navigate to every listed image object under the prefix.
8. IF a thumbnail cannot be retrieved for a listed image, THEN THE Portal SHALL display the image's object key in place of the thumbnail and SHALL keep the image selectable.

### Requirement 3: Preview Execution

**User Story:** As a Job_Creator, I want the preview to run the exact same model invocation, prompt construction, and response validation that the labeling job will run, so that preview results are a faithful predictor of labeling-time behavior.

#### Acceptance Criteria

1. WHEN the Preview_API receives a valid Preview_Run request, THE Preview_API SHALL, for each Sample_Image, issue exactly one Bedrock Converse request to the specified LLM_Auto_Label_Model carrying the Sample_Image and the prompt, SHALL construct the prompt with the same prompt construction used by the Auto_Labeler for the `llm:` family (Detection_Prompt character-for-character, Label_Set, and the Sample_Image's pixel dimensions), and SHALL issue no retry or additional invocations for that Sample_Image regardless of outcome.
2. WHEN a model response is received for a Sample_Image, THE Preview_API SHALL parse and validate the response as Coordinate_Guidance and convert it to a Pre_Label using the same parsing, validation, and conversion rules the Auto_Labeler applies for the `llm:` family.
3. THE Preview_API SHALL bound each model invocation for a Sample_Image at a maximum of 120 seconds, matching the Auto_Labeler's invocation bound, and SHALL treat an invocation exceeding this bound as a failure of that Sample_Image.
4. THE Preview_API SHALL send to the model only image content and prompt content, and SHALL include no dataset credentials and no portal secrets in any model request.
5. WHEN a Preview_Run completes, THE Preview_API SHALL return exactly one Preview_Result per requested Sample_Image, each identifying its Sample_Image and containing either the converted Pre_Label or a failure reason, and SHALL persist no Labeling_Job record, no Task_Assignment, no Pre_Label artifact, and SHALL send no labeler notification.
6. WHEN a Preview_Run executes, THE Preview_API SHALL read Sample_Images from the Use_Case's dataset bucket through the same cross-account access mechanism, including its direct-access fallback, that the Auto_Labeler uses.
7. IF one Sample_Image in a Preview_Run fails, THEN THE Preview_API SHALL continue processing the remaining Sample_Images and SHALL return the successful Preview_Results together with the per-image failures.
8. WHEN a Preview_Run executes, THE DDA_Labeling_System SHALL record an audit event containing the requesting user's identity, the Use_Case, the model identifier, and the Sample_Image count.
9. IF the Preview_API cannot read a Sample_Image from the dataset bucket, or cannot determine the Sample_Image's pixel dimensions, THEN THE Preview_API SHALL record a failure for that Sample_Image containing a reason indicating the cause, and SHALL issue no model invocation for that Sample_Image.
10. IF a model invocation for a Sample_Image times out or returns an error, THEN THE Preview_API SHALL record a failure for that Sample_Image whose reason distinguishes a timeout from a model error, matching the Auto_Labeler's failure-reason distinction for the `llm:` family.
11. IF a model response for a Sample_Image fails Coordinate_Guidance parsing, validation, or Pre_Label conversion, THEN THE Preview_API SHALL record a failure for that Sample_Image containing the same failure reason the Auto_Labeler would record for that response.

### Requirement 4: Preview Result Rendering

**User Story:** As a Job_Creator, I want to see the model's output drawn on each sample image in the job's modality, so that I can judge auto-label accuracy visually.

#### Acceptance Criteria

1. WHERE the Labeling_Modality is ObjectDetection, WHEN a Preview_Run returns, THE Portal SHALL render each successful Preview_Result's bounding boxes overlaid on its Sample_Image, positioned according to the Pre_Label's coordinates and scaled proportionally to the displayed image size, with each box's class name from the Label_Set displayed adjacent to its box.
2. WHERE the Labeling_Modality is Segmentation, WHEN a Preview_Run returns, THE Portal SHALL render each successful Preview_Result's mask regions overlaid on its Sample_Image, positioned according to the Pre_Label's region geometry and scaled proportionally to the displayed image size, with each region's class name from the Label_Set displayed in association with its region.
3. WHERE the Labeling_Modality is Classification, WHEN a Preview_Run returns, THE Portal SHALL display each successful Preview_Result's classification label (normal or anomaly) alongside its Sample_Image.
4. WHEN a Preview_Result contains a Pre_Label with zero detections, THE Portal SHALL display the Sample_Image with an explicit empty-result indication that is visually distinct from both a successful Preview_Result containing detections and a failed Preview_Result.
5. WHILE a Preview_Run is in progress, THE Portal SHALL display an in-progress indication and SHALL prevent starting another Preview_Run until the current Preview_Run returns or fails.
6. WHEN a Preview_Run returns, THE Portal SHALL display exactly one result entry per requested Sample_Image, pairing each Preview_Result with the Sample_Image it was generated from.
7. IF the Preview_API request for a Preview_Run fails or returns an error for the entire Preview_Run, THEN THE Portal SHALL display an error message indicating the Preview_Run failure and SHALL re-enable starting a Preview_Run.

### Requirement 5: Prompt and Model Iteration

**User Story:** As a Job_Creator, I want to edit the prompt or switch the model and re-run the preview on the same images, so that I can converge on the configuration that auto-labels best.

#### Acceptance Criteria

1. WHEN a Preview_Run returns, THE Portal SHALL permit the Job_Creator to edit the Detection_Prompt, change the LLM_Auto_Label_Model selection, toggle the Few_Shot_Option, and start a new Preview_Run without leaving the job creation flow, and THE Portal SHALL apply the same validations defined in Requirement 1 and Requirement 2 to each new Preview_Run attempt before invoking the Preview_API.
2. WHEN a Job_Creator prepares a new Preview_Run after a prior Preview_Run has returned, THE Portal SHALL present the previously selected Sample_Images as the default selection and SHALL permit the Job_Creator to add or remove Sample_Images within the 1 to Sample_Limit bounds of Requirement 2 before starting the new Preview_Run.
3. WHEN a new Preview_Run returns, THE Portal SHALL replace all displayed Preview_Results from the prior Preview_Run with the new Preview_Run's Preview_Results, including its per-image failures, leaving no Preview_Result from the prior Preview_Run displayed.
4. IF a new Preview_Run fails before returning any Preview_Result (request rejection or Preview_API error), THEN THE Portal SHALL display an error message identifying the failure and SHALL retain the previously displayed Preview_Results unchanged.
5. WHEN the Job_Creator submits the Labeling_Job after previewing, THE Portal SHALL submit the Detection_Prompt, LLM_Auto_Label_Model, and Few_Shot_Option values as they stand at submission time in the job creation form, regardless of whether those values match the configuration of any completed Preview_Run.

### Requirement 6: Few-Shot Examples Option

**User Story:** As a Job_Creator, I want to attach my good and bad example images to the model request along with the prompt, so that the model produces better auto-labels by learning from concrete examples.

#### Acceptance Criteria

1. WHILE an LLM_Auto_Label_Model is selected for a DDA Labeling_Job, THE Portal SHALL offer the Few_Shot_Option as a per-job setting, disabled by default.
2. IF the Few_Shot_Option is enabled and the job has zero good example images and zero bad example images, THEN THE Portal SHALL reject job submission and Preview_Run start with a validation message stating that at least one example image is required for the Few_Shot_Option, and SHALL invoke no Preview_API request.
3. IF the Preview_API receives a Preview_Run request with the Few_Shot_Option enabled and zero example image references, THEN THE Preview_API SHALL reject the request with a validation error stating that at least one example image is required for the Few_Shot_Option and SHALL invoke no model.
4. WHEN a DDA Labeling_Job with the Few_Shot_Option enabled is created, THE DDA_Labeling_System SHALL persist with the Labeling_Job record the Few_Shot_Option value and, for each example image reference (at most 10 good example images and at most 10 bad example images), its good-or-bad designation and its position in the stored order.
5. WHERE the Few_Shot_Option is enabled on a Labeling_Job, THE Auto_Labeler SHALL include the job's Few_Shot_Examples as image content in every LLM_Auto_Label_Model Converse request for that job, attached as good example images in their stored order first, then bad example images in their stored order, subject to the Model_Image_Limit truncation of Requirement 7, each example identified to the model as a good example or a bad example, alongside the Detection_Prompt and the dataset image.
6. WHERE the Few_Shot_Option is enabled in a Preview_Run request, THE Preview_API SHALL attach to each Sample_Image's model request the identical example image set, in the identical order, with the identical good-or-bad identification, that the Auto_Labeler attaches to each dataset image's model request at labeling time for a Labeling_Job with the same example images, stored order, and selected LLM_Auto_Label_Model.
7. IF an example image referenced by a Labeling_Job with the Few_Shot_Option enabled cannot be read when building a model request, THEN THE Auto_Labeler SHALL treat Pre_Label generation for that dataset image as failed with a reason identifying the unreadable example image and SHALL continue processing the remaining dataset images of the Labeling_Job.
8. IF an example image referenced in a Preview_Run request with the Few_Shot_Option enabled cannot be read when building a Sample_Image's model request, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result with a reason identifying the unreadable example image and SHALL continue processing the remaining Sample_Images.
9. IF the auto-label model selection is changed away from the `llm:` family while the Few_Shot_Option is enabled, THEN THE Portal SHALL submit the Labeling_Job with the Few_Shot_Option disabled, and THE DDA_Labeling_System SHALL attach no Few_Shot_Examples to any model request for that job.

### Requirement 7: Model Request Image Bounds

**User Story:** As a Job_Creator, I want the system to respect the model's per-request image limits automatically, so that enabling few-shot examples never causes request failures from oversized payloads.

#### Acceptance Criteria

1. THE DDA_Labeling_System SHALL maintain a Model_Image_Limit per LLM_Auto_Label_Model as an integer value of at least 1, and SHALL apply a default value of 20 for models without a specific configured limit.
2. WHERE the Few_Shot_Option is enabled, THE DDA_Labeling_System SHALL bound the total image count in each Converse request issued by the Preview_API or the Auto_Labeler (the target image plus attached Few_Shot_Examples) to at most the Model_Image_Limit of the selected model.
3. WHEN attaching Few_Shot_Examples to a Converse request, THE DDA_Labeling_System SHALL order the attached example images as good example images in the order persisted with the Labeling_Job record first, followed by bad example images in their persisted order.
4. IF the number of stored example images exceeds Model_Image_Limit minus one, THEN THE DDA_Labeling_System SHALL attach only the first Model_Image_Limit minus one example images from the ordering of good examples in persisted order followed by bad examples in persisted order, SHALL omit the remaining example images, and SHALL attach zero example images when Model_Image_Limit is 1.
5. WHILE the Few_Shot_Option is enabled and the number of stored example images exceeds the Model_Image_Limit of the selected LLM_Auto_Label_Model minus one, THE Portal SHALL display within the job creation flow the count of example images that will be attached and the count that will be omitted, recomputing both counts whenever the selected model or the stored example image set changes.
6. WHEN attaching Few_Shot_Examples for the same Labeling_Job configuration, THE Preview_API and THE Auto_Labeler SHALL select identical example image subsets in identical order.

### Requirement 8: Access Control and Run Bounds

**User Story:** As a portal administrator, I want previews restricted to users who can create labeling jobs and bounded in size, so that the preview capability cannot be used to run up Bedrock costs or probe data outside the user's authorization.

#### Acceptance Criteria

1. WHEN a user authorized to create DDA labeling jobs in a Use_Case requests a Preview_Run for that Use_Case and the request passes all validations defined in this requirement, THE Preview_API SHALL execute the Preview_Run.
2. IF a user without authorization to create DDA labeling jobs in the target Use_Case requests a Preview_Run, THEN THE Preview_API SHALL reject the request with an authorization error that contains no dataset content and does not disclose whether the target Use_Case or any referenced object exists, SHALL read no Sample_Image, and SHALL invoke no model.
3. IF a Preview_Run request contains a Sample_Image reference whose resolved bucket and object key fall outside the requesting Use_Case's dataset bucket and prefix, THEN THE Preview_API SHALL reject the request with a validation error identifying each out-of-scope reference, SHALL not read any out-of-scope referenced object, and SHALL invoke no model.
4. IF a Preview_Run request contains more than Sample_Limit Sample_Images, zero Sample_Images, a Detection_Prompt that is empty after trimming or exceeds 2000 characters, a model identifier that is not in the `llm:` family, or a Label_Set that is invalid for the selected Labeling_Modality, THEN THE Preview_API SHALL reject the request with a validation error identifying each invalid element and SHALL invoke no model.
5. IF a Preview_Run request specifies any model identifier that is not an `llm:` family identifier, including but not limited to `sam` and `bedrock:` identifiers, THEN THE Preview_API SHALL reject the request with a validation error identifying the disallowed model identifier and SHALL invoke no model.
6. WHEN the Preview_API receives a Preview_Run request, THE Preview_API SHALL check the requesting user's authorization to create DDA labeling jobs in the target Use_Case before evaluating any other validation in this requirement.
7. WHEN the Preview_API checks Sample_Image scope for a Preview_Run request, THE Preview_API SHALL resolve each Sample_Image reference to its target bucket and object key before comparing the resolved location against the Use_Case's dataset bucket and prefix.
8. IF a Preview_Run request is received from a user who has another Preview_Run still executing in the same Use_Case, THEN THE Preview_API SHALL reject the new request with an error indicating that a Preview_Run is already in progress for that user and Use_Case and SHALL invoke no model for the rejected request.

### Requirement 9: Preview Error Surfacing

**User Story:** As a Job_Creator, I want each failed sample to tell me what went wrong — model error, timeout, or unusable model output — including what the model actually returned, so that I can tune the prompt effectively instead of guessing.

#### Acceptance Criteria

1. IF a model invocation for a Sample_Image fails with a model error, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as a model error, carrying the error description from the failed invocation.
2. IF a model invocation for a Sample_Image does not return a response within the 120-second bound, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as a timeout, where the timeout category is distinct from the model error category.
3. IF a model response for a Sample_Image fails Coordinate_Guidance parsing, validation, or Pre_Label conversion, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as unusable model output, carrying the failure reason produced by the shared parsing, validation, and conversion rules (identifying which rejection occurred: no parseable JSON, structural mismatch, unrecognized class name, malformed geometry, detection count limit exceeded, or conversion failure) and the model's raw text output character-for-character as received.
4. IF a Sample_Image cannot be read from the dataset bucket, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as an image access failure with a reason identifying the unreadable Sample_Image, and SHALL invoke no model for that Sample_Image.
5. IF a Sample_Image's pixel dimensions cannot be determined from the image data, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as unsupported image content with a reason identifying the Sample_Image, and SHALL invoke no model for that Sample_Image.
6. THE Preview_API SHALL assign every failed Preview_Result exactly one failure category from the set: model error, timeout, unusable model output, image access failure, unsupported image content, or unreadable example image (per Requirement 6), with each category distinguishable from every other category in the returned Preview_Result.
7. WHEN a failed Preview_Result is displayed, THE Portal SHALL show the failure category and the failure reason with the affected Sample_Image.
8. WHERE a displayed failed Preview_Result is categorized as unusable model output, THE Portal SHALL make the model's complete raw text output viewable by the Job_Creator.

### Requirement 10: Preservation of Existing Behavior

**User Story:** As a portal operator, I want existing labeling jobs and the other auto-label model families to behave exactly as they do today, so that this feature is purely additive.

#### Acceptance Criteria

1. WHEN a DDA Labeling_Job is created with the `sam` or `bedrock:` model family, THE DDA_Labeling_System SHALL apply the same job creation validation rules, the same model request construction, and the same Pre_Label generation rules that applied to that model family before this feature, and SHALL include no Few_Shot_Examples and no few-shot identification content in any model request for such jobs.
2. WHEN the Auto_Labeler builds a model request for a DDA Labeling_Job with an LLM_Auto_Label_Model and the Few_Shot_Option disabled, THE Auto_Labeler SHALL include in the request exactly one image (the dataset image) and the prompt constructed from the Detection_Prompt character-for-character, the Label_Set, and the dataset image's pixel dimensions, with no Few_Shot_Examples and no example identification content, matching the request content the Auto_Labeler built for the `llm:` family before this feature.
3. WHEN the Auto_Labeler processes a Labeling_Job record that contains no Few_Shot_Option value, THE Auto_Labeler SHALL treat the Few_Shot_Option as disabled, SHALL build each model request carrying only the dataset image and the prompt with no Few_Shot_Examples, and SHALL report no failure attributable to the absent Few_Shot_Option value.
4. WHEN a Labeling_Job submission omits the Few_Shot_Option, THE DDA_Labeling_System SHALL apply the same Labeling_Job creation validation rules that applied before this feature and SHALL reject no submission on account of the omitted Few_Shot_Option.
5. WHILE a Job_Creator is configuring a DDA Labeling_Job with the `sam` model, a `bedrock:` model, or no auto-label model selected, THE Portal SHALL present no Prompt_Tuning_Preview controls and no Few_Shot_Option control.
6. WHEN a DDA Labeling_Job is created with an LLM_Auto_Label_Model and the Few_Shot_Option disabled, THE DDA_Labeling_System SHALL persist the Few_Shot_Option as disabled and SHALL retain the job's good/bad example images in their existing labeler-instruction role, unchanged from behavior before this feature.
