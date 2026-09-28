# Requirements Document

## Introduction

Today a workflow can contain at most one frame-fed camera source.

- **The Portal rejects a second source.** The Workflow_Validator reports `V7_COEXISTENCE_CONFLICT` for a second `aravis_camera_source` or `custom_python_source` node, or for one of each (`workflow_core/validator/checks.py`, `COEXISTENCE_SINGLETON_TYPES`).
- **The device would refuse the run anyway.**
  - Its feed planners accept one Frame_Feed point (`aravis_feed.plan_aravis_feeds`, `python_source.plan_python_sources`).
  - The executor pushes one frame into one element renamed `appsrc` (`pipeline_executor._point_appsrc_at_frame_feed`, `gstreamer/gst_pipeline.py`).
  - Every run writes one base image, one overlay and one result set under a single `capture_id`.

**Why the limit exists.** It was an initial scope choice, not a hardware or GStreamer constraint:

- `aravis-camera-input` design: "Initial scope executes one Aravis feed per run".
- `custom-python-source` Requirement 8.

Much of the plumbing is already per node:

- The compiler names each source's element `appsrc_{nodeId}`.
- Packaging emits one binding point per camera node.
- Device-side camera binding resolution is keyed by node.

**What this feature does.** It lets one workflow contain several frame-fed sources, each feeding its own branch. For example:

- the Static_Image_Camera and the Static_Video_Camera side by side, which is Requirement 6.4 of `static-camera-video-loop`, deferred to this spec;
- two GenICam cameras inspecting different views of one part.

One trigger runs the whole workflow:

1. Every source grabs one frame.
2. Every branch processes its own frame.
3. The run's results keep each branch's images and detections apart.

Workflows with one source keep validating, compiling, packaging and running exactly as they do today.

## Glossary

- **Frame_Feed_Source**: A workflow node whose frame the LocalServer supplies at run time instead of an in-pipeline element. These are `aravis_camera_source` and `custom_python_source`, including a `unified_input` that expands to one of them.
- **Single_Source_Workflow**: A workflow with at most one Frame_Feed_Source.
- **Multi_Source_Workflow**: A workflow with two or more Frame_Feed_Sources.
- **Source_Branch**: The nodes reachable downstream from one Frame_Feed_Source.
- **Run**: One execution of a registered workflow on the device (a `WorkflowExecution`).
- **Multi_Source_Floor**: For each LocalServer variant, the first version that can run a Multi_Source_Workflow.
- **Frame_Feed**: As defined in `aravis-camera-input`.
- **Workflow_Validator**, **Workflow_Compiler**, **Workflow_Packager**, **Workflow_Builder**, **Workflow_Executor**, **Workflow_Test_Runner**, **Generation_Gate**: The existing components with these names.

## Requirements

### Requirement 1: Validation

**User Story:** As a workflow developer, I want to put more than one camera source in a workflow, so that one workflow can inspect several views or inputs in the same run.

#### Acceptance Criteria

1. THE Workflow_Validator SHALL accept a workflow with two to four Frame_Feed_Sources, in any mix of `aravis_camera_source` and `custom_python_source`, and SHALL NOT report `V7_COEXISTENCE_CONFLICT` for it.
2. IF a workflow contains more than four Frame_Feed_Sources, THEN THE Workflow_Validator SHALL report an error naming the limit and every source node.
3. IF a node is reachable from more than one Frame_Feed_Source, THEN THE Workflow_Validator SHALL report an error naming that node and the sources that reach it. Joining branches is out of scope.
4. THE Portal's inline builder checks and the device's vendored validator copy SHALL apply the same rules with the same codes and messages as the Workflow_Validator.
5. THE Generation_Gate SHALL accept Multi_Source_Workflows within the limits of criteria 1 to 3, and its repair guidance SHALL describe those limits instead of "keep at most one node of this type".

### Requirement 2: Building a Multi_Source_Workflow

**User Story:** As a workflow developer, I want to add a second camera node in the builder and bind each node to its own camera, so that I can build the workflow without editing JSON.

#### Acceptance Criteria

1. THE Workflow_Builder SHALL let a user add several Frame_Feed_Source nodes and choose a camera for each one independently, with the existing per-node camera picker and shortcuts.
2. WHILE a Multi_Source_Workflow violates Requirement 1, THE Workflow_Builder SHALL mark the offending nodes with the validator's message.
3. WHEN both static cameras are pinned on the reference device, THE camera picker of each source node SHALL offer both, so that one node can bind the Static_Image_Camera and another the Static_Video_Camera.

### Requirement 3: Compilation and packaging

**User Story:** As a workflow developer, I want a Multi_Source_Workflow to package like any other workflow, so that I can deploy it the same way.

#### Acceptance Criteria

1. THE Workflow_Compiler SHALL compile a Multi_Source_Workflow into one pipeline document in which each Frame_Feed_Source keeps its own `appsrc_{nodeId}` element.
2. THE Workflow_Packager SHALL emit one binding point per Frame_Feed_Source, as it does today, and SHALL record the number of Frame_Feed_Sources in the package manifest.
3. THE Workflow_Packager SHALL give a Multi_Source_Workflow package, for each target architecture, a minimum LocalServer version no lower than that variant's Multi_Source_Floor.
4. THE Workflow_Compiler and Workflow_Packager SHALL produce Single_Source_Workflow documents and packages that are byte-identical to today's.

### Requirement 4: Deployment

**User Story:** As an operator, I want deployment to check every camera a workflow needs, so that a missing camera or an old LocalServer is caught before the workflow reaches the device.

#### Acceptance Criteria

