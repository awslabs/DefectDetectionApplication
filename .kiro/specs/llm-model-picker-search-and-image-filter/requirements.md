# Requirements Document

## Introduction

The DDA labeling job creation wizard offers model-assisted pre-labeling. When auto-labeling is enabled, the Auto_Label_Picker lists every model of the Model_Catalog twice — once as a `bedrock:<id>` entry (per-label-prompt Bedrock vision family) and once as an `llm:<id>` entry (prompt-guided family, the one behind the Prompt_Tuning_Preview). The Model_Catalog (GET `/data-accounts/bedrock-configuration/models`, built by `data_accounts.list_bedrock_model_options`) merges every system inference profile with every ACTIVE, on-demand foundation model in the configured region. That is a long list, and most of it is text-only models — embedding models and text generators that cannot label images at all, while **both** auto-label families send the dataset image to the model on every request. The Job_Creator scrolls a picker roughly twice the size of the full regional catalog to find one of the handful of vision models that actually work.

This feature makes the Auto_Label_Picker usable in two independent ways:

**1. Image-input filtering.** `bedrock:ListFoundationModels` summaries already carry `inputModalities` (for example `['TEXT','IMAGE']`) — the authoritative statement of what a model accepts — but the Model_Catalog_Endpoint currently ignores it. `bedrock:ListInferenceProfiles` summaries carry no modality data at all; a profile's capability is only knowable through the foundation model it fronts (a profile id is `<prefix>.<fronted-model-id>`, a join the endpoint already computes for deduplication). This feature has the endpoint resolve an Image_Input_Capability for every Model_Option and annotate it as an additive per-option field, and has the Auto_Label_Picker exclude the models positively known to lack image input.

**2. Type-to-search.** A text filter inside the Auto_Label_Picker narrows the remaining options by label or model identifier as the Job_Creator types.

Three decisions shape the requirements, each with a stated rationale:

