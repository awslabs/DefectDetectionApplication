# Requirements Document

## Introduction

The DDA Labeling System's prompt-guided auto-label family (`llm:<model_identifier>`) builds one Bedrock Converse request per image through a single shared path (`dda_llm_request.build_llm_request`, invoked by `dda_llm_prelabel.generate_llm_prelabel`), used by both the Prompt_Tuning_Preview and the Auto_Labeler. Two independent problems in that path motivate this feature.

**Problem 1 — one global output token budget for every model.** The Converse `inferenceConfig` is built by `bedrock_common.build_inference_config`, which emits `maxTokens` from a single global Bedrock_Configuration value (`bedrock_configuration.max_tokens` in the portal settings table), shared by every Bedrock consumer in the portal. The deployed value is currently 128000, which suits the configured Anthropic model. US Amazon Nova Pro caps output at 10000 tokens, so every `llm:` auto-label invocation of that model fails before the images are even considered, with `ValidationException: The maximum tokens you requested exceeds the model limit of 10000`. The root cause is the single shared value: tuning it for one model breaks another. This feature decouples the output token budget for the `llm:` auto-label family from the global Bedrock_Configuration and makes it resolvable per model, with a safe default of 10000.

**Problem 2 — no control over the pixel size of images sent to the model.** Every image is sent at its original resolution, for both the target image and any attached Few_Shot_Examples. Large dataset images inflate request payloads, input token consumption, and latency, and the Job_Creator has no way to trade image resolution against cost. This feature adds a Job_Creator-selectable Downscale_Setting, chosen in the Prompt_Tuning_Preview and persisted with the Labeling_Job so the Auto_Labeler applies the identical resize at labeling time.

**These two capabilities are independent.** Image downscaling reduces *input* payload and input tokens; it has no effect on the *output* token budget and does not resolve the `maxTokens` ValidationException. They are specified together only because they were requested together.

Both capabilities extend the same seams the llm-autolabel-prompt-tuning feature established and must preserve that feature's guarantees: the Preview_API and the Auto_Labeler issue byte-identical Converse requests for the same job configuration (its Properties 1 and 2), a request with no Few_Shot_Examples stays byte-identical to the pre-feature request (its Requirement 10.2), and Model_Image_Limit resolution stays total and safe (its Property 13). Behavior for the `sam` and `bedrock:` auto-label families, and for every other Bedrock consumer that reads the global Bedrock_Configuration (workflow generation, custom node code assist, node designer), is preserved unchanged.

## Glossary

Terms carried over from the llm-autolabel-prompt-tuning spec keep their existing definitions and are restated here only where this feature constrains them.

- **DDA_Labeling_System**: The portal-native data labeling backend, comprising job creation, task distribution, the Auto_Labeler, and the labeler/admin interfaces.
- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **Job_Creator**: A portal user authorized to create labeling jobs (DataScientist, UseCaseAdmin, or PortalAdmin) within a Use_Case.
- **Use_Case**: The existing portal tenant construct; datasets, jobs, and previews are scoped to a Use_Case.
- **Labeling_Job**: A unit of labeling work created against a dataset prefix in S3, with a modality, Label_Set, instructions, example images, and optional auto-labeling configuration.
- **Labeling_Modality**: The kind of annotation a job collects — Classification, Segmentation, or ObjectDetection.
- **Label_Set**: The ordered list of class names defined at job creation.
- **LLM_Auto_Label_Model**: An auto-label model in the `llm:<model_identifier>` family, invoked with one Bedrock Converse request per image.
- **Detection_Prompt**: The Job_Creator-authored prompt (1–2000 characters, persisted character-for-character) that guides the LLM_Auto_Label_Model's detections.
- **Coordinate_Guidance**: The `{"detections": [...]}` JSON the LLM_Auto_Label_Model returns for one image, whose coordinates are pixel coordinates of the image the model was sent.
- **Pre_Label**: A machine-generated candidate annotation in the job's Labeling_Modality (classification label, bounding boxes, or mask regions).
- **Prompt_Tuning_Preview**: The capability, embedded in the labeling job creation flow, that runs the configured LLM_Auto_Label_Model and Detection_Prompt against selected Sample_Images and displays the resulting Pre_Labels before the Labeling_Job is created.
- **Preview_Run**: One invocation of the Preview_API for one model, one Detection_Prompt, one Labeling_Modality, one Label_Set, one few-shot setting, one Downscale_Setting, one Token_Budget_Selection, and 1 to 5 Sample_Images.
- **Preview_API**: The backend API that executes a Preview_Run and returns per-image Preview_Results without creating any Labeling_Job, Task_Assignment, or notification.
- **Sample_Image**: A dataset image under the Use_Case's dataset prefix selected for a Preview_Run.
- **Preview_Result**: The per-Sample_Image outcome of a Preview_Run: either a validated Pre_Label or a failure carrying exactly one failure category.
- **Auto_Labeler**: The DDA_Labeling_System component (SQS worker) that generates Pre_Labels for dataset images at labeling time.
- **Few_Shot_Examples**: The job's good and bad example images, attached to a model request as additional image content identified to the model as good or bad examples.
- **Few_Shot_Option**: The per-job boolean setting that enables attaching Few_Shot_Examples to every LLM_Auto_Label_Model request for the job.
- **Model_Image_Limit**: The maximum number of image blocks permitted in one Converse request for a given model, from a per-model configuration with a default of 20.
- **Bedrock_Configuration**: The single global settings item (`setting_key='bedrock_configuration'`) holding `model_id`, `region`, `max_tokens`, `temperature`, `top_p`, and `timeout_seconds`, read by every Bedrock consumer in the Portal.
- **Global_Max_Tokens**: The `max_tokens` field of the Bedrock_Configuration.
- **Bedrock_Consumer**: A Portal feature that reads the Bedrock_Configuration to invoke Bedrock, other than the `llm:` auto-label family: workflow generation, custom node code assist, and the node designer.
- **Model_Token_Limit**: The maximum output token budget to request for one LLM_Auto_Label_Model, as an integer of at least 1.
- **Model_Token_Limits**: The per-model configuration mapping an LLM_Auto_Label_Model identifier to its Model_Token_Limit, maintained independently of the Bedrock_Configuration.
- **Model_Token_Limit_Default**: The Model_Token_Limit applied when no valid configured or selected value exists for a model: 10000.
- **Model_Token_Limit_Ceiling**: The largest Model_Token_Limit the DDA_Labeling_System accepts: 128000.
- **Token_Budget_Selection**: The Job_Creator's per-model output token budget value, entered in the Prompt_Tuning_Preview, carried in a Preview_Run request and persisted with the Labeling_Job.
- **Effective_Token_Budget**: The integer sent as Converse `maxTokens` for one `llm:` family request, resolved by the Token_Budget_Resolver.
- **Token_Budget_Resolver**: The single shared function that resolves the Effective_Token_Budget for one model identifier from the Token_Budget_Selection, the Model_Token_Limits, and the Model_Token_Limit_Default.
- **Downscale_Setting**: The Job_Creator's per-job image sizing choice: either Downscale_Off, or one Max_Image_Edge value from the Max_Image_Edge_Options.
- **Downscale_Off**: The Downscale_Setting value meaning image bytes are sent to the model unmodified.
- **Max_Image_Edge**: The maximum permitted length in pixels of the longer edge of an image sent to the model.
- **Max_Image_Edge_Options**: The permitted Max_Image_Edge values: 512, 768, 1024, 1280, 1536, and 2048.
- **Image_Downscaler**: The single shared component that derives the Downscaled_Image from source image bytes, the source image format, and the Downscale_Setting.
- **Downscaled_Image**: The image bytes, pixel width, and pixel height actually sent to the model for one source image.
- **Max_Source_Pixel_Count**: The largest decoded source pixel count (width multiplied by height) the Image_Downscaler accepts: 100,000,000 pixels.
- **Downscale_Duration_Bound**: The per-image bound within which the Image_Downscaler returns either a Downscaled_Image or a failure signal: 5 seconds.
- **Sent_Dimensions**: The pixel width and height of the Downscaled_Image of the target image — the dimensions embedded in the Detection_Prompt and used to validate Coordinate_Guidance.
- **Source_Dimensions**: The pixel width and height of the target image as stored in the dataset, determined by the existing PNG IHDR / JPEG SOF header parsing.
- **Settings_API**: The Portal API that reads and writes portal settings items, including the Bedrock_Configuration and the Model_Token_Limits.

