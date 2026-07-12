# Implementation Plan

## Overview

This plan finishes the visualization and surfacing work for object detection, which already flows end-to-end by reusing the anomaly-classification tensor contract. Work proceeds bottom-up so each layer is testable before the next depends on it: shared label-map util → Base_Model detection-tensor builder (sentinel + labels) → Marshal (detection typing, count/confidence, overlay ref relocation, Detections_Block, overlay drawing) → portal-backend captures endpoint → portal Results_Viewer. Property-based tests (hypothesis + pytest, min 100 iterations) sit alongside each backend change and cover all 9 correctness properties; the frontend `scaleBox` helper is covered by a fast-check property test and the viewer by vitest component tests. Every backend change is confined to hot-patchable model-template files and the shared util, so no Triton or ensemble rebuild is required, and the anomaly path stays byte-for-byte compatible. Device build/publish is handled separately and is out of scope for these coding tasks.

## Tasks

- [x] 1. Create shared Class_Label_Map util
  - [x] 1.1 Implement `class_label_map.py`
    - Create `src/backend/lyra_science_processing_utils/utils/class_label_map.py`
    - Define `COCO_CLASS_LABELS: dict[int, str]` (canonical COCO index → name)
    - Implement `resolve_class_label(class_index, class_map=None) -> str`: return the mapped label when the (provided or default COCO) map has an entry, otherwise `str(class_index)`; accept int or numeric-string indices; never raise (non-numeric/missing index falls back to the string form)
    - _Requirements: 3.2, 3.3_

  - [x]* 1.2 Write property test for label resolution
    - **Property 5: Class-label resolution falls back to the index string**
    - **Validates: Requirements 3.2, 3.3**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 5: ...`
    - Generate random indices (int and numeric-string) and random maps; assert mapped name when present else `str(index)`; assert it never raises for non-numeric/missing indices

- [x] 2. Base_Model detection-tensor builder (labels + zero-object sentinel)
  - [x] 2.1 Add manifest class-name loader and embed resolved labels
    - In `src/backend/dda_triton/resources_for_copy/lfv_model_template.py`, add private `__load_class_names(self, model_dir) -> dict | None` following the existing `__load_task` pattern (manifest `class_names` / `dataset.class_names` if present, else `None`; on `OSError`/`ValueError` log a warning and return `None`)
    - In `__build_detection_tensors`, resolve a human-readable `class_label` per detection via the shared `resolve_class_label` using the loaded map, embedding `class_label` alongside the retained numeric `class` (index) in each serialized detection
    - Keep the tensor signature/set unchanged (`output, output_confidence, output_score, mask, anomalies`)
    - _Requirements: 3.1, 3.5, 5.1_

  - [x] 2.2 Emit the zero-object sentinel
    - In `__build_detection_tensors`, when there are no real detections, serialize the sentinel list `[{"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": true}]` into the `anomalies` tensor instead of `[]`
    - Preserve existing `output`/`output_confidence`/`output_score` behavior for the non-empty case
    - _Requirements: 1.1, 2.5, 5.5_

  - [x]* 2.3 Write property test for missing-task default
    - **Property 9: Missing task defaults to anomaly**
    - **Validates: Requirements 5.2**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 9: ...`
    - Generate random manifests that omit the `task` field; assert `__load_task` resolves to the anomaly task

  - [x]* 2.4 Write smoke test for detection output tensor set
    - Smoke test asserting the Base_Model detection path emits exactly the tensor names `{output, output_confidence, output_score, mask, anomalies}` (no added/removed tensors)
    - _Requirements: 5.1_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Marshal detection classification, count, and confidence
  - [x] 4.1 Type detection captures distinctly in `_generate_capture_meta_data`
    - In `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py`, when `_is_detection_list(inference_anomalies)` is true, set `inf_result["Inference result"] = "Detection"` and set `class-name`/`anomaly-label-detected` to the detection form; never set `"Anomaly"`/`"Normal"` for detection captures
    - Ensure `_is_detection_list` treats the zero-object sentinel (non-empty list, dict entry with a `bounding_box` field) as a detection
    - Leave the anomaly branch (payload with no `bounding_box` field) producing existing metadata unchanged
    - _Requirements: 1.1, 1.2, 1.3, 1.7, 5.5_

  - [x] 4.2 Populate detection count and top-confidence
    - Add `inf_result["Detection_count"]` = number of **valid** detections (entries with a 4-element `bounding_box`; sentinel yields 0)
    - When count ≥ 1, set the capture `Confidence` to the max object confidence; when count is 0, report `0.0`
    - _Requirements: 1.4, 1.5_

  - [x]* 4.3 Write property test for payload discrimination
    - **Property 1: Detection payloads are distinguished solely by a bounding_box field**
    - **Validates: Requirements 1.1, 5.5**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 1: ...`
    - Generate anomaly-shaped and detection-shaped payloads (including the sentinel); assert classification is a detection iff the payload is a non-empty list whose entries carry a `bounding_box` field

  - [x]* 4.4 Write property test for detection typing
    - **Property 2: Detection captures receive a distinct inference-result type**
    - **Validates: Requirements 1.2, 1.3**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 2: ...`
    - For any detection payload, assert `Inference result == "Detection"` and never `"Anomaly"`/`"Normal"`

  - [x]* 4.5 Write property test for count and top confidence
    - **Property 3: Detection count and top confidence are reported**
    - **Validates: Requirements 1.4, 1.5**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 3: ...`
    - Generate random object lists (including sentinel); assert `Detection_count` equals the valid-box count and, for non-empty payloads, capture confidence equals the max object confidence

- [x] 5. Marshal Detections_Block and overlay reference
  - [x] 5.1 Emit the Detections_Block with index and label
    - In `_generate_capture_meta_data`, for each **valid** detection emit `{ "class_index": <original>, "class_label": <resolved>, "bounding_box": [...], "confidence": <f> }` into `deviceFleetAuxiliaryOutputs`; filter the sentinel so the block reflects only real objects
    - Consume `class_label` from the payload; if missing/empty, re-resolve via the shared `resolve_class_label` and finally fall back to the `Class_Index` string
    - _Requirements: 1.6, 3.1, 3.5_

  - [x] 5.2 Relocate the overlay Auxiliary_Output_Reference
    - Compute `overlay_present = is_detection OR self._has_anomaly_mask(...)` and append the overlay `data-ref` (`file://{capture_folder}/{capture_id}.overlay.jpg`) whenever `overlay_present`, moving it out of the anomaly-mask-only branch
    - Only emit the ref when overlay bytes were successfully produced (omit on encode failure to avoid a dangling ref); keep the anomaly mask ref/metadata exactly as today
    - _Requirements: 2.3, 2.4, 2.6_

  - [x]* 5.3 Write property test for the Detections_Block
    - **Property 4: The Detections_Block is emitted and retains index plus label**
    - **Validates: Requirements 1.6, 3.1, 3.5**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 4: ...`
    - For any detection capture, assert the block is present and every entry retains the original `class_index` and includes a `class_label`

  - [x]* 5.4 Write property test for anomaly backward-compatibility
    - **Property 7: Anomaly-classification behavior is unchanged**
    - **Validates: Requirements 1.7, 2.6, 5.3, 5.4**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 7: ...`
    - Model-based/equivalence test: for any payload with no `bounding_box` field, assert the Marshal metadata and auxiliary outputs (including the mask-based overlay ref) equal a captured legacy baseline

