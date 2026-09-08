# Bugfix Requirements Document

## Introduction

Two independent device-side defects, both confirmed on `jetson-thor1` running
`aws.edgeml.dda.LocalServer.arm64JP7` **1.0.24**, both fixed in the LocalServer component and
therefore both riding the SAME Greengrass component build.

**Defect A — the Static_Image_Camera's frames are Bayer-demosaiced, destroying the pinned image.**
After pinning a test image and creating an Image_Source from `static-image-camera`, the device HMI
"Image preview" renders correct GEOMETRY (the pinned image's text labels readable, border and
diagonal X present) but destroyed colors: an overall gray cast with fine diagonal hatching and
rainbow/chromatic fringing on every edge. That signature is a Bayer demosaic applied to
already-packed RGB. The Static_Image_Camera store serves packed RGB and tags every frame
`pixel_format: "RGB"`, but the classic Image_Source path resolves its GStreamer conversion chain by
camera VENDOR against `src/backend/utils/config/default_camera_configurations.json`. The static
camera's vendor is `AWS-DDA`, which is not a key in that file, so it falls through to
`default`/`default` — a BGGR Bayer demosaic. `create_buffer()` then derives the appsrc caps from the
FIRST `caps=` clause of that chain, so the packed-RGB bytes are declared to GStreamer as a BGGR
mosaic and `bayer2rgb` mangles them. This breaks the BASE `static-image-camera-source` feature —
preview, capture, AND workflow inference through the classic Image_Source path — defeating the whole
point of a known, reproducible input. Confirmed live in the running container logs (see 1.4).

**Defect B — manually-run inference on detection (YOLO) models never persists a result.** The
inference runs and renders in the UI, but the saved history stays empty: "when I manually run
inference on legacy style pipelines, it does not save the results on the bottom view and view all
results are empty." The marshal types a detection capture's `Inference result` as `"Detection"`,
which is correct and deliberate. `InferenceResultSchema.prediction` validates against
`PREDICTION = ['Normal', 'Anomaly']`, so the row is rejected. Because the manual-run route persists
through a FastAPI BackgroundTask AFTER the response is sent, the failure cannot reach the user: the
result renders, the UI reports no error, and nothing is saved. Reproduced end to end on
`jetson-thor1` with the exact exception (see 1.9 through 1.13).

Neither defect is anything the sibling spec `static-image-camera-binding-and-pin-discoverability`
touched. That spec's portal fixes are deployed and its DEVICE fix (de-duplicating the
Static_Image_Camera registration in `src/backend/camera_sync/inventory.py` plus a one-shot
shadow-key retirement in `src/backend/camera_sync/agent.py`) is implemented, tested, and committed,
awaiting only the user's build (its task 10). **The fixes in this spec must land in that SAME build**
so ONE ~100-minute build delivers all three device fixes. Nothing in this spec modifies that spec's
files or documents.

## Bug Analysis

### Current Behavior (Defect)

Observed on `jetson-thor1` (LocalServer `arm64JP7` 1.0.24, backend container
`awsedgemlddalocalserverarm64jp7-backend_tegra_gpu_enabled-1`, LocalServer API on
`http://127.0.0.1:5000`) and confirmed against the shipped source.

**Defect A — static camera frames Bayer-demosaiced**

1.1 WHEN an Image_Source of type `Camera` is created for `cameraId: "static-image-camera"` with no explicit `imageSourceConfiguration` THEN the system resolves the conversion chain by vendor and model — `ImageSourceAccessor.__get_default_image_source_configuration` (`src/backend/resources/accessors/image_source_accessor.py` line 246) calls `aravis_functions.getCamera(cameraId)` and reads `get_vendor_name()` / `get_model_name()`, which `_StaticImageCameraHandle` (`src/backend/edge_ml1_p_camera_management/aravis_functions.py` lines 164, 167) answers `"AWS-DDA"` / `"Static Image Camera"` — and because neither is a key in `default_camera_configurations.json` (whose top-level vendor keys are exactly `Lucid Vision Labs`, `Zebra Technologies`, `Basler`, `Allied Vision`, `OMRON SENTECH`, `Nvidia CSI`, `ICAM`, `default`) the two-step lookup at lines 277-278 collapses both to `default`, yielding `default`/`default`

1.2 WHEN that resolution completes THEN the system PERSISTS the resulting BGGR Bayer chain `capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert` into the Image_Source_Configuration row, because `__create_image_source_configuration` (line 237) computes the default ONCE at CREATION time and stores it; live on `jetson-thor1`, Image_Source `my6j3zx1` ("static-image-camera1", `cameraId: static-image-camera`) points at config `jk1y5ln8` whose stored `processingPipeline` is exactly that Bayer string

