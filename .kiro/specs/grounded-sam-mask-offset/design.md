# Grounded-SAM Mask Offset Bugfix Design

## Overview

Grounded-SAM Segmentation pre-labels return well-shaped masks displaced from the detected object (screenshot evidence on job `labeling-8022a9dc`, 576×768 cookie images: ≈150–200 px downward, ≈25–33 % of height). The detection boxes are correct (DINO verified at IoU 0.950 in the 7.2 deploy; live ObjectDetection pre-labels align) and the RLE encoding is correct (property-tested byte-identical to the canonical portal encoder), so the defect sits in the coordinate handling between the detection box and the SAM ONNX decoder in `edge-cv-portal/backend/grounded-sam-worker/handler.py`.

The fix strategy is empirical-first: an exploration test runs the handler's real `_segment_masks` against the **real deployed MobileSAM ONNX artifacts** (the `mobile_sam_20230629.zip` samexporter export the Dockerfile bakes in) with a synthetic image whose ground truth is exactly known, measures the displacement, and probes the candidate coordinate conventions side by side to pin the one the export actually expects. The fix then changes only the SAM decoder coordinate handling in `_run_sam_decoder` (and, only if implicated, the `orig_im_size` feed) to match the empirically pinned convention, documents the convention with the probe numbers in code comments, and leaves the DINO path, `_sam_preprocess`'s two export conventions, RLE encoding, and the handler contract untouched. Delivery is a worker-image rebuild + CDK redeploy followed by quantitative live verification on real cookie images.

## Glossary

- **Bug_Condition (C)**: Segmentation request with detections on an image whose `_sam_preprocess` resized geometry (new_h, new_w) differs from the export's constant-folded pad-crop shape (683, 1024) — the mask comes back displaced/stretched. *Task 2 update*: scale = 1 does NOT protect an image; only the tracing geometry (1024×683) is spared
- **Property (P)**: masks align with the prompted object — IoU ≥ 0.85 and centroid displacement < 3 % of the image diagonal on known-ground-truth synthetic images
- **Preservation**: ObjectDetection responses, traced-crop-geometry (resized (683, 1024), e.g. 1024×683) Segmentation alignment, RLE encoding, event validation, and the pure-logic suite are unchanged by the fix
- **`_sam_preprocess`**: handler function that resizes the longest image side to the encoder resolution (1024 for the deployed export) and returns the encoder tensor plus `scale = 1024 / max(W, H)`; supports rank-3 HWC unpadded (samexporter in-graph preprocessing) and rank-4 NCHW normalized+padded exports
- **`_run_sam_decoder`**: handler function that builds the decoder feed for one detection box — the box as the canonical two-point encoding (top-left label 2, bottom-right label 3), coords multiplied by `scale`, `orig_im_size = [H, W]` float32 — and returns masks expected at source resolution
- **`_segment_masks`**: handler function that embeds the image once, decodes per box, thresholds logits at 0.0, drops empty masks, and RLE-encodes at source resolution
- **samexporter export**: the MobileSAM ONNX bundle from `https://huggingface.co/vietanhdev/segment-anything-onnx-models/resolve/main/mobile_sam_20230629.zip` (~40 MB) — encoder takes rank-3 HWC resized image and normalizes/pads in-graph; decoder takes `image_embeddings, point_coords, point_labels, mask_input, has_mask_input, orig_im_size`
- **Official ONNX convention**: the segment-anything reference decoder export expects `point_coords` pre-transformed into the 1024-longest-side resized frame and `orig_im_size = [H, W]` of the true original image; its in-graph `resize_longest_image_size` crop removes the encoder padding before interpolating masks back to `orig_im_size`
- **sam-worker**: the older interactive/proposal SAM worker (`edge-cv-portal/backend/sam-worker/handler.py`), in production longer; ships the same MobileSAM archive and pre-scales prompts identically, but prompts with single points (label 1 + padding point −1) from a coarse grid rather than box two-point prompts

## Bug Details

### Bug Condition

The bug manifests when the SAM mask pass runs on a resized image: the decoder prompt derived from a correct detection box lands displaced in the mask model's coordinate space, so SAM segments a plausible object-shaped region at the wrong location. The handler assumes the official segment-anything ONNX convention (coords pre-scaled into the 1024 resized frame); if the deployed samexporter export expects a different frame — or different `orig_im_size` semantics — the prompt is systematically displaced. A coords double-transform displaces prompts down/right by (scale−1)·position ≈ 33 % on a 576×768 image, matching the screenshot.

