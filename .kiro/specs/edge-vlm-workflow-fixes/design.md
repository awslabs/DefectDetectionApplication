# Edge VLM & Workflow Display Fixes — Bugfix Design

## Overview

Three independent edge-side (`src/`) defects are fixed under one spec because they share the same
theme (VLM handling + deployed-workflow identity) and are confined to the on-device frontend served
from `src/frontend`. Each is a minimal, targeted fix scoped to a confirmed bug condition; no backend
change is required.

- **Bug 1 (VLM leaks into legacy-workflow model selection).** The legacy workflow editor builds its
  model `<Select>` options from the raw `/feature-configurations` response, which now includes
  vision-language / vLLM models (`type === "VllmModel"`, emitted by `get_features_vllm()` via
  `get_features_triton()` in `src/backend/utils/feature_configs_utils.py`). Legacy workflows cannot
  run VLMs, so the operator can pick one and build a broken workflow. The intended guard —
  `FeatureConfigurationAPI.listModels()` — is a no-op: its filter
  `config.type === FeatureConfigurationType.LFVModel || FeatureConfigurationType.TritonModel` binds
  as `(config.type === LFVModel) || FeatureConfigurationType.TritonModel`, and the right operand is
  the truthy string `"TritonModel"`, so the predicate is always true and excludes nothing. Worse,
  `EditWorkflow.tsx` does not even call `listModels()` — it maps the unfiltered
  `listFeatureConfigurations()` result. `VllmModel` is also missing from the
  `FeatureConfigurationType` enum. The fix excludes `VllmModel` entries from the legacy model options
  while leaving `LFVModel`/`TritonModel` selectable and keeping `VllmModel` visible to every other
  consumer of the endpoint.

- **Bug 2 (deployed-workflow details shows the UUID as primary identity).** `DeployedWorkflowDetails.tsx`
  renders `registration.workflowId` (the opaque UUID) as the page `<Header>` title, even when the
  registration carries a human-readable `name`. The name is already available end to end — the
  backend `registration_to_dict()` (in `src/backend/workflow_engine/api.py`) reads `workflowName`
  from the deployed `manifest.json` and serializes it as `name`, and `WorkflowRegistrationAPI.ts`
  types it as `name?: string | null`. The fix renders the name as the page's primary identity, with
  the UUID kept as a fallback.

- **Bug 3 (registration details omit the name).** The "Registration details" `ColumnLayout` in
  `DeployedWorkflowDetails.tsx` shows "Workflow" (UUID) and "Registration ID" but never the
  human-readable name. The fix adds a "Name" field alongside the existing identifiers, keeping the
  UUID fallback when no name is present.

The strategy for each defect follows the bug-condition methodology: (1) write an exploration test
that demonstrates the defect on unfixed code, (2) capture the behavior that must be preserved with
property-based tests where applicable, (3) apply a minimal fix, (4) re-run both. Verification is via
the `src/frontend` test suite (Jest/RTL) plus the existing property-based test approach used in this
repo.

## Glossary

- **Bug_Condition (C)**: The predicate identifying inputs that trigger a bug (defined per bug below).
- **Property (P)**: The desired behavior for inputs satisfying the bug condition.
- **Preservation**: Existing behavior for non-bug-condition inputs that the fix must not change.
- **Feature configuration**: A deployed-model descriptor served by `GET /feature-configurations`,
  with fields `type` (`"LFVModel"` | `"TritonModel"` | `"VllmModel"`), `modelName`, `status`,
  `defaultConfiguration`. TypeScript type `FeatureConfiguration` in
  `src/frontend/src/components/workflow/types.ts`.
- **VLLM_FEATURE_TYPE**: The string constant `"VllmModel"` in
  `src/backend/utils/feature_configs_utils.py`; the exact `type` value emitted by
  `get_features_vllm()` for vLLM models (surfaced through `get_features_triton()`).
- **`listModels()` / `listFeatureConfigurations()`**: The two fetchers in
  `src/frontend/src/api/FeatureConfigurationAPI.ts`. `listModels()` is intended to return only
  models assignable to legacy workflows but its filter is a no-op; `listFeatureConfigurations()`
  returns the raw, unfiltered list and is what `EditWorkflow.tsx` currently consumes.
