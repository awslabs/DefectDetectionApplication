# Implementation Plan: Custom Node Designer

## Overview

Implementation proceeds on the `workflow_manager` git branch. The shared `workflow_core` additions (catalog.custom, classification, scaffold module, `ARCH_X86_64_NVIDIA` constants) are built first because the portal Lambdas, the packager, the test sandbox, and the LocalServer vendored copies all depend on them. Portal backend Lambdas and build infrastructure follow, then the Plugin_Simulator, Custom_Node_Type registration and catalog integration, packaging/deployment integration, frontend, cloud test runs, edge verification, and finally backward-compatibility verification. Python code uses `hypothesis` and TypeScript code uses `fast-check` for property-based tests, each configured for a minimum of 100 iterations and tagged `**Feature: custom-node-designer, Property {number}: {property_text}**`.

## Tasks

- [x] 1. Extend workflow_core for custom node types
  - [x] 1.1 Add the x86_64_nvidia Target_Architecture to workflow_core constants
    - Add `ARCH_X86_64_NVIDIA = "x86_64_nvidia"` to `ARCHITECTURES` and `DEVICE_ARCHITECTURES` in `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/models.py`
    - Extend `bundled_plugins_for` with an `x86_64_nvidia` entry (initially mirroring `x86_64`); verify built-in `NODE_CATALOG` descriptors in `nodes.py` gain an `x86_64_nvidia` GstMapping via the per-`DEVICE_ARCHITECTURES` generation
    - Assert the existing compiler, validator, and packaging arch checks accept `x86_64_nvidia` with no further change
    - _Requirements: 3.1, 16.1_

  - [x] 1.2 Implement workflow_core.catalog.custom
    - Write `descriptor_from_declaration(decl) -> NodeTypeDescriptor` converting a stored Custom_Node_Type declaration (node-catalog wire shape) into a frozen descriptor, validating port types against `PORT_TYPES`, categories against `CATEGORIES`, parameter descriptors against `PARAMETER_TYPES`, and mappings against `ARCHITECTURES`; raise `DeclarationError` identifying the offending field; DeepStream-flagged declarations restricted to arm64_jp4/jp5/jp6 mappings
    - Write `resolve_catalog(custom_descriptors) -> tuple` returning `NODE_CATALOG + tuple(custom_descriptors)` with duplicate `type_id` rejection (built-ins win)
    - _Requirements: 5.3, 8.2, 8.5, 8.6_

  - [x] 1.3 Write property test for declaration conversion
    - **Feature: custom-node-designer, Property 1: Declaration conversion accepts exactly the valid declarations**
    - **Validates: Requirements 1.7, 5.3, 8.4, 8.5**

  - [x] 1.4 Implement workflow_core.catalog.classification
    - Write `classify_plugin_set(module_name, repo_url) -> good|bad|ugly|unclassified` derived from the official plugin-set module names (`gst-plugins-good`, `gst-plugins-bad`, `gst-plugins-ugly`) and their known repository locations; everything else is `unclassified`
    - Define the fixed plain-language explanation text for each of the four classification values
    - _Requirements: 15.3, 15.4_

  - [x] 1.5 Write property test for plugin-set classification
    - **Feature: custom-node-designer, Property 6: Plugin-set classification is exact**
    - **Validates: Requirements 15.1, 15.3, 15.4**

  - [x] 1.6 Implement workflow_core.scaffold
    - Write pure template rendering: given a validated declaration (name, category, ports, parameters, architectures), render a GStreamer plugin project — C skeleton element wrapping an embedded Python `process_frame(frame, params) -> frame` Frame_Processing_Hook file (appsink/appsrc bridge per the existing `emlpython` approach), one `meson.build` build configuration per selected Target_Architecture, and a README; declared parameters surface as GObject properties plumbed into the hook's `params` dict
    - Write scaffold validation rejecting non-buildable source (missing hook file, missing build configurations, empty required files) with a description of the failure
    - _Requirements: 1.2, 1.3, 1.4, 1.5_

  - [x] 1.7 Write property test for scaffold completeness
    - **Feature: custom-node-designer, Property 2: Scaffold generation is complete for the declaration**
    - **Validates: Requirements 1.2, 1.4**

  - [x] 1.8 Write property test for scaffold validation
    - **Feature: custom-node-designer, Property 3: Scaffold validation rejects non-buildable source**
    - **Validates: Requirements 2.6**

  - [x] 1.9 Write property test for merged-catalog compilation
    - **Feature: custom-node-designer, Property 12: Merged-catalog compilation includes custom plugin dependencies**
    - **Validates: Requirements 5.4, 8.6**

