/**
 * Camera reference selection for the Workflow_Builder camera picker
 * (camera-registry-sync Requirements 7.1-7.5).
 *
 * The `icam_source` node's `device` parameter renders as a camera
 * reference control in the node configuration panel: a reference-device
 * selector over the current Use_Case's devices and a camera dropdown fed
 * by `GET /devices/{id}/cameras`. Selecting a Camera_Source populates
 * the node's `device` parameter (plus `gain`/`exposure` when the
 * source's params carry them) and records an advisory binding hint
 * (`data.cameraBindingHint`) on the node (Requirement 7.2).
 *
 * Everything here is pure: `applyCameraSelection` is the single place
 * a Camera_Source is applied to a Camera_Input_Node's parameters, kept
 * free of React/DOM so the fast-check property test (Property 11) can
 * target it directly. The hint is advisory node data — the validator
 * and compiler ignore it, so the definition stays device-portable
 * (Requirement 7.5).
 */

import type { JsonValue } from './types';

// --------------------------------------------------------------------------
// Wire shapes (GET /devices/{id}/cameras — camera_registry.py camera_view)
// --------------------------------------------------------------------------

/** One Camera_Registry entry as served by `GET /devices/{id}/cameras`. */
export interface CameraSourceEntry {
  camera_source_id: string;
  name?: string | null;
  type?: string | null;
  /** Type-specific parameters (devicePath / url, gain, exposure, ...). */
  params?: Record<string, JsonValue> | null;
  capabilities?: Record<string, JsonValue> | null;
  origin?: string | null;
  version?: number | null;
  last_reported_at?: number | null;
  sync_status?: string | null;
  failure_reason?: string | null;
  absent?: boolean;
  absent_since?: number | null;
  /** Computed against the Staleness_Threshold by the read route (Req 4.1). */
  stale?: boolean;
  /**
   * Credential state of a stream Camera_Source (`RTSP` / `RTMP` only;
   * rtsp-rtmp-stream-cameras Requirement 5.7). Never a credential value.
   */
  credentials?: { configured: boolean; updatedAt?: number | null } | null;
}

/**
 * Device_Stream_Capabilities as the Portal stores them from the device's
 * report (rtsp-rtmp-stream-cameras Requirement 16.5): which protocols the
 * LocalServer can pull, the decoder element per codec and path (a path the
 * device cannot decode with is absent), and the library versions.
 */
export interface DeviceStreamCapabilities {
  rtsp?: boolean;
  rtmp?: boolean;
  tls?: boolean;
  codecs?: Record<string, { hardware?: string; software?: string }>;
  gstreamer?: string;
  pyav?: string;
  ffmpeg?: string;
  probedAtMs?: number;
}

/** Response of `GET /devices/{device_id}/cameras`. */
export interface DeviceCamerasResponse {
  device_id: string;
  usecase_id?: string | null;
  /** Explicit never-synced state, never a bare empty list (Req 1.6). */
  state: 'synced' | 'never-synced';
  never_synced?: boolean;
  last_report_at?: number | null;
  staleness_threshold_hours?: number;
  /** IoT connectivity from the existing device-status lookup (Req 4.2). */
  device_status?: string;
  cameras: CameraSourceEntry[];
  count?: number;
  /** Present once the device has reported them (Requirement 16.5). */
  stream_capabilities?: DeviceStreamCapabilities | null;
}

/**
 * One conflict event as served by `GET /devices/{id}/cameras/conflicts`
 * (Req 6.3): both conflicting versions, the applied resolution, and the
 * timestamp; `reapplied_as` records a re-issued portal change (Req 6.4).
 */
export interface CameraConflictEvent {
  conflict_id: string;
  camera_source_id?: string | null;
  edge_version?: Record<string, JsonValue> | null;
  portal_version?: Record<string, JsonValue> | null;
  resolution?: string | null;
  created_at?: number | null;
  reapplied_as?: string | null;
}

/** Response of `GET /devices/{device_id}/cameras/conflicts`. */
export interface DeviceCameraConflictsResponse {
  device_id: string;
  usecase_id?: string | null;
  conflicts: CameraConflictEvent[];
  count?: number;
}

