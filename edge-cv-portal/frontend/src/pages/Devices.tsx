import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Table,
  Header,
  SpaceBetween,
  StatusIndicator,
  Box,
  TextFilter,
  Button,
  Link,
  Modal,
  Alert,
  Select,
  SelectProps,
} from '@cloudscape-design/components';
import {
  apiService,
  DeviceRegistration,
  RegistrationWithCommand,
} from '../services/api';
import { Device, UseCase } from '../types';
import { useUsecase } from '../contexts/UsecaseContext';
import { useTableSort } from '../hooks/useTableSort';
import { getErrorMessage } from '../utils/errorHandling';
import RegisterDeviceDialog from '../components/RegisterDeviceDialog';
import SetupCommandDialog from '../components/SetupCommandDialog';

export default function Devices() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [filteringText, setFilteringText] = useState('');
  const [selectedItems, setSelectedItems] = useState<Device[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Device registrations (station-quick-setup Requirements 6.3, 6.4, 6.6, 6.9).
  const [showRegisterDialog, setShowRegisterDialog] = useState(false);
  const [setupResult, setSetupResult] = useState<RegistrationWithCommand | null>(
    null
  );
  const [registrations, setRegistrations] = useState<DeviceRegistration[]>([]);
  const [registrationsLoading, setRegistrationsLoading] = useState(false);
  const [registrationsError, setRegistrationsError] = useState<string | null>(
    null
  );
  // registration_id currently being regenerated/deleted (disables its row actions).
  const [actioningId, setActioningId] = useState<string | null>(null);
  // Registration pending a delete confirmation.
  const [deleteTarget, setDeleteTarget] = useState<DeviceRegistration | null>(
    null
  );
  const [deleting, setDeleting] = useState(false);

  // Use case management
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        
        // Use saved selection from context, or check URL, or auto-select first
        if (selectedUsecaseId) {
          const saved = useCaseList.find((uc: UseCase) => uc.usecase_id === selectedUsecaseId);
          if (saved) {
            setSelectedUseCase({
              label: saved.name,
              value: saved.usecase_id,
            });
            return;
          }
        }
        
        // Check for URL parameter
        const urlUseCaseId = searchParams.get('usecase_id');
        if (urlUseCaseId) {
          const preSelectedUseCase = useCaseList.find((uc: UseCase) => uc.usecase_id === urlUseCaseId);
          if (preSelectedUseCase) {
            setSelectedUseCase({
              label: preSelectedUseCase.name,
              value: preSelectedUseCase.usecase_id,
            });
            setSelectedUsecaseId(preSelectedUseCase.usecase_id);
            return;
          }
        }
        
        // Auto-select first use case if available
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
          setSelectedUsecaseId(useCaseList[0].usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
  }, [selectedUsecaseId, setSelectedUsecaseId, searchParams]);

  // Load devices and registrations when the use case changes (poll on load).
  useEffect(() => {
    if (selectedUseCase?.value) {
      loadDevices();
      loadRegistrations();
    } else {
      setDevices([]);
      setRegistrations([]);
      setLoading(false);
    }
  }, [selectedUseCase]);

  const loadDevices = async () => {
    if (!selectedUseCase?.value) return;
    
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.listDevices(selectedUseCase.value);
      setDevices(response.devices || []);
    } catch (err: any) {
      console.error('Failed to load devices:', err);
      setError(err.message || 'Failed to load devices');
      setDevices([]);
    } finally {
      setLoading(false);
    }
  };

  // Poll the Device_Registrations for the selected Use_Case (Requirement 6.3).
  const loadRegistrations = async () => {
    if (!selectedUseCase?.value) {
      setRegistrations([]);
      return;
    }
    try {
      setRegistrationsLoading(true);
      setRegistrationsError(null);
      const response = await apiService.listDeviceRegistrations(
        selectedUseCase.value
      );
      setRegistrations(response.registrations || []);
    } catch (err: any) {
      console.error('Failed to load device registrations:', err);
      setRegistrationsError(
        getErrorMessage(err, 'Failed to load device registrations')
      );
      setRegistrations([]);
    } finally {
      setRegistrationsLoading(false);
    }
  };

  // Refresh both the device list and the registrations panel.
  const handleRefresh = () => {
    loadDevices();
    loadRegistrations();
  };

  // Regenerate the Setup_Command for a non-completed registration
  // (Requirements 6.4, 2.5) and present the new command.
  const handleRegenerate = async (registration: DeviceRegistration) => {
    try {
      setActioningId(registration.registration_id);
      setRegistrationsError(null);
      const result = await apiService.regenerateSetupCommand(
        registration.registration_id
      );
      setSetupResult(result);
      await loadRegistrations();
    } catch (err: any) {
      setRegistrationsError(
        getErrorMessage(err, 'Failed to regenerate the setup command')
      );
    } finally {
      setActioningId(null);
    }
  };

  // Delete a non-completed registration, invalidating its token
  // (Requirements 6.6, 6.9).
  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      setDeleting(true);
      setActioningId(deleteTarget.registration_id);
      setRegistrationsError(null);
      await apiService.deleteDeviceRegistration(deleteTarget.registration_id);
      setDeleteTarget(null);
      await loadRegistrations();
    } catch (err: any) {
      setRegistrationsError(
        getErrorMessage(err, 'Failed to delete the device registration')
      );
    } finally {
      setDeleting(false);
      setActioningId(null);
    }
  };

  // A newly created or regenerated registration should refresh the panel.
  const handleRegistered = (result: RegistrationWithCommand) => {
    setShowRegisterDialog(false);
    setSetupResult(result);
    loadRegistrations();
  };

  const getStatusIndicator = (status: string) => {
    const statusLower = status?.toLowerCase() || 'unknown';
    switch (statusLower) {
      case 'healthy':
      case 'online':
        return <StatusIndicator type="success">Healthy</StatusIndicator>;
      case 'unhealthy':
      case 'offline':
        return <StatusIndicator type="error">Unhealthy</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Error</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status || 'Unknown'}</StatusIndicator>;
    }
  };

  // Setup_Status chip for a Device_Registration (Requirement 6.3).
  const getRegistrationStatus = (status: DeviceRegistration['status']) => {
    switch (status) {
      case 'completed':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'in_progress':
        return <StatusIndicator type="in-progress">In progress</StatusIndicator>;
      case 'pending':
        return <StatusIndicator type="pending">Pending</StatusIndicator>;
      case 'expired':
        return <StatusIndicator type="stopped">Expired</StatusIndicator>;
      case 'failed':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp?: string | number) => {
    if (!timestamp) return 'Never';
    const date = new Date(timestamp);
    const now = Date.now();
    const diff = now - date.getTime();

    if (diff < 60000) {
      return 'Just now';
    } else if (diff < 3600000) {
      return `${Math.floor(diff / 60000)} minutes ago`;
    } else if (diff < 86400000) {
      return `${Math.floor(diff / 3600000)} hours ago`;
    } else {
      return date.toLocaleString();
    }
  };

  // Filter devices based on search text
  const filteredDevices = devices.filter((device: Device) => {
    if (!filteringText) return true;
    const searchLower = filteringText.toLowerCase();
    return (
      device.device_id.toLowerCase().includes(searchLower) ||
      device.thing_name?.toLowerCase().includes(searchLower) ||
      device.status?.toLowerCase().includes(searchLower) ||
      device.platform?.toLowerCase().includes(searchLower)
    );
  });

  const { items: sortedDevices, sortingProps } = useTableSort(filteredDevices);

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {registrationsError && (
        <Alert
          type="error"
          dismissible
          onDismiss={() => setRegistrationsError(null)}
        >
          {registrationsError}
        </Alert>
      )}

      {/* Device registrations panel (station-quick-setup Requirements 6.3, 6.4, 6.6, 6.9) */}
      <Table
        resizableColumns
        variant="container"
        header={
          <Header
            variant="h2"
            description="Register a device to generate a one-line setup command, then track its provisioning status."
            counter={`(${registrations.length})`}
            actions={
              <Button
                variant="primary"
                onClick={() => setShowRegisterDialog(true)}
                disabled={!selectedUseCase}
              >
                Add Device
              </Button>
            }
          >
            Device Registrations
          </Header>
        }
        loading={registrationsLoading}
        items={registrations}
        columnDefinitions={[
          {
            id: 'device_name',
            header: 'Device Name',
            cell: (item: DeviceRegistration) => item.device_name,
          },
          {
            id: 'device_group',
            header: 'Device Group',
            cell: (item: DeviceRegistration) => item.device_group,
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item: DeviceRegistration) => getRegistrationStatus(item.status),
          },
          {
            id: 'token_expires_at',
            header: 'Token Expires',
            // Show the token expiry only while pending/in_progress (Requirement 6.3).
            cell: (item: DeviceRegistration) =>
              item.status === 'pending' || item.status === 'in_progress'
                ? new Date(item.token_expires_at * 1000).toLocaleString()
                : '-',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item: DeviceRegistration) => {
              // Regenerate and Delete are offered only for non-completed
              // registrations (Requirements 6.4, 6.6, 6.9).
              if (item.status === 'completed') {
                return '-';
              }
              const busy = actioningId === item.registration_id;
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="normal"
                    loading={busy}
                    onClick={() => handleRegenerate(item)}
                  >
                    Regenerate
                  </Button>
                  <Button
                    variant="normal"
                    disabled={busy}
                    onClick={() => setDeleteTarget(item)}
                  >
                    Delete
                  </Button>
                </SpaceBetween>
              );
            },
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No device registrations</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase
                ? 'Add a device to generate a setup command for a new station.'
                : 'Select a use case to view device registrations.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => setShowRegisterDialog(true)}>
                Add Device
              </Button>
            )}
          </Box>
        }
      />

      <Table
        resizableColumns
        header={
          <Header
            variant="h1"
            description="Monitor and manage Greengrass core devices set up via setup_station.sh"
            counter={`(${filteredDevices.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => {
                    setSelectedUseCase(detail.selectedOption);
                    setSelectedUsecaseId(detail.selectedOption?.value || null);
                  }}
                  placeholder="Select use case"
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                  }))}
                />
                <Button
                  iconName="refresh"
                  onClick={handleRefresh}
                  loading={loading || registrationsLoading}
                  disabled={!selectedUseCase}
                >
                  Refresh
                </Button>
                <Button
                  variant="primary"
                  onClick={() => setShowRegisterDialog(true)}
                  disabled={!selectedUseCase}
                >
                  Add Device
                </Button>
              </SpaceBetween>
            }
          >
            Devices
          </Header>
        }
        loading={loading}
        items={sortedDevices}
        {...sortingProps}
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        columnDefinitions={[
          {
            id: 'device_id',
            header: 'Device ID',
            cell: (item: Device) => (
              <Link onFollow={() => navigate(`/devices/${item.device_id}?usecase_id=${selectedUseCase?.value}`)}>
                {item.device_id}
              </Link>
            ),
            sortingField: 'device_id',
          },
          {
            id: 'thing_name',
            header: 'Thing Name',
            cell: (item: Device) => item.thing_name || '-',
            sortingField: 'thing_name',
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item: Device) => getStatusIndicator(item.status),
            sortingField: 'status',
          },
          {
            id: 'platform',
            header: 'Platform',
            cell: (item: Device) => item.platform || '-',
          },
          {
            id: 'architecture',
            header: 'Architecture',
            cell: (item: Device) => item.architecture || '-',
          },
          {
            id: 'last_status_update',
            header: 'Last Seen',
            cell: (item: Device) => formatTimestamp(item.last_status_update),
            sortingField: 'last_status_update',
          },
          {
            id: 'components',
            header: 'Components',
            cell: (item: Device) => item.installed_components?.length || 0,
          },
        ]}
        filter={
          <TextFilter
            filteringText={filteringText}
            filteringPlaceholder="Search devices"
            filteringAriaLabel="Filter devices"
            onChange={({ detail }) => setFilteringText(detail.filteringText)}
          />
        }
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No devices</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase 
                ? 'No portal-managed devices found. Register a device above to provision a new station.'
                : 'Select a use case to view devices.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => setShowRegisterDialog(true)}>Add Device</Button>
            )}
          </Box>
        }
        variant="full-page"
      />

      {/* Register a device -> generates a one-line setup command (tasks 9.2/9.3). */}
      <RegisterDeviceDialog
        visible={showRegisterDialog}
        usecaseId={selectedUseCase?.value ?? null}
        onDismiss={() => setShowRegisterDialog(false)}
        onRegistered={handleRegistered}
      />

      {/* Display the generated Setup_Command after create/regenerate. */}
      {setupResult && (
        <SetupCommandDialog
          setupCommand={setupResult.setup_command}
          tokenExpiresAt={setupResult.token_expires_at}
          deviceName={setupResult.registration.device_name}
          onDismiss={() => setSetupResult(null)}
        />
      )}

      {/* Confirm deletion of a non-completed registration (Requirement 6.6). */}
      <Modal
        visible={!!deleteTarget}
        onDismiss={() => (deleting ? undefined : setDeleteTarget(null))}
        header="Delete device registration"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleConfirmDelete}
                loading={deleting}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            Delete the registration for{' '}
            <b>{deleteTarget?.device_name}</b>? This invalidates its setup
            token, so any unused setup command will stop working.
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
