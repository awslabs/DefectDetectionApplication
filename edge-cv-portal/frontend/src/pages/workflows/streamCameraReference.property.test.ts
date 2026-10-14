/**
 * **Feature: rtsp-rtmp-stream-cameras, Property 7: Stream picker compatibility filter**
 * **Feature: rtsp-rtmp-stream-cameras, Property 8: Stream selection sets the URL and hint and never credentials**
 *
 * Property 7: for any list of Camera_Registry entries and any stream node
 * type, the picker offers exactly the entries whose type matches the
 * node's protocol (`RTSP` for `rtsp_camera_source`, `RTMP` for
 * `rtmp_stream_source`).
 *
 * Property 8: for any stream entry and prior node parameters, applying
 * the selection sets `url` to the entry's Stream_URL, produces the
 * standard binding hint, leaves every other parameter unchanged, and
 * copies no key or value from the entry other than the URL.
 *
 * **Validates: Requirements 3.3, 3.4, 3.6**
 */
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import {
  applyStreamCameraSelection,
  isCameraReferenceParameter,
  isStreamCompatibleCamera,
  STREAM_CAMERA_SOURCE_TYPES,
  streamUrlValue,
  type CameraSourceEntry,
} from './cameraReference';
import type { JsonValue } from './types';

const NUM_RUNS = { numRuns: 100 };
const STREAM_NODE_TYPES = ['rtsp_camera_source', 'rtmp_stream_source'] as const;
const ENTRY_TYPES = [
  'RTSP',
  'RTMP',
  'rtsp',
  'Camera',
  'ICam',
  'V4L2Discovered',
  'AravisDiscovered',
  'StaticImage',
  'Folder',
  '',
] as const;

const jsonScalar: fc.Arbitrary<JsonValue> = fc.oneof(
  fc.string({ maxLength: 12 }),
  fc.integer(),
  fc.boolean(),
  fc.constant(null)
);

const entryArb: fc.Arbitrary<CameraSourceEntry> = fc.record(
  {
    camera_source_id: fc.string({ minLength: 1, maxLength: 10 }),
    name: fc.option(fc.string({ maxLength: 12 }), { nil: null }),
    type: fc.oneof(fc.constantFrom(...ENTRY_TYPES), fc.constant(null)),
    params: fc.option(fc.dictionary(fc.string({ maxLength: 8 }), jsonScalar, { maxKeys: 5 }), {
      nil: null,
    }),
    sync_status: fc.constantFrom('synced', 'pending', 'failed'),
    stale: fc.boolean(),
  },
  { requiredKeys: ['camera_source_id'] }
);

describe('Property 7: the stream picker offers exactly the matching protocol', () => {
  it('filters any registry list to the entries of the node type', () => {
    fc.assert(
      fc.property(fc.constantFrom(...STREAM_NODE_TYPES), fc.array(entryArb, { maxLength: 12 }), (typeId, entries) => {
        const offered = entries.filter((entry) => isStreamCompatibleCamera(typeId, entry));
        const wanted = STREAM_CAMERA_SOURCE_TYPES[typeId];
        expect(offered).toEqual(entries.filter((entry) => entry.type === wanted));
      }),
      NUM_RUNS
    );
  });

  it('offers nothing for a non-stream node type', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('icam_source', 'aravis_camera_source', 'folder_source', 'toString', ''),
        entryArb,
        (typeId, entry) => {
          expect(isStreamCompatibleCamera(typeId, entry)).toBe(false);
        }
      ),
      NUM_RUNS
    );
  });

  it('renders the url parameter of a stream node as the reference control, and nothing else', () => {
    for (const typeId of STREAM_NODE_TYPES) {
      expect(isCameraReferenceParameter(typeId, 'url')).toBe(true);
      expect(isCameraReferenceParameter(typeId, 'processing_mode')).toBe(false);
    }
    expect(isCameraReferenceParameter('toString', 'url')).toBe(false);
    expect(isCameraReferenceParameter('unified_input', 'url')).toBe(false);
  });
});

// Stream entries carrying credential-looking and server-managed keys, so a
// copied value is detectable.
const streamEntryArb = fc
  .record({
    id: fc.string({ minLength: 1, maxLength: 10 }),
    name: fc.option(fc.string({ maxLength: 12 }), { nil: null }),
    type: fc.constantFrom('RTSP', 'RTMP'),
    url: fc.stringMatching(/^rtsps?:\/\/[a-z0-9.]{1,12}(\/[a-z0-9]{0,8})?$/),
    extra: fc.dictionary(
      fc.constantFrom(
        'credentialRef',
        'credentialsConfigured',
        'credentialsUpdatedAt',
        'transport',
        'latencyMs',
        'decoder',
        'password',
        'username',
        'urlSecret'
      ),
      fc.oneof(
        fc.string({ minLength: 1, maxLength: 8 }).map((s) => `ENTRY-${s}`),
        fc.integer({ min: 1_000_000, max: 2_000_000 }),
        fc.constant({ secretArn: 'arn:ENTRY-secret', versionId: 'ENTRY-v' })
      ),
      { maxKeys: 6 }
    ),
  })
  .map(
    ({ id, name, type, url, extra }): CameraSourceEntry => ({
      camera_source_id: id,
      name,
      type,
      params: { ...extra, url },
      credentials: { configured: true, updatedAt: 1 },
    })
  );

const priorParametersArb = fc.dictionary(
  fc.constantFrom('url', 'processing_mode', 'frames_per_second', 'keep_recent_runs', 'max_frame_age_ms', 'x'),
  fc.oneof(fc.string({ maxLength: 10 }), fc.integer({ min: 0, max: 100 }), fc.constant(null)),
  { maxKeys: 5 }
);

describe('Property 8: selection sets the URL and hint, and never credentials', () => {
  it('sets url, keeps every other parameter, and records the standard hint', () => {
    fc.assert(
      fc.property(streamEntryArb, priorParametersArb, fc.string({ minLength: 1, maxLength: 10 }), (entry, prior, deviceId) => {
        const before = JSON.parse(JSON.stringify(prior));
        const { parameters, hint } = applyStreamCameraSelection(prior, entry, deviceId);

        expect(parameters.url).toBe(streamUrlValue(entry));
        for (const key of Object.keys(parameters)) {
          if (key !== 'url') {
            expect(parameters[key]).toEqual(before[key]);
          }
        }
        expect(new Set(Object.keys(parameters))).toEqual(new Set([...Object.keys(before), 'url']));
        expect(hint).toEqual({
          cameraSourceId: entry.camera_source_id,
          cameraName: entry.name ? entry.name : entry.camera_source_id,
          sourceDeviceId: deviceId,
        });
        // The input record is not mutated.
        expect(prior).toEqual(before);
      }),
      NUM_RUNS
    );
  });

  it('copies no key or value from the entry other than the URL', () => {
    fc.assert(
      fc.property(streamEntryArb, priorParametersArb, (entry, prior) => {
        const { parameters } = applyStreamCameraSelection(prior, entry, 'device-1');
        const serialized = JSON.stringify({ ...parameters, url: undefined });
        expect(serialized).not.toContain('ENTRY-');
        for (const [key, value] of Object.entries(entry.params ?? {})) {
          if (key === 'url') continue;
          if (!(key in prior)) {
            expect(key in parameters).toBe(false);
          } else if (typeof value === 'number') {
            expect(parameters[key]).toEqual(prior[key]);
          }
        }
      }),
      NUM_RUNS
    );
  });
});
