# Requirements Document

## Introduction

This feature adds cloud-side provisioning for the existing static-image camera (spec `static-image-camera-source`). Today a user pins a still image on the device itself, through the LocalServer's device-local Pin_API. This convenience layer lets a Portal user do the same thing from the cloud: upload an image in the edge-cv-portal for a chosen edge device, have the image sync down, and land as that device's Pinned_Image — so the device's Static_Image_Camera works without SSHing or curling the device.

The device remains the source of truth: cloud-initiated pins apply on the device through the exact same pin semantics the Device_Pin_API established (validation, atomic replacement, persistence across restarts, enumeration only while pinned). The cloud side reuses the existing per-device Camera_Registry sync infrastructure (the named-shadow Sync_Channel that the camera-registry-sync feature already operates) for control messages and status, and a separate Image_Transport for the image binary — a named-shadow document cannot carry image bytes (the sync document limit is on the order of kilobytes; images are up to 50 MB).

Once pinned, the Static_Image_Camera flows into the device's reported camera inventory, the Portal Camera_Registry, the Workflow_Builder camera reference picker, and the deployment Camera_Binding_Matrix through the existing paths, with no special-casing.

Out of scope: multiple static cameras per device, changes to the device-side pin semantics, and fan-out pinning of one image to multiple devices in a single request.

## Glossary

- **Portal**: The edge-cv-portal cloud application (backend APIs and frontend) that manages use cases, devices, workflows, and deployments.
- **Workflow_Builder**: The Portal's workflow authoring UI, including the camera reference picker that lists registry-backed camera sources.
- **Camera_Binding_Matrix**: The Portal's deployment-creation UI that maps a workflow's camera references to concrete camera sources on each target device.
- **LocalServer**: The DDA edge application backend running on an edge device; it owns the device-side pin store, camera enumeration, and frame serving.
- **Static_Image_Camera**: The virtual GenICam-style camera from the base feature, with the fixed identifier `static-image-camera`, that serves the Pinned_Image as frames and enumerates only while a Pinned_Image exists.
- **Pinned_Image**: The still image currently assigned to a device's Static_Image_Camera.
- **Device_Pin_API**: The existing device-local pin interface on the LocalServer (pin, status, unpin). Its semantics — validation, atomic replacement, restart persistence, enumeration-while-pinned — are authoritative for what "pinned" means on the device.
- **Portal_Pin_API**: The new Portal interface through which a user creates, inspects, replaces, and removes a device's Pinned_Image from the cloud.
- **Pin_Request**: One cloud-initiated pin, replace, or removal operation targeting exactly one device, tracked by the Portal with a Sync_Status.
- **Sync_Status**: The lifecycle state of a Pin_Request: `pending`, `applied`, `failed`, or `superseded`.
- **Sync_Channel**: The existing per-device named-shadow document channel operated by the camera-registry sync infrastructure, through which desired changes reach a device and confirmations return.
- **Image_Transport**: The delivery mechanism for image binary content between the Portal and a device, separate from the Sync_Channel (for example, cloud object storage with device-authorized download).
- **Content_Checksum**: A checksum of the image file bytes, carried with the Sync_Channel reference, that the LocalServer verifies before applying a pin.
- **Camera_Registry**: The Portal's per-device camera inventory, synchronized from device reports over the Sync_Channel and served under the existing `/devices/{id}/cameras` routes.
- **Target_Device**: The single edge device a Pin_Request addresses.
- **Supported_Image_Format**: An image file format accepted for pinning: JPEG, PNG, or BMP (identical to the base feature).

## Requirements

### Requirement 1: Pin a static image from the Portal

**User Story:** As a workflow developer, I want to upload a still image in the Portal and pin it to a chosen edge device, so that the device's static image camera works without me accessing the device directly.

#### Acceptance Criteria

