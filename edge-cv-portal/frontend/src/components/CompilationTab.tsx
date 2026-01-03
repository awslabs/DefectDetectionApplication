import { useState, useEffect } from 'react';
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
  ColumnLayout,
  KeyValuePairs,
  Modal,
  Checkbox,
  FormField,
  Input,
} from '@cloudscape-design/components';
import { CompilationJob, TrainingJob, GreengrassComponent } from '../types';
import { apiService } from '../services/api';

interface CompilationTabProps {
  trainingId: string;
  trainingJob: TrainingJob;
  onRefresh?: () => void;
}

// Available compilation targets with descriptions
const COMPILATION_TARGETS = [
  {
    id: 'jetson-xavier',
    name: 'NVIDIA Jetson Xavier',
    description: 'ARM64 with NVIDIA GPU acceleration for edge AI inference',
    recommended: true,
  },
  {
    id: 'x86_64-cpu',
    name: 'x86_64 CPU',
    description: 'Standard x86 64-bit CPU-only inference',
    recommended: true,
  },
  {
    id: 'x86_64-cuda',
    name: 'x86_64 with CUDA',
    description: 'x86 64-bit with NVIDIA GPU acceleration',
    recommended: false,
  },
  {
    id: 'arm64-cpu',
    name: 'ARM64 CPU',
    description: 'ARM 64-bit CPU-only inference (e.g., AWS Graviton)',
    recommended: false,
  },
];

