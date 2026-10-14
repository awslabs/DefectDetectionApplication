# Workflow Deploy Component Version Bugfix Design

## Overview

`create_workflow_deployment` (edge-cv-portal/backend/functions/deployments.py ~line 3127) pins the deployed workflow component entry at `workflow_component_version(workflow_version)` = `{workflow_version}.0.0`. But re-packaging a workflow version deliberately bumps the component MAJOR past the workflow version (`workflow_packaging.py::next_component_version` returns `max(workflow_version, highest_major + 1)`, because Greengrass component versions are immutable and patch/minor bumps can leave stale artifacts on-device). Deploying a re-packaged version through the portal therefore pins the OLD component version: Greengrass sees no component change, delivers nothing, and reports COMPLETED while the fixed component never reaches the device. The association record, audit entry, and 201 response all misreport what was deployed.

Confirmed live on ryan-orin-nano (2026-08-06): workflow `modbus_test` (`e830f55d-5744-4edf-be43-1a33fbd4605d`) v1 was re-packaged to component `2.0.0` after `1.0.0` shipped a broken recipe; portal deploy `72c2f784` (revision 14) pinned `dda.workflow.e830f55d...` at `1.0.0`; the device reported COMPLETED, artifacts still missing, workflow unregistered.

The related structural facet (deferred as facet 1.3 of the workflow-deploy-subscribe-merge spec): three consumers derive a workflow version by parsing the MAJOR out of a component entry's `componentVersion` and calling `workflow_guards.get_version_item(workflow_id, major)` — `collect_workflow_subscribed_topics` (subscribe grant merge, ~2130), the vLLM gate manifest builder `collect_vllm_component_manifests` (~1994), and `_deployed_workflow_binding_keys` (camera-binding shadow prune keys, ~3044). Once an entry carries a bumped major (from the corrected deploy path, the generic path, or carried over from an existing deployment), the major no longer equals a workflow version: the lookup silently resolves nothing (or the wrong item), topics go uncollected (on-device `SubscribeToIoTCore` denial), binding keys are misderived (valid keys can be pruned), and LLM-bearing workflows can evade the architecture gate. Live example: `bedrock_test` (`f81a4c66-...`) workflow v5 runs component `6.0.0`; a fresh grant-merge would look up version item 6 (nonexistent) and collect nothing.

The fix has three legs, all built on the same authoritative fact: the packaging side already knows the true component version, and records it in the `component_arn` it writes on the version item (suffix `:versions:{component_version}`).

1. **Forward resolution (2.1, 2.2)**: `create_workflow_deployment` resolves the actual registered component version from the version item it already loads and gates on — new helper `resolve_workflow_component_version(version_item, workflow_version)` — and threads it through the component entry, the association record, the audit entry, the vLLM gate manifest, and the 201 response.
2. **Reverse resolution (2.3, 2.4)**: the three major-parse consumers resolve `componentVersion → workflow version item` by scanning the workflow's version items for a matching recorded component version (new `workflow_guards` helper; version items per workflow are few, so the scan is bounded), falling back to today's major-parse when nothing matches (pre-change items, test fakes).
3. **Packaging (2.5)**: `workflow_packaging.py` records `component_version` as a discrete field on the version item in the same `update_item` that records `component_arn`, so the mapping is directly available going forward (arn parsing remains the fallback for versions packaged before this change).

For first packages the `component_arn` suffix equals `{workflow_version}.0.0`, so resolution and today's derivation produce the identical string — Requirement 3.1's byte-identity holds by construction, not by special-casing. The generic `create_deployment` path is untouched.

## Glossary

