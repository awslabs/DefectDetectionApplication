# Stale Workflow Registrations Bugfix Design

## Overview

The deployed-workflows view shows every workflow version ever deployed because nothing on either side of the system ever retires a version. The fix has two cooperating halves:

- **Portal side (`edge-cv-portal/backend/functions/workflow_packaging.py::build_recipe`)**: add a `Shutdown` lifecycle step to each platform manifest of the generated `dda.workflow.{workflowId}` recipe that removes `/aws_dda/workflows/{workflowId}/{workflowVersion}`. Greengrass runs the *outgoing* version's Shutdown when a component version is replaced or removed, so staged files stop accumulating for all future packages. The Run step already re-copies artifacts on every component (re)start, so deletion at shutdown is self-healing across reboots and redeployments.
- **Device side (`src/backend/workflow_engine/`)**: make the registration store truthful. The WorkflowWatcher gains two reconciliation rules — registrations whose artifact directory disappeared become status `removed` (this is what the new Shutdown produces), and among version directories still on disk for the same workflow only the highest numeric version stays active while lower ones become `superseded` (this covers already-accumulated stale versions and legacy recipe-less components that will never get a Shutdown). The listing endpoint filters non-active statuses by default. Rows and execution history are never deleted (run artifacts live under `/aws_dda/captures/`, independent of the staged workflow dirs), so history is preserved and reachable via `includeInactive=true` and the per-registration detail route.

Both halves are needed: (a) alone leaves devices lying about the already-accumulated versions and about legacy components whose recipes will never gain a Shutdown; (b) alone leaves stale files accumulating on disk forever.

**Marking vs deleting (decision)**: the device marks superseded registrations but does NOT delete their on-disk directories. Deletion is left to the recipe Shutdown (which Greengrass sequences correctly against installs). Device-initiated deletion was considered and rejected: if the highest-version heuristic is ever wrong (rollback deployment of an older component version under a legacy recipe), deleting the directory would break the actually-deployed workflow until the next component restart, whereas a wrong *marking* self-corrects on the next scan once the state changes. Registration rows and executions are likewise never hard-deleted, preserving run history.

## Glossary

- **Bug_Condition (C)**: a device state in which `/aws_dda/workflows/` holds artifact directories (or the DB holds registrations) for workflow versions that are not the currently-deployed version of their workflow — i.e., a superseded on-disk version directory, or a registration whose directory is gone
- **Property (P)**: the desired behavior — the default registrations listing contains exactly the active registrations (the deployed version of each workflow, `registered` or `invalid`), stale versions carry a non-active status, and future component replacements clean their staged files
- **Preservation**: the deployed version's registration/listing payload, execution history, trigger behavior, and every existing recipe field (Run script, ComponentDependencies, ComponentConfiguration, manifests) apart from the added Shutdown
- **build_recipe**: the function in `edge-cv-portal/backend/functions/workflow_packaging.py` (line ~1539) that emits the Greengrass recipe; today each manifest's Lifecycle has only the one-shot `Run` copy script
- **WorkflowWatcher**: `src/backend/workflow_engine/watcher.py`; startup scan + inotify/poll rescan of `/aws_dda/workflows/`, upserting `WorkflowRegistration` rows via `sync_once()`; `_invalidate_removed` currently marks missing-dir registrations `invalid`
- **WorkflowRegistration**: SQLAlchemy model (`src/backend/workflow_engine/models.py`), one row per `{workflowId}:{version}`, `status` column currently takes `registered` | `invalid`
- **STATUS_REMOVED / STATUS_SUPERSEDED**: new non-active statuses — `removed` (artifact directory gone) and `superseded` (directory present but a higher numeric version of the same workflow is also present)
- **Listing endpoint**: `GET /workflows/registrations` in `src/backend/workflow_engine/api.py::list_workflow_registrations`, today returns every row unconditionally
- **install_dir**: `/aws_dda/workflows/{workflowId}/{workflowVersion}` — note the recipe keys this by *workflow* version, while the component version major can advance past it on re-package (`next_component_version`); a re-package of the same workflow version reuses the same install_dir, so the outgoing component's Shutdown removing it before the incoming Run re-copies it is exactly right

## Bug Details

### Bug Condition

The bug manifests whenever a device has ever had more than one version of a workflow component deployed. Because the recipe has no Shutdown and the watcher has no notion of supersession, every version's directory and registration persists as active.

**Formal Specification:**
```
FUNCTION isBugCondition(deviceState)
  INPUT: deviceState of type (diskDirs: set of (workflowId, version),
                              registrations: set of (workflowId, version, status),
                              deployed: map workflowId -> version)
  OUTPUT: boolean

  RETURN EXISTS (wf, v) IN registrations WHERE
           status(wf, v) = 'registered'
           AND v ≠ deployed[wf]
         // equivalently, on the unfixed system: the same workflowId has
         // more than one version directory on disk, or a registration
         // whose artifact directory is gone still appears in the listing
END FUNCTION
```

