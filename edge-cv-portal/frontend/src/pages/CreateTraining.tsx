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
  Alert,
  Checkbox,
  ColumnLayout,
  Box,
  Tiles,
  Modal,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useUsecase } from '../contexts/UsecaseContext';

export default function CreateTraining() {
  const navigate = useNavigate();
  const location = useLocation();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const cloneFrom = location.state?.cloneFrom;
  
  const [useCaseId, setUseCaseId] = useState<SelectProps.Option | null>(null);
  const [useCases, setUseCases] = useState<SelectProps.Option[]>([]);
  const [useCaseData, setUseCaseData] = useState<any[]>([]); // Store full usecase objects
  const [modelSource, setModelSource] = useState<SelectProps.Option>({
    label: 'AWS Marketplace - Computer Vision Defect Detection',
    value: 'marketplace',
  });
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('1.0.0');
  const [modelType, setModelType] = useState<SelectProps.Option>({
    label: 'Classification',
    value: 'classification',
  });
  const [datasetSource, setDatasetSource] = useState<string>('ground-truth'); // 'ground-truth', 'pre-labeled'
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
  const [manifestFormat, setManifestFormat] = useState<'dda' | 'ground-truth' | 'unknown' | null>(null);
  const [checkingManifestFormat, setCheckingManifestFormat] = useState(false);
  const [showTransformModal, setShowTransformModal] = useState(false);
  const [transforming, setTransforming] = useState(false);
  const [transformError, setTransformError] = useState<string | null>(null);
  const [transformedManifestUri, setTransformedManifestUri] = useState<string | null>(null);

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
        
        // If cloning, set the use case from cloneFrom, otherwise use saved selection or first option
        if (cloneFrom?.usecase_id) {
          const clonedUseCase = options.find(opt => opt.value === cloneFrom.usecase_id);
          if (clonedUseCase) {
            setUseCaseId(clonedUseCase);
            setSelectedUsecaseId(clonedUseCase.value);
          } else if (options.length > 0) {
            setUseCaseId(options[0]);
            setSelectedUsecaseId(options[0].value);
          }
        } else if (selectedUsecaseId) {
          const saved = options.find(opt => opt.value === selectedUsecaseId);
          if (saved) {
            setUseCaseId(saved);
          } else if (options.length > 0) {
            setUseCaseId(options[0]);
            setSelectedUsecaseId(options[0].value);
          }
        } else if (options.length > 0) {
          setUseCaseId(options[0]);
          setSelectedUsecaseId(options[0].value);
        }
      } catch (err) {
        console.error('Failed to fetch use cases:', err);
      }
    };
    fetchUseCases();
  }, [cloneFrom, selectedUsecaseId, setSelectedUsecaseId]);

  // Fetch labeling jobs and pre-labeled datasets when use case changes AND dataset source requires it
  useEffect(() => {
    if (!useCaseId?.value) return;
    
    // Only fetch if user has selected a dataset source that needs this data
    const needsLabelingJobs = datasetSource === 'ground-truth';
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
              label: `${job.job_name} (${job.image_count} images)${job.is_transformed ? ' ✓ Transformed' : ' ⚠️ Not Transformed'}`,
              value: job.output_manifest_s3_uri,
              description: `Created: ${new Date(job.created_at * 1000).toLocaleDateString()}${job.is_transformed ? ' • DDA-compatible' : ' • Requires transformation'}`,
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

  const handleLabelingJobSelect = async (option: SelectProps.Option | null) => {
    setSelectedLabelingJob(option);
    setManifestFormat(null);
    setTransformedManifestUri(null);
    
    if (option?.value) {
      await checkManifestFormat(option.value as string);
    }
  };

  const handlePreLabeledDatasetSelect = async (option: SelectProps.Option | null) => {
    setSelectedPreLabeledDataset(option);
    setManifestFormat(null);
    setTransformedManifestUri(null);
    
    if (option?.value) {
      await checkManifestFormat(option.value as string);
    }
  };

  const checkManifestFormat = async (manifestUri: string) => {
    if (!useCaseId?.value) return;
    
    try {
      setCheckingManifestFormat(true);
      const validation = await apiService.validateManifest({
        usecase_id: useCaseId.value as string,
        manifest_s3_uri: manifestUri,
      });
      
      console.log('Manifest validation result:', validation);
      
      // Check if manifest has Ground Truth format attributes
      const sampleEntry = validation.stats.sample_entries?.[0];
      console.log('Sample entry:', sampleEntry);
      
      if (sampleEntry) {
        // Look for Ground Truth specific attributes (ends with -metadata but not anomaly-label-metadata)
        const hasGroundTruthAttrs = Object.keys(sampleEntry).some(
          key => key.endsWith('-metadata') && 
                  key !== 'anomaly-label-metadata' && 
                  key !== 'anomaly-mask-ref-metadata'
        );
        
        console.log('Has Ground Truth attributes:', hasGroundTruthAttrs);
        console.log('Sample entry keys:', Object.keys(sampleEntry));
        
        if (hasGroundTruthAttrs) {
          setManifestFormat('ground-truth');
        } else if (sampleEntry['anomaly-label'] !== undefined) {
          setManifestFormat('dda');
        } else {
          setManifestFormat('unknown');
        }
      } else {
        console.log('No sample entries found in validation response');
        setManifestFormat('unknown');
      }
    } catch (err) {
      console.error('Failed to check manifest format:', err);
      setManifestFormat('unknown');
    } finally {
      setCheckingManifestFormat(false);
    }
  };

  const handleTransformManifest = async () => {
    const manifestUri = getManifestUri();
    if (!useCaseId?.value || !manifestUri) return;
    
    try {
      setTransforming(true);
      setTransformError(null);
      
      const result = await apiService.transformManifest({
        usecase_id: useCaseId.value as string,
        source_manifest_uri: manifestUri,
        task_type: (modelType.value as string)?.includes('segmentation') ? 'segmentation' : 'classification',
      });
      
      setTransformedManifestUri(result.transformed_manifest_uri);
      setManifestFormat('dda');
      setShowTransformModal(false);
    } catch (err) {
      setTransformError(err instanceof Error ? err.message : 'Failed to transform manifest');
    } finally {
      setTransforming(false);
    }
  };

  const handleSubmit = async () => {
    if (!useCaseId) {
      setError('Please select a use case');
      return;
    }

    // Check if manifest is in Ground Truth format and not transformed
    if (manifestFormat === 'ground-truth' && !transformedManifestUri) {
      setError('Manifest is in Ground Truth format and must be transformed before training. Click "Transform Manifest Now" to proceed.');
      return;
    }

    // Get the manifest URI based on selected source
    // Use transformed manifest if available, otherwise use original
    let manifestUri = '';
    if (transformedManifestUri) {
      manifestUri = transformedManifestUri;
    } else if (datasetSource === 'ground-truth' && selectedLabelingJob) {
      manifestUri = selectedLabelingJob.value as string;
    } else if (datasetSource === 'pre-labeled' && selectedPreLabeledDataset) {
      manifestUri = selectedPreLabeledDataset.value as string;
    }

    if (!manifestUri) {
      setError('Please select a dataset');
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
        model_source: modelSource.value as string,
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
      const errorMessage = err instanceof Error ? err.message : 'Failed to create training job';
      
      // Check if error is about manifest validation
      if (errorMessage.includes('Manifest validation failed')) {
        setError(`Manifest validation failed. The manifest is not in DDA format. Please transform the manifest using the "Transform Manifest Now" button and try again.`);
      } else {
        setError(errorMessage);
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Check if form is valid based on selected dataset source
  const getManifestUri = () => {
    if (datasetSource === 'ground-truth') return selectedLabelingJob?.value;
    if (datasetSource === 'pre-labeled') return selectedPreLabeledDataset?.value;
    return '';
  };

  // Get validation errors
  const getValidationErrors = () => {
    const errors: string[] = [];
    if (!useCaseId) errors.push('Use Case is required');
    if (!modelName) errors.push('Model Name is required');
    if (modelName && !/^[a-zA-Z0-9-]+$/.test(modelName)) errors.push('Model Name can only contain letters, numbers, and hyphens');
    if (!modelVersion) errors.push('Model Version is required');
    if (!getManifestUri()) errors.push('Dataset selection is required');
    if (!modelType) errors.push('Model Type is required');
    if (!instanceType) errors.push('Instance Type is required');
    if (manifestFormat === 'ground-truth' && !transformedManifestUri) errors.push('Manifest must be transformed from Ground Truth format');
    return errors;
  };

  const validationErrors = getValidationErrors();
  const isFormValid = validationErrors.length === 0;

  // Get the selected usecase object
  const selectedUseCase = useCaseData.find(uc => uc.usecase_id === useCaseId?.value);

  return (
    <Form
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => navigate('/training')} disabled={submitting}>
            Cancel
          </Button>
          <Button 
            variant="primary" 
            onClick={handleSubmit} 
            disabled={!isFormValid || submitting} 
            loading={submitting}
          >
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

            {validationErrors.length > 0 && (
              <Alert type="warning">
                <Box variant="h4">Complete the form to start training</Box>
                <ul style={{ marginLeft: '20px', marginTop: '8px' }}>
                  {validationErrors.map((err, idx) => (
                    <li key={idx}>{err}</li>
                  ))}
                </ul>
              </Alert>
            )}

            <Alert type="info">
              Select your model source. AWS Marketplace model requires properly formatted manifests with 'anomaly-label' attributes.
              Use the Manifest Transformer tool if your Ground Truth manifest needs conversion.
            </Alert>

            <FormField
              label="Model Source"
              description="Choose the model to train"
              stretch
            >
              <Select
                selectedOption={modelSource}
                onChange={({ detail }) => setModelSource(detail.selectedOption)}
                options={[
                  {
                    label: 'AWS Marketplace - Computer Vision Defect Detection',
                    value: 'marketplace',
                    description: 'Pre-trained defect detection model (requires subscription)',
                  },
                  {
                    label: 'Bring Your Own Model (BYOM)',
                    value: 'byom',
                    description: 'Coming soon - Use your custom model',
                    disabled: true,
                  },
                ]}
                selectedAriaLabel="Selected"
              />
            </FormField>

            {modelSource.value === 'marketplace' && (
              <Alert type="warning">
                <Box variant="h4">Manifest Requirements</Box>
                <Box variant="p">
                  The AWS Marketplace model requires manifests with these exact attribute names:
                </Box>
                <ul style={{ marginLeft: '20px' }}>
                  <li><code>source-ref</code> - Image S3 URI</li>
                  <li><code>anomaly-label</code> - Label value (0 or 1)</li>
                  <li><code>anomaly-label-metadata</code> - Label metadata</li>
                </ul>
                <Box variant="p">
                  If your Ground Truth manifest uses different names (e.g., <code>my-job</code>, <code>my-job-metadata</code>),
                  the system will automatically detect this and offer to transform it when you select your dataset.
                </Box>
              </Alert>
            )}

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
                  : !modelName ? 'Model Name is required' : undefined
              }
              stretch
            >
              <Input
                value={modelName}
                onChange={({ detail }) => setModelName(detail.value)}
                placeholder="e.g., defect-detector-line1"
                invalid={modelName ? !/^[a-zA-Z0-9-]+$/.test(modelName) : !modelName}
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
                  onChange={({ detail }) => handleLabelingJobSelect(detail.selectedOption)}
                  options={labelingJobs}
                  placeholder={labelingJobs.length > 0 ? 'Select a labeling job' : 'No completed labeling jobs found'}
                  empty="No completed labeling jobs available"
                  selectedAriaLabel="Selected"
                  disabled={checkingManifestFormat}
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
                  onChange={({ detail }) => handlePreLabeledDatasetSelect(detail.selectedOption)}
                  options={preLabeledDatasets}
                  placeholder={preLabeledDatasets.length > 0 ? 'Select a dataset' : 'No pre-labeled datasets found'}
                  empty="No pre-labeled datasets available"
                  selectedAriaLabel="Selected"
                  disabled={checkingManifestFormat}
                />
                {preLabeledDatasets.length === 0 && (
                  <Box variant="small" color="text-status-inactive" margin={{ top: 'xs' }}>
                    No datasets available.{' '}
                    <Button variant="link" onClick={() => navigate('/labeling')}>
                      Add a pre-labeled dataset first
                    </Button>
                  </Box>
                )}
              </FormField>
            )}

            {manifestFormat === 'ground-truth' && (
              <Alert type="warning" dismissible onDismiss={() => setManifestFormat(null)}>
                <Box variant="h4">Ground Truth Format Detected</Box>
                <Box variant="p">
                  This manifest uses Ground Truth attribute names (e.g., <code>my-job</code>, <code>my-job-metadata</code>).
                  The AWS Marketplace model requires DDA format (<code>anomaly-label</code>, <code>anomaly-label-metadata</code>).
                </Box>
                <Box variant="p">
                  <Button variant="primary" onClick={() => setShowTransformModal(true)} loading={transforming}>
                    Transform Manifest Now
                  </Button>
                </Box>
              </Alert>
            )}

            {manifestFormat === 'dda' && (
              <Alert type="success" dismissible onDismiss={() => setManifestFormat(null)}>
                ✓ Manifest is in DDA format and ready for training
                {transformedManifestUri && (
                  <Box variant="small" color="text-status-success" margin={{ top: 'xs' }}>
                    Using transformed manifest: {transformedManifestUri}
                  </Box>
                )}
              </Alert>
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

      <Modal
        onDismiss={() => setShowTransformModal(false)}
        visible={showTransformModal}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowTransformModal(false)} disabled={transforming}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleTransformManifest} loading={transforming}>
                Transform Manifest
              </Button>
            </SpaceBetween>
          </Box>
        }
        header="Transform Ground Truth Manifest"
      >
        <SpaceBetween size="m">
          {transformError && (
            <Alert type="error" dismissible onDismiss={() => setTransformError(null)}>
              {transformError}
            </Alert>
          )}
          <Box variant="p">
            This manifest is in Ground Truth format. It will be transformed to DDA format required by the AWS Marketplace model.
          </Box>
          <Box variant="p">
            <strong>What happens:</strong>
          </Box>
          <ul style={{ marginLeft: '20px' }}>
            <li>Ground Truth attribute names will be renamed to DDA standard names</li>
            <li>A new transformed manifest will be created in your S3 bucket (original remains unchanged)</li>
            <li>Training will use the transformed manifest</li>
          </ul>
          <Box variant="p">
            <strong>Original manifest:</strong> {getManifestUri()}
          </Box>
          <Box variant="p">
            <strong>Transformed manifest:</strong> {getManifestUri()?.replace('.manifest', '-dda.manifest')}
          </Box>
        </SpaceBetween>
      </Modal>
    </Form>
  );
}
