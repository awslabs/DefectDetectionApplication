/**
 * vLLM architecture gate — pure TS twin of the backend gate
 * (vllm-triton-inference task 15.3, Requirement 3.9).
 *
 * Structural twin of `evaluate_vllm_arch_gate` in
 * edge-cv-portal/backend/functions/deployments.py: pure over component
 * manifests ({name: {version, architectures}}) and device architectures
 * ({thing: arch | null}); architectures are matched by exact name with
 * no cross-architecture fallback; a device with no recorded
 * Target_Architecture (null/absent) fails closed; `arm64_jp4` misses
 * carry the JetPack-4-specific reason. The backend gate remains
 * authoritative — this twin only predicts its verdict so incompatible
 * devices can be warned about before submit.
 *
 * Kept free of React/UI imports so the fast-check property test
 * (task 15.5) can exercise it directly.
 */

/** Keep in sync with deployments.py VLLM_MODEL_COMPONENT_PREFIX. */
export const VLLM_MODEL_COMPONENT_PREFIX = 'model-vllm-';

/** Keep in sync with deployments.py VLLM_GATE_REASON_JP4 / _ARCH. */
export const VLLM_GATE_REASON_JP4 = 'JP4_UNSUPPORTED';
export const VLLM_GATE_REASON_ARCH = 'ARCH_UNSUPPORTED';

/** Keep in sync with deployments.py VLLM_JP4_UNSUPPORTED_MESSAGE. */
export const VLLM_JP4_UNSUPPORTED_MESSAGE =
  'JetPack 4 does not support vLLM inference';

/** True when a Greengrass component is a published vLLM_Model_Component. */
export function isVllmModelComponent(
  componentName: string | null | undefined
): boolean {
  return (
    typeof componentName === 'string' &&
    componentName.startsWith(VLLM_MODEL_COMPONENT_PREFIX)
  );
}

/**
 * One vLLM-bearing component's gate manifest: its version and the
 * supported Target_Architecture set. For model-vllm-* components the
 * set is resolved per component from the backing record's
 * `published_component` map by `vllmArchsForComponent` below (a
 * Per_JetPack_Component gates on its OWN architecture, a legacy
 * unsuffixed name on the record-wide set); an unresolvable record
 * contributes an empty set so every device fails closed — mirroring the
 * backend's `collect_vllm_component_manifests`.
 */
export interface VllmComponentManifest {
  version: string | null;
  architectures: string[];
}

/**
 * One (component, device) miss, shaped exactly like the backend gate's
 * entries (and the 409 VLLM_ARCH_UNSUPPORTED details.unsupported list).
 */
export interface VllmArchGateEntry {
  component: string;
  version: string | null;
  device: string;
  deviceArch: string | null;
  supported: string[];
  reason: typeof VLLM_GATE_REASON_JP4 | typeof VLLM_GATE_REASON_ARCH;
}

/**
 * Pure TS twin of `evaluate_vllm_arch_gate` (3.3-3.7, 3.9): each target
 * device's recorded Target_Architecture must appear in the supported
 * set of every vLLM-bearing component, by exact name, with no fallback.
 * A device whose architecture is null/absent fails closed. Returns []
 * when every device is covered, otherwise one entry per
 * (component, device) miss, with the JetPack-4 reason for `arm64_jp4`
 * devices and `ARCH_UNSUPPORTED` otherwise.
 */
export function evaluateVllmArchGate(
  componentManifests: Record<string, VllmComponentManifest>,
  deviceArchs: Record<string, string | null>
): VllmArchGateEntry[] {
  const offending: VllmArchGateEntry[] = [];
  for (const name of Object.keys(componentManifests).sort()) {
    const manifest = componentManifests[name];
    const supported = new Set(manifest.architectures ?? []);
    for (const device of Object.keys(deviceArchs).sort()) {
      const deviceArch = deviceArchs[device] ?? null;
      if (deviceArch === null || !supported.has(deviceArch)) {
        offending.push({
          component: name,
          version: manifest.version ?? null,
          device,
          deviceArch,
          supported: [...supported].sort(),
          reason:
            deviceArch === 'arm64_jp4'
              ? VLLM_GATE_REASON_JP4
              : VLLM_GATE_REASON_ARCH,
        });
      }
    }
  }
  return offending;
}

/**
 * One-line description of a gate entry: the device's recorded
 * architecture (or its absence) alongside the component's supported set
 * (3.9), with the JetPack-4 message for jp4 misses.
 */
export function describeVllmArchEntry(entry: VllmArchGateEntry): string {
  const version = entry.version ? ` v${entry.version}` : '';
  const arch = entry.deviceArch
    ? `recorded architecture "${entry.deviceArch}"`
    : 'no recorded architecture';
  const supported =
    entry.supported.length > 0
      ? `supported: ${entry.supported.join(', ')}`
      : 'no supported architectures recorded';
  const jp4 =
    entry.reason === VLLM_GATE_REASON_JP4
      ? ` — ${VLLM_JP4_UNSUPPORTED_MESSAGE}`
      : '';
  return (
    `Device "${entry.device}" (${arch}) is not supported by ` +
    `${entry.component}${version} (${supported})${jp4}`
  );
}

// ---------------------------------------------------------------------------
// Backend 409 VLLM_ARCH_UNSUPPORTED rejection parsing
// ---------------------------------------------------------------------------

export interface VllmGateRejection {
  code: 'VLLM_ARCH_UNSUPPORTED';
  message: string;
  unsupported: VllmArchGateEntry[];
}

/**
 * Parse a structured API error into the vLLM architecture gate
 * rejection the backend returns (409 VLLM_ARCH_UNSUPPORTED with
 * details.unsupported), or null when the error is something else.
 */
