/**
 * Devices page: removing selected devices (DELETE /devices/{id} through
 * RemoveDevicesModal) and showing the outcome, including the notice the
 * device detail page hands over after it removes a device.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Devices from './Devices';
import { UsecaseProvider } from '../contexts/UsecaseContext';

const {
  listUseCases,
  listDevices,
  listDeviceRegistrations,
  listThingGroups,
  deleteDevice,
} = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  listDevices: vi.fn(),
  listDeviceRegistrations: vi.fn(),
  listThingGroups: vi.fn(),
  deleteDevice: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases,
      listDevices,
      listDeviceRegistrations,
      listThingGroups,
      deleteDevice,
    },
  };
});

function device(deviceId: string) {
  return {
    device_id: deviceId,
    usecase_id: 'uc-1',
    // Distinct from the id so each renders exactly once per row.
    thing_name: `${deviceId}-thing`,
    status: 'HEALTHY',
    installed_components: [],
  };
}

async function renderDevices(initialEntry: Parameters<typeof MemoryRouter>[0]['initialEntries'] = ['/devices']) {
  // Preselect the use case so the page loads its devices in a single pass.
  localStorage.setItem('dda-selected-usecase-id', 'uc-1');
  render(
    <MemoryRouter initialEntries={initialEntry}>
      <UsecaseProvider>
        <Devices />
      </UsecaseProvider>
    </MemoryRouter>
  );
  await waitFor(() => expect(listDevices).toHaveBeenCalled());
}

async function selectRow(deviceId: string) {
  const link = await screen.findByText(deviceId);
  const row = link.closest('tr');
  if (!row) throw new Error(`No row for ${deviceId}`);
  fireEvent.click(within(row).getByRole('checkbox'));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC One' }],
  });
  listDeviceRegistrations.mockResolvedValue({ registrations: [], count: 0 });
  listThingGroups.mockResolvedValue({ thing_groups: [], count: 0 });
});

describe('Devices — remove devices', () => {
  it('removes the selected device, then reloads the devices and registrations', async () => {
    listDevices
      .mockResolvedValueOnce({ devices: [device('station-a'), device('station-b')] })
      .mockResolvedValue({ devices: [device('station-b')] });
    deleteDevice.mockResolvedValue({
      deleted: true,
      device_id: 'station-a',
      usecase_id: 'uc-1',
      delete_thing: false,
      thing_deleted: false,
      certificates_deactivated: [],
      certificates_deleted: [],
      shadows_deleted: [],
      warnings: [],
    });
    await renderDevices();

    const removeButton = screen.getByTestId('remove-devices-button');
    expect(removeButton).toBeDisabled();

    await selectRow('station-a');
    expect(removeButton).not.toBeDisabled();

    fireEvent.click(removeButton);
    expect(await screen.findByText('Remove device')).toBeInTheDocument();

    const registrationLoads = listDeviceRegistrations.mock.calls.length;
    await act(async () => {
      fireEvent.click(screen.getByTestId('remove-devices-confirm'));
    });

    expect(deleteDevice).toHaveBeenCalledWith('station-a', 'uc-1', {
      deleteThing: false,
    });
    expect(await screen.findByText('Removed station-a from DDA.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('station-a')).not.toBeInTheDocument());
    expect(screen.getByText('station-b')).toBeInTheDocument();
    expect(listDeviceRegistrations.mock.calls.length).toBeGreaterThan(registrationLoads);
    // The dialog is closed and nothing is left selected.
    expect(screen.queryByTestId('remove-devices-confirm')).not.toBeInTheDocument();
    expect(removeButton).toBeDisabled();
  });

  it('reports a failed removal and keeps the device listed and selected', async () => {
    listDevices.mockResolvedValue({ devices: [device('station-a')] });
    deleteDevice.mockRejectedValue(
      new Error('Failed to delete the Greengrass core device: ConflictException')
    );
    await renderDevices();

    await selectRow('station-a');
    fireEvent.click(screen.getByTestId('remove-devices-button'));
    await act(async () => {
      fireEvent.click(await screen.findByTestId('remove-devices-confirm'));
    });

    expect(
      await screen.findByText(
        'Could not remove station-a: Failed to delete the Greengrass core device: ConflictException'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('station-a')).toBeInTheDocument();
    expect(screen.getByTestId('remove-devices-button')).not.toBeDisabled();
    expect(listDevices).toHaveBeenCalledTimes(1);
  });

  it('shows the notice handed over by the device detail page', async () => {
    listDevices.mockResolvedValue({ devices: [] });
    await renderDevices([
      {
        pathname: '/devices',
        search: '?usecase_id=uc-1',
        state: {
          removalNotice: {
            message: 'Removed jp6-old from DDA and deleted its AWS IoT thing.',
            warnings: ['jp6-old: Certificate c-1 is also attached to jp6-spare'],
          },
        },
      },
    ]);

    expect(
      screen.getByText('Removed jp6-old from DDA and deleted its AWS IoT thing.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('jp6-old: Certificate c-1 is also attached to jp6-spare')
    ).toBeInTheDocument();
  });
});
