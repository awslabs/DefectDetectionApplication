/**
 * Deploy-time Camera_Binding support for the CreateDeployment page
 * (camera-registry-sync task 12.1 — Requirements 8.1, 8.4, 8.5, 8.7,
 * 8.8, 8.9, 9.2, 9.3).
 *
 * Pure helpers behind the nodes × target-devices binding matrix:
 * recognizing packaged Workflow_Components (`dda.workflow.*`) in the
 * component selection, shaping the per-cell binding state seeded from
 * the binding-context endpoint's hint pre-selection (8.5), building the
 * `camera_bindings` request payload, computing the warning set whose
 * confirmation checkboxes feed `confirmed_warnings` (8.8, 9.3), and
 * parsing the structured 409/503/502 rejections of the submission path
 * (deployments.py). Kept free of React/DOM so the task 12.2 component
 * and property tests can target the logic directly.
 */
import type { JsonValue } from '../workflows/types';
import type { CameraSourceEntry } from '../workflows/cameraReference';
import { cameraDeviceValue, cameraDisplayName } from '../workflows/cameraReference';

/** Keep in sync with deployments.py WORKFLOW_COMPONENT_PREFIX. */
export const WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.';

/** True when a Greengrass component is a packaged Workflow_Component. */
export function isWorkflowComponent(componentName: string | null | undefined): boolean {
  return (
    typeof componentName === 'string' &&
    componentName.startsWith(WORKFLOW_COMPONENT_PREFIX) &&
    componentName.length > WORKFLOW_COMPONENT_PREFIX.length
  );
}

/** A selected Workflow_Component resolved to its workflow identity. */
export interface WorkflowComponentRef {
  workflowId: string;
  /**
   * Always null: the Greengrass component version no longer maps to the
   * workflow version. Re-packaging a workflow bumps the component MAJOR
   * (N.0.0) to force Greengrass to re-install on-device — like the model
   * components — so the component version's major is a Greengrass revision
   * counter, NOT the workflow version. The true workflow version travels in
   * the manifest/recipe (`workflowVersion`); the binding context endpoint
   * resolves the workflow's latest version when none is pinned, which is what
   * a freshly (re-)packaged deployable component always is.
   */
  workflowVersion: number | null;
}

/**
 * Parse a component selection into a Workflow_Component reference, or
 * null when the component is not a `dda.workflow.*` component.
 *
 * ``workflowVersion`` is intentionally always null (see WorkflowComponentRef):
 * the component version's major is no longer the workflow version, so callers
 * omit the version and let the backend resolve the workflow's latest.
 */
export function parseWorkflowComponent(
  componentName: string | null | undefined,
  // Accepted for call-site compatibility; no longer used to derive the
  // workflow version now that re-packaging bumps the component major.
  _componentVersion?: string | null
): WorkflowComponentRef | null {
  if (!isWorkflowComponent(componentName)) {
    return null;
  }
  const workflowId = (componentName as string).slice(WORKFLOW_COMPONENT_PREFIX.length);
  return { workflowId, workflowVersion: null };
}

// ---------------------------------------------------------------------------
// Binding context wire shapes (GET /deployments?view=binding-context)
// ---------------------------------------------------------------------------

/** One Camera_Input_Node of the workflow version (8.1). */
export interface BindingContextNode {
  node_id: string;
  node_type: string;
  /** The advisory hint recorded by the Workflow_Builder (7.2, 8.5). */
  binding_hint?: Record<string, JsonValue>;
}

/** One target Edge_Device's registered Camera_Sources (8.1, 8.8). */
export interface BindingContextTarget {
  /** Explicit never-synced state, never a bare empty list (1.6, 8.8). */
  state: 'synced' | 'never-synced';
  cameras: CameraSourceEntry[];
  /** Hint-matching pre-selection per node id (8.5). */
  preselected: Record<string, string>;
}

/** Response of `GET /deployments?view=binding-context`. */
export interface CameraBindingContext {
  workflow_id: string;
  workflow_version: number;
  has_binding_points: boolean;
  /** The matrix-step discriminator: false skips the step entirely (8.9). */
  binding_required: boolean;
  camera_input_nodes: BindingContextNode[];
  targets: Record<string, BindingContextTarget>;
}

// ---------------------------------------------------------------------------
// Per-cell binding state (nodes × target devices)
// ---------------------------------------------------------------------------

