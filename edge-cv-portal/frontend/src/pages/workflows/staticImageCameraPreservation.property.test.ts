/**
 * Preservation property tests for the Aravis id resolution, selection
 * semantics, and picker compatibility filters
 * (static-image-camera-binding-and-pin-discoverability task 2, Defect 1).
 *
 * **Property 2: Preservation Checking (portal frontend)**
 *
 * ```pascal
 * FOR ALL X WHERE NOT isBugCondition(X) DO
 *   ASSERT F(X) = F'(X)
 * END FOR
 * FOR ALL cameras DO
 *   ASSERT isAravisCompatibleCamera'(cameras) = isAravisCompatibleCamera(cameras)
 * END FOR
 * ```
 *
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
 *
 * Every source whose id already resolves from `params`, every source
 * that resolves no id at all, and every picker compatibility decision
 * behaves identically after the capabilities fallback lands. These tests
 * PASS on the UNFIXED code — they pin the baseline the fix must not
 * regress, so a fix that widened the offered set or let the capabilities
 * lookup shadow `params.cameraId` fails here.
 *
 * ## Observation-first baseline (recorded against the UNFIXED code)
 *
 * `cameraIdValue()`:
 * - `AravisDiscovered` fixture (`params.cameraId: 'Basler-40123456'`) → `'Basler-40123456'`
 * - configured `Camera` fixture (`params.cameraId: 'cam-1'`) → `'cam-1'`
 * - no `params` → `null`; `params: null` → `null`
 * - `params.cameraId: ''` → `null`; `params.cameraId: 42` → `null`
 * - `params.cameraId` present alongside `capabilities.staticImage.id` → the `params` value
 * - `Camera` entry carrying `capabilities.staticImage.id` and no `params.cameraId` → `null`
 *
 * `isAravisCompatibleCamera` / `isV4l2CompatibleCamera` (offered sets):
 * - `AravisDiscovered` → aravis `true`, v4l2 `false`
 * - `StaticImage` → aravis `true`, v4l2 `false`
 * - `Camera` with `cameraId` + `devicePath` → aravis `true`, v4l2 `true`
 * - `Camera` carrying a StaticImage capabilities id but no `params.cameraId` → aravis `false`, v4l2 `true`
 * - bare `Camera` → `false` / `false`; `ICam`, `V4L2Discovered` → `false` / `true`
 * - `RTSP`, `type: null` → `false` / `false`
 *
 * `applyAravisCameraSelection()`:
 * - a source resolving no id retains a pre-existing `camera_id` (`'prior-cam'`) and leaves the
 *   key absent when there was none
 * - `gain` copied when the source's params carry it as a number (`9`), `exposure` left at its
 *   prior value (`2`) when the source's is non-numeric (`'high'`)
 * - the hint carries the source id, its display name (name, falling back to the id), and the
 *   reference device id; neither input is mutated
 *
 * Conventions follow `aravisCameraReference.property.test.ts`: generators
 * plus independent oracles restating the pre-fix semantics, and
 * `structuredClone` purity checks over both inputs. Requirements 3.8,
 * 3.9, 3.10 (the Cameras tab's create-form type options, role gate, and
 * normal/loading/error renders) are covered component-level in
 * `src/components/DeviceCamerasTab.staticImageFocus.test.tsx`;
 * Requirement 3.11 (the pin shortcut's existing parameters and gating)
 * in `NodeConfigPanel.pinShortcut.test.tsx` and `NodeConfigPanel.test.tsx`.
 */

import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import {
  applyAravisCameraSelection,
  cameraDeviceValue,
  cameraDisplayName,
  cameraIdValue,
  isAravisCompatibleCamera,
  isV4l2CompatibleCamera,
  type CameraSourceEntry,
} from './cameraReference';
import type { JsonValue } from './types';

// --------------------------------------------------------------------------
// Recorded fixtures (the exact shapes observed on the unfixed code)
// --------------------------------------------------------------------------

/** The bus-camera fixture the existing suites pin (`cameraReference.test.ts`). */
const ARAVIS_DISCOVERED: CameraSourceEntry = {
  camera_source_id: 'arv-1a2b3c4d5e6f',
  name: 'Basler acA1920',
  type: 'AravisDiscovered',
  params: { cameraId: 'Basler-40123456', serial: '40123456', protocol: 'GigEVision' },
  origin: 'edge-discovered',
  sync_status: 'synced',
};

