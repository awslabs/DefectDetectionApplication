# Requirements Document

## Introduction

The Static_Image_Camera (spec `static-image-camera-source`, cloud provisioning in `cloud-static-camera-provisioning`) is a virtual camera that serves one pinned still image as every frame. That is enough to exercise a pipeline against a known input, but not to test a workflow against a scene that changes over time the way a live camera does.

This feature adds a second virtual camera, the Static_Video_Camera, with its own pin slot. A user pins a video file to it, and the camera plays that video in a continuous loop in real time. Every frame grab returns the frame that is "on screen" at that moment, and playback wraps back to the first frame after the last one. The two cameras are independent: a device can have an image pinned to the Static_Image_Camera and a video pinned to the Static_Video_Camera at the same time, and each appears as its own camera everywhere a camera can be chosen. The Static_Image_Camera keeps working exactly as it does today.

The Static_Video_Camera reuses the existing camera plumbing:

- camera enumeration and the camera-sync inventory;
- Image_Source configuration, live preview, capture, and digital-input triggers;
- workflow camera binding and the executor's Frame_Feed.

A video is pinned through a device-side Video_Pin_API or through the Portal. The Portal validates the video by decoding it with the same logic the device uses, then delivers it through the existing Sync_Channel.

## Glossary

- **Static_Image_Camera**: The existing virtual camera with the fixed identifier `static-image-camera` (base spec `static-image-camera-source`). Unchanged by this feature.
- **Static_Video_Camera**: The new virtual camera with the fixed identifier `static-video-camera`. It serves the Pinned_Video through Loop_Playback.
- **Pinned_Video**: The video file currently assigned to the Static_Video_Camera. At most one exists per device.
- **Supported_Video_Container**: MP4 (including M4V), MOV (QuickTime), AVI, MKV (Matroska), or WebM.
- **Supported_Video_Codec**: H.264/AVC, H.265/HEVC, MPEG-4 Part 2, Motion JPEG, VP8, or VP9. These codecs decode on every LocalServer_Platform and in the Portal's validator.
- **Video_Validation**: Deciding whether a file is an acceptable Pinned_Video: container recognition, size, frame rate and dimension limits, and decoding of its first and last frames. The same logic runs on the device and in the Portal.
- **Loop_Playback**: Serving a Pinned_Video as a live source: frames advance in real time at the video's native frame rate and wrap to the first frame after the last one.
- **Loop_Epoch**: The instant the Pinned_Video was pinned (its `pinnedAtEpochMs` metadata). Loop position is measured from it.
- **Loop_Position**: The index of the video frame that Loop_Playback serves at a given wall-clock instant.
- **Video_Metadata**: The metadata recorded for the Pinned_Video and reported through the pin APIs and the camera inventory.
- **Video_Pin_API**: The new LocalServer interface `/static-video-camera/pin` for pinning, inspecting, and removing the Pinned_Video on the device.
- **Portal_Video_Pin_API**: The new Portal routes under `/devices/{id}/cameras/static-video…` that stage an upload, validate it, submit a video Pin_Request, and report its Sync_Status.
- **Video_Pin_Request**: A Pin_Request (as defined in `cloud-static-camera-provisioning`) for the Static_Video_Camera's slot.
- **Pin_Request**, **Sync_Status**, **Sync_Channel**, **Image_Transport**, **Portal_Pin_API**: As defined in `cloud-static-camera-provisioning`.
- **LocalServer_Platform**: Any platform the LocalServer ships for: amd64, amd64 with NVIDIA GPU, arm64 CPU, JetPack 5, JetPack 6, JetPack 7.
- **Frame_Consumer**: Any existing consumer of camera frames: Image_Source preview, image capture, digital-input triggers, and workflow runs through the executor's Frame_Feed.

## Requirements

### Requirement 1: Pin a video on the device

**User Story:** As a workflow developer, I want to pin a video clip of a scene to the device's video camera, so that I can test a workflow against changing input without camera hardware.

#### Acceptance Criteria

1. WHEN a user submits a file through the Video_Pin_API that passes Video_Validation, THE LocalServer SHALL store it as the Pinned_Video and replace any existing Pinned_Video.
2. WHEN a video pin request completes successfully, THE Video_Pin_API SHALL return the Static_Video_Camera identifier and Video_Metadata. The Video_Metadata SHALL contain:
   - the container format and the codec;
   - the displayed frame width and height in pixels;
   - the frame rate, the frame count, and the loop duration in milliseconds;
   - the original file name and the file size in bytes.
