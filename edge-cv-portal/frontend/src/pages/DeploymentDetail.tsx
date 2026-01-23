import { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  ColumnLayout,
  Box,
  StatusIndicator,
  Button,
  Tabs,
  KeyValuePairs,
  Table,
  Alert,
  Toggle,
  ProgressBar,
  Link,
  BreadcrumbGroup,
  Icon,
} from '@cloudscape-design/components';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import ConfirmationModal from '../components/ConfirmationModal';

interface EffectiveDeployment {
  core_device: string;
  deployment_status: string;
  reason: string;
  description: string;
  status_details: Record<string, unknown>;
  modified_timestamp: string;
}

interface ErrorMessage {
  device: string;
  status: string;
  reason: string;
  description: string;
  detailed_status?: string;
  detailed_status_reason?: string;
}

interface DeploymentDetail {
  deployment_id: string;
  deployment_name: string;
  target_arn: string;
  revision_id: string;
  deployment_status: string;
  iot_job_id: string;
  iot_job_arn: string;
  is_latest_for_target: boolean;
  creation_timestamp: string;
  components: Array<{
    component_name: string;
    component_version: string;
    configuration_update: Record<string, unknown>;
  }>;
  deployment_policies: Record<string, unknown>;
  tags: Record<string, string>;
  usecase_id: string;
  effective_deployments?: EffectiveDeployment[];
  error_messages?: ErrorMessage[];
}

