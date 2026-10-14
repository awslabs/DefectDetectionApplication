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
import { useAuth } from '../contexts/AuthContext';
import { validateS3Uri } from '../utils/s3Validation';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';
import { useTableSort } from '../hooks/useTableSort';

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

/**
 * One jobs-table row: the shared LabelingJob shape plus the persisted
 * `labeling_backend` discriminator every list-payload job carries (the
 * backend defaults legacy jobs to GroundTruth). The page's own delete
 * predicate needs the discriminator because Ground Truth jobs never offer
 * deletion, and `status` is widened to the raw backend string because the
 * DDA lifecycle statuses (InProgress, Completed, Failed, Stopped,
 * Deleting, DeleteFailed) exceed the shared union
 * (labeling-job-cleanup-work-stealing-and-podium Requirements 4.1, 4.2).
 */
interface LabelingJobRow extends Omit<LabelingJob, 'status'> {
  status: string;
  labeling_backend: 'DDA' | 'GroundTruth';
}

/**
 * The job statuses from which a deletion may be requested from the list
 * page — the resting statuses, compared against the raw backend status
 * strings the list payload carries. InProgress (stop first) and Deleting
 * jobs offer no delete control
 * (labeling-job-cleanup-work-stealing-and-podium Requirements 4.1, 4.2).
 */
const DELETABLE_DDA_STATUSES = [
  'Completed',
  'Failed',
  'Stopped',
  'DeleteFailed',
];

