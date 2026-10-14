# Requirements Document

## Introduction

The DDA labeling system's auto-labeler offers three model families today: `sam` (a container-image Lambda running CPU ONNX SAM, producing *class-agnostic* region proposals that labelers must classify by hand), `bedrock:<id>` (Bedrock vision models answering classification and bounding-box prompts), and `llm:<id>` (prompt-guided coordinate guidance). None of them can do what open-vocabulary detection does: take the *names of the job's labels as text prompts* and return regions that already carry those classes.

This feature adds a fourth family, `grounded-sam`, implementing the Grounded-SAM pipeline — Grounding DINO finds bounding boxes from text prompts, and a SAM-family mask model turns those boxes into segmentation masks. The user-visible payoff is **classified pre-labels**: where a `sam` job hands the labeler unclassified geometry to sort, a `grounded-sam` job hands the labeler regions already tagged `"dent"` or `"scratch"`, so the labeler verifies instead of classifies. Offered for Segmentation (DINO boxes → SAM masks) and ObjectDetection (DINO boxes alone, no mask pass); not for Classification (the family produces geometry, not a whole-image verdict — the same reason `sam` is excluded there).

The scoping decisions, each with its rationale:

- **CPU container-image Lambda following the existing `sam-worker` pattern, not SageMaker GPU.** The `dda_sam_worker` precedent (backend/sam-worker; CDK `DdaSamWorker` at compute-stack.ts ~2107-2177) already solved every operational question this worker raises: models baked into the image at build time behind overridable build-arg URLs, a `deploySamWorker`-style context flag so ordinary portal deploys never pay the Docker build or the model download, a synchronous invoke from the auto-label worker with a presigned image URL, and graceful pre-label failures when the worker is not deployed. Grounding DINO tiny on CPU takes tens of seconds per image — acceptable for background pre-labeling, which is asynchronous fan-out work nobody watches in real time. The per-image invocation bound is raised for this family (240 s vs the sam family's 120 s) to accommodate that latency; the worker Lambda keeps the 300 s / 10 GB envelope of its sibling.
- **Also offered for ObjectDetection.** Grounding DINO's native output *is* boxes; suppressing the mask pass gives ObjectDetection jobs classified box pre-labels in exactly the shape the Bedrock ObjectDetection path already stores and the labeler workspace already renders — no new frontend consumption surface.
- **Per-label text prompt overrides.** The default text prompt for each label is the label name itself, so the feature works with zero extra configuration. But label names are often shop shorthand ("fod", "dent2") that an open-vocabulary detector grounded in natural language cannot ground; an optional per-label override (label "dent" → prompt "small surface dent") entered at job creation and persisted with the job's `auto_label` config fixes that. An empty or absent override means the label name is used. These overrides are a different thing from the existing skip-verification `per_label_prompts` (which are *required* per label and drive Bedrock question prompts); they get their own key, `auto_label.prompt_overrides`, and never interact with the skip-verification field.
- **Detection thresholds are fixed defaults with environment-variable override on the worker — no new UI controls.** Box/text confidence thresholds are model-tuning knobs, not job-configuration decisions; exposing them in the wizard would demand explanation, validation, persistence, and draft-schema surface for a knob almost nobody should turn. The worker ships the community-standard Grounding DINO defaults (box 0.35, text 0.25) and an operator can retune per deployment through Lambda environment variables, the exact mechanism `dda_sam_worker` uses for its grid density and IoU thresholds. Recorded as a scope decision; a wizard control would be a follow-up feature if field evidence demands it.
- **Prompt tuning preview stays `llm:`-only.** The Prompt_Tuning_Preview machinery (sample selection, preview runs, few-shot) is built around the `llm:` request chokepoint. Wiring grounded-sam into it means a second preview executor path and preview-run persistence for a family whose "prompt" is a handful of short phrases. Considered and rejected as a follow-up; this feature's overrides are cheap enough to iterate on by re-creating a job.
- **The Setup_Draft schema change is additive-optional.** The labeling wizard's session-recovery draft (`labelingJobDraft.ts`) validates drafts field-by-field and rejects non-conforming shapes. The new per-label override state is added as an *optional* draft field with explicit read-side defaulting — a pre-feature draft (field absent) restores exactly as before with no overrides, and only a present-but-malformed value invalidates a draft. No version bump; nobody's saved setup is discarded. The round-trip property coverage is extended, not weakened.
- **Preservation throughout.** The `sam`, `bedrock:` and `llm:` families behave byte-identically: their job records gain no keys, their SQS message shapes are untouched (the new family's prompt inputs travel on the job record, which the auto-label worker already fetches per message), their picker entries, wizard controls, and worker paths are unchanged. When `grounded-sam` is not selected, every wizard flow is exactly today's.

## Glossary

Terms carried over from the dda-data-labeling, llm-auto-labeling, llm-autolabel-prompt-tuning, llm-model-picker-search-and-image-filter, and labeling-setup-session-recovery specs keep their existing definitions; they are restated here only where this feature constrains them.

- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **DDA_Labeling_System**: The portal-native data labeling backend: job creation/validation (`dda_labeling.py`), task distribution (`dda_labeling_worker.py`), and auto-label generation (`dda_autolabel_worker.py`).
- **Job_Creator**: A portal user authorized to create labeling jobs within a Use_Case.
- **Labeling_Job**: A persisted DDA labeling job record, including its `auto_label` configuration document.
- **Label_Set**: The ordered list of class names configured for a Labeling_Job.
- **Modality**: The Labeling_Job's task type: Classification, Segmentation, or ObjectDetection.
- **Auto_Label_Picker**: The "Auto-label model" selection control in the DDA labeling-job wizard (`CreateLabelingJob.tsx`), offering entries per the model/modality compatibility matrix.
- **Auto_Labeler**: The SQS consumer (`dda_autolabel_worker.py`) that generates one Pre_Label per dataset image using the Labeling_Job's selected model.
- **Pre_Label**: The per-image annotation JSON the Auto_Labeler writes to the portal artifacts bucket and the labeler workspace renders as the editable starting layer.
- **Classified_Pre_Label**: A Pre_Label whose regions or boxes each carry a class from the Label_Set (as the `bedrock:`/`llm:` families produce), in contrast to the class-agnostic (`class: null`) Pre_Labels of the `sam` family.
- **Grounded_SAM_Family**: The auto-label model family this feature adds, with the selection value `grounded-sam`.
- **Grounded_SAM_Entry**: The static Auto_Label_Picker option labeled "Grounded-SAM (text-prompted)" with value `grounded-sam`.
- **Grounding_DINO**: The open-vocabulary object detection model (ONNX export of grounding-dino-tiny) that maps an image plus Text_Prompts to scored bounding boxes.
- **SAM_Mask_Model**: The SAM-family ONNX encoder/decoder pair (samexporter naming convention, MobileSAM by default) that converts a bounding-box prompt into a segmentation mask.
- **Grounded_SAM_Worker**: The new container-image Lambda (`backend/grounded-sam-worker`, CDK id `DdaGroundedSamWorker`) that runs Grounding_DINO and, for Segmentation, the SAM_Mask_Model, and answers the Auto_Labeler's synchronous invocations.
- **Text_Prompt**: The text phrase submitted to Grounding_DINO for one Label_Set label.
- **Prompt_Override**: An optional Job_Creator-entered replacement Text_Prompt for one label, persisted under `auto_label.prompt_overrides` keyed by label name.
- **Prompt_Map**: The ordered list of `{label, prompt}` pairs derived from the Label_Set and the Prompt_Overrides: each label's prompt is its Prompt_Override when one is present and non-empty after trimming, otherwise the label name itself.
- **Detection_Thresholds**: The Grounded_SAM_Worker's confidence cutoffs — the Box_Threshold (minimum overall detection confidence, default 0.35) and the Text_Threshold (minimum prompt-attribution confidence, default 0.25) — fixed defaults overridable through worker environment variables.
- **Worker_Flag**: The CDK context flag `deployGroundedSamWorker` gating the Grounded_SAM_Worker's deployment (default off), mirroring the existing `deploySamWorker` flag.
- **Setup_Draft**: The versioned wizard-state snapshot persisted per Use_Case by the labeling-setup-session-recovery feature (`labelingJobDraft.ts`).
- **RLE**: The portal's canonical run-length mask encoding — COCO-style uncompressed counts over the column-major flattening, background run first, as produced by the shared layer's `dda_manifest.rle_encode`.

## Requirements

### Requirement 1: Grounded-SAM as a selectable auto-label model family

**User Story:** As a Job_Creator, I want to pick Grounded-SAM as the auto-label model for segmentation and object detection jobs, so that pre-labels arrive already tagged with my label names instead of as unclassified geometry.

#### Acceptance Criteria

1. WHILE the wizard's Modality is Segmentation or ObjectDetection and model-assisted pre-labeling is enabled, THE Portal SHALL offer the Grounded_SAM_Entry in the Auto_Label_Picker as a static entry beside the existing SAM entry, labeled "Grounded-SAM (text-prompted)" with the selection value `grounded-sam`.
2. WHILE the wizard's Modality is Classification, THE Portal SHALL offer no Grounded_SAM_Entry.
3. WHEN a Job_Creator selects the Grounded_SAM_Entry, THE Portal SHALL record the selection value `grounded-sam` and SHALL judge that value compatible with exactly the Segmentation and ObjectDetection modalities in the wizard's model/modality compatibility check.
4. WHEN the Modality changes to Classification while `grounded-sam` is the recorded selection, THE Portal SHALL clear the auto-label model selection through the same incompatible-selection clearing that applies to the existing families.
5. WHEN a labeling job submission carries the auto-label model value `grounded-sam` with the Segmentation or ObjectDetection Modality, THE DDA_Labeling_System SHALL accept the model value and persist it as the Labeling_Job's `auto_label.model`.
6. IF a labeling job submission carries the auto-label model value `grounded-sam` with the Classification Modality, THEN THE DDA_Labeling_System SHALL reject the submission with a validation error identifying the model value and the Modality.
7. WHEN a Labeling_Job with the model value `grounded-sam` is created, THE DDA_Labeling_System SHALL record the job-creation audit event with the model value `grounded-sam` and the auto-label mode `grounded-sam`.

### Requirement 2: Per-label text prompt overrides

**User Story:** As a Job_Creator, I want to optionally rephrase the text prompt sent to the detector for each label, so that shorthand label names still ground to the right objects.

#### Acceptance Criteria

1. WHILE `grounded-sam` is the recorded auto-label model selection, THE Portal SHALL present one optional Prompt_Override text entry per label of the wizard's current effective Label_Set, presenting the label name as the entry's placeholder.
2. WHILE any other auto-label model selection (or none) is recorded, THE Portal SHALL present no Prompt_Override entries.
3. WHEN a labeling job submission with the model value `grounded-sam` is assembled, THE Portal SHALL include under `auto_label.prompt_overrides` exactly the Prompt_Override entries that are non-empty after trimming and whose label belongs to the submitted Label_Set, each transmitted character-for-character as entered, and SHALL omit the `prompt_overrides` key entirely when no such entry exists.
4. WHEN the DDA_Labeling_System accepts a `grounded-sam` submission carrying `prompt_overrides`, THE DDA_Labeling_System SHALL persist each override character-for-character under the Labeling_Job's `auto_label.prompt_overrides`, dropping entries that are empty after trimming, and SHALL persist no `prompt_overrides` key when no override remains.
5. IF a `grounded-sam` submission carries a `prompt_overrides` value that is not an object mapping label names to strings, or carries a key that is not a member of the submitted Label_Set, THEN THE DDA_Labeling_System SHALL reject the submission with a validation error identifying the offending content.
6. IF a Prompt_Override exceeds 256 characters, THEN THE Portal SHALL reject the wizard step with an error naming the label, and THE DDA_Labeling_System SHALL reject a submission carrying such an override with a validation error naming the label.
7. WHEN the DDA_Labeling_System derives the Prompt_Map for a Labeling_Job, THE DDA_Labeling_System SHALL produce one `{label, prompt}` pair per Label_Set label in Label_Set order, using the label's persisted Prompt_Override where one is present and non-empty after trimming and the label name itself otherwise.
8. WHEN a labeling job submission with a model value other than `grounded-sam` is assembled or validated, THE Portal and THE DDA_Labeling_System SHALL attach no `prompt_overrides` key, so that records of the other families are byte-identical to records created before this feature.

### Requirement 3: Grounded-SAM worker inference

**User Story:** As a portal operator, I want a self-contained CPU worker that turns text prompts into classified boxes and masks, so that pre-labeling needs no GPU fleet and no external service.

#### Acceptance Criteria

1. WHEN the Grounded_SAM_Worker is invoked with an event carrying an https presigned image URL, a non-empty Prompt_Map, and the Segmentation Modality, THE Grounded_SAM_Worker SHALL return a JSON object carrying `regions` — one entry per retained detection with `class` equal to the originating Prompt_Map label, `rle` holding the detection's mask at source resolution, and `score` — plus the source `image_width` and `image_height`.
2. WHEN the Grounded_SAM_Worker is invoked with the ObjectDetection Modality, THE Grounded_SAM_Worker SHALL return `regions` entries carrying `class`, `score`, and `box` (an object with `left`, `top`, `width`, `height` in source-image pixels) without running the SAM_Mask_Model.
3. THE Grounded_SAM_Worker SHALL derive detections by submitting the Prompt_Map's prompts to Grounding_DINO in a single caption, SHALL attribute each detection to exactly one Prompt_Map entry, and SHALL emit `class` values drawn only from the Prompt_Map's labels.
4. THE Grounded_SAM_Worker SHALL retain only detections whose confidence meets the Box_Threshold and whose prompt attribution meets the Text_Threshold, SHALL suppress near-duplicate detections of the same label by greedy IoU-based deduplication, and SHALL cap the returned detections at a maximum count, keeping the highest-scoring detections.
5. THE Grounded_SAM_Worker SHALL read the Box_Threshold (default 0.35), the Text_Threshold (default 0.25), the deduplication IoU threshold, and the maximum detection count from environment variables, with no per-job or wizard-level control surface.
6. THE Grounded_SAM_Worker SHALL clamp every returned box to the source image bounds and SHALL drop detections whose clamped box has no positive area.
7. THE Grounded_SAM_Worker SHALL encode every returned `rle` value in the portal's canonical RLE form, matching the shared layer's `dda_manifest.rle_encode` output for the same mask.
8. IF the invocation event lacks a usable image source, carries an empty or malformed Prompt_Map, or names a Modality other than Segmentation or ObjectDetection, THEN THE Grounded_SAM_Worker SHALL raise an error so the synchronous caller records the invocation as a Pre_Label generation failure.
9. THE Grounded_SAM_Worker SHALL load the Grounding_DINO model, its tokenizer, and the SAM_Mask_Model from image-baked files resolved through environment-variable paths, and THE Grounded_SAM_Worker's container image SHALL bake those files at build time from build-argument URLs with pinned defaults, each overridable per build.
10. WHERE zero detections survive the Detection_Thresholds, THE Grounded_SAM_Worker SHALL return an empty `regions` list with the source image dimensions, and the invocation SHALL count as a success.

### Requirement 4: Auto-labeler integration

**User Story:** As a labeler, I want grounded-sam pre-labels to appear on my tasks with classes already assigned, so that I verify annotations instead of drawing and classifying them.

#### Acceptance Criteria

1. WHEN the Auto_Labeler processes a fan-out message whose model value is `grounded-sam`, THE Auto_Labeler SHALL invoke the Grounded_SAM_Worker synchronously with a time-limited presigned URL for the message's image, the Prompt_Map derived per Requirement 2.7 from the message's Label_Set and the Labeling_Job record's `prompt_overrides`, and the message's Modality.
2. IF the Grounded_SAM_Worker's function name is absent from the Auto_Labeler's environment, THEN THE Auto_Labeler SHALL record the image's Pre_Label generation as failed with a reason stating the worker is not configured.
3. IF a Grounded_SAM_Worker invocation exceeds 240 seconds of wall-clock time, THEN THE Auto_Labeler SHALL record the image's Pre_Label generation as failed with the invocation error as the reason.
4. IF the Grounded_SAM_Worker invocation returns a function error, an unparseable payload, or a payload lacking a `regions` list or integer image dimensions, THEN THE Auto_Labeler SHALL record the image's Pre_Label generation as failed with a descriptive reason.
5. IF a returned region carries a `class` outside the message's Label_Set, lacks its modality's geometry (`rle` for Segmentation, `box` for ObjectDetection), or carries an ObjectDetection box with non-positive dimensions or coordinates outside the returned image bounds, THEN THE Auto_Labeler SHALL record the image's Pre_Label generation as failed with a descriptive reason.
6. WHEN a Segmentation invocation succeeds, THE Auto_Labeler SHALL store the Pre_Label as `{modality, regions: [{class, rle, score?}], image_width, image_height}` with each region's class taken from the worker response.
7. WHEN an ObjectDetection invocation succeeds, THE Auto_Labeler SHALL store the Pre_Label as `{modality, boxes: [{class, left, top, width, height}], image_width, image_height}` — the identical shape the Bedrock ObjectDetection path stores, so the labeler workspace consumes it with no change.
8. WHEN a `grounded-sam` Pre_Label resolves (success or failure), THE Auto_Labeler SHALL apply the same task-state, artifact-storage, duplicate-delivery idempotency, storage-failure retry, and skip-verification counter semantics that apply to the `sam` family today.

### Requirement 5: Gated worker deployment

**User Story:** As a portal operator, I want the grounded-sam worker deployed only when I explicitly ask for it, so that routine portal deployments never pay a multi-gigabyte Docker build or model downloads.

#### Acceptance Criteria

1. WHERE the Worker_Flag is set true at synthesis, THE compute stack SHALL define the Grounded_SAM_Worker as a container-image Lambda function (10240 MB memory, 300 s timeout, x86_64 architecture with the image platform pinned to linux/amd64), SHALL set the worker's function name into the Auto_Labeler's environment as `GROUNDED_SAM_WORKER_FUNCTION_NAME`, and SHALL grant the Auto_Labeler invoke permission on the worker.
2. WHERE the Worker_Flag is absent or not true, THE compute stack SHALL define no Grounded_SAM_Worker resources and SHALL leave the Auto_Labeler's environment without a `GROUNDED_SAM_WORKER_FUNCTION_NAME` entry.
3. WHERE the Worker_Flag is set true, THE compute stack SHALL pass any provided model-source context values (Grounding_DINO model URL, tokenizer URL, SAM archive URL) to the image build as build arguments, leaving the Dockerfile's pinned defaults in force when none is provided.
4. WHILE the Grounded_SAM_Worker is not deployed, THE DDA_Labeling_System SHALL keep accepting `grounded-sam` job creations, and THE Auto_Labeler SHALL resolve each of the job's images as a Pre_Label generation failure per Requirement 4.2 — the same degradation the `sam` family exhibits when its worker is not deployed.
5. THE compute stack SHALL leave the existing `DdaSamWorker` definition, its `deploySamWorker` gating, and every other function's configuration unchanged by this feature.

### Requirement 6: Setup draft compatibility

**User Story:** As a Job_Creator, I want my in-progress grounded-sam setup — including prompt overrides — to survive a refresh or session expiry, without invalidating drafts saved before this feature existed.

#### Acceptance Criteria

1. WHEN the wizard captures a Setup_Draft, THE Portal SHALL include the Prompt_Override entries as an additive draft field alongside the existing fields.
2. WHEN a Setup_Draft carrying the Prompt_Override field is restored, THE Portal SHALL return the entries to the Prompt_Override controls exactly as saved.
3. WHEN a Setup_Draft lacking the Prompt_Override field is read, THE Portal SHALL accept the draft and restore it with zero Prompt_Override entries, so that every draft saved before this feature restores exactly as it did.
4. IF a stored draft's Prompt_Override field is present but does not conform to an object mapping strings to strings, THEN THE Portal SHALL treat the stored content as no draft, per the draft store's existing non-conforming-shape rule.
5. WHEN a Setup_Draft is written and read back, THE Portal SHALL preserve the Prompt_Override entries through the round trip, and THE Portal SHALL judge two wizard states differing only in Prompt_Override entries as differing for the draft save gate.

### Requirement 7: Preservation of existing behavior

**User Story:** As a portal operator, I want the existing auto-label families and wizard flows untouched, so that adding a model family breaks nothing that works today.

#### Acceptance Criteria

1. WHEN a Labeling_Job of the `sam`, `bedrock:` or `llm:` family is created or processed, THE DDA_Labeling_System SHALL produce byte-identical job records, fan-out messages, Pre_Label artifacts, and task-state transitions to those produced before this feature.
2. THE Portal SHALL leave every existing Auto_Label_Picker entry, group header, label decoration, capability filtering, and type-to-search behavior unchanged, the Grounded_SAM_Entry being the only addition.
3. WHILE a model other than `grounded-sam` is selected (or none), THE Portal SHALL render every wizard control exactly as before this feature, and THE Portal SHALL never render the `llm:`-only controls (detection prompt, few-shot, sizing, prompt tuning preview) for a `grounded-sam` selection.
4. WHEN the Auto_Labeler processes a message of an existing family, THE Auto_Labeler SHALL execute the identical code path it executes today, including the `sam` family's 120-second invocation bound and class-agnostic stored shape.
5. THE DDA_Labeling_System SHALL leave the skip-verification flow's validation, persistence, and fan-out semantics unchanged, including for jobs whose auto-label model is `grounded-sam`.
6. WHEN a pre-feature Labeling_Job record (no `prompt_overrides` key) or a pre-feature Setup_Draft (no Prompt_Override field) is read by any consumer, THE Portal and THE DDA_Labeling_System SHALL process it exactly as before this feature.
