/**
 * Unit tests for the pre-submit deployment closure validation helpers
 * (deployment-preflight-validation task 4.7 — Requirements 2.13, 2.14,
 * 2.15, 2.16).
 *
 * The fixtures are the response contract pinned by the backend suite
 * `edge-cv-portal/backend/tests/test_deployment_preflight_exploration.py`
 * and produced by `deployment_preflight.py`: HTTP 409 in the
 * `_workflow_error` envelope, `details.findings`, one of the two codes,
 * and the per-kind fields of each finding. The verbatim Counterexample A
 * (platform), B (dependency) and C (de-selected) shapes are used so a
 * field rename on either side fails here rather than in production.
 */
import { describe, expect, it } from 'vitest';
import {
  ACKNOWLEDGEMENT_FIELD,
  PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED,
  PREFLIGHT_VALIDATION_FAILED,
  PreflightFinding,
  acknowledgementForSubmit,
  componentSelectionKey,
  describePlatform,
  describePreflightFinding,
  describeRequirer,
  isComponentAcknowledged,
  parsePreflightRejection,
  prunedAcknowledgement,
  withAcknowledgedComponent,
} from './deploymentPreflight';

// ---------------------------------------------------------------------------
// Backend finding fixtures (deployment_preflight.py wire shape)
// ---------------------------------------------------------------------------

/** Counterexample A: jp5/jp6-only workflow component on a JP7 device. */
const PLATFORM_FINDING = {
  kind: 'platform-mismatch',
  finding_class: 'blocking-invalid',
  component_name: 'dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe',
  component_version: '1.0.0',
  claimed_platforms: [
    { os: 'linux', variant: 'arm64_jp5', architecture: 'aarch64' },
    { os: 'linux', variant: 'arm64_jp6', architecture: 'aarch64' },
  ],
  devices: [
    {
      thing_name: 'adlink-dlap-701',
      platform: { os: 'linux', architecture: 'aarch64', variant: 'arm64_jp7' },
    },
  ],
  remediation: 'Re-package it for the target device(s) JetPack/platform.',
};

/** Counterexample B: HARD dependency on a name with zero published versions. */
const DEPENDENCY_FINDING = {
  kind: 'dependency-unresolvable',
  finding_class: 'blocking-invalid',
  component_name: 'aws.edgeml.dda.LocalServer.arm64JP4',
  version_requirement: '>=1.0.0 <2.0.0',
  required_by: [
    {
      component_name: 'model-cookies-segmentation-seghead-jetson-xavier',
      component_version: '2.0.0',
      version_requirement: '>=1.0.0 <2.0.0',
      dependency_type: 'HARD',
    },
  ],
  has_published_versions: false,
  published_versions: [],
  remediation: 'It must be re-registered or repackaged for the target platform.',
};

/** Counterexample C: de-selected model still HARD-required by a kept workflow. */
const DESELECTED_FINDING = {
  kind: 'deselected-still-required',
  finding_class: 'acknowledgement-required',
  component_name: 'model-vllm-qwen3-5-9b-jetson-xavier-jp7',
  component_version: '1.0.0',
  required_by: [
    {
      component_name: 'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8',
      component_version: '12.0.0',
      version_requirement: '>=0.0.0',
      dependency_type: 'HARD',
    },
  ],
  remains_installed: true,
  removed_by_this_deployment: false,
  effective_outcome:
    'model-vllm-qwen3-5-9b-jetson-xavier-jp7 will REMAIN installed and running ' +
    'on the target device(s) as a resolved dependency of ' +
    'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8 12.0.0, and will NOT be ' +
    'removed from the device by this deployment.',
  remediation:
    'De-select the component(s) that require it, or re-submit with ' +
    'acknowledged_retained_components naming exactly it.',
};

const UNVERIFIED_FINDING = {
  kind: 'dependency-unresolvable',
  finding_class: 'unverified',
  component_name: 'aws.edgeml.dda.LocalServer.arm64JP7',
  version_requirement: '>=0.0.0',
  required_by: [
    {
      component_name: 'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8',
      component_version: '12.0.0',
      version_requirement: '>=0.0.0',
      dependency_type: 'HARD',
    },
  ],
  has_published_versions: false,
  published_versions: [],
  reason: 'the requirement is satisfied by every version',
  remediation: 'Greengrass remains the authoritative check at submit time.',
};

