/**
 * Pre-submit deployment closure validation support for the
 * Create/Revise Deployment screen
 * (deployment-preflight-validation task 4.7 — Requirements 2.13, 2.14,
 * 2.15, 2.16).
 *
 * Pure helpers for parsing the two rejection codes the backend's
 * pre-submit closure validation returns (`deployment_preflight.py`,
 * surfaced through `deployments._workflow_error` as
 * `{error: {code, message, details: {findings: [...]}}}` with HTTP 409),
 * for grouping the findings by their class, and for modelling the
 * SPECIFIC acknowledgement that clears the acknowledgement-required
 * class.
 *
 * The three finding classes are load-bearing and must never be
 * collapsed into one another (bugfix.md 2.16):
 *
 * - `blocking-invalid` — Greengrass itself would reject the deployment
 *   (a platform manifest no target device satisfies, a dependency with
 *   no satisfying published version). NO acknowledgement bypasses it, so
 *   no acknowledgement control is ever offered for it.
 * - `acknowledgement-required` — the deployment is legitimate and
 *   Greengrass would accept it, but a component the operator de-selected
 *   stays installed as a resolved dependency of one they kept, so the
 *   outcome differs from the apparent intent. Refused on first submit;
 *   proceeds on a re-submit naming exactly those components in
 *   `acknowledged_retained_components`.
 * - `unverified` — a check the backend could not perform. Reported,
 *   blocks nothing (the backend fails OPEN), so it carries no control.
 *
 * The acknowledgement is SPECIFIC, never blanket: the backend requires
 * EXACT set equality between `acknowledged_retained_components` and the
 * de-selected component names of the finding computed for the SUBMITTED
 * set. A component-set change therefore invalidates a pending
 * acknowledgement — `componentSelectionKey` / `acknowledgementForSubmit`
 * below make that invalidation the default rather than something the UI
 * has to remember, so the refusal is re-reported instead of silently
 * authorized.
 *
 * Kept free of React/DOM imports so the sibling vitest suite can
 * exercise it directly — the established pattern of
 * `parsePluginGateRejection` (pluginComponents.ts),
 * `parseVllmGateRejection` (vllmArchGate.ts) and
 * `parseCameraBindingRejection` (cameraBindings.ts).
 */

/** Keep in sync with deployment_preflight.py CODE_VALIDATION_FAILED. */
export const PREFLIGHT_VALIDATION_FAILED = 'PREFLIGHT_VALIDATION_FAILED';
/** Keep in sync with deployment_preflight.py CODE_ACKNOWLEDGEMENT_REQUIRED. */
export const PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED = 'PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED';
/** Keep in sync with deployment_preflight.py ACKNOWLEDGEMENT_FIELD. */
export const ACKNOWLEDGEMENT_FIELD = 'acknowledged_retained_components';

/** Keep in sync with deployment_preflight.py KIND_*. */
export type PreflightFindingKind =
  | 'platform-mismatch'
  | 'dependency-unresolvable'
  | 'deselected-still-required';

/** Keep in sync with deployment_preflight.py CLASS_*. */
export type PreflightFindingClass =
  | 'blocking-invalid'
  | 'acknowledgement-required'
  | 'unverified';

export type PreflightRejectionCode =
  | typeof PREFLIGHT_VALIDATION_FAILED
  | typeof PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED;

const FINDING_KINDS: PreflightFindingKind[] = [
  'platform-mismatch',
  'dependency-unresolvable',
  'deselected-still-required',
];

const FINDING_CLASSES: PreflightFindingClass[] = [
  'blocking-invalid',
  'acknowledgement-required',
  'unverified',
];

/**
 * One entry of a finding's `required_by` list: a component that declares
 * the dependency, with the version requirement and dependency type of the
 * edge (2.12). Used by both the `dependency-unresolvable` findings (who
 * needs the unresolvable name) and the `deselected-still-required` ones
 * (who keeps the de-selected component installed).
 */
export interface PreflightRequirer {
  componentName: string;
  componentVersion: string | null;
  versionRequirement: string | null;
  dependencyType: string | null;
}

/**
 * One target device of a `platform-mismatch` finding, with the platform
 * attributes it reports. Carried as a fact DISTINCT from the component's
 * claimed platforms (2.7): the Greengrass wording that presents the
 * device's platform as the component's claim is never reproduced.
 */
export interface PreflightDevice {
  thingName: string;
  platform: Record<string, string>;
}