1.3 WHEN preview or capture runs for that Image_Source THEN the system reads only the PERSISTED value — `GstPipelineBuilder._add_camera_image_source` (`src/backend/gstreamer/pipeline_builder.py` lines 50-52) appends `image_source.get("processingPipeline")` verbatim after `appsrc` — and `GstPipelineManager.create_buffer` (`src/backend/gstreamer/gst_pipeline.py` lines 61-72) then sets the appsrc caps from the FIRST `caps=` clause found by the regex `caps=([^!]+)`, so the packed-RGB Pinned_Image bytes are declared to GStreamer as `video/x-bayer,format=bggr`

1.4 WHEN `bayer2rgb` demosaics those already-packed RGB bytes THEN the system renders geometry correctly but destroys color — an overall gray cast, fine diagonal hatching, and rainbow/chromatic fringing on every edge — so the pinned 640x480 PNG's deep-blue field, yellow border, red disc, and white diagonals (sha256 `83cca4bb036a4faef925a54b8b34e784a7d04e7b67dc1079ba6e139fc9ea5205`, still pinned on `jetson-thor1`) are all lost; live container log for `POST /image-sources/my6j3zx1/preview`, request `d5c45a4d99fe4a578ee35e2a4fc92341`, shows the executed pipeline character-for-character: `appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! videocrop top=0 bottom=0 left=0 right=0 ! jpegenc idct-method=2 quality=100 ! filesink location=/aws_dda/image-capture/preview/default_file_prefix-my6j3zx1.jpg`

1.5 WHEN a workflow runs inference on that Image_Source through the classic Image_Source path THEN the system feeds the same mangled frame to the model — `endpoints/workflow.py` `configure_image_source_and_run_pipeline` grabs the frame and hands it to `execute_workflow_pipeline`, which builds the same `appsrc` + persisted-chain prefix — so preview, capture, AND classic-path inference are all affected

1.6 WHEN the Pixel_Format of the frame IS in fact known THEN the system ignores it on this path: `StaticImageStore._decode_frame_locked` (`src/backend/utils/static_image_camera.py` lines ~301-322) returns `{"data": rgb.tobytes(), "width", "height", "pixel_format": "RGB"}` with `len(data) == 3 * width * height` (docstring, line ~232), and `camera_manager.get_camera_frame` (`src/backend/utils/camera_manager.py` line 618) short-circuits for the static camera and returns that dict verbatim, tag included — yet the classic path's caps come from the vendor-keyed JSON, never from the tag

1.7 WHEN the base feature chose this fall-through THEN the system treats a camera whose format is KNOWN exactly like an unknown physical GenICam camera ("that path resolves to the 'default' configuration exactly like an unknown physical vendor/model", base spec Requirement 4.1); a BGGR guess is defensible for an unknown physical bus camera and is wrong for this one

1.8 WHEN the fix is delivered as a `default_camera_configurations.json` entry alone THEN the system does NOT heal `my6j3zx1`, because the pipeline was resolved at creation time and persisted (1.2) and no code path recomputes it — `update_image_source` only re-creates a configuration when the caller SUPPLIES an `imageSourceConfiguration`, and it stores that payload as given without consulting the defaults — so the existing Image_Source keeps serving Bayer-mangled frames indefinitely

**Defect B — manually-run detection inference never persists**

1.9 WHEN a detection (object-detection) capture is marshalled THEN the system sets `inf_result["Inference result"] = "Detection"` (`src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py` line 399), deliberately typed distinctly and "never labeled Anomaly/Normal", and `GetInferenceResults.save_image_object`'s detection branch (`src/backend/utils/inference_results_utils.py` line 74 onward) produces a complete `infer_res` carrying `confidence`, `inference_result`, `anomaly_score`, `anomaly_threshold`, `detections`, and `detection_count`

1.10 WHEN `convert_inference_res_to_save_in_db` marshals that result for the database (`src/backend/utils/inference_results_utils.py` line ~330) THEN the system succeeds and produces `prediction: "Detection"` along with valid `confidence`, `anomalyScore`, `anomalyThreshod`, `inputImageFilePath`, and `outputImageFilePath` — the marshal is NOT the failure point

1.11 WHEN `store_inference_result` validates that row (`src/backend/resources/accessors/inference_result_accessor.py` line 58, `result = self.schema.load(data)`) THEN the system raises `marshmallow.exceptions.ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}`, because `InferenceResultSchema.prediction` is `fields.Str(validate=validate.OneOf(PREDICTION), required=True)` (`src/backend/model/inference_result.py` line 83) and `PREDICTION = [NORMAL, ANOMALY] = ['Normal', 'Anomaly']` (`src/backend/utils/constants.py` line 53) — the vocabulary never grew a detection member when the detection marshal branch was added

