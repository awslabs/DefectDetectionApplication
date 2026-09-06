# Requirements Document

## Introduction

This feature adds a static-image virtual camera to the DDA LocalServer. A user pins a still image, and the LocalServer exposes it as an Aravis (GenICam) camera: it enumerates alongside physical cameras and serves the pinned image as frames on every grab, similar to Aravis' built-in Fake camera but with user-provided content instead of a synthetic test pattern.

The purpose is workflow/pipeline testing without camera hardware: in cloud (x86, no Jetson, no cameras attached) and on edge devices where physically staging a scene the user already has an image of is impractical.

The virtual camera must flow through the existing camera plumbing without special-casing: camera enumeration (`GET /cameras`, rescan), the camera manager grab path (`get_camera_frame`), Image_Source configuration, live preview, capture, and workflow `aravis_camera_source` nodes fed by the executor's Frame_Feed.

## Glossary

- **LocalServer**: The DDA edge application backend (Flask/FastAPI service under `src/backend`) that manages cameras, image sources, previews, captures, and workflow execution.
- **Static_Image_Camera**: The virtual Aravis-compatible camera introduced by this feature. It enumerates like a GenICam camera and serves the Pinned_Image as frames.
- **Pinned_Image**: The user-provided still image currently assigned to the Static_Image_Camera.
- **Pin_API**: The LocalServer interface through which a user sets, inspects, replaces, and removes the Pinned_Image.
- **Camera_Enumeration**: The LocalServer facility that lists cameras on the Aravis bus (`aravis_functions.getCameras`/`rescan_cameras`, surfaced via `GET /cameras` and `POST /cameras/rescan`, and consumed by Camera_Discovery).
- **Camera_Manager**: The LocalServer component (`utils/camera_manager`) that opens cameras and performs per-request acquisition (`get_camera_frame`: start acquisition, grab, stop).
- **Workflow_Executor**: The LocalServer component that runs compiled workflow pipelines, feeding camera-manager-grabbed frames into an appsrc-headed GStreamer chain (the Frame_Feed model planned by `plan_aravis_feeds`).
- **Image_Source**: A configured input source record (camera identity plus ImageSourceConfiguration: gain, exposure, processing pipeline, advanced settings) that workflows reference.
- **Cloud_Environment**: A LocalServer deployment on x86 without Jetson hardware or physical cameras (e.g. for cloud-side workflow testing).
- **Edge_Device**: A LocalServer deployment on a Jetson device (JP5/JP6/JP7 arm64) where physical cameras may also be connected.
- **Supported_Image_Format**: An image file format the LocalServer accepts for pinning: JPEG, PNG, or BMP.

## Requirements

### Requirement 1: Pin a static image

**User Story:** As a workflow developer, I want to pin a still image I already have, so that I can exercise workflows and pipelines without camera hardware or physically staging a scene.

#### Acceptance Criteria

1. WHEN a user submits an image file in a Supported_Image_Format through the Pin_API, THE LocalServer SHALL decode the file and store the result as the Pinned_Image, replacing any previously existing Pinned_Image.
2. WHILE a Pinned_Image exists, THE LocalServer SHALL list the Static_Image_Camera as a selectable camera source in camera enumeration.
3. IF a submitted file cannot be decoded as an image in a Supported_Image_Format, THEN THE LocalServer SHALL reject the pin request with an error message indicating the file could not be decoded and enumerating the Supported_Image_Formats (JPEG, PNG, BMP), and SHALL leave any existing Pinned_Image and the Static_Image_Camera availability unchanged.
4. IF a submitted image file exceeds 50 MB, THEN THE LocalServer SHALL reject the pin request with an error message indicating the maximum accepted file size and SHALL leave any existing Pinned_Image unchanged.
5. WHEN a pin request completes successfully, THE Pin_API SHALL return the Static_Image_Camera identifier and the Pinned_Image metadata (width in pixels, height in pixels, image format, and original file name).
6. WHEN a user queries pin status through the Pin_API, THE Pin_API SHALL report whether a Pinned_Image exists and, when one exists, SHALL include its metadata (width in pixels, height in pixels, image format, and original file name).
7. WHERE the deployment holds previously captured images on the device, THE Pin_API SHALL accept a reference to an existing captured image as the Pinned_Image, applying the same decode validation and size limit as an uploaded file.
8. IF a pin request references a captured image that does not exist on the device, THEN THE LocalServer SHALL reject the request with an error message indicating the referenced image was not found and SHALL leave any existing Pinned_Image unchanged.

