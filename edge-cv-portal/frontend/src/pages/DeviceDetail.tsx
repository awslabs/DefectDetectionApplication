import { useState, useEffect } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
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
  BreadcrumbGroup,
  Badge,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Device, InstalledComponent, DeviceDeployment } from '../types';

export default function DeviceDetail() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showRestartModal, setShowRestartModal] = useState(false);
  const [device, setDevice] = useState<Device | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const usecaseId = searchParams.get('usecase_id');

  useEffect(() => {
    if (deviceId && usecaseId) {
      loadDevice();
    } else if (!usecaseId) {
      setError('Use case ID is required');
      setLoading(false);
    }
  }, [deviceId, usecaseId]);

  const loadDevice = async () => {
    if (!deviceId || !usecaseId) return;
    
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.getDevice(deviceId, usecaseId);
      setDevice(response.device);
    } catch (err: any) {
      console.error('Failed to load device:', err);
      setError(err.message || 'Failed to load device');
    } finally {
      setLoading(false);
    }
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

  const getComponentStatusIndicator = (state: string) => {
    switch (state?.toUpperCase()) {
      case 'RUNNING':
        return <StatusIndicator type="success">Running</StatusIndicator>;
      case 'FINISHED':
        return <StatusIndicator type="info">Finished</StatusIndicator>;
      case 'BROKEN':
      case 'ERRORED':
        return <StatusIndicator type="error">{state}</StatusIndicator>;
      case 'INSTALLED':
        return <StatusIndicator type="info">Installed</StatusIndicator>;
      case 'STARTING':
        return <StatusIndicator type="in-progress">Starting</StatusIndicator>;
      case 'STOPPING':
        return <StatusIndicator type="in-progress">Stopping</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{state || 'Unknown'}</StatusIndicator>;
    }
  };

  const getDeploymentStatusIndicator = (status: string) => {
    switch (status?.toUpperCase()) {
      case 'SUCCEEDED':
        return <StatusIndicator type="success">Succeeded</StatusIndicator>;
      case 'FAILED':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'IN_PROGRESS':
        return <StatusIndicator type="in-progress">In Progress</StatusIndicator>;
      case 'QUEUED':
        return <StatusIndicator type="pending">Queued</StatusIndicator>;
      case 'CANCELED':
        return <StatusIndicator type="stopped">Canceled</StatusIndicator>;
      case 'TIMED_OUT':
        return <StatusIndicator type="warning">Timed Out</StatusIndicator>;
      case 'REJECTED':
        return <StatusIndicator type="error">Rejected</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status || 'Unknown'}</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp?: string | number) => {
    if (!timestamp) return 'N/A';
    return new Date(timestamp).toLocaleString();
  };

  if (loading) {
    return <Box>Loading device details...</Box>;
  }

  if (error) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{error}</Alert>
        <Button onClick={() => navigate(`/devices?usecase_id=${usecaseId}`)}>Back to Devices</Button>
      </SpaceBetween>
    );
  }

  if (!device) {
    return (
      <Box textAlign="center" padding="xxl">
        <Alert type="error">Device not found</Alert>
        <Button onClick={() => navigate(`/devices?usecase_id=${usecaseId}`)}>Back to Devices</Button>
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      {/* Breadcrumb */}
      <BreadcrumbGroup
        items={[
          { text: 'Devices', href: `/devices?usecase_id=${usecaseId}` },
          { text: device.device_id, href: '#' },
        ]}
        onFollow={(e) => {
          e.preventDefault();
          if (e.detail.href !== '#') {
            navigate(e.detail.href);
          }
        }}
      />

      {/* Header */}
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate(`/devices?usecase_id=${usecaseId}`)}>Back to Devices</Button>
            <Button iconName="refresh" onClick={loadDevice}>Refresh</Button>
            <Button onClick={() => setShowRestartModal(true)}>Restart Greengrass</Button>
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
          <Box variant="h3">{formatTimestamp(device.last_status_update)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Greengrass Version</Box>
          <Box variant="h3">{device.greengrass_version || 'N/A'}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Components</Box>
          <Box variant="h3">{device.installed_components?.length || 0}</Box>
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
                        { label: 'Thing ARN', value: device.thing_arn || '-' },
                        { label: 'Thing Type', value: device.thing_type || '-' },
                        { label: 'Status', value: device.status },
                      ]}
                    />
                    <KeyValuePairs
                      columns={1}
                      items={[
                        { label: 'Greengrass Version', value: device.greengrass_version || '-' },
                        { label: 'Platform', value: device.platform || '-' },
                        { label: 'Architecture', value: device.architecture || '-' },
                        { label: 'Last Status Update', value: formatTimestamp(device.last_status_update) },
                      ]}
                    />
                  </ColumnLayout>
                </Container>

                {device.tags && Object.keys(device.tags).length > 0 && (
                  <Container header={<Header variant="h2">Tags</Header>}>
                    <SpaceBetween direction="horizontal" size="xs">
                      {Object.entries(device.tags).map(([key, value]) => (
                        <Badge key={key} color="blue">{key}: {value}</Badge>
                      ))}
                    </SpaceBetween>
                  </Container>
                )}

                {device.attributes && Object.keys(device.attributes).length > 0 && (
                  <Container header={<Header variant="h2">Attributes</Header>}>
                    <KeyValuePairs
                      columns={3}
                      items={Object.entries(device.attributes).map(([key, value]) => ({
                        label: key,
                        value: value || '-',
                      }))}
                    />
                  </Container>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: 'components',
            label: `Components (${device.installed_components?.length || 0})`,
            content: (
              <Container header={<Header variant="h2">Installed Components</Header>}>
                <Table
                  items={device.installed_components || []}
                  columnDefinitions={[
                    {
                      id: 'name',
                      header: 'Component Name',
                      cell: (item: InstalledComponent) => item.componentName,
                      sortingField: 'componentName',
                    },
                    {
                      id: 'version',
                      header: 'Version',
                      cell: (item: InstalledComponent) => item.componentVersion,
                    },
                    {
                      id: 'status',
                      header: 'Lifecycle State',
                      cell: (item: InstalledComponent) => getComponentStatusIndicator(item.lifecycleState),
                    },
                    {
                      id: 'isRoot',
                      header: 'Root',
                      cell: (item: InstalledComponent) => item.isRoot ? 'Yes' : 'No',
                    },
                    {
                      id: 'lastReported',
                      header: 'Last Reported',
                      cell: (item: InstalledComponent) => formatTimestamp(item.lastReportedTimestamp),
                    },
                  ]}
                  sortingDisabled={false}
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
            id: 'deployments',
            label: `Deployments (${device.deployments?.length || 0})`,
            content: (
              <Container header={<Header variant="h2">Effective Deployments</Header>}>
                <Table
                  items={device.deployments || []}
                  columnDefinitions={[
                    {
                      id: 'deploymentId',
                      header: 'Deployment ID',
                      cell: (item: DeviceDeployment) => item.deploymentId,
                    },
                    {
                      id: 'deploymentName',
                      header: 'Name',
                      cell: (item: DeviceDeployment) => item.deploymentName || '-',
                    },
                    {
                      id: 'status',
                      header: 'Status',
                      cell: (item: DeviceDeployment) => getDeploymentStatusIndicator(item.coreDeviceExecutionStatus),
                    },
                    {
                      id: 'reason',
                      header: 'Reason',
                      cell: (item: DeviceDeployment) => item.reason || '-',
                    },
                    {
                      id: 'created',
                      header: 'Created',
                      cell: (item: DeviceDeployment) => formatTimestamp(item.creationTimestamp),
                    },
                    {
                      id: 'modified',
                      header: 'Modified',
                      cell: (item: DeviceDeployment) => formatTimestamp(item.modifiedTimestamp),
                    },
                  ]}
                  empty={
                    <Box textAlign="center" color="inherit">
                      No deployments targeting this device
                    </Box>
                  }
                />
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
                  // TODO: Implement restart via IoT Jobs
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
          <Alert type="info">
            Note: Remote restart functionality requires an IoT Job to be sent to the device.
            This feature is not yet fully implemented.
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
