# Requirements Document

## Introduction

A line-side inspection cell needs the following end-to-end flow on an edge
device: an MES publishes an MQTT message (through the device's
Greengrass-managed broker) whose JSON payload lists N reference images in
order — each entry carrying an identifier and either an `s3://` URI, an
`http(s)://` URL, or base64-encoded image data. The message triggers a
workflow run: an Aravis camera captures the live frame, a deployed
object-detection model (e.g. YOLO-World "blue plate") locates the N parts in
the frame, each detected part is cropped from the frame and compared against
its positionally-matched reference image by a Bedrock multimodal model, and
one MQTT message per part is published with that part's inspection verdict.

Most of the graph exists today: `mqtt_subscribe` (Greengrass) triggers runs
and carries `payload_json`; the executor grabs one Aravis frame per run;
`model_inference` runs the deployed Triton model; `bedrock_inference`
natively performs a two-image (input + reference) Converse comparison; and
`mqtt_publish` (Greengrass) emits templated payloads per node. Five gaps
block the flow:

1. **Detections never reach the Run_Metadata.** The pipeline surfaces model
   results as GStreamer TAG messages parsed into exactly two scalars —
   `is_anomalous` and `confidence` (`gst_pipeline.py::parse_msg`). The
   per-detection structure (bounding boxes, labels, confidences) exists on
   the device — the Marshal_Model builds a `detections` map for capture
   records — but no workflow node can address it.
2. **No detection-driven crop.** The `crop` node compiles to a static
   `videocrop` with design-time integers; the engine is single-frame and
   once-per-run with no data-driven fan-out. (Because `bedrock_inference`
   already consumes captured JPEG files at the executor level, per-detection
   crops can be produced in the executor without any pipeline fan-out: N
   parallel Bedrock branches, each bound to one detection slot.)
3. **Reference frames cannot come from the trigger payload.**
   `aravis_camera_source` and `custom_python_source` are frame-feed
   singletons and mutually exclusive (validator V7), so a workflow cannot
   have both the live camera frame and a payload-decoded reference frame.
   The `bedrock_inference` `reference` port is only fed by VideoFrames
   branches.
4. **Parallel Bedrock verdicts clobber each other.** Anomaly-mode results
   merge flat `is_anomalous`/`confidence` keys last-writer-wins; only
   freeform mode records nested per-node text. Three parallel inspections
   need per-node verdict keys addressable from payload templates and
   conditions.
5. **Bedrock branches serialize and outputs batch.** The
   BedrockInferenceProcessor runs bindings sequentially and every output
   binding runs only after all inference completes, so with N inspections
   the first result waits on the last inference. Independent branches need
   concurrent invocation with each branch's outputs published as its
   result lands.

This feature closes those gaps with executor-level mechanisms (no GStreamer
or proprietary-plugin changes): detections surfaced into the Run_Metadata
(randomly-ID'd, configurably ordered), a detection-crop selector and a
payload-sourced reference on the `bedrock_inference` node, per-node verdict
namespacing, and concurrent Bedrock branches whose outputs publish as each
result lands. The feature
spans the Portal (node catalog, validator, designer config panel) and the
LocalServer edge runtime (detection surfacing, Bedrock binding processing),
with the vendored `workflow_core` catalog mirror kept in sync.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend,
  Lambda backend) used to design, package, and deploy workflows.
- **LocalServer**: The Greengrass component running on an edge device that
  executes compiled workflow pipelines through GStreamer.
- **Node_Catalog**: The data catalog of node type descriptors in
  `workflow_core.catalog.nodes` (`NODE_CATALOG`), maintained in the Portal
  Lambda layer and mirrored verbatim in the LocalServer vendored copy at
  `src/backend/workflow_engine/vendor/workflow_core/`.
- **Workflow_Validator**: The `workflow_core.validator` component checking
  graph structure, port compatibility, and node configuration validity.
- **Run_Metadata**: The per-run dict the executor threads through the run
  (named `tag_values` in `pipeline_executor.py`), seeded from GStreamer TAG
  messages and the Trigger_Context, progressively merged by the inference
  processors, and consumed by conditions, templates, and output bindings.
- **Trigger_Context**: The dict built for one trigger firing. MQTT shape:
  `{topic, payload, payload_json, qos, timestamp}` where `payload_json` is
  the payload parsed as JSON (or None).
- **Detection_List**: The ordered list of per-detection records this feature
  adds to the Run_Metadata: Detection_ID, bounding box in source-frame pixel
  coordinates, class label, and confidence, ordered by the configured
  Detection_Sort_Order.
