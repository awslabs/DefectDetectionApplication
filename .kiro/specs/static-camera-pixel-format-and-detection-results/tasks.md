# Implementation Plan

## Overview

This plan fixes two independent device-side defects using the exploratory bugfix workflow —
reproduce first, capture existing behavior, apply the minimal fix, then validate. Both defects live
in the LocalServer component, so unlike the sibling spec there is only ONE delivery track and both
fixes ride the SAME Greengrass component build.

- **Defect A (tasks 1, 2, 5)**: the Static_Image_Camera's frames are Bayer-demosaiced. Fixed at the
  provisioning lookup — `src/backend/utils/config/default_camera_configurations.json` gains an entry
  keyed by the shipped enumeration vendor (`AWS-DDA`) carrying the RGB passthrough chain
  `capsfilter caps=video/x-raw,format=RGB ! videoconvert`, the same chain the RGB-native physical
  vendors already use — plus a scoped, idempotent backfill in
  `src/backend/dao/sqlite_db/db_backfill.py` so the Image_Source that already persisted the Bayer
  chain (`my6j3zx1` on `jetson-thor1`) converges without the user recreating it.
- **Defect B (tasks 3, 4, 6)**: manually-run inference on detection (YOLO) models never persists.
  Fixed by adding a detection member to the STORED prediction vocabulary
  (`src/backend/utils/constants.py`, applied only to `InferenceResultSchema.prediction` in
  `src/backend/model/inference_result.py`), counting Detection rows in the summary
  (`src/backend/dao/sqlite_db/inference_result_dao.py`), labeling a Detection row faithfully on the
  history page (`src/frontend/src/components/result-history/ColoredInferenceBox.tsx`), and logging a
  persistence failure loudly from the background task (`src/backend/endpoints/workflow.py`).

**Why the JSON change alone is not enough for Defect A (confirmed on device, not assumed).** The
conversion chain is resolved ONCE at Image_Source CREATION time and PERSISTED:
`ImageSourceAccessor.__create_image_source_configuration`
(`src/backend/resources/accessors/image_source_accessor.py` line 237) computes the default only when
the caller supplies no `imageSourceConfiguration`, and
`__get_default_image_source_configuration` (line 246) writes the resolved
`processingPipeline` into the Image_Source_Configuration row. Preview, capture, and the classic
workflow path all read only that stored value —
`GstPipelineBuilder._add_camera_image_source` (`src/backend/gstreamer/pipeline_builder.py` lines
50-52) appends `image_source.get("processingPipeline")` verbatim, and
`GstPipelineManager.create_buffer` (`src/backend/gstreamer/gst_pipeline.py` lines 61-72) sets the
appsrc caps from the FIRST `caps=` clause of that string. `update_image_source` re-creates a
configuration only when the caller SUPPLIES one, and stores that payload as given without consulting
the defaults, so there is no recompute path either. **Verified live**: `GET /image-sources/my6j3zx1`
on `jetson-thor1` returns `imageSourceConfiguration.processingPipeline` =
`"capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"`
(config `jk1y5ln8`). **Observable consequence**: a JSON-only fix would heal only NEWLY created
static-camera Image_Sources; `my6j3zx1` would keep serving Bayer-mangled frames until deleted and
recreated. Task 5.2 therefore adds the backfill so no user action is required.

**Lookup shape (confirmed from code).** The lookup is vendor-THEN-model, each falling back to
`default`: `cameraVendor = cameraVendor if cameraVendor in self.default_camera_config else "default"`
then `cameraModel = cameraModel if cameraModel in self.default_camera_config[cameraVendor] else "default"`
(`image_source_accessor.py` lines 277-278), read as
`self.default_camera_config.get(cameraVendor).get(cameraModel).get("processingPipeline")` (line 282).
Every one of the eight existing top-level keys carries a `default` sub-key — **confirmed by reading
the file** — and the code REQUIRES it, because a vendor without one would make
`.get("default")` return `None` and the chained `.get("processingPipeline")` raise. The new entry
therefore MUST carry a `default` sub-key; a `Static Image Camera` model sub-key alongside it is
optional and is included for symmetry with the vendors that pin their models explicitly.

