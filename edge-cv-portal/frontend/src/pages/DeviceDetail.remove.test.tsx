/**
 * DeviceDetail "Remove device": confirms through RemoveDevicesModal, then
 * returns to the device list with the outcome, or stays and shows why the
 * removal failed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import DeviceDetail from './DeviceDetail';

const { getDevice, deleteDevice, navigate } = vi.hoisted(() => ({
  getDevice: vi.fn(),
  deleteDevice: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice, deleteDevice },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return () => Promise.resolve({});
      },
    }
  );
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ deviceId: 'jp6-orinagx' }),
  useNavigate: () => navigate,
  useSearchParams: () => [new URLSearchParams('usecase_id=usecase-1'), vi.fn()],
}));

// Heavy tabs are irrelevant here.
vi.mock('../components/DeviceCamerasTab', () => ({ default: () => null }));
vi.mock('../components/LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('../components/RemoteAccessTab', () => ({ default: () => null }));
vi.mock('../components/ResultsTab', () => ({ default: () => null }));

const device = {
  device_id: 'jp6-orinagx',
  usecase_id: 'usecase-1',
  thing_name: 'jp6-orinagx',
  status: 'HEALTHY',
  installed_components: [],
  test_device: false,
  target_architecture: 'arm64_jp6',
};

async function renderPage() {
  getDevice.mockResolvedValue({ device });
  render(<DeviceDetail />);
  await act(async () => {});
}

async function removeDevice({ deleteThing = false } = {}) {
  fireEvent.click(screen.getByTestId('remove-device-button'));
  if (deleteThing) {
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: /Also delete the AWS IoT thing and its certificates/,
      })
    );
  }
  await act(async () => {
    fireEvent.click(screen.getByTestId('remove-devices-confirm'));
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('DeviceDetail — remove device', () => {
  it('returns to the device list with the outcome after removing the device', async () => {
    deleteDevice.mockResolvedValue({
      deleted: true,
      device_id: 'jp6-orinagx',
      usecase_id: 'usecase-1',
      delete_thing: true,
      thing_deleted: true,
      certificates_deactivated: ['c-1'],
      certificates_deleted: ['c-1'],
      shadows_deleted: ['(classic)'],
      warnings: [],
    });
    await renderPage();

    await removeDevice({ deleteThing: true });

    expect(deleteDevice).toHaveBeenCalledWith('jp6-orinagx', 'usecase-1', {
      deleteThing: true,
    });
    expect(navigate).toHaveBeenCalledWith('/devices?usecase_id=usecase-1', {
      state: {
        removalNotice: {
          message: 'Removed jp6-orinagx from DDA and deleted its AWS IoT thing.',
          warnings: [],
        },
      },
    });
  });

  it('stays on the page and shows why the removal failed', async () => {
    deleteDevice.mockRejectedValue(new Error('Access denied'));
    await renderPage();

    await removeDevice();

    expect(navigate).not.toHaveBeenCalled();
    expect(
      screen.getByText('Could not remove jp6-orinagx: Access denied')
    ).toBeInTheDocument();
    // The dialog is closed; the page is still usable.
    expect(screen.queryByTestId('remove-devices-confirm')).not.toBeInTheDocument();
    expect(screen.getByTestId('remove-device-button')).toBeInTheDocument();
  });
});
