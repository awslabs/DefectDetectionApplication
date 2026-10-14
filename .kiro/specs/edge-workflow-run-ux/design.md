# Edge Workflow Run UX Bugfix Design

## Overview

Portal-built workflows deployed to an edge device are discovered and registered by LocalServer's workflow engine, and the edge HTTP API (`src/backend/workflow_engine/api.py`) fully supports listing registrations, viewing a registration with its executions, triggering runs, and checking run status. The LocalServer frontend, however, has no UI over these endpoints: its routes cover only the legacy Pipeline_Configuration workflow pages (`/workflows` → `ListWorkflows`/`WorkflowDetails`/`EditWorkflow`), so an operator at the device cannot see cloud-deployed registrations or run them.

The fix is UI-only and additive in `src/frontend`: a new API client module over the existing registrations/executions endpoints, a "Deployed workflows" list page, and a registration details page with a trigger control and execution history. Integration with the rest of the app is limited to three small additive edits (one route subtree in `App.tsx`, one nav link in `SideNav.tsx`, plus new files). No backend changes and no changes to the legacy Pipeline_Configuration pages or the existing `WorkflowAPI.ts` client.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — a Workflow_Component registration (or its executions) exists on the device, but the LocalServer UI renders no view or control for it
- **Property (P)**: The desired behavior — the UI lists registrations with identity and status, offers a trigger control only for `registered` registrations, shows invalid reasons, and displays execution status/history including failure details
- **Preservation**: The legacy Pipeline_Configuration pages, all other LocalServer UI pages, and the backend workflow engine must behave exactly as before
- **Workflow_Component registration**: A cloud-deployed workflow discovered on the device, served by `GET /workflows/registrations`; shape per `registration_to_dict`: `{registrationId, workflowId, version, arch, artifactPath, status, registeredAt, invalidReason?}` with `status ∈ {registered, invalid}` (`invalidReason` present only when not `registered`)
- **Workflow execution**: A run of a registration, shape per `execution_to_dict`: `{executionId, registrationId, status, startedAt, finishedAt, failingNodeId, error}` with `status ∈ {pending, running, completed, failed}` (from `pipeline_executor.py`)
- **Legacy Pipeline_Configuration pages**: The existing `/workflows` UI (`components/workflow/**`, `api/WorkflowAPI.ts`) over the legacy `/workflows` endpoints — untouched by this fix
- **Edge HTTP API**: `GET /workflows/registrations`, `GET /workflows/registrations/{id}` (includes `executions`), `POST /workflows/registrations/{id}/trigger` (409 for invalid registrations), `GET /workflows/executions/{id}` in `src/backend/workflow_engine/api.py`

## Bug Details

### Bug Condition

The bug manifests whenever at least one Workflow_Component registration exists on the device. The frontend router (`App.tsx`) defines no route for registrations, the side navigation (`SideNav.tsx`) has no entry, and no API client module calls the registrations/executions endpoints — so the data the backend serves is unreachable from the UI.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type DeviceUiState
    -- registrations: list of Workflow_Component registrations on the device
    -- ui: the set of routes/pages the LocalServer frontend renders
  OUTPUT: boolean

  RETURN input.registrations IS NOT EMPTY
         AND NOT EXISTS page IN input.ui THAT displays input.registrations
             (identity, status, invalidReason, trigger control, executions)