- **Bug_Condition (C)**: A workflow deployment (or deployment component set) involving a workflow version whose registered component version's major exceeds the workflow version — i.e. the version was re-packaged, or the entry's `componentVersion` major was bumped past any workflow version.
- **Property (P)**: The deployed entry, records, and response carry the ACTUAL registered component version; the major-parse consumers resolve the correct workflow version item for bumped-major entries.
- **Preservation**: First-package deploys (component version == `{workflow_version}.0.0`) submit byte-identical deployments; the generic `create_deployment` path, revision semantics, all pre-submit gates, the subscribe-merge behavior, and the packaging version-numbering scheme are unchanged.
- **`create_workflow_deployment`**: The portal workflow deploy path (deployments.py ~3127). Loads and gates on the version item (`component_arn` required), merges the target's existing component set with the workflow entry, submits via the Use_Case-account greengrassv2 client.
- **`workflow_component_version(workflow_version)`**: deployments.py ~2125 — returns `{workflow_version}.0.0`. The buggy pin; retained as the last-resort fallback only.
- **`next_component_version`** / **`component_version_for`**: workflow_packaging.py — first package of workflow vN is `N.0.0`; each re-package strictly increases the major (`max(workflow_version, highest_major + 1)`). Consequence used throughout this design: **at most one version item of a workflow can record a given component version** (majors never repeat), so matching by recorded component version is unambiguous.
- **`component_arn`**: Recorded on the version item by `workflow_packaging.py`'s success bookkeeping (`SET component_arn = :arn, ...`). Ends `:versions:{component_version}` (e.g. `...:components:dda.workflow.{id}:versions:2.0.0`) — the authoritative component version is already persisted here for every packaged version, past and future.
- **`component_version` (version-item field)**: NEW discrete field this fix records at packaging time (2.5), preferred over arn parsing when present.
- **Major-parse consumers**: `collect_workflow_subscribed_topics`, `collect_vllm_component_manifests` (workflow branch), `_deployed_workflow_binding_keys` — the three places that today do `int(componentVersion.split('.')[0])` → `get_version_item`.
- **`workflow_guards.get_version_item(workflow_id, version)`**: GetItem on the WorkflowVersions table. The property suite `test_property_subscribed_topics.py` swaps this module attribute for a fake — the reverse-resolution fallback must keep routing through it.
- **WorkflowDeployEnv harness**: The endpoint-level `create_workflow_deployment` harness from `tests/test_workflow_deploy_subscribe_merge_exploration.py` — FakeGreengrass/FakeIot as the Use_Case-account clients, real workflow metadata + version items (seeded WITH `component_arn` ending `:versions:{v}.0.0`) in the moto-backed tables.

## Bug Details

### Bug Condition

The bug manifests whenever the component version of record diverges from `{workflow_version}.0.0` — on the deploy path (the pin is wrong) and in the major-parse consumers (the reverse lookup is wrong). The deploy path facet is the delivery-killer: the pinned old version means Greengrass has nothing to change.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type DeploymentPathEvaluation
         {kind: "workflow_deploy" | "components_map_consumer" | "packaging",
          workflowId, workflowVersion?, versionItem?, componentsMap?}
  OUTPUT: boolean

  -- registered(v_item): the component version the packager registered,
  -- as recorded on the version item (component_version field, or the
  -- component_arn suffix after ":versions:")

  IF X.kind = "workflow_deploy" THEN
    -- facets 1.1 / 1.2: the pin and the bookkeeping
    RETURN registered(X.versionItem) ≠ workflowVersion.0.0
           AND submittedEntry(X).componentVersion = workflowVersion.0.0
  ELSE IF X.kind = "components_map_consumer" THEN
    -- facets 1.3 / 1.4: subscribe merge, binding keys, vLLM gate
    RETURN ∃ entry ∈ workflowEntries(X.componentsMap):
             major(entry.componentVersion) resolves no version item
               whose registered component version = entry.componentVersion
             AND ∃ v_item of X.workflowId:
               registered(v_item) = entry.componentVersion
  ELSE  -- "packaging", facet 1.5
    RETURN versionItemAfterPackaging records component_arn
           AND NOT component_version
  END IF