- **Marshal_Model**: The per-model Triton ensemble stage
  (`marshal_for_capture_template.py`) that builds capture records, including
  the existing per-detection `detections` map for object-detection models.
- **Bedrock_Binding**: A compiled `bedrock_inference` executor binding,
  processed by `BedrockInferenceProcessor` (`output_bindings.py`) after the
  pipeline run: it reads the captured JPEG(s) from `capturePaths` and calls
  the Bedrock Converse API.
- **Detection_Crop**: The JPEG produced by cropping the captured input frame
  to one Detection_List entry's bounding box (plus an optional margin),
  selected by a Bedrock_Binding's crop selector parameters.
- **Payload_Reference**: A reference image resolved at executor time from
  the run's Trigger_Context by a dotted path into `payload_json`, supporting
  `s3://` URIs, `http(s)://` URLs, and base64-encoded image data.
- **Detection_Sort_Order**: The configurable deterministic ordering applied
  to the Detection_List so positional matching against the ordered payload
  entries is well-defined: `left_to_right` (default), `right_to_left`,
  `top_to_bottom`, `bottom_to_top`, or `confidence_desc`; ties broken by the
  orthogonal axis ascending (confidence ties by `left_to_right`).
- **Detection_ID**: The per-run random unique identifier (short opaque
  string, e.g. 8 hex characters) assigned to each Detection_List entry when
  the list is built, so a specific detection is unambiguously referenceable
  across nodes, artifacts, and output messages within the run.

## Requirements

### Requirement 1: Detections in the Run Metadata

**User Story:** As a workflow author, I want the object-detection model's
per-detection results (boxes, labels, confidences) available in the run's
metadata, so that downstream nodes can act on individual detections instead
of a single anomaly flag.

#### Acceptance Criteria

1. WHEN a workflow run's `model_inference` node executes an object-detection
   model that reports detections, THE LocalServer SHALL merge a
   Detection_List into the Run_Metadata under the key `detections` before
   the Bedrock inference processor runs.
2. THE Detection_List SHALL carry, per detection: a Detection_ID, the
   bounding box in source-frame pixel coordinates (`x_min`, `y_min`,
   `x_max`, `y_max`), the class label, and the confidence.
3. THE LocalServer SHALL assign each Detection_List entry a Detection_ID
   that is unique within the run and randomly generated (never derived from
   list position), so downstream references cannot silently follow a
   different detection after a re-sort or re-run.
4. THE `model_inference` descriptor SHALL declare an optional
   `detection_sort_order` enum parameter (values `left_to_right`,
   `right_to_left`, `top_to_bottom`, `bottom_to_top`, `confidence_desc`;
   default `left_to_right`), and THE LocalServer SHALL order the
   Detection_List by the configured Detection_Sort_Order using bounding box
   centers.
5. WHEN the model reports zero detections, THE LocalServer SHALL merge an
   empty Detection_List (not omit the key), so conditions can distinguish
   "ran with no detections" from "no detection model in the graph".
6. WHEN a run's metadata is persisted, THE LocalServer SHALL include the
   Detection_List (Detection_IDs included), so detections are visible in
   run observability.
7. THE LocalServer SHALL surface the Detection_List without modifying the
   proprietary GStreamer inference plugins; the mechanism SHALL be
   implementable within the LocalServer repository (e.g. reading the
   Marshal_Model's existing detection output at the executor level).
8. IF the detection structure cannot be obtained for a run (e.g. the model
   is not an object-detection model), THEN THE LocalServer SHALL leave the
   Run_Metadata unchanged with respect to `detections` and SHALL NOT fail
   the run.
9. WHEN a `conditional` or `inference_filter` condition references the
   detection count, THE LocalServer SHALL make the count addressable as
   `detection_count` in the Run_Metadata.
10. WHEN a `custom_python` node with an InferenceMeta input port executes in
    a run whose Run_Metadata carries a Detection_List, THE LocalServer SHALL
    make the Detection_List (Detection_IDs included) available in the
    metadata passed to the node's handler, so custom code can reference
    specific detections by Detection_ID.

### Requirement 2: Detection-Crop Selection on Bedrock Inference

**User Story:** As a workflow author, I want a Bedrock inference node to
inspect one specific detected object rather than the whole frame, so that
each detected part gets its own comparison against its own reference.

#### Acceptance Criteria

1. THE `bedrock_inference` descriptor SHALL declare an optional
   `crop_detection_index` integer parameter (0-based, default absent); an
   absent value SHALL keep today's whole-frame behavior byte-identical.
2. WHEN a Bedrock_Binding carries `crop_detection_index` = k and the run's
   Detection_List has more than k entries, THE LocalServer SHALL crop the
   captured `in` frame to entry k's bounding box (per the configured
   Detection_Sort_Order) and send the Detection_Crop as the Converse
   request's input image.
