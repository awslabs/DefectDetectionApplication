/**
 * Bug condition exploration property test for the Static_Image_Camera
 * binding dead end (static-image-camera-binding-and-pin-discoverability
 * task 1, Defect 1).
 *
 * **Property 1: Bug Condition / Fix Checking (portal frontend)**
 *
 * For any Camera_Source entry the Aravis picker already offers whose
 * camera id lives in the capabilities block instead of `params` — the
 * shipped `StaticImage` inventory shape produced by
 * `_static_image_entry()` in `src/backend/camera_sync/inventory.py`
 * (`params: {}` plus `capabilities.staticImage.id`) — the system SHALL
 * resolve that capabilities id as the Aravis camera id and SHALL write
 * it into the node's `camera_id` parameter, so the offered selection
 * binds and the node validates.
 *
 * **Validates: Requirements 2.1, 2.2, 2.3, 2.4**
 *
 * **CRITICAL — exploration test**: this file MUST FAIL on the unfixed
 * code. `cameraIdValue()` reads only `params.cameraId`, so it returns
 * null for every StaticImage entry and `applyAravisCameraSelection()`
 * never writes `camera_id`. The failures ARE the reproduction of the
 * bug; the same assertions validate the fix once task 3.1 lands.
 *
 * Conventions follow `aravisCameraReference.property.test.ts`:
 * generators plus an independent oracle restating the acceptance
 * criteria, and `structuredClone` purity checks over both inputs. The
 * functions under test are the pure `cameraIdValue` and
 * `applyAravisCameraSelection` from `cameraReference.ts`.
 */

import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import {
  applyAravisCameraSelection,
  cameraDisplayName,
  cameraIdValue,
  isAravisCompatibleCamera,
  type CameraSourceEntry,
} from './cameraReference';
import { checkParameterValue, VIOLATION_REQUIRED } from './parameters';
import type { JsonValue, ParameterDescriptor } from './types';

// --------------------------------------------------------------------------
// The live counterexample (Defect 1's reproduction input)
// --------------------------------------------------------------------------

/**
 * The Static_Image_Camera entry exactly as read from the live
 * `dda-camera-registry` named shadow for `jetson-thor1`
 * (`reported.cameras["static-image-camera"]`) and served by
 * `GET /devices/jetson-thor1/cameras`: type `StaticImage`, origin
 * `edge-discovered`, an EMPTY params block, and the identity under
 * `capabilities.staticImage`. This is the entry the Aravis picker offers
 * and the user selected.
 */
const LIVE_STATIC_IMAGE_ENTRY: CameraSourceEntry = {
  camera_source_id: 'static-image-camera',
  name: 'Static Image Camera',
  type: 'StaticImage',
  origin: 'edge-discovered',
  params: {},
  capabilities: {
    staticImage: {
      id: 'static-image-camera',
      model: 'Static Image Camera',
      address: 'internal',
      physicalId: 'static-image-camera',
      protocol: 'StaticImage',
      serial: 'STATIC-IMAGE-0',
      vendor: 'AWS-DDA',
    },
  },
  discovered: true,
  absent: true,
  absentSince: 1788839397466,
} as CameraSourceEntry;

/** The device-side fixed enumeration id the picker must bind to. */
const STATIC_IMAGE_CAMERA_ID = 'static-image-camera';

/**
 * The `aravis_camera_source` node's `camera_id` parameter as served by
 * the catalog: required, string, non-empty. Used to assert the applied
 * parameter record carries no `Required parameter 'camera_id' has no
 * value` violation (Requirement 2.3).
 */