- [x] 2. Checkpoint - workflow_core extensions complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement plugin records, lifecycle, and access control backend
  - [x] 3.1 Implement plugin_records.py
    - Plugin_Record CRUD over the `PluginRecords` DynamoDB table (`plugin_id` + `version`): new records and new versions start with `lifecycle_state = dev` and `review.decision = pending` independently of prior versions; provenance, per-arch artifact entries, and component pointer per the data model
    - Lifecycle transitions with guards: dev→test requires at least one successfully built Plugin_Artifact (409 identifying the missing build); test→prod requires an approved security review (409 identifying the missing approval); demotion (prod→test, test→dev) always succeeds and applies gates only to subsequent packaging/deployment requests, leaving deployed Workflow_Components untouched
    - Security review endpoints: pending-record display with full provenance (repo URL/revision, scaffold origin, or generation prompt, user, timestamps, classification), per-arch checksums and signatures, and source inspection; approve/reject recording decision, acting PortalAdmin, and timestamp in the existing AuditLog table
    - _Requirements: 9.1, 9.3, 9.4, 9.5, 9.9, 9.10, 9.12, 9.13, 10.1, 10.2, 10.3, 10.5, 15.6_

  - [x] 3.2 Write property test for the lifecycle state machine
    - **Feature: custom-node-designer, Property 10: Lifecycle state machine conformance**
    - **Validates: Requirements 9.1, 9.4, 9.5, 9.9, 9.10, 9.13, 10.1, 10.5**

  - [x] 3.3 Register node-designer RBAC actions
    - Add `node-designer:read` (all roles, read-only for DataScientist/Operator/Viewer), `node-designer:create/generate/import/simulate/register/promote-demote/manage` (UseCaseAdmin within own Use_Case, PortalAdmin), and `node-designer:security-review` (PortalAdmin only) to `rbac_middleware`, mapped to existing roles; denials return the standard authorization error envelope
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [x] 3.4 Write RBAC and audit tests
    - Parameterized role×action matrix covering UseCaseAdmin, PortalAdmin, DataScientist, Operator, and Viewer against create/generate/import/simulate/register/promote/demote/approve/update/remove; audit log write per create/generate/import/simulate/register/promote/demote/approve/reject/update/deprecate/remove operation
    - _Requirements: 10.3, 13.1, 13.2, 13.3, 13.4, 13.5_

