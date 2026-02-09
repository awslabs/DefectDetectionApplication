import { useState, useEffect } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  StatusIndicator,
  Button,
  Alert,
  ColumnLayout,
  KeyValuePairs,
  Table,
  Tabs,
  Badge,
  Link,
  BreadcrumbGroup,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { ComponentDetails, ComponentVersion } from '../types';
import { useAuth } from '../contexts/AuthContext';
import ConfirmationModal from '../components/ConfirmationModal';

// Portal-managed component prefix - these should not be deleted by non-admin users
const PORTAL_MANAGED_COMPONENT_PREFIX = 'aws.edgeml.dda.LocalServer';

export default function ComponentDetail() {
  const { arn } = useParams<{ arn: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { user } = useAuth();
  
  const [component, setComponent] = useState<ComponentDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [showDeployModal, setShowDeployModal] = useState(false);

  const currentUseCaseId = searchParams.get('usecase_id');
  const decodedArn = arn ? decodeURIComponent(arn) : '';

  useEffect(() => {
    if (decodedArn && currentUseCaseId) {
      loadComponent();
    }
  }, [decodedArn, currentUseCaseId]);

  const loadComponent = async () => {
    try {
      setLoading(true);
      setError(null);
      
      const response = await apiService.getComponent(decodedArn, currentUseCaseId!);
      setComponent(response);
    } catch (err: any) {
      console.error('Failed to load component:', err);
      // Handle different error types
      if (typeof err === 'string') {
        setError(err);
      } else if (err?.message && typeof err.message === 'string') {
        setError(err.message);
      } else if (err?.errors && Array.isArray(err.errors)) {
        setError(err.errors.map((e: any) => e.message || String(e)).join(', '));
      } else {
        setError('Failed to load component');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleDeleteComponent = async () => {
    if (!component) return;
    
    try {
      await apiService.deleteComponent(component.arn, currentUseCaseId!);
      setShowDeleteModal(false);
      navigate(`/components?usecase_id=${currentUseCaseId}`);
    } catch (err: any) {
      console.error('Failed to delete component:', err);
      if (typeof err === 'string') {
        setError(err);
      } else if (err?.message && typeof err.message === 'string') {
        setError(err.message);
      } else {
        setError('Failed to delete component');
      }
    }
  };

  const handleDeployComponent = () => {
    navigate(`/deployments/create?component_arn=${encodeURIComponent(decodedArn)}&usecase_id=${currentUseCaseId}`);
    setShowDeployModal(false);
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

  const getPlatformBadges = (platforms: ComponentDetails['platforms']) => {
    if (!platforms || platforms.length === 0) return [<Badge key="all" color="blue">All platforms</Badge>];
    
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

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading component details...
        </Box>
      </Container>
    );
  }

  if (error) {
    return (
      <Container>
        <Alert type="error">
          {error}
        </Alert>
      </Container>
    );
  }

  if (!component) {
    return (
      <Container>
        <Alert type="warning">
          Component not found.
        </Alert>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: 'Components', href: `/components?usecase_id=${currentUseCaseId}` },
          { text: component.component_name, href: '#' },
        ]}
        onFollow={(event) => {
          event.preventDefault();
          if (event.detail.href !== '#') {
            navigate(event.detail.href);
          }
        }}
      />

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h1"
            description={component.description || 'No description available'}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  onClick={loadComponent}
                  loading={loading}
                >
                  Refresh
                </Button>
                <Button
                  variant="normal"
                  iconName="settings"
                  onClick={() => navigate(`/components/configure?component_name=${encodeURIComponent(component.component_name)}&usecase_id=${currentUseCaseId}`)}
                >
                  Configure
                </Button>
                <Button
                  variant="primary"
                  iconName="share"
                  onClick={() => setShowDeployModal(true)}
                >
                  Deploy Component
                </Button>
                {component.deployment_info.device_count === 0 && 
                 // Hide delete for portal-managed components unless user is PortalAdmin
                 !(component.component_name.startsWith(PORTAL_MANAGED_COMPONENT_PREFIX) && user?.role !== 'PortalAdmin') && (
                  <Button
                    variant="normal"
                    iconName="remove"
                    onClick={() => setShowDeleteModal(true)}
                  >
                    Delete
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            {component.component_name}
          </Header>
        }
      >
        <SpaceBetween size="l">
          {/* Overview */}
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Status</Box>
              {getStatusIndicator(component.status)}
            </div>
            <div>
              <Box variant="awsui-key-label">Publisher</Box>
              <div>
                {component.publisher || 'DDA Portal'}
                {component.tags?.CreatedBy === 'DDA-Portal' && (
                  <Box>
                    <Badge color="green">Portal Created</Badge>
                  </Box>
                )}
              </div>
            </div>
            <div>
              <Box variant="awsui-key-label">Type</Box>
              <div>{component.component_type}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Created</Box>
              <div>{new Date(component.creation_timestamp).toLocaleString()}</div>
            </div>
          </ColumnLayout>

          {/* DDA Portal Information */}
          {component.tags?.CreatedBy === 'DDA-Portal' && (
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Model Name</Box>
                <div>{component.tags.ModelName || component.component_name}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Training Job</Box>
                <div>
                  {component.tags.TrainingJob ? (
                    <Link
                      onFollow={() => navigate(`/training/${component.tags.TrainingJob}?usecase_id=${currentUseCaseId}`)}
                    >
                      {component.tags.TrainingJob}
                    </Link>
                  ) : (
                    'N/A'
                  )}
                </div>
              </div>
              <div>
                <Box variant="awsui-key-label">Platform</Box>
                <div>{component.tags.Platform || 'Unknown'}</div>
              </div>
            </ColumnLayout>
          )}

          {/* Platforms */}
          <div>
            <Box variant="awsui-key-label">Supported Platforms</Box>
            <SpaceBetween direction="horizontal" size="xs">
              {getPlatformBadges(component.platforms)}
            </SpaceBetween>
          </div>

          {/* Deployment Information */}
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Active Deployments</Box>
              <Box variant="h3">{component.deployment_info.active_deployments}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Total Deployments</Box>
              <Box variant="h3">{component.deployment_info.total_deployments}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Deployed Devices</Box>
              <Box variant="h3">{component.deployment_info.device_count}</Box>
            </div>
          </ColumnLayout>

          {/* Deployed Devices */}
          {component.deployment_info.deployed_devices.length > 0 && (
            <Container header={<Header variant="h2">Deployed Devices</Header>}>
              <SpaceBetween direction="horizontal" size="xs">
                {component.deployment_info.deployed_devices.map((deviceId) => (
                  <Link
                    key={deviceId}
                    onFollow={() => navigate(`/devices/${deviceId}?usecase_id=${currentUseCaseId}`)}
                  >
                    {deviceId}
                  </Link>
                ))}
              </SpaceBetween>
            </Container>
          )}
        </SpaceBetween>
      </Container>

      {/* Tabs for detailed information */}
      <Tabs
        tabs={[
          {
            label: 'Versions',
            id: 'versions',
            content: (
              <Container>
                <Table
                  columnDefinitions={[
                    {
                      id: 'version',
                      header: 'Version',
                      cell: (item: ComponentVersion) => (
                        <Box variant="strong">{item.componentVersion}</Box>
                      ),
                      isRowHeader: true,
                    },
                    {
                      id: 'status',
                      header: 'Status',
                      cell: (item: ComponentVersion) => getStatusIndicator(item.status),
                    },
                    {
                      id: 'description',
                      header: 'Description',
                      cell: (item: ComponentVersion) => item.description || 'No description',
                    },
                    {
                      id: 'platforms',
                      header: 'Platforms',
                      cell: (item: ComponentVersion) => (
                        <SpaceBetween direction="horizontal" size="xs">
                          {getPlatformBadges(item.platforms)}
                        </SpaceBetween>
                      ),
                    },
                    {
                      id: 'created',
                      header: 'Created',
                      cell: (item: ComponentVersion) => 
                        new Date(item.creationTimestamp).toLocaleString(),
                    },
                  ]}
                  items={component.versions}
                  empty={
                    <Box textAlign="center" color="inherit">
                      <b>No versions found</b>
                    </Box>
                  }
                />
              </Container>
            ),
          },
          {
            label: 'Recipe',
            id: 'recipe',
            content: (
              <Container>
                <Box padding="s">
                  <pre style={{ 
                    backgroundColor: '#f4f4f4', 
                    padding: '16px', 
                    borderRadius: '4px',
                    overflow: 'auto',
                    maxHeight: '600px',
                    fontSize: '13px',
                    fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word'
                  }}>
                    {component.recipe ? JSON.stringify(component.recipe, null, 2) : 'No recipe available'}
                  </pre>
                </Box>
              </Container>
            ),
          },
          {
            label: 'Tags',
            id: 'tags',
            content: (
              <Container>
                {Object.keys(component.tags).length > 0 ? (
                  <KeyValuePairs
                    columns={2}
                    items={Object.entries(component.tags).map(([key, value]) => ({
                      label: key,
                      value: value,
                    }))}
                  />
                ) : (
                  <Box textAlign="center" color="inherit">
                    <b>No tags</b>
                    <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                      This component has no tags assigned.
                    </Box>
                  </Box>
                )}
              </Container>
            ),
          },
        ]}
      />

      {/* Delete Confirmation Modal */}
      <ConfirmationModal
        visible={showDeleteModal}
        title="Delete Component"
        message={`Are you sure you want to delete the component "${component.component_name}"? This action cannot be undone.`}
        confirmButtonText="Delete"
        cancelButtonText="Cancel"
        variant="danger"
        onConfirm={handleDeleteComponent}
        onCancel={() => setShowDeleteModal(false)}
      >
        {component.deployment_info.device_count > 0 && (
          <Alert type="warning">
            This component is currently deployed to {component.deployment_info.device_count} device(s). 
            You must undeploy it before deletion.
          </Alert>
        )}
      </ConfirmationModal>

      {/* Deploy Confirmation Modal */}
      <ConfirmationModal
        visible={showDeployModal}
        title="Deploy Component"
        message={`You are about to create a new deployment for component "${component.component_name}". You will be redirected to the deployment creation page.`}
        confirmButtonText="Create Deployment"
        cancelButtonText="Cancel"
        onConfirm={handleDeployComponent}
        onCancel={() => setShowDeployModal(false)}
      />
    </SpaceBetween>
  );
}