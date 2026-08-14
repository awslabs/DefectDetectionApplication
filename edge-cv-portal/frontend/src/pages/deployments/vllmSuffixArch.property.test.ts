/**
 * **Feature: vllm-multi-arch-publish-conflict, Property 6: Fix Checking —
 * Frontend twin of the per-JetPack gate and resolution rules**
 *
 * `vllmArchsForComponent` SHALL be the exact twin of the backend
 * resolution (`vllm_component_architectures` in
 * edge-cv-portal/backend/functions/deployments.py) — the same
 * three-rule order over any `published_component` map:
 *
 * 1. a `published_component.components` entry whose `component_name`
 *    matches → that entry's `supported_architectures`;
 * 2. elif the name carries a known target suffix → `[arch]` when that
 *    arch is in the record-wide set, else `[]` (fail closed);
 * 3. else → the record-wide `supported_architectures`, exactly as
 *    before (Property 2 twin — legacy unsuffixed names).
 *
 * A per-JetPack component SHALL be compatible with its own architecture
 * and with no other, including the null-device-arch and empty-set
 * fail-closed cases (Property 6 twin), and the JP7 component of a
 * JP6+JP7 record SHALL be selectable for an `arm64_jp7` device while
 * the JP6 component is shown incompatible.
 *
 * **Validates: Requirements 2.13, 2.14, 3.4, 3.5, 3.6, 3.7, 3.9**
 *
 * The oracles are computed independently of the module's helpers: the
 * suffix vocabulary is a local copy of `packaging.VLLM_ARCH_TO_TARGET`
 * reversed (not read from `VLLM_TARGET_SUFFIX_TO_ARCH`), and the
 * three-rule resolution is re-derived from the backend's documented
 * order (not via `splitVllmComponentName` / `vllmArchsForComponent`).
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  evaluateVllmArchGate,
  splitVllmComponentName,
  vllmArchsForComponent,
  VLLM_GATE_REASON_ARCH,
  VLLM_GATE_REASON_JP4,
  VLLM_JP4_UNSUPPORTED_MESSAGE,
  describeVllmArchEntry,
  type VllmPerJetPackComponent,
  type VllmPublishedComponentArchSource,
} from './vllmArchGate';

// ---------------------------------------------------------------- oracles

/**
 * Independent suffix oracle: packaging.VLLM_ARCH_TO_TARGET reversed,
 * written out by hand so the test does not trust the module's own
 * VLLM_TARGET_SUFFIX_TO_ARCH table.
 */
const ORACLE_SUFFIX_TO_ARCH: ReadonlyArray<readonly [string, string]> = [
  ['jetson-xavier-jp5', 'arm64_jp5'],
  ['jetson-xavier-jp6', 'arm64_jp6'],
  ['jetson-xavier-jp7', 'arm64_jp7'],
];

/** The arch a name's per-JetPack target suffix names, or null. */
function oracleSuffixArch(name: string): string | null {
  for (const [suffix, arch] of ORACLE_SUFFIX_TO_ARCH) {
    const marker = `-${suffix}`;
    if (name.endsWith(marker) && name.length > marker.length) {
      return arch;
    }
  }
  return null;
}

/**
 * Independent three-rule oracle re-derived from the backend's
 * `vllm_component_architectures` (design step 11), not via the module
 * under test.
 */
function oracleArchs(
  componentName: string | null | undefined,
  published: VllmPublishedComponentArchSource | null | undefined
): string[] {
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
    const arch = oracleSuffixArch(name);
    if (arch !== null) {
      return recordWide.includes(arch) ? [arch] : [];
    }
  }
  return recordWide;
}

// ------------------------------------------------------------- generators

const VLLM_ARCHS = ['arm64_jp5', 'arm64_jp6', 'arm64_jp7'] as const;
const ARCH_TO_SUFFIX: Record<string, string> = {
  arm64_jp5: 'jetson-xavier-jp5',
  arm64_jp6: 'jetson-xavier-jp6',
  arm64_jp7: 'jetson-xavier-jp7',
};

