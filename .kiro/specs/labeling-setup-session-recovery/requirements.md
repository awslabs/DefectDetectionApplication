# Requirements Document

## Introduction

The DDA labeling-job creation wizard (`edge-cv-portal/frontend/src/pages/CreateLabelingJob.tsx`, route `/labeling/create`) holds every piece of setup state in client-side React state: job name, description, use case, dataset S3 URI, modality, labeling team, label set, instructions, auto-label model, detection prompt, few-shot toggle, good/bad example images, downscale setting, token budget, the skip-verification configuration, and — inside the embedded Prompt_Tuning_Preview — the sample-image selection and the active preview run. A browser refresh, tab close, crash, or Cognito session expiry loses all of it, and there is no route back into an in-progress setup. The users this hurts most are the ones iterating on prompts: the expensive loop (tune prompt → run preview → inspect results) loses its entire context, even though the Preview_Run itself keeps executing and its results keep existing **server-side** (`PREVIEW#<run_id>` items in the labeling-tasks table, result payloads under `labeling-previews/` in the artifacts bucket).

This feature makes the setup recoverable: the wizard continuously saves a Setup_Draft to browser localStorage, and re-opening `/labeling/create` with a draft present offers to restore it or discard it. A restored wizard resumes polling an in-flight Preview_Run — or re-displays a completed one — exactly the way the workflow TestPanel already resumes its test runs.

Five decisions shape the requirements, each with a stated rationale:

- **The draft is client-side localStorage, per browser profile, keyed by use case.** No backend, API, or infrastructure change. Rationale: the reported loss is a same-browser event (refresh, tab close, session expiry on the machine the user is sitting at), and localStorage survives all of them including logout/re-login; the repo already established exactly this pattern twice (`TestPanel.tsx` persists the active test run under `edgeCvPortal.activeTestRun` with tolerant read/persist/clear helpers and resumes polling on the next mount; `UsecaseContext.tsx` persists the selected use case). A server-side draft API (cross-device recovery) would still not restore the one thing browser storage cannot hold either — un-uploaded `File` objects — so it buys little for the reported problem and is a separable follow-up. Keying per use case (`edgeCvPortal.labelingJobDraft.<usecase_id>`) keeps drafts for different use cases from clobbering each other and matches the wizard's per-use-case entry (`?usecase_id=` from the Labeling page).
- **Only recoverable example-image state is persisted: uploaded S3 refs, never Files.** `File` objects cannot be persisted by any browser storage. But the wizard already stages example images to S3 *before* use (`uploadExampleImages` presign-PUTs each file; `exampleUploadCache` memoizes the resulting `s3://` refs per file-set identity), and both the preview request (`few_shot.examples[].ref`) and job submission (`example_images: {good, bad}`) consume **refs, not bytes**. The refs live under `labeling-examples/` in the Use_Case data bucket with no expiring lifecycle rule, and the backend validates refs by bucket membership only. So the draft persists exactly the refs of the current Example_Upload_Cache — populated by the first preview run or submission attempt — and the restored wizard represents them as named, removable Restored_Example_References that flow into preview and submission exactly as refs already do, alongside any newly added files. Files added but never uploaded are genuinely ephemeral and are knowingly not restored.
- **The Preview_Run reference is kept and resumed within a backend-derived window.** `POST /labeling-preview/runs` executes as an async Lambda self-invoke: the run keeps running server-side across a refresh, and `GET /labeling-preview/runs/{runId}` re-answers status and results with per-request presigned payload URLs. The RUN item's logical `expires_at` is `start + min(sample_count×120+60, 900)` seconds and its DynamoDB `ttl` (reaping, never earlier than) is `expires_at + 3600`; result payloads expire from S3 after one day. The draft therefore resumes (one poll — Running shows live progress, terminal status re-displays results, a 404 shows the existing "no longer available" message) only when the run started within the guaranteed-readable window derived from those constants, and silently drops older references. Rationale: aligning with `dda_labeling.py`'s persistence TTLs avoids resurrecting references the backend has already reaped, and reusing the existing poll loop (the TestPanel resume pattern) adds no new result-rendering machinery.
- **Restore is an explicit choice, and saving is suppressed until it is made.** On entering the wizard with a draft present for the resolved use case, a Restore_Offer with exactly two actions — restore or discard — is shown once; while it is unresolved, no draft save runs. Rationale: a debounced save racing an unresolved offer could overwrite the very draft it is offering to restore; two explicit actions make the outcome unambiguous. The draft is cleared on successful job creation and on discard, drafts older than 14 days are treated as absent and purged, and any unreadable content (corrupt JSON, unknown schema version, mismatched use case) is treated as no draft — the versioned, tolerant-parse discipline of the TestPanel precedent.
- **Persistence is side-effect-free on every pre-existing flow.** Saving a draft only reads wizard state and writes localStorage; it changes no validation outcome, no submission payload byte, and no preview request byte. The wizard with no stored draft renders and behaves exactly as today (no offer, no new controls beyond none); `PromptTuningPreview` without its new optional props behaves exactly as today (the established optional-callback pattern of `onDownscaleMaxEdgeChange`/`onTokenBudgetChange`). The draft contains only setup metadata and S3 refs — the same sensitivity as the page itself — and never any credential material; it survives re-login by construction (localStorage is not cleared by Cognito expiry), and cross-user reads on a shared browser profile are bounded by the backend's existing authorization (a Preview_Run answers 404 to any caller other than its creator; refs and job creation are RBAC-checked per use case on every request).

