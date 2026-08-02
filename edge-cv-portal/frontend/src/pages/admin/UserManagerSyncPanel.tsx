/**
 * Edge sync panel of the User Manager page (portal-user-manager
 * Requirements 7.1, 7.4).
 *
 * Lists every configured edge device from
 * `GET /api/v1/admin/edge-sync/devices` with its last account-sync status
 * and timestamp (Requirement 7.4). A "Sync accounts" action on the
 * selected device opens a modal with an account multi-select; confirming
 * posts `{usernames: [...]}` to
 * `POST /api/v1/admin/edge-sync/devices/{deviceId}` to stage the selected
 * accounts and trigger an immediate sync attempt (Requirement 7.1).
 *
 * The component is self-contained: it owns its device list, selection,
 * modal, and error state, so the surrounding UserManager page only passes
 * the current account list (for the multi-select) and a success callback.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  FormField,
  Header,
  Modal,
  Multiselect,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import type {
  MultiselectProps,
  StatusIndicatorProps,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import type { AdminAccount, EdgeSyncDevice } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';

/** One display row of the device sync table (Requirement 7.4). */
export interface SyncDeviceRow {
  deviceId: string;
  /** Human-readable last sync status, e.g. "Success", "Never synced". */
  statusLabel: string;
  statusType: StatusIndicatorProps.Type;
  /** Formatted last sync timestamp, or '-' when never synced. */
  lastSyncLabel: string;
  pendingChanges: boolean;
  failureReason: string;
}

/**
 * Pure device row-model builder (Requirement 7.4): one row per device
 * carrying the last sync status (mapped to a status-indicator type) and
 * the last sync timestamp. Devices without any recorded sync show
 * "Never synced" and no timestamp.
 */
export function buildSyncDeviceRows(devices: EdgeSyncDevice[]): SyncDeviceRow[] {
  return devices.map((device) => {
    const status = device.lastSyncStatus ?? null;
    let statusLabel: string;
    let statusType: StatusIndicatorProps.Type;
    switch (status) {
      case 'success':
        statusLabel = 'Success';
        statusType = 'success';
        break;
      case 'failed':
        statusLabel = 'Failed';
        statusType = 'error';
        break;
      case 'in_progress':
        statusLabel = 'In progress';
        statusType = 'in-progress';
        break;
      case 'pending':
        statusLabel = 'Pending';
        statusType = 'pending';
        break;
      default:
        statusLabel = 'Never synced';
        statusType = 'stopped';
    }
    return {
      deviceId: device.device_id,
      statusLabel,
      statusType,
      lastSyncLabel:
        device.lastSyncAt != null
          ? new Date(device.lastSyncAt).toLocaleString()
          : '-',
      pendingChanges: device.pendingChanges === true,
      failureReason: device.failureReason ?? '',
    };
  });
}

interface UserManagerSyncPanelProps {
  /** Accounts offered in the sync multi-select (Requirement 7.1). */
  accounts: AdminAccount[];
  /** Called with the flashbar confirmation text after a sync is staged. */
  onSyncStarted: (message: string) => void;
}