1. WHEN a Multi_Source_Workflow is deployed, THE deployment camera-binding check SHALL check each Frame_Feed_Source's camera on each target device, and SHALL report any problem per source node.
2. IF a target device runs a LocalServer older than its variant's Multi_Source_Floor, THEN THE deployment check SHALL refuse the deployment, naming the device, its version, and the minimum version.

### Requirement 5: Running a Multi_Source_Workflow

**User Story:** As a workflow developer, I want one trigger to capture from every camera in the workflow, so that the branches inspect the same moment.

#### Acceptance Criteria

1. WHEN a Run of a Multi_Source_Workflow starts, THE Workflow_Executor SHALL grab one frame from every Frame_Feed_Source before it starts the pipeline.
2. THE Workflow_Executor SHALL push each frame into its own source's `appsrc_{nodeId}` element with caps matching that frame, then end the stream on every source element.
3. THE Workflow_Executor SHALL start the grabs for all sources concurrently rather than one after another, and SHALL record each source's grab start and end times in the Run metadata.
4. WHEN two Frame_Feed_Sources in one workflow reference the same camera, THE Workflow_Executor SHALL grab that camera once per Run and feed the same frame to both sources.
5. THE Workflow_Executor SHALL apply each source's own device Image_Source configuration, including its gain, exposure and region of interest, to that source's frame. An explicit Crop node SHALL suppress the device region of interest only in its own Source_Branch.
6. IF any source's grab fails, THEN THE Workflow_Executor SHALL fail the Run without starting the pipeline, and the Run SHALL name the failing source node and camera.
7. WHEN a trigger connected to the workflow fires, including a trigger wired to one source's activation port, THE Workflow_Executor SHALL start one Run that grabs every Frame_Feed_Source.
8. THE Workflow_Executor SHALL run a Single_Source_Workflow exactly as it does today.

### Requirement 6: Run results

**User Story:** As a workflow developer, I want each camera's results kept apart, so that I can see what every branch saw and found.

#### Acceptance Criteria

1. THE Workflow_Executor SHALL store each Source_Branch's output image, overlay, mask and detection results separately in the Run's output directory, named by source node, so that no branch overwrites another branch's files. Two capture nodes in different branches SHALL never write the same file.
2. THE Workflow_Executor SHALL keep today's artifact names for a Single_Source_Workflow (`{capture_id}.jpg`, `.overlay.jpg`, `.mask.png`, `.jsonl`).
3. THE LocalServer run-results API SHALL list each Source_Branch's outputs with its source node id and camera id.
4. THE LocalServer run views and the Portal run views SHALL show each branch's images and detections, labeled by source.
5. WHEN an output node (MQTT, OPC UA, Modbus, capture) runs, it SHALL use its own Source_Branch's results, including in payload templates such as `{detection_count}`.

### Requirement 7: Testing and previews

**User Story:** As a workflow developer, I want to test a Multi_Source_Workflow before I deploy it, so that I can check each branch against known images.

#### Acceptance Criteria

1. WHEN a user tests a Multi_Source_Workflow, THE Workflow_Test_Runner SHALL feed each Frame_Feed_Source images from the Test_Dataset the user selected for that source.
2. IF a Frame_Feed_Source has no Test_Dataset selected, THEN THE Workflow_Test_Runner SHALL refuse to start the test and name the source.
3. THE Workflow_Test_Runner and the builder's node previews SHALL behave exactly as today for a Single_Source_Workflow.

### Requirement 8: Preservation and cross-spec consistency

**User Story:** As a maintainer, I want existing workflows and specs to stay valid, so that this feature cannot regress deployed devices.

#### Acceptance Criteria

1. For a Single_Source_Workflow, these SHALL be unchanged: validation findings, compiled documents, packages, device Runs, artifact names, and run-results API responses.
2. THE statements that limit a workflow to one Frame_Feed_Source SHALL be amended to reference this spec: the `aravis-camera-input` design scope and error table, and `custom-python-source` Requirement 8. Every other requirement of those specs SHALL keep holding.
3. WHEN a workflow binds one source to the Static_Image_Camera and another to the Static_Video_Camera, the image branch SHALL receive the pinned image and the video branch SHALL receive the frame at the current Loop_Position. This closes `static-camera-video-loop` Requirement 6.4.
4. A package built before this feature SHALL keep running unchanged on a LocalServer that has this feature.

### Requirement 9: Verification on hardware

**User Story:** As a maintainer, I want the feature proven on every device type, so that it ships with the same confidence as other edge changes.

#### Acceptance Criteria

1. THE feature SHALL be verified on MIC-730 (JP5), Orin AGX (JP6), thor1 (JP7) and the Dell (amd64), with a two-source workflow that binds the Static_Image_Camera and the Static_Video_Camera.
2. On at least one device with two cameras (for example a GenICam camera and a virtual camera), the feature SHALL be verified with a workflow that uses both.
3. In each verification, workflow runs SHALL complete repeatedly, SHALL keep each branch's results apart, and SHALL leave the backend healthy for 30 minutes or more.

## Out of scope

- Nodes that combine frames or results from several Source_Branches, such as joins, comparisons or voting across cameras.
- Hardware-synchronized capture, and any guaranteed maximum time skew between sources beyond the concurrent grabs of Requirement 5.3.
- Per-branch triggers that run only part of a workflow.
- Partial Runs in which some branches proceed after another source fails.
- In-pipeline sources (`csi_camera_source`, `icam_source`, `folder_source`). They are not Frame_Feed_Sources, and this spec does not change how they combine.

## Open questions for review

1. **Source limit.** Is four Frame_Feed_Sources per workflow the right limit (Requirement 1.1)?
2. **Test inputs.** Should the Workflow_Test_Runner take one Test_Dataset per source (Requirement 7.1), or feed the same dataset image to every source?
3. **Joins.** Should a first join or compare node be part of this spec, or a follow-up?