const CAMERA_ID_DESCRIPTOR: ParameterDescriptor = {
  name: 'camera_id',
  paramType: 'string',
  required: true,
  default: null,
  constraints: { minLength: 1 },
};

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** Arbitrary JSON values for extra params/capability keys. */
const jsonValueArb: fc.Arbitrary<JsonValue> = fc.oneof(
  { maxDepth: 1 },
  fc.string({ unit: 'grapheme', maxLength: 12 }),
  fc.integer(),
  fc.double({ noNaN: true }),
  fc.boolean(),
  fc.constant(null),
  fc.array(fc.oneof(fc.string({ maxLength: 6 }), fc.integer()), { maxLength: 3 })
);

/** Identifiers: non-empty, unicode allowed. */
const idArb = fc.string({ unit: 'grapheme', minLength: 1, maxLength: 20 });

/**
 * A params block carrying NO usable `cameraId`: the shipped empty
 * record, an absent/null block, or a record whose `cameraId` is unusable
 * (empty string or a non-string JSON value), plus arbitrary extra keys.
 * This is the `paramsCameraId(camera) = NULL` half of the bug condition.
 */
const paramsWithoutUsableCameraIdArb: fc.Arbitrary<
  Record<string, JsonValue> | null | undefined
> = fc.oneof(
  fc.constant({} as Record<string, JsonValue>),
  fc.constant(null),
  fc.constant(undefined),
  fc
    .dictionary(
      fc.string({ minLength: 1, maxLength: 8 }).filter((key) => key !== 'cameraId'),
      jsonValueArb,
      { maxKeys: 3 }
    )
    .chain((extra) =>
      fc.oneof(
        fc.constant(extra),
        // An unusable cameraId: present but not a non-empty string.
        fc
          .oneof(
            fc.constant('' as JsonValue),
            fc.integer(),
            fc.boolean(),
            fc.constant(null),
            fc.array(fc.string({ maxLength: 4 }), { maxLength: 2 })
          )
          .map((cameraId) => ({ ...extra, cameraId }))
      )
    )
);

/**
 * The StaticImage capabilities identity: a non-empty `id` (the value the
 * fix must resolve, read from the block rather than hardcoded so a
 * future identity change flows through — Requirement 2.1) plus the
 * shipped descriptive fields and arbitrary extra keys.
 */
const staticImageCapabilityArb = fc
  .record(
    {
      id: idArb,
      model: fc.string({ maxLength: 20 }),
      address: fc.constantFrom('internal', 'localhost'),
      physicalId: fc.string({ maxLength: 20 }),
      protocol: fc.constant('StaticImage'),
      serial: fc.string({ maxLength: 20 }),
      vendor: fc.string({ maxLength: 20 }),
    },
    { requiredKeys: ['id'] }
  )
  .chain((identity) =>
    fc
      .dictionary(
        fc.string({ minLength: 1, maxLength: 6 }).filter((key) => key !== 'id'),
        jsonValueArb,
        { maxKeys: 2 }
      )
      .map((extra) => ({ ...extra, ...identity }))
  );

/**
 * A `StaticImage` Camera_Source entry in the bug condition: offered by
 * the Aravis picker, a non-empty `capabilities.staticImage.id`, and no
 * usable `params.cameraId`. Arbitrary sibling capability keys are mixed
 * in — registry payloads are external input.
 */
const staticImageEntryArb: fc.Arbitrary<CameraSourceEntry> = fc
  .record({
    camera_source_id: idArb,
    name: fc.oneof(
      fc.string({ unit: 'grapheme', maxLength: 24 }),
      fc.constant(null),
      fc.constant(undefined)
    ),
    params: paramsWithoutUsableCameraIdArb,
    staticImage: staticImageCapabilityArb,
    otherCapabilities: fc.dictionary(
      fc.string({ minLength: 1, maxLength: 6 }).filter((key) => key !== 'staticImage'),
      jsonValueArb,
      { maxKeys: 2 }
    ),
    origin: fc.constantFrom('edge-discovered', 'edge-configured'),
    sync_status: fc.constantFrom('synced', 'pending'),
    absent: fc.boolean(),
    absent_since: fc.oneof(fc.constant(null), fc.integer({ min: 1 })),
    stale: fc.boolean(),
  })
  .map(({ staticImage, otherCapabilities, ...rest }) => ({
    ...rest,
    type: 'StaticImage',
    capabilities: { ...otherCapabilities, staticImage } as Record<string, JsonValue>,
  }));

