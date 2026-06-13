# Requirements Document

## Introduction

Today the DefectDetectionApplication backend can serve a live camera preview to only one viewer at a time. Every preview frame grab in `camera_manager.py` runs through a global `get_frame_lock` and a per-request `start_acquisition()` / `get_frame()` / `stop_acquisition()` cycle. When two browser tabs or clients poll the same camera, they contend on this single lock, so effectively only one page can view the stream while the others stall or fail.

This feature introduces a shared, broadcast-style frame source: a single acquisition loop per physical camera that fans out the most recent frame to every connected viewer. Viewers subscribe to a stream rather than each claiming the device. The shared acquisition starts when the first viewer connects and stops when the last viewer disconnects. The feature must cover concurrent viewing, frame fan-out, stream lifecycle, performance bounds, and correct interaction with the existing edit-settings / preview flow and the inference / capture pipeline, across both GenICam/USB3Vision (Aravis) cameras and NVIDIA CSI (GStreamer) cameras.

This document defines what the system must do. Implementation choices (threading vs. multiprocessing, queue vs. shared memory, transport protocol) are deferred to design.

## Glossary

- **Camera_Manager**: The backend module responsible for opening, configuring, and reading frames from physical cameras (currently `src/backend/utils/camera_manager.py`).
- **Physical_Camera**: A single hardware camera device addressed by a camera identifier, accessed via Aravis (GenICam/USB3Vision) or a GStreamer pipeline (NVIDIA CSI / ICAM).
- **Stream_Broadcaster**: The component that owns exactly one acquisition session per Physical_Camera and distributes the most recent frame to all subscribed Viewers.
- **Viewer**: A single client subscription to a camera stream, corresponding to one browser tab or client polling/receiving preview frames. A Physical_Camera may have multiple concurrent Viewers.
- **Latest_Frame**: The most recently acquired frame held by the Stream_Broadcaster for a given Physical_Camera, available for fan-out to Viewers.
- **Stream_Session**: The lifecycle of a Stream_Broadcaster's acquisition for one Physical_Camera, from the first Viewer subscribing to the last Viewer unsubscribing.
- **Viewer_Heartbeat**: A periodic signal (poll request or keep-alive) from a Viewer that indicates the Viewer is still active. Viewers are expected to send a Viewer_Heartbeat at intervals of no more than 10 seconds.
- **Edit_Settings_Preview**: The existing flow where a user adjusts camera controls (gain, exposure, advanced GenICam features) and previews the result, supplying a per-request image-source configuration override.
- **Inference_Pipeline**: The capture and inference paths that grab full frames for workflows (`digital_input_process_manager`, `digital_input_thread_manager`, `workflow`, capture endpoints) via the Camera_Manager.
- **Device_Claim**: An open, acquiring handle on a Physical_Camera. A USB3Vision/GenICam device that is already claimed returns a busy error to a second open attempt.
- **Stale_Viewer**: A registered Viewer from which no Viewer_Heartbeat has been received within 30 seconds of its last-active timestamp (three consecutive missed heartbeats at the 10-second expected interval).

## Requirements

### Requirement 1: Concurrent Viewing of a Single Camera

**User Story:** As an operator, I want to open the same camera's live preview in multiple browser tabs or on multiple clients at once, so that several people can watch the same camera without one viewer blocking the others.

#### Acceptance Criteria

1. WHEN two or more Viewers are subscribed to the same Physical_Camera, THE Stream_Broadcaster SHALL deliver each captured live preview frame to every subscribed Viewer, such that each Viewer receives frames at a rate no lower than 90 percent of the Physical_Camera's configured capture frame rate.
2. WHILE one or more Viewers are subscribed to the same Physical_Camera, THE Camera_Manager SHALL hold exactly one Device_Claim for that Physical_Camera, and SHALL hold zero Device_Claims when no Viewers are subscribed.
3. THE Stream_Broadcaster SHALL support a minimum of 8 concurrent Viewers per Physical_Camera while meeting the frame delivery rate defined in criterion 1.
4. WHEN an additional Viewer subscribes to a Physical_Camera that already has between 1 and 7 active Viewers, THE Stream_Broadcaster SHALL begin delivering frames to the new Viewer within 2000 milliseconds, while continuing to deliver frames to each existing Viewer at the rate defined in criterion 1 with no dropped frames attributable to the new subscription.
5. WHILE multiple Viewers are subscribed to the same Physical_Camera, THE Stream_Broadcaster SHALL serve each Viewer's frame requests independently, such that the time for any Viewer to receive a requested frame does not depend on, and is not blocked by, the pending or in-progress frame request of any other Viewer.
6. IF a Viewer attempts to subscribe to a Physical_Camera that already has 8 active Viewers, THEN THE Stream_Broadcaster SHALL reject the subscription, return a response to the requesting Viewer indicating the per-camera Viewer limit has been reached, and continue delivering frames to the 8 existing Viewers without interruption.
7. IF the Camera_Manager cannot establish or maintain the Device_Claim for a Physical_Camera when a Viewer subscribes, THEN THE Stream_Broadcaster SHALL reject the subscription and return a response to the requesting Viewer indicating the Physical_Camera is unavailable, without affecting frame delivery to any other Physical_Camera.

