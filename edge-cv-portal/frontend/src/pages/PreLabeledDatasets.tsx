import { useState, useEffect } from 'react';
import {
  ContentLayout,
  Header,
  Button,
  Table,
  Box,
  SpaceBetween,
  Alert,
  Modal,
  Form,
  FormField,
  Input,
  Textarea,
  Badge,
  Select,
  Container,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import S3Browser from '../components/S3Browser';

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

export default function PreLabeledDatasets() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const useCaseIdFromUrl = searchParams.get('usecase_id');
  const [datasets, setDatasets] = useState<PreLabeledDataset[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<ManifestValidation | null>(null);
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<any>(null);
  const [showHelpPanel, setShowHelpPanel] = useState(false);
  const [showBrowseModal, setShowBrowseModal] = useState(false);
  
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    manifest_s3_uri: '',
  });

  // Load use cases on mount
  useEffect(() => {
    loadUseCases();
  }, []);

  // Load datasets when use case changes
  useEffect(() => {
    if (selectedUseCase) {
      loadDatasets();
    }
  }, [selectedUseCase]);

  const loadUseCases = async () => {
    try {
      const data = await apiService.listUseCases();
      setUseCases(data.usecases || []);
      
      // If use case ID is in URL, select that one
      if (useCaseIdFromUrl && data.usecases) {
        const useCaseFromUrl = data.usecases.find((uc: any) => uc.usecase_id === useCaseIdFromUrl);
        if (useCaseFromUrl) {
          setSelectedUseCase(useCaseFromUrl);
          return;
        }
      }
      
      // Otherwise auto-select first use case if available
      if (data.usecases && data.usecases.length > 0) {
        setSelectedUseCase(data.usecases[0]);
      }
    } catch (err) {
      console.error('Failed to load use cases:', err);
      setError('Failed to load use cases');
    }
  };

  const loadDatasets = async () => {
    if (!selectedUseCase) {
      setLoading(false);
      return;
    }
    
    try {
      setLoading(true);
      setError(null);
      const data = await apiService.listPreLabeledDatasets(selectedUseCase.usecase_id);
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

    if (!selectedUseCase) {
      setError('No use case selected');
      return;
    }

    try {
      setValidating(true);
      setError(null);
      
      const result = await apiService.validateManifest({
        usecase_id: selectedUseCase.usecase_id,
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

    if (!selectedUseCase) {
      setError('No use case selected');
      return;
    }

    try {
      setCreating(true);
      setError(null);
      
      await apiService.createPreLabeledDataset({
        usecase_id: selectedUseCase.usecase_id,
        name: formData.name,
        description: formData.description,
        manifest_s3_uri: formData.manifest_s3_uri,
        task_type: validation.stats.task_type,
        label_attribute: Object.keys(validation.stats.label_distribution)[0] || '',
        image_count: validation.stats.total_images,
        label_stats: validation.stats.label_distribution,
        created_by: 'current-user', // TODO: Get from auth context
      });
      
      setShowCreateModal(false);
      setFormData({
        name: '',
        description: '',
        manifest_s3_uri: '',
      });
      setValidation(null);
      
      await loadDatasets();
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
      await loadDatasets();
    } catch (err) {
      setError('Failed to delete dataset');
      console.error('Delete error:', err);
    }
  };

  const selectManifestFromBrowser = (s3Uri: string) => {
    setFormData({ ...formData, manifest_s3_uri: s3Uri });
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={useCaseIdFromUrl && selectedUseCase ? `Use Case: ${selectedUseCase.name}` : undefined}
          info={
            <Button
              variant="icon"
              iconName="status-info"
              onClick={() => setShowHelpPanel(true)}
            />
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate('/labeling')}>Back to Labeling</Button>
              <Button
                variant="primary"
                onClick={() => setShowCreateModal(true)}
                disabled={!selectedUseCase}
              >
                Add Pre-Labeled Dataset
              </Button>
            </SpaceBetween>
          }
        >
          Pre-Labeled Datasets
        </Header>
      }
    >
      <SpaceBetween size="l">
        {!useCaseIdFromUrl && (
          <Container>
            <FormField
              label="Use Case"
              description="Select a use case to view its pre-labeled datasets"
            >
              <Select
                selectedOption={
                  selectedUseCase
                    ? {
                        label: selectedUseCase.name,
                        value: selectedUseCase.usecase_id,
                      }
                    : null
                }
                onChange={({ detail }) => {
                  const useCase = useCases.find(
                    (uc) => uc.usecase_id === detail.selectedOption.value
                  );
                  setSelectedUseCase(useCase);
                }}
                options={useCases.map((uc) => ({
                  label: uc.name,
                  value: uc.usecase_id,
                }))}
                placeholder="Select a use case"
                selectedAriaLabel="Selected"
              />
            </FormField>
          </Container>
        )}

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

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

        <Modal
          visible={showCreateModal}
          onDismiss={() => setShowCreateModal(false)}
          header="Add Pre-Labeled Dataset"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setShowCreateModal(false)}>Cancel</Button>
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
              >
                <SpaceBetween direction="horizontal" size="xs">
                  <Input
                    value={formData.manifest_s3_uri}
                    onChange={({ detail }) => setFormData({ ...formData, manifest_s3_uri: detail.value })}
                    placeholder="s3://your-bucket/path/manifest.manifest"
                  />
                  <Button 
                    onClick={() => setShowBrowseModal(true)} 
                    disabled={!selectedUseCase}
                    variant="normal"
                  >
                    Browse S3
                  </Button>
                </SpaceBetween>
              </FormField>

              {validation && (
                <SpaceBetween size="s">
                  <Alert
                    type={validation.valid ? 'success' : 'error'}
                    header={validation.valid ? '✓ Manifest Valid' : '✗ Validation Failed'}
                  >
                    {validation.valid ? (
                      <Box>
                        <strong>Dataset Statistics:</strong>
                        <ul style={{ marginTop: '8px', marginBottom: 0 }}>
                          <li>Total Images: {validation.stats.total_images}</li>
                          <li>Task Type: {validation.stats.task_type}</li>
                          <li>Labels: {Object.entries(validation.stats.label_distribution).map(([label, count]) => `${label} (${count})`).join(', ')}</li>
                        </ul>
                      </Box>
                    ) : (
                      <Box>
                        <strong style={{ color: '#d13212' }}>Issues Found:</strong>
                        <ul style={{ marginTop: '8px', marginBottom: 0, color: '#d13212' }}>
                          {validation.errors.map((error, index) => (
                            <li key={index} style={{ marginBottom: '4px' }}>
                              {error}
                            </li>
                          ))}
                        </ul>
                      </Box>
                    )}
                  </Alert>

                  {validation.warnings.length > 0 && (
                    <Alert type="warning" header="⚠ Warnings">
                      <Box>
                        <ul style={{ marginTop: 0, marginBottom: 0 }}>
                          {validation.warnings.map((warning, index) => (
                            <li key={index} style={{ marginBottom: '4px' }}>
                              {warning}
                            </li>
                          ))}
                        </ul>
                      </Box>
                    </Alert>
                  )}

                  {!validation.valid && (
                    <Alert type="info" header="How to Fix">
                      <Box>
                        <p style={{ marginTop: 0 }}>
                          Your manifest file has issues that need to be resolved before creating a dataset:
                        </p>
                        <ul style={{ marginBottom: 0 }}>
                          <li>
                            <strong>Missing label columns:</strong> Ensure your manifest has a label column (e.g., "cookie-classification") and its corresponding metadata column (e.g., "cookie-classification-metadata")
                          </li>
                          <li>
                            <strong>Malformed S3 URIs:</strong> Check that S3 paths don't have duplicate "s3://" prefixes or double slashes. Example: "s3://bucket/path/image.jpg" (not "s3://s3://bucket//path//image.jpg")
                          </li>
                          <li>
                            <strong>Invalid JSON:</strong> Ensure each line in the manifest is valid JSON
                          </li>
                        </ul>
                      </Box>
                    </Alert>
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>
          </Form>
        </Modal>

        <Modal
          visible={showHelpPanel}
          onDismiss={() => setShowHelpPanel(false)}
          header="How to Organize S3 for Pre-Labeled Datasets"
          size="large"
          footer={
            <Box float="right">
              <Button variant="primary" onClick={() => setShowHelpPanel(false)}>
                Got it
              </Button>
            </Box>
          }
        >
          <SpaceBetween size="l">
            <Box>
              <Box variant="h3">Required S3 Structure</Box>
              <Box variant="p">
                Your S3 bucket should be organized with training images and a manifest file:
              </Box>
              <Box padding="s" color="text-body-secondary">
                <pre style={{ margin: 0, fontFamily: 'monospace' }}>
                  s3://your-bucket/dataset-name/
                  {'\n'}├── training-images/
                  {'\n'}│   ├── image-1.jpg
                  {'\n'}│   ├── image-2.jpg
                  {'\n'}│   └── ...
                  {'\n'}└── train.manifest
                </pre>
              </Box>
            </Box>

            <Box>
              <Box variant="h3">Manifest File Format</Box>
              <Box variant="p">
                The manifest file is a JSON Lines (.jsonl) file with one JSON object per line:
              </Box>
              <Box padding="s" color="text-body-secondary">
                <pre style={{ margin: 0, fontFamily: 'monospace' }}>
                  {`{"source-ref": "s3://bucket/path/image1.jpg", "label": 1}`}
                  {'\n'}{`{"source-ref": "s3://bucket/path/image2.jpg", "label": 0}`}
                </pre>
              </Box>
            </Box>

            <Box>
              <Box variant="h3">Supported Task Types</Box>
              <ul>
                <li>
                  <strong>Classification:</strong> Each image has a label (e.g., defect=1, normal=0)
                </li>
                <li>
                  <strong>Object Detection:</strong> Images with bounding boxes
                </li>
                <li>
                  <strong>Semantic Segmentation:</strong> Images with pixel-level masks
                </li>
              </ul>
            </Box>

            <Box>
              <Box variant="h3">Example Upload Commands</Box>
              <Box variant="p">Upload your images and manifest to S3:</Box>
              <Box padding="s" color="text-body-secondary">
                <pre style={{ margin: 0, fontFamily: 'monospace' }}>
                  # Upload training images
                  {'\n'}aws s3 sync ./training-images/ s3://your-bucket/dataset-name/training-images/
                  {'\n'}
                  {'\n'}# Upload manifest file
                  {'\n'}aws s3 cp train.manifest s3://your-bucket/dataset-name/train.manifest
                </pre>
              </Box>
            </Box>

            <Alert type="info">
              <strong>Tip:</strong> After uploading, use the "Validate Manifest" button to verify your
              manifest file is correctly formatted and all images are accessible.
            </Alert>
          </SpaceBetween>
        </Modal>

        <S3Browser
          visible={showBrowseModal}
          onDismiss={() => setShowBrowseModal(false)}
          usecaseId={selectedUseCase?.usecase_id || ''}
          onSelectFile={selectManifestFromBrowser}
          fileFilter={(item) => item.type === 'manifest'}
          title="Select Manifest File"
          selectButtonText="Select"
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
