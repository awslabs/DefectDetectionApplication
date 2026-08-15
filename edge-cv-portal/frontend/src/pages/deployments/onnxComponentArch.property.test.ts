/**
 * Per-JetPack ONNX component architecture inference — example pinning
 * (onnx-jetson-publish-packaging task 1, case 6 / `isBugCondition_4`).
 *
 * These example cases pin `inferComponentTargetArchs` for the new
 * per-JetPack ONNX component names (`model-{safe}-onnx-jetson-xavier-jp{N}`).
 * They PASS on the unfixed tree: the generic JetPack-token regex
 * `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/` already covers the names, so the
 * fix here is PINNING (tests + comment), not production code — that is
 * exactly what these cases document. The full fast-check property suite
 * (Correctness Property 7) is added by task 4.5 in this same file.
 *
 * The PRESERVATION block below (task 2, Property 2 / Requirement 3.13)
 * pins that every EXISTING (non-ONNX) component-name shape keeps resolving
 * to exactly today's architecture set — observed on the unfixed tree and
 * encoded as fast-check properties that must keep passing after the fix.
 *
 * Validates: Requirements 1.8 (expected behavior 2.11), 3.13
 */
import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import {
  inferComponentTargetArchs,
  isArchCompatible,
  isCompatibleWithAllDevices,
} from './archCompatibility';

describe('per-JetPack ONNX component arch inference — example pinning (task 1 case 6)', () => {
  // Validates: Requirements 1.8 (expected behavior 2.11)
  it('model-x-onnx-jetson-xavier-jp7 infers exactly [arm64_jp7]', () => {
    expect(
      inferComponentTargetArchs('model-x-onnx-jetson-xavier-jp7')
    ).toEqual(['arm64_jp7']);
  });

  // Validates: Requirements 1.8 (expected behavior 2.11)
  it('model-x-onnx-jetson-xavier-jp6 infers exactly [arm64_jp6]', () => {
    expect(
      inferComponentTargetArchs('model-x-onnx-jetson-xavier-jp6')
    ).toEqual(['arm64_jp6']);
  });

  // Validates: Requirements 1.8 (expected behavior 2.11)
  it('model-x-onnx-jetson-xavier-jp5 infers exactly [arm64_jp5]', () => {
    expect(
      inferComponentTargetArchs('model-x-onnx-jetson-xavier-jp5')
    ).toEqual(['arm64_jp5']);
  });
});

// ---------------------------------------------------------------------------
// Preservation (task 2, Property 2): existing-name inference is unchanged
// ---------------------------------------------------------------------------

const RUNS = { numRuns: 100 };

// Safe name fragments that can never form a JetPack token: the alphabet
// excludes 'j' and 'p' entirely (matched case-insensitively), so neither
// 'jp' nor 'jetpack' can appear — the only token in a composed name is the
// one the template supplies. A smart generator, not a filtered one.
const TOKEN_FREE_ALPHABET = 'abcdefghikmnoqrstuvwxyz0123456789-';
const safeFragmentArb = fc
  .array(fc.constantFrom(...TOKEN_FREE_ALPHABET.split('')), {
    minLength: 1,
    maxLength: 16,
  })
  .map((chars) => chars.join(''))
  .filter((s) => !s.startsWith('-') && !s.endsWith('-'));

const jetpackMajorArb = fc.constantFrom('4', '5', '6', '7');
const vllmNeoMajorArb = fc.constantFrom('5', '6', '7');
const neoVisionMajorArb = fc.constantFrom('5', '6');

describe('existing-name arch inference — preservation baseline (task 2, Property 2)', () => {
  // Validates: Requirements 3.13
  it('LocalServer variants keep resolving to their singleton JetPack arch', () => {
    fc.assert(
      fc.property(jetpackMajorArb, (major) => {
        expect(
          inferComponentTargetArchs(`aws.edgeml.dda.LocalServer.arm64JP${major}`)
        ).toEqual([`arm64_jp${major}`]);
      }),
      RUNS
    );
    // The amd64 LocalServer carries no JetPack token: empty set, unchanged.
    expect(inferComponentTargetArchs('aws.edgeml.dda.LocalServer.amd64')).toEqual(
      []
    );
  });

  // Validates: Requirements 3.13
  it('per-JetPack vLLM suffixed names keep resolving to their singleton arch', () => {
    fc.assert(
      fc.property(safeFragmentArb, vllmNeoMajorArb, (safe, major) => {
        expect(
          inferComponentTargetArchs(
            `model-vllm-${safe}-jetson-xavier-jp${major}`
          )
        ).toEqual([`arm64_jp${major}`]);
      }),
      RUNS
    );
  });

  // Validates: Requirements 3.13
  it('Neo vision per-target names keep resolving exactly as today', () => {
    fc.assert(
      fc.property(safeFragmentArb, neoVisionMajorArb, (safe, major) => {
        // JetPack-suffixed Neo names: the singleton arch of their token.
        expect(
          inferComponentTargetArchs(`model-${safe}-jetson-xavier-jp${major}`)
        ).toEqual([`arm64_jp${major}`]);
      }),
      RUNS
    );
    fc.assert(
      fc.property(safeFragmentArb, (safe) => {
        // The legacy JP4 id 'jetson-xavier' and the x86 targets carry NO
        // JetPack token: empty set (left to the coarse arm64/amd64 filter).
        expect(
          inferComponentTargetArchs(`model-${safe}-jetson-xavier`)
        ).toEqual([]);
        expect(inferComponentTargetArchs(`model-${safe}-x86-64-cpu`)).toEqual(
          []
        );
      }),
      RUNS
    );
  });

  // Validates: Requirements 3.13
  it('token-less names keep yielding the empty architecture set', () => {
    fc.assert(
      fc.property(safeFragmentArb, (safe) => {
        expect(inferComponentTargetArchs(`model-${safe}`)).toEqual([]);
        expect(inferComponentTargetArchs(safe)).toEqual([]);
      }),
      RUNS
    );
  });
});