/**
 * Write-only Stream_Credentials of a stream Camera_Source
 * (rtsp-rtmp-stream-cameras Requirement 5.3). Sent only when the operator
 * typed a value; the Portal stores them in the Credential_Vault and never
 * returns them.
 */
export interface StreamCredentialsInput {
  username?: string;
  password?: string;
  urlSecret?: string;
}

/** Create/update body for portal-managed Camera_Sources (Req 5.1). */
export interface CameraSourceMutationBody {
  name: string;
  type: string;
  params?: Record<string, JsonValue>;
  /** Stream types only: new credentials (Requirement 5.3). */
  credentials?: StreamCredentialsInput;
  /** Stream types only: remove the stored credentials (Requirement 5.8). */
  clearCredentials?: boolean;
}

/**
 * Response of the mutating camera routes (create/update/delete and
 * conflict re-apply): the entry is marked pending with a fresh
 * portal_change_id after the shadow desired write succeeded.
 */
export interface CameraMutationResponse {
  device_id: string;
  camera_source_id: string;
  origin?: string;
  sync_status: string;
  portal_change_id: string;
  conflict_id?: string;
}

// --------------------------------------------------------------------------
// Static-image pin provisioning wire shapes (cloud-static-camera-
// provisioning task 9.1 — camera_registry.py Portal_Pin_API routes)
// --------------------------------------------------------------------------

/** Response of `POST /devices/{id}/cameras/static-image/upload-url`. */
export interface StaticImageUploadUrlResponse {
  deviceId: string;
  /** Presigned PUT for the staging key (15-minute TTL). */
  uploadUrl: string;
  stagingKey: string;
  bucket: string;
  expiresInSeconds: number;
}

/**
 * Response of the pin submit (POST .../static-image/pin) and removal
 * (DELETE .../static-image/pin) routes: the new Pin_Request in the
 * `pending` Sync_Status (Reqs 1.5, 7.2).
 */
export interface StaticImagePinSubmitResponse {
  pinRequestId: string;
  deviceId: string;
  status: string;
}

/** Device-reported image metadata recorded at confirmation (Req 1.7). */
export interface StaticImagePinMetadata {
  width?: number | null;
  height?: number | null;
  format?: string | null;
  fileName?: string | null;
}

/** The most recent non-superseded Pin_Request (Reqs 4.4, 5.7). */
export interface StaticImagePinLatest {
  pinRequestId: string;
  op?: 'pin' | 'remove' | string | null;
  status?: 'pending' | 'applied' | 'failed' | string | null;
  createdAt?: number | null;
  completedAt?: number | null;
  failureReason?: string | null;
  deviceMetadata?: StaticImagePinMetadata | null;
}

/** One status-history record (superseded requests retained, Req 5.7). */
export interface StaticImagePinHistoryEntry {
  pinRequestId: string;
  op?: string | null;
  status?: string | null;
  createdAt?: number | null;
}

/**
 * Device-reported pinned state derived from the CAMERA#static-image-camera
 * registry entry, presented as the current state even when it disagrees
 * with the recorded Pin_Request outcome (Reqs 4.6, 4.8).
 */
export interface StaticImageDeviceReported {
  present: boolean;
  absent: boolean;
  absentSince?: number | null;
}

/**
 * Query parameter the node panel's "Pin a static test image…" shortcut
 * carries into the device page, and that the device Cameras tab reads to
 * bring the "Static image camera" panel into view and flag it on arrival
 * (static-image-camera-binding-and-pin-discoverability Requirement 2.5).
 * Shared by the writer (`NodeConfigPanel`) and the reader
 * (`DeviceDetail`) so the two cannot drift apart.
 */
export const STATIC_IMAGE_FOCUS_PARAM = 'focus';

/** Value of `STATIC_IMAGE_FOCUS_PARAM` targeting the static-image panel. */
export const STATIC_IMAGE_FOCUS_VALUE = 'static-image';

