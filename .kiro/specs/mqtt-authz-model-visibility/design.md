# MQTT Authorization & Model Visibility Bugfix Design

## Overview

Two independent, minimal fixes:

**Defect 1 (recipe access control):** the workflow engine's Greengrass MQTT publish path (`OutputBindingProcessor._run_mqtt_publish` → `_default_greengrass_publisher`, `src/backend/workflow_engine/output_bindings.py`) calls Greengrass IPC `PublishToIoTCore` under the LocalServer component's identity. Greengrass authorizes IPC MQTT operations against the component's `aws.greengrass.ipc.mqttproxy` access-control policies. All four recipe variants carry exactly one policy (`...:mqttproxy:1`) whose only resource is `$aws/things/*/shadow/name/*`. The `mqtt_publish` node's `topic` is free-form user input (catalog descriptor: `ParameterDescriptor("topic", "string", required=True, constraints={"min_length": 1})`, example `factory/line1/inspection`), so workflow publishes are denied with `UnauthorizedError`. The fix adds a second, publish-only mqttproxy policy entry (`...:mqttproxy:2`) with resource `*` to each of the four recipe variants, with a policyDescription documenting why the broad resource is required. `recipe.yaml` is a build artifact and is NOT edited.

**Defect 2 (frontend data source):** the Deployed models page (`DeployedModels.tsx`) fetches through `listModels()`, which applies the legacy-workflow filter `isAssignableModel` (drops `VllmModel`). The backend already returns the vLLM entry (verified on-device). The fix switches the page to the unfiltered fetcher `listFeatureConfigurations()` (both already exist in `FeatureConfigurationAPI.ts`) and adds a friendly "vLLM" case to `modelTypeLabel`. `listModels()` and its legacy-workflow consumers are untouched.

## Glossary

- **Bug_Condition (C)**: Defect 1 — an `mqtt_publish` Greengrass publish to a topic not matching `$aws/things/*/shadow/name/*`; Defect 2 — a `/feature-configurations` response containing at least one `VllmModel` entry rendered by the Deployed models page.
- **Property (P)**: Defect 1 — the recipe authorizes `PublishToIoTCore` for the topic, so the IPC call succeeds; Defect 2 — every returned model, including `VllmModel` entries, appears as a row on the Deployed models page.
- **Preservation**: shadow-topic policy, recipe lifecycle/structure, non-Greengrass MQTT paths, legacy workflow model filtering, and LFV/Triton row rendering — all unchanged.
- **mqttproxy policy**: an entry under `ComponentConfiguration.DefaultConfiguration.accessControl["aws.greengrass.ipc.mqttproxy"]` in a Greengrass component recipe; its `resources` list is matched against the topic of `PublishToIoTCore`/`SubscribeToIoTCore` calls (`*` is a wildcard).
- **`_default_greengrass_publisher`**: `src/backend/workflow_engine/output_bindings.py` (~line 379) — publishes one message via Greengrass IPC `PublishToIoTCore` with the LocalServer component identity.
- **Recipe variants**: `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` at the repo root (source of truth). `recipe.yaml` is a generated build artifact — never edited.
- **`isAssignableModel`**: `src/frontend/src/components/workflow/types.ts` — returns false for `FeatureConfigurationType.VllmModel`; the shared legacy-workflow model filter introduced by edge-vlm-workflow-fixes.
- **`listModels` / `listFeatureConfigurations`**: `src/frontend/src/api/FeatureConfigurationAPI.ts` — filtered (legacy-assignable only) and unfiltered fetchers of `/feature-configurations`.
- **Structure goldens**: `test/backend-test/deploy_reliability/goldens/recipe_*_structure.golden.json` — masked parsed-recipe snapshots asserted by `test_config_structure_preservation.py`; `accessControl` is INSIDE the golden (not masked), so the Defect 1 recipe edit requires a reviewed golden regeneration via that test file's `--regenerate` mode.

## Bug Details

### Bug Condition

**Defect 1** manifests whenever a workflow `mqtt_publish` node with `greengrass=true` publishes to any topic outside the shadow namespace — i.e. every realistic workflow topic.