describe('parsePreflightRejection', () => {
  it('returns null for every other error code so existing handling is untouched', () => {
    expect(parsePreflightRejection('VLLM_ARCH_UNSUPPORTED', 'nope', {})).toBeNull();
    expect(parsePreflightRejection('PLUGIN_ARCH_UNSUPPORTED', 'nope', {})).toBeNull();
    expect(parsePreflightRejection('CAMERA_BINDINGS_INVALID', 'nope', {})).toBeNull();
    expect(parsePreflightRejection(undefined, 'plain failure', undefined)).toBeNull();
  });

  it('parses a PREFLIGHT_VALIDATION_FAILED platform finding, keeping claimed and reported platforms distinct (2.3, 2.7)', () => {
    const rejection = parsePreflightRejection(
      PREFLIGHT_VALIDATION_FAILED,
      'components cannot be deployed to the target devices',
      { findings: [PLATFORM_FINDING] }
    );
    expect(rejection).not.toBeNull();
    expect(rejection!.code).toBe(PREFLIGHT_VALIDATION_FAILED);
    expect(rejection!.blocking).toHaveLength(1);
    expect(rejection!.acknowledgementRequired).toEqual([]);
    expect(rejection!.unverified).toEqual([]);

    const finding = rejection!.blocking[0];
    expect(finding.kind).toBe('platform-mismatch');
    expect(finding.componentName).toBe(
      'dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe'
    );
    expect(finding.componentVersion).toBe('1.0.0');
    // The component's claim and the device's report never merge.
    expect(finding.claimedPlatforms).toEqual([
      { os: 'linux', variant: 'arm64_jp5', architecture: 'aarch64' },
      { os: 'linux', variant: 'arm64_jp6', architecture: 'aarch64' },
    ]);
    expect(finding.devices).toEqual([
      {
        thingName: 'adlink-dlap-701',
        platform: { os: 'linux', architecture: 'aarch64', variant: 'arm64_jp7' },
      },
    ]);
    expect(finding.remediation).toContain('Re-package');
  });

  it('parses a dependency finding with its requirers and the zero-versions distinction (2.5, 2.12)', () => {
    const rejection = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'refused', {
      findings: [DEPENDENCY_FINDING],
    });
    const finding = rejection!.blocking[0];
    expect(finding.kind).toBe('dependency-unresolvable');
    expect(finding.componentName).toBe('aws.edgeml.dda.LocalServer.arm64JP4');
    expect(finding.versionRequirement).toBe('>=1.0.0 <2.0.0');
    expect(finding.hasPublishedVersions).toBe(false);
    expect(finding.publishedVersions).toEqual([]);
    expect(finding.requiredBy).toEqual([
      {
        componentName: 'model-cookies-segmentation-seghead-jetson-xavier',
        componentVersion: '2.0.0',
        versionRequirement: '>=1.0.0 <2.0.0',
        dependencyType: 'HARD',
      },
    ]);
  });

  it('parses a PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED de-selected finding with the effective outcome and requirers (2.13, 2.15)', () => {
    const rejection = parsePreflightRejection(
      PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED,
      'de-selected components will remain installed',
      {
        findings: [DESELECTED_FINDING],
        acknowledgement_field: ACKNOWLEDGEMENT_FIELD,
        acknowledgement_required_for: ['model-vllm-qwen3-5-9b-jetson-xavier-jp7'],
      }
    );
    expect(rejection!.code).toBe(PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED);
    expect(rejection!.blocking).toEqual([]);
    expect(rejection!.acknowledgementRequired).toHaveLength(1);

    const finding = rejection!.acknowledgementRequired[0];
    expect(finding.remainsInstalled).toBe(true);
    expect(finding.removedByThisDeployment).toBe(false);
    expect(finding.effectiveOutcome).toContain('will NOT be');
    expect(finding.effectiveOutcome).toContain('REMAIN installed');
    expect(finding.acknowledged).toBe(false);
    // The requiring component is named with its version and edge (2.15).
    expect(finding.requiredBy[0].componentName).toBe(
      'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8'
    );
    expect(finding.requiredBy[0].componentVersion).toBe('12.0.0');
    expect(finding.requiredBy[0].dependencyType).toBe('HARD');

    expect(rejection!.acknowledgementRequiredFor).toEqual([
      'model-vllm-qwen3-5-9b-jetson-xavier-jp7',
    ]);
    expect(rejection!.acknowledgementField).toBe(ACKNOWLEDGEMENT_FIELD);
  });

  it('groups a single-pass response by class without merging or dropping any finding (2.6, 2.16)', () => {
    const rejection = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'refused', {
      findings: [
        PLATFORM_FINDING,
        DEPENDENCY_FINDING,
        DESELECTED_FINDING,
        UNVERIFIED_FINDING,
      ],
    });
    expect(rejection!.findings).toHaveLength(4);
    expect(rejection!.blocking.map((f) => f.kind)).toEqual([
      'platform-mismatch',
      'dependency-unresolvable',
    ]);
    expect(rejection!.acknowledgementRequired.map((f) => f.componentName)).toEqual([
      'model-vllm-qwen3-5-9b-jetson-xavier-jp7',
    ]);
    expect(rejection!.unverified.map((f) => f.componentName)).toEqual([
      'aws.edgeml.dda.LocalServer.arm64JP7',
    ]);
  });

  it('derives acknowledgementRequiredFor from the findings when the backend omits it', () => {
    const rejection = parsePreflightRejection(
      PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED,
      'refused',
      {
        findings: [
          DESELECTED_FINDING,
          { ...DESELECTED_FINDING, component_name: 'model-b' },
          // A duplicate name must not produce a duplicate entry.
          { ...DESELECTED_FINDING },
        ],
      }
    );
    expect(rejection!.acknowledgementRequiredFor).toEqual([
      'model-b',
      'model-vllm-qwen3-5-9b-jetson-xavier-jp7',
    ]);
    expect(rejection!.acknowledgementField).toBe(ACKNOWLEDGEMENT_FIELD);
  });

  it('tolerates missing, malformed and unknown-valued details without throwing', () => {
    const empty = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'refused', undefined);
    expect(empty!.findings).toEqual([]);
    expect(empty!.acknowledgementRequiredFor).toEqual([]);

    const messy = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'refused', {
      findings: [
        null,
        'garbage',
        { kind: 'who-knows', finding_class: 'brand-new-class' },
        // A manifest with no Platform block arrives as null.
        {
          kind: 'platform-mismatch',
          finding_class: 'unverified',
          component_name: 'testmodel',
          claimed_platforms: [null, { os: 'linux' }],
        },
      ],
    });
    expect(messy!.findings).toHaveLength(2);
    // An unknown class blocks nothing and carries no control.
    expect(messy!.findings[0].findingClass).toBe('unverified');
    expect(messy!.findings[0].kind).toBe('platform-mismatch');
    expect(messy!.findings[0].componentName).toBe('unknown component');
    expect(messy!.blocking).toEqual([]);
    expect(messy!.findings[1].claimedPlatforms).toEqual([{}, { os: 'linux' }]);
  });
});