/** Prior node parameters, sometimes already carrying camera_id/gain/exposure. */
const priorParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc
  .dictionary(fc.string({ minLength: 1, maxLength: 8 }), jsonValueArb, { maxKeys: 3 })
  .chain((base) =>
    fc
      .record(
        { camera_id: jsonValueArb, gain: jsonValueArb, exposure: jsonValueArb },
        { requiredKeys: [] }
      )
      .map((known) => ({ ...base, ...known }))
  );

// --------------------------------------------------------------------------
// Oracles (restate the expected behavior independently)
// --------------------------------------------------------------------------

/**
 * `staticImageCapabilityId(camera)` from the bug condition: the
 * `capabilities.staticImage.id` as a non-empty string, else null.
 * Guarded like `getCameraBindingHint` — a null, array, or non-object
 * `staticImage`, or a non-string / empty `id`, yields null.
 */
function staticImageCapabilityIdOracle(camera: CameraSourceEntry): string | null {
  const block = (camera.capabilities ?? {}).staticImage;
  if (block === null || block === undefined || typeof block !== 'object' || Array.isArray(block)) {
    return null;
  }
  const id = (block as Record<string, JsonValue>).id;
  return typeof id === 'string' && id !== '' ? id : null;
}

/** `paramsCameraId(camera)`: `params.cameraId` as a non-empty string, else null. */
function paramsCameraIdOracle(camera: CameraSourceEntry): string | null {
  const cameraId = (camera.params ?? {}).cameraId;
  return typeof cameraId === 'string' && cameraId !== '' ? cameraId : null;
}

/** Bug condition part 1, restated over an Aravis picker selection. */
function isBugConditionPart1(camera: CameraSourceEntry): boolean {
  return (
    isAravisCompatibleCamera(camera) &&
    staticImageCapabilityIdOracle(camera) !== null &&
    paramsCameraIdOracle(camera) === null
  );
}

// --------------------------------------------------------------------------
// Property 1 (part 1): the capabilities id resolves and binds
// --------------------------------------------------------------------------

