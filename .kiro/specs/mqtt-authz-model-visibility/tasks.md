# Implementation Plan

## Overview

This plan fixes both defects using the exploratory bugfix workflow: surface each defect on UNFIXED
code first (Property 1: workflow topics unauthorized in recipes; Property 2: `VllmModel` rows
dropped from the Deployed models page), capture behavior that must not change (Property 3: shadow
policy and recipe structure; Property 4: legacy model filtering and existing rows), apply the two
minimal fixes, then validate. Defect 1 adds a publish-only `aws.greengrass.ipc.mqttproxy` policy
entry (resource `*`, documented) to the four recipe variants and regenerates the deploy_reliability
structure goldens for exactly that reviewed change. Defect 2 switches the Deployed models page from
the legacy-filtered `listModels()` to the unfiltered `listFeatureConfigurations()` and adds a
friendly "vLLM" type label. A final on-hardware JP6 gate (task 5) requires the user's explicit
go-ahead because the recipe change only takes effect in a rebuilt and redeployed component.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Conditions, Properties 1-2) FAILS; task 2 (Preservation, Properties 3-4) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the fixes (3.1 recipe mqttproxy policy + golden regeneration, 3.2 Deployed models data source), then re-run exploration (3.3) and preservation (3.4). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - run the backend and frontend test suites. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "On-hardware JP6 validation gate (component rebuild + redeploy + live-device checks). Runs only with the user's explicit go-ahead. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.3 and 3.4 depend on 3.1-3.2.
- Task 4 depends on task 3. Task 5 depends on task 4 and on the user's go-ahead (live JP6 device, component rebuild).

## Tasks

- [x] 1. Write bug condition exploration tests (BEFORE any fix)

  - [x] 1.1 Defect 1 exploration — workflow topics unauthorized in recipes
    - **Property 1: Bug Condition** - Workflow topics authorized for Greengrass publish
    - **CRITICAL**: This test MUST FAIL on unfixed recipes — failure confirms the bug exists
    - **DO NOT attempt to fix the test or the recipes when it fails**
    - **NOTE**: This test encodes the expected behavior — it validates the fix when it passes later
    - **GOAL**: Surface counterexamples: non-shadow topics unmatched by every mqttproxy policy
    - **Scoped PBT Approach**: include the concrete failing case `factory/line1/inspection` (and the
      device counterexample semantics from execution `85bf7a61`) alongside hypothesis-generated
      arbitrary non-shadow topics
    - New `test/backend-test/mqtt_authz/test_publish_authorization_exploration.py`: parse each of
      `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`;
      implement Greengrass resource wildcard matching (`*` matches any sequence); assert for every
      generated non-empty topic that some `aws.greengrass.ipc.mqttproxy` policy with operation
      `aws.greengrass#PublishToIoTCore` matches it (from Bug Condition `isBugCondition_1` in design)
    - Run on UNFIXED recipes — **EXPECTED OUTCOME**: FAILS (only `$aws/things/*/shadow/name/*` is
      authorized); document the counterexamples found
    - _Requirements: 1.1, 1.2_

  - [x] 1.2 Defect 2 exploration — VllmModel rows dropped from the Deployed models page
    - **Property 2: Bug Condition** - vLLM models visible on the Deployed models page
    - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
    - **DO NOT attempt to fix the test or the code when it fails**
    - **GOAL**: Surface counterexamples: `VllmModel` entries returned by the backend never render
    - **Scoped PBT Approach**: include the concrete on-device counterexample
      `{"type":"VllmModel","modelName":"opt125m-smoke","status":"LOADING",...}` alongside
      fast-check-generated mixed lists
    - New `src/frontend/src/components/model/vllmVisibility.exploration.test.tsx`: mock the
      `/feature-configurations` axios response, render `DeployedModels`, assert every returned
      model name (including each `VllmModel`) appears as a table row (from `isBugCondition_2` in
      design)
    - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (`listModels()` filters `VllmModel` out via
      `isAssignableModel`); document the counterexamples found
    - _Requirements: 1.3_

- [x] 2. Write preservation property tests (BEFORE any fix)

  - [x] 2.1 Defect 1 preservation — shadow policy and subscribe scope unchanged
    - **Property 3: Preservation** - Recipe access control and structure outside the new policy
    - **IMPORTANT**: Follow observation-first methodology — capture what the UNFIXED recipes contain
    - Observe: each variant's `mqttproxy:1` entry (operations `SubscribeToIoTCore` +
      `PublishToIoTCore`, resource `$aws/things/*/shadow/name/*`) and the full set of
      `SubscribeToIoTCore`-authorized resources
    - New `test/backend-test/mqtt_authz/test_access_control_preservation.py`: property tests
      (hypothesis over shadow-style topics) asserting shadow topics remain authorized for
      subscribe+publish, the `mqttproxy:1` entry equals the recorded baseline, and the
      `SubscribeToIoTCore` resource set is exactly the baseline set (no broadening) — per variant
    - Note: recipe STRUCTURE preservation is already covered by
      `test/backend-test/deploy_reliability/test_config_structure_preservation.py` goldens; those
      goldens will be intentionally regenerated in task 3.1 for exactly the mqttproxy addition
    - Run on UNFIXED recipes — **EXPECTED OUTCOME**: PASSES (baseline confirmed)
    - _Requirements: 3.1, 3.3, 3.4_

  - [x] 2.2 Defect 2 preservation — legacy filtering and existing rows unchanged
    - **Property 4: Preservation** - Legacy model filtering and existing model rows
    - **IMPORTANT**: Follow observation-first methodology — observe UNFIXED behavior first
    - Observe: `listModels()` returns only `LFVModel`/`TritonModel`; `EditWorkflow` model options
      exclude `VllmModel`; `DeployedModels` renders LFV/Triton rows (name, status, type label,
      shape)
    - New `src/frontend/src/components/model/legacyFilterAndRows.preservation.test.tsx` (fast-check):
      for any generated feature-config list, `listModels()` equals the `isAssignableModel` subset
      and `EditWorkflow` options contain no `VllmModel`; for any vLLM-free list, `DeployedModels`
      renders one row per entry with the existing name/status/type/shape rendering
    - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES (baseline confirmed)
    - _Requirements: 3.5, 3.6, 3.7_