export default function UserManagerSyncPanel({
  accounts,
  onSyncStarted,
}: UserManagerSyncPanelProps) {
  const [devices, setDevices] = useState<EdgeSyncDevice[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [syncModalOpen, setSyncModalOpen] = useState(false);

  const loadDevices = useCallback(async () => {
    setError('');
    setLoading(true);
    try {
      const response = await apiService.listEdgeSyncDevices();
      setDevices(response.devices ?? []);
    } catch (err: unknown) {
      setDevices([]);
      setError(getErrorMessage(err, 'Failed to load edge devices'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDevices();
  }, [loadDevices]);

  const rows = buildSyncDeviceRows(devices);
  const selectedRows = rows.filter((row) => row.deviceId === selectedDeviceId);

  const handleSyncStarted = (message: string) => {
    setSyncModalOpen(false);
    onSyncStarted(message);
    loadDevices();
  };

  return (
    <>
      {error ? (
        <Alert
          type="error"
          header="Failed to load edge devices"
          action={<Button onClick={loadDevices}>Retry</Button>}
        >
          {error}
        </Alert>
      ) : (
        <Table
          header={
            <Header
              variant="h2"
              counter={`(${devices.length})`}
              description="Last account-sync status per edge device"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={loadDevices}>
                    Refresh
                  </Button>
                  <Button
                    disabled={!selectedDeviceId}
                    onClick={() => setSyncModalOpen(true)}
                  >
                    Sync accounts
                  </Button>
                </SpaceBetween>
              }
            >
              Edge account sync
            </Header>
          }
          selectionType="single"
          selectedItems={selectedRows}
          onSelectionChange={({ detail }) =>
            setSelectedDeviceId(detail.selectedItems[0]?.deviceId ?? null)
          }
          trackBy="deviceId"
          ariaLabels={{
            selectionGroupLabel: 'Device selection',
            itemSelectionLabel: (_data, row) => `Select ${row.deviceId}`,
          }}
          columnDefinitions={[
            {
              id: 'deviceId',
              header: 'Device',
              cell: (item) => item.deviceId,
            },
            {
              id: 'status',
              header: 'Last sync status',
              cell: (item) => (
                <StatusIndicator type={item.statusType}>
                  {item.statusLabel}
                </StatusIndicator>
              ),
            },
            {
              id: 'lastSyncAt',
              header: 'Last sync',
              cell: (item) => item.lastSyncLabel,
            },
            {
              id: 'pending',
              header: 'Pending changes',
              cell: (item) => (item.pendingChanges ? 'Yes' : 'No'),
            },
            {
              id: 'failureReason',
              header: 'Failure reason',
              cell: (item) => item.failureReason || '-',
            },
          ]}
          items={rows}
          loading={loading}
          loadingText="Loading edge devices"
          variant="container"
          empty={
            <Box textAlign="center" color="inherit">
              <b>No edge devices</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                No edge devices are configured.
              </Box>
            </Box>
          }
        />
      )}

      {syncModalOpen && selectedDeviceId && (
        <SyncAccountsModal
          deviceId={selectedDeviceId}
          accounts={accounts}
          onSuccess={handleSyncStarted}
          onDismiss={() => setSyncModalOpen(false)}
        />
      )}
    </>
  );
}

interface SyncAccountsModalProps {
  deviceId: string;
  accounts: AdminAccount[];
  onSuccess: (message: string) => void;
  onDismiss: () => void;
}

export function SyncAccountsModal({
  deviceId,
  accounts,
  onSuccess,
  onDismiss,
}: SyncAccountsModalProps) {
  const [selectedOptions, setSelectedOptions] = useState<
    readonly MultiselectProps.Option[]
  >([]);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const options: MultiselectProps.Option[] = accounts.map((account) => ({
    value: account.username,
    label: account.username,
    description: account.email,
    tags: account.edge_capable ? undefined : ['no edge credential'],
  }));

  const submit = async () => {
    const usernames = selectedOptions
      .map((option) => option.value)
      .filter((value): value is string => typeof value === 'string');
    if (usernames.length === 0) {
      return;
    }
    setServerError('');
    setSubmitting(true);
    try {
      // Stage the selected accounts and trigger a sync attempt
      // (Requirement 7.1).
      await apiService.syncEdgeDevice(deviceId, usernames);
      onSuccess(
        `Account sync to ${deviceId} started for ${usernames.length} account${
          usernames.length === 1 ? '' : 's'
        }.`
      );
    } catch (err: unknown) {
      setServerError(getErrorMessage(err, 'Account sync failed to start'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Sync accounts to ${deviceId}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={selectedOptions.length === 0}
            >
              Sync accounts
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {serverError && (
          <Alert type="error" header="Account sync failed to start">
            {serverError}
          </Alert>
        )}
        <FormField
          label="Accounts to sync"
          description="The selected accounts are transferred to the device's local credential cache. Accounts without a captured edge credential sync without one and cannot log in locally."
        >
          <Multiselect
            selectedOptions={selectedOptions}
            onChange={({ detail }) =>
              setSelectedOptions(detail.selectedOptions)
            }
            options={options}
            placeholder="Choose accounts"
            ariaLabel="Accounts to sync"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}