export default function CompilationTab({ trainingId, trainingJob, onRefresh }: CompilationTabProps) {
  const [compilationJobs, setCompilationJobs] = useState<CompilationJob[]>(trainingJob.compilation_jobs || []);
  const [loading, setLoading] = useState(false);
  const [packagingLoading, setPackagingLoading] = useState(false);
  const [publishLoading, setPublishLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [showTargetModal, setShowTargetModal] = useState(false);
  const [showPublishModal, setShowPublishModal] = useState(false);
  const [selectedTargets, setSelectedTargets] = useState<string[]>(['jetson-xavier', 'x86_64-cpu']);
  const [componentName, setComponentName] = useState(`model-${trainingJob?.model_name?.toLowerCase().replace(/[^a-z0-9-]/g, '-') || 'model'}`);
  const [componentVersion, setComponentVersion] = useState('1.0.0');

  // Refresh compilation status
  const refreshCompilationStatus = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.getCompilationStatus(trainingId);
      setCompilationJobs(response.compilation_jobs as CompilationJob[]);
      if (onRefresh) {
        onRefresh();
      }
    } catch (err) {
      console.error('Failed to refresh compilation status:', err);
      setError(err instanceof Error ? err.message : 'Failed to refresh compilation status');
    } finally {
      setLoading(false);
    }
  };

  // Auto-refresh every 30 seconds if there are in-progress jobs
  useEffect(() => {
    const hasInProgressJobs = compilationJobs.some(job => job.status === 'InProgress');
    
    if (hasInProgressJobs) {
      const interval = setInterval(refreshCompilationStatus, 30000);
      return () => clearInterval(interval);
    }
  }, [compilationJobs, trainingId]);

  // Debug modal state changes
  useEffect(() => {
    console.log('Modal state changed to:', showTargetModal);
  }, [showTargetModal]);

  const getStatusIndicator = (status: CompilationJob['status']) => {
    switch (status) {
      case 'Completed':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'InProgress':
        return <StatusIndicator type="in-progress">In Progress</StatusIndicator>;
      case 'Failed':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'Stopped':
        return <StatusIndicator type="stopped">Stopped</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  const formatDuration = (startTime?: number, endTime?: number) => {
    if (!startTime) return 'N/A';
    const end = endTime || Date.now();
    const duration = end - startTime;
    const hours = Math.floor(duration / (1000 * 60 * 60));
    const minutes = Math.floor((duration % (1000 * 60 * 60)) / (1000 * 60));
    return `${hours}h ${minutes}m`;
  };

  const getArtifactDownloadLink = (job: CompilationJob) => {
    if (!job.compiled_model_s3) return null;
    
    // Generate a presigned URL or direct S3 link
    // For now, we'll show the S3 URI - in production you'd want a presigned URL
    return (
      <Link external href={`https://s3.console.aws.amazon.com/s3/object/${job.compiled_model_s3?.replace('s3://', '').replace('/', '?region=us-east-1&prefix=')}`}>
        Download Model
      </Link>
    );
  };

  const handleStartCompilation = () => {
    console.log('Start compilation button clicked');
    console.log('Training job status:', trainingJob?.status);
    console.log('Training job:', trainingJob);
    console.log('Current showTargetModal state:', showTargetModal);
    
    // Check if training job is completed
    if (!trainingJob || trainingJob.status !== 'Completed') {
      setError('Training job must be completed before starting compilation');
      return;
    }
    
    setShowTargetModal(true);
    console.log('Modal state set to true');
  };

  const handleConfirmCompilation = async () => {
    console.log('Confirm compilation called');
    console.log('Training job:', trainingJob);
    console.log('Training job status:', trainingJob?.status);
    console.log('Selected targets:', selectedTargets);
    
    if (!trainingJob || trainingJob.status !== 'Completed' || selectedTargets.length === 0) {
      console.log('Compilation blocked:', {
        hasTrainingJob: !!trainingJob,
        status: trainingJob?.status,
        isCompleted: trainingJob?.status === 'Completed',
        hasTargets: selectedTargets.length > 0
      });
      return;
    }
    
    try {
      setLoading(true);
      setError(null);
      setShowTargetModal(false);
      
      // Start compilation with selected targets
      await apiService.startCompilation(trainingId, selectedTargets);
      
      // Refresh compilation status
      await refreshCompilationStatus();
    } catch (err) {
      console.error('Failed to start compilation:', err);
      setError(err instanceof Error ? err.message : 'Failed to start compilation');
    } finally {
      setLoading(false);
    }
  };

  const handleTargetChange = (targetId: string, checked: boolean) => {
    if (checked) {
      setSelectedTargets(prev => [...prev, targetId]);
    } else {
      setSelectedTargets(prev => prev.filter(id => id !== targetId));
    }
  };

  // Handle manual packaging
  const handleStartPackaging = async () => {
    try {
      setPackagingLoading(true);
      setError(null);
      setSuccessMessage(null);
      
      const response = await apiService.startPackaging(trainingId);
      setSuccessMessage(`Packaging completed: ${response.message}`);
      
      if (onRefresh) {
        onRefresh();
      }
    } catch (err) {
      console.error('Failed to start packaging:', err);
      setError(err instanceof Error ? err.message : 'Failed to start packaging');
    } finally {
      setPackagingLoading(false);
    }
  };

  // Handle Greengrass component publish
  const handlePublishComponent = async () => {
    try {
      setPublishLoading(true);
      setError(null);
      setSuccessMessage(null);
      setShowPublishModal(false);
      
      const response = await apiService.publishGreengrassComponent(
        trainingId,
        componentName,
        componentVersion,
        trainingJob?.model_name
      );
      
      setSuccessMessage(`Published ${response.published_components.filter(c => c.status === 'published').length} component(s) successfully`);
      
      if (onRefresh) {
        onRefresh();
      }
    } catch (err) {
      console.error('Failed to publish component:', err);
      setError(err instanceof Error ? err.message : 'Failed to publish Greengrass component');
    } finally {
      setPublishLoading(false);
    }
  };

  // Check if we have completed compilation jobs (case-insensitive)
  const hasCompletedCompilations = compilationJobs.some(
    job => job.status?.toUpperCase() === 'COMPLETED'
  );

  // Check if packaging has been done
  const hasPackagedComponents = (trainingJob as any)?.packaged_components && 
    (trainingJob as any).packaged_components.some((c: any) => c.status === 'packaged');

  if (compilationJobs.length === 0) {
    return (
      <SpaceBetween size="l">
        {error ? (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        ) : null}
        
        <Container>
          <Box textAlign="center" color="inherit" padding="xxl">
            <b>No compilation jobs</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              This training job hasn't been compiled yet.
            </Box>
            <SpaceBetween size="m" direction="vertical" alignItems="center">
              <Box variant="p" color="inherit">
                Training Status: <strong>{trainingJob?.status || 'Unknown'}</strong>
              </Box>
              {trainingJob?.status === 'Completed' ? (
                <>
                  <Box variant="p" color="inherit">
                    Your training is complete! You can now compile the model for different target architectures.
                  </Box>
                  <Button
                    variant="primary"
                    onClick={handleStartCompilation}
                    loading={loading}
                    disabled={loading}
                  >
                    Start Compilation
                  </Button>
                </>
              ) : (
                <Box variant="p" color="inherit">
                  Compilation will be available once training is completed.
                </Box>
              )}
            </SpaceBetween>
          </Box>
        </Container>

        {/* Target Selection Modal - moved outside the container */}
        <Modal
          visible={showTargetModal}
          onDismiss={() => {
            console.log('Modal dismissed');
            setShowTargetModal(false);
          }}
          header="Select Compilation Targets"
          size="medium"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setShowTargetModal(false)}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleConfirmCompilation}
                  disabled={selectedTargets.length === 0}
                  loading={loading}
                >
                  Start Compilation ({selectedTargets.length} target{selectedTargets.length !== 1 ? 's' : ''})
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="l">
            <Box>
              Select the target architectures you want to compile your model for. 
              Each target will create an optimized model for that specific hardware platform.
            </Box>
            
            <FormField label="Target Architectures">
              <SpaceBetween size="s">
                {COMPILATION_TARGETS.map((target) => (
                  <Checkbox
                    key={target.id}
                    checked={selectedTargets.includes(target.id)}
                    onChange={({ detail }) => handleTargetChange(target.id, detail.checked)}
                  >
                    <SpaceBetween size="xs" direction="vertical">
                      <Box>
                        <strong>{target.name}</strong>
                        {target.recommended ? (
                          <Box display="inline" color="text-status-success" fontSize="body-s" fontWeight="bold">
                            {' '}(Recommended)
                          </Box>
                        ) : null}
                      </Box>
                      <Box variant="small" color="text-body-secondary">
                        {target.description}
                      </Box>
                    </SpaceBetween>
                  </Checkbox>
                ))}
              </SpaceBetween>
            </FormField>

            <Alert type="info" header="Compilation Time">
              Each target typically takes 10-30 minutes to compile. You can monitor progress in the compilation jobs table.
            </Alert>
          </SpaceBetween>
        </Modal>
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="l">
      {error ? (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      ) : null}

      {/* Compilation Jobs Table */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  onClick={refreshCompilationStatus}
                  loading={loading}
                  disabled={loading}
                >
                  Refresh Status
                </Button>
                {trainingJob?.status === 'Completed' ? (
                  <Button
                    variant="primary"
                    onClick={handleStartCompilation}
                    loading={loading}
                    disabled={loading}
                  >
                    Compile for Additional Targets
                  </Button>
                ) : null}
              </SpaceBetween>
            }
          >
            Compilation Jobs
          </Header>
        }
      >
        <Table
          columnDefinitions={[
            {
              id: 'target',
              header: 'Target Architecture',
              cell: (item) => (
                <Box>
                  <Box variant="strong">{item.target}</Box>
                  <Box variant="small" color="text-body-secondary">
                    {getTargetDescription(item.target)}
                  </Box>
                </Box>
              ),
              sortingField: 'target',
            },
            {
              id: 'status',
              header: 'Status',
              cell: (item) => getStatusIndicator(item.status),
              sortingField: 'status',
            },
            {
              id: 'duration',
              header: 'Duration',
              cell: (item) => formatDuration(item.created_at, item.completed_at),
            },
            {
              id: 'artifact',
              header: 'Compiled Model',
              cell: (item) => {
                if (item.status === 'Completed' && item.compiled_model_s3) {
                  return getArtifactDownloadLink(item);
                }
                return item.status === 'InProgress' ? 'Compiling...' : 'N/A';
              },
            },
            {
              id: 'job_name',
              header: 'SageMaker Job Name',
              cell: (item) => (
                <Box fontSize="body-s">
                  <span style={{ fontFamily: 'monospace' }}>{item.compilation_job_name}</span>
                </Box>
              ),
            },
          ]}
          items={compilationJobs}
          loading={loading}
          loadingText="Loading compilation jobs"
          empty={
            <Box textAlign="center" color="inherit">
              <b>No compilation jobs found</b>
            </Box>
          }
          sortingDisabled={false}
        />
      </Container>

      {/* Manual Packaging & Publish Actions */}
      {hasCompletedCompilations && (
        <Container
          header={
            <Header
              variant="h2"
              description="Package compiled models and publish as Greengrass components"
            >
              Component Actions
            </Header>
          }
        >
          <SpaceBetween size="m">
            {successMessage && (
              <Alert type="success" dismissible onDismiss={() => setSuccessMessage(null)}>
                {successMessage}
              </Alert>
            )}
            
            <ColumnLayout columns={2}>
              <Box>
                <SpaceBetween size="s">
                  <Box variant="h4">Step 1: Package Models</Box>
                  <Box variant="p" color="text-body-secondary">
                    Package compiled models into Greengrass-compatible ZIP artifacts.
                    {hasPackagedComponents && (
                      <Box color="text-status-success"> ✓ Packaging completed</Box>
                    )}
                  </Box>
                  <Button
                    onClick={handleStartPackaging}
                    loading={packagingLoading}
                    disabled={packagingLoading || publishLoading}
                  >
                    {hasPackagedComponents ? 'Re-package Models' : 'Package Models'}
                  </Button>
                </SpaceBetween>
              </Box>
              
              <Box>
                <SpaceBetween size="s">
                  <Box variant="h4">Step 2: Publish to Greengrass</Box>
                  <Box variant="p" color="text-body-secondary">
                    Create Greengrass components from packaged models.
                    {!hasPackagedComponents && (
                      <Box color="text-status-warning"> ⚠ Package models first</Box>
                    )}
                  </Box>
                  <Button
                    variant="primary"
                    onClick={() => setShowPublishModal(true)}
                    loading={publishLoading}
                    disabled={!hasPackagedComponents || packagingLoading || publishLoading}
                  >
                    Publish Component
                  </Button>
                </SpaceBetween>
              </Box>
            </ColumnLayout>
          </SpaceBetween>
        </Container>
      )}

      {/* Publish Modal */}
      <Modal
        visible={showPublishModal}
        onDismiss={() => setShowPublishModal(false)}
        header="Publish Greengrass Component"
        size="medium"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowPublishModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handlePublishComponent}
                loading={publishLoading}
                disabled={!componentName.startsWith('model-') || !componentVersion}
              >
                Publish Component
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          <FormField
            label="Component Name"
            description="Must start with 'model-' (e.g., model-defect-classifier)"
            errorText={!componentName.startsWith('model-') ? "Component name must start with 'model-'" : undefined}
          >
            <Input
              value={componentName}
              onChange={({ detail }) => setComponentName(detail.value)}
              placeholder="model-my-classifier"
            />
          </FormField>
          
          <FormField
            label="Component Version"
            description="Semantic version (e.g., 1.0.0)"
          >
            <Input
              value={componentVersion}
              onChange={({ detail }) => setComponentVersion(detail.value)}
              placeholder="1.0.0"
            />
          </FormField>
          
          <Alert type="info">
            This will create a Greengrass component that can be deployed to edge devices.
            The component will include the compiled model for all packaged targets.
          </Alert>
        </SpaceBetween>
      </Modal>

      {/* Error Details */}
      {compilationJobs.some(job => job.status === 'Failed') ? (
        <Container header={<Header variant="h2">Compilation Errors</Header>}>
          <SpaceBetween size="m">
            {compilationJobs
              .filter(job => job.status === 'Failed')
              .map((job, index) => (
                <Alert key={index} type="error" header={`${job.target} Compilation Failed`}>
                  <SpaceBetween size="s">
                    <Box>
                      <strong>Job:</strong> {job.compilation_job_name}
                    </Box>
                    {job.failure_reason ? (
                      <Box>
                        <strong>Reason:</strong> {job.failure_reason}
                      </Box>
                    ) : null}
                    {job.error ? (
                      <Box>
                        <strong>Error:</strong> {job.error}
                      </Box>
                    ) : null}
                    <Box variant="small" color="text-body-secondary">
                      Common causes: Model size too large, unsupported operations, or insufficient resources.
                      Check the SageMaker console for detailed logs.
                    </Box>
                  </SpaceBetween>
                </Alert>
              ))}
          </SpaceBetween>
        </Container>
      ) : null}

      {/* Greengrass Components (if available) */}
      {trainingJob.greengrass_components && trainingJob.greengrass_components.length > 0 ? (
        <Container header={<Header variant="h2">Greengrass Components</Header>}>
          <ColumnLayout columns={1}>
            {trainingJob.greengrass_components.map((component, index) => (
              <Container key={index}>
                <KeyValuePairs
                  columns={2}
                  items={[
                    { label: 'Component Name', value: component.component_name },
                    { label: 'Version', value: component.component_version },
                    { label: 'Target Architecture', value: component.target_architecture },
                    { label: 'Status', value: getComponentStatusIndicator(component.status) },
                    { label: 'Deployments', value: `${component.deployment_count} device(s)` },
                    {
                      label: 'AWS Console',
                      value: (
                        <Link
                          external
                          href={`https://console.aws.amazon.com/iot/home#/greengrass/v2/components/${component.component_name}`}
                        >
                          View in Console
                        </Link>
                      ),
                    },
                  ]}
                />
              </Container>
            ))}
          </ColumnLayout>
        </Container>
      ) : null}

      {/* Target Selection Modal - for when compilation jobs exist */}
      <Modal
        visible={showTargetModal}
        onDismiss={() => {
          console.log('Modal dismissed');
          setShowTargetModal(false);
        }}
        header="Select Compilation Targets"
        size="medium"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowTargetModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleConfirmCompilation}
                disabled={selectedTargets.length === 0}
                loading={loading}
              >
                Start Compilation ({selectedTargets.length} target{selectedTargets.length !== 1 ? 's' : ''})
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          <Box>
            Select the target architectures you want to compile your model for. 
            Each target will create an optimized model for that specific hardware platform.
          </Box>
          
          <FormField label="Target Architectures">
            <SpaceBetween size="s">
              {COMPILATION_TARGETS.map((target) => (
                <Checkbox
                  key={target.id}
                  checked={selectedTargets.includes(target.id)}
                  onChange={({ detail }) => handleTargetChange(target.id, detail.checked)}
                >
                  <SpaceBetween size="xs" direction="vertical">
                    <Box>
                      <strong>{target.name}</strong>
                      {target.recommended ? (
                        <Box display="inline" color="text-status-success" fontSize="body-s" fontWeight="bold">
                          {' '}(Recommended)
                        </Box>
                      ) : null}
                    </Box>
                    <Box variant="small" color="text-body-secondary">
                      {target.description}
                    </Box>
                  </SpaceBetween>
                </Checkbox>
              ))}
            </SpaceBetween>
          </FormField>

          <Alert type="info" header="Compilation Time">
            Each target typically takes 10-30 minutes to compile. You can monitor progress in the compilation jobs table.
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}

// Helper functions
function getTargetDescription(target: string): string {
  const descriptions: Record<string, string> = {
    'jetson-xavier': 'NVIDIA Jetson Xavier (ARM64 + GPU)',
    'x86_64-cpu': 'x86_64 CPU only',
    'x86_64-cuda': 'x86_64 with NVIDIA GPU',
    'arm64-cpu': 'ARM64 CPU only',
  };
  return descriptions[target] || 'Unknown architecture';
}

function getComponentStatusIndicator(status: GreengrassComponent['status']) {
  switch (status) {
    case 'active':
      return <StatusIndicator type="success">Active</StatusIndicator>;
    case 'creating':
      return <StatusIndicator type="in-progress">Creating</StatusIndicator>;
    case 'failed':
      return <StatusIndicator type="error">Failed</StatusIndicator>;
    default:
      return <StatusIndicator type="info">{status}</StatusIndicator>;
  }
}