/** Response of `GET /devices/{id}/cameras/static-image` (status view). */
export interface StaticImagePinStatusResponse {
  deviceId: string;
  usecaseId?: string | null;
  latest: StaticImagePinLatest | null;
  /** True for a device with zero Pin_Requests (Reqs 1.10, 4.7). */
  noPinRequest: boolean;
  deviceReported: StaticImageDeviceReported | null;
  history: StaticImagePinHistoryEntry[];
  /** Included exactly while `latest` is `pending` (Req 4.5). */
  connectivity?: 'connected' | 'disconnected';
  /** Present when the most recent pin-type request is applied (Req 1.7). */
  deviceMetadata?: StaticImagePinMetadata;
}

// --------------------------------------------------------------------------
// Static-video pin provisioning wire shapes (static-camera-video-loop —
// camera_registry.py Portal_Video_Pin_API routes)
// --------------------------------------------------------------------------

/**
 * Response of `POST /devices/{id}/cameras/static-video/upload-url`. Video
 * uploads share the image staging prefix, so the shape is the image one.
 */
export type StaticVideoUploadUrlResponse = StaticImageUploadUrlResponse;

/**
 * Video_Metadata: what the Portal's validation determined (container
 * `format`, codec, displayed size, fps, frame count, loop duration) and,
 * once applied, what the device reports (additionally the file name, file
 * size, and the loop epoch).
 */
export interface StaticVideoPinMetadata {
  format?: string | null;
  codec?: string | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  frameCount?: number | null;
  durationMs?: number | null;
  fileName?: string | null;
  fileSizeBytes?: number | null;
  pinnedAtEpochMs?: number | null;
}

/**
 * Response of the video pin submit (POST .../static-video/pin, HTTP 202):
 * the staged video was accepted for Video_Validation, which runs
 * asynchronously (up to a minute of decoding). The status view's
 * `validation` reports the outcome.
 */
export interface StaticVideoValidationSubmitResponse {
  validationId: string;
  deviceId: string;
  status: 'validating' | string;
}

/**
 * Response of the video removal route (DELETE .../static-video/pin): the
 * new removal Video_Pin_Request, pending.
 */
export type StaticVideoPinSubmitResponse = StaticImagePinSubmitResponse;

/**
 * The latest video submission's asynchronous Video_Validation, as the
 * status view reports it. `accepted` carries the Video_Pin_Request it
 * created; `rejected` and `expired` carry the reason; `superseded` means a
 * newer submission or a removal replaced it.
 */
export interface StaticVideoValidation {
  validationId: string;
  status: 'validating' | 'accepted' | 'rejected' | 'superseded' | 'expired' | string;
  createdAt?: number | null;
  completedAt?: number | null;
  fileName?: string | null;
  error?: string | null;
  pinRequestId?: string | null;
  validatedMetadata?: StaticVideoPinMetadata | null;
}

/** The most recent non-superseded Video_Pin_Request. */
export interface StaticVideoPinLatest
  extends Omit<StaticImagePinLatest, 'deviceMetadata'> {
  deviceMetadata?: StaticVideoPinMetadata | null;
  /** Metadata the Portal's validation determined at submission. */
  validatedMetadata?: StaticVideoPinMetadata | null;
}

/** Response of `GET /devices/{id}/cameras/static-video` (status view). */
export interface StaticVideoPinStatusResponse
  extends Omit<StaticImagePinStatusResponse, 'latest' | 'deviceMetadata'> {
  latest: StaticVideoPinLatest | null;
  /** Present when the most recent video pin request is applied. */
  deviceMetadata?: StaticVideoPinMetadata;
  /** The latest submission's validation, when the device has one. */
  validation?: StaticVideoValidation;
}

/**
 * Value of `STATIC_IMAGE_FOCUS_PARAM` targeting the static-video panel
 * (the node panel's "Pin a test video…" shortcut).
 */
export const STATIC_VIDEO_FOCUS_VALUE = 'static-video';

/** The video pin size limit, the device's (and Portal's) 100 MB. */
export const MAX_PIN_VIDEO_BYTES = 100 * 1024 * 1024;

// --------------------------------------------------------------------------
// The advisory binding hint stored on the node (Requirements 7.2, 7.5)
// --------------------------------------------------------------------------

/** Key of the hint inside the definition node's advisory `data` record. */
export const CAMERA_BINDING_HINT_KEY = 'cameraBindingHint';