**The two-file spirit is preserved per defect.** Defect A touches 2 files. Defect B's persistence fix
touches 2. The summary bucket (1 file) is a SECOND, independent defect (bugfix.md 1.15) that stays
wrong even after the persistence fix, so it cannot be deferred without leaving the user's reported
symptom half-fixed. The history-page label (1 file) is in scope because fixing persistence is what
first puts a Detection row in front of `ClassificationTypeTag`, which hardcodes the label "Anomaly"
for everything that is not Normal — a fix must not introduce a new user-visible wrong. The loud
persistence log (1 file) is recommended and included: it is a try/except at one call site, and this
exact class of invisible background-task failure is why the defect survived unnoticed. `logging.conf`
is deliberately NOT touched (see Notes).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2", "3", "4"],
      "description": "Write tests against UNFIXED code. Defect A: task 1 (Property 1: Bug Condition) fails, task 2 (Property 2: Preservation) passes. Defect B: task 3 (Property 3: Bug Condition) fails, task 4 (Property 4: Preservation) passes. All four are independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["5", "6"],
      "description": "Implementations. Task 5 (Defect A, depends on 1+2) then re-runs 1 and 2. Task 6 (Defect B, depends on 3+4) then re-runs 3 and 4. The two are independent and touch disjoint files."
    },
    {
      "wave": 3,
      "tasks": ["7"],
      "description": "Checkpoint: the full device suite green in the flask-app x86 container, no component build (depends on 5 and 6)."
    },
    {
      "wave": 4,
      "tasks": ["8"],
      "description": "Build hand-off to the user plus the post-build on-device verification checklist for BOTH defects (depends on 7). Builds nothing and deploys nothing."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 5. Tasks 3 and 4 are independent
  and must be completed BEFORE task 6. Sub-tasks 5.3 / 5.4 depend on 5.1 / 5.2; 6.5 / 6.6 depend on
  6.1 through 6.4.
- Task 7 depends on both 5 and 6. Task 8 depends on 7.
- **Nothing in this plan builds or deploys a component.** Builds take ~100 minutes, corrupt each
  other if run concurrently, and are the user's to drive.

## Tasks

- [x] 1. Write bug condition exploration test for the static camera's pixel format
  - **Property 1: Bug Condition** - Static camera frames Bayer-demosaiced instead of served as RGB
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms the defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples showing the static camera's conversion chain is a Bayer demosaic
  - **Conventions first**: the device suite uses **hypothesis** (not fast-check) and runs in the flask-app x86 container. READ `test/backend-test/static_image_camera/test_image_source_wiring.py` (it already drives the REAL `ImageSourceAccessor` over a private sqlite database and already patches `constants.DEFAULT_CAMERA_CONFIG_FILE_PATH` to the REAL `src/backend/utils/config/default_camera_configurations.json` at line 157 — reuse that harness rather than inventing one), `test/backend-test/static_image_camera/static_image_strategies.py` and `camera_manager_support.py` (generators and the forkserver-safe camera_manager import), and `test_property_grab_determinism.py` (hypothesis profile usage) BEFORE writing anything. Root conftest profiles: `fast` = 25 examples, `HYPOTHESIS_PROFILE=ci` = 100
  - New file: `test/backend-test/static_image_camera/test_property_static_camera_pixel_format.py`
  - **Scoped PBT approach**: the defect is deterministic, so scope the property to the shipped identity while generating around it. Generate arbitrary pinned images (size, format, colors via the existing `image_specs` / `render_image_bytes` strategies) and arbitrary acquisition configs, and assert the RESOLVED chain and the BUILT pipeline are RGB for every one of them
  - Assert the config file has an entry for the shipped identity and that the JSON key is not a drifting literal: `STATIC_IMAGE_CAMERA_IDENTITY["vendor"]` is a top-level key of the loaded `default_camera_configurations.json`, that entry carries a `default` sub-key, and the resolved `processingPipeline` equals `capsfilter caps=video/x-raw,format=RGB ! videoconvert`. If a model sub-key is used, assert it equals `STATIC_IMAGE_CAMERA_IDENTITY["model"]`. **This assertion is the guard the requirement asks for** (Requirement 2.2): a future identity change fails loudly here instead of silently reverting to Bayer
  - Assert through the REAL accessor: creating an Image_Source of type `Camera` for `cameraId: STATIC_IMAGE_CAMERA_ID` with no supplied `imageSourceConfiguration` persists `processingPipeline == "capsfilter caps=video/x-raw,format=RGB ! videoconvert"` (today it persists the BGGR chain)
  - Assert through the REAL `GstPipelineBuilder`: `add_image_source(...).build(is_preview=True)` for that Image_Source produces a launch string whose FIRST `caps=` clause — extracted with the same regex `create_buffer` uses, `caps=([^!]+)` — is `video/x-raw,format=RGB`, and that `bayer2rgb` does NOT appear anywhere in it
  - Assert the migration half: an Image_Source_Configuration row already storing the exact `default`/`default` Bayer chain for a static-camera Image_Source is rewritten to the RGB chain by the backfill, and running the backfill twice is a no-op the second time (idempotence)
  - Include the exact live counterexamples as concrete cases: the stored config `{"imageSourceConfigId":"jk1y5ln8","gain":1,"exposure":500,"processingPipeline":"capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"}` for Image_Source `my6j3zx1` (`cameraId: static-image-camera`), and the executed preview launch string `appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! videocrop top=0 bottom=0 left=0 right=0 ! jpegenc idct-method=2 quality=100 ! filesink location=/aws_dda/image-capture/preview/default_file_prefix-my6j3zx1.jpg`
  - The assertions encode Expected Behavior 2.1 through 2.5 (the Fix Checking property in bugfix.md)
  - Run in the flask-app x86 container. **CRITICAL interpreter note**: `flask-app:latest` is currently the JP6-layout image — `python3` is 3.10.12 and the app deps (incl. pydantic) live under python3.10 — so the documented `python3.11 || python3.10` shim picks a dep-less 3.11 and conftest dies with `ModuleNotFoundError: No module named 'pydantic'`. Use the FLIPPED order:
    `docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; $PY -m pytest test/backend-test/static_image_camera/test_property_static_camera_pixel_format.py -q -p no:cacheprovider'`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the defect exists)
  - Document counterexamples found (expected: `AWS-DDA` absent from the config file so the lookup collapses to `default`/`default`; the persisted and built pipelines both carry `video/x-bayer,format=bggr` and `bayer2rgb`; the backfill does not exist)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 2. Write preservation property tests for Defect A (BEFORE implementing the fix)
  - **Property 2: Preservation** - Physical vendors' pipelines, the store's packed-RGB frame contract, and the Frame_Feed path unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record the actual outputs, then assert those recorded outputs
  - New file: `test/backend-test/static_image_camera/test_property_static_camera_pixel_format_preservation.py`
  - Observe on UNFIXED code and record: the loaded `default_camera_configurations.json` in full — all eight top-level keys (`Lucid Vision Labs`, `Zebra Technologies`, `Basler`, `Allied Vision`, `OMRON SENTECH`, `Nvidia CSI`, `ICAM`, `default`) and every model sub-key under each, with their exact `processingPipeline` / `device` / `deviceName` / `gain` / `exposure` values. Assert every recorded entry is byte-for-byte unchanged, and that every top-level key carries a `default` sub-key (the lookup requires it — a vendor without one would raise on `.get("default").get("processingPipeline")`)
  - Assert explicitly that `default`/`default` is STILL the BGGR Bayer chain: an unknown PHYSICAL vendor must keep falling through to it, because a Bayer guess remains correct for an unknown bus camera. Generate arbitrary vendor/model strings NOT in the file and assert the two-step lookup still yields that chain
  - Observe on UNFIXED code and record: `resolvedPipeline` for each RGB-native vendor (`Lucid Vision Labs`, `Allied Vision`, `OMRON SENTECH` → `capsfilter caps=video/x-raw,format=RGB ! videoconvert`), the GRAY8 model variants under `Zebra Technologies` / `Basler` / `Allied Vision`, and the NVIDIA CSI and ICAM special cases (which never reach the vendor lookup). Assert unchanged
  - Observe on UNFIXED code and record: `StaticImageStore.get_frame()` for arbitrary pinned images — `pixel_format == "RGB"`, `len(data) == 3 * width * height`, EXIF-transposed dimensions, byte-identical across repeated grabs and across arbitrary acquisition configs. Assert unchanged (the existing `test_property_grab_determinism.py` already covers determinism; this asserts the FORMAT contract the fix must not disturb)
  - Observe on UNFIXED code and record: `workflow_engine.pipeline_executor._frame_caps` (line 2821) for a frame tagged `pixel_format: "RGB"` → `video/x-raw,format=RGB`, for `bayer:bggr` → `video/x-bayer,format=bggr`, and for an untagged frame → the bytes-per-pixel guess. Assert unchanged. **This is the "verify and preserve" the requirement asks for: the newer workflow Frame_Feed path is ALREADY CORRECT for the static camera**, because the store tags every frame `"RGB"` and `_frame_caps` honors the tag; the existing assertion `"appsrc name=appsrc caps=video/x-raw,format=RGB "` in `test/backend-test/static_image_camera/test_workflow_feed.py` must keep passing untouched
  - Observe on UNFIXED code and record: `camera_manager._PFNC_TO_TAG` and `gst_pixel_format()` for every mapped code plus unmapped / non-integer inputs (→ `None`); `getCameras()` and `getCamera(id)` for pinned and unpinned states; `STATIC_IMAGE_CAMERA_IDENTITY` field-for-field. Assert unchanged
  - Backfill scoping: assert the backfill is a no-op for every configuration that is NOT (a static-camera Image_Source AND storing exactly the known-wrong `default`/`default` string) — including a static-camera source whose pipeline was deliberately customized, and every physical-camera source storing the same Bayer chain (there are four such rows live on `jetson-thor1`: `pk0pppde`, `u5ox1y1s`, `563tiauk`, `3w200mtb`, `g6zrsox3` — Basler sources that MUST keep their Bayer chain)
  - Note that the existing suites `test/backend-test/static_image_camera/*` (especially `test_image_source_wiring.py`, whose `assert row.imageSourceConfiguration.processingPipeline` is a truthiness check and stays valid, and `test_workflow_feed.py`) are themselves preservation coverage and must keep passing untouched
  - Run the tests on UNFIXED code with the container command from task 1
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [x] 3. Write bug condition exploration test for detection result persistence
  - **Property 3: Bug Condition** - Detection results rejected by the stored-row validation and absent from the summary
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms the defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples showing a detection row cannot be stored and is counted nowhere
  - **Independent of tasks 1, 2, 5**: different files, different defect; write it in parallel
  - **Conventions first**: READ `test/backend-test/utils/test_inference_results_utils.py` (its `convert_inference_res_to_save_in_db` coverage and the `LocalServerBaseTestCase` harness), `test/backend-test/resources/test_inference_result_accessor.py`, `test/backend-test/api-endpoints/test_inference_result_api.py` (list and summary endpoint coverage), and the marshal suites `test_marshal_payload_discrimination.py` / `test_marshal_detections_block.py` / `test_marshal_detection_count_confidence.py` BEFORE writing anything, and follow their harnesses
  - New file: `test/backend-test/utils/test_property_detection_result_persistence.py`
  - **Scoped PBT approach**: the defect is deterministic in `prediction`, so scope the property to the detection prediction value while generating everything else. Generate arbitrary detection rows — arbitrary `captureId`, `confidence` in [0, 1], `anomalyScore`, `anomalyThreshod`, `detection_count`, arbitrary numbers of detections with arbitrary class labels and bounding boxes, arbitrary `humanReviewRequired` — all carrying `prediction: "Detection"`, and assert every one of them validates and stores
  - Assert `InferenceResultSchema().load(row)` succeeds for every generated detection row, and that every field other than `prediction` round-trips unchanged. On unfixed code this raises `marshmallow.exceptions.ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}`
  - Assert the widening is SCOPED: `humanClassification` on both `InferenceResultSchema` and `CapturedDataSchema` still rejects `"Detection"`; `constants.OUTPUT_RULE` is unchanged; the `prediction` query parameter on `GET /workflows/{id}/results` still accepts only `Normal` / `Anomaly`
  - Assert the summary: for a workflow whose rows include Detection rows, `get_inference_result_summary` reports `detection` equal to the Detection row count and `totalInference == normal + anomaly + detection == COUNT(all rows in window)`. On unfixed code there is no `detection` key and `totalInference` excludes them
  - Assert the end-to-end marshal → store path with a REAL detection payload: build the `deviceFleetAuxiliaryOutputs` / `deviceFleetAuxiliaryInputs` shape the marshal emits for `task=object_detection`, run it through `GetInferenceResults.save_image_object` and `convert_inference_res_to_save_in_db`, and assert the resulting row validates. Use the confirmed live values as the concrete case: `{"confidence": 0.8879303932189941, "inference_result": "Detection", "anomaly_score": 0.8879303932189941, "anomaly_threshold": 1.0, "detection_count": 5}` producing `{"prediction": "Detection", "modelId": "model-yolo-test-jetson-xavier-jp7", "captureId": "db9721b27fc54b8f993fb06ef8738631", ...}`
  - Assert the history-page label expectation for the frontend change: a Detection prediction must not render as "Anomaly". Note in the test file that `src/frontend/src/components/result-history/ColoredInferenceBox.tsx` has NO jest suite in this repo (the device HMI has no `.test.tsx` files) and that this one-line mapping is verified by on-device inspection in task 8 instead — do NOT stand up a new frontend test framework for it
  - Assert the observability expectation: a persistence failure inside `save_full_inference_result` logs an error naming the workflow id and the capture id, and does NOT change the manual-run response
  - Run in the flask-app x86 container with the FLIPPED interpreter order (see task 1):
    `docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; $PY -m pytest test/backend-test/utils/test_property_detection_result_persistence.py -q -p no:cacheprovider'`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the defect exists)
  - Document counterexamples found (expected: `ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}` from `inference_result_accessor.py` line 58 for every generated detection row; the summary has no `detection` key and `totalInference` under-reports)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 1.15, 1.16, 1.17, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11_

