# Workflow Deploy Subscribe Merge Bugfix Design

## Overview

trigger-activation-runtime Requirement 10.2 requires every portal-created Greengrass deployment that carries both a subscribing workflow component (version item with recorded `subscribed_topics`) and a LocalServer component to merge the `dda:workflow-subscribe:<workflowId>` `aws.greengrass#SubscribeToIoTCore` accessControl policy into the LocalServer entry's `configurationUpdate.merge`. Task 11.2 wired that merge — `apply_subscribe_access_control(components_map)` — into the generic `create_deployment` path only (deployments.py line ~1222).

The portal's workflow deploy page routes through `create_workflow_deployment` (dispatched by the handler when the body carries `component_type: workflow` / `workflow_id`, deployments.py line ~471). That function builds its OWN components map — the target's current component set (which includes LocalServer) merged with the workflow component entry — and calls `greengrass_client.create_deployment` directly. It never calls `apply_subscribe_access_control`, so portal workflow deployments ship no subscribe grant.

Confirmed live on ryan-orin-nano (2026-08-05): the `bedrock_test` workflow (workflow_id `f81a4c66-39ab-4068-a8b4-77509446e8c8`, version 5, mqtt_subscribe trigger on `dda/bedrock-test-trigger`) was portal-deployed; deployment `2014a473-6cd4-4470-9181-42e689cb4c89` shows LocalServer arm64JP6 1.0.51 with no configurationUpdate while carrying the workflow component. The device denied SubscribeToIoTCore ("Greengrass IPC denied SubscribeToIoTCore for topic 'dda/bedrock-test-trigger'") and the trigger never fired. A manual deployment revision adding the grant fixed the device immediately.

The fix is one addition inside `create_workflow_deployment`: call `apply_subscribe_access_control(components_map)` on the FINAL merged components map (after the existing-deployment merge and the workflow entry placement, before `greengrass_client.create_deployment`), and surface the returned warnings additively in the 201 response (a `warnings` field present only when non-empty, mirroring `create_deployment`). `apply_subscribe_access_control` itself and the generic path are untouched.

## Glossary

- **Bug_Condition (C)**: A deployment submitted by `create_workflow_deployment` whose merged component set contains a workflow component with recorded `subscribed_topics` — with a LocalServer component present (policy missing from the submitted set) or absent (no warning in the response).
- **Property (P)**: The desired behavior — the submitted LocalServer entry carries the `dda:workflow-subscribe:<workflowId>` policy scoped to exactly the recorded topic filters (merged non-destructively into any existing merge document), or, when LocalServer is absent from the set, the 201 response carries the actionable warnings additively.
- **Preservation**: Non-subscribing workflow deployments submit byte-identical component maps with no `warnings` field; the generic `create_deployment` path, `apply_subscribe_access_control` itself, all pre-submit gates, revision semantics, and record/audit bookkeeping are unchanged.
- **`create_workflow_deployment`**: The workflow deployment path in `edge-cv-portal/backend/functions/deployments.py` (~line 3127). Validates RBAC, version guards, packaging, LocalServer compatibility, plugin/vLLM gates, and camera bindings; merges the target's existing component set with the workflow entry; submits via the Use_Case-account greengrassv2 client.
- **`apply_subscribe_access_control(components_map)`**: The existing merge helper (deployments.py ~line 2163). Collects `{workflow_id: topics}` for `dda.workflow.*` entries via `collect_workflow_subscribed_topics`, deep-merges one uniquely-keyed policy per subscribing workflow into the LocalServer entry's `configurationUpdate.merge` (non-destructively into any existing merge document; never clobbering an unparseable one), and returns actionable warnings when LocalServer is absent. Not modified by this fix.
- **`collect_workflow_subscribed_topics`**: Resolves each `dda.workflow.*` entry's version item via `workflow_guards.get_version_item(workflow_id, major(componentVersion))`. On the workflow deployment path the deployed entry's componentVersion is always `{workflow_version}.0.0` (`workflow_component_version`), so the major parse resolves exactly the authoritative version item `create_workflow_deployment` already loaded and validated — satisfying Requirement 2.3 by construction.
- **components map**: The `{componentName: {componentVersion, configurationUpdate?}}` dict submitted to Greengrass CreateDeployment. On the workflow path: the existing deployment's components (when revising) plus the workflow entry.
- **`warnings` field**: The additive response field `create_deployment` uses for the helper's warnings — present in the 201 body only when non-empty (trigger-activation-runtime 10.3/10.4 byte-identity).
- **DeployEnv harness**: The endpoint-level test convention from `tests/test_subscribe_deployment_warning.py`: FakeGreengrass/FakeIot (from `test_workflow_packaging_deployment_integration.py`) wired in as the Use_Case-account clients via monkeypatched `get_usecase_client`, real version items seeded in the moto-backed WorkflowVersions table.

