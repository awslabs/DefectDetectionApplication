# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Bridged runner receives the camera frame
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **Scoped PBT Approach**: The bug is deterministic (a fixed wrong variable at `pipeline_executor.py:1600`), so scope the property to the concrete camera+bridges document family
  - Create `test/backend-test/workflow_engine/test_camera_bridged_frame_feed_exploration.py` following the existing executor test style (no GStreamer): combine the Aravis document shape from `test_workflow_aravis_executor.py` (`aravisBinding: true` point + appsrc element, injected `frame_grabber` returning a known frame) with an `emlpython` element and handler artifact as in `test_workflow_python_bridge.py`
  - Inject a recording `bridged_pipeline_runner` double capturing `(launch_string, bridges, kwargs)`; use `pipeline_manager_factory=ExplodingPipelineManager` so fallthrough to the plain manager fails loudly
  - Assert the runner was called once with `kwargs["frame_data"] is grabber.frame` and the execution completed
  - Run on UNFIXED code: `python3 -m pytest test/backend-test/workflow_engine/test_camera_bridged_frame_feed_exploration.py -q` (PYTHONPATH per `test/backend-test/workflow_engine/conftest.py` conventions, or in the flask-app container per the repo build rules)
  - **EXPECTED OUTCOME**: Test FAILS - the runner is invoked with NO `frame_data` keyword (`_run_bridged` omits the kwarg when the value is None), matching the on-device diagnosis (`fed_source is None`)
  - Document the counterexample in the test docstring (production executions ed3b60aa / 1aed6b7f / f7e430b7 on jetson-thor1, 120s watchdog timeout)
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-camera bridged and non-bridged call shapes
  - **IMPORTANT**: Follow observation-first methodology - run each family on UNFIXED code first and encode the observed invocation
  - Create `test/backend-test/workflow_engine/test_camera_bridged_frame_feed_preservation.py` covering:
    - Python-source bridged run: runner receives the produced frame as `frame_data` (Requirement 3.1; cross-check `test_workflow_python_source_executor.py`)
    - Feed-free bridged run: runner invoked with no `frame_data` keyword at all - the pre-existing bridged call shape (Requirement 3.2; cross-check `test_workflow_python_bridge.py`)
    - Non-bridged Aravis run: `run_pipeline(launch_string, frame_data)` with the grabbed frame (Requirement 3.3; already pinned by `test_workflow_aravis_executor.py` - reference it rather than duplicating if coverage is exact)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix the bridged branch frame feed in pipeline_executor.py

  - [x] 3.1 Implement the fix
    - In `src/backend/workflow_engine/pipeline_executor.py`, `WorkflowExecutor.execute` bridged branch (lines 1596-1600): change `frame_data=python_frame_data` to `frame_data=frame_data`
    - Update the surrounding comment (lines 1586-1595) to reflect that the merged Frame_Feed (Aravis grab OR Custom Python Produced_Frame) is forwarded to the bridged runner
    - This is the exact one-line fix already validated on-device via hot-patch (execution 26f833f8-f687-4a59-89d2-1a8e2f79dcbc, 3.4s, re-verified after clean container restart)
    - _Bug_Condition: isBugCondition(execution) - Aravis feed planned (frame_data set, python_frame_data None) AND bridge_specs non-empty_
    - _Expected_Behavior: run_bridged_pipeline receives the grabbed camera frame; pipeline reaches EOS_
    - _Preservation: python-source bridged runs (frame_data is python_frame_data after the line 1344-1345 merge), feed-free bridged runs (both None), non-bridged branches untouched_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Bridged runner receives the camera frame
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - **EXPECTED OUTCOME**: Test PASSES (confirms the runner now receives the camera frame)
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-camera bridged and non-bridged call shapes
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)

- [x] 4. Run the workflow_engine backend test suite for regressions
  - Run the full suite: `python3 -m pytest test/backend-test/workflow_engine -q` (or in the flask-app container per the repo build rules; note the interpreter differs by image - python3.11 on JP5, python3.10 on JP6)
  - Pay attention to the sibling suites that pin adjacent behavior: `test_python_bridge_pipeline_stall.py`, `test_workflow_aravis_executor.py`, `test_workflow_python_source_executor.py`, `test_workflow_python_bridge.py`, `test_property_aravis_free_execution_identity.py`, `test_property_python_source_free_identity.py`
  - **EXPECTED OUTCOME**: All green

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise
  - Commit the fix + tests together, stating in the commit what was proven on-device (hot-patch validation) and that component-build verification follows

- [x] 6. Component build (JP7 1.0.17), deploy to thor1, on-device verification
  - **IMPORTANT: Execute with user coordination, NOT autonomously** - builds take ~1-2h and deploys touch a shared device
  - Pre-build gates (per the repo builds rule - do ALL of these BEFORE dispatching the build):
    - Confirm no build is already running: `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"`
    - Move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) - a portal deploy regenerated it today, and the security gate's cdk.out drift guard runs AFTER the ~1h compile
    - Run the preservation guard suite and confirm green (never assume): `python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`
    - This fix touches no preservation-tracked file, so no baseline rebaselining should be needed - but verify with the guard suite
    - Do NOT run any portal deploy (deploy-portal.sh / deploy-infrastructure.sh / deploy-frontend.sh) while the build runs
  - Build: swap `gdk-config.json` to `aws.edgeml.dda.LocalServer.arm64JP7`, `gdk component build` (NEXT_PATCH resolves to 1.0.17), capture output to `.gdk_build_jp7.log`, restore `gdk-config.json` when done
  - Deploy 1.0.17 to jetson-thor1 (this recreates the backend container, reverting the hot-patch - the built fix must take over)
  - On-device end-to-end verification (mandatory per repo rules - the change is not "done" until verified from a real built+deployed component): run workflow `bdfabc2a-d246-466f-a4ca-53bb40c9e119` v5, confirm completion in seconds (not 120s), a Bedrock verdict in the tags, and a healthy backend (no crash, no container restart, no crash-loop) for a sustained period
  - State in the commit/PR what was verified on which device
  - _Requirements: 2.2, 2.3_