/**
 * The default binding hint recorded on a Camera_Input_Node when a
 * Camera_Source is selected. Advisory only: it pre-selects the binding
 * at deploy time (Requirement 8.5) but never makes the definition
 * specific to the referenced device (Requirement 7.5).
 */
export interface CameraBindingHint {
  cameraSourceId: string;
  cameraName: string;
  sourceDeviceId: string;
}

/**
 * The binding hint carried in a node's advisory data record, or null
 * when absent or malformed (definitions are external input; a hint that
 * does not carry the three string fields is ignored, never thrown on).
 */
export function getCameraBindingHint(
  data: Record<string, JsonValue> | undefined
): CameraBindingHint | null {
  const raw = data?.[CAMERA_BINDING_HINT_KEY];
  if (raw === null || raw === undefined || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const { cameraSourceId, cameraName, sourceDeviceId } = raw as Record<string, JsonValue>;
  if (
    typeof cameraSourceId !== 'string' ||
    typeof cameraName !== 'string' ||
    typeof sourceDeviceId !== 'string'
  ) {
    return null;
  }
  return { cameraSourceId, cameraName, sourceDeviceId };
}

// --------------------------------------------------------------------------
// Control selection (which parameters render the reference control)
// --------------------------------------------------------------------------

/**
 * Whether a parameter renders as the camera reference control: the
 * `icam_source` node's `device` parameter, keyed off node type +
 * parameter name — no `workflow_core` schema change (Requirement 7.1;
 * csi-icam-input-nodes Requirement 5.2), and the `aravis_camera_source`
 * node's `camera_id` parameter (aravis-camera-input Requirement 3.1).
 * The `csi_camera_source` node's `gain`/`exposure` parameters are NOT
 * camera-reference parameters — they render as plain numeric inputs
 * (csi-icam-input-nodes Requirement 5.3).
 */
export function isCameraReferenceParameter(typeId: string, parameterName: string): boolean {
  return (
    (typeId === 'icam_source' && parameterName === 'device') ||
    (typeId === 'aravis_camera_source' && parameterName === 'camera_id') ||
    // rtsp-rtmp-stream-cameras Requirement 3.2: a stream node's `url`.
    (Object.prototype.hasOwnProperty.call(STREAM_CAMERA_SOURCE_TYPES, typeId) &&
      parameterName === 'url')
  );
}

// --------------------------------------------------------------------------
// Pure selection application (Requirement 7.2; Property 11 target)
// --------------------------------------------------------------------------

/** The Camera_Source's display name: its name, falling back to its id. */
export function cameraDisplayName(camera: CameraSourceEntry): string {
  const name = camera.name;
  return typeof name === 'string' && name !== '' ? name : camera.camera_source_id;
}

/**
 * The device path or URL a Camera_Source resolves to: `devicePath`
 * when present, else `url`, else null (Requirement 7.4 display and the
 * value written into the node's `device` parameter).
 */
export function cameraDeviceValue(camera: CameraSourceEntry): string | null {
  const params = camera.params ?? {};
  const devicePath = params.devicePath;
  if (typeof devicePath === 'string' && devicePath !== '') {
    return devicePath;
  }
  const url = params.url;
  if (typeof url === 'string' && url !== '') {
    return url;
  }
  return null;
}

/** Result of applying a Camera_Source selection to a node. */
export interface CameraSelectionResult {
  /** The node's updated parameters record. */
  parameters: Record<string, JsonValue>;
  /** The advisory hint to store as `data.cameraBindingHint`. */
  hint: CameraBindingHint;
}

/**
 * Apply a Camera_Source selection to a Camera_Input_Node's parameters
 * (Requirement 7.2): populates `device` from the source's device path
 * or URL (existing value retained when the source carries neither),
 * copies `gain` and `exposure` when present in the source's params, and
 * produces the advisory binding hint recording the selection. Pure over
 * its inputs — the caller stores the hint on the node's `data`.
 */
export function applyCameraSelection(
  parameters: Record<string, JsonValue>,
  camera: CameraSourceEntry,
  sourceDeviceId: string
): CameraSelectionResult {
  const next: Record<string, JsonValue> = { ...parameters };
  const device = cameraDeviceValue(camera);
  if (device !== null) {
    next.device = device;
  }
  const params = camera.params ?? {};
  if (typeof params.gain === 'number') {
    next.gain = params.gain;
  }
  if (typeof params.exposure === 'number') {
    next.exposure = params.exposure;
  }
  return {
    parameters: next,
    hint: {
      cameraSourceId: camera.camera_source_id,
      cameraName: cameraDisplayName(camera),
      sourceDeviceId,
    },
  };
}

// --------------------------------------------------------------------------
// ICAM (V4L2 smart camera) picker helpers
// (csi-icam-input-nodes Requirement 5.2)
// --------------------------------------------------------------------------

/**
 * Whether a Camera_Source is V4L2-compatible for the `icam_source`
 * picker (csi-icam-input-nodes Requirement 5.2): a smart camera
 * (type `ICam`), a discovered V4L2 device (type `V4L2Discovered`), or a
 * configured `Camera`-type Image_Source carrying a device path. Mirrors
 * the deploy-time compatible set {ICam, V4L2Discovered, Camera} so the
 * picker never offers a source the validator would reject.
 */
export function isV4l2CompatibleCamera(camera: CameraSourceEntry): boolean {
  if (camera.type === 'ICam' || camera.type === 'V4L2Discovered') {
    return true;
  }
  return camera.type === 'Camera' && cameraDeviceValue(camera) !== null;
}

// --------------------------------------------------------------------------
// Aravis picker helpers (aravis-camera-input Requirements 3.2, 3.3)
// --------------------------------------------------------------------------

/**
 * Whether a Camera_Source is Aravis-compatible for the
 * `aravis_camera_source` picker (Requirement 3.2): a discovered bus
 * camera (type `AravisDiscovered`), a configured `Camera`-type
 * Image_Source carrying a non-empty string `cameraId` parameter, or the
 * registry-backed Static_Image_Camera entry (type `StaticImage`) — the
 * device serves the static camera through the same aravis frame-feed
 * path bus cameras use (see the static-image-camera-source base spec;
 * cloud-static-camera-provisioning Requirements 6.3, 6.4). The
 * Static_Video_Camera entry (type `StaticVideo`) is served the same way
 * (static-camera-video-loop Requirement 4.8). Mirrors the deploy-time
 * compatible set {Camera, AravisDiscovered, StaticImage, StaticVideo} so
 * the picker never offers a source the validator would reject.
 */
export function isAravisCompatibleCamera(camera: CameraSourceEntry): boolean {
  if (
    camera.type === 'AravisDiscovered' ||
    camera.type === 'StaticImage' ||
    camera.type === 'StaticVideo'
  ) {
    return true;
  }
  return camera.type === 'Camera' && cameraIdValue(camera) !== null;
}

/**
 * The Static_Image_Camera's enumeration id as the device reports it
 * inside the entry's capability metadata — `capabilities.staticImage.id`
 * as a non-empty string, else null
 * (static-image-camera-binding-and-pin-discoverability Requirement 2.1).
 *
 * The device reports the static camera with an EMPTY `params` block and
 * its identity under `capabilities.staticImage`
 * (`_static_image_entry()` in `src/backend/camera_sync/inventory.py`),
 * which is the shipped, hardware-verified inventory contract. The id is
 * read from that block rather than compared against a hardcoded
 * `'static-image-camera'`, so a future change to the device's fixed
 * enumeration identity flows through.
 *
 * Registry payloads are external input, so the lookup is guarded the
 * way `getCameraBindingHint` guards the advisory hint: a null, array, or
 * non-object `staticImage`, or a non-string / empty `id`, resolves null
 * instead of throwing.
 */
function staticImageCapabilityId(camera: CameraSourceEntry): string | null {
  return capabilityBlockId(camera, 'staticImage');
}

/**
 * The Static_Video_Camera's id from `capabilities.staticVideo.id`, guarded
 * the same way (static-camera-video-loop Requirement 4.8).
 */
function staticVideoCapabilityId(camera: CameraSourceEntry): string | null {
  return capabilityBlockId(camera, 'staticVideo');
}

/** `capabilities[family].id` as a non-empty string, else null. */
function capabilityBlockId(
  camera: CameraSourceEntry,
  family: 'staticImage' | 'staticVideo'
): string | null {
  const block = (camera.capabilities ?? {})[family];
  if (
    block === null ||
    block === undefined ||
    typeof block !== 'object' ||
    Array.isArray(block)
  ) {
    return null;
  }
  const id = (block as Record<string, JsonValue>).id;
  return typeof id === 'string' && id !== '' ? id : null;
}

/**
 * The Aravis camera id a Camera_Source resolves to (`params.cameraId`
 * as a non-empty string), or null when the source carries none
 * (Requirement 3.3 population and 3.5 display).
 *
 * `params.cameraId` is resolved FIRST and unchanged; a `StaticImage`
 * entry that carries no usable one falls back to its capabilities
 * identity, so the Static_Image_Camera the picker already offers binds
 * instead of leaving `camera_id` unset
 * (static-image-camera-binding-and-pin-discoverability Requirements 2.1,
 * 2.2, 3.1). The fallback is scoped to `StaticImage` deliberately:
 * `isAravisCompatibleCamera()` resolves ids in its `Camera` arm, so a
 * type-agnostic fallback would let a `Camera` entry carrying a
 * StaticImage capabilities block become Aravis-compatible and widen the
 * offered set past the deploy-time compatible set (Requirement 3.4).
 * `StaticImage` is already unconditionally compatible, so the type gate
 * costs this fix nothing.
 */
export function cameraIdValue(camera: CameraSourceEntry): string | null {
  const cameraId = (camera.params ?? {}).cameraId;
  if (typeof cameraId === 'string' && cameraId !== '') {
    return cameraId;
  }
  if (camera.type === 'StaticImage') {
    return staticImageCapabilityId(camera);
  }
  // The Static_Video_Camera reports the same shape under
  // `capabilities.staticVideo` (static-camera-video-loop Requirement 4.8),
  // type-gated for the same reason.
  if (camera.type === 'StaticVideo') {
    return staticVideoCapabilityId(camera);
  }
  return null;
}

/**
 * Apply an Aravis_Camera_Source selection to an Aravis_Camera_Source_Node's
 * parameters (Requirement 3.3): populates `camera_id` from the source's
 * camera id parameter (existing value retained when the source carries
 * none), copies `gain` and `exposure` when numerically present in the
 * source's params, leaves all other parameters untouched, and produces
 * the standard advisory binding hint. Pure over its inputs — mirrors
 * `applyCameraSelection`.
 */
export function applyAravisCameraSelection(
  parameters: Record<string, JsonValue>,
  camera: CameraSourceEntry,
  sourceDeviceId: string
): CameraSelectionResult {
  const next: Record<string, JsonValue> = { ...parameters };
  const cameraId = cameraIdValue(camera);
  if (cameraId !== null) {
    next.camera_id = cameraId;
  }
  const params = camera.params ?? {};
  if (typeof params.gain === 'number') {
    next.gain = params.gain;
  }
  if (typeof params.exposure === 'number') {
    next.exposure = params.exposure;
  }
  return {
    parameters: next,
    hint: {
      cameraSourceId: camera.camera_source_id,
      cameraName: cameraDisplayName(camera),
      sourceDeviceId,
    },
  };
}

// --------------------------------------------------------------------------
// Stream camera picker helpers (rtsp-rtmp-stream-cameras Requirements
// 3.3, 3.4, 3.6; Properties 7 and 8)
// --------------------------------------------------------------------------

/**
 * The Camera_Source type each Stream_Camera_Source_Node type binds to:
 * the protocol must match (Requirement 3.3), mirroring the deploy-time
 * compatible sets in `deployments.py`.
 */
export const STREAM_CAMERA_SOURCE_TYPES: Readonly<Record<string, string>> = {
  rtsp_camera_source: 'RTSP',
  rtmp_stream_source: 'RTMP',
};

/** Whether `camera` may be offered for a stream node of type `typeId`. */
export function isStreamCompatibleCamera(typeId: string, camera: CameraSourceEntry): boolean {
  if (!Object.prototype.hasOwnProperty.call(STREAM_CAMERA_SOURCE_TYPES, typeId)) {
    return false;
  }
  return camera.type === STREAM_CAMERA_SOURCE_TYPES[typeId];
}

/** A stream Camera_Source's Stream_URL (`params.url`), or null. */
export function streamUrlValue(camera: CameraSourceEntry): string | null {
  const url = (camera.params ?? {}).url;
  return typeof url === 'string' && url !== '' ? url : null;
}

/**
 * Apply a stream Camera_Source selection to a Stream_Camera_Source_Node
 * (Requirement 3.4): sets `url` to the entry's Stream_URL and nothing
 * else, and produces the standard binding hint. No other key or value is
 * copied from the entry, so no credential material, reference, or
 * setting can reach the workflow (Requirement 3.6). Pure over its inputs.
 */
export function applyStreamCameraSelection(
  parameters: Record<string, JsonValue>,
  camera: CameraSourceEntry,
  sourceDeviceId: string
): CameraSelectionResult {
  const next: Record<string, JsonValue> = { ...parameters };
  const url = streamUrlValue(camera);
  if (url !== null) {
    next.url = url;
  }
  return {
    parameters: next,
    hint: {
      cameraSourceId: camera.camera_source_id,
      cameraName: cameraDisplayName(camera),
      sourceDeviceId,
    },
  };
}

/** What the picker and the Cameras tab show about a stream camera. */
export interface StreamCameraDetails {
  /** `H.264` / `H.265` (or the reported codec as written), when reported. */
  codec: string | null;
  /** `width×height`, when reported. */
  resolution: string | null;
  /** Coarse Stream_Health: streaming, reconnecting, failed or idle. */
  health: string | null;
  /** The decoder path in use: hardware or software. */
  decoder: string | null;
  credentialsConfigured: boolean;
}

const CODEC_LABELS: Readonly<Record<string, string>> = { h264: 'H.264', h265: 'H.265' };

/**
 * The display label of a reported codec name: `H.264` / `H.265`, or the
 * name as written. Own-property lookup, so a reported name such as
 * `constructor` displays as written rather than resolving to an
 * Object.prototype member.
 */
export function streamCodecLabel(codec: string): string {
  return Object.prototype.hasOwnProperty.call(CODEC_LABELS, codec) ? CODEC_LABELS[codec] : codec;
}

function positiveInteger(value: JsonValue | undefined): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * The reported stream details of a Camera_Source, read from
 * `capabilities.stream` (Requirement 16.4) and the credential state
 * (Requirement 5.7). Registry payloads are external input, so malformed
 * values resolve to null instead of throwing.
 */
export function streamCameraDetails(camera: CameraSourceEntry): StreamCameraDetails {
  const raw = (camera.capabilities ?? {}).stream;
  const stream =
    raw !== null && raw !== undefined && typeof raw === 'object' && !Array.isArray(raw)
      ? (raw as Record<string, JsonValue>)
      : {};
  const text = (value: JsonValue | undefined) =>
    typeof value === 'string' && value !== '' ? value : null;
  const codec = text(stream.codec);
  const width = positiveInteger(stream.width);
  const height = positiveInteger(stream.height);
  const configured =
    camera.credentials?.configured ?? (camera.params ?? {}).credentialsConfigured === true;
  return {
    codec: codec === null ? null : streamCodecLabel(codec),
    resolution: width !== null && height !== null ? `${width}\u00d7${height}` : null,
    health: text(stream.state),
    decoder: text(stream.decoder),
    credentialsConfigured: configured === true,
  };
}

// --------------------------------------------------------------------------
// Manual entry default (Requirement 7.3)
// --------------------------------------------------------------------------

/**
 * Whether the control starts in manual-entry mode: a node whose device
 * value was typed by hand (an explicit, non-empty value different from
 * the declared default) and that carries no binding hint keeps its
 * plain text input; nodes carrying a hint or still on the declared
 * default start on the reference picker.
 */
export function defaultManualEntry(
  parameters: Record<string, JsonValue>,
  parameterName: string,
  declaredDefault: JsonValue | null | undefined,
  hint: CameraBindingHint | null
): boolean {
  if (hint !== null) {
    return false;
  }
  if (!Object.prototype.hasOwnProperty.call(parameters, parameterName)) {
    return false;
  }
  const value = parameters[parameterName];
  if (value === null || value === '') {
    return false;
  }
  return value !== (declaredDefault ?? null);
}