/** The configured `Camera` fixture: carries both a device path and a camera id. */
const CONFIGURED_CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-a1b2',
  name: 'Line 1 inspection cam',
  type: 'Camera',
  params: { devicePath: '/dev/video2', cameraId: 'cam-1', gain: 8, exposure: 16000000 },
  origin: 'edge-configured',
  sync_status: 'synced',
  stale: false,
  absent: false,
};

/**
 * The shipped StaticImage capabilities identity, exactly as the device
 * reports it (`_static_image_identity()` in
 * `src/backend/camera_sync/inventory.py`). Used to build the guard cases:
 * the same block attached to a NON-`StaticImage` entry must never make
 * that entry resolve an id or become Aravis-compatible.
 */
const STATIC_IMAGE_CAPABILITIES: Record<string, JsonValue> = {
  staticImage: {
    id: 'static-image-camera',
    model: 'Static Image Camera',
    address: 'internal',
    physicalId: 'static-image-camera',
    protocol: 'StaticImage',
    serial: 'STATIC-IMAGE-0',
    vendor: 'AWS-DDA',
  },
};

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** Arbitrary JSON parameter values, including nested arrays/objects. */
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

/** Every camera type the picker filters over, plus unknown/absent types. */
const typeArb: fc.Arbitrary<string | null | undefined> = fc.oneof(
  fc.constantFrom(
    'AravisDiscovered',
    'Camera',
    'StaticImage',
    'V4L2Discovered',
    'RTSP',
    'CSI',
    'NvidiaCSI',
    'Folder',
    'ICam'
  ),
  fc.string({ maxLength: 12 }),
  fc.constant(null),
  fc.constant(undefined)
);

/**
 * A value that is NOT a usable id: absent, empty string, or a non-string
 * JSON value. Drives the "resolves null" half of the baseline.
 */
const unusableIdArb: fc.Arbitrary<JsonValue> = fc.oneof(
  fc.constant('' as JsonValue),
  fc.integer(),
  fc.double({ noNaN: true }),
  fc.boolean(),
  fc.constant(null),
  fc.array(fc.string({ maxLength: 4 }), { maxLength: 2 }),
  fc.dictionary(fc.string({ maxLength: 4 }), fc.integer(), { maxKeys: 2 })
);

/**
 * A capabilities `staticImage` block that carries NO usable id — null,
 * a non-object, an array, an empty record, or a record whose `id` is not
 * a non-empty string. Registry payloads are external input, so each of
 * these must resolve null rather than throw.
 */
const staticImageWithoutIdArb: fc.Arbitrary<JsonValue> = fc.oneof(
  fc.constant(null),
  fc.constant('a string, not an object' as JsonValue),
  fc.constant(42 as JsonValue),
  fc.array(fc.string({ maxLength: 4 }), { maxLength: 2 }),
  fc.constant({} as JsonValue),
  unusableIdArb.map((id) => ({ id, vendor: 'AWS-DDA' }) as JsonValue)
);

/** The shipped identity block with an arbitrary non-empty id. */
const staticImageWithIdArb: fc.Arbitrary<JsonValue> = idArb.map(
  (id) =>
    ({
      id,
      model: 'Static Image Camera',
      address: 'internal',
      physicalId: id,
      protocol: 'StaticImage',
      serial: 'STATIC-IMAGE-0',
      vendor: 'AWS-DDA',
    }) as JsonValue
);

/** Extra sibling capability keys — registry payloads are external input. */
const otherCapabilitiesArb = fc.dictionary(
  fc.string({ minLength: 1, maxLength: 6 }).filter((key) => key !== 'staticImage'),
  jsonValueArb,
  { maxKeys: 2 }
);

/** A capabilities block: absent, null, or a record with/without a staticImage id. */
const capabilitiesArb: fc.Arbitrary<Record<string, JsonValue> | null | undefined> = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  otherCapabilitiesArb,
  fc
    .tuple(otherCapabilitiesArb, fc.oneof(staticImageWithIdArb, staticImageWithoutIdArb))
    .map(([other, staticImage]) => ({ ...other, staticImage }))
);