## Requirements

### Requirement 1: Per-Model Output Token Budget Decoupled from the Global Bedrock Configuration

**User Story:** As a Job_Creator, I want the output token budget for prompt-guided auto-labeling to be configured per model rather than by one global value, so that tuning the budget for one model does not break auto-labeling with another model.

#### Acceptance Criteria

1. THE DDA_Labeling_System SHALL maintain Model_Token_Limits as a configuration mapping whose keys are the LLM_Auto_Label_Model identifier portion following the `llm:` prefix, matched by exact string comparison with no trimming and no case folding, and whose values are integers between 1 and Model_Token_Limit_Ceiling (128000) inclusive, held independently of the Bedrock_Configuration such that no read or write of the Bedrock_Configuration changes any Model_Token_Limits entry.
2. THE DDA_Labeling_System SHALL apply Model_Token_Limit_Default (10000) as the Model_Token_Limit for every LLM_Auto_Label_Model identifier whose Model_Token_Limits entry is absent, null, a boolean, a non-integer, or an integer outside 1 to Model_Token_Limit_Ceiling (128000) inclusive.
3. WHEN the DDA_Labeling_System builds a Converse request for an LLM_Auto_Label_Model, THE DDA_Labeling_System SHALL set that request's `maxTokens` to the Effective_Token_Budget resolved for that request's model identifier, and SHALL derive that `maxTokens` value from no field of the Bedrock_Configuration, including the Global_Max_Tokens.
4. WHEN the Preview_API and the Auto_Labeler build Converse requests for the same model identifier, the same Token_Budget_Selection, and the same Model_Token_Limits configuration, THE DDA_Labeling_System SHALL resolve the Effective_Token_Budget through the Token_Budget_Resolver in both paths and SHALL set equal `maxTokens` values in both requests.
5. WHEN a Bedrock_Consumer builds a Converse request, THE Portal SHALL set that request's `maxTokens` from the Global_Max_Tokens and SHALL read no Model_Token_Limits entry, unchanged from behavior before this feature.
6. THE DDA_Labeling_System SHALL resolve every Effective_Token_Budget for the Preview_API, the Auto_Labeler, and the Portal's model option listing from the same persisted Model_Token_Limits configuration, so that the Effective_Token_Budget displayed for the selected model in the job creation flow equals the `maxTokens` of every Converse request built for that model identifier and that Token_Budget_Selection.
7. WHEN the Global_Max_Tokens is changed to any integer value permitted by the Bedrock_Configuration validation rules, THE DDA_Labeling_System SHALL resolve an unchanged Effective_Token_Budget for every model identifier, Token_Budget_Selection, and Model_Token_Limits configuration.
8. THE DDA_Labeling_System SHALL deliver the Model_Token_Limits configuration to both the Preview_API and the Auto_Labeler through the same per-model configuration delivery mechanism the DDA_Labeling_System uses to deliver the Model_Image_Limit, so that both paths read equal Model_Token_Limits entries for equal persisted configuration.

### Requirement 2: Output Token Budget Resolution Is Total and Safe

**User Story:** As a portal operator, I want output token budget resolution to yield a valid value for every input, so that a missing, malformed, or out-of-range configuration value can never produce a failing or unbounded model invocation.

#### Acceptance Criteria

1. THE Token_Budget_Resolver SHALL return an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, and SHALL raise no exception and report no error, for every combination of model identifier value of any type, Token_Budget_Selection value of any type, and Model_Token_Limits configuration value of any type.
2. WHEN the Token_Budget_Selection for a request is an integer that is not a boolean and is between 1 and Model_Token_Limit_Ceiling inclusive, THE Token_Budget_Resolver SHALL return that Token_Budget_Selection unchanged.
3. IF the Token_Budget_Selection is absent, null, a boolean, a value of any non-integer type, or an integer outside 1 to Model_Token_Limit_Ceiling inclusive, AND the Model_Token_Limits entry for the model identifier is an integer that is not a boolean and is between 1 and Model_Token_Limit_Ceiling inclusive, THEN THE Token_Budget_Resolver SHALL return that Model_Token_Limits entry unchanged.
4. IF neither the Token_Budget_Selection nor the Model_Token_Limits entry for the model identifier is an integer that is not a boolean and is between 1 and Model_Token_Limit_Ceiling inclusive, THEN THE Token_Budget_Resolver SHALL return Model_Token_Limit_Default (10000).
5. IF a Token_Budget_Selection or a Model_Token_Limits entry is a boolean, THEN THE Token_Budget_Resolver SHALL treat that value as invalid at that resolution stage and SHALL continue to the next resolution stage without converting that value to 1 or 0.
6. THE Token_Budget_Resolver SHALL return equal values for every repeated evaluation with equal inputs, and SHALL leave the Token_Budget_Selection and the Model_Token_Limits configuration passed to it unmodified.
7. IF the Model_Token_Limits configuration is absent, is null, is not a mapping, contains no entry for the model identifier, or maps the model identifier to a value that is not an integer between 1 and Model_Token_Limit_Ceiling inclusive, THEN THE Token_Budget_Resolver SHALL treat the configured entry as invalid, SHALL continue to the Model_Token_Limit_Default stage, and SHALL report no error to the caller.
8. IF a Token_Budget_Selection or a Model_Token_Limits entry is a string, including a string containing only decimal digits, or is a float, including a float whose value is a whole number, THEN THE Token_Budget_Resolver SHALL treat that value as invalid at that resolution stage and SHALL continue to the next resolution stage without performing any numeric conversion of that value.
9. IF a Token_Budget_Selection or a Model_Token_Limits entry is an integer greater than Model_Token_Limit_Ceiling or less than 1, THEN THE Token_Budget_Resolver SHALL treat that value as invalid at that resolution stage, SHALL continue to the next resolution stage without clamping that value into range, and SHALL return a value no greater than Model_Token_Limit_Ceiling.
10. IF the model identifier passed to the Token_Budget_Resolver is not a string, THEN THE Token_Budget_Resolver SHALL perform no Model_Token_Limits lookup and SHALL return the Token_Budget_Selection when that selection is an integer that is not a boolean and is between 1 and Model_Token_Limit_Ceiling inclusive, and SHALL otherwise return Model_Token_Limit_Default.