describe('describePlatform / describeRequirer / describePreflightFinding', () => {
  it('renders a platform attribute block, and a wildcard block as any platform (2.8)', () => {
    expect(describePlatform({ os: 'linux', variant: 'arm64_jp5' })).toBe(
      'os=linux, variant=arm64_jp5'
    );
    expect(describePlatform({})).toBe('any platform');
  });

  it('names a requiring component with its version and dependency edge (2.12, 2.15)', () => {
    const text = describeRequirer({
      componentName: 'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8',
      componentVersion: '12.0.0',
      versionRequirement: '>=0.0.0',
      dependencyType: 'HARD',
    });
    expect(text).toContain('dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8');
    expect(text).toContain('v12.0.0');
    expect(text).toContain('>=0.0.0');
    expect(text).toContain('HARD');
  });

  it('describes a platform mismatch with the claim and the report as separate facts (2.7)', () => {
    const [finding] = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'x', {
      findings: [PLATFORM_FINDING],
    })!.findings;
    const text = describePreflightFinding(finding);
    expect(text).toContain('dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe v1.0.0');
    expect(text).toContain('variant=arm64_jp5');
    expect(text).toContain('variant=arm64_jp6');
    expect(text).toContain('adlink-dlap-701');
    expect(text).toContain('variant=arm64_jp7');
  });

  it('describes an unresolvable dependency with the requirement, requirer and availability (2.5)', () => {
    const [finding] = parsePreflightRejection(PREFLIGHT_VALIDATION_FAILED, 'x', {
      findings: [DEPENDENCY_FINDING],
    })!.findings;
    const text = describePreflightFinding(finding);
    expect(text).toContain('aws.edgeml.dda.LocalServer.arm64JP4');
    expect(text).toContain('>=1.0.0 <2.0.0');
    expect(text).toContain('model-cookies-segmentation-seghead-jetson-xavier v2.0.0');
    expect(text).toContain('no published version in this account');
  });

  it('uses the backend effective-outcome statement verbatim for a de-selected finding (2.13)', () => {
    const [finding] = parsePreflightRejection(PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED, 'x', {
      findings: [DESELECTED_FINDING],
    })!.findings;
    expect(describePreflightFinding(finding)).toBe(DESELECTED_FINDING.effective_outcome);
  });

  it('falls back to an explicit outcome statement when the backend omitted one', () => {
    const finding: PreflightFinding = {
      ...parsePreflightRejection(PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED, 'x', {
        findings: [{ ...DESELECTED_FINDING, effective_outcome: null }],
      })!.findings[0],
    };
    const text = describePreflightFinding(finding);
    expect(text).toContain('remains installed');
    expect(text).toContain('NOT removed');
  });
});