/** Extra params keys, never colliding with the known ones. */
const otherParamsArb = fc.dictionary(
  fc
    .string({ minLength: 1, maxLength: 8 })
    .filter((key) => !['cameraId', 'devicePath', 'url', 'gain', 'exposure'].includes(key)),
  jsonValueArb,
  { maxKeys: 3 }
);

/** gain/exposure/devicePath/url drawn over usable and unusable shapes. */
const knownParamsArb = fc.record(
  {
    devicePath: fc.oneof(fc.string({ maxLength: 16 }), jsonValueArb),
    url: fc.oneof(fc.string({ maxLength: 16 }), jsonValueArb),
    gain: fc.oneof(fc.double({ noNaN: true }), fc.integer(), jsonValueArb),
    exposure: fc.oneof(fc.double({ noNaN: true }), fc.integer(), jsonValueArb),
  },
  { requiredKeys: [] }
);

/** A params record whose `cameraId` is a non-empty string. */
const paramsWithCameraIdArb = (cameraId: string) =>
  fc
    .tuple(otherParamsArb, knownParamsArb)
    .map(([other, known]) => ({ ...other, ...known, cameraId }));

/** A params record carrying NO usable `cameraId` (absent or unusable). */
const paramsWithoutCameraIdArb: fc.Arbitrary<
  Record<string, JsonValue> | null | undefined
> = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.constant({} as Record<string, JsonValue>),
  fc.tuple(otherParamsArb, knownParamsArb).map(([other, known]) => ({ ...other, ...known })),
  fc
    .tuple(otherParamsArb, knownParamsArb, unusableIdArb)
    .map(([other, known, cameraId]) => ({ ...other, ...known, cameraId }))
);

/** The shared entry envelope: id, name, origin, and the sync/staleness flags. */
const entryEnvelopeArb = fc.record(
  {
    camera_source_id: idArb,
    name: fc.oneof(
      fc.string({ unit: 'grapheme', maxLength: 24 }),
      fc.constant(null),
      fc.constant(undefined)
    ),
    origin: fc.constantFrom('edge-discovered', 'edge-configured', 'portal-created'),
    sync_status: fc.constantFrom('synced', 'pending', 'failed'),
    stale: fc.boolean(),
    absent: fc.boolean(),
  },
  { requiredKeys: ['camera_source_id'] }
);

/**
 * An arbitrary Camera_Registry entry of any type, with a capabilities
 * block that sometimes carries a StaticImage identity — so the offered
 * set is exercised against exactly the payloads the fix touches.
 */
const anyCameraArb: fc.Arbitrary<CameraSourceEntry> = fc
  .tuple(
    entryEnvelopeArb,
    typeArb,
    fc.oneof(
      paramsWithoutCameraIdArb,
      idArb.chain((cameraId) => paramsWithCameraIdArb(cameraId))
    ),
    capabilitiesArb
  )
  .map(([envelope, type, params, capabilities]) => ({
    ...envelope,
    type,
    params,
    capabilities,
  }));

/**
 * An entry whose `params.cameraId` IS a usable id while a DIFFERENT
 * non-empty `capabilities.staticImage.id` is also present — the exact
 * shape that would break if the capabilities lookup ran first
 * (Requirement 3.1).
 */
const paramsFirstEntryArb: fc.Arbitrary<{ camera: CameraSourceEntry; paramsId: string }> = fc
  .tuple(entryEnvelopeArb, typeArb, idArb, idArb, otherCapabilitiesArb)
  .map(([envelope, type, paramsId, rawCapsId, otherCaps]) => {
    // Force the two ids apart so "params wins" is observable every run.
    const capsId = rawCapsId === paramsId ? `caps-${rawCapsId}` : rawCapsId;
    return {
      paramsId,
      camera: {
        ...envelope,
        type,
        params: { cameraId: paramsId, extra: 'kept' } as Record<string, JsonValue>,
        capabilities: {
          ...otherCaps,
          staticImage: { id: capsId, vendor: 'AWS-DDA', protocol: 'StaticImage' },
        } as Record<string, JsonValue>,
      },
    };
  });

/**
 * An entry resolving NO id under either rule: no usable `params.cameraId`
 * and no non-empty `capabilities.staticImage.id` (Requirement 3.2).
 */
