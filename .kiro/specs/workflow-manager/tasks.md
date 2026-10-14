# Implementation Plan: Workflow Manager

## Overview

Implementation proceeds on the `workflow_manager` git branch (already created). The shared `workflow_core` Python package (catalog, serializer, validator, compiler) is built first because the portal Lambdas, the test sandbox, and the LocalServer workflow engine all depend on it. Portal backend and frontend work follow, then generation, cloud testing, edge execution, and finally backward-compatibility verification. Python code uses `hypothesis` and TypeScript code uses `fast-check` for property-based tests, each configured for a minimum of 100 iterations and tagged `**Feature: workflow-manager, Property {number}: {property_text}**`.

## Tasks

- [x] 1. Set up the shared workflow_core package and node catalog
  - [x] 1.1 Create the workflow_core package skeleton and test infrastructure
    - Create `edge-cv-portal/backend/layers/workflow_core/` with package layout (`catalog`, `serializer`, `validator`, `compiler` modules), `pyproject.toml`, and pytest + hypothesis setup configured for 100+ iterations per property
    - Add layer build script consistent with existing `edge-cv-portal/backend/layers/` conventions
    - _Requirements: 3.1, 4.6, 6.1_

  - [x] 1.2 Implement node catalog data models and initial catalog
    - Write `PortDescriptor`, `ParameterDescriptor`, `GstMapping`, and `NodeTypeDescriptor` dataclasses in `workflow_core.catalog`
    - Populate the initial catalog: camera_source, folder_source, digital_input, dewarp, rotate, crop, format_convert, model_inference, custom_python, inference_filter, digital_output, mqtt_publish, opcua_write, capture — each with ports, port types (VideoFrames, InferenceMeta, EventSignal), parameters (types, defaults, constraints), per-architecture GstMappings (x86_64, arm64_jp4, arm64_jp5, arm64_jp6, sim), executor bindings, plugin dependencies, and hardware_dependent flags
    - Implement port-type compatibility rules (exact match plus declared coercions) and the per-arch LocalServer-bundled plugin manifest
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.8_

  - [x] 1.3 Write property test for catalog well-formedness
    - **Property 13: Catalog well-formedness**
    - **Validates: Requirements 2.8**

  - [x] 1.4 Write unit tests for catalog content
    - Assert presence and parameterization of all required input, preprocessing, inference, post-processing, and output node types
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 2. Implement Workflow_Serializer
  - [x] 2.1 Implement the graph model, JSON Schema, and canonical serialization
    - Write the `WorkflowGraph` model (nodes with id/type/position/parameters, connections with typed port endpoints)
    - Author the Workflow_Definition JSON Schema (schemaVersion 1) and implement `serialize(graph)` with canonical output: sorted keys, nodes and connections ordered by id
    - _Requirements: 3.1_

  - [x] 2.2 Implement parse with descriptive errors and schema migration
    - Implement `parse(doc)` running JSON-Schema validation first (first violation reported with JSON-pointer path), then graph construction
    - Implement the stepwise migration registry: older supported schemaVersion documents upgrade to current and the ParseResult reports `migrations: [from, to]`; unsupported versions return `UNSUPPORTED_SCHEMA_VERSION`
    - _Requirements: 3.2, 3.3, 3.5_

  - [x] 2.3 Implement shared hypothesis generators
    - Write `graph_strategy` producing random valid Workflow_Definitions from the catalog (random node subsets, valid parameter values, type-compatible DAG wiring, optional fan-out) with edge cases: empty/whitespace/unicode strings, single-node graphs, maximal fan-out
    - Write defect-seeding combinators producing controlled invalid graphs (missing input/output nodes, incompatible-port connections, injected cycles, cleared required parameters, detached unreachable nodes) and schema-corrupting document mutators
    - _Requirements: 3.4, 4.6, 6.6_

  - [x] 2.4 Write property test for serialization round trip
    - **Property 1: Serialization round trip**
    - **Validates: Requirements 3.1, 3.2, 3.4**

  - [x] 2.5 Write property test for parser rejection of invalid documents
    - **Property 2: Parser rejects invalid documents descriptively**
    - **Validates: Requirements 3.3**

  - [x] 2.6 Write unit tests for schema migration
    - Fixture documents per registered migration, asserting migrated output and reported migration path
    - _Requirements: 3.5_