- [x] 4. Write preservation property tests for Defect B (BEFORE implementing the fix)
  - **Property 4: Preservation** - Segmentation and classification results, the marshal's three branches, and the endpoint shapes unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record the actual outputs, then assert those recorded outputs
  - New file: `test/backend-test/utils/test_property_detection_result_preservation.py`
  - Observe on UNFIXED code and record: `InferenceResultSchema().load(row)` for arbitrary segmentation rows (`prediction` in {Normal, Anomaly}, `anomalyScore`, `anomalyThreshod`, `maskImage`, `maskBackground`, `anomalyLabels`) and arbitrary classification rows (no mask fields), including which rows are REJECTED today and why (a missing required `anomalyScore` / `anomalyThreshod` / `confidence`, a `prediction` outside the vocabulary, a `captureType` outside `CAPTURE_TYPE`, an over-length `textNote`). Assert both the accepted and the rejected sets are unchanged apart from the single added detection prediction value
  - Observe on UNFIXED code and record: `GetInferenceResults.save_image_object` output for all THREE branches — a detection payload, a segmentation payload (with and without a mask), and a classification payload — and the branch-selection predicates `is_detection_model_output_result` / `is_segmentation_model_output_result` over arbitrary output lists. Assert byte-identical; the fix widens what the STORE accepts, never what the marshal produces
  - Observe on UNFIXED code and record: `convert_inference_res_to_save_in_db` output for arbitrary segmentation and classification results, including the `None`-stripping behavior and the `get_default_configs_lfv` fallback on `ResourceNotFoundError`. Assert unchanged — this function is NOT modified by the fix
  - Observe on UNFIXED code and record: `get_inference_result_summary` for windows containing only Normal and Anomaly rows, including the `summaryStartTime` boundary. Assert `normal` and `anomaly` are unchanged and that the only difference is the additive `detection: 0` key; assert the response envelope stays `{"stats": {...}, "lastResetTime": ...}`
  - Observe on UNFIXED code and record: `GET /workflows/{id}/results` for every filter combination (`prediction`, `downloaded`, `textNoteFilter`, `humanClassificationProvided`, `humanReviewRequired`, `captureType`, pagination) and the descending `inferenceCreationTime` ordering. Assert unchanged in shape, filtering, and ordering
  - Observe on UNFIXED code and record: the manual-run response payload for both `returnPartialResultsEarly` modes and both `returnImageString` values. Assert byte-identical after the fix — a persistence failure must still never fail the response (Requirement 3.17)
  - Observe on UNFIXED code and record: `generate_smgt_format_manifest` output and the `download_file` prediction check for Normal / Anomaly rows. Assert unchanged
  - Note that the existing suites `test/backend-test/utils/test_inference_results_utils.py`, `test/backend-test/resources/test_inference_result_accessor.py`, `test/backend-test/api-endpoints/test_inference_result_api.py`, `test_marshal_payload_discrimination.py`, `test_marshal_detections_block.py`, `test_marshal_detection_count_confidence.py`, `test_marshal_detection_typing.py`, `test_marshal_anomaly_backward_compat.py`, and `test_lfv_detection_tensor_set.py` are themselves preservation coverage and must keep passing untouched
  - Run the tests on UNFIXED code with the container command from task 3
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19_