export default function Labeling() {
  const navigate = useNavigate();
  const { user } = useAuth();
  // Labeling team management is admin-only (dda-data-labeling Req 3.7):
  // the link renders only for UseCaseAdmin/PortalAdmin, mirroring the
  // /labeling/teams route gate in App.tsx.
  const canManageTeams =
    user?.role === 'UseCaseAdmin' || user?.role === 'PortalAdmin';
  const [dataSourceType, setDataSourceType] = useState<'labeling' | 'pre-labeled'>('labeling');
  const [jobs, setJobs] = useState<LabelingJobRow[]>([]);
  const [datasets, setDatasets] = useState<PreLabeledDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState<any[]>([]);
  // Deletion of the selected resting DDA job
  // (labeling-job-cleanup-work-stealing-and-podium Requirements 4.1-4.5):
  // `jobToDelete` doubles as the confirmation-modal visibility and pins
  // the named job while the dialog is open.
  const [jobToDelete, setJobToDelete] = useState<LabelingJobRow | null>(null);
  const [deletingJob, setDeletingJob] = useState(false);
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
      
      // Transform API response to match the page's row type
      const transformedJobs: LabelingJobRow[] = response.jobs.map(job => ({
        job_id: job.job_id,
        usecase_id: useCaseId,
        name: job.job_name,
        manifest_s3: '', // Not provided by list endpoint
        output_s3: '', // Not provided by list endpoint
        task_type: job.task_type as LabelingJob['task_type'],
        images_count: job.image_count,
        labeled_count: job.labeled_objects || 0,
        status: job.status,
        progress_percent: job.progress_percent || 0,
        ground_truth_job_arn: '', // Not provided by list endpoint
        workforce_type: 'private', // Default value
        created_by: '', // Not provided by list endpoint
        created_at: job.created_at,
        // The list payload carries the persisted labeling_backend per job
        // (legacy jobs are defaulted to GroundTruth server-side); it is
        // read off the raw item because the typed list response predates
        // the field. Anything but 'DDA' offers no delete control
        // (labeling-job-cleanup-work-stealing-and-podium Req 4.1).
        labeling_backend:
          (job as { labeling_backend?: string }).labeling_backend === 'DDA'
            ? 'DDA'
            : 'GroundTruth',
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
      setError(getErrorMessage(err, 'Failed to validate manifest'));
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
      setError(getErrorMessage(err, 'Failed to create dataset'));
      console.error('Creation error:', err);
      scrollToTop();
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
      setError(getErrorMessage(err, 'Failed to delete dataset'));
      console.error('Delete error:', err);
    }
  };

  // Request deletion of the selected resting DDA job
  // (labeling-job-cleanup-work-stealing-and-podium Requirements 4.4, 4.5,
  // 4.7): the 202 answer reloads the list so the job renders in the
  // Deleting status (a completed deletion's later reload drops the row);
  // on failure the error surfaces through the page's existing alert
  // pattern with the job rendered unchanged.
  const handleDeleteJob = async () => {
    if (!jobToDelete) return;
    setDeletingJob(true);
    try {
      await apiService.deleteLabelingJob(jobToDelete.job_id);
      setJobToDelete(null);
      setSelectedItems([]);
      await loadLabelingJobs();
    } catch (err) {
      setJobToDelete(null);
      setError(getErrorMessage(err, 'Failed to delete labeling job'));
      console.error('Delete job error:', err);
    } finally {
      setDeletingJob(false);
    }
  };

  const getStatusIndicator = (status: LabelingJobRow['status']) => {
    const normalizedStatus = status.toLowerCase().replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
    
    // The deletion lifecycle pair renders distinctly: Deleting as
    // in-progress, Delete Failed as an error
    // (labeling-job-cleanup-work-stealing-and-podium Requirement 4.6).
    // Both spellings of each snake-cased status are keyed because the
    // leading toLowerCase() precedes the camelCase split — the backend's
    // 'DeleteFailed' reaches the map as 'deletefailed', the same way
    // 'InProgress' reaches it as 'inprogress'.
    const statusMap: Record<string, { type: 'pending' | 'in-progress' | 'success' | 'error' | 'info', label: string }> = {
      pending: { type: 'pending', label: 'Pending' },
      in_progress: { type: 'in-progress', label: 'In Progress' },
      inprogress: { type: 'in-progress', label: 'In Progress' },
      completed: { type: 'success', label: 'Completed' },
      failed: { type: 'error', label: 'Failed' },
      stopped: { type: 'info', label: 'Stopped' },
      deleting: { type: 'in-progress', label: 'Deleting' },
      delete_failed: { type: 'error', label: 'Delete Failed' },
      deletefailed: { type: 'error', label: 'Delete Failed' },
    };
    const config = statusMap[normalizedStatus] || { type: 'info' as const, label: status };
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  // The selected jobs-table row (the selection model is shared with the
  // datasets table and cleared on source switch), and the inline delete
  // predicate on the page's own row type: only a resting DDA job offers
  // the control — never InProgress or Deleting, and never Ground Truth
  // (labeling-job-cleanup-work-stealing-and-podium Requirements 4.1, 4.2).
  const selectedJob =
    dataSourceType === 'labeling'
      ? (selectedItems[0] as LabelingJobRow | undefined)
      : undefined;
  const selectedJobDeletable =
    selectedJob !== undefined &&
    selectedJob.labeling_backend === 'DDA' &&
    DELETABLE_DDA_STATUSES.includes(selectedJob.status);

  const getPrimaryAction = () => {
    if (dataSourceType === 'labeling') {
      return (
        <SpaceBetween direction="horizontal" size="xs">
          {selectedJob && selectedJobDeletable && (
            <Button
              data-testid="delete-job-button"
              onClick={() => setJobToDelete(selectedJob)}
            >
              {selectedJob.status === 'DeleteFailed'
                ? 'Retry Delete'
                : 'Delete Job'}
            </Button>
          )}
          {canManageTeams && (
            <Button onClick={() => navigate('/labeling/teams')}>
              Manage Teams
            </Button>
          )}
          <Button 
            variant="primary" 
            onClick={() => navigate(`/labeling/create?usecase_id=${selectedUseCase?.value || ''}`)}
            disabled={!selectedUseCase}
          >
            Create Labeling Job
          </Button>
        </SpaceBetween>
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

  const { items: sortedJobs, sortingProps: jobsSortingProps } = useTableSort(jobs);
  const { items: sortedDatasets, sortingProps: datasetsSortingProps } = useTableSort(datasets);

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
            resizableColumns
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
                // created_at is epoch seconds; convert to ms (same convention as the datasets table below)
                cell: (item) => new Date(item.created_at * 1000).toLocaleString(),
                sortingField: 'created_at',
              },
            ]}
            items={sortedJobs}
            {...jobsSortingProps}
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
            resizableColumns
            columnDefinitions={[
              {
                id: 'name',
                header: 'Dataset Name',
                cell: (item: PreLabeledDataset) => item.name,
                sortingField: 'name',
              },
              {
                id: 'task_type',
                header: 'Task Type',
                cell: (item: PreLabeledDataset) => (
                  <Badge color={item.task_type === 'classification' ? 'blue' : 'green'}>
                    {item.task_type}
                  </Badge>
                ),
                sortingField: 'task_type',
              },
              {
                id: 'image_count',
                header: 'Images',
                cell: (item: PreLabeledDataset) => item.image_count?.toLocaleString() || 'Unknown',
                sortingField: 'image_count',
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
                sortingField: 'created_at',
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
            items={sortedDatasets}
            {...datasetsSortingProps}
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

        {/* Delete confirmation for the selected DDA job — the detail
            page's wording verbatim (labeling-job-cleanup-work-stealing-
            and-podium Requirements 4.3, 4.4): names the job and states
            exactly what the cleanup removes (task assignments, pre-label/
            annotation artifacts) and what it retains (dataset images, any
            generated training manifest). */}
        <Modal
          visible={jobToDelete !== null}
          onDismiss={() => setJobToDelete(null)}
          header="Delete labeling job"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => setJobToDelete(null)}
                  disabled={deletingJob}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleDeleteJob}
                  loading={deletingJob}
                  data-testid="delete-job-confirm"
                >
                  Delete Job
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          {jobToDelete && (
            <Box>
              Are you sure you want to delete "{jobToDelete.name}"? Its
              task assignments and pre-label/annotation artifacts are
              removed. The dataset images and any generated training
              manifest are retained.
            </Box>
          )}
        </Modal>

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
                errorText={validateS3Uri(formData.manifest_s3_uri)}
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
