/**
 * **Feature: vllm-multi-arch-publish-conflict, Property 6: Fix Checking —
 * Frontend twin of the per-JetPack gate and resolution rules**
 *
 * `vllmArchsForComponent` SHALL be the exact twin of the backend
 * architecture resolution (`vllm_component_architectures` in
 * edge-cv-portal/backend/functions/deployments.py), applying the same
 * three rules in the same order over a record's `published_component`
 * map:
 *
 *   1. a `published_component.components` entry whose `component_name`
 *      matches → that entry's `supported_architectures`;
 *   2. elif the name carries a known target suffix → `[arch]` when that
 *      arch is in the record-wide set, else `[]` (fail closed on an
 *      out-of-set suffix);
 *   3. else → the record-wide `supported_architectures`.
 *
 * Composed with `evaluateVllmArchGate`, a Per_JetPack_Component SHALL be
 * compatible with exactly its own architecture and no other, a device
 * with a null recorded architecture SHALL fail closed, an empty resolved
 * set SHALL fail every device closed, and legacy unsuffixed names SHALL
 * keep resolving to the record-wide set (Property 2 twin).
 *
 * **Validates: Requirements 2.13, 2.14, 3.4, 3.5, 3.6, 3.7, 3.9**
 *
 * The oracles are computed independently of vllmArchGate.ts: the suffix
 * vocabulary, the three-rule order, and gate compatibility are re-derived
 * here from the backend's rules (packaging.VLLM_ARCH_TO_TARGET reversed,
 * design step 11) rather than by calling the module's own helpers.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  evaluateVllmArchGate,
  splitVllmComponentName,
  vllmArchsForComponent,
  VLLM_GATE_REASON_ARCH,
  VLLM_GATE_REASON_JP4,
  type VllmPerJetPackComponent,
  type VllmPublishedComponentArchSource,
} from './vllmArchGate';

// ---------------------------------------------------------------- oracles

/**
 * Independent copy of the closed suffix vocabulary — the reverse of the
 * backend's `packaging.VLLM_ARCH_TO_TARGET` with `_` → `-` (design step
 * 9). Deliberately NOT imported from vllmArchGate.ts so a drifted module
 * map is caught.
 */
const ORACLE_SUFFIX_TO_ARCH: ReadonlyArray<readonly [string, string]> = [
  ['jetson-xavier-jp5', 'arm64_jp5'],
  ['jetson-xavier-jp6', 'arm64_jp6'],
  ['jetson-xavier-jp7', 'arm64_jp7'],
];

const ORACLE_ARCH_TO_SUFFIX: Record<string, string> = Object.fromEntries(
  ORACLE_SUFFIX_TO_ARCH.map(([suffix, arch]) => [arch, suffix])
);

/** Independent name-split oracle (backend `split_vllm_component_name`). */
function oracleSplit(name: string): { baseName: string; arch: string | null } {
  for (const [suffix, arch] of ORACLE_SUFFIX_TO_ARCH) {
    const marker = `-${suffix}`;
    if (name.endsWith(marker) && name.length > marker.length) {
      return { baseName: name.slice(0, -marker.length), arch };
    }
  }
  return { baseName: name, arch: null };
}

/**
 * Independent three-rule resolution oracle (backend
 * `vllm_component_architectures`, design step 11).
 */
function oracleArchsForComponent(
  componentName: string | null | undefined,
  published: VllmPublishedComponentArchSource | null | undefined
): string[] {
  const recordWide = (published?.supported_architectures ?? []).map(String);
  const name = componentName == null ? '' : String(componentName);
  if (name) {
    // Rule 1: a components entry whose component_name matches (first wins).
    for (const entry of published?.components ?? []) {
      if (!entry || typeof entry !== 'object') {
        continue;
      }
      if (String(entry.component_name ?? '') === name) {
        return (entry.supported_architectures ?? []).map(String);
      }
    }
    // Rule 2: a known target suffix — [arch] iff in the record-wide set.
    const { arch } = oracleSplit(name);
    if (arch !== null) {
      return recordWide.includes(arch) ? [arch] : [];
    }
  }
  // Rule 3: record-wide set, exactly as before the fix.
  return recordWide;
}

// ------------------------------------------------------------- generators

/** Suffix-mapped Jetson architectures the publish can produce. */
const SUFFIXED_ARCHES = ['arm64_jp5', 'arm64_jp6', 'arm64_jp7'] as const;

/** Architectures a device or record can carry, mapped and unmapped alike. */
const ALL_ARCHES = [...SUFFIXED_ARCHES, 'arm64_jp4', 'x86_64'] as const;

const archArb = fc.constantFrom(...ALL_ARCHES);

/**
 * Base_Component_Name from `derive_vllm_component_name`: alphanumeric
 * tail, so a bare base can never accidentally end in a target suffix.
 */
const baseNameArb = fc
  .stringMatching(/^[a-z0-9]{1,16}$/)
  .map((s) => `model-vllm-${s}`);

const suffixArb = fc.constantFrom(
  ...ORACLE_SUFFIX_TO_ARCH.map(([suffix]) => suffix)
);