1. WHEN a user submits an image file for a Target_Device through the Portal_Pin_API, THE Portal SHALL validate that the file decodes as a Supported_Image_Format and that the file size is at most 50 MB (the same limit the Device_Pin_API enforces) before creating a Pin_Request.
2. WHEN validation of a submitted image file succeeds, THE Portal SHALL create a Pin_Request in the `pending` Sync_Status and initiate delivery to the Target_Device by making the image content retrievable through the Image_Transport and writing the Pin_Request reference to the Target_Device's Sync_Channel.
3. IF a submitted file cannot be decoded as an image in a Supported_Image_Format, THEN THE Portal SHALL reject the submission with an error message enumerating the Supported_Image_Formats (JPEG, PNG, BMP), create no Pin_Request, and send nothing to the Target_Device.
4. IF a submitted file exceeds 50 MB, THEN THE Portal SHALL reject the submission with an error message naming the 50 MB limit, create no Pin_Request, and send nothing to the Target_Device.
5. WHEN a Pin_Request is created, THE Portal_Pin_API SHALL return the Pin_Request identifier, the Target_Device identifier, and the `pending` Sync_Status.
6. THE Portal_Pin_API SHALL associate each Pin_Request with exactly one Target_Device.
7. WHEN a user queries the static camera provisioning state for a Target_Device that has at least one Pin_Request, THE Portal_Pin_API SHALL report the most recent Pin_Request's Sync_Status and, when the most recent pin-type Pin_Request is in the `applied` Sync_Status, SHALL include the device-reported image metadata recorded at confirmation (width in pixels, height in pixels, image format, and original file name).
8. IF a pin submission targets a device identifier that does not correspond to a device registered in the Portal, THEN THE Portal SHALL reject the submission with an error message indicating the device was not found, create no Pin_Request, and send nothing to any device.
9. IF the Portal cannot make the image content retrievable through the Image_Transport or cannot write the Pin_Request reference to the Target_Device's Sync_Channel, THEN THE Portal SHALL transition the Pin_Request to the `failed` Sync_Status and return an error message through the Portal_Pin_API identifying the delivery initiation failure.
10. WHEN a user queries the static camera provisioning state for a Target_Device that has no Pin_Request, THE Portal_Pin_API SHALL report that no cloud-initiated Pin_Request exists for that Target_Device.

### Requirement 2: Deliver the image outside the Sync_Channel

**User Story:** As a platform operator, I want the image content delivered through a channel suited to binary payloads, so that pinning works within the sync infrastructure's document size limits and survives transient network conditions.

#### Acceptance Criteria

1. WHEN the Portal accepts a Pin_Request, THE Portal SHALL store the Pin_Request image content in the Image_Transport before writing any Sync_Channel document for that Pin_Request.
2. WHEN the Portal writes a Sync_Channel document for a Pin_Request, THE Portal SHALL include in the document a reference to the stored image content, the Content_Checksum, and the image metadata (at minimum the image byte size and the image format), and SHALL exclude the image bytes.
3. THE Portal SHALL keep each Sync_Channel document written for a Pin_Request at or below 8 KB.
4. IF the Sync_Channel document for a Pin_Request would exceed 8 KB, THEN THE Portal SHALL reject the Pin_Request without writing to the Sync_Channel and SHALL present the requesting operator an error indicating the document size limit was exceeded.
5. IF the Portal fails to store the Pin_Request image content in the Image_Transport, THEN THE Portal SHALL NOT write a Sync_Channel document for that Pin_Request and SHALL present the requesting operator an error indicating the storage failure.
6. WHILE a Pin_Request is in the `pending` Sync_Status, THE Portal SHALL keep the referenced image content retrievable by the Target_Device through the Image_Transport.
7. THE Image_Transport SHALL reject retrieval requests for Pin_Request image content that do not carry authorization credentials belonging to the Target_Device.
8. WHEN the LocalServer retrieves the image content for a Pin_Request, THE LocalServer SHALL verify that the retrieved bytes match the Content_Checksum before applying the pin.
9. IF a retrieval attempt does not complete within 120 seconds, or completes with bytes that do not match the Content_Checksum, THEN THE LocalServer SHALL discard any retrieved bytes and treat the attempt as a failed retrieval attempt.
10. IF a retrieval attempt for the Target_Device's most recent Pin_Request fails, THEN THE LocalServer SHALL retry retrieval, waiting at least 5 seconds between consecutive attempts, until the content is retrieved and verified against the Content_Checksum or until 3 attempts total have failed.
11. IF all 3 retrieval attempts for a Pin_Request fail, THEN THE LocalServer SHALL leave any existing Pinned_Image unchanged and report a `failed` Sync_Status with a reason identifying the failure cause of the final attempt (retrieval failure or checksum mismatch).

