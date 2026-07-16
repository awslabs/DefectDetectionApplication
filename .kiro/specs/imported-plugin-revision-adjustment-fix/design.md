# Imported Plugin Revision Adjustment Bugfix Design

## Overview

The Node Designer import flow records an advisory per-platform compatibility check (`platform_compatibility`) on imported plugins. When a source requires a newer GStreamer than a platform's build image provides, the plugin detail page shows a warning with a `suggestedRevision` (e.g. "arm64 JetPack 4 provides 1.14. Import revision 1.14 for this platform instead."). The bug is that this advice is a dead end: per-architecture source revisions (`arch_revisions`) are accepted only at import time on POST /plugins/import, and the detail page's "Retry build" re-submits the same incompatible source tree, which fails again for the same reason. The user's only recourse today is deleting the record and re-importing from scratch.

The fix adds a post-import revision adjustment path:

1. **Backend**: a new `POST /plugins/{id}/versions/{v}/adjust-revision` route on `plugin_importer.py` (body `{architecture, revision}`) that fetches the adjusted revision's source tree into the record's `fetches` map (reusing an already-fetched tree when the revision is already recorded there), maps the platform through `arch_revisions[arch] -> fetches[slug]`, and re-runs the affected platform's build. A fetch failure surfaces on the affected platform's build entry only.
2. **Frontend**: an "Adjust revision" action under the incompatible-platform warning on the plugin detail page, pre-filled with the recorded `suggestedRevision` and editable, wired to the new endpoint.
3. **Infrastructure**: the new API Gateway route on the node-designer API stack, integrated with the existing importer Lambda.

The fix deliberately reuses the existing multi-revision machinery (`fetches` map, `revision_slug`, `arch_source_prefix`, `start_fetch` with `REVISION_SLUG`, `start_queued_builds`) so a post-import adjustment converges on exactly the same record shape an import-time `arch_revisions` produces, and all downstream behavior (build source resolution, revision labels on the detail page) works unchanged.

## Glossary

- **Bug_Condition (C)**: A user holding node-designer:manage wants an imported, settled plugin's specific platform to build from a different source revision (typically the recorded `suggestedRevision`), but no API or UI path exists to apply it — every available action leaves the platform building from the same incompatible tree.
- **Property (P)**: Applying a per-platform revision override fetches (or reuses) the adjusted revision's source tree, records the platform's `arch_revisions` mapping, and re-runs that platform's build from the adjusted tree; a fetch failure surfaces only on the affected platform's entry.
- **Preservation**: The import-time multi-revision flow, compatible platforms' display, plain retries, the single-revision flat `source_s3_prefix` layout, other platforms' builds/artifacts, and once-per-round component auto-packaging must all remain unchanged.
- **Plugin_Record**: The DynamoDB version item (`plugin_records.py` shape) carrying `import_status`, `provenance`, `artifacts` (per-arch build entries), `requested_architectures`, `platform_compatibility`, and — for multi-revision imports — `fetches` and `arch_revisions`.
- **fetches map**: `{slug: {revision, source_prefix, status, fetch_build_id}}` — one entry per distinct fetched source revision, each synced to its own `rev-{slug}/` S3 prefix (`plugin_importer.revision_fetch_plan`).
- **arch_revisions**: `{arch: slug}` — maps a Target_Architecture to the `fetches` entry whose tree its builds read (`plugin_builds.arch_source_prefix` resolves `arch_revisions[arch] -> fetches[slug].source_prefix`, falling back to the flat `source_s3_prefix` for unmapped architectures).
- **suggestedRevision**: The upstream release branch matching a platform's GStreamer minor version, recorded on incompatible `platform_compatibility` entries for official GStreamer modules (`plugin_importer.platform_compatibility`).
- **start_fetch / handle_fetch_result**: The asynchronous CodeBuild fetch step (clone + sync to S3) and the EventBridge result handler that settles it (`plugin_importer.py`).
- **start_queued_builds**: `plugin_builds.py` helper that StartBuilds every requested architecture whose artifact entry is `queued`; never raises to the caller.
- **components_triggered**: The conditional once-per-build-round marker for Plugin_Component auto-packaging; `start_builds` REMOVEs it when opening a new build round.

