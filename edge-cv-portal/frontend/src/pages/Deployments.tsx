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
import { useUsecase } from '../contexts/UsecaseContext';
import { useTableSort } from '../hooks/useTableSort';

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

/**
 * The subset of a deployment the Created_Date sort needs. `creation_timestamp`
 * is intentionally widened to allow null/undefined so the comparator can place
 * missing dates deterministically (Req 6.3), independent of the stricter
 * DeploymentItem shape.
 */
type SortableByCreated = {
  deployment_id: string;
  creation_timestamp?: string | null;
};

/**
 * Parse a deployment's Created_Date to a comparable epoch value, or null when
 * it is absent/unparseable. Kept separate so the sort placement of missing
 * dates is explicit (Req 6.3).
 */
function parseCreatedTimestamp(item: SortableByCreated): number | null {
  const raw = item.creation_timestamp;
  if (raw == null) return null;
  const parsed = Date.parse(String(raw));
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Ascending comparator for the "Created" column.
 *
 * - Orders by parsed `creation_timestamp` ascending.
 * - Deterministic secondary tie-break on `deployment_id` so equal timestamps
 *   keep a stable order across loads (Req 6.2).
 * - Places unparseable/absent timestamps FIRST in ascending order, so that
 *   under the hook's descending `reverse()` they end up LAST — never dropped
 *   (Req 6.3).
 *
 * The Deployments list defaults to descending on this column (newest-first,
 * Req 6.1); `useTableSort` reverses this ascending order for that default.
 */
export const createdSortingComparator = (
  a: SortableByCreated,
  b: SortableByCreated
): number => {
  const at = parseCreatedTimestamp(a);
  const bt = parseCreatedTimestamp(b);

  // Missing dates sort first ascending -> last after the descending reverse.
  if (at === null && bt === null) {
    return a.deployment_id.localeCompare(b.deployment_id);
  }
  if (at === null) return -1;
  if (bt === null) return 1;

  if (at !== bt) return at - bt;
  // Equal timestamps: deterministic tie-break.
  return a.deployment_id.localeCompare(b.deployment_id);
};

/**
 * Default sort applied by the Deployments list: newest-first by Created_Date
 * (Req 6.1). References the same `creation_timestamp` field AND the custom
 * comparator so that (a) `useTableSort` actually uses the comparator for the
 * default sort, and (b) Cloudscape highlights the "Created" column header as
 * the active sort. A user header click overrides this default (Req 6.4).
 */
export const deploymentsSortingDefaults = {
  sortingColumn: {
    sortingField: 'creation_timestamp',
    sortingComparator: createdSortingComparator,
  },
  sortingDescending: true,
};

export default function Deployments() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
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

  const getTargetName = (targetArn: string) => {
    if (!targetArn) return '-';
    const parts = targetArn.split('/');
    return parts[parts.length - 1] || targetArn;
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
  const { items: sortedDeployments, sortingProps } = useTableSort(
    filteredDeployments,
    deploymentsSortingDefaults
  );
  const paginatedDeployments = sortedDeployments.slice(
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
        resizableColumns
        header={
          <Header
            variant="h1"
            description="Manage Greengrass deployments to edge devices"
            counter={`(${filteredDeployments.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
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
        {...sortingProps}
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
            sortingComparator: (a, b) =>
              getTargetName(a.target_arn).localeCompare(getTargetName(b.target_arn)),
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
            sortingComparator: (a, b) =>
              Number(a.is_latest_for_target) - Number(b.is_latest_for_target),
          },
          {
            id: 'created',
            header: 'Created',
            cell: (item) => formatTimestamp(item.creation_timestamp),
            sortingField: 'creation_timestamp',
            sortingComparator: createdSortingComparator,
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