Also decided: **no new route.** The recovery entry point is re-opening `/labeling/create` (with or without `?usecase_id=`), where the offer appears for the resolved use case — matching how users reach the wizard today. And the restored auto-label model is the recorded `llm:<id>` / `bedrock:<id>` / `sam` selection value verbatim, independent of what the capability-filtered Auto_Label_Picker (llm-model-picker-search-and-image-filter) currently offers, so a catalog or capability change never corrupts a restored selection.

## Glossary

Terms carried over from the dda-data-labeling, llm-auto-labeling, llm-autolabel-prompt-tuning, llm-model-token-and-image-sizing, and llm-model-picker-search-and-image-filter specs keep their existing definitions and are restated here only where this feature constrains them.

- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **Job_Creator**: A portal user authorized to create labeling jobs within a Use_Case.
- **Labeling_Wizard**: The labeling-job creation page (`CreateLabelingJob.tsx`, route `/labeling/create`) with its six steps (backend, job configuration, dataset, task configuration, labeling setup / workforce, review).
- **Wizard_Setup_State**: The client-side state of the Labeling_Wizard enumerated in Requirement 2: the entered, selected, and toggled setup values of both the DDA and Ground Truth branches, the Sample_Selection, and the Preview_Run_Reference.
- **Setup_Draft**: The versioned JSON serialization of the persistable part of the Wizard_Setup_State, stamped with a schema version, a save time, and the Use_Case id it belongs to.
- **Draft_Store**: Browser localStorage, accessed tolerantly (an unavailable or failing storage never breaks the wizard).
- **Draft_Key**: The Draft_Store key of one Use_Case's Setup_Draft: `edgeCvPortal.labelingJobDraft.<usecase_id>`.
- **Pristine_State**: The Wizard_Setup_State immediately after the Labeling_Wizard mounts for a given entry context (including a dataset preselected via navigation state), before any Job_Creator input.
- **Draft_Worthy**: A Wizard_Setup_State whose Setup_Draft serialization differs from the Pristine_State's serialization in at least one field.
- **Restore_Offer**: The indication shown when the Labeling_Wizard finds a Setup_Draft for the resolved Use_Case, presenting exactly the two actions Restore and Discard.
- **Draft_Staleness_Bound**: 14 days; a Setup_Draft saved longer ago is treated as absent.
- **Example_Upload_Cache**: The wizard's existing memo of the most recently uploaded example-image file set: the file-set identity key and the uploaded `s3://` URIs per designation (`exampleUploadCache` in `CreateLabelingJob.tsx`).
- **Current_Upload_Refs**: The Example_Upload_Cache URIs at a moment when the cache's identity key equals the identity of the example files as currently staged — i.e. refs that the next preview run or submission would reuse rather than re-upload.
- **Restored_Example_Reference**: One example-image S3 ref restored from a Setup_Draft, carrying its designation (good or bad) and position, displayed with a name derived from the ref and individually removable.
- **Merged_Example_Refs**: Per designation, the Restored_Example_References in draft order followed by the refs of newly uploaded example files in upload order — the ref set the preview request and the job submission consume.
- **Prompt_Tuning_Preview**: The embedded preview surface (`PromptTuningPreview.tsx`) of the labeling setup step.
- **Sample_Selection**: The Prompt_Tuning_Preview's ordered set of selected sample-image object keys (at most 5).
- **Preview_Run**: One server-side prompt-tuning preview execution, identified by `run_id`, polled via `GET /labeling-preview/runs/{runId}`.
- **Preview_Run_Reference**: The persisted identity of the most recently started Preview_Run: its `run_id`, its sample count, and its start time.
- **Resume_Window**: The period after a Preview_Run's start during which its backend items are guaranteed still readable: `min(sample_count × 120 + 60, 900) + 3600` seconds, derived from `dda_labeling.py`'s `expires_at` derivation and TTL grace.
- **Auto_Label_Picker**: The "Auto-label model" selection control in the labeling setup step, capability-filtered per the llm-model-picker-search-and-image-filter spec.
- **Skip_Verification_Configuration**: The admin-only skip-verification toggle, its Bedrock model id, and its per-label prompts.