describe('the specific acknowledgement and its invalidation (2.14)', () => {
  const SET_A = componentSelectionKey([
    { component_name: 'dda.workflow.421f8233', component_version: '12.0.0' },
    { component_name: 'aws.edgeml.dda.LocalServer.arm64JP7', component_version: '1.0.19' },
  ]);

  it('keys a selection independently of order and dependently of version', () => {
    const reordered = componentSelectionKey([
      { component_name: 'aws.edgeml.dda.LocalServer.arm64JP7', component_version: '1.0.19' },
      { component_name: 'dda.workflow.421f8233', component_version: '12.0.0' },
    ]);
    expect(reordered).toBe(SET_A);

    const bumped = componentSelectionKey([
      { component_name: 'dda.workflow.421f8233', component_version: '12.0.0' },
      { component_name: 'aws.edgeml.dda.LocalServer.arm64JP7', component_version: '1.0.20' },
    ]);
    expect(bumped).not.toBe(SET_A);

    const shrunk = componentSelectionKey([
      { component_name: 'dda.workflow.421f8233', component_version: '12.0.0' },
    ]);
    expect(shrunk).not.toBe(SET_A);
  });

  it('acknowledges exactly the named component(s), never a blanket proceed', () => {
    let ack = withAcknowledgedComponent(null, 'model-a', true, SET_A);
    expect(ack).toEqual({ componentNames: ['model-a'], selectionKey: SET_A });
    ack = withAcknowledgedComponent(ack, 'model-b', true, SET_A);
    expect(acknowledgementForSubmit(ack, SET_A)).toEqual(['model-a', 'model-b']);
    expect(isComponentAcknowledged(ack, 'model-a', SET_A)).toBe(true);
    expect(isComponentAcknowledged(ack, 'model-c', SET_A)).toBe(false);
  });

  it('un-acknowledging the last component submits no acknowledgement at all', () => {
    let ack = withAcknowledgedComponent(null, 'model-a', true, SET_A);
    ack = withAcknowledgedComponent(ack, 'model-a', false, SET_A);
    expect(ack).toBeNull();
    expect(acknowledgementForSubmit(ack, SET_A)).toBeUndefined();
  });

  it('does not send an acknowledgement made against a different component set', () => {
    const ack = withAcknowledgedComponent(null, 'model-a', true, SET_A);
    const changed = componentSelectionKey([
      { component_name: 'dda.workflow.421f8233', component_version: '12.0.0' },
    ]);
    // The finding computed for the changed set may differ, so the
    // acknowledgement must not travel with it: the backend re-reports and
    // refuses again rather than silently authorizing the retention.
    expect(acknowledgementForSubmit(ack, changed)).toBeUndefined();
    expect(isComponentAcknowledged(ack, 'model-a', changed)).toBe(false);
    expect(prunedAcknowledgement(ack, changed)).toBeNull();
    // ...and it is still valid for the set it was made against.
    expect(acknowledgementForSubmit(ack, SET_A)).toEqual(['model-a']);
    expect(prunedAcknowledgement(ack, SET_A)).toBe(ack);
  });

  it('re-anchors to the current set when a stale acknowledgement is toggled', () => {
    const stale = { componentNames: ['model-a', 'model-b'], selectionKey: 'other-set' };
    const ack = withAcknowledgedComponent(stale, 'model-c', true, SET_A);
    // The stale names are dropped rather than carried into the new set.
    expect(ack).toEqual({ componentNames: ['model-c'], selectionKey: SET_A });
  });
});