/**
 * One finding of a pre-submit validation pass. Every field the backend
 * only sets for some kinds is optional-shaped (`null` / `[]`) rather than
 * absent, so callers never branch on presence.
 */
export interface PreflightFinding {
  kind: PreflightFindingKind;
  findingClass: PreflightFindingClass;
  componentName: string;
  componentVersion: string | null;
  remediation: string;
  /** Why a check could not be made (unverified findings). */
  reason: string | null;
  // platform-mismatch
  /**
   * The manifest `Platform` blocks the component version actually
   * publishes. A manifest with no `Platform` block at all (satisfied by
   * every device) arrives as `null` and is normalized to `{}`.
   */
  claimedPlatforms: Record<string, string>[];
  devices: PreflightDevice[];
  // dependency-unresolvable
  versionRequirement: string | null;
  hasPublishedVersions: boolean | null;
  publishedVersions: string[];
  // dependency-unresolvable + deselected-still-required
  requiredBy: PreflightRequirer[];
  // deselected-still-required
  remainsInstalled: boolean | null;
  removedByThisDeployment: boolean | null;
  /**
   * The explicit effective-outcome statement (2.13): the component will
   * remain installed and running as a resolved dependency and will NOT be
   * removed from the device by this deployment. Rendered prominently — it
   * is the one thing the operator must not be able to miss.
   */
  effectiveOutcome: string | null;
  /** Whether the submission's acknowledgement matched this finding. */
  acknowledged: boolean;
}

export interface PreflightRejection {
  code: PreflightRejectionCode;
  message: string;
  /** Every finding of the single-pass response, in the backend's order. */
  findings: PreflightFinding[];
  /** The findings grouped by class (2.16) — never re-ordered or merged. */
  blocking: PreflightFinding[];
  acknowledgementRequired: PreflightFinding[];
  unverified: PreflightFinding[];
  /**
   * The de-selected component names an acknowledgement must name EXACTLY
   * to clear this response. Taken from `details.acknowledgement_required_for`
   * when the backend supplied it, else derived from the
   * acknowledgement-required findings.
   */
  acknowledgementRequiredFor: string[];
  /** The request field the acknowledgement travels in. */
  acknowledgementField: string;
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((v): v is Record<string, unknown> => !!v && typeof v === 'object')
    : [];
}

/** String-valued view of a platform attribute block; non-objects → {}. */
function toPlatform(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {};
  }
  const platform: Record<string, string> = {};
  for (const [key, attribute] of Object.entries(value as Record<string, unknown>)) {
    if (attribute != null) {
      platform[key] = String(attribute);
    }
  }
  return platform;
}

function toRequirer(raw: Record<string, unknown>): PreflightRequirer {
  return {
    componentName: String(raw.component_name ?? 'unknown component'),
    componentVersion: raw.component_version == null ? null : String(raw.component_version),
    versionRequirement:
      raw.version_requirement == null ? null : String(raw.version_requirement),
    dependencyType: raw.dependency_type == null ? null : String(raw.dependency_type),
  };
}

function toDevice(raw: Record<string, unknown>): PreflightDevice {
  return {
    thingName: String(raw.thing_name ?? 'unknown device'),
    platform: toPlatform(raw.platform),
  };
}

function toFinding(raw: Record<string, unknown>): PreflightFinding {
  const kind = FINDING_KINDS.includes(raw.kind as PreflightFindingKind)
    ? (raw.kind as PreflightFindingKind)
    : 'platform-mismatch';
  // An unrecognized class is treated as `unverified`: it carries no
  // control and blocks nothing, which is the only safe default for a
  // class this build does not know about.
  const findingClass = FINDING_CLASSES.includes(raw.finding_class as PreflightFindingClass)
    ? (raw.finding_class as PreflightFindingClass)
    : 'unverified';
  return {
    kind,
    findingClass,
    componentName: String(raw.component_name ?? 'unknown component'),
    componentVersion: raw.component_version == null ? null : String(raw.component_version),
    remediation: raw.remediation == null ? '' : String(raw.remediation),
    reason: raw.reason == null ? null : String(raw.reason),
    claimedPlatforms: Array.isArray(raw.claimed_platforms)
      ? raw.claimed_platforms.map(toPlatform)
      : [],
    devices: asRecordArray(raw.devices).map(toDevice),
    versionRequirement:
      raw.version_requirement == null ? null : String(raw.version_requirement),
    hasPublishedVersions:
      raw.has_published_versions == null ? null : Boolean(raw.has_published_versions),
    publishedVersions: Array.isArray(raw.published_versions)
      ? raw.published_versions.map(String)
      : [],
    requiredBy: asRecordArray(raw.required_by).map(toRequirer),
    remainsInstalled: raw.remains_installed == null ? null : Boolean(raw.remains_installed),
    removedByThisDeployment:
      raw.removed_by_this_deployment == null
        ? null
        : Boolean(raw.removed_by_this_deployment),
    effectiveOutcome: raw.effective_outcome == null ? null : String(raw.effective_outcome),
    acknowledged: raw.acknowledged === true,
  };
}