- **Legacy workflow editor**: `src/frontend/src/components/workflow/edit/EditWorkflow.tsx` +
  `ImageSourceAndModel.tsx`; the traditional (non-designer) workflow add/edit screen with a model
  `<Select>` built from `modelOptions`.
- **WorkflowRegistration**: A deployed on-device workflow package, serialized by
  `registration_to_dict()` in `src/backend/workflow_engine/api.py` and typed as
  `WorkflowRegistration` in `src/frontend/src/api/WorkflowRegistrationAPI.ts`. Carries `workflowId`
  (UUID), optional `name`, `version`, `arch`, `artifactPath`, `status`, `registeredAt`.
- **`name` field**: The human-readable workflow name. Backend `_registration_name()` reads
  `workflowName` from `<artifact_path>/manifest.json` at serve time and returns `None` for
  missing/empty/unreadable values, so the UI falls back to the UUID. Confirmed correct — no backend
  change is in scope.
- **Deployed-workflows list**: `src/frontend/src/components/deployed-workflow/list/ListDeployedWorkflows.tsx`.
  Already renders `item.name || item.workflowId` with the UUID as secondary text — confirmed correct
  and out of scope.
- **Registration details view**: `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx`
  — the sole defect surface for Bugs 2 and 3.

## Bug Details

### Bug 1 — VLM in the legacy model-assignment list

The bug manifests when the legacy workflow editor renders its model options. `EditWorkflow.tsx`
fetches the raw feature-config list with `listFeatureConfigurations()` and maps **every** entry into
`modelOptions` with no type filtering:

```ts
const modelOptions = featureConfigurations
  ?.sort(sortWorkflowModelOptions)
  ?.map((config) => ({
    label: getWorkflowModelOptionLabelWithoutVersion(config),
    value: config.modelName,
  })) || [];
```

The one guard that was meant to exclude non-assignable models — `listModels()` in
`FeatureConfigurationAPI.ts` — is a no-op:

```ts
return data.filter(
  (config) =>
    config.type === FeatureConfigurationType.LFVModel || FeatureConfigurationType.TritonModel,
);
```

Operator precedence parses this as `(config.type === LFVModel) || "TritonModel"`. The right operand
is a non-empty (truthy) string constant, so the predicate is always `true` and the filter returns
every element — including `VllmModel`. `VllmModel` is also not a member of the
`FeatureConfigurationType` enum yet.

**Formal Specification:**
```
FUNCTION isBugCondition1(config)
  INPUT: config of type FeatureConfiguration presented in the legacy workflow model list
  OUTPUT: boolean

  RETURN config.type == "VllmModel"
         AND config appears in the legacy-workflow selectable model options
END FUNCTION
```

#### Examples

Given a deployed feature-config listing (name / status / type):
- `cookies-binary` / READY / `TritonModel` → must remain selectable (not a bug).
- `model-cookies-binary` / LOADING / `LFVModel` → must remain selectable (not a bug).
- `opt125m-smoke` / READY / **`VllmModel`** → currently selectable (BUG); must be excluded.
- Edge case: a list containing only `VllmModel` entries → the legacy model options must be empty.

### Bug 2 — Deployed-workflow details shows the UUID as primary identity

`DeployedWorkflowDetails.tsx` renders the UUID as the page title even when a name exists:

```tsx
<Header variant="h1" actions={...}>
  {registration.workflowId}
</Header>
```

The details endpoint (`GET /workflows/registrations/{id}`) already returns a non-null `name` for
named packages (via `registration_to_dict` → `_registration_name`), and the list view already prefers
it — but the details header never references `registration.name`.

**Formal Specification:**
```
FUNCTION isBugCondition2(registration)
  INPUT: registration of type WorkflowRegistrationDetails
  OUTPUT: boolean

  RETURN registration.name is a non-empty string
         AND detailsPrimaryIdentity(registration) == registration.workflowId  // shows UUID, not name
END FUNCTION
```

#### Examples

- A registration with `name == "Cookie Inspector"` → the details page title must show
  "Cookie Inspector", with the UUID retained as an identifier elsewhere on the page.
- Edge case: a registration with `name == null` (a package built before the packager emitted
  `workflowName`) → the title continues to show the UUID (preserved fallback, NOT a bug).

