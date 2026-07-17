/**
 * Unit tests for the camera reference selection logic
 * (camera-registry-sync task 9.1, Requirements 7.2, 7.3, 7.5): applying
 * a Camera_Source to a Camera_Input_Node's parameters, the advisory
 * binding hint, the manual-entry default, and the hint surviving the
 * definition save/load round trip. The fast-check property test
 * (Property 11) and picker component tests are tasks 9.2/9.3.
 */

import { describe, expect, it } from 'vitest';
import {
  applyAravisCameraSelection,
  applyCameraSelection,
  cameraDeviceValue,
  cameraIdValue,
  defaultManualEntry,
  getCameraBindingHint,
  isAravisCompatibleCamera,
  isCameraReferenceParameter,
  type CameraSourceEntry,
} from './cameraReference';
import { fromWorkflowDefinition, toWorkflowDefinition, type BuilderNode } from './builderGraph';
import type { NodeTypeDescriptor, WorkflowDefinition } from './types';

const CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-a1b2',
  name: 'Line 1 inspection cam',
  type: 'Camera',
  params: { devicePath: '/dev/video2', cameraId: 'cam-1', gain: 8, exposure: 16000000 },
  origin: 'edge-configured',
  sync_status: 'synced',
  stale: false,
  absent: false,
};

describe('isCameraReferenceParameter', () => {
  it('matches only the camera_source device parameter', () => {
    expect(isCameraReferenceParameter('camera_source', 'device')).toBe(true);
    expect(isCameraReferenceParameter('camera_source', 'gain')).toBe(false);
    expect(isCameraReferenceParameter('folder_source', 'device')).toBe(false);
  });

  it('matches the aravis_camera_source camera_id parameter (Requirement 3.1)', () => {
    expect(isCameraReferenceParameter('aravis_camera_source', 'camera_id')).toBe(true);
    expect(isCameraReferenceParameter('aravis_camera_source', 'gain')).toBe(false);
    expect(isCameraReferenceParameter('aravis_camera_source', 'device')).toBe(false);
    expect(isCameraReferenceParameter('camera_source', 'camera_id')).toBe(false);
  });
});

describe('applyCameraSelection (Requirement 7.2)', () => {
  it('populates device, gain, and exposure and records the hint', () => {
    const { parameters, hint } = applyCameraSelection(
      { device: '/dev/video0', mode: 'auto' },
      CAMERA,
      'edge-device-1'
    );
    expect(parameters).toEqual({
      device: '/dev/video2',
      mode: 'auto',
      gain: 8,
      exposure: 16000000,
    });
    expect(hint).toEqual({
      cameraSourceId: 'cfg-a1b2',
      cameraName: 'Line 1 inspection cam',
      sourceDeviceId: 'edge-device-1',
    });
  });

  it('uses the url for sources without a device path and skips absent gain/exposure', () => {
    const rtsp: CameraSourceEntry = {
      camera_source_id: 'cfg-rtsp',
      name: '',
      type: 'RTSP',
      params: { url: 'rtsp://10.0.0.5/stream' },
    };
    const { parameters, hint } = applyCameraSelection({ gain: 4 }, rtsp, 'edge-device-2');
    expect(parameters).toEqual({ gain: 4, device: 'rtsp://10.0.0.5/stream' });
    // Nameless sources fall back to the id for the hint's display name.
    expect(hint.cameraName).toBe('cfg-rtsp');
  });

  it('retains the existing device value when the source carries neither path nor url', () => {
    const bare: CameraSourceEntry = { camera_source_id: 'disc-1', params: {} };
    const { parameters } = applyCameraSelection({ device: '/dev/video0' }, bare, 'd');
    expect(parameters.device).toBe('/dev/video0');
    expect(cameraDeviceValue(bare)).toBeNull();
  });

  it('does not mutate the input parameters (pure)', () => {
    const input = { device: '/dev/video0' };
    applyCameraSelection(input, CAMERA, 'edge-device-1');
    expect(input).toEqual({ device: '/dev/video0' });
  });
});

