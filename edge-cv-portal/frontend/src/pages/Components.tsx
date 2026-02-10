import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Container,
  Header,
  Table,
  Button,
  SpaceBetween,
  Box,
  StatusIndicator,
  Link,
  Alert,
  Select,
  SelectProps,
  Badge,
  Modal,
  TextFilter,
  Pagination,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Component, UseCase } from '../types';
import { useAuth } from '../contexts/AuthContext';
import { useUsecase } from '../contexts/UsecaseContext';
import ConfirmationModal from '../components/ConfirmationModal';

// Portal-managed component prefix - these should not be deleted by non-admin users
const PORTAL_MANAGED_COMPONENT_PREFIX = 'aws.edgeml.dda.LocalServer';

export default function Components() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  
  const [components, setComponents] = useState<Component[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedItems, setSelectedItems] = useState<Component[]>([]);
  
  // Use case management
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  
  // Filters and pagination
  const [searchText, setSearchText] = useState(searchParams.get('search') || '');
  const [scopeFilter, setScopeFilter] = useState<SelectProps.Option | null>(
    searchParams.get('scope') ? { label: searchParams.get('scope')!, value: searchParams.get('scope')! } : null
  );
  const [sortBy, setSortBy] = useState(searchParams.get('sort_by') || 'component_name');
  const [sortOrder, setSortOrder] = useState(searchParams.get('sort_order') || 'asc');
  const [currentPageIndex, setCurrentPageIndex] = useState(0);
  const [pageSize] = useState(20);
  
  // Modals
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [componentToDelete, setComponentToDelete] = useState<Component | null>(null);
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [componentToDeploy, setComponentToDeploy] = useState<Component | null>(null);

  const scopeOptions: SelectProps.Options = [
    { label: 'All DDA Components', value: '' },
    { label: 'Private Components', value: 'PRIVATE' },
    { label: 'Public Components', value: 'PUBLIC' },
  ];

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        
        // Use saved selection from context, or check URL, or auto-select first
        if (selectedUsecaseId) {
          const saved = useCaseList.find(uc => uc.usecase_id === selectedUsecaseId);
          if (saved) {
            setSelectedUseCase({
              label: saved.name,
              value: saved.usecase_id,
            });
            return;
          }
        }
        
        // Check for pre-selected use case from URL
        const urlUseCaseId = searchParams.get('usecase_id');
        if (urlUseCaseId) {
          const preSelectedUseCase = useCaseList.find(uc => uc.usecase_id === urlUseCaseId);
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
      } catch (error) {
        console.error('Failed to load use cases:', error);
        setError('Failed to load use cases');
      }
    };
    loadUseCases();
  }, [selectedUsecaseId, setSelectedUsecaseId, searchParams]);

  // Load components when use case changes
  useEffect(() => {
    if (selectedUseCase?.value) {
      loadComponents();
    } else {
      setComponents([]);
      setLoading(false);
    }
  }, [selectedUseCase, searchText, scopeFilter, sortBy, sortOrder]);

  const loadComponents = async () => {
    if (!selectedUseCase?.value) return;
    
    try {
      setLoading(true);
      setError(null);
      
      const response = await apiService.listComponents({
        usecase_id: selectedUseCase.value,
        ...(scopeFilter?.value && { scope: scopeFilter.value as 'PRIVATE' | 'PUBLIC' }),
        ...(searchText && { search: searchText }),
        sort_by: sortBy as 'component_name' | 'creation_timestamp',
        sort_order: sortOrder as 'asc' | 'desc',
      });
      
      setComponents(response.components);
    } catch (err) {
      console.error('Failed to load components:', err);
      setError(err instanceof Error ? err.message : 'Failed to load components');
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (value: string) => {
    setSearchText(value);
    updateUrlParams({ search: value || undefined });
  };

  const handleScopeChange = (option: SelectProps.Option | null) => {
    setScopeFilter(option);
    updateUrlParams({ scope: option?.value || undefined });
  };

  const updateUrlParams = (params: Record<string, string | undefined>) => {
    const newParams = new URLSearchParams(searchParams);
    
    Object.entries(params).forEach(([key, value]) => {
      if (value) {
        newParams.set(key, value);
      } else {
        newParams.delete(key);
      }
    });
    
    setSearchParams(newParams);
  };

  const getStatusIndicator = (status: string) => {
    switch (status) {
      case 'DEPLOYABLE':
        return <StatusIndicator type="success">Deployable</StatusIndicator>;
      case 'DEPRECATED':
        return <StatusIndicator type="warning">Deprecated</StatusIndicator>;
      case 'FAILED':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  const getComponentType = (type: string) => {
    const typeMap: Record<string, string> = {
      'aws.greengrass.generic': 'Generic',
      'aws.greengrass.lambda': 'Lambda',
      'aws.greengrass.nucleus': 'Nucleus',
    };
    return typeMap[type] || type;
  };

  const getPlatformBadges = (platforms: Component['platforms']) => {
    if (!platforms || platforms.length === 0) return 'All platforms';
    
    return platforms.map((platform, index) => {
      const platformName = platform.attributes?.os || platform.name || 'Unknown';
      const arch = platform.attributes?.architecture;
      const displayName = arch ? `${platformName}/${arch}` : platformName;
      
      return (
        <Badge key={index} color="blue">
          {displayName}
        </Badge>
      );
    });
  };

  const handleDeleteComponent = async () => {
    if (!componentToDelete || !selectedUseCase?.value) return;
    
    try {
      await apiService.deleteComponent(componentToDelete.arn, selectedUseCase.value);
      setShowDeleteModal(false);
      setComponentToDelete(null);
      await loadComponents();
    } catch (err) {
      console.error('Failed to delete component:', err);
      setError(err instanceof Error ? err.message : 'Failed to delete component');
    }
  };

  const handleDeployComponent = () => {
    if (!componentToDeploy || !selectedUseCase?.value) return;
    
    // Navigate to create deployment page with pre-filled component
    navigate(`/deployments/create?component_arn=${encodeURIComponent(componentToDeploy.arn)}&usecase_id=${selectedUseCase.value}`);
    setShowDeployModal(false);
    setComponentToDeploy(null);
  };

  const paginatedComponents = components.slice(
    currentPageIndex * pageSize,
    (currentPageIndex + 1) * pageSize
  );



  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h1"
            description={
              <SpaceBetween direction="horizontal" size="s" alignItems="center">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => {
                    setSelectedUseCase(detail.selectedOption);
                    setSelectedUsecaseId(detail.selectedOption?.value || null);
                  }}
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                  }))}
                  placeholder="Select a use case"
                  disabled={useCases.length === 0}
                  expandToViewport
                />
              </SpaceBetween>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  onClick={loadComponents}
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
            Greengrass Components
          </Header>
        }
      >
        <SpaceBetween size="m">
          {/* Filters */}
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              filteringText={searchText}
              filteringPlaceholder="Search components..."
              filteringAriaLabel="Filter components"
              onChange={({ detail }) => handleSearch(detail.filteringText)}
            />
            <Select
              selectedOption={scopeFilter}
              onChange={({ detail }) => handleScopeChange(detail.selectedOption)}
              options={scopeOptions}
              placeholder="Filter by scope"
              expandToViewport
            />
          </SpaceBetween>

          {/* Components Table */}
          <Table
            columnDefinitions={[
              {
                id: 'name',
                header: 'Component Name',
                cell: (item: Component) => (
                  <Link
                    onFollow={() => navigate(`/components/${encodeURIComponent(item.arn)}?usecase_id=${selectedUseCase?.value || ''}`)}
                  >
                    {item.component_name}
                  </Link>
                ),
                sortingField: 'component_name',
                isRowHeader: true,
              },
              {
                id: 'version',
                header: 'Latest Version',
                cell: (item: Component) => item.latest_version?.componentVersion || 'N/A',
              },
              {
                id: 'status',
                header: 'Status',
                cell: (item: Component) => getStatusIndicator(item.status),
              },
              {
                id: 'type',
                header: 'Type',
                cell: (item: Component) => getComponentType(item.component_type),
              },
              {
                id: 'platforms',
                header: 'Platforms',
                cell: (item: Component) => (
                  <SpaceBetween direction="horizontal" size="xs">
                    {getPlatformBadges(item.platforms)}
                  </SpaceBetween>
                ),
              },
              {
                id: 'model_name',
                header: 'Model Name',
                cell: (item: Component) => item.model_name || item.component_name,
              },
              {
                id: 'publisher',
                header: 'Publisher',
                cell: (item: Component) => (
                  <Box>
                    <Box variant="strong">{item.publisher || 'DDA Portal'}</Box>
                    {item.created_by_portal && (
                      <Badge color="green">Portal Created</Badge>
                    )}
                  </Box>
                ),
              },
              {
                id: 'deployments',
                header: 'Deployments',
                cell: (item: Component) => (
                  <Box>
                    <Box variant="strong">{item.deployment_info?.active_deployments ?? 0}</Box>
                    <Box variant="small" color="text-body-secondary">
                      {item.deployment_info?.device_count ?? 0} device{(item.deployment_info?.device_count ?? 0) !== 1 ? 's' : ''}
                    </Box>
                  </Box>
                ),
              },
              {
                id: 'created',
                header: 'Created',
                cell: (item: Component) => 
                  item.creation_timestamp ? new Date(item.creation_timestamp).toLocaleDateString() : '-',
                sortingField: 'creation_timestamp',
              },
              {
                id: 'actions',
                header: 'Actions',
                cell: (item: Component) => (
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      variant="normal"
                      iconName="settings"
                      onClick={() => {
                        navigate(`/components/configure?component_name=${encodeURIComponent(item.component_name)}&usecase_id=${selectedUseCase?.value || ''}`);
                      }}
                    >
                      Configure
                    </Button>
                    <Button
                      variant="normal"
                      iconName="share"
                      onClick={() => {
                        setComponentToDeploy(item);
                        setShowDeployModal(true);
                      }}
                    >
                      Deploy
                    </Button>
                    {(item.deployment_info?.device_count ?? 0) === 0 && 
                     // Hide delete for portal-managed components unless user is PortalAdmin
                     !(item.component_name.startsWith(PORTAL_MANAGED_COMPONENT_PREFIX) && user?.role !== 'PortalAdmin') && (
                      <Button
                        variant="normal"
                        iconName="remove"
                        onClick={() => {
                          setComponentToDelete(item);
                          setShowDeleteModal(true);
                        }}
                      >
                        Delete
                      </Button>
                    )}
                  </SpaceBetween>
                ),
              },
            ]}
            items={paginatedComponents}
            loading={loading}
            loadingText="Loading components..."
            selectedItems={selectedItems}
            onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
            selectionType="multi"
            sortingColumn={{ sortingField: sortBy }}
            sortingDescending={sortOrder === 'desc'}
            onSortingChange={({ detail }) => {
              setSortBy(detail.sortingColumn.sortingField || 'component_name');
              setSortOrder(detail.isDescending ? 'desc' : 'asc');
              updateUrlParams({
                sort_by: detail.sortingColumn.sortingField || 'component_name',
                sort_order: detail.isDescending ? 'desc' : 'asc',
              });
            }}
            empty={
              <Box textAlign="center" color="inherit">
                <b>No components found</b>
                <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                  {selectedUseCase 
                    ? 'No Greengrass components match your current filters.'
                    : 'Select a use case to view components.'}
                </Box>
                {selectedUseCase && (
                  <Button onClick={() => navigate(`/deployments/create?usecase_id=${selectedUseCase.value}`)}>
                    Create Deployment
                  </Button>
                )}
              </Box>
            }
            header={
              <Header
                counter={`(${components.length})`}
                actions={
                  selectedItems.length > 0 ? (
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button
                        variant="primary"
                        onClick={() => {
                          if (selectedItems.length === 1) {
                            // Single component - deploy directly
                            setComponentToDeploy(selectedItems[0]);
                            setShowDeployModal(true);
                          } else {
                            // Multiple components - navigate to create deployment with all selected
                            const componentArns = selectedItems.map(c => c.arn).join(',');
                            navigate(`/deployments/create?component_arns=${encodeURIComponent(componentArns)}&usecase_id=${selectedUseCase?.value || ''}`);
                          }
                        }}
                      >
                        Deploy Selected ({selectedItems.length})
                      </Button>
                    </SpaceBetween>
                  ) : undefined
                }
              >
                Components
              </Header>
            }
            pagination={
              <Pagination
                currentPageIndex={currentPageIndex + 1}
                pagesCount={Math.ceil(components.length / pageSize)}
                onChange={({ detail }) => setCurrentPageIndex(detail.currentPageIndex - 1)}
              />
            }
          />
        </SpaceBetween>
      </Container>

      {/* Delete Confirmation Modal */}
      <ConfirmationModal
        visible={showDeleteModal}
        title="Delete Component"
        message={`Are you sure you want to delete the component "${componentToDelete?.component_name}"? This action cannot be undone.`}
        confirmButtonText="Delete"
        cancelButtonText="Cancel"
        variant="danger"
        onConfirm={handleDeleteComponent}
        onCancel={() => {
          setShowDeleteModal(false);
          setComponentToDelete(null);
        }}
      >
        {componentToDelete && (componentToDelete.deployment_info?.device_count ?? 0) > 0 && (
          <Alert type="warning">
            This component is currently deployed to {componentToDelete.deployment_info?.device_count ?? 0} device(s). 
            You must undeploy it before deletion.
          </Alert>
        )}
      </ConfirmationModal>

      {/* Deploy Confirmation Modal */}
      <Modal
        visible={showDeployModal}
        onDismiss={() => {
          setShowDeployModal(false);
          setComponentToDeploy(null);
        }}
        header="Deploy Component"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowDeployModal(false);
                  setComponentToDeploy(null);
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDeployComponent}>
                Create Deployment
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            You are about to create a new deployment for component:
          </Box>
          <Box>
            <Box variant="strong">{componentToDeploy?.component_name}</Box>
            <Box variant="small" color="text-body-secondary">
              Version: {componentToDeploy?.latest_version?.componentVersion}
            </Box>
          </Box>
          <Box>
            You will be redirected to the deployment creation page where you can select target devices and configure deployment settings.
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}