// ---------------------------------------------------------------------------
// Correctness Property 7 (task 4.5): fix checking — per-JetPack ONNX arch
// inference and deploy-screen compatibility verdicts are pinned
// ---------------------------------------------------------------------------

// The per-JetPack compiled-ONNX targets cover exactly JetPack 5/6/7
// (packaging.ONNX_ARCH_TO_TARGET → onnx-jetson-xavier-jp{5,6,7}).
const onnxMajorArb = fc.constantFrom('5', '6', '7');

// Every recorded device Target_Architecture the deploy screen may see,
// plus coarse/noise values, so "every other arch" is genuinely exercised
// (same fixed set as archCompatibility.property.test.ts).
const ALL_DEVICE_ARCHS = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
  'arm64',
  'aarch64',
  'amd64',
];
const anyDeviceArchArb = fc.option(fc.constantFrom(...ALL_DEVICE_ARCHS), {
  nil: null,
});

describe('per-JetPack ONNX arch inference — Property 7 (fix checking, task 4.5)', () => {
  // Feature: onnx-jetson-publish-packaging, Property 7: Fix Checking
  // Validates: Requirements 2.11
  it('Property 7: inference is exactly the singleton arch of the JetPack token', () => {
    fc.assert(
      fc.property(safeFragmentArb, onnxMajorArb, (safe, major) => {
        expect(
          inferComponentTargetArchs(
            `model-${safe}-onnx-jetson-xavier-jp${major}`
          )
        ).toEqual([`arm64_jp${major}`]);
      }),
      RUNS
    );
  });

  // Feature: onnx-jetson-publish-packaging, Property 7: Fix Checking
  // Validates: Requirements 2.11, 2.12
  it('Property 7: own arch is compatible; every other arch and a null device arch fail closed', () => {
    fc.assert(
      fc.property(safeFragmentArb, onnxMajorArb, (safe, major) => {
        const inferred = inferComponentTargetArchs(
          `model-${safe}-onnx-jetson-xavier-jp${major}`
        );
        const own = `arm64_jp${major}`;
        // The component's own architecture is compatible.
        expect(isArchCompatible(own, inferred)).toBe(true);
        // Every other recorded architecture fails closed — including the
        // adjacent JetPack majors and the coarse arm64/aarch64 values.
        for (const other of ALL_DEVICE_ARCHS) {
          if (other !== own) {
            expect(isArchCompatible(other, inferred)).toBe(false);
          }
        }
        // A device with no recorded architecture fails closed.
        expect(isArchCompatible(null, inferred)).toBe(false);
      }),
      RUNS
    );
  });

  // Feature: onnx-jetson-publish-packaging, Property 7: Fix Checking
  // Validates: Requirements 2.12
  it('Property 7: all-devices verdict is exact — true iff every selected device is the own arch', () => {
    fc.assert(
      fc.property(
        safeFragmentArb,
        onnxMajorArb,
        fc.array(anyDeviceArchArb, { maxLength: 6 }),
        (safe, major, deviceArchs) => {
          const inferred = inferComponentTargetArchs(
            `model-${safe}-onnx-jetson-xavier-jp${major}`
          );
          const own = `arm64_jp${major}`;
          const expected = deviceArchs.every((arch) => arch === own);
          expect(isCompatibleWithAllDevices(inferred, deviceArchs)).toBe(
            expected
          );
        }
      ),
      RUNS
    );
  });

  // Feature: onnx-jetson-publish-packaging, Property 7: Fix Checking
  // (Property 2 twin: existing-name inference unchanged)
  // Validates: Requirements 3.13
  it('Property 7: existing (non-ONNX) name inference is unchanged', () => {
    // A composed (name, expected-arch-set) generator over today's name
    // shapes: LocalServer variants, per-JetPack vLLM suffixed names, Neo
    // vision per-target names, and token-less names — the same shapes the
    // preservation block observed on the unfixed tree.
    const existingNameCaseArb = fc.oneof(
      jetpackMajorArb.map((major) => ({
        name: `aws.edgeml.dda.LocalServer.arm64JP${major}`,
        expected: [`arm64_jp${major}`],
      })),
      fc
        .tuple(safeFragmentArb, vllmNeoMajorArb)
        .map(([safe, major]) => ({
          name: `model-vllm-${safe}-jetson-xavier-jp${major}`,
          expected: [`arm64_jp${major}`],
        })),
      fc
        .tuple(safeFragmentArb, neoVisionMajorArb)
        .map(([safe, major]) => ({
          name: `model-${safe}-jetson-xavier-jp${major}`,
          expected: [`arm64_jp${major}`],
        })),
      safeFragmentArb.map((safe) => ({
        name: `model-${safe}-jetson-xavier`,
        expected: [] as string[],
      })),
      safeFragmentArb.map((safe) => ({
        name: `model-${safe}`,
        expected: [] as string[],
      }))
    );
    fc.assert(
      fc.property(existingNameCaseArb, ({ name, expected }) => {
        expect(inferComponentTargetArchs(name)).toEqual(expected);
      }),
      RUNS
    );
  });
});
