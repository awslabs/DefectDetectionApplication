# Requirements Document

## Introduction

The on-device deployed-workflow run views (`src/frontend/src/components/deployed-workflow/`) do not show what an object-detection model found. Every detection run already produces the data:

- a server-rendered overlay with the boxes and labels drawn in, `{capture_id}.overlay.jpg`;
- the run's Detection_List (label, confidence and box per object) in the run metadata `{capture_id}.json`.

Neither reaches the user:

- **Run results page.** "View results" serves only `{capture_id}.jpg`, the raw frame. `run_artifacts.base_output_image_path` skips the overlay file on purpose. The overlay toggle understands only segmentation masks, so for a detection run `/results` reports `hasOverlay: true`, `/overlay` returns `maskImage: null`, and the toggle stays hidden.
- **Run-status graph.** Selecting a `model_inference` node shows only "model_1 — success", because `model_inference` is not one of the preview node types.
- **Detection list.** It is served by `/metadata`, but the run views read metadata only for LLM and Bedrock nodes.

Observed on `jetson-thor1`, 2026-09-26, run `617776d1` of `ppe-detector-v3-static-test` (10 detections):

- `/results` returned `[{"kind": "output", "hasOverlay": true}]`.
- `/overlay` returned `{"maskImage": null}`.
- `/output-image` served the `.jpg`, sha256 `589e9b87…`, not the `.overlay.jpg`, sha256 `aa1b1c61…`.
- `/metadata` carried all 10 detections.
- The `model_1` node status carried only `status` and `durationMs`.

The deployed-workflow-run-observability spec promised "any applicable overlay (anomaly mask / detection boxes)" on the results page; only the mask half was built.

The older "Run inference" screen already handles detections. It has a "Show bounding boxes" toggle that swaps between the overlay and the input image (`live-result/ResultsLayout.tsx`), and an "Objects detected" list (`live-result/LiveResultCard.tsx`). This feature brings the same visibility to deployed-workflow runs.

Decisions (made autonomously; the owner asked for overlay + original image and a list of detected objects with confidence):

- **D1, overlay presentation.** The results page shows one full-size image with the Run inference screen's toggle: "Show bounding boxes", on by default, shows the overlay; off shows the original frame. A side-by-side layout was rejected because it halves the image size on the device UI. It would also diverge from both existing overlay toggles.
- **D2, detection source.** The Detection_List in the run metadata, already served by `GET /workflows/executions/{id}/metadata`, is the only source. It is the list downstream nodes saw, in the same order and with the same Detection_IDs. No detections endpoint is added.
- **D3, backend additions.**
  - One route, `GET /workflows/executions/{id}/overlay-image`. It sits on the download router with token-in-query auth, mirroring `/output-image`.
  - One additive field, `hasOverlayImage`, on the `/results` `output` entry.
- **D4, masks keep precedence.** A run with a segmentation mask keeps today's client-side mask composite and "Show anomaly masks" toggle exactly. The overlay image is used only when the run has no mask.
- **D5, graph preview.** `model_inference` joins the previewable node types. Its preview shows the overlay thumbnail and a compact list: the first 10 objects with their confidence, then the count of the rest. For a non-detection model it shows the `is_anomalous` / `confidence` fields instead.
- **D6, run-level list.** A run has one Detection_List, built from the run's capture record; the executor already takes the sort order from the first `model_inference` node. Each `model_inference` preview therefore shows the run's list, labelled "Objects detected in this run".
- **D7, formatting.**
  - Confidence is shown as a percentage with one decimal.
  - Boxes are shown as integer pixel ranges in source-frame coordinates.
  - The list index is 0-based, so it matches the `detections.N` template paths and Bedrock's `crop_detection_index`.

## Glossary

