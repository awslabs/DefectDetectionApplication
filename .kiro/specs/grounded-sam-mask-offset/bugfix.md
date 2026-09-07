# Bugfix Requirements Document

## Introduction

Grounded-SAM Segmentation pre-labels come back with plausible, well-shaped masks that are geometrically displaced from the object they were detected on. Verified by screenshot on job `labeling-8022a9dc` (cookie images, 576×768 portrait): the green pre-label mask is a credible cookie-like blob, but it sits roughly 150–200 px too low (≈25–33 % of the image height), anchored over the cookie's lower half, spilling below it onto the tray, and clipped at the bottom image edge. Mask *shape* quality is fine — the defect is a pure geometric displacement of where the mask lands.

The displacement originates in the SAM mask pass of `edge-cv-portal/backend/grounded-sam-worker/handler.py`. The detection stage is not the suspect: Grounding DINO box accuracy was verified empirically at IoU 0.950 during the grounded-sam-autolabel 7.2 deploy, and live ObjectDetection pre-labels on the same pipeline look correct. The RLE encoding stage is also not the suspect: `_rle_encode_fast` / `mask_utils` are property-tested byte-identical to the canonical portal encoder (`dda_manifest`). What remains is the coordinate handling between the detection box and the SAM ONNX decoder — the box prompt is scaled by `1024 / max(W, H)` into the encoder's resized frame before being fed to the decoder (`_run_sam_decoder`), on the assumption that the deployed samexporter MobileSAM decoder export follows the official segment-anything ONNX convention (prompt coords pre-transformed into the resized frame). If the deployed export expects a different coordinate frame (or different `orig_im_size` semantics), the prompt lands displaced — by (scale−1) ≈ 33 % down/right on a 576×768 image if coords are double-transformed — and SAM faithfully segments at the displaced location. The exact convention must be pinned empirically against the real ONNX artifacts before the fix.

Impact: every Segmentation pre-label produced by the grounded-sam worker on non-trivially-sized images is misplaced, so labelers cannot verify masks and must redraw them — defeating the purpose of pre-labeling. ObjectDetection pre-labels are unaffected.

## Bug Analysis

### Current Behavior (Defect)

The SAM mask pass segments at a location displaced from the detection box.

1.1 WHEN the grounded-sam worker runs a Segmentation request on an image whose longest side differs from the SAM encoder input size (resize scale ≠ 1, e.g. 576×768 with scale ≈ 1.333) THEN the system returns a mask displaced from the detected object — observed ≈150–200 px downward (≈25–33 % of height) on the 576×768 job images, with the mask spilling past the object onto the background and clipping at the image edge

1.2 WHEN the returned displaced mask is compared against the object it was detected on THEN the mask-to-object IoU falls well below usable pre-label quality (mask shape plausible, position wrong), so the pre-label cannot be verified by a labeler and must be redrawn

### Expected Behavior (Correct)

2.1 WHEN the grounded-sam worker runs a Segmentation request on an image with resize scale ≠ 1 THEN the system SHALL return a mask aligned with the detected object — for a synthetic high-contrast rectangle at a known bbox, mask-to-rectangle IoU ≥ 0.85 with mask-centroid displacement < 3 % of the image diagonal, across portrait (576×768), landscape (768×576), and square (512×512) geometries

2.2 WHEN the SAM decoder prompt is constructed from a detection box THEN the system SHALL feed prompt coordinates and `orig_im_size` in the coordinate convention the deployed ONNX decoder export actually expects, pinned empirically against the real model artifacts and documented in the code

### Unchanged Behavior (Regression Prevention)

The fix is confined to the SAM decoder coordinate handling; everything around it stays as it is.

3.1 WHEN the grounded-sam worker runs an ObjectDetection request THEN the system SHALL CONTINUE TO return the same detection boxes as before (the DINO path — preprocessing, caption attribution, thresholding, NMS, clamped pixel boxes — is untouched, including the fixed-shape-export handling in `_dino_preprocess`)

