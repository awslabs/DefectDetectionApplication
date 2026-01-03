import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  ColumnLayout,
  StatusIndicator,
  Tabs,
  Button,
  Modal,
  Alert,
  Table,
  KeyValuePairs,
  Textarea,
  FormField,
  Input,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

export default function DeviceDetail() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const navigate = useNavigate();
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showRestartModal, setShowRestartModal] = useState(false);
  const [showRebootModal, setShowRebootModal] = useState(false);
  const [logFilter, setLogFilter] = useState('');
  const [filePath, setFilePath] = useState('/');

  const { data: deviceData, isLoading } = useQuery({
    queryKey: ['device', deviceId],
    queryFn: () => apiService.getDevice(deviceId!),
    enabled: !!deviceId,
  });

  const device = deviceData?.device;

  const getStatusIndicator = (status: string) => {
    switch (status) {
      case 'online':
        return <StatusIndicator type="success">Online</StatusIndicator>;
      case 'offline':
        return <StatusIndicator type="stopped">Offline</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Error</StatusIndicator>;
      default:
        return <StatusIndicator type="info">Unknown</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp?: number) => {
    if (!timestamp) return 'Never';
    return new Date(timestamp).toLocaleString();
  };

  const formatStorage = (used?: number, total?: number) => {
    if (!used || !total) return 'N/A';
    const usedGB = (used / (1024 * 1024 * 1024)).toFixed(2);
    const totalGB = (total / (1024 * 1024 * 1024)).toFixed(2);
    const percentage = ((used / total) * 100).toFixed(1);
    return `${usedGB} GB / ${totalGB} GB (${percentage}%)`;
  };

  if (isLoading) {
    return <Box>Loading device details...</Box>;
  }

  if (!device) {
    return (
      <Box textAlign="center" padding="xxl">
        <Alert type="error">Device not found</Alert>
        <Button onClick={() => navigate('/devices')}>Back to Devices</Button>
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      {/* Header */}
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/devices')}>Back to Devices</Button>
            <Button onClick={() => setShowRestartModal(true)}>Restart Greengrass</Button>
            <Button onClick={() => setShowRebootModal(true)}>Reboot Device</Button>
          </SpaceBetween>
        }
      >
        {device.device_id}
      </Header>

      {/* Status Cards */}
      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Status</Box>
          <Box variant="h2">{getStatusIndicator(device.status)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Last Seen</Box>
          <Box variant="h3">{formatTimestamp(device.last_heartbeat)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Storage</Box>
          <Box variant="h3">{formatStorage(device.storage_used, device.storage_total)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Components</Box>
          <Box variant="h3">{device.components?.length || 0}</Box>
        </Container>
      </ColumnLayout>

      {/* Tabs */}
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'overview',
            label: 'Overview',
            content: (
              <SpaceBetween size="l">
                <Container header={<Header variant="h2">Device Information</Header>}>
                  <ColumnLayout columns={2} variant="text-grid">
                    <KeyValuePairs
                      columns={1}
                      items={[
                        { label: 'Device ID', value: device.device_id },
                        { label: 'Thing Name', value: device.thing_name || '-' },
                        { label: 'Use Case', value: device.usecase_id },
                        { label: 'Status', value: device.status },
                      ]}
                    />
                    <KeyValuePairs
                      columns={1}
                      items={[
                        { label: 'Greengrass Version', value: device.greengrass_version || '-' },
                        { label: 'Camera Status', value: device.camera_status || '-' },
                        { label: 'Created', value: formatTimestamp(device.created_at) },
                        { label: 'Updated', value: formatTimestamp(device.updated_at) },
                      ]}
                    />
                  </ColumnLayout>
                </Container>

                <Container header={<Header variant="h2">Storage</Header>}>
                  <KeyValuePairs
                    columns={3}
                    items={[
                      {
                        label: 'Used',
                        value: device.storage_used
                          ? `${(device.storage_used / (1024 * 1024 * 1024)).toFixed(2)} GB`
                          : '-',
                      },
                      {
                        label: 'Total',
                        value: device.storage_total
                          ? `${(device.storage_total / (1024 * 1024 * 1024)).toFixed(2)} GB`
                          : '-',
                      },
                      {
                        label: 'Usage',
                        value:
                          device.storage_used && device.storage_total
                            ? `${((device.storage_used / device.storage_total) * 100).toFixed(1)}%`
                            : '-',
                      },
                    ]}
                  />
                </Container>
              </SpaceBetween>
            ),
          },
          {
            id: 'components',
            label: 'Components',
            content: (
              <Container header={<Header variant="h2">Installed Components</Header>}>
                <Table
                  items={device.components || []}
                  columnDefinitions={[
                    {
                      id: 'name',
                      header: 'Component Name',
                      cell: (item) => item.name,
                    },
                    {
                      id: 'version',
                      header: 'Version',
                      cell: (item) => item.version,
                    },
                    {
                      id: 'status',
                      header: 'Status',
                      cell: (item) => (
                        <StatusIndicator type={item.status === 'RUNNING' ? 'success' : 'info'}>
                          {item.status}
                        </StatusIndicator>
                      ),
                    },
                  ]}
                  empty={
                    <Box textAlign="center" color="inherit">
                      No components installed
                    </Box>
                  }
                />
              </Container>
            ),
          },
          {
            id: 'logs',
            label: 'Logs',
            content: (
              <Container
                header={
                  <Header
                    variant="h2"
                    actions={
                      <Button iconName="refresh" onClick={() => {}}>
                        Refresh
                      </Button>
                    }
                  >
                    Device Logs
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  <FormField label="Filter logs">
                    <Input
                      value={logFilter}
                      onChange={({ detail }) => setLogFilter(detail.value)}
                      placeholder="Search logs..."
                    />
                  </FormField>

                  <Box>
                    <Textarea
                      value="[2024-01-15 10:30:45] INFO: Greengrass Core started\n[2024-01-15 10:30:46] INFO: Component aws.edgeml.dda.LocalServer started\n[2024-01-15 10:30:47] INFO: Model component loaded successfully\n[2024-01-15 10:31:00] INFO: Inference request processed"
                      rows={20}
                      readOnly
                    />
                  </Box>

                  <Box variant="small" color="text-status-inactive">
                    Note: Log streaming is not yet implemented. This is sample data.
                  </Box>
                </SpaceBetween>
              </Container>
            ),
          },
          {
            id: 'files',
            label: 'Files',
            content: (
              <Container
                header={
                  <Header
                    variant="h2"
                    actions={
                      <Button iconName="refresh" onClick={() => {}}>
                        Refresh
                      </Button>
                    }
                  >
                    File Browser
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  <FormField label="Current Path">
                    <Input
                      value={filePath}
                      onChange={({ detail }) => setFilePath(detail.value)}
                      placeholder="/path/to/directory"
                    />
                  </FormField>

                  <Table
                    items={[
                      { name: 'greengrass.log', size: '2.5 MB', modified: '2024-01-15 10:30:00' },
                      { name: 'dda-app.log', size: '1.2 MB', modified: '2024-01-15 10:25:00' },
                      { name: 'config.json', size: '1.5 KB', modified: '2024-01-14 15:00:00' },
                    ]}
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Name',
                        cell: (item) => item.name,
                      },
                      {
                        id: 'size',
                        header: 'Size',
                        cell: (item) => item.size,
                      },
                      {
                        id: 'modified',
                        header: 'Modified',
                        cell: (item) => item.modified,
                      },
                      {
                        id: 'actions',
                        header: 'Actions',
                        cell: () => <Button iconName="download">Download</Button>,
                      },
                    ]}
                    empty={
                      <Box textAlign="center" color="inherit">
                        No files found
                      </Box>
                    }
                  />

                  <Box variant="small" color="text-status-inactive">
                    Note: File browsing is not yet implemented. This is sample data.
                  </Box>
                </SpaceBetween>
              </Container>
            ),
          },
        ]}
      />

      {/* Restart Modal */}
      <Modal
        visible={showRestartModal}
        onDismiss={() => setShowRestartModal(false)}
        header="Restart Greengrass"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowRestartModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  // TODO: Implement restart
                  setShowRestartModal(false);
                }}
              >
                Restart
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="warning">
            This will restart the AWS IoT Greengrass Core software on the device. The device will
            be temporarily unavailable during the restart.
          </Alert>
          <Box>
            Are you sure you want to restart Greengrass on device{' '}
            <Box variant="strong">{device.device_id}</Box>?
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Reboot Modal */}
      <Modal
        visible={showRebootModal}
        onDismiss={() => setShowRebootModal(false)}
        header="Reboot Device"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowRebootModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  // TODO: Implement reboot
                  setShowRebootModal(false);
                }}
              >
                Reboot
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="error">
            This will reboot the entire device. All running processes will be stopped and the device
            will be unavailable for several minutes.
          </Alert>
          <Box>
            Are you sure you want to reboot device <Box variant="strong">{device.device_id}</Box>?
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