- **Run_Results_Page**: `RunResults.tsx`, at `/deployed-workflows/{registrationId}/executions/{executionId}/results`.
- **Run_Status_Graph**: `RunStatusGraph.tsx`, the per-run graph with node status coloring and the selected-node detail area.
- **Preview_Card**: `NodePreviewCard.tsx`, the selected-node preview below the graph canvas (output-node-preview-popover spec).
- **Original_Image**: `{capture_id}.jpg`, the captured frame, served by `/output-image`.
- **Overlay_Image**: `{capture_id}.overlay.jpg`, the marshal's server-rendered overlay (for detection models: boxes, labels and percentages drawn on the frame).
- **Mask_Overlay**: the segmentation mask served by `/overlay` (`maskImage` non-null), composited client-side.
- **Run_Metadata**: the parsed `{capture_id}.json`, served by `/metadata`.
- **Detection_List**: the Run_Metadata `detections` array. Each entry is `{id, label, confidence, x_min, y_min, x_max, y_max}`, written by `workflow_engine.detections.merge_detections`.
- **Detected_Objects_Table**: the new results-page table of the Detection_List.
- **Terminal_Status**: a node status of `success`, `warning` or `failure`.

## Requirements

### Requirement 1: Overlay and original image on the results page

**User Story:** As an operator reviewing a deployed-workflow run, I want to see the frame with the model's boxes drawn on it and switch to the untouched frame, so that I can check what the model found against what the camera saw.

#### Acceptance Criteria

1. WHERE the run has an Overlay_Image and no Mask_Overlay, THE Run_Results_Page SHALL display the Overlay_Image by default, with an overlay toggle that is on.
2. WHEN the user turns the overlay toggle off, THE Run_Results_Page SHALL display the Original_Image; WHEN the user turns it back on, THE Run_Results_Page SHALL display the Overlay_Image again.
3. WHERE the overlay toggle controls an Overlay_Image, THE toggle SHALL read "Show bounding boxes" when the Run_Metadata carries a Detection_List, and "Show overlay" otherwise.
4. WHERE the run has a Mask_Overlay, THE Run_Results_Page SHALL keep the existing behavior unchanged: the Original_Image with the mask composited client-side, and the "Show anomaly masks" toggle.
5. WHERE the run has neither an Overlay_Image nor a Mask_Overlay, THE Run_Results_Page SHALL display the Original_Image without a toggle, as today.
6. WHILE the Mask_Overlay request for a run that also has an Overlay_Image is in flight, THE Run_Results_Page SHALL display the Original_Image, so a segmentation run never flashes the server overlay before its mask arrives.

### Requirement 2: Detected objects list on the results page

**User Story:** As an operator, I want a list of the objects the model detected with their confidence, so that I can read the result without inspecting the image.

#### Acceptance Criteria

1. WHEN the Run_Metadata carries a Detection_List, THE Run_Results_Page SHALL display the Detected_Objects_Table with one row per detection, showing its index, label, confidence and bounding box.
2. THE Detected_Objects_Table header SHALL show the number of detections and a per-label count summary (for example `helmet 2 · human 5`).
3. THE Detected_Objects_Table SHALL list rows in Detection_List order by default, and SHALL let the user sort by index, label and confidence.
4. THE Detected_Objects_Table SHALL show confidence as a percentage with one decimal place, and the bounding box as integer source-frame pixel ranges.
5. WHEN the Detection_List is empty, THE Detected_Objects_Table SHALL state that no objects were detected in the run.
6. WHERE the Run_Metadata carries no Detection_List (a non-detection model, or unavailable metadata), THE Run_Results_Page SHALL NOT display the Detected_Objects_Table.
7. WHEN the run has no viewable image results but the Run_Metadata carries a Detection_List, THE Run_Results_Page SHALL still display the Detected_Objects_Table beside the no-results message.
8. IF a Detection_List entry lacks a finite numeric confidence, THEN THE Run_Results_Page SHALL skip that entry. IF an entry lacks a label, the label SHALL show as "object". IF an entry lacks a complete box, the box SHALL show as "-". Malformed metadata SHALL never break the page.

### Requirement 3: Model node preview in the run-status graph