**Note on criterion 10 — intentional divergence from `resolve_model_image_limit`:** where `resolve_model_image_limit` returns its default for a non-string model identifier, the Token_Budget_Resolver deliberately still returns a valid Token_Budget_Selection, because the selection tier does not depend on the model identifier; this asymmetry is intended and is not to be "corrected" to match the image-limit resolver.

### Requirement 3: Job Creator Control of the Output Token Budget

**User Story:** As a Job_Creator trying different models in the preview, I want to set the output token budget for the model I have selected, so that I can find a value each model accepts without waiting for an administrator.

#### Acceptance Criteria

1. WHILE a Job_Creator is configuring a DDA Labeling_Job with an LLM_Auto_Label_Model selected, THE Portal SHALL present within the Prompt_Tuning_Preview controls a Token_Budget_Selection control that accepts a whole number from 1 to Model_Token_Limit_Ceiling (128000) inclusive, pre-filled with the Effective_Token_Budget resolved for the selected model identifier, and SHALL display that accepted range alongside the control.
2. WHEN a Job_Creator changes the LLM_Auto_Label_Model selection, THE Portal SHALL replace the value shown in the Token_Budget_Selection control with the Effective_Token_Budget resolved for the newly selected model identifier, discarding the value shown for the previously selected model, and SHALL leave the Detection_Prompt, the Label_Set, the selected Sample_Images, the Few_Shot_Option, and the Downscale_Setting unchanged.
3. IF a Job_Creator enters a non-empty Token_Budget_Selection that is not a whole number from 1 to Model_Token_Limit_Ceiling (128000) inclusive, THEN THE Portal SHALL reject both the Preview_Run start and the Labeling_Job submission with a validation message stating the accepted range of 1 to Model_Token_Limit_Ceiling, SHALL invoke no Preview_API request and no Labeling_Job creation request, and SHALL retain every value the Job_Creator has entered in the job creation flow.
4. WHEN a Job_Creator starts a Preview_Run while the Token_Budget_Selection control holds a whole number from 1 to Model_Token_Limit_Ceiling (128000) inclusive, THE Portal SHALL send that number as the Token_Budget_Selection of the Preview_Run request.
5. IF a Preview_Run request carries a Token_Budget_Selection that is present and is not an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, THEN THE Preview_API SHALL reject the request with a validation error identifying the Token_Budget_Selection, SHALL start no Preview_Run, and SHALL invoke no model.
6. WHEN a DDA Labeling_Job with an LLM_Auto_Label_Model is created carrying a Token_Budget_Selection that is an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, THE DDA_Labeling_System SHALL persist that integer unchanged with the Labeling_Job record's auto-label configuration, alongside the Few_Shot_Option and the Downscale_Setting.
7. WHERE a Labeling_Job record carries a Token_Budget_Selection that is an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, THE Auto_Labeler SHALL resolve the Effective_Token_Budget for every Converse request of that Labeling_Job to that persisted value, unchanged by any Model_Token_Limits change made after the Labeling_Job was created.
8. WHEN the Auto_Labeler processes a Labeling_Job record that carries no Token_Budget_Selection, or carries a Token_Budget_Selection that is not an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, THE Auto_Labeler SHALL resolve the Effective_Token_Budget from the Model_Token_Limits and the Model_Token_Limit_Default (10000), SHALL generate Pre_Labels for every dataset image of that Labeling_Job, and SHALL report no failure attributable to the Token_Budget_Selection value.
9. WHILE a user without authority to write the Bedrock_Configuration is using the Prompt_Tuning_Preview, THE Portal SHALL apply the Token_Budget_Selection to the Preview_Run and to the Labeling_Job under configuration, SHALL invoke no Settings_API write, and SHALL leave the stored Model_Token_Limits and the stored Bedrock_Configuration unchanged.
10. WHEN a Job_Creator starts a Preview_Run or submits a Labeling_Job while the Token_Budget_Selection control is empty, THE Portal SHALL omit the Token_Budget_Selection from that request, so that the Effective_Token_Budget is resolved from the Model_Token_Limits and the Model_Token_Limit_Default (10000).
11. WHEN a Job_Creator changes the Token_Budget_Selection after a Preview_Run has returned, THE Portal SHALL permit starting a new Preview_Run over the same Sample_Images and the same Detection_Prompt without leaving the job creation flow, and SHALL apply the changed Token_Budget_Selection to that new Preview_Run.

### Requirement 4: Administered Per-Model Token Limits

**User Story:** As a portal administrator, I want to record a per-model output token limit that applies to every labeling job, so that a model's cap is respected without every Job_Creator having to know it.

#### Acceptance Criteria

