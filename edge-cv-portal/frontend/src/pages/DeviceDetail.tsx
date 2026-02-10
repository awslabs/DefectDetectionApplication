import { useState, useEffect, useCallback } from 'react';
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
  Select,
  Input,
  Spinner,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Device, InstalledComponent, DeviceDeployment } from '../types';
import LogsDiagnosticsTab from '../components/LogsDiagnosticsTab';

interface LogGroup {
  log_group_name: string;
  component_type: 'system' | 'user';
  component_name: string;
  creation_time?: number;
  stored_bytes: number;
  retention_days?: number;
  has_logs?: boolean;
  note?: string;
}

interface LogEntry {
  timestamp: number;
  message: string;
  log_stream_name: string;
  ingestion_time?: number;
}

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

  // Logs state
  const [logGroups, setLogGroups] = useState<LogGroup[]>([]);
  const [selectedComponent, setSelectedComponent] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [logNextToken, setLogNextToken] = useState<string | undefined>();
  const [filterPattern, setFilterPattern] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [timeRange, setTimeRange] = useState<{ startTime: number; endTime: number }>({
    startTime: Date.now() - 60 * 60 * 1000, // 1 hour ago
    endTime: Date.now(),
  });

  useEffect(() => {
    if (deviceId && usecaseId) {
      loadDevice();
    } else if (!usecaseId) {
      setError('Use case ID is required');
      setLoading(false);
    }
  }, [deviceId, usecaseId]);

  // Load log groups when switching to logs tab
  useEffect(() => {
    if (activeTabId === 'logs' && deviceId && usecaseId && logGroups.length === 0) {
      loadLogGroups();
    }
  }, [activeTabId, deviceId, usecaseId]);

  // Auto-refresh logs
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null;
    if (autoRefresh && selectedComponent) {
      interval = setInterval(() => {
        loadLogs(selectedComponent, false);
      }, 10000); // Refresh every 10 seconds
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoRefresh, selectedComponent, timeRange, filterPattern]);

  const loadLogGroups = async () => {
    if (!deviceId || !usecaseId) return;
    
    try {
      const response = await apiService.getDeviceLogGroups(deviceId, usecaseId);
      setLogGroups(response.log_groups);
      
      // Auto-select first component if available
      if (response.log_groups.length > 0 && !selectedComponent) {
        setSelectedComponent(response.log_groups[0].component_name);
      }
    } catch (err: any) {
      console.error('Failed to load log groups:', err);
      setLogsError(err.message || 'Failed to load log groups');
    }
  };

  const loadLogs = useCallback(async (componentName: string, showLoading = true) => {
    if (!deviceId || !usecaseId) return;
    
    try {
      if (showLoading) setLogsLoading(true);
      setLogsError(null);
      
      const response = await apiService.getDeviceLogs(
        deviceId,
        componentName,
        usecaseId,
        {
          start_time: timeRange.startTime,
          end_time: timeRange.endTime,
          limit: 200,
          filter_pattern: filterPattern || undefined,
        }
      );
      
      setLogs(response.logs);
      setLogNextToken(response.next_token);
    } catch (err: any) {
      console.error('Failed to load logs:', err);
      setLogsError(err.message || 'Failed to load logs');
    } finally {
      setLogsLoading(false);
    }
  }, [deviceId, usecaseId, timeRange, filterPattern]);

  // Load logs when component selection changes
  useEffect(() => {
    if (selectedComponent && activeTabId === 'logs') {
      loadLogs(selectedComponent);
    }
  }, [selectedComponent, activeTabId]);

  const loadMoreLogs = async () => {
    if (!deviceId || !usecaseId || !selectedComponent || !logNextToken) return;
    
    try {
      setLogsLoading(true);
      
      const response = await apiService.getDeviceLogs(
        deviceId,
        selectedComponent,
        usecaseId,
        {
          start_time: timeRange.startTime,
          end_time: timeRange.endTime,
          limit: 200,
          next_token: logNextToken,
          filter_pattern: filterPattern || undefined,
        }
      );
      
      setLogs(prev => [...prev, ...response.logs]);
      setLogNextToken(response.next_token);
    } catch (err: any) {
      console.error('Failed to load more logs:', err);
      setLogsError(err.message || 'Failed to load more logs');
    } finally {
      setLogsLoading(false);
    }
  };

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
                      cell: (item: DeviceDeployment) => (
                        <Button
                          variant="link"
                          onClick={() => navigate(`/deployments/${item.deploymentId}?usecase_id=${usecaseId}`)}
                        >
                          {item.deploymentId}
                        </Button>
                      ),
                    },
                    {
                      id: 'deploymentName',
                      header: 'Name',
                      cell: (item: DeviceDeployment) => (
                        <Button
                          variant="link"
                          onClick={() => navigate(`/deployments/${item.deploymentId}?usecase_id=${usecaseId}`)}
                        >
                          {item.deploymentName || '-'}
                        </Button>
                      ),
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
          {
            id: 'logs',
            label: 'Logs',
            content: (
              <SpaceBetween size="l">
                {/* Info Alert if no logs available */}
                {logGroups.length > 0 && logGroups.every(lg => lg.has_logs === false) && (
                  <Alert type="info" dismissible>
                    <SpaceBetween size="xs">
                      <Box variant="strong">No logs available yet</Box>
                      <Box>
                        Components have been detected on this device, but CloudWatch logs haven't been created yet. This typically happens when:
                      </Box>
                      <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                        <li>CloudWatch logging is not enabled in the deployment</li>
                        <li>Components haven't generated any output yet</li>
                        <li>Log groups are still being created (wait a few moments and refresh)</li>
                      </ul>
                      <Box>
                        <strong>To enable logging:</strong> Ensure the LogManager component is configured in your deployment to send logs to CloudWatch.
                      </Box>
                    </SpaceBetween>
                  </Alert>
                )}

                {/* Log Controls */}
                <Container
                  header={
                    <Header
                      variant="h2"
                      actions={
                        <SpaceBetween direction="horizontal" size="xs">
                          <Button
                            iconName={autoRefresh ? 'status-in-progress' : 'refresh'}
                            onClick={() => setAutoRefresh(!autoRefresh)}
                            variant={autoRefresh ? 'primary' : 'normal'}
                          >
                            {autoRefresh ? 'Auto-refresh On' : 'Auto-refresh'}
                          </Button>
                          <Button
                            iconName="refresh"
                            onClick={() => selectedComponent && loadLogs(selectedComponent)}
                            disabled={!selectedComponent || logsLoading}
                          >
                            Refresh
                          </Button>
                        </SpaceBetween>
                      }
                    >
                      Component Logs
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <ColumnLayout columns={3}>
                      <div>
                        <Box variant="awsui-key-label">Component</Box>
                        <Select
                          selectedOption={
                            selectedComponent
                              ? { label: selectedComponent, value: selectedComponent }
                              : null
                          }
                          onChange={({ detail }) => {
                            setSelectedComponent(detail.selectedOption.value || null);
                            setLogs([]);
                            setLogNextToken(undefined);
                          }}
                          options={logGroups.map((lg) => ({
                            label: `${lg.component_name} (${lg.component_type})${lg.has_logs === false ? ' - No logs yet' : ''}`,
                            value: lg.component_name,
                            description: lg.note || lg.log_group_name,
                          }))}
                          placeholder="Select a component"
                          empty={logGroups.length === 0 ? 'No log groups found. CloudWatch logging may not be configured on this device.' : undefined}
                        />
                        {logGroups.length > 0 && logGroups.every(lg => lg.has_logs === false) && (
                          <Box variant="small" color="text-body-secondary" margin={{ top: 'xs' }}>
                            ℹ️ Components detected but no logs yet. Logs appear after components generate output. Check that CloudWatch logging is enabled in your deployment.
                          </Box>
                        )}
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Time Range</Box>
                        <Select
                          selectedOption={{ label: 'Last 1 hour', value: '1h' }}
                          onChange={({ detail }) => {
                            const now = Date.now();
                            let startTime = now;
                            switch (detail.selectedOption.value) {
                              case '15m':
                                startTime = now - 15 * 60 * 1000;
                                break;
                              case '1h':
                                startTime = now - 60 * 60 * 1000;
                                break;
                              case '3h':
                                startTime = now - 3 * 60 * 60 * 1000;
                                break;
                              case '12h':
                                startTime = now - 12 * 60 * 60 * 1000;
                                break;
                              case '24h':
                                startTime = now - 24 * 60 * 60 * 1000;
                                break;
                              case '7d':
                                startTime = now - 7 * 24 * 60 * 60 * 1000;
                                break;
                            }
                            setTimeRange({ startTime, endTime: now });
                            if (selectedComponent) {
                              setLogs([]);
                              setLogNextToken(undefined);
                            }
                          }}
                          options={[
                            { label: 'Last 15 minutes', value: '15m' },
                            { label: 'Last 1 hour', value: '1h' },
                            { label: 'Last 3 hours', value: '3h' },
                            { label: 'Last 12 hours', value: '12h' },
                            { label: 'Last 24 hours', value: '24h' },
                            { label: 'Last 7 days', value: '7d' },
                          ]}
                        />
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Filter Pattern</Box>
                        <Input
                          value={filterPattern}
                          onChange={({ detail }) => setFilterPattern(detail.value)}
                          placeholder="e.g., ERROR, WARNING"
                          onKeyDown={(e) => {
                            if (e.detail.key === 'Enter' && selectedComponent) {
                              setLogs([]);
                              setLogNextToken(undefined);
                              loadLogs(selectedComponent);
                            }
                          }}
                        />
                      </div>
                    </ColumnLayout>
                  </SpaceBetween>
                </Container>

                {/* Log Output */}
                {logsError && (
                  <Alert type="error" dismissible onDismiss={() => setLogsError(null)}>
                    {logsError}
                  </Alert>
                )}

                {!selectedComponent ? (
                  <Container>
                    <Box textAlign="center" padding="xxl" color="text-body-secondary">
                      Select a component to view logs
                    </Box>
                  </Container>
                ) : logsLoading && logs.length === 0 ? (
                  <Container>
                    <Box textAlign="center" padding="xxl">
                      <Spinner size="large" />
                      <Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>
                        Loading logs...
                      </Box>
                    </Box>
                  </Container>
                ) : (
                  <Container
                    header={
                      <Header
                        variant="h3"
                        counter={`(${logs.length} events)`}
                      >
                        Log Events
                      </Header>
                    }
                  >
                    {logs.length === 0 ? (
                      <Box textAlign="center" padding="l" color="text-body-secondary">
                        No logs found for the selected time range
                      </Box>
                    ) : (
                      <SpaceBetween size="xs">
                        <div
                          style={{
                            maxHeight: '500px',
                            overflow: 'auto',
                            backgroundColor: '#1a1a2e',
                            borderRadius: '4px',
                            padding: '12px',
                            fontFamily: 'Monaco, Consolas, "Courier New", monospace',
                            fontSize: '12px',
                          }}
                        >
                          {logs.map((log, index) => (
                            <div
                              key={`${log.timestamp}-${index}`}
                              style={{
                                padding: '4px 0',
                                borderBottom: '1px solid #2a2a4e',
                                color: log.message.includes('ERROR')
                                  ? '#ff6b6b'
                                  : log.message.includes('WARN')
                                  ? '#ffd93d'
                                  : '#e0e0e0',
                              }}
                            >
                              <span style={{ color: '#6c757d', marginRight: '12px' }}>
                                {new Date(log.timestamp).toISOString()}
                              </span>
                              <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                                {log.message}
                              </span>
                            </div>
                          ))}
                        </div>
                        {logNextToken && (
                          <Box textAlign="center">
                            <Button
                              onClick={loadMoreLogs}
                              loading={logsLoading}
                            >
                              Load More
                            </Button>
                          </Box>
                        )}
                      </SpaceBetween>
                    )}
                  </Container>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: 'diagnostics',
            label: 'AI Diagnostics',
            content: (
              <LogsDiagnosticsTab deviceId={device.device_id} usecaseId={usecaseId || ''} />
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
