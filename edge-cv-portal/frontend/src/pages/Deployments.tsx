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
  Pagination,
  TextFilter,
  ColumnLayout,
  Container,
  Icon,
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

const PAGE_SIZE = 10;

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
  
  // Filtering and pagination
  const [filterText, setFilterText] = useState('');
  const [statusFilter, setStatusFilter] = useState<SelectProps.Option | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

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
      setCurrentPage(1);
    } catch (err: any) {
      console.error('Failed to load deployments:', err);
      setError(err.message || 'Failed to load deployments');
      setDeployments([]);
    } finally {
      setLoading(false);
    }
  };

  // Filter deployments
  const filteredDeployments = deployments.filter(dep => {
    const matchesText = !filterText || 
      dep.deployment_name?.toLowerCase().includes(filterText.toLowerCase()) ||
      dep.deployment_id.toLowerCase().includes(filterText.toLowerCase()) ||
      getTargetName(dep.target_arn).toLowerCase().includes(filterText.toLowerCase());
    
    const matchesStatus = !statusFilter?.value || 
      dep.deployment_status.toLowerCase() === statusFilter.value.toLowerCase();
    
    return matchesText && matchesStatus;
  });

  // Paginate
  const paginatedDeployments = filteredDeployments.slice(
    (currentPage - 1) * PAGE_SIZE,
    currentPage * PAGE_SIZE
  );

  // Calculate stats
  const stats = {
    total: deployments.length,
    active: deployments.filter(d => d.deployment_status === 'ACTIVE').length,
    completed: deployments.filter(d => d.deployment_status === 'COMPLETED').length,
    failed: deployments.filter(d => d.deployment_status === 'FAILED').length,
  };

  const getStatusIndicator = (status: string) => {
    const statusLower = status?.toLowerCase() || 'unknown';
    switch (statusLower) {
      case 'active':
        return <StatusIndicator type="in-progress">{status}</StatusIndicator>;
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

  const statusOptions: SelectProps.Option[] = [
    { label: 'All Statuses', value: '' },
    { label: 'Active', value: 'ACTIVE' },
    { label: 'Completed', value: 'COMPLETED' },
    { label: 'Failed', value: 'FAILED' },
    { label: 'Canceled', value: 'CANCELED' },
    { label: 'Inactive', value: 'INACTIVE' },
  ];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Stats Cards */}
      {selectedUseCase && deployments.length > 0 && (
        <ColumnLayout columns={4} variant="text-grid">
          <Container>
            <Box variant="awsui-key-label">Total Deployments</Box>
            <Box variant="h2">{stats.total}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Active</Box>
            <Box variant="h2" color="text-status-info">{stats.active}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Completed</Box>
            <Box variant="h2" color="text-status-success">{stats.completed}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Failed</Box>
            <Box variant="h2" color="text-status-error">{stats.failed}</Box>
          </Container>
        </ColumnLayout>
      )}
      
      <Table
        header={
          <Header
            variant="h1"
            description="Manage Greengrass deployments to edge devices"
            counter={`(${filteredDeployments.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
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
        filter={
          <SpaceBetween direction="horizontal" size="xs">
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Search by name, ID, or target"
              onChange={({ detail }) => {
                setFilterText(detail.filteringText);
                setCurrentPage(1);
              }}
            />
            <Select
              selectedOption={statusFilter}
              onChange={({ detail }) => {
                setStatusFilter(detail.selectedOption);
                setCurrentPage(1);
              }}
              options={statusOptions}
              placeholder="Filter by status"
            />
          </SpaceBetween>
        }
        pagination={
          <Pagination
            currentPageIndex={currentPage}
            pagesCount={Math.ceil(filteredDeployments.length / PAGE_SIZE)}
            onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
          />
        }
        loading={loading}
        items={paginatedDeployments}
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
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xxs">
                <Icon name={getTargetType(item.target_arn) === 'group' ? 'group' : 'status-positive'} />
                <span>{getTargetName(item.target_arn)}</span>
              </SpaceBetween>
            ),
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
            cell: (item) => item.is_latest_for_target ? 
              <StatusIndicator type="success">Yes</StatusIndicator> : 
              <Box color="text-body-secondary">No</Box>,
          },
          {
            id: 'created',
            header: 'Created',
            cell: (item) => formatTimestamp(item.creation_timestamp),
            sortingField: 'creation_timestamp',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xxs">
                <Button
                  variant="inline-icon"
                  iconName="external"
                  onClick={() => navigate(`/deployments/${item.deployment_id}?usecase_id=${selectedUseCase?.value}`)}
                  ariaLabel="View details"
                />
                {getTargetType(item.target_arn) === 'device' && (
                  <Button
                    variant="inline-icon"
                    iconName="status-positive"
                    onClick={() => navigate(`/devices/${getTargetName(item.target_arn)}?usecase_id=${selectedUseCase?.value}`)}
                    ariaLabel="View device"
                  />
                )}
              </SpaceBetween>
            ),
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