END FUNCTION
```

### Examples

- **Live incident (1.1/1.2)**: `modbus_test` v1, version item `component_arn` ends `:versions:2.0.0`. Portal deploy `72c2f784` submitted `dda.workflow.e830f55d...` at `componentVersion: "1.0.0"`; the target already ran 1.0.0, so Greengrass delivered nothing and reported COMPLETED. The association record, audit entry, and 201 body all said `component_version: "1.0.0"` — which is simultaneously what was submitted and not what the user asked to deploy. Expected: entry, record, audit, and response all carry `2.0.0`, and the device receives the fixed component.
- **Carried-over entry (1.3)**: `bedrock_test` workflow v5 runs component `6.0.0` (its v5 version item's arn ends `:versions:6.0.0`). A deployment revision carrying that entry calls `collect_workflow_subscribed_topics` → `get_version_item(f81a4c66..., 6)` → None (latest_version is 5) → no topics collected → the `dda:workflow-subscribe:` grant is dropped from a fresh merge. Its grant currently survives only because `apply_subscribe_access_control` preserves pre-existing merge keys on carry-over. Expected: the entry resolves workflow v5's item and its topics.
- **Binding-key misderivation (1.4)**: an entry at `2.0.0` for a workflow deployed at v1 makes `_deployed_workflow_binding_keys` derive `{workflowId}/2` — so the live key `{workflowId}/1` is absent from the survive-set and `deliver_camera_bindings` prunes it from the device shadow. Expected: the survive-set contains `{workflowId}/1`.
- **vLLM gate evasion (1.4)**: the generic path's `collect_vllm_component_manifests` looks up version item 2 for the `2.0.0` entry, finds nothing (or an item without `has_llm_inference`), and an LLM-bearing workflow v1 sails past the architecture gate onto a jp4 device. Expected: the entry resolves v1's item, and the gate activates.
- **Non-bug (preservation)**: first package of workflow v1 — arn suffix is `1.0.0` = `{workflow_version}.0.0`. Resolution returns the same string the current code derives; the submitted deployment, record, audit, and response are byte-identical (3.1).

## Expected Behavior

### Fix Semantics — the two resolution decisions

**D1 — Forward resolution (deploy path).** New helper in deployments.py next to `workflow_component_version`:

```
FUNCTION resolve_workflow_component_version(version_item, workflow_version)
  IF version_item.component_version is a non-empty string THEN
    RETURN version_item.component_version              -- recorded by 2.5
  arn_suffix := text after the last ":versions:" in version_item.component_arn
  IF arn_suffix matches ^\d+\.\d+\.\d+$ THEN
    RETURN arn_suffix                                  -- every packaged version, past and future
  RETURN workflow_component_version(workflow_version)  -- {v}.0.0 last resort
END FUNCTION
```

Justification: the version item is already loaded and already gated on (`WORKFLOW_NOT_PACKAGED` fires when `component_arn` is absent), so resolution costs no extra I/O and the arn branch covers every version ever packaged. The discrete field is preferred once 2.5 records it — it is the declared contract rather than a parse. The `{v}.0.0` fallback is nearly unreachable (a present-but-unparseable arn) but keeps the function total. For first packages `arn_suffix == {workflow_version}.0.0`, so behavior is identical either way — 3.1's byte-identity is a corollary, not a special case.

**D2 — Reverse resolution (major-parse consumers).** New helper in workflow_guards.py (it owns WorkflowVersions access):

```
FUNCTION find_version_item_by_component_version(workflow_id, component_version)
  items := Query WorkflowVersions partition workflow_id     -- bounded: few items per workflow
  RETURN the item whose component_version field OR component_arn
         ":versions:" suffix equals component_version, or None
END FUNCTION
```

and a small resolver in deployments.py used by all three consumers:

```
FUNCTION _resolve_workflow_version_item(workflow_id, entry_component_version)
  -- scan-first: authoritative match wins
  item := workflow_guards.find_version_item_by_component_version(
              workflow_id, entry_component_version)
  IF item THEN RETURN (item.version, item)
  -- fallback: today's major parse, for items packaged before 2.5 whose
  -- entry predates re-packaging, and for test fakes of get_version_item
  major := int(entry_component_version.split('.')[0])
  RETURN (major, workflow_guards.get_version_item(workflow_id, major))
