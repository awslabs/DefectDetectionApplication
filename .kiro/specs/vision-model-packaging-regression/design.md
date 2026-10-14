# Bugfix Design Document

## Overview

Teach the Defect C model-dependency resolution to understand the vision publish shape. Changes confined to `edge-cv-portal/backend/functions/workflow_packaging.py`: `resolve_model_components` gains the plural-shape reading (and needs the selected architectures to pick target-relevant entries), and `model_component_dependencies` gains the Defect F-style single-name rule for per-target vision components.

## Glossary

- **vLLM shape**: `record['published_component']` — singular dict with `component_name` (arch-agnostic name, e.g. `model-vllm-qwen...`), written by the vLLM publish path.
- **Vision shape**: `record['published_components']` — list of `{component_name, target, component_version, status, platform}` entries, one per publish target (e.g. `model-yolo-test-jetson-xavier-jp5`), written by greengrass_publish.py.
- **arch→target mapping**: `arm64_jp4→jetson-xavier` (legacy), `arm64_jp5→jetson-xavier-jp5`, `arm64_jp6→jetson-xavier-jp6`, `x86_64`/`x86_64_nvidia`→ the x86 target id used by greengrass_publish.TARGET_TO_LOCAL_SERVER/TARGET_TO_PLATFORM (confirm exact key from that dict at implementation time; fail-closed discipline — unknown arch raises as today).

## Bug Details

### Root Cause

Defect C's `resolve_model_components` reads only `record.get('published_component')`. Vision publishes never write that attribute (they append to `published_components`), so the fail-closed gate misfires with "has no published Greengrass component" for every published vision model. Verified: dda-portal-training-jobs item for `yolo_test` (training 6a43ff2b) has `published_component: null`, `published_components: [{component_name: model-yolo-test-jetson-xavier-jp5, status: published, ...}]`; Lambda error observed live at 2026-08-04T04:38Z for workflow 6075bf76 v3.

### isBugCondition

isBugCondition(package) — a referenced model whose registry record has no singular `published_component.component_name` but has at least one `published_components` entry with `status == 'published'`.

## Expected Behavior

Resolution returns, per referenced model, the set of published component names relevant to the workflow's selected architectures:

- vLLM shape present → `{component_name}` (today's path, unchanged).
- else vision shape present → map each selected arch to its publish target; collect `component_name` of entries with `status=='published'` whose `target` matches a selected arch's target. If any selected architecture's target has no published entry → PackagingError naming the model and the uncovered architecture/target (2.6, superseded by edge-deploy-reliability Defect G 2.19 — fail closed on uncovered architectures).
- else → existing fail-closed PackagingError (2.5).

Emission (`model_component_dependencies`): for each model, |names|==1 → one HARD unpinned entry as today; |names|>1 → omit all entries for that model with a warning (2.4, mirrors Defect F); |names|==0 → nothing.

## Correctness Properties

Property 1: Bug Condition - Published vision models resolve and package

_For any_ model record carrying only the vision shape with ≥1 `status=='published'` entry matching a selected architecture's target, the fixed resolution SHALL NOT raise when every selected architecture is covered, and packaging SHALL emit exactly one HARD unpinned dependency on that entry's component_name when the selected architectures resolve to one name (zero entries, with a logged warning, when names diverge; a PackagingError naming the model and the uncovered architecture/target when a selected architecture has no matching entry — superseded by edge-deploy-reliability Defect G 2.19).

**Validates: Requirements 2.1–2.6 (bug-condition side: 1.1, 1.2)**

Property 2: Preservation - vLLM resolution, fail-closed gates, and non-model paths unchanged

_For any_ record with the singular vLLM shape, resolution and emission are byte-identical to today; _for any_ model with neither shape (or no record), the existing PackagingError messages still raise; no-model workflows, plugin deps, LocalServer deps (Defect F rule), and the TRAINING_JOBS_TABLE-unset skip behave identically.

**Validates: Requirements 3.1–3.4**

## Fix Implementation

`resolve_model_components(model_names, usecase_id, archs)` — signature gains the selected archs (the packaging handler already has them; update the call site). Internals per Expected Behavior. Add `ARCH_TO_PUBLISH_TARGET` beside `ARCH_TO_LOCAL_SERVER_COMPONENT`, values validated against greengrass_publish.py's target ids at implementation time. `model_component_dependencies(resolved)` consumes `{model_name: set_of_component_names}` (or keep dict-of-lists); single-name → today's entry; multi-name → omit + warn. Keep the returned structure change internal to workflow_packaging.py (update the one call site and any helper types).

Note an important preservation subtlety: today's vLLM path returns `{model_name: published_map}` and emission reads `published['component_name']` — restructure freely but the EMITTED entries must be byte-identical for vLLM inputs.

## Testing Strategy

Existing seams: the Defect C/F test files under `edge-cv-portal/backend/tests/` (moto aws_stack fixture, direct function calls). Exploration (fails on unfixed): vision-shape record with published jp5 entry + archs ['arm64_jp5'] → resolve raises today. Preservation (passes before and after): vLLM singular shape resolution/emission equality; neither-shape fail-closed messages; no-model/plugin/LocalServer emission unchanged. Checkpoint: full portal packaging suites + deploy_reliability. Then portal deploy + live repackage of yolo_test as final verification.