1.12 WHEN that ValidationError is caught (same file, line 65) THEN the system re-raises it as `HTTPException: 400: Unable to store inference result. {'prediction': ['Must be one of: Normal, Anomaly.']}'.` and no row is written

1.13 WHEN that happens inside the manual-run route THEN the system cannot surface it: `@router.post("/workflows/{workflow_id}/run")` (`src/backend/endpoints/workflow.py`) computes and RETURNS the result, then persists via `background_tasks.add_task(save_full_inference_result, result, workflow)` (line 172) → `save_full_inference_result` (line 124) → `store_inference_result` (line 131), so display and persistence are decoupled and a persistence failure can never reach the response; the user sees a correct rendered result, no error, and an empty history. Reproduced live on `jetson-thor1`: `POST /workflows/pagb7vj8/run` returned HTTP 200 with a full 5-object detection result (`inference_result: "Detection"`, `detection_count: 5`, persons + bus, capture `db9721b27fc54b8f993fb06ef8738631`), while `GET /workflows/pagb7vj8/results` stayed `{"total":0,...}` and the container log carried `[error] {'prediction': ['Must be one of: Normal, Anomaly.']} [resources.accessors.inference_result_accessor]` followed by the `store_inference_result` traceback and the `HTTPException: 400` re-raise

1.14 WHEN the defect is observed across the device's workflows THEN the system shows a perfect split by model task — every segmentation model persists and every YOLO model persists nothing (`GET /workflows/{id}/results` on `jetson-thor1`, all six workflows legacy-shaped with `imageSourceId` / `featureConfigurations`, all `type: LFVModel`): `atubh5ft` "rf-detrtest" (`model-rf-detr-seg-nano-jetson-xavier-jp7`) 5 results, `6q3lur2w` "cookies-segmentation" (`model-cookies-segmentation-onnx-jetson-xavier-jp7`) 14, `78wzlpjn` "camera_rf_detr" (`model-rf-detr-seg-nano-jetson-xavier-jp7`) 4, versus `pagb7vj8` "yolotest" (`model-yolo-test-jetson-xavier-jp7`) 0, `adxkab93` "camera_yolo" (same model) 0, `515zkeve` "yoloworld_blue_plate_test" (`model-yolo-world-blue-plate-jetson-xavier-jp7`) 0; every persisted row is segmentation-shaped, carrying `anomalyScore` / `maskImage` / `maskBackground`

1.15 WHEN the results summary is computed THEN the system counts ONLY Normal and Anomaly and derives the total from them — `get_inference_result_summary` (`src/backend/dao/sqlite_db/inference_result_dao.py` lines 99-115) runs one count filtered `prediction == NORMAL` and one filtered `prediction == ANOMALY` and returns `{"totalInference": normal_count + anomaly_count, "normal": ..., "anomaly": ...}` — so a Detection row is counted nowhere and `totalInference` under-reports; live payload is `{"stats":{"totalInference":0,"normal":0,"anomaly":0},"lastResetTime":1786814771}` with NO detection bucket, and this remains true even after 1.11 is fixed, making it a SECOND, independent defect rather than a symptom of the first

1.16 WHEN a Detection row eventually reaches the "view all results" history page THEN the system mislabels it: `ClassificationTypeTag` in `src/frontend/src/components/result-history/ColoredInferenceBox.tsx` renders `Normal` as a success indicator and EVERYTHING ELSE as `<StatusIndicator type="error">Anomaly</StatusIndicator>`, hardcoding the label, so a Detection result would read "Anomaly" — unlike the live card (`src/frontend/src/components/live-result/LiveResultCard.tsx`), which already understands `PredictionType.Detection` and renders the prediction string itself

1.17 WHEN a background-task persistence failure occurs THEN the system logs it anonymously and nowhere a user looks: the traceback lands in container stdout with no statement of WHICH workflow or capture failed to persist, buried in a log that carries >111,000 lines on this device, and the `logging.conf` FileHandler that would write `aws.edgeml.dda.LocalServerCameraApp.log` is attached only to the unused `cameraStation` logger (`qualname=cameraStation`) while every module uses `logging.getLogger(__name__)`, so that file is never a usable destination

### Expected Behavior (Correct)

**Defect A**

2.1 WHEN the conversion chain is resolved for `cameraId: "static-image-camera"` THEN the system SHALL resolve an RGB passthrough chain — `capsfilter caps=video/x-raw,format=RGB ! videoconvert`, the same chain the RGB-native physical vendors (`Lucid Vision Labs`, `Allied Vision`, `OMRON SENTECH`) already use — instead of the `default`/`default` Bayer demosaic

