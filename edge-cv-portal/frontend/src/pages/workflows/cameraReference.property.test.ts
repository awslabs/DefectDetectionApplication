/**
 * **Feature: camera-registry-sync, Property 11: Camera selection populates the node and records the hint**
 *
 * For all Camera_Sources, applying one as the selection for a
 * Camera_Input_Node yields node parameter values matching the source's
 * parameters for every parameter the source provides, and a binding
 * hint recording exactly that source's identifier.
 *
 * **Validates: Requirements 7.2**
 *
 * The function under test is the pure `applyCameraSelection` from
 * `cameraReference.ts` — the single place a Camera_Source is applied to
 * a Camera_Input_Node's parameters. The property generates arbitrary
 * Camera_Sources (with/without devicePath, url, gain, exposure; unicode
 * and missing names; null/absent params records) and arbitrary prior
 * parameter records, and asserts:
 *
 * - `device` is populated from the source's `devicePath` (non-empty
 *   string), falling back to `url`, retaining the prior value when the
 *   source carries neither;
 * - `gain` / `exposure` are copied exactly when numerically present in
 *   the source's params, and untouched otherwise;
 * - every other prior parameter is untouched and no other key appears;
 * - neither input is mutated (pure);
 * - the hint records exactly `{ cameraSourceId, cameraName (name or id
 *   fallback), sourceDeviceId }`.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { applyCameraSelection, type CameraSourceEntry } from './cameraReference';
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
 * The source's params record: devicePath/url/gain/exposure each either
 * absent or carrying a value of an arbitrary JSON type (so non-string
 * paths and non-numeric gain/exposure are exercised), plus extra keys.
 * The whole record may also be null or absent.
 */
const paramsArb: fc.Arbitrary<Record<string, JsonValue> | null | undefined> = fc.oneof(
  fc.constant(null),
  fc.constant(undefined),
  fc
    .record(
      {
        devicePath: fc.oneof(
          fc.string({ unit: 'grapheme', maxLength: 30 }),
          jsonValueArb
        ),
        url: fc.oneof(fc.string({ unit: 'grapheme', maxLength: 30 }), jsonValueArb),
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

const cameraArb: fc.Arbitrary<CameraSourceEntry> = fc.record(
  {
    camera_source_id: idArb,
    name: fc.oneof(
      fc.string({ unit: 'grapheme', maxLength: 20 }),
      fc.constant(null)
    ),
    type: fc.oneof(fc.constantFrom('Camera', 'RTSP', 'CSI'), fc.constant(null)),
    params: paramsArb,
    origin: fc.constantFrom('edge-configured', 'edge-discovered', 'portal-created'),
    sync_status: fc.constantFrom('synced', 'pending', 'failed'),
    stale: fc.boolean(),
    absent: fc.boolean(),
  },
  { requiredKeys: ['camera_source_id'] }
);

/** Prior node parameters, sometimes already carrying device/gain/exposure. */
const priorParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc
  .dictionary(fc.string({ minLength: 1, maxLength: 8 }), jsonValueArb, { maxKeys: 4 })
  .chain((base) =>
    fc
      .record(
        {
          device: jsonValueArb,
          gain: jsonValueArb,
          exposure: jsonValueArb,
        },
        { requiredKeys: [] }
      )
      .map((known) => ({ ...base, ...known }))
  );

// --------------------------------------------------------------------------
// Oracle helpers (mirror the specified selection semantics independently)
// --------------------------------------------------------------------------

function expectedDevice(
  camera: CameraSourceEntry,
  prior: Record<string, JsonValue>
): { present: boolean; value?: JsonValue } {
  const params = camera.params ?? {};
  if (typeof params.devicePath === 'string' && params.devicePath !== '') {
    return { present: true, value: params.devicePath };
  }
  if (typeof params.url === 'string' && params.url !== '') {
    return { present: true, value: params.url };
  }
  // Neither: the prior device value (or absence) is retained.
  return Object.prototype.hasOwnProperty.call(prior, 'device')
    ? { present: true, value: prior.device }
    : { present: false };
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 11: Camera selection populates the node and records the hint', () => {
  it('populates device/gain/exposure from the source, leaves the rest untouched, and records the hint', () => {
    fc.assert(
      fc.property(
        priorParametersArb,
        cameraArb,
        idArb,
        (prior, camera, sourceDeviceId) => {
          const priorSnapshot = structuredClone(prior);
          const cameraSnapshot = structuredClone(camera);

          const { parameters, hint } = applyCameraSelection(prior, camera, sourceDeviceId);

          // Purity: neither input is mutated.
          expect(prior).toEqual(priorSnapshot);
          expect(camera).toEqual(cameraSnapshot);

          const params = camera.params ?? {};

          // Device population: devicePath, else url, else prior retained.
          const device = expectedDevice(camera, priorSnapshot);
          if (device.present) {
            expect(parameters.device).toEqual(device.value);
          } else {
            expect(Object.prototype.hasOwnProperty.call(parameters, 'device')).toBe(false);
          }

          // gain/exposure copied exactly when numerically present.
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
          // beyond the prior keys plus device/gain/exposure.
          for (const [key, value] of Object.entries(priorSnapshot)) {
            if (key === 'device' || key === 'gain' || key === 'exposure') continue;
            expect(parameters[key]).toEqual(value);
          }
          const allowed = new Set([
            ...Object.keys(priorSnapshot),
            'device',
            'gain',
            'exposure',
          ]);
          for (const key of Object.keys(parameters)) {
            expect(allowed.has(key)).toBe(true);
          }

          // The hint records exactly the selected source's identifier,
          // its display name (name, falling back to the id), and the
          // reference device.
          expect(hint).toEqual({
            cameraSourceId: camera.camera_source_id,
            cameraName:
              typeof camera.name === 'string' && camera.name !== ''
                ? camera.name
                : camera.camera_source_id,
            sourceDeviceId,
          });
        }
      )
    );
  });
});