## Requirements

### Requirement 1: Continuous Draft Capture

**User Story:** As a Job_Creator, I want my in-progress labeling job setup saved automatically as I work, so that a refresh, crash, tab close, or session expiry does not lose it.

#### Acceptance Criteria

1. WHEN the Wizard_Setup_State changes and the changed state is Draft_Worthy, THE Labeling_Wizard SHALL write the Setup_Draft to the Draft_Store under the Draft_Key of the currently selected Use_Case, debounced so that a burst of changes produces one write.
2. WHILE the Wizard_Setup_State equals the Pristine_State, THE Labeling_Wizard SHALL write no Setup_Draft, so that merely visiting the page never creates a draft or a later Restore_Offer.
3. WHILE a Restore_Offer is unresolved, THE Labeling_Wizard SHALL write no Setup_Draft, so that new input cannot overwrite the draft being offered.
4. THE Labeling_Wizard SHALL stamp every written Setup_Draft with the schema version, the save time, and the Use_Case id it belongs to.
5. IF writing to the Draft_Store fails or the Draft_Store is unavailable, THEN THE Labeling_Wizard SHALL continue operating with unchanged behavior for everything except draft persistence.
6. THE Setup_Draft SHALL contain no credential material and no bearer token, and SHALL be limited to setup metadata, S3 references, and the Preview_Run_Reference.

### Requirement 2: Draft Contents

**User Story:** As a Job_Creator, I want the draft to cover everything I entered that can be faithfully brought back, so that a restore puts me back where I left off.

#### Acceptance Criteria

1. THE Setup_Draft SHALL carry the wizard step position, the labeling backend selection, the job name, the description, the dataset S3 URI, the mask prefix, the task type selection, the workforce type selection, the label categories, the Ground Truth instructions, the automated-labeling toggle, the DDA label set rows, the DDA instructions, the selected labeling team's id and name, the auto-label toggle, the auto-label model selection value, the detection prompt, the few-shot toggle, the downscale setting, the token budget entry, and the Skip_Verification_Configuration.
2. THE Setup_Draft SHALL carry the auto-label model selection as the recorded selection value (`sam`, `bedrock:<id>`, or `llm:<id>`) verbatim, independent of which entries the Auto_Label_Picker currently offers.
3. THE Setup_Draft SHALL carry, per designation, exactly the Merged_Example_Refs as of the save: the Restored_Example_References still present in the form followed by the Current_Upload_Refs, and SHALL carry no reference for example files that have not been uploaded.
4. THE Setup_Draft SHALL carry the Sample_Selection and the Preview_Run_Reference of the most recently started Preview_Run.
5. THE Setup_Draft SHALL carry no server-derived collection (use case list, team list, workteam list, model catalog), no preview result payloads, and no `File` object content.