### Bug 3 — Registration details view omits the name

The "Registration details" `ColumnLayout` renders "Workflow" (the UUID) and "Registration ID" (plus
version, arch, status, registered-at) but never the human-readable name:

```tsx
<div>
  <Box variant="awsui-key-label">Workflow</Box>
  <div>{registration.workflowId}</div>
</div>
...
<div>
  <Box variant="awsui-key-label">Registration ID</Box>
  <div>{registration.registrationId}</div>
</div>
```

**Formal Specification:**
```
FUNCTION isBugCondition3(registration)
  INPUT: registration of type WorkflowRegistrationDetails
  OUTPUT: boolean

  RETURN registration.name is a non-empty string
         AND registrationDetailsDisplaysName(registration) == FALSE  // no name field rendered
END FUNCTION
```

#### Examples

- A registration with `name == "Cookie Inspector"` → the registration details must include a "Name"
  field showing "Cookie Inspector", while still showing the "Workflow" (UUID) and "Registration ID"
  fields.
- Edge case: a registration with `name == null` → the name field falls back to the UUID (or a
  neutral "-"), and every other field renders unchanged.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Non-VLM models (`LFVModel`, `TritonModel`) remain selectable in the legacy workflow model list, in
  the same sort order and with the same labels (Requirement 3.1).
- The `/feature-configurations` endpoint and every other consumer of it — the general
  feature-configuration listing and model-status reporting — continue to receive `VllmModel` entries.
  The VLM exclusion is scoped to the legacy-workflow model options only (Requirement 3.2).
- The deployed-workflow details view and (unchanged) list view continue to fall back to the workflow
  UUID when no name is resolvable (Requirement 3.3).
- The deployed-workflows list surface continues to show the human-readable name with a UUID fallback
  exactly as it does today; it is not modified (Requirement 3.4).
- The registration details view continues to display registration ID, version, architecture, status,
  and registered-timestamp unchanged (Requirement 3.5).
- A legacy workflow that already has a non-VLM model assigned continues to load, display, and run that
  model unchanged (Requirement 3.6).

**Scope:**
All inputs that do NOT satisfy a bug condition are unaffected:
- Non-`VllmModel` feature configs (Bug 1).
- Registrations with no resolvable name (Bugs 2, 3).
- Every other view, endpoint field, and interaction on these screens.

## Hypothesized Root Cause

**Bug 1** — Two independent gaps, both confirmed by reading the code:
1. `EditWorkflow.tsx` builds `modelOptions` from `listFeatureConfigurations()` (the raw, unfiltered
   fetch), so no type filtering is applied at the consumer.
2. `listModels()` in `FeatureConfigurationAPI.ts` — the function that was meant to filter — has a
   JavaScript precedence/truthiness bug: `config.type === A || B` where `B` is the truthy string
   constant `"TritonModel"`, so the predicate is always true and nothing is excluded. `VllmModel` is
   additionally absent from the `FeatureConfigurationType` enum.

Root cause: there is no effective type filter between the endpoint (which legitimately includes
`VllmModel`) and the legacy model `<Select>`.

**Bug 2** — Pure display omission. `DeployedWorkflowDetails.tsx` hardcodes `registration.workflowId`
in the page `<Header>` and never references `registration.name`, even though the API contract and
backend already supply a non-null `name` for named packages. The list view already demonstrates the
correct name-then-UUID preference; the details header was simply never updated.

**Bug 3** — Pure display omission in the same component. The "Registration details" `ColumnLayout`
was authored with "Workflow" and "Registration ID" fields but no field bound to `registration.name`.

The backend name-resolution path (`_registration_name` → `registration_to_dict`) and the list surface
were inspected and confirmed correct, so no backend or list-surface change is in scope.

## Correctness Properties

Property 1: Bug Condition — VLM excluded from legacy model options

_For any_ feature-configuration list presented to the legacy workflow editor where the bug condition
holds (`isBugCondition1` returns true — the list contains one or more entries with
`type == "VllmModel"`), the fixed model-option builder SHALL produce selectable options that contain
no `"VllmModel"` entry, while retaining every `"LFVModel"` / `"TritonModel"` entry.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition — Details view shows the name as primary identity