- [x] 3. Fix both defects

  - [x] 3.1 Defect 1 — add the publish-only mqttproxy policy to the four recipe variants
    - In each of `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`,
      `recipe-amd64.yaml`: add `'<ComponentName>:mqttproxy:2'` under
      `accessControl["aws.greengrass.ipc.mqttproxy"]` with operations
      `["aws.greengrass#PublishToIoTCore"]` only and resources `["*"]`; the policyDescription
      documents that the workflow `mqtt_publish` topic is free-form user input (no prefix can cover
      it) and that the entry is deliberately publish-only
    - Do NOT edit `recipe.yaml` (build artifact); do NOT touch `mqttproxy:1` or any lifecycle block
    - Regenerate the deploy_reliability structure goldens for this reviewed change:
      `python3 test/backend-test/deploy_reliability/test_config_structure_preservation.py --regenerate`
      then re-run the deploy_reliability suite and confirm it is green
    - No workflow-engine code change (`_default_greengrass_publisher` is correct)
    - _Bug_Condition: isBugCondition_1(input) from design (non-shadow topic publish)_
    - _Expected_Behavior: Property 1 — every accepted topic matched by a PublishToIoTCore policy_
    - _Preservation: Property 3 — shadow policy, subscribe scope, and recipe structure unchanged_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Defect 2 — switch the Deployed models page to the unfiltered fetcher
    - In `src/frontend/src/components/model/DeployedModels.tsx`: call `listFeatureConfigurations()`
      (unfiltered) instead of `listModels()`; update the react-query `queryKey`
    - In `src/frontend/src/components/model/helpers.ts` `modelTypeLabel`: add
      `case FeatureConfigurationType.VllmModel: return "vLLM";`
    - Leave `listModels`, `isAssignableModel`, and `EditWorkflow` untouched
    - _Bug_Condition: isBugCondition_2(input) from design (VllmModel entry in the response)_
    - _Expected_Behavior: Property 2 — every returned model renders as a row_
    - _Preservation: Property 4 — legacy filtering and LFV/Triton rendering unchanged_
    - _Requirements: 2.3, 3.5, 3.6, 3.7_

  - [x] 3.3 Verify bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Workflow topics authorized for Greengrass publish
    - **IMPORTANT**: Re-run the SAME tests from tasks 1.1 and 1.2 — do NOT write new tests
    - The exploration tests encode the expected behavior; passing confirms both fixes
    - **EXPECTED OUTCOME**: both exploration tests PASS
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 3: Preservation** - Recipe access control and structure outside the new policy
    - **IMPORTANT**: Re-run the SAME tests from tasks 2.1 and 2.2 — do NOT write new tests
    - Also re-run the deploy_reliability suite (with the regenerated goldens) and the existing
      edge-vlm-workflow-fixes frontend tests (`legacyModelOptions.exploration.test.tsx` and
      related preservation tests)
    - **EXPECTED OUTCOME**: all preservation tests PASS (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the backend test suite (`test/backend-test`, including `mqtt_authz` and
    `deploy_reliability`) and the frontend jest suite (single run, not watch mode)
  - Ensure all tests pass; ask the user if questions arise

- [~] 5. On-hardware JP6 validation gate (REQUIRES USER GO-AHEAD)
    - **IMPORTANT**: Do not start without explicit user approval — the recipe change only takes
      effect in a rebuilt and redeployed LocalServer component (new component version), which
      affects the live JP6 device (thing `ryanorinagxdevkithomelabjp622`)
    - Rebuild/publish the `aws.edgeml.dda.LocalServer.arm64JP6` component with the updated recipe
      and deploy to the device
    - Defect 1: run a workflow with an `mqtt_publish` (greengrass) node; confirm the workflow-engine
      log shows no `UnauthorizedError` and the message reaches AWS IoT Core on the configured topic;
      confirm shadow sync still works
    - Defect 2: open the local Deployed models page; confirm `opt125m-smoke` is listed with status
      and the "vLLM" type label alongside the Triton model
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.7_

## Notes

- `recipe.yaml` at the repo root is a build artifact — never edit it; the four `recipe-*.yaml`
  variants are the source of truth.
- The deploy_reliability structure goldens include `accessControl`, so the Defect 1 recipe edit
  intentionally fails those golden tests until the goldens are regenerated in task 3.1 (reviewed
  change: only the added `mqttproxy:2` entry may differ).
- The Defect 2 fix must not touch `listModels`, `isAssignableModel`, or `EditWorkflow` — the
  legacy-workflow filter is correct where it is; only the Deployed models page's data source was
  wrong.
- The device 409 from `Text_Generation_API` for `opt125m-smoke` in state `loading` is a handled
  transient (model mid-load after component restart), not part of this spec.
- Frontend tests: jest via react-scripts (run single-shot with `CI=true npm test`, not watch mode);
  property-based tests use the existing fast-check 3.23.2 devDependency. Backend tests: pytest +
  hypothesis under `test/backend-test/`.