const vllmArchArb = fc.constantFrom<string>(...VLLM_ARCHS);

/** Device architectures the gate sees, including jp4 and the null
 *  fail-closed case. */
const deviceArchArb = fc.constantFrom<string | null>(
  ...VLLM_ARCHS,
  'arm64_jp4',
  'x86_64',
  null
);

/**
 * Legacy/base component names carrying NO known target suffix,
 * including tricky near-miss fragments (`…-jetson-xavier`, `…-jp6`,
 * a bare marker with no base) that must not be misread as suffixes.
 */
const baseNameArb: fc.Arbitrary<string> = fc
  .oneof(
    fc
      .stringMatching(/^[a-z0-9][a-z0-9-]{0,24}$/)
      .map((s) => `model-vllm-${s}`),
    fc.constantFrom(
      'model-vllm-qwen3-vl-8b-instruct',
      'model-vllm-x-jetson-xavier',
      'model-vllm-a-jp6',
      'model-vllm-jetson-xavier-jp8',
      '-jetson-xavier-jp6' // marker with no base: stays legacy
    )
  )
  .filter((name) => oracleSuffixArch(name) === null);

/** A per-JetPack (suffixed) name and the arch its suffix names. */
const suffixedNameArb: fc.Arbitrary<{ name: string; arch: string }> = fc
  .tuple(baseNameArb, vllmArchArb)
  .map(([base, arch]) => ({
    name: `${base}-${ARCH_TO_SUFFIX[arch]}`,
    arch,
  }));

const archListArb = fc.uniqueArray(
  fc.constantFrom<string>(...VLLM_ARCHS, 'x86_64'),
  { maxLength: 4 }
);

/** One write-back `components` entry with an arbitrary name. */
const componentsEntryArb: fc.Arbitrary<VllmPerJetPackComponent> = fc.record(
  {
    component_name: fc.oneof(
      suffixedNameArb.map(({ name }) => name),
      baseNameArb
    ),
    component_version: fc.option(
      fc.integer({ min: 1, max: 9 }).map((n) => `${n}.0.0`),
      { nil: undefined }
    ),
    target: fc.option(
      fc.constantFrom(...Object.values(ARCH_TO_SUFFIX)),
      { nil: undefined }
    ),
    architecture: fc.option(vllmArchArb, { nil: undefined }),
    supported_architectures: fc.option(archListArb, { nil: undefined }),
    component_arn: fc.option(fc.string({ maxLength: 30 }), {
      nil: undefined,
    }),
  },
  { requiredKeys: ['component_name'] }
);

/** Arbitrary `published_component` maps: record-wide set and/or a
 *  per-JetPack components list, each independently absent. */
const publishedArb: fc.Arbitrary<VllmPublishedComponentArchSource | null> =
  fc.oneof(
    fc.constant(null),
    fc.record(
      {
        supported_architectures: fc.option(archListArb, { nil: undefined }),
        components: fc.option(
          fc.array(componentsEntryArb, { maxLength: 4 }),
          { nil: undefined }
        ),
      },
      { requiredKeys: [] }
    )
  );

/** Any component-name input the resolver can be handed. */
const anyNameArb: fc.Arbitrary<string | null | undefined> = fc.oneof(
  baseNameArb,
  suffixedNameArb.map(({ name }) => name),
  fc.constant(null),
  fc.constant(undefined),
  fc.constant('')
);

// ------------------------------------------------------------------ tests

