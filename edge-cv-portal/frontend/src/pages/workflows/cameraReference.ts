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

/** Create/update body for portal-managed Camera_Sources (Req 5.1). */
export interface CameraSourceMutationBody {
  name: string;
  type: string;
  params?: Record<string, JsonValue>;
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
    (typeId === 'aravis_camera_source' && parameterName === 'camera_id')
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
 * camera (type `AravisDiscovered`), or a configured `Camera`-type
 * Image_Source carrying a non-empty string `cameraId` parameter.
 */
export function isAravisCompatibleCamera(camera: CameraSourceEntry): boolean {
  if (camera.type === 'AravisDiscovered') {
    return true;
  }
  return camera.type === 'Camera' && cameraIdValue(camera) !== null;
}

/**
 * The Aravis camera id a Camera_Source resolves to (`params.cameraId`
 * as a non-empty string), or null when the source carries none
 * (Requirement 3.3 population and 3.5 display).
 */
export function cameraIdValue(camera: CameraSourceEntry): string | null {
  const cameraId = (camera.params ?? {}).cameraId;
  return typeof cameraId === 'string' && cameraId !== '' ? cameraId : null;
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