3. THE `bedrock_inference` descriptor SHALL declare an optional
   `crop_margin_percent` parameter (default 0) expanding the crop box on
   every side by that percentage of the box's dimension, clamped to the
   frame bounds.
4. IF a Bedrock_Binding carries `crop_detection_index` = k and the run's
   Detection_List has k or fewer entries (or no Detection_List), THEN THE
   LocalServer SHALL record that node's outcome as an error naming the node,
   the requested index, and the available detection count, SHALL gate the
   node's downstream outputs exactly as a failed condition gates them, and
   SHALL NOT fail the run or the other Bedrock_Bindings.
5. WHEN a Detection_Crop is produced, THE LocalServer SHALL persist it as a
   run artifact alongside the existing captured frames, named to include
   the selected entry's Detection_ID, so the operator can see exactly what
   image was inspected and which detection it came from.
6. THE crop SHALL be computed from the same captured frame file the binding
   would otherwise send, requiring no changes to the compiled GStreamer
   pipeline.
7. WHEN a Bedrock_Binding inspects a Detection_Crop, THE LocalServer SHALL
   record the selected entry's Detection_ID under
   `bedrock.{nodeId}.detection_id` in the Run_Metadata, so each verdict is
   attributable to the exact detection it judged.

### Requirement 3: Reference Image from the Trigger Payload

**User Story:** As a workflow author, I want each Bedrock inference node to
load its reference image from the MQTT payload that triggered the run, so
that the MES controls which reference each detected part is compared
against.

#### Acceptance Criteria

1. THE `bedrock_inference` descriptor SHALL declare an optional
   `reference_payload_path` string parameter: a dotted path resolved against
   the run's Trigger_Context `payload_json` (for example `refs.0.image`);
   an absent value SHALL keep today's reference-port behavior
   byte-identical.
2. WHEN the resolved payload value is a string beginning with `s3://`,
   `http://`, or `https://`, THE LocalServer SHALL fetch the referenced
   object and use its bytes as the reference image.
3. WHEN the resolved payload value is a base64-encoded image (either a bare
   base64 string or a `data:` URL), THE LocalServer SHALL decode it and use
   the decoded bytes as the reference image.
4. THE `bedrock_inference` descriptor SHALL declare an optional
   `allowed_uri_prefixes` parameter mirroring the Custom Python source
   node's: a newline-separated list of URI prefixes that payload-resolved
   fetches may target; empty permits any source.
5. IF `reference_payload_path` is configured and the path does not resolve,
   the fetch fails, the fetched or decoded bytes are not a decodable image,
   or the URI is outside `allowed_uri_prefixes`, THEN THE LocalServer SHALL
   record that node's outcome as an error naming the node and the reason,
   SHALL gate the node's downstream outputs, and SHALL NOT fall back to
   single-image inference and SHALL NOT fail the run or the other
   Bedrock_Bindings.
6. WHEN both `reference_payload_path` and a fed `reference` frame port are
   present, THE Workflow_Validator SHALL report a configuration error: the
   two reference sources are mutually exclusive on one node.
7. THE LocalServer SHALL apply a bounded network timeout to every payload
   reference fetch and SHALL bound the accepted image size, so a slow or
   oversized reference cannot stall the run past its wall-clock limits.
8. WHEN a Payload_Reference is used, THE LocalServer SHALL record the
   resolved source (URI or "base64 payload data", never the decoded bytes)
   in the run log for observability.

### Requirement 4: Per-Node Verdict Namespacing

**User Story:** As a workflow author, I want each Bedrock node's verdict
addressable under its own key, so that parallel inspections in one run do
not overwrite each other and each output message carries its own branch's
result.

#### Acceptance Criteria

1. WHEN an anomaly-mode Bedrock_Binding produces a verdict, THE LocalServer
   SHALL merge it under nested per-node keys
   `bedrock.{nodeId}.is_anomalous` and `bedrock.{nodeId}.confidence` in
   addition to today's flat keys, whose last-writer-wins behavior SHALL be
   unchanged.
2. WHEN a Bedrock_Binding's outcome is an error under Requirement 2.4 or
   3.5, THE LocalServer SHALL record it under `bedrock.{nodeId}.error`.