**Formal Specification:**
```
FUNCTION isBugCondition_1(input)
  INPUT: input of type GreengrassPublish  // (topic, payload, qos) via IPC PublishToIoTCore
  OUTPUT: boolean

  RETURN input.publisher = LocalServer_component_identity
         AND NOT topicMatches(input.topic, "$aws/things/*/shadow/name/*")
         // no mqttproxy policy resource covers input.topic → UnauthorizedError
END FUNCTION
```

**Defect 2** manifests whenever the feature-configurations response contains a `VllmModel` entry and the Deployed models page renders.

**Formal Specification:**
```
FUNCTION isBugCondition_2(input)
  INPUT: input of type FeatureConfiguration[]  // /feature-configurations response
  OUTPUT: boolean

  RETURN EXISTS m IN input WHERE m.type = "VllmModel"
         // listModels() drops m, so the Deployed models page never shows it
END FUNCTION
```

### Examples

- Workflow execution `85bf7a61` on the JP6 device: `_run_mqtt_publish` → `_default_greengrass_publisher` → `operation.get_response().result(timeout=10.0)` raised `awsiot.greengrasscoreipc.model.UnauthorizedError`. Expected: message published to the configured workflow topic.
- Deployed 1.0.45 recipe on-device carries only `aws.edgeml.dda.LocalServer.arm64JP6:mqttproxy:1` with resource `$aws/things/*/shadow/name/*` — a publish to `factory/line1/inspection` is unauthorized. Expected: an additional policy authorizes it.
- On-device `/feature-configurations` returns `{"type":"VllmModel","modelName":"opt125m-smoke","status":"LOADING",...}` alongside a `TritonModel` entry, but the Deployed models page lists only the Triton model. Expected: both rows.
- Edge case (works today, must keep working): shadow sync publishes to `$aws/things/<thing>/shadow/name/<shadow>/...` are authorized by `mqttproxy:1` and must remain so.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The existing `...:mqttproxy:1` shadow policy (operations, resources, description) in every recipe variant.
- Every other recipe section: ComponentDependencies, ComponentConfiguration keys, ShadowManager/Cli access control, and the edge-deploy-reliability Install/Startup/Shutdown lifecycle.
- The `mqtt_publish` plain-broker and `aws_iot` publishing paths (no Greengrass IPC involved).
- `listModels()` output (still filtered by `isAssignableModel`) and the legacy workflow editor's model options (still exclude `VllmModel`).
- Deployed models page rendering of `LFVModel`/`TritonModel` rows: friendly name, status, type label, input shape.

**Scope:**
All inputs that do NOT involve (a) a Greengrass IPC publish to a non-shadow topic or (b) a `VllmModel` feature-config entry on the Deployed models page are completely unaffected. This includes shadow pubsub, other components' access control, legacy workflow model assignment, and all non-vLLM model rows.

## Hypothesized Root Cause

Both root causes were confirmed with code and device evidence during investigation:

1. **Defect 1 — missing publish authorization (CONFIRMED)**: the recipes' single mqttproxy policy covers only shadow topics; the `mqtt_publish` topic is free-form user input, so Greengrass denies `PublishToIoTCore` with `UnauthorizedError`. Confirmed in all four repo recipe variants (~lines 37–45) and in the deployed 1.0.45 recipe on the device. The publisher code itself is correct — it fails only on authorization.
   - Because the topic is unconstrained (`min_length: 1`, arbitrary user string), no topic prefix can cover it; the new policy needs resource `*`. Least privilege is kept on the operation axis: publish-only, no new subscribe resources.
2. **Defect 2 — stale/over-broad filter on the models page (CONFIRMED)**: `DeployedModels.tsx` uses `listModels()`, whose `isAssignableModel` filter (added by edge-vlm-workflow-fixes for legacy workflow assignment) drops `VllmModel` entries. The backend returns the entry (device-verified), so this is purely a frontend data-source choice. `modelTypeLabel` also lacks a friendly `VllmModel` case (would render the raw enum string via its default branch).

## Correctness Properties