END FUNCTION
```

Why scan-first rather than "major-parse, verify, then scan": matching by recorded component version is unambiguous (majors strictly increase per `next_component_version`, so at most one item matches), whereas the verify-then-scan variant has a wrong-item hazard — `get_version_item(workflow_id, 2)` can return a real workflow-v2 item that records no packaging fields (drafted, never packaged), which cannot be disconfirmed even though the `2.0.0` entry actually belongs to re-packaged v1. Scan-first resolves the carried-over `bedrock_test` case (entry `6.0.0` → v5's item, whose arn records `6.0.0`) with no ambiguity. Cost: one bounded Query per workflow entry per submission — acceptable on a deploy-time Lambda path.

The fallback preserves two populations exactly: (a) pre-change deployments of never-re-packaged versions, where the scan also succeeds and agrees with the major parse; (b) `test_property_subscribed_topics.py`, which swaps `deployments.workflow_guards.get_version_item` for a fake and generates version items without `component_arn` — there the scan finds nothing in the (empty) real table and the faked major-parse path produces today's results verbatim.

Each consumer keeps its current resilience shape (the `major.isdigit()` gate on the fallback, the try/except-with-warning around table reads):

- `collect_workflow_subscribed_topics`: resolve the item via `_resolve_workflow_version_item`; topics logic unchanged.
- `collect_vllm_component_manifests` (workflow branch): resolve the item the same way; `has_llm_inference` / `packaged_architectures` logic unchanged.
- `_deployed_workflow_binding_keys`: use the resolved workflow version for `_workflow_binding_key(wf_id, version)`. Note this helper becomes table-reading (today it is pure over the map); it is only called from `create_workflow_deployment`, which already holds table access, and a read failure falls back to the major parse rather than raising.

**D3 — Packaging record (2.5).** In `workflow_packaging.py`'s success bookkeeping, extend the SAME `update_item` that records `component_arn`:

```
update_expression: ... SET component_arn = :arn, component_version = :cv, ...
update_values:     ':cv': resolved_component_version
```

Additive field on an item that already changes at packaging time; the version-numbering scheme (`component_version_for`, `next_component_version`) is untouched (3.7).

**D4 — Threading on the deploy path.** `create_workflow_deployment` computes `component_version = resolve_workflow_component_version(version_item, workflow_version)` once (replacing the `workflow_component_version(workflow_version)` call at ~3350) and threads it to: the components-map entry, the vLLM gate manifest's `version` label (the architectures already come from the authoritative item on this path), `record_workflow_deployment` (new keyword `component_version=None`, defaulting to the old derivation so the signature stays safe), `audit_details`, and `response_body`. The stale comment above the `apply_subscribe_access_control` call ("The deployed entry's componentVersion is {workflow_version}.0.0, so the helper's major-parse resolves the authoritative version item") is rewritten: the subscribe-merge design's "satisfying Requirement 2.3 by construction" note is obsolete — this fix makes the resolution authoritative **by resolution** (D2), which also covers the generic path and carried-over entries that "by construction" never did.

### Preservation Requirements

**Unchanged Behaviors:**

- First-package deploys (registered version == `{workflow_version}.0.0`): byte-identical submitted components map, association record, audit entry, and response body (3.1 — holds because arn resolution returns the same string).
- The generic `create_deployment` path honors caller-supplied component versions and is not edited (3.2). Its only behavioral delta is D2 inside the shared consumers, which is invisible when entry majors match version items (all existing tests) and strictly corrective otherwise.
- Revision semantics: existing components preserved, older workflow entry replaced, deployment name reused, record/audit shapes unchanged — only the componentVersion VALUE is corrected (3.3).
- All pre-submit gates keep their error envelopes and ordering (3.4).
- Non-subscribing deployments: byte-identical component set (modulo only the corrected componentVersion) and no `warnings` field (3.5); `apply_subscribe_access_control`'s merge semantics — non-destructive upsert, unparseable-merge safety, LocalServer-absent warnings — untouched (3.6).
- Packaging version numbering: first package `{v}.0.0`, re-package bumps to the next free major (3.7).
- The uncommitted working-tree fixes in `deployments.py` / `workflow_packaging.py` (subscribe-merge on the workflow path, run-script cleanup) keep their verified behaviors — this fix layers on the working tree, not HEAD (3.8).

**Scope:** All inputs NOT involving a re-packaged workflow version (or a bumped-major component entry) are unaffected. This includes every existing test scenario: the WorkflowDeployEnv suites seed version items with `component_arn` ending `:versions:{v}.0.0` and pin entries at `{v}.0.0` (scan matches → identical outcomes); the FleetEnv integration suite packages through the real pipeline (arn recorded) and only ever deploys first packages; `test_deployment_vllm_gate.py` seeds arns at `:versions:1.0.0` for v1; `test_property_subscribed_topics.py` rides the fallback. **No existing test hardcodes the `{workflow_version}.0.0` pin for a re-packaged deploy scenario** — the integration suite re-packages (asserting `2.0.0`, `3.0.0`) but never deploys afterward; that missing coverage is exactly how this bug shipped, and the exploration test below closes it. The 110-passing baseline is expected to pass unchanged.

## Hypothesized Root Cause

Verified, not hypothesized — confirmed by code reading and the live incident:

1. `create_workflow_deployment` pins `workflow_component_version(workflow_version)` = `{workflow_version}.0.0` (deployments.py ~3350) and never consults the version item's `component_arn`, which it already loaded and gated on three screens earlier. `record_workflow_deployment` independently re-derives the same wrong value for the association record.
2. The pin was correct when written: components were originally versioned `{workflow_version}.0.x` and re-packaging did not exist. `next_component_version` later introduced the deliberate major bump (Greengrass immutability + stale on-device patch/minor artifacts), and the deploy path was never updated to follow.
3. Greengrass CreateDeployment accepts the old pin without error — the component version exists — so nothing fails loudly: the target device compares the requested set against its installed set, finds no change, and reports COMPLETED. Silent success is what let revision 14 of the live incident look healthy.
4. The major-parse consumers (facets 1.3/1.4) encode the same outdated invariant "component major == workflow version". The subscribe-merge fix documented this as its facet 1.3 and deferred it, relying on the deploy path pinning `{v}.0.0` "by construction" — which is precisely the pin this fix corrects, so the deferral ends here.
5. Packaging records only `component_arn` (no discrete `component_version`), so no consumer has a direct mapping — facet 1.5, the enabler for D1/D2's arn parsing plus 2.5's cleaner field going forward.

## Correctness Properties

Property 1: Bug Condition (Fix Check) - Deploy path pins and reports the registered component version

_For any_ workflow deployment through `create_workflow_deployment` where the version item records a registered component version (component_version field or component_arn `:versions:` suffix), including versions whose registered major exceeds the workflow version (isBugCondition, kind workflow_deploy), the fixed function SHALL submit the workflow component entry at exactly that registered version, and SHALL record the same value in the association record, the audit log entry, and the 201 response's `component_version` field — falling back to `{workflow_version}.0.0` only when no authoritative record exists.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition (Fix Check) - Subscribe-topic resolution survives bumped majors

_For any_ deployment component set carrying a workflow component entry whose `componentVersion` equals the registered component version of one of that workflow's version items — whether or not the major equals the workflow version, and whether the entry was set by the fixed deploy path, a generic-path caller, or carried over from an existing deployment — the fixed `collect_workflow_subscribed_topics` SHALL resolve that version item and collect its recorded `subscribed_topics`, so the `dda:workflow-subscribe:<workflowId>` grant merges regardless of re-packaging history.

**Validates: Requirements 2.3**

Property 3: Bug Condition (Fix Check) - Binding keys and the vLLM gate resolve the true workflow version

_For any_ deployment component set carrying a workflow component entry with a bumped major (isBugCondition, kind components_map_consumer), the fixed `_deployed_workflow_binding_keys` SHALL derive the binding key from the resolved workflow version (so live keys survive the shadow prune), and the fixed `collect_vllm_component_manifests` SHALL resolve the correct version item (so LLM-bearing workflows keep activating the architecture gate with their recorded `packaged_architectures`).

**Validates: Requirements 2.4**

Property 4: Bug Condition (Fix Check) - Packaging records the component version discretely

_For any_ successful workflow packaging run, the fixed `workflow_packaging.py` SHALL record `component_version` = the resolved component version on the version item in the same update that records `component_arn`, and the two SHALL agree (the arn suffix equals the field).

**Validates: Requirements 2.5**

Property 5: Preservation - First-package deploys are byte-identical

_For any_ workflow deployment where the registered component version equals `{workflow_version}.0.0` (NOT isBugCondition), the fixed `create_workflow_deployment` SHALL produce the same submitted components map, association record, audit entry, and response body as the original function.

**Validates: Requirements 3.1**

Property 6: Preservation - Generic path and consumer fallback unchanged

_For any_ component set on the generic `create_deployment` path, the fixed code SHALL honor the caller's explicit component versions unchanged; and for any workflow entry whose componentVersion matches no version item's recorded component version, the consumers SHALL fall back to the original major-parse resolution, producing the original results (test_property_subscribed_topics.py and the 6 subscribe-warning tests pass unchanged).

**Validates: Requirements 3.2, 3.5, 3.6**

Property 7: Preservation - Revision semantics and pre-submit gates unchanged

_For any_ workflow deployment, the fixed function SHALL preserve revision semantics (existing components carried, older workflow entry replaced, deployment name reused, record/audit shapes identical modulo the corrected componentVersion value) and SHALL enforce every pre-submit gate with unchanged error envelopes and ordering.

**Validates: Requirements 3.3, 3.4**

Property 8: Preservation - Packaging numbering scheme and working-tree fixes unchanged

_For any_ packaging run, the fixed `workflow_packaging.py` SHALL keep assigning `{workflow_version}.0.0` on first package and the next free major on re-package; and the verified uncommitted working-tree behaviors (workflow-path subscribe merge, run-script cleanup) SHALL continue to hold.

**Validates: Requirements 3.7, 3.8**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/backend/functions/workflow_guards.py`