1. WHEN a user authorized to write the Bedrock_Configuration submits a Model_Token_Limits change whose submitted value is a mapping of at most 200 entries in which every entry maps a model identifier that is a non-empty string of at most 256 characters to an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive, THE Settings_API SHALL replace the persisted Model_Token_Limits with the submitted mapping in its entirety, SHALL retain no entry that the submitted mapping omits, and SHALL return the persisted mapping in the response.
2. IF a Model_Token_Limits change submits a value that is not a mapping, contains more than 200 entries, contains a model identifier that is not a non-empty string of at most 256 characters, or contains a value that is not an integer between 1 and Model_Token_Limit_Ceiling inclusive, with boolean values classified as non-integers, THEN THE Settings_API SHALL reject the entire change with a validation error identifying each invalid entry and SHALL leave the persisted Model_Token_Limits unchanged.
3. IF a user without authority to write the Bedrock_Configuration submits a Model_Token_Limits change, THEN THE Settings_API SHALL reject the request with an authorization error indicating that PortalAdmin authority is required, SHALL record an audit entry marking the attempt as an unauthorized access with a denied result, and SHALL leave the persisted Model_Token_Limits unchanged.
4. WHEN the Settings_API persists a Model_Token_Limits change, THE Settings_API SHALL leave every field of the Bedrock_Configuration item (`model_id`, `region`, `max_tokens`, `temperature`, `top_p`, `timeout_seconds`) unchanged.
5. WHEN the Settings_API validates a Bedrock_Configuration change, THE Settings_API SHALL accept the change only where `model_id` and `region` are each a string that is non-empty after surrounding whitespace is trimmed, `max_tokens` is an integer of at least 1 with no upper bound applied, `temperature` and `top_p` are each either unset or a number between 0 and 1 inclusive, and `timeout_seconds` is an integer between 1 and 240 inclusive, SHALL classify boolean values for `max_tokens` and `timeout_seconds` as invalid, and SHALL apply Model_Token_Limit_Ceiling to no field of the Bedrock_Configuration.
6. WHEN a user authorized to write the Bedrock_Configuration submits a change containing a subset of the Bedrock_Configuration fields, THE Settings_API SHALL merge the submitted fields over the current effective Bedrock_Configuration, SHALL validate the merged result as a whole, and SHALL leave every omitted field at its current effective value.
7. WHEN the Settings_API persists a Bedrock_Configuration change, THE Settings_API SHALL leave the persisted Model_Token_Limits unchanged.
8. WHEN a user authorized to write the Bedrock_Configuration submits a Model_Token_Limits change whose submitted value is an empty mapping, THE Settings_API SHALL persist an empty Model_Token_Limits, so that the Token_Budget_Resolver returns Model_Token_Limit_Default for every model identifier.

### Requirement 5: Image Downscale Selection in the Preview

**User Story:** As a Job_Creator, I want to pick how much the images are downsized before they go to the model, and see the effect in the preview, so that I can trade image resolution against request size and cost with evidence.

#### Acceptance Criteria

1. WHILE a Job_Creator is configuring a DDA Labeling_Job with an LLM_Auto_Label_Model selected, THE Portal SHALL present a Downscale_Setting control within the Prompt_Tuning_Preview controls offering exactly seven options — Downscale_Off and each Max_Image_Edge value in Max_Image_Edge_Options (512, 768, 1024, 1280, 1536, 2048) labelled with its value in pixels — with Downscale_Off selected by default, and SHALL accept no Downscale_Setting value outside those seven options.
2. WHILE the selected auto-label model is `sam`, a `bedrock:` model, or no model, THE Portal SHALL hide the Downscale_Setting control and the Token_Budget_Selection control, and SHALL send neither a Downscale_Setting nor a Token_Budget_Selection with any request for that model.
3. WHEN a Job_Creator starts a Preview_Run with an LLM_Auto_Label_Model selected, THE Portal SHALL send with the Preview_Run request the Downscale_Setting currently selected in the control, as either Downscale_Off or one Max_Image_Edge integer from Max_Image_Edge_Options.
4. WHEN a Preview_Run returns, THE Portal SHALL display for each Sample_Image whose Source_Dimensions and Sent_Dimensions are both determined the Source_Dimensions in pixels, the Sent_Dimensions in pixels, and the ratio of the longer edge of the Sent_Dimensions to the longer edge of the Source_Dimensions as a percentage rounded to the nearest whole percent within 1 to 100 inclusive, as a read-only value the Job_Creator cannot edit.
5. IF a Preview_Run request carries a Downscale_Setting that is neither Downscale_Off nor an integer equal to a value in Max_Image_Edge_Options — including a boolean, a string, a non-integer number, or an integer outside Max_Image_Edge_Options — THEN THE Preview_API SHALL reject the request with a validation error identifying the Downscale_Setting and the permitted values, SHALL invoke no model, and SHALL return no Preview_Result.
6. WHEN a Job_Creator changes the Downscale_Setting after a Preview_Run has returned, THE Portal SHALL retain the Sample_Image selection of 1 to 5 Sample_Images, the Detection_Prompt, the Label_Set, the Few_Shot_Option, and the Token_Budget_Selection, and SHALL enable starting a new Preview_Run over the same Sample_Images without navigating away from the job creation flow.
7. WHEN a DDA Labeling_Job with an LLM_Auto_Label_Model is created, THE DDA_Labeling_System SHALL persist with the Labeling_Job record the submitted Downscale_Setting unchanged, as either Downscale_Off or one Max_Image_Edge integer from Max_Image_Edge_Options.
8. WHERE a Labeling_Job record carries a Downscale_Setting that is Downscale_Off or an integer equal to a value in Max_Image_Edge_Options, THE Auto_Labeler SHALL apply that Downscale_Setting to every image of every Converse request for that Labeling_Job, including the target image and every attached Few_Shot_Example image.
9. WHEN the Auto_Labeler processes a Labeling_Job record that carries no Downscale_Setting, THE Auto_Labeler SHALL apply Downscale_Off to every image of every Converse request for that Labeling_Job and SHALL report no failure attributable to the absent Downscale_Setting.
10. WHEN the Preview_API returns the Preview_Results of a Preview_Run, THE Preview_API SHALL include for each Sample_Image the applied Downscale_Setting, the Source_Dimensions, and the Sent_Dimensions.
11. IF the Source_Dimensions or the Sent_Dimensions of a Sample_Image cannot be determined, THEN THE Portal SHALL display for that Sample_Image an indication that the dimensions are unavailable in place of the Source_Dimensions, the Sent_Dimensions, and the percentage, and SHALL display the remaining Preview_Result content for that Sample_Image.
12. IF a Labeling_Job record carries a Downscale_Setting that is neither Downscale_Off nor an integer equal to a value in Max_Image_Edge_Options, THEN THE Auto_Labeler SHALL apply Downscale_Off to every image of every Converse request for that Labeling_Job, SHALL report no failure attributable to the Downscale_Setting value, and SHALL continue processing every dataset image of the Labeling_Job.

### Requirement 6: Deterministic Shared Downscaling

**User Story:** As a portal operator, I want downscaling to be computed once, in the code path both the preview and the labeling worker use, so that preview results stay a faithful predictor of labeling-time behavior.

#### Acceptance Criteria

