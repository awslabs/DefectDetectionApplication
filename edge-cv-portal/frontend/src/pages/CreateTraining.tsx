import { useState, useEffect, useRef } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
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
  ColumnLayout,
  Box,
  Tiles,
  Modal,
  Toggle,
  Popover,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useUsecase } from '../contexts/UsecaseContext';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';
import {
  classifyManifestFormat,
  isDetectionModelType,
  ManifestFormat,
} from '../utils/manifestFormat';
import {
  ALL_MODEL_TYPE_OPTIONS,
  LFV_MODEL_TYPE_OPTIONS,
  MODEL_SOURCE_OPTIONS,
  MODEL_SOURCE_YOLO,
  labelingJobCoversFolder,
  modelSourceForLabelingTask,
  modelSourceForType,
  modelTypeOptionsForSource,
  normalizeS3Folder,
} from '../utils/trainingSources';

// Object Detection (YOLO) training settings forwarded to the script-mode
// entry point (datasets/detection_training/train.py) as `hyperparameters`.
// Defaults mirror the backend's DETECTION_DEFAULTS (detection_training.py).
interface DetectionParams {
  imgsz: string;
  epochs: string;
  batch: string;
  baseWeights: string;
  patience: string;
  scoreThreshold: string;
  iouThreshold: string;
}

const DETECTION_PARAM_DEFAULTS: DetectionParams = {
  imgsz: '1280',
  epochs: '100',
  batch: '4',
  baseWeights: 'yolo11s.pt',
  patience: '30',
  scoreThreshold: '0.25',
  iouThreshold: '0.45',
};

const DETECTION_IMGSZ_OPTIONS: SelectProps.Option[] = [
  { label: '640 (fast; small objects may be missed)', value: '640' },
  { label: '960', value: '960' },
  { label: '1280 (recommended for high-resolution captures)', value: '1280' },
  { label: '1600 (slow; large-memory GPU)', value: '1600' },
];

const DETECTION_BASE_WEIGHTS_OPTIONS: SelectProps.Option[] = [
  { label: 'yolo11n.pt (nano — fastest on device)', value: 'yolo11n.pt' },
  { label: 'yolo11s.pt (small — recommended)', value: 'yolo11s.pt' },
  { label: 'yolo11m.pt (medium — more accurate, slower)', value: 'yolo11m.pt' },
];

// Detection jobs run on a plain SageMaker PyTorch GPU DLC, so any GPU instance
// works (the marketplace list below is constrained by the LFV algorithm).
const DETECTION_INSTANCE_TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'ml.g4dn.xlarge (GPU - Recommended)', value: 'ml.g4dn.xlarge' },
  { label: 'ml.g4dn.2xlarge (GPU - More compute)', value: 'ml.g4dn.2xlarge' },
  { label: 'ml.g5.xlarge (GPU - A10G)', value: 'ml.g5.xlarge' },
  { label: 'ml.g5.2xlarge (GPU - A10G, more compute)', value: 'ml.g5.2xlarge' },
];
const DETECTION_DEFAULT_INSTANCE = DETECTION_INSTANCE_TYPE_OPTIONS[0];
const DETECTION_DEFAULT_MAX_RUNTIME = '10800';

