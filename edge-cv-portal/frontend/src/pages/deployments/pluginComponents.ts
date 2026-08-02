/**
 * Plugin_Component support for the deployment screen
 * (custom-node-designer task 12.6, Requirements 16.2, 16.3, 16.6).
 *
 * Pure helpers for recognizing `dda.plugin.*` Node Designer
 * Plugin_Components in the component listing (16.2) and for parsing the
 * pre-submit deployment gate rejections the backend returns with the
 * distinct `PLUGIN_LIFECYCLE_VIOLATION` (16.3) and
 * `PLUGIN_ARCH_UNSUPPORTED` (16.6) error codes (deployments.py).
 */
import { ARCHITECTURE_LABELS, DeviceArchitecture } from '../node-designer/types';

/** Keep in sync with plugin_components.py PLUGIN_COMPONENT_PREFIX. */
export const PLUGIN_COMPONENT_PREFIX = 'dda.plugin.';

/** True when a Greengrass component is a Node Designer Plugin_Component. */
export function isPluginComponent(componentName: string | null | undefined): boolean {
  return typeof componentName === 'string' && componentName.startsWith(PLUGIN_COMPONENT_PREFIX);
}

/** Human-readable label for a DDA Target_Architecture chip (16.2). */
export function architectureLabel(arch: string): string {
  return ARCHITECTURE_LABELS[arch as DeviceArchitecture] ?? arch;
}

// ---------------------------------------------------------------------------
// Pre-submit gate rejections (16.3, 16.6)
// ---------------------------------------------------------------------------

/**
 * One entry of the `PLUGIN_LIFECYCLE_VIOLATION` details.violations list:
 * a Plugin_Component whose backing Plugin_Record Lifecycle_State forbids
 * deployment to the listed target devices (16.3).
 */
export interface PluginLifecycleViolation {
  pluginComponent: string;
  lifecycleState: string | null;
  devices: string[];
  version?: string;
}

/**
 * One entry of the `PLUGIN_ARCH_UNSUPPORTED` details.unsupported list: a
 * device whose recorded Target_Architecture has no published
 * Plugin_Artifact in the depended-on Plugin_Component version (16.6).
 */
export interface PluginArchUnsupported {
  pluginComponent: string;
  version: string;
  device: string;
  deviceArch: string | null;
}

export interface PluginGateRejection {
  code: 'PLUGIN_LIFECYCLE_VIOLATION' | 'PLUGIN_ARCH_UNSUPPORTED';
  message: string;
  lifecycleViolations: PluginLifecycleViolation[];
  archUnsupported: PluginArchUnsupported[];
}

/**
 * Parse a structured API error into a Plugin_Component gate rejection,
 * or null when the error is not one of the two plugin gate codes. The
 * backend envelope is {error: {code, message, details}} with
 * details.violations (lifecycle) or details.unsupported (architecture).
 */
export function parsePluginGateRejection(
  code: string | undefined,
  message: string,
  details: Record<string, unknown> | undefined
): PluginGateRejection | null {
  if (code === 'PLUGIN_LIFECYCLE_VIOLATION') {
    const raw = Array.isArray(details?.violations) ? details.violations : [];
    return {
      code,
      message,
      lifecycleViolations: raw
        .filter((v): v is Record<string, unknown> => !!v && typeof v === 'object')
        .map((v) => ({
          pluginComponent: String(v.pluginComponent ?? 'unknown component'),
          lifecycleState: v.lifecycleState == null ? null : String(v.lifecycleState),
          devices: Array.isArray(v.devices) ? v.devices.map(String) : [],
          version: v.version == null ? undefined : String(v.version),
        })),
      archUnsupported: [],
    };
  }
  if (code === 'PLUGIN_ARCH_UNSUPPORTED') {
    const raw = Array.isArray(details?.unsupported) ? details.unsupported : [];
    return {
      code,
      message,
      lifecycleViolations: [],
      archUnsupported: raw
        .filter((u): u is Record<string, unknown> => !!u && typeof u === 'object')
        .map((u) => ({
          pluginComponent: String(u.pluginComponent ?? 'unknown component'),
          version: String(u.version ?? 'unknown'),
          device: String(u.device ?? 'unknown device'),
          deviceArch: u.deviceArch == null ? null : String(u.deviceArch),
        })),
    };
  }
  return null;
}

/**
 * One-line description of a lifecycle violation identifying the
 * Plugin_Component, its Lifecycle_State, and the offending devices (16.3).
 */
export function describeLifecycleViolation(v: PluginLifecycleViolation): string {
  const version = v.version ? ` v${v.version}` : '';
  const state = v.lifecycleState ?? 'unknown';
  const deviceList = v.devices.length > 0 ? v.devices.join(', ') : 'the requested target devices';
  if (state === 'test') {
    return (
      `${v.pluginComponent}${version} is in the "test" lifecycle state and can only be ` +
      `deployed to devices flagged as test devices. Not permitted for: ${deviceList}`
    );
  }
  return (
    `${v.pluginComponent}${version} is in the "${state}" lifecycle state and is not ` +
    `deployable. Rejected for: ${deviceList}`
  );
}

/**
 * One-line description of an architecture mismatch identifying the
 * Plugin_Component version and the unsupported device architecture (16.6).
 */
export function describeArchUnsupported(u: PluginArchUnsupported): string {
  const arch = u.deviceArch
    ? `architecture ${architectureLabel(u.deviceArch)}`
    : 'an unknown architecture';
  return (
    `${u.pluginComponent} v${u.version} has no published build for device ` +
    `"${u.device}" (${arch})`
  );
}