- [x] 3. Implement Workflow_Validator
  - [x] 3.1 Implement the shared parameter constraint predicate
    - Write the parameter-validation predicate over `ParameterDescriptor` (type check plus min/max, enum, regex constraints) in `workflow_core`, used by the validator and mirrored by the frontend
    - _Requirements: 1.8, 4.4_

  - [x] 3.2 Implement validate() with all checks
    - Implement checks V1 (≥1 input and ≥1 output node), V2 (connection port direction and type compatibility), V3 (cycle detection via Tarjan SCC reporting nodes in each cycle), V4 (required parameters satisfy constraints), V5 (reachability from input nodes via forward BFS), and W1 warnings
    - Always run all checks and return the complete `ValidationFinding` list with severity, code, message, and nodeId/connectionId
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 3.3 Write property test for the parameter constraint predicate
    - **Property 9: Parameter constraint predicate correctness**
    - **Validates: Requirements 1.8**

  - [x] 3.4 Write property test for validator finding-set exactness
    - **Property 3: Validator finding-set exactness**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**

- [x] 4. Implement Workflow_Compiler
  - [x] 4.1 Implement compile() producing the Compiled Pipeline Document
    - Re-run validation and refuse to compile on errors; topologically sort the DAG
    - Emit one ElementChain per node tagged with nodeId (emltriton chains for model inference with model name and Triton repo/server paths; ExecutorBinding entries for executor-level nodes), each node appearing exactly once
    - Linearize connections with `tee name=t<i>` plus `queue` per branch for fan-out; return CompileError{nodeId, arch} for node types lacking a mapping on the target architecture
    - Compute pluginDependencies as catalog-declared dependencies minus the per-arch LocalServer-bundled set; output the CompiledPipelineDocument JSON (segments, executorBindings, pluginDependencies)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 4.2 Implement simulation-mode compilation
    - Add `simulation=true` support: hardware-dependent nodes (per catalog flag) map to recording stubs (dataset-fed sources via multifilesrc/appsrc, recording bindings for outputs); non-hardware nodes compile identically to non-simulation output
    - _Requirements: 12.6_

  - [x] 4.3 Write property test for node reference exactness
    - **Property 4: Compiler references every node exactly once**
    - **Validates: Requirements 6.6, 6.1**

  - [x] 4.4 Write property test for compiled order and branching
    - **Property 5: Compiled order respects the graph, branches get tee/queue**
    - **Validates: Requirements 6.1, 6.3**

  - [x] 4.5 Write property test for emltriton configuration
    - **Property 6: Inference nodes compile to correctly configured emltriton elements**
    - **Validates: Requirements 6.2**

  - [x] 4.6 Write property test for plugin dependency set
    - **Property 7: Plugin dependency set correctness**
    - **Validates: Requirements 6.4**

  - [x] 4.7 Write property test for unmapped architecture errors
    - **Property 8: Unmapped architecture yields identifying compile errors**
    - **Validates: Requirements 6.5**

  - [x] 4.8 Write property test for simulation stubbing
    - **Property 14: Simulation stubs exactly the hardware-dependent nodes**
    - **Validates: Requirements 12.6**