/**
 * Parse a structured API error into a pre-submit validation rejection, or
 * null when the error is neither of the two preflight codes (so the
 * existing gate handling and the generic error path stay untouched).
 *
 * The backend envelope is `{error: {code, message, details}}` with
 * `details.findings` and, for the acknowledgement code,
 * `details.acknowledgement_required_for` and
 * `details.acknowledgement_field`.
 */
export function parsePreflightRejection(
  code: string | undefined,
  message: string,
  details: Record<string, unknown> | undefined
): PreflightRejection | null {
  if (
    code !== PREFLIGHT_VALIDATION_FAILED &&
    code !== PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED
  ) {
    return null;
  }
  const findings = asRecordArray(details?.findings).map(toFinding);
  const acknowledgementRequired = findings.filter(
    (f) => f.findingClass === 'acknowledgement-required'
  );
  const reported = Array.isArray(details?.acknowledgement_required_for)
    ? (details?.acknowledgement_required_for as unknown[]).map(String)
    : acknowledgementRequired.map((f) => f.componentName);
  return {
    code,
    message,
    findings,
    blocking: findings.filter((f) => f.findingClass === 'blocking-invalid'),
    acknowledgementRequired,
    unverified: findings.filter((f) => f.findingClass === 'unverified'),
    acknowledgementRequiredFor: [...new Set(reported)].sort(),
    acknowledgementField:
      details?.acknowledgement_field == null
        ? ACKNOWLEDGEMENT_FIELD
        : String(details.acknowledgement_field),
  };
}

// ---------------------------------------------------------------------------
// Descriptions (each finding names its component and its remediation, 2.7)
// ---------------------------------------------------------------------------

/** `os=linux, variant=arm64_jp5` — or `any platform` for a wildcard block. */
export function describePlatform(platform: Record<string, string>): string {
  const entries = Object.keys(platform).sort();
  if (entries.length === 0) {
    return 'any platform';
  }
  return entries.map((key) => `${key}=${platform[key]}`).join(', ');
}

/**
 * One requiring component, with the dependency edge that makes it the
 * reason the finding exists (2.12, 2.15): named precisely enough that the
 * operator can de-select it in the same pre-submit edit.
 */
export function describeRequirer(requirer: PreflightRequirer): string {
  const version = requirer.componentVersion ? ` v${requirer.componentVersion}` : '';
  const requirement = requirer.versionRequirement
    ? ` at "${requirer.versionRequirement}"`
    : '';
  const type = requirer.dependencyType ? ` (${requirer.dependencyType})` : '';
  return `${requirer.componentName}${version}${requirement}${type}`;
}

/**
 * One-line description of a finding, attributing the fault to a named
 * component and keeping the component's CLAIMED platforms and each
 * device's REPORTED platform as distinct facts (2.7).
 */