/**
 * One cell of the binding matrix: a registered Camera_Source selection
 * (`suggested` marks an unconfirmed hint pre-selection the user can see
 * and change, 8.5), a manual override carrying explicit parameter values
 * (device path at minimum, 8.4), or still unbound (8.7).
 */
export type BindingCell =
  | { mode: 'camera'; cameraSourceId: string; suggested: boolean }
  | { mode: 'override'; device: string }
  | { mode: 'unbound' };

/** {thing name: {node id: cell}} — distinct per-device bindings (8.3). */
export type BindingSelections = Record<string, Record<string, BindingCell>>;

const UNBOUND: BindingCell = { mode: 'unbound' };

/** The cell of one node on one device, defaulting to unbound. */
export function getBindingCell(
  selections: BindingSelections,
  device: string,
  nodeId: string
): BindingCell {
  return selections[device]?.[nodeId] ?? UNBOUND;
}

/**
 * The initial matrix state for a binding context: hint pre-selections
 * are pre-filled as suggested camera selections awaiting the user's
 * confirmation or change (8.5); never-synced targets start in manual
 * override mode because registered-source selection is unavailable for
 * them (8.8); everything else starts unbound. Cells the user already
 * touched (in `previous`) are retained when their node and device are
 * still part of the context.
 */
export function initialBindingSelections(
  context: CameraBindingContext,
  previous?: BindingSelections
): BindingSelections {
  const selections: BindingSelections = {};
  for (const [device, target] of Object.entries(context.targets)) {
    const deviceCells: Record<string, BindingCell> = {};
    for (const node of context.camera_input_nodes) {
      const kept = previous?.[device]?.[node.node_id];
      if (kept && !(kept.mode === 'camera' && kept.suggested)) {
        // A user-made choice survives context refreshes; suggestions
        // are re-derived from the fresh context below.
        deviceCells[node.node_id] = kept;
        continue;
      }
      const preselected = target.preselected?.[node.node_id];
      if (target.state === 'never-synced') {
        deviceCells[node.node_id] = { mode: 'override', device: '' };
      } else if (typeof preselected === 'string' && preselected !== '') {
        deviceCells[node.node_id] = {
          mode: 'camera',
          cameraSourceId: preselected,
          suggested: true,
        };
      } else {
        deviceCells[node.node_id] = UNBOUND;
      }
    }
    selections[device] = deviceCells;
  }
  return selections;
}

/** An immutable update of one matrix cell. */
export function withBindingCell(
  selections: BindingSelections,
  device: string,
  nodeId: string,
  cell: BindingCell
): BindingSelections {
  return {
    ...selections,
    [device]: { ...(selections[device] ?? {}), [nodeId]: cell },
  };
}

/**
 * The `camera_bindings` request payload (deployments.py contract):
 * {thing: {node_id: {cameraSourceId} | {override: {device}}}}. A
 * suggested pre-selection counts as a selection (8.5 — the user saw it
 * in the matrix and left it in place); unbound cells and overrides
 * without a device path are omitted (the backend rejects them as
 * unbound, 8.7).
 */
export function buildCameraBindings(
  selections: BindingSelections
): Record<string, Record<string, { cameraSourceId: string } | { override: Record<string, JsonValue> }>> {
  const payload: Record<
    string,
    Record<string, { cameraSourceId: string } | { override: Record<string, JsonValue> }>
  > = {};
  for (const [device, cells] of Object.entries(selections)) {
    for (const [nodeId, cell] of Object.entries(cells)) {
      if (cell.mode === 'camera' && cell.cameraSourceId !== '') {
        (payload[device] ??= {})[nodeId] = { cameraSourceId: cell.cameraSourceId };
      } else if (cell.mode === 'override' && cell.device.trim() !== '') {
        (payload[device] ??= {})[nodeId] = { override: { device: cell.device.trim() } };
      }
    }
  }
  return payload;
}

/** One cell still lacking a binding, identified by node and device (8.7). */
export interface UnboundCell {
  device: string;
  nodeId: string;
}

/**
 * The matrix cells that would be rejected as unbound on submission: a
 * Camera_Input_Node with neither a selected Camera_Source nor a manual
 * override with a device path, on any target device (8.7).
 */