export default function DeploymentDetail() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [deployment, setDeployment] = useState<DeploymentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);

  const usecaseId = searchParams.get('usecase_id');

  const loadDeployment = useCallback(async () => {
    if (!deploymentId || !usecaseId) return;
    
    try {
      const response = await apiService.getDeployment(deploymentId, usecaseId);
      setDeployment(response.deployment);
      setError(null);
      
      // Auto-disable refresh when deployment is complete
      if (response.deployment.deployment_status !== 'ACTIVE') {
        setAutoRefresh(false);
      }
    } catch (err: any) {
      console.error('Failed to load deployment:', err);
      setError(err.message || 'Failed to load deployment');
    } finally {
      setLoading(false);
    }
  }, [deploymentId, usecaseId]);

  useEffect(() => {
    if (deploymentId && usecaseId) {
      loadDeployment();
    } else if (!usecaseId) {
      setError('Use case ID is required');
      setLoading(false);
    }
  }, [deploymentId, usecaseId, loadDeployment]);

  // Auto-refresh for active deployments
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null;
    if (autoRefresh && deployment?.deployment_status === 'ACTIVE') {
      interval = setInterval(() => {
        loadDeployment();
      }, 5000); // Refresh every 5 seconds
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoRefresh, deployment?.deployment_status, loadDeployment]);

  const handleCancel = async () => {
    if (!deploymentId || !usecaseId) return;
    
    setCancelling(true);
    try {
      await apiService.cancelDeployment(deploymentId, usecaseId);
      setShowCancelModal(false);
      navigate(`/deployments?usecase_id=${usecaseId}`);
    } catch (err: any) {
      console.error('Failed to cancel deployment:', err);
      setError(err.message || 'Failed to cancel deployment');
    } finally {
      setCancelling(false);
    }
  };

  const getStatusIndicator = (status: string) => {
    const statusLower = status?.toLowerCase() || 'unknown';
    switch (statusLower) {
      case 'active':
      case 'completed':
        return <StatusIndicator type="success">{status}</StatusIndicator>;
      case 'failed':
        return <StatusIndicator type="error">{status}</StatusIndicator>;
      case 'canceled':
        return <StatusIndicator type="stopped">{status}</StatusIndicator>;
      case 'inactive':
        return <StatusIndicator type="info">{status}</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status || 'Unknown'}</StatusIndicator>;
    }
  };

  const getTargetName = (targetArn: string) => {
    if (!targetArn) return '-';
    const parts = targetArn.split('/');
    return parts[parts.length - 1] || targetArn;
  };

  const getTargetType = (targetArn: string) => {
    if (!targetArn) return 'unknown';
    if (targetArn.includes(':thinggroup/')) return 'group';
    if (targetArn.includes(':thing/')) return 'device';
    return 'unknown';
  };

  const formatTimestamp = (timestamp?: string) => {
    if (!timestamp) return '-';
    return new Date(timestamp).toLocaleString();
  };

  // Calculate deployment progress
  const getDeploymentProgress = () => {
    if (!deployment?.effective_deployments?.length) return null;
    
    const total = deployment.effective_deployments.length;
    const succeeded = deployment.effective_deployments.filter(
      d => d.deployment_status === 'SUCCEEDED'
    ).length;
    const failed = deployment.effective_deployments.filter(
      d => ['FAILED', 'REJECTED', 'TIMED_OUT'].includes(d.deployment_status)
    ).length;
    const inProgress = deployment.effective_deployments.filter(
      d => ['IN_PROGRESS', 'QUEUED'].includes(d.deployment_status)
    ).length;
    
    return { total, succeeded, failed, inProgress };
  };

  const progress = getDeploymentProgress();

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading deployment details...
        </Box>
      </Container>
    );
  }

  if (error) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{error}</Alert>
        <Button onClick={() => navigate(`/deployments?usecase_id=${usecaseId}`)}>Back to Deployments</Button>
      </SpaceBetween>
    );
  }

  if (!deployment) {
    return (
      <Container>
        <Alert type="error">Deployment not found</Alert>
        <Button onClick={() => navigate(`/deployments?usecase_id=${usecaseId}`)}>Back to Deployments</Button>
      </Container>
    );
  }

  return (
    <>
      <SpaceBetween size="l">
        {/* Breadcrumb */}
        <BreadcrumbGroup
          items={[
            { text: 'Deployments', href: `/deployments?usecase_id=${usecaseId}` },
            { text: deployment.deployment_name || deployment.deployment_id.substring(0, 12), href: '#' },
          ]}
          onFollow={(e) => {
            e.preventDefault();
            if (e.detail.href !== '#') {
              navigate(e.detail.href);
            }
          }}
        />

        <Container
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  {deployment.deployment_status === 'ACTIVE' && (
                    <Toggle
                      checked={autoRefresh}
                      onChange={({ detail }) => setAutoRefresh(detail.checked)}
                    >
                      Auto-refresh
                    </Toggle>
                  )}
                  <Button iconName="refresh" onClick={loadDeployment} loading={loading}>
                    Refresh
                  </Button>
                  {deployment.deployment_status === 'FAILED' && (
                    <Button
                      onClick={() => navigate(`/deployments/create?usecase_id=${usecaseId}&retry=${deploymentId}`)}
                    >
                      Retry Deployment
                    </Button>
                  )}
                  {deployment.deployment_status === 'ACTIVE' && (
                    <Button onClick={() => setShowCancelModal(true)}>
                      Cancel Deployment
                    </Button>
                  )}
                </SpaceBetween>
              }
            >
              {deployment.deployment_name || `Deployment ${deployment.deployment_id.substring(0, 12)}...`}
            </Header>
          }
        >
          <SpaceBetween size="m">
            <ColumnLayout columns={4} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Status</Box>
                <div>{getStatusIndicator(deployment.deployment_status)}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Target</Box>
                <SpaceBetween direction="horizontal" size="xxs">
                  <Icon name={getTargetType(deployment.target_arn) === 'group' ? 'group' : 'status-positive'} />
                  {getTargetType(deployment.target_arn) === 'device' ? (
                    <Link onFollow={() => navigate(`/devices/${getTargetName(deployment.target_arn)}?usecase_id=${usecaseId}`)}>
                      {getTargetName(deployment.target_arn)}
                    </Link>
                  ) : (
                    <span>{getTargetName(deployment.target_arn)}</span>
                  )}
                </SpaceBetween>
              </div>
              <div>
                <Box variant="awsui-key-label">Components</Box>
                <div>{deployment.components?.length || 0}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Latest for Target</Box>
                <div>{deployment.is_latest_for_target ? 
                  <StatusIndicator type="success">Yes</StatusIndicator> : 
                  <Box color="text-body-secondary">No</Box>
                }</div>
              </div>
            </ColumnLayout>

            {/* Progress bar for multi-device deployments */}
            {progress && progress.total > 1 && (
              <Box>
                <Box variant="awsui-key-label" margin={{ bottom: 'xxs' }}>
                  Deployment Progress ({progress.succeeded + progress.failed}/{progress.total} devices)
                </Box>
                <ProgressBar
                  value={((progress.succeeded + progress.failed) / progress.total) * 100}
                  status={progress.failed > 0 ? 'error' : progress.inProgress > 0 ? 'in-progress' : 'success'}
                  additionalInfo={
                    <SpaceBetween direction="horizontal" size="s">
                      <span style={{ color: 'green' }}>✓ {progress.succeeded} succeeded</span>
                      {progress.failed > 0 && <span style={{ color: 'red' }}>✗ {progress.failed} failed</span>}
                      {progress.inProgress > 0 && <span style={{ color: 'blue' }}>⋯ {progress.inProgress} in progress</span>}
                    </SpaceBetween>
                  }
                />
              </Box>
            )}
          </SpaceBetween>
        </Container>

        <Container>
          <Tabs
            activeTabId={activeTabId}
            onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
            tabs={[
              {
                id: 'overview',
                label: 'Overview',
                content: (
                  <SpaceBetween size="l">
                    {/* Show error alert if deployment failed */}
                    {deployment.error_messages && deployment.error_messages.length > 0 && (
                      <Alert type="error" header="Deployment Failed">
                        <SpaceBetween size="s">
                          {deployment.error_messages.map((err, idx) => (
                            <Box key={idx}>
                              <Box variant="strong">Device: {err.device}</Box>
                              <Box>Status: {err.status}</Box>
                              {err.reason && <Box>Reason: {err.reason}</Box>}
                              {err.description && <Box>Description: {err.description}</Box>}
                              {err.detailed_status && <Box>Detailed Status: {err.detailed_status}</Box>}
                              {err.detailed_status_reason && (
                                <Box color="text-status-error">
                                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'monospace', fontSize: '12px' }}>
                                    {err.detailed_status_reason}
                                  </pre>
                                </Box>
                              )}
                            </Box>
                          ))}
                        </SpaceBetween>
                      </Alert>
                    )}

                    <KeyValuePairs
                      columns={2}
                      items={[
                        { label: 'Deployment ID', value: deployment.deployment_id },
                        { label: 'Deployment Name', value: deployment.deployment_name || '-' },
                        { label: 'Target ARN', value: <Box fontSize="body-s">{deployment.target_arn}</Box> },
                        { label: 'Revision ID', value: deployment.revision_id || '-' },
                        { label: 'IoT Job ID', value: deployment.iot_job_id || '-' },
                        { label: 'Created', value: formatTimestamp(deployment.creation_timestamp) },
                      ]}
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: 'device-status',
                label: `Device Status (${deployment.effective_deployments?.length || 0})`,
                content: (
                  <SpaceBetween size="l">
                    {deployment.effective_deployments && deployment.effective_deployments.length > 0 ? (
                      <Table
                        columnDefinitions={[
                          {
                            id: 'device',
                            header: 'Device',
                            cell: (item) => (
                              <Link onFollow={() => navigate(`/devices/${item.core_device}?usecase_id=${usecaseId}`)}>
                                {item.core_device}
                              </Link>
                            ),
                          },
                          {
                            id: 'status',
                            header: 'Status',
                            cell: (item) => {
                              const status = item.deployment_status?.toLowerCase() || 'unknown';
                              switch (status) {
                                case 'succeeded':
                                  return <StatusIndicator type="success">{item.deployment_status}</StatusIndicator>;
                                case 'failed':
                                case 'rejected':
                                case 'timed_out':
                                  return <StatusIndicator type="error">{item.deployment_status}</StatusIndicator>;
                                case 'in_progress':
                                case 'queued':
                                  return <StatusIndicator type="in-progress">{item.deployment_status}</StatusIndicator>;
                                case 'canceled':
                                  return <StatusIndicator type="stopped">{item.deployment_status}</StatusIndicator>;
                                default:
                                  return <StatusIndicator type="info">{item.deployment_status || 'Unknown'}</StatusIndicator>;
                              }
                            },
                          },
                          {
                            id: 'reason',
                            header: 'Reason',
                            cell: (item) => item.reason || '-',
                          },
                          {
                            id: 'description',
                            header: 'Description',
                            cell: (item) => item.description || '-',
                          },
                          {
                            id: 'modified',
                            header: 'Last Updated',
                            cell: (item) => formatTimestamp(item.modified_timestamp),
                          },
                          {
                            id: 'actions',
                            header: 'Actions',
                            cell: (item) => (
                              <Button
                                variant="inline-link"
                                onClick={() => navigate(`/devices/${item.core_device}?usecase_id=${usecaseId}`)}
                              >
                                View Logs
                              </Button>
                            ),
                          },
                        ]}
                        items={deployment.effective_deployments}
                        empty={
                          <Box textAlign="center" color="inherit">
                            No device status available
                          </Box>
                        }
                      />
                    ) : (
                      <Box textAlign="center" color="inherit">
                        No device status available. The deployment may still be initializing.
                      </Box>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: 'components',
                label: `Components (${deployment.components?.length || 0})`,
                content: (
                  <Table
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Component Name',
                        cell: (item) => item.component_name,
                      },
                      {
                        id: 'version',
                        header: 'Version',
                        cell: (item) => item.component_version || 'latest',
                      },
                      {
                        id: 'config',
                        header: 'Configuration Update',
                        cell: (item) => 
                          Object.keys(item.configuration_update || {}).length > 0 
                            ? 'Yes' 
                            : 'No',
                      },
                    ]}
                    items={deployment.components || []}
                    empty={
                      <Box textAlign="center" color="inherit">
                        No components in this deployment
                      </Box>
                    }
                  />
                ),
              },
              {
                id: 'policies',
                label: 'Deployment Policies',
                content: (
                  <Box>
                    {deployment.deployment_policies && Object.keys(deployment.deployment_policies).length > 0 ? (
                      <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
                        {JSON.stringify(deployment.deployment_policies, null, 2)}
                      </pre>
                    ) : (
                      <Box textAlign="center" color="inherit">
                        No deployment policies configured
                      </Box>
                    )}
                  </Box>
                ),
              },
            ]}
          />
        </Container>
      </SpaceBetween>

      <ConfirmationModal
        visible={showCancelModal}
        title="Cancel Deployment"
        message="This action will cancel the deployment. Devices that have already received the deployment will not be affected."
        confirmButtonText="Cancel Deployment"
        variant="warning"
        loading={cancelling}
        onConfirm={handleCancel}
        onCancel={() => setShowCancelModal(false)}
      >
        <Box>
          Are you sure you want to cancel deployment <strong>{deployment.deployment_name || deployment.deployment_id}</strong>?
        </Box>
      </ConfirmationModal>
    </>
  );
}