1. THE Image_Downscaler SHALL be the single implementation of image resizing for the `llm:` family, invoked from the shared request path used by both the Preview_API and the Auto_Labeler, and SHALL be applied exactly once to each image before that image becomes a Converse image block.
2. WHERE the Downscale_Setting is Downscale_Off, THE Image_Downscaler SHALL return the source image bytes byte-for-byte unmodified, SHALL perform no decode-and-re-encode of those bytes, and SHALL return the Source_Dimensions as the Downscaled_Image dimensions.
3. WHERE the Downscale_Setting is a Max_Image_Edge value AND the longer edge of the source image is at most that Max_Image_Edge, THE Image_Downscaler SHALL return the source image bytes byte-for-byte unmodified, SHALL perform no decode-and-re-encode of those bytes, and SHALL return the Source_Dimensions as the Downscaled_Image dimensions.
4. WHERE the Downscale_Setting is a Max_Image_Edge value AND the longer edge of the source image exceeds that Max_Image_Edge, THE Image_Downscaler SHALL determine the source width and source height by decoding the source image bytes and SHALL return a Downscaled_Image whose width is `max(1, floor(source_width * Max_Image_Edge / max(source_width, source_height)))` and whose height is `max(1, floor(source_height * Max_Image_Edge / max(source_width, source_height)))`.
5. THE Image_Downscaler SHALL return a Downscaled_Image whose width is at most the source width, whose height is at most the source height, and whose width and height are each at least 1 pixel.
6. THE Image_Downscaler SHALL return identical output bytes and identical dimensions for identical combinations of source image bytes, source image format, and Downscale_Setting, across repeated invocations within one process, across separate processes, and between the Preview_API and the Auto_Labeler, by applying one fixed set of resampling and encoding parameters held in the Image_Downscaler and by excluding from the output every value that varies between invocations, including wall-clock time, invoking component identity, and environment configuration.
7. THE Image_Downscaler SHALL return a Downscaled_Image in the same Converse image format as the source image — png output for a source the DDA_Labeling_System derived as png from the object key, jpeg output for every other accepted source — and SHALL convert between png and jpeg in neither direction, so that the format value of each Converse image block equals the format the DDA_Labeling_System derived from the object key before this feature.
8. WHEN the Preview_API and the Auto_Labeler build a Converse request for the same model identifier, Labeling_Modality, Label_Set, Detection_Prompt, per-label prompt map, Token_Budget_Selection, Downscale_Setting, source image bytes, and Few_Shot_Example set, THE DDA_Labeling_System SHALL produce byte-identical requests in both paths, including every image block's bytes and format, the order of the image blocks, every text block, and the inference configuration.
9. IF the Image_Downscaler cannot decode the source image bytes, cannot determine a source width and a source height that are each at least 1 pixel, or cannot re-encode the resized image in the source's Converse image format, THEN THE Image_Downscaler SHALL return no Downscaled_Image, SHALL signal to its caller a failure indicating unsupported image content that identifies the image and the requested Downscale_Setting, and SHALL leave the source image bytes unmodified.
10. IF the source image's decoded pixel count (width multiplied by height) exceeds Max_Source_Pixel_Count (100,000,000 pixels), THEN THE Image_Downscaler SHALL return no Downscaled_Image and SHALL signal to its caller a failure indicating unsupported image content that identifies the image and its source pixel count, without decoding the full image.
11. WHEN the Image_Downscaler is invoked for a source image whose pixel count is at most Max_Source_Pixel_Count (100,000,000 pixels), THE Image_Downscaler SHALL return either a Downscaled_Image or a failure signal within Downscale_Duration_Bound (5 seconds) for that image.

### Requirement 7: Coordinate Consistency Under Downscaling

**User Story:** As a Job_Creator, I want the coordinates the model returns to land in the right place on my original images even when the images are downscaled, so that downscaling improves cost without corrupting the annotations.

#### Acceptance Criteria

1. WHEN the DDA_Labeling_System builds the Detection_Prompt for a target image, THE DDA_Labeling_System SHALL embed the Sent_Dimensions of that target image as the image's pixel dimensions and SHALL embed no other pixel dimensions for that image.
2. WHEN the DDA_Labeling_System validates Coordinate_Guidance for a target image, THE DDA_Labeling_System SHALL accept a horizontal coordinate only if it lies from 0 to the Sent_Dimensions width of that target image inclusive, and a vertical coordinate only if it lies from 0 to the Sent_Dimensions height of that target image inclusive.
3. WHEN the DDA_Labeling_System converts validated Coordinate_Guidance into a Pre_Label for a Labeling_Modality that carries geometry (ObjectDetection bounding boxes or Segmentation mask regions), THE DDA_Labeling_System SHALL map every horizontal coordinate `x` to `min(source_width, max(0, round(x * source_width / sent_width)))` and every vertical coordinate `y` to `min(source_height, max(0, round(y * source_height / sent_height)))`, where `source_width` and `source_height` are the Source_Dimensions, `sent_width` and `sent_height` are the Sent_Dimensions, and `round` is round-half-up to the nearest integer.
4. THE DDA_Labeling_System SHALL produce Pre_Label geometry in which every horizontal coordinate lies from 0 to the Source_Dimensions width inclusive and every vertical coordinate lies from 0 to the Source_Dimensions height inclusive.
5. WHERE the Sent_Dimensions equal the Source_Dimensions, THE DDA_Labeling_System SHALL apply no scaling, no rounding, and no clamping to the validated Coordinate_Guidance coordinates and SHALL produce Pre_Label geometry whose coordinate values equal those validated coordinate values exactly, as produced before this feature.
6. WHEN the DDA_Labeling_System determines the Source_Dimensions of a target image, THE DDA_Labeling_System SHALL use the existing PNG IHDR and JPEG SOF header parsing, accepting the same inputs it accepted before this feature.
7. WHEN a Preview_Result carrying Pre_Label geometry is displayed, THE Portal SHALL position each geometry coordinate in the Source_Dimensions coordinate space of the Sample_Image, applying only the uniform ratio between the displayed image size and the Source_Dimensions.
8. WHERE the Labeling_Modality is Classification, THE DDA_Labeling_System SHALL apply no coordinate scaling and SHALL produce the Pre_Label it produced for the same Coordinate_Guidance before this feature.
9. IF a coordinate in the returned Coordinate_Guidance for a target image lies outside 0 to the corresponding Sent_Dimensions bound inclusive, THEN THE DDA_Labeling_System SHALL record for that image a failure categorized as unusable model output whose reason identifies the out-of-bounds coordinate, SHALL produce no Pre_Label for that image, and SHALL continue processing the remaining images of the Labeling_Job or Preview_Run.
10. IF the Source_Dimensions of a target image cannot be determined from its PNG IHDR or JPEG SOF header, THEN THE DDA_Labeling_System SHALL treat the Downscale_Setting for that image as Downscale_Off and SHALL produce for that image the same Detection_Prompt dimension content and the same Pre_Label outcome it produced before this feature.

### Requirement 8: Downscaling of Few-Shot Example Images

**User Story:** As a Job_Creator, I want the downscale choice to apply to my example images too, so that enabling few-shot examples does not defeat the payload reduction I asked for.