const noIdEntryArb: fc.Arbitrary<CameraSourceEntry> = fc
  .tuple(
    entryEnvelopeArb,
    typeArb,
    paramsWithoutCameraIdArb,
    fc.oneof(
      fc.constant(undefined),
      fc.constant(null),
      otherCapabilitiesArb,
      fc
        .tuple(otherCapabilitiesArb, staticImageWithoutIdArb)
        .map(([other, staticImage]) => ({ ...other, staticImage }))
    )
  )
  .map(([envelope, type, params, capabilities]) => ({
    ...envelope,
    type,
    params,
    capabilities,
  }));

/**
 * The CRITICAL guard case: a NON-`StaticImage` entry (type `Camera`)
 * carrying a non-empty `capabilities.staticImage.id` and no usable
 * `params.cameraId`. `isAravisCompatibleCamera` resolves the id in its
 * `Camera` arm, so a type-agnostic fallback would widen the offered set.
 */
const cameraTypeGuardEntryArb: fc.Arbitrary<CameraSourceEntry> = fc
  .tuple(entryEnvelopeArb, paramsWithoutCameraIdArb, otherCapabilitiesArb, staticImageWithIdArb)
  .map(([envelope, params, otherCaps, staticImage]) => ({
    ...envelope,
    type: 'Camera',
    params,
    capabilities: { ...otherCaps, staticImage } as Record<string, JsonValue>,
  }));

/** Prior node parameters, sometimes already carrying camera_id/gain/exposure. */
const priorParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc
  .dictionary(fc.string({ minLength: 1, maxLength: 8 }), jsonValueArb, { maxKeys: 4 })
  .chain((base) =>
    fc
      .record(
        { camera_id: jsonValueArb, gain: jsonValueArb, exposure: jsonValueArb },
        { requiredKeys: [] }
      )
      .map((known) => ({ ...base, ...known }))
  );

// --------------------------------------------------------------------------
// Pre-fix oracles (restate the recorded baseline independently)
// --------------------------------------------------------------------------

/** `paramsCameraId(camera)`: `params.cameraId` as a non-empty string, else null. */
function paramsCameraIdOracle(camera: CameraSourceEntry): string | null {
  const cameraId = (camera.params ?? {}).cameraId;
  return typeof cameraId === 'string' && cameraId !== '' ? cameraId : null;
}

/** `staticImageCapabilityId(camera)`: `capabilities.staticImage.id`, else null. */
function staticImageCapabilityIdOracle(camera: CameraSourceEntry): string | null {
  const block = (camera.capabilities ?? {}).staticImage;
  if (
    block === null ||
    block === undefined ||
    typeof block !== 'object' ||
    Array.isArray(block)
  ) {
    return null;
  }
  const id = (block as Record<string, JsonValue>).id;
  return typeof id === 'string' && id !== '' ? id : null;
}

/**
 * The PRE-FIX Aravis compatibility decision, recorded from the shipped
 * `isAravisCompatibleCamera`: `AravisDiscovered` and `StaticImage`
 * unconditionally, plus `Camera` carrying a non-empty string
 * `params.cameraId`. The fix must reproduce this set exactly — no entry
 * gained, none lost (Requirement 3.4).
 */
function aravisCompatibleOracle(camera: CameraSourceEntry): boolean {
  if (camera.type === 'AravisDiscovered' || camera.type === 'StaticImage') {
    return true;
  }
  if (camera.type !== 'Camera') {
    return false;
  }
  return paramsCameraIdOracle(camera) !== null;
}

/** `cameraDeviceValue` restated: `devicePath`, else `url`, else null. */
function deviceValueOracle(camera: CameraSourceEntry): string | null {
  const params = camera.params ?? {};
  const devicePath = params.devicePath;
  if (typeof devicePath === 'string' && devicePath !== '') {
    return devicePath;
  }
  const url = params.url;
  return typeof url === 'string' && url !== '' ? url : null;
}

/**
 * The V4L2/ICAM compatibility decision, recorded from the shipped
 * `isV4l2CompatibleCamera`: `ICam` and `V4L2Discovered` unconditionally,
 * plus `Camera` carrying a non-empty `devicePath` or `url`
 * (Requirement 3.4 — unaffected by this fix).
 */
