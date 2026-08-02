# Implementation Plan

## Overview

This plan fixes the two-part `opcua_write` output-node bug using the exploratory bugfix
workflow: reproduce the bug first (Property 1: Bug Condition), capture existing behavior
(Property 2: Preservation), apply the minimal fix (add `opcua` to `requirements.txt` +
surface output-binding failures into the run's terminal status), then validate and republish
the JP5 edge component. Requirement 13.7 binding isolation is preserved throughout.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition) fails, task 2 (Preservation) passes. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Implement the fix (3.1 requirements.txt, 3.2 processor+executor), then re-run task 1 (3.3) and task 2 (3.4). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Add property-based fix/preservation coverage and integration tests. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Rebuild/republish and verify on the JP5 edge device. Depends on wave 3."
    },
    {
      "wave": 5,
      "tasks": ["6"],
      "description": "Checkpoint - ensure all tests pass. Depends on wave 4."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 3 (tests written against unfixed code).
- Task 3 depends on 1 and 2. Sub-tasks 3.3 and 3.4 depend on 3.1 and 3.2.
- Task 4 depends on 3. Task 5 depends on 4. Task 6 depends on 5.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - OPC UA package missing and binding failure reported as completed
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate both parts of the bug exist
  - **Scoped PBT Approach**: The bug is deterministic, so scope the property to the concrete failing cases. Part 1: parse `src/backend/requirements.txt` and assert `opcua` is a listed requirement. Part 2: drive the executor/processor with a compiled document containing an `opcua_write` binding (node `n4`) whose injected `opcua_writer` raises (simulating the missing package / a write failure) and assert the run's terminal status is `EXECUTION_STATUS_FAILED` with `failing_node_id == "n4"`. Vary the incidental binding set (add mqtt/dio bindings, reorder) to show the counterexample holds across layouts.
  - Add test for part 1 in a new/existing packaging test (e.g. `test/backend-test/test_requirements.py` or alongside packaging tests): assert the `opcua` package name appears in `src/backend/requirements.txt` (from Bug Condition part 1 in design) — this FAILS on unfixed code
  - Add test for part 2 in `test/backend-test/workflow_engine/` : build a compiled document with an `opcua_write` binding and inject an `opcua_writer` (or `OutputBindingProcessor(opcua_writer=...)`) that raises `RuntimeError("The 'opcua' Python package is not available...")`; run through `WorkflowExecutor.execute` with a stub session/pipeline and assert terminal status is `failed` and `failing_node_id == "n4"` (assertions encode Expected Behavior 2.3, 2.4) — this FAILS on unfixed code (status is `completed`)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g., "`opcua` absent from requirements.txt"; "run with failing opcua_write binding finalizes `completed` with no failing_node_id")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Binding isolation and all-success behavior unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record actual outputs, then assert those outputs
  - Observe on UNFIXED code: a run where every output binding succeeds finalizes `EXECUTION_STATUS_COMPLETED` and logs "Workflow execution %s completed". Record this
  - Observe on UNFIXED code: with several bindings where one fails (e.g. mqtt raises), every other binding runner (opcua, dio) is still invoked — the invariant asserted by `test/backend-test/workflow_engine/test_workflow_output_bindings.py`. Record this
  - Observe on UNFIXED code: `_default_opcua_writer` with a fake `opcua` module injected at the client boundary performs connect → set_value → disconnect — the path exercised by `test/backend-test/workflow_engine/test_workflow_engine_integration.py`. Record this
  - Observe on UNFIXED code: a Bedrock/LLM inference binding failure finalizes `failed` with its node id (existing surfacing path). Record this
  - Write property-based tests (generate random output-binding sets, all succeeding) asserting the fixed executor result equals the original (`completed`) for these non-buggy inputs (from Preservation Requirements in design)
  - Write a property-based test that, with an arbitrary failing subset, asserts every binding runner was invoked (Requirement 13.7 isolation preserved)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 3. Fix for missing opcua dependency and unsurfaced output-binding failures

  - [x] 3.1 Add the opcua runtime dependency to requirements.txt
    - In `src/backend/requirements.txt`, add a pinned `opcua==<version>` entry (python-opcua; import name `opcua`) following the existing convention (exact `==` pin; preserve `setuptools<81`)
    - Confirm the pinned version provides a wheel importable under JP5 Python 3.11 / aarch64 (python-opcua is pure-Python → `py3-none-any` suffices) and that its transitive deps (e.g. `cryptography`, `python-dateutil`, `lxml`/`aiofiles` per version) resolve on-device
    - Add a short comment explaining the pin choice, matching the style of the `grpcio-tools` / `dlr` comments already in the file
    - Do NOT change any other pins
    - _Bug_Condition: isBugCondition(input) where input.hasOpcuaWriteBinding AND "opcua" NOT IN packagedRuntimeDependencies()_
    - _Expected_Behavior: "opcua" IN packagedRuntimeDependencies() AND import_succeeds("opcua")_
    - _Preservation: no other pins change; setuptools<81 cap retained_
    - _Requirements: 2.1, 2.2_

  - [x] 3.2 Surface output-binding failures into the run's terminal status
    - In `src/backend/workflow_engine/output_bindings.py`, add a dedicated `OutputBindingError` exception (mirroring `BedrockInferenceError`) carrying the failing node id(s). In `OutputBindingProcessor.process`, keep the per-binding `try/except` (Requirement 13.7) and existing `logger.exception(...)`, but collect each failed binding's `node_id` and error; after the loop, if any failed, raise `OutputBindingError` with the collected node id(s)
    - In `src/backend/workflow_engine/pipeline_executor.py` (`WorkflowExecutor.execute`, ~lines 1079–1091), reorder finalization so the output-binding handler runs BEFORE the terminal status is committed: run the handler, and if it reports failures, call `_finish_failed(session, execution, error=<summary>, failing_node_id=<node id>)` and `_persist_node_status(session, execution, collector, failing_node_id=..., failure_detail=...)`, then return — do NOT set `COMPLETED` or log "completed". Mirror the existing Bedrock `except ... as e: failing_node_id = getattr(e, "node_id", None)` block (~lines 1040–1063)
    - Update `_run_post_run_handler` (~line 1452) so `OutputBindingError` propagates (or is returned) to `execute` instead of being swallowed; keep containment for any OTHER unexpected handler exception (fall back to `_mark_failed_best_effort`)
    - Reuse existing helpers `_finish_failed`, `_persist_node_status`, `_mark_failed_best_effort`; do NOT introduce a new terminal status (use `EXECUTION_STATUS_FAILED`)
    - Preserve the all-success path: when the handler reports no failures, keep `execution.status = EXECUTION_STATUS_COMPLETED`, commit, `_persist_node_status(success=True)`, and log "completed"
    - _Bug_Condition: isBugCondition(input) where atLeastOneOutputBindingFailed(input) AND terminalStatus(input) = EXECUTION_STATUS_COMPLETED_
    - _Expected_Behavior: terminalStatus(input) = EXECUTION_STATUS_FAILED AND failing_node_id IN failedBindingNodeIds(input)_
    - _Preservation: every output binding still attempted (13.7); all-success runs still finalize completed; Bedrock/LLM surfacing unchanged_
    - _Requirements: 2.3, 2.4, 3.1, 3.2, 3.5_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - OPC UA packaged and binding failures fail the run
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm `opcua` is listed in `requirements.txt` and a run with a failing `opcua_write` binding finalizes `failed` with `failing_node_id == "n4"`
    - Run the bug condition exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (confirms both parts of the bug are fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Binding isolation and all-success behavior unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2, plus the existing
      `test_workflow_output_bindings.py` and `test_workflow_engine_integration.py`
    - **EXPECTED OUTCOME**: Tests PASS (all-success → completed; every binding still attempted; opcua writer connect/write/disconnect unchanged; Bedrock/LLM surfacing unchanged)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Add property-based fix/preservation coverage and integration tests
  - **Property 1: Fix Checking (PBT)** - Generate random output-binding sets with an arbitrary subset marked to fail; assert (a) every binding runner is invoked (isolation) and (b) the run finalizes `failed` iff at least one binding failed (with a failing node id), else `completed`
  - **Property 2: Preservation (PBT)** - Generate all-success binding sets and assert the fixed executor result equals the original (`completed`, "completed" logged)
  - Add an integration test (fake `opcua` module injected at the client boundary, as in `test_workflow_engine_integration.py`) for a FAILING opcua write: assert the run ends `failed` with `failing_node_id` set to the opcua node and that other bindings were still attempted
  - Run all tests
  - **EXPECTED OUTCOME**: All tests PASS
  - _Requirements: 2.3, 2.4, 3.1, 3.2, 3.3_

- [~] 5. Rebuild/republish and verify on the JP5 edge device
  - Rebuild and republish the JP5 edge component so the new `requirements.txt` (including `opcua`) is packaged and installed (recipe `recipe-arm64-jp5.yaml`; build/publish logs `.gdk_build_jp5.log`, `.gdk_publish_jp5.log` at the project root)
  - **NOTE**: gdk build/publish and redeploy are long-running steps that must be run manually by the user in their terminal — provide the exact commands rather than running them here
  - Redeploy to the affected JP5 device so the component reinstalls with `opcua` present
  - Confirm on-device: `python -c "import opcua"` succeeds and node `n4` (`opcua_write`) connects, writes, and disconnects without `ModuleNotFoundError`/`RuntimeError`
  - Confirm on-device: a run whose output binding fails is reported `failed` (with the failing node id) in the run status/execution log — not "completed"
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [~] 6. Checkpoint - Ensure all tests pass
  - Ensure all unit, property-based, and integration tests pass; ask the user if questions arise.

## Notes

- **Test-first ordering is mandatory**: Task 1 (bug condition) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `requirements.txt`, `output_bindings.py`, or `pipeline_executor.py` until both are written and their expected outcomes documented.
- **Property references**: Property 1 (Fix Checking) validates Requirements 2.1, 2.2, 2.3, 2.4; Property 2 (Preservation) validates Requirements 3.1, 3.2, 3.3, 3.4, 3.5.
- **Preserve Requirement 13.7**: the fix must keep per-binding isolation — every output binding is still attempted; failures are collected, not short-circuited.
- **Primary fix locations**: `src/backend/requirements.txt` (add pinned `opcua`); `src/backend/workflow_engine/output_bindings.py` (`OutputBindingProcessor.process` collects failures and raises `OutputBindingError`); `src/backend/workflow_engine/pipeline_executor.py` (`WorkflowExecutor.execute` finalization reordered to let the handler outcome set the terminal status; `_run_post_run_handler` propagates `OutputBindingError`).
- **Deployment (task 5) is manual**: gdk rebuild/republish and redeploy commands are long-running and must be run by the user in their terminal, not by the agent.
