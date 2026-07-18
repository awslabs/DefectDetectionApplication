/**
 * Example-based unit tests for the edge sync panel
 * (portal-user-manager Requirements 7.1, 7.4):
 *
 * - `buildSyncDeviceRows`: one row per device carrying the last sync
 *   status (mapped to a status-indicator type) and the last sync
 *   timestamp; devices without a recorded sync show "Never synced" (7.4).
 * - Rendering: devices listed with status, timestamp, pending changes,
 *   and failure reason (7.4); load failures render an error alert.
 * - Sync action: enabled once a device is selected; the modal's account
 *   multi-select posts the chosen usernames to the device sync endpoint
 *   (7.1); submit is disabled with no accounts chosen; server errors are
 *   surfaced in the modal.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import UserManagerSyncPanel, {
  buildSyncDeviceRows,
} from './UserManagerSyncPanel';
import type { AdminAccount, EdgeSyncDevice } from '../../services/api';

const { listEdgeSyncDevices, syncEdgeDevice } = vi.hoisted(() => ({
  listEdgeSyncDevices: vi.fn(),
  syncEdgeDevice: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { listEdgeSyncDevices, syncEdgeDevice },
}));

const LAST_SYNC_AT = Date.UTC(2024, 0, 15, 12, 30, 0);

const DEVICES: EdgeSyncDevice[] = [
  {
    device_id: 'edge-device-1',
    lastSyncStatus: 'success',
    lastSyncAt: LAST_SYNC_AT,
    pendingChanges: false,
  },
  {
    device_id: 'edge-device-2',
    lastSyncStatus: 'failed',
    lastSyncAt: null,
    pendingChanges: true,
    failureReason: 'device unreachable',
  },
  {
    device_id: 'edge-device-3',
  },
];

function account(overrides: Partial<AdminAccount> = {}): AdminAccount {
  return {
    username: 'operator1',
    email: 'op1@example.com',
    email_verified: true,
    role: 'Operator',
    user_status: 'CONFIRMED',
    enabled: true,
    edge_capable: true,
    ...overrides,
  };
}

const ACCOUNTS: AdminAccount[] = [
  account(),
  account({ username: 'admin', email: 'admin@example.com', role: 'PortalAdmin' }),
];

function renderPanel(onSyncStarted = vi.fn()) {
  render(
    <UserManagerSyncPanel accounts={ACCOUNTS} onSyncStarted={onSyncStarted} />
  );
  return { onSyncStarted };
}

function isDisabled(button: HTMLElement): boolean {
  return (
    button.hasAttribute('disabled') ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listEdgeSyncDevices.mockResolvedValue({ devices: DEVICES, count: DEVICES.length });
});

describe('buildSyncDeviceRows', () => {
  it('produces exactly one row per device with status and timestamp (Requirement 7.4)', () => {
    const rows = buildSyncDeviceRows(DEVICES);
    expect(rows).toHaveLength(DEVICES.length);
    expect(rows[0]).toEqual({
      deviceId: 'edge-device-1',
      statusLabel: 'Success',
      statusType: 'success',
      lastSyncLabel: new Date(LAST_SYNC_AT).toLocaleString(),
      pendingChanges: false,
      failureReason: '',
    });
  });

  it('maps a failed sync with its reason and no timestamp', () => {
    const row = buildSyncDeviceRows(DEVICES)[1];
    expect(row.statusLabel).toBe('Failed');
    expect(row.statusType).toBe('error');
    expect(row.lastSyncLabel).toBe('-');
    expect(row.pendingChanges).toBe(true);
    expect(row.failureReason).toBe('device unreachable');
  });

  it('labels devices without any recorded sync as never synced', () => {
    const row = buildSyncDeviceRows(DEVICES)[2];
    expect(row.statusLabel).toBe('Never synced');
    expect(row.lastSyncLabel).toBe('-');
    expect(row.pendingChanges).toBe(false);
  });

  it('maps in-progress and pending statuses', () => {
    const rows = buildSyncDeviceRows([
      { device_id: 'd1', lastSyncStatus: 'in_progress' },
      { device_id: 'd2', lastSyncStatus: 'pending' },
    ]);
    expect(rows[0].statusLabel).toBe('In progress');
    expect(rows[0].statusType).toBe('in-progress');
    expect(rows[1].statusLabel).toBe('Pending');
    expect(rows[1].statusType).toBe('pending');
  });

  it('returns an empty row list for no devices', () => {
    expect(buildSyncDeviceRows([])).toEqual([]);
  });
});

describe('UserManagerSyncPanel rendering', () => {
  it('lists every device with its last sync status and timestamp (Requirement 7.4)', async () => {
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText('edge-device-1')).toBeInTheDocument();
    });
    expect(listEdgeSyncDevices).toHaveBeenCalledTimes(1);

    expect(screen.getByText('Success')).toBeInTheDocument();
    expect(screen.getByText(new Date(LAST_SYNC_AT).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('device unreachable')).toBeInTheDocument();
    expect(screen.getByText('Never synced')).toBeInTheDocument();

    for (const header of [
      'Device',
      'Last sync status',
      'Last sync',
      'Pending changes',
      'Failure reason',
    ]) {
      expect(screen.getByRole('columnheader', { name: header })).toBeInTheDocument();
    }
  });

  it('renders an error alert with retry when the device list fails to load', async () => {
    listEdgeSyncDevices.mockRejectedValue(new Error('devices exploded'));
    renderPanel();

    expect(await screen.findByText('Failed to load edge devices')).toBeInTheDocument();
    expect(screen.getByText('devices exploded')).toBeInTheDocument();
    expect(screen.queryByText('edge-device-1')).toBeNull();

    // Retry re-fetches and restores the device table.
    listEdgeSyncDevices.mockResolvedValue({ devices: DEVICES, count: DEVICES.length });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('edge-device-1')).toBeInTheDocument();
  });

  it('shows the empty state when no devices are configured', async () => {
    listEdgeSyncDevices.mockResolvedValue({ devices: [], count: 0 });
    renderPanel();

    expect(await screen.findByText('No edge devices')).toBeInTheDocument();
  });
});

describe('sync action (Requirement 7.1)', () => {
  async function openSyncModal() {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText('edge-device-1')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('radio', { name: 'Select edge-device-1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Sync accounts' }));
    await screen.findByText('Sync accounts to edge-device-1');
  }

  it('enables the sync action only once a device is selected', async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText('edge-device-1')).toBeInTheDocument();
    });

    const syncButton = screen.getByRole('button', { name: 'Sync accounts' });
    expect(isDisabled(syncButton)).toBe(true);

    fireEvent.click(screen.getByRole('radio', { name: 'Select edge-device-1' }));
    expect(isDisabled(syncButton)).toBe(false);
  });

  it('posts the multi-selected usernames to the device sync endpoint (Requirement 7.1)', async () => {
    syncEdgeDevice.mockResolvedValue({ message: 'ok' });
    await openSyncModal();

    const multiselect = createWrapper(document.body).findMultiselect()!;
    multiselect.openDropdown();
    multiselect.selectOptionByValue('operator1');
    multiselect.selectOptionByValue('admin');

    // The modal footer's submit button (the last "Sync accounts" button).
    fireEvent.click(screen.getAllByRole('button', { name: 'Sync accounts' }).at(-1)!);

    await waitFor(() =>
      expect(syncEdgeDevice).toHaveBeenCalledWith('edge-device-1', [
        'operator1',
        'admin',
      ])
    );
  });

  it('disables the modal submit until at least one account is chosen', async () => {
    await openSyncModal();

    // The modal footer's submit button (the last "Sync accounts" button).
    const submit = screen.getAllByRole('button', { name: 'Sync accounts' }).at(-1)!;
    expect(isDisabled(submit)).toBe(true);
    expect(syncEdgeDevice).not.toHaveBeenCalled();

    const multiselect = createWrapper(document.body).findMultiselect()!;
    multiselect.openDropdown();
    multiselect.selectOptionByValue('operator1');
    expect(isDisabled(submit)).toBe(false);
  });

  it('reports the started sync so the parent can show a confirmation', async () => {
    syncEdgeDevice.mockResolvedValue({ message: 'ok' });
    const onSyncStarted = vi.fn();
    render(
      <UserManagerSyncPanel accounts={ACCOUNTS} onSyncStarted={onSyncStarted} />
    );
    await waitFor(() => {
      expect(screen.getByText('edge-device-1')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('radio', { name: 'Select edge-device-1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Sync accounts' }));
    await screen.findByText('Sync accounts to edge-device-1');

    const multiselect = createWrapper(document.body).findMultiselect()!;
    multiselect.openDropdown();
    multiselect.selectOptionByValue('operator1');
    fireEvent.click(screen.getAllByRole('button', { name: 'Sync accounts' }).at(-1)!);

    await waitFor(() => expect(onSyncStarted).toHaveBeenCalledTimes(1));
    const message: string = onSyncStarted.mock.calls[0][0];
    expect(message).toContain('edge-device-1');
    // The device list is refreshed after staging a sync.
    expect(listEdgeSyncDevices).toHaveBeenCalledTimes(2);
  });

  it('surfaces server errors in the modal without reporting success', async () => {
    syncEdgeDevice.mockRejectedValue(new Error('sync staging failed'));
    await openSyncModal();

    const multiselect = createWrapper(document.body).findMultiselect()!;
    multiselect.openDropdown();
    multiselect.selectOptionByValue('operator1');
    fireEvent.click(screen.getAllByRole('button', { name: 'Sync accounts' }).at(-1)!);

    expect(await screen.findByText('sync staging failed')).toBeInTheDocument();
    // Only the initial device load happened: no refresh on failure.
    expect(listEdgeSyncDevices).toHaveBeenCalledTimes(1);
  });
});