function v4l2CompatibleOracle(camera: CameraSourceEntry): boolean {
  if (camera.type === 'ICam' || camera.type === 'V4L2Discovered') {
    return true;
  }
  if (camera.type !== 'Camera') {
    return false;
  }
  return deviceValueOracle(camera) !== null;
}

/**
 * The `camera_id` the pre-fix selection produces for an entry that is
 * NOT in the bug condition: the source's `params.cameraId`, else the
 * prior value, else the key stays absent (Requirement 3.2).
 */
function expectedCameraIdOracle(
  camera: CameraSourceEntry,
  prior: Record<string, JsonValue>
): { present: boolean; value?: JsonValue } {
  const fromParams = paramsCameraIdOracle(camera);
  if (fromParams !== null) {
    return { present: true, value: fromParams };
  }
  return Object.prototype.hasOwnProperty.call(prior, 'camera_id')
    ? { present: true, value: prior.camera_id }
    : { present: false };
}

/**
 * The gain/exposure, other-parameter, and hint assertions shared by every
 * selection property (Requirement 3.3) — the half of the selection
 * semantics the fix never touches.
 */
function assertSelectionInvariants(
  camera: CameraSourceEntry,
  priorSnapshot: Record<string, JsonValue>,
  sourceDeviceId: string,
  parameters: Record<string, JsonValue>,
  hint: unknown
): void {
  const params = camera.params ?? {};

  // gain/exposure copied exactly when the source's params carry them as
  // numbers; the prior value (or absence) retained otherwise.
  for (const key of ['gain', 'exposure'] as const) {
    if (typeof params[key] === 'number') {
      expect(parameters[key]).toBe(params[key]);
    } else if (Object.prototype.hasOwnProperty.call(priorSnapshot, key)) {
      expect(parameters[key]).toEqual(priorSnapshot[key]);
    } else {
      expect(Object.prototype.hasOwnProperty.call(parameters, key)).toBe(false);
    }
  }

  // Every other prior parameter is untouched, and no key appears beyond
  // the prior keys plus camera_id/gain/exposure.
  for (const [key, value] of Object.entries(priorSnapshot)) {
    if (key === 'camera_id' || key === 'gain' || key === 'exposure') continue;
    expect(parameters[key]).toEqual(value);
  }
  const allowed = new Set([...Object.keys(priorSnapshot), 'camera_id', 'gain', 'exposure']);
  for (const key of Object.keys(parameters)) {
    expect(allowed.has(key)).toBe(true);
  }

  // The hint carries the source id, its display name, and the device id.
  expect(hint).toEqual({
    cameraSourceId: camera.camera_source_id,
    cameraName: cameraDisplayName(camera),
    sourceDeviceId,
  });
}

// --------------------------------------------------------------------------
// Property 2a: params-first id resolution (Requirement 3.1)
// --------------------------------------------------------------------------

