/**
 * Property-based tests for the deploy-screen architecture-compatibility
 * twin (device-arch-compatibility task 3.2).
 *
 * These assert the pure predicate in `archCompatibility.ts` matches the
 * backend `evaluate_vllm_arch_gate` semantics: exact-name membership,
 * fail-closed on a null device architecture, fail-closed on an empty
 * supported set, the all-devices conjunction, twin-equivalence with the
 * gate over client-classifiable gated components, and the non-gated
 * exemption. Every property runs a minimum of 100 examples.
 */
import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import {
  classifyGatedComponent,
  componentSupportedArchs,
  inferComponentTargetArchs,
  isArchCompatible,
  isCompatibleWithAllDevices,
} from './archCompatibility';
import {
  VLLM_TARGET_SUFFIX_TO_ARCH,
  VllmComponentManifest,
  evaluateVllmArchGate,
  vllmArchsForComponent,
} from './vllmArchGate';

const RUNS = { numRuns: 100 };

// The fixed DDA Target_Architecture set the gates match by exact name,
// plus some out-of-set noise so exactness is genuinely exercised.
const FIXED_ARCHS = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
];
const archArb = fc.constantFrom(...FIXED_ARCHS, 'arm64', 'aarch64', 'amd64', '');
const supportedArb = fc.uniqueArray(archArb, { maxLength: 5 });
// A device arch is either a recorded value or null (no recorded arch).
const deviceArchArb = fc.option(archArb, { nil: null });

