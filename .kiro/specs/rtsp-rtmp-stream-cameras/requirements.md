# Requirements Document

## Introduction

DDA workflows cannot use a network video camera as an input today. The LocalServer knows four Image_Source types (`Camera` for Aravis, `Folder`, `ICam`, `NvidiaCSI`). The Portal's device Cameras tab already offers an `RTSP` type, but the device rejects it on apply and no built-in workflow node can bind to it. The only workaround is a hand-built camera-backed custom node, and that puts the stream URL, credentials included, into the compiled pipeline. The workflow engine also runs exactly one frame per run, starts runs only from manual, MQTT, or OPC UA triggers, keeps every run forever, and sends every output on every run. So even with a stream source, it could not watch a live scene. Detection models can already be trained in the Portal and run on devices, but run metadata carries only a flat detection list and a total count. Conditions and outputs therefore cannot express "a person without a hard hat" or "twelve boxes in the staging zone".

This feature makes RTSP and RTMP cameras first-class workflow inputs on every LocalServer target, with H.264 and H.265 video. It also makes live-scene analytics practical:

- **Catalog nodes**: two input node types, `rtsp_camera_source` ("RTSP Camera") and `rtmp_stream_source` ("RTMP Stream"). They flow through the existing builder, camera picker, packaging, deploy-time binding, and device execution seams, the same way the Aravis and CSI/ICAM nodes do.
- **Device stream ingest**: one isolated, self-recovering connection per camera, shared by workflows, live preview, and image capture. It decodes in hardware where the device exposes a hardware decoder and in software otherwise.
- **Two processing modes**: `on_trigger`, where a triggered run analyzes the camera's latest frame, and `continuous`, where the device samples the stream at a configured rate and runs the workflow on each sampled frame.
- **Bounded continuous operation**: run retention, RAM-staged artifacts, and log limits, so a station never fills its disk or floods its logs.
- **Credential handling**: camera credentials never appear in workflows, compiled documents, shadows, registry items, API responses, or logs.
- **Scene analytics nodes**: a Detection Counter (per-class counts, optionally inside a zone), an Object Association check (for example, every person has a hard hat and a vest), and an Event Gate (fire once when a condition persists, not on every frame).

Scope decisions this document commits to:

- RTMP is supported in pull mode: the device connects to an RTMP URL as a client. A device-hosted RTMP ingest endpoint for cameras that can only push is out of scope. Such cameras can publish to an RTMP server, and the device pulls from it.
- Continuous mode samples frames into the existing single-frame run model, up to 10 frames per second per workflow. A persistent streaming inference pipeline is out of scope.
- Out of scope:
  - ONVIF discovery and PTZ
  - Audio
  - Recording and NVR functions
  - Re-streaming
  - WebRTC or HLS preview
  - Multi-object tracking and line-crossing counts
  - The non-standard HEVC-in-FLV "codec id 12" variant
  - A visual zone editor
  - Portal-side views of continuous-run history
  - Classic (non-workflow) Pipeline_Configuration workflows that use stream cameras

## Glossary