/**
 * Component names across every resolution path: bare legacy bases,
 * per-JetPack suffixed names, doubly-suffixed and suffix-only oddities,
 * and null/undefined/empty.
 */
const anyNameArb: fc.Arbitrary<string | null | undefined> = fc.oneof(
  baseNameArb,
  fc.tuple(baseNameArb, suffixArb).map(([base, suffix]) => `${base}-${suffix}`),
  fc
    .tuple(baseNameArb, suffixArb, suffixArb)
    .map(([base, s1, s2]) => `${base}-${s1}-${s2}`),
  suffixArb, // a bare suffix with no leading dash — not a suffixed name
  fc.constant(''),
  fc.constant(null),
  fc.constant(undefined)
);

/** One arbitrary `published_component.components` entry. */
const componentsEntryArb: fc.Arbitrary<VllmPerJetPackComponent> = fc.record(
  {
    component_name: fc.oneof(
      baseNameArb,
      fc
        .tuple(baseNameArb, suffixArb)
        .map(([base, suffix]) => `${base}-${suffix}`),
      fc.constant(null)
    ),
    component_version: fc.option(fc.constant('1.0.0'), { nil: undefined }),
    target: fc.option(suffixArb, { nil: undefined }),
    architecture: fc.option(archArb, { nil: undefined }),
    supported_architectures: fc.oneof(
      fc.array(archArb, { maxLength: 3 }),
      fc.constant(null),
      fc.constant(undefined)
    ),
    component_arn: fc.option(fc.string({ maxLength: 20 }), { nil: undefined }),
  },
  { requiredKeys: [] }
);

/** An arbitrary `published_component` map, sparse fields included. */
const publishedArb: fc.Arbitrary<VllmPublishedComponentArchSource | null> =
  fc.oneof(
    fc.record(
      {
        supported_architectures: fc.oneof(
          fc.array(archArb, { maxLength: 4 }),
          fc.constant(null),
          fc.constant(undefined)
        ),
        components: fc.oneof(
          fc.array(componentsEntryArb, { maxLength: 4 }),
          fc.constant(null),
          fc.constant(undefined)
        ),
      },
      { requiredKeys: [] }
    ),
    fc.constant(null)
  );

/**
 * A publish write-back exactly as greengrass_publish.py's fixed
 * write-back records it (design step 7): record-wide union of the
 * packaged architectures, plus one per-JetPack `components` entry per
 * architecture with `supported_architectures: [a]`.
 */
interface WriteBack {
  base: string;
  archs: string[];
  published: VllmPublishedComponentArchSource;
}

const writeBackArb: fc.Arbitrary<WriteBack> = fc
  .tuple(
    baseNameArb,
    fc.uniqueArray(fc.constantFrom(...SUFFIXED_ARCHES), {
      minLength: 1,
      maxLength: 3,
    })
  )
  .map(([base, archs]) => ({
    base,
    archs,
    published: {
      supported_architectures: [...archs],
      components: archs.map((arch) => ({
        component_name: `${base}-${ORACLE_ARCH_TO_SUFFIX[arch]}`,
        component_version: '1.0.0',
        target: ORACLE_ARCH_TO_SUFFIX[arch],
        architecture: arch,
        supported_architectures: [arch],
        component_arn: `arn:aws:greengrass:us-east-1:000000000000:components:${base}-${ORACLE_ARCH_TO_SUFFIX[arch]}`,
      })),
    },
  }));

/** Device fleets mixing every architecture with the null/absent case. */
const deviceArchsArb: fc.Arbitrary<Record<string, string | null>> =
  fc.dictionary(
    fc.stringMatching(/^[a-z0-9-]{1,12}$/),
    fc.oneof(archArb, fc.constant(null)),
    { minKeys: 1, maxKeys: 4 }
  );

// ------------------------------------------------------------------ tests