### Requirement 2: Shared Frame Acquisition and Fan-Out

**User Story:** As a developer, I want a single acquisition loop per camera that shares the most recent frame with all viewers, so that the device is read once and the result is reused instead of being re-grabbed per request.

#### Acceptance Criteria

1. WHILE one or more Viewers are subscribed to a Physical_Camera, THE Stream_Broadcaster SHALL maintain exactly one active acquisition session that reads frames from that Physical_Camera, regardless of the number of subscribed Viewers.
2. WHEN the Stream_Broadcaster acquires a new frame from a Physical_Camera, THE Stream_Broadcaster SHALL replace the Latest_Frame for that Physical_Camera with the newly acquired frame.
3. WHEN a Viewer requests the current preview frame, THE Stream_Broadcaster SHALL return the Latest_Frame for that Physical_Camera within 100 milliseconds of receiving the request.
4. WHERE no new frame has been acquired since a Viewer's previous request, THE Stream_Broadcaster SHALL return the most recent Latest_Frame for that Physical_Camera to that Viewer without re-grabbing from the Physical_Camera.
5. WHEN two or more Viewers request the current preview frame for the same Physical_Camera within the same acquisition interval, THE Stream_Broadcaster SHALL return the identical Latest_Frame to each requesting Viewer.
6. IF no Latest_Frame is available for a Physical_Camera when a Viewer requests the current preview frame, THEN THE Stream_Broadcaster SHALL return a response to the Viewer indicating that no frame is currently available, while retaining the active acquisition session.
7. IF frame acquisition from a Physical_Camera fails, THEN THE Stream_Broadcaster SHALL retain the existing Latest_Frame unchanged and return an indication of the acquisition failure to subscribed Viewers.
8. THE Stream_Broadcaster SHALL provide the same frame-sharing behavior defined in criteria 1 through 7 for GenICam/USB3Vision (Aravis) cameras and for NVIDIA CSI (GStreamer) cameras.

### Requirement 3: Stream Session Lifecycle

**User Story:** As an operator, I want the camera stream to start automatically when the first viewer opens it and stop when the last viewer leaves, so that the device is only acquiring while someone is actually watching.

#### Acceptance Criteria

1. WHEN the first Viewer subscribes to a Physical_Camera that has no active Stream_Session, THE Stream_Broadcaster SHALL start a Stream_Session for that Physical_Camera within 5 seconds.
2. IF the Device_Claim for a Physical_Camera cannot be acquired when starting a Stream_Session, THEN THE Stream_Broadcaster SHALL not start the Stream_Session and SHALL return an error indication to the subscribing Viewer identifying the Physical_Camera.
3. WHEN the last active Viewer unsubscribes from a Physical_Camera, THE Stream_Broadcaster SHALL stop the Stream_Session for that Physical_Camera within 5 seconds.
4. WHEN the Stream_Broadcaster stops a Stream_Session, THE Stream_Broadcaster SHALL release the Device_Claim for that Physical_Camera.
5. IF stopping a Stream_Session fails, THEN THE Stream_Broadcaster SHALL release the Device_Claim for that Physical_Camera, mark the Stream_Session as stopped, and return an error indication identifying the failure.
6. WHEN a Viewer explicitly unsubscribes from a Physical_Camera, THE Stream_Broadcaster SHALL stop delivering frames to that Viewer within 1 second.
7. WHEN a Stream_Session stops, THE Stream_Broadcaster SHALL stop delivering frames to all Viewers that were subscribed to that Stream_Session within 1 second.
8. IF a Stale_Viewer is detected, THEN THE Stream_Broadcaster SHALL treat the Stale_Viewer as unsubscribed.
9. THE Stream_Broadcaster SHALL detect a Stale_Viewer when no Viewer_Heartbeat has been received from that Viewer within 30 seconds of its last-active timestamp.
10. WHEN a new Stream_Session starts for a Physical_Camera, THE Stream_Broadcaster SHALL make the first Latest_Frame available to Viewers within 10 seconds.
11. IF no frame is acquired within 10 seconds of a new Stream_Session starting, THEN THE Stream_Broadcaster SHALL report a stream error to the subscribed Viewers identifying the Physical_Camera.