- [x] 5. Checkpoint - workflow_core complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement portal backend storage and validation APIs
  - [x] 6.1 Add workflow infrastructure to CDK
    - Define DynamoDB tables (Workflows, WorkflowVersions, TestDatasets, TestRuns, WorkflowChatSessions with TTL) with GSIs, portal S3 `workflows/{usecase_id}/...` prefixes, the workflow_core Lambda layer, new Lambda functions, and API Gateway routes in `edge-cv-portal/infrastructure/lib` following existing stack patterns
    - Register new RBAC permission actions (workflow:read/create/edit/save/delete/test/package/deploy, bedrock-config:write) mapped to existing roles in `rbac_middleware`
    - _Requirements: 5.1, 11.1, 11.2, 11.3, 11.4_

  - [x] 6.2 Implement workflows.py (Workflow_Store API)
    - CRUD with Use_Case scoping, save-as-new-version with prior versions retained, list filtered by authorized Use_Cases, open/load returning the stored definition, duplicate under a new name, delete rejected with referencing deployment ids when active deployments exist (409), cross-tenant access denied (403/404 without existence leaks)
    - Write audit log entries for create/modify/delete via the existing AuditLog table
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 11.4, 11.5_

  - [x] 6.3 Implement workflow_validation.py and the node-catalog endpoint
    - Validate endpoint importing the workflow_core layer, returning the complete findings list and recording validation status (passed/failed, findings key, validated_at) on the workflow version
    - `GET /api/v1/workflows/node-catalog` serving the serialized catalog for the frontend palette
    - Shared guard helper used by packaging/publishing/deployment endpoints: reject when error-severity findings exist or the version lacks a passed-validation record
    - _Requirements: 2.8, 4.6, 4.7, 4.10_

  - [x] 6.4 Write integration tests for Workflow_Store
    - CRUD, versioning, duplication, delete-with-deployments against local DynamoDB (moto)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 6.5 Write API guard unit tests
    - Packaging/deployment rejection on validation errors and on missing passed-validation records; delete-with-active-deployments identifying deployments
    - _Requirements: 4.7, 4.10, 5.6_

