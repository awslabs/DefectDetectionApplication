# Requirements Document

## Introduction

The `DdaGroundedSamWorker` Lambda (a `DockerImageFunction`, 10240 MB / 300 s, CPU Grounded-SAM inference) is gated in `compute-stack.ts` behind the `deployGroundedSamWorker` CDK context flag (`=== true || === 'true'`, default OFF). Any `cdk deploy EdgeCVPortalComputeStack` without `-c deployGroundedSamWorker=true` synthesizes a template without the worker, and CloudFormation **deletes the live function**. This has happened **four times** (2026-09-07 02:53Z, 08:04Z, and twice more from other checkouts/sessions — lineage in `.kiro/specs/grounded-sam-mask-offset/verification-notes.md` §2 and `.kiro/specs/grounded-sam-prompt-tuning-preview/verification-notes.md` §7: `…-qHx8huoBWZFA` → `…-i6P1oAqkvVtZ` → `…-xnyXEaM1eNXB` → the current `…-9paW7gMXvjg2`). Each deletion breaks live grounded-sam pre-labeling and the prompt-tuning preview until someone redeploys with the flag. One of the four deletions came through `edge-cv-portal/deploy-frontend.sh` step 6, which runs an internal flag-less `cdk deploy EdgeCVPortalComputeStack`; sessions have been working around it by running the script's steps 1–5 manually. The user's direction: make it so flag-less deploys stop deleting the worker — "making deployGroundedSamWorker default-on in cdk.json or having the worker keep its own stack".

A second footgun, found 2026-09-07: passing `-c cloudFrontDomain=https://d23v4ltibogb5x.cloudfront.net` (the `https://`-prefixed spelling) corrupts live configuration — the storage stack's `PortalArtifactsBucket` CORS and the backend `CLOUDFRONT_DOMAIN`/`PORTAL_DOMAIN` environment values become `https://https://…`, because the consuming code prepends the scheme itself (`storage-stack.ts` builds `` `https://${corsCloudFrontDomain}` ``; `dda_labeling_worker.py` builds `` f"https://{portal_domain}/labeler?job=…" ``). The bare-domain spelling is required but nothing enforces it — the prompt-tuning-preview spec's own deploy task recorded the `https://` spelling, so even documentation carried the poison.

Facts verified in the shipped code and the live account (164152369890 / us-east-1) that shape this spec:

