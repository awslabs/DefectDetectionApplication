# Implementation Plan

## Overview

Fixes the vision/ONNX packaging regression (Defect C resolver reads only the vLLM `published_component` shape) using the exploratory bugfix workflow: exploration test fails on unfixed code, preservation captured before the fix, then fix, checkpoint, portal deploy, and a live yolo_test repackage as verification.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1", "2"], "description": "Tests against UNFIXED code: task 1 (Bug Condition) fails, task 2 (Preservation) passes. Independent."},
    {"wave": 2, "tasks": ["3"], "description": "Implement the fix (3.1), re-run task 1 (3.2) and task 2 (3.3). Depends on wave 1."},
    {"wave": 3, "tasks": ["4"], "description": "Checkpoint: portal packaging suites + deploy_reliability. Depends on wave 2."},
    {"wave": 4, "tasks": ["5"], "description": "Portal deploy + live yolo_test repackage verification. Depends on wave 3."}
  ]
}
```

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Published vision models resolve and package
  - **CRITICAL**: MUST FAIL on unfixed code. DO NOT fix code or test when it fails.
  - Create `edge-cv-portal/backend/tests/test_vision_model_packaging_exploration.py` (moto `aws_stack` fixture pattern from the Defect C/F packaging tests; seed a training-jobs record shaped like the live yolo_test item: `published_component` absent/None, `published_components` list with one `{component_name: 'model-yolo-test-jetson-xavier-jp5', target: 'jetson-xavier-jp5', component_version: '6.0.0', status: 'published'}` entry)
  - Cases: resolve for archs ['arm64_jp5'] succeeds (no PackagingError) and yields the jp5 component name; the emitted model dependency is one HARD unpinned entry on that name; a record whose only published entry targets jp5 while archs=['arm64_jp6'] fails closed with a PackagingError naming the model and the uncovered arch/target (superseded by edge-deploy-reliability Defect G 2.19); a record with published entries for BOTH jp5 and jp6 under DIFFERENT names with archs spanning both emits zero model entries (divergence omission)
  - Run on UNFIXED code — EXPECTED: FAILS with today's "has no published Greengrass component" PackagingError (the live incident's error, Lambda 04:38Z workflow 6075bf76 v3)
  - Document counterexamples; mark [x] when written, run, failure documented
  - _Requirements: 1.1, 1.2 (expected 2.1-2.6)_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - vLLM resolution, fail-closed gates, and non-model paths unchanged
  - Create `edge-cv-portal/backend/tests/test_vision_model_packaging_preservation.py`
  - Observe on UNFIXED code, then encode (Hypothesis where natural): singular vLLM-shape records resolve to the same emitted entries (byte-identical); records with neither shape raise the exact existing PackagingError messages ("no published Greengrass component" / "no record in the Use_Case model registry"); empty model list → {}; TRAINING_JOBS_TABLE unset → skip+warning; plugin and LocalServer emission (incl. Defect F single-variant rule) unchanged for any inputs
  - Run on UNFIXED code — EXPECTED: PASSES
  - Mark [x] when written, run, passing
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix vision-shape model resolution in `edge-cv-portal/backend/functions/workflow_packaging.py`
  - [x] 3.1 Implement per design Fix Implementation: `resolve_model_components` gains the selected archs (update the packaging-handler call site); add `ARCH_TO_PUBLISH_TARGET` (validate target ids against greengrass_publish.py's TARGET_TO_LOCAL_SERVER keys — including what x86_64/x86_64_nvidia and arm64_jp4 map to — fail closed on unknown arch as today); resolution order: singular vLLM shape → today's path; else plural vision shape → status=='published' entries matching selected targets; else fail closed with today's message; `model_component_dependencies`: one name → today's HARD unpinned entry, multiple divergent names for one model → omit that model's entries + warning, zero names → nothing
    - vLLM emitted entries MUST be byte-identical to today (preservation)
    - _Bug_Condition: isBugCondition(package) from design_
    - _Expected_Behavior: Property 1_
    - _Preservation: Property 2_
    - _Requirements: 2.1-2.6, 3.1-3.4_
  - [x] 3.2 Re-run the SAME task 1 test — EXPECTED: PASSES (3/3 passed)
  - [x] 3.3 Re-run the SAME task 2 tests — EXPECTED: PASSES (9/9 passed)
- [x] 4. Checkpoint - Ensure all tests pass
  - `cd edge-cv-portal/backend && python3 -m pytest tests/` — no new failures beyond the repo-steering known list (workflow test-runner x10, collection-order errors, stale-workflow-registrations exploration, aravis); the Defect C/F, name-mismatch, and new vision suites must pass
  - `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/deploy_reliability/` — green
- [ ] 5. Deploy + live verification
  - Deploy the compute stack (`CDK_STACKS="EdgeCVPortalComputeStack" ./deploy_portal_fixes.sh`), verify the deployed WorkflowPackagingHandler code contains the vision-shape resolution (download + grep, as prior deploys)
  - DEPLOY PORTION DONE (2026-08-04 05:18Z): EdgeCVPortalComputeStack UPDATE_COMPLETE; downloaded the deployed WorkflowPackagingHandler code and confirmed it contains `ARCH_TO_PUBLISH_TARGET` and the `published_components` (plural) vision-shape resolution
  - REMAINING (user-gated): repackage the yolo_test workflow (6075bf76) from the portal and confirm packaging succeeds — final live verification of the regression fix. Checkbox stays unchecked until that live verification is done.
  - Ask the user to repackage the yolo_test workflow (6075bf76) from the portal and confirm packaging succeeds — final live verification of the regression fix

## Notes

- Known pre-existing failures per repo steering apply at the checkpoint.
- This spec supersedes nothing in edge-deploy-reliability; it corrects the Defect C resolver's registry-shape assumption. Emission discipline mirrors Defect F (single-name or omit).
