/**
 * Property tests for the Aravis camera picker helpers
 * (aravis-camera-input tasks 6.3 and 6.4).
 *
 * **Feature: aravis-camera-input, Property 7: Aravis picker compatibility filter**
 *
 * For any list of Camera_Registry entries, the Aravis picker option
 * list SHALL contain exactly the entries that are Aravis-compatible
 * (type `AravisDiscovered`, or type `Camera` carrying a non-empty
 * camera id parameter) — no incompatible entry offered, no compatible
 * entry omitted.
 *
 * **Validates: Requirements 3.2**
 *
 * **Feature: aravis-camera-input, Property 8: Aravis selection populates the node and records the hint**
 *
 * For any Aravis-compatible Camera_Source and any prior parameter
 * record, applying the selection SHALL set `camera_id` to the source's
 * camera id, copy `gain` and `exposure` exactly when the source's
 * params carry them as numbers, leave all other parameters untouched,
 * and produce a binding hint carrying the source id, display name, and
 * reference device id.
 *
 * **Validates: Requirements 3.3**
 *
 * The functions under test are the pure `isAravisCompatibleCamera` and
 * `applyAravisCameraSelection` from `cameraReference.ts` — the filter
 * feeding the Aravis picker's option list and the single place an
 * Aravis Camera_Source is applied to an Aravis_Camera_Source_Node's
 * parameters. Both properties assert against independent oracles that
 * restate the specified semantics.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  applyAravisCameraSelection,
  cameraIdValue,
  isAravisCompatibleCamera,
  type CameraSourceEntry,
} from './cameraReference';
import type { JsonValue } from './types';

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** Arbitrary JSON parameter values, including nested arrays/objects. */
const jsonValueArb: fc.Arbitrary<JsonValue> = fc.oneof(
  { maxDepth: 2 },
  fc.string({ unit: 'grapheme' }),
  fc.double({ noNaN: true }),
  fc.integer(),
  fc.boolean(),
  fc.constant(null),
  fc.array(fc.oneof(fc.string(), fc.integer(), fc.boolean(), fc.constant(null)), {
    maxLength: 3,
  }),
  fc.dictionary(fc.string({ maxLength: 5 }), fc.oneof(fc.string(), fc.integer()), {
    maxKeys: 3,
  })
);

/** Identifiers: non-empty, unicode allowed. */
const idArb = fc.string({ unit: 'grapheme', minLength: 1, maxLength: 20 });

/**
 * The source's params record: `cameraId` either absent or carrying a
 * value of an arbitrary JSON type (so empty strings and non-string ids
 * are exercised), gain/exposure numeric or arbitrary, plus extra keys.
 * The whole record may also be null or absent.
 */
const paramsArb: fc.Arbitrary<Record<string, JsonValue> | null | undefined> = fc.oneof(
  fc.constant(null),
  fc.constant(undefined),
  fc
    .record(
      {
        cameraId: fc.oneof(
          fc.string({ unit: 'grapheme', maxLength: 30 }),
          jsonValueArb
        ),
        gain: fc.oneof(fc.double({ noNaN: true }), fc.integer(), jsonValueArb),
        exposure: fc.oneof(fc.double({ noNaN: true }), fc.integer(), jsonValueArb),
      },
      { requiredKeys: [] }
    )
    .chain((known) =>
      fc
        .dictionary(fc.string({ minLength: 1, maxLength: 8 }), jsonValueArb, { maxKeys: 3 })
        .map((extra) => ({ ...extra, ...known }))
    )
);

/**
 * An arbitrary Camera_Registry entry of any type — Aravis-compatible
 * types, incompatible types (V4L2Discovered, RTSP, CSI), unknown type
 * strings, and absent/null types — with arbitrary params.
 */
const anyCameraArb: fc.Arbitrary<CameraSourceEntry> = fc.record(
  {
    camera_source_id: idArb,
    name: fc.oneof(fc.string({ unit: 'grapheme', maxLength: 20 }), fc.constant(null)),
    type: fc.oneof(
      fc.constantFrom('AravisDiscovered', 'Camera', 'V4L2Discovered', 'RTSP', 'CSI', 'ICam'),
      fc.string({ maxLength: 12 }),
      fc.constant(null)
    ),
    params: paramsArb,
    origin: fc.constantFrom('edge-configured', 'edge-discovered', 'portal-created'),
    sync_status: fc.constantFrom('synced', 'pending', 'failed'),
    stale: fc.boolean(),
    absent: fc.boolean(),
  },
  { requiredKeys: ['camera_source_id'] }
);

/**
 * An Aravis-compatible entry: type `AravisDiscovered` (cameraId of any
 * shape, including absent), or type `Camera` whose params carry a
 * non-empty string `cameraId`.
 */
const compatibleCameraArb: fc.Arbitrary<CameraSourceEntry> = fc.oneof(
  anyCameraArb.map((camera) => ({ ...camera, type: 'AravisDiscovered' })),
  fc
    .tuple(anyCameraArb, fc.string({ unit: 'grapheme', minLength: 1, maxLength: 30 }))
    .map(([camera, cameraId]) => ({
      ...camera,
      type: 'Camera',
      params: { ...(camera.params ?? {}), cameraId },
    }))
);

