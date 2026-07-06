# Requirements Document

## Introduction

The DefectDetectionApplication (DDA) is a Greengrass edge computer-vision application with an on-device Triton inference pipeline (`src/backend/dda_triton/...`) and a cloud portal (`edge-cv-portal/`). Object-detection support (YOLO / RF-DETR) has been added by reusing the existing anomaly-classification output contract to avoid a Triton or ensemble rebuild: detections ride through the variable-length `anomalies` tensor as a serialized JSON list and are surfaced in the on-device results JSONL as a `detections` block.

The data layer is working end-to-end and correct detections are being written to disk. This feature finishes the remaining visualization and surfacing work so that a detection result is a first-class, human-readable, and visually rendered result. Specifically it covers: (1) distinct detection result typing in the emitted capture metadata, (2) reliable bounding-box overlay generation and auxiliary file reference for detection captures, (3) human-readable class labels instead of numeric class indices, and (4) portal rendering of detection bounding boxes in the results view.

All changes MUST preserve the existing anomaly-classification behavior (full backward compatibility) and MUST reuse the existing tensor contract, where detection payloads are distinguished from anomaly payloads by the presence of a `bounding_box` field in the reused `anomalies` list.

## Glossary

- **DDA**: DefectDetectionApplication; the overall edge CV application.
- **Base_Model**: The Triton Python-backend base model (`lfv_model_template.py`) that runs inference and emits result tensors.
- **Marshal**: The Triton Python-backend marshal model (`marshal_for_capture_template.py`) that builds capture metadata and encodes result/overlay outputs.
- **Portal**: The cloud web application under `edge-cv-portal/` (frontend and backend).
- **Results_Viewer**: The Portal frontend view that displays a capture's source image and inference results.
- **Detection_Result**: An object-detection outcome for a single detected object, consisting of a bounding box, class label, and confidence, as produced by `ObjectDetectionResult`.
- **Anomaly_Result**: An anomaly-classification or segmentation outcome (label, confidence, score, optional mask, optional anomaly list).
- **Detection_Task**: The inference task selected when the model manifest sets `task=object_detection`.
- **Anomaly_Task**: The default inference task (`task=anomaly`) used for anomaly classification and segmentation.
- **Detections_Block**: The `{"detections": {...}}` structure emitted into `deviceFleetAuxiliaryOutputs` for detection captures.
- **Overlay_Image**: A copy of the source capture image with bounding boxes and labels drawn on it, JPEG-encoded and written as `{capture_id}.overlay.jpg`.
- **Auxiliary_Output_Reference**: An entry in `deviceFleetAuxiliaryOutputs` that references an output artifact by file path (`data-ref`) or inline data.
- **Bounding_Box**: A 4-element list `[x_min, y_min, x_max, y_max]` in source-image pixel coordinates.
- **Class_Label**: A human-readable name for a detected object's class.
- **Class_Index**: The numeric class identifier produced by the detection model (for example the COCO index string `"17"`).
- **Class_Label_Map**: A mapping from Class_Index to Class_Label used to produce human-readable labels.
- **Capture_Metadata**: The JSON metadata object produced by Marshal for a single capture and written to the results JSONL.

## Requirements

### Requirement 1: Distinct detection result typing in capture metadata

**User Story:** As an operator reviewing on-device results, I want detection captures to be labeled as detection results rather than as generic anomaly results, so that I can tell a detection outcome apart from an anomaly-classification outcome.

#### Acceptance Criteria

1. WHEN Marshal processes a capture whose reused anomalies payload contains a `bounding_box` field, THE Marshal SHALL classify the capture as a Detection_Result regardless of the number of detected objects.
2. WHEN Marshal classifies a capture as a Detection_Result, THE Marshal SHALL set the inference-result type in the Capture_Metadata to a detection-specific value that is distinct from the anomaly-classification value.
3. WHILE a capture is classified as a Detection_Result, THE Marshal SHALL set the inference-result type to the detection-specific value and SHALL NOT set it to the anomaly-classification value.
4. WHEN Marshal classifies a capture as a Detection_Result, THE Marshal SHALL populate the Capture_Metadata detection summary with the count of detected objects.
5. IF a Detection_Result contains at least one detected object, THEN THE Marshal SHALL report the highest detection confidence among the detected objects as the capture confidence.
6. WHERE a capture is classified as a Detection_Result, THE Marshal SHALL emit the Detections_Block into `deviceFleetAuxiliaryOutputs`.
7. WHEN Marshal processes a capture whose reused anomalies payload does not contain a `bounding_box` field, THE Marshal SHALL produce the existing anomaly-classification Capture_Metadata unchanged.

