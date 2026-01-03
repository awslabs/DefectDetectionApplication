import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Table,
  Button,
  Box,
  StatusIndicator,
  Link,
  Badge,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { Deployment } from '../types';

export default function Deployments() {
  const navigate = useNavigate();
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState<Deployment[]>([]);

  useEffect(() => {
    loadDeployments();
  }, []);

  const loadDeployments = async () => {
    setLoading(true);
    try {
      // Mock data for now
      const mockDeployments: Deployment[] = [
        {
          deployment_id: 'deploy-001',
          usecase_id: 'usecase-001',
          component_arn: 'arn:aws:greengrass:us-east-1:123456789012:components:DefectDetectionModel:versions:1.0.0',
          component_version: '1.0.0',
          target_devices: ['device-001', 'device-002', 'device-003'],
          target_groups: [],
          rollout_strategy: 'all-at-once',
          status: 'completed',
          device_statuses: {
            'device-001': 'SUCCEEDED',
            'device-002': 'SUCCEEDED',
            'device-003': 'SUCCEEDED',
          },
          greengrass_deployment_id: 'gg-deploy-001',
          created_by: 'user@example.com',
          created_at: Date.now() - 86400000,
          completed_at: Date.now() - 86400000 + 3600000,
        },
        {
          deployment_id: 'deploy-002',
          usecase_id: 'usecase-001',
          component_arn: 'arn:aws:greengrass:us-east-1:123456789012:components:DefectDetectionModel:versions:1.1.0',
          component_version: '1.1.0',
          target_devices: ['device-001', 'device-002'],
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
          },
          greengrass_deployment_id: 'gg-deploy-002',
          created_by: 'user@example.com',
          created_at: Date.now() - 3600000,
        },
      ];
      setDeployments(mockDeployments);
    } catch (error) {
      console.error('Failed to load deployments:', error);
    } finally {
      setLoading(false);
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

  const getComponentName = (arn: string) => {
    const parts = arn.split(':');
    return parts[parts.length - 3] || arn;
  };

  return (
    <Container
      header={
        <Header
          variant="h1"
          actions={
            <Button variant="primary" onClick={() => navigate('/deployments/create')}>
              Create Deployment
            </Button>
          }
        >
          Deployments
        </Header>
      }
    >
      <Table
        columnDefinitions={[
          {
            id: 'deployment_id',
            header: 'Deployment ID',
            cell: (item) => (
              <Link onFollow={() => navigate(`/deployments/${item.deployment_id}`)}>
                {item.deployment_id}
              </Link>
            ),
            sortingField: 'deployment_id',
          },
          {
            id: 'component',
            header: 'Component',
            cell: (item) => (
              <Box>
                <Box fontWeight="bold">{getComponentName(item.component_arn)}</Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  v{item.component_version}
                </Box>
              </Box>
            ),
          },
          {
            id: 'strategy',
            header: 'Rollout Strategy',
            cell: (item) => (
              <Badge color="blue">
                {item.rollout_strategy.replace('-', ' ')}
              </Badge>
            ),
          },
          {
            id: 'targets',
            header: 'Target Devices',
            cell: (item) => `${item.target_devices.length} devices`,
          },
          {
            id: 'progress',
            header: 'Progress',
            cell: (item) => {
              const total = item.target_devices.length;
              const succeeded = Object.values(item.device_statuses).filter(
                (s) => s === 'SUCCEEDED'
              ).length;
              return `${succeeded}/${total}`;
            },
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item) => getStatusIndicator(item.status),
          },
          {
            id: 'created_at',
            header: 'Created',
            cell: (item) => new Date(item.created_at).toLocaleString(),
            sortingField: 'created_at',
          },
        ]}
        items={deployments}
        loading={loading}
        loadingText="Loading deployments"
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          setSelectedItems(detail.selectedItems)
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No deployments</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No deployments found for this use case.
            </Box>
            <Button onClick={() => navigate('/deployments/create')}>
              Create Deployment
            </Button>
          </Box>
        }
        sortingDisabled={false}
      />
    </Container>
  );
}