## Bug Details

### Bug Condition

The bug manifests when `create_workflow_deployment` submits a deployment for a workflow version whose item records non-empty `subscribed_topics`.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type WorkflowDeploymentSubmission
         {workflowId, workflowVersion, versionItem, mergedComponentsMap}
  OUTPUT: boolean

  -- X passes every pre-submit gate (RBAC, validation guard, packaging,
  -- LocalServer compatibility, plugin/vLLM gates, camera bindings) and
  -- reaches greengrass_client.create_deployment.
  RETURN nonEmpty(X.versionItem.subscribed_topics)
         AND (
           -- facet 1 (1.1): LocalServer in the merged set, policy missing
           (∃ ls ∈ X.mergedComponentsMap:
              ls startsWith "aws.edgeml.dda.LocalServer"
              AND "dda:workflow-subscribe:" + X.workflowId
                  ∉ policies(ls.configurationUpdate.merge))
           OR
           -- facet 2 (1.2): no LocalServer anywhere to attach to,
           -- and the 201 response carries no warning
           (∄ ls ∈ X.mergedComponentsMap:
              ls startsWith "aws.edgeml.dda.LocalServer")
         )
END FUNCTION
```

A third facet (1.3) is structural: `collect_workflow_subscribed_topics` resolves the version item by parsing the MAJOR out of the entry's componentVersion, and re-packaging bumps component majors past the workflow version (`next_component_version` returns `max(workflow_version, highest_major + 1)`). On the workflow deployment path this is neutralized by construction: the path sets its own entry to `workflow_component_version(workflow_version)` = `{workflow_version}.0.0`, so the major parse resolves the authoritative version item the path already loaded (Requirement 2.3). No helper change is needed or permitted (constraint: `apply_subscribe_access_control` untouched).

### Examples

- **Live incident (facet 1)**: portal deploy of `bedrock_test` v5 to ryan-orin-nano — the merged set carried `aws.edgeml.dda.LocalServer.arm64JP6` 1.0.51 (from the target's existing deployment) plus `dda.workflow.f81a4c66...`; the submitted LocalServer entry had NO configurationUpdate. On-device: "Greengrass IPC denied SubscribeToIoTCore for topic 'dda/bedrock-test-trigger'"; the workflow never triggered. Expected: the LocalServer entry's configurationUpdate.merge carries `dda:workflow-subscribe:f81a4c66-...` with resources `["dda/bedrock-test-trigger"]`.
- **Facet 2**: fresh workflow deployment to a target with no existing deployment — the merged set is just the workflow entry, no LocalServer. Today the 201 response is silent. Expected: the response carries the actionable warning(s) under `warnings`.
- **Existing-merge edge (facet 1)**: the target's existing deployment already carries a LocalServer `configurationUpdate.merge` (e.g. the manually-added subscribe grant from the incident recovery, or a Nucleus-store-style config document). Expected: the policy key is upserted into the existing merge document with every pre-existing key preserved (`apply_subscribe_access_control` already merges non-destructively; the workflow path must reuse it, not reimplement it).
- **Non-bug (preservation)**: a workflow version without recorded `subscribed_topics` — the submitted components map gains no configurationUpdate anywhere and the 201 response has no `warnings` key (byte-identity, 3.1).

## Expected Behavior

### Fix Semantics

After the components map is final — existing components merged in (revision case) and the workflow entry placed — and before `greengrass_client.create_deployment` is called, `create_workflow_deployment` calls `apply_subscribe_access_control(components_map)` and holds the returned warnings. The 201 response gains a `warnings` field only when the list is non-empty, exactly mirroring `create_deployment`'s response pattern. Nothing else in the function moves: the merge runs after every gate (a rejected submission never reaches it) and before submission, name resolution and rollout policies are unaffected (the merge only touches the LocalServer entry's configurationUpdate), and record/audit bookkeeping is unchanged.

### Preservation Requirements

**Unchanged Behaviors:**

- Non-subscribing workflow deployments (no `subscribed_topics` on the version item, or an empty list) submit byte-identical component maps — no accessControl merge, no new keys anywhere — and return responses without a `warnings` field (3.1).
- The generic `create_deployment` path applies the merge and surfaces warnings exactly as today; `apply_subscribe_access_control` keeps its single-argument signature and behavior, including the non-destructive existing-merge upsert and the unparseable-merge safety path (3.2, 3.5).
- Revision semantics: the target's existing components are preserved, the older workflow component version is replaced, the existing deployment name is reused, and the association record + audit entries are written as today (3.3).
- All pre-submit gates (validation guard, packaged-component check, LocalServer compatibility, plugin gates, vLLM gate, camera binding validation/delivery) keep their error envelopes and ordering; rejected submissions never reach the merge or Greengrass (3.4).

**Scope:** Only `create_workflow_deployment` in `edge-cv-portal/backend/functions/deployments.py` changes. `create_deployment`, `apply_subscribe_access_control`, `collect_workflow_subscribed_topics`, the packaging pipeline, and everything under `src/backend/workflow_engine/` are untouched (the latter is under active work in this tree).

## Hypothesized Root Cause

Verified, not hypothesized — confirmed by code reading and the live incident:

1. trigger-activation-runtime task 11.2 added `apply_subscribe_access_control(components_map)` to `create_deployment` (line ~1222), the generic path that receives an explicit component list.
2. `create_workflow_deployment` (line ~3127) predates the feature and builds its own components map (existing deployment components + `workflow_component_version(workflow_version)` entry, lines ~3338–3353), submitting directly via `greengrass_client.create_deployment`. The merge call was never added there.
3. The handler dispatches `component_type: workflow` / `workflow_id` bodies to `create_workflow_deployment` (line ~471) — the portal's workflow deploy page always takes this path, so every portal workflow deployment bypassed the merge.
4. On-device, the LocalServer's IPC `SubscribeToIoTCore` call has no matching accessControl policy and the Greengrass IPC broker denies it; the mqtt_subscribe trigger never subscribes.

## Correctness Properties

Property 1: Bug Condition (Fix Check) - Subscribe policy rides every workflow deployment with LocalServer in the merged set

_For any_ workflow deployment submission where the deployed version item records non-empty `subscribed_topics` and the merged component set contains a LocalServer component (isBugCondition, facet 1), the fixed `create_workflow_deployment` SHALL submit a components map whose LocalServer entry's `configurationUpdate.merge` carries the `dda:workflow-subscribe:<workflowId>` policy with operations `["aws.greengrass#SubscribeToIoTCore"]` and resources exactly the recorded topic filters — resolved from the authoritative workflow version being deployed — with any pre-existing merge document's keys preserved, and the 201 response SHALL carry no `warnings` field.

**Validates: Requirements 2.1, 2.3**

Property 2: Bug Condition (Fix Check) - Warning surfaced when LocalServer is absent from the merged set

_For any_ workflow deployment submission where the deployed version item records non-empty `subscribed_topics` and the merged component set contains no LocalServer component (isBugCondition, facet 2), the fixed `create_workflow_deployment` SHALL return a 201 response carrying the actionable warning(s) under an additive `warnings` field (naming the workflow, its topics, the LocalServer component, and the on-device denial consequence), and the submitted components map SHALL be untouched by the merge.

**Validates: Requirements 2.2**

Property 3: Preservation - Non-subscribing workflow deployments are byte-identical

_For any_ workflow deployment submission where the deployed version item records no `subscribed_topics` (NOT isBugCondition), the fixed `create_workflow_deployment` SHALL submit a components map identical to the unfixed function's output — no configurationUpdate added to any entry, carried-over components unchanged — and return a 201 response without a `warnings` key.

**Validates: Requirements 3.1**

Property 4: Preservation - Generic path, helper, gates, and revision semantics unchanged

_For any_ input to the generic `create_deployment` path, to `apply_subscribe_access_control` (single argument), or to `create_workflow_deployment`'s pre-submit gates and revision bookkeeping, the fixed code SHALL behave identically to the original: the existing suites — test_subscribe_deployment_warning.py, test_property_subscribed_topics.py, and every existing workflow-deployment/deployment test — pass unchanged.

**Validates: Requirements 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Code Change

`edge-cv-portal/backend/functions/deployments.py`, inside `create_workflow_deployment`, immediately after the workflow entry is placed into `components_map` (after line ~3353) and before `deployment_params` is built:

```python
        # Subscribe accessControl (trigger-activation-runtime 10.2, 10.3):
        # same merge as create_deployment, applied to the FINAL merged
        # component set (target's existing components + this workflow
        # entry). The deployed entry's componentVersion is
        # {workflow_version}.0.0, so the helper's major-parse resolves the
        # authoritative version item this function already validated.
        deployment_warnings = apply_subscribe_access_control(components_map)