3. IF a submitted file is not a Supported_Video_Container, THEN THE LocalServer SHALL reject the request with an error message stating that the file is not a supported video and listing the Supported_Video_Containers.
4. IF a submitted file is a Supported_Video_Container but its first frame or last frame cannot be decoded, THEN THE LocalServer SHALL reject the request with an error message stating that the video could not be decoded, naming the detected codec when known, and listing the Supported_Video_Codecs.
5. IF a submitted video file exceeds 100 MB, THEN THE LocalServer SHALL reject the request with an error message naming the maximum accepted video file size.
6. IF a submitted video's frame rate is 0 or less or above 240 frames per second, or its displayed frame width or height exceeds 4096 pixels, THEN THE LocalServer SHALL reject the request with an error message naming the violated limit.
7. WHEN a pin request references an existing video file under the captured-image roots, THE Video_Pin_API SHALL pin it with the same Video_Validation and size limit as an uploaded file.
8. WHEN a user queries the Video_Pin_API status, THE Video_Pin_API SHALL report whether a Pinned_Video exists and, when one exists, its Video_Metadata.
9. IF a video pin request is rejected, THEN THE LocalServer SHALL leave the existing Pinned_Video, its Video_Metadata, and its Loop_Epoch unchanged.

### Requirement 2: Enumerate the Static_Video_Camera

**User Story:** As a workflow developer, I want the video camera to show up in the camera list next to my other cameras, so that I can select it the same way I select any camera.

#### Acceptance Criteria

1. WHILE a Pinned_Video exists, THE Camera_Enumeration SHALL include exactly one Static_Video_Camera entry in every enumeration result (standard enumeration and forced rescan alike). Each identity field (id, model, address, physical id, protocol, serial, vendor) SHALL be populated with a non-empty value.
2. WHILE no Pinned_Video exists, THE Camera_Enumeration SHALL exclude the Static_Video_Camera from every enumeration result.
3. THE LocalServer SHALL assign the Static_Video_Camera the fixed identifier `static-video-camera`. The identifier SHALL be character-for-character identical across pin, replace, and restart, and SHALL differ from the Static_Image_Camera identifier and from every physical camera identifier.
4. WHEN a Pinned_Video is created or removed, THE Camera_Enumeration SHALL reflect the change in the first enumeration result requested after the operation completes, without a LocalServer restart.
5. WHILE a Pinned_Video exists, THE Camera_Enumeration SHALL continue to include every physical camera and, while a Pinned_Image exists, the Static_Image_Camera, each with identity fields unchanged.
6. IF the Static_Video_Camera entry cannot be constructed during an enumeration, THEN THE Camera_Enumeration SHALL return the rest of the enumeration result rather than failing the request.

### Requirement 3: Loop playback

**User Story:** As a workflow developer, I want the pinned video to behave like a live camera, so that repeated captures and workflow runs see the scene move the way it would in production.

#### Acceptance Criteria

1. WHILE a Pinned_Video exists, WHEN a Frame_Consumer grabs a frame from the Static_Video_Camera, THE LocalServer SHALL return the frame at the Loop_Position for the grab's wall-clock time. The Loop_Position is the frame whose display interval contains the time elapsed since the Loop_Epoch, modulo the loop duration.
2. THE LocalServer SHALL compute the loop duration of a Pinned_Video as its frame count divided by its frame rate, so that playback advances at the video's native frame rate and returns to the first frame immediately after the display interval of the last frame.
3. WHEN two grabs fall within the same frame display interval, THE LocalServer SHALL return byte-for-byte identical frame data, width, height, and pixel format tag for both grabs.
4. WHEN grabs for the same wall-clock instant are served by different LocalServer processes, or by the LocalServer before and after a restart, THE LocalServer SHALL return the same video frame, without coordination between the processes.
5. THE LocalServer SHALL return every Static_Video_Camera frame as packed 24-bit RGB tagged with pixel format `RGB`. Width and height SHALL equal the Video_Metadata dimensions, and the data length SHALL be exactly 3 × width × height bytes.
6. WHERE a Pinned_Video carries rotation metadata of 90, 180, or 270 degrees, THE LocalServer SHALL serve frames in the orientation the video is meant to be displayed in and SHALL report the displayed width and height in the Video_Metadata.
7. WHEN acquisition settings (gain, exposure, or advanced settings) are supplied for a grab from the Static_Video_Camera, THE LocalServer SHALL complete the grab without error and return the same frame as a grab at the same instant without those settings.
8. WHEN a grab from the Static_Video_Camera begins, THE LocalServer SHALL return the frame or fail with an error within 10 seconds.
9. WHILE no Frame_Consumer is grabbing frames from the Static_Video_Camera, THE LocalServer SHALL perform no video decoding.
10. IF a grab targets the Static_Video_Camera while no Pinned_Video exists or the stored video cannot be decoded, THEN THE LocalServer SHALL fail the grab with an error that names the Static_Video_Camera and states that no usable pinned video is available.