1. **`find_version_item_by_component_version(workflow_id, component_version)`** (new, near `get_version_item`): Query the WorkflowVersions partition for `workflow_id` (paged; version counts per workflow are small), return the `_decimal_to_native` item whose `component_version` field or `component_arn` `:versions:` suffix equals `component_version`, else None. Table-read errors return None (callers fall back).

**File**: `edge-cv-portal/backend/functions/workflow_packaging.py`

2. **Record the discrete field** (~line 2266): extend the success-bookkeeping `update_expression` with `component_version = :cv` and `update_values[':cv'] = resolved_component_version`. Nothing else moves.

**File**: `edge-cv-portal/backend/functions/deployments.py`

3. **`resolve_workflow_component_version(version_item, workflow_version)`** (new, next to `workflow_component_version` ~2125): D1's forward resolution — discrete field, else arn suffix (validated `^\d+\.\d+\.\d+$`), else `workflow_component_version(workflow_version)`.

4. **`_resolve_workflow_version_item(workflow_id, entry_component_version)`** (new, near the consumers): D2's scan-first resolver with major-parse fallback, returning `(workflow_version, version_item_or_None)`; exceptions logged and degraded to the fallback, mirroring the consumers' current resilience.

5. **Consumer rewiring**:
   - `collect_workflow_subscribed_topics` (~2130): resolve the item through the new resolver; topic extraction unchanged.
   - `collect_vllm_component_manifests` workflow branch (~2021): same resolution; `has_llm_inference`/`packaged_architectures` logic unchanged.
   - `_deployed_workflow_binding_keys` (~3044): binding keys from the resolved workflow version.

