import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Table,
  Button,
  SpaceBetween,
  Box,
  StatusIndicator,
  ProgressBar,
  Link,
  Select,
  SelectProps,
  RadioGroup,
  Badge,
  Alert,
  Modal,
  Form,
  FormField,
  Input,
  Textarea,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { LabelingJob, UseCase } from '../types';
import { apiService } from '../services/api';

interface PreLabeledDataset {
  dataset_id: string;
  usecase_id: string;
  name: string;
  description?: string;
  manifest_s3_uri: string;
  image_count: number;
  label_attribute: string;
  label_stats: Record<string, number>;
  task_type: string;
  created_at: number;
  created_by: string;
  updated_at: number;
}

interface ManifestValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  stats: {
    total_images: number;
    task_type: string;
    label_distribution: Record<string, number>;
    sample_entries: any[];
  };
}

export default function Labeling() {
  const navigate = useNavigate();
  const [dataSourceType, setDataSourceType] = useState<'labeling' | 'pre-labeled'>('labeling');
  const [jobs, setJobs] = useState<LabelingJob[]>([]);
  const [datasets, setDatasets] = useState<PreLabeledDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState<any[]>([]);
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<ManifestValidation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    manifest_s3_uri: '',
  });

  useEffect(() => {
    loadUseCases();
  }, []);

  const loadUseCases = async () => {
    try {
      const response = await apiService.listUseCases();
      const useCaseList = response.usecases || [];
      setUseCases(useCaseList);
      // Auto-select first use case if available
      if (useCaseList.length > 0) {
        setSelectedUseCase({
          label: useCaseList[0].name,
          value: useCaseList[0].usecase_id,
        });
      }
    } catch (error) {
      console.error('Failed to load use cases:', error);
    }
  };

  useEffect(() => {
    if (selectedUseCase) {
      if (dataSourceType === 'labeling') {
        loadLabelingJobs();
      } else {
        loadPreLabeledDatasets();
      }
    } else {
      setJobs([]);
      setDatasets([]);
      setLoading(false);
    }
  }, [selectedUseCase, dataSourceType]);

  const loadLabelingJobs = async () => {
    if (!selectedUseCase?.value) return;
    
    const useCaseId = selectedUseCase.value;
    
    try {
      setLoading(true);
      const response = await apiService.listLabelingJobs({
        usecase_id: useCaseId,
      });
      
      // Transform API response to match LabelingJob type
      const transformedJobs: LabelingJob[] = response.jobs.map(job => ({
        job_id: job.job_id,
        usecase_id: useCaseId,
        name: job.job_name,
        manifest_s3: '', // Not provided by list endpoint
        output_s3: '', // Not provided by list endpoint
        task_type: job.task_type as LabelingJob['task_type'],
        images_count: job.image_count,
        labeled_count: job.labeled_objects || 0,
        status: job.status as LabelingJob['status'],
        progress_percent: job.progress_percent || 0,
        ground_truth_job_arn: '', // Not provided by list endpoint
        workforce_type: 'private', // Default value
        created_by: '', // Not provided by list endpoint
        created_at: job.created_at,
      }));
      
      setJobs(transformedJobs);
    } catch (error) {
      console.error('Failed to load labeling jobs:', error);
      setJobs([]);
    } finally {
      setLoading(false);
    }
  };

  const loadPreLabeledDatasets = async () => {
    if (!selectedUseCase?.value) return;
    
    try {
      setLoading(true);
      setError(null);
      const data = await apiService.listPreLabeledDatasets(selectedUseCase.value);
      setDatasets(data.datasets || []);
    } catch (err) {
      console.error('Error loading datasets:', err);
      setError('Failed to load datasets. Please check your connection and try again.');
      setDatasets([]);
    } finally {
      setLoading(false);
    }
  };

  const validateManifest = async () => {
    if (!formData.manifest_s3_uri) {
      setError('Please provide S3 URI');
      return;
    }

    if (!selectedUseCase?.value) {
      setError('No use case selected');
      return;
    }

    try {
      setValidating(true);
      setError(null);
      
      const result = await apiService.validateManifest({
        usecase_id: selectedUseCase.value,
        manifest_s3_uri: formData.manifest_s3_uri,
      });
      
      setValidation(result as any);
      
      if (!result.valid) {
        setError(`Validation failed: ${result.errors?.join(', ')}`);
      }
    } catch (err) {
      setError('Failed to validate manifest');
      console.error('Validation error:', err);
    } finally {
      setValidating(false);
    }
  };

  const createDataset = async () => {
    if (!validation?.valid) {
      setError('Please validate the manifest first');
      return;
    }

    if (!selectedUseCase?.value) {
      setError('No use case selected');
      return;
    }

    try {
      setCreating(true);
      setError(null);
      
      await apiService.createPreLabeledDataset({
        usecase_id: selectedUseCase.value,
        name: formData.name,
        description: formData.description,
        manifest_s3_uri: formData.manifest_s3_uri,
        task_type: validation.stats.task_type,
        label_attribute: Object.keys(validation.stats.label_distribution)[0] || '',
        image_count: validation.stats.total_images,
        label_stats: validation.stats.label_distribution,
        created_by: 'current-user',
      });
      
      setShowCreateModal(false);
      setFormData({
        name: '',
        description: '',
        manifest_s3_uri: '',
      });
      setValidation(null);
      
      await loadPreLabeledDatasets();
    } catch (err) {
      setError('Failed to create dataset');
      console.error('Creation error:', err);
    } finally {
      setCreating(false);
    }
  };

  const deleteDataset = async (datasetId: string) => {
    if (!confirm('Are you sure you want to delete this dataset?')) return;
    
    try {
      await apiService.deletePreLabeledDataset(datasetId);
      await loadPreLabeledDatasets();
    } catch (err) {
      setError('Failed to delete dataset');
      console.error('Delete error:', err);
    }
  };

  const getStatusIndicator = (status: LabelingJob['status']) => {
    const normalizedStatus = status.toLowerCase().replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
    
    const statusMap: Record<string, { type: 'pending' | 'in-progress' | 'success' | 'error' | 'info', label: string }> = {
      pending: { type: 'pending', label: 'Pending' },
      in_progress: { type: 'in-progress', label: 'In Progress' },
      inprogress: { type: 'in-progress', label: 'In Progress' },
      completed: { type: 'success', label: 'Completed' },
      failed: { type: 'error', label: 'Failed' },
      stopped: { type: 'info', label: 'Stopped' },
    };
    const config = statusMap[normalizedStatus] || { type: 'info' as const, label: status };
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  const getPrimaryAction = () => {
    if (dataSourceType === 'labeling') {
      return (
        <Button 
          variant="primary" 
          onClick={() => navigate(`/labeling/create?usecase_id=${selectedUseCase?.value || ''}`)}
          disabled={!selectedUseCase}
        >
          Create Labeling Job
        </Button>
      );
    } else {
      return (
        <Button
          variant="primary"
          onClick={() => setShowCreateModal(true)}
          disabled={!selectedUseCase}
        >
          Add Pre-Labeled Dataset
        </Button>
      );
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={
            <SpaceBetween direction="horizontal" size="m" alignItems="center">
              <Box variant="span">Use Case:</Box>
              <Select
                selectedOption={selectedUseCase}
                onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
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
          actions={getPrimaryAction()}
        >
          Data Labeling
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Box>
          <RadioGroup
            value={dataSourceType}
            onChange={({ detail }) => {
              setDataSourceType(detail.value as 'labeling' | 'pre-labeled');
              setSelectedItems([]);
            }}
            items={[
              {
                value: 'labeling',
                label: (
                  <Box>
                    <Box variant="strong">Use Ground Truth Labeling</Box>
                    <Box variant="p" color="text-body-secondary" fontSize="body-s">
                      Create labeling jobs and complete labeling through AWS Ground Truth
                    </Box>
                  </Box>
                ),
              },
              {
                value: 'pre-labeled',
                label: (
                  <Box>
                    <Box variant="strong">Use Pre-Labeled Dataset</Box>
                    <Box variant="p" color="text-body-secondary" fontSize="body-s">
                      Register pre-labeled datasets to skip labeling and go directly to training
                    </Box>
                  </Box>
                ),
              },
            ]}
          />
        </Box>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {dataSourceType === 'labeling' ? (
          <Table
            columnDefinitions={[
              {
                id: 'name',
                header: 'Job Name',
                cell: (item) => (
                  <Link onFollow={() => navigate(`/labeling/${item.job_id}`)}>
                    {item.name}
                  </Link>
                ),
                sortingField: 'name',
              },
              {
                id: 'task_type',
                header: 'Task Type',
                cell: (item) => item.task_type,
              },
              {
                id: 'progress',
                header: 'Progress',
                cell: (item) => (
                  <SpaceBetween direction="vertical" size="xxs">
                    <ProgressBar
                      value={item.progress_percent}
                      label={`${item.labeled_count} / ${item.images_count} images`}
                    />
                    <Box fontSize="body-s" color="text-body-secondary">
                      {item.progress_percent}% complete
                    </Box>
                  </SpaceBetween>
                ),
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
            items={jobs}
            loading={loading}
            loadingText="Loading labeling jobs"
            selectionType="single"
            selectedItems={selectedItems}
            onSelectionChange={({ detail }) =>
              setSelectedItems(detail.selectedItems)
            }
            empty={
              <Box textAlign="center" color="inherit">
                <b>No labeling jobs</b>
                <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                  {selectedUseCase 
                    ? 'No labeling jobs found for this use case.'
                    : 'Select a use case to view labeling jobs.'}
                </Box>
                {selectedUseCase && (
                  <Button onClick={() => navigate(`/labeling/create?usecase_id=${selectedUseCase.value}`)}>
                    Create Labeling Job
                  </Button>
                )}
              </Box>
            }
            sortingDisabled={false}
          />
        ) : (
          <Table
            columnDefinitions={[
              {
                id: 'name',
                header: 'Dataset Name',
                cell: (item: PreLabeledDataset) => item.name,
              },
              {
                id: 'task_type',
                header: 'Task Type',
                cell: (item: PreLabeledDataset) => (
                  <Badge color={item.task_type === 'classification' ? 'blue' : 'green'}>
                    {item.task_type}
                  </Badge>
                ),
              },
              {
                id: 'image_count',
                header: 'Images',
                cell: (item: PreLabeledDataset) => item.image_count?.toLocaleString() || 'Unknown',
              },
              {
                id: 'label_stats',
                header: 'Label Distribution',
                cell: (item: PreLabeledDataset) => (
                  <Box fontSize="body-s">
                    {item.label_stats
                      ? Object.entries(item.label_stats)
                          .map(([label, count]) => `${label}: ${count}`)
                          .join(', ')
                      : 'Unknown'}
                  </Box>
                ),
              },
              {
                id: 'created_at',
                header: 'Created',
                cell: (item: PreLabeledDataset) => new Date(item.created_at * 1000).toLocaleDateString(),
              },
              {
                id: 'actions',
                header: 'Actions',
                cell: (item: PreLabeledDataset) => (
                  <Button
                    variant="link"
                    onClick={() => deleteDataset(item.dataset_id)}
                  >
                    Delete
                  </Button>
                ),
              },
            ]}
            items={datasets}
            loading={loading}
            empty={
              <Box textAlign="center" color="inherit">
                <b>No pre-labeled datasets</b>
                <Box variant="p" color="inherit">
                  {selectedUseCase
                    ? 'Add a pre-labeled dataset to skip the labeling step and go directly to training.'
                    : 'Select a use case to view and manage pre-labeled datasets.'}
                </Box>
              </Box>
            }
          />
        )}

        <Modal
          visible={showCreateModal}
          onDismiss={() => {
            setShowCreateModal(false);
            setFormData({ name: '', description: '', manifest_s3_uri: '' });
            setValidation(null);
            setError(null);
          }}
          header="Add Pre-Labeled Dataset"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => {
                  setShowCreateModal(false);
                  setFormData({ name: '', description: '', manifest_s3_uri: '' });
                  setValidation(null);
                  setError(null);
                }}>
                  Cancel
                </Button>
                <Button
                  onClick={validateManifest}
                  loading={validating}
                  disabled={!formData.manifest_s3_uri || validating}
                >
                  Validate Manifest
                </Button>
                <Button
                  variant="primary"
                  onClick={createDataset}
                  loading={creating}
                  disabled={!validation?.valid || creating}
                >
                  Create Dataset
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Form>
            <SpaceBetween size="l">
              <FormField label="Dataset Name" stretch>
                <Input
                  value={formData.name}
                  onChange={({ detail }) => setFormData({ ...formData, name: detail.value })}
                  placeholder="Enter dataset name"
                />
              </FormField>

              <FormField label="Description" stretch>
                <Textarea
                  value={formData.description}
                  onChange={({ detail }) => setFormData({ ...formData, description: detail.value })}
                  placeholder="Describe your dataset"
                  rows={3}
                />
              </FormField>

              <FormField
                label="Manifest S3 URI"
                description="S3 path to your manifest file (e.g., s3://bucket/path/manifest.manifest)"
                stretch
              >
                <Input
                  value={formData.manifest_s3_uri}
                  onChange={({ detail }) => setFormData({ ...formData, manifest_s3_uri: detail.value })}
                  placeholder="s3://your-bucket/path/manifest.manifest"
                />
              </FormField>

              {validation && (
                <Alert
                  type={validation.valid ? 'success' : 'error'}
                  header={validation.valid ? 'Manifest Valid' : 'Validation Failed'}
                >
                  <SpaceBetween size="s">
                    {validation.valid ? (
                      <Box>
                        <strong>Dataset Statistics:</strong>
                        <ul>
                          <li>Total Images: {validation.stats.total_images}</li>
                          <li>Task Type: {validation.stats.task_type}</li>
                          <li>Labels: {Object.entries(validation.stats.label_distribution).map(([label, count]) => `${label} (${count})`).join(', ')}</li>
                        </ul>
                      </Box>
                    ) : (
                      <Box>
                        <strong>Errors:</strong>
                        <ul>
                          {validation.errors.map((err, index) => (
                            <li key={index}>{err}</li>
                          ))}
                        </ul>
                      </Box>
                    )}
                    
                    {validation.warnings.length > 0 && (
                      <Box>
                        <strong>Warnings:</strong>
                        <ul>
                          {validation.warnings.map((warning, index) => (
                            <li key={index}>{warning}</li>
                          ))}
                        </ul>
                      </Box>
                    )}
                  </SpaceBetween>
                </Alert>
              )}
            </SpaceBetween>
          </Form>
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