### Requirement 2: Reliable overlay generation and reference for detections

**User Story:** As an operator, I want the bounding-box overlay image to be generated and referenced for detection captures, so that I can view where objects were detected.

#### Acceptance Criteria

1. WHEN the Base_Model runs under the Detection_Task and produces at least one Detection_Result, THE Marshal SHALL generate an Overlay_Image by drawing each Bounding_Box and its Class_Label onto a copy of the source capture image.
2. WHEN Marshal generates an Overlay_Image for a Detection_Result, THE Marshal SHALL write the Overlay_Image to `{capture_id}.overlay.jpg` in the capture folder.
3. WHEN Marshal writes an Overlay_Image for a Detection_Result, THE Marshal SHALL add an Auxiliary_Output_Reference to that Overlay_Image in `deviceFleetAuxiliaryOutputs`.
4. WHILE a capture is classified as a Detection_Result and has an empty anomaly mask, THE Marshal SHALL add the Overlay_Image Auxiliary_Output_Reference, including when the Detection_Result contains no detected objects.
5. IF a Detection_Result contains no detected objects, THEN THE Marshal SHALL generate the Overlay_Image as an unannotated copy of the source capture image and add its Auxiliary_Output_Reference.
6. WHEN Marshal processes an anomaly-classification capture that has an anomaly mask, THE Marshal SHALL add the Overlay_Image Auxiliary_Output_Reference exactly as it does today.

### Requirement 3: Human-readable class labels

**User Story:** As an operator, I want detected objects labeled with human-readable class names, so that I can understand what was detected without decoding numeric indices.

#### Acceptance Criteria

1. WHEN Marshal builds the Detections_Block, THE Marshal SHALL include a human-readable Class_Label for each detected object.
2. WHERE a Class_Label_Map contains an entry for a detected object's Class_Index, THE Marshal SHALL use the mapped Class_Label as the object's Class_Label.
3. IF a Class_Label_Map contains no entry for a detected object's Class_Index, THEN THE Marshal SHALL use the Class_Index string as the Class_Label.
4. WHEN Marshal draws a detected object onto the Overlay_Image, THE Marshal SHALL render the object's human-readable Class_Label and confidence.
5. THE Detections_Block SHALL retain the original Class_Index for each detected object in addition to the human-readable Class_Label.

### Requirement 4: Portal rendering of detection bounding boxes

**User Story:** As a Portal user, I want detection bounding boxes rendered over the capture image in the results view, so that I can visually inspect detection outcomes from the cloud portal.

#### Acceptance Criteria

1. WHEN a user opens a capture that is a Detection_Result in the Results_Viewer, THE Portal SHALL display the source capture image with each Bounding_Box drawn over it.
2. WHEN the Results_Viewer renders a Bounding_Box, THE Portal SHALL display the associated human-readable Class_Label and confidence.
3. WHERE a capture provides an Overlay_Image reference, THE Portal SHALL allow the user to view the Overlay_Image for that capture.
4. WHEN a user opens an anomaly-classification capture in the Results_Viewer, THE Portal SHALL display the existing anomaly result presentation unchanged.
5. IF a Detection_Result contains no detected objects, THEN THE Portal SHALL display the source capture image with an indication that no objects were detected.
6. WHERE Bounding_Box coordinates are expressed in source-image pixel coordinates, THE Portal SHALL scale the rendered boxes to the displayed image dimensions.

### Requirement 5: Backward compatibility and contract reuse

**User Story:** As a maintainer, I want object-detection support to reuse the existing tensor contract without breaking anomaly classification, so that existing deployments continue to work without a Triton or ensemble rebuild.

#### Acceptance Criteria

1. THE Base_Model SHALL emit Detection_Results through the existing variable-length `anomalies` tensor without adding or removing tensors from the output contract.
2. WHEN the model manifest omits the `task` field, THE Base_Model SHALL operate as an Anomaly_Task model.
3. WHEN the Anomaly_Task path executes, THE Base_Model SHALL produce the same output tensors it produced before object-detection support was added.
4. WHEN the Anomaly_Task path executes, THE Marshal SHALL produce the same Capture_Metadata and auxiliary outputs it produced before object-detection support was added.
5. THE DDA SHALL distinguish a detection payload from an anomaly payload solely by the presence of a `bounding_box` field in the reused anomalies list.