_For any_ deployed-workflow details view where the bug condition holds (`isBugCondition2` returns true
— `registration.name` is a non-empty string), the fixed details view SHALL render that workflow name
as the page's primary identity (the UUID may remain as an identifier field), instead of showing the
UUID as the primary label.

**Validates: Requirements 2.3**

Property 3: Bug Condition — Registration details include the name

_For any_ registration details view where the bug condition holds (`isBugCondition3` returns true —
`registration.name` is a non-empty string), the fixed registration-details section SHALL display that
name alongside the existing identifiers (workflow UUID and registration ID).

**Validates: Requirements 2.4**

Property 4: Preservation — Non-VLM models, other consumers, and UUID fallback unchanged

_For any_ input where the bug conditions do NOT hold (`isBugCondition*` return false — non-`VllmModel`
feature configs; registrations with no resolvable name; other feature-config consumers; other details
fields), the fixed code SHALL produce the same result as the original code: non-VLM models stay
selectable and sorted, the `/feature-configurations` payload is unchanged for other consumers, the
list surface is untouched, the details view and its registration-details section fall back to the
workflow UUID when no name is present, and all other registration fields render identically.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, all changes are in `src/frontend`; no backend change is
required.

**Bug 1 — exclude VLM from the legacy model options**

**File**: `src/frontend/src/components/workflow/types.ts`
1. Add `VllmModel = "VllmModel"` to the `FeatureConfigurationType` enum so the filter can reference a
   typed member and future consumers can discriminate VLM entries. The value must equal the backend
   `VLLM_FEATURE_TYPE` string `"VllmModel"`.

**File**: `src/frontend/src/api/FeatureConfigurationAPI.ts`
2. Fix the no-op filter in `listModels()`. Replace
   `config.type === FeatureConfigurationType.LFVModel || FeatureConfigurationType.TritonModel` with a
   correct predicate that excludes `VllmModel` — either an explicit allow-list
   (`config.type === LFVModel || config.type === TritonModel`) or `config.type !== VllmModel`.
   Consider extracting a small shared helper (e.g. `isAssignableModel(config)`) so the legacy model
   list has a single filter definition reused by `EditWorkflow.tsx`. Leave
   `listFeatureConfigurations()` (the raw fetch) intact for consumers that need VLM entries.

**File**: `src/frontend/src/components/workflow/edit/EditWorkflow.tsx`
3. Filter the fetched feature configurations to exclude `VllmModel` before building `modelOptions`
   (keep `LFVModel` / `TritonModel`), using the shared helper. Either switch the query to
   `listModels()` (now correct) or apply the helper to the `listFeatureConfigurations()` result — in
   both cases the legacy model options must contain no `VllmModel` entry while preserving order and
   labels for the retained types.

**Bug 2 — details view shows the name as primary identity**

**File**: `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx`
4. Change the page `<Header>` title from `{registration.workflowId}` to
   `{registration.name || registration.workflowId}` so a named workflow shows its name and unnamed
   packages still show the UUID.

**Bug 3 — registration details include the name**

**File**: `src/frontend/src/components/deployed-workflow/details/DeployedWorkflowDetails.tsx`
5. Add a "Name" key/label field to the "Registration details" `ColumnLayout`, rendering
   `registration.name || registration.workflowId` (or a neutral fallback), while leaving the existing
   "Workflow" (UUID), "Registration ID", version, arch, status, and registered-at fields unchanged.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate each bug on the unfixed code, then verify
the fix works and preserves existing behavior. Frontend tests use the existing React test tooling
(Jest/RTL under `src/frontend`); property-based tests use the repo's established PBT approach for the
universal preservation guarantees.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing the fix, and confirm
the root-cause analysis.

**Test Plan**:
- Bug 1 (frontend): render the legacy editor (or compute its `modelOptions`) from a feature-config
  list that includes a `VllmModel` entry alongside `LFVModel`/`TritonModel`, and assert the options
  contain no VLM entry. On unfixed code the VLM option is present → FAIL. A companion assertion on
  `listModels()` shows it returns the `VllmModel` entry (proving the no-op filter).
- Bug 2 (frontend): render `DeployedWorkflowDetails` with a registration whose `name` is set and
  assert the name appears as the page title. On unfixed code the header shows only the UUID → FAIL.