- [x] 6. Marshal detection overlay drawing
  - [x] 6.1 Draw resolved labels and produce a source-sized overlay
    - In `_generate_detection_overlay`, draw each valid box with its resolved `class_label` (not the numeric index) plus confidence; skip entries without a 4-element box (including the sentinel) so a zero-object capture yields an **unannotated** copy of the source image
    - Ensure `execute` runs the overlay path for detection captures (including zero-object via the sentinel) so a non-empty `overlay` tensor is always produced for the gstreamer plugin to write; count valid boxes only
    - _Requirements: 2.1, 2.5, 3.4_

  - [x]* 6.2 Write property test for the overlay reference and dimensions
    - **Property 6: Detection captures always get an overlay reference of source dimensions**
    - **Validates: Requirements 2.1, 2.3, 2.4, 2.5**
    - hypothesis + pytest, min 100 iterations; tag `# Feature: object-detection-visualization, Property 6: ...`
    - For any detection capture with an empty mask (including sentinel), assert the overlay ref is present and the generated overlay dims equal the source dims (unannotated when no objects)

  - [x]* 6.3 Write example and edge tests for overlay drawing
    - Example: a single labeled box is drawn with `class_label` + confidence on the overlay (3.4); one-object end-to-end metadata example
    - Edge: zero-object sentinel → unannotated overlay, count 0, ref present, `Inference result == "Detection"`
    - _Requirements: 2.5, 3.4_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Portal backend captures endpoint
  - [x] 8.1 Implement the captures endpoint
    - In `edge-cv-portal/backend/functions/`, add a captures handler mirroring `datasets.get_image_preview`'s presigned-URL pattern
    - Given usecase/device and capture prefix in the inference-results bucket: list capture artifacts, parse the results `.jsonl` `Capture_Metadata`, and return per capture `inference_result_type`, `detection_count`, the parsed `Detections_Block`, and presigned URLs for the source image, `overlay.jpg`, and `mask.png`
    - Tolerate missing artifacts (return `null` URLs) and a missing/parse-failed block (return empty detections); log and skip presigned-URL failures
    - _Requirements: 4.1, 4.2, 4.3, 4.5_

  - [x]* 8.2 Write unit tests for the captures endpoint
    - Test metadata parsing for detection, zero-object, and anomaly captures; presigned URL assembly; and graceful handling of missing artifacts / unparseable metadata
    - _Requirements: 4.3, 4.5_

