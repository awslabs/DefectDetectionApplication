/**
 * Shared architecture-compatibility twin for the deployment screen
 * (device-arch-compatibility, tasks 3.1/4.x, Requirements 3, 4, 5).
 *
 * A pure, UI-free module (mirroring `vllmArchGate.ts`) that unifies the
 * gated-component `Target_Architecture` compatibility contract the
 * Create/Revise Deployment screen predicts client-side. The predicate is
 * intentionally identical in shape to the authoritative backend gate
 * `evaluate_vllm_arch_gate` in
 * edge-cv-portal/backend/functions/deployments.py — exact-name
 * `Set.has` membership, no cross-architecture fallback, fail closed on a
 * null device architecture or an empty supported set (Requirement 5.1).
 * The backend gate remains the sole enforcement point; this twin only
 * predicts its verdict so incompatible components can be hidden/labeled
 * before submit.
 *
 * Kept free of React/UI imports so the fast-check property test
 * (task 3.2) can exercise it directly.
 *
 * vLLM keying note (vllm-multi-arch-publish-conflict design step 13): a
 * published vLLM model is now ONE Per_JetPack_Component per packaged
 * target, so the `vllmArchs` entries this module reads are keyed by the
 * SUFFIXED per-JetPack component name (`model-vllm-{safe}-jetson-xavier-jp7`)
 * and each entry holds that component's own single architecture, resolved
 * by `vllmArchsForComponent` in `vllmArchGate.ts`. No production change is
 * needed here: `componentSupportedArchs` already keys `vllmArchs` by the
 * exact component name, and `inferComponentTargetArchs` already matches the
 * `jp5`/`jp6`/`jp7` token a suffixed name carries (Requirement 2.13).
 */
import { architectureLabel, isPluginComponent } from './pluginComponents';
import { isVllmModelComponent } from './vllmArchGate';

/** The client-side gated classes the deploy-screen arch filter covers. */
export type GatedKind = 'vllm' | 'plugin';

/**
 * Infer the DDA JetPack Target_Architecture(s) a component targets from
 * the JetPack token encoded in its component name — the DDA naming
 * convention (`*-jp5`, `*JP6`, `arm64JP5`, `LocalServer.arm64JP6`, …).
 *
 * This covers ordinary (non-gated) model and LocalServer components,
 * which the backend deployment gate does NOT arch-check but which still
 * only run on the matching JetPack major: a `-jp5` build must not be
 * offered for a jp6 device (device-arch-compatibility Req 3). Returns
 * the inferred fixed-set arch(es), or `[]` when the name carries no
 * JetPack token (arch cannot be judged from the name — the component is
 * left to the coarse arm64/amd64 filter and kept).
 *
 * Matched case-insensitively; the `jpN` token is bounded by a non-digit
 * (or string end) so `jp5` does not match inside `jp513`-style strings
 * only via its leading major — the JetPack MAJOR is what gates.
 */
export function inferComponentTargetArchs(componentName: string): string[] {
  const name = String(componentName).toLowerCase();
  const archs = new Set<string>();
  // Match a jetpack major token: jp4/jp5/jp6/jp7 or jetpack4/5/6/7, where
  // the digit is the major and is not immediately followed by another digit
  // (so "jp6" and "jp6.2" match major 6; "arm64jp5" matches major 5;
  // "arm64JP7" matches major 7 — jetpack7-support Req 7.3).
  const re = /(?:jp|jetpack)(4|5|6|7)(?![0-9])/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(name)) !== null) {
    archs.add(`arm64_jp${m[1]}`);
  }
  return [...archs];
}

/**
 * A component as far as the arch filter is concerned. Structurally a
 * subset of `CreateDeployment.tsx`'s `ComponentInfo` so callers can pass
 * their component records directly without a shared import.
 */
export interface ArchGatedComponent {
  component_name: string;
  /** Plugin_Component supported Target_Architectures (components.py). */
  supported_architectures?: string[];
}

/**
 * Which gated class a component belongs to for client-side arch
 * filtering, or null when it is not client-side gateable.
 *
 * `model-vllm-*` → 'vllm'; `dda.plugin.*` → 'plugin'. Everything else —
 * including LLM-bearing workflow components, whose
 * has_llm_inference/packaged_architectures discriminators are not
 * exposed to the client — returns null and stays backend-authoritative
 * (Requirements 3.6, 5.3).
 */
export function classifyGatedComponent(
  componentName: string
): GatedKind | null {
  if (isVllmModelComponent(componentName)) {
    return 'vllm';
  }
  if (isPluginComponent(componentName)) {
    return 'plugin';
  }
  return null;
}

/**
 * The component's Supported_Architecture_Set. For plugins the listing's
 * `supported_architectures`; for vLLM the set resolved by component name
 * (see the catalog resolution effect). An unresolvable/undefined set
 * yields `[]` so the component fails closed (Requirement 5.2). A
 * non-gated component also yields `[]` (it is never arch-filtered, so
 * the set is never consulted).
 */
export function componentSupportedArchs(
  component: ArchGatedComponent,
  vllmArchs: Record<string, string[]>
): string[] {
  const kind = classifyGatedComponent(component.component_name);
  if (kind === 'plugin') {
    return component.supported_architectures ?? [];
  }
  if (kind === 'vllm') {
    return vllmArchs[component.component_name] ?? [];
  }
  return [];
}

/**
 * Exact-name membership of a device's recorded Target_Architecture in a
 * component's Supported_Architecture_Set. A null device architecture and
 * an empty supported set are both incompatible (fail closed) — the same
 * predicate as the backend `evaluate_vllm_arch_gate` `device_arch in
 * supported` check (Requirement 5.1).
 */
export function isArchCompatible(
  deviceArch: string | null,
  supported: string[]
): boolean {
  if (deviceArch === null) {
    return false;
  }
  return new Set(supported).has(deviceArch);
}

/**
 * True iff the component is compatible with EVERY selected device
 * architecture (Requirement 3.2). An empty device list yields true (no
 * constraint applies yet — Requirement 5.4).
 */
export function isCompatibleWithAllDevices(
  supported: string[],
  deviceArchs: (string | null)[]
): boolean {
  return deviceArchs.every((arch) => isArchCompatible(arch, supported));
}

/**
 * Human-readable exclusion reason for a gated component: the selected
 * device Target_Architecture(s) that caused the exclusion and the
 * component's Supported_Architecture_Set (Requirement 3.4), reusing the
 * `describeVllmArchEntry` / `describeArchUnsupported` phrasing and the
 * shared `architectureLabel`.
 */
export function describeArchIncompatibility(
  component: ArchGatedComponent,
  deviceArchs: Record<string, string | null>,
  supported: string[]
): string {
  const supportedLabel =
    supported.length > 0
      ? supported.map(architectureLabel).join(', ')
      : 'no supported architectures recorded';

  const offending = Object.keys(deviceArchs)
    .sort()
    .filter((device) => !isArchCompatible(deviceArchs[device], supported))
    .map((device) => {
      const arch = deviceArchs[device];
      return arch
        ? `"${device}" (recorded architecture ${architectureLabel(arch)})`
        : `"${device}" (no recorded architecture)`;
    });

  const devicePart =
    offending.length > 0 ? offending.join(', ') : 'the selected device(s)';

  return (
    `${component.component_name} is not supported by ${devicePart} ` +
    `(supported: ${supportedLabel})`
  );
}
