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
  applyCameraSelection,
  cameraDeviceValue,
  defaultManualEntry,
  getCameraBindingHint,
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