## Bug Details

### Bug Condition

The bug manifests when an imported plugin's settled record carries an incompatible `platform_compatibility` entry with a `suggestedRevision` and the user wants that platform to build from the suggested (or another) revision. The system offers no operation that changes the platform's effective source revision: the adjust capability simply does not exist post-import — `arch_revisions` is only read from the POST /plugins/import body, `handle_fetch_result` only processes records whose `import_status` is still `fetching`, the build endpoint re-submits whatever `arch_source_prefix` currently resolves, and the detail page renders the warning as plain text.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type (record: Plugin_Record, arch: Target_Architecture,
                        requestedRevision: string)
  OUTPUT: boolean

  RETURN record.kind == 'imported'
         AND record.import_status == 'imported'
         AND record.platform_compatibility[arch].compatible == false
         AND record.platform_compatibility[arch].suggestedRevision != null
         AND requestedRevision != effectiveRevision(record, arch)
         -- and, on unfixed code, no operation exists that changes
         -- effectiveRevision(record, arch): every reachable action
         -- (retry, poll, lifecycle, delete) leaves
         -- arch_source_prefix(record, arch) unchanged
END FUNCTION

WHERE effectiveRevision(record, arch) =
  record.fetches[record.arch_revisions[arch]].revision  when mapped,
  record.provenance.revision                            otherwise