On the unfixed system, `deployed[wf]` is not directly observable by the device; the fix approximates it as the highest numeric version directory present (made reliable going forward by the Shutdown cleanup, which guarantees replaced versions' directories disappear).

### Examples

- Verified on JP6: Greengrass deploys only `dda.workflow.1f0b4c0c-...` v7.0.0, yet `/aws_dda/workflows/1f0b4c0c-.../` holds `2/`, `6/`, `7/` and `GET /workflows/registrations` returns v2, v6, v7 all `registered`. Expected: only v7 active; v2 and v6 `superseded`.
- Packaging workflow version 8 and deploying it: today `7/` stays on disk and v7 stays `registered` alongside v8. Expected: outgoing v7 component's Shutdown removes `7/`; watcher marks `wf:7` `removed`; listing shows only v8.
- Removing the workflow component from the deployment entirely: today the last version's files and `registered` row persist. Expected: Shutdown removes the directory; registration becomes `removed`; default listing no longer shows it; its executions remain readable via the detail route.
- Edge case — single version ever deployed: one directory, one `registered` row. Expected: completely unchanged behavior (no supersession, no removal, identical payload).
- Edge case — rollback under fixed recipes: deploying v6 after v7 runs v7's Shutdown (removes `7/`) and v6's Run (re-copies `6/`); highest-on-disk is then 6, so v6 is `registered` and v7 flips to `removed`. Correct.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The currently-deployed version (highest on disk, valid artifacts) lists as `registered` with the exact existing payload keys and values; its executions accumulate and its trigger path is untouched
- Malformed/incompatible artifact sets for the deployed version still register as `invalid` with a reason, still appear in the default listing, and still reject triggers with 409
- The per-registration detail route returns any known registration (any status) with its executions
- Empty/absent `/aws_dda/workflows/` keeps today's byte-identical no-op behavior
- Every recipe field except the added Shutdown: RecipeFormatVersion, ComponentName/Version/Type, Publisher, ComponentConfiguration (WorkflowId/WorkflowVersion), platform manifests and their ordering/attributes (`variant`, `runtime: nvidia`), the one-shot Run script (`mkdir -p … && cp -r …`, Timeout 300, requiresPrivilege), artifact URIs/Unarchive/Permission, the empty top-level Lifecycle, and ComponentDependencies passthrough (dda.plugin.* pinned HARD entries, model component entries, per-arch LocalServer floors). The packaged llm_inference modelName rewrite is untouched (different code path, but locked by existing tests)
- Watcher resilience: scan errors never take LocalServer down; rescans stay idempotent; a directory that reappears (component restart re-copies) flips its registration back to active on the next scan

**Scope:**
All inputs that do NOT involve multiple versions of a workflow or vanished artifact directories are completely unaffected. This includes:
- Devices with exactly one version per workflow, all directories present
- The executions API surface, run artifacts under `/aws_dda/captures/`, and the executor
- Camera-binding resolution, plugin validation, and every other watcher validation rule
- Packaging inputs of any architecture combination (only the Lifecycle gains a key)

**Important side effect to preserve/verify**: existing portal golden-contract tests (`test_workflow_packaging_recipe_preservation.py`, and any fixture pinning the manifest Lifecycle) intentionally assert the exact recipe shape. The Shutdown addition changes that contract; those goldens must be updated to include the Shutdown while continuing to lock every other field.

## Hypothesized Root Cause

Verified (not merely hypothesized) by code reading and live device inspection:

1. **No cleanup lifecycle in the generated recipe**: `build_recipe` emits only `Lifecycle.Run` per manifest. Greengrass runs the outgoing version's Shutdown on replace/remove — but there is none, so `/aws_dda/workflows/{id}/{version}` survives its component version forever.
2. **Watcher registers whatever is on disk, additively**: `WorkflowWatcher.sync_once` upserts a `registered` row per discovered `{workflowId}/{version}` directory with no concept of "deployed vs historical". `_invalidate_removed` only handles vanished directories, and only by marking them `invalid` — which still lists.
3. **Listing returns every row**: `list_workflow_registrations` has no status filter, so the grow-only table surfaces directly in the deployed-workflows view.

## Correctness Properties

Property 1: Bug Condition - Stale Versions Are Retired and Cleaned

_For any_ device state where the bug condition holds (a workflow has superseded version directories on disk, or registrations whose artifact directory is gone), the fixed system SHALL (a) generate recipes whose every platform manifest carries a Shutdown step removing that workflow version's install directory, (b) mark registrations with missing artifact directories `removed` and lower-than-highest numeric on-disk versions `superseded`, (c) return only `registered`/`invalid` registrations from the default `GET /workflows/registrations` listing, and (d) reject triggers against non-active registrations with 409 — while their rows and executions remain in the database and reachable via `includeInactive=true` and the detail route.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Deployed Version and Recipe Contract Unchanged

_For any_ input where the bug condition does NOT hold (a single valid version per workflow on disk, or packaging any workflow), the fixed system SHALL produce the same result as the original: the deployed version's registration payload, status transitions (including invalid-with-reason and flip-back-on-reappearance), execution history, and trigger behavior are byte-equivalent to the unfixed system, and the generated recipe equals the unfixed recipe in every field apart from the added `Shutdown` key in each manifest's Lifecycle.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

**1. Portal — `edge-cv-portal/backend/functions/workflow_packaging.py`**

**Function**: `build_recipe`

Add a `Shutdown` entry beside `Run` in each manifest's Lifecycle:

```python
'Shutdown': {
    'Script': f"rm -rf {install_dir}",
    'Timeout': 60,
    'requiresPrivilege': True
}
```

- Greengrass executes the *outgoing* component version's Shutdown before the incoming version installs/runs, so a re-package of the same workflow version (same install_dir, new component major) is remove-then-recopy in the right order.
- On nucleus stop/restart, Shutdown may run and Run re-copies at start; the watcher already tolerates disappear/reappear cycles (flip to non-active, flip back on rescan). This reasoning — Greengrass running Shutdown for a FINISHED one-shot generic component on replace/remove and Run re-executing on restart — is asserted from Greengrass lifecycle semantics and MUST be confirmed by the user-gated on-hardware task.
- No other recipe field changes. `docstring` updated to document the cleanup contract.

**2. Device — `src/backend/workflow_engine/discovery.py`**

Add status constants (single source of truth, next to `STATUS_REGISTERED`/`STATUS_INVALID`):

```python
STATUS_REMOVED = "removed"        # artifact directory no longer exists
STATUS_SUPERSEDED = "superseded"  # a higher numeric version of the same workflow is on disk
ACTIVE_STATUSES = (STATUS_REGISTERED, STATUS_INVALID)
```

**3. Device — `src/backend/workflow_engine/watcher.py`**

**Function**: `WorkflowWatcher.sync_once` and helpers

1. **Supersession marking**: after scanning, group discovered artifact sets by `workflow_id`. Among versions that parse as integers, only the highest is eligible for `_register` as today; each lower numeric version is upserted with status `superseded` (reason `"superseded by version {highest}"` in the reasons map), skipping artifact validation for them (they are not runnable regardless). Non-numeric version directories (manual tinkering) never supersede and are never superseded — they validate/register exactly as today.
2. **Removal marking**: `_invalidate_removed` becomes `_mark_removed`: any registration row not seen in this scan gets status `removed` (reason `"Artifact directory was removed"`), regardless of prior status, idempotently. Rows already `removed` are skipped (no touched-churn).
3. **Flip-back**: unchanged mechanics — a directory that reappears is in `seen_ids` and goes through `_register`/supersession as normal, so `removed`/`superseded` rows flip back to `registered`/`invalid`/`superseded` per current disk state. `registered_at` refresh semantics stay as today (updated when the row changes).
4. Rows/executions are never deleted.

**4. Device — `src/backend/workflow_engine/api.py`**

**Function**: `list_workflow_registrations`

Add `includeInactive: bool = False` query parameter. Default: filter to `status IN ACTIVE_STATUSES`. With `includeInactive=true`: return all rows (existing ordering preserved). `registration_to_dict` unchanged — it already surfaces `invalidReason` for any non-`registered` status, which now carries the superseded/removed reasons. The detail, trigger (409 guard already keys on `status != STATUS_REGISTERED`), graph, and executions routes are untouched.

**5. No schema migration**: `status` is an unconstrained String column; new values need no alembic change. No frontend change: the default listing keeps returning only statuses the UI already knows (`registered`, `invalid`).

**Deployment note**: the portal half ships with a portal deploy (Lambda). The device half rides the NEXT LocalServer JP6 build (one is already in flight; this fix goes on the following one, alongside the folder-source-image-consumption fix).

## Testing Strategy

### Validation Approach

Two-phase, exploration-first: write property/exploration tests that FAIL on the unfixed code (proving the bug and the root-cause chain), and preservation tests that PASS on the unfixed code (recording the baseline), then implement and re-run both. Known pre-existing failures in the suites are ignored per repo steering.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating the bug BEFORE the fix; confirm the root-cause chain (no Shutdown in recipe; watcher keeps all versions registered; listing returns everything). If refuted, re-hypothesize.

**Test Plan**: Two exploration tests on UNFIXED code:
- Portal (`edge-cv-portal/backend/tests/`): property test over random workflow ids/versions/arch subsets asserting every `build_recipe` manifest Lifecycle contains a Shutdown script that removes the install dir. FAILS on unfixed code (Lifecycle has only Run).
- Device (`test/backend-test/workflow_engine/`): using the `workflow_engine_test_utils` harness (`make_session_factory`, `make_watcher`, `write_artifact_set`), property test over multi-version on-disk layouts asserting only the highest numeric version is active and the default listing excludes stale versions. FAILS on unfixed code (all versions `registered`, all listed).

**Test Cases**:
1. **Recipe Shutdown presence**: for all archs, each manifest carries the cleanup Shutdown (will fail on unfixed code)
2. **Multi-version supersession**: dirs `2/`, `6/`, `7/` for one workflow → only `wf:7` active (will fail on unfixed code — reproduces the live JP6 state)
3. **Removed-dir retirement**: delete a registered version's dir, rescan → status `removed`, excluded from default listing (will fail on unfixed code — status becomes `invalid` and stays listed)
4. **Trigger rejection on stale version** (may fail on unfixed code — stale versions are `registered` and trigger successfully)

**Expected Counterexamples**:
- `build_recipe(...)['Manifests'][i]['Lifecycle']` == `{'Run': …}` with no Shutdown key
- After scanning dirs 2/6/7: three rows all `registered`; listing returns all three
- Possible causes confirmed: missing Shutdown lifecycle, no supersession logic, unfiltered listing

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed system produces the expected behavior.

**Pseudocode:**
```
FOR ALL deviceState WHERE isBugCondition(deviceState) DO
  result := scan_and_list_fixed(deviceState)
  ASSERT active(result) = { highest numeric on-disk version per workflow }
  ASSERT staleOnDisk(result) have status 'superseded'
  ASSERT missingDir(result) have status 'removed'
  ASSERT rows and executions preserved; trigger on non-active → 409
END FOR
FOR ALL (workflowId, version, archs) DO
  recipe := build_recipe_fixed(...)
  ASSERT every manifest Lifecycle.Shutdown removes the install_dir
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed system produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT scan_and_list_original(input) = scan_and_list_fixed(input)
  ASSERT recipe_original(input) = recipe_fixed(input) MODULO added Lifecycle.Shutdown
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis, per existing suite patterns) is used for preservation because it generates many single-version/valid/invalid/empty layouts and arbitrary packaging inputs automatically, catching edge cases manual tests miss.

**Test Plan**: Observe UNFIXED behavior first for non-bug inputs (single-version scans, invalid artifact sets, empty roots, recipe fields), then write property tests capturing it; verify they PASS on unfixed code before implementing.

**Test Cases**:
1. **Single-version registration identity**: for any one-version-per-workflow layout, row fields, statuses, listing payload, and flip-back behavior match the unfixed observations
2. **Recipe equality modulo Shutdown**: for any (id, version, archs, dependencies), the fixed recipe with each manifest's `Shutdown` key deleted equals the unfixed recipe byte-for-byte (Run script, ComponentDependencies passthrough, configuration, ordering all locked)
3. **Invalid-artifact and detail/executions behavior**: malformed sets still register `invalid` with reason; detail route still returns executions; trigger guard behavior unchanged

**Existing golden updates**: `test_workflow_packaging_recipe_preservation.py`'s `expected_recipe_modulo_dependencies` (and any deployment fixture pinning the manifest Lifecycle) must gain the Shutdown entry in its golden contract — updated in the implementation task, with every other assertion untouched.

### Unit Tests

- `build_recipe`: Shutdown present per manifest, correct install_dir, Run/artifacts/dependencies unchanged
- Watcher: supersession grouping (numeric ordering, non-numeric exemption), removed marking (from any prior status, idempotent), flip-back, reasons surfaced
- API: default filter, `includeInactive=true`, detail route for non-active ids, 409 trigger guard for `removed`/`superseded`

### Property-Based Tests

- Random multi-version disk layouts → exactly-one-active-per-workflow invariant (fix check)
- Random single-version layouts and packaging inputs → behavior identical to recorded unfixed baseline (preservation)
- Random remove/re-add sequences → status flips are consistent and rows never disappear

### Integration Tests

- Full watcher-scan → API-listing flow over the JP6-shaped fixture (dirs 2/6/7) in the backend-test harness
- Trigger flow against active vs stale registrations
- On-hardware (user-gated): package with the fixed Lambda, deploy N then N+1 to the JP6 device, verify the old directory is removed by Shutdown, the listing shows only the deployed version, legacy dirs 2/ and 6/ show superseded, and execution history remains visible