export function describePreflightFinding(finding: PreflightFinding): string {
  const version = finding.componentVersion ? ` v${finding.componentVersion}` : '';
  if (finding.kind === 'platform-mismatch') {
    const claimed =
      finding.claimedPlatforms.length > 0
        ? finding.claimedPlatforms.map((p) => `[${describePlatform(p)}]`).join(' ')
        : 'no platform manifests';
    if (finding.devices.length === 0) {
      return (
        `${finding.componentName}${version} publishes ${claimed}` +
        (finding.reason ? ` — ${finding.reason}` : '')
      );
    }
    const devices = finding.devices
      .map((d) => `${d.thingName} (${describePlatform(d.platform)})`)
      .join(', ');
    return (
      `${finding.componentName}${version} claims ${claimed}, which is not ` +
      `satisfied by ${devices}`
    );
  }
  if (finding.kind === 'dependency-unresolvable') {
    const requirement = finding.versionRequirement
      ? `"${finding.versionRequirement}"`
      : 'its version requirement';
    const requirers =
      finding.requiredBy.length > 0
        ? finding.requiredBy.map(describeRequirer).join(', ')
        : 'a selected component';
    const availability =
      finding.hasPublishedVersions === false
        ? 'has no published version in this account'
        : finding.publishedVersions.length > 0
          ? `publishes only ${finding.publishedVersions.join(', ')}`
          : 'has no version satisfying it';
    return (
      `${finding.componentName} is required at ${requirement} by ${requirers}, ` +
      `but ${availability}` +
      (finding.reason ? ` — ${finding.reason}` : '')
    );
  }
  // deselected-still-required: the effective-outcome statement IS the
  // description, and it is never paraphrased away.
  return (
    finding.effectiveOutcome ??
    `${finding.componentName} remains installed on the target device(s) as a ` +
      `resolved dependency and is NOT removed by this deployment`
  );
}

// ---------------------------------------------------------------------------
// The SPECIFIC acknowledgement and its invalidation (2.14)
// ---------------------------------------------------------------------------

/**
 * A pending acknowledgement: the de-selected component names whose
 * retention the operator authorized, PLUS the component selection they
 * authorized it against. The selection key is what makes the
 * acknowledgement specific in time as well as in content — the backend
 * matches by exact set equality against the finding computed for the
 * SUBMITTED set, so an acknowledgement made against a different set must
 * not be sent.
 */
export interface RetainedAcknowledgement {
  componentNames: string[];
  selectionKey: string;
}

/**
 * A stable signature of the submitted component set. Any add, removal or
 * version change produces a different key, which invalidates a pending
 * acknowledgement (see `acknowledgementForSubmit`).
 */
export function componentSelectionKey(
  components: Array<{ component_name: string; component_version?: string | null }>
): string {
  return components
    .map((c) => `${c.component_name}@${c.component_version ?? ''}`)
    .sort()
    .join('|');
}

/**
 * Toggle ONE named component in the acknowledgement, re-anchoring it to
 * the current selection. Un-checking the last name yields null (nothing
 * is acknowledged), so an empty acknowledgement is never submitted.
 */
export function withAcknowledgedComponent(
  acknowledgement: RetainedAcknowledgement | null,
  componentName: string,
  acknowledged: boolean,
  selectionKey: string
): RetainedAcknowledgement | null {
  const current =
    acknowledgement && acknowledgement.selectionKey === selectionKey
      ? acknowledgement.componentNames
      : [];
  const names = new Set(current);
  if (acknowledged) {
    names.add(componentName);
  } else {
    names.delete(componentName);
  }
  if (names.size === 0) {
    return null;
  }
  return { componentNames: [...names].sort(), selectionKey };
}

/** Is `componentName` acknowledged for the CURRENT component selection? */
export function isComponentAcknowledged(
  acknowledgement: RetainedAcknowledgement | null,
  componentName: string,
  selectionKey: string
): boolean {
  return (
    !!acknowledgement &&
    acknowledgement.selectionKey === selectionKey &&
    acknowledgement.componentNames.includes(componentName)
  );
}

/**
 * The `acknowledged_retained_components` value to submit, or undefined
 * when nothing is acknowledged for the set being submitted.
 *
 * This is the invalidation rule (2.14): an acknowledgement made against a
 * DIFFERENT component selection is not sent, so the backend recomputes
 * the finding for the submitted set and refuses again rather than
 * silently authorizing a retention the operator never saw.
 */
export function acknowledgementForSubmit(
  acknowledgement: RetainedAcknowledgement | null,
  selectionKey: string
): string[] | undefined {
  if (
    !acknowledgement ||
    acknowledgement.selectionKey !== selectionKey ||
    acknowledgement.componentNames.length === 0
  ) {
    return undefined;
  }
  return [...acknowledgement.componentNames].sort();
}

/**
 * Drop a pending acknowledgement that no longer matches the current
 * component selection. Returns the same reference when it still applies,
 * so it is safe to call from a state updater on every selection change.
 */
export function prunedAcknowledgement(
  acknowledgement: RetainedAcknowledgement | null,
  selectionKey: string
): RetainedAcknowledgement | null {
  if (!acknowledgement || acknowledgement.selectionKey === selectionKey) {
    return acknowledgement;
  }
  return null;
}