### Requirement 4: Performance and Freshness

**User Story:** As an operator, I want the shared live preview to stay responsive and current, so that multiple viewers see a near-real-time view rather than stale or stuttering images.

#### Acceptance Criteria

1. WHEN a Viewer requests the current preview frame while a Latest_Frame is available, THE Stream_Broadcaster SHALL return that frame within 500 milliseconds of receiving the request.
2. IF a Viewer requests the current preview frame while no Latest_Frame is available, THEN THE Stream_Broadcaster SHALL return a response indicating no frame is currently available within 500 milliseconds of receiving the request.
3. WHILE the Physical_Camera is delivering frames at a rate of 5 frames per second or greater, THE Stream_Broadcaster SHALL refresh the Latest_Frame at a rate of at least 5 frames per second.
4. WHILE the Physical_Camera is delivering frames at a rate below 5 frames per second, THE Stream_Broadcaster SHALL refresh the Latest_Frame at the rate the Physical_Camera is delivering frames.
5. WHILE the number of Viewers subscribed to a Physical_Camera is between 1 and the supported Viewer count defined in Requirement 1, THE Stream_Broadcaster SHALL maintain the Latest_Frame refresh rate specified in criteria 3 and 4 for that Physical_Camera independent of the number of subscribed Viewers.
6. WHEN serving the Latest_Frame to a Viewer, THE Stream_Broadcaster SHALL return a frame that was acquired no more than 2 seconds before the time the request was received.
7. IF the most recently acquired Latest_Frame was acquired more than 2 seconds before the request was received, THEN THE Stream_Broadcaster SHALL return a response indicating the frame is stale within 500 milliseconds of receiving the request.

### Requirement 5: Interaction with Edit-Settings Preview

**User Story:** As an operator adjusting camera settings, I want my gain, exposure, and advanced feature changes to apply to the live shared preview, so that I can verify settings while others may also be viewing the same camera.

#### Acceptance Criteria

1. WHEN a user applies camera control values (gain, exposure, or advanced GenICam features) through the Edit_Settings_Preview flow, THE Camera_Manager SHALL apply those values to the active Stream_Session for that Physical_Camera within 2 seconds of the apply request.
2. WHEN camera control values are applied to an active Stream_Session, THE Stream_Broadcaster SHALL reflect the applied values in the Latest_Frame delivered to all subscribed Viewers within 5 frames following completion of the apply operation.
3. WHILE camera control values are being applied to a Physical_Camera, THE Stream_Broadcaster SHALL continue delivering the most recent available frame to subscribed Viewers without terminating the Stream_Session and without dropping any subscribed Viewer.
4. WHEN a user requests an Edit_Settings_Preview with a per-request configuration override, THE Camera_Manager SHALL return a single preview frame that reflects the override configuration without modifying the camera control values applied to the active Stream_Session or the Latest_Frame delivered to other subscribed Viewers.
5. IF applying a camera control value fails, THEN THE Camera_Manager SHALL return a descriptive error identifying the failed control, SHALL retain the camera control values that were in effect before the failed apply request, and SHALL keep the Stream_Session active.

### Requirement 6: Interaction with the Inference and Capture Pipeline

**User Story:** As an operator running inference, I want capture and workflow frame grabs to keep working while people are viewing the live preview, so that monitoring and inspection can happen at the same time without conflicting over the device.

#### Acceptance Criteria

