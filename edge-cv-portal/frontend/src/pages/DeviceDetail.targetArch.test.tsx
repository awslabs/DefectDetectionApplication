/**
 * Tests for the DeviceDetail Target Architecture editor: the portal UI for
 * recording the Devices-table `target_architecture` attribute that the
 * deployment architecture gates check (a device without one fails closed,
 * rejecting vLLM/plugin deployments).
 *
 * Covers:
 * - warning shown when no architecture is recorded;
 * - editing pre-suggests the value from the installed LocalServer
 *   component suffix (arm64JP6 -> arm64_jp6);
 * - Save calls PUT /devices/{id} with the selection and updates the view;
 * - recorded value renders as a badge.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import DeviceDetail from './DeviceDetail';

const { getDevice, updateDeviceFlags, otherApiCalls } = vi.hoisted(() => ({
  getDevice: vi.fn(),
  updateDeviceFlags: vi.fn(),
  otherApiCalls: [] as string[],
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice, updateDeviceFlags },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => {
          otherApiCalls.push(String(prop));
          return Promise.resolve({});
        };
      },
    }
  );
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ deviceId: 'jp6-orinagx' }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams('usecase_id=usecase-1'), vi.fn()],
}));

// The camera/logs/remote-access/results tabs pull heavy dependencies; stub
// them — this test exercises only the Overview tab.
vi.mock('../components/DeviceCamerasTab', () => ({ default: () => null }));
vi.mock('../components/LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('../components/RemoteAccessTab', () => ({ default: () => null }));
vi.mock('../components/ResultsTab', () => ({ default: () => null }));

const baseDevice = {
  device_id: 'jp6-orinagx',
  usecase_id: 'usecase-1',
  thing_name: 'jp6-orinagx',
  status: 'HEALTHY',
  installed_components: [
    {
      componentName: 'aws.edgeml.dda.LocalServer.arm64JP6',
      componentVersion: '1.0.28',
      lifecycleState: 'RUNNING',
      isRoot: true,
    },
  ],
  test_device: false,
  target_architecture: null as string | null,
};

const wrapper = () => createWrapper(document.body);

async function renderPage(device = baseDevice) {
  getDevice.mockResolvedValue({ device });
  const rendered = render(<DeviceDetail />);
  await act(async () => {});
  return rendered;
}

afterEach(() => {
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

describe('DeviceDetail — Target Architecture editor', () => {
  it('warns when no architecture is recorded', async () => {
    await renderPage();
    expect(
      screen.getByText(
        'Not recorded — vLLM/plugin deployments will be rejected'
      )
    ).not.toBeNull();
  });

  it('pre-suggests the architecture from the LocalServer component suffix and saves it', async () => {
    updateDeviceFlags.mockResolvedValue({
      device_id: 'jp6-orinagx',
      usecase_id: 'usecase-1',
      test_device: false,
      target_architecture: 'arm64_jp6',
    });
    await renderPage();

    await act(async () => {
      screen.getByText('Edit').click();
    });

    // Suggestion derived from ...LocalServer.arm64JP6 -> arm64_jp6.
    const select = wrapper().findSelect()!;
    expect(select.findTrigger().getElement().textContent).toContain(
      'arm64_jp6'
    );

    await act(async () => {
      screen.getByText('Save').click();
    });

    expect(updateDeviceFlags).toHaveBeenCalledTimes(1);
    expect(updateDeviceFlags).toHaveBeenCalledWith(
      'jp6-orinagx',
      'usecase-1',
      { target_architecture: 'arm64_jp6' }
    );
    // The recorded value now renders as a badge; the warning is gone.
    expect(screen.getByText('arm64_jp6')).not.toBeNull();
    expect(
      screen.queryByText(
        'Not recorded — vLLM/plugin deployments will be rejected'
      )
    ).toBeNull();
  });

  it('renders a recorded architecture as a badge without the warning', async () => {
    await renderPage({ ...baseDevice, target_architecture: 'arm64_jp6' });
    expect(screen.getByText('arm64_jp6')).not.toBeNull();
    expect(
      screen.queryByText(
        'Not recorded — vLLM/plugin deployments will be rejected'
      )
    ).toBeNull();
  });

  it('surfaces a save failure and keeps the editor open', async () => {
    updateDeviceFlags.mockRejectedValue(new Error('Access denied'));
    await renderPage();

    await act(async () => {
      screen.getByText('Edit').click();
    });
    await act(async () => {
      screen.getByText('Save').click();
    });

    expect(screen.getByText('Access denied')).not.toBeNull();
    // Editor stays open for retry.
    expect(screen.getByText('Save')).not.toBeNull();
  });
});