```

### Examples

- **gst-plugins-good on arm64 JetPack 4**: import `main` (requires GStreamer >= 1.24) for all five platforms. The arm64_jp4 build fails; its entry warns "The source requires GStreamer >= 1.24; arm64 JetPack 4 provides 1.14. Import revision 1.14 for this platform instead." Expected: an action to apply revision `1.14` to arm64_jp4 and rebuild. Actual: only "Retry build", which re-submits the same `main` tree and fails identically.
- **Two incompatible platforms**: same import, arm64_jp4 (suggests 1.14) and arm64_jp5 (suggests 1.16) both warn. Expected: adjust each platform independently to its own revision. Actual: no action; the user deletes the record and re-imports with `arch_revisions` set for both.
- **Editable suggestion**: the user knows branch `1.16` also works on JetPack 4 for their plugin subset. Expected: the pre-filled suggestion is editable before applying. Actual: no input exists at all.
- **Edge case — revision already fetched**: a multi-revision import fetched `1.16` for arm64_jp5; the user now adjusts arm64_jp4 to `1.16` too. Expected: the already-synced `rev-1.16/` tree is reused (no second fetch) and arm64_jp4 rebuilds from it immediately.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Import-time `arch_revisions` on POST /plugins/import: `revision_fetch_plan`, one fetch per distinct revision, `arch -> slug` mapping, per-arch builds from their own trees (bugfix 3.1).
- Compatible platforms (or entries without a suggested revision) display exactly as today — their revision, source tree, and build entries are never altered by the fix or by another platform's adjustment (3.2).
- Plain "Retry build" / "Retry failed builds" (POST .../build) re-submits from the platform's currently recorded source tree via `arch_source_prefix` (3.3).
- Single-revision imports without overrides keep the flat `source_s3_prefix` layout for builds, source inspection, and record display; the record's `source_s3_prefix` (the tree the buildability scan, source view, and node-generator read) is never rewritten by an adjustment (3.4).
- Other platforms' build status, artifacts, checksums, signatures, and `logTail` are untouched by one platform's adjustment — on success and on fetch failure alike (3.5).
- Component auto-packaging triggers exactly once per build round, including rounds completed by a revision-adjusted retry (the adjustment opens a new round by REMOVE-ing `components_triggered`, exactly like `start_builds`) (3.6).

**Scope:**
All inputs that do NOT go through the new adjust-revision action are completely unaffected. This includes:
- POST /plugins/import (with or without `arch_revisions`), select-plugins, module listings
- POST .../build (plain retries, prebuilt uploads) and GET .../builds
- EventBridge results for import fetches (`import_status == 'fetching'`) and per-arch builds
- The detail page's rendering of builds, warnings, revision labels, source view, lifecycle and delete actions

## Hypothesized Root Cause

This is a capability gap rather than a defect in an existing computation. The analysis is high-confidence because the absence is structural:

1. **API surface gap**: `arch_revisions` is only parsed from the POST /plugins/import body (`import_repository`). No route mutates `fetches` / `arch_revisions` after import; `plugin_importer.handler` routes only `/plugins/import`, `/plugin-modules`, and `.../select-plugins`.

2. **Fetch-result handler scoped to imports in flight**: `handle_fetch_result` and `_handle_multi_fetch_result` guard on `import_status == 'fetching'` (and per-slug `status == 'fetching'`), so even if a fetch were started for a settled record, its result would be skipped as "already recorded". The handler needs a distinct path for post-import adjustment fetches.

3. **Retry rebuilds the same tree by design**: POST .../build resolves each architecture's source through `arch_source_prefix(item, arch)`, which is pure over the record's current `arch_revisions`/`fetches`/`source_s3_prefix`. Without a record mutation, retrying can only reproduce the failure.

4. **UI renders advice without an action**: `PluginDetail.tsx` shows `platformWarningMessage(arch, compat)` as a `StatusIndicator` line; no control exists, and `nodeDesignerApi` has no method to call even if one did.

The good news shaping the fix: `arch_source_prefix` already resolves per-arch trees with a flat-prefix fallback for unmapped architectures, so a settled single-revision record can gain a `fetches` map + `arch_revisions` entry for just the adjusted platform without disturbing anything else.

## Correctness Properties

Property 1: Bug Condition - Applying a Per-Platform Revision Override Adjusts the Tree and Re-runs the Build

_For any_ imported, settled Plugin_Record with an incompatible platform entry carrying a suggested revision, and any non-empty requested revision for that platform (isBugCondition returns true), the fixed system SHALL offer the adjustment action (pre-filled with the suggested revision, editable), and applying it SHALL: fetch the requested revision's source tree into the record's fetches map — reusing an existing succeeded fetches entry recording the same revision instead of fetching again — record arch_revisions[arch] as the slug of the fetches entry whose revision equals the requested revision, and re-run the affected platform's build so that its source resolves through the adjusted tree (arch_source_prefix returns the adjusted entry's source_prefix); when the fetch fails, the failure SHALL be recorded on the affected platform's build entry only, the platform's prior arch_revisions mapping left unchanged. The action SHALL require node-designer:manage and SHALL be rejected (409) for records that are not imports or whose import status is not 'imported'.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Non-Adjusted Flows Are Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns false) — import-time multi-revision imports, compatible platforms, plain build retries, single-revision flat-layout records, other platforms' build entries during and after another platform's adjustment, and the component auto-packaging trigger — the fixed system SHALL produce the same result as the original system: revision_fetch_plan output, arch_source_prefix resolution for non-adjusted architectures, builds_view content for untouched entries, and the once-per-round components_triggered semantics are all preserved bit-for-bit.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

#### 1. Backend endpoint — `edge-cv-portal/backend/functions/plugin_importer.py`

**New route**: `POST /plugins/{id}/versions/{v}/adjust-revision`, body `{architecture: string, revision: string}`, dispatched from `handler`.

**New handler `adjust_revision(event, user, plugin_id, version)`**:
1. **Validate**: `architecture` must be one of the record's `requested_architectures`; `revision` must be a non-empty string (trimmed). 400 `INVALID_ARCHITECTURE` / `INVALID_REVISION` otherwise.
2. **Authorize**: `authorize_record_access(user, event, item, manage=True, permission=Permission.NODE_DESIGNER_MANAGE)` — same permission as build submission (2.5).
3. **Reject non-adjustable records** (409 `REVISION_ADJUSTMENT_NOT_AVAILABLE`): `kind != 'imported'`, missing `provenance.repoUrl`, or `import_status != 'imported'` (fetching / pending_selection / failed imports have no settled build round to adjust) (2.5).
4. **Resolve the target slug** (new pure helper `adjustment_fetch_slot(item, revision)` so it is unit/property testable):
   - If an existing `fetches[slug]` records the same revision string with status `succeeded` → **reuse**: no new fetch (2.2).
   - If an existing entry records the same revision with status `fetching` (a concurrent adjustment) → join it: add the arch to its `pending_archs`.
   - Otherwise allocate a fresh slug via `revision_slug(revision)`, disambiguating numeric-suffix collisions against existing slugs exactly like `revision_fetch_plan`; the new entry is `{revision, source_prefix: f'{source_s3_prefix(...)}rev-{slug}/', status: 'fetching', pending_archs: [arch]}`. A previously **failed** entry for the same revision is re-fetched in place (status reset, new build id, `pending_archs` set).
5. **Persist + act**:
   - **Reuse path**: set `arch_revisions[arch] = slug` (creating the map if the record is flat), set `artifacts[arch] = {'buildStatus': 'queued'}`, `REMOVE components_triggered` (new build round, 3.6), then call `plugin_builds.start_queued_builds(plugin_id, version)` (lazy import, mirrors `_start_queued_builds`). The queued->building start touches only the adjusted arch because only its entry is `queued`.
   - **Fetch path**: write the `fetches[slug]` entry, set `artifacts[arch] = {'buildStatus': 'queued'}` (the UI shows the platform as queued while the fetch runs), `REMOVE components_triggered`, then `start_fetch(repoUrl, revision, source_prefix, ..., revision_slug_id=slug)` and record its `fetch_build_id` on the entry. `arch_revisions[arch]` is NOT changed yet — it flips only when the fetch succeeds, so a fetch failure leaves the prior mapping intact (2.4, 3.3).
   - The record's `source_s3_prefix`, `default_fetch_slug`, `plugins_found`, `selected_plugins`, and every other architecture's `artifacts` entry are never written (3.4, 3.5).
6. **Audit + respond**: `log_audit_event(action='adjust_plugin_revision', ...)`; answer 202 with `{plugin: import_detail(updated), builds: plugin_builds.builds_view(updated)}` so the page refreshes in one round trip.

**Adjustment fetch results — extend `handle_fetch_result`**: before the existing `import_status == 'fetching'` paths, route fetch results whose `REVISION_SLUG` names a `fetches` entry carrying `pending_archs` (adjustment marker) to a new `_handle_adjustment_fetch_result(item, build_id, build_status, env)`:
- **Idempotency**: per-slug conditional write guarded on `fetches[slug].fetch_build_id == build_id AND fetches[slug].status == 'fetching'`, mirroring `_handle_multi_fetch_result`; superseded/duplicate deliveries are skipped.
- **SUCCEEDED**: set `fetches[slug].status = 'succeeded'` and clear `pending_archs`; set `arch_revisions[a] = slug` for each pending arch; call `plugin_builds.start_queued_builds` to start the queued entries (their source now resolves through the adjusted tree via `arch_source_prefix`) (2.3).
- **FAILED / FAULT / STOPPED / TIMED_OUT**: set `fetches[slug].status = 'failed'` and clear `pending_archs`; for each pending arch set its artifact entry to `{'buildStatus': 'failed', 'logTail': 'The adjusted revision {revision} could not be fetched: the repository is unreachable or the revision does not exist'}` via `set_arch_entry`-style per-arch writes. `arch_revisions` and `import_status` are untouched; no other platform's entry is written (2.4, 3.5).
- Never raises to the EventBridge handler beyond what `handle_build_result` already tolerates; audit-logged as the record's `created_by` (no authenticated user on this path).

#### 2. Infrastructure — `edge-cv-portal/infrastructure/lib/node-designer-api-stack.ts`

Add `addMethod(versionResource.addResource('adjust-revision'), 'POST', importerIntegration);` next to the existing `select-plugins` route (the compiled `.js`/`.d.ts` regenerate on build).

#### 3. Frontend API client — `edge-cv-portal/frontend/src/pages/node-designer/api.ts` (+ `types.ts`)

New method `adjustRevision(pluginId, version, architecture, revision)` → `POST /plugins/{id}/versions/{v}/adjust-revision`, returning `{plugin: PluginVersionDetail, builds: PluginBuildsView}` (new `AdjustRevisionResponse` type).

#### 4. Frontend pure helpers — `edge-cv-portal/frontend/src/pages/node-designer/importFlow.ts`

- `canAdjustRevision(detail, arch)`: true exactly when the detail is an imported record with `import_status === 'imported'` and `platform_compatibility[arch]` is incompatible with a non-null `suggestedRevision` (2.1, mirrors the backend gate).
- `adjustRevisionError(value)`: validation for the input (`null` when trimmed non-empty, message otherwise).
- Existing `platformWarningMessage`, `archRevisionLabel`, `incompatiblePlatformWarnings` are unchanged (3.2); after a successful adjustment `archRevisionLabel` automatically shows the adjusted revision because the record now carries `arch_revisions`/`fetches`.

#### 5. Frontend UI — `edge-cv-portal/frontend/src/pages/node-designer/PluginDetail.tsx`

Under each incompatible-platform warning where `canAdjustRevision` holds, render an inline "Adjust revision for this platform" action that expands to an `Input` pre-filled with `compat.suggestedRevision` (editable) plus Apply/Cancel buttons. Apply calls `nodeDesignerApi.adjustRevision`, replaces `plugin` and `builds` state from the response (the existing poll resumes because the builds view is no longer settled), and disables while a retry or another adjustment is in flight. Per-arch adjustment errors surface in an alert on the affected platform's entry only (2.4). Plain retry buttons and all other page behavior are untouched (3.3).

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples on the UNFIXED code demonstrating the dead end (confirming the root-cause analysis), then verify the fix satisfies Property 1 and preserve-check Property 2. Backend tests live in `edge-cv-portal/backend/tests/test_plugin_importer.py` (pytest + moto/stubs, Hypothesis for properties); frontend tests in `importFlow.test.ts` and `PluginDetail.test.tsx` (vitest, run with `--run`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Exercise the unfixed handlers and components with a settled imported record carrying an incompatible platform entry with a suggested revision, and assert the adjustment paths that should exist do not.

**Test Cases**:
1. **No adjust route**: POST `/plugins/{id}/versions/{v}/adjust-revision` against `plugin_importer.handler` returns 404 NOT_FOUND (will fail on unfixed code once asserted as 202)
2. **Retry re-uses the incompatible tree**: on a flat single-revision record, `arch_source_prefix(item, 'arm64_jp4')` before and after a POST .../build retry is the identical flat prefix — no operation changed the effective revision (will fail on unfixed code when asserted against the adjusted prefix)
3. **UI offers no action**: `PluginDetail` rendered with an incompatible `arm64_jp4` entry (suggestedRevision '1.14') shows the warning text but no adjust control (will fail on unfixed code once the control is expected)
4. **Settled-record fetch results are dropped** (edge): a fetch result for a record whose `import_status` is 'imported' is skipped as "already recorded" by `handle_fetch_result` (confirms root cause 2; may fail on unfixed code once adjustment results are expected to be processed)

**Expected Counterexamples**:
- Every path returns the record to the same `arch_source_prefix` for the affected platform; the adjustment surface is absent end to end
- Possible causes confirmed: missing route, fetch-result guard on `import_status == 'fetching'`, UI renders advice as plain text

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL (record, arch, revision) WHERE isBugCondition(record, arch, revision) DO
  result := adjust_revision_fixed(record, arch, revision)
  ASSERT result.fetches contains a slot whose revision == revision
  IF fetch succeeds (or tree reused) THEN
    ASSERT result.arch_revisions[arch] names that slot
    ASSERT arch_source_prefix(result, arch) == fetches[slot].source_prefix
    ASSERT the arch's build was re-run (queued -> building)
  ELSE
    ASSERT only artifacts[arch] records the fetch failure
    ASSERT result.arch_revisions == record.arch_revisions
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalBehavior(input) = fixedBehavior(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain (record shapes, fetches maps, arch subsets, revision strings)
- It catches edge cases that manual unit tests might miss (slug collisions, records with and without fetches maps, partial arch mappings)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for import-time multi-revision flows, plain retries, and flat-layout resolution, then write property-based tests capturing that behavior and re-run them on the fixed code.

**Test Cases**:
1. **arch_source_prefix preservation**: for randomly generated records (with/without fetches maps and arch_revisions), every architecture NOT adjusted resolves to the same prefix before and after another architecture's adjustment (3.1, 3.4, 3.5)
2. **Untouched-entry preservation**: applying an adjustment to arch A leaves every other architecture's artifact entry (status, s3Key, checksum, signature, logTail) byte-identical (3.5)
3. **Plain retry preservation**: POST .../build without an adjustment produces the same StartBuild source locations and record writes as the original code (3.3)
4. **Import-flow preservation**: `revision_fetch_plan` and the import-time multi-revision persistence are byte-identical to the original (3.1); compatible platform entries and their warnings render unchanged (3.2)

### Unit Tests

- `adjust_revision` handler: happy path with a new revision (fetches entry written with `pending_archs`, arch queued, fetch started with `REVISION_SLUG`, `components_triggered` removed); reuse path (existing succeeded slot: no fetch, `arch_revisions` mapped, build started); failed-slot re-fetch; concurrent-fetch join (arch appended to `pending_archs`)
- Rejections: 403 without node-designer:manage; 409 for scaffolds/generated records, missing repoUrl, and `import_status` in {fetching, pending_selection, failed}; 400 for unknown architecture or empty revision
- `_handle_adjustment_fetch_result`: success maps the arch and starts the queued build; failure records the fetch-failure logTail on the affected arch only and leaves `arch_revisions` unchanged; duplicate/superseded deliveries are idempotent
- `adjustment_fetch_slot`: slug reuse by identical revision, numeric-suffix collision handling, failed-slot reset
- Frontend: `canAdjustRevision` / `adjustRevisionError` truth tables; `PluginDetail` renders the action exactly for incompatible+suggested entries, pre-fills and permits editing, applies via the API, and surfaces per-platform errors

### Property-Based Tests

- **Property 1 (fix)**: Hypothesis-generated settled imported records × architectures × revision strings satisfying the bug condition — after adjustment (with the fetch result simulated succeeded), `arch_source_prefix` for the arch equals the adjusted entry's prefix and its build entry was re-queued; with the fetch simulated failed, only that arch's entry changed
- **Property 2 (preservation)**: Hypothesis-generated records and non-bug-condition operations — non-adjusted architectures' source resolution, artifact entries, and `builds_view` output are identical to the original implementation's; `revision_fetch_plan` behavior at import time is unchanged
- Slug allocation: for any set of existing fetches slugs and any revision, `adjustment_fetch_slot` never clobbers an entry recording a different revision

### Integration Tests

- Full flow: import (flat, single revision) → simulate incompatible platform with suggestedRevision → adjust via the endpoint → simulate fetch SUCCEEDED via `handle_fetch_result` → assert the arch's StartBuild used the `rev-{slug}/` prefix → simulate build SUCCEEDED → assert auto-packaging triggers exactly once for the round (3.6)
- Fetch-failure flow: adjust → simulate fetch FAILED → assert the affected arch shows the fetch-failure logTail, other archs and the record's `source_s3_prefix` untouched, and a plain retry still builds from the prior tree
- Detail-page flow (`PluginDetail.test.tsx`): warning renders with the action → apply → page state reflects the response's builds view and the poll resumes; revision label shows the adjusted revision once mapped