### Requirement 2: Enumerate as an Aravis GenICam camera

**User Story:** As a workflow developer, I want the static image camera to show up in the camera list like any GenICam camera, so that the existing camera selection UI and flows work without special-casing.

#### Acceptance Criteria

1. WHILE a Pinned_Image exists, THE Camera_Enumeration SHALL include exactly one Static_Image_Camera entry in every enumeration result (standard enumeration and forced rescan alike), with each of the same identity fields as physical Aravis cameras (id, model, address, physical id, protocol, serial, vendor) populated with a non-empty value.
2. WHILE no Pinned_Image exists, THE Camera_Enumeration SHALL exclude the Static_Image_Camera from every enumeration result (standard enumeration and forced rescan alike).
3. WHEN a Pinned_Image is created, THE Camera_Enumeration SHALL include the Static_Image_Camera in the first enumeration result requested after the creation completes (whether a standard enumeration or a forced rescan), without requiring a LocalServer restart.
4. THE LocalServer SHALL assign the Static_Image_Camera a single fixed camera identifier whose string value is character-for-character identical across pin, replace, and LocalServer restart operations, and that does not equal the identifier of any physical camera in the same enumeration result, so that Image_Source records and workflow camera bindings referencing the identifier stay valid.
5. WHILE a Pinned_Image exists, THE Camera_Enumeration SHALL continue to include every physical camera discovered on the bus, with each physical camera's identity fields unchanged relative to an enumeration performed with no Pinned_Image present.
6. WHEN a Pinned_Image is removed, THE Camera_Enumeration SHALL exclude the Static_Image_Camera from the first enumeration result requested after the removal completes (whether a standard enumeration or a forced rescan).
7. IF the Static_Image_Camera entry cannot be constructed during an enumeration while a Pinned_Image exists, THEN THE Camera_Enumeration SHALL return the enumeration result containing all discovered physical cameras rather than failing the enumeration request.

### Requirement 3: Serve the pinned image as frames

**User Story:** As a workflow developer, I want every frame grabbed from the static image camera to be my pinned image, so that pipelines process a known, reproducible input.

#### Acceptance Criteria

1. WHEN the Camera_Manager grabs a frame from the Static_Image_Camera, THE Camera_Manager SHALL return a frame whose pixel content, interpreted using the frame's tagged pixel format, width, and height, reproduces the decoded Pinned_Image pixel for pixel.
2. WHEN the Camera_Manager returns a frame from the Static_Image_Camera, THE Camera_Manager SHALL set the frame width and height to the pixel dimensions of the decoded Pinned_Image.
3. WHEN the Camera_Manager grabs frames repeatedly while the same Pinned_Image exists, THE Camera_Manager SHALL return byte-for-byte identical frame data with identical width, height, and pixel format tag on every grab.
4. WHEN acquisition settings (gain, exposure, or advanced GenICam settings) are supplied for a grab from the Static_Image_Camera, THE Camera_Manager SHALL complete the grab without error and SHALL return the same frame content as a grab performed without those settings.
5. IF a grab targets the Static_Image_Camera identifier while no Pinned_Image exists, or while the stored Pinned_Image cannot be read or decoded, THEN THE Camera_Manager SHALL fail the grab with an error that identifies the Static_Image_Camera and indicates that no usable Pinned_Image is available.
6. WHEN the Camera_Manager performs its per-request acquisition cycle (start acquisition, grab, stop acquisition) against the Static_Image_Camera, THE Camera_Manager SHALL complete each step of the cycle without error.
7. WHEN the Camera_Manager returns a frame from the Static_Image_Camera, THE Camera_Manager SHALL tag the frame with a pixel format whose bytes-per-pixel, multiplied by the frame width and height, equals the byte length of the frame data.
8. WHEN a grab from the Static_Image_Camera begins, THE Camera_Manager SHALL return the frame or fail with an error within 10 seconds.