describe('Property 6 twin: frontend suffix-arch resolution', () => {
  it(
    'vllmArchsForComponent applies exactly the backend three-rule order ' +
      'over any name and published_component map',
    () => {
      fc.assert(
        fc.property(anyNameArb, publishedArb, (name, published) => {
          // Req 2.13, 3.4, 3.5: same rules, same order, same output.
          expect(vllmArchsForComponent(name, published)).toEqual(
            oracleArchsForComponent(name, published)
          );
        }),
        { numRuns: 100 }
      );
    }
  );

  it('splitVllmComponentName round-trips base + suffix and leaves legacy names whole', () => {
    fc.assert(
      fc.property(baseNameArb, suffixArb, (base, suffix) => {
        // A suffixed per-JetPack name splits back to its base and arch
        // (Req 2.13).
        const suffixed = splitVllmComponentName(`${base}-${suffix}`);
        expect(suffixed).toEqual({
          baseName: base,
          arch: oracleSplit(`${base}-${suffix}`).arch,
        });
        expect(suffixed.arch).not.toBeNull();

        // A legacy unsuffixed name is returned verbatim with a null arch
        // (Req 3.4).
        expect(splitVllmComponentName(base)).toEqual({
          baseName: base,
          arch: null,
        });
      }),
      { numRuns: 100 }
    );
  });

  it(
    'a per-JetPack component from the publish write-back is compatible ' +
      'with its own architecture and no other, null device archs fail ' +
      'closed, and jp4 misses carry the JetPack-4 reason',
    () => {
      fc.assert(
        fc.property(writeBackArb, deviceArchsArb, ({ base, archs, published }, deviceArchs) => {
          for (const arch of archs) {
            const name = `${base}-${ORACLE_ARCH_TO_SUFFIX[arch]}`;

            // The write-back entry resolves to exactly its own arch
            // (Req 2.13, 2.14).
            const resolved = vllmArchsForComponent(name, published);
            expect(resolved).toEqual([arch]);

            // Gate the component over the fleet (Req 2.14, 3.6, 3.9).
            const findings = evaluateVllmArchGate(
              { [name]: { version: '1.0.0', architectures: resolved } },
              deviceArchs
            );

            for (const [device, deviceArch] of Object.entries(deviceArchs)) {
              const misses = findings.filter(
                (entry) => entry.component === name && entry.device === device
              );
              if (deviceArch === arch) {
                // Compatible with its own arch: no finding (Req 2.14).
                expect(misses).toHaveLength(0);
              } else {
                // Every other arch — and the null-arch device — fails
                // closed with exactly one entry per (component, device)
                // miss (Req 3.6, 3.9).
                expect(misses).toHaveLength(1);
                expect(misses[0].deviceArch).toBe(deviceArch);
                expect(misses[0].supported).toEqual([arch]);
                expect(misses[0].reason).toBe(
                  deviceArch === 'arm64_jp4'
                    ? VLLM_GATE_REASON_JP4
                    : VLLM_GATE_REASON_ARCH
                );
              }
            }
          }
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'an out-of-set suffix and a missing components entry resolve to the ' +
      'empty set, which fails every device closed',
    () => {
      // A suffixed name whose arch the record never packaged: rule 1
      // finds no entry (the write-back only lists packaged archs) and
      // rule 2 fails closed because the arch is outside the record-wide
      // set (Req 3.7).
      const outOfSetArb = writeBackArb
        .map((writeBack) => {
          const missing = SUFFIXED_ARCHES.filter(
            (arch) => !writeBack.archs.includes(arch)
          );
          return { writeBack, missing };
        })
        .filter(({ missing }) => missing.length > 0);

      fc.assert(
        fc.property(outOfSetArb, deviceArchsArb, ({ writeBack, missing }, deviceArchs) => {
          for (const arch of missing) {
            const name = `${writeBack.base}-${ORACLE_ARCH_TO_SUFFIX[arch]}`;
            const resolved = vllmArchsForComponent(name, writeBack.published);
            expect(resolved).toEqual([]);

            // Empty set → every device incompatible, one entry each
            // (Req 3.7, 3.9).
            const findings = evaluateVllmArchGate(
              { [name]: { version: null, architectures: resolved } },
              deviceArchs
            );
            expect(findings).toHaveLength(Object.keys(deviceArchs).length);
            for (const entry of findings) {
              expect(entry.component).toBe(name);
              expect(entry.supported).toEqual([]);
            }
          }
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'legacy unsuffixed names keep resolving to the record-wide set and ' +
      'gate exactly as before (Property 2 twin)',
    () => {
      // A legacy record: record-wide set only, components absent or
      // listing OTHER names — never the queried base name.
      const legacyArb = fc
        .tuple(
          baseNameArb,
          fc.array(archArb, { maxLength: 4 }),
          fc.oneof(
            fc.constant(undefined),
            fc.array(componentsEntryArb, { maxLength: 3 })
          )
        )
        .map(([base, recordWide, components]) => ({
          base,
          recordWide,
          published: {
            supported_architectures: recordWide,
            components: components?.filter(
              (entry) => String(entry.component_name ?? '') !== base
            ),
          } as VllmPublishedComponentArchSource,
        }));

      fc.assert(
        fc.property(legacyArb, deviceArchsArb, ({ base, recordWide, published }, deviceArchs) => {
          // Rule 3: the record-wide set, verbatim (Req 3.4, 3.5).
          const resolved = vllmArchsForComponent(base, published);
          expect(resolved).toEqual(recordWide);

          // The gate keeps its exact-name, no-fallback semantics over
          // the record-wide set (Req 3.5, 3.9).
          const supported = new Set<string>(recordWide);
          const findings = evaluateVllmArchGate(
            { [base]: { version: null, architectures: resolved } },
            deviceArchs
          );
          for (const [device, deviceArch] of Object.entries(deviceArchs)) {
            const misses = findings.filter(
              (entry) => entry.component === base && entry.device === device
            );
            const compatible =
              deviceArch !== null && supported.has(deviceArch);
            expect(misses).toHaveLength(compatible ? 0 : 1);
          }
        }),
        { numRuns: 100 }
      );
    }
  );
});