#### Acceptance Criteria

1. WHERE the Few_Shot_Option is enabled, WHEN the DDA_Labeling_System builds a Converse request for a target image, THE DDA_Labeling_System SHALL apply through the Image_Downscaler, to each attached Few_Shot_Example image, the same Downscale_Setting value it applies to that request's target image (the Preview_Run request's Downscale_Setting for the Preview_API, the Labeling_Job record's Downscale_Setting for the Auto_Labeler), and SHALL carry each attached example image in the request as that example's Downscaled_Image bytes in the Converse image format derived from that example image's object key.
2. THE DDA_Labeling_System SHALL derive the Sent_Dimensions embedded in the Detection_Prompt from the target image alone, independently of the dimensions of any Few_Shot_Example image, and SHALL embed the dimensions of no Few_Shot_Example image in the Detection_Prompt.
3. WHERE the Few_Shot_Option is enabled, THE DDA_Labeling_System SHALL select the attached Few_Shot_Example subset and order independently of the Downscale_Setting, as the first `max(0, Model_Image_Limit - 1)` images of the candidate order (good example images in their stored order, followed by bad example images in their stored order), reserving one image slot of the Model_Image_Limit for the target image, and SHALL attach zero example images WHERE the Model_Image_Limit is 1.
4. WHEN the Preview_API and the Auto_Labeler attach Few_Shot_Examples for the same example image set, the same stored order, the same model identifier, and the same Downscale_Setting, THE DDA_Labeling_System SHALL produce byte-identical example image bytes, identical image format values, and identical example ordering in both paths, notwithstanding the different cross-account mechanisms by which each path reads the example images.
5. IF the Image_Downscaler cannot decode or re-encode an attached Few_Shot_Example image for the requested Downscale_Setting, THEN THE DDA_Labeling_System SHALL record for the affected target image exactly one failure drawn from the failure category set defined before this feature, categorized as unreadable example image, carrying a reason identifying that example image and the requested Downscale_Setting, SHALL invoke no model for that target image, SHALL record no Pre_Label for that target image, and SHALL continue processing the remaining images of the Labeling_Job or Preview_Run.
6. WHERE the Few_Shot_Option is enabled, THE DDA_Labeling_System SHALL order the content blocks of each Converse request as the few-shot header text block, then for each attached example image in attachment order the text block identifying that image as a good example or a bad example immediately followed by that example's image block, then the few-shot target-image introduction text block, then the target image block, then the prompt text block, unchanged by the Downscale_Setting.
7. WHERE the Few_Shot_Option is enabled AND the Downscale_Setting is Downscale_Off, THE DDA_Labeling_System SHALL carry each attached Few_Shot_Example image's source bytes unmodified in the Converse request, producing example image blocks byte-identical to those produced for the same example image set before this feature.
8. WHERE the Few_Shot_Option is enabled AND the Downscale_Setting is a Max_Image_Edge value, THE DDA_Labeling_System SHALL attach each Few_Shot_Example image with a longer edge of at most that Max_Image_Edge value, with a width of at most that example image's source width, and with a height of at most that example image's source height.

### Requirement 9: Downscaling and Token Budget Failure Handling

**User Story:** As a Job_Creator, I want a clear, categorized failure when an image cannot be resized or a model rejects the request, so that I can tell a sizing problem from a prompt problem.

#### Acceptance Criteria

1. IF the Image_Downscaler cannot decode or cannot re-encode a target image for a requested Downscale_Setting, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as unsupported image content whose reason identifies the Sample_Image object key and the requested Downscale_Setting value (Downscale_Off or the Max_Image_Edge value in pixels), SHALL invoke no model for that Sample_Image, and SHALL continue processing the remaining Sample_Images of the Preview_Run.
2. IF the Image_Downscaler cannot decode or cannot re-encode a target image for a requested Downscale_Setting, THEN THE Auto_Labeler SHALL record Pre_Label generation for that dataset image as failed through the same pre-label failure outcome it recorded for pre-label generation failures before this feature, with a reason identifying the dataset image object key and the requested Downscale_Setting value, SHALL invoke no model for that dataset image, and SHALL continue processing the remaining dataset images of the Labeling_Job.
3. THE Preview_API SHALL assign every failed Preview_Result exactly one failure category from the closed category set defined before this feature — model error, timeout, unusable model output, image access failure, unsupported image content, or unreadable example image — and SHALL return no failure category outside that set.
4. IF a model invocation for a Sample_Image is rejected because the Effective_Token_Budget exceeds the model's output token limit, THEN THE Preview_API SHALL return for that Sample_Image a failed Preview_Result categorized as a model error whose reason carries the error description from the failed invocation character-for-character, including the model's stated limit when the invocation reports one, and SHALL issue no retry and no further invocation for that Sample_Image.
5. WHEN a Preview_Run executes, THE DDA_Labeling_System SHALL record exactly one audit event, whether every Preview_Result succeeds or any Preview_Result fails, containing the requesting user's identity, the Use_Case, the model identifier, the Sample_Image count as an integer from 1 to 5 inclusive, the Downscale_Setting value (Downscale_Off or the Max_Image_Edge value in pixels), and the Effective_Token_Budget as an integer between 1 and Model_Token_Limit_Ceiling (128000) inclusive.
6. WHEN the DDA_Labeling_System records a failure for a failure mode that existed before this feature, THE DDA_Labeling_System SHALL record the failure reason text character-for-character as it recorded that reason before this feature.
7. IF a model invocation for a dataset image is rejected because the Effective_Token_Budget exceeds the model's output token limit, THEN THE Auto_Labeler SHALL record Pre_Label generation for that dataset image as failed with a reason carrying the error description from the failed invocation character-for-character, SHALL issue no retry and no further invocation for that dataset image, and SHALL continue processing the remaining dataset images of the Labeling_Job.
8. WHEN a failed Preview_Result is displayed, THE Portal SHALL show with the affected Sample_Image its failure category, its failure reason, the Downscale_Setting applied to the Preview_Run, and the Effective_Token_Budget applied to the Preview_Run.

### Requirement 10: Preservation of Existing Behavior

**User Story:** As a portal operator, I want every behavior that works today to keep working, so that adding token and image sizing controls is purely additive.

#### Acceptance Criteria