- [x] 5. Fix for the static camera's Bayer-demosaiced frames

  - [x] 5.1 Give the static camera an RGB conversion chain in the camera configuration file
    - `src/backend/utils/config/default_camera_configurations.json`: add ONE new top-level entry keyed by the shipped enumeration vendor, `"AWS-DDA"`, containing a `"default"` sub-key (REQUIRED — the lookup does `.get(vendor).get(model).get("processingPipeline")`, so a vendor without `default` would raise) and a `"Static Image Camera"` model sub-key, both with `"processingPipeline": "capsfilter caps=video/x-raw,format=RGB ! videoconvert"` — the identical chain `Lucid Vision Labs`, `Allied Vision`, and `OMRON SENTECH` already use
    - Match the existing file shape exactly: tab indentation, the ` : ` spacing around colons, and the `{"processingPipeline": "..."}` value shape used by the RGB-native vendors (no `device` / `deviceName` / `gain` / `exposure` keys — those belong only to `Nvidia CSI` and `ICAM`, which never reach the vendor lookup)
    - Do NOT touch any existing top-level key or model sub-key. In particular leave `default`/`default` as the BGGR Bayer chain: an unknown PHYSICAL GenICam vendor must keep falling through to it (Requirement 3.1)
    - The literal JSON key is what task 1 pins against `STATIC_IMAGE_CAMERA_IDENTITY["vendor"]` and `["model"]`, so a future identity change fails loudly instead of silently reverting to Bayer
    - Do NOT change `StaticImageStore._decode_frame_locked` or `get_frame` — packed RGB with the `"RGB"` tag is CORRECT and the Frame_Feed path depends on that tag (Requirements 3.3, 3.4). Do NOT change `_StaticImageCameraHandle`, `getCameras()`, `rescan_cameras()`, `getCamera()`, `_PFNC_TO_TAG`, or `gst_pixel_format()`
    - Do NOT change `create_buffer`'s first-`caps=` regex or `_add_camera_image_source`. Deriving the classic path's appsrc caps from the frame's own `pixel_format` tag (the way `_frame_caps` does) would be the more general fix and is explicitly OUT of scope: it changes the caps for every physical camera on the hot path
    - _Bug_Condition: isBugCondition(X) part a1 - resolvedPipeline(X) = bayerDefaultChain() for X.cameraId = STATIC_IMAGE_CAMERA_ID_
    - _Expected_Behavior: resolvedPipeline'(X) = rgbPassthroughChain() and firstCaps(builtPipeline) = video/x-raw,format=RGB with no bayer2rgb - the Fix Checking property in bugfix.md_
    - _Preservation: every existing vendor and model entry byte-for-byte unchanged, default/default still Bayer, the store's packed-RGB contract and the Frame_Feed path untouched_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 5.2 Converge the already-persisted Bayer chain on existing static-camera Image_Sources
    - `src/backend/dao/sqlite_db/db_backfill.py`: add a third backfill function alongside `migration_cleanup_imgsrc_db` and `migration_cleanup_workflow_db`, and call it from `backfill()` inside the existing `session.begin()` block. `backfill()` already runs once at startup from `src/backend/app.py` line 273 — reuse that wiring rather than adding a new hook
    - Scope it tightly: for each Image_Source of type `Camera` whose `cameraId` equals `STATIC_IMAGE_CAMERA_ID`, load its Image_Source_Configuration and rewrite `processingPipeline` ONLY when the stored value equals EXACTLY the known-wrong `default`/`default` string. Read that string from the loaded `default_camera_configurations.json` rather than hardcoding it, so the two halves of the fix cannot drift, and read the replacement from the new `AWS-DDA` entry for the same reason
    - Leave any other stored value untouched, including a deliberately customized static-camera pipeline (Requirement 3.8), and never touch a configuration belonging to a physical camera — five rows on `jetson-thor1` store the same Bayer chain for Basler sources and MUST keep it
    - Make it idempotent (a second run rewrites nothing) and non-fatal: wrap it the way `migration_cleanup_workflow_db` wraps its `session.execute`, so a failure logs a warning and cannot block startup
    - Log the Image_Source ids it rewrote, so the post-build verification can confirm `my6j3zx1` converged
    - Do NOT add a schema migration — `processingPipeline` is already a nullable string column and no column changes
    - Do NOT recompute defaults for any other camera or introduce a general "re-resolve all pipelines" pass; that would clobber user customizations
    - _Bug_Condition: isBugCondition(X) part a2 - a stored static-camera configuration whose processingPipeline is bayerDefaultChain()_
    - _Expected_Behavior: backfill'(X).config.processingPipeline = rgbPassthroughChain(), idempotent_
    - _Preservation: backfill'(X) = X for every configuration that is not a static-camera source storing exactly the known-wrong string_
    - _Requirements: 2.5, 3.8_

  - [x] 5.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Static camera frames served as RGB
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm the resolved chain, the built pipeline's first caps, and the backfill all behave correctly
    - **EXPECTED OUTCOME**: Tests PASS (confirms the defect is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 5.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Physical vendors, the store, and the Frame_Feed path unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Also run the existing suites `test/backend-test/static_image_camera/` (all of them) and `test/backend-test/gstreamer/`
    - Confirm `git diff` for this defect touches exactly two source files — `src/backend/utils/config/default_camera_configurations.json` and `src/backend/dao/sqlite_db/db_backfill.py` — plus the new test files: no `static_image_camera.py`, no `camera_manager.py`, no `aravis_functions.py`, no `pipeline_builder.py`, no `gst_pipeline.py`, no `pipeline_executor.py`, and no file belonging to the sibling spec (`camera_sync/inventory.py`, `camera_sync/agent.py`)
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.20_

- [x] 6. Fix for manually-run detection inference never persisting

  - [x] 6.1 Accept the detection prediction value for stored inference results
    - `src/backend/utils/constants.py`: add a `DETECTION = 'Detection'` constant next to `ANOMALY` / `NORMAL` (line 48-49) and a separate stored-prediction vocabulary, e.g. `STORED_PREDICTION = PREDICTION + [DETECTION]`. Leave `PREDICTION`, `OUTPUT_RULE`, and `CAPTURE_TYPE` unchanged
    - `src/backend/model/inference_result.py`: use the new vocabulary for `InferenceResultSchema.prediction` ONLY (line 83). Leave `humanClassification` on both `InferenceResultSchema` (line 96) and `CapturedDataSchema` (line 135) validating against `PREDICTION` — a human verdict is a binary judgement, not a model task type (Requirement 3.13)
    - The value string MUST match what the marshal emits: `"Detection"` from `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py` line 399. Do NOT change the marshal, and do NOT map Detection onto Anomaly or Normal — the marshal types it distinctly on purpose and `test_marshal_payload_discrimination.py` pins that
    - Do NOT change `convert_inference_res_to_save_in_db`, `save_image_object`, or either branch predicate; do NOT widen the `prediction` query Literal on `GET /workflows/{id}/results` (`src/backend/endpoints/inference_result.py` line 84), the `download_file` prediction check, or `inference_result_accessor.get_inference_results_by_prediction`'s `constants.PREDICTION` guard
    - No schema migration: the `prediction` column is already a free-text string
    - _Bug_Condition: isBugCondition(X) part b1 - X.row.prediction = DETECTION and NOT validatesForStorage(X.row)_
    - _Expected_Behavior: validatesForStorage'(X.row) is true and every other field round-trips unchanged_
    - _Preservation: humanClassification, the results-list prediction filter, OUTPUT_RULE, the marshal, and the db marshal all unchanged_
    - _Requirements: 2.6, 2.7, 2.8, 3.10, 3.11, 3.12, 3.13, 3.14_

  - [x] 6.2 Count detection results in the workflow results summary
    - `src/backend/dao/sqlite_db/inference_result_dao.py` `get_inference_result_summary` (lines 99-115): add a third count filtered `prediction == DETECTION` over the same workflow and `summaryStartTime` window, return it as an additive `detection` key, and make `totalInference` the sum of all three so it means what its name says
    - Keep `normal` and `anomaly` computed exactly as today and keep the response envelope `{"stats": {...}, "lastResetTime": ...}` unchanged — the HMI reads `totalInference` / `normal` / `anomaly` and ignores unknown keys, so this is backward compatible (Requirement 3.15)
    - This is a SECOND, independent defect: it stays wrong after 6.1 alone, because a persisted Detection row would still be counted in no bucket and excluded from the total (bugfix.md 1.15)
    - Do NOT add a fourth tile to `src/frontend/src/components/live-result/ResultAnalyticsSummary.tsx` — its `ColumnLayout columns={4}` is full (Total inferences, Anomalous results, Normal results, Last reset date) and adding a fifth value would reflow the panel. Surfacing a dedicated "Detections" tile is a follow-up; making `totalInference` correct is what the reported symptom needs
    - _Bug_Condition: isBugCondition(X) part b2 - a summary window containing Detection rows whose totalInference is less than the row count_
    - _Expected_Behavior: s.detection equals the Detection row count and s.totalInference = s.normal + s.anomaly + s.detection = COUNT(rows in window)_
    - _Preservation: normal and anomaly unchanged for existing data, the envelope unchanged, detection additive_
    - _Requirements: 2.9, 3.15_

  - [x] 6.3 Label a detection result faithfully on the results history page
    - `src/frontend/src/components/result-history/ColoredInferenceBox.tsx`: `ClassificationTypeTag` currently renders `Normal` as a success indicator and hardcodes the label `Anomaly` for everything else, so a persisted Detection row would read "Anomaly". Render the classification's own value instead, mirroring what the live card already does (`src/frontend/src/components/live-result/LiveResultCard.tsx` sets `predictionType = prediction === PredictionType.Normal ? "success" : "error"` and renders `{prediction}` as the label)
    - `PredictionType.Detection` already exists in `src/frontend/src/components/image-source/types.ts` — reuse it; do not add a new type
    - Keep Normal and Anomaly rendering visually identical (Requirement 3.18): same success / error indicator types and the same `normallInferenceBoxStyle` / `anomalylInferenceBoxStyle` box selection. This is in scope because 6.1 is what first puts a Detection row in front of this component; a fix must not introduce a new user-visible wrong
    - Do NOT touch `ResultCardFilterConfig.tsx` — the API's `prediction` filter accepts only Normal / Anomaly (Requirement 3.14), so offering a Detection filter option would offer a filter the backend rejects. That belongs in a follow-up alongside widening the query Literal
    - **No automated coverage for this one line**: the device HMI has no jest / vitest suites (`react-scripts test` is configured but there are no `.test.tsx` files under `src/frontend/src/components/`). Standing up a frontend test framework is out of scope; task 8 verifies this visually on device. State this gap in the commit message
    - _Bug_Condition: isBugCondition(X) part b1, downstream - a stored Detection row rendered by a component that hardcodes the Anomaly label_
    - _Expected_Behavior: the history page labels a Detection row by its own prediction value_
    - _Preservation: Normal and Anomaly render identically on both the history page and the live card_
    - _Requirements: 2.10, 3.18_

  - [x] 6.4 Make a persistence failure loud and identifiable
    - `src/backend/endpoints/workflow.py`: wrap the body of `save_full_inference_result` (line 124) so any exception is logged at ERROR with `exc_info=True` and names the workflow id and the capture id, then is swallowed rather than escaping the background task anonymously
    - Keep the manual-run RESPONSE unchanged (Requirement 3.17, 2.11): persistence must stay in a background task that cannot fail the response. Failing the response on a persistence error would regress every currently working model
    - Apply the same wrapping to `read_full_results_and_save` (line 133), the `returnPartialResultsEarly` background task, so both persistence paths are covered
    - **Do NOT touch `src/backend/logging.conf`.** Its `fileHandler` writing `aws.edgeml.dda.LocalServerCameraApp.log` is attached only to the unused `cameraStation` logger while every module uses `logging.getLogger(__name__)`, so that file was never a live destination — but re-routing handlers inside a component build risks the whole log pipeline for no gain. Container stdout already carries the full traceback (proven live: the `ValidationError` and both re-raise tracebacks were captured from `docker logs`); what was missing is a NAMED line saying which workflow and capture failed to persist. That is what this sub-task adds
    - _Bug_Condition: isBugCondition(X) part b1, invisibility - a background-task persistence failure that reaches no user and names no workflow_
    - _Expected_Behavior: loggedError'(X) contains the workflow id and the capture id, and manualRunResponse'(X) = manualRunResponse(X)_
    - _Preservation: the manual-run response payload and the background-task decoupling unchanged_
    - _Requirements: 2.11, 3.17, 3.19_

  - [x] 6.5 Verify bug condition exploration test now passes
    - **Property 3: Expected Behavior** - Detection results persist and are counted
    - **IMPORTANT**: Re-run the SAME tests from task 3 - do NOT write new tests
    - The tests from task 3 encode the expected behavior; when they pass they confirm a detection row validates and stores, the summary counts it, and a persistence failure is logged with identifying context
    - **EXPECTED OUTCOME**: Tests PASS (confirms the defect is fixed)
    - _Requirements: 2.6, 2.7, 2.8, 2.9, 2.10, 2.11_

  - [x] 6.6 Verify preservation tests still pass
    - **Property 4: Preservation** - Segmentation and classification results and the endpoint shapes unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 4 - do NOT write new tests
    - Also run the existing suites `test/backend-test/utils/`, `test/backend-test/resources/`, `test/backend-test/api-endpoints/`, and the marshal suites at the root of `test/backend-test/` (`test_marshal_*.py`, `test_lfv_detection_tensor_set.py`, `test_yolo_detection_postprocessor.py`, `test_rf_detr_*.py`)
    - Confirm `git diff` for this defect touches exactly five source files — `src/backend/utils/constants.py`, `src/backend/model/inference_result.py`, `src/backend/dao/sqlite_db/inference_result_dao.py`, `src/frontend/src/components/result-history/ColoredInferenceBox.tsx`, `src/backend/endpoints/workflow.py` — plus the new test files: no marshal file, no `inference_results_utils.py`, no `logging.conf`, no alembic migration, and no file belonging to the sibling spec
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19, 3.20_

- [x] 7. Checkpoint - Ensure all tests pass
  - Run the four new suites plus the existing device suites in the flask-app x86 container, with the FLIPPED interpreter order (`python3.10 || python3.11`):
    `docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; $PY -m pytest test/backend-test/static_image_camera test/backend-test/utils test/backend-test/resources test/backend-test/api-endpoints test/backend-test/camera_sync test/backend-test/gstreamer -q -p no:cacheprovider'`
  - **Interpreter note, do not "fix" this**: `flask-app:latest` is currently the JP6-layout image — `python3` is 3.10.12 and the app deps including pydantic live under python3.10 — so the shim documented in `.kiro/steering/builds.md` (`python3.11 || python3.10`) picks a dep-less 3.11 and conftest dies with `ModuleNotFoundError: No module named 'pydantic'`. The flipped order above is correct for this image
  - **Known pre-existing failures to expect and IGNORE** (environmental, proven unrelated by stash): 5 failures in `test/backend-test/camera_sync/test_server_setup_isolation.py::TestServerSetupCameraSyncIsolation` from `ImportError: libtritonserver.so`. Everything else must be green
  - Also run the security preservation guards before the hand-off, since a stale baseline fails the build gate AFTER the ~1h compile:
    `python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`
  - None of the six files this spec changes is preservation-tracked (the tracked set is `src/docker-compose.yaml`, the backend / frontend / edgemlsdk Dockerfiles, `src/backend/requirements.txt`, the recipe variants, and `station_install/setup_station.sh`), so no baseline rebaseline is expected. If a guard fails, it is almost certainly an unbaselined `edge-cv-portal/infrastructure/cdk.out` from a portal deploy — move it aside per builds.md rather than editing baselines
  - Ensure all tests pass; ask the user if questions arise
  - _Requirements: all_

- [~] 8. Build hand-off and post-build on-device verification
  - **THIS TASK BUILDS NOTHING AND DEPLOYS NOTHING.** A component build takes ~100 minutes, corrupts other builds if run concurrently, and is the user's to drive. This task hands off and then verifies
  - **Component and version**: `aws.edgeml.dda.LocalServer.arm64JP7`, currently **1.0.24** on `jetson-thor1` → next patch (`bash build-custom.sh aws.edgeml.dda.LocalServer.arm64JP7 NEXT_PATCH`, or `TARGETS="7" ./run_jp_builds.sh`), then a deployment revision to reach the device
  - **This build ALSO carries the sibling spec's device fix.** `.kiro/specs/static-image-camera-binding-and-pin-discoverability` is complete except its task 10: its device fix — de-duplicating the Static_Image_Camera registration in `src/backend/camera_sync/inventory.py` plus the one-shot shadow-key retirement in `src/backend/camera_sync/agent.py` — is already implemented, tested, and committed, awaiting exactly this build. **ONE build delivers all three device fixes**: the static camera's RGB pixel format, detection result persistence, and the registry de-duplication. Do not schedule a separate build for either spec
  - **Pre-flight gates from `.kiro/steering/builds.md`, in order**:
    - (a) `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` must BOTH return nothing. If either returns a process, wait — never start a second build
    - (b) Move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) so the cdk.out drift guard does not fail the security gate after the ~1h compile
    - (c) Do NOT run a portal deploy (`deploy-portal.sh` / `deploy-infrastructure.sh` / `deploy-frontend.sh`) while the build runs — a portal deploy regenerates `cdk.out` mid-build and fails the gate. Sequence: portal deploy fully finishes → move `cdk.out` aside → start the build
    - (d) Confirm the security preservation guards are green BEFORE starting (task 7 already runs them) — never assume it
    - (e) ONE target at a time. `gdk-config.json` holds a single component; swap it per target and restore it when done. Capture output to `.gdk_build_jp7.log`
  - **Post-build on-device verification — Defect A (static camera pixel format)**:
    - (a) Confirm the deployed component version on `jetson-thor1` is the new patch and the backend container comes up healthy and stays healthy (no restart loop)
    - (b) Confirm the backfill converged the existing Image_Source: `GET /image-sources/my6j3zx1` reports `imageSourceConfiguration.processingPipeline` = `capsfilter caps=video/x-raw,format=RGB ! videoconvert`, no longer the BGGR Bayer chain. The startup log should name `my6j3zx1` as rewritten. **If the backfill was NOT shipped**, this is the step that fails, and the fallback is to delete and recreate the Image_Source from `static-image-camera` — the JSON entry alone only affects newly created sources (see the Overview)
    - (c) Preview and capture `my6j3zx1` and confirm the container log shows a pipeline whose first caps is `video/x-raw,format=RGB` with NO `bayer2rgb`
    - (d) **Look at the image.** The pinned 640x480 PNG (sha256 `83cca4bb036a4faef925a54b8b34e784a7d04e7b67dc1079ba6e139fc9ea5205`) must render with CORRECT colors: deep-blue field, yellow border, red disc, white diagonals — with NO gray cast, NO fine diagonal hatching, and NO rainbow/chromatic fringing on edges. Geometry was already correct before the fix, so geometry alone does not prove anything; the colors are the test
    - (e) Create a NEW Image_Source from `static-image-camera` and confirm it provisions the RGB chain directly, so the JSON half is verified independently of the backfill
    - (f) Spot-check a physical camera: the Basler source `28183exv` (`Basler-26760165225D-23405149`, Connected) must still provision and execute its BGGR Bayer chain and still preview correctly — the RGB entry must not have leaked into any physical vendor's resolution
    - (g) Run a workflow through the classic Image_Source path on the static camera and confirm the model sees unmangled pixels
  - **Post-build on-device verification — Defect B (detection result persistence)**:
    - (h) Run a manual inference on the YOLO workflow `pagb7vj8` ("yolotest", Folder-backed at `/aws_dda/yolotest`, model `model-yolo-test-jetson-xavier-jp7`) and confirm the response still returns the full detection result, unchanged in shape
    - (i) Confirm `GET /workflows/pagb7vj8/results` now returns the row — `total` ≥ 1 with `prediction: "Detection"`, the `outputImageFilePath` pointing at the `.overlay.jpg`, and the confidence matching the response
    - (j) Confirm `GET /workflows/pagb7vj8/results/summary` counts it: a `detection` count ≥ 1 and `totalInference` equal to the total row count in the window
    - (k) Confirm the container log carries NO `{'prediction': ['Must be one of: Normal, Anomaly.']}` for the run, and that no `Unable to store inference result` HTTPException traceback appears
    - (l) In the HMI, confirm the bottom view and "view all results" now show the detection result, and that it is labeled **Detection** — not "Anomaly" — with the overlay image and the object list rendering
    - (m) Repeat (h) through (j) for `515zkeve` ("yoloworld_blue_plate_test") to cover a second detection model
    - (n) Regression spot-check: run a manual inference on a segmentation workflow (`6q3lur2w` "cookies-segmentation", currently 14 results) and confirm it still persists with the identical row shape — `prediction` Normal or Anomaly, `anomalyScore`, `maskImage`, `maskBackground` — and that its existing rows and summary counts are unchanged
    - (o) Cold-start note, not a defect in scope: a first run against an unloaded model can fail with `Model is not ready for inference` / `Pipeline failed to change state to PLAYING` and moves the source image to `<workflowOutputPath>/failed/`. This was observed during reproduction on `pagb7vj8` and is the known lazy-load race, unrelated to Defect B. Re-run once the model reaches READY. **A folder-backed workflow CONSUMES its oldest source image on a successful run**, so restore `/aws_dda/yolotest` afterwards if the images are needed again
  - **STOP AND ASK THE USER BEFORE ANY STEP THAT MUTATES LIVE DEVICE STATE**: `jetson-thor1` is a real Jetson in use. Re-pinning or replacing the pinned image, deleting or recreating `my6j3zx1`, and running folder-backed workflows (which consume source images) all change live state. Get explicit confirmation first; the read-only checks — (b), (c), (i), (j), (k) — are the default
  - _Requirements: 2.3, 2.4, 2.5, 2.8, 2.9, 2.10, 3.1, 3.9, 3.15, 3.17, 3.18, 3.20_