**User Story:** As an operator looking at a run's graph, I want clicking the model node to show what it detected, so that I can see the result where I am looking.

#### Acceptance Criteria

1. THE Run_Status_Graph SHALL render the Preview_Card for a selected `model_inference` node.
2. WHEN the selected `model_inference` node has a Terminal_Status of `success` or `warning` and the Run_Metadata carries a Detection_List, THE Preview_Card SHALL list the first 10 detections as label and confidence, state the number of remaining detections when there are more than 10, and label the list as the run's detections.
3. WHERE the run also has an Overlay_Image, THE Preview_Card SHALL show the Overlay_Image thumbnail above the list. IF the thumbnail fails to load, THEN THE Preview_Card SHALL hide it and keep the list.
4. WHERE the Run_Metadata carries no Detection_List but carries `is_anomalous` or `confidence`, THE Preview_Card SHALL show those fields. Otherwise it SHALL show the existing unavailable message.
5. THE `model_inference` preview SHALL follow the existing state precedence: a missing or non-terminal status shows the in-progress placeholder; `failure` shows the failure alert; an in-flight metadata request shows the loading indicator; a failed metadata request shows the unavailable message.
6. THE Preview_Card SHALL keep the "View full results" link in every `model_inference` preview state.
7. THE Run_Status_Graph SHALL fetch the Run_Metadata and the run results only when a `model_inference` node with a Terminal_Status is selected. The results fetch also requires the run to have image results.

### Requirement 4: Overlay image endpoint and results flag

**User Story:** As the frontend, I want the overlay image served and its availability reported, so that the results page and the graph can show it.

#### Acceptance Criteria

1. WHEN `GET /workflows/executions/{execution_id}/overlay-image` is requested for a known execution whose Overlay_Image file exists, THE backend SHALL return the file with status 200 and content type `image/jpeg`.
2. IF the execution is unknown, or its Overlay_Image file does not exist, THEN THE backend SHALL return status 404.
3. THE overlay-image route SHALL apply the same token-in-query authorization as `/output-image` (`validate_token_in_query_param`).
4. THE `/results` `output` entry SHALL carry `hasOverlayImage`, true exactly when the run's Overlay_Image file exists. The existing `hasOverlay`, `hasImageResults`, `captureId` and node entries SHALL keep their current meaning and values.
5. THE overlay-image route SHALL resolve only `{output_dir}/{capture_id}.overlay.jpg`, from the execution record. Request input SHALL NOT influence the file path beyond selecting the execution.

### Requirement 5: Preservation

**User Story:** As an operator, I want existing run views to keep working, so that the enhancement does not regress segmentation runs, VLM runs or other node previews.

#### Acceptance Criteria

1. THE Run_Results_Page SHALL render mask runs, node-frame sections (`llm_inference` / `bedrock_inference`), and the no-results and error states exactly as before.
2. THE Preview_Card SHALL render every pre-existing previewable node type (`capture`, `llm_inference`, `bedrock_inference`, `mqtt_publish`, `opcua_write`, `digital_output`) exactly as before. Nodes that are still not previewable SHALL keep the plain detail rendering.
3. THE existing routes and response shapes SHALL stay unchanged, apart from the additive `hasOverlayImage` field.
4. THE feature SHALL add no dependency and SHALL change no preservation-tracked file (Dockerfiles, `docker-compose.yaml`, `requirements.txt`, recipes, `setup_station.sh`).

### Requirement 6: On-device verification

**User Story:** As the owner, I want the change verified on a real device before it is committed, per `.kiro/steering/builds.md`.

#### Acceptance Criteria

1. THE change SHALL be exercised on `jetson-thor1` (JP7) with a detection workflow. The results page SHALL serve the Overlay_Image and the Original_Image and list the detections; the model node preview SHALL list them too.
2. THE backend SHALL stay healthy (no crash, no restart loop) while the new routes are exercised repeatedly.
3. THE change SHALL be committed only after verification from a built and deployed LocalServer component, and only on the owner's request. The commit SHALL state what was verified on which device.