describe('archCompatibility twin', () => {
  // Feature: device-arch-compatibility, Property 1: Fail-closed on null device arch
  // Validates: Requirements 3.5, 5.1
  it('Property 1: a null device arch is never compatible', () => {
    fc.assert(
      fc.property(supportedArb, (supported) => {
        expect(isArchCompatible(null, supported)).toBe(false);
      }),
      RUNS
    );
  });

  // Feature: device-arch-compatibility, Property 2: Fail-closed on empty supported set
  // Validates: Requirements 5.1, 5.2
  it('Property 2: an empty supported set is never compatible', () => {
    fc.assert(
      fc.property(deviceArchArb, (deviceArch) => {
        expect(isArchCompatible(deviceArch, [])).toBe(false);
      }),
      RUNS
    );
  });

  // Feature: device-arch-compatibility, Property 3: Exact-name membership, no fallback
  // Validates: Requirements 5.1
  it('Property 3: compatible iff supported contains the arch by exact name', () => {
    fc.assert(
      fc.property(archArb, supportedArb, (deviceArch, supported) => {
        const expected = supported.includes(deviceArch);
        expect(isArchCompatible(deviceArch, supported)).toBe(expected);
      }),
      RUNS
    );
  });

  // Feature: device-arch-compatibility, Property 3: Exact-name membership, no fallback
  // Validates: Requirements 5.1
  it('Property 3b: no cross-arch coercion (arm64 does not satisfy arm64_jp6)', () => {
    // A device reporting a coarse "arm64"/"aarch64" is never satisfied by
    // a JetPack-specific supported set unless it matches by exact name.
    expect(isArchCompatible('arm64', ['arm64_jp6'])).toBe(false);
    expect(isArchCompatible('aarch64', ['arm64_jp5'])).toBe(false);
    expect(isArchCompatible('arm64_jp6', ['arm64'])).toBe(false);
    expect(isArchCompatible('arm64_jp6', ['arm64_jp6'])).toBe(true);
  });

  // Feature: device-arch-compatibility, Property 4: All-devices conjunction
  // Validates: Requirements 3.2, 5.4
  it('Property 4: compatible-with-all iff compatible with every device', () => {
    fc.assert(
      fc.property(
        supportedArb,
        fc.array(deviceArchArb, { maxLength: 6 }),
        (supported, deviceArchs) => {
          const expected = deviceArchs.every((a) =>
            isArchCompatible(a, supported)
          );
          expect(isCompatibleWithAllDevices(supported, deviceArchs)).toBe(
            expected
          );
        }
      ),
      RUNS
    );
  });

  // Feature: device-arch-compatibility, Property 4: All-devices conjunction
  // Validates: Requirements 3.2, 5.4
  it('Property 4b: an empty device list is compatible (no constraint)', () => {
    fc.assert(
      fc.property(supportedArb, (supported) => {
        expect(isCompatibleWithAllDevices(supported, [])).toBe(true);
      }),
      RUNS
    );
  });

  // Feature: device-arch-compatibility, Property 5: Twin equivalence with the backend gate
  // Validates: Requirements 5.1
  it('Property 5: incompatible (component,device) pairs match the backend gate', () => {
    const vllmNameArb = fc
      .string({ minLength: 1, maxLength: 6 })
      .map((s) => `model-vllm-${s}`);
    const deviceNameArb = fc.string({ minLength: 1, maxLength: 6 });
    fc.assert(
      fc.property(
        fc.dictionary(vllmNameArb, supportedArb, { maxKeys: 4 }),
        fc.dictionary(deviceNameArb, deviceArchArb, { maxKeys: 4 }),
        (supportedByComponent, deviceArchs) => {
          // Backend gate manifests: {component: {version, architectures}}.
          const manifests: Record<string, VllmComponentManifest> = {};
          for (const [name, supported] of Object.entries(supportedByComponent)) {
            manifests[name] = { version: null, architectures: supported };
          }
          const gateEntries = evaluateVllmArchGate(manifests, deviceArchs);
          const gatePairs = new Set(
            gateEntries.map((e) => `${e.component}\u0000${e.device}`)
          );

          // Client twin: the same (component, device) pairs marked
          // incompatible, restricted to client-classifiable components.
          const clientPairs = new Set<string>();
          for (const [name, supported] of Object.entries(supportedByComponent)) {
            expect(classifyGatedComponent(name)).toBe('vllm');
            const resolved = componentSupportedArchs(
              { component_name: name },
              { [name]: supported }
            );
            for (const [device, arch] of Object.entries(deviceArchs)) {
              if (!isArchCompatible(arch, resolved)) {
                clientPairs.add(`${name}\u0000${device}`);
              }
            }
          }
          expect([...clientPairs].sort()).toEqual([...gatePairs].sort());
        }
      ),
      RUNS
    );
  });

  // device-arch-compatibility Req 3.3: JetPack target inferred from the
  // component name so jp5 builds are hidden on a jp6 device.
  describe('inferComponentTargetArchs (name-based JetPack inference)', () => {
    it('infers the JetPack major from DDA name conventions', () => {
      expect(inferComponentTargetArchs('model-cookies-binary-jetson-xavier-jp5'))
        .toEqual(['arm64_jp5']);
      expect(inferComponentTargetArchs('model-cookies-binaryJP5-jetson-xavier-jp5'))
        .toEqual(['arm64_jp5']);
      expect(inferComponentTargetArchs('model-rf-detr-seg-nano-jetson-xavier-jp6'))
        .toEqual(['arm64_jp6']);
      expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.arm64JP5'))
        .toEqual(['arm64_jp5']);
      expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.arm64JP6'))
        .toEqual(['arm64_jp6']);
      expect(inferComponentTargetArchs('jp4mic730ai')).toEqual(['arm64_jp4']);
      // jetpack7-support Req 7.3: the JP7 token requires arm64_jp7.
      expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.arm64JP7'))
        .toEqual(['arm64_jp7']);
      expect(inferComponentTargetArchs('model-x-jetson-thor-jp7'))
        .toEqual(['arm64_jp7']);
    });

    it('returns [] for names with no JetPack token (kept, not hidden)', () => {
      expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.aarch64'))
        .toEqual([]);
      expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.arm64'))
        .toEqual([]);
      expect(inferComponentTargetArchs('model-cookies-binary-jetson-xavier'))
        .toEqual([]);
      expect(inferComponentTargetArchs('model-vllm-opt125m-smoke')).toEqual([]);
    });

    it('a jp5 build is incompatible with a jp6 device by exact name', () => {
      const inferred = inferComponentTargetArchs('model-x-jp5');
      expect(isArchCompatible('arm64_jp6', inferred)).toBe(false);
      expect(isArchCompatible('arm64_jp5', inferred)).toBe(true);
    });

    it('a jp7 build only matches a jp7 device by exact name (Req 7.3)', () => {
      const inferred = inferComponentTargetArchs(
        'aws.edgeml.dda.LocalServer.arm64JP7'
      );
      expect(isArchCompatible('arm64_jp7', inferred)).toBe(true);
      expect(isArchCompatible('arm64_jp5', inferred)).toBe(false);
      expect(isArchCompatible('arm64_jp6', inferred)).toBe(false);
      // And jp5/jp6 builds are incompatible with a jp7 device.
      expect(
        isArchCompatible('arm64_jp7', inferComponentTargetArchs('model-x-jp6'))
      ).toBe(false);
    });

    it('only ever returns fixed-set arm64_jpN values', () => {
      const majorArb = fc.constantFrom('4', '5', '6', '7');
      const prefixArb = fc.constantFrom('jp', 'JP', 'jetpack', 'JetPack');
      fc.assert(
        fc.property(
          fc.string({ maxLength: 8 }),
          prefixArb,
          majorArb,
          fc.string({ maxLength: 8 }),
          (pre, kw, major, post) => {
            const archs = inferComponentTargetArchs(`${pre}${kw}${major}${post}`);
            for (const a of archs) {
              expect(FIXED_ARCHS).toContain(a);
            }
          }
        ),
        RUNS
      );
    });
  });

  // Suffixed Per_JetPack_Component keying
  // (vllm-multi-arch-publish-conflict design step 13, task 8.2).
  // A published vLLM record now contributes one `vllmArchs` entry per
  // packaged target, keyed by the SUFFIXED per-JetPack name with a
  // disjoint single-arch set each. The exact-name membership and
  // fail-closed rules above apply to those keys unchanged.
  // Validates: Requirements 2.13, 2.14, 3.4, 3.5, 3.6, 3.7, 3.9
  describe('suffixed per-JetPack vllmArchs keys', () => {
    const SUFFIXES = Object.keys(VLLM_TARGET_SUFFIX_TO_ARCH);
    const baseNameArb = fc
      .string({ minLength: 1, maxLength: 8 })
      .map((s) => `model-vllm-${s.replace(/[^a-zA-Z0-9._-]/g, 'x')}`)
      // A generated base name must not itself end in a target suffix,
      // or the suffixed keys built from it would nest suffixes.
      .filter((n) => SUFFIXES.every((suf) => !n.endsWith(`-${suf}`)));
    // Non-empty subset of the closed per-JetPack suffix vocabulary.
    const suffixSubsetArb = fc.uniqueArray(fc.constantFrom(...SUFFIXES), {
      minLength: 1,
      maxLength: SUFFIXES.length,
    });

    it('each suffixed key resolves to its own disjoint single-arch set', () => {
      fc.assert(
        fc.property(baseNameArb, suffixSubsetArb, (baseName, suffixes) => {
          // The record's published_component write-back: a components
          // list with one per-JetPack entry per packaged target, plus
          // the record-wide union retained for legacy readers.
          const publishedComponent = {
            supported_architectures: suffixes.map(
              (s) => VLLM_TARGET_SUFFIX_TO_ARCH[s]
            ),
            components: suffixes.map((s) => ({
              component_name: `${baseName}-${s}`,
              supported_architectures: [VLLM_TARGET_SUFFIX_TO_ARCH[s]],
            })),
          };
          // vllmArchs as CreateDeployment.tsx builds it: keyed by the
          // exact suffixed name, resolved via vllmArchsForComponent.
          const vllmArchs: Record<string, string[]> = {};
          for (const s of suffixes) {
            const name = `${baseName}-${s}`;
            vllmArchs[name] = vllmArchsForComponent(name, publishedComponent);
          }

          for (const s of suffixes) {
            const name = `${baseName}-${s}`;
            const ownArch = VLLM_TARGET_SUFFIX_TO_ARCH[s];
            // Suffixed names are still classified/resolved as vLLM.
            expect(classifyGatedComponent(name)).toBe('vllm');
            const resolved = componentSupportedArchs(
              { component_name: name },
              vllmArchs
            );
            // Disjoint single-arch set: exactly the component's own arch.
            expect(resolved).toEqual([ownArch]);
            // Compatible with its own arch and no other fixed-set arch.
            expect(isArchCompatible(ownArch, resolved)).toBe(true);
            for (const other of FIXED_ARCHS) {
              if (other !== ownArch) {
                expect(isArchCompatible(other, resolved)).toBe(false);
              }
            }
            // Fail-closed rules apply to suffixed keys unchanged.
            expect(isArchCompatible(null, resolved)).toBe(false);
          }
        }),
        RUNS
      );
    });

    it('a suffixed key absent from vllmArchs fails closed (empty set)', () => {
      fc.assert(
        fc.property(
          baseNameArb,
          fc.constantFrom(...SUFFIXES),
          (baseName, suffix) => {
            const name = `${baseName}-${suffix}`;
            // Still-resolving / unresolvable record: no entry under the
            // exact suffixed key — no fallback to any other key.
            const resolved = componentSupportedArchs(
              { component_name: name },
              {}
            );
            expect(resolved).toEqual([]);
            expect(
              isArchCompatible(VLLM_TARGET_SUFFIX_TO_ARCH[suffix], resolved)
            ).toBe(false);
          }
        ),
        RUNS
      );
    });

    it('JP7 component is offered and JP6 rejected for an arm64_jp7 device', () => {
      const base = 'model-vllm-qwen3-vl-8b-instruct';
      const publishedComponent = {
        supported_architectures: ['arm64_jp6', 'arm64_jp7'],
        components: [
          {
            component_name: `${base}-jetson-xavier-jp6`,
            supported_architectures: ['arm64_jp6'],
          },
          {
            component_name: `${base}-jetson-xavier-jp7`,
            supported_architectures: ['arm64_jp7'],
          },
        ],
      };
      const vllmArchs: Record<string, string[]> = {
        [`${base}-jetson-xavier-jp6`]: vllmArchsForComponent(
          `${base}-jetson-xavier-jp6`,
          publishedComponent
        ),
        [`${base}-jetson-xavier-jp7`]: vllmArchsForComponent(
          `${base}-jetson-xavier-jp7`,
          publishedComponent
        ),
      };
      const jp7 = componentSupportedArchs(
        { component_name: `${base}-jetson-xavier-jp7` },
        vllmArchs
      );
      const jp6 = componentSupportedArchs(
        { component_name: `${base}-jetson-xavier-jp6` },
        vllmArchs
      );
      expect(isArchCompatible('arm64_jp7', jp7)).toBe(true);
      expect(isArchCompatible('arm64_jp7', jp6)).toBe(false);
      expect(isArchCompatible('arm64_jp6', jp6)).toBe(true);
      expect(isArchCompatible('arm64_jp6', jp7)).toBe(false);
    });
  });

  // Feature: device-arch-compatibility, Property 6: Non-gated exemption
  // Validates: Requirements 3.6, 5.3
  it('Property 6: non-gated components are never classified as gated', () => {
    // Names that are neither model-vllm-* nor dda.plugin.*.
    const nonGatedArb = fc
      .string({ maxLength: 12 })
      .filter(
        (s) => !s.startsWith('model-vllm-') && !s.startsWith('dda.plugin.')
      );
    fc.assert(
      fc.property(nonGatedArb, (name) => {
        expect(classifyGatedComponent(name)).toBeNull();
      }),
      RUNS
    );
  });
});