2.2 WHEN that entry is added to `default_camera_configurations.json` THEN the system SHALL key it by the shipped enumeration identity, and the test SHALL assert the literal JSON key agrees with `STATIC_IMAGE_CAMERA_IDENTITY["vendor"]` (and with `["model"]` if a model sub-key is used), so a future identity change fails loudly instead of silently reverting to the Bayer default

2.3 WHEN preview or capture runs for a static-camera Image_Source THEN the system SHALL declare the appsrc caps as `video/x-raw,format=RGB`, apply no demosaic, and render the Pinned_Image with correct colors — deep-blue field, yellow border, red disc, white diagonals, no diagonal hatching, no rainbow fringing

2.4 WHEN a workflow runs inference on a static-camera Image_Source through the classic Image_Source path THEN the system SHALL feed the model the same unmangled pixels the store decoded, so the feature delivers the reproducible input it exists to provide

2.5 WHEN an Image_Source for the static camera ALREADY exists carrying the persisted `default`/`default` Bayer chain (`my6j3zx1` on `jetson-thor1`) THEN the system SHALL converge it to the RGB chain on its own rather than requiring the user to delete and recreate the source: a one-time, idempotent backfill SHALL rewrite the stored `processingPipeline` for Image_Source_Configurations belonging to Image_Sources whose `cameraId` is `STATIC_IMAGE_CAMERA_ID` and whose stored value is EXACTLY the known-wrong `default`/`default` string, leaving any other value — including a deliberate user customization — untouched

**Defect B**

2.6 WHEN a detection capture's row is validated for storage THEN the system SHALL accept `prediction: "Detection"` as a valid stored prediction, so the detection result persists with the same `captureId`, `confidence`, `anomalyScore`, `anomalyThreshod`, `inputImageFilePath`, and `outputImageFilePath` values `convert_inference_res_to_save_in_db` already produces

