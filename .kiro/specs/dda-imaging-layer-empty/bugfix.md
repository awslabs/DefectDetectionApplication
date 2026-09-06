# Bugfix Requirements Document

## Introduction

The `llm-model-token-and-image-sizing` feature added an `ImagingLayer` LayerVersion to the ComputeStack (`edge-cv-portal/infrastructure/lib/compute-stack.ts`, ~line 1805) that ships Pillow to the three DDA labeling functions: `DdaLabelingWorker`, `DdaLabelingHandler` (which also runs the async Prompt_Tuning_Preview executor via self-invoke), and `DdaAutolabelWorker`. The layer is packaged with a raw `lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/imaging'))`, which silently depends on `backend/layers/imaging/build.sh` having been run manually beforehand to create the `python/` directory with the Pillow install — and `edge-cv-portal/deploy-infrastructure.sh` never runs it. On this host the directory held only `build.sh` + `requirements.txt` at deploy time (2026-09-06, log `edge-cv-portal/deploy-infra-llm-token-image-sizing-20260906T013238Z.out`), so the layer shipped empty: the deployed cdk.out asset `98f3a4d8ab2dddc0be7b06f4df3eb8ff53523dd9b4c7c616e31f105fc9d3ce65` contains exactly those two files, no PIL.

Runtime symptom: a Prompt Tuning Preview run reported "completed" while every per-image result carried a red "Model error" badge with `model error: No module named 'PIL'` and "dimensions unavailable". The error is per-image because `edge-cv-portal/backend/layers/shared/python/dda_llm_image.py` deliberately imports Pillow lazily inside `_import_pillow_image()` (module contract: no Pillow at import time), so the ImportError surfaces inside the per-image model-call path where the executor catches and reports it, not at Lambda cold start. The existing infra test (`llm-model-token-and-image-sizing-infra.test.ts`) asserts the layer's existence, runtimes, description, and attach sites — nothing asserts the asset content, so synth and deploy succeed with an empty layer.

The identical bug in the SyntheticDataStack's `SyntheticImagingLayer` (same source directory) was fixed with CDK asset bundling in the `synthetic-imaging-layer-empty` spec, cherry-picked onto this branch as commit ea528a3. This fix mirrors that bundling on the compute `ImagingLayer`. That half-applied state also broke the pinned cross-stack test 'SyntheticImagingLayer is unchanged and still bundles its own copy of the imaging asset' (synthetic asset now bundled, compute asset still raw — S3Keys diverge; 1 failed / 124 passed); applying identical bundling to the compute layer restores it without editing the test. The live incident has already been mitigated operationally (manual `build.sh` + redeploy); this fix removes the host-state dependence permanently.

## Bug Analysis

### Current Behavior (Defect)

When the imaging layer asset is staged verbatim from the raw source directory, its content is whatever the deploy host happens to contain, and nothing at synth, deploy, or test time guards against an empty layer.

1.1 WHEN the ComputeStack is synthesized THEN the system stages the `ImagingLayer` asset as a verbatim copy of the raw `backend/layers/imaging/` directory — build tooling (`build.sh`, `requirements.txt`) staged as layer content, and `python/PIL` present only if `build.sh` happened to have been run on that host — and synth succeeds regardless
1.2 WHEN `deploy-infrastructure.sh` deploys the ComputeStack from a host where `build.sh` was never run THEN the system deploys an empty imaging layer (deployed asset `98f3a4d8...` contains exactly `build.sh` and `requirements.txt`, no PIL) and the deploy succeeds anyway
1.3 WHEN a deployed DDA labeling function reaches a Pillow code path (e.g. the Prompt_Tuning_Preview executor's per-image Image_Downscaler / dimension probe via `_import_pillow_image()`) with the empty layer attached THEN the system fails each image with `model error: No module named 'PIL'` ("Model error" badge, "dimensions unavailable") while the preview run itself reports "completed"
1.4 WHEN the infrastructure jest suite runs on this branch (synthetic layer bundled by cherry-pick ea528a3, compute layer still raw) THEN the pinned test 'SyntheticImagingLayer is unchanged and still bundles its own copy of the imaging asset' fails on the `Content.S3Key` equality assertion (1 failed / 124 passed)

### Expected Behavior (Correct)

2.1 WHEN the ComputeStack is synthesized THEN the system SHALL bundle the `ImagingLayer` asset at synth time (pip install of `requirements.txt` into `python/`), so that the staged asset always contains `python/PIL` and no staged build tooling, regardless of whether `build.sh` was run on the host
2.2 WHEN a deployed DDA labeling function reaches a Pillow code path with the bundled imaging layer attached THEN the system SHALL import PIL successfully and complete the imaging operation (per-image preview results report dimensions instead of module-not-found model errors)
2.3 WHEN the ComputeStack and SyntheticDataStack are synthesized in the same app THEN the system SHALL produce the identical bundled imaging asset for both layers (equal `Content.S3Key`), restoring the pinned cross-stack test to green without modifying it

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the ComputeStack is synthesized THEN the system SHALL CONTINUE TO attach the single `ImagingLayer` LayerVersion to exactly `DdaLabelingWorker`, `DdaLabelingHandler`, and `DdaAutolabelWorker` as their second layer after `SharedLayer`, with each function's configuration unchanged (runtime python3.11; handlers `dda_labeling_worker.handler` / `dda_labeling.handler` / `dda_autolabel_worker.handler`; timeouts 900/900/300 s; 2048 MB each; environment wiring unchanged)
3.2 WHEN the ComputeStack is synthesized THEN the system SHALL CONTINUE TO produce the `ImagingLayer` with its existing metadata (description `Pillow imaging layer for DDA labeling mask rendering (built by backend/layers/imaging/build.sh)`, compatible runtime python3.11 — both pinned by an existing passing test) and to stage all other compute-stack layer assets (e.g. `SharedLayer`) exactly as before
3.3 WHEN the SyntheticDataStack is synthesized THEN the system SHALL CONTINUE TO bundle its `SyntheticImagingLayer` exactly as the cherry-picked fix left it (`synthetic-data-stack.ts` untouched by this fix)
3.4 WHEN the full infrastructure jest suite runs after the fix THEN the system SHALL CONTINUE TO pass all 124 currently-passing tests with no existing test file modified, and with the restored pinned test the pre-existing suite SHALL be green at 125/125
3.5 WHEN `build.sh` is run manually in `backend/layers/imaging/` THEN the system SHALL CONTINUE TO produce a `python/` directory with Pillow for manual/standalone layer builds (`build.sh` unchanged)