### Requirement 4: Existing consumers work with the video camera

**User Story:** As a workflow developer, I want to use the video camera everywhere I can use a real camera, so that my test setup matches production except for the camera content.

#### Acceptance Criteria

1. WHEN a user configures an Image_Source with the Static_Video_Camera identifier, THE LocalServer SHALL accept it through the same interface and fields as physical cameras. The default processing pipeline SHALL declare packed RGB input.
2. WHEN a live preview is requested for an Image_Source backed by the Static_Video_Camera, THE LocalServer SHALL serve the frame at the current Loop_Position through the existing preview interface.
3. WHEN an image capture is requested from an Image_Source backed by the Static_Video_Camera, THE LocalServer SHALL store the frame at the Loop_Position of the capture time through the existing capture path.
4. WHEN a workflow run whose camera binding resolves to the Static_Video_Camera executes, THE Workflow_Executor SHALL feed the frame at the Loop_Position of the grab time through the existing Frame_Feed path. This SHALL require no change to the workflow document or the node catalog.
5. WHEN a digital-input trigger grabs a frame from the Static_Video_Camera, THE LocalServer SHALL return the frame at the Loop_Position of the grab time.
6. WHILE a Pinned_Video exists, THE camera inventory SHALL report exactly one Static_Video_Camera entry. The entry SHALL have:
   - type `StaticVideo`, origin `edge-discovered`, and empty parameters;
   - its identity and Video_Metadata under a `staticVideo` capability block.
7. WHEN a previously reported Static_Video_Camera is unpinned, THE camera inventory SHALL report it as absent with a stable absent-since timestamp instead of omitting it.
8. THE Portal workflow designer camera picker, the Camera_Binding path, and the deployment validator SHALL treat a `StaticVideo` inventory entry as compatible with `aravis_camera_source` nodes, and SHALL resolve its camera identifier from the `staticVideo` capability block.
9. WHEN the Static_Video_Camera is pinned, replaced, removed, or grabbed, THE LocalServer SHALL leave every physical camera connection and in-progress physical acquisition undisturbed.

### Requirement 5: Replace and remove

**User Story:** As a workflow developer, I want to swap or remove the looping video, so that I can iterate through test scenarios and return the device to its normal state.

#### Acceptance Criteria

1. WHEN a user pins a new video while a Pinned_Video exists, THE LocalServer SHALL replace it as a single atomic update, so that no grab returns a frame of any video other than the prior or the new one.
2. WHEN a grab begins after a replacement is confirmed, THE LocalServer SHALL serve only the new Pinned_Video, with the Loop_Epoch at the time of the replacement.
3. WHEN a user removes the Pinned_Video through a pin API, THE LocalServer SHALL delete the stored video, return a success confirmation, exclude the Static_Video_Camera from subsequent enumerations, and fail subsequent grabs per Requirement 3.10.
4. IF a removal request arrives while no Pinned_Video exists, THEN THE Video_Pin_API SHALL return an error stating that no video is pinned and change nothing.
5. WHEN the Pinned_Video is replaced or removed, THE LocalServer SHALL release each process's open decoder of the prior video no later than that process's first grab after the change.

### Requirement 6: Independence from the Static_Image_Camera

**User Story:** As a workflow developer, I want the image camera and the video camera to be separate options, so that I can use either one or both at the same time.

#### Acceptance Criteria

1. THE LocalServer SHALL hold the Pinned_Image and the Pinned_Video in separate slots, so that both can exist at the same time.
2. WHEN any pin, replace, or remove operation targets one of the two virtual cameras, THE LocalServer SHALL leave the other camera's pinned media, metadata, served frames, enumeration presence, and inventory entry unchanged.
3. THE Static_Image_Camera SHALL continue to satisfy every requirement of the `static-image-camera-source`, `cloud-static-camera-provisioning`, `static-image-camera-binding-and-pin-discoverability`, `static-camera-workflow-binding-invisible`, and `static-camera-pixel-format-and-detection-results` specifications, with its behavior, limits, messages, identity, and inventory entry unchanged.
4. WHILE both virtual cameras are pinned, THE workflow camera picker SHALL list them as two separate cameras, and a workflow SHALL be able to bind different nodes to each.