**Empirical update (tasks 1–2)**: the probe refuted the coords hypotheses — the handler's coords×scale feed is correct (canvas-frame control: masks land exactly on the prompt). The defect is the export's in-graph masks postprocess: its pad-crop is CONSTANT-FOLDED to the tracing shape (683, 1024) instead of derived from `orig_im_size`. The bug condition is therefore broader than `scale ≠ 1`: any resized geometry ≠ (683, 1024) misaligns — task 2 measured scale = 1 counterexamples 1024×768 (IoU 0.8009, +26 px) and 768×1024 (IoU 0.2939, 138 px) — and only the tracing geometry 1024×683 is spared (IoU 0.9948 on unfixed code).

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type GroundedSamRequest
  OUTPUT: boolean

  // scale   = encoderSize / max(input.image.W, input.image.H)
  // resized = (round(H·scale), round(W·scale))   // _sam_preprocess
  RETURN input.modality = Segmentation
         AND input.detections ≠ ∅
         AND resized(input.image) ≠ (683, 1024)   // export's baked pad-crop
END FUNCTION
```

### Examples

- **Live counterexample**: 576×768 portrait cookie image, correct `cookie_gap` box → mask displaced ≈150–200 px downward (≈25–33 % of height), anchored over the cookie's lower half, spilling onto the tray, clipped at the bottom edge; shape plausible, position wrong
- **Synthetic portrait (576×768, scale ≈ 1.333)**: bright rectangle at a known off-center bbox on dark background, detection box = rectangle bbox → expected on unfixed code: IoU(mask, rectangle) well below 0.5 with measurable down/right centroid displacement
- **Synthetic landscape (768×576)** and **square (512×512, scale = 2.0)**: same construction — the displacement should track (scale−1)·position if the double-transform hypothesis holds, confirming the convention generalizes across geometries
- **Edge case — traced-crop geometry (1024×683, resized shape = the baked crop (683, 1024))**: the constant-folded pad-crop is the identity exactly there, so masks align on unfixed code (task 2: IoU 0.9948, 0.2 px) — the true preservation boundary. General scale = 1 images (1024×768, 768×1024) are NOT spared (task 2: IoU 0.8009 / 0.2939) and belong to the bug condition — reclassified into the exploration expected-fail set

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- ObjectDetection requests: identical responses (DINO preprocessing, caption attribution, thresholds, NMS, clamped boxes, fixed-shape-export handling — all untouched)
- Segmentation on the traced-crop geometry (1024×683 → resized (683, 1024) = the export's baked pad-crop): masks already align today and must continue to (observation-first baseline against the real models; general scale = 1 images misalign on unfixed code and are bug-condition inputs, not preservation — task 2)
- RLE encoding: `mask_utils.py` untouched; the drift-guard byte-identity between the two workers' `mask_utils` copies and `dda_manifest` holds; `_rle_encode_fast` unchanged
- `_sam_preprocess`: both export conventions (HWC unpadded / NCHW normalized+padded) keep working; `scale` semantics unchanged for the encoder input
- Handler contract: event validation, model resolution/caching, empty-mask dropping, detection capping, response shape — all unchanged
- The existing pure-logic suite `edge-cv-portal/backend/tests/test_dda_grounded_sam_worker_utils.py` (23 tests, no onnxruntime) stays green

**Scope:**
All inputs that do NOT reach the SAM decoder with a non-traced-crop resized geometry are completely unaffected: ObjectDetection requests, empty-detection Segmentation requests (no decoder call), malformed events (rejected before model import), and Segmentation requests whose resized geometry equals the baked crop (683, 1024) (the constant-folded postprocess is the identity there).

## Hypothesized Root Cause

The handler's `_run_sam_decoder` assumes the official segment-anything ONNX decoder convention. The deployed decoder is the samexporter MobileSAM export, and the offset evidence says some assumption doesn't hold for it. Candidates, in order of suspicion:

1. **H1 — decoder expects original-frame coords (double-transform)**: if the export bakes the coordinate resize into the graph (deriving it from `orig_im_size`), the handler's `coord * scale` is applied twice → prompts displaced down/right by (scale−1)·position ≈ 33 % on 576×768 — the best quantitative match for the screenshot. 
   - Evidence to weigh carefully: the sibling sam-worker pre-scales identically against the same archive and has been in production longer. But its prompts are single grid points filtered by predicted-IoU and NMS, and its proposals are class-agnostic — a displaced point still lands on/near large objects and a displaced-but-plausible proposal has no ground-truth box to betray it. sam-worker "looking fine" is therefore weak evidence against H1; the probe matrix settles it.
2. **H2 — `orig_im_size` semantics mismatch**: samexporter's own inference feeds `orig_im_size` = its fixed resized canvas (e.g. `(684, 1024)`) and inverse-warps masks outside the graph, never the true original size. If the export's post-processing (`resize_longest_image_size` pad-crop + interpolation) misbehaves when fed the true original size, the mask is cropped/interpolated from the wrong region — a geometric displacement/stretch.
3. **H3 — `[H, W]` vs `[W, H]` order**: unlikely as the sole cause — for a non-square image a swapped `orig_im_size` shears/garbles the mask through `reshape(-1, H, W)` rather than cleanly translating it, and the screenshot mask is clean — but cheap to probe alongside the others.
4. **H4 — encoder in-graph padding convention**: if the encoder letterboxes centered instead of top-left-anchored, content sits offset relative to the decoder's assumed crop. For portrait images padding is horizontal, which predicts a rightward (not downward) offset — secondary, but the probe's landscape case would expose it.

The exploration test resolves the ambiguity empirically by running the decoder variants side by side against the real artifacts; the fix implements whichever convention wins, with the probe numbers recorded in the code comment.

## Correctness Properties

Property 1: Bug Condition - Segmentation Masks Align With The Prompted Box

_For any_ Segmentation request where the bug condition holds (isBugCondition returns true — detections present, resized geometry ≠ the export's baked pad-crop (683, 1024)), the fixed `_segment_masks` SHALL return masks aligned with the prompted object: for a synthetic high-contrast rectangle whose detection box equals the rectangle bbox, the decoded mask achieves IoU ≥ 0.85 against the rectangle with centroid displacement < 3 % of the image diagonal, across portrait 576×768, landscape 768×576, and square 512×512 geometries — plus the scale = 1 geometries 1024×768 and 768×1024 reclassified by task 2 — against the real MobileSAM ONNX artifacts.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Non-Bug Inputs Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns false — ObjectDetection requests, empty-detection or traced-crop-geometry (resized = (683, 1024)) Segmentation requests, malformed events), the fixed code SHALL produce the same result as the original code: identical ObjectDetection responses, traced-crop-geometry masks still aligned (observed-first baseline: 1024×683 IoU 0.9948 on unfixed code), unchanged RLE encoding and handler validation, and the existing 23-test pure-logic suite passing unmodified.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming the probe pins the convention (any hypothesis):

**File**: `edge-cv-portal/backend/grounded-sam-worker/handler.py`

**Function**: `_run_sam_decoder` (only; `_segment_masks` only if `orig_im_size` semantics are implicated)

**Specific Changes**:
1. **Coordinate transform**: feed `point_coords` in the empirically pinned frame (e.g. drop the `* scale` if H1 wins; keep it if H2/H4 win) — minimal diff, no new abstractions
2. **`orig_im_size` feed**: adjust only if the probe implicates it (H2/H3); if the samexporter-native convention wins (resized-frame `orig_im_size`), resize the returned mask to source resolution before thresholding, keeping the response contract at source resolution
3. **Convention documentation**: record the pinned convention and the probe evidence (IoU per variant per geometry) in the `_run_sam_decoder` docstring/comment so the next reader doesn't have to re-derive it
4. **Untouched**: `_sam_preprocess` (both export conventions and `scale` for the encoder input), the DINO path including fixed-shape-export handling, `mask_utils.py` (drift-guard byte-identity), `_rle_encode_fast`, event validation, model resolution, empty-mask dropping, response shape
5. **sam-worker**: out of scope for the code fix (different prompt style, separate deploy); the probe outcome is recorded so a follow-up can assess whether `sam-worker/handler.py::_run_decoder` shares the defect

### Delivery

Worker image rebuild + redeploy via `EdgeCVPortalComputeStack` with the **mandatory** `-c deployGroundedSamWorker=true` context flag (without it CDK deletes the live worker). Docker layers are cached so only the handler COPY layer rebuilds (~5–10 min). Live verification is quantitative, by direct invoke of the redeployed worker on real cookie images (the job's rerun API is ineligible — 0 Failed tasks).

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples on the UNFIXED code with the real ONNX artifacts and pin the correct coordinate convention empirically (exploration probe matrix); then verify the fix restores alignment on all geometries and that everything outside the bug condition is byte-preserved.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating the offset BEFORE the fix, and pin the decoder's coordinate convention empirically. Confirm or refute H1–H4; if all are refuted, re-hypothesize.

**Test Plan**: New `edge-cv-portal/backend/tests/test_gsam_mask_offset_exploration.py` running the handler's real `_segment_masks` against the real MobileSAM ONNX artifacts (downloaded/cached to `/tmp/gsam-models`, ~40 MB; requires onnxruntime + numpy + Pillow — all present on this host: onnxruntime 1.19.2, numpy 2.0.2). Synthetic image: bright rectangle at a known off-center bbox on a dark background; detection box = rectangle bbox; decode the returned RLE with `dda_manifest.rle_decode` and compute IoU(mask, rectangle) plus the centroid displacement vector.

**Test Cases**:
1. **Portrait 576×768** (the incident geometry): assert Property 1 thresholds (will fail on unfixed code — the counterexample; record IoU and displacement vector)
2. **Landscape 768×576**: same assertion (will fail on unfixed code; exposes H4's horizontal-offset prediction)
3. **Square 512×512, scale = 2.0**: same assertion (will fail on unfixed code; displacement magnitude discriminates H1's (scale−1)·position prediction)
4. **Probe matrix (diagnostic, not an assertion)**: run the decoder directly on the same embedding with (a) original-frame coords unscaled, (b) 1024-frame coords (current behavior), (c) swapped `orig_im_size` `[W, H]`, (d) samexporter-native resized-frame `orig_im_size` `[new_h, new_w]` with external mask resize — report IoU per variant per geometry to pin the convention

**Expected Counterexamples**:
- Variant (b) (current): IoU well below 0.5 with down/right centroid displacement ≈ (scale−1)·position on 576×768
- Exactly one variant should score IoU ≥ 0.85 on all three geometries — that variant is the convention the fix implements

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces aligned masks.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  regions := _segment_masks_fixed(input.image, input.detections)
  FOR EACH (detection, rle) IN regions DO
    ASSERT IoU(rle_decode(rle), object(detection)) >= 0.85
    ASSERT |centroid(mask) - centroid(object)| < 0.03 * diagonal(input.image)
  END FOR
END FOR
```