Property 1: Bug Condition - Workflow topics authorized for Greengrass publish

_For any_ topic string the `mqtt_publish` node accepts (any non-empty topic, including non-shadow workflow topics), each of the four LocalServer recipe variants SHALL contain an `aws.greengrass.ipc.mqttproxy` policy entry whose operations include `aws.greengrass#PublishToIoTCore` and whose resources match that topic under Greengrass wildcard matching, so the fixed component's IPC publish is authorized and completes without `UnauthorizedError`.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - vLLM models visible on the Deployed models page

_For any_ `/feature-configurations` response containing `VllmModel` entries, the fixed Deployed models page SHALL render one row per returned model — including every `VllmModel` entry, with its model name, status, and a type label — instead of silently dropping them.

**Validates: Requirements 2.3**

Property 3: Preservation - Recipe access control and structure outside the new policy

_For any_ of the four recipe variants, the fixed recipe SHALL be identical to the original recipe everywhere except the added publish-only mqttproxy policy entry: the `mqttproxy:1` shadow policy is byte-identical, no `SubscribeToIoTCore` resource is added anywhere, and all other sections (lifecycle, dependencies, other access-control blocks) parse to the same structure as before, preserving the edge-deploy-reliability changes.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Property 4: Preservation - Legacy model filtering and existing model rows

_For any_ feature-configuration list, the fixed code SHALL produce the same result as the original for all legacy-workflow consumers — `listModels()` still returns exactly the `isAssignableModel` subset and `EditWorkflow` model options still exclude `VllmModel` — and the Deployed models page SHALL render `LFVModel`/`TritonModel` rows (name, status, type label, shape) exactly as the original did.

**Validates: Requirements 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

**Defect 1**

**Files**: `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` (NOT `recipe.yaml` — build artifact)

**Specific Changes**:
1. **Add a second mqttproxy policy entry** to each variant's `accessControl["aws.greengrass.ipc.mqttproxy"]`, keyed `'<ComponentName>:mqttproxy:2'`:
   - `policyDescription`: documents that workflow `mqtt_publish` output topics are free-form user input, so publish must be authorized on `*`; publish-only by design (no subscribe broadening)
   - `operations`: `["aws.greengrass#PublishToIoTCore"]` only
   - `resources`: `["*"]`
2. **Leave `mqttproxy:1` untouched** (shadow subscribe+publish).
3. **Regenerate deploy_reliability structure goldens** (reviewed, intentional change):
   `python3 test/backend-test/deploy_reliability/test_config_structure_preservation.py --regenerate`
   and re-run the deploy_reliability suite.
4. No code change: `_default_greengrass_publisher` is correct as-is.
5. Recipe changes take effect only in a rebuilt/redeployed component version — final validation is an on-hardware JP6 gate (user go-ahead required).

**Defect 2**

**Files**: `src/frontend/src/components/model/DeployedModels.tsx`, `src/frontend/src/components/model/helpers.ts`

**Specific Changes**:
1. **Switch the page's data source**: in `DeployedModels.tsx`, import and call `listFeatureConfigurations` (unfiltered) instead of `listModels`; update the `queryKey` accordingly. No other page logic changes.
2. **Friendly type label**: add `case FeatureConfigurationType.VllmModel: return "vLLM";` to `modelTypeLabel` in `helpers.ts` (the default branch would otherwise show the raw `"VllmModel"` string).
3. **Leave `listModels`, `isAssignableModel`, and `EditWorkflow` untouched.**

## Testing Strategy

### Validation Approach

Two-phase: first write exploration tests that FAIL on the unfixed tree (confirming both bugs and their root causes), and preservation tests that PASS on the unfixed tree (capturing the baseline). Then apply the fixes and verify exploration tests pass and preservation tests still pass. Backend/recipe tests use pytest + hypothesis (already in the test stack); frontend tests use jest + fast-check 3.23.2 (already a devDependency).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating both bugs BEFORE the fix, confirming the root-cause analysis.