### Requirement 3: Restore Offer and Application

**User Story:** As a Job_Creator, I want to be offered my saved setup when I come back to the create page, so that I can pick up the iteration instead of starting over.

#### Acceptance Criteria

1. WHEN the Labeling_Wizard has resolved its selected Use_Case and a Setup_Draft exists under that Use_Case's Draft_Key, THE Labeling_Wizard SHALL display the Restore_Offer with exactly the two actions Restore and Discard, at most once per Use_Case per mount.
2. WHEN the Job_Creator chooses Restore, THE Labeling_Wizard SHALL apply every Requirement 2.1 field of the Setup_Draft to the Wizard_Setup_State, SHALL present the restored values in the wizard's controls, and SHALL then resume draft capture per Requirement 1.
3. WHEN the Job_Creator chooses Restore, THE Labeling_Wizard SHALL leave every restored value in place through the wizard's own reactive effects — in particular, the restored token budget SHALL be presented rather than replaced by the model-change pre-fill, and the restored task type and model selection SHALL survive the compatibility effects that applied when the draft was saved.
4. WHEN the Job_Creator chooses Restore and the Setup_Draft carries a labeling team id, THE Labeling_Wizard SHALL select that team once the Use_Case's team list has loaded, and IF the team id is no longer present in the loaded list, THEN THE Labeling_Wizard SHALL leave the team unselected.
5. WHEN the Job_Creator chooses Restore, THE Labeling_Wizard SHALL restore the auto-label model selection value verbatim per Requirement 2.2, even when the Auto_Label_Picker's currently offered entries do not include that value.
6. IF the Setup_Draft carries an enabled Skip_Verification_Configuration and the restoring Job_Creator is not an administrator, THEN THE Labeling_Wizard SHALL restore the draft with skip verification disabled and its model id and per-label prompts omitted.
7. WHEN the Job_Creator chooses Discard, THE Labeling_Wizard SHALL remove the Setup_Draft from the Draft_Store and SHALL leave the Wizard_Setup_State unchanged.
8. WHILE no Setup_Draft exists for the resolved Use_Case, THE Labeling_Wizard SHALL display no Restore_Offer and SHALL render exactly as it does before this feature.

### Requirement 4: Restored Example References

**User Story:** As a Job_Creator, I want my already-uploaded example images back after a restore, so that few-shot iteration continues without re-collecting files.

#### Acceptance Criteria

1. WHEN a Setup_Draft carrying example refs is restored, THE Labeling_Wizard SHALL display each ref as a Restored_Example_Reference under its designation, showing a name derived from the ref, and SHALL provide a control to remove each Restored_Example_Reference individually.
2. THE Labeling_Wizard SHALL count Restored_Example_References together with newly staged example files toward the per-designation example limits, the few-shot at-least-one-example rule, and the few-shot attach/omit counts.
3. WHEN a preview run or a job submission needs example refs, THE Labeling_Wizard SHALL supply the Merged_Example_Refs — Restored_Example_References first, newly uploaded refs after, per designation — in the same ref shape those flows consume before this feature, and SHALL upload only the newly staged files.
4. THE Labeling_Wizard SHALL never fabricate a `File` object for a Restored_Example_Reference.
5. WHEN the selected Use_Case changes after a restore, THE Labeling_Wizard SHALL discard the Restored_Example_References, so that refs never cross into another Use_Case's data bucket scope.

### Requirement 5: Preview Run Resumption and Sample Selection Restore

**User Story:** As a Job_Creator iterating on prompts, I want the preview run I started — or just finished — to come back after a reload, so that I do not lose the expensive model results.

