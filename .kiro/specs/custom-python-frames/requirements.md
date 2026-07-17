# Requirements Document

## Introduction

The workflow designer's Custom Python support today consists of a single post-processing node type (`custom_python`) whose user code runs on the edge device inside a subprocess bridge (`emlpython` → executor-managed appsink/appsrc pair) with a raw-bytes contract: `handle(frame_bytes, metadata) -> (frame_bytes, metadata)`. Writing image-processing code against raw bytes is impractical — the handler receives no frame dimensions, has no conversion helpers, and the catalog's own parameter description advertises a `process(data, metadata)` function that does not exist in the runtime.

This feature adds a Custom Python **preprocessing** node type that takes video frames in and emits processed frames out, and upgrades the Custom Python runtime (shared by the preprocessing and post-processing node types) into a practical OpenCV frame-processing environment: an ndarray-based `process_frame` handler contract, OpenCV and NumPy pre-imported into the handler namespace, helper functions for converting frames to and from NumPy arrays, current-frame dimension/format access, and an image loader that reads a JPEG/PNG from local disk or from S3 into an OpenCV array. Existing `handle`-based handlers keep working unchanged.

The feature spans the Portal (node catalog, packaging) and the LocalServer edge runtime (the Python bridge runner), with the vendored `workflow_core` catalog mirror kept in sync.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend) used to design, package, and deploy workflows.
- **LocalServer**: The Greengrass component running on an edge device that executes compiled workflow pipelines through GStreamer.
- **Node_Catalog**: The data catalog of node type descriptors in `workflow_core.catalog.nodes` (`NODE_CATALOG`), maintained in the Portal Lambda layer at `edge-cv-portal/backend/layers/workflow_core/` and mirrored verbatim in the LocalServer vendored copy at `src/backend/workflow_engine/vendor/workflow_core/`.
- **Workflow_Builder**: The graphical canvas UI (Node_Palette, canvas, NodeConfigPanel) where users compose workflows.
- **Node_Palette**: The categorized node type list in the Workflow_Builder, populated from the Node_Catalog via the node-catalog API.
- **Workflow_Validator**: The `workflow_core.validator` component checking graph structure and port type compatibility.
- **Workflow_Compiler**: The `workflow_core.compiler` component translating a workflow definition into per-architecture compiled pipeline documents.
- **Component_Packager**: The Portal backend packaging Lambda (`workflow_packaging.py`) that assembles per-architecture Workflow_Component artifact zips, including `python/{nodeId}/handler.py` and `python/{nodeId}/requirements.txt` for Custom Python nodes.
- **Custom_Python_Node**: The existing post-processing node type (`custom_python`) with per-instance input/output port type parameters, executed on the edge through the Python_Bridge.
- **Custom_Python_Preprocess_Node**: The new preprocessing node type (`custom_python_preprocess`) added by this feature, with fixed VideoFrames input and output ports.
- **Python_Bridge**: The LocalServer component (`src/backend/workflow_engine/python_bridge.py`) that replaces each `emlpython` element with an executor-managed appsink/appsrc pair and pumps frames through a handler subprocess over a framed stdin/stdout protocol.
- **Python_Runner**: The self-contained script (`RUNNER_SOURCE` in the Python_Bridge) executed inside the handler subprocess; it loads the user's `handler.py` and invokes the handler function once per frame.
- **Frame_Helpers**: The helper module added by this feature, importable from handler code as `dda_frames`, providing frame/array conversion, current-frame info, and image loading functions.
- **Pixel_Format**: The GStreamer video format string carried in the stream caps (this feature supports array conversion for RGB, BGR, RGBA, and GRAY8).

## Requirements

### Requirement 1: Custom Python Preprocessing Node Type

**User Story:** As a computer vision engineer, I want a Custom Python preprocessing node that takes video frames in and emits processed frames out, so that I can apply OpenCV transformations to the video stream before inference.

#### Acceptance Criteria