### Requirement 3: Apply on the device through existing pin semantics

**User Story:** As a workflow developer, I want a cloud-initiated pin to behave exactly like a device-side pin, so that the static image camera works identically no matter how the image was pinned.

#### Acceptance Criteria

1. WHEN the LocalServer applies a cloud-initiated Pin_Request, THE LocalServer SHALL store the image as the device's Pinned_Image through the same pin operation the Device_Pin_API uses, applying the same decode validation and file size limit as a device-initiated pin, and replacing any previously existing Pinned_Image (whether device-initiated or cloud-initiated) as a single atomic update such that no frame grab returns content combining the prior and new images.
2. WHEN a cloud-initiated pin completes on the device, THE LocalServer SHALL include the Static_Image_Camera under the fixed camera identifier `static-image-camera` in the first camera enumeration result requested after completion, with each identity field (id, model, address, physical id, protocol, serial, vendor) equal to the value a device-initiated pin produces.
3. WHEN a cloud-initiated pin completes on the device, THE LocalServer SHALL report through the Device_Pin_API status query that a Pinned_Image exists, with each metadata field (width in pixels, height in pixels, image format, and original file name) equal to the value a device-initiated pin of the same image file reports.
4. IF the on-device application of a Pin_Request fails at any step (validation, decode, or storage), THEN THE LocalServer SHALL retain any prior Pinned_Image unchanged such that frame grabs beginning after the failure return the prior image content, and SHALL report a `failed` Sync_Status for that Pin_Request through the Sync_Channel including a failure reason describing why the application failed.
5. WHEN a Pin_Request bearing an identifier the LocalServer has already applied is delivered again, THE LocalServer SHALL NOT re-execute the pin operation and SHALL leave the Pinned_Image content, the Static_Image_Camera enumeration entry, and the reported Sync_Status for that Pin_Request unchanged from the first application.
6. WHEN a cloud-initiated pin application completes, THE LocalServer SHALL confirm the application through the Sync_Channel only after the Pinned_Image is stored and available to frame grabs, including in the confirmation the Pin_Request identifier and the applied image metadata (width in pixels, height in pixels, image format, and original file name).
7. WHEN a frame grab targets the Static_Image_Camera after a cloud-initiated pin of an image file completes, THE LocalServer SHALL return frame data byte-for-byte identical, with identical width, height, and pixel format, to the frame data returned after a device-initiated pin of the same image file.
8. WHEN a user replaces or removes a cloud-initiated Pinned_Image through the Device_Pin_API, THE LocalServer SHALL complete the operation with the same observable results (success or error response, resulting Pinned_Image state, and Static_Image_Camera enumeration) as the same operation performed on a device-initiated Pinned_Image.

### Requirement 4: Sync status visibility in the Portal

**User Story:** As a workflow developer, I want to see whether my pin reached the device, so that I know when the camera is ready to use in a workflow.

#### Acceptance Criteria