- Bug 3 (frontend): render `DeployedWorkflowDetails` with a named registration and assert a "Name"
  field carrying the name appears in the registration details. On unfixed code no name field exists →
  FAIL.

**Test Cases**:
1. **Legacy model VLM exclusion** — options built from a mixed list exclude `VllmModel` (will fail on unfixed code).
2. **Details title shows name** — the details header renders the name when present (will fail on unfixed code).
3. **Registration details name field** — the details section shows a "Name" field with the name (will fail on unfixed code).
4. **Edge case** — a list of only `VllmModel` entries yields empty legacy options (will fail on unfixed code).

**Expected Counterexamples**:
- Bug 1: `opt125m-smoke` (`VllmModel`) appears as a selectable legacy model option.
- Bug 2: the details header renders the workflow UUID while a name is available.
- Bug 3: the registration details section renders only "Workflow" (UUID) and "Registration ID", no name.

### Fix Checking

**Goal**: For all inputs where a bug condition holds, the fixed code produces the expected behavior.

**Pseudocode:**
```
FOR ALL config WHERE isBugCondition1(config) DO
  options := buildLegacyModelOptions_fixed(list containing config)
  ASSERT config not in options AND every non-VllmModel entry retained in options
END FOR

FOR ALL registration WHERE registration.name is non-empty DO
  ASSERT detailsPrimaryIdentity_fixed(registration) == registration.name
  ASSERT registrationDetailsDisplaysName_fixed(registration) == TRUE
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code produces the same
result as the original.

**Pseudocode:**
```
FOR ALL config WHERE NOT isBugCondition1(config) DO
  ASSERT buildLegacyModelOptions_original(config) == buildLegacyModelOptions_fixed(config)
END FOR

FOR ALL featureConfigRequest DO   // endpoint payload for other consumers
  ASSERT featureConfigs_fixed(request) == featureConfigs_original(request)  // VllmModel still present
END FOR

FOR ALL registration WHERE registration.name is empty/absent DO
  ASSERT detailsPrimaryIdentity_fixed(registration) == registration.workflowId
  ASSERT otherDetailsFields_fixed(registration) == otherDetailsFields_original(registration)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation because the guarantees
are universal ("for all non-VLM configs", "for all name-less registrations"): it generates many cases
automatically and catches edge cases manual tests miss. Observe behavior on the UNFIXED code first,
then encode it.

**Test Cases**:
1. **Non-VLM model preservation** — observe that `LFVModel`/`TritonModel` entries are selectable and
   ordered on unfixed code, then property-test that they are unchanged after the fix.
2. **Endpoint payload preservation** — the `/feature-configurations` response (including `VllmModel`
   entries) is unchanged for non-legacy consumers.
3. **Null-name fallback preservation** — a details view for a registration with no resolvable name
   still shows the UUID as the title and in the name field.
4. **Other details fields preservation** — version, arch, status, registered-at, registration id, and
   executions render unchanged after the fix.
5. **List surface untouched** — `ListDeployedWorkflows` continues to render `name || workflowId` with
   the UUID as secondary text (regression guard; no code change).

### Unit Tests

- Legacy model-option builder excludes `VllmModel`, keeps `LFVModel`/`TritonModel`, and handles the
  empty and all-VLM lists.
- `FeatureConfigurationAPI.listModels()` returns only assignable (non-VLM) models, while
  `listFeatureConfigurations()` still returns `VllmModel` entries.
- `DeployedWorkflowDetails` renders the name as the title when present and the UUID when absent.
- `DeployedWorkflowDetails` renders a "Name" field in registration details when a name is present, and
  keeps every existing field when absent.

### Property-Based Tests

- Over randomly generated feature-config lists (each including at least one `VllmModel`): fixed legacy
  options == (original options minus every `VllmModel` entry), with order and labels of retained
  entries preserved.
- Over randomly generated registrations with and without a `name`: the details title and name field
  follow name-then-UUID, and all other fields are byte-for-byte unchanged versus the original render.

### Integration Tests

- Add/edit legacy-workflow flow: the model `<Select>` never offers a VLM, and non-VLM models remain
  selectable and assignable.
- Deployed-workflows list → details navigation: the details page title and registration details show
  the workflow name for a named registration and fall back to the UUID for an unnamed one, with all
  other fields intact.