- **Portal**: The edge-cv-portal cloud application (React frontend, Lambda backend, DynamoDB) that manages DDA use cases, models, workflows, deployments, and devices.
- **LocalServer**: The Greengrass component on an Edge_Device. It owns device-local Image_Sources and runs deployed workflows. Its backend runs in the `flask-app` container, and its web UI is served by the `react-webapp` container.
- **Edge_Device**: A Greengrass core device that runs LocalServer and is registered in the Portal.
- **Use_Case_Account**: The AWS account that holds a use case's devices, and so the device role, the device shadows, and the Credential_Vault.
- **Node_Catalog**: The shared workflow_core node type catalog. It is kept as two byte-identical copies: the Portal layer (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`) and the LocalServer vendor mirror (`src/backend/workflow_engine/vendor/workflow_core/`).
- **Workflow_Builder**: The Portal's graphical workflow editor.
- **Camera_Picker**: The Workflow_Builder's camera reference control, which offers a reference device's Camera_Registry entries for a camera node's identity parameter and records a `cameraBindingHint`.
- **Component_Packager**: The Portal packaging Lambda (`workflow_packaging.py`). It compiles workflows per architecture and emits `bindingPoints` for camera input nodes.
- **Deployment_Service**: The Portal deployment Lambda (`deployments.py`). It includes the binding context, `validate_camera_bindings`, and `dda-camera-bindings` shadow delivery.
- **Camera_Registry**: The Portal's per-device Camera_Source store and API (`camera_registry.py`, table `dda-portal-camera-registry`). It is synchronized with devices through the `dda-camera-registry` named shadow.
- **Camera_Source**: One entry in the Camera_Registry or in a device's reported inventory. It has an id, name, type, params, capabilities, origin, version, and sync metadata.
- **Edge_Sync_Agent**: The LocalServer component (`src/backend/camera_sync/`). It builds and reports the device inventory and applies Portal-originated camera changes.
- **Workflow_Engine**: The LocalServer subsystem (`src/backend/workflow_engine/`). It registers deployed workflows, resolves camera bindings, and runs workflow executions.
- **Workflow_Executor**: The Workflow_Engine component (`pipeline_executor.py`) that runs one execution's compiled pipeline.
- **Frame_Feed**: The existing mechanism by which the Workflow_Executor pushes one frame into a compiled pipeline's `appsrc` (the Aravis and Custom Python source feeds).
- **Frame_Feed_Source_Node**: An input node whose frame the Workflow_Executor pushes through the Frame_Feed. Before this feature these are `aravis_camera_source` and `custom_python_source`; after it, the two Stream_Camera_Source_Nodes are included too.
- **Stream_Camera**: A network video source that the Edge_Device reads over RTSP or RTMP. It is configured on the device as an Image_Source of type `RTSP` or `RTMP` and reported to the Camera_Registry under the same type.
- **Stream_URL**: The credential-free locator of a Stream_Camera. It has a scheme (`rtsp`, `rtsps`, `rtmp`, or `rtmps`), a host, and an optional port, path, and query. It carries no user information and no Secret_Query_Parameter.
- **Secret_Query_Parameter**: A URL query parameter whose name, compared case-insensitively, is one of `password`, `passwd`, `pwd`, `pass`, `secret`, `token`, `key`, `apikey`, `api_key`, `auth`, `signature`, `sig`, `streamkey`, or `stream_key`.
- **Stream_Credentials**: A Stream_Camera's secret material. It can hold an optional username and password (RTSP basic or digest authentication, or RTMP user information) and an optional URL secret suffix. The suffix is appended to the Stream_URL only when connecting, and covers RTMP stream keys and secret query strings.
- **Credential_Store**: The LocalServer's device-local store of Stream_Credentials.
- **Credential_Vault**: The AWS Secrets Manager secrets, in the device's Use_Case_Account, that hold Stream_Credentials entered in the Portal.
- **Credential_Reference**: The non-secret pointer to one Credential_Vault secret version: the secret ARN and version id.
- **Stream_Ingest_Service**: The new LocalServer subsystem that owns every Stream_Session on the device.
- **Stream_Session**: The single connection to, and decode of, one Stream_Camera on an Edge_Device. It is shared by every consumer of that camera.
- **Stream_Worker**: The isolated operating-system process that runs one Stream_Session's network ingest and video decoding.
- **Stream_Lease**: A consumer's hold on a Stream_Session. The consumer can be a registered workflow, a live-preview viewer, an image capture, or a connection test.
- **Latest_Frame**: The most recently decoded frame of a Stream_Session. It carries a sequence number, an acquisition timestamp, and its dimensions.
- **Decoder_Policy**: A Stream_Camera's decoder preference: `auto`, `hardware`, or `software`.
- **Stream_Health**: A Stream_Session's observable state and properties.
  - State: `connecting`, `streaming`, `reconnecting`, `failed`, or `stopped`.
  - Properties: codec, resolution, measured frame rate, decoder in use, reconnect count, last frame time, and last error.
- **Device_Stream_Capabilities**: The protocol, codec, and decoder combinations a LocalServer can serve, found by probing its runtime.
- **Stream_Camera_Source_Node**: Either of the new input node types, `rtsp_camera_source` or `rtmp_stream_source`.
- **Processing_Mode**: A Stream_Camera_Source_Node's run model: `continuous` or `on_trigger`.
- **Subscription_Trigger_Node**: An `mqtt_subscribe` or `opcua_subscribe` node, which starts runs when an external event arrives.
- **Continuous_Runner**: The LocalServer component that starts runs of a registered continuous-mode workflow at its sampling rate.
- **Sampling_Tick**: One scheduled run start of a Continuous_Runner. Ticks occur every 1/`frames_per_second` seconds.
- **Notable_Run**: A continuous run that failed, sent at least one output, or recorded an Event_Gate transition.
- **Detection_List**: The existing run metadata `detections` list. Each entry has `id`, `label`, `confidence`, `x_min`, `y_min`, `x_max`, and `y_max`, with coordinates in source-frame pixels.
- **Label_Key**: A detection label normalized for use in metadata keys and conditions. It is lowercased, each run of characters other than ASCII letters and digits is replaced by one underscore, and leading and trailing underscores are removed.
- **Zone**: A polygon of 3 to 32 points in normalized frame coordinates (0.0 to 1.0) that restricts which detections a Scene_Analytics_Node considers.
- **Scene_Analytics_Node**: Any of the new post-processing node types `detection_counter`, `object_association`, and `event_gate`.
- **Event_Gate_State**: The state an `event_gate` node carries between runs, kept per registration and per node.
- **Condition_Language**: The existing rule-expression language of `inference_filter`, `conditional`, and `digital_output`, including dotted field paths.

## Requirements

### Requirement 1: Stream camera source node types in the Node_Catalog

**User Story:** As a computer vision engineer, I want RTSP and RTMP camera input nodes in the workflow builder, so that I can build workflows whose frames come from live network video.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include an `rtsp_camera_source` node type with display name "RTSP Camera" and an `rtmp_stream_source` node type with display name "RTMP Stream". Each SHALL be in the input category, with one `activation` input port of type EventSignal and exactly one `out` output port of type VideoFrames.
2. Each Stream_Camera_Source_Node SHALL declare the following parameters, each with a description and at least one working example:
   - `url` (string, required): the Stream_URL.
   - `processing_mode` (enum `continuous` | `on_trigger`, default `continuous`).
   - `frames_per_second` (float, 0.05 to 10, default 1.0), visible only while `processing_mode` is `continuous`.
   - `max_frame_age_ms` (int, 100 to 60000, default 2000).
   - `keep_recent_runs` (int, 1 to 200, default 20), visible only while `processing_mode` is `continuous`.
   - `keep_notable_runs` (int, 0 to 5000, default 200), visible only while `processing_mode` is `continuous`.
3. THE Node_Catalog's `url` constraint SHALL reject a value whose scheme is not `rtsp`, `rtsps`, `rtmp`, or `rtmps`, a value with no host, and a value that embeds user information in its authority.
4. Each Stream_Camera_Source_Node SHALL be hardware dependent. Its mappings SHALL be:
   - On every physical device architecture, the appsrc-headed element chain `appsrc name=appsrc_{nodeId} ! videoconvert`, with plugin dependencies `app` and `videoconvertscale`.
   - On the `sim` architecture, the shared dataset-fed simulation stub.

   No parameter SHALL appear in any element argument.
5. WHEN a workflow definition containing Stream_Camera_Source_Nodes is validated, compiled, or serialized through workflow_core, THE Node_Catalog SHALL process them through the generic descriptor-driven paths, and parsing then serializing the definition SHALL produce an equivalent definition.
6. THE unified `Input Source` node SHALL offer the source kinds `rtsp_camera` and `rtmp_stream`, which expand to `rtsp_camera_source` and `rtmp_stream_source`. The Portal frontend's source-kind map SHALL mirror the catalog's.
7. THE Node_Catalog SHALL append the new node types after every existing entry. Every existing descriptor's position and content SHALL stay unchanged, and both mirrored copies SHALL carry the new node types identically.

### Requirement 2: Workflow validation for stream sources

**User Story:** As a workflow author, I want the builder to catch stream-source mistakes before I deploy, so that invalid stream workflows never reach a device.

#### Acceptance Criteria

1. WHEN a Stream_Camera_Source_Node's `url` has a scheme that does not belong to its node type, THE workflow validator SHALL report an error finding on that node naming the accepted schemes. The accepted schemes are `rtsp` and `rtsps` for `rtsp_camera_source`, and `rtmp` and `rtmps` for `rtmp_stream_source`.
2. WHEN a Stream_Camera_Source_Node's `url` contains a Secret_Query_Parameter or embedded user information, THE workflow validator SHALL report an error finding on that node stating that credentials belong in the camera's configuration.
3. THE workflow validator SHALL report an error finding on a Stream_Camera_Source_Node in `continuous` mode, stating that continuous processing is its own activation model, WHEN either of the following holds:
   - The node has a connection into its `activation` port.
   - The workflow also contains a Subscription_Trigger_Node.
4. THE workflow validator SHALL treat both Stream_Camera_Source_Node types as Frame_Feed_Source_Nodes. WHEN a workflow contains two or more Frame_Feed_Source_Nodes of any combination of types, THE workflow validator SHALL report one coexistence error finding per such node, naming every member.
5. WHEN a Stream_Camera_Source_Node is in `on_trigger` mode, THE workflow validator SHALL apply the existing activation-model rule to it exactly as it does to any other input node.
6. THE Workflow_Builder's inline checks SHALL report the same finding codes, node ids, and severities as the backend validator for the rules in this requirement.
7. WHEN a workflow definition contains no Stream_Camera_Source_Node and no Scene_Analytics_Node, THE workflow validator SHALL report exactly the findings it reported before this feature.

### Requirement 3: Workflow_Builder support for stream sources

**User Story:** As a computer vision engineer, I want to pick a device's registered stream camera from the node panel, so that my workflow input matches a camera that exists and I never type credentials into a workflow.

#### Acceptance Criteria

1. WHEN the Workflow_Builder renders its palette, THE palette SHALL list "RTSP Camera" and "RTMP Stream" under the Input category, taken from the served catalog.
2. WHEN a user configures a Stream_Camera_Source_Node, THE node configuration panel SHALL render `url` as the camera reference control and `processing_mode` as a selection. It SHALL show each mode-dependent parameter only while its mode is selected.
3. WHEN the Camera_Picker lists Camera_Sources for a Stream_Camera_Source_Node, THE Camera_Picker SHALL offer only the reference device's entries whose type matches the node: `RTSP` for `rtsp_camera_source`, and `RTMP` for `rtmp_stream_source`. For each entry it SHALL show:
   - Name and Stream_URL.
   - Reported codec and resolution, when available.
   - Stream_Health state, sync status, and staleness.
   - Whether credentials are configured.
4. WHEN a user selects a Stream_Camera, THE Camera_Picker SHALL set `url` to the entry's Stream_URL and record the selection as the node's `cameraBindingHint`. It SHALL copy no credential material into the node.
5. WHERE a user types a URL manually, THE Workflow_Builder SHALL accept a Stream_URL. It SHALL reject a URL that embeds credentials, with a message stating that credentials belong in the camera's configuration.
6. THE Workflow_Builder SHALL provide no credential input on any Stream_Camera_Source_Node.

### Requirement 4: Stream camera configuration on the device

**User Story:** As an operator at the station, I want to add RTSP and RTMP cameras in the LocalServer UI and check that they work, so that workflows and previews can use them.

#### Acceptance Criteria

1. THE LocalServer SHALL support Image_Sources of type `RTSP` and `RTMP` through its API and UI, with these fields:
   - Name, description, and Stream_URL.
   - Stream_Credentials, write-only.
   - Transport (`RTSP` only): `tcp`, `udp`, or `auto`, default `tcp`.
   - Latency in milliseconds (`RTSP` only): 0 to 5000, default 200.
   - Decoder_Policy, default `auto`.
   - Maximum frame dimension: 320 to 4096 pixels, default 1920.
   - Stall timeout: 2 to 60 seconds, default 10.
2. WHEN a stream Image_Source is created or updated with an invalid Stream_URL, an invalid setting, or credentials embedded in the URL, THE LocalServer SHALL reject the request with a message identifying the field.
3. WHEN an operator runs a connection test on a stream Image_Source, THE LocalServer SHALL report one of the following within 20 seconds:
   - Success, with the detected codec, resolution, measured frame rate, decoder in use, and a preview frame.
   - Failure, with a reason category and a redacted detail message. The reason category SHALL be one of `unreachable`, `authentication_failed`, `not_found`, `unsupported_codec`, `decoder_unavailable`, `tls_verification_failed`, or `timeout`.
4. THE LocalServer SHALL provide live preview and single-frame image capture for stream Image_Sources through the existing preview and capture actions.
5. WHEN the Edge_Sync_Agent reports inventory, THE Edge_Sync_Agent SHALL report each stream Image_Source as a Camera_Source of type `RTSP` or `RTMP` with origin `edge-configured`, where:
   - Its parameters carry the Stream_URL, the non-secret settings, and a `credentialsConfigured` flag.
   - Its capabilities carry the codec, resolution, decoder in use, and coarse Stream_Health state.
6. THE Edge_Sync_Agent SHALL re-report a stream camera's capabilities only when its codec, resolution, decoder in use, or coarse Stream_Health state changes.
7. WHEN a stream Image_Source is deleted, THE LocalServer SHALL delete its Stream_Credentials from the Credential_Store and stop its Stream_Session.
8. IF a classic Pipeline_Configuration workflow or a digital-input capture is configured with a stream Image_Source, THEN THE LocalServer SHALL reject the configuration with a message stating that stream cameras are supported in deployed workflows, live preview, and image capture.

### Requirement 5: Stream camera management in the Portal

**User Story:** As a fleet operator, I want to register stream cameras for a device in the Portal, credentials included, so that I can set up stations without visiting them.

#### Acceptance Criteria

1. THE Portal's device Cameras tab SHALL offer the `RTSP` and `RTMP` types with type-specific form fields matching Requirement 4.1, in place of the raw parameters JSON. It SHALL render credential fields as masked, write-only inputs.
2. WHEN the Camera_Registry receives a create or update for an `RTSP` or `RTMP` Camera_Source, THE Camera_Registry SHALL validate the Stream_URL and settings against the rules of Requirements 1.3, 2.1, 2.2, and 4.1. It SHALL reject credential material inside `params` with a 400 response identifying the field. Validation of every other type SHALL stay unchanged.
3. WHEN a create or update carries Stream_Credentials, THE Camera_Registry SHALL store them in the Credential_Vault of the device's Use_Case_Account before writing the desired change. The desired change, registry item, pending content, conflict records, and audit events SHALL carry only the Credential_Reference.
4. IF writing the desired change fails after the credentials were stored, THEN THE Camera_Registry SHALL withdraw the stored secret version so that nothing references it, and SHALL return the existing delivery-failure response with the registry unchanged.
5. WHEN an Edge_Device applies a change carrying a Credential_Reference, THE Edge_Sync_Agent SHALL retrieve the credentials with the device's own AWS credentials, store them in the Credential_Store, and acknowledge the change.
6. IF retrieving a Credential_Reference fails, THEN THE Edge_Sync_Agent SHALL report the change as failed, with a reason that contains no secret material.
7. THE Camera_Registry SHALL never return credential values. It SHALL report `credentials.configured` and the time the credentials were last updated. It SHALL return any stored `url` that contains user information with the user information redacted, including entries created before this feature.
8. WHEN a Portal-managed stream camera is deleted, THE Camera_Registry SHALL schedule deletion of its Credential_Vault secret. WHEN its credentials are updated, THE Camera_Registry SHALL write a new secret version and deliver the new Credential_Reference.
9. IF the Use_Case_Account does not grant the Portal access to store credentials, THEN THE Camera_Registry SHALL reject a credentialed create or update with a message naming the missing capability, SHALL leave the registry and shadow unchanged, and SHALL still accept credential-free stream cameras.
10. THE stream camera routes SHALL use the existing camera registry authorization and audit events: the view and manage device permissions, resolved from the device's use case.

### Requirement 6: Stream credential confidentiality

**User Story:** As a security reviewer, I want camera credentials kept out of every artifact, log, and API response, so that a workflow export, a log bundle, or a registry read never leaks camera access.

#### Acceptance Criteria

1. Stream_Credentials SHALL NOT appear in any of the following:
   - Workflow definitions, compiled pipeline documents, or binding points.
   - The `dda-camera-registry` or `dda-camera-bindings` shadows.
   - Portal DynamoDB items.
   - Portal or LocalServer API responses.
   - Audit events, component logs, run logs, or run metadata.
   - GStreamer debug output.
   - Any process's command line or environment.
2. THE Credential_Store SHALL keep Stream_Credentials in a file readable and writable only by its owner (mode 0600), inside a directory accessible only by its owner (mode 0700). The file SHALL be outside the SQLite databases and outside every path the LocalServer serves over HTTP.
3. THE LocalServer SHALL apply a redaction filter to every log record its backend writes. The filter SHALL mask URL user information, Secret_Query_Parameter values, and every value held in the Credential_Store.
4. THE Stream_Ingest_Service SHALL pass Stream_Credentials to a Stream_Worker only through the worker's standard input.
5. WHEN a Stream_URL uses `rtsps` or `rtmps`, THE Stream_Ingest_Service SHALL verify the server certificate and host name against the system trust store. It SHALL fail the session with `tls_verification_failed` when verification fails, with no unverified fallback.
6. THE Portal SHALL hold permission to create, update, and delete Credential_Vault secrets but not to read their values.
7. Each Edge_Device's role SHALL be able to read only the Credential_Vault secrets stored for its own thing name.

### Requirement 7: Stream ingest and decoding

**User Story:** As an operator, I want my cameras' H.264 and H.265 streams decoded over RTSP or RTMP, with hardware decoding where the device has it, so that stream workflows run efficiently on every station type.

#### Acceptance Criteria

1. THE Stream_Ingest_Service SHALL read RTSP streams over the configured transport, authenticate with basic or digest authentication using the Stream_Credentials, and decode H.264 and H.265 video tracks.
2. THE Stream_Ingest_Service SHALL read RTMP streams as a client (pull), authenticate with the Stream_Credentials, and decode both H.264 video and H.265 video carried as Enhanced RTMP.
3. THE Stream_Ingest_Service SHALL decode the first video track and ignore audio, metadata, and additional tracks without failing the session.
4. THE Stream_Ingest_Service SHALL select the decoder according to the Decoder_Policy:
   - WHILE Decoder_Policy is `auto`, it SHALL use a hardware decoder when the Device_Stream_Capabilities include one for the stream's codec, and a software decoder otherwise.
   - WHILE Decoder_Policy is `hardware`, it SHALL fail the session with `decoder_unavailable` when no hardware decoder exists for the codec.
   - WHILE Decoder_Policy is `software`, it SHALL always use a software decoder.
5. IF a hardware decoder fails to start or fails during a session whose Decoder_Policy is `auto`, THEN THE Stream_Ingest_Service SHALL continue the session with a software decoder and record the fallback in Stream_Health.
6. THE Stream_Ingest_Service SHALL deliver frames as RGB. It SHALL scale them, preserving aspect ratio, so that the longer edge does not exceed the configured maximum frame dimension. It SHALL never upscale.
7. IF the stream's video codec is neither H.264 nor H.265, THEN THE Stream_Ingest_Service SHALL mark the session failed with `unsupported_codec`, naming the codec.

### Requirement 8: Stream session sharing, health, and recovery

**User Story:** As an operator, I want one connection per camera that recovers on its own, so that cameras with session limits keep working and short network outages do not need a technician.

#### Acceptance Criteria

1. THE Stream_Ingest_Service SHALL run at most one Stream_Session per Stream_Camera per Edge_Device, and SHALL serve every Stream_Lease on that camera from it.
2. THE Stream_Ingest_Service SHALL start a Stream_Session when its first Stream_Lease is acquired. It SHALL stop the session no sooner than 30 seconds after the last Stream_Lease is released, and SHALL keep it running if a new lease arrives within that time.
3. THE Stream_Session SHALL expose only its Latest_Frame to consumers. Sequence numbers SHALL increase strictly, and a slow consumer SHALL NOT cause frames to accumulate anywhere in the session.
4. WHEN a streaming session delivers no decoded frame for its stall timeout, THE Stream_Ingest_Service SHALL treat the session as disconnected and reconnect.
5. WHEN a session disconnects or fails to connect for a transient reason, THE Stream_Ingest_Service SHALL retry with exponential backoff, starting at 1 second and capped at 30 seconds, for as long as any Stream_Lease is held. It SHALL reset the backoff after 60 seconds of continuous streaming. Transient reasons are network errors, timeouts, server errors, stalls, and worker exits.
6. WHEN a session fails for a configuration reason, THE Stream_Ingest_Service SHALL retry no more than once every 5 minutes until the camera's configuration changes. Configuration reasons are `authentication_failed`, `not_found`, `unsupported_codec`, `decoder_unavailable`, and `tls_verification_failed`. A `not_found` failure of a session that has streamed since the camera's configuration last changed is not a configuration reason: the path existed, and a relay server or NVR whose publisher dropped answers the same way. It SHALL be retried under the transient-failure backoff of criterion 5, and still be reported as `not_found` (owner decision, 2026-09-29, after hardware verification showed a 90-second publisher outage costing up to 5 minutes).
7. IF a Stream_Worker exits unexpectedly, THEN THE Stream_Ingest_Service SHALL restart it under the transient-failure backoff. The LocalServer backend process and every other Stream_Session SHALL keep running.
8. THE Stream_Ingest_Service SHALL maintain Stream_Health for every session and expose it through the LocalServer API.
9. THE Stream_Ingest_Service SHALL enforce a device-level maximum number of concurrent Stream_Sessions, default 4 and configurable.
10. IF acquiring a Stream_Lease would exceed the maximum number of concurrent Stream_Sessions, THEN THE Stream_Ingest_Service SHALL refuse the lease with a reason naming the limit.
11. WHEN a Stream_Camera's configuration or credentials change, THE Stream_Ingest_Service SHALL restart its session with the new configuration within 10 seconds, preserving the existing Stream_Leases.

### Requirement 9: Packaging and deploy-time binding

**User Story:** As an operator, I want to bind a workflow's stream node to a registered stream camera on each target device at deploy time, so that one workflow can run against each station's own camera.

#### Acceptance Criteria

1. WHEN the Component_Packager packages a workflow containing Stream_Camera_Source_Nodes, THE Component_Packager SHALL treat each as a camera input node:
   - It SHALL emit a `bindingPoints` entry per architecture with `streamBinding: true`, `streamProtocol` (`rtsp` or `rtmp`), empty slots, and the node's rendered parameters.
   - It SHALL record the node in `camera_input_nodes` with `has_binding_points: true`.
2. WHEN a workflow contains no Stream_Camera_Source_Node, THE Component_Packager SHALL produce output byte-identical to its pre-feature output.
3. WHEN a deployment is created, THE Deployment_Service SHALL offer each Stream_Camera_Source_Node the target device's Camera_Sources of the matching type (`RTSP` or `RTMP`), with hint pre-selection. It SHALL reject a binding to any other type with `CAMERA_TYPE_INCOMPATIBLE`.
4. WHERE a user chooses a manual override for a Stream_Camera_Source_Node, THE binding matrix SHALL collect a Stream_URL for the node's `url` parameter. THE Deployment_Service SHALL validate the URL against the node's parameter constraints and the rules of Requirements 2.1 and 2.2.
5. WHEN a bound Camera_Source's last reported Stream_Health state is `failed`, THE Deployment_Service SHALL raise the existing degraded-source warning, which requires confirmation.
6. THE Deployment_Service SHALL deliver stream bindings through the existing `dda-camera-bindings` shadow and leave the packaged artifact unchanged.
7. WHEN a workflow contains a Stream_Camera_Source_Node or a Scene_Analytics_Node, THE Component_Packager and the Deployment_Service SHALL require, on each target architecture, a LocalServer version that supports those node types. THE Deployment_Service SHALL reject deployment to a device running an older LocalServer with a message naming the required version. WHERE an architecture has no LocalServer build verified on hardware for those node types, THE Component_Packager SHALL reject such a workflow for that architecture with `STREAM_CAMERAS_UNSUPPORTED_ARCH`.

### Requirement 10: Device-side binding resolution and triggered frame feed

**User Story:** As an operator, I want a triggered run to analyze the camera's current frame, so that PLC- or MQTT-triggered inspections work with network cameras.

#### Acceptance Criteria

1. WHEN the Workflow_Engine resolves bindings for a document containing stream binding points, THE Workflow_Engine SHALL resolve each `cameraSourceId` binding to a device-local Stream_Camera of the matching type and produce a stream assignment without substituting element arguments. It SHALL constraint-check override values against the vendored catalog.
2. IF a stream binding's `cameraSourceId` has no device-local Stream_Camera of the matching type, THEN THE Workflow_Engine SHALL mark the registration invalid, with a reason naming the missing camera.
3. WHILE a registration bound to a Stream_Camera is registered and valid, THE Workflow_Engine SHALL hold a Stream_Lease on that camera. It SHALL release the lease when the registration is removed, superseded, or becomes invalid.
4. WHEN an `on_trigger` run starts, THE Workflow_Executor SHALL:
   - Take the resolved camera's Latest_Frame, waiting up to `max_frame_age_ms` for a frame that is no older than `max_frame_age_ms`.
   - Push that frame into the node's appsrc through the Frame_Feed.
   - Record the frame's sequence number, acquisition time, and dimensions in the run metadata.
5. IF no frame younger than `max_frame_age_ms` becomes available, THEN THE Workflow_Executor SHALL fail the run with the failing node set to the Stream_Camera_Source_Node and an error naming the camera and its Stream_Health state.
6. WHEN a stream node is unbound or bound by override, THE Workflow_Executor SHALL use the device-local Stream_Camera whose normalized Stream_URL equals the node's `url`, when one exists. Otherwise it SHALL use a credential-less session to that URL.
7. WHEN a document contains no stream binding point, THE Workflow_Executor SHALL run it exactly as before this feature.

### Requirement 11: Continuous processing mode

**User Story:** As a safety engineer, I want a workflow to watch a camera continuously at a rate I choose, so that PPE violations and scene counts are detected without an external trigger.

#### Acceptance Criteria

1. WHEN a valid registration's Stream_Camera_Source_Node is in `continuous` mode, THE Continuous_Runner SHALL start runs for it on Sampling_Ticks at `frames_per_second`. It SHALL begin within 10 seconds of the Stream_Session reaching `streaming` and every model the workflow uses being READY.
2. Each continuous run SHALL process the Latest_Frame at its tick. THE Continuous_Runner SHALL process each frame sequence number at most once, and SHALL skip a tick when no newer frame exists.
3. IF a run is still in progress at a Sampling_Tick, THEN THE Continuous_Runner SHALL skip that tick and count it as skipped. Ticks SHALL never queue.
4. THE Continuous_Runner SHALL learn of a run's completion directly from the Workflow_Executor, so that its achievable rate is limited by run duration and not by status polling.
5. WHILE the Stream_Session is not `streaming`, THE Continuous_Runner SHALL start no runs and SHALL record one stream-unavailable event per outage. It SHALL resume at the next tick after streaming resumes.
6. THE LocalServer SHALL let an operator pause and resume a continuous workflow. A pause SHALL persist across backend restarts until the operator resumes it or the registration is superseded.
7. WHEN a manual trigger arrives for a continuous workflow, THE LocalServer SHALL accept it only while that workflow is paused.
8. WHEN a continuous registration is removed or superseded, THE Continuous_Runner SHALL stop starting runs immediately, let an in-progress run finish, and release its Stream_Lease.
9. THE Continuous_Runner SHALL record each run's trigger context as the continuous source, with the frame sequence number, frame acquisition time, and tick time.
10. IF a continuous run fails, THEN THE Continuous_Runner SHALL record that run's failure and continue with the next tick.
11. WHILE a model the workflow uses is not READY in Triton, THE Continuous_Runner SHALL start no runs, SHALL record one model-unavailable event per wait, and SHALL resume at the next tick once every model is READY.
12. THE Continuous_Runner SHALL request a model load only when the model's repository files are complete and have not changed for 10 seconds. It SHALL report a wait longer than 600 seconds as stalled.

### Requirement 12: Continuous run retention, storage, and logging bounds

**User Story:** As a device owner, I want continuous workflows to keep a bounded, useful run history, so that a station never fills its disk, wears out its flash, or floods its logs.

#### Acceptance Criteria

1. For each continuous workflow registration, THE LocalServer SHALL retain the most recent `keep_recent_runs` runs of any kind, plus up to `keep_notable_runs` Notable_Runs. It SHALL delete the records and artifacts of every other continuous run of that registration.
2. THE LocalServer SHALL enforce a device-wide cap on the bytes of retained continuous-run artifacts, default 2 GiB and configurable. WHEN the cap is exceeded, it SHALL delete the oldest Notable_Runs first.
3. THE LocalServer SHALL write continuous runs' artifacts to RAM-backed staging storage, and SHALL persist to the device's capture storage only the runs retained as Notable_Runs. WHERE RAM-backed staging is unavailable, it SHALL use persistent storage under the same retention.
4. THE LocalServer SHALL bound the RAM-backed staging used by continuous runs, default 256 MiB and configurable, evicting the oldest non-notable runs first.
5. THE LocalServer SHALL maintain per-registration continuous counters that survive run deletion, and SHALL expose them through the LocalServer API. The counters are:
   - Runs started, completed, and failed.
   - Ticks skipped.
   - Notable_Runs.
   - Outputs sent.
   - The effective run rate over the last 60 seconds.
6. THE LocalServer SHALL NOT write per-run informational log lines for continuous runs to the component log. THE Continuous_Runner SHALL log state changes and at most one summary line per minute per registration, and SHALL keep per-run detail in each run's own log.
7. THE LocalServer SHALL bound, in both level and size, the GStreamer debug output that Stream_Workers and continuous runs produce.
8. THE LocalServer SHALL NOT delete or alter the run history of any workflow that has no continuous-mode stream node.
9. WHEN the backend starts, THE LocalServer SHALL mark every continuous run that a previous process left `pending` or `running` as failed, with an interrupted error. It SHALL count each such run as failed and SHALL retain it under the limits of 12.1 as a run that is not notable. Runs of every other kind are untouched (12.8).
10. THE LocalServer SHALL bound the disk use of its component logs, whatever their volume. It SHALL cap each container log by size, and SHALL cap the total size of the rotated `application.log` and `service.log` files, in addition to their 14-day age limit. Per-call trace lines from the native inference runtime are not informational for 12.6 purposes, and SHALL be logged below the component log's level.

### Requirement 13: Detection counting with zones

**User Story:** As an operations engineer, I want per-class counts of detected objects, optionally inside a region of the frame, so that I can count items in a scene and act on the counts.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include a `detection_counter` node type with category post_processing, display name "Detection Counter", one InferenceMeta input, and one InferenceMeta output. It SHALL have these parameters:
   - `classes` (string, optional): comma-separated labels to always report.
   - `min_confidence` (float, 0 to 1, default 0).
   - `zone` (string, optional): a Zone as JSON.
   - `zone_rule` (enum `center` | `overlap`, default `center`).
2. WHEN a run reaches a `detection_counter` node, THE LocalServer SHALL count, grouped by Label_Key, the Detection_List entries that have a confidence of at least `min_confidence` and that pass the Zone when one is set. With `center`, an entry passes when its box center lies inside the Zone. With `overlap`, an entry passes when its box intersects the Zone.
3. Before any condition or output downstream of the node is evaluated, THE LocalServer SHALL merge the result into the run metadata as:
   - `counter.<nodeId>.counts.<Label_Key>`, with every label in `classes` present and zero when unseen.
   - `counter.<nodeId>.total`.
   - `counter.<nodeId>.labels`, mapping each Label_Key to its original label.
4. THE Condition_Language and output templates SHALL be able to reference these values through dotted field paths.
5. IF a run has no Detection_List, THEN THE node SHALL report zero counts and record a warning on the node.
6. IF a Zone is set and the frame dimensions are unknown, THEN THE node SHALL record an error outcome that gates its downstream nodes without failing the run.
7. WHEN a node's `zone` or `classes` value is malformed, THE workflow validator SHALL report an error finding on the node. Malformed means invalid JSON, fewer than 3 or more than 32 points, a coordinate outside 0 to 1, or an empty label.
8. WHEN a `detection_counter` or `object_association` node has no `model_inference` node upstream, THE workflow validator SHALL report a warning finding on it.
9. THE LocalServer and the Portal's cloud test sandbox SHALL produce identical counter metadata for identical inputs.

### Requirement 14: Object association for PPE compliance

**User Story:** As a safety engineer, I want to check that every detected person has the required protective equipment, so that I can flag PPE violations with a detector trained on positive classes such as person, hardhat, and vest.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include an `object_association` node type with category post_processing, display name "Object Association", one InferenceMeta input, and one InferenceMeta output. It SHALL have these parameters:
   - `subject_class` (string, required).
   - `required_classes` (string, required): 1 to 10 comma-separated labels.
   - `min_overlap` (float, 0.05 to 1, default 0.5).
   - `min_confidence` (float, 0 to 1, default 0).
   - `zone` (string, optional): a Zone.
2. WHEN a run reaches an `object_association` node, THE LocalServer SHALL take as subjects the detections whose Label_Key equals the Label_Key of `subject_class`, whose confidence is at least `min_confidence`, and whose box center lies inside the Zone when one is set.
3. THE LocalServer SHALL match each required class's detections to subjects one-to-one, preferring larger overlaps. A detection can match a subject only when at least `min_overlap` of its own box area lies inside the subject's box. A subject is compliant when every required class has a match.
4. Before any condition or output downstream of the node is evaluated, THE LocalServer SHALL merge these values into the run metadata:
   - `association.<nodeId>.subjects`.
   - `association.<nodeId>.compliant`.
   - `association.<nodeId>.violations`.
   - `association.<nodeId>.missing.<Label_Key>`: the number of subjects lacking each required class.
   - `association.<nodeId>.violating_ids`: the Detection_List `id` values of the non-compliant subjects.
5. THE Condition_Language and output templates SHALL be able to reference these values through dotted field paths, for example `association.ppe.violations > 0`.
6. THE rules of Requirements 13.5 through 13.9 SHALL apply to `object_association` nodes: no Detection_List, unknown frame dimensions, validation findings, and sandbox parity.

### Requirement 15: Event gating across runs

**User Story:** As a safety engineer, I want an alarm to fire once when a violation persists, not on every frame, so that operators get actionable alerts instead of noise.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include an `event_gate` node type with category post_processing, display name "Event Gate", one InferenceMeta input, and one InferenceMeta output. It SHALL have these parameters:
   - `condition` (string, required, in the Condition_Language).
   - `activate_after` (int, 1 to 1000, default 3).
   - `clear_after` (int, 1 to 1000, default 3).
   - `emit` (enum `on_activate` | `on_change` | `while_active`, default `on_activate`).
   - `repeat_interval_ms` (int, 0 to 86400000, default 0), visible only while `emit` is `while_active`.
2. THE LocalServer SHALL keep Event_Gate_State per registration and per node. The gate SHALL become active after `activate_after` consecutive runs whose condition is true, and inactive after `clear_after` consecutive runs whose condition is false. A condition that cannot be evaluated SHALL count as false and be recorded on the node.
3. THE LocalServer SHALL let a run through to the gate's downstream nodes only when the run satisfies the configured `emit` value:
   - `on_activate`: the gate activates on that run.
   - `on_change`: the gate activates or clears on that run.
   - `while_active`: the gate is active on that run. When `repeat_interval_ms` is greater than 0, this passes at most once per `repeat_interval_ms`.
4. THE LocalServer SHALL merge the following into the run metadata:
   - `event.<nodeId>.state`.
   - `event.<nodeId>.transition`: `activated`, `cleared`, or `none`.
   - `event.<nodeId>.active_since`.
   - The consecutive true and false counts.
5. THE LocalServer SHALL reset Event_Gate_State to inactive when the registration changes or the backend restarts.
6. THE `event_gate` node SHALL work in both processing modes and in the cloud test sandbox, where each test run starts from the inactive state.

### Requirement 16: Observability

**User Story:** As an operator, I want to see stream health and continuous-processing status at the station and in the Portal, so that I can tell at a glance whether a camera and its workflow are working.

#### Acceptance Criteria

1. THE LocalServer UI SHALL show each stream camera's Stream_Health. WHILE a camera is not streaming, its live preview SHALL show the session state.
2. THE LocalServer UI SHALL show the following for each continuous workflow registration:
   - Its state: `running`, `paused`, `waiting_for_stream`, or `waiting_for_model`.
   - Its configured and effective run rates.
   - The counters of Requirement 12.5.
   - Its recent runs and recent Notable_Runs.
   - Pause and resume controls.
3. THE LocalServer run results view SHALL render detection counter, object association, and event gate outputs, and SHALL highlight violating detections in the detections table.
4. THE Portal SHALL show each stream camera's reported codec, resolution, decoder in use, and coarse Stream_Health state in the Cameras tab and the Camera_Picker.
5. THE LocalServer SHALL report Device_Stream_Capabilities through the camera registry sync, and THE Portal SHALL show them on the device's page.

### Requirement 17: Platform support, packaging, and hardware verification

**User Story:** As a release owner, I want stream cameras to work on every LocalServer target and be proven on real hardware, so that the feature ships without surprises in the field.

#### Acceptance Criteria

1. THE LocalServer SHALL support stream cameras on the `arm64_jp5`, `arm64_jp6`, `arm64_jp7`, `x86_64`, `x86_64_nvidia`, and `arm64_cpu` targets. It SHALL use hardware decoding where the target exposes it to the backend container, and software decoding otherwise.
2. Each LocalServer image SHALL contain every component its target needs for RTSP and RTMP with H.264 and H.265, including H.265 over Enhanced RTMP.
3. IF a component required by Requirement 17.2 is missing, THEN THE image build SHALL fail.
4. THE LocalServer SHALL probe the Device_Stream_Capabilities at startup and log them once.
5. WHEN this feature changes a file pinned by the security preservation gate, THE same change SHALL update the corresponding baseline.
6. THE feature SHALL be verified on real JetPack 5, JetPack 6, and JetPack 7 devices before its on-device changes are committed. Each device SHALL be tested with RTSP H.264, RTSP H.265, RTMP H.264, and RTMP H.265 sources, covering:
   - Continuous mode for at least 2 hours with a detection model.
   - Triggered runs.
   - Reconnection after a source outage.
   - The backend staying healthy throughout: no crash, no restart, and no unbounded memory growth.
7. THE bundled media components SHALL pass a third-party license review before release.

### Requirement 18: Backward compatibility

**User Story:** As an operator, I want existing workflows, cameras, and deployments to behave exactly as before, so that adopting stream cameras disrupts nothing in production.

#### Acceptance Criteria

1. WHEN a workflow definition, compiled document, or deployment contains none of the new node types, THE Portal and LocalServer SHALL validate, compile, package, bind, register, and execute it with the same behavior as before this feature.
2. THE LocalServer SHALL create, preview, capture from, and report in inventory the Image_Sources of existing types exactly as before.
3. THE Camera_Registry SHALL handle Camera_Sources of every other type exactly as before. THE Cameras tab SHALL add the `RTMP` type option and keep every existing option's label, value, and order.
4. THE trigger runtime, manual trigger endpoint, output bindings, and run history SHALL behave as before for every workflow without a continuous-mode stream node.
5. THE StreamBroadcaster SHALL behave exactly as before for `Camera`, `NvidiaCSI`, and `ICam` sources.
