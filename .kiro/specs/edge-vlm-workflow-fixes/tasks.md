# Implementation Plan

## Overview

This plan fixes three edge-side (`src/frontend`) defects using the exploratory bugfix workflow:
reproduce each bug first (Property N: Bug Condition), capture existing behavior that must not change
(Property 4: Preservation), apply the minimal frontend-only fixes, then validate and confirm no
regressions. All exploration and preservation tests are written and run against the UNFIXED code
before any fix is applied. Bug 1 excludes `VllmModel` from the legacy-workflow model options (enum +
shared `isAssignableModel` helper + no-op filter repair). Bug 2 renders the workflow name (UUID
fallback) as the details-page primary identity. Bug 3 adds a "Name" field (UUID fallback) to the
registration-details `ColumnLayout`. No backend change is in scope.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Conditions for Bugs 1/2/3) FAILS; task 2 (Preservation) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the frontend-only fixes (Bug 1 VLM filter; Bug 2 header name; Bug 3 details Name field), then re-run task 1 (3.4) and task 2 (3.5). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - ensure all tests pass. Depends on wave 2."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.4 and 3.5 depend on 3.1–3.3.
- Task 4 depends on task 3.

## Tasks

- [x] 1. Write bug condition exploration tests (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - VLM excluded from legacy model options (Bug 1); **Property 2: Bug Condition** - Details view shows the name as primary identity (Bug 2); **Property 3: Bug Condition** - Registration details include the name (Bug 3)
  - **CRITICAL**: These tests MUST FAIL on unfixed code — the failures confirm each bug exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate all three bugs exist
  - **Scoped PBT Approach**: For each deterministic bug, scope the property to the concrete failing case for reproducibility, then generalize over randomly generated inputs
  - Bug 1 — legacy model VLM exclusion (`isBugCondition1`, design Bug 1): build the legacy editor's model options as `EditWorkflow.tsx` does (feature-config list → `sortWorkflowModelOptions` → `modelOptions`) from a mixed list — `cookies-binary` (`TritonModel`, READY), `model-cookies-binary` (`LFVModel`, LOADING), `opt125m-smoke` (`VllmModel`, READY) — and assert no option is backed by a `type === "VllmModel"` entry while every `LFVModel`/`TritonModel` entry is retained. Add a companion assertion that `FeatureConfigurationAPI.listModels()` currently returns the `VllmModel` entry (proving the no-op filter). Generalize with a property over random lists containing at least one `VllmModel` entry. Frontend test under `src/frontend` (Jest/RTL)
  - Bug 1 edge case: a list of only `VllmModel` entries yields empty legacy model options
  - Bug 2 — details title (`isBugCondition2`, design Bug 2): render `DeployedWorkflowDetails` with a registration `{ name: "Cookie Inspector", workflowId: "<uuid>", ... }` and assert "Cookie Inspector" is rendered as the page `<Header>` (primary identity). Generalize over random non-empty names. Frontend test under `src/frontend` (Jest/RTL)
  - Bug 3 — registration-details name field (`isBugCondition3`, design Bug 3): render `DeployedWorkflowDetails` with the same named registration and assert a "Name" key/label carrying "Cookie Inspector" appears in the "Registration details" `ColumnLayout`, alongside the existing "Workflow" (UUID) and "Registration ID" fields. Generalize over random non-empty names. Frontend test under `src/frontend` (Jest/RTL)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (Bug 1: `opt125m-smoke` VLM option present; Bug 2: header shows the UUID; Bug 3: no "Name" field rendered)
  - Document counterexamples found (e.g., "opt125m-smoke (VllmModel) is offered as a selectable legacy model option"; "details header renders the UUID while name 'Cookie Inspector' is available"; "registration details shows only Workflow (UUID) and Registration ID, no Name field")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 4: Preservation** - Non-VLM models, other consumers, and UUID fallback unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on UNFIXED code, record it, then encode it
  - Observe on UNFIXED code: `LFVModel`/`TritonModel` entries are selectable and appear in the same `sortWorkflowModelOptions` order with the same labels in the legacy model options
  - Observe on UNFIXED code: `listFeatureConfigurations()` (the raw fetch used by other consumers, e.g. model-status reporting) returns `VllmModel` entries unchanged
  - Observe on UNFIXED code: a registration with `name == null`/empty renders the workflow UUID as the details title and the details `ColumnLayout` shows Version, Architecture, Status, Registered-at, and Registration ID
  - Observe on UNFIXED code: `ListDeployedWorkflows` renders `name || workflowId` with the UUID as secondary text (regression guard; no code change)
  - Write property-based tests capturing these patterns from the design Preservation Requirements:
    - fixed legacy options == (original options minus every `VllmModel` entry) — non-VLM entries, order, and labels unchanged (Requirement 3.1)
    - the feature-config endpoint payload for other consumers is unchanged, `VllmModel` entries still present (Requirement 3.2)
    - null/empty-name registrations still fall back to the UUID as the details title and in the Name field (Requirement 3.3)
    - `ListDeployedWorkflows` still renders `name || workflowId` unchanged (Requirement 3.4)
    - Version, Architecture, Status, Registered-at, and Registration ID render unchanged (Requirement 3.5)
    - a legacy workflow with a non-VLM model already assigned still loads and displays that model (Requirement 3.6)
  - **Testing Approach**: Property-based testing is recommended — the preservation guarantees are universal ("for all non-VLM configs", "for all name-less registrations"); generate many cases automatically to catch edge cases manual tests miss
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Fix the three edge VLM & workflow-display defects (frontend-only)

  - [x] 3.1 Bug 1 — exclude VLM from the legacy workflow model options
    - Add `VllmModel = "VllmModel"` to the `FeatureConfigurationType` enum in `src/frontend/src/components/workflow/types.ts` (value MUST equal the backend `VLLM_FEATURE_TYPE` string `"VllmModel"`)
    - In `src/frontend/src/api/FeatureConfigurationAPI.ts`, fix the no-op filter in `listModels()` (`config.type === FeatureConfigurationType.LFVModel || FeatureConfigurationType.TritonModel`) so it correctly excludes `VllmModel`; extract a shared `isAssignableModel(config)` helper (allow-list `LFVModel`/`TritonModel`, or `config.type !== VllmModel`) and reuse it. Leave `listFeatureConfigurations()` (raw fetch) intact for other consumers
    - In `src/frontend/src/components/workflow/edit/EditWorkflow.tsx`, filter the fetched feature configurations with the shared `isAssignableModel` helper before building `modelOptions`, preserving `sortWorkflowModelOptions` order and existing labels for retained types
    - _Bug_Condition: isBugCondition1(config) where config.type == "VllmModel" AND config appears in the legacy-workflow selectable model options_
    - _Expected_Behavior: fixed legacy options contain no "VllmModel" entry; all "LFVModel"/"TritonModel" entries retained (design Property 1)_
    - _Preservation: non-VLM models selectable and ordered; `listFeatureConfigurations()` payload unchanged for other consumers (design Property 4)_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.6_

  - [x] 3.2 Bug 2 — render the workflow name as the details-page primary identity
    - In `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx`, change the page `<Header>` title from `{registration.workflowId}` to `{registration.name || registration.workflowId}` so a named workflow shows its name and unnamed packages still show the UUID
    - _Bug_Condition: isBugCondition2(registration) where registration.name is a non-empty string AND the details primary identity shows the UUID_
    - _Expected_Behavior: details view renders the name as primary identity; UUID retained as an identifier field (design Property 2)_
    - _Preservation: null/empty-name registrations still show the UUID title (design Property 4)_
    - _Requirements: 2.3, 3.3_

  - [x] 3.3 Bug 3 — add a "Name" field to the registration-details ColumnLayout
    - In `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx`, add a "Name" key/label (`Box variant="awsui-key-label"`) to the "Registration details" `ColumnLayout` rendering `registration.name || registration.workflowId` (or a neutral fallback), while leaving the existing "Workflow" (UUID), "Version", "Architecture", "Status", "Registered", and "Registration ID" fields unchanged
    - _Bug_Condition: isBugCondition3(registration) where registration.name is a non-empty string AND no name field is rendered in the registration details_
    - _Expected_Behavior: registration details display the name alongside the workflow UUID and registration ID (design Property 3)_
    - _Preservation: null/empty-name registrations fall back to the UUID; all other fields render unchanged (design Property 4)_
    - _Requirements: 2.4, 3.3, 3.5_

  - [x] 3.4 Verify the bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - VLM excluded from legacy model options; **Property 2: Expected Behavior** - Details view shows the name; **Property 3: Expected Behavior** - Registration details include the name
    - **IMPORTANT**: Re-run the SAME tests from task 1 — do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm each bug is fixed
    - Run the bug condition exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (no `VllmModel` option in the legacy model list; details header shows the name; a "Name" field appears in registration details)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 4: Preservation** - Non-VLM models, other consumers, and UUID fallback unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (no regressions: non-VLM models selectable and ordered; `listFeatureConfigurations()` payload unchanged; null-name fallback and all other details fields intact; list surface untouched)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the `src/frontend` test suite (Jest/RTL) for the touched areas plus the property-based tests, and ensure all tests pass; ask the user if questions arise

## Notes

- **Test-first ordering is mandatory**: task 1 (bug conditions) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `types.ts`, `FeatureConfigurationAPI.ts`, `EditWorkflow.tsx`, or `DeployedWorkflowDetails.tsx` until the tests are written and their expected outcomes documented.
- **Frontend-only scope**: no backend change is required. The backend name-resolution path (`_registration_name` → `registration_to_dict` in `src/backend/workflow_engine/api.py`) and the deployed-workflows list surface (`ListDeployedWorkflows.tsx`) were confirmed correct and are out of scope.
- **Property references**: Property 1 (Bug 1 — VLM filter) validates Requirements 2.1, 2.2; Property 2 (Bug 2 — details name) validates 2.3; Property 3 (Bug 3 — registration-details name) validates 2.4; Property 4 (Preservation) validates 3.1, 3.2, 3.3, 3.4, 3.5, 3.6.
- **Scope guard**: the VLM exclusion is client-side and scoped to the legacy-workflow model options only — the `/feature-configurations` endpoint (`listFeatureConfigurations()`) must keep emitting `VllmModel` entries for other consumers. The null-name UUID fallback must be preserved in the details view header and the new "Name" field.
- **Primary fix locations**: `src/frontend/src/components/workflow/types.ts` (add `VllmModel` enum), `src/frontend/src/api/FeatureConfigurationAPI.ts` (repair no-op `listModels()` filter + shared `isAssignableModel` helper), `src/frontend/src/components/workflow/edit/EditWorkflow.tsx` (filter `modelOptions` via the helper) for Bug 1; `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx` (header name + registration-details "Name" field) for Bugs 2 and 3.