3. WHEN an `mqtt_publish` payload template or an output binding condition
   references a dotted per-node key (for example
   `{bedrock.bedrock_1.is_anomalous}`), THE LocalServer SHALL resolve it
   from the Run_Metadata.
4. WHEN a `conditional` or `inference_filter` condition references a dotted
   per-node key, THE LocalServer SHALL evaluate it from the Run_Metadata.
5. WHEN a run's metadata is persisted, THE LocalServer SHALL include the
   nested per-node verdict keys.

### Requirement 5: Per-Inspection Output Messages

**User Story:** As an integration engineer, I want one MQTT message per
inspected part, each carrying only that part's verdict and its payload
identifier, so that downstream systems process part results independently.

#### Acceptance Criteria

1. WHEN a workflow contains N `bedrock_inference` branches each feeding its
   own `mqtt_publish` node, THE LocalServer SHALL publish N independent
   messages in one run, each rendered from its own node's template.
2. WHEN a run contains multiple Bedrock_Bindings, THE LocalServer SHALL
   invoke them concurrently, so one branch's Bedrock latency does not
   serialize behind another's.
3. WHEN one Bedrock_Binding's outcome is recorded (verdict or error), THE
   LocalServer SHALL run that branch's downstream output bindings
   immediately, without waiting for the other Bedrock_Bindings to complete,
   so each inspection result is published as it occurs.
4. WHEN one branch's Bedrock_Binding records an error outcome (Requirements
   2.4, 3.5), THE LocalServer SHALL still invoke and publish the other
   branches' Bedrock_Bindings and messages.
5. WHEN an `mqtt_publish` node is downstream of an errored Bedrock_Binding,
   THE LocalServer SHALL NOT publish that node's message.
6. THE run SHALL reach its terminal status only after every Bedrock_Binding
   outcome is recorded and every branch's output bindings have run, with
   per-node outcomes reported truthfully in the run's node status.
7. WHEN branch-scoped output bindings run concurrently, output bindings that
   are NOT downstream of any Bedrock_Binding SHALL keep today's post-run
   ordering and behavior unchanged.
8. THE `metadata` node's trigger-payload mappings SHALL remain usable to
   attach payload identifiers (for example each reference entry's ID) to
   each branch's published message.

### Requirement 6: Designer, Validator, and Packaging Support

**User Story:** As a workflow author, I want the new parameters configurable
in the designer and validated before packaging, so that a misconfigured
inspection workflow is caught at design time, not on the device.

#### Acceptance Criteria

1. THE Node_Catalog SHALL document the new `bedrock_inference` parameters
   (`crop_detection_index`, `crop_margin_percent`,
   `reference_payload_path`, `allowed_uri_prefixes`) and the new
   `model_inference` parameter (`detection_sort_order`) with descriptions
   and examples, and the designer's node config panel SHALL render them
   through the existing parameter-driven mechanism.
2. WHEN `crop_detection_index` is configured on a node in a workflow with no
   `model_inference` node, THE Workflow_Validator SHALL report a warning
   naming the node (no detection model can populate the Detection_List).
3. WHEN `reference_payload_path` is configured in a workflow with no
   CATEGORY_TRIGGER node, THE Workflow_Validator SHALL report a warning
   naming the node (no trigger payload will exist at runtime).
4. THE Workflow_Validator SHALL report an error for a negative
   `crop_detection_index` or a `crop_margin_percent` outside 0-100.
5. THE Node_Catalog copy in the LocalServer vendored mirror SHALL be
   byte-identical to the Portal layer copy after the change.
6. THE compiled Bedrock_Binding SHALL carry the new parameters through the
   existing untouched-parameter-copy mechanism, requiring no compiler
   changes beyond the descriptor.

### Requirement 7: Simulation and Backward Compatibility

**User Story:** As a workflow author, I want existing workflows and the
Portal test sandbox to behave exactly as before, so that this feature is
purely additive.

#### Acceptance Criteria

1. WHEN a workflow uses none of the new parameters, THE LocalServer SHALL
   produce byte-identical Bedrock request content and Run_Metadata (modulo
   the additive `detections`/`detection_count` keys of Requirement 1) to
   today's behavior.
2. THE simulation-architecture mappings SHALL be unchanged: the new
   parameters compile in the sandbox exactly like existing untouched
   parameters, and simulated runs inject configured outcomes as today.
3. WHEN a deployed workflow packaged before this feature runs on an updated
   LocalServer, THE LocalServer SHALL execute it unchanged.
