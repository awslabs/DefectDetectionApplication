/**
 * Component tests for the stream flavor of the camera reference picker
 * (rtsp-rtmp-stream-cameras task 11.7 — Requirements 3.3, 3.4, 3.5, 3.6).
 *
 * - A Stream_Camera_Source_Node's `url` picker offers only the reference
 *   device's Camera_Sources of the node's protocol, each described by its
 *   name, Stream_URL, codec, resolution, health, sync status, and
 *   credential state (3.3).
 * - Selecting one sets `url` and the standard binding hint, and nothing
 *   else: no setting and no credential state reaches the node (3.4, 3.6).
 * - A typed URL with credentials, the other protocol's scheme, or a
 *   Secret_Query_Parameter shows the Stream_URL message, in manual entry
 *   and in picker mode alike, without echoing the credential (3.5).
 *
 * The mock scaffolding mirrors `NodeConfigPanel.pinShortcut.test.tsx`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { CameraBindingHint, CameraSourceEntry } from './cameraReference';
import { STREAM_URL_PATTERN } from './streamUrl';
import type { JsonValue, NodeTypeDescriptor } from './types';

const { listModels, listDevices, getDeviceCameras, useUsecaseMock } = vi.hoisted(() => ({
  listModels: vi.fn(),
  listDevices: vi.fn(),
  getDeviceCameras: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { listModels, listDevices, getDeviceCameras },
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: useUsecaseMock,
}));

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

type StreamTypeId = 'rtsp_camera_source' | 'rtmp_stream_source';

/** A stream node type with only its `url` parameter, as served. */
function streamDescriptor(typeId: StreamTypeId): NodeTypeDescriptor {
  return {
    typeId,
    category: 'input',
    displayName: typeId === 'rtsp_camera_source' ? 'RTSP Camera' : 'RTMP Stream',
    inputs: [],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
    parameters: [
      {
        name: 'url',
        paramType: 'string',
        required: true,
        default: null,
        description: 'The Stream_URL, without credentials',
        constraints: { minLength: 1, maxLength: 2048, regex: STREAM_URL_PATTERN },
      },
    ],
    mappings: [],
    hardwareDependent: false,
  };
}

function streamNode(
  typeId: StreamTypeId,
  parameters: Record<string, JsonValue> = {},
  hint?: CameraBindingHint
): BuilderNode {
  return {
    id: `${typeId}_1`,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: {
      descriptor: streamDescriptor(typeId),
      parameters,
      validationMessages: [],
      ...(hint ? { advisoryData: { cameraBindingHint: hint as unknown as JsonValue } } : {}),
    },
  };
}

const DEVICE_ID = 'jetson-thor1';
const DOCK_URL = 'rtsp://10.0.4.21:554/Streaming/Channels/101';