```

And in the 201 response construction (after the existing keys, before `create_response`):

```python
        response_body = { ... existing keys unchanged ... }
        # Additive: present only when the merge produced warnings,
        # mirroring create_deployment (10.3/10.4 byte-identity).
        if deployment_warnings:
            response_body['warnings'] = deployment_warnings
        return create_response(201, response_body)
```

The placement guarantees:
- Every gate rejection returns before the merge runs (3.4).
- The merge sees the complete map — carried-over LocalServer included — so the revision case (the live incident's shape) attaches the policy (2.1).
- `deployment_name` reuse, rollout policies, tags, camera binding delivery, `record_workflow_deployment`, and audit logging are untouched (3.3).

## Testing Strategy

All new tests live in `edge-cv-portal/backend/tests/`, using the DeployEnv-style harness from `test_subscribe_deployment_warning.py` with the stateful `FakeGreengrass`/`FakeIot` from `test_workflow_packaging_deployment_integration.py` (which support `seed_deployment` for the revision case and `register_device` for the LocalServer compatibility gate). Workflow deployments additionally need: a workflows-table item (`get_workflow_metadata`), a version item with `validation_status: passed` and `component_arn` (deployment guard + packaging check), and a registered device with a compatible LocalServer version. Versions are seeded without `camera_input_nodes`/`plugin_components`/`has_llm_inference` so those gates no-op.

1. **Bug condition exploration test** (`test_workflow_deploy_subscribe_merge_exploration.py`) — asserts the FIXED behavior on unfixed code, expected to FAIL before the fix:
   - Revision case (live incident shape): seeded existing deployment carrying LocalServer arm64JP6 → deploy subscribing workflow → asserts the submitted LocalServer entry carries the policy.
   - Fresh case: no existing deployment → asserts the 201 response carries `warnings`.
   - Existing-merge case: seeded existing deployment whose LocalServer entry already has a `configurationUpdate.merge` (pre-existing accessControl key) → asserts the pre-existing key survives and the policy key is upserted.
2. **Fix-check tests** (same file family, run after the fix): the exploration assertions pass, plus response-shape checks (`warnings` absent when the merge is clean).
3. **Preservation tests**: non-subscribing workflow deploys byte-identically (submitted map deep-equal, no `warnings` key); existing suites re-run unchanged (`python3 -m pytest tests/ -q -k "workflow_deployment or deployment"` — 102 passing baseline plus the 6 subscribe-warning tests; known pre-existing collection cascade in test_property_setup_command_wellformed is out of scope).

## Deployment

Portal-only: no device build. Ships with a portal deploy (backend Lambda update); user go-ahead already given for this fix.