## Notes

- **Test-first ordering is mandatory.** Task 1 must FAIL and task 2 must PASS on UNFIXED code before
  implementing task 5; task 3 must FAIL and task 4 must PASS before implementing task 6. Do not
  modify `default_camera_configurations.json` or `db_backfill.py` until 1 and 2 are written and
  documented, and do not modify `constants.py`, `model/inference_result.py`,
  `inference_result_dao.py`, `ColoredInferenceBox.tsx`, or `endpoints/workflow.py` until 3 and 4 are.
- **Property references**: Property 1 (Bug Condition / Fix Checking, Defect A) validates Requirements
  2.1 through 2.5; Property 2 (Preservation, Defect A) validates 3.1 through 3.8; Property 3 (Bug
  Condition / Fix Checking, Defect B) validates 2.6 through 2.11; Property 4 (Preservation, Defect B)
  validates 3.9 through 3.19. Requirement 3.20 (this build also carries the sibling spec's committed
  device fix, whose files this spec does not touch) is enforced by the zero-diff constraints in tasks
  5.4 and 6.6 and re-checked at the hand-off in task 8.
- **Confirmed root cause, Defect A (file and live evidence)**: the store serves packed RGB tagged
  `"RGB"` (`utils/static_image_camera.py` `_decode_frame_locked`, lines ~301-322; docstring line
  ~232) and `camera_manager.get_camera_frame` returns it verbatim through the static short-circuit
  (line 618). The classic path resolves its conversion chain by vendor:
  `_StaticImageCameraHandle.get_vendor_name()` / `get_model_name()`
  (`edge_ml1_p_camera_management/aravis_functions.py` lines 164, 167) answer `AWS-DDA` /
  `Static Image Camera`, neither is a key in `default_camera_configurations.json`, so
  `image_source_accessor.py` lines 277-278 collapse both to `default` and line 282 returns the BGGR
  chain. That string is PERSISTED at creation (line 237), read back verbatim by
  `pipeline_builder._add_camera_image_source` (lines 50-52), and turned into the appsrc caps by
  `gst_pipeline.create_buffer`'s first-`caps=` regex (lines 61-72). Live on `jetson-thor1`:
  `my6j3zx1` → config `jk1y5ln8` stores the Bayer chain, and the container log for
  `POST /image-sources/my6j3zx1/preview` shows
  `appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! ...`.