const RTSP_DOCK: CameraSourceEntry = {
  camera_source_id: 'rtsp-dock',
  name: 'Dock 3 overview',
  type: 'RTSP',
  params: {
    url: DOCK_URL,
    transport: 'udp',
    latencyMs: 400,
    credentialsConfigured: true,
    credentialsUpdatedAt: 1790000000000,
  },
  credentials: { configured: true, updatedAt: 1790000000000 },
  capabilities: {
    stream: { state: 'streaming', codec: 'h265', width: 1920, height: 1080, decoder: 'hardware' },
  },
  origin: 'portal-created',
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const RTSP_YARD: CameraSourceEntry = {
  camera_source_id: 'rtsp-yard',
  name: 'Yard',
  type: 'RTSP',
  params: { url: 'rtsps://yard.local/live' },
  credentials: { configured: false, updatedAt: null },
  capabilities: {
    stream: { state: 'reconnecting', codec: 'h264', width: 1280, height: 720, decoder: 'software' },
  },
  origin: 'edge-configured',
  sync_status: 'pending',
  stale: true,
  absent: false,
};

const RTMP_LINE: CameraSourceEntry = {
  camera_source_id: 'rtmp-line',
  name: 'Line 1 encoder',
  type: 'RTMP',
  params: { url: 'rtmp://media.local/live/line1' },
  credentials: { configured: true, updatedAt: 1790000000000 },
  capabilities: { stream: { state: 'streaming', codec: 'h264', width: 1920, height: 1080 } },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const USB_CAM: CameraSourceEntry = {
  camera_source_id: 'cfg-usb',
  name: 'USB cam',
  type: 'Camera',
  params: { devicePath: '/dev/video0' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

function registry(cameras: CameraSourceEntry[]) {
  return {
    device_id: DEVICE_ID,
    usecase_id: 'uc-1',
    state: 'synced',
    cameras,
    count: cameras.length,
  };
}

beforeEach(() => {
  listModels.mockReset();
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  listDevices.mockReset();
  listDevices.mockResolvedValue({
    devices: [{ device_id: DEVICE_ID, usecase_id: 'uc-1', thing_name: DEVICE_ID, status: 'HEALTHY' }],
    count: 1,
  });
  getDeviceCameras.mockReset();
  getDeviceCameras.mockResolvedValue(registry([USB_CAM, RTSP_DOCK, RTMP_LINE, RTSP_YARD]));
  useUsecaseMock.mockReturnValue({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() });
});

/** Choose the reference device and open the camera dropdown. */
async function openCameraOptions(container: HTMLElement) {
  await waitFor(() => expect(listDevices).toHaveBeenCalledWith('uc-1'));
  const [deviceSelect] = createWrapper(container).findAllSelects();
  await waitFor(() => {
    deviceSelect.openDropdown();
    expect(deviceSelect.findDropdown().findOptions()).toHaveLength(1);
  });
  deviceSelect.selectOptionByValue(DEVICE_ID);
  await waitFor(() => expect(getDeviceCameras).toHaveBeenCalledWith(DEVICE_ID, 'uc-1'));
  const cameraSelect = createWrapper(container).findAllSelects()[1];
  await waitFor(() => {
    cameraSelect.openDropdown();
    expect(cameraSelect.findDropdown().getElement().textContent).not.toContain('Loading cameras');
  });
  return cameraSelect;
}

function optionTexts(select: ReturnType<ReturnType<typeof createWrapper>['findSelect']>) {
  return select!
    .findDropdown()
    .findOptions()
    .map((option) => option.getElement().textContent ?? '');
}

/** The error text of the field that has one, or null. */
function fieldError(container: HTMLElement): string | null {
  for (const field of createWrapper(container).findAllFormFields()) {
    const error = field.findError();
    if (error) {
      return error.getElement().textContent;
    }
  }
  return null;
}

// --------------------------------------------------------------------------
// Options and descriptions (Requirement 3.3)
// --------------------------------------------------------------------------

describe('stream picker options (Requirement 3.3)', () => {
  it("offers only the RTSP node's protocol, with URL, codec, resolution, health, sync and credentials", async () => {
    const { container } = render(
      <NodeConfigPanel node={streamNode('rtsp_camera_source')} onParametersChange={vi.fn()} />
    );
    const cameraSelect = await openCameraOptions(container);
    const options = optionTexts(cameraSelect);

    expect(options).toHaveLength(2);
    const [dock, yard] = options;
    for (const part of [
      'Dock 3 overview',
      DOCK_URL,
      'H.265',
      '1920\u00d71080',
      'streaming',
      'synced',
      'Credentials configured',
    ]) {
      expect(dock).toContain(part);
    }
    for (const part of [
      'Yard',
      'Stale',
      'rtsps://yard.local/live',
      'H.264',
      '1280\u00d7720',
      'reconnecting',
      'pending',
      'No credentials',
    ]) {
      expect(yard).toContain(part);
    }
    // The other protocol and the V4L2 camera are not offered.
    expect(options.join(' ')).not.toContain('Line 1 encoder');
    expect(options.join(' ')).not.toContain('USB cam');
    // A setting and the credential timestamp are never shown as text.
    expect(options.join(' ')).not.toContain('udp');
    expect(options.join(' ')).not.toContain('1790000000000');
  });

  it("offers only RTMP cameras on an RTMP node", async () => {
    const { container } = render(
      <NodeConfigPanel node={streamNode('rtmp_stream_source')} onParametersChange={vi.fn()} />
    );
    const options = optionTexts(await openCameraOptions(container));
    expect(options).toHaveLength(1);
    expect(options[0]).toContain('Line 1 encoder');
    expect(options[0]).toContain('rtmp://media.local/live/line1');
  });

  it('names the protocol when the device has no camera of it', async () => {
    getDeviceCameras.mockResolvedValue(registry([USB_CAM, RTSP_DOCK]));
    const { container } = render(
      <NodeConfigPanel node={streamNode('rtmp_stream_source')} onParametersChange={vi.fn()} />
    );
    const cameraSelect = await openCameraOptions(container);
    expect(cameraSelect.findDropdown().findOptions()).toHaveLength(0);
    expect(cameraSelect.findDropdown().getElement().textContent).toContain(
      'No RTMP cameras registered for this device'
    );
  });

  it('hides the static-image pin shortcut on a stream node', async () => {
    const { container } = render(
      <NodeConfigPanel node={streamNode('rtsp_camera_source')} onParametersChange={vi.fn()} />
    );
    await openCameraOptions(container);
    expect(screen.queryByTestId('pin-static-image-shortcut')).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Selection (Requirements 3.4, 3.6)
// --------------------------------------------------------------------------

describe('stream picker selection (Requirements 3.4, 3.6)', () => {
  it('sets url and the binding hint only, keeping every other parameter', async () => {
    const onCameraSelection = vi.fn();
    const { container } = render(
      <NodeConfigPanel
        node={streamNode('rtsp_camera_source', { processing_mode: 'on_trigger' })}
        onParametersChange={vi.fn()}
        onCameraSelection={onCameraSelection}
      />
    );
    const cameraSelect = await openCameraOptions(container);
    cameraSelect.selectOptionByValue('rtsp-dock');

    expect(onCameraSelection).toHaveBeenCalledTimes(1);
    const [nodeId, parameters, hint] = onCameraSelection.mock.calls[0];
    expect(nodeId).toBe('rtsp_camera_source_1');
    expect(parameters).toEqual({ processing_mode: 'on_trigger', url: DOCK_URL });
    expect(hint).toEqual({
      cameraSourceId: 'rtsp-dock',
      cameraName: 'Dock 3 overview',
      sourceDeviceId: DEVICE_ID,
    });
    // No setting, credential flag, or timestamp of the entry is copied.
    expect(JSON.stringify(parameters)).not.toMatch(/udp|latency|credential|1790000000000/i);
  });
});

// --------------------------------------------------------------------------
// Manual entry rejection message (Requirement 3.5)
// --------------------------------------------------------------------------

describe('typed Stream_URL messages (Requirement 3.5)', () => {
  it('rejects embedded credentials, naming where they belong, without echoing them', () => {
    const { container } = render(
      <NodeConfigPanel
        node={streamNode('rtsp_camera_source', { url: 'rtsp://admin:hunter2@10.0.4.21/stream' })}
        onParametersChange={vi.fn()}
      />
    );
    // A typed value without a hint opens in manual entry.
    const toggle = createWrapper(container).findCheckbox()!;
    expect(toggle.findNativeInput().getElement()).toBeChecked();
    const error = fieldError(container);
    expect(error).toContain('must not contain embedded user information');
    expect(error).toContain("credentials belong in the camera's configuration");
    expect(error).not.toContain('hunter2');
  });

  it("rejects the other protocol's scheme and a Secret_Query_Parameter", () => {
    const { container, rerender } = render(
      <NodeConfigPanel
        node={streamNode('rtsp_camera_source', { url: 'rtmp://media.local/live/line1' })}
        onParametersChange={vi.fn()}
      />
    );
    expect(fieldError(container)).toContain(
      "Stream URL scheme 'rtmp' is not allowed here; accepted schemes are rtsp, rtsps."
    );

    rerender(
      <NodeConfigPanel
        node={streamNode('rtmp_stream_source', { url: 'rtmp://media.local/live?streamkey=abc123' })}
        onParametersChange={vi.fn()}
      />
    );
    const error = fieldError(container);
    expect(error).toContain("query parameter 'streamkey' carries credentials");
    expect(error).not.toContain('abc123');
  });

  it('shows no message for a valid URL and reports what is typed', () => {
    const onParametersChange = vi.fn();
    const { container } = render(
      <NodeConfigPanel
        node={streamNode('rtsp_camera_source', { url: DOCK_URL })}
        onParametersChange={onParametersChange}
      />
    );
    expect(fieldError(container)).toBeNull();
    createWrapper(container).findInput()!.setInputValue('rtsp://u:p@10.0.4.21/x');
    expect(onParametersChange).toHaveBeenLastCalledWith('rtsp_camera_source_1', {
      url: 'rtsp://u:p@10.0.4.21/x',
    });
  });

  it('shows the same message in picker mode', () => {
    const { container } = render(
      <NodeConfigPanel
        node={streamNode(
          'rtsp_camera_source',
          { url: 'rtsp://admin:hunter2@10.0.4.21/stream' },
          { cameraSourceId: 'rtsp-dock', cameraName: 'Dock 3 overview', sourceDeviceId: DEVICE_ID }
        )}
        onParametersChange={vi.fn()}
      />
    );
    // A node with a hint opens on the picker.
    const toggle = createWrapper(container).findCheckbox()!;
    expect(toggle.findNativeInput().getElement()).not.toBeChecked();
    expect(createWrapper(container).findAllSelects()).toHaveLength(2);
    const error = fieldError(container);
    expect(error).toContain("credentials belong in the camera's configuration");
    expect(error).not.toContain('hunter2');
  });
});