- [x] 4. Implement the Plugin_Importer and Module_Listing
  - [x] 4.1 Implement repository import in plugin_importer.py
    - `POST /plugins/import`: validate the URL, run the lightweight CodeBuild fetch step cloning the repository at the requested revision (default branch when omitted) and syncing the tree to `plugin-sources/{usecase_id}/{plugin_id}/{version}/`; unreachable repository or missing revision fails before any Plugin_Record is created
    - Buildability scan (presence of a GStreamer plugin build definition: `meson.build`/`configure.ac` with a plugin target, or prebuilt `.so`); unbuildable imports mark the Plugin_Record failed with the finding reported
    - Successful fetch creates the Plugin_Record with provenance `{repoUrl, revision, importedBy, importedAt, classification}` (classification via `classify_plugin_set`) and submits builds for the user-selected Target_Architectures
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 15.4, 15.5_

  - [x] 4.2 Write property test for the buildability scan
    - **Feature: custom-node-designer, Property 4: Import buildability scan matches source-tree construction**
    - **Validates: Requirements 4.5**

  - [x] 4.3 Write property test for import provenance
    - **Feature: custom-node-designer, Property 7: Import provenance records the classification**
    - **Validates: Requirements 4.2, 15.5**

  - [x] 4.4 Implement the Module_Listing endpoint
    - `GET /plugin-modules`: fetch `https://gstreamer.freedesktop.org/modules/`, parse server-side into `{name, description, repoUrl, classification}` entries, cache in the `ModuleIndexCache` DynamoDB item with `fetchedAt` and a 24-hour TTL, reusing the cached index for subsequent views; fetch/parse failure returns the distinct `MODULE_LISTING_UNAVAILABLE` code so the UI offers manual URL entry; selecting a module feeds its published repository location into the repository import path
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 4.5 Write property test for module listing parsing
    - **Feature: custom-node-designer, Property 5: Module listing parse covers every module**
    - **Validates: Requirements 6.1**

  - [x] 4.6 Write unit tests for import error paths and cache behavior
    - Unreachable repository and missing revision creating no record; unbuildable source marking the record failed; listing fetch/parse failure returning `MODULE_LISTING_UNAVAILABLE`; cache TTL boundary at 24 hours
    - _Requirements: 4.4, 4.5, 6.3, 6.4_

- [x] 5. Implement the Node_Generator
  - [x] 5.1 Implement node_generator.py
    - Mirror `workflow_generator.py`: chat sessions in the TTL'd `NodeGenSessions` table with the current scaffold source snapshot in S3; Bedrock Converse invocation via `get_bedrock_configuration()` (timeout clamped ≤ 60 s, cached client, no retries) with a forced `create_plugin_scaffold` tool whose input schema is the scaffold file map plus the declaration; system prompt embeds the scaffold template conventions and the Frame_Processing_Hook contract
    - Follow-up prompts include the current source and instruct modification rather than regeneration; output failing scaffold validation returns an error with the prompt preserved; Bedrock failures/timeouts return descriptive errors with the prompt preserved; accepted source enters the standard build/simulate/lifecycle path with the generation prompt recorded as provenance
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7_

  - [x] 5.2 Write integration tests for generation
    - Mocked Converse API asserting prompt/template-convention assembly, tool-use output handling, follow-up source inclusion, scaffold-validation rejection with prompt preservation, and timeout behavior
    - _Requirements: 2.2, 2.4, 2.6, 2.7_