1. WHEN the DDA_Labeling_System builds a Converse request for an LLM_Auto_Label_Model with the Few_Shot_Option disabled or absent and the Downscale_Setting Downscale_Off or absent, THE DDA_Labeling_System SHALL produce a request whose content contains exactly one image block, whose bytes equal the source image bytes byte-for-byte and whose format equals the format derived from the object key before this feature, followed by exactly one text block built from the Detection_Prompt character-for-character, the Label_Set, and the Source_Dimensions, and SHALL invoke the Image_Downscaler for no image of that request.
2. WHEN the DDA_Labeling_System builds a Converse inference configuration, THE DDA_Labeling_System SHALL include `maxTokens`, SHALL include `temperature` and omit `topP` when the resolved temperature is present and numeric, SHALL include `topP` and omit `temperature` when the resolved temperature is absent or non-numeric and the resolved top_p is present and numeric, SHALL include neither when both are absent or non-numeric, and SHALL include at most one of `temperature` and `topP` in every inference configuration it builds.
3. WHEN the Portal resolves the Bedrock_Configuration, THE Portal SHALL coerce `timeout_seconds` to an integer, SHALL substitute 240 when the stored value is absent, null, boolean, or cannot be coerced to an integer, SHALL substitute 1 for a coerced value below 1, SHALL substitute 240 for a coerced value above 240, and SHALL yield an integer between 1 and 240 inclusive for every stored value.
4. WHEN a DDA Labeling_Job is created with the `sam` model family or a `bedrock:<model_identifier>` model family, THE DDA_Labeling_System SHALL apply the job creation validation, the model request construction, and the Pre_Label generation rules that applied to that family before this feature, SHALL invoke the Image_Downscaler for no image of any request for that Labeling_Job, SHALL send every image of such a request with bytes equal to the source image bytes byte-for-byte at its Source_Dimensions, and SHALL apply no Token_Budget_Selection and no Downscale_Setting value to any request for that Labeling_Job.
5. WHEN a Bedrock_Consumer invokes Bedrock, THE Portal SHALL resolve the Bedrock_Configuration by the rules of criterion 3, SHALL set that request's `maxTokens` from the Global_Max_Tokens, SHALL build that request's inference configuration by the rules of criterion 2, and SHALL let no Model_Token_Limits entry, Token_Budget_Selection, or Downscale_Setting value change any field of that request or of the client construction.
6. WHEN a Labeling_Job submission omits the Token_Budget_Selection and the Downscale_Setting, THE DDA_Labeling_System SHALL apply the Labeling_Job creation validation rules that applied before this feature, SHALL reject no submission on account of either omitted value, and SHALL return no validation message referring to either omitted value.
7. WHERE the Few_Shot_Option is enabled, THE DDA_Labeling_System SHALL bound the total image count of each Converse request (the target image plus every attached Few_Shot_Example) to at most the Model_Image_Limit of the selected model, and SHALL resolve the Model_Image_Limit to an integer of at least 1 for every model identifier and every configuration state, applying the default of 20 for a model identifier with no valid configured limit.
8. IF both the resolved temperature and the resolved top_p for a Converse inference configuration are present and numeric, THEN THE DDA_Labeling_System SHALL include `temperature`, SHALL omit `topP`, and SHALL issue the request with exactly one sampling parameter.
9. IF a Bedrock_Configuration field other than `timeout_seconds` is absent or cannot be coerced to its expected type, THEN THE Portal SHALL apply the same default value for that field that applied before this feature, SHALL report no error attributable to the absent or uncoercible field, and SHALL leave the stored Bedrock_Configuration unchanged.
10. WHEN the Auto_Labeler processes a Labeling_Job record that carries neither a Token_Budget_Selection nor a Downscale_Setting, THE Auto_Labeler SHALL treat the Token_Budget_Selection as unset, SHALL treat the Downscale_Setting as Downscale_Off, SHALL send every image of that Labeling_Job's requests at its Source_Dimensions, and SHALL report no failure attributable to either absent value.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Each property below is intended for property-based testing at 100 iterations, the established bar in this repository. Overlapping acceptance criteria (resolution totality, request identity, backward compatibility, non-regression) were consolidated so each property carries unique validation value.

### Property 1: Output token budget resolution is total and safe

*For any* model identifier (including non-string values), *any* Token_Budget_Selection (absent, null, boolean, string, float, negative, zero, in-range integer, above-ceiling integer) and *any* Model_Token_Limits configuration (absent, non-mapping, missing entry, boolean entry, string entry, float entry, out-of-range entry, in-range entry), the Token_Budget_Resolver SHALL return an integer between 1 and 128000 inclusive, SHALL return the Token_Budget_Selection whenever that value is an in-range non-boolean integer, SHALL otherwise return the configured entry whenever that entry is an in-range non-boolean integer, SHALL otherwise return 10000, SHALL neither convert nor clamp any invalid value, SHALL leave its inputs unmodified, and SHALL return the identical value on repeated evaluation.

**Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10**

### Property 2: Every `llm:` request carries the resolved per-model budget, never the global value