export function unboundCells(
  context: CameraBindingContext,
  selections: BindingSelections
): UnboundCell[] {
  const unbound: UnboundCell[] = [];
  for (const device of Object.keys(context.targets)) {
    for (const node of context.camera_input_nodes) {
      const cell = getBindingCell(selections, device, node.node_id);
      const bound =
        (cell.mode === 'camera' && cell.cameraSourceId !== '') ||
        (cell.mode === 'override' && cell.device.trim() !== '');
      if (!bound) {
        unbound.push({ device, nodeId: node.node_id });
      }
    }
  }
  return unbound;
}

// ---------------------------------------------------------------------------
// Warning confirmation (8.8, 9.3) — checkbox list feeding confirmed_warnings
// ---------------------------------------------------------------------------

/**
 * One warning requiring explicit confirmation before submission. The
 * shape mirrors validate_camera_bindings' warning entries; `id` is the
 * value submitted in `confirmed_warnings`.
 */
export interface CameraBindingWarning {
  id: string;
  code: string;
  device: string;
  nodeId?: string;
  cameraSourceId?: string;
  conditions?: string[];
  message: string;
}

/**
 * The Requirement 9.3 degraded conditions of a registry entry, in the
 * backend's order. Keep in sync with deployments.py
 * _degraded_source_conditions.
 */
function degradedConditions(entry: CameraSourceEntry): string[] {
  const conditions: string[] = [];
  if (entry.absent) conditions.push('absent');
  if (entry.stale) conditions.push('stale');
  if (entry.sync_status === 'pending' || entry.sync_status === 'failed') {
    conditions.push(entry.sync_status);
  }
  return conditions;
}

/**
 * The warnings the current matrix state will produce on submission,
 * with ids matching validate_camera_bindings so their confirmation
 * checkboxes feed `confirmed_warnings` directly (8.8, 9.3):
 *
 * - `never-synced:{device}` for a never-synced target with an empty
 *   registry (bindings restricted to manual override, 8.8), and
 * - `camera-degraded:{device}:{node}:{csid}:{cond+cond}` for a bound
 *   source that is absent, stale, or sync-status pending/failed (9.3).
 *
 * The backend remains authoritative: any warning it returns that was
 * not predicted here is surfaced from the 409 rejection and confirmed
 * the same way.
 */
export function expectedBindingWarnings(
  context: CameraBindingContext,
  selections: BindingSelections
): CameraBindingWarning[] {
  const warnings: CameraBindingWarning[] = [];
  for (const device of Object.keys(context.targets).sort()) {
    const target = context.targets[device];
    if (target.state === 'never-synced' && target.cameras.length === 0) {
      warnings.push({
        id: `never-synced:${device}`,
        code: 'DEVICE_NEVER_SYNCED',
        device,
        message:
          `Device '${device}' has never completed a camera registry ` +
          `synchronization; camera bindings are restricted to manual override`,
      });
    }
    const cameraById = new Map(target.cameras.map((c) => [c.camera_source_id, c]));
    for (const node of context.camera_input_nodes) {
      const cell = getBindingCell(selections, device, node.node_id);
      if (cell.mode !== 'camera' || cell.cameraSourceId === '') {
        continue;
      }
      const entry = cameraById.get(cell.cameraSourceId);
      if (!entry) {
        continue; // missing source is an error (9.2), not a warning
      }
      const conditions = degradedConditions(entry);
      if (conditions.length > 0) {
        warnings.push({
          id:
            `camera-degraded:${device}:${node.node_id}:` +
            `${cell.cameraSourceId}:${conditions.join('+')}`,
          code: 'CAMERA_SOURCE_DEGRADED',
          device,
          nodeId: node.node_id,
          cameraSourceId: cell.cameraSourceId,
          conditions,
          message:
            `Camera source '${cell.cameraSourceId}' bound to node ` +
            `'${node.node_id}' on device '${device}' is ${conditions.join(', ')}`,
        });
      }
    }
  }
  return warnings;
}

// ---------------------------------------------------------------------------
// Submission rejections (deployments.py error envelope)
// ---------------------------------------------------------------------------

/**
 * One validation error of a 409 CAMERA_BINDINGS_INVALID rejection,
 * identifying the node and device (8.7, 9.2, 9.4, 8.4).
 */