- **Confirmed root cause, Defect B (exact exception and failing line)**: the marshal sets
  `inf_result["Inference result"] = "Detection"`
  (`dda_triton/resources_for_copy/marshal_for_capture_template.py` line 399);
  `convert_inference_res_to_save_in_db` (`utils/inference_results_utils.py` line ~330) succeeds and
  produces `prediction: "Detection"`; then
  **`src/backend/resources/accessors/inference_result_accessor.py` line 58, `result = self.schema.load(data)`,
  raises `marshmallow.exceptions.ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}`**
  against `InferenceResultSchema.prediction = fields.Str(validate=validate.OneOf(PREDICTION), required=True)`
  (`model/inference_result.py` line 83, `PREDICTION = ['Normal', 'Anomaly']` at `utils/constants.py`
  line 53). It is caught at line 65 and re-raised as
  `HTTPException: 400: Unable to store inference result.`, inside the background task
  `save_full_inference_result` (`endpoints/workflow.py` lines 124-131, scheduled at line 172), so it
  cannot reach the already-sent response. Confirmed twice on `jetson-thor1`: read-only, by feeding a
  REAL on-disk YOLO result jsonl through the real marshal and the real schema in the backend
  container; and end to end, by `POST /workflows/pagb7vj8/run` returning a 5-object detection result
  (capture `db9721b27fc54b8f993fb06ef8738631`) while `results` stayed `{"total":0,...}` and the log
  carried the ValidationError, the `store_inference_result` traceback, and the HTTPException re-raise.
  **`anomalyScore` and `anomalyThreshod` are NOT the problem** — the marshal supplies both for
  detection captures (`Anomaly_score` from the inference score, `Anomaly_threshold` = 1.0), so
  `prediction` is the single failing field.
