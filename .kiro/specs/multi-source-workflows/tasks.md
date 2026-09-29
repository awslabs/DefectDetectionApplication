# Implementation Plan: multi-source-workflows

## Overview

A workflow may contain two to four Frame_Feed_Sources, and each feeds its own Source_Branch. Every new path sits behind one gate: does the document have at least two Frame_Feed_Sources (design Decision 1)? At zero or one source, today's code runs unchanged. The work follows the data from the Portal to the device and back:

1. **Validation.** A shared `frame_feeds` helper, the rewritten `V7_COEXISTENCE_CONFLICT` rule, the Generation_Gate texts, the vendored copy, and the builder's inline mirror.
2. **Packaging and deployment.** Binding points by effective type, `frameFeedBranches`, `frameFeedSourceCount`, and the Multi_Source_Floor, both at packaging time and at deploy time.
3. **Device feed stage.** The camera-lock fix, `frame_feeds` in both runners, the multi-feed planner, an early hardware spike, concurrent grabs, and the device ROI scoped per branch.
4. **Device results.** Branch_Stem artifacts, per-branch detections, tags and output bindings, the results API, and the LocalServer UI.
5. **Portal results and test runner.** The ResultsViewer source label and one Test_Dataset per source.
6. **Verification.** Container suites, component builds, the Portal deploy, and on-device runs on all four lab devices.

Property tests implement the design's 14 properties with Hypothesis on the Python side and fast-check on the TypeScript side:

- one test per property, in its own module;
- tagged `**Feature: multi-source-workflows, Property {N}: {property_text}**`;
- using the repo profiles (100 examples with `HYPOTHESIS_PROFILE=ci`).

Portal backend tests take function modules from the `aws_stack` fixture instead of importing them at module level.

Existing suites keep passing unmodified. The exceptions are the tests that pin the one-source rule itself, which task 1.1 lists and which are updated on purpose.