> **Deviation (device verification, 2026-09-27).** The second clause of criterion 4, "a workflow SHALL be able to bind different nodes to each", conflicts with an existing rule. The Workflow_Validator rule `V7_COEXISTENCE_CONFLICT` allows one frame-fed camera source per workflow, and the device runtime has the same single-Frame_Feed limit. The clause is deferred to the `multi-source-workflows` spec (Requirement 8.3 there), agreed with the user.
>
> This spec verifies criterion 4 as follows:
> - the camera picker lists both virtual cameras;
> - two workflows, one bound to each camera, run concurrently on one device.

### Requirement 7: Persistence across restarts

**User Story:** As a workflow developer, I want a pinned video to survive a LocalServer restart, so that long-running test setups keep their input.

#### Acceptance Criteria

1. WHILE a Pinned_Video is stored, WHEN the LocalServer restarts, THE LocalServer SHALL restore the Pinned_Video, its Video_Metadata, and its Loop_Epoch without user action.
2. WHEN Loop_Playback resumes after a restart, THE LocalServer SHALL continue from the Loop_Position given by the wall-clock time and the original Loop_Epoch, as if playback had continued during the restart.
3. IF a stored Pinned_Video cannot be opened or its first frame cannot be decoded at restore, THEN THE LocalServer SHALL:
   - log an error identifying the cause category (missing data versus undecodable data);
   - report no Pinned_Video;
   - keep serving the other cameras;
   - accept new video pin requests.

### Requirement 8: Pin a video from the Portal

**User Story:** As an operator, I want to upload a video from the Portal and have it play on the device's video camera, so that I can set up a simulated scene remotely.

#### Acceptance Criteria