- [x] 6. Implement build infrastructure and the Plugin_Build_Service
  - [x] 6.1 Add node-designer infrastructure to CDK
    - Create `node-designer-stack.ts` in `edge-cv-portal/infrastructure/lib` following `test-runner-stack.ts` patterns: DynamoDB tables (`PluginRecords`, `CustomNodeTypes`, `ModuleIndexCache`, `SimulationRuns`, `NodeGenSessions` with TTL) with GSIs; the KMS asymmetric signing key (ECDSA P-256); five CodeBuild projects with per-arch custom build images — x86_64 (Ubuntu 22.04/GStreamer 1.20 matching the sandbox image), x86_64_nvidia (same base plus CUDA toolkit and NVIDIA GStreamer runtime headers), arm64 JetPack 4/5/6 cross-build images pinning the DeepStream SDK version matching each JetPack release — each build running in a fresh container with a role scoped to exactly that build's source and staging prefixes, no VPC access to portal internals; the lightweight fetch project; EventBridge rules for build results; new Lambda functions and API Gateway routes
    - _Requirements: 3.1, 3.2, 5.2_

  - [x] 6.2 Implement plugin_builds.py
    - `POST /plugins/{id}/versions/{v}/build`: mark per-arch build status building, StartBuild per selected Target_Architecture (source S3 key, arch image); EventBridge result handler (idempotent on build id) recording per-arch `{s3Key, checksum, signature, buildStatus, logTail}` — successful builds SHA-256 checksummed, KMS-signed, and promoted to the Plugin_Library `workflow-plugins/custom/{usecase_id}/{arch}/{plugin}.so` + `.sig`; failed builds store the CloudWatch log tail with no artifact
    - Prebuilt binary upload path accepting a `.so` per arch, checksummed and signed identically with `prebuilt: true` provenance; DeepStream-flagged records restrict selectable architectures to arm64_jp4/jp5/jp6; per-arch build status endpoint for the UI; trigger `plugin_components.py` when all requested arch builds settle with at least one success
    - _Requirements: 1.6, 3.1, 3.3, 3.4, 3.5, 3.6, 5.1, 5.2_

  - [x] 6.3 Implement plugin_components.py
    - Auto-package successfully built Plugin_Artifacts into the Greengrass component `dda.plugin.{pluginId}` version `{pluginVersion}.0.0` in the Use_Case account: install-only recipe (no Run lifecycle), one platform manifest per successfully built Target_Architecture using `ARCH_TO_GG_PLATFORM` plus platform attributes (`variant` for JetPack arm64, `runtime: nvidia` for x86_64_nvidia, plain `x86_64` manifest ordered after `x86_64_nvidia`); artifacts (signed `.so` + `plugin-manifest.json` with name, version, arch, checksum) copied to the account bucket under `plugins/components/{pluginId}/{pluginVersion}/{arch}/` via staging, installed to `/aws_dda/plugins/{pluginId}/{pluginVersion}/{arch}/`
    - All-or-nothing stage/promote/register with failed-registration cleanup deleting the component version; registry tags (`dda-portal:managed`, `usecase-id`, `plugin-id`, `plugin-version`); `component` status pointer recorded on the Plugin_Record; retry idempotent on plugin id + version (registered short-circuits, `ConflictException` re-describes); rebuilds publish a new component version leaving prior versions unchanged; auto-packaging failure never fails the build
    - _Requirements: 16.1, 16.7_

  - [x] 6.4 Write property test for Plugin_Component recipe assembly
    - **Feature: custom-node-designer, Property 20: Plugin_Component manifests are exactly the built architectures**
    - **Validates: Requirements 16.1**

  - [x] 6.5 Write property test for Plugin_Component version immutability
    - **Feature: custom-node-designer, Property 23: Plugin_Component versions are immutable under rebuild**
    - **Validates: Requirements 16.7**

  - [x] 6.6 Write integration tests for builds and auto-packaging
    - CodeBuild orchestration with mocked StartBuild/EventBridge results including idempotent double-delivery; build-project IAM policy static assertions and all-five-projects stack snapshot; failure log-tail recording with no stored artifact; auto-packaging trigger on build settlement, stage/promote/register against mocked Greengrass, failure cleanup and retry idempotency
    - _Requirements: 3.1, 3.2, 3.4, 4.3, 16.1, 16.7_

