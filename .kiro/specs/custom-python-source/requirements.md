# Requirements Document

## Introduction

Trigger-driven workflows today can be *activated* by an MQTT or OPC UA message but cannot *use* it. The Trigger_Runtime builds a Trigger_Context for every firing and persists it to `workflow_executions.trigger_context_json`, and nothing ever reads that column back — the executor, the inference nodes, and the output bindings all run as if the run had been triggered manually. At the same time, every frame source available in the designer reads from a fixed location decided at design time: a device camera, or a path on the device file system.

The result is a gap for the most common integration pattern in a line-side inspection cell: a PLC or MES publishes "inspect part XYZ, reference image at s3://…", and the workflow needs to fetch *that* image from *that* location and run inference on it.

This feature closes the gap with a Custom Python Source node: a `CATEGORY_INPUT` node type whose user-authored Python receives the Trigger_Context that started the run, fetches a frame from wherever that payload points (S3, an HTTP(S) URL, the local file system, or anything else reachable with a pip-installable library), and hands it to the compiled pipeline through a GStreamer `appsrc` — from which the existing `bedrock_inference`, `llm_inference`, and `model_inference` nodes consume it with no changes.

The Trigger_Context is also seeded into the run's metadata, so its fields become addressable from `llm_inference` prompt templates, `conditional` conditions, and output bindings through the placeholder syntax those features already support.

