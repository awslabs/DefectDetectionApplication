import { useState, useEffect } from 'react';
import {
  Header,
  Table,
  Button,
  Box,
  StatusIndicator,
  Link,
  SpaceBetween,
  Select,
  SelectProps,
  Alert,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import { UseCase } from '../types';

interface DeploymentItem {
  deployment_id: string;
  deployment_name: string;
  target_arn: string;
  revision_id: string;
  deployment_status: string;
  is_latest_for_target: boolean;
  creation_timestamp: string;
  usecase_id: string;
}

export default function Deployments() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [deployments, setDeployments] = useState<DeploymentItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedItems, setSelectedItems] = useState<DeploymentItem[]>([]);
  
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
        
        // Check for URL parameter first
        const urlUseCaseId = searchParams.get('usecase_id');
        if (urlUseCaseId) {
          const preSelectedUseCase = useCaseList.find((uc: UseCase) => uc.usecase_id === urlUseCaseId);
          if (preSelectedUseCase) {
            setSelectedUseCase({
              label: preSelectedUseCase.name,
              value: preSelectedUseCase.usecase_id,
            });
            return;
          }
        }
        
        // Auto-select first use case if available
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
  }, [searchParams]);

  // Load deployments when use case changes
  useEffect(() => {
    if (selectedUseCase?.value) {
      loadDeployments();
    } else {
      setDeployments([]);
    }
  }, [selectedUseCase]);

  const loadDeployments = async () => {
    if (!selectedUseCase?.value) return;
    
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.listDeployments(selectedUseCase.value);
      setDeployments(response.deployments || []);
    } catch (err: any) {
      console.error('Failed to load deployments:', err);
      setError(err.message || 'Failed to load deployments');
      setDeployments([]);
    } finally {
      setLoading(false);
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

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      
      <Table
        header={
          <Header
            variant="h1"
            description="Manage Greengrass deployments to edge devices"
            counter={`(${deployments.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
                  placeholder="Select use case"
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                  }))}
                />
                <Button
                  iconName="refresh"
                  onClick={loadDeployments}
                  loading={loading}
                  disabled={!selectedUseCase}
                >
                  Refresh
                </Button>
                <Button 
                  variant="primary" 
                  onClick={() => navigate(`/deployments/create?usecase_id=${selectedUseCase?.value || ''}`)}
                  disabled={!selectedUseCase}
                >
                  Create Deployment
                </Button>
              </SpaceBetween>
            }
          >
            Deployments
          </Header>
        }
        loading={loading}
        items={deployments}
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        columnDefinitions={[
          {
            id: 'deployment_id',
            header: 'Deployment ID',
            cell: (item) => (
              <Link onFollow={() => navigate(`/deployments/${item.deployment_id}?usecase_id=${selectedUseCase?.value}`)}>
                {item.deployment_id.substring(0, 12)}...
              </Link>
            ),
            sortingField: 'deployment_id',
          },
          {
            id: 'name',
            header: 'Name',
            cell: (item) => item.deployment_name || '-',
            sortingField: 'deployment_name',
          },
          {
            id: 'target',
            header: 'Target',
            cell: (item) => getTargetName(item.target_arn),
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item) => getStatusIndicator(item.deployment_status),
            sortingField: 'deployment_status',
          },
          {
            id: 'latest',
            header: 'Latest',
            cell: (item) => item.is_latest_for_target ? 'Yes' : 'No',
          },
          {
            id: 'created',
            header: 'Created',
            cell: (item) => formatTimestamp(item.creation_timestamp),
            sortingField: 'creation_timestamp',
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No deployments</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase 
                ? 'No deployments found for this use case.'
                : 'Select a use case to view deployments.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => navigate(`/deployments/create?usecase_id=${selectedUseCase.value}`)}>
                Create Deployment
              </Button>
            )}
          </Box>
        }
        sortingDisabled={false}
      />
    </SpaceBetween>
  );
}