export interface CameraBindingIssue {
  code: string;
  device: string;
  nodeId?: string;
  cameraSourceId?: string;
  parameter?: string;
  message: string;
}

export type CameraBindingRejectionCode =
  | 'CAMERA_BINDINGS_INVALID'
  | 'CAMERA_WARNINGS_UNCONFIRMED'
  | 'REGISTRY_UNAVAILABLE'
  | 'BINDING_DELIVERY_FAILED';

export interface CameraBindingRejection {
  code: CameraBindingRejectionCode;
  message: string;
  errors: CameraBindingIssue[];
  warnings: CameraBindingWarning[];
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((v): v is Record<string, unknown> => !!v && typeof v === 'object')
    : [];
}

function toIssue(raw: Record<string, unknown>): CameraBindingIssue {
  return {
    code: String(raw.code ?? 'CAMERA_BINDING_ERROR'),
    device: String(raw.device ?? 'unknown device'),
    nodeId: raw.nodeId == null ? undefined : String(raw.nodeId),
    cameraSourceId: raw.cameraSourceId == null ? undefined : String(raw.cameraSourceId),
    parameter: raw.parameter == null ? undefined : String(raw.parameter),
    message: String(raw.message ?? 'Invalid camera binding'),
  };
}

function toWarning(raw: Record<string, unknown>): CameraBindingWarning {
  return {
    id: String(raw.id ?? ''),
    code: String(raw.code ?? 'CAMERA_BINDING_WARNING'),
    device: String(raw.device ?? 'unknown device'),
    nodeId: raw.nodeId == null ? undefined : String(raw.nodeId),
    cameraSourceId: raw.cameraSourceId == null ? undefined : String(raw.cameraSourceId),
    conditions: Array.isArray(raw.conditions) ? raw.conditions.map(String) : undefined,
    message: String(raw.message ?? 'Camera binding warning'),
  };
}

/**
 * Parse a structured API error into a camera binding rejection, or null
 * when the error is not one of the submission path's binding codes:
 * 409 CAMERA_BINDINGS_INVALID {errors, warnings}, 409
 * CAMERA_WARNINGS_UNCONFIRMED {warnings}, 503 REGISTRY_UNAVAILABLE, and
 * 502 BINDING_DELIVERY_FAILED.
 */
export function parseCameraBindingRejection(
  code: string | undefined,
  message: string,
  details: Record<string, unknown> | undefined
): CameraBindingRejection | null {
  if (
    code !== 'CAMERA_BINDINGS_INVALID' &&
    code !== 'CAMERA_WARNINGS_UNCONFIRMED' &&
    code !== 'REGISTRY_UNAVAILABLE' &&
    code !== 'BINDING_DELIVERY_FAILED'
  ) {
    return null;
  }
  return {
    code,
    message,
    errors: asRecordArray(details?.errors).map(toIssue),
    warnings: asRecordArray(details?.warnings)
      .map(toWarning)
      .filter((w) => w.id !== ''),
  };
}

/**
 * One-line description of a binding validation error naming the node
 * and device (8.7, 9.2 rejection surfacing).
 */
export function describeBindingIssue(issue: CameraBindingIssue): string {
  const where = issue.nodeId
    ? `Node '${issue.nodeId}' on device '${issue.device}'`
    : `Device '${issue.device}'`;
  return `${where}: ${issue.message}`;
}

// ---------------------------------------------------------------------------
// Dropdown display (8.1 selectable options with 7.4-style fields)
// ---------------------------------------------------------------------------

/** The dropdown label of a Camera_Source option: its display name. */
export function cameraOptionLabel(camera: CameraSourceEntry): string {
  return cameraDisplayName(camera);
}

/** The dropdown description: type and device path or URL. */
export function cameraOptionDescription(camera: CameraSourceEntry): string {
  const parts: string[] = [];
  if (camera.type) parts.push(String(camera.type));
  const device = cameraDeviceValue(camera);
  if (device) parts.push(device);
  return parts.join(' • ');
}

/** Status tags of a Camera_Source option (stale/absent/sync status). */
export function cameraOptionTags(camera: CameraSourceEntry): string[] {
  const tags: string[] = [];
  if (camera.absent) tags.push('absent');
  if (camera.stale) tags.push('stale');
  if (camera.sync_status && camera.sync_status !== 'synced') {
    tags.push(camera.sync_status);
  }
  return tags;
}