describe(
  'Property 6 twin: vllmArchsForComponent is the exact twin of the ' +
    'backend three-rule resolution',
  () => {
    it(
      'matches the independent three-rule oracle over arbitrary names ' +
        'and published_component maps',
      () => {
        fc.assert(
          fc.property(anyNameArb, publishedArb, (name, published) => {
            expect(vllmArchsForComponent(name, published)).toEqual(
              oracleArchs(name, published)
            );
          }),
          { numRuns: 100 }
        );
      }
    );

    it(
      'rule 1: a matching components entry wins — its own ' +
        'supported_architectures, regardless of suffix or record-wide set',
      () => {
        fc.assert(
          fc.property(
            suffixedNameArb,
            publishedArb,
            archListArb,
            fc.array(componentsEntryArb, { maxLength: 3 }),
            ({ name }, published, entryArchs, otherEntries) => {
              // Entries whose names differ from the queried name, plus
              // ONE matching entry carrying its own set.
              const rest = otherEntries.filter(
                (entry) => String(entry.component_name ?? '') !== name
              );
              const withEntry: VllmPublishedComponentArchSource = {
                ...(published ?? {}),
                components: [
                  ...rest,
                  { component_name: name, supported_architectures: entryArchs },
                ],
              };
              expect(vllmArchsForComponent(name, withEntry)).toEqual(
                entryArchs
              );
            }
          ),
          { numRuns: 100 }
        );
      }
    );

    it(
      'rule 2: a suffixed name with no matching entry resolves to ' +
        '[arch] iff the record-wide set contains it, else [] (fail ' +
        'closed on an out-of-set suffix)',
      () => {
        fc.assert(
          fc.property(
            suffixedNameArb,
            archListArb,
            ({ name, arch }, recordWide) => {
              // Split round-trips the per-JetPack naming first.
              expect(splitVllmComponentName(name).arch).toBe(arch);

              const published: VllmPublishedComponentArchSource = {
                supported_architectures: recordWide,
                components: [], // no rule-1 entry
              };
              const resolved = vllmArchsForComponent(name, published);
              if (recordWide.includes(arch)) {
                expect(resolved).toEqual([arch]);
              } else {
                expect(resolved).toEqual([]);
              }
            }
          ),
          { numRuns: 100 }
        );
      }
    );

    it(
      'rule 3 (Property 2 twin): legacy unsuffixed names resolve to the ' +
        'record-wide set exactly; an unresolvable record resolves to []',
      () => {
        fc.assert(
          fc.property(baseNameArb, publishedArb, (name, published) => {
            // No components entry matches the legacy name.
            const legacy: VllmPublishedComponentArchSource | null =
              published === null
                ? null
                : {
                    ...published,
                    components: (published.components ?? []).filter(
                      (entry) =>
                        String(entry?.component_name ?? '') !== name
                    ),
                  };
            const recordWide = (
              legacy?.supported_architectures ?? []
            ).map(String);
            expect(vllmArchsForComponent(name, legacy)).toEqual(recordWide);

            // Null / absent published_component fails closed to [].
            expect(vllmArchsForComponent(name, null)).toEqual([]);
            expect(vllmArchsForComponent(name, undefined)).toEqual([]);
          }),
          { numRuns: 100 }
        );
      }
    );
  }
);

