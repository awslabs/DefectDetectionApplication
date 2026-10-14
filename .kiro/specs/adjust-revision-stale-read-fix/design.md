# Adjust-Revision Stale Read Bugfix Design

## Overview

The imported-plugin revision adjustment feature (imported-plugin-revision-adjustment-fix spec) writes a platform's revision mapping (`arch_revisions` / `fetches`) with a DynamoDB `update_item` and then, within the same Lambda invocation, auto-starts the queued builds via `plugin_builds.start_queued_builds`. That auto-start re-reads the record through `plugin_records.get_version_item`, which issues a plain (eventually-consistent) `get_item`. When the read lands on a replica that has not yet applied the write, the returned item lacks the just-written `arch_revisions` mapping, `arch_source_prefix` falls back to the flat `source_s3_prefix` (the original revision's tree), and CodeBuild's `sourceLocationOverride` points at the wrong source. The first post-adjustment build therefore compiles the original tree and fails deterministically (e.g. meson requires `gstreamer-1.0 >= 1.19.0` while the JetPack 4 image ships 1.16.3); a manual retry reads fresh state and succeeds, masking the defect as a transient failure.

The fix is a targeted read-your-own-write correction: add an opt-in `consistent_read` parameter to `plugin_records.get_version_item` (defaulting to `False`, so every existing caller is bit-for-bit unchanged) and have `start_queued_builds` — the single function through which every auto-start flows — request `ConsistentRead=True`. This follows the precedent already in the codebase: `plugin_importer._handle_multi_fetch_result` performs its post-write settlement check with `ConsistentRead=True` for exactly this reason. Both adjustment race sites (the fetch-success result handler and the reuse path) funnel through `start_queued_builds`, so one change fixes both; the import-time fetch-settle path also flows through it and gains the same (strictly safer) guarantee for its own same-invocation read-after-write.

## Glossary

- **Bug_Condition (C)**: A queued per-arch build is auto-started in the same invocation immediately after an `update_item` that changed the record's revision mapping, and the auto-start's eventually-consistent re-read returns the pre-write item — so the build's source prefix resolves without the just-written mapping.
- **Property (P)**: The auto-started build's source prefix reflects the post-write record state: `arch_source_prefix` resolves through the just-written `arch_revisions[arch] -> fetches[slug].source_prefix`, and the CodeBuild `sourceLocationOverride` names the adjusted revision's tree.
- **Preservation**: Every other read path (`get_version_item` callers across `plugin_records.py`, `plugin_builds.py`, `plugin_importer.py`, `plugin_components.py`, `custom_node_types.py`, `workflow_packaging.py`), the auto-start's idempotency/never-raise semantics, manual retries, and flat single-revision source resolution must all remain unchanged.
- **get_version_item**: `plugin_records.py` (~line 262) — fetches one Plugin_Record version item via `plugin_table().get_item(Key=...)`; today it never sets `ConsistentRead`, so every read is eventually consistent.
- **start_queued_builds**: `plugin_builds.py` (~line 655) — re-reads the record via `get_version_item`, StartBuilds every requested architecture whose artifact entry is `queued`, and never raises to the caller. Called from the import-time fetch-settle path and (as `_start_queued_builds`, a lazy-import wrapper) from both adjustment paths.
- **arch_source_prefix**: `plugin_builds.py` (~line 266) — pure resolver `arch_revisions[arch] -> fetches[slug].source_prefix`, falling back to the flat `source_s3_prefix` when the item carries no mapping for the arch. It is correct; the bug is that it can be handed a stale item.
- **Race sites**: `plugin_importer._handle_adjustment_fetch_result` (fetch-success path, ~2040–2145: conditional `update_item` writing `arch_revisions` then `_start_queued_builds`) and `plugin_importer.adjust_revision` ADJUST_REUSE branch (~2340–2445: `update_item` writing `arch_revisions` + re-queue then `_start_queued_builds`).
- **ConsistentRead precedent**: `plugin_importer.py` ~line 1971 — the multi-fetch settlement check already reads with `ConsistentRead=True` so concurrent slug deliveries never all see an unsettled map.

## Bug Details

### Bug Condition

The bug manifests when a build is auto-started immediately after an adjustment write in the same invocation. DynamoDB `GetItem` without `ConsistentRead` may return data that does not reflect a recently completed write; because the write and the read are milliseconds apart in the same invocation, the stale window is hit reliably in practice. The stale item lacks the `arch_revisions[arch]` mapping (or the updated `fetches[slug]` entry), so `arch_source_prefix(item, arch)` falls back to `source_s3_prefix` and the build compiles the original revision's tree.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type (write: RevisionMappingWrite, autoStart: StartQueuedBuildsCall)
  OUTPUT: boolean

  RETURN autoStart follows write within the same invocation
         AND write changed arch_revisions[arch] (adjustment fetch-success
             OR adjustment reuse path)
         AND autoStart.reRead is eventually consistent
         AND autoStart.reRead returns the pre-write item
         -- consequence on unfixed code:
         --   arch_source_prefix(staleItem, arch) == source_s3_prefix
         --   != fetches[arch_revisions[arch]].source_prefix