2.7 WHEN the stored-prediction vocabulary is widened THEN the system SHALL widen it for the STORED `prediction` field only, and SHALL NOT widen `humanClassification` (a human's Normal-or-Anomaly verdict), the `prediction` query filter on `GET /workflows/{id}/results`, the digital-output `OUTPUT_RULE` set, or the download-file prediction check

2.8 WHEN a manual run on a detection workflow completes THEN the system SHALL persist exactly one result row per capture and that row SHALL appear in `GET /workflows/{id}/results`, so the bottom view and "view all results" are populated for YOLO workflows exactly as they already are for segmentation workflows

2.9 WHEN the results summary is computed THEN the system SHALL count Detection rows: it SHALL report a `detection` count alongside `normal` and `anomaly`, and `totalInference` SHALL be the sum of all three so it means what its name says

2.10 WHEN a Detection row is rendered on the "view all results" history page THEN the system SHALL label it by its own prediction value rather than as "Anomaly", mirroring the live card's existing `PredictionType.Detection` handling

2.11 WHEN persisting a result fails for any reason THEN the system SHALL log that failure loudly and identifiably from the background task — naming the workflow id, the capture id, and the exception — so this class of defect cannot be invisible again; the manual-run RESPONSE SHALL remain unchanged, because failing the response on a persistence error would regress every currently working model

### Unchanged Behavior (Regression Prevention)

**Defect A**

3.1 WHEN a physical camera's conversion chain is resolved THEN the system SHALL CONTINUE TO return exactly today's string for every shipped vendor and model — all eight existing top-level keys (`Lucid Vision Labs`, `Zebra Technologies`, `Basler`, `Allied Vision`, `OMRON SENTECH`, `Nvidia CSI`, `ICAM`, `default`) and every model sub-key under them byte-for-byte unchanged, including the `default`/`default` BGGR chain that unknown physical vendors still and correctly fall through to

3.2 WHEN the vendor/model lookup runs THEN the system SHALL CONTINUE TO use the existing two-step shape — vendor key if present else `default`, then model key within that vendor if present else `default` — and every top-level key SHALL CONTINUE TO carry a `default` sub-key, which the code requires because `self.default_camera_config.get(cameraVendor).get(cameraModel).get("processingPipeline")` would raise on a vendor without one

3.3 WHEN the Static_Image_Camera serves a frame THEN the store SHALL CONTINUE TO emit packed RGB — `{'data', 'width', 'height', 'pixel_format': 'RGB'}` with `len(data) == 3 * width * height`, EXIF-transposed, deterministic, and `(inode, mtime_ns, size)`-cached — with `_decode_frame_locked`, `get_frame`, the store lock, the atomic replace, and `StaticImageUnavailableError` all unchanged; the packed-RGB output is CORRECT and the workflow Frame_Feed path depends on the `"RGB"` tag

3.4 WHEN the newer workflow Frame_Feed path builds its appsrc caps THEN the system SHALL CONTINUE TO derive them from the frame's own `pixel_format` tag — `pipeline_executor._frame_caps` (`src/backend/workflow_engine/pipeline_executor.py` line 2821) maps a non-`bayer:` tag to `video/x-raw,format={tag}`, so a static-camera frame already yields `video/x-raw,format=RGB` and that path is ALREADY CORRECT; the existing assertion `"appsrc name=appsrc caps=video/x-raw,format=RGB "` in `test/backend-test/static_image_camera/test_workflow_feed.py` SHALL keep passing untouched

3.5 WHEN a camera's PFNC pixel format is mapped THEN `_PFNC_TO_TAG` and `gst_pixel_format()` (`src/backend/utils/camera_manager.py`) SHALL CONTINUE TO map exactly today's codes to exactly today's tags, with the bytes-per-pixel fallback for unmapped formats unchanged

3.6 WHEN the static camera is enumerated or opened THEN `getCameras()`, `rescan_cameras()`, the `getCamera()` short-circuit, and `_StaticImageCameraHandle` (including `get_vendor_name()` / `get_model_name()` returning `AWS-DDA` / `Static Image Camera`) SHALL CONTINUE TO behave exactly as today; the identity is the JSON key's source of truth and is NOT changed

3.7 WHEN `camera_manager.get_camera_frame` is called for the static camera THEN the system SHALL CONTINUE TO short-circuit before touching `get_frame_lock`, `camera_objects`, or `connect_camera`, accept-and-ignore acquisition config, and raise the camera-naming error when nothing is pinned

3.8 WHEN an Image_Source is created for any camera other than the static one, or with an explicit `imageSourceConfiguration` supplied THEN the system SHALL CONTINUE TO behave identically — same `gain: 1` / `exposure: 500` defaults, same NVIDIA CSI and ICAM special cases, same store-the-payload-as-given behavior on update — and the backfill SHALL touch no configuration whose stored `processingPipeline` differs from the exact known-wrong `default`/`default` string, so a deliberately customized static-camera pipeline survives

**Defect B**

3.9 WHEN a segmentation model's result is persisted THEN the system SHALL CONTINUE TO persist it with an identical row shape and identical values — `prediction` of `Normal` or `Anomaly`, `anomalyScore`, `anomalyThreshod`, `maskImage`, `maskBackground`, `anomalyLabels`, `confidence`, `humanReviewRequired`, `modelConfidenceThresholds` — with the existing 5 / 14 / 4 rows on `atubh5ft` / `6q3lur2w` / `78wzlpjn` unaffected

3.10 WHEN a classification model's result is persisted THEN the system SHALL CONTINUE TO persist it exactly as today, and the marshal's THREE branches in `save_image_object` (detection, segmentation, classification) and their selection predicates `is_detection_model_output_result` / `is_segmentation_model_output_result` SHALL CONTINUE TO produce byte-identical `infer_res` dicts for every non-detection model

3.11 WHEN the marshal types a capture THEN `marshal_for_capture_template.py` SHALL CONTINUE TO emit `"Detection"` for detection captures and `"Anomaly"` / `"Normal"` otherwise, unchanged — the fix widens what the STORE accepts, never what the marshal produces, so `test_marshal_payload_discrimination.py` and the other marshal suites SHALL keep passing untouched

3.12 WHEN `convert_inference_res_to_save_in_db` marshals a row THEN the system SHALL CONTINUE TO read the same keys, call `get_default_configs_lfv` the same way, strip `None` values the same way, and produce the same output for every input it already handles; the fix does NOT change this function

3.13 WHEN a human classification is recorded THEN the system SHALL CONTINUE TO restrict `humanClassification` to `Normal` or `Anomaly` on both `InferenceResultSchema` and `CapturedDataSchema`, because a human verdict is a binary judgement and not a model task type

3.14 WHEN `GET /workflows/{id}/results` is called THEN the system SHALL CONTINUE TO return the same paginated shape with the same filters and the same `prediction` query Literal of `Normal` / `Anomaly`, SHALL CONTINUE TO apply no prediction filter when the parameter is absent, and SHALL CONTINUE TO order by `inferenceCreationTime` descending

3.15 WHEN `GET /workflows/{id}/results/summary` is called THEN the system SHALL CONTINUE TO return the same envelope `{"stats": {...}, "lastResetTime": ...}` with `normal` and `anomaly` carrying exactly today's values for existing data and `lastResetTime` sourced from the same workflow metadata; the `detection` key is ADDITIVE, and `totalInference` changes only by including detections that are currently counted nowhere

3.16 WHEN the results export / download and SageMaker Ground Truth manifest paths run THEN `generate_smgt_format_manifest`, the download-file prediction check, and the bulk-update path SHALL CONTINUE TO behave exactly as today for Normal and Anomaly rows

3.17 WHEN a manual run completes THEN the RESPONSE payload SHALL CONTINUE TO be byte-identical — the same `captureId`, `inferenceResult`, `processingTime`, `image` (or its absence when `returnImageString` is false), and the same early-return shape when `returnPartialResultsEarly` is true — and persistence SHALL CONTINUE TO happen in a background task that cannot fail the response

3.18 WHEN a result renders on the live card THEN the system SHALL CONTINUE TO render `PredictionType.Detection` as it does today, with the "Objects detected" list from the `detections` block, and Normal / Anomaly rendering on both the live card and the history page SHALL CONTINUE TO be visually identical

3.19 WHEN this fix is delivered THEN the system SHALL CONTINUE TO expose every affected route unchanged in shape and status codes, SHALL introduce no database schema migration (the `prediction` column is already a free-text string), and SHALL leave `logging.conf` untouched — the loud persistence-failure log is added at the call site rather than by re-routing handlers in a component build

3.20 WHEN this build is prepared THEN it SHALL CONTINUE TO carry the sibling spec `static-image-camera-binding-and-pin-discoverability`'s already-committed device fix unchanged — the inventory de-duplication in `src/backend/camera_sync/inventory.py` and the one-shot shadow-key retirement in `src/backend/camera_sync/agent.py` — and this spec SHALL modify neither those files nor that spec's documents

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type StaticCameraPipelineResolution OR StoredStaticSourceConfig
         OR DetectionResultPersistence OR ResultsSummaryQuery
  OUTPUT: boolean

  // Defect A part 1 (provisioning): resolving the conversion chain for a
  // camera whose vendor is the static camera's identity yields a Bayer
  // demosaic, because the vendor is absent from the config file.
  a1 := X IS StaticCameraPipelineResolution
        AND X.cameraId = STATIC_IMAGE_CAMERA_ID
        AND resolvedPipeline(X) = bayerDefaultChain()

  // Defect A part 2 (migration): an ALREADY stored configuration for a
  // static-camera Image_Source still carries that Bayer chain.
  a2 := X IS StoredStaticSourceConfig
        AND X.imageSource.cameraId = STATIC_IMAGE_CAMERA_ID
        AND X.config.processingPipeline = bayerDefaultChain()

  // Defect B part 1 (persistence): a marshalled row whose prediction is
  // the detection type is rejected by the stored-row validation.
  b1 := X IS DetectionResultPersistence
        AND X.row.prediction = DETECTION
        AND NOT validatesForStorage(X.row)

  // Defect B part 2 (summary): a Detection row is counted in no bucket
  // and excluded from the total.
  b2 := X IS ResultsSummaryQuery
        AND EXISTS r IN rowsInWindow(X) WHERE r.prediction = DETECTION
        AND summary(X).totalInference < COUNT r IN rowsInWindow(X)

  RETURN a1 OR a2 OR b1 OR b2
END FUNCTION

FUNCTION bayerDefaultChain()
  OUTPUT: string

  // The shipped default/default fall-through, read from the config file
  // rather than hardcoded, so the two halves of Defect A cannot drift.
  RETURN defaultCameraConfig()['default']['default'].processingPipeline
END FUNCTION

FUNCTION rgbPassthroughChain()
  OUTPUT: string

  // The chain the RGB-native physical vendors already use.
  RETURN 'capsfilter caps=video/x-raw,format=RGB ! videoconvert'
END FUNCTION

FUNCTION resolvedPipeline(X)
  INPUT: X of type StaticCameraPipelineResolution
  OUTPUT: string

  vendor := getCamera(X.cameraId).get_vendor_name()
  model  := getCamera(X.cameraId).get_model_name()
  vendorKey := vendor IF vendor IN defaultCameraConfig() ELSE 'default'
  modelKey  := model IF model IN defaultCameraConfig()[vendorKey] ELSE 'default'
  RETURN defaultCameraConfig()[vendorKey][modelKey].processingPipeline
END FUNCTION

FUNCTION validatesForStorage(row)
  INPUT: row of type InferenceResultRow
  OUTPUT: boolean

  // True when InferenceResultSchema().load(row) succeeds.
  RETURN NOT raises(InferenceResultSchema.load, row)
END FUNCTION
```

### Property 1: Fix Checking (Defect A, static camera pixel format)

```pascal
FOR ALL X WHERE isBugCondition(X) AND (X IS StaticCameraPipelineResolution
                                       OR X IS StoredStaticSourceConfig) DO
  IF X IS StaticCameraPipelineResolution THEN
    // The provisioning lookup now yields RGB, and the JSON key is the
    // shipped identity rather than a drifting literal.
    ASSERT resolvedPipeline'(X) = rgbPassthroughChain()
    ASSERT STATIC_IMAGE_CAMERA_IDENTITY.vendor IN defaultCameraConfig'()
    ASSERT 'default' IN defaultCameraConfig'()[STATIC_IMAGE_CAMERA_IDENTITY.vendor]
    // The pipeline the classic path actually executes declares RGB caps
    // and inserts no demosaic.
    launch ← buildImageSourcePipeline'(X)
    ASSERT firstCaps(launch) = 'video/x-raw,format=RGB'
    ASSERT 'bayer2rgb' NOT IN launch
  ELSE
    // The already-stored Bayer chain converges without user action.
    ASSERT backfill'(X).config.processingPipeline = rgbPassthroughChain()
    ASSERT backfill'(backfill'(X)) = backfill'(X)   // idempotent
  END IF