**Test Plan**:
- Defect 1 (`test/backend-test/mqtt_authz/`): parse each of the four recipe variants; implement Greengrass resource wildcard matching (`*` matches any sequence); property test (hypothesis) generating arbitrary non-shadow workflow topics and asserting some mqttproxy policy with `PublishToIoTCore` matches the topic. Run on UNFIXED recipes.
- Defect 2 (`src/frontend/src/components/model/`): render `DeployedModels` with a mocked `/feature-configurations` response mixing `TritonModel` and `VllmModel` entries (fast-check-generated lists including the concrete on-device counterexample `opt125m-smoke`); assert every returned model name appears in the table. Run on UNFIXED code.

**Test Cases**:
1. **Workflow topic authorization (per variant)**: `factory/line1/inspection` and generated topics must be covered by a publish policy (will fail on unfixed recipes)
2. **Concrete device counterexample**: JP6 variant + the workflow-engine publish path topic semantics (will fail on unfixed recipes)
3. **vLLM row rendering**: mocked response containing `opt125m-smoke` (`VllmModel`) must produce a row (will fail on unfixed code)
4. **All-models property**: for any generated mixed list, rendered row count equals returned entry count (will fail on unfixed code whenever a `VllmModel` is present)

**Expected Counterexamples**:
- Every non-shadow topic is unmatched by all mqttproxy policies in all four variants
- Every `VllmModel` entry is absent from the rendered table
- Possible alternate causes ruled out by evidence: publisher code path (correct), backend endpoint (returns the entry, device-verified)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed artifacts produce the expected behavior.

**Pseudocode:**
```
FOR ALL topic WHERE isBugCondition_1(topic-publish) DO
  ASSERT EXISTS policy IN recipe.mqttproxy WHERE
    "aws.greengrass#PublishToIoTCore" IN policy.operations
    AND resourceMatches(policy.resources, topic)
END FOR

FOR ALL response WHERE isBugCondition_2(response) DO
  rows := renderDeployedModels_fixed(response)
  ASSERT FOR ALL m IN response: m.modelName IN rows
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed code/recipes behave identically to the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT original(input) = fixed(input)
END FOR
```

**Testing Approach**: Property-based testing generates many topics/model lists automatically, catching edge cases and giving strong unchanged-behavior guarantees.

**Test Plan**: Observe UNFIXED behavior first, capture it as property-based tests, verify they pass on the unfixed tree, then re-run after the fix.

**Test Cases**:
1. **Shadow policy preservation**: `mqttproxy:1` entry (operations, resources, description) is byte-identical in all variants; shadow topics remain authorized for subscribe+publish
2. **No subscribe broadening**: the set of resources authorized for `SubscribeToIoTCore` is unchanged
3. **Recipe structure preservation**: existing deploy_reliability golden tests (with goldens regenerated for exactly the reviewed mqttproxy addition) — lifecycle, dependencies, everything else unchanged
4. **Legacy filter preservation**: for any generated feature-config list, `listModels()` returns exactly the `isAssignableModel` subset, and `EditWorkflow` options exclude `VllmModel` (passes on unfixed code)
5. **LFV/Triton row preservation**: for any vLLM-free list, the Deployed models page renders identical rows before/after the fix

### Unit Tests

- Greengrass resource matcher helper (exact, `*`, shadow patterns)
- `modelTypeLabel` cases: LFV, Triton, vLLM, unknown string, null
- `DeployedModels` empty/error states unchanged

### Property-Based Tests

- hypothesis: arbitrary non-shadow topics are publish-authorized in every variant (fix check); shadow topics stay authorized and subscribe resources unchanged (preservation)
- fast-check: arbitrary mixed model lists render completely on the page (fix check); `listModels`/`EditWorkflow` filtering unchanged and vLLM-free lists render identically (preservation)

### Integration Tests

- Local frontend jest suite green (including edge-vlm-workflow-fixes legacy-filter tests)
- deploy_reliability suite green with regenerated goldens
- On-hardware JP6 gate (requires component rebuild + redeploy, user go-ahead): run a workflow with an `mqtt_publish` (greengrass) node and confirm no `UnauthorizedError` and message arrival in IoT Core; confirm `opt125m-smoke` listed on the Deployed models page