1. THE Portal SHALL provide the Portal_Video_Pin_API as a set of routes separate from the Portal_Pin_API: upload URL, pin submit, removal, and status. These routes SHALL use the same authorization permissions (device-mutation for upload, pin, and removal; device-view for status), use-case resolution, and audit behavior as the Portal_Pin_API.
2. WHEN an operator submits a staged upload through the Portal_Video_Pin_API, THE Portal SHALL accept the submission for validation, show it as validating in the video status view, and run Video_Validation on it, decoding its first and last frames with the same validation logic and decoder version the LocalServer uses. Checks that need no decoding (authorization, the request body, the 100 MB limit, the staged object's presence) SHALL still reject the submission immediately.
3. IF the staged upload fails Video_Validation, THEN THE Portal SHALL:
   - show the submission as rejected, with the validation error message, in the video status view;
   - record no Video_Pin_Request;
   - write nothing to the Image_Transport or the Sync_Channel.
4. IF Video_Validation of a staged upload does not complete within 60 seconds, THEN THE Portal SHALL reject the submission as in 8.3, with an error stating that the video took too long to validate and suggesting a lower resolution or more frequent keyframes.
5. WHEN a staged upload passes Video_Validation, and no newer video submission or Video_Pin_Request for the device arrived while it was validating, THE Portal SHALL:
   - store it in the Image_Transport;
   - record a pending Video_Pin_Request that carries the validated Video_Metadata;
   - deliver it through a video slot of the Sync_Channel that is separate from the image slot.
6. WHEN a device receives a Video_Pin_Request, THE device SHALL retrieve and verify the content with the existing retrieval policy. It SHALL then apply the content through the same pin operation the Video_Pin_API uses.
7. IF the device rejects a Video_Pin_Request during validation, THEN THE device SHALL report it as failed with the validation error as the reason, and THE Portal SHALL show that reason in the status.
8. WHEN a Video_Pin_Request is applied, THE Portal video status view SHALL include the device-reported Video_Metadata.
9. THE Portal SHALL keep Video_Pin_Requests and image Pin_Requests independent. A video request SHALL never supersede, alter, or appear in the status of an image request, and vice versa.

### Requirement 9: Portal user interface

**User Story:** As an operator, I want separate, clearly labeled options for the static image camera and the video camera, so that I can set up either one without confusion.

#### Acceptance Criteria

1. THE Portal device Cameras tab SHALL show the static image camera panel (unchanged) and a separate static video camera panel.
2. THE static video camera panel SHALL accept video files (MP4, M4V, MOV, AVI, MKV, WebM) in its file picker, and SHALL state the 100 MB limit and that the video plays in a loop.
3. IF an operator selects a video file larger than 100 MB, THEN THE Portal SHALL show an error naming the limit and SHALL NOT start the upload.
4. WHILE a video upload is in progress, THE Portal SHALL show the upload progress.
5. WHEN a Pinned_Video is applied, THE static video camera panel SHALL show its duration, frame rate, frame count, codec, dimensions, container format, and file name.
6. THE static video camera panel SHALL show the latest Video_Pin_Request status, failure reason, device-reported presence, and a connectivity hint while pending, and SHALL offer replace and remove actions gated on the device-mutation permission, in the same way the static image camera panel does for images.
7. THE workflow node camera picker SHALL offer a separate shortcut for each virtual camera: one to the static image camera panel and one to the static video camera panel.

### Requirement 10: Platform support and resource safety

**User Story:** As a workflow developer, I want the video camera to behave the same on every device type and not starve the device, so that a simulated test is trustworthy.

#### Acceptance Criteria

1. THE LocalServer SHALL accept, decode, and loop videos in every Supported_Video_Container encoded with every Supported_Video_Codec on every LocalServer_Platform, and SHALL serve the same frame for the same Pinned_Video and Loop_Position on each of them.
2. THE LocalServer SHALL support the Static_Video_Camera without adding a dependency or modifying a Dockerfile, the on-device docker-compose file, `src/backend/requirements.txt`, or a component recipe.
3. WHILE a Pinned_Video exists, THE LocalServer SHALL hold at most one open decoder and one cached decoded frame per process for it.
4. THE LocalServer SHALL keep at most one Pinned_Video file on disk, apart from transient staging files that are removed when a pin completes or fails.
5. THE LocalServer and the Portal SHALL keep the Sync_Channel shadow document within the shadow document size limit in effect (the AWS IoT "Maximum size of a JSON state document" quota of the device's account, 8 KB by default) while both virtual cameras hold pinned media and their pin sections are populated.
6. WHERE the account's shadow document size quota is above the 8 KB default, THE Portal SHALL configure the device's local shadow size limit to the quota (at most 30 KB) when it deploys the LocalServer, and THE LocalServer SHALL raise its camera report cap to match, up to 10 KB. IF the quota or the local limit cannot be read, THEN both SHALL keep the 8 KB default budget.

## Resolved Decisions

These were decided with the user on 2026-09-26:

1. **A second camera.** Videos use a new `static-video-camera` with its own slot. The image camera is unchanged, and both can be pinned at once.
2. **Real-time loop.** Playback follows the wall clock at the video's native frame rate, so a slow workflow skips frames the way it would on a live camera.
3. **100 MB video limit.** Images stay at 50 MB. The device keeps its retrieval bound of 120 seconds per attempt, so a 100 MB video needs about 7 Mbit/s of download bandwidth on the device.
4. **Codecs.** H.264, H.265/HEVC, MPEG-4 Part 2, Motion JPEG, VP8, and VP9 are guaranteed. AV1 is rejected with a clear message, audio is ignored, and nothing is transcoded.
5. **The Portal decodes.** Portal validation decodes the first and last frames with the same logic and decoder version as the device. The device validates again when it applies the pin.
6. **Two separate UI options.** The Cameras tab has one panel per virtual camera, and the workflow picker has one shortcut per virtual camera.

Decided later on 2026-09-26, answering the open design choices:

7. **Portal validation time budget: 60 seconds.** The API Gateway integration timeout (29 s) cannot hold a 60 s decode, so validation runs asynchronously (Requirement 8.4): the pin submission is accepted for validation at once, and the status view reports the outcome.
8. **Where the Portal decodes.** A new Lambda function carries an OpenCV layer of about 190 MB and serves only the four video routes. It shares the existing camera registry function's IAM role, and the existing function's size and cold start are unaffected. The asynchronous validation adds one IAM statement: the function may invoke itself.
9. **Videos are stored in S3**, under the existing `static-image-pins/` prefix (`static-image-pins/{device}/video/…`, staging shared). That keeps the S3 grants, the 1-day staging lifecycle rule, and the CORS rules unchanged.
10. **Shadow budget: a report cap of up to 10 KB, following the account quota** (Requirements 10.5, 10.6). The cap is the shadow size limit minus a 3.5 KB reserve for both pin slots: 4.5 KB at the 8 KB default (5 KB overran the limit by 72 bytes in the measured worst case) and 10 KB from a 13.5 KB quota up. A quota increase to 16 KB was requested for the test account.