/** Prior node parameters, sometimes already carrying camera_id/gain/exposure. */
const priorParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc
  .dictionary(fc.string({ minLength: 1, maxLength: 8 }), jsonValueArb, { maxKeys: 4 })
  .chain((base) =>
    fc
      .record(
        {
          camera_id: jsonValueArb,
          gain: jsonValueArb,
          exposure: jsonValueArb,
        },
        { requiredKeys: [] }
      )
      .map((known) => ({ ...base, ...known }))
  );

// --------------------------------------------------------------------------
// Oracles (restate the specified semantics independently)
// --------------------------------------------------------------------------

/** Requirement 3.2 compatibility, restated from the acceptance criterion. */
function compatibleOracle(camera: CameraSourceEntry): boolean {
  if (camera.type === 'AravisDiscovered') {
    return true;
  }
  if (camera.type !== 'Camera') {
    return false;
  }
  const cameraId = (camera.params ?? {}).cameraId;
  return typeof cameraId === 'string' && cameraId !== '';
}

/** The camera id the selection populates, restated (non-empty string). */
function expectedCameraId(
  camera: CameraSourceEntry,
  prior: Record<string, JsonValue>
): { present: boolean; value?: JsonValue } {
  const cameraId = (camera.params ?? {}).cameraId;
  if (typeof cameraId === 'string' && cameraId !== '') {
    return { present: true, value: cameraId };
  }
  // The source carries no camera id: the prior value (or absence) is retained.
  return Object.prototype.hasOwnProperty.call(prior, 'camera_id')
    ? { present: true, value: prior.camera_id }
    : { present: false };
}

// --------------------------------------------------------------------------
// Property 7: Aravis picker compatibility filter
// --------------------------------------------------------------------------

describe('Property 7: Aravis picker compatibility filter', () => {
  it('offers exactly the Aravis-compatible entries — soundness and completeness', () => {
    fc.assert(
      fc.property(fc.array(anyCameraArb, { maxLength: 12 }), (cameras) => {
        const offered = cameras.filter(isAravisCompatibleCamera);

        // Soundness: every offered entry is compatible per the oracle.
        for (const camera of offered) {
          expect(compatibleOracle(camera)).toBe(true);
        }

        // Completeness: no compatible entry is omitted — the offered
        // list is exactly the oracle-filtered list, order preserved.
        expect(offered).toEqual(cameras.filter(compatibleOracle));

        // Per-entry agreement (covers entries the list filter shares).
        for (const camera of cameras) {
          expect(isAravisCompatibleCamera(camera)).toBe(compatibleOracle(camera));
        }
      }),
      { numRuns: 100 }
    );
  });
});

// --------------------------------------------------------------------------
// Property 8: Aravis selection populates the node and records the hint
// --------------------------------------------------------------------------

describe('Property 8: Aravis selection populates the node and records the hint', () => {
  it('sets camera_id, copies numeric gain/exposure, leaves the rest untouched, and records the hint', () => {
    fc.assert(
      fc.property(
        priorParametersArb,
        compatibleCameraArb,
        idArb,
        (prior, camera, sourceDeviceId) => {
          const priorSnapshot = structuredClone(prior);
          const cameraSnapshot = structuredClone(camera);

          const { parameters, hint } = applyAravisCameraSelection(
            prior,
            camera,
            sourceDeviceId
          );

          // Purity: neither input is mutated.
          expect(prior).toEqual(priorSnapshot);
          expect(camera).toEqual(cameraSnapshot);

          const params = camera.params ?? {};

          // camera_id is set from the source's camera id; a source
          // without one retains the prior value (or absence).
          const cameraId = expectedCameraId(camera, priorSnapshot);
          if (cameraId.present) {
            expect(parameters.camera_id).toEqual(cameraId.value);
          } else {
            expect(Object.prototype.hasOwnProperty.call(parameters, 'camera_id')).toBe(
              false
            );
          }
          // Sanity: cameraIdValue agrees with the populated value when
          // the source carries a usable id.
          if (cameraIdValue(camera) !== null) {
            expect(parameters.camera_id).toBe(cameraIdValue(camera));
          }

          // gain/exposure copied exactly when the source's params carry
          // them as numbers, untouched otherwise.
          for (const key of ['gain', 'exposure'] as const) {
            if (typeof params[key] === 'number') {
              expect(parameters[key]).toBe(params[key]);
            } else if (Object.prototype.hasOwnProperty.call(priorSnapshot, key)) {
              expect(parameters[key]).toEqual(priorSnapshot[key]);
            } else {
              expect(Object.prototype.hasOwnProperty.call(parameters, key)).toBe(false);
            }
          }

          // Every other prior parameter is untouched, and no keys appear
          // beyond the prior keys plus camera_id/gain/exposure.
          for (const [key, value] of Object.entries(priorSnapshot)) {
            if (key === 'camera_id' || key === 'gain' || key === 'exposure') continue;
            expect(parameters[key]).toEqual(value);
          }
          const allowed = new Set([
            ...Object.keys(priorSnapshot),
            'camera_id',
            'gain',
            'exposure',
          ]);
          for (const key of Object.keys(parameters)) {
            expect(allowed.has(key)).toBe(true);
          }

          // The hint carries exactly the source id, its display name
          // (name, falling back to the id), and the reference device id.
          expect(hint).toEqual({
            cameraSourceId: camera.camera_source_id,
            cameraName:
              typeof camera.name === 'string' && camera.name !== ''
                ? camera.name
                : camera.camera_source_id,
            sourceDeviceId,
          });
        }
      ),
      { numRuns: 100 }
    );
  });
});