1. THE Portal SHALL track each Pin_Request in exactly one Sync_Status at a time — `pending`, `applied`, `failed`, or `superseded` — transitioning a Pin_Request's Sync_Status only from the `pending` Sync_Status to exactly one of `applied`, `failed`, or `superseded`.
2. WHEN the Target_Device confirms a pin application through the Sync_Channel for a Pin_Request in the `pending` Sync_Status, THE Portal SHALL transition that Pin_Request to the `applied` Sync_Status and record the device-reported image metadata (width in pixels, height in pixels, image format, and original file name) and the confirmation timestamp.
3. WHEN the Target_Device reports a pin application failure through the Sync_Channel for a Pin_Request in the `pending` Sync_Status, THE Portal SHALL transition that Pin_Request to the `failed` Sync_Status, record the device-reported failure reason and the timestamp of the failure report, and include both in subsequent status query responses.
4. WHEN a user queries a Target_Device's static camera provisioning status, THE Portal_Pin_API SHALL return the identifier, Sync_Status, operation type (pin or removal), and creation timestamp of the Target_Device's most recent Pin_Request, where "most recent" means the Pin_Request with the latest creation timestamp for that Target_Device.
5. WHILE the most recent Pin_Request is in the `pending` Sync_Status, THE Portal_Pin_API SHALL include in status query responses the Target_Device's connectivity status as exactly one of `connected` or `disconnected`, reflecting the Portal's connectivity record for the Target_Device at the time of the query.
6. IF the Target_Device's reported pinned state differs from the outcome recorded on the most recent Pin_Request, THEN THE Portal SHALL present the device-reported pinned state as the current state in status query responses while reporting the Pin_Request's recorded Sync_Status unchanged.
7. IF a user queries the static camera provisioning status for a Target_Device with zero Pin_Requests, THEN THE Portal_Pin_API SHALL return a response indicating that no Pin_Request exists for the Target_Device, rather than an error.
8. IF the Portal receives a Sync_Channel confirmation or failure report referencing a Pin_Request that is not in the `pending` Sync_Status, THEN THE Portal SHALL leave that Pin_Request's Sync_Status unchanged and present the device-reported pinned state as the current state in status query responses.

### Requirement 5: Offline devices and superseding requests

**User Story:** As a workflow developer, I want a pin sent to a temporarily offline device to apply when the device reconnects, and a newer request to win over an older one, so that the device converges to my latest intent.

#### Acceptance Criteria

1. WHILE the Target_Device is disconnected, THE Portal SHALL retain the most recent Pin_Request in the `pending` Sync_Status without time-based expiry and SHALL keep that Pin_Request's Sync_Channel document available for the Target_Device.
2. WHILE a `pending` Pin_Request exists for the Target_Device, WHEN the Target_Device re-establishes its Sync_Channel connection, THE LocalServer SHALL begin processing the most recent Pin_Request within 60 seconds of reconnection and SHALL apply it without user action.
3. WHEN a user submits a new Pin_Request for a Target_Device whose most recent Pin_Request is in the `pending` Sync_Status, THE Portal SHALL transition the earlier Pin_Request to the `superseded` Sync_Status and SHALL dispatch the new Pin_Request by replacing the Sync_Channel document contents, so that the Target_Device can observe only the newest Pin_Request.
4. WHEN the Target_Device reconnects after two or more Pin_Requests were issued while it was disconnected, THE LocalServer SHALL apply only the newest Pin_Request, converging the device's Pinned_Image state to that request's requested state (a Pinned_Image for a pin operation, no Pinned_Image for a removal operation), and SHALL apply zero superseded Pin_Requests.
5. IF application of the newest Pin_Request fails after the Target_Device reconnects, THEN THE LocalServer SHALL report a `failed` Sync_Status for the newest Pin_Request, SHALL leave any existing Pinned_Image unchanged, and SHALL NOT apply any superseded Pin_Request.
6. IF the Portal receives an application confirmation or failure report through the Sync_Channel for a Pin_Request that is in the `superseded` Sync_Status, THEN THE Portal SHALL keep that Pin_Request in the `superseded` Sync_Status and SHALL leave the Sync_Status of the most recent Pin_Request unchanged.
7. WHEN a Pin_Request transitions to the `superseded` Sync_Status, THE Portal SHALL exclude the superseded Pin_Request from the current provisioning state in status query responses and SHALL retain its record (Pin_Request identifier, operation type, creation timestamp, and `superseded` Sync_Status) for status history.

### Requirement 6: Registry, Workflow_Builder, and deployment integration