export function parseVllmGateRejection(
  code: string | undefined,
  message: string,
  details: Record<string, unknown> | undefined
): VllmGateRejection | null {
  if (code !== 'VLLM_ARCH_UNSUPPORTED') {
    return null;
  }
  const raw = Array.isArray(details?.unsupported) ? details.unsupported : [];
  return {
    code,
    message,
    unsupported: raw
      .filter((u): u is Record<string, unknown> => !!u && typeof u === 'object')
      .map((u) => ({
        component: String(u.component ?? 'unknown component'),
        version: u.version == null ? null : String(u.version),
        device: String(u.device ?? 'unknown device'),
        deviceArch: u.deviceArch == null ? null : String(u.deviceArch),
        supported: Array.isArray(u.supported) ? u.supported.map(String) : [],
        reason:
          u.reason === VLLM_GATE_REASON_JP4
            ? VLLM_GATE_REASON_JP4
            : VLLM_GATE_REASON_ARCH,
      })),
  };
}

// ---------------------------------------------------------------------------
// Per_JetPack_Component name / architecture resolution
// (vllm-multi-arch-publish-conflict design steps 12-14, Requirements 2.13,
// 2.14, 3.4, 3.5)
// ---------------------------------------------------------------------------

/**
 * Per_JetPack_Component naming: greengrass_publish.py suffixes the
 * Base_Component_Name with `target.replace('_', '-')` for one component
 * per packaged target, so `model-vllm-{safe}-jetson-xavier-jp7` serves
 * exactly `arm64_jp7`.
 *
 * KEEP IN SYNC with deployments.py `VLLM_TARGET_SUFFIX_TO_ARCH` (itself
 * the reverse of `packaging.VLLM_ARCH_TO_TARGET`). The vocabulary is
 * closed, so a model name that merely happens to end in a `-jetson-…`
 * fragment cannot be misread as a target suffix.
 */
export const VLLM_TARGET_SUFFIX_TO_ARCH: Record<string, string> = {
  'jetson-xavier-jp5': 'arm64_jp5',
  'jetson-xavier-jp6': 'arm64_jp6',
  'jetson-xavier-jp7': 'arm64_jp7',
};

/**
 * One Per_JetPack_Component entry of the publish write-back's
 * `published_component.components` list (greengrass_publish.py design
 * step 7). Only the two fields the resolution rules read are required;
 * the rest are carried for callers that display them.
 */
export interface VllmPerJetPackComponent {
  component_name?: string | null;
  component_version?: string | null;
  target?: string | null;
  architecture?: string | null;
  supported_architectures?: string[] | null;
  component_arn?: string | null;
}

/**
 * The subset of a vLLM_Model_Record's `published_component` map the
 * architecture resolution reads: the record-wide union kept for legacy
 * readers, and the per-JetPack `components` list. Structurally a subset
 * of `VllmPublishedComponent` in services/api.ts, so the model detail
 * response can be passed straight through.
 */
export interface VllmPublishedComponentArchSource {
  supported_architectures?: string[] | null;
  components?: VllmPerJetPackComponent[] | null;
}

/**
 * Split a vLLM component name into its Base_Component_Name and the
 * Target_Architecture its per-JetPack target suffix names. `arch` is
 * null when the name carries no known suffix (a legacy unsuffixed
 * component), in which case `baseName` is the name verbatim.
 *
 * Pure TS twin of `split_vllm_component_name` in
 * edge-cv-portal/backend/functions/deployments.py.
 */
export function splitVllmComponentName(
  componentName: string | null | undefined
): { baseName: string; arch: string | null } {
  const name = componentName == null ? '' : String(componentName);
  for (const [suffix, arch] of Object.entries(VLLM_TARGET_SUFFIX_TO_ARCH)) {
    const marker = `-${suffix}`;
    if (name.endsWith(marker) && name.length > marker.length) {
      return { baseName: name.slice(0, -marker.length), arch };
    }
  }
  return { baseName: name, arch: null };
}

/**
 * The supported Target_Architecture set of ONE published vLLM component,
 * resolved from its backing record's `published_component` map.
 *
 * Pure TS twin of `vllm_component_architectures` in
 * edge-cv-portal/backend/functions/deployments.py — the same three-rule
 * order (design step 11):
 *
 * 1. a `published_component.components` entry whose `component_name`
 *    matches → that entry's `supported_architectures` (the per-JetPack
 *    write-back the publish produced);
 * 2. elif the name carries a known target suffix → `[arch]` when that
 *    arch is in the record-wide set, else `[]` (fail closed on an
 *    out-of-set suffix);
 * 3. else (no name, or an unsuffixed legacy name) → the record-wide
 *    `published_component.supported_architectures`, exactly as before
 *    (Requirements 3.4, 3.5).
 *
 * Empty when unresolvable, so the gate twin fails closed for every
 * device (Requirement 3.7). Kept UI-free so fast-check can exercise it.
 */
export function vllmArchsForComponent(
  componentName: string | null | undefined,
  publishedComponent: VllmPublishedComponentArchSource | null | undefined
): string[] {
  const published = publishedComponent ?? null;
  const recordWide = (published?.supported_architectures ?? []).map(String);

  const name = componentName == null ? '' : String(componentName);
  if (name) {
    for (const entry of published?.components ?? []) {
      if (!entry || typeof entry !== 'object') {
        continue;
      }
      if (String(entry.component_name ?? '') === name) {
        return (entry.supported_architectures ?? []).map(String);
      }
    }
    const { arch } = splitVllmComponentName(name);
    if (arch !== null) {
      return recordWide.includes(arch) ? [arch] : [];
    }
  }

  return recordWide;
}