- [x] 7. Checkpoint - records, importer, generator, and builds complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement the Plugin_Simulator
  - [x] 8.1 Implement the sandbox harness simulate mode
    - Add `HARNESS_MODE=simulate` to `edge-cv-portal/test-sandbox/`: render a single-plugin pipeline `multifilesrc ! decode ! <element> <declared-params> ! frame capture + metadata tap` via `Gst.parse_launch`, staging the plugin `.so` into the task's plugin scan directory; flush per-frame results `{frameIndex, inputRef, outputRef, metadata}` incrementally to S3; abnormal plugin termination captures stderr/bus error output, contained to the task
    - _Requirements: 7.2, 7.3, 7.6_

  - [x] 8.2 Implement plugin_simulator.py and the simulator state machine
    - Add the Step Functions state machine to `node-designer-stack.ts`: Guard (refuse when no successful x86_64 Plugin_Artifact, 409 describing the missing build) → Prepare (stage the selected Test_Dataset or uploaded sample frames, stage the plugin from the Plugin_Library) → RunSandbox (Fargate, isolated subnet, task role limited to the run's S3 prefix — no Plugin_Library write, no other Use_Case data) → Collect; 5-minute execution timeout stopping the task, marking the run failed-with-timeout, retaining flushed partial results
    - `POST /plugins/{id}/versions/{v}/simulate` starting a run with parameter values (re-run with changed parameters supported); `GET /simulations/{runId}` returning status and results; `SimulationRuns` records per the data model
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6, 7.7_

  - [x] 8.3 Write property test for the simulator start guard
    - **Feature: custom-node-designer, Property 15: Simulator start guard equals x86_64 artifact presence**
    - **Validates: Requirements 7.5**

  - [x] 8.4 Write property test for simulation result coverage
    - **Feature: custom-node-designer, Property 16: Simulation results cover every input frame**
    - **Validates: Requirements 7.3**

  - [x] 8.5 Write simulator integration tests
    - Task-role policy assertions (no Plugin_Library write path); timeout behavior with a shortened limit retaining partial results; containerized single-plugin run in the sandbox image; failure containment with plugin error output reported
    - _Requirements: 7.2, 7.6, 7.7_

- [x] 9. Implement Custom_Node_Type registration and catalog integration
  - [x] 9.1 Implement custom_node_types.py
    - Registration collecting display name, category, Ports with types, parameters (types, defaults, constraints, descriptions, examples), hardware-dependence flag, element/property mapping per built Target_Architecture, and Use_Case scoping; declarations validated through `descriptor_from_declaration` with invalid Port declarations rejected identifying the offense; plugin dependency recorded as `custom:{usecase_id}/{plugin_name}` in the mapping
    - Versioning: declaration updates create a new `CustomNodeTypes` version item retaining prior versions; deprecation flips the `deprecated` flag; removal scans WorkflowVersions references via the inverted-index GSI maintained at save — zero references deletes catalog items, Plugin_Library artifacts, and the plugin's Plugin_Component versions; otherwise rejects listing the referencing workflows
    - _Requirements: 8.1, 8.2, 8.5, 8.6, 14.1, 14.3, 14.4, 14.5_

  - [x] 9.2 Wire merged catalog resolution into existing consumers
    - `GET /workflows/node-catalog`: load the Use_Case's registered Custom_Node_Types (test/prod only, dev excluded), merge via `resolve_catalog`, serve with `lifecycleState: "test"` markers on test-state entries; `workflow_validation.py` and `workflow_generator.py` validate/generate against the merged catalog for the workflow's Use_Case; workflow save records the Custom_Node_Type `typeVersion` on custom node entries and maintains the reference GSI; deprecated types excluded from the palette merge but resolvable for loading/validating/packaging existing workflows
    - _Requirements: 8.2, 8.3, 9.2, 9.6, 14.2, 14.3_

  - [x] 9.3 Write property test for resolved catalog membership
    - **Feature: custom-node-designer, Property 11: Resolved catalog membership is exact**
    - **Validates: Requirements 8.2, 9.2, 9.6, 14.3**

  - [x] 9.4 Write property test for version retention and pinning
    - **Feature: custom-node-designer, Property 18: Version retention and pinned resolution**
    - **Validates: Requirements 14.1, 14.2**

  - [x] 9.5 Write property test for reference-counted removal
    - **Feature: custom-node-designer, Property 19: Reference-counted removal**
    - **Validates: Requirements 14.4, 14.5**

- [x] 10. Integrate packaging and deployment
  - [x] 10.1 Extend workflow_packaging.py for custom plugins
    - Compile against the merged catalog resolving pinned Custom_Node_Type versions; `split_plugin_dependencies` routes `custom:{usecase}/{name}` dependencies to the custom prefix while built-in/curated plugins keep the existing inline `plugins/{arch}/*.so` bundling unchanged
    - For each `custom:` dependency: load the backing Plugin_Record and reject on `dev` lifecycle state, missing per-arch artifact, or missing Plugin_Component version (Custom_Node_Type and arch/state identified); stream artifact bytes per selected architecture, recompute SHA-256, and KMS-Verify the signature, failing packaging via the existing `PackagingError` path (stage cleanup, no partial component) on either mismatch
    - Declare Greengrass `ComponentDependencies` on `dda.plugin.{pluginId}` with `VersionRequirement` pinned to the recorded Custom_Node_Type version (custom `.so` files never bundled inline); write per-plugin `pluginChecksums` into each arch `manifest.json`; add `x86_64_nvidia` to `ARCH_TO_GG_PLATFORM` with the `runtime: nvidia` platform attribute and the manifest ordering placing plain `x86_64` after `x86_64_nvidia`
    - _Requirements: 10.4, 11.1, 11.2, 11.3, 16.4_

  - [x] 10.2 Write property test for sign-then-verify
    - **Feature: custom-node-designer, Property 8: Sign-then-verify round trip with tamper detection**
    - **Validates: Requirements 3.3, 3.6, 10.4**

  - [x] 10.3 Write property test for packaging gates
    - **Feature: custom-node-designer, Property 13: Packaging gates on lifecycle state and artifact presence**
    - **Validates: Requirements 11.1, 11.2, 11.3**

  - [x] 10.4 Write property test for Workflow_Component dependencies
    - **Feature: custom-node-designer, Property 21: Workflow_Component dependencies are exactly the custom plugins**
    - **Validates: Requirements 16.4, 11.1**

  - [x] 10.5 Extend deployments.py with plugin lifecycle and architecture gates
    - Add the `test_device` flag on the Devices table (set by a UseCaseAdmin); extend the pre-submit check alongside the existing `minLocalServerVersion` pass: lifecycle gate over the dependency closure (dev-state components rejected for any target; test-state components permitted only to devices flagged `test_device`; prod deploys anywhere in the Use_Case) rejecting with `PLUGIN_LIFECYCLE_VIOLATION` identifying the Custom_Node_Type/Plugin_Component and Lifecycle_State
    - Architecture gate: each target device's recorded Target_Architecture checked against the platform manifests of every depended-on Plugin_Component version (`x86_64` and `x86_64_nvidia` matched distinctly, no fallback), rejecting with `PLUGIN_ARCH_UNSUPPORTED` listing each offending `{pluginComponent, version, device, deviceArch}`; standalone Plugin_Component deployments recorded in the Deployments table with `component_type: 'plugin'`; Greengrass dependency resolution delivers depended-on Plugin_Component versions with workflow deployments
    - _Requirements: 9.7, 9.8, 9.11, 16.3, 16.5, 16.6_

  - [x] 10.6 Write property test for the test-device deployment gate
    - **Feature: custom-node-designer, Property 14: Deployment gate restricts test-state plugins to Test_Devices**
    - **Validates: Requirements 9.7, 9.8, 9.11**

  - [x] 10.7 Write property test for Plugin_Component deployment gates
    - **Feature: custom-node-designer, Property 22: Plugin_Component deployment gates on lifecycle and architecture coverage**
    - **Validates: Requirements 16.3, 16.6**

  - [x] 10.8 Extend components.py for Plugin_Component listing
    - Recognize `dda.plugin.*` components in `list_components`, join with the backing Plugin_Record via registry tags, and return name, version, the backing Lifecycle_State, and supported Target_Architectures derived from the recipe's platform manifests for the deployment screen
    - _Requirements: 16.2_

  - [x] 10.9 Write packaging and deployment unit tests
    - Recipe fixtures for amd64 manifest ordering and attribute matching with x86_64 and x86_64_nvidia flavors present and absent; packaging rejection messages identifying node/arch/state; deployment gate rejection codes per device; Plugin_Component listing fields
    - _Requirements: 16.1, 16.2, 16.6_

- [x] 11. Checkpoint - backend integration complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement the Node_Designer frontend
  - [x] 12.1 Implement the plugin library list and create wizard
    - New pages under `edge-cv-portal/frontend/src/pages/node-designer/`: Plugin_Record list with lifecycle badges, per-arch build status (succeeded/failed with log excerpt), and classification risk badges; create wizard collecting name, description, category, Port declarations, parameter declarations, and Target_Architectures → scaffold preview and zip download → source editor → submit to build; scaffold generation failures display the failing input with no record created
    - _Requirements: 1.1, 1.5, 1.6, 1.7, 3.5_

  - [x] 12.2 Implement the generate panel
    - Chat interface accepting natural-language node descriptions; generated scaffold source displayed for review and optional editing before acceptance; generation and Bedrock failures display the error and preserve the prompt for retry
    - _Requirements: 2.1, 2.3, 2.6, 2.7_

  - [x] 12.3 Implement the import views
    - Repository URL form (with optional revision) and the Module_Listing select populated from `GET /plugin-modules` with classification risk badges beside module names; import confirmation view displaying the classification and its plain-language explanation before proceeding, with required acknowledgment for bad/ugly/unclassified imports; DeepStream toggle restricting selectable architectures to arm64 JetPack 4/5/6; listing failure surfaces the error and falls back to manual URL entry
    - _Requirements: 5.1, 6.1, 6.3, 15.1, 15.2, 15.3, 15.7_

  - [x] 12.4 Implement the simulator view
    - Test_Dataset picker (Use_Case-scoped) or sample frame/video upload; side-by-side input/output frame strips with per-frame emitted metadata; parameter editor and re-run with changed values; missing-x86_64 refusal and failure/timeout display with partial results
    - _Requirements: 7.1, 7.3, 7.4, 7.5_

  - [x] 12.5 Implement the registration wizard and review queue
    - Registration wizard prompted after the first successful build: Custom_Node_Type declaration with ports from `PORT_TYPES`, parameters with descriptions and examples, element property mapping per built arch, hardware-dependence flag, Use_Case scoping; invalid Port declarations surfaced with the offending field
    - PortalAdmin review queue: pending Plugin_Records with provenance, classification, per-arch checksums/signatures, and source inspection; approve/reject actions
    - _Requirements: 4.6, 8.1, 8.5, 10.2, 15.6_

  - [x] 12.6 Extend the deployment screen for Plugin_Components
    - List `dda.plugin.*` components in the existing `pages/deployments` with name, version, backing Lifecycle_State badge, and supported Target_Architecture chips; surface pre-submit gate rejections with the Plugin_Component and unsupported architecture or lifecycle violation identified
    - _Requirements: 16.2, 16.3, 16.6_

  - [x] 12.7 Write frontend tests
    - Create wizard field coverage and scaffold download; generate panel review-before-accept and prompt preservation; module list classification badges, explanations, and acknowledgment; listing-failure fallback to manual URL; simulator flows; palette display of custom types in their declared category with test-state markers and built-in configuration panel behavior; review screen completeness including classification; deployment screen Plugin_Component fields
    - _Requirements: 1.1, 1.5, 2.3, 2.6, 6.3, 7.1, 7.4, 8.3, 8.4, 9.6, 10.2, 15.1, 15.2, 15.7, 16.2_

- [x] 13. Integrate custom nodes into cloud test runs
  - [x] 13.1 Extend the Workflow_Test_Runner for custom plugins
    - Compile step in `workflow_test_steps.py` uses the merged catalog; the sandbox task downloads custom x86_64 Plugin_Artifacts into its plugin scan path and executes them within the simulated pipeline; Custom_Node_Types lacking an x86_64 Plugin_Artifact substitute a pass-through recording stub (in addition to the hardware-dependent stubbing rules) identified as stubbed in the test run report
    - _Requirements: 12.1, 12.2_

  - [x] 13.2 Write property test for custom-node stubbing
    - **Feature: custom-node-designer, Property 17: Test-run stubbing is exactly the unavailable custom nodes**
    - **Validates: Requirements 12.2**

  - [x] 13.3 Write tests for stub reporting
    - Test report and Workflow_Builder display describing the limitation that a stubbed Custom_Node_Type was simulated because no x86_64 build exists; sandbox integration run with a real custom x86_64 artifact
    - _Requirements: 12.1, 12.2, 12.3_

- [x] 14. Implement edge plugin verification
  - [x] 14.1 Re-sync vendored workflow_core copies
    - Propagate the `ARCH_X86_64_NVIDIA` constants, `bundled_plugins_for` entry, and new catalog modules to the vendored copies in `edge-cv-portal/test-sandbox/` and `src/backend/workflow_engine/vendor/` (the edge never resolves custom catalogs — compiled documents remain self-contained)
    - _Requirements: 11.4, 12.1_

  - [x] 14.2 Add checksum verification to the LocalServer plugin loader
    - Extend `src/backend/workflow_engine/gst_plugins.py`: before the registry scan, verify each plugin `.so` referenced by `manifest.json` `pluginChecksums` — whether delivered inline under `plugins/<arch>/` or installed by a depended-on Plugin_Component under `/aws_dda/plugins/{pluginId}/{version}/{arch}/` (scan path extended to the Plugin_Component install roots named in the manifest); a mismatch skips the plugin, registers the workflow as invalid with the file identified, and reports through the existing status path; bundled plugins and Pipeline_Configuration execution unaffected
    - _Requirements: 10.6, 11.4_

  - [x] 14.3 Write property test for edge checksum verification
    - **Feature: custom-node-designer, Property 9: Edge checksum verification gates plugin loading**
    - **Validates: Requirements 10.6**

  - [x] 14.4 Write edge integration tests
    - Checksum-verified load and custom element execution within a compiled pipeline on a device/CI image; mismatch rejection identifying the file and reporting through the status path; Plugin_Component install-root loading
    - _Requirements: 10.6, 11.4_

- [x] 15. Verify backward compatibility
  - [x] 15.1 Run existing workflow-manager and LocalServer test suites unchanged against the new build
    - Assert same outcomes: the static `NODE_CATALOG`, built-in plugin bundling, existing packaging/deployment flows, and devices without Plugin_Components behave identically; existing workflows without custom nodes validate, package, and deploy unchanged
    - _Requirements: 8.2, 11.4_

- [x] 16. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Development happens on the `workflow_manager` git branch
- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use hypothesis (Python) and fast-check (TypeScript) with a minimum of 100 iterations, tagged `**Feature: custom-node-designer, Property {number}: {property_text}**`
- workflow_core extensions are built first as they are shared dependencies of the portal Lambdas, the Component_Packager, the test sandbox, and the LocalServer vendored copies
- KMS sign/verify is mocked in property tests with a real ECDSA keypair so the round trip is genuine; S3/DynamoDB via moto; the GStreamer pipeline layer is mocked for results-assembly properties
- Built-in/curated plugin bundling, the static NODE_CATALOG, and the existing `src/backend/gstreamer/` Pipeline_Configuration path are never modified; all changes are additive

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.4"] },
    { "id": 2, "tasks": ["1.3", "1.5", "1.6", "6.1"] },
    { "id": 3, "tasks": ["1.7", "1.8", "1.9", "3.1", "3.3"] },
    { "id": 4, "tasks": ["3.2", "3.4", "4.1", "4.4", "5.1", "6.2"] },
    { "id": 5, "tasks": ["4.2", "4.3", "4.5", "4.6", "5.2", "6.3", "8.1"] },
    { "id": 6, "tasks": ["6.4", "6.5", "6.6", "8.2", "9.1"] },
    { "id": 7, "tasks": ["8.3", "8.4", "8.5", "9.2", "10.1"] },
    { "id": 8, "tasks": ["9.3", "9.4", "9.5", "10.2", "10.3", "10.4", "10.5"] },
    { "id": 9, "tasks": ["10.6", "10.7", "10.8", "12.1", "13.1"] },
    { "id": 10, "tasks": ["10.9", "12.2", "12.3", "13.2", "13.3", "14.1"] },
    { "id": 11, "tasks": ["12.4", "12.5", "14.2"] },
    { "id": 12, "tasks": ["12.6", "14.3"] },
    { "id": 13, "tasks": ["12.7", "14.4"] },
    { "id": 14, "tasks": ["15.1"] }
  ]
}
```