describe('Property 1 (Bug Condition, Defect 1): StaticImage camera id resolves from capabilities', () => {
  it('resolves capabilities.staticImage.id as the Aravis camera id for every offered StaticImage entry', () => {
    fc.assert(
      fc.property(staticImageEntryArb, (camera) => {
        // Precondition: the generated entry is in the bug condition.
        fc.pre(isBugConditionPart1(camera));

        const snapshot = structuredClone(camera);
        const expected = staticImageCapabilityIdOracle(camera);

        // Requirement 2.1: the capabilities id resolves, read from the
        // block rather than a hardcoded constant.
        expect(cameraIdValue(camera)).toBe(expected);

        // Purity: resolution never mutates the registry entry.
        expect(camera).toEqual(snapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('writes the resolved id into camera_id and keeps the binding hint intact', () => {
    fc.assert(
      fc.property(
        priorParametersArb,
        staticImageEntryArb,
        idArb,
        (prior, camera, sourceDeviceId) => {
          fc.pre(isBugConditionPart1(camera));

          const priorSnapshot = structuredClone(prior);
          const cameraSnapshot = structuredClone(camera);
          const expected = staticImageCapabilityIdOracle(camera);

          const { parameters, hint } = applyAravisCameraSelection(
            prior,
            camera,
            sourceDeviceId
          );

          // Requirement 2.2: camera_id carries the resolved id.
          expect(parameters.camera_id).toBe(expected);
          // ...and it agrees with the id resolution itself.
          expect(parameters.camera_id).toBe(cameraIdValue(camera));

          // Requirement 2.3: no required-parameter violation remains.
          const violation = checkParameterValue(
            CAMERA_ID_DESCRIPTOR,
            parameters.camera_id ?? null
          );
          expect(violation).toBeNull();

          // Requirement 3.3 (unchanged): the hint still carries the
          // source id, display name, and reference device id.
          expect(hint).toEqual({
            cameraSourceId: camera.camera_source_id,
            cameraName: cameraDisplayName(camera),
            sourceDeviceId,
          });

          // Purity: neither input is mutated.
          expect(prior).toEqual(priorSnapshot);
          expect(camera).toEqual(cameraSnapshot);
        }
      ),
      { numRuns: 25 }
    );
  });
});

// --------------------------------------------------------------------------
// The concrete live counterexample (jetson-thor1)
// --------------------------------------------------------------------------

describe('Property 1 (Bug Condition, Defect 1): the live jetson-thor1 registry entry', () => {
  it('is offered by the Aravis picker and is in the bug condition', () => {
    // The picker offers it today (isAravisCompatibleCamera accepts
    // StaticImage unconditionally — correct, and unchanged by the fix),
    // while its id lives in the capabilities block only.
    expect(isAravisCompatibleCamera(LIVE_STATIC_IMAGE_ENTRY)).toBe(true);
    expect(staticImageCapabilityIdOracle(LIVE_STATIC_IMAGE_ENTRY)).toBe(
      STATIC_IMAGE_CAMERA_ID
    );
    expect(paramsCameraIdOracle(LIVE_STATIC_IMAGE_ENTRY)).toBeNull();
  });

  it("resolves 'static-image-camera' as its Aravis camera id (Requirements 2.1, 2.4)", () => {
    // Requirement 2.4: the picker option description is fed by this same
    // resolution, so a resolved id also fixes the missing description.
    expect(cameraIdValue(LIVE_STATIC_IMAGE_ENTRY)).toBe(STATIC_IMAGE_CAMERA_ID);
  });

  it('binds camera_id and clears the required-parameter violation (Requirements 2.2, 2.3)', () => {
    const { parameters, hint } = applyAravisCameraSelection(
      {},
      LIVE_STATIC_IMAGE_ENTRY,
      'jetson-thor1'
    );

    expect(Object.prototype.hasOwnProperty.call(parameters, 'camera_id')).toBe(true);
    expect(parameters.camera_id).toBe(STATIC_IMAGE_CAMERA_ID);

    // The violation reported live — `Required parameter 'camera_id' has
    // no value` — is gone once the parameter is populated.
    const violation = checkParameterValue(
      CAMERA_ID_DESCRIPTOR,
      parameters.camera_id ?? null
    );
    expect(violation?.code).not.toBe(VIOLATION_REQUIRED);
    expect(violation).toBeNull();

    // The advisory hint is unchanged by the fix.
    expect(hint).toEqual({
      cameraSourceId: 'static-image-camera',
      cameraName: 'Static Image Camera',
      sourceDeviceId: 'jetson-thor1',
    });
  });

  it('retains a pre-existing unrelated parameter record while binding camera_id', () => {
    const prior: Record<string, JsonValue> = { gain: 3, exposure: 1000, other: 'keep' };
    const priorSnapshot = structuredClone(prior);

    const { parameters } = applyAravisCameraSelection(
      prior,
      LIVE_STATIC_IMAGE_ENTRY,
      'jetson-thor1'
    );

    expect(parameters.camera_id).toBe(STATIC_IMAGE_CAMERA_ID);
    // The static entry's params are empty, so gain/exposure keep their
    // prior values and no other key is touched (Requirement 3.3).
    expect(parameters.gain).toBe(3);
    expect(parameters.exposure).toBe(1000);
    expect(parameters.other).toBe('keep');
    expect(prior).toEqual(priorSnapshot);
  });
});
