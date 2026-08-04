# Implementation Plan

## Overview

This plan fixes the stale-workflow-registrations bug using the exploratory bugfix workflow:
surface the defect on UNFIXED code first (Property 1: Bug Condition — recipes have no Shutdown
cleanup, the watcher keeps every version registered, the listing is unfiltered), capture existing
behavior that must not change (Property 2: Preservation — the deployed version's registration/
listing/trigger behavior and the full recipe contract modulo the added Shutdown), apply the two
cooperating fixes, then validate and confirm no regressions. All exploration and preservation
tests are written and run against the UNFIXED code before any fix is applied.

The portal fix (task 3) adds a `Shutdown` lifecycle step to `build_recipe` so Greengrass removes
the outgoing version's staged `/aws_dda/workflows/{id}/{version}` directory on replace/remove.
The device fix (task 4) makes the WorkflowWatcher retire registrations — `removed` when the
artifact directory is gone, `superseded` when a higher numeric version of the same workflow is on
disk (covers legacy accumulation and recipe-less components) — and filters the default
`GET /workflows/registrations` listing to active statuses while preserving all rows and execution
history. A final on-hardware JP6 gate (task 6) runs only with the user's explicit go-ahead.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition exploration, portal + device) FAILS; task 2 (Preservation, portal + device) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3", "4"],
      "description": "Apply the fixes: task 3 (portal build_recipe Shutdown + golden updates) and task 4 (device watcher reconciliation + listing filter). Independent of each other; both depend on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["5"],
      "description": "Re-run the SAME task-1 exploration tests (5.1, now PASS) and task-2 preservation tests (5.2, still PASS). Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["6"],
      "description": "Checkpoint: full suites pass, then user-gated on-hardware JP6 verification (portal deploy + NEXT LocalServer build). Runs only with explicit go-ahead. Depends on wave 3."
    }
  ]
}
```

## Tasks

- [ ] 1. Write bug condition exploration tests
  - **Property 1: Bug Condition** - Stale Versions Are Retired and Cleaned
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists and confirm the root-cause chain (no Shutdown in recipe; watcher keeps every version registered; listing unfiltered)
  - **Scoped PBT Approach**: scope the device property to the concrete verified JP6 failing case (dirs `2/`, `6/`, `7/` for one workflow) plus Hypothesis-generated multi-version layouts; scope the portal property across random ids/versions/arch subsets
  - Portal test (`edge-cv-portal/backend/tests/test_workflow_packaging_shutdown_exploration.py`): Hypothesis property over (workflow_id, workflow_version, arch subsets) asserting every `build_recipe` manifest's Lifecycle carries a Shutdown script that removes `/aws_dda/workflows/{id}/{workflow_version}` (from Bug Condition / Fix Implementation change 1 in design); reuse the pure-seam pattern from `test_workflow_packaging_recipe_preservation.py::TestBuildRecipePureContract`
  - Device test (`test/backend-test/workflow_engine/test_stale_registrations_exploration.py`): using `workflow_engine_test_utils` (`make_session_factory`, `make_watcher`, `write_artifact_set`), write version dirs 2/6/7 (and Hypothesis-generated numeric version sets), run `sync_once`, assert only the highest numeric version is active and that `GET /workflows/registrations` (via the api harness pattern from `test_workflow_engine_api.py`) omits stale versions by default; also assert a deleted directory yields status `removed` (not `invalid`) and is omitted, and a trigger on a stale version is rejected 409
  - Run tests on UNFIXED code (portal: `pytest edge-cv-portal/backend/tests/test_workflow_packaging_shutdown_exploration.py`; device: `pytest test/backend-test/workflow_engine/test_stale_registrations_exploration.py`)
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bug exists: Lifecycle has only Run; all versions `registered`; listing returns everything)
  - Document counterexamples found (e.g., "Manifests[0].Lifecycle == {'Run': …}"; "rows wf:2, wf:6, wf:7 all status='registered', listing length 3")
  - Mark task complete when tests are written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Deployed Version and Recipe Contract Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code: single-version scan behavior (row fields, `registered` status, listing payload keys, invalid-artifact reason path, invalid→registered flip-back, empty-root no-op) and the full recipe contract (Run script, ComponentDependencies passthrough incl. dda.plugin.*/model/LocalServer entries, ComponentConfiguration, manifest ordering/attributes)
  - Portal test (`edge-cv-portal/backend/tests/test_workflow_packaging_shutdown_preservation.py`): Hypothesis property asserting the recipe with each manifest's `Shutdown` key deleted (`{k: v for k, v in lifecycle.items() if k != 'Shutdown'}`) equals the independently-computed unfixed golden — structured to hold BOTH pre-fix and post-fix, mirroring `test_workflow_packaging_recipe_preservation.py`'s modulo-comparison technique; include the recently-added ComponentDependencies passthrough and note the llm modelName rewrite is locked by its own existing suite
  - Device test (`test/backend-test/workflow_engine/test_stale_registrations_preservation.py`): Hypothesis property over single-version-per-workflow layouts (valid, malformed, empty root, remove-then-readd sequences) asserting registration rows, listing payloads, invalid reasons, flip-back, and 409 trigger guard match the observed unfixed baseline; assert detail route returns executions for any known id
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [ ] 3. Fix portal packaging: Shutdown cleanup in the generated recipe

  - [ ] 3.1 Add the Shutdown lifecycle step to build_recipe
    - In `edge-cv-portal/backend/functions/workflow_packaging.py::build_recipe`, add to each manifest's Lifecycle: `'Shutdown': {'Script': f"rm -rf {install_dir}", 'Timeout': 60, 'requiresPrivilege': True}`
    - Change nothing else in the recipe (Run script, Artifacts, ComponentDependencies, ComponentConfiguration, manifest ordering/platform attributes all byte-identical)
    - Update the build_recipe docstring: outgoing version's Shutdown removes the staged dir on replace/remove; Run re-copies on every (re)start so reboot-cycle deletion is self-healing; same-workflow-version re-package (new component major, same install_dir) is remove-then-recopy in Greengrass's stop-old-before-start-new order
    - Update the existing golden-contract tests that pin the manifest Lifecycle shape — `edge-cv-portal/backend/tests/test_workflow_packaging_recipe_preservation.py::expected_recipe_modulo_dependencies` (add the Shutdown entry to the golden; every other assertion untouched) and any deployment fixture in `test_packaging_deployment_fixtures.py` that snapshots the Lifecycle
    - _Bug_Condition: isBugCondition(deviceState) from design — stale version dirs persist because the recipe has no cleanup_
    - _Expected_Behavior: Property 1(a) — every manifest carries the install_dir-removing Shutdown_
    - _Preservation: Preservation Requirements from design — recipe unchanged modulo Shutdown_
    - _Requirements: 1.1, 2.1, 3.2_

  - [ ] 3.2 Run portal test suites
    - Run task 1's portal exploration test — the recipe half now PASSES
    - Run task 2's portal preservation test and the updated existing recipe suites (`test_workflow_packaging_recipe_preservation.py`, `test_property_workflow_dependencies.py`, `test_packaging_deployment_fixtures.py`, `test_workflow_packaging_custom_plugins.py`) — all PASS
    - _Requirements: 2.1, 3.2_

- [ ] 4. Fix device engine: retire stale registrations and filter the listing

  - [ ] 4.1 Add non-active statuses and watcher reconciliation
    - In `src/backend/workflow_engine/discovery.py`: add `STATUS_REMOVED = "removed"`, `STATUS_SUPERSEDED = "superseded"`, `ACTIVE_STATUSES = (STATUS_REGISTERED, STATUS_INVALID)`
    - In `src/backend/workflow_engine/watcher.py::sync_once`: group discovered artifact sets by workflow_id; among integer-parsing versions only the highest goes through `_register` as today, lower ones upsert as `superseded` with reason `"superseded by version {highest}"` (skip artifact validation for them); non-numeric versions register exactly as today (never supersede, never superseded)
    - Rename/replace `_invalidate_removed` with `_mark_removed`: any row not seen in the scan gets status `removed` with reason `"Artifact directory was removed"`, from any prior status, idempotently (skip rows already `removed`)
    - Preserve flip-back: reappearing dirs go through the normal path so `removed`/`superseded` rows return to `registered`/`invalid`/`superseded` per current disk state; never delete rows or executions
    - No alembic migration (status is an unconstrained String column)
    - _Bug_Condition: isBugCondition(deviceState) from design — every on-disk version registers active forever_
    - _Expected_Behavior: Property 1(b) — missing dirs → removed, lower numeric versions → superseded_
    - _Preservation: single-version layouts, invalid-artifact path, empty-root no-op, flip-back all unchanged_
    - _Requirements: 1.2, 1.4, 2.2, 2.3, 2.6, 3.1, 3.3, 3.5, 3.6_

  - [ ] 4.2 Filter the registrations listing
    - In `src/backend/workflow_engine/api.py::list_workflow_registrations`: add `includeInactive: bool = False` query parameter; default filters to `status IN ACTIVE_STATUSES`, `includeInactive=true` returns all rows with the existing ordering
    - Leave `registration_to_dict`, the detail route (must keep returning non-active registrations with executions), the trigger 409 guard (`status != STATUS_REGISTERED` already covers removed/superseded), and all execution routes untouched
    - _Bug_Condition: listing returns every historical row_
    - _Expected_Behavior: Property 1(c)/(d) — default listing = active only; history reachable via includeInactive and detail route_
    - _Preservation: active-registration payload shape and all other routes unchanged_
    - _Requirements: 1.3, 2.4, 2.5, 3.1, 3.4_

  - [ ] 4.3 Run device test suites
    - Run task 1's device exploration test — now PASSES
    - Run task 2's device preservation test and the existing watcher/api suites (`test/backend-test/workflow_engine/test_workflow_watcher.py`, `test_workflow_engine_api.py`, `test_workflow_watcher_binding_behavior.py`, `test_property_registration_reevaluation.py`) — all PASS; update `test_workflow_watcher.py::test_removed_artifacts_marked_invalid` to the new `removed` status contract (behavior intentionally changed by 2.2)
    - _Requirements: 2.2, 2.3, 2.4, 3.3, 3.6_

- [ ] 5. Verify fix and preservation properties end to end

  - [ ] 5.1 Verify bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Stale Versions Are Retired and Cleaned
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass, the expected behavior is satisfied
    - Run both portal and device exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ] 5.2 Verify preservation tests still pass
    - **Property 2: Preservation** - Deployed Version and Recipe Contract Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run both portal and device preservation tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [ ] 6. Checkpoint - Ensure all tests pass, then user-gated on-hardware verification
  - Ensure the full portal (`edge-cv-portal/backend/tests/`) and device (`test/backend-test/workflow_engine/`) suites pass (known pre-existing failures ignored per repo steering); ask the user if questions arise
  - **USER-GATED (do not run unattended)**: on the JP6 device, after the portal deploy and the NEXT LocalServer JP6 build are on the device:
    - Package and deploy workflow version N+1 over N with the fixed Lambda; verify Greengrass runs the outgoing version's Shutdown (old `/aws_dda/workflows/{id}/{N}` directory removed) and the Run re-copy on restart — confirming the Greengrass one-shot lifecycle reasoning from the design
    - Verify `GET /workflows/registrations` lists only the deployed version; legacy dirs `2/` and `6/` show `superseded` under `includeInactive=true`; execution history of old versions remains readable via the detail route
    - Verify a nucleus restart leaves the deployed version registered (Shutdown-then-Run re-copy cycle self-heals)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.6_

## Notes

- **Build/shipping**: portal-side changes (task 3) ship via a portal deploy (Lambda). Device-side changes (task 4) ride a LocalServer build — a JP6 build is currently in flight; this fix rides the NEXT one, alongside the folder-source-image-consumption fix.
- **Exploration-first discipline**: task 1's tests MUST fail on unfixed code and MUST NOT be "fixed" — they encode the expected behavior and become the fix check in 5.1. Task 2's tests MUST pass on unfixed code before any implementation begins (observation-first).
- **Greengrass lifecycle assumption to confirm on hardware**: the design asserts that Greengrass runs the outgoing component version's Shutdown for a FINISHED one-shot generic component on replace/remove, and re-executes Run on component (re)start. Task 6 verifies this on the real JP6 device before the fix is considered done.
- **Intentional golden changes**: `test_workflow_packaging_recipe_preservation.py`'s golden recipe contract and `test_workflow_watcher.py::test_removed_artifacts_marked_invalid` pin behavior this fix intentionally changes; tasks 3.1 and 4.3 update them alongside the code, keeping every other assertion untouched.
- **Known pre-existing test failures are ignored** per repo steering.
