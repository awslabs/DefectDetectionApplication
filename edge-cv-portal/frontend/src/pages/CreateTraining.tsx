import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Form,
  Button,
  FormField,
  Input,
  Select,
  SelectProps,
  Textarea,
  Alert,
  Checkbox,
  ColumnLayout,
  Box,
  Tiles,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

export default function CreateTraining() {
  const navigate = useNavigate();
  const location = useLocation();
  const cloneFrom = location.state?.cloneFrom;
  
  const [useCaseId, setUseCaseId] = useState<SelectProps.Option | null>(null);
  const [useCases, setUseCases] = useState<SelectProps.Option[]>([]);
  const [useCaseData, setUseCaseData] = useState<any[]>([]); // Store full usecase objects
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('1.0.0');
  const [modelType, setModelType] = useState<SelectProps.Option>({
    label: 'Classification',
    value: 'classification',
  });
  const [datasetSource, setDatasetSource] = useState<string>('manual'); // 'manual', 'ground-truth', 'pre-labeled'
  const [datasetManifest, setDatasetManifest] = useState('');
  const [selectedLabelingJob, setSelectedLabelingJob] = useState<SelectProps.Option | null>(null);
  const [selectedPreLabeledDataset, setSelectedPreLabeledDataset] = useState<SelectProps.Option | null>(null);
  const [labelingJobs, setLabelingJobs] = useState<SelectProps.Option[]>([]);
  const [preLabeledDatasets, setPreLabeledDatasets] = useState<SelectProps.Option[]>([]);
  const [instanceType, setInstanceType] = useState<SelectProps.Option>({
    label: 'ml.g4dn.2xlarge (GPU - Recommended)',
    value: 'ml.g4dn.2xlarge',
  });
  const [maxRuntime, setMaxRuntime] = useState('3600'); // 1 hour default for classification
  const [autoCompile, setAutoCompile] = useState(true);
  const [selectedTargets, setSelectedTargets] = useState({
    x86_64: true,
    aarch64: true,
    jetson: false,
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Define options before they're used in useEffect
  const modelTypeOptions: SelectProps.Option[] = [
    { label: 'Classification', value: 'classification', description: 'Binary defect detection' },
    {
      label: 'Classification (Robust)',
      value: 'classification-robust',
      description: 'Enhanced classification model',
    },
    {
      label: 'Segmentation',
      value: 'segmentation',
      description: 'Pixel-level defect localization',
    },
    {
      label: 'Segmentation (Robust)',
      value: 'segmentation-robust',
      description: 'Enhanced segmentation model',
    },
  ];

  const instanceTypeOptions: SelectProps.Option[] = [
    { label: 'ml.g4dn.2xlarge (GPU - Recommended)', value: 'ml.g4dn.2xlarge' },
    { label: 'ml.p3.2xlarge (GPU - High Performance)', value: 'ml.p3.2xlarge' },
    { label: 'ml.m5.xlarge (CPU - Budget)', value: 'ml.m5.xlarge' },
  ];

  // Populate form from cloned job
  useEffect(() => {
    if (cloneFrom) {
      // Set model name with a suffix to indicate it's a clone
      setModelName(cloneFrom.model_name ? `${cloneFrom.model_name}-clone` : '');
      
      // Set model type
      if (cloneFrom.model_type) {
        const typeOption = modelTypeOptions.find(opt => opt.value === cloneFrom.model_type);
        if (typeOption) {
          setModelType(typeOption);
        }
      }
      
      // Set dataset manifest
      if (cloneFrom.dataset_manifest_s3) {
        setDatasetManifest(cloneFrom.dataset_manifest_s3);
        setDatasetSource('manual');
      }
      
      // Set instance type
      if (cloneFrom.instance_type) {
        const instanceOption = instanceTypeOptions.find(opt => opt.value === cloneFrom.instance_type);
        if (instanceOption) {
          setInstanceType(instanceOption);
        }
      }
    }
  }, [cloneFrom]);

  // Fetch use cases
  useEffect(() => {
    const fetchUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        setUseCaseData(response.usecases); // Store full usecase objects
        const options = response.usecases.map(uc => ({
          label: uc.name,
          value: uc.usecase_id,
        }));
        setUseCases(options);
        
        // If cloning, set the use case from cloneFrom, otherwise use first option
        if (cloneFrom?.usecase_id) {
          const clonedUseCase = options.find(opt => opt.value === cloneFrom.usecase_id);
          if (clonedUseCase) {
            setUseCaseId(clonedUseCase);
          } else if (options.length > 0) {
            setUseCaseId(options[0]);
          }
        } else if (options.length > 0) {
          setUseCaseId(options[0]);
        }
      } catch (err) {
        console.error('Failed to fetch use cases:', err);
      }
    };
    fetchUseCases();
  }, [cloneFrom]);

  // Fetch labeling jobs and pre-labeled datasets when use case changes AND dataset source requires it
  useEffect(() => {
    if (!useCaseId?.value) return;
    
    // Only fetch if user has selected a dataset source that needs this data
    const needsLabelingJobs = datasetSource === 'labeling';
    const needsPreLabeled = datasetSource === 'pre-labeled';
    
    if (!needsLabelingJobs && !needsPreLabeled) return;

    const fetchDatasets = async () => {
      try {
        // Fetch completed labeling jobs only if needed
        if (needsLabelingJobs) {
          const labelingData = await apiService.listLabelingJobs({
            usecase_id: useCaseId.value as string,
            status: 'Completed',
          });
          const jobOptions = labelingData.jobs
            ?.filter((job: any) => job.output_manifest_s3_uri) // Only include jobs with output manifest
            .map((job: any) => ({
              label: `${job.job_name} (${job.image_count} images)`,
              value: job.output_manifest_s3_uri,
              description: `Created: ${new Date(job.created_at * 1000).toLocaleDateString()}`,
            })) || [];
          setLabelingJobs(jobOptions);
        }

        // Fetch pre-labeled datasets only if needed
        if (needsPreLabeled) {
          const preLabeledData = await apiService.listPreLabeledDatasets(useCaseId.value as string);
          const datasetOptions = preLabeledData.datasets?.map((dataset: any) => ({
            label: `${dataset.name} (${dataset.image_count} images)`,
            value: dataset.manifest_s3_uri,
            description: `Task: ${dataset.task_type}, Labels: ${Object.keys(dataset.label_stats || {}).join(', ')}`,
          })) || [];
          setPreLabeledDatasets(datasetOptions);
        }
      } catch (err) {
        console.error('Failed to fetch datasets:', err);
      }
    };

    fetchDatasets();
  }, [useCaseId, datasetSource]);

  const handleSubmit = async () => {
    if (!useCaseId) {
      setError('Please select a use case');
      return;
    }

    // Get the manifest URI based on selected source
    let manifestUri = '';
    if (datasetSource === 'manual') {
      manifestUri = datasetManifest;
    } else if (datasetSource === 'ground-truth' && selectedLabelingJob) {
      manifestUri = selectedLabelingJob.value as string;
    } else if (datasetSource === 'pre-labeled' && selectedPreLabeledDataset) {
      manifestUri = selectedPreLabeledDataset.value as string;
    }

    if (!manifestUri) {
      setError('Please select or enter a dataset manifest');
      return;
    }

    try {
      setSubmitting(true);
      setError(null);

      const compilationTargets = Object.entries(selectedTargets)
        .filter(([, checked]) => checked)
        .map(([target]) => target);

      await apiService.createTrainingJob({
        usecase_id: useCaseId.value as string,
        model_name: modelName.trim(),
        model_version: modelVersion.trim(),
        model_type: modelType.value as string,
        dataset_manifest_s3: manifestUri.trim(),
        instance_type: instanceType.value as string,
        max_runtime_seconds: parseInt(maxRuntime),
        auto_compile: autoCompile,
        compilation_targets: autoCompile ? compilationTargets : undefined,
      });

      navigate('/training');
    } catch (err) {
      console.error('Failed to create training job:', err);
      setError(err instanceof Error ? err.message : 'Failed to create training job');
    } finally {
      setSubmitting(false);
    }
  };

  // Check if form is valid based on selected dataset source
  const getManifestUri = () => {
    if (datasetSource === 'manual') return datasetManifest;
    if (datasetSource === 'ground-truth') return selectedLabelingJob?.value;
    if (datasetSource === 'pre-labeled') return selectedPreLabeledDataset?.value;
    return '';
  };

  // Get the selected usecase object
  const selectedUseCase = useCaseData.find(uc => uc.usecase_id === useCaseId?.value);

  const isFormValid = 
    useCaseId && 
    modelName && 
    /^[a-zA-Z0-9-]+$/.test(modelName) &&
    modelVersion &&
    getManifestUri() && 
    modelType && 
    instanceType;

  return (
    <Form
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => navigate('/training')} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" onClick={handleSubmit} disabled={!isFormValid || submitting} loading={submitting}>
            Start Training
          </Button>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="l">
        <Container header={<Header variant="h1">Start Training Job</Header>}>
          <SpaceBetween size="m">
            {error && (
              <Alert type="error" dismissible onDismiss={() => setError(null)}>
                {error}
              </Alert>
            )}

            <Alert type="info">
              Training uses the AWS Marketplace Computer Vision Defect Detection algorithm. Ensure
              you have an active subscription before proceeding.
            </Alert>

            <FormField
              label="Use Case"
              description="Select the use case for this training job"
              stretch
            >
              <Select
                selectedOption={useCaseId}
                onChange={({ detail }) => setUseCaseId(detail.selectedOption)}
                options={useCases}
                placeholder="Select a use case"
                selectedAriaLabel="Selected"
              />
            </FormField>

            <FormField
              label="Model Name"
              description="Name for the trained model (not the training job). This will be used to identify the model in deployments."
              errorText={
                modelName && !/^[a-zA-Z0-9-]+$/.test(modelName)
                  ? 'Model name can only contain letters, numbers, and hyphens'
                  : undefined
              }
              stretch
            >
              <Input
                value={modelName}
                onChange={({ detail }) => setModelName(detail.value)}
                placeholder="e.g., defect-detector-line1"
                invalid={modelName ? !/^[a-zA-Z0-9-]+$/.test(modelName) : false}
              />
            </FormField>

            <FormField 
              label="Model Version" 
              description="Version number for this model iteration (e.g., 1.0.0, 2.1.0). This is for tracking only and not used in the training job name." 
              stretch
            >
              <Input
                value={modelVersion}
                onChange={({ detail }) => setModelVersion(detail.value)}
                placeholder="1.0.0"
              />
            </FormField>

            <FormField
              label="Model Type"
              description="Choose between classification or segmentation"
              stretch
            >
              <Select
                selectedOption={modelType}
                onChange={({ detail }) => {
                  setModelType(detail.selectedOption);
                  // Update max runtime based on model type - segmentation takes longer but should still be reasonable
                  const isSegmentation = detail.selectedOption?.value?.includes('segmentation');
                  setMaxRuntime(isSegmentation ? '7200' : '3600'); // 2 hours for segmentation, 1 hour for classification
                }}
                options={modelTypeOptions}
                selectedAriaLabel="Selected"
              />
            </FormField>

            <FormField
              label="Dataset Source"
              description="Choose how to provide your training dataset"
              stretch
            >
              <Tiles
                value={datasetSource}
                onChange={({ detail }) => setDatasetSource(detail.value)}
                items={[
                  {
                    value: 'ground-truth',
                    label: 'Ground Truth Job',
                    description: 'Use output from a completed labeling job',
                  },
                  {
                    value: 'pre-labeled',
                    label: 'Pre-Labeled Dataset',
                    description: 'Use existing labeled data',
                  },
                  {
                    value: 'manual',
                    label: 'Manual S3 URI',
                    description: 'Enter manifest location directly',
                  },
                ]}
              />
            </FormField>

            {datasetSource === 'ground-truth' && (
              <FormField
                label="Select Labeling Job"
                description="Choose a completed Ground Truth labeling job"
                stretch
              >
                <Select
                  selectedOption={selectedLabelingJob}
                  onChange={({ detail }) => setSelectedLabelingJob(detail.selectedOption)}
                  options={labelingJobs}
                  placeholder={labelingJobs.length > 0 ? 'Select a labeling job' : 'No completed labeling jobs found'}
                  empty="No completed labeling jobs available"
                  selectedAriaLabel="Selected"
                />
              </FormField>
            )}

            {datasetSource === 'pre-labeled' && (
              <FormField
                label="Select Pre-Labeled Dataset"
                description="Choose from your uploaded pre-labeled datasets"
                stretch
              >
                <Select
                  selectedOption={selectedPreLabeledDataset}
                  onChange={({ detail }) => setSelectedPreLabeledDataset(detail.selectedOption)}
                  options={preLabeledDatasets}
                  placeholder={preLabeledDatasets.length > 0 ? 'Select a dataset' : 'No pre-labeled datasets found'}
                  empty="No pre-labeled datasets available"
                  selectedAriaLabel="Selected"
                />
                {preLabeledDatasets.length === 0 && (
                  <Box variant="small" color="text-status-inactive" margin={{ top: 'xs' }}>
                    <Button variant="link" onClick={() => navigate('/labeling/pre-labeled')}>
                      Add a pre-labeled dataset
                    </Button>
                  </Box>
                )}
              </FormField>
            )}

            {datasetSource === 'manual' && (
              <FormField
                label="Dataset Manifest S3 URI"
                description="S3 path to your manifest file"
                stretch
              >
                <Textarea
                  value={datasetManifest}
                  onChange={({ detail }) => setDatasetManifest(detail.value)}
                  placeholder="s3://your-bucket/manifests/train.manifest"
                  rows={2}
                />
              </FormField>
            )}
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Compute Configuration</Header>}>
          <SpaceBetween size="m">
            <FormField
              label="Instance Type"
              description="GPU instances recommended for faster training"
              stretch
            >
              <Select
                selectedOption={instanceType}
                onChange={({ detail }) => setInstanceType(detail.selectedOption)}
                options={instanceTypeOptions}
                selectedAriaLabel="Selected"
              />
            </FormField>

            <FormField
              label="Max Runtime (seconds)"
              description="Maximum training time. Typical training takes 2-4 hours depending on dataset size. Default: 14400 (4 hours)"
              stretch
            >
              <Input
                value={maxRuntime}
                onChange={({ detail }) => setMaxRuntime(detail.value)}
                type="number"
              />
            </FormField>

            <Alert type="warning">
              Training time varies based on dataset size and complexity. If your training job fails with "MaxRuntimeExceeded", increase this value. Recommended: 14400-21600 seconds (4-6 hours) for production datasets.
            </Alert>

            <Box variant="small" color="text-status-inactive">
              Estimated cost: ~$3-6/hour depending on instance type
            </Box>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Post-Training Options</Header>}>
          <SpaceBetween size="m">
            <Checkbox checked={autoCompile} onChange={({ detail }) => setAutoCompile(detail.checked)}>
              Automatically compile model after training completes
            </Checkbox>

            {autoCompile && (
              <FormField
                label="Compilation Targets"
                description="Select target platforms for edge deployment"
                stretch
              >
                <SpaceBetween size="s">
                  <Checkbox
                    checked={selectedTargets.x86_64}
                    onChange={({ detail }) =>
                      setSelectedTargets({ ...selectedTargets, x86_64: detail.checked })
                    }
                  >
                    x86_64 CPU (Intel/AMD processors)
                  </Checkbox>
                  <Checkbox
                    checked={selectedTargets.aarch64}
                    onChange={({ detail }) =>
                      setSelectedTargets({ ...selectedTargets, aarch64: detail.checked })
                    }
                  >
                    ARM64 (Standard ARM processors)
                  </Checkbox>
                  <Checkbox
                    checked={selectedTargets.jetson}
                    onChange={({ detail }) =>
                      setSelectedTargets({ ...selectedTargets, jetson: detail.checked })
                    }
                  >
                    Jetson Xavier (ARM64 with NVIDIA GPU)
                  </Checkbox>
                </SpaceBetween>
              </FormField>
            )}

            <Alert type="info">
              Compilation optimizes your model for specific edge hardware using SageMaker Neo.
              Compiled models will be automatically packaged as Greengrass components.
            </Alert>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Summary</Header>}>
          <ColumnLayout columns={2} variant="text-grid">
            <SpaceBetween size="xs">
              <Box>
                <Box variant="awsui-key-label">Model</Box>
                <Box>{modelName || 'Not specified'}</Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Type</Box>
                <Box>{modelType.label}</Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Instance</Box>
                <Box>{instanceType.label}</Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Dataset Source</Box>
                <Box>
                  {datasetSource === 'ground-truth' && 'Ground Truth Job'}
                  {datasetSource === 'pre-labeled' && 'Pre-Labeled Dataset'}
                  {datasetSource === 'manual' && 'Manual S3 URI'}
                </Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Training Data Bucket</Box>
                <Box>{selectedUseCase?.data_s3_bucket || selectedUseCase?.s3_bucket || 'Not configured'}</Box>
              </Box>
            </SpaceBetween>
            <SpaceBetween size="xs">
              <Box>
                <Box variant="awsui-key-label">Dataset</Box>
                <Box fontSize="body-s">
                  {datasetSource === 'ground-truth' && (selectedLabelingJob?.label || 'Not selected')}
                  {datasetSource === 'pre-labeled' && (selectedPreLabeledDataset?.label || 'Not selected')}
                  {datasetSource === 'manual' && (datasetManifest || 'Not specified')}
                </Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Model Output Bucket</Box>
                <Box>
                  {selectedUseCase?.s3_bucket 
                    ? selectedUseCase.s3_bucket 
                    : <Alert type="error">Output bucket not configured</Alert>
                  }
                </Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Auto-Compile</Box>
                <Box>{autoCompile ? 'Yes' : 'No'}</Box>
              </Box>
              {autoCompile && (
                <Box>
                  <Box variant="awsui-key-label">Targets</Box>
                  <Box>
                    {Object.entries(selectedTargets)
                      .filter(([, checked]) => checked)
                      .map(([target]) => target)
                      .join(', ') || 'None selected'}
                  </Box>
                </Box>
              )}
            </SpaceBetween>
          </ColumnLayout>
        </Container>
      </SpaceBetween>
    </Form>
  );
}