- **Default-on does not force multi-GB builds on routine deploys.** In the installed aws-cdk-lib (2.229.1), `DockerImageAsset` construction at synth time only runs `AssetStaging` — it fingerprints the small source directory (`backend/grounded-sam-worker`, ~136 KB); the actual `docker build` runs at deploy time via cdk-assets and is **ECR-cached by fingerprint** (recorded in `edge-cv-portal/infrastructure/test/gsam-preview-infra.test.ts`'s header, which synthesizes flag-ON under jest on exactly this fact). The image rebuilds only when `grounded-sam-worker/` source changes. The worker is currently live with the flag-on template, so the first deploy after this change is a no-op for the worker.
- **The flag has exactly one read point** — `compute-stack.ts` (`this.node.tryGetContext('deployGroundedSamWorker')`), guarding one block: the `DdaGroundedSamWorker` definition plus its wiring (`GROUNDED_SAM_WORKER_FUNCTION_NAME` + `grantInvoke` onto both `DdaAutolabelWorker` and `DdaLabelingHandler`).
- **`cloudFrontDomain` has exactly two read points**: `bin/app.ts` (`app.node.tryGetContext` → the `cloudFrontDomain` prop consumed by `UseCasesHandler`'s `CLOUDFRONT_DOMAIN` and `DdaLabelingWorker`'s `PORTAL_DOMAIN` environment entries) and `storage-stack.ts` (its own `this.node.tryGetContext` → the `PortalArtifactsBucket` CORS origin). No other infrastructure consumer exists.
- **`deploy-frontend.sh` step 6 passes no worker flag and passes the bare domain** — it resolves `CLOUDFRONT_URL` from the `EdgeCVPortalFrontendStack` output `DistributionDomainName` (bare, e.g. `d23v4ltibogb5x.cloudfront.net`) and runs `cdk deploy` with `-c cloudFrontDomain="$CLOUDFRONT_URL"` and no `deployGroundedSamWorker` context. Nothing in the script fights a default-on flag; with the default flipped its step 6 becomes safe (deploys the worker rather than deleting it) with **zero script changes**.
- **The live configuration is currently clean** — `UseCasesHandler` `CLOUDFRONT_DOMAIN` and `DdaLabelingWorker` `PORTAL_DOMAIN` carry the bare domain, and the artifacts-bucket CORS carries exactly one `https://` prefix — so the deploy that proves this spec should show no corrective diff, only the absence of a worker deletion.
- **The flag-off degradation paths are application behavior, not infrastructure.** With `GROUNDED_SAM_WORKER_FUNCTION_NAME` absent, job creation degrades per grounded-sam-autolabel Req 5.4 and the preview start route answers the 'Grounded-SAM worker is not deployed' 400 per grounded-sam-prompt-tuning-preview Req 6.1. Those paths must keep working for deliberately flag-off deployments; no application code changes.

Scoping decisions, each with its rationale:

- **The default flips at both layers: a code-level default in `compute-stack.ts` and a documented default in `cdk.json`.** `new cdk.App()` (jest synths, any programmatic app) does **not** read `cdk.json`, so a cdk.json-only default protects only CLI deploys; the code-level default protects everything. Both land: the code default is the guarantee, the cdk.json entry is the visible, documented posture for anyone inspecting CLI context.
- **Only an explicit false disables the worker.** The failure asymmetry dictates strictness: an accidental deploy of the worker is cheap and recoverable (ECR-cached image, no data loss); an accidental deletion breaks live pre-labeling. So absent, `true`, `'true'`, and any unrecognized value all deploy the worker; only boolean `false` or the string `'false'` (case-insensitive, trimmed) omits it — preserving a deliberate teardown path (`-c deployGroundedSamWorker=false`, which also overrides the cdk.json entry by CDK's context precedence) and the flag-off degradation behaviors.
- **A separate worker stack was considered and rejected** (recorded in the design): cross-stack export/import coupling on the worker ARN (handler env + two grants), deploy-ordering constraints, and the CloudFormation export-mutation trap, for the same protection the default-on flag achieves with a fraction of the change.
- **The sibling `deploySamWorker` flag (DdaSamWorker) stays untouched.** No DdaSamWorker function exists in the live account (verified), no deletion incident involves it, and the user's direction names only `deployGroundedSamWorker`. Flipping its default would force a second container image into every default synth for a worker nobody runs.
- **Domain normalization happens at the two context read points, in shared pure helpers.** Both `tryGetContext('cloudFrontDomain')` sites normalize through one exported function, so both spellings produce the bare domain and every consumer (CORS origin, env values, and transitively the backend's scheme-prepending code) receives exactly one scheme. The helpers live in a new `lib/context-helpers.ts` rather than `compute-stack.ts` because `storage-stack.ts` must not import the ~5,000-line compute stack module (with its five nested-stack imports) to normalize a string; pure, synth-free imports are also what make the helpers property-testable at full iteration counts.

**Test-suite consequence, declared up front (the spec's entire permitted rebaseline class):** two shipped infra suites pin the current flag-off-by-default synth and invert under this spec; one more synthesizes default-flag but survives. Exactly two files change, in exactly these ways; every other pre-existing assertion stays byte-identical (zero-rebaseline rule):

1. `edge-cv-portal/infrastructure/test/grounded-sam-worker-infra.test.ts` — its default (no-context) synth becomes the WITH-worker case. The "no image-package Lambda" and "no DdaGroundedSamWorker logical ids" assertions invert for `DdaGroundedSamWorker` (exactly one image function, the worker, with its shipped configuration; `DdaSamWorker` stays asserted absent); the "environment carries neither" assertion becomes: `GROUNDED_SAM_WORKER_FUNCTION_NAME` present and referencing the worker, `SAM_WORKER_FUNCTION_NAME` still absent; the Requirement 5.5 exact environment-key-set assertion for `DdaAutolabelWorker` gains the one key `GROUNDED_SAM_WORKER_FUNCTION_NAME`; the header's outdated claim that a flag-on synth performs a Docker build is corrected to cite the staging-only fact established by `gsam-preview-infra.test.ts`. New assertions MAY be added for the Worker_Wiring now present in the default synth (`GROUNDED_SAM_WORKER_FUNCTION_NAME` on `DdaLabelingHandler`, both invoke grants); every pre-existing assertion outside the inversions above (handler, runtime, timeout, memory, layers, static env values) is untouched.
2. `edge-cv-portal/infrastructure/test/gsam-preview-infra.test.ts` — the flag-OFF suite's synth context changes from no-context to explicit `{ deployGroundedSamWorker: 'false' }` (the without-worker case is now the explicit-false synth); its three assertions (no worker ids, no handler env entry, no grant) stay byte-identical against that synth. The flag-ON suite (context `'true'`) is untouched. The header notes the default flip.
3. `edge-cv-portal/infrastructure/test/labeling-cleanup-infra.test.ts` — synthesizes default-flag but **survives unchanged** (verified): its compute assertions are regex-scoped env-key checks (`/DELET|STEAL|PODIUM/`) and `toBeDefined` checks on `dda_labeling.handler` / `dda_labeling_worker.handler`, none disturbed by the worker joining the default template (the worker is an image function with no `Handler` property, so `lambdaByHandler` uniqueness holds).

## Glossary

Terms carried over from the grounded-sam-autolabel, grounded-sam-mask-offset, and grounded-sam-prompt-tuning-preview specs keep their existing definitions (Grounded_SAM_Worker, Worker_Flag, Portal, DDA_Labeling_System, Auto_Labeler, Pre_Label, Prompt_Tuning_Preview, Use_Case). New or constrained terms:

- **Worker_Flag**: The `deployGroundedSamWorker` CDK context key (constrained here: its absence now means deploy).
- **Grounded_SAM_Worker**: The `DdaGroundedSamWorker` `DockerImageFunction` in the Compute_Stack — image code from `backend/grounded-sam-worker`, x86_64, 10240 MB, 300 s, no environment block.
- **Compute_Stack**: The `ComputeStack` construct (`edge-cv-portal/infrastructure/lib/compute-stack.ts`) deployed as `EdgeCVPortalComputeStack`.
- **Storage_Stack**: The `StorageStack` construct (`edge-cv-portal/infrastructure/lib/storage-stack.ts`) deployed as `EdgeCVPortalStorageStack`, owner of the `PortalArtifactsBucket` CORS rule.
- **App_Entry**: `edge-cv-portal/infrastructure/bin/app.ts`, the CDK app that reads `cloudFrontDomain` context and passes it to the Compute_Stack as a prop.
- **Deploy_Script**: `edge-cv-portal/deploy-frontend.sh`, whose step 6 runs an internal `cdk deploy EdgeCVPortalComputeStack` without the Worker_Flag.
- **Portal_Infrastructure**: The CDK application under `edge-cv-portal/infrastructure` — the App_Entry, the stacks, `cdk.json`, and the jest suites.
- **Explicit_False**: A Worker_Flag context value that is boolean `false`, or a string equal to `false` case-insensitively after trimming.
- **Worker_Wiring**: The four wiring effects inside the gated block: `GROUNDED_SAM_WORKER_FUNCTION_NAME` on `DdaAutolabelWorker`'s environment, `grantInvoke` to `DdaAutolabelWorker`, `GROUNDED_SAM_WORKER_FUNCTION_NAME` on `DdaLabelingHandler`'s environment, and `grantInvoke` to `DdaLabelingHandler`.
- **Flag_Resolver**: The exported pure function deciding worker deployment from a raw Worker_Flag context value.
- **Domain_Context**: The `cloudFrontDomain` CDK context value as supplied (bare, scheme-prefixed, trailing-slashed, or absent).
- **Normalized_Domain**: The Domain_Context with any single leading `http://` or `https://` scheme (case-insensitive) and all trailing slashes removed, surrounding whitespace trimmed.
- **Domain_Normalizer**: The exported pure function deriving the Normalized_Domain from a raw Domain_Context value.
- **Context_Helpers**: The new module `edge-cv-portal/infrastructure/lib/context-helpers.ts` exporting the Flag_Resolver and Domain_Normalizer.
- **Declared_Rebaseline**: The amendments to `grounded-sam-worker-infra.test.ts` and `gsam-preview-infra.test.ts` enumerated in the Introduction — the spec's entire permitted class of pre-existing test changes.

## Requirements

### Requirement 1: The Grounded_SAM_Worker deploys by default

**User Story:** As a portal operator, I want a flag-less compute deploy to keep the Grounded_SAM_Worker, so that routine deploys (including other sessions' and scripts' deploys) stop deleting the live pre-labeling worker.

#### Acceptance Criteria

1. WHEN the Compute_Stack synthesizes with the Worker_Flag absent from context, THE Compute_Stack SHALL define the Grounded_SAM_Worker and the complete Worker_Wiring in the synthesized template.
2. WHEN the Compute_Stack synthesizes with the Worker_Flag set to `true` or `'true'`, THE Compute_Stack SHALL define the Grounded_SAM_Worker and the complete Worker_Wiring, exactly as it does today.
3. WHEN the Compute_Stack synthesizes with a Worker_Flag context value that is not Explicit_False (including unrecognized strings such as `'0'`, `'no'`, `'off'`, or arbitrary text), THE Compute_Stack SHALL define the Grounded_SAM_Worker and the complete Worker_Wiring.
4. WHEN the Compute_Stack synthesizes with an Explicit_False Worker_Flag, THE Compute_Stack SHALL omit the Grounded_SAM_Worker, every Worker_Wiring environment entry, and every Worker_Wiring grant — producing the template shape a flag-absent synth produces today.
5. THE Compute_Stack SHALL resolve the Worker_Flag through the Flag_Resolver at the flag's single existing read point, and THE Flag_Resolver SHALL return deploy for every input except an Explicit_False value.
6. WHEN the Grounded_SAM_Worker is present in a synthesized template, THE Compute_Stack SHALL define it with its shipped configuration unchanged: image code from `backend/grounded-sam-worker` with the pinned amd64 platform and build-arg passthrough, x86_64 architecture, 10240 MB memory, 300 s timeout, and no environment block.
7. THE Compute_Stack SHALL include the Worker_Wiring in a synthesized template exactly when the Grounded_SAM_Worker is present in that template.

### Requirement 2: The default is documented in cdk.json and overridable from the CLI

**User Story:** As a portal operator inspecting or scripting CLI deploys, I want the default visible in `cdk.json` and a deliberate teardown path preserved, so that the posture is discoverable and an intentional flag-off deployment remains possible.

#### Acceptance Criteria

1. THE Portal_Infrastructure SHALL carry `"deployGroundedSamWorker": true` in `cdk.json`'s `context` block.
2. WHEN a CLI deploy passes `-c deployGroundedSamWorker=false`, THE Compute_Stack SHALL omit the Grounded_SAM_Worker and Worker_Wiring, the command-line context taking precedence over the `cdk.json` entry.
3. WHILE the Compute_Stack is deployed with an Explicit_False Worker_Flag, THE DDA_Labeling_System SHALL keep its flag-off degradation behaviors reachable with no application code changes: `GROUNDED_SAM_WORKER_FUNCTION_NAME` absent from `DdaAutolabelWorker` and `DdaLabelingHandler`, so job creation degrades per grounded-sam-autolabel Requirement 5.4 and the Prompt_Tuning_Preview start route answers the 'Grounded-SAM worker is not deployed' validation error per grounded-sam-prompt-tuning-preview Requirement 6.1.

### Requirement 3: The Domain_Context is normalized at both read points

**User Story:** As a portal operator, I want either spelling of the CloudFront domain context to produce correct configuration, so that the `https://`-prefixed spelling can never again corrupt CORS or backend environment values.

#### Acceptance Criteria

1. THE App_Entry SHALL pass the Compute_Stack the Normalized_Domain derived from its `cloudFrontDomain` context read through the Domain_Normalizer.
2. THE Storage_Stack SHALL derive the Normalized_Domain from its `cloudFrontDomain` context read through the Domain_Normalizer before building the `PortalArtifactsBucket` CORS origin.
3. WHEN a non-empty Domain_Context is supplied in any spelling (bare, `http://`-prefixed, `https://`-prefixed, mixed-case scheme, with or without trailing slashes), THE Storage_Stack SHALL emit a `PortalArtifactsBucket` CORS `allowedOrigins` of exactly `` [`https://{Normalized_Domain}`] `` — one scheme prefix, no trailing slash.
4. WHEN a non-empty Domain_Context is supplied in any spelling, THE Compute_Stack SHALL set `UseCasesHandler`'s `CLOUDFRONT_DOMAIN` and `DdaLabelingWorker`'s `PORTAL_DOMAIN` environment values to exactly the Normalized_Domain, carrying no scheme.
5. WHEN the same domain is supplied bare and `https://`-prefixed, THE Portal_Infrastructure SHALL synthesize identical Compute_Stack and Storage_Stack templates for the two spellings.
6. WHEN the Domain_Context is absent, or is empty or whitespace-only after normalization, THE Portal_Infrastructure SHALL behave as it does today with an absent Domain_Context: `allowedOrigins: ['*']` on the `PortalArtifactsBucket` and no `CLOUDFRONT_DOMAIN` or `PORTAL_DOMAIN` environment entries.
7. THE Domain_Normalizer SHALL be a pure exported function of the Context_Helpers module, removing surrounding whitespace, at most one leading scheme, and all trailing slashes, and leaving every other character of the domain unchanged.

### Requirement 4: The Deploy_Script's flag-less step 6 is safe under the new default

**User Story:** As a portal operator, I want `deploy-frontend.sh` runnable end-to-end again, so that frontend deploys stop requiring the manual steps-1-5 workaround.

#### Acceptance Criteria

1. WHEN the Deploy_Script's step 6 runs its internal `cdk deploy EdgeCVPortalComputeStack` without the Worker_Flag, THE Compute_Stack SHALL synthesize with the Grounded_SAM_Worker present, preserving the live worker.
2. THE Deploy_Script SHALL keep resolving its `cloudFrontDomain` context value from the `EdgeCVPortalFrontendStack` output `DistributionDomainName` (the bare-domain form), which the Domain_Normalizer maps to itself.
3. THE Deploy_Script SHALL require no amendment for this feature; IF implementation reveals any step of the Deploy_Script fighting the new default, THEN THE implementation SHALL stop and surface it rather than silently amending the script.

### Requirement 5: Preservation

**User Story:** As a portal operator, I want everything outside the flag default and domain normalization byte-identical, so that this hardening cannot regress the worker, its consumers, or any other stack.

#### Acceptance Criteria

1. WHEN the Compute_Stack synthesizes with the Worker_Flag set `'true'` and a bare Domain_Context, THE Compute_Stack SHALL produce a template identical to today's flag-on template for the same inputs.
2. THE Compute_Stack SHALL leave the `deploySamWorker` flag's read, semantics (default OFF), and the `DdaSamWorker` gated block untouched by this feature.
3. THE Portal_Infrastructure SHALL leave every stack other than the Compute_Stack and Storage_Stack, every Lambda definition other than the flag-gated block's, and every application source file (backend `functions/`, frontend `src/`, worker image `grounded-sam-worker/`) unchanged by this feature.
4. THE DDA_Labeling_System SHALL leave the grounded-sam-autolabel Requirement 5.4 job-creation degradation and the grounded-sam-prompt-tuning-preview Requirement 6.1 not-deployed rejection code paths unchanged by this feature.
5. THE Portal_Infrastructure SHALL leave `.kiro/steering/builds.md` unchanged by this feature: its deploy gates (pgrep checks, cdk.out drift handling) are flag-agnostic and remain correct under the new default.

### Requirement 6: Declared test rebaseline class

**User Story:** As a maintainer, I want the suites that pin the old flag-off default amended in exactly the declared ways, so that the default flip is auditable and everything else stays pinned.

#### Acceptance Criteria

1. WHEN this feature's changes land, THE test suite SHALL amend `grounded-sam-worker-infra.test.ts` exactly as the Introduction declares: the default synth becomes the WITH-worker case (worker present with shipped configuration, `DdaSamWorker` still absent, `DdaAutolabelWorker` env gaining exactly the `GROUNDED_SAM_WORKER_FUNCTION_NAME` key, header corrected), every other assertion byte-identical.
2. WHEN this feature's changes land, THE test suite SHALL amend `gsam-preview-infra.test.ts` exactly as the Introduction declares: the flag-OFF suite synthesizes with explicit `{ deployGroundedSamWorker: 'false' }` context, its assertions and the entire flag-ON suite byte-identical, header noting the default flip.
3. THE test suite SHALL keep `labeling-cleanup-infra.test.ts` and every other pre-existing test file byte-identical; IF any assertion outside the two declared files fails under the new default, THEN THE implementation SHALL stop and surface it as a design violation rather than amending further files.