**User Story:** As a workflow developer, I want the pinned static camera to show up in the Portal's camera registry, the Workflow Builder camera picker, and the deployment camera binding matrix like any other camera, so that my test workflows bind to it with no special steps.

#### Acceptance Criteria

1. WHEN the Target_Device reports its camera inventory after a pin is applied, THE Camera_Registry SHALL list the Static_Image_Camera entry for that device under the fixed identifier `static-image-camera`, recorded as a device-reported discovery-managed entry carrying the device-reported metadata, through the same inventory synchronization used for physical cameras, including restoring to present an entry previously marked absent.
2. WHEN the Target_Device reports a camera inventory without the Static_Image_Camera after a removal, THE Camera_Registry SHALL mark the device's Static_Image_Camera entry absent rather than deleting it, record the timestamp at which the absence was reported, and apply the same absence handling used for physical cameras.
3. WHEN a user opens the Workflow_Builder camera reference picker with registry data for a device whose inventory includes the Static_Image_Camera, THE Workflow_Builder SHALL present the Static_Image_Camera as a selectable camera reference through the same registry-backed listing as physical cameras.
4. WHEN a user creates a deployment targeting a device whose inventory includes the Static_Image_Camera, THE Camera_Binding_Matrix SHALL offer the Static_Image_Camera as a bindable camera source through the same binding flow as physical cameras.
5. IF a user attempts to modify or delete the device-reported Static_Image_Camera entry through the generic Camera_Registry mutation routes, THEN THE Camera_Registry SHALL reject the mutation with the existing discovery-managed rejection, leave the Static_Image_Camera entry unchanged, and return an error indicating the entry is discovery-managed and cannot be modified through those routes.
6. THE Portal SHALL bind workflows to the Static_Image_Camera through the existing camera reference and camera binding mechanisms, with zero additions to the workflow node catalog and zero changes to the workflow document schema.
7. IF a deployment submission contains a Camera_Binding referencing a Target_Device's Static_Image_Camera entry that is marked absent in the Camera_Registry, THEN THE Portal SHALL display the same absence warning used for physical cameras identifying the Static_Image_Camera's condition and require explicit user confirmation before creating the deployment.
8. IF a deployment submission contains a Camera_Binding referencing the Static_Image_Camera on a Target_Device whose Camera_Registry contains no Static_Image_Camera entry, THEN THE Portal SHALL reject the deployment with a message identifying the missing camera source and the Target_Device, through the same missing-source rejection used for physical cameras.

### Requirement 7: Replace and unpin from the Portal

**User Story:** As a workflow developer, I want to swap or remove the pinned image from the Portal, so that I can iterate through test scenarios and clean up without device access.

#### Acceptance Criteria

1. WHEN a user submits a new image for a Target_Device that already holds an applied Pinned_Image, THE Portal SHALL process the submission as a Pin_Request through the same validation and Sync_Status lifecycle as an initial pin, whose on-device application replaces the existing Pinned_Image as a single atomic update under the Device_Pin_API's atomic replacement semantics.
2. WHEN a user requests removal of a Target_Device's Pinned_Image through the Portal_Pin_API, THE Portal SHALL create a removal Pin_Request in the `pending` Sync_Status, return the Pin_Request identifier, the Target_Device identifier, and the `pending` Sync_Status to the requesting user, and dispatch the removal Pin_Request through the Sync_Channel.
3. WHEN the LocalServer applies a removal Pin_Request, THE LocalServer SHALL delete the Pinned_Image and exclude the Static_Image_Camera from camera enumeration through the same unpin operation the Device_Pin_API uses, and SHALL confirm the removal through the Sync_Channel.
4. WHEN a removal Pin_Request arrives at a Target_Device holding no Pinned_Image, including on repeated deliveries of the same removal Pin_Request, THE LocalServer SHALL make zero changes to the device's pin state and SHALL confirm the removal through the Sync_Channel.
5. WHEN the Portal receives a removal confirmation through the Sync_Channel, THE Portal SHALL transition the removal Pin_Request to the `applied` Sync_Status and report the Target_Device as holding no Pinned_Image in status query responses.
6. WHEN the Target_Device reports its camera inventory after a pin, replace, or unpin performed directly through the Device_Pin_API, THE Portal SHALL update that device's Camera_Registry to match the reported inventory through the existing Camera_Registry synchronization.
7. WHILE a replacement Pin_Request for a Target_Device is in the `pending` Sync_Status, THE LocalServer SHALL retain the previously applied Pinned_Image as the device's Pinned_Image and continue serving it through the Static_Image_Camera.
8. IF the on-device application of a replacement or removal Pin_Request fails, THEN THE LocalServer SHALL leave the existing Pinned_Image and the Static_Image_Camera's enumeration state unchanged and report a `failed` Sync_Status carrying the failure reason.