#### Acceptance Criteria

1. WHEN a Setup_Draft is restored and its Preview_Run_Reference's start time lies within the Resume_Window, THE Prompt_Tuning_Preview SHALL resume polling that Preview_Run with the existing status route and poll loop.
2. WHILE a resumed Preview_Run reports status Running, THE Prompt_Tuning_Preview SHALL display its progressive results exactly as it displays a run it started itself.
3. WHEN a resumed Preview_Run reports a terminal status on the first poll, THE Prompt_Tuning_Preview SHALL display that run's results.
4. IF the status route answers 404 for a resumed Preview_Run, THEN THE Prompt_Tuning_Preview SHALL display the existing run-no-longer-available indication and re-enable the run control.
5. IF a restored Preview_Run_Reference's start time lies outside the Resume_Window, THEN THE Labeling_Wizard SHALL drop the reference without polling and without displaying an error.
6. WHEN a new Preview_Run starts, THE Labeling_Wizard SHALL replace the Preview_Run_Reference in the next Setup_Draft write with the new run's identity.
7. WHEN a Setup_Draft carrying a Sample_Selection is restored, THE Prompt_Tuning_Preview SHALL initialize its Sample_Selection with the restored keys, which SHALL count toward the selection cap, ride the next run, and show as selected wherever their listing page is displayed.
8. WHILE the Sample_Selection is non-empty, THE Prompt_Tuning_Preview SHALL provide a control that clears the entire Sample_Selection, so that a restored key whose object no longer appears in any listing page can still be deselected.

### Requirement 6: Draft Lifecycle and Hygiene

**User Story:** As a Job_Creator, I want drafts to clean up after themselves, so that I am never offered stale or broken state.

#### Acceptance Criteria

1. WHEN a labeling job is created successfully, THE Labeling_Wizard SHALL remove the Setup_Draft under the selected Use_Case's Draft_Key before navigating away.
2. IF the content under a Draft_Key fails to parse, carries an unknown schema version, or carries a Use_Case id different from the key's, THEN THE Labeling_Wizard SHALL treat that content as no Setup_Draft.
3. IF a Setup_Draft's save time is older than the Draft_Staleness_Bound, THEN THE Labeling_Wizard SHALL treat that Setup_Draft as absent and SHALL remove that Setup_Draft from the Draft_Store.
4. WHEN the Job_Creator leaves the Labeling_Wizard without creating a job, THE Labeling_Wizard SHALL leave the Setup_Draft in the Draft_Store, so that navigation and cancellation remain recoverable events.
5. THE Labeling_Wizard SHALL read and write the Draft_Store only through tolerant accessors that catch storage exceptions and report absence instead of throwing.

### Requirement 7: Preservation of Existing Behavior

**User Story:** As a portal operator, I want the wizard, the preview, and every payload they produce to stay exactly as they are today, so that adding recovery breaks nothing.

#### Acceptance Criteria

1. WHEN a labeling job is submitted with a given set of setup values, THE Labeling_Wizard SHALL produce a creation request byte-identical to the request it produced before this feature for the same values, for both the DDA and Ground Truth branches, whether or not those values were restored from a Setup_Draft.
2. WHEN a preview run is started with a given set of setup values, THE Prompt_Tuning_Preview SHALL produce a start request byte-identical to the request it produced before this feature for the same values.
3. THE Labeling_Wizard SHALL apply the same step validation rules, the same error messages, and the same submission gating as before this feature, with restored values validated exactly as manually entered values.
4. WHILE no Setup_Draft exists and no restore has occurred, THE Labeling_Wizard SHALL render the same controls and follow the same flows as before this feature.
5. WHEN the Prompt_Tuning_Preview is rendered without the new restore-related inputs, THE Prompt_Tuning_Preview SHALL behave exactly as before this feature, with the Requirement 5.8 clear-selection control as the only addition to its rendered surface.
6. THE Labeling_Wizard SHALL leave the authentication flows, the Use_Case selection persistence, and every backend API and route unchanged.