export default function CreateTraining() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const cloneFrom = location.state?.cloneFrom;
  // Data Management → "Use for Training" arrives with the use case and the
  // image folder the user was browsing. A raw folder has no labels, so the
  // folder is used to find the labeling job that labeled it.
  const urlUseCaseId = searchParams.get('usecase_id');
  const dataPath = normalizeS3Folder(searchParams.get('data_path'));
  
  const [useCaseId, setUseCaseId] = useState<SelectProps.Option | null>(null);
  const [useCases, setUseCases] = useState<SelectProps.Option[]>([]);
  const [useCaseData, setUseCaseData] = useState<any[]>([]); // Store full usecase objects
  const [modelSource, setModelSource] = useState<SelectProps.Option>(MODEL_SOURCE_OPTIONS[0]);
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('1.0.0');
  const [modelType, setModelType] = useState<SelectProps.Option>(LFV_MODEL_TYPE_OPTIONS[0]);
  // Result of matching `data_path` against completed labeling jobs.
  const [dataPathMatch, setDataPathMatch] = useState<'pending' | 'matched' | 'none' | null>(dataPath ? 'pending' : null);
  const autoSelectedFromDataPath = useRef(false);
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
  const [segheadOnly, setSegheadOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manifestFormat, setManifestFormat] = useState<ManifestFormat | null>(null);
  const [detectionParams, setDetectionParams] = useState<DetectionParams>(DETECTION_PARAM_DEFAULTS);
  const [checkingManifestFormat, setCheckingManifestFormat] = useState(false);
  const [showTransformModal, setShowTransformModal] = useState(false);
  const [transforming, setTransforming] = useState(false);
  const [transformError, setTransformError] = useState<string | null>(null);
  const [transformedManifestUri, setTransformedManifestUri] = useState<string | null>(null);

  // Model types the selected source can train (LFV four, or Object Detection).
  const modelTypeOptions = modelTypeOptionsForSource(modelSource.value);

  // Instance types supported by the AWS Marketplace "Computer Vision Defect
  // Detection" training algorithm. This MUST match the algorithm's
  // SupportedTrainingInstanceTypes (TrainingSpecification) — other types are
  // rejected at job creation with "Unsupported instanceType". The algorithm
  // currently supports only these two g4dn (NVIDIA T4) sizes.
  const instanceTypeOptions: SelectProps.Option[] = [
    { label: 'ml.g4dn.2xlarge (GPU - Recommended)', value: 'ml.g4dn.2xlarge' },
    { label: 'ml.g4dn.4xlarge (GPU - More compute)', value: 'ml.g4dn.4xlarge' },
  ];

  // Flattened list of the individual instance options (for value lookups).
  const flatInstanceOptions: SelectProps.Option[] = [
    ...instanceTypeOptions,
    ...DETECTION_INSTANCE_TYPE_OPTIONS.filter(
      opt => !instanceTypeOptions.some(existing => existing.value === opt.value)
    ),
  ];

  const isDetection = isDetectionModelType(modelType.value);
  const activeInstanceTypeOptions = isDetection ? DETECTION_INSTANCE_TYPE_OPTIONS : instanceTypeOptions;

  /**
   * Select a model type and apply the defaults that go with it: max runtime,
   * the seghead toggle (segmentation only), and — because the marketplace
   * algorithm and the detection DLC accept different instance lists — the
   * instance type when crossing the detection boundary.
   */
  const applyModelType = (option: SelectProps.Option) => {
    const nextValue = option.value as string | undefined;
    const wasDetection = isDetection;
    const nextIsDetection = isDetectionModelType(nextValue);
    setModelType(option);
    const isSegmentation = !!nextValue?.includes('segmentation');
    const isRobust = !!nextValue?.includes('robust');
    setMaxRuntime(
      nextIsDetection ? DETECTION_DEFAULT_MAX_RUNTIME
        : isRobust ? '86400' : isSegmentation ? '7200' : '3600'
    );
    if (!isSegmentation) setSegheadOnly(false);
    if (nextIsDetection && !wasDetection) {
      setInstanceType(DETECTION_DEFAULT_INSTANCE);
    } else if (!nextIsDetection && wasDetection) {
      setInstanceType(instanceTypeOptions[0]);
    }
  };

  /** Select a model source and move Model Type to that source's first option. */
  const applyModelSource = (option: SelectProps.Option) => {
    setModelSource(option);
    const allowed = modelTypeOptionsForSource(option.value);
    if (!allowed.some(o => o.value === modelType.value)) {
      applyModelType(allowed[0]);
    }
  };

  // Populate form from cloned job
  useEffect(() => {
    if (cloneFrom) {
      // Set model name with a suffix to indicate it's a clone
      setModelName(cloneFrom.model_name ? `${cloneFrom.model_name}-clone` : '');
      
      // Set model source + type (the source is derived from the type: a
      // detection job clones onto YOLO, anything else onto the marketplace).
      if (cloneFrom.model_type) {
        const typeOption = ALL_MODEL_TYPE_OPTIONS.find(opt => opt.value === cloneFrom.model_type);
        if (typeOption) {
          const sourceOption = MODEL_SOURCE_OPTIONS.find(
            opt => opt.value === modelSourceForType(cloneFrom.model_type)
          );
          if (sourceOption) setModelSource(sourceOption);
          setModelType(typeOption);
        }
      }
      
      // Set instance type
      if (cloneFrom.instance_type) {
        const instanceOption = flatInstanceOptions.find(opt => opt.value === cloneFrom.instance_type);
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
        
        // Priority: cloned job → ?usecase_id= (Data Management link) → saved
        // selection → first option.
        const fromUrl = urlUseCaseId ? options.find(opt => opt.value === urlUseCaseId) : undefined;
        if (cloneFrom?.usecase_id) {
          const clonedUseCase = options.find(opt => opt.value === cloneFrom.usecase_id);
          if (clonedUseCase) {
            setUseCaseId(clonedUseCase);
            setSelectedUsecaseId(clonedUseCase.value);
          } else if (options.length > 0) {
            setUseCaseId(options[0]);
            setSelectedUsecaseId(options[0].value);
          }
        } else if (fromUrl) {
          setUseCaseId(fromUrl);
          setSelectedUsecaseId(fromUrl.value);
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
  }, [cloneFrom, urlUseCaseId, selectedUsecaseId, setSelectedUsecaseId]);

  const checkManifestFormat = async (manifestUri: string) => {
    if (!useCaseId?.value) return;
    
    try {
      setCheckingManifestFormat(true);
      const validation = await apiService.validateManifest({
        usecase_id: useCaseId.value as string,
        manifest_s3_uri: manifestUri,
      });
      
      console.log('Manifest validation result:', validation);
      
      // Classify the manifest from its first entry. Bounding-box manifests are
      // recognized FIRST ('detection'): they carry `<attr>-metadata` keys like
      // a Ground Truth classification manifest would, but the transform the
      // 'ground-truth' path insists on cannot be performed for detection.
      const sampleEntry = validation.stats.sample_entries?.[0];
      console.log('Sample entry keys:', sampleEntry ? Object.keys(sampleEntry) : '(none)');
      if (!sampleEntry) {
        console.log('No sample entries found in validation response');
      }
      setManifestFormat(classifyManifestFormat(sampleEntry));
    } catch (err) {
      console.error('Failed to check manifest format:', err);
      setManifestFormat('unknown');
    } finally {
      setCheckingManifestFormat(false);
    }
  };

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
          const completedJobs: any[] = (labelingData.jobs || [])
            .filter((job: any) => job.output_manifest_s3_uri); // Only include jobs with output manifest
          const jobOptions = completedJobs.map((job: any) => {
            // Bounding-box jobs never need the DDA transform; for detection
            // the "Transformed" badge would be noise (and wrong).
            const isBboxJob = String(job.task_type || '').toLowerCase().includes('detection');
            const suffix = isDetection || isBboxJob
              ? (isBboxJob ? ' • Bounding boxes' : '')
              : (job.is_transformed ? ' ✓ Transformed' : ' ⚠️ Not Transformed');
            const desc = isDetection || isBboxJob
              ? (isBboxJob ? ' • Object detection labels' : ` • ${job.task_type || 'labels'}`)
              : (job.is_transformed ? ' • DDA-compatible' : ' • Requires transformation');
            const coversFolder = dataPath && labelingJobCoversFolder(job, dataPath);
            return {
              label: `${job.job_name} (${job.image_count} images)${suffix}${coversFolder ? ' • this folder' : ''}`,
              value: job.output_manifest_s3_uri,
              description: `Created: ${new Date(job.created_at * 1000).toLocaleDateString()}${desc}`,
            };
          });
          setLabelingJobs(jobOptions);

          // Arrived from Data Management with a folder: pre-select the most
          // recent completed labeling job that labeled exactly that folder
          // (the list is newest-first), and point Model Source at the family
          // that can train from its labels. Once only.
          if (dataPath && !autoSelectedFromDataPath.current) {
            autoSelectedFromDataPath.current = true;
            const idx = completedJobs.findIndex((job: any) => labelingJobCoversFolder(job, dataPath));
            if (idx >= 0) {
              const job = completedJobs[idx];
              const wantedSource = modelSourceForLabelingTask(job.task_type);
              if (modelSource.value !== wantedSource) {
                const sourceOption = MODEL_SOURCE_OPTIONS.find(o => o.value === wantedSource);
                if (sourceOption) applyModelSource(sourceOption);
              }
              setDataPathMatch('matched');
              await handleLabelingJobSelect(jobOptions[idx]);
            } else {
              setDataPathMatch('none');
            }
          }
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
  }, [useCaseId, datasetSource, isDetection]);

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
      setTransformError(getErrorMessage(err, 'Failed to transform manifest'));
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

      // Detection sends its YOLO settings as `hyperparameters` (validated by
      // the backend); LFV sends only the seghead flag when toggled. The two
      // never mix (the seghead toggle is hidden for detection).
      const hyperparameters = isDetection
        ? {
            imgsz: parseInt(detectionParams.imgsz, 10),
            epochs: parseInt(detectionParams.epochs, 10),
            batch: parseInt(detectionParams.batch, 10),
            base_weights: detectionParams.baseWeights,
            patience: parseInt(detectionParams.patience, 10),
            score_threshold: parseFloat(detectionParams.scoreThreshold),
            iou_threshold: parseFloat(detectionParams.iouThreshold),
          }
        : segheadOnly
          ? { classification_logic: 'seg_head' }
          : undefined;

      await apiService.createTrainingJob({
        usecase_id: useCaseId.value as string,
        model_source: modelSource.value as string,
        model_name: modelName.trim(),
        model_version: modelVersion.trim(),
        model_type: modelType.value as string,
        dataset_manifest_s3: manifestUri.trim(),
        instance_type: instanceType.value as string,
        max_runtime_seconds: parseInt(maxRuntime),
        ...(hyperparameters && { hyperparameters }),
      });

      navigate('/training');
    } catch (err) {
      console.error('Failed to create training job:', err);
      const errorMessage = getErrorMessage(err, 'Failed to create training job');
      
      // Check if error is about manifest validation
      if (!isDetection && errorMessage.includes('Manifest validation failed')) {
        setError(`Manifest validation failed. The manifest is not in DDA format. Please transform the manifest using the "Transform Manifest Now" button and try again.`);
      } else {
        setError(errorMessage);
      }
      scrollToTop();
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
    // Model type / manifest kind must agree: a detector cannot learn from
    // anomaly labels, and the LFV algorithm cannot read bounding boxes.
    if (isDetection && (manifestFormat === 'dda' || manifestFormat === 'ground-truth')) {
      errors.push('Object Detection requires a bounding-box manifest (this manifest has classification/segmentation labels)');
    }
    if (!isDetection && manifestFormat === 'detection') {
      errors.push('This is a bounding-box manifest — select the Object Detection (YOLO) model type');
    }
    if (isDetection) {
      const imgsz = parseInt(detectionParams.imgsz, 10);
      const epochs = parseInt(detectionParams.epochs, 10);
      const batch = parseInt(detectionParams.batch, 10);
      const patience = parseInt(detectionParams.patience, 10);
      const score = parseFloat(detectionParams.scoreThreshold);
      const iou = parseFloat(detectionParams.iouThreshold);
      if (!(imgsz >= 320 && imgsz <= 2048 && imgsz % 32 === 0)) errors.push('Image size must be a multiple of 32 between 320 and 2048');
      if (!(epochs >= 1 && epochs <= 1000)) errors.push('Epochs must be between 1 and 1000');
      if (!(batch >= 1 && batch <= 64)) errors.push('Batch size must be between 1 and 64');
      if (!(patience >= 0 && patience <= 1000)) errors.push('Patience must be between 0 and 1000');
      if (!(score > 0 && score < 1)) errors.push('Score threshold must be strictly between 0 and 1');
      if (!(iou > 0 && iou < 1)) errors.push('IoU threshold must be strictly between 0 and 1');
    }
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

            {isDetection ? (
              <Alert type="info">
                Object Detection fine-tunes a YOLO detector on a bounding-box manifest (the output of a
                bounding-box labeling job) and exports ONNX for the DDA edge runtime. The model is served
                letterboxed exactly as it was trained, and needs no compilation step.
              </Alert>
            ) : (
              <Alert type="info">
                Select your model source. AWS Marketplace model requires properly formatted manifests with 'anomaly-label' attributes.
                Use the Manifest Transformer tool if your Ground Truth manifest needs conversion.
              </Alert>
            )}

            <FormField
              label="Model Source"
              description="Choose the model to train"
              stretch
            >
              <Select
                selectedOption={modelSource}
                onChange={({ detail }) => applyModelSource(detail.selectedOption)}
                options={MODEL_SOURCE_OPTIONS}
                selectedAriaLabel="Selected"
              />
            </FormField>

            {isDetection ? (
              <Alert type="info">
                <Box variant="h4">Manifest Requirements</Box>
                <Box variant="p">
                  Object Detection trains from a bounding-box manifest with these attributes:
                </Box>
                <ul style={{ marginLeft: '20px' }}>
                  <li><code>source-ref</code> - Image S3 URI</li>
                  <li><code>bounding-box</code> - Boxes (<code>annotations</code>, <code>image_size</code>)</li>
                  <li><code>bounding-box-metadata</code> - <code>class-map</code> naming each class id</li>
                </ul>
                <Box variant="p">
                  Ground Truth bounding-box jobs (job-named attribute) are accepted as-is; no transform is needed.
                </Box>
              </Alert>
            ) : modelSource.value === 'marketplace' && (
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
              description={
                modelSource.value === MODEL_SOURCE_YOLO
                  ? 'YOLO trains bounding-box object detectors'
                  : 'Choose between classification or segmentation'
              }
              stretch
            >
              <Select
                selectedOption={modelType}
                onChange={({ detail }) => applyModelType(detail.selectedOption)}
                options={modelTypeOptions}
                selectedAriaLabel="Selected"
              />
            </FormField>

            {isDetection && (
              <Container header={<Header variant="h3">Detection Settings</Header>}>
                <SpaceBetween size="m">
                  <ColumnLayout columns={2}>
                    <FormField
                      label="Network input size"
                      description="Square letterboxed input the detector trains and exports at. The device letterboxes identically."
                    >
                      <Select
                        selectedOption={
                          DETECTION_IMGSZ_OPTIONS.find(o => o.value === detectionParams.imgsz) ??
                          { label: detectionParams.imgsz, value: detectionParams.imgsz }
                        }
                        onChange={({ detail }) =>
                          setDetectionParams(p => ({ ...p, imgsz: detail.selectedOption.value as string }))
                        }
                        options={DETECTION_IMGSZ_OPTIONS}
                        selectedAriaLabel="Selected"
                      />
                    </FormField>
                    <FormField
                      label="Base weights"
                      description="Pretrained YOLO checkpoint to fine-tune from."
                    >
                      <Select
                        selectedOption={
                          DETECTION_BASE_WEIGHTS_OPTIONS.find(o => o.value === detectionParams.baseWeights) ??
                          { label: detectionParams.baseWeights, value: detectionParams.baseWeights }
                        }
                        onChange={({ detail }) =>
                          setDetectionParams(p => ({ ...p, baseWeights: detail.selectedOption.value as string }))
                        }
                        options={DETECTION_BASE_WEIGHTS_OPTIONS}
                        selectedAriaLabel="Selected"
                      />
                    </FormField>
                    <FormField label="Epochs" description="Fine-tuning epochs (early stopping applies).">
                      <Input
                        type="number"
                        value={detectionParams.epochs}
                        onChange={({ detail }) => setDetectionParams(p => ({ ...p, epochs: detail.value }))}
                      />
                    </FormField>
                    <FormField label="Batch size" description="Images per step; lower if the GPU runs out of memory.">
                      <Input
                        type="number"
                        value={detectionParams.batch}
                        onChange={({ detail }) => setDetectionParams(p => ({ ...p, batch: detail.value }))}
                      />
                    </FormField>
                    <FormField label="Patience" description="Epochs without improvement before early stopping.">
                      <Input
                        type="number"
                        value={detectionParams.patience}
                        onChange={({ detail }) => setDetectionParams(p => ({ ...p, patience: detail.value }))}
                      />
                    </FormField>
                    <FormField label="Score threshold" description="Minimum confidence kept on device (0–1).">
                      <Input
                        type="number"
                        step={0.05}
                        value={detectionParams.scoreThreshold}
                        onChange={({ detail }) => setDetectionParams(p => ({ ...p, scoreThreshold: detail.value }))}
                      />
                    </FormField>
                    <FormField label="IoU threshold" description="Non-max-suppression overlap threshold (0–1).">
                      <Input
                        type="number"
                        step={0.05}
                        value={detectionParams.iouThreshold}
                        onChange={({ detail }) => setDetectionParams(p => ({ ...p, iouThreshold: detail.value }))}
                      />
                    </FormField>
                  </ColumnLayout>
                  <Box variant="small" color="text-status-inactive">
                    Class names are read from the manifest's class-map. The trained model is served with
                    aspect-preserving (letterbox) preprocessing to match training.
                  </Box>
                </SpaceBetween>
              </Container>
            )}

            {modelType.value?.includes('robust') && (
              <Alert type="warning">
                Robust mode simulates multiple camera angles and lighting conditions during training.
                This significantly increases training time (6-24+ hours). Use it when your production
                environment has variable lighting or camera positions. If you have fixed cameras and
                controlled lighting, standard mode will train faster and perform just as well.
              </Alert>
            )}

            {modelType.value?.includes('segmentation') && (
              <FormField
                label={
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>Segmentation head only</span>
                    <Popover
                      dismissButton={false}
                      position="top"
                      size="medium"
                      triggerType="custom"
                      content="Trains using only the segmentation head (classification_logic=seg_head). Enable this if the binary classifier is producing false negatives on defective parts. The segmentation mask alone is often more accurate for detecting subtle defects."
                    >
                      <StatusIndicator type="info">Info</StatusIndicator>
                    </Popover>
                  </SpaceBetween>
                }
                stretch
              >
                <Toggle
                  checked={segheadOnly}
                  onChange={({ detail }) => setSegheadOnly(detail.checked)}
                >
                  Use segmentation head only (disable binary classifier)
                </Toggle>
              </FormField>
            )}

            {dataPath && dataPathMatch === 'matched' && (
              <Alert type="success" dismissible onDismiss={() => setDataPathMatch(null)}>
                Using the labeling job that labeled <code>{dataPath}</code>. Model Source was set to
                match its labels; change it if you meant something else.
              </Alert>
            )}
            {dataPath && dataPathMatch === 'none' && (
              <Alert type="warning" dismissible onDismiss={() => setDataPathMatch(null)}>
                <Box variant="p">
                  You arrived from <code>{dataPath}</code>, but training needs labels and no completed
                  labeling job covers that folder. Label it first, or pick another labeled dataset below.
                </Box>
                <Button
                  onClick={() =>
                    navigate(
                      `/labeling/create?usecase_id=${useCaseId?.value ?? ''}&input_path=${encodeURIComponent(dataPath)}`
                    )
                  }
                >
                  Create a labeling job for this folder
                </Button>
              </Alert>
            )}

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
              <Alert type={isDetection ? 'error' : 'success'} dismissible onDismiss={() => setManifestFormat(null)}>
                {isDetection
                  ? 'This manifest has classification/segmentation labels (anomaly-label). Object Detection needs a bounding-box manifest — pick a bounding-box labeling job or switch the model type.'
                  : '✓ Manifest is in DDA format and ready for training'}
                {!isDetection && transformedManifestUri && (
                  <Box variant="small" color="text-status-success" margin={{ top: 'xs' }}>
                    Using transformed manifest: {transformedManifestUri}
                  </Box>
                )}
              </Alert>
            )}

            {manifestFormat === 'detection' && (
              <Alert type={isDetection ? 'success' : 'error'} dismissible onDismiss={() => setManifestFormat(null)}>
                {isDetection
                  ? '✓ Bounding-box manifest detected — ready for Object Detection training (no transform needed)'
                  : 'This is a bounding-box manifest. Select the Object Detection (YOLO) model type to train from it.'}
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
                options={activeInstanceTypeOptions}
                selectedAriaLabel="Selected"
              />
            </FormField>

            <FormField
              label="Max Runtime (seconds)"
              description={
                isDetection
                  ? 'Maximum training time. A 100-epoch fine-tune on ~150 images takes about 20-40 minutes on ml.g4dn.xlarge. Default: 10800 (3 hours)'
                  : 'Maximum training time. Typical training takes 2-4 hours depending on dataset size. Default: 14400 (4 hours)'
              }
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

        <Container header={<Header variant="h2">Next Steps After Training</Header>}>
          {isDetection ? (
            <Alert type="info">
              Once training completes successfully, follow these steps to deploy your detector to edge devices:
              <ol style={{ marginTop: '8px', marginBottom: 0 }}>
                <li>Go to <strong>Training</strong> page and verify the job status is <strong>Completed</strong> (test mAP@50 is shown on the job)</li>
                <li>Open the job → <strong>Component Actions</strong> → <strong>Package Models</strong>. The exported ONNX needs no compilation; one package serves JetPack 5/6/7 and x86.</li>
                <li>Click <strong>Publish Component</strong> to create the Greengrass model component</li>
                <li>Go to <strong>Deployments</strong> → <strong>Create Deployment</strong> to deploy the detector to your edge devices</li>
              </ol>
            </Alert>
          ) : (
            <Alert type="info">
              Once training completes successfully, follow these steps to deploy your model to edge devices:
              <ol style={{ marginTop: '8px', marginBottom: 0 }}>
                <li>Go to <strong>Training</strong> page and verify the job status is <strong>Completed</strong></li>
                <li>Navigate to <strong>Components</strong> → click <strong>Compile Model</strong> to compile for your target architecture (ARM64 or x86_64)</li>
                <li>After compilation, the model is automatically packaged as a Greengrass component</li>
                <li>Go to <strong>Deployments</strong> → <strong>Create Deployment</strong> to deploy the compiled model to your edge devices</li>
              </ol>
            </Alert>
          )}
        </Container>

        <Container header={<Header variant="h2">Summary</Header>}>
          <ColumnLayout columns={2} variant="text-grid">
            <SpaceBetween size="xs">
              <Box>
                <Box variant="awsui-key-label">Model</Box>
                <Box>{modelName || 'Not specified'}</Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">Model Source</Box>
                <Box>{modelSource.label}</Box>
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