describe('Property 2 (Preservation): params-first id resolution', () => {
  it('resolves exactly params.cameraId even when a capabilities.staticImage.id is also present', () => {
    fc.assert(
      fc.property(paramsFirstEntryArb, ({ camera, paramsId }) => {
        const snapshot = structuredClone(camera);
        const capsId = staticImageCapabilityIdOracle(camera);

        // The generator guarantees a competing, DIFFERENT capabilities id.
        expect(capsId).not.toBeNull();
        expect(capsId).not.toBe(paramsId);

        // Requirement 3.1: the capabilities lookup never overrides params.
        expect(cameraIdValue(camera)).toBe(paramsId);
        expect(cameraIdValue(camera)).toBe(paramsCameraIdOracle(camera));

        // Purity: resolution never mutates the registry entry.
        expect(camera).toEqual(snapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('writes exactly params.cameraId into camera_id, overwriting any prior value', () => {
    fc.assert(
      fc.property(
        priorParametersArb,
        paramsFirstEntryArb,
        idArb,
        (prior, { camera, paramsId }, sourceDeviceId) => {
          const priorSnapshot = structuredClone(prior);
          const cameraSnapshot = structuredClone(camera);

          const { parameters, hint } = applyAravisCameraSelection(
            prior,
            camera,
            sourceDeviceId
          );

          expect(parameters.camera_id).toBe(paramsId);
          assertSelectionInvariants(camera, priorSnapshot, sourceDeviceId, parameters, hint);

          // Purity: neither input is mutated.
          expect(prior).toEqual(priorSnapshot);
          expect(camera).toEqual(cameraSnapshot);
        }
      ),
      { numRuns: 25 }
    );
  });

  it('resolves the recorded fixture ids unchanged', () => {
    expect(cameraIdValue(ARAVIS_DISCOVERED)).toBe('Basler-40123456');
    expect(cameraIdValue(CONFIGURED_CAMERA)).toBe('cam-1');

    // The same fixtures with the StaticImage identity block bolted on
    // still resolve their own params ids.
    expect(
      cameraIdValue({ ...ARAVIS_DISCOVERED, capabilities: STATIC_IMAGE_CAPABILITIES })
    ).toBe('Basler-40123456');
    expect(
      cameraIdValue({ ...CONFIGURED_CAMERA, capabilities: STATIC_IMAGE_CAPABILITIES })
    ).toBe('cam-1');
  });
});

// --------------------------------------------------------------------------
// Property 2b: sources resolving no id retain the prior value (Req 3.2)
// --------------------------------------------------------------------------

describe('Property 2 (Preservation): sources resolving no id', () => {
  it('resolves null for every entry carrying neither a usable params.cameraId nor a StaticImage capabilities id', () => {
    fc.assert(
      fc.property(noIdEntryArb, (camera) => {
        const snapshot = structuredClone(camera);

        // The generator's own precondition, restated by the oracles.
        expect(paramsCameraIdOracle(camera)).toBeNull();
        expect(staticImageCapabilityIdOracle(camera)).toBeNull();

        expect(cameraIdValue(camera)).toBeNull();
        expect(camera).toEqual(snapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('retains a prior camera_id — or leaves the key absent — when the source resolves none', () => {
    fc.assert(
      fc.property(priorParametersArb, noIdEntryArb, idArb, (prior, camera, deviceId) => {
        const priorSnapshot = structuredClone(prior);
        const cameraSnapshot = structuredClone(camera);

        const { parameters, hint } = applyAravisCameraSelection(prior, camera, deviceId);

        const expected = expectedCameraIdOracle(camera, priorSnapshot);
        if (expected.present) {
          expect(parameters.camera_id).toEqual(expected.value);
        } else {
          expect(Object.prototype.hasOwnProperty.call(parameters, 'camera_id')).toBe(false);
        }

        assertSelectionInvariants(camera, priorSnapshot, deviceId, parameters, hint);
        expect(prior).toEqual(priorSnapshot);
        expect(camera).toEqual(cameraSnapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('resolves null for the recorded unusable params shapes', () => {
    expect(cameraIdValue({ camera_source_id: 'x' })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: null })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: {} })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: { cameraId: '' } })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: { cameraId: 42 } })).toBeNull();
  });

  it('retains the recorded prior camera_id and leaves the key absent otherwise', () => {
    const bare: CameraSourceEntry = {
      camera_source_id: 'disc-1',
      name: 'Nameless source',
      type: 'AravisDiscovered',
      params: {},
    };

    expect(
      applyAravisCameraSelection({ camera_id: 'prior-cam', other: 1 }, bare, 'dev-1')
    ).toEqual({
      parameters: { camera_id: 'prior-cam', other: 1 },
      hint: {
        cameraSourceId: 'disc-1',
        cameraName: 'Nameless source',
        sourceDeviceId: 'dev-1',
      },
    });

    expect(applyAravisCameraSelection({ other: 1 }, bare, 'dev-1').parameters).toEqual({
      other: 1,
    });
  });
});

// --------------------------------------------------------------------------
// Property 2c: the picker compatibility sets are unchanged (Req 3.4)
// --------------------------------------------------------------------------

describe('Property 2 (Preservation): picker compatibility sets', () => {
  it('offers exactly the pre-fix Aravis-compatible set for any list of entries', () => {
    fc.assert(
      fc.property(fc.array(anyCameraArb, { maxLength: 12 }), (cameras) => {
        const snapshot = structuredClone(cameras);
        const offered = cameras.filter(isAravisCompatibleCamera);

        // The offered list is exactly the pre-fix oracle's list, in order:
        // nothing gained (no widening), nothing lost (no narrowing).
        expect(offered).toEqual(cameras.filter(aravisCompatibleOracle));
        for (const camera of cameras) {
          expect(isAravisCompatibleCamera(camera)).toBe(aravisCompatibleOracle(camera));
        }

        expect(cameras).toEqual(snapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('agrees with the V4L2/ICAM oracle for any list of entries', () => {
    fc.assert(
      fc.property(fc.array(anyCameraArb, { maxLength: 12 }), (cameras) => {
        expect(cameras.filter(isV4l2CompatibleCamera)).toEqual(
          cameras.filter(v4l2CompatibleOracle)
        );
        for (const camera of cameras) {
          expect(isV4l2CompatibleCamera(camera)).toBe(v4l2CompatibleOracle(camera));
          // The device-path resolution the V4L2 filter reads is untouched.
          expect(cameraDeviceValue(camera)).toBe(deviceValueOracle(camera));
        }
      }),
      { numRuns: 25 }
    );
  });

  it('records the fixture-level offered decisions unchanged', () => {
    const staticEntry: CameraSourceEntry = {
      camera_source_id: 'static-image-camera',
      name: 'Static Image Camera',
      type: 'StaticImage',
      params: {},
      capabilities: STATIC_IMAGE_CAPABILITIES,
    };

    // Aravis picker
    expect(isAravisCompatibleCamera(ARAVIS_DISCOVERED)).toBe(true);
    expect(isAravisCompatibleCamera(CONFIGURED_CAMERA)).toBe(true);
    expect(isAravisCompatibleCamera(staticEntry)).toBe(true);
    expect(isAravisCompatibleCamera({ camera_source_id: 'c', type: 'Camera' })).toBe(false);
    expect(isAravisCompatibleCamera({ camera_source_id: 'i', type: 'ICam' })).toBe(false);
    expect(isAravisCompatibleCamera({ camera_source_id: 'v', type: 'V4L2Discovered' })).toBe(
      false
    );
    expect(
      isAravisCompatibleCamera({ camera_source_id: 'r', type: 'RTSP', params: { url: 'u' } })
    ).toBe(false);
    expect(isAravisCompatibleCamera({ camera_source_id: 'n', type: null })).toBe(false);

    // ICAM / V4L2 picker
    expect(isV4l2CompatibleCamera(ARAVIS_DISCOVERED)).toBe(false);
    expect(isV4l2CompatibleCamera(CONFIGURED_CAMERA)).toBe(true);
    expect(isV4l2CompatibleCamera(staticEntry)).toBe(false);
    expect(isV4l2CompatibleCamera({ camera_source_id: 'i', type: 'ICam' })).toBe(true);
    expect(isV4l2CompatibleCamera({ camera_source_id: 'v', type: 'V4L2Discovered' })).toBe(true);
    expect(isV4l2CompatibleCamera({ camera_source_id: 'c', type: 'Camera' })).toBe(false);
    expect(
      isV4l2CompatibleCamera({ camera_source_id: 'r', type: 'RTSP', params: { url: 'u' } })
    ).toBe(false);
    expect(isV4l2CompatibleCamera({ camera_source_id: 'n', type: null })).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Property 2d: the non-StaticImage guard — the offered set cannot widen
// (Requirement 3.4; tasks.md 3.1's type gate)
// --------------------------------------------------------------------------

describe('Property 2 (Preservation): a Camera entry carrying a StaticImage capabilities id', () => {
  it('still resolves null and is still NOT offered by the Aravis picker', () => {
    fc.assert(
      fc.property(cameraTypeGuardEntryArb, (camera) => {
        const snapshot = structuredClone(camera);

        // The entry really is the guard case: a non-empty capabilities id
        // with no usable params id, on a type that is NOT StaticImage.
        expect(camera.type).toBe('Camera');
        expect(staticImageCapabilityIdOracle(camera)).not.toBeNull();
        expect(paramsCameraIdOracle(camera)).toBeNull();

        // The fallback is scoped to StaticImage, so nothing resolves here…
        expect(cameraIdValue(camera)).toBeNull();
        // …and the picker's Camera arm therefore does not offer it.
        // `isAravisCompatibleCamera` reads `cameraIdValue`, so a
        // type-agnostic fallback would flip this to true and widen the
        // offered set past the deploy-time compatible set.
        expect(isAravisCompatibleCamera(camera)).toBe(false);
        expect(isAravisCompatibleCamera(camera)).toBe(aravisCompatibleOracle(camera));

        expect(camera).toEqual(snapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('is not offered even when the block is the live static-image identity verbatim', () => {
    const guard: CameraSourceEntry = {
      camera_source_id: 'cfg-guard',
      name: 'Configured source with a static-image capabilities block',
      type: 'Camera',
      params: { devicePath: '/dev/video0' },
      capabilities: STATIC_IMAGE_CAPABILITIES,
      origin: 'edge-configured',
      sync_status: 'synced',
    };

    expect(cameraIdValue(guard)).toBeNull();
    expect(isAravisCompatibleCamera(guard)).toBe(false);
    // It stays V4L2-compatible through its device path, as recorded.
    expect(isV4l2CompatibleCamera(guard)).toBe(true);

    // Applying it leaves camera_id absent rather than binding the
    // capabilities id.
    const { parameters } = applyAravisCameraSelection({}, guard, 'jetson-thor1');
    expect(Object.prototype.hasOwnProperty.call(parameters, 'camera_id')).toBe(false);
  });

  it('keeps every non-StaticImage type out of the fallback', () => {
    for (const type of ['AravisDiscovered', 'V4L2Discovered', 'RTSP', 'ICam', 'Folder']) {
      expect(
        cameraIdValue({
          camera_source_id: `entry-${type}`,
          type,
          params: {},
          capabilities: STATIC_IMAGE_CAPABILITIES,
        })
      ).toBeNull();
    }
  });
});

// --------------------------------------------------------------------------
// Property 2e: gain/exposure, the hint, and purity (Requirement 3.3)
// --------------------------------------------------------------------------

describe('Property 2 (Preservation): gain/exposure copying, the binding hint, and purity', () => {
  it('copies numeric gain/exposure, leaves other parameters untouched, and records the hint for any entry', () => {
    fc.assert(
      fc.property(priorParametersArb, anyCameraArb, idArb, (prior, camera, deviceId) => {
        const priorSnapshot = structuredClone(prior);
        const cameraSnapshot = structuredClone(camera);

        const { parameters, hint } = applyAravisCameraSelection(prior, camera, deviceId);

        // gain/exposure, the untouched-parameter guarantee, and the hint
        // hold for EVERY entry — including the StaticImage shapes the fix
        // changes the `camera_id` resolution for.
        assertSelectionInvariants(camera, priorSnapshot, deviceId, parameters, hint);

        // camera_id is asserted only outside the bug condition, where the
        // pre-fix oracle still describes the fixed behavior.
        const inBugCondition =
          camera.type === 'StaticImage' && staticImageCapabilityIdOracle(camera) !== null;
        if (!inBugCondition) {
          const expected = expectedCameraIdOracle(camera, priorSnapshot);
          if (expected.present) {
            expect(parameters.camera_id).toEqual(expected.value);
          } else {
            expect(Object.prototype.hasOwnProperty.call(parameters, 'camera_id')).toBe(false);
          }
        }

        // Purity: neither input is mutated.
        expect(prior).toEqual(priorSnapshot);
        expect(camera).toEqual(cameraSnapshot);
      }),
      { numRuns: 25 }
    );
  });

  it('records the fixture-level gain/exposure and hint outcomes unchanged', () => {
    // gain copied (numeric 9), exposure kept at its prior value (the
    // source's is the string 'high').
    const source: CameraSourceEntry = {
      ...ARAVIS_DISCOVERED,
      params: { cameraId: 'c', gain: 9, exposure: 'high' },
    };
    expect(
      applyAravisCameraSelection({ gain: 1, exposure: 2 }, source, 'dev-1')
    ).toEqual({
      parameters: { gain: 9, exposure: 2, camera_id: 'c' },
      hint: {
        cameraSourceId: 'arv-1a2b3c4d5e6f',
        cameraName: 'Basler acA1920',
        sourceDeviceId: 'dev-1',
      },
    });

    // A nameless source's hint falls back to the source id.
    expect(
      applyAravisCameraSelection(
        {},
        { camera_source_id: 'bare', name: '', type: 'StaticImage' },
        'dev-2'
      )
    ).toEqual({
      parameters: {},
      hint: { cameraSourceId: 'bare', cameraName: 'bare', sourceDeviceId: 'dev-2' },
    });
  });
});