### Requirement 4: Work through existing camera consumers without special-casing

**User Story:** As a workflow developer, I want to use the static image camera everywhere a real camera is used — image source configuration, preview, capture, and workflow camera nodes — so that my test workflows are identical to production workflows except for the camera identifier.

#### Acceptance Criteria

1. WHEN a user configures an Image_Source with the Static_Image_Camera identifier, THE LocalServer SHALL accept the configuration through the same interface and with the same ImageSourceConfiguration fields (gain, exposure, processing pipeline, advanced settings) as physical cameras, persisting an Image_Source record that subsequent Image_Source queries return.
2. WHEN a workflow whose `aravis_camera_source` node resolves to the Static_Image_Camera identifier runs, THE Workflow_Executor SHALL feed a frame whose pixel content renders the Pinned_Image and whose dimensions equal the Pinned_Image dimensions into the compiled pipeline through the existing Frame_Feed path, and the run SHALL proceed to completion with downstream nodes receiving that frame.
3. WHEN a live preview is requested for the Static_Image_Camera, THE LocalServer SHALL serve preview frames through the same preview interface used for physical cameras, with every served frame rendering the Pinned_Image content.
4. WHEN an image capture is requested from an Image_Source backed by the Static_Image_Camera, THE LocalServer SHALL store a captured image rendering the Pinned_Image through the same capture path used for physical cameras, and the stored capture SHALL be retrievable through the same interfaces that list and serve captures from physical cameras.
5. WHEN a workflow document authored for a physical Aravis camera has its camera binding resolved to the Static_Image_Camera identifier, THE Workflow_Executor SHALL plan and execute the run through the same camera binding and feed planning path used for physical Aravis camera identifiers, with no modification to the workflow document and no addition to the node catalog.
6. IF a live preview or an image capture is requested for the Static_Image_Camera while no Pinned_Image exists, THEN THE LocalServer SHALL fail the request with a descriptive error naming the Static_Image_Camera and SHALL store no captured image.

### Requirement 5: Replace and unpin lifecycle

**User Story:** As a workflow developer, I want to swap the pinned image or remove it entirely, so that I can iterate through test scenarios and return the system to its normal state.

#### Acceptance Criteria

1. WHEN a user pins a new image through the Pin_API while a Pinned_Image already exists, THE LocalServer SHALL replace the Pinned_Image with the new image as a single atomic update, such that no grab returns a frame combining prior and new image content, and SHALL return a Pin_API success confirmation when the replacement is complete.
2. WHEN a grab from the Static_Image_Camera begins after the Pin_API has returned the success confirmation for a replacement, THE Static_Image_Camera SHALL return frames containing only the new image content.
3. IF a Pinned_Image replacement operation fails, THEN THE LocalServer SHALL return a Pin_API error response indicating the replacement failure, retain the prior Pinned_Image unchanged, and continue serving the prior Pinned_Image content for grabs that begin after the error response.
4. WHEN a user removes the Pinned_Image through the Pin_API, THE LocalServer SHALL delete the stored image, return a Pin_API success confirmation, and exclude the Static_Image_Camera from every Camera_Enumeration result produced after that confirmation.
5. IF a removal request is received through the Pin_API when no Pinned_Image exists, THEN THE LocalServer SHALL return an error response indicating that no image is pinned and SHALL make no change to the stored Pinned_Image state or to Camera_Enumeration results.
6. IF a workflow run references the Static_Image_Camera after the Pinned_Image removal has been confirmed through the Pin_API, THEN THE Workflow_Executor SHALL fail that workflow run with an error naming the Static_Image_Camera and SHALL leave the execution and status of all other workflow runs unchanged.
7. WHEN a pin, replace, or unpin operation executes, THE LocalServer SHALL keep every physical camera connection open with unchanged configuration and SHALL allow every in-progress physical camera acquisition to continue to completion.