- **A model is excluded only on positive knowledge.** A Model_Option is excluded from the Auto_Label_Picker only when its resolved Input_Modalities are present and lack `IMAGE` (Text_Only). Unknown_Capability — no modality data resolvable, whether because `bedrock:ListFoundationModels` was denied, a profile's fronted model is not in the summaries, or the summary simply carries no modality list — means **included**. Rationale: the catalog already degrades gracefully on missing permissions (empty list plus a `permissions` hint, with the wizard falling back to free-text entry), and filtering on absent data would silently hide invokable models; unknown data must never shrink the list.
- **The catalog response change is strictly additive.** The Model_Catalog_Endpoint keeps returning the full catalog — it excludes nothing — and every pre-feature field of every option (`id`, `label`, `image_limit`, `token_limit`), the ordering, the deduplication, and the rest of the payload are unchanged. Only the Auto_Label_Picker applies the exclusion. Rationale: the same endpoint feeds the settings-page Bedrock configuration dropdown (a text-use consumer that needs every model, including text-only ones) and the admin-only skip-verification model select; a server-side exclusion would break them.
- **Search is display-only narrowing.** Typing in the picker filters what is shown (case-insensitive substring over the option's label and its catalog model identifier, with an explicit empty-result indication); it never changes the selected model, and it operates over the already capability-filtered options.

Everything else is preserved: the settings-page model dropdown behaves byte-for-byte as today, the skip-verification model select keeps the full catalog, the free-text fallback when the catalog is unavailable is untouched, and the per-model lookups the wizard performs for a selected model (the few-shot image-limit hint from `image_limit`, the token budget pre-fill from `token_limit`) keep reading the full catalog and keep resolving the same values.

## Glossary

Terms carried over from the dda-data-labeling, llm-autolabel-prompt-tuning, and llm-model-token-and-image-sizing specs keep their existing definitions and are restated here only where this feature constrains them.

- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **DDA_Labeling_System**: The portal-native data labeling backend and its job-creation wizard surfaces.
- **Job_Creator**: A portal user authorized to create labeling jobs (DataScientist, UseCaseAdmin, or PortalAdmin) within a Use_Case.
- **Model_Catalog**: The payload of GET `/data-accounts/bedrock-configuration/models`: the merged, deduplicated, sorted list of Model_Options, plus the resolved region and an optional `permissions` hint.
- **Model_Catalog_Endpoint**: The backend handler (`data_accounts.list_bedrock_model_options`) that builds and returns the Model_Catalog.
- **Model_Option**: One entry of the Model_Catalog, carrying before this feature exactly `id`, `label`, `image_limit`, and `token_limit`.
- **Foundation_Model_Option**: A Model_Option derived from a `bedrock:ListFoundationModels` summary (included only when ACTIVE, on-demand invokable, and not fronted by a listed inference profile).
- **Inference_Profile_Option**: A Model_Option derived from a `bedrock:ListInferenceProfiles` summary, listed as `{id: inferenceProfileId, label: inferenceProfileName}`.
- **Fronted_Model**: The foundation model an inference profile routes to; the profile id is `<prefix>.<fronted-model-id>`, so the Fronted_Model id is the portion of the profile id following the first `.`.
- **Input_Modalities**: The `inputModalities` list of a `bedrock:ListFoundationModels` summary (for example `['TEXT','IMAGE']`), the authoritative statement of the input content types the model accepts.
- **Image_Input_Capability**: The per-Model_Option classification this feature resolves: Image_Capable, Text_Only, or Unknown_Capability.
- **Image_Capable**: The Image_Input_Capability of a Model_Option whose resolved Input_Modalities list contains the entry `IMAGE`.
- **Text_Only**: The Image_Input_Capability of a Model_Option whose resolved Input_Modalities list is non-empty and contains no entry `IMAGE` — the only classification that positively establishes the model does not accept image input.
- **Unknown_Capability**: The Image_Input_Capability of a Model_Option for which no Input_Modalities list is resolvable.
- **Auto_Label_Picker**: The "Auto-label model" selection control in the DDA labeling-job wizard, offering the `sam` entry, the Bedrock_Auto_Label_Family, and the LLM_Auto_Label_Family according to the modality compatibility matrix.
- **Bedrock_Auto_Label_Family**: The Auto_Label_Picker option group whose entries are `bedrock:<Model_Option id>` values built from the Model_Catalog.
- **LLM_Auto_Label_Family**: The Auto_Label_Picker option group whose entries are `llm:<Model_Option id>` values built from the Model_Catalog (the prompt-guided family).
- **Picker_Search**: The type-to-filter capability inside the Auto_Label_Picker added by this feature.
- **Search_Text**: The text a Job_Creator has typed into the Picker_Search entry.
- **Skip_Verification_Picker**: The admin-only "Bedrock model" select in the wizard's skip-verification section, which chooses the model that auto-labels the whole dataset with per-label prompts.
- **Settings_Model_Dropdown**: The model select of the Bedrock configuration settings page (`BedrockConfigurationSettings`), used to choose the Portal's global Bedrock model for text-use consumers (workflow generation, code assist, node designer).
- **Free_Text_Fallback**: The existing wizard affordance that presents a plain model-identifier text input for the LLM_Auto_Label_Family when the Model_Catalog is unavailable, so prompt-guided auto-labeling stays reachable without a catalog.
- **Catalog_Unavailable**: The existing wizard state entered when the Model_Catalog request fails or returns zero Model_Options (including the missing-permissions degradation).

## Requirements

### Requirement 1: Model Catalog Carries Image-Input Capability

**User Story:** As a Job_Creator, I want the model catalog to state which models accept image input, so that the labeling wizard can distinguish vision models from text-only models without guessing from model names.

#### Acceptance Criteria

1. WHEN the Model_Catalog_Endpoint builds a Foundation_Model_Option, THE Model_Catalog_Endpoint SHALL resolve that option's Image_Input_Capability from the Input_Modalities of that option's own `bedrock:ListFoundationModels` summary: Image_Capable WHERE the Input_Modalities list contains the entry `IMAGE`, and Text_Only WHERE the Input_Modalities list is non-empty and contains no entry `IMAGE`.
2. WHEN the Model_Catalog_Endpoint builds an Inference_Profile_Option, THE Model_Catalog_Endpoint SHALL resolve that option's Image_Input_Capability from the Input_Modalities of the Fronted_Model's summary within the same `bedrock:ListFoundationModels` response, matching the Fronted_Model id by exact string comparison against each summary's model id, and SHALL perform that resolution over every summary of the response, including summaries that the Foundation_Model_Option filters (lifecycle status, on-demand invokability, and fronted-model deduplication) exclude from the returned Model_Options.
3. IF no Input_Modalities list is resolvable for a Model_Option — the summary carries no Input_Modalities list, the carried value is not a list, the carried list is empty, the profile id contains no `.` separator, the Fronted_Model id matches no summary, or the `bedrock:ListFoundationModels` call was denied — THEN THE Model_Catalog_Endpoint SHALL resolve that Model_Option's Image_Input_Capability as Unknown_Capability.
4. THE Model_Catalog_Endpoint SHALL annotate the resolved Image_Input_Capability on each Model_Option as an additive per-option field that distinguishes Image_Capable, Text_Only, and Unknown_Capability, and SHALL include every Model_Option in the Model_Catalog regardless of its Image_Input_Capability, so that the Model_Catalog continues to carry the full catalog for every consumer.
5. THE Model_Catalog_Endpoint SHALL resolve an Image_Input_Capability for every combination of profile summaries and foundation model summaries, SHALL raise no error and fail no Model_Catalog request on account of any Input_Modalities value, and SHALL leave the Image_Input_Capability resolution without effect on which Model_Options are returned, their order, and every pre-feature field value.

### Requirement 2: Auto-Label Picker Excludes Models Known to Lack Image Input

**User Story:** As a Job_Creator, I want models that cannot take images excluded from the auto-label model list, so that the picker offers only models that can actually label my images.

#### Acceptance Criteria

1. WHEN the Portal builds the Auto_Label_Picker options from the Model_Catalog, THE Portal SHALL build the Bedrock_Auto_Label_Family and the LLM_Auto_Label_Family entries from exactly the Model_Options whose Image_Input_Capability is not Text_Only, preserving the Model_Catalog's option order within each family.
2. WHERE a Model_Option's Image_Input_Capability is Unknown_Capability, THE Portal SHALL include that Model_Option in both the Bedrock_Auto_Label_Family and the LLM_Auto_Label_Family, so that absent capability data never removes a model from the Auto_Label_Picker.
3. THE Portal SHALL apply the Text_Only exclusion to the Bedrock_Auto_Label_Family and the LLM_Auto_Label_Family entries only, and SHALL derive the `sam` entry's presence solely from the modality compatibility matrix, unchanged by this feature.
4. IF the Model_Catalog contains at least one Model_Option and every Model_Option's Image_Input_Capability is Text_Only, THEN THE Portal SHALL display with the Auto_Label_Picker an indication that no catalog model accepts image input and SHALL present the Free_Text_Fallback's model-identifier entry for the LLM_Auto_Label_Family, so that a Job_Creator is never left without a way to configure a prompt-guided model.
5. WHEN a Job_Creator selects an Auto_Label_Picker entry, THE Portal SHALL record the same `sam`, `bedrock:<Model_Option id>`, or `llm:<Model_Option id>` selection value that the entry recorded before this feature, so that the exclusion changes which entries are offered and nothing about what a selection means.

### Requirement 3: Type-to-Search in the Auto-Label Picker

**User Story:** As a Job_Creator, I want to type in the auto-label model picker to narrow the list, so that I can find a specific model quickly instead of scrolling.

#### Acceptance Criteria

1. WHILE the Auto_Label_Picker's option list is open, THE Portal SHALL present a Picker_Search text entry within the Auto_Label_Picker.
2. WHEN a Job_Creator enters a non-empty Search_Text, THE Portal SHALL display every offered Bedrock_Auto_Label_Family and LLM_Auto_Label_Family entry whose displayed label or whose Model_Option id contains the Search_Text as a case-insensitive substring.
3. WHILE a non-empty Search_Text is entered, THE Portal SHALL display only entries for which the Search_Text is a case-insensitive substring of the entry's displayed label, of the entry's Model_Option id, or of the entry's family-prefixed selection value.
4. IF a non-empty Search_Text matches no offered entry, THEN THE Portal SHALL display within the Auto_Label_Picker an indication that no model matches the Search_Text.
5. THE Portal SHALL leave the recorded auto-label model selection unchanged by entering, changing, or clearing Search_Text; SHALL restore the full set of offered entries when the Search_Text is cleared; and SHALL record, for a selection made while a Search_Text is entered, the same selection value that selecting the same entry with no Search_Text records.
6. WHILE any Search_Text is entered, THE Portal SHALL display no Model_Option whose Image_Input_Capability is Text_Only, so that Picker_Search narrows the capability-filtered entries of Requirement 2 and never reintroduces an excluded model.

### Requirement 4: Preservation of Existing Behavior

**User Story:** As a portal operator, I want every current consumer of the model catalog to keep working exactly as it does today, so that narrowing the labeling picker breaks nothing else.

#### Acceptance Criteria

1. WHEN the Settings_Model_Dropdown loads the Model_Catalog, THE Portal SHALL offer every Model_Option of the Model_Catalog — including every Text_Only Model_Option — and SHALL apply to the Settings_Model_Dropdown the same option content, the same existing type-to-filter behavior, the same not-in-list stored-model handling, and the same free-text fallback it applied before this feature.
2. WHEN the Skip_Verification_Picker is displayed, THE Portal SHALL offer every Model_Option of the Model_Catalog — including every Text_Only Model_Option — and SHALL apply to the Skip_Verification_Picker the same option content and the same free-text fallback behavior it applied before this feature.
3. WHILE Catalog_Unavailable holds, THE Portal SHALL apply the Catalog_Unavailable degradation that applied before this feature: the unavailability notice, the Free_Text_Fallback for the LLM_Auto_Label_Family, and the Settings_Model_Dropdown's free-text entry, each triggered by the same conditions that triggered them before this feature.
4. WHEN the Model_Catalog_Endpoint returns a Model_Catalog, THE Model_Catalog_Endpoint SHALL return every pre-feature field of every Model_Option (`id`, `label`, `image_limit`, `token_limit`) with unchanged values, the pre-feature option ordering (anthropic-first, then alphabetical), the pre-feature deduplication (an inference profile winning over its Fronted_Model), the pre-feature region resolution including the `?region` override, the pre-feature `permissions` hint on denied list permissions, and the pre-feature error response on unexpected failure.
5. IF the `bedrock:ListFoundationModels` call is denied while the `bedrock:ListInferenceProfiles` call succeeds, THEN THE Model_Catalog_Endpoint SHALL return every Inference_Profile_Option with Unknown_Capability alongside the `permissions` hint, exactly as many options as it returned before this feature, and THE Portal SHALL include every such option in the Auto_Label_Picker per Requirement 2.2.
6. WHEN the Portal resolves the per-model values for a selected auto-label model — the few-shot attach/omit hint from `image_limit` and the token budget pre-fill from `token_limit` — THE Portal SHALL resolve them from the full Model_Catalog with the same fallback defaults as before this feature, for every selected model identifier including one entered through the Free_Text_Fallback and one belonging to a Text_Only Model_Option.
7. THE Portal SHALL apply the pre-feature modality compatibility matrix to which families the Auto_Label_Picker offers, and SHALL keep each family's pre-feature group header and entry label decoration (`Bedrock: <label>` and `<label> (prompt-guided)`).
8. WHEN a consumer of the Model_Catalog other than the Auto_Label_Picker reads the Model_Catalog, THE Portal SHALL produce for that consumer the same behavior it produced before this feature, with the additive Image_Input_Capability field ignored by every consumer that does not read it.