1. THE Node_Catalog SHALL provide a node type with type id `custom_python_preprocess`, display name "Custom Python (Frames)", and the preprocessing category.
2. THE Custom_Python_Preprocess_Node descriptor SHALL declare exactly one input port and one output port, both of Pixel_Format-carrying type VideoFrames, without per-instance port type override parameters.
3. THE Custom_Python_Preprocess_Node descriptor SHALL declare a required `code` parameter of parameter type `code` and an optional `requirements` parameter accepting extra pip packages in requirements.txt form.
4. THE Custom_Python_Preprocess_Node descriptor SHALL map on every architecture to the same `emlpython` element chain (carrying the `{python_handler_path}` argument) and `dda-emlpython` plugin dependency as the Custom_Python_Node.
5. THE Custom_Python_Preprocess_Node descriptor's `code` parameter description and examples SHALL document the `process_frame(frame, metadata)` contract with OpenCV, where `frame` is a NumPy array.
6. THE Node_Catalog copy in the LocalServer vendored mirror (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`) SHALL be byte-identical to the Portal layer copy after the change.

### Requirement 2: Validation, Compilation, and Packaging

**User Story:** As a computer vision engineer, I want workflows containing the new preprocessing node to validate, compile, and package exactly like other Custom Python nodes, so that I can deploy them to devices without special handling.

#### Acceptance Criteria

1. WHEN a workflow connects an output port whose type is compatible with VideoFrames under the Node_Catalog's declared coercion rules (VideoFrames, or InferenceMeta via the declared coercion) to a Custom_Python_Preprocess_Node input port, THE Workflow_Validator SHALL accept the connection.
2. WHEN a workflow connects an EventSignal output port to a Custom_Python_Preprocess_Node input port, THE Workflow_Validator SHALL report a port type compatibility error identifying the connection.
3. WHEN a workflow containing a Custom_Python_Preprocess_Node is compiled for any architecture, THE Workflow_Compiler SHALL emit an `emlpython` element for that node carrying handler path `python/{nodeId}/handler.py`.
4. WHEN a workflow containing Custom_Python_Preprocess_Nodes is packaged, THE Component_Packager SHALL write each such node's `code` to `python/{nodeId}/handler.py` and its `requirements` to `python/{nodeId}/requirements.txt` in every architecture artifact zip.
5. WHEN the per-architecture manifest is built, THE Component_Packager SHALL list the node ids of Custom_Python_Nodes and Custom_Python_Preprocess_Nodes together in `customPythonNodeIds`.

### Requirement 3: Frame-Based Handler Contract

**User Story:** As a computer vision engineer, I want to write my per-frame code as `process_frame(frame, metadata)` receiving a NumPy array, so that I can use OpenCV operations directly instead of decoding raw bytes myself.

#### Acceptance Criteria

1. WHEN a handler file defines a callable `process_frame`, THE Python_Runner SHALL invoke `process_frame(frame, metadata)` once per frame with `frame` as a NumPy uint8 array whose shape is derived from the stream's width, height, and Pixel_Format (rows × columns × channels; single-channel formats yield a rows × columns array).
2. WHEN `process_frame` returns a NumPy array with the same shape and dtype as the input array, THE Python_Runner SHALL emit the returned array's pixels as the node's output frame, preserving the frame's byte length including any row padding present in the input buffer.
3. WHEN `process_frame` returns None, THE Python_Runner SHALL emit the input frame unchanged.
4. IF `process_frame` returns a value whose shape or dtype differs from the input array, THEN THE Python_Bridge SHALL fail the workflow run with an error identifying the node and describing the mismatch.
5. IF a handler file defines `process_frame` and the frame's Pixel_Format is outside the supported conversion set (RGB, BGR, RGBA, GRAY8), THEN THE Python_Bridge SHALL fail the workflow run with an error identifying the node and the unsupported Pixel_Format.
6. WHEN a handler file defines a callable `handle` and no `process_frame`, THE Python_Runner SHALL invoke `handle(frame_bytes, metadata)` under the existing raw-bytes contract, unchanged in behavior.
7. WHEN a handler file defines both `process_frame` and `handle`, THE Python_Runner SHALL invoke `process_frame` and ignore `handle`.
8. IF a handler file defines neither `process_frame` nor `handle`, THEN THE Python_Bridge SHALL fail the workflow run with an error identifying the node and naming both accepted entry points.
9. WHEN THE Python_Bridge dispatches a frame to the handler subprocess, THE Python_Runner SHALL deliver the frame's width, height, and Pixel_Format to the handler inside the `metadata` dict under the key `frame`.
10. THE frame-based handler contract SHALL apply identically to handlers of Custom_Python_Nodes and Custom_Python_Preprocess_Nodes.

### Requirement 4: Pre-Imported OpenCV and Device Library Imports

**User Story:** As a computer vision engineer, I want OpenCV available in my handler code without boilerplate and the freedom to import other Python libraries installed on the device, so that I can write concise processing code.

#### Acceptance Criteria

1. WHEN a handler module is loaded and OpenCV is importable in the handler subprocess, THE Python_Runner SHALL bind `cv2` in the handler module's global namespace before the handler code executes.
2. WHEN a handler module is loaded and NumPy is importable in the handler subprocess, THE Python_Runner SHALL bind `np` and `numpy` in the handler module's global namespace before the handler code executes.
3. IF OpenCV or NumPy is not importable in the handler subprocess, THEN THE Python_Runner SHALL still load handler modules whose code does not reference the missing binding.
4. THE Python_Runner SHALL execute handler code with the device Python interpreter, so that a standard `import` statement in handler code resolves any Python library installed on the device.
5. WHEN handler code imports a Python module shipped beside `handler.py` in the node's artifact directory, THE Python_Runner SHALL resolve the import.

### Requirement 5: Frame Input and Output Helper Functions

**User Story:** As a computer vision engineer, I want helper functions for getting frames into and out of NumPy arrays, so that I can also work with frames from the raw-bytes `handle` contract or convert data explicitly.

#### Acceptance Criteria

1. THE Frame_Helpers module SHALL be importable from handler code as `dda_frames` without the node shipping additional files in its artifacts.
2. THE Frame_Helpers module SHALL provide `to_array(frame_bytes, width, height, format)` converting raw frame bytes to a NumPy uint8 array for the RGB, BGR, RGBA, and GRAY8 Pixel_Formats, tolerating row padding in the frame bytes.
3. THE Frame_Helpers module SHALL provide `to_bytes(array)` converting a NumPy uint8 array to raw frame bytes.
4. FOR ALL frames in the supported Pixel_Formats without row padding, `to_bytes(to_array(frame_bytes, width, height, format))` SHALL equal the original frame bytes (round-trip property).
5. IF `to_array` is called with a Pixel_Format outside the supported set or with frame bytes too short for the stated dimensions, THEN THE Frame_Helpers SHALL raise an error describing the format or size problem.
6. WHILE a handler invocation is in progress, THE Frame_Helpers module SHALL return the current frame's width, height, and Pixel_Format from a `frame_info()` function.
7. THE Frame_Helpers module SHALL be available to handlers of Custom_Python_Nodes and Custom_Python_Preprocess_Nodes alike.

### Requirement 6: Image Loading from Disk or S3

**User Story:** As a computer vision engineer, I want to load a reference image (for example a golden-sample JPEG) from the device file system or from S3 into an OpenCV array, so that my handler can compare or combine it with live frames.

#### Acceptance Criteria

1. WHEN `dda_frames.load_image(source)` is called with a local file path of a decodable image file, THE Frame_Helpers SHALL return the decoded image as a NumPy uint8 array in OpenCV BGR channel order (single-channel images decode to a two-dimensional array).
2. WHEN `dda_frames.load_image(source)` is called with an `s3://bucket/key` URI, THE Frame_Helpers SHALL download the object using the device's AWS credentials and return the decoded image as a NumPy uint8 array in OpenCV BGR channel order.
3. FOR ALL image arrays written losslessly to disk as PNG, `load_image` of the written file SHALL return an array equal to the original array (round-trip property).
4. IF the local file does not exist, the S3 object cannot be fetched, the URI is malformed, or the content cannot be decoded as an image, THEN THE Frame_Helpers SHALL raise an error identifying the source and describing the failure.
5. WHEN a handler raises the `load_image` error, THE Python_Bridge SHALL fail only that workflow run with the node identified, consistent with existing handler failure containment.

### Requirement 7: Workflow Designer Support

**User Story:** As a computer vision engineer, I want the new preprocessing node to appear and behave correctly in the workflow designer, so that I can place, configure, and connect it like any other node.

#### Acceptance Criteria

1. WHEN a user opens the Node_Palette, THE Workflow_Builder SHALL display the Custom_Python_Preprocess_Node in the preprocessing section.
2. WHEN a user selects a Custom_Python_Preprocess_Node, THE Workflow_Builder SHALL render a code editor for the `code` parameter.
3. WHEN a user attempts to connect an output port whose type is not compatible with VideoFrames under the declared coercion rules (VideoFrames exactly, or InferenceMeta via the declared coercion, consistent with Requirement 2.1) to a Custom_Python_Preprocess_Node input port, THE Workflow_Builder SHALL reject the connection and display the reason.
4. WHILE a Custom_Python_Preprocess_Node has no `code` value, THE Workflow_Builder SHALL display an inline required-parameter validation marker on the node.

### Requirement 8: Accurate Custom Python Contract Documentation

**User Story:** As a computer vision engineer, I want the Custom Python node's in-designer documentation to match the actual runtime contract, so that code I write from the parameter description runs on the device.

#### Acceptance Criteria

1. THE Custom_Python_Node descriptor's `code` parameter description SHALL state the actual runtime entry points (`process_frame(frame, metadata)` for NumPy-array processing and `handle(frame_bytes, metadata)` for raw bytes) in place of the current non-existent `process(data, metadata)` contract.
2. THE Custom_Python_Node descriptor's `code` parameter examples SHALL contain at least one example that is a valid handler under the runtime contract.