- [x] 9. Portal frontend Results_Viewer
  - [x] 9.1 Implement the pure `scaleBox` helper
    - Add a framework-free `scaleBox(box, src, disp)` helper (e.g. under `edge-cv-portal/frontend/src/`) returning `{ x, y, w, h }` scaled by the width/height ratios; guard against zero/undefined natural dimensions (no-op)
    - _Requirements: 4.6_

  - [x]* 9.2 Write fast-check property test for `scaleBox`
    - **Property 8: Box coordinates scale proportionally to the displayed image**
    - **Validates: Requirements 4.6**
    - fast-check + vitest, min 100 runs; tag `// Feature: object-detection-visualization, Property 8: ...`
    - Assert coordinates scale by the width/height ratios; identity when `src == disp`; in-bounds boxes remain in-bounds

  - [x] 9.3 Implement `ResultsViewer.tsx`
    - Add `ResultsViewer.tsx` under `edge-cv-portal/frontend/src/components/`, consuming the captures endpoint
    - Detection capture: render the source `<img>` with an absolutely-positioned `<svg>` of `<rect>` + `<text>` per box, scaled via `scaleBox` from source pixel coords to rendered size (`naturalWidth/naturalHeight` vs `clientWidth/clientHeight`); each box shows `class_label` + confidence; add a toggle to switch to the server `overlay.jpg`
    - Zero objects: show source image with a "No objects detected" indicator and disable the overlay toggle when no overlay URL
    - Anomaly capture: delegate to the existing anomaly presentation unchanged
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x]* 9.4 Write vitest component tests for Results_Viewer
    - Renders N boxes for a detection capture (4.1); each box shows label + confidence (4.2); overlay toggle switches to `overlay.jpg` (4.3); anomaly presentation snapshot unchanged (4.4); empty detections shows "No objects detected" (4.5)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 10. Final wiring and verification
  - [x] 10.1 Wire the captures endpoint into the portal and Results_Viewer into the results route
    - Register the captures endpoint route (mirroring the datasets route wiring) and mount `ResultsViewer` where captures are opened; confirm the frontend fetches parsed detections + presigned URLs and renders end-to-end for detection, zero-object, and anomaly captures
    - _Requirements: 4.1, 4.3, 4.4_

  - [x]* 10.2 Write integration/smoke test for overlay path consistency
    - Integration (2.2): assert the gstreamer `emlcapture` overlay target path equals the Marshal metadata `data-ref`, and that a produced overlay tensor results in an on-disk `{capture_id}.overlay.jpg` for 1–2 representative captures
    - _Requirements: 2.2_

  - [x] 10.3 Verify full test suite
    - Run the backend property/unit tests (`pytest`, hypothesis min 100 iterations) and frontend checks (`tsc`, `vitest --run`, fast-check); fix any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.1, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation sub-tasks are never optional.
- Each task references specific requirements for traceability, and every one of the 9 correctness properties has a dedicated property-test sub-task (P1→4.3, P2→4.4, P3→4.5, P4→5.3, P5→1.2, P6→6.2, P7→5.4, P8→9.2, P9→2.3).
- Backend property tests use hypothesis + pytest at a minimum of 100 iterations and are tagged `# Feature: object-detection-visualization, Property N: ...`; the frontend `scaleBox` property test uses fast-check + vitest tagged `// Feature: object-detection-visualization, Property 8: ...`.
- All backend changes are confined to the hot-patchable `resources_for_copy` model templates and the shared `utils/class_label_map.py`; the tensor contract and gstreamer wiring are unchanged, so no Triton or ensemble rebuild is required.
- Device build/publish/deploy is handled separately and is intentionally excluded from these coding tasks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4"] },
    { "id": 3, "tasks": ["4.1"] },
    { "id": 4, "tasks": ["4.2", "4.3", "4.4"] },
    { "id": 5, "tasks": ["4.5", "5.1"] },
    { "id": 6, "tasks": ["5.2", "5.3", "5.4"] },
    { "id": 7, "tasks": ["6.1"] },
    { "id": 8, "tasks": ["6.2", "6.3"] },
    { "id": 9, "tasks": ["8.1", "9.1"] },
    { "id": 10, "tasks": ["8.2", "9.2", "9.3"] },
    { "id": 11, "tasks": ["9.4", "10.1"] },
    { "id": 12, "tasks": ["10.2", "10.3"] }
  ]
}
```