- **Migration answer, Defect A, stated as an observable consequence**: pipeline resolution is
  CREATION-time and persisted, not runtime. A `default_camera_configurations.json` change alone fixes
  only Image_Sources created AFTER the build; `my6j3zx1` would keep its stale Bayer chain and keep
  rendering mangled frames. Two routes were considered: (i) tell the user to delete and recreate the
  Image_Source — zero code risk, but it leaves a landmine for every static-camera source created
  before the fix and requires manual action on every device; (ii) a scoped one-time backfill through
  the EXISTING `db_backfill.backfill()` startup hook that rewrites the stored `processingPipeline`
  only when the Image_Source's `cameraId` is the static camera AND the stored value is EXACTLY the
  known-wrong `default`/`default` string. **(ii) is chosen** — it reuses a proven, idempotent,
  non-fatal mechanism already wired at startup (`app.py` line 273), needs no schema migration, and
  the exact-string condition means a deliberately customized pipeline and every physical camera's
  identical Bayer chain are provably untouched. Route (i) remains the documented fallback in task
  8(b) if the backfill is dropped.
- **Lookup shape, answered**: vendor-then-model, each falling back to `default`
  (`image_source_accessor.py` lines 277-278), read as
  `.get(vendor).get(model).get("processingPipeline")` (line 282). Every existing top-level key
  carries a `default` sub-key — confirmed by reading the file — and the code REQUIRES it, so the new
  `AWS-DDA` entry must carry one. The RGB-native vendors' value shape
  (`{"processingPipeline": "capsfilter caps=video/x-raw,format=RGB ! videoconvert"}`) is the shape to
  copy; `device` / `deviceName` / `gain` / `exposure` appear only under `Nvidia CSI` and `ICAM`,
  which short-circuit before the vendor lookup.