### Requirement 8: Authorization and audit

**User Story:** As a portal administrator, I want cloud-side pin operations permission-gated and audited, so that device state changes are controlled and traceable.

#### Acceptance Criteria

1. WHEN a user holding the device-mutation permission for the Target_Device's use case (the same permission that gates the existing Camera_Registry mutation routes) submits a pin, replace, or removal request through the Portal_Pin_API, THE Portal SHALL accept the request.
2. IF a user without the device-mutation permission for the Target_Device's use case submits a pin, replace, or removal request through the Portal_Pin_API, THEN THE Portal SHALL reject the request with an authorization error identifying the required permission, create no Pin_Request, write nothing to the Sync_Channel, and log an unauthorized-access audit event recording the acting user, the Target_Device, the attempted operation type, and the timestamp of the rejection.
3. WHEN a user holding the device-view permission for the Target_Device's use case (the same permission that gates the existing Camera_Registry view routes) queries provisioning status through the Portal_Pin_API, THE Portal SHALL return the status response.
4. WHEN the Portal accepts a pin, replace, or removal request, THE Portal SHALL log an audit event recording the acting user, the Target_Device, the operation type, the Pin_Request identifier, and the timestamp of the acceptance, in the same audit log that records existing Camera_Registry mutation audit events.
5. WHEN the Portal performs a permission check for a Pin_Request submission or a provisioning status query, THE Portal SHALL resolve the Target_Device's use case from Portal-side device records rather than from caller-supplied scoping parameters.
6. IF a user without the device-view permission for the Target_Device's use case queries provisioning status through the Portal_Pin_API, THEN THE Portal SHALL reject the query with an authorization error, return no provisioning status data, and log an unauthorized-access audit event recording the acting user, the Target_Device, and the timestamp of the rejection.
7. IF a pin, replace, or removal request names a Target_Device for which no Portal-side device record exists, THEN THE Portal SHALL reject the request with an error indicating the Target_Device is not registered, create no Pin_Request, and write nothing to the Sync_Channel.

## Open Decisions

Defaults chosen for this initial version — flag any you want changed:

1. **UX entry point(s)**: Where does the user pin from? Default: the device detail page (alongside the existing camera registry view) as the primary management surface, plus a shortcut from the Workflow_Builder camera reference picker that asks the user to choose a target device. Either surface alone, or both, is a UX call — the requirements above are entry-point-agnostic.
2. **Offline / pending behavior**: Pending Pin_Requests persist indefinitely (no expiry) and the newest request wins when the device reconnects (Requirement 5). If you prefer pending requests to expire after a time limit, Requirement 5 gains an expiry criterion.
3. **Who may pin**: The same device-mutation permission that gates the existing Camera_Registry mutation routes (Operator-level, `MANAGE_DEVICES`). If pinning should require a stricter or looser role, Requirement 8 changes.
4. **Cloud-side image retention**: The Portal keeps the uploaded image retrievable while its Pin_Request is pending (Requirement 2.3); after the request is applied, superseded, or removed, the cloud copy may be deleted. If you want the portal to retain applied images (e.g. for preview or re-pin), that becomes an added requirement.
5. **Portal preview of the pinned image**: Not included. The status response carries metadata only (width, height, format, file name). A thumbnail/preview in the Portal UI would be an added requirement.