1. WHEN the Inference_Pipeline requests a frame from a Physical_Camera that has an active Stream_Session, THE Camera_Manager SHALL return the frame using the existing Device_Claim within 500 milliseconds and SHALL NOT open a second Device_Claim for that Physical_Camera.
2. WHILE the Inference_Pipeline is acquiring a frame from a Physical_Camera that has an active Stream_Session, THE Stream_Broadcaster SHALL continue delivering preview frames to each subscribed Viewer at a rate no lower than 90 percent of the Stream_Session configured frame rate, with no single inter-frame gap exceeding 500 milliseconds.
3. WHEN the Inference_Pipeline requests a frame from a Physical_Camera that has no active Stream_Session, THE Camera_Manager SHALL open a dedicated Device_Claim, acquire the frame, and return it within 2000 milliseconds.
4. WHEN the Inference_Pipeline applies a per-capture image-source configuration whose parameter values are within their accepted ranges, THE Camera_Manager SHALL return a frame that reflects that configuration.
5. IF the Inference_Pipeline applies a per-capture image-source configuration containing one or more parameter values outside their accepted ranges, THEN THE Camera_Manager SHALL reject the request, return an error indication identifying the invalid parameter, and preserve the previously active image-source configuration unchanged.
6. IF the Camera_Manager cannot return a requested frame within the applicable time bound or the Physical_Camera becomes unavailable, THEN THE Camera_Manager SHALL return an error indication to the Inference_Pipeline identifying the failure and SHALL leave any active Stream_Session and its Device_Claim intact.

### Requirement 7: Error Handling and Camera Disconnection

**User Story:** As an operator, I want clear feedback when a shared camera stream fails, so that all viewers understand the camera is unavailable instead of seeing a frozen image indefinitely.

#### Acceptance Criteria

1. IF the Stream_Broadcaster fails to acquire a frame within the configured frame timeout (configurable from 500 to 30,000 milliseconds, default 5,000 milliseconds), THEN THE Camera_Manager SHALL mark the Physical_Camera as disconnected within 1,000 milliseconds of the timeout expiring.
2. WHEN the Camera_Manager marks a Physical_Camera as disconnected, THE Stream_Broadcaster SHALL report a stream error identifying the Physical_Camera to every subscribed Viewer within 1,000 milliseconds, where each error indicates the camera is disconnected.
3. WHEN a Physical_Camera becomes disconnected during a Stream_Session, THE Stream_Broadcaster SHALL stop the Stream_Session within 2,000 milliseconds.
4. WHEN the Stream_Broadcaster stops a Stream_Session due to disconnection, THE Stream_Broadcaster SHALL release the Device_Claim before reporting completion.
5. IF a Viewer requests a frame while the Physical_Camera is disconnected, THEN THE Stream_Broadcaster SHALL return a stream error indicating the camera is disconnected and SHALL NOT return any previously acquired frame.
6. WHEN a Viewer subscribes to a Physical_Camera that cannot be opened within the configured open timeout (configurable from 500 to 30,000 milliseconds, default 5,000 milliseconds) after a maximum of 3 open attempts, THE Stream_Broadcaster SHALL return an error identifying the Physical_Camera and SHALL NOT start a Stream_Session.
7. IF the last Viewer unsubscribes while a frame acquisition is in progress, THEN THE Stream_Broadcaster SHALL complete the release of the Device_Claim before allowing a new Stream_Session for that Physical_Camera to start.

### Requirement 8: Viewer Subscription Management

**User Story:** As a developer, I want viewers to be tracked explicitly so the system knows when to start and stop streams, so that abandoned tabs do not keep the device claimed forever.

#### Acceptance Criteria

1. WHEN a Viewer subscribes to a Physical_Camera, THE Stream_Broadcaster SHALL register the Viewer as an active Viewer for that Physical_Camera within 1 second of receiving the subscription request.
2. WHILE a Viewer is registered as active, THE Stream_Broadcaster SHALL accept Viewer_Heartbeats from that Viewer that are received at intervals of no more than 10 seconds to keep the subscription active.
3. WHEN a Viewer_Heartbeat is received from a registered Viewer, THE Stream_Broadcaster SHALL update the last-active timestamp for that Viewer to the receipt time.
4. WHEN the current count of active Viewers for a Physical_Camera is requested, THE Stream_Broadcaster SHALL return a non-negative integer count within 1 second of the request.
5. IF a Viewer subscribes more than once to the same Physical_Camera, THEN THE Stream_Broadcaster SHALL track each subscription as a distinct active Viewer, counting each toward the active Viewer count.
6. IF no Viewer_Heartbeat is received from a registered Viewer within 30 seconds of that Viewer's last-active timestamp, THEN THE Stream_Broadcaster SHALL deregister that Viewer from the Physical_Camera and decrement the active Viewer count for that Physical_Camera.
7. WHEN the count of active Viewers for a Physical_Camera reaches 0, THE Stream_Broadcaster SHALL stop the Stream_Session and release the Device_Claim on that Physical_Camera within 5 seconds.
8. WHEN a Viewer explicitly unsubscribes from a Physical_Camera, THE Stream_Broadcaster SHALL deregister that Viewer and decrement the active Viewer count for that Physical_Camera within 1 second.
