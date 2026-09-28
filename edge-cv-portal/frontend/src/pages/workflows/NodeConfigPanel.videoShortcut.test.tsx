/**
 * The node panel's "Pin a test video…" shortcut (static-camera-video-loop
 * task 9.5, Requirement 9.7): one shortcut per virtual camera.
 *
 * The video shortcut sits next to the unchanged image shortcut, shares its
 * gating (disabled until a reference device is chosen, hidden in manual
 * entry mode), and opens the device's Cameras tab with the video focus
 * target, so the "Static video camera" panel is brought into view.
 *
 * The mock scaffolding mirrors `NodeConfigPanel.pinShortcut.test.tsx`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import {
  STATIC_IMAGE_FOCUS_PARAM,
  STATIC_VIDEO_FOCUS_VALUE,
  type CameraSourceEntry,
} from './cameraReference';
import type { NodeTypeDescriptor } from './types';

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

const ARAVIS: NodeTypeDescriptor = {
  typeId: 'aravis_camera_source',
  category: 'input',
  displayName: 'Aravis Camera Source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    {
      name: 'camera_id',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1 },
    },
  ],
  mappings: [],
  hardwareDependent: true,
};

const DEVICES = [
  { device_id: 'jetson-thor1', usecase_id: 'uc-1', thing_name: 'edge-thing-1', status: 'HEALTHY' },
];

const VIDEO_CAMERA: CameraSourceEntry = {
  camera_source_id: 'static-video-camera',
  name: 'Static Video Camera',
  type: 'StaticVideo',
  params: {},
  capabilities: { staticVideo: { id: 'static-video-camera', fps: 29.97 } },
  origin: 'edge-discovered',
  sync_status: 'synced',
  stale: false,
  absent: false,
};

/**
 * Choose the reference device once the device list has loaded. The
 * dropdown is opened and asserted outside waitFor: Cloudscape wrapper
 * interactions inside a waitFor callback keep re-triggering its
 * MutationObserver, so a failing check would hang instead of failing.
 */
async function chooseReferenceDevice(container: HTMLElement) {
  await waitFor(() => expect(listDevices).toHaveBeenCalled());
  await act(async () => {});
  const [deviceSelect] = createWrapper(container).findAllSelects();
  deviceSelect.openDropdown();
  expect(deviceSelect.findDropdown().findOptions()).toHaveLength(1);
  deviceSelect.selectOptionByValue('jetson-thor1');
}

function aravisNode(): BuilderNode {
  return {
    id: 'aravis_camera_source_1',
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor: ARAVIS, parameters: {}, validationMessages: [] },
  };
}

beforeEach(() => {
  listModels.mockReset();
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  listDevices.mockReset();
  listDevices.mockResolvedValue({ devices: DEVICES, count: DEVICES.length });
  getDeviceCameras.mockReset();
  getDeviceCameras.mockResolvedValue({
    device_id: 'jetson-thor1',
    state: 'synced',
    cameras: [VIDEO_CAMERA],
    count: 1,
  });
  useUsecaseMock.mockReturnValue({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() });
});

describe('the "Pin a test video…" shortcut', () => {
  it('opens the Cameras tab with the video focus target once a device is chosen', async () => {
    const windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { container } = render(
      <NodeConfigPanel node={aravisNode()} onParametersChange={vi.fn()} />
    );
    await waitFor(() => expect(listDevices).toHaveBeenCalledWith('uc-1'));

    // Two shortcuts, one per virtual camera, both gated on the device.
    expect(screen.getByTestId('pin-static-image-shortcut')).toBeDisabled();
    expect(screen.getByTestId('pin-static-video-shortcut')).toBeDisabled();
    expect(screen.getByTestId('pin-static-video-shortcut').textContent).toContain(
      'Pin a test video…'
    );

    await chooseReferenceDevice(container);

    const shortcut = screen.getByTestId('pin-static-video-shortcut');
    await waitFor(() => expect(shortcut).not.toBeDisabled());
    fireEvent.click(shortcut);

    expect(windowOpen).toHaveBeenCalledTimes(1);
    expect(windowOpen).toHaveBeenCalledWith(
      '/devices/jetson-thor1?usecase_id=uc-1&tab=cameras&focus=static-video',
      '_blank',
      'noopener'
    );
    const [url] = windowOpen.mock.calls[0] as [string];
    expect(new URL(url, 'https://portal.example').searchParams.get(STATIC_IMAGE_FOCUS_PARAM)).toBe(
      STATIC_VIDEO_FOCUS_VALUE
    );

    // The image shortcut still targets the image panel.
    fireEvent.click(screen.getByTestId('pin-static-image-shortcut'));
    expect(windowOpen).toHaveBeenLastCalledWith(
      '/devices/jetson-thor1?usecase_id=uc-1&tab=cameras&focus=static-image',
      '_blank',
      'noopener'
    );
    windowOpen.mockRestore();
  });

  it('offers the static video camera in the Aravis camera picker', async () => {
    const { container } = render(
      <NodeConfigPanel node={aravisNode()} onParametersChange={vi.fn()} />
    );
    await waitFor(() => expect(listDevices).toHaveBeenCalledWith('uc-1'));
    await chooseReferenceDevice(container);
    await waitFor(() => expect(getDeviceCameras).toHaveBeenCalled());
    await act(async () => {});

    const [, cameraSelect] = createWrapper(container).findAllSelects();
    cameraSelect.openDropdown();
    const options = cameraSelect
      .findDropdown()
      .findOptions()
      .map((option) => option.getElement().textContent ?? '');
    expect(options.some((text) => text.includes('Static Video Camera'))).toBe(true);
  });

  it('is hidden in manual entry mode, like the image shortcut', () => {
    const { container } = render(
      <NodeConfigPanel node={aravisNode()} onParametersChange={vi.fn()} />
    );
    const toggle = container.querySelector('input[aria-label="Manual entry for camera_id"]')!;
    fireEvent.click(toggle);
    expect(screen.queryByTestId('pin-static-video-shortcut')).toBeNull();
    expect(screen.queryByTestId('pin-static-image-shortcut')).toBeNull();
  });
});
