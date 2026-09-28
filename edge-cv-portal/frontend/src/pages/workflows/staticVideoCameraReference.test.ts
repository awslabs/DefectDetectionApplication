/**
 * Picker compatibility and id resolution for the Static_Video_Camera
 * (static-camera-video-loop task 9.2, Requirement 4.8).
 *
 * The device reports the video camera with empty `params` and its identity
 * under `capabilities.staticVideo`, the way it reports the image camera
 * under `capabilities.staticImage`. The Aravis picker offers it, and its id
 * resolves from that block — params first, type-gated, guarded against
 * malformed payloads — without changing how any other entry resolves.
 */
import { describe, expect, it } from 'vitest';
import {
  applyAravisCameraSelection,
  cameraIdValue,
  isAravisCompatibleCamera,
  isV4l2CompatibleCamera,
  MAX_PIN_VIDEO_BYTES,
  STATIC_IMAGE_FOCUS_PARAM,
  STATIC_IMAGE_FOCUS_VALUE,
  STATIC_VIDEO_FOCUS_VALUE,
  type CameraSourceEntry,
} from './cameraReference';
import type { JsonValue } from './types';

/** The video entry exactly as the device reports it (inventory.py). */
const STATIC_VIDEO_ENTRY: CameraSourceEntry = {
  camera_source_id: 'static-video-camera',
  name: 'Static Video Camera',
  type: 'StaticVideo',
  params: {},
  capabilities: {
    staticVideo: {
      id: 'static-video-camera',
      model: 'Static Video Camera',
      address: 'internal',
      physicalId: 'static-video-camera',
      protocol: 'StaticVideo',
      serial: 'STATIC-VIDEO-0',
      vendor: 'AWS-DDA',
      fps: 29.97002997002997,
    },
  },
  origin: 'edge-discovered',
  sync_status: 'synced',
  stale: false,
  absent: false,
};

describe('Static_Video_Camera in the Aravis picker', () => {
  it('is offered by the Aravis picker and not by the V4L2 picker', () => {
    expect(isAravisCompatibleCamera(STATIC_VIDEO_ENTRY)).toBe(true);
    expect(isV4l2CompatibleCamera(STATIC_VIDEO_ENTRY)).toBe(false);
    // Offered like StaticImage: unconditionally, even while absent.
    expect(isAravisCompatibleCamera({ ...STATIC_VIDEO_ENTRY, absent: true })).toBe(true);
    expect(isAravisCompatibleCamera({ camera_source_id: 'bare', type: 'StaticVideo' })).toBe(true);
  });

  it('resolves its id from capabilities.staticVideo.id', () => {
    expect(cameraIdValue(STATIC_VIDEO_ENTRY)).toBe('static-video-camera');
    const { parameters, hint } = applyAravisCameraSelection(
      { camera_id: 'previous', other: 1 },
      STATIC_VIDEO_ENTRY,
      'jetson-thor1'
    );
    expect(parameters).toEqual({ camera_id: 'static-video-camera', other: 1 });
    expect(hint).toEqual({
      cameraSourceId: 'static-video-camera',
      cameraName: 'Static Video Camera',
      sourceDeviceId: 'jetson-thor1',
    });
  });

  it('resolves params.cameraId first', () => {
    expect(
      cameraIdValue({ ...STATIC_VIDEO_ENTRY, params: { cameraId: 'from-params' } })
    ).toBe('from-params');
  });

  it('resolves null for malformed capability blocks', () => {
    const malformed: JsonValue[] = [null, 'text', 42, [], {}, { id: '' }, { id: 7 }];
    for (const staticVideo of malformed) {
      expect(
        cameraIdValue({ ...STATIC_VIDEO_ENTRY, capabilities: { staticVideo } })
      ).toBeNull();
    }
    expect(cameraIdValue({ ...STATIC_VIDEO_ENTRY, capabilities: null })).toBeNull();
  });

  it('keeps the two families apart and the fallback type-gated', () => {
    // A StaticVideo entry does not read the image block, nor vice versa.
    expect(
      cameraIdValue({
        ...STATIC_VIDEO_ENTRY,
        capabilities: { staticImage: { id: 'static-image-camera' } },
      })
    ).toBeNull();
    expect(
      cameraIdValue({
        camera_source_id: 'static-image-camera',
        type: 'StaticImage',
        params: {},
        capabilities: { staticVideo: { id: 'static-video-camera' } },
      })
    ).toBeNull();
    // Other types never resolve the video block, so a Camera entry carrying
    // it is not offered (the offered set cannot widen).
    for (const type of ['Camera', 'AravisDiscovered', 'V4L2Discovered', 'RTSP', 'ICam']) {
      const entry: CameraSourceEntry = {
        camera_source_id: `entry-${type}`,
        type,
        params: {},
        capabilities: STATIC_VIDEO_ENTRY.capabilities,
      };
      expect(cameraIdValue(entry)).toBeNull();
    }
    expect(
      isAravisCompatibleCamera({
        camera_source_id: 'cfg-1',
        type: 'Camera',
        params: { devicePath: '/dev/video0' },
        capabilities: STATIC_VIDEO_ENTRY.capabilities,
      })
    ).toBe(false);
  });

  it('shares the focus parameter with the image shortcut, under its own value', () => {
    expect(STATIC_IMAGE_FOCUS_PARAM).toBe('focus');
    expect(STATIC_IMAGE_FOCUS_VALUE).toBe('static-image');
    expect(STATIC_VIDEO_FOCUS_VALUE).toBe('static-video');
    expect(MAX_PIN_VIDEO_BYTES).toBe(100 * 1024 * 1024);
  });
});
