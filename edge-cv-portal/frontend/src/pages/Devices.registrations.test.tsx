/**
 * Component tests for the Device Registrations panel on the Devices page
 * (station-quick-setup task 9.5, Requirements 6.3, 6.4).
 *
 * Covers the registrations panel affordances from the design's frontend
 * section:
 *   - a Setup_Status chip per registration (Requirement 6.3),
 *   - the token expiry shown only while `pending`/`in_progress` (6.3),
 *   - Regenerate and Delete actions offered only for non-completed
 *     registrations (6.4, 6.6, 6.9), with completed registrations offering
 *     neither.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Devices from './Devices';
import { UsecaseProvider } from '../contexts/UsecaseContext';
import { DeviceRegistration, RegistrationWithCommand } from '../services/api';

const {
  listUseCases,
  listDevices,
  listDeviceRegistrations,
  regenerateSetupCommand,
  deleteDeviceRegistration,
  listThingGroups,
  registerDevice,
} = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  listDevices: vi.fn(),
  listDeviceRegistrations: vi.fn(),
  regenerateSetupCommand: vi.fn(),
  deleteDeviceRegistration: vi.fn(),
  listThingGroups: vi.fn(),
  registerDevice: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases,
      listDevices,
      listDeviceRegistrations,
      regenerateSetupCommand,
      deleteDeviceRegistration,
      listThingGroups,
      registerDevice,
    },
  };
});

function reg(overrides: Partial<DeviceRegistration> = {}): DeviceRegistration {
  return {
    registration_id: 'reg-1',
    usecase_id: 'uc-1',
    device_name: 'station-1',
    device_group: 'Line3_Group',
    status: 'pending',
    created_by: 'user-1',
    created_at: 1730000000,
    updated_at: 1730000000,
    token_expires_at: 1730005400,
    ...overrides,
  };
}

function regenResult(): RegistrationWithCommand {
  return {
    registration: reg({ status: 'pending' }),
    setup_command: 'curl -fsSL https://x/quick-setup/bootstrap | sudo bash',
    token_expires_at: 1730009000,
  };
}

async function renderDevices(registrations: DeviceRegistration[]) {
  listDeviceRegistrations.mockResolvedValue({
    registrations,
    count: registrations.length,
  });
  // Preselect the use case so the page loads registrations in a single pass
  // (avoids the auto-select effect re-triggering a reload mid-assertion).
  localStorage.setItem('dda-selected-usecase-id', 'uc-1');
  render(
    <MemoryRouter>
      <UsecaseProvider>
        <Devices />
      </UsecaseProvider>
    </MemoryRouter>
  );
  // Wait for the registrations load to complete and the panel to settle.
  if (registrations.length > 0) {
    await screen.findByText(registrations[0].device_name);
  } else {
    await waitFor(() => expect(listDeviceRegistrations).toHaveBeenCalled());
  }
}

/** Locate the registrations table row containing the given device name. */
async function registrationRow(deviceName: string): Promise<HTMLElement> {
  const cell = await screen.findByText(deviceName);
  const row = cell.closest('tr');
  if (!row) throw new Error(`No row for ${deviceName}`);
  return row;
}

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC One' }],
  });
  listDevices.mockResolvedValue({ devices: [] });
  listThingGroups.mockResolvedValue({ thing_groups: [], count: 0 });
  regenerateSetupCommand.mockResolvedValue(regenResult());
  deleteDeviceRegistration.mockResolvedValue({ message: 'deleted' });
  localStorage.clear();
});

describe('registration status chips (Requirement 6.3)', () => {
  it('renders a distinct status label for each Setup_Status', async () => {
    await renderDevices([
      reg({ registration_id: 'r-p', device_name: 'dev-pending', status: 'pending' }),
      reg({ registration_id: 'r-i', device_name: 'dev-inprog', status: 'in_progress' }),
      reg({ registration_id: 'r-c', device_name: 'dev-complete', status: 'completed' }),
      reg({ registration_id: 'r-e', device_name: 'dev-expired', status: 'expired' }),
      reg({ registration_id: 'r-f', device_name: 'dev-failed', status: 'failed' }),
    ]);

    await screen.findByText('dev-pending');
    expect(screen.getByText('Pending')).toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
  });
});

describe('token expiry visibility (Requirement 6.3)', () => {
  it('shows the expiry while pending/in_progress and hides it otherwise', async () => {
    const expiry = 1730005400;
    await renderDevices([
      reg({ registration_id: 'r-p', device_name: 'dev-pending', status: 'pending', token_expires_at: expiry }),
      reg({ registration_id: 'r-i', device_name: 'dev-inprog', status: 'in_progress', token_expires_at: expiry }),
      reg({ registration_id: 'r-c', device_name: 'dev-complete', status: 'completed', token_expires_at: expiry }),
    ]);

    await screen.findByText('dev-pending');
    const expiryLabel = new Date(expiry * 1000).toLocaleString();

    // pending and in_progress rows show the expiry timestamp.
    expect(within(await registrationRow('dev-pending')).getByText(expiryLabel)).toBeInTheDocument();
    expect(within(await registrationRow('dev-inprog')).getByText(expiryLabel)).toBeInTheDocument();
    // completed row shows a placeholder instead of the expiry.
    expect(within(await registrationRow('dev-complete')).queryByText(expiryLabel)).toBeNull();
  });
});

describe('regenerate / delete affordances (Requirements 6.4, 6.6, 6.9)', () => {
  it('offers neither Regenerate nor Delete for a completed registration', async () => {
    await renderDevices([
      reg({ registration_id: 'r-c', device_name: 'dev-complete', status: 'completed' }),
    ]);
    const row = await registrationRow('dev-complete');
    expect(within(row).queryByRole('button', { name: 'Regenerate' })).toBeNull();
    expect(within(row).queryByRole('button', { name: 'Delete' })).toBeNull();
  });

  it('offers Regenerate and Delete for a non-completed registration', async () => {
    await renderDevices([
      reg({ registration_id: 'r-e', device_name: 'dev-expired', status: 'expired' }),
    ]);
    const row = await registrationRow('dev-expired');
    expect(within(row).getByRole('button', { name: 'Regenerate' })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('regenerates the Setup_Command and presents the new command (Requirement 6.4/2.5)', async () => {
    await renderDevices([
      reg({ registration_id: 'r-f', device_name: 'dev-failed', status: 'failed' }),
    ]);
    fireEvent.click(
      within(await registrationRow('dev-failed')).getByRole('button', { name: 'Regenerate' })
    );

    await waitFor(() =>
      expect(regenerateSetupCommand).toHaveBeenCalledWith('r-f')
    );
    // The generated command is displayed in the SetupCommandDialog.
    expect(
      await screen.findByText('curl -fsSL https://x/quick-setup/bootstrap | sudo bash')
    ).toBeInTheDocument();
  });

  it('deletes a non-completed registration after confirmation (Requirement 6.6)', async () => {
    await renderDevices([
      reg({ registration_id: 'r-p', device_name: 'dev-pending', status: 'pending' }),
    ]);
    fireEvent.click(
      within(await registrationRow('dev-pending')).getByRole('button', { name: 'Delete' })
    );

    // Confirmation modal appears; confirm the deletion.
    const confirm = await screen.findByRole('heading', { name: 'Delete device registration' });
    expect(confirm).toBeInTheDocument();

    // The modal footer "Delete" button confirms.
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1)!);

    await waitFor(() =>
      expect(deleteDeviceRegistration).toHaveBeenCalledWith('r-p')
    );
  });
});