- [x] 7. Implement Component_Packager and deployment extension
  - [x] 7.1 Implement workflow_packaging.py
    - Compile per user-selected architecture; assemble per-arch artifacts (manifest.json, workflow.json, compiled_pipeline.json, plugins/<arch>/*.so from the curated plugin library in portal S3, python/{nodeId}/handler.py + requirements.txt for Custom_Python_Nodes)
    - Upload to the Use_Case account S3 via assumed role and register `dda.workflow.{workflowId}` version `{workflowVersion}.0.0` with per-arch platform manifests and an install-only recipe (no Run lifecycle)
    - All-or-nothing staging: temp S3 prefix, register only after all artifacts upload; on failure delete the stage, report the failing artifact, register nothing; write audit log entries for packaging
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 11.5, 13.3_

  - [x] 7.2 Write unit tests for packaging atomicity
    - Simulated artifact upload failures assert stage cleanup, failing-artifact reporting, and absence of partial component versions
    - _Requirements: 7.5_

  - [x] 7.3 Extend deployments.py for Workflow_Components
    - Add workflow component type with device/thing-group targeting within the Use_Case; record workflow version → deployment → devices associations (component_type: workflow) in the Deployments table
    - Surface per-device Greengrass deployment status for the workflow page; pre-submit compatibility check comparing device LocalServer version against the component's minLocalServerVersion; rely on Greengrass revision semantics for version replacement; write audit log entries for deploy
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 11.5_

  - [x] 7.4 Write integration tests for packaging and deployment
    - Packaging against mocked S3/Greengrass asserting artifact sets and registration calls; deployment creation and association records
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 7.5 Write RBAC and audit tests
    - Parameterized role×action matrix covering all roles against read/create/edit/save/delete/test/package/deploy; audit log write per create/modify/delete/package/deploy operation
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [x] 8. Implement the Workflow_Builder frontend
  - [x] 8.1 Implement TypeScript schema types, validator mirror, and port compatibility
    - Generate TypeScript types from the Workflow_Definition JSON Schema; implement `arePortsCompatible` mirroring catalog compatibility rules; implement the TS mirror of validator checks V4 (missing required parameters, using the parameter constraint predicate) and V5 (unreachable nodes)
    - _Requirements: 1.4, 1.8, 1.9_

  - [x] 8.2 Implement the canvas page with React Flow
    - Add `@xyflow/react`; build the canvas at `edge-cv-portal/frontend/src/pages/workflows/` with custom node components (category color, title, typed port handles, validation badges), pan/zoom/reposition, and delete of nodes/connections removing attached connections
    - Node_Palette sidebar grouped by the five categories, sourced from the node-catalog endpoint, with HTML5 drag-and-drop placing nodes with default configuration
    - Connection rules via `isValidConnection` calling `arePortsCompatible`; rejected attempts show the reason
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 8.3 Implement the node configuration panel
    - CloudScape form controls rendered from the parameter schema with inline validation errors from the constraint predicate; model_inference `modelName` select populated from the model registry API filtered by Use_Case; Custom_Python_Node code editor plus input/output port-type pickers
    - _Requirements: 1.7, 1.8, 2.6, 2.7_

  - [x] 8.4 Implement inline validation markers
    - Run the TS V4/V5 mirror on every graph mutation; add warning badges to offending nodes and clear them when the condition resolves
    - _Requirements: 1.9, 1.10_

  - [x] 8.5 Implement the actions toolbar and API wiring
    - Save (versioned), Validate (backend full validation with complete findings display), Duplicate, Delete — each gated by role from the existing auth context; open/load renders saved nodes, positions, configurations, and connections
    - _Requirements: 4.8, 4.9, 5.1, 5.2, 5.4, 5.7, 11.1, 11.3_

  - [x] 8.6 Write property test for connection acceptance
    - **Property 10: Connection acceptance equals port compatibility**
    - **Validates: Requirements 1.3, 1.4**

  - [x] 8.7 Write property test for node deletion
    - **Property 11: Node deletion leaves no dangling connections**
    - **Validates: Requirements 1.5**

  - [x] 8.8 Write property test for inline markers
    - **Property 12: Inline markers are exactly the offending nodes**
    - **Validates: Requirements 1.9, 1.10**

  - [x] 8.9 Write frontend component tests for the builder
    - Palette rendering with five sections; validate button invoking backend and displaying the complete findings list
    - _Requirements: 1.1, 4.8, 4.9_

- [x] 9. Checkpoint - portal storage, packaging, and builder complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement prompt-based workflow generation
  - [x] 10.1 Implement workflow_generator.py
    - Chat sessions in WorkflowChatSessions (TTL) holding message history and current canvas definition snapshot; Bedrock Converse API invocation with a `create_workflow` tool whose input schema is the Workflow_Definition JSON Schema and the serialized node catalog in the system prompt
    - Follow-up prompts include the current canvas definition and instruct modification rather than regeneration; tool output parsed by Workflow_Serializer then validated by Workflow_Validator, returning definition plus findings (never auto-saved or deployed); client-side timeout equal to the configured value (≤ 60 s) with failures returned as descriptive errors
    - _Requirements: 10.2, 10.3, 10.5, 10.7_

  - [x] 10.2 Implement Bedrock_Configuration settings
    - Model identifier, region, inference parameters, and timeout in the existing portal settings storage; settings UI section and API restricted to PortalAdmin via bedrock-config:write
    - _Requirements: 10.6_

  - [x] 10.3 Implement the frontend chat panel
    - Collapsible panel maintaining a session id; renders generated workflows onto the canvas only after client-side parse and backend validation succeed; on parse/invocation failure displays the error, leaves the canvas unchanged, and preserves the prompt for retry
    - _Requirements: 10.1, 10.3, 10.4, 10.7_

  - [x] 10.4 Write integration tests for generation
    - Mocked Converse API asserting prompt/catalog assembly, tool-use output handling, follow-up modification flow, and timeout behavior
    - _Requirements: 10.2, 10.5, 10.7_

  - [x] 10.5 Write frontend tests for the chat panel
    - Panel presence, render-after-validation, failure handling leaving canvas untouched, prompt preservation
    - _Requirements: 10.1, 10.3, 10.4, 10.5, 10.7_

- [x] 11. Implement the Workflow_Test_Runner
  - [x] 11.1 Implement workflow_testing.py
    - `POST /test-datasets` with S3 presigned multipart upload and server-side verification (total size ≤ 500 MB, JPEG/PNG formats) before committing the dataset record; violations reject with reason and persist nothing; dataset list scoped to Use_Case
    - `POST /workflows/{id}/test-runs` starting the Step Functions execution; `GET /test-runs/{id}` returning status and per-node results
    - _Requirements: 12.2, 12.3, 12.11_

  - [x] 11.2 Implement the test-run Step Functions state machine and CDK infrastructure
    - Validate → Compile (x86_64, simulation=true) → RunSandbox (Fargate in isolated subnet, no device network routes, no Greengrass resources) → CollectResults; validation/compilation errors short-circuit with per-node/connection error records and status failed without executing the pipeline
    - 10-minute execution timeout stopping the task, marking failed-with-timeout, retaining partial results; TestRuns DynamoDB item and results in S3
    - _Requirements: 12.4, 12.9, 12.12, 12.13_

  - [x] 11.3 Implement the sandbox container and test harness
    - x86_64 image with GStreamer, the DDA plugin set, CPU Triton, and vendored workflow_core; harness renders the compiled document exactly as LocalServer does, feeds sources from the Test_Dataset, records stub activity for hardware bindings instead of actuating endpoints
    - Per-node results `{nodeId, status, outputs, stubActivity, error}` flushed incrementally to S3 so mid-run failures retain prior results and identify the failing node
    - _Requirements: 12.5, 12.6, 12.7, 12.10_

  - [x] 11.4 Write property test for test report coverage
    - **Property 15: Test report covers every node**
    - **Validates: Requirements 12.7**

  - [x] 11.5 Write unit tests for test-run error paths
    - Dataset upload boundaries at/over 500 MB and unsupported formats; validator/compiler failure short-circuit; mid-run failure retention
    - _Requirements: 12.10, 12.11, 12.12_

  - [x] 11.6 Implement the frontend test panel
    - Test action prompting dataset selection or upload scoped to the Use_Case; per-node result display marking stubbed nodes with a "simulated" badge and limitation text
    - _Requirements: 12.1, 12.2, 12.8_

  - [x] 11.7 Write frontend tests for the test panel
    - Test action, dataset picker/uploader, stub identification and limitation description in reports
    - _Requirements: 12.1, 12.2, 12.8_

  - [x] 11.8 Write sandbox integration tests
    - Containerized end-to-end run of a sample workflow against a small Test_Dataset with CPU Triton; timeout behavior with a shortened limit; assert no Greengrass interaction in the test path
    - _Requirements: 12.5, 12.9, 12.13_

- [x] 12. Implement the LocalServer workflow engine
  - [x] 12.1 Vendor workflow_core and add additive database migration
    - Vendor workflow_core into LocalServer; add alembic migration creating `workflow_registrations` and `workflow_executions` tables (additive only, no changes to existing tables)
    - _Requirements: 9.1, 13.5_

  - [x] 12.2 Implement WorkflowWatcher and registration endpoints
    - New `src/backend/workflow_engine/` package: startup scan plus inotify/poll watch of `/aws_dda/workflows/`; register discovered manifest.json + compiled_pipeline.json in workflow_registrations (malformed/incompatible artifacts registered as invalid and reported, never runnable); new Flask endpoints `/workflows` list/trigger/status — no changes to existing endpoints
    - _Requirements: 9.1, 13.3, 13.6_

  - [x] 12.3 Implement WorkflowExecutor pipeline execution
    - Render the launch string from the compiled document (segments joined with `!`, tee branch references); prepend the component's `plugins/<arch>/` directory to GST_PLUGIN_PATH per-run only; execute via existing `GstPipelineManager.run_pipeline` (watchdog, error capture, tag parsing inherited) so inference flows through emltriton → embedded Triton
    - Map failing element back to nodeId via compiled-document tags and report workflow status as failed through the existing status reporting path; execute in a separate thread with a separate registry so Pipeline_Configuration execution is never touched
    - _Requirements: 9.2, 9.3, 9.7, 13.1, 13.4, 13.7, 13.8_

  - [x] 12.4 Implement executor output bindings
    - Post-pipeline processing of executorBindings: digital output actuation via existing dio_utils (condition, pin, signal type, pulse width), MQTT publish via the existing mqtt client, OPC UA writes via the packaged client
    - _Requirements: 9.4, 9.5, 9.6_

  - [x] 12.5 Implement the Custom_Python_Node bridge
    - `emlpython` bridge running user code in a subprocess with a defined stdin/stdout frame+metadata protocol, wall-clock and memory limits; appsink/appsrc pair managed by the executor; failures fail only that workflow run with the node identified
    - _Requirements: 9.8_

  - [x] 12.6 Write edge integration tests for the workflow engine
    - Discovery and registration; execution through GstPipelineManager with delivered plugins; Triton inference; digital output/MQTT/OPC UA against local endpoints or mocks; failure reporting with failing node identified; custom Python bridge end-to-end
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8_

- [x] 13. Verify backward compatibility
  - [x] 13.1 Run existing LocalServer and portal test suites unchanged against the new build
    - Assert same outcomes with no new mandatory configuration; devices without Workflow_Components behave identically
    - _Requirements: 13.1, 13.2, 13.5, 13.6_

  - [x] 13.2 Write device regression tests for workflow/pipeline isolation
    - Deploy and remove a Workflow_Component while a Pipeline_Configuration runs; assert no LocalServer restarts, unchanged Pipeline_Configuration results and status reporting, and workflow failures contained without affecting non-workflow pipelines
    - _Requirements: 13.3, 13.4, 13.7_

  - [x] 13.3 Write concurrent Triton and migration safety tests
    - Concurrent workflow and Pipeline_Configuration inference against the same Triton-served model compared against baseline results; alembic migration applied to a copy of a production-shaped DB asserting additive-only changes
    - _Requirements: 13.5, 13.8_

- [x] 14. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Development happens on the `workflow_manager` git branch (already created)
- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use hypothesis (Python) and fast-check (TypeScript) with a minimum of 100 iterations, tagged `**Feature: workflow-manager, Property {number}: {property_text}**`
- workflow_core is built first as it is a shared dependency of the portal Lambdas, the test sandbox, and the LocalServer workflow engine
- The existing `src/backend/gstreamer/` Pipeline_Configuration path is never modified; all edge changes are additive (Requirement 13)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.1", "3.1"] },
    { "id": 3, "tasks": ["2.2", "3.2", "3.3"] },
    { "id": 4, "tasks": ["2.3", "4.1", "6.1"] },
    { "id": 5, "tasks": ["2.4", "2.5", "2.6", "3.4", "4.2", "8.1"] },
    { "id": 6, "tasks": ["4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "6.2", "8.2"] },
    { "id": 7, "tasks": ["6.3", "6.4", "7.1", "8.3"] },
    { "id": 8, "tasks": ["6.5", "7.2", "7.3", "8.4", "8.6", "8.7"] },
    { "id": 9, "tasks": ["7.5", "7.4", "8.5", "8.8", "10.1", "11.1"] },
    { "id": 10, "tasks": ["8.9", "10.2", "10.3", "11.2", "12.1"] },
    { "id": 11, "tasks": ["10.4", "10.5", "11.3", "11.6", "12.2"] },
    { "id": 12, "tasks": ["11.4", "11.5", "11.7", "12.3"] },
    { "id": 13, "tasks": ["11.8", "12.4"] },
    { "id": 14, "tasks": ["12.5"] },
    { "id": 15, "tasks": ["12.6"] },
    { "id": 16, "tasks": ["13.1", "13.2", "13.3"] }
  ]
}
```