- **Scope decisions, Defect B**: the summary's missing detection bucket IS a second, separate defect
  (`inference_result_dao.py` lines 99-115 derive `totalInference` from `normal + anomaly` only), so it
  gets its own clauses (1.15 / 2.9 / 3.15) rather than being folded into the persistence fix — it
  stays wrong after the persistence fix and the user's report explicitly names the bottom view. The
  history-page label is included because 6.1 is what first puts a Detection row in front of a
  component that hardcodes "Anomaly". The observability fix is included and kept to one try/except
  per background task: cheap, low-risk, and this class of invisible failure is precisely why the
  defect survived. `logging.conf` is deliberately NOT touched — its `fileHandler` is attached only to
  the unused `cameraStation` logger, container stdout already carries the full traceback (proven
  live), and re-routing handlers inside a component build risks the whole log pipeline for no gain.
- **Deliberately out of scope**: deriving the CLASSIC path's appsrc caps from the frame's own
  `pixel_format` tag the way `pipeline_executor._frame_caps` already does. That is the more general
  fix for Defect A and it would make every camera's caps truthful — but it changes the hot path for
  every physical camera and the user's preservation list explicitly forbids altering any physical
  vendor's pipeline. If it is wanted, it belongs in its own spec with its own physical-hardware
  verification. Also out of scope: a dedicated "Detections" tile in `ResultAnalyticsSummary`
  (its 4-column layout is full), widening the `prediction` query filter and the history filter to
  accept `Detection`, and standing up a jest/vitest suite for the device HMI.
- **Verified NOT defects** (preservation clauses, not fixes): the store's packed-RGB output with the
  `"RGB"` tag is CORRECT and must not change — the workflow Frame_Feed path depends on that tag and
  `_frame_caps` (`pipeline_executor.py` line 2821) already turns it into `video/x-raw,format=RGB`, so
  **that path is already correct for the static camera** and `test_workflow_feed.py`'s
  `"appsrc name=appsrc caps=video/x-raw,format=RGB "` assertion documents it. The `default`/`default`
  BGGR chain is correct for unknown PHYSICAL vendors and stays. `convert_inference_res_to_save_in_db`
  is correct and unchanged. The marshal's `"Detection"` typing is correct and unchanged.
- **Risks**: (1) the backfill rewrites a user-editable field; the exact-known-wrong-string condition
  is what bounds it, and task 2 pins the no-op cases including the five physical-camera rows on
  `jetson-thor1` that store the same Bayer chain. (2) Widening the stored prediction vocabulary makes
  `Detection` reachable in code paths that assume a Normal/Anomaly binary; tasks 3 and 4 pin
  `humanClassification`, the results-list filter, `OUTPUT_RULE`, the download-file check, and the SMGT
  manifest as unchanged, and 6.3 fixes the one rendering path that assumed the binary. (3) The
  history-page label change has no automated coverage because the device HMI has no test suite; it is
  one line, mirrors an existing pattern, and is verified visually in task 8(l). (4) `totalInference`
  changes meaning for workflows that have Detection rows — by design, since those rows are currently
  counted nowhere; `normal` and `anomaly` are untouched so no existing display regresses.
