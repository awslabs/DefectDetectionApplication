# Bugfix Requirements Document

## Introduction

Portal synthetic data generation with `stability.stable-image-inpaint-v1:0` fails at generation time with `No module named 'PIL'` (generation session 6525c09c recorded a generation failure and landed in `awaiting_review`). The `dda-synthetic-data-handler` Lambda imports PIL (`edge-cv-portal/backend/functions/synthetic_data.py`, `_render_mask_png` mask synthesis and the image decode/diff paths), and expects Pillow to come from the `SyntheticImagingLayer` Lambda layer. That layer's CDK asset is `edge-cv-portal/backend/layers/imaging/`, which contains only `build.sh` and `requirements.txt` — the `build.sh` step that pip-installs Pillow into a `python/` directory was never run before `cdk deploy`, so the deployed layer version (`arn:aws:lambda:us-east-1:164152369890:layer:SyntheticImagingLayer6E5D81DF:1`) is empty and the runtime import fails. The deploy pipeline has no guard: an empty layer asset synthesizes and deploys successfully, and the failure only surfaces at Lambda runtime.

## Bug Analysis

### Current Behavior (Defect)

When the imaging layer asset directory contains no `python/PIL` content at synth time, the deploy succeeds but the handler fails at runtime.

1.1 WHEN the SyntheticDataStack is synthesized and the imaging layer asset directory (`backend/layers/imaging/`) contains no `python/PIL` content THEN the system stages an empty layer asset (only `build.sh` and `requirements.txt`) and the deploy succeeds anyway
1.2 WHEN the deployed `dda-synthetic-data-handler` executes a code path that imports PIL (e.g. `_render_mask_png` during stability inpaint generation) with the empty imaging layer attached THEN the system fails with `No module named 'PIL'` and records a generation failure on the session

### Expected Behavior (Correct)

2.1 WHEN the SyntheticDataStack is synthesized THEN the system SHALL bundle the imaging layer asset so that it always contains `python/PIL` (Pillow installed from `requirements.txt`), regardless of whether `build.sh` was run manually beforehand
2.2 WHEN the deployed `dda-synthetic-data-handler` executes a code path that imports PIL with the bundled imaging layer attached THEN the system SHALL import PIL successfully and complete the imaging operation without a module-not-found error

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the SyntheticDataStack is synthesized THEN the system SHALL CONTINUE TO stage the shared and JWT layer assets exactly as before (verbatim copies of their source directories, unaffected by the imaging layer bundling)
3.2 WHEN the SyntheticDataStack is synthesized THEN the system SHALL CONTINUE TO attach exactly three layers (shared, JWT, imaging) to the `dda-synthetic-data-handler` function with its configuration unchanged (runtime python3.11, handler `synthetic_data.handler`, 1024 MB, 15 min timeout, environment variables, IAM role grants, API routes)
3.3 WHEN the existing infrastructure test suite runs THEN the system SHALL CONTINUE TO pass all 94 existing tests
3.4 WHEN `build.sh` is run manually in `backend/layers/imaging/` THEN the system SHALL CONTINUE TO produce a `python/` directory with Pillow for manual layer builds