const ARAVIS_DISCOVERED: CameraSourceEntry = {
  camera_source_id: 'arv-1a2b3c4d5e6f',
  name: 'Basler acA1920',
  type: 'AravisDiscovered',
  params: { cameraId: 'Basler-40123456', serial: '40123456', protocol: 'GigEVision' },
  origin: 'edge-discovered',
  sync_status: 'synced',
};

describe('isAravisCompatibleCamera (Requirement 3.2)', () => {
  it('accepts AravisDiscovered entries and Camera entries with a cameraId', () => {
    expect(isAravisCompatibleCamera(ARAVIS_DISCOVERED)).toBe(true);
    // The configured CAMERA fixture carries params.cameraId: 'cam-1'.
    expect(isAravisCompatibleCamera(CAMERA)).toBe(true);
    // AravisDiscovered is compatible even without params.
    expect(
      isAravisCompatibleCamera({ camera_source_id: 'arv-x', type: 'AravisDiscovered' })
    ).toBe(true);
  });

  it('rejects other types and Camera entries without a non-empty cameraId', () => {
    expect(isAravisCompatibleCamera({ camera_source_id: 'v4l', type: 'V4L2Discovered' })).toBe(
      false
    );
    expect(
      isAravisCompatibleCamera({ camera_source_id: 'rtsp', type: 'RTSP', params: { url: 'u' } })
    ).toBe(false);
    expect(isAravisCompatibleCamera({ camera_source_id: 'c', type: 'Camera' })).toBe(false);
    expect(
      isAravisCompatibleCamera({ camera_source_id: 'c', type: 'Camera', params: { cameraId: '' } })
    ).toBe(false);
    expect(
      isAravisCompatibleCamera({ camera_source_id: 'c', type: 'Camera', params: { cameraId: 7 } })
    ).toBe(false);
  });
});

describe('cameraIdValue', () => {
  it('returns the non-empty string cameraId or null', () => {
    expect(cameraIdValue(ARAVIS_DISCOVERED)).toBe('Basler-40123456');
    expect(cameraIdValue(CAMERA)).toBe('cam-1');
    expect(cameraIdValue({ camera_source_id: 'x' })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: { cameraId: '' } })).toBeNull();
    expect(cameraIdValue({ camera_source_id: 'x', params: { cameraId: 42 } })).toBeNull();
  });
});

describe('applyAravisCameraSelection (Requirement 3.3)', () => {
  it('populates camera_id, gain, and exposure and records the hint', () => {
    const source: CameraSourceEntry = {
      ...ARAVIS_DISCOVERED,
      params: { cameraId: 'Basler-40123456', gain: 12, exposure: 250000 },
    };
    const { parameters, hint } = applyAravisCameraSelection(
      { camera_id: 'old-cam', mode: 'auto' },
      source,
      'edge-device-1'
    );
    expect(parameters).toEqual({
      camera_id: 'Basler-40123456',
      mode: 'auto',
      gain: 12,
      exposure: 250000,
    });
    expect(hint).toEqual({
      cameraSourceId: 'arv-1a2b3c4d5e6f',
      cameraName: 'Basler acA1920',
      sourceDeviceId: 'edge-device-1',
    });
  });

  it('skips absent or non-numeric gain/exposure and leaves other parameters untouched', () => {
    const source: CameraSourceEntry = {
      camera_source_id: 'arv-2',
      name: '',
      type: 'AravisDiscovered',
      params: { cameraId: 'cam-9', gain: 'high' },
    };
    const { parameters, hint } = applyAravisCameraSelection(
      { camera_id: '', gain: 4, custom: true },
      source,
      'edge-device-2'
    );
    expect(parameters).toEqual({ camera_id: 'cam-9', gain: 4, custom: true });
    // Nameless sources fall back to the id for the hint's display name.
    expect(hint.cameraName).toBe('arv-2');
  });

  it('retains the existing camera_id when the source carries none', () => {
    const bare: CameraSourceEntry = { camera_source_id: 'arv-3', type: 'AravisDiscovered' };
    const { parameters } = applyAravisCameraSelection({ camera_id: 'cam-kept' }, bare, 'd');
    expect(parameters.camera_id).toBe('cam-kept');
  });

  it('does not mutate the input parameters (pure)', () => {
    const input = { camera_id: 'cam-old' };
    applyAravisCameraSelection(input, ARAVIS_DISCOVERED, 'edge-device-1');
    expect(input).toEqual({ camera_id: 'cam-old' });
  });
});