describe(
  'Property 6 twin: a per-JetPack component gates on its own ' +
    'architecture and no other, fail-closed',
  () => {
    /** A JP-suffixed component resolved through the write-back shape the
     *  publish produces, then fed to the gate twin. */
    const perJetPackCaseArb = fc
      .tuple(suffixedNameArb, fc.string({ maxLength: 20 }))
      .map(([{ name, arch }, device]) => {
        const published: VllmPublishedComponentArchSource = {
          supported_architectures: [...VLLM_ARCHS],
          components: [
            { component_name: name, supported_architectures: [arch] },
          ],
        };
        return { name, arch, device: `device-${device}`, published };
      });

    it(
      'no findings when the device arch equals the component arch; ' +
        'exactly one (component, device) finding otherwise, with the ' +
        'JP4 reason and message for arm64_jp4 devices',
      () => {
        fc.assert(
          fc.property(
            perJetPackCaseArb,
            deviceArchArb,
            ({ name, arch, device, published }, deviceArch) => {
              const archs = vllmArchsForComponent(name, published);
              expect(archs).toEqual([arch]);

              const findings = evaluateVllmArchGate(
                { [name]: { version: '1.0.0', architectures: archs } },
                { [device]: deviceArch }
              );

              if (deviceArch === arch) {
                // Compatible with its OWN architecture (2.14).
                expect(findings).toEqual([]);
                return;
              }

              // …and with NO other: one entry per (component, device)
              // miss, exact-name matching with no fallback (3.9); null
              // device arch fails closed (3.6).
              expect(findings).toHaveLength(1);
              const entry = findings[0];
              expect(entry.component).toBe(name);
              expect(entry.device).toBe(device);
              expect(entry.deviceArch).toBe(deviceArch);
              expect(entry.supported).toEqual([arch]);
              if (deviceArch === 'arm64_jp4') {
                expect(entry.reason).toBe(VLLM_GATE_REASON_JP4);
                expect(describeVllmArchEntry(entry)).toContain(
                  VLLM_JP4_UNSUPPORTED_MESSAGE
                );
              } else {
                expect(entry.reason).toBe(VLLM_GATE_REASON_ARCH);
              }
            }
          ),
          { numRuns: 100 }
        );
      }
    );

    it(
      'an empty resolved set (unresolvable record or out-of-set suffix) ' +
        'fails closed for every device',
      () => {
        fc.assert(
          fc.property(
            suffixedNameArb,
            deviceArchArb,
            ({ name, arch }, deviceArch) => {
              // Out-of-set suffix: the record-wide set lacks the arch the
              // suffix names, so resolution yields [] (rule 2 fail-closed).
              const published: VllmPublishedComponentArchSource = {
                supported_architectures: VLLM_ARCHS.filter(
                  (a) => a !== arch
                ),
              };
              const archs = vllmArchsForComponent(name, published);
              expect(archs).toEqual([]);

              // Empty set → every device incompatible (3.7).
              const findings = evaluateVllmArchGate(
                { [name]: { version: null, architectures: archs } },
                { device: deviceArch }
              );
              expect(findings).toHaveLength(1);
              expect(findings[0].supported).toEqual([]);
            }
          ),
          { numRuns: 100 }
        );
      }
    );

    it(
      'a JP6+JP7 record shows its JP7 component as selectable for an ' +
        'arm64_jp7 device and its JP6 component as incompatible (2.14)',
      () => {
        fc.assert(
          fc.property(baseNameArb, (base) => {
            const jp6Name = `${base}-jetson-xavier-jp6`;
            const jp7Name = `${base}-jetson-xavier-jp7`;
            const published: VllmPublishedComponentArchSource = {
              supported_architectures: ['arm64_jp6', 'arm64_jp7'],
              components: [
                {
                  component_name: jp6Name,
                  supported_architectures: ['arm64_jp6'],
                },
                {
                  component_name: jp7Name,
                  supported_architectures: ['arm64_jp7'],
                },
              ],
            };
            const devices = { 'jetson-thor1': 'arm64_jp7' };

            // JP7 component: selectable (no findings).
            expect(
              evaluateVllmArchGate(
                {
                  [jp7Name]: {
                    version: '1.0.0',
                    architectures: vllmArchsForComponent(jp7Name, published),
                  },
                },
                devices
              )
            ).toEqual([]);

            // JP6 component: shown incompatible.
            const findings = evaluateVllmArchGate(
              {
                [jp6Name]: {
                  version: '1.0.0',
                  architectures: vllmArchsForComponent(jp6Name, published),
                },
              },
              devices
            );
            expect(findings).toHaveLength(1);
            expect(findings[0].component).toBe(jp6Name);
            expect(findings[0].deviceArch).toBe('arm64_jp7');
            expect(findings[0].supported).toEqual(['arm64_jp6']);
          }),
          { numRuns: 100 }
        );
      }
    );
  }
);