6. **`create_workflow_deployment`** (~3350): replace `component_version = workflow_component_version(workflow_version)` with `component_version = resolve_workflow_component_version(version_item, workflow_version)`; use it in the components-map entry and the inline vLLM manifest `version` label (~3270); pass it to `record_workflow_deployment(..., component_version=component_version)`; `audit_details` and `response_body` already read the local variable. Rewrite the now-stale comment above `apply_subscribe_access_control` (~3356): the entry is authoritative by resolution, and the helper itself now resolves bumped majors (superseding the subscribe-merge design's "by construction" note).

7. **`record_workflow_deployment`** (~2376): add keyword `component_version=None`; item value becomes `component_version or workflow_component_version(workflow_version)` — signature-safe for any other caller.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples on UNFIXED code with an exploration test that seeds the exact re-packaged shape, then verify the fix (Properties 1–4) and preservation (Properties 5–8). All new tests live in `edge-cv-portal/backend/tests/`, reusing the WorkflowDeployEnv harness (endpoint level) and the FakeGreengrass/FleetEnv integration fixtures.

### Exploratory Bug Condition Checking

**Goal**: Demonstrate the bug BEFORE the fix and confirm the root-cause analysis (if refuted, re-hypothesize).

**Test Plan**: Extend the WorkflowDeployEnv seeding so the version item's `component_arn` ends `:versions:2.0.0` while `workflow_version=1` (`latest_version: 1`) — the modbus_test incident shape. Deploy through the handler and assert the FIXED behavior; every assertion is expected to FAIL on unfixed code.

**Test Cases**:
1. **Re-packaged pin (incident shape)**: deploy v1 with arn `:versions:2.0.0` → assert the submitted entry is `{"componentVersion": "2.0.0"}` and the record/audit/201 all say `component_version == "2.0.0"` (will fail on unfixed code: all say `1.0.0`).
2. **Subscribe resolution on bumped major**: same seeding plus `subscribed_topics` on the v1 item and a carried-over LocalServer entry → assert the submitted LocalServer merge carries the workflow's grant with v1's topics (will fail on unfixed code once the entry is 2.0.0-shaped; on unfixed code the entry stays 1.0.0, so this case directly asserts the consumer against a seeded components map with a `2.0.0` carried-over entry — the bedrock_test shape — via `apply_subscribe_access_control`).
3. **Binding-key survival**: seeded map with a `2.0.0` entry for workflow v1 → assert `_deployed_workflow_binding_keys` yields `{workflowId}/1` (will fail on unfixed code: yields `{workflowId}/2`).
4. **vLLM gate on bumped major**: `2.0.0` entry, v1 item with `has_llm_inference` + `packaged_architectures` → assert `collect_vllm_component_manifests` produces the workflow manifest (will fail on unfixed code: no manifest, gate skipped).
5. **Packaging field**: package through FleetEnv, read the version item → assert `component_version` equals the response's `component_version` (will fail on unfixed code: attribute absent).

**Expected Counterexamples**:
- Submitted entry / record / audit / response pinned at `1.0.0` while the arn says `2.0.0`.
- Consumers resolving version item 2 (nonexistent) for the `2.0.0` entry: empty topics, `{workflowId}/2` key, missing vLLM manifest.
- Possible causes if assertions fail differently than predicted: the packaged-check gate reading more than `component_arn`, or FakeGreengrass rejecting the entry shape — both would force re-hypothesis.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := fixedPath(X)
  ASSERT submittedComponentVersion(result) = registered(X.versionItem)      -- P1
  ASSERT recordedVersions(result) all = registered(X.versionItem)           -- P1
  ASSERT resolvedVersionItem(consumers, entry) = the item whose registered
         component version = entry.componentVersion                         -- P2, P3
  ASSERT versionItemAfterPackaging.component_version = resolvedVersion      -- P4
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, fixed == original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT create_workflow_deployment_original(X) = create_workflow_deployment_fixed(X)
  ASSERT consumers_original(X.componentsMap) = consumers_fixed(X.componentsMap)
END FOR
```

**Testing Approach**: Property-based testing for the consumer preservation half — generate component sets whose workflow entries match their (faked or seeded) version items' `{v}.0.0` recordings and assert identical collection/derivation; the existing `test_property_subscribed_topics.py` already IS this property for the fallback path and must pass unchanged.

**Test Plan**: Run the full existing baseline on unfixed code first (110 passing), then unchanged after the fix. Specifically pinned suites: the 6 subscribe-warning tests, `test_property_subscribed_topics.py` (fallback path via faked `get_version_item`), `test_workflow_deploy_subscribe_merge_exploration.py` / `_preservation.py` (arn-seeded `{v}.0.0` items — scan resolves identically), `test_workflow_packaging_deployment_integration.py` (first-package deploys; the re-package test asserts `2.0.0`/`3.0.0` packaging output, untouched), `test_deployment_vllm_gate.py` (arn `:versions:1.0.0`, v1 entries).

**Test Cases**:
1. **First-package deploy byte-identity**: WorkflowDeployEnv fresh + revision deploys with `{v}.0.0` arns — submitted map, record, and response deep-equal today's shapes (P5; largely already pinned by the preservation suite).
2. **Fallback fidelity**: components map with entries matching no recorded component version → consumers produce exactly the major-parse results (P6).
3. **Gate/revision semantics**: re-run the existing envelope and revision assertions on the re-packaged seeding — only the componentVersion value differs (P7).
4. **Packaging scheme**: the existing `1.0.0 → 2.0.0 → 3.0.0` re-package test passes unchanged; the new field agrees with the arn each time (P8, P4).

### Unit Tests

- `resolve_workflow_component_version`: field-preferred, arn-parse, malformed-arn fallback, first-package equality with `workflow_component_version`.
- `find_version_item_by_component_version`: match by field, match by arn suffix, no match, table-error → None.
- `_resolve_workflow_version_item`: scan hit, scan miss → major fallback, non-numeric major → skip semantics preserved in each consumer.

### Property-Based Tests

- Generate (workflow_version, registered component version with major ≥ workflow_version) pairs: the deploy path always submits/records the registered version, and equality holds with today's output iff major == workflow_version (P1 + P5 in one property).
- Generate component sets mixing matching-recorded, bumped-major, and unrecorded workflow entries: consumers resolve the recorded item when one matches and fall back identically otherwise (P2, P3, P6).

### Integration Tests

- FleetEnv end-to-end: package v1 → deploy (1.0.0) → re-package (2.0.0) → deploy again → the revision submits `2.0.0`, the record/audit/response agree, FakeGreengrass shows a real component change — the exact flow that failed live, closing the coverage gap that let it ship.
- Subscribe merge + binding delivery through the re-packaged deploy: grant carries v1's topics, shadow prune preserves `{workflowId}/1`.

## Deployment

Portal-only: no device build, no LocalServer or component changes. Ships with a backend Lambda deploy of the portal (deployments + workflow packaging functions).

Live incident retest on ryan-orin-nano: re-deploy `modbus_test` (`e830f55d-5744-4edf-be43-1a33fbd4605d`) workflow v1 through the portal — expect the submitted entry at componentVersion `2.0.0`, Greengrass actually delivering the component (artifacts present on-device), the workflow registering with the LocalServer watcher, and the association record/audit/201 all reporting `2.0.0`. Optionally re-package first to pick up a fresh `3.0.0` and confirm the deploy follows it.