describe('getCameraBindingHint', () => {
  it('parses a well-formed hint and rejects malformed ones', () => {
    const hint = { cameraSourceId: 'cfg-1', cameraName: 'cam', sourceDeviceId: 'dev' };
    expect(getCameraBindingHint({ cameraBindingHint: hint })).toEqual(hint);
    expect(getCameraBindingHint(undefined)).toBeNull();
    expect(getCameraBindingHint({})).toBeNull();
    expect(getCameraBindingHint({ cameraBindingHint: 'nope' })).toBeNull();
    expect(getCameraBindingHint({ cameraBindingHint: { cameraSourceId: 1 } })).toBeNull();
  });
});

describe('defaultManualEntry (Requirement 7.3)', () => {
  it('starts manual for hand-typed values and on the picker otherwise', () => {
    const hint = { cameraSourceId: 'c', cameraName: 'n', sourceDeviceId: 'd' };
    // Hand-typed value differing from the declared default: manual.
    expect(defaultManualEntry({ device: '/dev/video7' }, 'device', '/dev/video0', null)).toBe(true);
    // A hint always starts on the reference picker.
    expect(defaultManualEntry({ device: '/dev/video7' }, 'device', '/dev/video0', hint)).toBe(false);
    // Unset or still on the declared default: reference picker.
    expect(defaultManualEntry({}, 'device', '/dev/video0', null)).toBe(false);
    expect(defaultManualEntry({ device: '/dev/video0' }, 'device', '/dev/video0', null)).toBe(false);
    expect(defaultManualEntry({ device: '' }, 'device', null, null)).toBe(false);
  });
});

describe('binding hint definition round trip (Requirement 7.5)', () => {
  const DESCRIPTOR: NodeTypeDescriptor = {
    typeId: 'camera_source',
    category: 'input',
    displayName: 'Camera Source',
    inputs: [],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
    parameters: [
      { name: 'device', paramType: 'string', required: false, default: '/dev/video0' },
    ],
    mappings: [],
    hardwareDependent: true,
  };

  it('serializes the hint into nodes[].data and restores it on load', () => {
    const hint = { cameraSourceId: 'cfg-a1b2', cameraName: 'cam', sourceDeviceId: 'dev-1' };
    const node: BuilderNode = {
      id: 'camera_source_1',
      type: 'workflowNode',
      position: { x: 1, y: 2 },
      data: {
        descriptor: DESCRIPTOR,
        parameters: { device: '/dev/video2' },
        validationMessages: [],
        advisoryData: { cameraBindingHint: hint },
      },
    };

    const definition = toWorkflowDefinition([node], []);
    expect(definition.nodes[0].data).toEqual({ cameraBindingHint: hint });

    const restored = fromWorkflowDefinition(definition, [DESCRIPTOR]);
    expect(getCameraBindingHint(restored.nodes[0].data.advisoryData)).toEqual(hint);
  });

  it('omits the data field entirely for nodes without advisory data', () => {
    const definition: WorkflowDefinition = {
      schemaVersion: 1,
      nodes: [
        {
          id: 'camera_source_1',
          type: 'camera_source',
          position: { x: 0, y: 0 },
          parameters: { device: '/dev/video0' },
        },
      ],
      connections: [],
    };
    const loaded = fromWorkflowDefinition(definition, [DESCRIPTOR]);
    expect(loaded.nodes[0].data.advisoryData).toBeUndefined();
    const roundTripped = toWorkflowDefinition(loaded.nodes, loaded.edges);
    // Pre-feature definitions serialize identically (no data key).
    expect('data' in roundTripped.nodes[0]).toBe(false);
    expect(roundTripped).toEqual(definition);
  });
});