END FUNCTION
```

Equivalently, for every UI input touching cloud-deployed registrations — viewing the list, viewing a registration's status/executions, triggering a run, seeing an invalid reason — the UI has no behavior at all (defect clauses 1.1–1.4).

### Examples

- A device has one registration `{workflowId: "wf-1", version: "3", status: "registered"}`. Expected: a UI page lists it with identity and status and offers a Run control. Actual: no page in the LocalServer UI shows it; the operator must curl `GET /workflows/registrations`.
- A registration is `invalid` with `invalidReason: "Missing required artifact file: manifest.json"`. Expected: the UI shows the registration with invalid status and the reason, with no Run control. Actual: the operator has no indication the registration exists at all.
- Executions exist for a registration, the latest with `status: "failed"`, `failingNodeId: "node-7"`, `error: "..."`. Expected: the UI shows execution history with the failure details. Actual: nothing is shown; the operator must curl `GET /workflows/registrations/{id}`.
- Edge case: a device with zero registrations. Expected: the registrations page renders an empty state without error (2.5). Actual: there is no registrations page.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Legacy Pipeline_Configuration pages (list, details, edit under `/workflows`) continue to render and call the legacy `/workflows` endpoints via the existing `WorkflowAPI.ts` client, unchanged (3.1)
- All other LocalServer UI pages (image sources, models, live results, result history, image capture, application health) continue to render and behave as before (3.2)
- The backend workflow engine and its HTTP API are not modified in any way (3.3)
- Triggering an invalid registration continues to be rejected by the existing backend 409; the UI must surface, not bypass or duplicate-in-conflict, that rejection (3.4)

**Scope:**
All inputs that do NOT involve the new deployed-workflows pages are completely unaffected by this fix. This includes:
- Any navigation to existing routes (`/workflows`, `/image-sources`, `/models`, `/result`, `/history`, `/capture`, `/capture-results`, `/application-health`)
- All existing API calls made by the frontend (legacy `/workflows` CRUD/run, cameras, streams, system health, etc.)
- All backend request handling, including the trigger endpoint's 409 behavior for invalid registrations

**Structural preservation:** the fix touches existing files only additively and minimally:
- `App.tsx`: add one new route subtree (`deployed-workflows`) alongside the existing routes; no existing route is edited
- `SideNav.tsx`: add one new link item; no existing item is edited
- Everything else is new files (`api/WorkflowRegistrationAPI.ts`, `components/deployed-workflow/**`)
- `config/Interface.tsx`, `api/WorkflowAPI.ts`, `components/workflow/**` are not modified (the new API module imports the existing `Connection.ENDPOINT` read-only)

## Hypothesized Root Cause

This is a missing-feature bug: the UI layer was never built for the workflow engine's registration/execution endpoints. The specific gaps are:

1. **No routes**: `App.tsx` defines routes only for legacy features; nothing maps to the registrations/executions concepts
2. **No navigation entry**: `SideNav.tsx` has no link, so even a hidden page would be undiscoverable
3. **No API client**: `src/frontend/src/api/` has no module calling `/workflows/registrations` or `/workflows/executions`; `WorkflowAPI.ts` covers only the legacy Pipeline_Configuration endpoints
4. **No pages/components**: there is no component tree rendering registrations, trigger controls, or execution history

The backend endpoints were verified working (`src/backend/workflow_engine/api.py`), confirming the root cause lives entirely in `src/frontend`.

## Correctness Properties

Property 1: Bug Condition - Deployed workflow registrations are visible and runnable from the UI

_For any_ set of registrations and executions returned by the edge HTTP API (any mix of `registered`/`invalid` registrations, including the empty set, and any mix of execution statuses), the fixed UI SHALL render the registrations list showing each registration's identity (workflowId, version) and status; SHALL render an empty state without error when the set is empty; SHALL offer a trigger control if and only if a registration's status is `registered`; SHALL display `invalidReason` for invalid registrations; and SHALL display execution status and history including `failingNodeId` and `error` for failed executions.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Existing pages, API calls, and backend behavior unchanged

_For any_ input that does NOT involve the new deployed-workflows pages (navigation to any pre-existing route, any legacy Pipeline_Configuration operation, any backend request), the fixed application SHALL produce the same result as the original application, preserving the legacy `/workflows` pages and API client, all other UI pages, unmodified backend behavior, and the backend's 409 rejection of trigger requests for invalid registrations.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

All paths under `src/frontend/src/`.

**New file**: `api/WorkflowRegistrationAPI.ts`

API client following the sibling module conventions (axios, typed request/response functions). It imports `Connection` from `config/Interface` and builds its own endpoint constants so `Interface.tsx` is untouched:

```typescript
export type RegistrationStatus = "registered" | "invalid";
export type ExecutionStatus = "pending" | "running" | "completed" | "failed";

export interface WorkflowRegistration {
  registrationId: string;
  workflowId: string;
  version: string;
  arch: string;
  artifactPath: string;
  status: RegistrationStatus;
  registeredAt: number;
  invalidReason?: string;
}

export interface WorkflowExecution {
  executionId: string;
  registrationId: string;
  status: ExecutionStatus;
  startedAt: number | null;
  finishedAt: number | null;
  failingNodeId: string | null;
  error: string | null;
}

export interface WorkflowRegistrationDetails extends WorkflowRegistration {
  executions: WorkflowExecution[];
}

listWorkflowRegistrations(): Promise<WorkflowRegistration[]>          // GET  /workflows/registrations
getWorkflowRegistration(id): Promise<WorkflowRegistrationDetails>     // GET  /workflows/registrations/{id}
triggerWorkflowRegistration(id): Promise<WorkflowExecution>           // POST /workflows/registrations/{id}/trigger
getWorkflowExecution(id): Promise<WorkflowExecution>                  // GET  /workflows/executions/{id}
```

**New files**: `components/deployed-workflow/`

1. **`presentation.ts`** — pure presentational logic, kept free of React/DOM so it is directly property-testable:
   - `canTrigger(registration): boolean` — true iff `status === "registered"` (2.2, 2.4)
   - `registrationStatusIndicator(registration): { type: StatusIndicatorProps.Type; text: string }` — maps `registered`/`invalid` to Cloudscape status indicator props
   - `executionStatusIndicator(execution)` — maps `pending`/`running`/`completed`/`failed` to indicator props
   - `sortExecutions(executions): WorkflowExecution[]` — newest first by `startedAt` (stable for ties/nulls) for the history table (2.3)
   - `executionFailureDetails(execution): { failingNodeId?: string; error?: string } | undefined` — present iff `status === "failed"` (2.3)
   - `isExecutionActive(execution): boolean` — true iff status is `pending` or `running`
   - `shouldPoll(executions): boolean` — true iff any execution is active; drives the detail page's refetch interval

2. **`list/ListDeployedWorkflows.tsx`** — registrations list page:
   - `useQuery(["listWorkflowRegistrations"], listWorkflowRegistrations)` (react-query, matching `ListWorkflows.tsx` conventions)
   - Cloudscape `Table` with columns: workflow (workflowId, link to details), version, arch, status (`StatusIndicator` via `registrationStatusIndicator`; invalid rows show `invalidReason` in a description/popover) (2.1, 2.4)
   - Cloudscape `Table` `empty` slot renders the empty state ("No deployed workflows") without error when the list is empty (2.5)

3. **`details/DeployedWorkflowDetails.tsx`** — registration details page (`:registrationId` from `useParams`):
   - `useQuery(["getWorkflowRegistration", registrationId], ...)` with `refetchInterval: shouldPoll(data.executions) ? EXECUTION_POLL_INTERVAL_MS : false` (matching the `refetchInterval` pattern used by `ApplicationHealthOverview`/`RefreshDisplay`) so running/pending executions refresh automatically and polling stops once all executions are terminal
   - Header shows identity, status, `registeredAt`; invalid registrations show an alert with `invalidReason` and render no trigger control (2.4)
   - Trigger control: a "Run workflow" `Button` rendered only when `canTrigger(registration)`; wired to `useMutation(triggerWorkflowRegistration)`; on success invalidates the detail query so the new `pending` execution appears immediately and polling starts (2.2)
   - Trigger error handling: on failure (including the backend 409 for registrations that became invalid between render and click) shows the backend's `detail` message via the existing `AppLayoutContext` flashbar (`addError`), reflecting rather than masking the backend rejection (3.4)
   - Executions table: `sortExecutions` order; columns for execution id, status indicator, started/finished timestamps; failed rows show `failingNodeId` and `error` via `executionFailureDetails` (2.3); empty slot when no executions exist yet

**Modified file**: `App.tsx` (additive only — one new route subtree)

```tsx
<Route path="deployed-workflows" handle={{ breadcrumb: "Deployed workflows" }}>
  <Route index element={<ListDeployedWorkflows />} />
  <Route
    path=":registrationId"
    element={<DeployedWorkflowDetails />}
    handle={{ breadcrumb: "Deployed workflow details" }}
  />
</Route>
```

The path `deployed-workflows` is a new top-level route distinct from the legacy `workflows` subtree, so no legacy route or breadcrumb changes (3.1).

**Modified file**: `components/layout/SideNav.tsx` (additive only — one new link)

Add `{ type: "link", text: "Deployed workflows", href: "/deployed-workflows" }` to the existing "Configure" section, after "Workflows". The `activeHref` prefix logic already handles the new top-level route without changes.

**Not modified**: `config/Interface.tsx`, `api/WorkflowAPI.ts`, `components/workflow/**`, anything under `src/backend/` (3.1–3.3).

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

The frontend uses Create React App, so Jest with `@testing-library/react` and `@testing-library/jest-dom` is available via `react-scripts test` (there are currently no unit tests under `src/frontend/src`; these will be the first). For property-based tests, **fast-check** will be added as a devDependency — it is the standard PBT library for Jest/TypeScript and requires no runner changes. Cypress (already configured) is available for integration-level checks. API calls in Jest tests are mocked at the API-module boundary (`jest.mock("api/WorkflowRegistrationAPI")`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis (missing routes, nav entry, API client, and pages). If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that render the app router / side navigation and assert the deployed-workflows surface exists, plus a static check that an API client for the registrations endpoints exists. Run these tests on the UNFIXED code to observe failures.

**Test Cases**:
1. **Route Existence Test**: Render the router at `/deployed-workflows` and assert a registrations page renders rather than falling through (will fail on unfixed code)
2. **Navigation Entry Test**: Render `SideNav` and assert a "Deployed workflows" link exists (will fail on unfixed code)
3. **API Client Test**: Assert `api/WorkflowRegistrationAPI` exports `listWorkflowRegistrations`/`triggerWorkflowRegistration` targeting `/workflows/registrations` (will fail on unfixed code — module does not exist)
4. **Invalid Registration Visibility Test**: With a mocked API returning an invalid registration, assert the UI surfaces its status and reason somewhere (will fail on unfixed code — nothing renders it)

**Expected Counterexamples**:
- No route matches `/deployed-workflows`; no nav link; no API module
- Possible causes: feature never implemented in the frontend (confirmed root cause), routes hidden behind a flag (refuted — no such flag exists), backend endpoints absent (refuted — verified in `api.py`)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed UI produces the expected behavior.

**Pseudocode:**
```
FOR ALL registrationSets WHERE isBugCondition(deviceState) DO
  render fixed UI with mocked API returning registrationSet
  ASSERT list shows every registration's identity and status        -- 2.1
  ASSERT trigger control present IFF status == "registered"          -- 2.2, 2.4
  ASSERT invalid registrations show invalidReason                    -- 2.4
  ASSERT executions shown with status/history and failure details    -- 2.3
  ASSERT empty registration set renders empty state without error    -- 2.5
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed application produces the same result as the original application.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalApp(input) = fixedApp(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

Preservation is additionally guaranteed structurally: the fix adds new files plus one route subtree and one nav link. A diff review confirming that no existing route, nav item, API module, or backend file changed is the strongest preservation evidence for 3.1–3.3; 3.4 is preserved because the backend is untouched and the UI surfaces the existing 409.

**Test Plan**: Observe behavior on UNFIXED code first for the legacy pages and navigation (route rendering, nav items, legacy API endpoint constants), then write tests capturing that behavior to verify it is identical after the fix.

**Test Cases**:
1. **Legacy Workflow Route Preservation**: Observe that `/workflows`, `/workflows/:id`, `/workflows/:id/edit` render the legacy components on unfixed code, then verify identical rendering and breadcrumbs after the fix (3.1)
2. **Navigation Preservation**: Observe the existing `SideNav` sections/links on unfixed code, then verify all pre-existing items are unchanged (same text, href, order) after the fix, with only the one new link added (3.2)
3. **Legacy API Client Preservation**: Verify `WorkflowAPI.ts` and `config/Interface.tsx` are byte-identical (no diff) and legacy endpoints still target `${Connection.ENDPOINT}/workflows` (3.1, 3.3)
4. **Backend Preservation**: Verify no file under `src/backend/` is modified; existing backend tests for the trigger 409 continue to pass unchanged (3.3, 3.4)

### Unit Tests

- `presentation.ts`: `canTrigger` for both statuses; status-indicator mappings for all four execution statuses and both registration statuses; `executionFailureDetails` present only for `failed`; `shouldPoll` true/false cases; `sortExecutions` ordering with null `startedAt`
- `ListDeployedWorkflows`: renders rows for mocked registrations (identity + status), invalid reason shown, empty state on `[]`, error state on API failure
- `DeployedWorkflowDetails`: trigger button present only for `registered`; invalid alert with reason; trigger click calls the API and refreshes; 409 mutation error shows the backend `detail` message; failed execution row shows `failingNodeId` and `error`

### Property-Based Tests

Using fast-check arbitraries over `WorkflowRegistration`/`WorkflowExecution` (random statuses, optional/null fields, arbitrary timestamps), targeting the pure logic in `presentation.ts`:

- **Property 1 (fix)**: for any generated registration, `canTrigger(r) === (r.status === "registered")`; for any registration list rendered into the list page, every registration's workflowId/version/status appears and a trigger affordance exists iff `canTrigger` (validates 2.1, 2.2, 2.4)
- **Property 1 (fix)**: for any generated execution list, `sortExecutions` returns a permutation ordered newest-first, `executionFailureDetails` is defined iff status is `failed` and echoes `failingNodeId`/`error`, and `shouldPoll` is true iff some execution is `pending` or `running` (validates 2.3)
- **Property 2 (preservation)**: for any generated app state not involving deployed workflows (random legacy route from the pre-existing route set), the fixed router resolves it to the same component as the original route table (validates 3.1, 3.2)

### Integration Tests

- Cypress (or RTL with the full router): navigate Side nav → Deployed workflows → select a registration → trigger a run → observe the new `pending` execution appear and (with a mocked/stubbed API sequence pending → running → completed) the status update via polling
- Trigger an invalid registration path: stub a 409 response and verify the UI shows the backend rejection message and no execution is added
- Legacy flow smoke test: navigate to `/workflows` list and details and verify the legacy pages still render and call the legacy endpoints