3.2 WHEN the grounded-sam worker runs a Segmentation request on an image whose `_sam_preprocess` resized geometry equals the deployed export's constant-folded pad-crop shape — (new_h, new_w) = (683, 1024), i.e. 1024×683 landscape images — THEN the system SHALL CONTINUE TO return aligned masks (the baked crop is the identity exactly there; task 2 observed IoU 0.9948 with 0.2 px centroid displacement on unfixed code). *Task 2 observation-first update*: the originally hypothesized scale = 1 boundary is REFUTED — general scale = 1 images are NOT aligned on unfixed code (1024×768: IoU 0.8009, +26 px down; 768×1024: IoU 0.2939, 138 px), so they fall under the bug condition and Expected Behavior 2.1, not preservation

3.3 WHEN a thresholded mask is RLE-encoded THEN the system SHALL CONTINUE TO produce the canonical portal RLE form — `mask_utils.py` is not touched, and the drift-guard byte-identity between the two workers' `mask_utils` copies and `dda_manifest` must hold

3.4 WHEN `_sam_preprocess` prepares the encoder input THEN the system SHALL CONTINUE TO support both export conventions (rank-3 HWC unpadded for samexporter-style in-graph preprocessing, and rank-4 NCHW normalized+padded otherwise), and the existing worker pure-logic suite (`edge-cv-portal/backend/tests/test_dda_grounded_sam_worker_utils.py`, 23 tests) SHALL CONTINUE TO pass

3.5 WHEN the worker validates events, resolves models, drops empty masks, or caps detections THEN the system SHALL CONTINUE TO behave exactly as before (no contract change to the handler event/response shape)

## Bug Condition

**Bug Condition Function** — identifies inputs that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type GroundedSamRequest
  OUTPUT: boolean

  // Empirically pinned (tasks 1–2): the deployed decoder's in-graph
  // masks postprocess has its pad-crop CONSTANT-FOLDED to the export's
  // tracing shape (683, 1024) instead of derived from orig_im_size, so
  // the mask is warped whenever the resized geometry differs from that
  // baked shape. scale = 1 does NOT protect an image (task 2: 1024×768
  // IoU 0.8009, 768×1024 IoU 0.2939 on unfixed code) — only the tracing
  // geometry itself is coincidentally correct today (1024×683,
  // IoU 0.9948).
  //   scale   = encoder_size / max(W, H)            // 1024 deployed
  //   resized = (round(H·scale), round(W·scale))    // (new_h, new_w)
  RETURN X.modality = Segmentation
         AND X.detections ≠ ∅
         AND resized(X.image) ≠ (683, 1024)          // the baked pad-crop
END FUNCTION
```

**Property Specification** — defines correct behavior for buggy inputs:

```pascal
// Property: Fix Checking — masks align with the prompted object
FOR ALL X WHERE isBugCondition(X) DO
  regions ← _segment_masks'(X.image, X.detections)   // F' = fixed
  FOR EACH (detection, rle) IN regions DO
    mask ← rle_decode(rle, X.image.W, X.image.H)
    ASSERT IoU(mask, object(detection)) ≥ 0.85
    ASSERT |centroid(mask) − centroid(object(detection))|
           < 0.03 × diagonal(X.image)
  END FOR
END FOR
```

**Preservation Goal**:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // Concretely: ObjectDetection responses byte-equivalent;
  // traced-crop-geometry (resized = (683, 1024), e.g. 1024×683)
  // Segmentation masks unchanged (the one aligned geometry); RLE
  // encoding, event validation, model resolution, empty-mask dropping,
  // and the pure-logic suite all unchanged.
END FOR
```

**Counterexample** (observed live, job `labeling-8022a9dc`): a 576×768 cookie image with a correct `cookie_gap` detection box produces a well-shaped mask displaced ≈150–200 px downward, overlapping the cookie's lower half and the tray below it. The exploration test reproduces this with a synthetic 576×768 rectangle image and records the measured IoU and centroid displacement as the counterexample numbers. Task 2 added the scale = 1 counterexamples that broadened the condition (1024×768: IoU 0.8009; 768×1024: IoU 0.2939) to the exploration test's expected-fail set.