END FOR
```

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

### Property 2: Preservation Checking (Defect A, physical cameras, store, Frame_Feed)

```pascal
// Every vendor and model already in the config file resolves byte-for-byte
// as before, including the default/default Bayer fall-through that unknown
// PHYSICAL vendors still and correctly use.
FOR ALL vendor, model IN preFixConfigKeys() DO
  ASSERT defaultCameraConfig'()[vendor][model] = defaultCameraConfig()[vendor][model]
END FOR
FOR ALL vendor IN defaultCameraConfig'() DO
  ASSERT 'default' IN defaultCameraConfig'()[vendor]   // required by the lookup
END FOR

// Every non-static camera provisions exactly as before.
FOR ALL X WHERE X.cameraId != STATIC_IMAGE_CAMERA_ID DO
  ASSERT resolvedPipeline'(X) = resolvedPipeline(X)
END FOR

// The store's frame contract is untouched: packed RGB, tagged RGB, 3*W*H.
FOR ALL pinnedImage, grabs, acquisitionConfig DO
  ASSERT getFrame'(pinnedImage) = getFrame(pinnedImage)
  ASSERT getFrame'(pinnedImage).pixel_format = 'RGB'
  ASSERT LENGTH(getFrame'(pinnedImage).data)
       = 3 * getFrame'(pinnedImage).width * getFrame'(pinnedImage).height
END FOR

// The Frame_Feed path was already correct and stays correct.
FOR ALL frame DO
  ASSERT frameCaps'(frame) = frameCaps(frame)
END FOR
ASSERT frameCaps'(staticFrame) = 'video/x-raw,format=RGB'

// Enumeration, identity, and the PFNC map are untouched.
ASSERT getCameras'() = getCameras()
FOR ALL cameraId DO ASSERT getCamera'(cameraId) = getCamera(cameraId) END FOR
ASSERT gstPixelFormat' = gstPixelFormat
ASSERT STATIC_IMAGE_CAMERA_IDENTITY' = STATIC_IMAGE_CAMERA_IDENTITY

// The backfill is scoped: it rewrites only the exact known-wrong string on
// a static-camera source, and nothing else in the database.
FOR ALL X WHERE NOT (X.imageSource.cameraId = STATIC_IMAGE_CAMERA_ID
                     AND X.config.processingPipeline = bayerDefaultChain()) DO
  ASSERT backfill'(X) = X
END FOR
```

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

### Property 3: Fix Checking (Defect B, detection result persistence and summary)

```pascal
FOR ALL X WHERE isBugCondition(X) AND (X IS DetectionResultPersistence
                                       OR X IS ResultsSummaryQuery) DO
  IF X IS DetectionResultPersistence THEN
    // The detection row validates and stores, unchanged in every other field.
    ASSERT validatesForStorage'(X.row) = TRUE
    ASSERT storedRow'(X).prediction = DETECTION
    FOR ALL field IN fields(X.row) WHERE field != 'prediction' DO
      ASSERT storedRow'(X)[field] = X.row[field]
    END FOR
    // The widening is scoped to the stored prediction field only.
    ASSERT allowedHumanClassification'() = allowedHumanClassification()
    ASSERT resultsListPredictionFilterValues'() = resultsListPredictionFilterValues()
    ASSERT OUTPUT_RULE' = OUTPUT_RULE
  ELSE
    // Detections are counted, and the total means what it says.
    s ← summary'(X)
    ASSERT s.detection = COUNT r IN rowsInWindow(X) WHERE r.prediction = DETECTION
    ASSERT s.totalInference = s.normal + s.anomaly + s.detection
    ASSERT s.totalInference = COUNT r IN rowsInWindow(X)
  END IF
END FOR

// A persistence failure is loud and identifiable, and never reaches the
// response.
FOR ALL X WHERE persistenceRaises(X) DO
  ASSERT loggedError'(X) CONTAINS X.workflowId AND CONTAINS X.captureId
  ASSERT manualRunResponse'(X) = manualRunResponse(X)
END FOR
```

**Validates: Requirements 2.6, 2.7, 2.8, 2.9, 2.10, 2.11**

### Property 4: Preservation Checking (Defect B, non-detection results and endpoint shapes)

```pascal
// Every non-detection row validates, stores, and reads back identically.
FOR ALL X WHERE X.row.prediction != DETECTION DO
  ASSERT validatesForStorage'(X.row) = validatesForStorage(X.row)
  ASSERT storedRow'(X) = storedRow(X)
END FOR

// The marshal is untouched for all three branches.
FOR ALL outputList DO
  ASSERT saveImageObject'(outputList) = saveImageObject(outputList)
  ASSERT isDetectionOutput'(outputList) = isDetectionOutput(outputList)
  ASSERT isSegmentationOutput'(outputList) = isSegmentationOutput(outputList)
END FOR

// The db marshal is untouched.
FOR ALL inferenceRes, workflow DO
  ASSERT convertForDb'(inferenceRes, workflow) = convertForDb(inferenceRes, workflow)
END FOR

// The summary keeps today's values for existing data and the same envelope.
FOR ALL X WHERE NO r IN rowsInWindow(X) HAS r.prediction = DETECTION DO
  ASSERT summary'(X) = summary(X) EXTENDED WITH {detection: 0}
END FOR

// The list endpoint's shape, filters, and ordering are unchanged.
FOR ALL query DO ASSERT listResults'(query) = listResults(query) END FOR

// The manual-run response is byte-identical in both return modes.
FOR ALL runRequest DO
  ASSERT manualRunResponse'(runRequest) = manualRunResponse(runRequest)
END FOR

// Normal and Anomaly render identically on both views.
FOR ALL prediction IN {NORMAL, ANOMALY} DO
  ASSERT renderHistoryTag'(prediction) = renderHistoryTag(prediction)
  ASSERT renderLiveCard'(prediction) = renderLiveCard(prediction)
END FOR
```

**Validates: Requirements 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19**

**Key Definitions:**
- **F**: `ImageSourceAccessor.__get_default_image_source_configuration` plus
  `default_camera_configurations.json`, and the stored Image_Source_Configuration rows (Defect A);
  `InferenceResultSchema` / `PREDICTION`, `get_inference_result_summary`,
  `save_full_inference_result`, and `ClassificationTypeTag` (Defect B) — all as they exist before
  the fix
- **F'**: the same code after adding the static camera's RGB entry keyed by the shipped identity, the
  scoped one-time backfill of already-stored Bayer chains, the detection member of the stored
  prediction vocabulary, the summary's detection bucket, the loud persistence-failure log, and the
  history page's prediction-faithful label
- **C(X)**: a conversion-chain resolution for the static camera that yields the Bayer default; a
  stored static-camera configuration still carrying it; a detection row rejected by the stored-row
  validation; or a summary that excludes Detection rows from every bucket and from the total
- **P(result)**: an RGB chain and RGB appsrc caps with no demosaic for the static camera in both new
  and existing Image_Sources; a persisted detection row visible in the results list; a summary whose
  total counts every row; and a named, loud log line on any persistence failure
- **Counterexamples**:
  - live on `jetson-thor1`, Image_Source `my6j3zx1` → config `jk1y5ln8` with
    `processingPipeline: "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"`,
    and the executed preview pipeline
    `appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! ...` from the
    container log — a BGGR demosaic over packed RGB
  - live on `jetson-thor1`, `POST /workflows/pagb7vj8/run` → HTTP 200 with a 5-object detection
    result (capture `db9721b27fc54b8f993fb06ef8738631`) while
    `GET /workflows/pagb7vj8/results` stays `{"total":0,...}` and the log carries
    `marshmallow.exceptions.ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}`
    from `inference_result_accessor.py` line 58, re-raised as
    `HTTPException: 400: Unable to store inference result.`
  - `GET /workflows/pagb7vj8/results/summary` → `{"stats":{"totalInference":0,"normal":0,"anomaly":0},...}`
    with no detection bucket, which stays wrong for a Detection row even after the persistence fix