**Preservation-tracked files touched:** only `src/backend/utils/camera_manager.py`, for the grouped grab in task 6.1. Its hash is rebaselined in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` in the same change. No Dockerfile, compose file, requirements file, device recipe or IAM statement changes.

**Branch.** Work on `spec/multi-source-workflows`, created from `integration/all-specs` after `static-camera-video-loop` and the `camera-grab-lock-leak` fix are merged. This spec builds on that fix and does not repeat it.

## Tasks

- [ ] 1. Validator: frame-feed source rules (design Decisions 1, 2)
  - [ ] 1.1 List every test that pins the one-source rule
    - Grep these for `V7_COEXISTENCE_CONFLICT`, `COEXISTENCE_SINGLETON_TYPES`, `FRAME_FEED_SOURCE_TYPES`, "single-frame appsrc" and frame-feed caps in generators:
      - the workflow_core tests;
      - `edge-cv-portal/backend/tests`;
      - the frontend tests;
      - `test/backend-test/workflow_engine`;
      - `test/backend-test/portal_builds`.
    - Record each assertion that must change, and why, in `verification-notes.md`. Everything else must pass unedited.
  - [ ] 1.2 Create `workflow_core/validator/frame_feeds.py`
    - `effective_type(node)`: resolves `unified_input` through `SOURCE_KIND_TO_SOURCE_TYPE`, with `source_kind` defaulting to `folder`.
    - `frame_feed_sources(graph)`: the source ids in graph order.
    - `source_reach(graph)`: node id → the set of sources that reach it, over every connection.
    - Pure, and imports only `..catalog`. Export the helpers from `validator/__init__.py`.
  - [ ] 1.3 Rewrite the frame-feed rule in `checks.py`
    - Add `MAX_FRAME_FEED_SOURCES = 4`, and remove the two frame-feed entries from `COEXISTENCE_SINGLETON_TYPES`, which keeps its name and type.
    - `_check_v7_coexistence` emits the design's three messages: the limit, the merge point and downstream-of-join. It stays at its position in `validate()` and returns `[]` at one source or none.
    - Update the header comment and the code comment at 118-130.
  - [ ] 1.4 Change the two Generation_Gate texts for `V7_COEXISTENCE_CONFLICT`: the repair instruction and the explanation. Leave the gate's categories and decision logic unchanged.
  - [ ] 1.5 Re-vendor with `src/backend/workflow_engine/vendor/re_vendor.sh`. Add `validator/checks.py` and `validator/frame_feeds.py` to the byte-identity files in `test_vendored_catalog_mirror.py`.
  - [ ]* 1.6 Property tests
    - Property 1 (limit and acceptance) and Property 2 (joins against an independent BFS oracle).
    - Property 4, validator part: the frozen pre-change `_check_v7_coexistence` is the oracle for graphs with one source or none.
    - Consciously update the pinned tests from 1.1, including the frame-feed caps in `generators.py`.
  - _Requirements: 1.1–1.5, 8.1_

- [ ] 2. Builder inline checks (design Decision 2)
  - [ ] 2.1 Add `frontend/src/pages/workflows/frameFeeds.ts` and rewrite `checkV7Coexistence` in `inlineChecks.ts`, with the same constants, messages and ordering as the Python rule. Update the table header comment (line 13).
  - [ ] 2.2 Rewrite `frameFeedMarkers.property.test.ts` for the new rule. Add the Property 3 parity test:
    - a script in the workflow_core tests writes a corpus of generated graphs and their Python findings to a checked-in fixture;
    - vitest asserts that the inline checks produce identical findings for that corpus.
  - [ ] 2.3 Add an example test only if none exists already. It checks that two source nodes can be added and bound independently, and that each node's picker lists both the Static_Image_Camera and the Static_Video_Camera.
  - _Requirements: 1.4, 2.1–2.3_

- [ ] 3. Packaging (design Decisions 3, 4)
  - [ ] 3.1 Before any packaging change, generate the golden corpus at the base commit
    - Compiled documents and packages, excluding `packagedAt`, for the existing single-source packaging and compilation fixtures.
    - Also one fixture per source type, and one `unified_input(aravis_camera)` single-source example. The base commit includes the unified-input-camera-binding fix, so this golden records the fixed output: an `aravisBinding` point and a `camera_input_nodes` record.
  - [ ] 3.2 Add `gather_frame_feed_source_nodes(graph)` (by effective type)
    - Build it on the expanded graph (`expand_unified_inputs(graph, catalog)`), which `package_workflow` already uses for Camera_Input_Nodes since the unified-input-camera-binding fix. A `unified_input(aravis_camera)` therefore already gets `aravisBinding: true`, `nodeType: "aravis_camera_source"` and a `camera_input_nodes` record in every package; nothing is added for it here.
  - [ ] 3.3 Add `frame_feed_branches_section(graph)` and emit `frameFeedBranches` in `compiled_document_json`. Add the `frameFeedSourceCount` manifest key. Both are multi-source only.
  - [ ] 3.4 Add the Multi_Source_Floor
    - `multi_source_min_local_server_version_for(arch)` reads `WORKFLOW_MULTI_SOURCE_MIN_LOCAL_SERVER_VERSIONS`.
    - Raise `minLocalServerVersion`, `minLocalServerVersions` and the recipe HARD dependency to the per-architecture max.
    - Answer 409 `MULTI_SOURCE_UNSUPPORTED_ARCH` for an architecture without a floor.
    - Record `frame_feed_source_count` and `multi_source_min_local_server_versions` on the version item.
  - [ ]* 3.5 Property 5, plus the Property 4 golden comparison against 3.1.
  - _Requirements: 3.1–3.4, 8.1_

- [ ] 4. Deployment check (design Decision 5)
  - [ ] 4.1 For version items with `frame_feed_source_count ≥ 2`, pass the per-architecture `max(today's minimum, recorded multi floor)` to `check_local_server_compatibility`. "Today's minimum" is either the version override or the base map.
  - [ ]* 4.2 Tests
    - Property 6.
    - Example tests: two Aravis nodes are each checked on each device, and a `unified_input(aravis)` source is covered by the camera check.
  - _Requirements: 4.1, 4.2_

- [ ] 5. Infra: floor configuration (design Decision 4)
  - [ ] 5.1 In `compute-stack.ts`, add `WORKFLOW_MULTI_SOURCE_MIN_LOCAL_SERVER_VERSIONS` to the packaging and deployments functions, as `{}` until task 13.4. Add a coverage test beside `test_workflow_min_localserver_floor_coverage.py`: its keys must be known architectures, and an empty map is allowed.
  - [ ] 5.2 Update the infra jest assertions for the new variable. Run the IAM synth gate on the host: no statement change.
  - _Requirements: 3.3_

- [ ] 6. Device feed stage (design Decisions 6, 7, 8)
  - [ ] 6.1 Add the grouped physical grab (design Decision 6a)
    - Add `Camera.trigger()` and `Camera.pop_frame()`, which split `get_frame()`. `get_frame()` stays unchanged.
    - Add `camera_manager.get_camera_frames(requests)`, under the one `get_frame_lock`:
      - open the cameras that aren't connected;
      - start every camera;
      - trigger every camera back to back;
      - pop each frame;
      - stop every camera that started, and return one result per camera.
    - `get_camera_frame` stays unchanged.
    - Property 14 runs against recording fakes. A real-Aravis test runs over two Fake cameras in the flask-app image.
    - Rebaseline `camera_manager.py` in `iam_out_of_scope_baseline.json` with a note entry, then run the guard pair.
    - Prerequisite: the `camera-grab-lock-leak` fix is merged.
  - [ ] 6.2 Add `frame_feeds` and `tag_sink` to both runners
    - Add the keywords to `GstPipelineManager.run_pipeline` and `python_bridge.run_bridged_pipeline`. The `frame_data` path stays byte-identical.
    - Real-GStreamer test in the flask-app image: two `appsrc` chains get one buffer and one EOS each, a missing element is named, and single-feed runs are unchanged.
  - [ ] 6.3 Create `workflow_engine/frame_feed.py`: `FeedPlan`, `plan_frame_feeds`, `FrameFeedError` and the limit of 4, reusing the per-point helpers of `aravis_feed.py` and `python_source.py`. Both of those modules stay unchanged.
  - [ ] 6.4 Early device spike on one Triton-capable device, hot-patched
    - Run a hand-packaged two-branch document with a METADATA model in one branch.
    - Confirm three things:
      - dotted correlation ids come out verbatim in broker file names;
      - staging-directory file targets work;
      - TAG messages identify their posting element.
    - Record the results in `verification-notes.md`. If any of the three fails, switch the Branch_Stem separator to `-src-`, update design Decision 9, and tell the user.
  - [ ] 6.5 Add the executor feed stage
    - The feed-count gate in `execute`, with today's calls unchanged at one feed or none.
    - `_prepare_frame_feeds`:
      - plan;
      - resolve configs and build producer bridges on the run thread;
      - de-duplicate by camera, and log a configuration conflict;
      - run the per-Run pool with a 60 s deadline;
      - apply the failure-message rules and fail before the pipeline.
    - `_point_appsrc_at_frame_feed(rename=False)`, with per-consumer frame copies.
    - A fourth run arm.
    - The `frameFeeds` record, run-log lines, and the failure when `frameFeedBranches` is missing.
  - [ ] 6.6 Scope the device ROI per branch: compute the Crop-bearing branches from the pristine document, then insert or skip each source's ROI.
  - [ ]* 6.7 Properties 7, 8 and 9. The single-source executor, feed and ROI suites run unedited.
  - _Requirements: 5.1–5.8, 8.3, 8.4_

- [ ] 7. Device per-branch artifacts and results (design Decisions 9, 10)
  - [ ] 7.1 Branch_Stem artifacts
    - `run_artifacts.branch_stem`, with token rules and collision suffixes.
    - Per-branch METADATA and `correlation-id` in `_inject_inference_metadata`.
    - Staging-directory routing and per-branch `meta` in `_route_capture_outputs`.
    - `_normalize_branch_artifacts`.
    - `{capture_id}.sources.json`, written after the grabs succeed.
  - [ ] 7.2 Per-branch detections and tags
    - `merge_detections` runs per branch with its own cache key, and Detection_IDs are allocated across the Run.
    - Per-branch tags come through `tag_sink`.
    - `branches` goes into the run metadata. Multi-source runs get no ambiguous top-level keys.
  - [ ] 7.3 Per-branch outputs and processors
    - `branch_of` in `OutputBindingProcessor.process` and `process_subset`, threaded through `_run_post_run_handler`, the capture-phase bindings and Bedrock publish-on-completion.
    - A per-branch `RunContext` for Bedrock and LLM.
    - The bridge detections injector resolves each node's branch.
  - [ ]* 7.4 Properties 10 and 11, plus single-source artifact-name goldens.
  - _Requirements: 6.1, 6.2, 6.5_

- [ ] 8. Device results API and LocalServer UI (design Decision 11)
  - [ ] 8.1 Results API
    - `/results` gets per-branch output entries, `sourceNodeId` on node frames, and a `sources` list.
    - `/output-image`, `/overlay-image` and `/overlay` accept `sourceNodeId`, checked against an allow-list and resolved with no fallback.
    - A multi-source run queried without `sourceNodeId` answers 404, naming the valid sources.
  - [ ] 8.2 LocalServer UI
    - `WorkflowRegistrationAPI.ts` types and URL builders.
    - One `RunResults.tsx` section per source, with its own image, overlay toggle and detections.
    - `RunStatusGraph` and `previewModel` show each branch's own image and detections.
  - [ ]* 8.3 Property 12, and vitest for `RunResults` and `previewModel`.
  - _Requirements: 6.3, 6.4_

- [ ] 9. Portal results view (design Decision 11)
  - [ ] 9.1 `captures.py` `_parse_capture` parses the `.src.{token}` marker into `source_node_id`. `ResultsViewer` shows "Source: {token}".
  - [ ]* 9.2 Backend example tests and vitest.
  - _Requirements: 6.4_

- [ ] 10. Test runner: one Test_Dataset per source (design Decision 12)
  - [ ] 10.1 In `workflow_testing.start_test_run`:
    - accept `source_datasets`;
    - answer 400 `SOURCE_DATASET_MISSING`, naming the sources without a dataset;
    - check each dataset's use case;
    - store the map on the TestRuns item and put `source_dataset_prefixes_json` in the execution input.
  - [ ] 10.2 In the harness, stage each source's dataset into `workdir/dataset/{nodeId}`. Add `renderer.resolve_placeholder_by_node` and report per-node frame counts. Keep today's single-dataset path unchanged.
  - [ ] 10.3 Add the `SOURCE_DATASET_PREFIXES` override to `test-runner-stack.ts`. Confirm the sandbox task role can read every dataset prefix of the use case. **If an IAM change would be needed, stop and ask the user.**
  - [ ] 10.4 In `TestPanel`, show one dataset select per source when there are two or more, and give a disabled reason naming the first source without a dataset. Update the `startTestRun` body type in `services/api.ts`.
  - [ ]* 10.5 Property 13, the harness tests and vitest.
  - _Requirements: 7.1–7.3_

- [ ] 11. Checkpoint: all local suites green
  - Suites to run:
    - device: `workflow_engine`, the camera suites and `camera_sync`;
    - Portal backend and workflow_core;
    - frontend vitest and build;
    - infra jest and the IAM gate;
    - the harness tests.
  - Also run the preservation guard pair.
  - Ask the user if questions arise.

- [ ] 12. Cross-platform container verification
  - [ ] 12.1 Run the preservation guard pair and the full security preservation suite in the flask-app container. Only the intended `camera_manager.py` hash may change.
  - [ ] 12.2 Run the `workflow_engine`, camera and `camera_sync` suites in each platform image:
    - arm64 CPU, JP5, JP6 and JP7 on this host;
    - amd64 on the x86 build server.

    Record the pass counts in `verification-notes.md`.
  - _Requirements: 8.1_

- [ ] 13. Build, deploy and verify on devices (Requirement 9; `.kiro/steering/builds.md`)
  - [ ] 13.1 Pre-build checks: no build running, the guard pair green, `cdk.out` moved aside, and no Portal deploy in flight.
  - [ ] 13.2 Build and publish the LocalServer components strictly one at a time:
    - JP5 on this host;
    - JP6 and JP7 on their fleet servers;
    - amd64 on the x86 build server.

    Record the published versions.
  - [ ] 13.3 Deploy the new components to the MIC-730 (JP5), the Orin AGX (JP6) and thor1 (JP7) by revising their thing deployments. Hold the Dell (amd64) back for 13.5. Confirm on each device that the single-source workflows already deployed keep running unchanged (Requirement 8.4).
  - [ ] 13.4 Set `WORKFLOW_MULTI_SOURCE_MIN_LOCAL_SERVER_VERSIONS` to the 13.2 versions, then deploy the Portal. Do this only after every build has finished, and move `cdk.out` aside afterwards. Verify the changed routes with a temporary Cognito user, and delete it afterwards.
  - [ ] 13.5 On the Dell, still on its old version:
    - a multi-source deployment is refused, naming the device, its version and the minimum (Requirement 4.2);
    - then deploy the new component to it.
  - [ ] 13.6 On each device, run a two-source workflow: one node bound to the Static_Image_Camera, one to the Static_Video_Camera, each branch ending in a capture node, and one branch also carrying a model and an MQTT output that uses `{detection_count}`.
    - Runs repeat from a manual trigger and from a trigger wired to one source's activation port.
    - The image branch's captures are identical and the video branch's change.
    - Artifacts, API entries, UI sections and MQTT payloads are per branch, and the grab times are recorded.
    - Unpinning the video fails the Run before the pipeline, naming the video node and camera.
    - Where the InferenceUploader runs, the Portal ResultsViewer shows both branches labeled by source.
  - [ ] 13.7 Physical cameras on thor1 (JP7, `Basler-267601652282-23405186`) and the Orin AGX (JP6, `Basler-26760165225D-23405149`). Both are Basler acA4600-10uc cameras on USB3 Vision with the BGGR Bayer chain. Cameras have moved between devices, so first confirm each one with `lsusb -d 2676:` and `GET /cameras`, and give it an Image_Source on the device it is attached to.
    - Requirement 9.2, on both devices: a workflow with one source bound to the Basler and one to the Static_Video_Camera. The Basler branch demosaics through its own `bayer2rgb`, and the video branch's captures change between runs.
    - Grouped grab on one device: a workflow with one source on the Basler and one on the Aravis Fake camera `Fake_1`, run repeatedly. Both go through one grouped grab (design Decision 6a). Record the grab times, and check that each branch applies its own camera's Image_Source settings, including a configured ROI (Requirement 5.5).
    - Two physical cameras: ask the user to move both Baslers to one device. Then run a workflow with one source per Basler and measure the grouped grab's skew. The target is a few milliseconds, against about 0.3 s one camera after the other.
  - [ ] 13.8 Soak each device for 30 minutes with periodic runs: the backend stays healthy, with no restart and no OOM kill.
  - [ ] 13.9 Write `verification-notes.md`: what was verified on which device and version, timings (including grab skew), and deviations. Restore every device and remove the verification workflows afterwards.
  - _Requirements: 4.2, 5.1–5.8, 6.1–6.5, 8.3, 8.4, 9.1–9.3_

- [ ] 14. Cross-spec amendments (Requirement 8.2)
  - Amend `aravis-camera-input/design.md` at lines 245 and 390.
  - Amend `custom-python-source/requirements.md`, Requirement 8 criteria 1, 2 and 5.
  - In `static-camera-video-loop`, mark the Requirement 6.4 deviation note closed, citing the 13.6 results (Requirement 8.3).
  - _Requirements: 8.2, 8.3_

- [ ] 15. Commit (with the user's go-ahead)
  - Commit on `spec/multi-source-workflows`. The message states the device verification (devices and versions) and the rebaselined `camera_manager.py` hash.
  - Merge into `integration/all-specs` as the user directs.