### Requirement 6: Persistence across restarts

**User Story:** As a workflow developer, I want my pinned image to survive a LocalServer restart, so that long-running test setups do not silently lose their input source.

#### Acceptance Criteria

1. WHILE a Pinned_Image is stored, WHEN the LocalServer restarts, THE LocalServer SHALL restore the Pinned_Image and the Static_Image_Camera without user action, completing the restore before serving the first Pin_API, Camera_Enumeration, or frame grab request after startup.
2. WHEN a user queries the Pin_API after a restart that restored the Pinned_Image, THE Pin_API SHALL report the Pinned_Image with the same metadata (width, height, format, and original file name) it reported before the restart.
3. WHEN the Camera_Manager grabs a frame from the Static_Image_Camera after a restart that restored the Pinned_Image, THE Camera_Manager SHALL return image content identical to the content it returned for the same Pinned_Image before the restart.
4. IF the stored Pinned_Image cannot be restored at startup, THEN THE LocalServer SHALL log an error identifying the restore failure and its cause (missing data versus undecodable data), complete startup, and report no Pinned_Image through the Pin_API.
5. IF the stored Pinned_Image cannot be restored at startup, THEN THE LocalServer SHALL continue to enumerate physical cameras, serve frame grabs from physical cameras, and accept new pin requests through the Pin_API.

### Requirement 7: Environment support

**User Story:** As a workflow developer, I want the static image camera to work both in cloud test environments and on edge devices, so that the same test approach applies wherever workflows run.

#### Acceptance Criteria

1. WHILE the LocalServer runs in a Cloud_Environment and a Pinned_Image exists, THE Camera_Enumeration SHALL include the Static_Image_Camera in enumeration results and complete the enumeration without error despite zero physical cameras being present.
2. WHILE the LocalServer runs in a Cloud_Environment, WHEN the Camera_Manager grabs a frame from the Static_Image_Camera, THE Camera_Manager SHALL return a frame meeting the frame content, dimension, and pixel format criteria of Requirement 3 without requiring any physical camera or Jetson hardware.
3. WHILE the LocalServer runs on an Edge_Device and a Pinned_Image exists, THE Camera_Enumeration SHALL include the Static_Image_Camera together with every connected physical camera in the same enumeration results.
4. WHILE the LocalServer runs on an Edge_Device, WHEN the Camera_Manager grabs a frame from the Static_Image_Camera, THE Camera_Manager SHALL return a frame meeting the frame content, dimension, and pixel format criteria of Requirement 3 while leaving physical camera connections and any in-progress physical camera acquisitions undisturbed.
5. THE Static_Image_Camera SHALL present the same camera identifier, the same enumeration identity fields, and identical frame pixel content for the same Pinned_Image on Cloud_Environment deployments and on Edge_Device deployments of every supported JetPack version (JP5, JP6, JP7).

## Open Decisions

Defaults chosen for this initial version — flag any you want changed:

1. **Single pinned image**: One Static_Image_Camera with one Pinned_Image at a time. Multiple simultaneous virtual cameras (for multi-camera workflow testing) are out of scope for now.
2. **Persistence**: The Pinned_Image persists across LocalServer restarts (Requirement 6). If you prefer an ephemeral, per-session pin, Requirement 6 changes.
3. **Image sources for pinning**: Direct file upload is required; selecting an existing on-device captured image is included as an optional feature (Requirement 1.5). Arbitrary device file paths are not exposed.
4. **Acquisition settings**: Gain/exposure/advanced settings are accepted without error but are not required to alter the pinned image content (Requirement 3.4).