The exploration test's three geometry assertions ARE the fix check — the same test flips from failing to passing after the fix, with no edits to the test.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT handler_original(input) = handler_fixed(input)
END FOR
```

**Testing Approach**: The existing property-based pure-logic suite (Hypothesis) already covers the validation/RLE/selection domains exhaustively and must pass unmodified. The real-model preservation boundary (the traced-crop geometry, resized = (683, 1024) — task 2 refuted the originally hypothesized scale = 1 boundary) follows observation-first: observe alignment on UNFIXED code, encode it, verify it still holds after the fix.

**Test Cases**:
1. **Traced-crop alignment** (task 2 observation-first outcome): 1024×683 synthetic rectangle image (resized shape = the baked crop (683, 1024)) on UNFIXED code — observed IoU 0.9948 / 0.2 px, encoded as a passing test in `test_gsam_mask_offset_preservation.py`, re-run after fix. The originally planned general scale = 1 case MISALIGNS on unfixed code (1024×768 IoU 0.8009, 768×1024 IoU 0.2939) and moved to the exploration expected-fail set
2. **Pure-logic suite**: `test_dda_grounded_sam_worker_utils.py` (23 tests incl. the mask_utils drift guard) green before and after
3. **ObjectDetection path**: by inspection the fix touches only `_run_sam_decoder`/`_segment_masks`, which the OD path never calls; the suite's validation/selection property tests cover the shared logic

### Unit Tests

- Exploration/fix assertions on the five geometries (real ONNX artifacts, deterministic synthetic images; incl. the two task-2 reclassified scale = 1 cases)
- Traced-crop-geometry (1024×683) preservation case (real ONNX artifacts)
- Existing 23-test pure-logic suite re-run (validation, caption attribution, selection, RLE, drift guard)

### Property-Based Tests

- Existing Hypothesis suites in `test_dda_grounded_sam_worker_utils.py` (RLE round-trip vs `dda_manifest`, selection invariants, validation) — must stay green, unmodified
- The exploration test is a scoped-PBT: the property (IoU/centroid thresholds) quantified over the three concrete geometries for deterministic reproducibility against the real models

### Integration Tests

- Local end-to-end: exploration test green post-fix against the real artifacts (the full `_segment_masks` path: encoder → decoder → threshold → RLE)
- Post-deploy live verification: direct invoke of the redeployed worker on 2–3 real cookie images (presigned from `s3://ryvan-cookies/training-images/`), Segmentation + ObjectDetection runs of the same payload; assert the mask centroid sits inside the DINO box (or box–mask containment ≥ 0.5); before/after numbers recorded in `verification-notes.md`