*For any* Global_Max_Tokens value (including values above every model's cap), *any* model identifier, *any* Token_Budget_Selection and *any* Model_Token_Limits configuration, the `maxTokens` of the Converse request the Preview_API issues and of the request the Auto_Labeler issues SHALL both equal the Effective_Token_Budget the Token_Budget_Resolver returns for the same persisted Model_Token_Limits, SHALL equal the budget the Portal displays for that model in the job creation flow, and SHALL be independent of the Global_Max_Tokens.

**Validates: Requirements 1.3, 1.4, 1.6, 1.7, 1.8, 3.7, 3.8**

### Property 3: Global Bedrock configuration semantics are preserved for every other consumer

*For any* stored Bedrock_Configuration (fields present, absent, null, or malformed) and *any* submitted partial change, the resolved configuration and the inference configuration built for a Bedrock_Consumer SHALL equal the pre-feature results: `maxTokens` from the Global_Max_Tokens, `temperature` when set, `topP` only when temperature is unset and top_p is set, never both, omitted fields left at their current effective values, per-field pre-feature defaults for absent or uncoercible fields, and `timeout_seconds` coerced and clamped into 1 to 240 inclusive.

**Validates: Requirements 1.5, 4.5, 4.6, 10.2, 10.3, 10.5, 10.8, 10.9**

### Property 4: Downscaling is deterministic, shrinking, and idempotent at the bound

*For any* decodable source image, *any* source format, and *any* Downscale_Setting (Downscale_Off or a Max_Image_Edge option), the Image_Downscaler SHALL yield dimensions of at least 1 pixel per edge, no larger than the source dimensions, with the longer edge at most the Max_Image_Edge whenever a Max_Image_Edge is selected, equal to the floor-scaled dimensions of Requirement 6.4 whenever the source exceeds the bound, equal to the source bytes exactly whenever the setting is Downscale_Off or the source already fits the bound, and always in the source's Converse image format; and applying the same setting to the result SHALL yield bytes and dimensions equal to that result.

**Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7**

### Property 5: Preview and Auto_Labeler requests stay byte-identical under downscaling

*For any* Labeling_Modality, Label_Set, Detection_Prompt, per-label prompt map, source image bytes, Few_Shot_Example set, Downscale_Setting, Token_Budget_Selection and `llm:` model identifier, the Converse request the Preview_API issues and the Converse request the Auto_Labeler issues SHALL be equal in every element — model id, ordered content blocks in the order of Requirement 8.6, image bytes and formats, prompt text, and inference configuration — and exactly one invocation SHALL be issued per image.

**Validates: Requirements 1.4, 6.1, 6.8, 8.4, 8.6**

### Property 6: Prompt dimensions equal the dimensions of the image actually sent

*For any* source image and *any* Downscale_Setting, the pixel dimensions embedded in the Detection_Prompt and the dimensions used to validate Coordinate_Guidance SHALL both equal the pixel dimensions of the image bytes present in the request's target image block, and SHALL be independent of the dimensions of any attached Few_Shot_Example image.

**Validates: Requirements 7.1, 7.2, 8.2**

### Property 7: Pre_Label geometry is expressed in the original image's coordinate space

*For any* validated Coordinate_Guidance over the Sent_Dimensions, *any* modality, *any* Label_Set and *any* Downscale_Setting, the resulting Pre_Label geometry SHALL lie within the Source_Dimensions bounds, SHALL equal the geometry scaled from the Sent_Dimensions to the Source_Dimensions by the rounding and clamping rule of Requirement 7.3, SHALL equal the pre-feature Pre_Label exactly whenever the Sent_Dimensions equal the Source_Dimensions, and SHALL be unscaled for the Classification modality.

**Validates: Requirements 7.3, 7.4, 7.5, 7.8**

### Property 8: An unconfigured Downscale_Setting reproduces the pre-feature request

*For any* `llm:` job configuration in which the Downscale_Setting is Downscale_Off, absent, null, or malformed in the job record, and the Few_Shot_Option is disabled, absent, null, or malformed, the model request content SHALL be exactly the source image block followed by the text block built from the Detection_Prompt character-for-character, the Label_Set and the Source_Dimensions, with no example image blocks and no example identification content, an omitted Token_Budget_Selection SHALL resolve through the Model_Token_Limits and the default of 10000, and no failure SHALL be attributable to the Downscale_Setting or the Token_Budget_Selection being absent or malformed.

**Validates: Requirements 3.8, 3.10, 5.9, 5.12, 10.1, 10.6, 10.10**

### Property 9: Few-shot selection and image bounds are unchanged by downscaling

*For any* stored example set (at most 10 good and 10 bad in stored order), *any* Model_Image_Limit of at least 1, and *any* Downscale_Setting, the attached example list SHALL equal the first `max(0, Model_Image_Limit - 1)` entries of good examples in stored order followed by bad examples in stored order, the total image count of the request SHALL be at least 1 and at most the Model_Image_Limit, each attached example SHALL carry the downscaled bytes of that example image for the selected setting — the source bytes exactly for Downscale_Off, and a longer edge at most the selected Max_Image_Edge otherwise — and the selection SHALL be identical in the Preview_API and the Auto_Labeler paths.

**Validates: Requirements 8.1, 8.3, 8.4, 8.7, 8.8, 10.7**

### Property 10: Every image yields exactly one categorized outcome from the closed category set

*For any* Preview_Run over 1 to 5 Sample_Images and *any* mix of per-sample conditions (unreadable object, undeterminable Source_Dimensions, undecodable image for the requested Downscale_Setting, unreadable attached example, undecodable attached example, invocation timeout, model error including a rejected token budget, out-of-bounds returned coordinates, unusable output, valid guidance, empty detections), the run SHALL return exactly one Preview_Result per requested Sample_Image, each result SHALL be either a Pre_Label or a failure carrying exactly one category from the pre-feature category set with pre-existing failure reasons reproduced character-for-character, a failure for one Sample_Image SHALL leave every other Sample_Image's outcome unchanged, every sample failing before invocation SHALL have had no model invoked, and the run SHALL record exactly one audit event.

**Validates: Requirements 7.9, 8.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7**

### Property 11: Request validation rejects invalid sizing inputs and touches nothing

*For any* Preview_Run request whose Downscale_Setting is neither Downscale_Off nor a Max_Image_Edge option, or whose Token_Budget_Selection is present and is not an integer between 1 and 128000 inclusive, the Preview_API SHALL reject the request with an error naming every violated rule, SHALL read no referenced object, and SHALL invoke no model; and *for any* Model_Token_Limits change containing an invalid key or value, an over-size mapping, a non-mapping value, or submitted without authority, the Settings_API SHALL reject the change and leave the persisted Model_Token_Limits and the Bedrock_Configuration unchanged.

**Validates: Requirements 3.3, 3.5, 4.2, 4.3, 5.5**

### Property 12: Untouched model families and dimension determination are unchanged

*For any* Labeling_Job configuration using the `sam` model or a `bedrock:` model, the creation validation outcome, the model request content, and the generated Pre_Label SHALL equal the pre-feature behavior, with every image sent at its Source_Dimensions and the Image_Downscaler invoked for no image; and *for any* byte string, the Source_Dimensions determination SHALL return the same result the pre-feature PNG IHDR and JPEG SOF header parsing returned, with an undeterminable-dimension image treated as Downscale_Off and yielding the pre-feature prompt content and Pre_Label outcome.

**Validates: Requirements 7.6, 7.10, 10.4**

### Property 13: Downscaling is bounded in resource use and always yields one outcome

*For any* byte string presented as source image bytes (valid png, valid jpeg, truncated, corrupt, empty, zero-dimension, non-image, and headers declaring a pixel count above Max_Source_Pixel_Count) and *any* Downscale_Setting, the Image_Downscaler SHALL return either a Downscaled_Image or exactly one failure signal identifying the image and the requested Downscale_Setting, SHALL raise no unhandled exception, SHALL leave the source bytes unmodified, SHALL refuse every source whose declared pixel count exceeds Max_Source_Pixel_Count without decoding the full image, and SHALL return within Downscale_Duration_Bound for every source whose pixel count is at most Max_Source_Pixel_Count.

**Validates: Requirements 6.9, 6.10, 6.11**

### Property 14: Token limit writes fully replace and stay isolated from the global configuration

*For any* persisted Model_Token_Limits mapping and *any* valid submitted mapping (including the empty mapping), a Model_Token_Limits write SHALL leave the persisted mapping equal to the submitted mapping entry-for-entry with no omitted entry retained, SHALL leave every Bedrock_Configuration field unchanged, and SHALL produce the same persisted state on repeated submission of the same mapping; *for any* valid Bedrock_Configuration change, the persisted Model_Token_Limits SHALL be unchanged; and after an empty mapping is persisted, the Token_Budget_Resolver SHALL return 10000 for every model identifier with no Token_Budget_Selection.

**Validates: Requirements 1.1, 4.1, 4.4, 4.7, 4.8**
