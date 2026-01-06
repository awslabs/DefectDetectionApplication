import { useState, useEffect } from 'react';
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
  Badge,
} from '@cloudscape-design/components';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import ConfirmationModal from '../components/ConfirmationModal';

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

  const usecaseId = searchParams.get('usecase_id');

  useEffect(() => {
    if (deploymentId && usecaseId) {
      loadDeployment();
    } else if (!usecaseId) {
      setError('Use case ID is required');
      setLoading(false);
    }
  }, [deploymentId, usecaseId]);

  const loadDeployment = async () => {
    if (!deploymentId || !usecaseId) return;
    
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.getDeployment(deploymentId, usecaseId);
      setDeployment(response.deployment);
    } catch (err: any) {
      console.error('Failed to load deployment:', err);
      setError(err.message || 'Failed to load deployment');
    } finally {
      setLoading(false);
    }
  };

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

  const formatTimestamp = (timestamp?: string) => {
    if (!timestamp) return '-';
    return new Date(timestamp).toLocaleString();
  };

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
        <Container
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => navigate(`/deployments?usecase_id=${usecaseId}`)}>
                    Back to List
                  </Button>
                  <Button iconName="refresh" onClick={loadDeployment}>
                    Refresh
                  </Button>
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
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Status</Box>
              <div>{getStatusIndicator(deployment.deployment_status)}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Target</Box>
              <div>{getTargetName(deployment.target_arn)}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Components</Box>
              <div>{deployment.components?.length || 0}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Latest for Target</Box>
              <div>{deployment.is_latest_for_target ? 'Yes' : 'No'}</div>
            </div>
          </ColumnLayout>
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

                    {deployment.tags && Object.keys(deployment.tags).length > 0 && (
                      <>
                        <Box variant="h3">Tags</Box>
                        <SpaceBetween direction="horizontal" size="xs">
                          {Object.entries(deployment.tags).map(([key, value]) => (
                            <Badge key={key} color="blue">{key}: {value}</Badge>
                          ))}
                        </SpaceBetween>
                      </>
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