END FUNCTION
```

### Examples

- **Adjustment fetch success**: gst-plugins-good arm64_jp5 is adjusted from `main` to `1.16`. The fetch succeeds, `_handle_adjustment_fetch_result` writes `arch_revisions.arm64_jp5 = '1-16'` and calls `_start_queued_builds`. Expected: the build's `sourceLocationOverride` names `.../rev-1-16/`. Actual: the re-read returns the pre-write item, the prefix falls back to the flat `main` tree, and the build fails on `gstreamer-1.0 >= 1.19.0` vs 1.16.3.
- **Reuse path**: arm64_jp4 is adjusted to `1.16`, whose tree an earlier adjustment already fetched. `adjust_revision` writes `arch_revisions.arm64_jp4 = '1-16'` + re-queue and immediately auto-starts. Expected: build from `rev-1-16/`. Actual: stale re-read, flat-prefix fallback, deterministic first-build failure.
- **Manual retry masks it**: the user clicks "Retry build" seconds later; the read now reflects the write, `arch_source_prefix` resolves the adjusted tree, and the build succeeds — the defect looks like a flaky build (requirement 1.3).
- **Edge case — import-time auto-start**: the original import flow's fetch-settle path writes `import_status`/artifacts then calls `start_queued_builds` in the same invocation. The same theoretical stale window exists (a stale item could show entries not yet `queued`, silently starting nothing); a consistent read there is strictly safer and behavior-preserving.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Architectures whose mapping was not changed by an adjustment resolve their source exactly as today: per-arch mapping if present, otherwise the flat `source_s3_prefix` (3.1); single-revision flat-layout records keep building from `source_s3_prefix` (3.2).
- `start_queued_builds` semantics beyond read consistency: already-started architectures are left alone, unconfigured architectures stay queued, StartBuild failures are recorded without raising to the caller, and the import-time fetch-settle auto-start keeps working (3.3).
- Manual retry (POST .../build) re-submits from the platform's currently recorded source tree (3.4).
- Every other `get_version_item` caller — detail/listing/source-inspection handlers in `plugin_records.py`, build views and build-result handling in `plugin_builds.py`, importer handlers, component packaging, custom node types, workflow packaging — keeps issuing eventually-consistent reads and behaves as today (3.5).
- A failed adjustment fetch still surfaces on the affected platform's entry only, without starting builds or touching other platforms (3.6).

**Scope:**
All reads that do NOT flow through `start_queued_builds` are completely unaffected by this fix. This includes:
- Every `get_version_item` call that does not pass the new parameter (it defaults to `False` — identical `get_item` call, no `ConsistentRead` key)
- All query/list paths (`query_versions`, GSI queries), which are unrelated to `get_item` consistency
- All write paths (`update_item`, `set_arch_entry`, conditional writes), which are untouched

## Hypothesized Root Cause

This root cause is confirmed by code inspection rather than hypothesized from symptoms alone:

1. **Eventually-consistent re-read after a same-invocation write (confirmed primary cause)**: `plugin_records.get_version_item` issues `plugin_table().get_item(Key=...)` with no `ConsistentRead`. `start_queued_builds` calls it immediately after the adjustment `update_item` in the same invocation, well inside DynamoDB's eventual-consistency window.

2. **Silent flat-prefix fallback amplifies the staleness (contributing, by design)**: `arch_source_prefix` deliberately falls back to `source_s3_prefix` for unmapped architectures (single-revision records rely on this). Handed a stale item, the fallback produces a *valid-looking* wrong prefix instead of an error, so the submission proceeds and the failure surfaces only inside the CodeBuild log.

3. **Both adjustment paths share the race shape**: `_handle_adjustment_fetch_result` (write mapping → `_start_queued_builds`) and `adjust_revision` ADJUST_REUSE (write mapping + re-queue → `_start_queued_builds`) both write then immediately re-read through the same helper — which is also why one fix at the re-read site covers both.

4. **Not a write problem**: the `update_item`s are correct and (on the fetch-result path) conditional; the manual-retry success proves the written state is right. Only the read timing is wrong.

## Correctness Properties

Property 1: Bug Condition - Auto-Started Builds Resolve the Post-Write Source Tree

_For any_ build auto-started via start_queued_builds immediately after a same-invocation write that changed the record's revision mapping — whether from the adjustment fetch-success handler or the adjustment reuse path (isBugCondition returns true) — the fixed system SHALL re-read the record with a strongly consistent read, so the item handed to arch_source_prefix carries the just-written arch_revisions mapping and the CodeBuild sourceLocationOverride names the adjusted revision's source prefix (fetches[arch_revisions[arch]].source_prefix), regardless of eventual-consistency timing.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - All Other Reads and Auto-Start Semantics Are Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns false) — get_version_item calls from every other caller (which do not pass consistent_read and therefore issue the identical eventually-consistent get_item as before), source resolution for architectures not touched by an adjustment, flat single-revision records, manual retries, the import-time auto-start's idempotency and never-raise guarantees, and fetch-failure handling — the fixed system SHALL produce the same result as the original system, preserving all existing read behavior and build flows bit-for-bit.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Chosen Approach and Rationale

**Option 1 — opt-in strong consistency on the re-read (chosen)**: add `consistent_read: bool = False` to `plugin_records.get_version_item`; `plugin_builds.start_queued_builds` passes `consistent_read=True`.

- Minimal: two files, a parameter and one call-site flag; no data-flow or signature changes anywhere else.
- Matches the existing precedent (`_handle_multi_fetch_result`'s `ConsistentRead=True` settlement check at plugin_importer.py ~1971).
- Fixes both race sites at once, because both adjustment paths flow through `start_queued_builds`; the import-time fetch-settle path (which also writes then auto-starts in one invocation) is covered by the same change, closing its identical theoretical race with no behavior change.
- Default `False` guarantees every other caller's `get_item` call is byte-identical (the `ConsistentRead` key is only added when requested), making preservation trivially auditable.

**Option 2 — pass the known post-write item into start_queued_builds (rejected)**: threading the post-write state through `_start_queued_builds` and its callers is more invasive (call-site signature changes in `plugin_importer.py` and `plugin_builds.py`), risks drift between the passed snapshot and table state (the write is an `update_item`, so callers would have to reconstruct the merged item), and the import-time path would need the same reconstruction treatment. The cost of a consistent `GetItem` on this rare, non-latency-sensitive path is negligible by comparison.

### Changes Required

**File**: `edge-cv-portal/backend/functions/plugin_records.py`

**Function**: `get_version_item` (~line 262)

1. **Add opt-in consistency parameter**: signature becomes `get_version_item(plugin_id: str, version: int, consistent_read: bool = False)`. When `consistent_read` is true, pass `ConsistentRead=True` to `plugin_table().get_item(...)`; when false, issue exactly today's call (do not pass the key at all, so stubs and moto behavior for existing tests are unchanged).
2. **Document the intent**: docstring notes the parameter exists for same-invocation read-your-own-write callers (auto-start after an adjustment/fetch-settle write), mirroring the `_handle_multi_fetch_result` precedent.

**File**: `edge-cv-portal/backend/functions/plugin_builds.py`

**Function**: `start_queued_builds` (~line 673)

3. **Request the consistent re-read**: `item = get_version_item(plugin_id, version, consistent_read=True)`. Everything else in the function (queued-arch selection, `submit_arch_builds`, `set_arch_entry` persistence, never-raise wrapper, audit logging) is untouched, preserving 3.3.

**No other changes**: `arch_source_prefix`, `submit_arch_builds`, both adjustment paths in `plugin_importer.py`, and every other `get_version_item` call site are not modified. The fix is entirely in how the auto-start obtains its item.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples on the UNFIXED code demonstrating the stale-read → wrong-source submission (confirming the root cause), then verify the fix satisfies Property 1 and preserve-check Property 2. Tests live in `edge-cv-portal/backend/tests/` (pytest + Hypothesis; venv at `/home/ubuntu/backend-test-venv`), reusing `test_plugin_importer.py`'s existing stub/moto patterns for the plugin table and CodeBuild. The stale read is deterministic to simulate: stub the table's `get_item` to return the pre-write item for plain (non-`ConsistentRead`) reads and the post-write item when `ConsistentRead=True` is passed. Property tests run ≥100 iterations and are tagged `Feature: adjust-revision-stale-read-fix, Property {n}: {title}`.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Drive each adjustment path against a stubbed plugin table whose `get_item` returns the stale (pre-write) item unless `ConsistentRead=True` is requested, with CodeBuild's `start_build` captured. Assert the submitted `sourceLocationOverride` names the adjusted revision's `rev-{slug}/` prefix. On unfixed code these assertions fail with the flat prefix, reproducing the field failure exactly.

**Test Cases**:
1. **Fetch-success stale read**: settle an adjustment fetch via `_handle_adjustment_fetch_result` with the stale-read stub; assert the auto-started build's source names the adjusted `rev-{slug}/` prefix (will fail on unfixed code — flat prefix submitted)
2. **Reuse-path stale read**: `adjust_revision` ADJUST_REUSE with the stale-read stub; assert the auto-started build's source names the reused entry's prefix (will fail on unfixed code)
3. **No ConsistentRead requested**: assert `start_queued_builds`'s re-read passes `ConsistentRead=True` to `get_item` (will fail on unfixed code — the parameter is absent)
4. **Retry succeeds after first failure** (edge, confirms 1.3): after the stale-read submission, a manual retry with fresh reads resolves the adjusted prefix (passes on unfixed code — documents the masking behavior)

**Expected Counterexamples**:
- `start_build` called with `sourceLocationOverride = {bucket}/{source_s3_prefix}` instead of `{bucket}/{fetches[slug].source_prefix}` on both adjustment paths
- Possible causes confirmed: eventually-consistent `get_item` in `get_version_item`, silent flat-prefix fallback in `arch_source_prefix`, same-invocation write-then-read in both paths

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL (record, arch, slug) WHERE isBugCondition(write(record, arch, slug),
                                                  autoStart) DO
  submissions := start_queued_builds_fixed(plugin_id, version)
    -- with get_item stubbed stale-unless-ConsistentRead
  ASSERT reRead used ConsistentRead=True
  ASSERT submissions[arch].sourceLocationOverride
         ends with fetches[slug].source_prefix
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
- It generates many test cases automatically across the input domain (record shapes with/without `arch_revisions`/`fetches`, arch subsets, artifact statuses)
- It catches edge cases that manual unit tests might miss (partially mapped records, unconfigured architectures, missing items)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for non-adjustment reads and auto-start flows, then write property-based tests capturing that behavior and re-run them on the fixed code.

**Test Cases**:
1. **Default-read preservation**: `get_version_item(plugin_id, version)` without the parameter issues a `get_item` call with no `ConsistentRead` key and returns the same decoded item as before the fix (3.5)
2. **Auto-start semantics preservation**: for randomly generated records, `start_queued_builds` starts exactly the queued+configured architectures, leaves non-queued and unconfigured entries alone, records StartBuild failures without raising, and returns `{}` for missing records — identical to unfixed behavior apart from the read consistency (3.3)
3. **Source-resolution preservation**: for randomly generated items, `arch_source_prefix(item, arch)` is byte-identical before and after the fix for every arch — mapped, unmapped, and flat (3.1, 3.2)
4. **Retry and failure-path preservation**: manual retry submissions and adjustment fetch-failure handling (per-arch logTail, no builds started, other platforms untouched) are unchanged (3.4, 3.6)

### Unit Tests

- `get_version_item`: passes `ConsistentRead=True` exactly when `consistent_read=True`; omits the key entirely by default; returns `None` for missing items in both modes
- `start_queued_builds`: re-reads with `consistent_read=True`; with the stale-read stub, submits the adjusted prefix; idempotency cases (already-building arch untouched, unconfigured arch left queued, StartBuild exception recorded as failed entry, missing record returns `{}`)
- Both adjustment paths end-to-end against the stale-read stub: fetch-success settles then builds from `rev-{slug}/`; ADJUST_REUSE maps then builds from the reused prefix

### Property-Based Tests

- **Property 1 (fix)**: Hypothesis-generated records × adjusted archs × slugs under the stale-unless-consistent stub — the auto-started submission's source always reflects the post-write mapping (≥100 iterations, tagged `Feature: adjust-revision-stale-read-fix, Property 1: Auto-Started Builds Resolve the Post-Write Source Tree`)
- **Property 2 (preservation)**: Hypothesis-generated records and non-bug-condition operations — default `get_version_item` call shape, `arch_source_prefix` resolution, and `start_queued_builds` outcomes are identical to the original implementation's (≥100 iterations, tagged `Feature: adjust-revision-stale-read-fix, Property 2: All Other Reads and Auto-Start Semantics Are Unchanged`)

### Integration Tests

- Full adjustment-fetch flow: adjust → simulate fetch SUCCEEDED via `handle_fetch_result` with the stale-read table stub → assert the arch's StartBuild used the adjusted `rev-{slug}/` prefix and the arch entry advanced queued → building
- Full reuse flow: adjust to an already-fetched revision → assert the immediate auto-start used the reused entry's prefix
- Import-time flow: original import fetch-settle → `start_queued_builds` still starts all queued architectures from the flat prefix with unchanged idempotency (3.2, 3.3)