The feature spans the Portal (node catalog, validator, packaging, Code_Assistant) and the LocalServer edge runtime (trigger-context plumbing, a frame-producer mode on the Python_Bridge, a source planner, and the executor's frame-feed integration), with the vendored `workflow_core` catalog mirror kept in sync.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend) used to design, package, and deploy workflows.
- **LocalServer**: The Greengrass component running on an edge device that executes compiled workflow pipelines through GStreamer.
- **Node_Catalog**: The data catalog of node type descriptors in `workflow_core.catalog.nodes` (`NODE_CATALOG`), maintained in the Portal Lambda layer at `edge-cv-portal/backend/layers/workflow_core/` and mirrored verbatim in the LocalServer vendored copy at `src/backend/workflow_engine/vendor/workflow_core/`.
- **Workflow_Builder**: The graphical canvas UI (Node_Palette, canvas, NodeConfigPanel) where users compose workflows.
- **Node_Palette**: The categorized node type list in the Workflow_Builder, populated from the Node_Catalog via the node-catalog API.
- **Workflow_Validator**: The `workflow_core.validator` component checking graph structure, port type compatibility, and the activation model.
- **Workflow_Compiler**: The `workflow_core.compiler` component translating a workflow definition into per-architecture compiled pipeline documents.
- **Component_Packager**: The Portal backend packaging Lambda (`workflow_packaging.py`) that assembles per-architecture Workflow_Component artifact zips, including `python/{nodeId}/handler.py` and `python/{nodeId}/requirements.txt` for Custom Python node types.
- **Trigger_Runtime**: The LocalServer component (`src/backend/workflow_engine/trigger_runtime.py`) that subscribes MQTT and OPC UA triggers, gates firings, and starts workflow runs.
- **Trigger_Context**: The dict a trigger transport builds for one firing and `default_run_starter` persists as `trigger_context_json`. MQTT shape: `{topic, payload, qos, timestamp}` where `payload` is the message body decoded as text. OPC UA shape: `{endpoint, node_id, value, source_timestamp}`.
- **Run_Metadata**: The per-run dict the executor threads through the run (named `tag_values` in `pipeline_executor.py`), seeded from GStreamer bus TAG messages and progressively merged by the inference processors before output bindings and run-metadata persistence consume it.
- **Custom_Python_Source_Node**: The new node type (`custom_python_source`) added by this feature: a `CATEGORY_INPUT` node whose Frame_Producer supplies the run's frame.
- **Frame_Producer**: The user-authored `produce_frame(context)` entry point of a Custom_Python_Source_Node, executed once per run inside the Python_Bridge handler subprocess.
- **Python_Bridge**: The LocalServer component (`src/backend/workflow_engine/python_bridge.py`) that runs user handler code in a subprocess over a framed stdin/stdout protocol.
- **Python_Runner**: The self-contained script (`RUNNER_SOURCE` in the Python_Bridge) executed inside the handler subprocess; it loads the user's `handler.py` and invokes the entry point.
- **Frame_Helpers**: The helper module injected by the Python_Runner and importable from handler code as `dda_frames`, providing frame/array conversion, current-frame info, and image loading.
- **Frame_Feed**: The LocalServer execution model in which the executor grabs one frame before the pipeline starts, pushes it into the pipeline's `appsrc`, and sends EOS — the model `aravis_camera_source` uses today.
- **Produced_Frame**: The value a Frame_Producer returns, resolved by the Python_Runner into raw frame bytes plus an explicit width, height, and Pixel_Format.
- **Pixel_Format**: The GStreamer video format string carried in the stream caps (this feature produces RGB, RGBA, or GRAY8).
- **Code_Assistant**: The Bedrock-backed code generation panel (`code_assist.py`, `CodeAssistPanel.tsx`) that generates handler code for a declared Node_Contract.

## Requirements

### Requirement 1: Custom Python Source Node Type

**User Story:** As an integration engineer, I want a Custom Python input source node in the designer, so that I can supply the run's frame from my own Python code instead of a fixed camera or folder.

#### Acceptance Criteria

1. THE Node_Catalog SHALL provide a node type with type id `custom_python_source`, display name "Custom Python (Source)", and the input category.
2. THE Custom_Python_Source_Node descriptor SHALL declare exactly one input port named `activation` of type EventSignal and exactly one output port named `out` of type VideoFrames.
3. THE Custom_Python_Source_Node descriptor SHALL declare a required `code` parameter of parameter type `code` and an optional `requirements` parameter accepting extra pip packages in requirements.txt form.
4. THE Custom_Python_Source_Node descriptor SHALL declare an optional `allowed_uri_prefixes` parameter accepting a newline-separated list of URI prefixes, defaulting to empty.
5. THE Custom_Python_Source_Node descriptor SHALL map on every device architecture to an `appsrc` element named `appsrc_{nodeId}` followed by `videoconvert`, with the same plugin dependencies the Aravis camera source declares for that chain.
6. THE Custom_Python_Source_Node descriptor SHALL declare a simulation-architecture mapping fed from the Test_Dataset, so that workflows containing the node compile and run in the Portal test sandbox.
7. THE Custom_Python_Source_Node descriptor's `code` parameter description and examples SHALL document the `produce_frame(context)` contract, the Trigger_Context keys available on `context`, and the `dda_frames` helpers.
8. THE Custom_Python_Source_Node SHALL NOT be selectable as a `unified_input` source kind, and `SOURCE_KIND_TO_SOURCE_TYPE` SHALL be unchanged by this feature.
9. THE Node_Catalog copy in the LocalServer vendored mirror (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`) SHALL be byte-identical to the Portal layer copy after the change.

### Requirement 2: Trigger Context Delivery to the Run

**User Story:** As an integration engineer, I want the MQTT or OPC UA message that started the run to be available inside the run, so that my code and my prompts can act on the part id, reference image URL, or setpoint it carries.

#### Acceptance Criteria

1. WHEN the executor runs an execution whose `trigger_context_json` holds a JSON object, THE LocalServer SHALL deserialize it into the run's Trigger_Context.
2. WHEN the executor runs an execution whose `trigger_context_json` is NULL, empty, or not a JSON object, THE LocalServer SHALL use an empty Trigger_Context and run exactly as it does today.
3. WHEN the Trigger_Context carries a `payload` string that parses as JSON, THE LocalServer SHALL add the parsed value under the key `payload_json`.
4. IF the Trigger_Context carries a `payload` string that does not parse as JSON, THEN THE LocalServer SHALL set `payload_json` to None and SHALL NOT fail the run.
5. THE LocalServer SHALL seed the Run_Metadata with the Trigger_Context under the key `trigger` before the Bedrock and LLM inference processors run.
6. WHEN an `llm_inference` node's prompt template references a Trigger_Context field by a dotted placeholder (for example `{trigger.payload_json.part_id}`), THE LocalServer SHALL resolve it from the seeded Run_Metadata.
7. THE LocalServer SHALL seed the Trigger_Context into the Run_Metadata without overwriting any key the pipeline's TAG messages produced.
8. WHEN a run's metadata is persisted, THE LocalServer SHALL include the seeded Trigger_Context, so that the payload that drove the run is visible in run observability.

### Requirement 3: Frame Producer Contract

**User Story:** As an integration engineer, I want to write a `produce_frame(context)` function that returns the frame for this run, so that I can fetch the image the trigger payload points at.

#### Acceptance Criteria

1. WHEN a Custom_Python_Source_Node's handler file defines a callable `produce_frame`, THE Python_Runner SHALL invoke `produce_frame(context)` exactly once per run with `context` as the run's Trigger_Context.
2. IF a Custom_Python_Source_Node's handler file does not define a callable `produce_frame`, THEN THE LocalServer SHALL fail the run with an error identifying the node and naming the required entry point.
3. WHEN `produce_frame` returns a two-dimensional NumPy uint8 array, THE Python_Runner SHALL resolve it as a Produced_Frame of Pixel_Format GRAY8 with width and height taken from the array's shape.
4. WHEN `produce_frame` returns a three-dimensional NumPy uint8 array with three channels, THE Python_Runner SHALL interpret the channels as OpenCV BGR order and SHALL resolve it as a Produced_Frame of Pixel_Format RGB with the channel order converted.
5. WHEN `produce_frame` returns a three-dimensional NumPy uint8 array with four channels, THE Python_Runner SHALL interpret the channels as OpenCV BGRA order and SHALL resolve it as a Produced_Frame of Pixel_Format RGBA with the channel order converted.
6. WHEN `produce_frame` returns a mapping carrying an `array` key and a `format` key naming a supported Pixel_Format, THE Python_Runner SHALL resolve the array's bytes under that Pixel_Format without converting the channel order.
7. WHEN `produce_frame` returns a mapping carrying `data`, `width`, `height`, and `format` keys, THE Python_Runner SHALL resolve those raw bytes as the Produced_Frame under the stated Pixel_Format and dimensions.
8. IF `produce_frame` returns None, THEN THE LocalServer SHALL fail the run with an error identifying the node and stating that a source must produce a frame.
9. IF `produce_frame` returns a value that is neither a supported NumPy array nor a mapping carrying a supported key set, THEN THE LocalServer SHALL fail the run with an error identifying the node and describing the accepted return values.
10. IF a mapping return declares a Pixel_Format outside the supported set, or declares dimensions inconsistent with the byte length it carries, THEN THE LocalServer SHALL fail the run with an error identifying the node and describing the inconsistency.
11. WHEN a handler file defines `produce_frame` alongside `process_frame` or `handle`, THE Python_Runner SHALL invoke only `produce_frame` for a Custom_Python_Source_Node.
12. THE Python_Runner SHALL bind `cv2`, `np`, and `numpy` in a Frame_Producer's module namespace and SHALL make `dda_frames` importable, on the same best-effort terms as for existing Custom Python node types.

### Requirement 4: Data Store Access Helpers

**User Story:** As an integration engineer, I want one helper that loads an image from S3, an HTTP(S) URL, or a local path, so that I do not write fetch-and-decode boilerplate for every data store my payloads reference.

#### Acceptance Criteria

1. WHEN `dda_frames.load_image(source)` is called with an `http://` or `https://` URL serving a decodable image, THE Frame_Helpers SHALL return the decoded image as a NumPy uint8 array in OpenCV BGR channel order.
2. THE Frame_Helpers SHALL preserve the existing `load_image` behavior for local file paths and `s3://bucket/key` URIs unchanged.
3. THE Frame_Helpers SHALL provide `load_bytes(source)` returning the raw bytes of a local path, `s3://` URI, or `http(s)://` URL without decoding them as an image.
4. THE Frame_Helpers SHALL apply a bounded network timeout to every HTTP(S) fetch, so that an unresponsive endpoint cannot hold the Frame_Producer open past its wall-clock limit.
5. IF an HTTP(S) fetch returns a non-success status, times out, or fails to connect, THEN THE Frame_Helpers SHALL raise an error identifying the source and describing the failure.
6. THE Frame_Helpers SHALL raise an error identifying the source when the fetched content cannot be decoded as an image, consistent with the existing local and S3 behavior.

### Requirement 5: Fetch Restriction

**User Story:** As a security reviewer, I want the option to restrict which URIs a source node may fetch, so that a payload arriving from outside the device's trust boundary cannot make the device fetch from an arbitrary endpoint.

#### Acceptance Criteria

1. WHEN a Custom_Python_Source_Node declares a non-empty `allowed_uri_prefixes`, THE Frame_Helpers SHALL permit a `load_image` or `load_bytes` fetch only when the source string starts with one of the declared prefixes.
2. IF a fetch source does not match any declared prefix, THEN THE Frame_Helpers SHALL raise an error identifying the source and stating that it is outside the node's allowed prefixes, and THE LocalServer SHALL fail the run with the node identified.
3. WHEN a Custom_Python_Source_Node declares an empty `allowed_uri_prefixes`, THE Frame_Helpers SHALL permit any fetch source, preserving the helper behavior existing Custom Python node types have today.
4. WHEN a Frame_Producer fetches a source through the Frame_Helpers, THE LocalServer SHALL record the source string in the run log.
5. THE `allowed_uri_prefixes` restriction SHALL apply only to fetches made through the Frame_Helpers, and the node's parameter description SHALL state that it is not a sandbox boundary.

### Requirement 6: Frame Producer Execution

**User Story:** As an integration engineer, I want my producer code to run under limits appropriate for a network fetch, so that a slow S3 GET does not fail the run but a hung endpoint does not hang the device.

#### Acceptance Criteria

1. THE Python_Bridge SHALL execute a Frame_Producer in the same subprocess isolation existing Custom Python handlers use, with the interpreter, environment passthrough, thread caps, and memory limit that mechanism already applies.
2. THE Python_Bridge SHALL apply a Frame_Producer wall-clock limit that is configurable independently of the per-frame handler limit, defaulting to a value that accommodates a remote object fetch and decode.
3. THE Python_Bridge SHALL apply a Frame_Producer memory limit that is configurable independently of the per-frame handler limit.
4. IF a Frame_Producer exceeds its wall-clock limit, THEN THE LocalServer SHALL fail the run with an error identifying the node and stating the limit.
5. IF a Frame_Producer raises, THEN THE LocalServer SHALL fail the run with an error identifying the node and carrying the handler's traceback, consistent with existing handler failure containment.
6. THE Python_Bridge SHALL carry the Trigger_Context to the Frame_Producer and the resolved Produced_Frame back through additive changes to the existing framed stdin/stdout protocol, without altering the per-frame request and response behavior existing handlers rely on.
7. WHEN a Frame_Producer returns metadata alongside its frame, THE LocalServer SHALL merge that metadata into the Run_Metadata under a key identifying the node.

### Requirement 7: Executor Frame Feed Integration

**User Story:** As an integration engineer, I want the frame my code produced to flow through the compiled pipeline to the inference nodes, so that Bedrock, VLM, and model inference see it exactly as they would a camera frame.

#### Acceptance Criteria

1. WHEN a compiled document declares a Custom_Python_Source_Node, THE LocalServer SHALL run its Frame_Producer before the pipeline starts and SHALL push the Produced_Frame into the node's compiled `appsrc` through the existing Frame_Feed execution model.
2. THE LocalServer SHALL set the fed `appsrc`'s caps from the Produced_Frame's explicit Pixel_Format, width, and height, and SHALL NOT infer the Pixel_Format from the payload's bytes-per-pixel.
3. WHEN a compiled document declares no Custom_Python_Source_Node, THE LocalServer SHALL plan zero Frame_Producers and SHALL take the pre-feature execution path unchanged.
4. WHEN a compiled document declares both a Custom_Python_Source_Node and one or more Custom Python preprocessing or post-processing nodes, THE LocalServer SHALL feed the Produced_Frame and pump the bridged nodes in the same run.
5. WHEN a compiled document declares a Custom_Python_Source_Node and the run's Produced_Frame has been pushed, THE LocalServer SHALL send end-of-stream so the pipeline completes, consistent with the existing single-frame Frame_Feed model.
6. IF planning a Frame_Producer fails, THEN THE LocalServer SHALL fail the run with the Custom_Python_Source_Node identified and SHALL NOT start the pipeline.
7. THE LocalServer SHALL surface a Custom_Python_Source_Node's per-node run status through the existing status collection, so that the node appears in deployed-run observability like other nodes.

### Requirement 8: Fed Source Coexistence

**User Story:** As a computer vision engineer, I want the designer to tell me when my workflow has more frame-feed sources than the runtime can serve, so that I find out at design time instead of on the device.

#### Acceptance Criteria

1. WHEN a workflow contains more than one Custom_Python_Source_Node, THE Workflow_Validator SHALL report an error finding per offending node naming the full conflicting membership.
2. WHEN a workflow contains a Custom_Python_Source_Node together with an `aravis_camera_source`, THE Workflow_Validator SHALL report an error finding per offending node naming the full conflicting membership and stating that the runtime serves one frame-feed source per workflow.
3. THE Workflow_Validator SHALL continue to report the pre-existing single-instance conflict for two or more `aravis_camera_source` nodes, with its finding code and message unchanged.
4. THE Workflow_Builder SHALL surface the fed-source conflicts as inline validation markers, in parity with the Workflow_Validator findings.
5. IF a compiled document reaches the device declaring more than one frame-feed source, THEN THE LocalServer SHALL fail the run with a reason naming every offending node and SHALL NOT start the pipeline.
6. WHEN a workflow contains a Custom_Python_Source_Node and at least one subscription trigger node, THE Workflow_Validator SHALL require the node's `activation` port to be connected under the existing single-activation-model rule.

### Requirement 9: Packaging and Code Assistance

**User Story:** As a computer vision engineer, I want the source node's code to ship to the device and to get generation help while writing it, so that it deploys and authors like the other Custom Python node types.

#### Acceptance Criteria

1. WHEN a workflow containing Custom_Python_Source_Nodes is packaged, THE Component_Packager SHALL write each such node's `code` to `python/{nodeId}/handler.py` and its `requirements` to `python/{nodeId}/requirements.txt` in every architecture artifact zip.
2. WHEN the per-architecture manifest is built, THE Component_Packager SHALL list Custom_Python_Source_Node ids alongside the other Custom Python node ids in `customPythonNodeIds`.
3. WHEN a workflow containing a Custom_Python_Source_Node is compiled for any architecture, THE Workflow_Compiler SHALL emit the node's `appsrc` element carrying the node id, so that the executor can locate it.
4. THE Code_Assistant SHALL offer a `produce_frame` Node_Contract whose runtime environment description states the Trigger_Context keys, the accepted return values, and the available Frame_Helpers.
5. WHEN generated code for the `produce_frame` contract lacks a top-level `produce_frame` function, THE Code_Assistant SHALL reject it with the existing missing-entry-point response.
6. WHEN a user edits a Custom_Python_Source_Node's `code` in the Workflow_Builder, THE Workflow_Builder SHALL offer the Code_Assistant panel and SHALL derive pip requirements from the code's imports, on the same terms as the other Custom Python node types.

### Requirement 10: Workflow Designer Support

**User Story:** As a computer vision engineer, I want the source node to appear and behave correctly in the designer, so that I can place, configure, and connect it like any other input node.

#### Acceptance Criteria

1. WHEN a user opens the Node_Palette, THE Workflow_Builder SHALL display the Custom_Python_Source_Node in the input section.
2. WHEN a user selects a Custom_Python_Source_Node, THE Workflow_Builder SHALL render a code editor for the `code` parameter.
3. WHEN a user attempts to connect the Custom_Python_Source_Node's `out` port to a target input port whose type is not compatible with VideoFrames under the declared coercion rules, THE Workflow_Builder SHALL reject the connection and display the reason.
4. WHEN a user connects a trigger node's output to the Custom_Python_Source_Node's `activation` port, THE Workflow_Builder SHALL accept the connection.
5. WHILE a Custom_Python_Source_Node has no `code` value, THE Workflow_Builder SHALL display an inline required-parameter validation marker on the node.

### Requirement 11: Backward Compatibility

**User Story:** As a platform maintainer, I want existing workflows and deployed components to behave exactly as they do today, so that this feature carries no regression risk for the installed base.

#### Acceptance Criteria

1. WHEN a workflow containing no Custom_Python_Source_Node runs, THE LocalServer SHALL produce the same pipeline execution, Run_Metadata, and run status it produces before this feature, apart from the seeded `trigger` key.
2. THE Frame_Helpers changes SHALL preserve the existing `load_image` behavior for local paths and `s3://` URIs, and the existing `to_array`, `to_bytes`, and `frame_info` behavior, unchanged.
3. THE per-frame `process_frame` and `handle` contracts SHALL be unchanged in signature and behavior.
4. THE Node_Catalog changes SHALL be additive: every pre-existing descriptor SHALL keep its position in `NODE_CATALOG` and its content.
5. WHEN a device runs a Workflow_Component packaged before this feature, THE LocalServer SHALL execute it unchanged.
