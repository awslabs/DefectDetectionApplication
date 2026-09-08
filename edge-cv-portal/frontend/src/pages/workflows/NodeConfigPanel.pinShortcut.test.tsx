/**
 * Bug condition exploration test for the "Pin a static test image…"
 * shortcut (static-image-camera-binding-and-pin-discoverability task 1,
 * Defect 2 — the recovery path).
 *
 * **Property 1: Bug Condition / Fix Checking (portal frontend, part 2)**
 *
 * WHEN the user clicks the node panel's pin shortcut THEN the URL the
 * system opens SHALL carry a focus parameter alongside the existing
 * `usecase_id` and `tab=cameras`, so the device Cameras tab can bring
 * the "Static image camera" panel into view on arrival instead of
 * landing at the top of a 9-row camera table.
 *
 * **Validates: Requirements 2.5, 2.6**
 *
 * **CRITICAL — exploration test**: this file MUST FAIL on the unfixed
 * code. The shortcut opens `/devices/{id}?usecase_id=…&tab=cameras` with
 * no scroll target, anchor, or highlight. The failure IS the
 * reproduction of the bug; the same assertions validate the fix once
 * task 3.2 lands.
 *
 * ## Fix contract declared by this test (task 3.2)
 *
 * The shortcut adds `focus=static-image` to the query it already builds,
 * keeping `usecase_id`, `tab=cameras`, the new-tab `noopener` open, the
 * disabled-until-a-reference-device-is-chosen gating, and the
 * manual-entry hiding. `DeviceDetail.tsx` reads that parameter and
 * passes it to `DeviceCamerasTab` (see
 * `src/components/DeviceCamerasTab.staticImageFocus.test.tsx`).
 *
 * The mock scaffolding mirrors `NodeConfigPanel.test.tsx`.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { CameraSourceEntry } from './cameraReference';
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

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

/** The query parameter the fix adds to the shortcut URL. */
const FOCUS_PARAMETER = 'focus';
const FOCUS_VALUE = 'static-image';

/**
 * `aravis_camera_source` with only the `camera_id` parameter, so the
 * picker's two selects (reference device + camera) are the only selects
 * rendered — the node the user is configuring when the static camera
 * turns out to have no pinned image.
 */
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
  {
    device_id: 'jetson-thor1',
    usecase_id: 'uc-1',
    thing_name: 'edge-thing-1',
    status: 'HEALTHY',
  },
];

/** The live jetson-thor1 static-image registry entry the picker offers. */
const ARAVIS_REGISTRY_CAMERAS: CameraSourceEntry[] = [
  {
    camera_source_id: 'static-image-camera',
    name: 'Static Image Camera',
    type: 'StaticImage',
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
    origin: 'edge-discovered',
    sync_status: 'synced',
    stale: false,
    absent: true,
  },
];

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
    cameras: ARAVIS_REGISTRY_CAMERAS,
    count: ARAVIS_REGISTRY_CAMERAS.length,
  });
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

// --------------------------------------------------------------------------
// Property 1 (part 2): the shortcut URL carries the focus parameter
// --------------------------------------------------------------------------

describe('Property 1 (Bug Condition, Defect 2): the pin shortcut URL', () => {
  it('carries the focus parameter alongside usecase_id and tab=cameras (Requirement 2.5)', async () => {
    const windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { container } = render(
      <NodeConfigPanel node={aravisNode()} onParametersChange={vi.fn()} />
    );
    await waitFor(() => expect(listDevices).toHaveBeenCalledWith('uc-1'));

    // Unchanged gating: the shortcut needs a reference device first.
    expect(screen.getByTestId('pin-static-image-shortcut')).toBeDisabled();

    const [deviceSelect] = createWrapper(container).findAllSelects();
    await waitFor(() => {
      deviceSelect.openDropdown();
      expect(deviceSelect.findDropdown().findOptions()).toHaveLength(1);
    });
    deviceSelect.selectOptionByValue('jetson-thor1');

    const shortcut = screen.getByTestId('pin-static-image-shortcut');
    await waitFor(() => expect(shortcut).not.toBeDisabled());
    fireEvent.click(shortcut);

    expect(windowOpen).toHaveBeenCalledTimes(1);
    const [url, target, features] = windowOpen.mock.calls[0] as [string, string, string];

    // Unchanged: the device page in a new tab with noopener.
    expect(target).toBe('_blank');
    expect(features).toBe('noopener');

    const opened = new URL(url, 'https://portal.example');
    expect(opened.pathname).toBe('/devices/jetson-thor1');

    // Unchanged parameters.
    expect(opened.searchParams.get('usecase_id')).toBe('uc-1');
    expect(opened.searchParams.get('tab')).toBe('cameras');

    // Requirement 2.5: the focus parameter the Cameras tab reads to bring
    // the "Static image camera" panel into view on arrival.
    expect(opened.searchParams.get(FOCUS_PARAMETER)).toBe(FOCUS_VALUE);

    windowOpen.mockRestore();
  });

  it('stays hidden in manual entry mode (Requirement 3.11 unchanged)', () => {
    const { container } = render(
      <NodeConfigPanel node={aravisNode()} onParametersChange={vi.fn()} />
    );
    const toggle = container.querySelector(
      'input[aria-label="Manual entry for camera_id"]'
    )!;
    fireEvent.click(toggle);
    expect(screen.queryByTestId('pin-static-image-shortcut')).toBeNull();
  });
});
