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
} from '@cloudscape-design/components';
import { useParams, useNavigate } from 'react-router-dom';
import { Deployment } from '../types';
import ConfirmationModal from '../components/ConfirmationModal';

export default function DeploymentDetail() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const navigate = useNavigate();
  const [deployment, setDeployment] = useState<Deployment | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showRollbackModal, setShowRollbackModal] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  useEffect(() => {
    loadDeployment();
  }, [deploymentId]);

  const loadDeployment = async () => {
    setLoading(true);
    try {
      // Mock data for now
      const mockDeployment: Deployment = {
        deployment_id: deploymentId || '',
        usecase_id: 'usecase-001',
        component_arn: 'arn:aws:greengrass:us-east-1:123456789012:components:DefectDetectionModel:versions:1.1.0',
        component_version: '1.1.0',
        target_devices: ['device-001', 'device-002', 'device-003'],
        target_groups: [],
        rollout_strategy: 'canary',
        rollout_config: {
          canarySize: 1,
          failureThreshold: 20,
        },
        status: 'in_progress',
        device_statuses: {
          'device-001': 'SUCCEEDED',
          'device-002': 'IN_PROGRESS',
          'device-003': 'PENDING',
        },
        greengrass_deployment_id: 'gg-deploy-002',
        created_by: 'user@example.com',
        created_at: Date.now() - 3600000,
      };
      setDeployment(mockDeployment);
    } catch (error) {
      console.error('Failed to load deployment:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleRollback = async () => {
    setRollingBack(true);
    try {
      // TODO: Implement API call
      await new Promise((resolve) => setTimeout(resolve, 1500));
      console.log('Rolling back deployment:', deploymentId);
      setShowRollbackModal(false);
      navigate('/deployments');
    } catch (error) {
      console.error('Failed to rollback deployment:', error);
    } finally {
      setRollingBack(false);
    }
  };

  const getStatusIndicator = (status: Deployment['status']) => {
    const statusMap = {
      pending: { type: 'pending' as const, label: 'Pending' },
      in_progress: { type: 'in-progress' as const, label: 'In Progress' },
      completed: { type: 'success' as const, label: 'Completed' },
      failed: { type: 'error' as const, label: 'Failed' },
      rolled_back: { type: 'warning' as const, label: 'Rolled Back' },
    };
    const config = statusMap[status];
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  const getDeviceStatusIndicator = (status: string) => {
    const statusMap: Record<string, { type: any; label: string }> = {
      SUCCEEDED: { type: 'success', label: 'Succeeded' },
      IN_PROGRESS: { type: 'in-progress', label: 'In Progress' },
      PENDING: { type: 'pending', label: 'Pending' },
      FAILED: { type: 'error', label: 'Failed' },
    };
    const config = statusMap[status] || { type: 'info', label: status };
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  const getComponentName = (arn: string) => {
    const parts = arn.split(':');
    return parts[parts.length - 3] || arn;
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

  if (!deployment) {
    return (
      <Container>
        <Alert type="error">Deployment not found</Alert>
      </Container>
    );
  }

  const deviceStatusItems = deployment.target_devices.map((deviceId) => ({
    deviceId,
    status: deployment.device_statuses[deviceId] || 'PENDING',
  }));

  return (
    <>
      <SpaceBetween size="l">
        <Container
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => navigate('/deployments')}>
                    Back to List
                  </Button>
                  {(deployment.status === 'in_progress' || deployment.status === 'failed') && (
                    <Button onClick={() => setShowRollbackModal(true)}>
                      Rollback Deployment
                    </Button>
                  )}
                </SpaceBetween>
              }
            >
              Deployment: {deployment.deployment_id}
            </Header>
          }
        >
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Status</Box>
              <div>{getStatusIndicator(deployment.status)}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Component</Box>
              <div>
                {getComponentName(deployment.component_arn)} v{deployment.component_version}
              </div>
            </div>
            <div>
              <Box variant="awsui-key-label">Rollout Strategy</Box>
              <div>{deployment.rollout_strategy.replace('-', ' ')}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Created By</Box>
              <div>{deployment.created_by}</div>
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
                        {
                          label: 'Deployment ID',
                          value: deployment.deployment_id,
                        },
                        {
                          label: 'Greengrass Deployment ID',
                          value: deployment.greengrass_deployment_id,
                        },
                        {
                          label: 'Component ARN',
                          value: (
                            <Box fontSize="body-s">
                              {deployment.component_arn}
                            </Box>
                          ),
                        },
                        {
                          label: 'Target Devices',
                          value: `${deployment.target_devices.length} devices`,
                        },
                        {
                          label: 'Created',
                          value: new Date(deployment.created_at).toLocaleString(),
                        },
                        {
                          label: 'Completed',
                          value: deployment.completed_at
                            ? new Date(deployment.completed_at).toLocaleString()
                            : '-',
                        },
                      ]}
                    />

                    {deployment.rollout_config && (
                      <>
                        <Box variant="h3">Rollout Configuration</Box>
                        <KeyValuePairs
                          columns={2}
                          items={[
                            {
                              label: 'Canary Size',
                              value: deployment.rollout_config.canarySize || '-',
                            },
                            {
                              label: 'Canary Percentage',
                              value: deployment.rollout_config.canaryPercentage
                                ? `${deployment.rollout_config.canaryPercentage}%`
                                : '-',
                            },
                            {
                              label: 'Failure Threshold',
                              value: deployment.rollout_config.failureThreshold
                                ? `${deployment.rollout_config.failureThreshold}%`
                                : '-',
                            },
                          ]}
                        />
                      </>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: 'devices',
                label: 'Device Status',
                content: (
                  <Table
                    columnDefinitions={[
                      {
                        id: 'device_id',
                        header: 'Device ID',
                        cell: (item) => item.deviceId,
                      },
                      {
                        id: 'status',
                        header: 'Deployment Status',
                        cell: (item) => getDeviceStatusIndicator(item.status),
                      },
                    ]}
                    items={deviceStatusItems}
                    empty={
                      <Box textAlign="center" color="inherit">
                        No devices in this deployment
                      </Box>
                    }
                  />
                ),
              },
            ]}
          />
        </Container>
      </SpaceBetween>

      <ConfirmationModal
        visible={showRollbackModal}
        title="Rollback Deployment"
        message="This action will rollback the deployment to the previous version on all target devices."
        confirmButtonText="Rollback"
        variant="warning"
        loading={rollingBack}
        onConfirm={handleRollback}
        onCancel={() => setShowRollbackModal(false)}
      >
        <Box>
          Are you sure you want to rollback deployment <strong>{deployment.deployment_id}</strong>?
        </Box>
      </ConfirmationModal>
    </>
  );
}
