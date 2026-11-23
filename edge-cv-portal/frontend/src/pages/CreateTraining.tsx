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
import type { BaseModelRef, DetectionArch, DetectionBaseModels, OutputMode } from '../services/api';
import { getConfig } from '../config';
import { useUsecase } from '../contexts/UsecaseContext';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';
import {
  classifyManifestFormat,
  INSTANCE_SEGMENTATION_MODEL_TYPE,
  isDetectionModelType,
  isLfvSegmentationType,
  ManifestFormat,
} from '../utils/manifestFormat';
import {
  ALL_MODEL_TYPE_OPTIONS,
  LFV_MODEL_TYPE_OPTIONS,
  MODEL_SOURCE_OPTIONS,
  MODEL_SOURCE_RF_DETR,
  MODEL_SOURCE_YOLO,
  detectionArchFor,
  detectionArchForSource,
  isInstanceSegmentationTaskType,
  labelingJobCoversFolder,
  modelSourceForLabelingTask,
  modelSourceForType,
  normalizeS3Folder,
  visibleModelTypeOptionsForSource,
} from '../utils/trainingSources';
import {
  DetectionFormParams,
  RFDETR_SEG_INPUT_BLOCK,
  RFDETR_SEG_RESOLUTION_BOUNDS,
  RFDETR_SEG_SIZES,
  RFDETR_SEG_SIZE_ORDER,
  RFDETR_SIZES,
  RFDETR_SIZE_ORDER,
  YOLO_PARAM_DEFAULTS,
  applyRfDetrSegSize,
  applyRfDetrSize,
  baseModelClassNames,
  baseModelSelectOptions,
  buildDetectionSubmitFields,
  classListsDiffer,
  classMismatchWarning,
  classNamesFromManifestEntry,
  defaultDetectionInstanceType,
  defaultInstanceTypeForSegSize,
  detectionParamDefaults,
  findBaseModelOption,
  findBaseModelRecord,
  groupDetectionBaseModels,
  isRfDetrSegSize,
  isRfDetrSize,
  parseBaseModelOptionValue,
  parseClassNamesInput,
  paramsFromBaseRecord,
  publishedBaseModelRef,
  rfdetrNativeResolution,
  rfdetrSegNativeResolution,
  validateDetectionParams,
} from '../utils/detectionBaseModels';

// Object Detection training settings are forwarded to the arch's script-mode
// entry point (datasets/detection_training/train.py / train_rfdetr.py) as
// `hyperparameters`. Defaults, bounds and the submit payload live in
// utils/detectionBaseModels.ts and mirror the backend's detection_training.py.

const DETECTION_IMGSZ_OPTIONS: SelectProps.Option[] = [
  { label: '640 (fast; small objects may be missed)', value: '640' },
  { label: '960', value: '960' },
  { label: '1280 (recommended for high-resolution captures)', value: '1280' },
  { label: '1600 (slow; large-memory GPU)', value: '1600' },
];

const RFDETR_SIZE_OPTIONS: SelectProps.Option[] = RFDETR_SIZE_ORDER.map(size => ({
  label: `${size} (native ${RFDETR_SIZES[size]}px)${size === 'small' ? ' — recommended' : ''}`,
  value: size,
  description: size === 'medium' || size === 'large'
    ? 'Larger backbone; ml.g5.xlarge recommended.'
    : 'Fits the documented ml.g4dn.xlarge (T4) configuration.',
}));
// RF-DETR-Seg sizes (rfdetr-seg-support Req 12.2; all six, D4). The native
// resolution in the label; the per-size instance default (Req 5.3) in the
// description.
const RFDETR_SEG_SIZE_OPTIONS: SelectProps.Option[] = RFDETR_SEG_SIZE_ORDER.map(size => ({
  label: `${size} (native ${RFDETR_SEG_SIZES[size]}px)${size === 'small' ? ' — recommended' : ''}`,
  value: size,
  description: defaultInstanceTypeForSegSize(size) === 'ml.g5.xlarge'
    ? 'Larger backbone; ml.g5.xlarge recommended.'
    : 'Fits the documented ml.g4dn.xlarge (T4) configuration.',
}));
// How a Seg model's masks are packaged for the device (rfdetr-seg-support
// D1): sent as the request's top-level `output_mode`.
const OUTPUT_MODE_OPTIONS: SelectProps.Option[] = [
  {
    label: 'instance',
    value: 'instance',
    description: 'One mask per detected object, beside its box and class (Detection_List with masks).',
  },
  {
    label: 'semantic overlay',
    value: 'semantic',
    description: 'All instances merged into one class-coloured mask over the frame.',
  },
];
// Client-side mirror of the backend's manifest-kind checks for the Seg form
// (rfdetr-seg-support Req 3.5): never "must be transformed".
const SEG_NEEDS_INSTANCE_BOXES_ONLY =
  'RF-DETR-Seg needs an instance-segmentation manifest (one outline per object); this manifest has boxes only';
const SEG_NEEDS_INSTANCE_SEMANTIC =
  'RF-DETR-Seg needs an instance-segmentation manifest (one outline per object); this manifest is a semantic or classification manifest';
const DETECTION_GOT_INSTANCE_MANIFEST =
  'This is an instance-segmentation manifest; choose Instance Segmentation (RF-DETR-Seg)';
const LFV_GOT_INSTANCE_MANIFEST =
  'This is an instance-segmentation manifest — select the RF-DETR model source with the Instance Segmentation model type to train from it';

// Detection jobs run on a plain SageMaker PyTorch GPU DLC, so any GPU instance
// works (the marketplace list below is constrained by the LFV algorithm).
const DETECTION_INSTANCE_TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'ml.g4dn.xlarge (GPU - Recommended)', value: 'ml.g4dn.xlarge' },
  { label: 'ml.g4dn.2xlarge (GPU - More compute)', value: 'ml.g4dn.2xlarge' },
  { label: 'ml.g5.xlarge (GPU - A10G)', value: 'ml.g5.xlarge' },
  { label: 'ml.g5.2xlarge (GPU - A10G, more compute)', value: 'ml.g5.2xlarge' },
];
const DETECTION_DEFAULT_MAX_RUNTIME = '10800';

/** The instance option for a type name (falls back to a bare option so the value is still sent). */
function detectionInstanceOption(value: string): SelectProps.Option {
  return DETECTION_INSTANCE_TYPE_OPTIONS.find(o => o.value === value) ?? { label: value, value };
}

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
  const [detectionParams, setDetectionParams] = useState<DetectionFormParams>(YOLO_PARAM_DEFAULTS);
  // RF-DETR-Seg only (rfdetr-seg-support Req 5.5): how the masks are packaged.
  const [outputMode, setOutputMode] = useState<OutputMode>('instance');
  // Base model (Req 6): null = "the published checkpoint the form names"
  // (YOLO base weights / RF-DETR size); otherwise a prior training job or an
  // imported fine-tunable checkpoint in this use case.
  const [baseModel, setBaseModel] = useState<BaseModelRef | null>(null);
  const [baseModelGroups, setBaseModelGroups] = useState<DetectionBaseModels | null>(null);
  const [baseModelsLoading, setBaseModelsLoading] = useState(false);
  // Optional class-name override (comma-separated). Empty = the manifest's
  // class-map; picking a base model prefills its classes when empty.
  const [classNamesInput, setClassNamesInput] = useState('');
  // Class names read from the selected manifest's class-map (for the
  // base-model mismatch warning).
  const [manifestClassNames, setManifestClassNames] = useState<string[]>([]);
  // True once the user picked an instance type themselves; the per-arch
  // default nudges (g4dn.xlarge / g5.xlarge for RF-DETR medium+) stop then.
  const instanceTouched = useRef(false);
  const [checkingManifestFormat, setCheckingManifestFormat] = useState(false);
  const [showTransformModal, setShowTransformModal] = useState(false);
  const [transforming, setTransforming] = useState(false);
  const [transformError, setTransformError] = useState<string | null>(null);
  const [transformedManifestUri, setTransformedManifestUri] = useState<string | null>(null);

  // Model types the selected source can train (LFV four, Object Detection, or
  // for RF-DETR also Instance Segmentation unless the Seg switch is off).
  const modelTypeOptions = visibleModelTypeOptionsForSource(modelSource.value, getConfig());

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
  // Detector family the selected source + model type trains (the request's
  // `detection_arch`); only meaningful when isDetection. The RF-DETR source
  // trains rf_detr for Object Detection and rf_detr_seg for Instance
  // Segmentation (rfdetr-seg-support Req 12.2).
  const detectionArch: DetectionArch = detectionArchFor(modelSource.value, modelType.value) ?? 'yolo';
  const isRfDetr = isDetection && detectionArch === 'rf_detr';
  const isRfDetrSeg = isDetection && detectionArch === 'rf_detr_seg';
  /** The size the per-arch instance default keys on: the Seg size for a Seg form, the RF-DETR size otherwise. */
  const archSize = (arch: DetectionArch, p: DetectionFormParams) =>
    arch === 'rf_detr_seg' ? p.rfdetrSegSize : p.rfdetrSize;

  /**
   * Select a model type and apply the defaults that go with it: max runtime,
   * the seghead toggle (LFV segmentation only), and — because the marketplace
   * algorithm and the detection DLC accept different instance lists — the
   * instance type when crossing the detection boundary. Crossing between
   * detector families (Object Detection <-> Instance Segmentation under the
   * RF-DETR source, or entering detection) resets Detection Settings to the
   * arch's defaults and drops the base model: a detection checkpoint cannot
   * seed a Seg run, and the rf_detr size / resolution are not Seg values.
   * `source` is the (possibly just chosen) model source the type belongs to.
   */
  const applyModelType = (option: SelectProps.Option, source: unknown = modelSource.value) => {
    const nextValue = option.value as string | undefined;
    const wasDetection = isDetection;
    const nextIsDetection = isDetectionModelType(nextValue);
    const nextArch: DetectionArch = detectionArchFor(source, nextValue) ?? 'yolo';
    setModelType(option);
    const isLfvSegmentation = isLfvSegmentationType(nextValue);
    const isRobust = !!nextValue?.includes('robust');
    setMaxRuntime(
      nextIsDetection ? DETECTION_DEFAULT_MAX_RUNTIME
        : isRobust ? '86400' : isLfvSegmentation ? '7200' : '3600'
    );
    if (!isLfvSegmentation) setSegheadOnly(false);
    let nextParams = detectionParams;
    if (nextIsDetection && (!wasDetection || nextArch !== detectionArch)) {
      nextParams = detectionParamDefaults(nextArch);
      setDetectionParams(nextParams);
      setBaseModel(null);
    }
    if (nextIsDetection && !wasDetection) {
      instanceTouched.current = false;
      setInstanceType(detectionInstanceOption(defaultDetectionInstanceType(nextArch, archSize(nextArch, nextParams))));
    } else if (!nextIsDetection && wasDetection) {
      instanceTouched.current = false;
      setInstanceType(instanceTypeOptions[0]);
    }
  };

  /**
   * Select a model source and move Model Type to that source's first option.
   * Crossing between the two detector families resets Detection Settings to
   * that arch's defaults and drops the base model (a YOLO checkpoint cannot
   * seed an RF-DETR run, and vice versa).
   */
  const applyModelSource = (option: SelectProps.Option) => {
    setModelSource(option);
    const nextArch = detectionArchForSource(option.value);
    if (nextArch && nextArch !== detectionArchForSource(modelSource.value)) {
      setDetectionParams(detectionParamDefaults(nextArch));
      setBaseModel(null);
    }
    const allowed = visibleModelTypeOptionsForSource(option.value, getConfig());
    if (!allowed.some(o => o.value === modelType.value)) {
      applyModelType(allowed[0], option.value);
    }
  };

  // Base model choices for this use case + arch (Req 6.1). Reloading drops
  // any selection: a ref from another use case / arch would be a 400.
  useEffect(() => {
    setBaseModel(null);
    if (!useCaseId?.value || !isDetection) {
      setBaseModelGroups(null);
      return;
    }
    let cancelled = false;
    setBaseModelsLoading(true);
    apiService.listDetectionBaseModels(useCaseId.value as string, detectionArch)
      .then(groups => { if (!cancelled) setBaseModelGroups(groups); })
      .catch(err => {
        console.error('Failed to list base models:', err);
        // Published checkpoints are static; still offer them.
        if (!cancelled) setBaseModelGroups(groupDetectionBaseModels([], detectionArch));
      })
      .finally(() => { if (!cancelled) setBaseModelsLoading(false); });
    return () => { cancelled = true; };
  }, [useCaseId?.value, isDetection, detectionArch]);

  // Instance default nudge (Req 3.5): ml.g4dn.xlarge for YOLO and RF-DETR
  // nano/small; ml.g5.xlarge for RF-DETR medium/large and RF-DETR-Seg
  // large/xlarge/2xlarge — until the user picks an instance type themselves.
  useEffect(() => {
    if (!isDetection || instanceTouched.current) return;
    const wanted = defaultDetectionInstanceType(detectionArch, archSize(detectionArch, detectionParams));
    setInstanceType(prev => (prev.value === wanted ? prev : detectionInstanceOption(wanted)));
  }, [isDetection, detectionArch, detectionParams.rfdetrSize, detectionParams.rfdetrSegSize]);

  const baseModelOptions = baseModelGroups
    ? baseModelSelectOptions(baseModelGroups)
    : baseModelSelectOptions(groupDetectionBaseModels([], detectionArch));
  const effectiveBaseModel: BaseModelRef = baseModel ?? publishedBaseModelRef(detectionArch, detectionParams);
  const selectedBaseModelOption = findBaseModelOption(baseModelOptions, effectiveBaseModel);
  const baseRecord = findBaseModelRecord(baseModelGroups, baseModel);
  const baseClasses = baseModelClassNames(baseRecord);
  // The run's class list: the override when given, else the manifest's class-map.
  const overrideClassNames = parseClassNamesInput(classNamesInput);
  const runClassNames = overrideClassNames.length > 0 ? overrideClassNames : manifestClassNames;
  const classMismatch = !!baseClasses && runClassNames.length > 0 && classListsDiffer(runClassNames, baseClasses);

  /** Base model picked: published -> drives base weights / size; otherwise a record (Req 6.2). */
  const handleBaseModelChange = (option: SelectProps.Option) => {
    const ref = parseBaseModelOptionValue(option.value);
    if (!ref) return;
    if (ref.kind === 'published') {
      setBaseModel(null);
      setDetectionParams(p =>
        detectionArch === 'rf_detr'
          ? (isRfDetrSize(ref.ref) ? applyRfDetrSize(p, ref.ref) : p)
          : detectionArch === 'rf_detr_seg'
            ? (isRfDetrSegSize(ref.ref) ? applyRfDetrSegSize(p, ref.ref) : p)
            : { ...p, baseWeights: ref.ref }
      );
      return;
    }
    setBaseModel(ref);
    const record = findBaseModelRecord(baseModelGroups, ref);
    // Default the network input (and RF-DETR size) to what the base was trained at.
    setDetectionParams(p => paramsFromBaseRecord(detectionArch, p, record));
    const classes = baseModelClassNames(record);
    if (classes && !classNamesInput.trim()) setClassNamesInput(classes.join(', '));
  };

  // Populate form from cloned job
  useEffect(() => {
    if (cloneFrom) {
      // Set model name with a suffix to indicate it's a clone
      setModelName(cloneFrom.model_name ? `${cloneFrom.model_name}-clone` : '');
      
      // Set model source + type (the source is derived from the type: a
      // detection job clones onto its arch's source — YOLO or RF-DETR —
      // anything else onto the marketplace).
      if (cloneFrom.model_type) {
        const typeOption = ALL_MODEL_TYPE_OPTIONS.find(opt => opt.value === cloneFrom.model_type);
        if (typeOption) {
          const sourceOption = MODEL_SOURCE_OPTIONS.find(
            opt => opt.value === modelSourceForType(cloneFrom.model_type, cloneFrom.detection_arch)
          );
          if (sourceOption) setModelSource(sourceOption);
          setModelType(typeOption);
          // The pair names the arch: a cloned Seg job takes the Seg defaults.
          const clonedArch = detectionArchFor(sourceOption?.value, cloneFrom.model_type);
          if (clonedArch) setDetectionParams(detectionParamDefaults(clonedArch));
        }
      }
      
      // Set instance type (an explicit choice: the per-arch nudge must not override it)
      if (cloneFrom.instance_type) {
        const instanceOption = flatInstanceOptions.find(opt => opt.value === cloneFrom.instance_type);
        if (instanceOption) {
          instanceTouched.current = true;
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
      setManifestClassNames(classNamesFromManifestEntry(sampleEntry));
    } catch (err) {
      console.error('Failed to check manifest format:', err);
      setManifestFormat('unknown');
      setManifestClassNames([]);
    } finally {
      setCheckingManifestFormat(false);
    }
  };

  const handleLabelingJobSelect = async (option: SelectProps.Option | null) => {
    setSelectedLabelingJob(option);
    setManifestFormat(null);
    setManifestClassNames([]);
    setTransformedManifestUri(null);
    
    if (option?.value) {
      await checkManifestFormat(option.value as string);
    }
  };

  const handlePreLabeledDatasetSelect = async (option: SelectProps.Option | null) => {
    setSelectedPreLabeledDataset(option);
    setManifestFormat(null);
    setManifestClassNames([]);
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
            // Bounding-box and instance-segmentation jobs never need the DDA
            // transform; for detection the "Transformed" badge would be noise
            // (and wrong).
            const isBboxJob = String(job.task_type || '').toLowerCase().includes('detection');
            const isInstanceJob = isInstanceSegmentationTaskType(job.task_type);
            const isDetectionFamilyJob = isBboxJob || isInstanceJob;
            const suffix = isDetection || isDetectionFamilyJob
              ? (isBboxJob ? ' • Bounding boxes' : isInstanceJob ? ' • Instance masks' : '')
              : (job.is_transformed ? ' ✓ Transformed' : ' ⚠️ Not Transformed');
            const desc = isDetection || isDetectionFamilyJob
              ? (isBboxJob ? ' • Object detection labels'
                : isInstanceJob ? ' • Instance segmentation labels'
                  : ` • ${job.task_type || 'labels'}`)
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
              // An InstanceSegmentation job trains only under the RF-DETR
              // source's Instance Segmentation type (the source alone defaults
              // to Object Detection); applied only while the type is offered.
              if (isInstanceSegmentationTaskType(job.task_type)) {
                const segOption = visibleModelTypeOptionsForSource(wantedSource, getConfig())
                  .find(o => o.value === INSTANCE_SEGMENTATION_MODEL_TYPE);
                if (segOption && modelType.value !== segOption.value) applyModelType(segOption, wantedSource);
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
        task_type: isLfvSegmentationType(modelType.value) ? 'segmentation' : 'classification',
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

      // Detection sends its arch's settings as `hyperparameters` (validated by
      // the backend) plus `detection_arch` and `base_model` (Req 3.2, 6.3);
      // LFV sends only the seghead flag when toggled. The two never mix (the
      // seghead toggle is hidden for detection).
      const detectionFields = isDetection
        ? buildDetectionSubmitFields(detectionArch, detectionParams, baseModel, overrideClassNames)
        : null;
      const hyperparameters = detectionFields
        ? detectionFields.hyperparameters
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
        ...(detectionFields && {
          detection_arch: detectionFields.detection_arch,
          base_model: detectionFields.base_model,
          ...(detectionFields.class_names && { class_names: detectionFields.class_names }),
        }),
        // RF-DETR-Seg only (Req 5.5): how the masks are packaged.
        ...(isRfDetrSeg && { output_mode: outputMode }),
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
    // anomaly labels, the LFV algorithm cannot read bounding boxes, and
    // RF-DETR-Seg needs instance masks (rfdetr-seg-support Req 3.5; an
    // instance manifest is never "transformed").
    if (isRfDetrSeg && (manifestFormat === 'dda' || manifestFormat === 'ground-truth')) {
      errors.push(SEG_NEEDS_INSTANCE_SEMANTIC);
    } else if (isDetection && (manifestFormat === 'dda' || manifestFormat === 'ground-truth')) {
      errors.push('Object Detection requires a bounding-box manifest (this manifest has classification/segmentation labels)');
    }
    if (isRfDetrSeg && manifestFormat === 'detection') {
      errors.push(SEG_NEEDS_INSTANCE_BOXES_ONLY);
    }
    if (!isDetection && manifestFormat === 'detection') {
      errors.push('This is a bounding-box manifest — select the YOLO or RF-DETR model source to train from it');
    }
    if (isDetection && !isRfDetrSeg && manifestFormat === 'instance') {
      errors.push(DETECTION_GOT_INSTANCE_MANIFEST);
    }
    if (!isDetection && manifestFormat === 'instance') {
      errors.push(LFV_GOT_INSTANCE_MANIFEST);
    }
    if (isDetection) {
      errors.push(...validateDetectionParams(detectionArch, detectionParams));
      // A non-published base must still be on offer (the list reloads per
      // use case / arch); otherwise the API would answer 400.
      if (baseModel && baseModelGroups && !baseRecord) {
        errors.push('The selected base model is not available for this use case and model source');
      }
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
                {isRfDetrSeg ? (
                  <>
                    Instance Segmentation fine-tunes an RF-DETR-Seg model (DINOv2 backbone, NMS-free, with a
                    mask head) on an instance-segmentation manifest (the output of an Instance Segmentation
                    labeling job) and exports ONNX for the DDA edge runtime. The model trains on a square resize
                    with ImageNet normalisation and is served exactly the same way; each kept detection carries a
                    binary mask. It needs no compilation step.
                  </>
                ) : isRfDetr ? (
                  <>
                    Object Detection fine-tunes an RF-DETR detector (DINOv2 backbone, NMS-free) on a
                    bounding-box manifest (the output of a bounding-box labeling job) and exports ONNX for
                    the DDA edge runtime. The model trains on a square resize with ImageNet normalisation
                    and is served exactly the same way; it needs no compilation step.
                  </>
                ) : (
                  <>
                    Object Detection fine-tunes a YOLO detector on a bounding-box manifest (the output of a
                    bounding-box labeling job) and exports ONNX for the DDA edge runtime. The model is served
                    letterboxed exactly as it was trained, and needs no compilation step.
                  </>
                )}
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

            {isRfDetrSeg ? (
              <Alert type="info">
                <Box variant="h4">Manifest Requirements</Box>
                <Box variant="p">
                  Instance Segmentation trains from an instance-segmentation manifest with these attributes:
                </Box>
                <ul style={{ marginLeft: '20px' }}>
                  <li><code>source-ref</code> - Image S3 URI</li>
                  <li><code>instance-segmentation</code> - One outline per object (<code>annotations</code> with a box and an <code>rle</code> mask, <code>image_size</code>)</li>
                  <li><code>instance-segmentation-metadata</code> - <code>class-map</code> naming each class id</li>
                </ul>
                <Box variant="p">
                  The output of an Instance Segmentation labeling job is accepted as-is; a bounding-box or semantic
                  manifest cannot be used, and no transform is offered.
                </Box>
              </Alert>
            ) : isDetection ? (
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
                  : modelSource.value === MODEL_SOURCE_RF_DETR
                    ? 'RF-DETR trains bounding-box object detectors and instance-segmentation models (masks + boxes)'
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
                  <FormField
                    label="Base model"
                    description={
                      isRfDetrSeg
                        ? 'Weights the run starts from: a published RF-DETR-Seg COCO checkpoint, one of your completed RF-DETR-Seg models in this use case, or an imported fine-tunable RF-DETR-Seg checkpoint.'
                        : isRfDetr
                          ? 'Weights the run starts from: a published RF-DETR COCO checkpoint, one of your completed RF-DETR detectors in this use case, or an imported fine-tunable checkpoint.'
                          : 'Weights the run starts from: a published YOLO COCO checkpoint, one of your completed YOLO detectors in this use case, or an imported fine-tunable checkpoint.'
                    }
                    stretch
                  >
                    <Select
                      selectedOption={selectedBaseModelOption}
                      onChange={({ detail }) => handleBaseModelChange(detail.selectedOption)}
                      options={baseModelOptions}
                      statusType={baseModelsLoading ? 'loading' : 'finished'}
                      loadingText="Loading your detectors"
                      placeholder="Select a base model"
                      selectedAriaLabel="Selected"
                      filteringType="auto"
                    />
                  </FormField>
                  {classMismatch && baseClasses && (
                    <Alert type="warning">{classMismatchWarning(baseClasses)}</Alert>
                  )}
                  <ColumnLayout columns={2}>
                    {isRfDetrSeg ? (
                      <>
                        <FormField
                          label="Size"
                          description={
                            baseModel
                              ? 'Fixed by the selected base model (its checkpoint is this size).'
                              : 'RF-DETR-Seg checkpoint family (all six sizes). Larger sizes are more accurate and slower on device.'
                          }
                        >
                          <Select
                            selectedOption={
                              RFDETR_SEG_SIZE_OPTIONS.find(o => o.value === detectionParams.rfdetrSegSize) ??
                              { label: detectionParams.rfdetrSegSize, value: detectionParams.rfdetrSegSize }
                            }
                            onChange={({ detail }) => {
                              const size = detail.selectedOption.value;
                              if (isRfDetrSegSize(size)) setDetectionParams(p => applyRfDetrSegSize(p, size));
                            }}
                            options={RFDETR_SEG_SIZE_OPTIONS}
                            disabled={!!baseModel}
                            selectedAriaLabel="Selected"
                          />
                        </FormField>
                        <FormField
                          label="Resolution"
                          description={`Square input the model trains and exports at; multiple of ${RFDETR_SEG_INPUT_BLOCK[detectionParams.rfdetrSegSize]}, ${RFDETR_SEG_RESOLUTION_BOUNDS.min}–${RFDETR_SEG_RESOLUTION_BOUNDS.max}. Native for ${detectionParams.rfdetrSegSize}: ${rfdetrSegNativeResolution(detectionParams.rfdetrSegSize)}.`}
                        >
                          <Input
                            type="number"
                            step={RFDETR_SEG_INPUT_BLOCK[detectionParams.rfdetrSegSize]}
                            value={detectionParams.resolution}
                            onChange={({ detail }) => setDetectionParams(p => ({ ...p, resolution: detail.value }))}
                          />
                        </FormField>
                      </>
                    ) : isRfDetr ? (
                      <>
                        <FormField
                          label="Size"
                          description={
                            baseModel
                              ? 'Fixed by the selected base model (its checkpoint is this size).'
                              : 'RF-DETR checkpoint family. Larger sizes are more accurate and slower on device.'
                          }
                        >
                          <Select
                            selectedOption={
                              RFDETR_SIZE_OPTIONS.find(o => o.value === detectionParams.rfdetrSize) ??
                              { label: detectionParams.rfdetrSize, value: detectionParams.rfdetrSize }
                            }
                            onChange={({ detail }) => {
                              const size = detail.selectedOption.value;
                              if (isRfDetrSize(size)) setDetectionParams(p => applyRfDetrSize(p, size));
                            }}
                            options={RFDETR_SIZE_OPTIONS}
                            disabled={!!baseModel}
                            selectedAriaLabel="Selected"
                          />
                        </FormField>
                        <FormField
                          label="Resolution"
                          description={`Square input the detector trains and exports at; multiple of 32, 224–1120. Native for ${detectionParams.rfdetrSize}: ${rfdetrNativeResolution(detectionParams.rfdetrSize)}.`}
                        >
                          <Input
                            type="number"
                            step={32}
                            value={detectionParams.resolution}
                            onChange={({ detail }) => setDetectionParams(p => ({ ...p, resolution: detail.value }))}
                          />
                        </FormField>
                      </>
                    ) : (
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
                    )}
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
                    {(isRfDetr || isRfDetrSeg) && (
                      <>
                        <FormField
                          label="Gradient accumulation"
                          description="Steps accumulated before each optimizer update (effective batch = batch × this)."
                        >
                          <Input
                            type="number"
                            value={detectionParams.gradAccum}
                            onChange={({ detail }) => setDetectionParams(p => ({ ...p, gradAccum: detail.value }))}
                          />
                        </FormField>
                        <FormField label="Learning rate" description="Optimizer learning rate (0–1).">
                          <Input
                            type="number"
                            step={0.00005}
                            value={detectionParams.lr}
                            onChange={({ detail }) => setDetectionParams(p => ({ ...p, lr: detail.value }))}
                          />
                        </FormField>
                      </>
                    )}
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
                    {isRfDetrSeg && (
                      <>
                        <FormField label="Mask threshold" description="Mask logit cut-off that binarises each kept detection's mask on device (0–1).">
                          <Input
                            type="number"
                            step={0.05}
                            value={detectionParams.maskThreshold}
                            onChange={({ detail }) => setDetectionParams(p => ({ ...p, maskThreshold: detail.value }))}
                          />
                        </FormField>
                        <FormField
                          label="Output"
                          description="How the masks are packaged for the device: one mask per object, or one semantic overlay. Can be overridden when packaging."
                        >
                          <Select
                            selectedOption={OUTPUT_MODE_OPTIONS.find(o => o.value === outputMode) ?? OUTPUT_MODE_OPTIONS[0]}
                            onChange={({ detail }) => setOutputMode((detail.selectedOption.value as OutputMode) ?? 'instance')}
                            options={OUTPUT_MODE_OPTIONS}
                            selectedAriaLabel="Selected"
                          />
                        </FormField>
                      </>
                    )}
                    {detectionArch === 'yolo' && (
                      <FormField label="IoU threshold" description="Non-max-suppression overlap threshold (0–1).">
                        <Input
                          type="number"
                          step={0.05}
                          value={detectionParams.iouThreshold}
                          onChange={({ detail }) => setDetectionParams(p => ({ ...p, iouThreshold: detail.value }))}
                        />
                      </FormField>
                    )}
                  </ColumnLayout>
                  <FormField
                    label="Class names"
                    description="Optional, comma-separated, in class-id order. Leave empty to use the manifest's class-map; choosing a trained base model prefills its classes."
                    stretch
                  >
                    <Input
                      value={classNamesInput}
                      onChange={({ detail }) => setClassNamesInput(detail.value)}
                      placeholder={manifestClassNames.length > 0 ? manifestClassNames.join(', ') : 'e.g., plate, luggage'}
                    />
                  </FormField>
                  <Box variant="small" color="text-status-inactive">
                    {isRfDetrSeg
                      ? 'The trained model is served with a square resize and ImageNet normalisation to match training; decoding is set-based (top-k, no NMS) and each kept detection carries a binary mask.'
                      : isRfDetr
                        ? 'The trained model is served with a square resize and ImageNet normalisation to match training; decoding is set-based (top-k, no NMS).'
                        : 'The trained model is served with aspect-preserving (letterbox) preprocessing to match training.'}
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

            {isLfvSegmentationType(modelType.value) && (
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

            {/* `!!`: an empty dataPath would otherwise become a '' child that
                SpaceBetween wraps in an (unkeyed) empty spacer div. */}
            {!!dataPath && dataPathMatch === 'matched' && (
              <Alert type="success" dismissible onDismiss={() => setDataPathMatch(null)}>
                Using the labeling job that labeled <code>{dataPath}</code>. Model Source was set to
                match its labels; change it if you meant something else.
              </Alert>
            )}
            {!!dataPath && dataPathMatch === 'none' && (
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
                {isRfDetrSeg
                  ? SEG_NEEDS_INSTANCE_SEMANTIC
                  : isDetection
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
              <Alert type={isDetection && !isRfDetrSeg ? 'success' : 'error'} dismissible onDismiss={() => setManifestFormat(null)}>
                {isRfDetrSeg
                  ? SEG_NEEDS_INSTANCE_BOXES_ONLY
                  : isDetection
                    ? `✓ Bounding-box manifest detected — ready for Object Detection training (no transform needed)${manifestClassNames.length > 0 ? `. Classes: ${manifestClassNames.join(', ')}` : ''}`
                    : 'This is a bounding-box manifest. Select the YOLO or RF-DETR model source to train from it.'}
              </Alert>
            )}
            {manifestFormat === 'instance' && (
              <Alert type={isRfDetrSeg ? 'success' : 'error'} dismissible onDismiss={() => setManifestFormat(null)}>
                {isRfDetrSeg
                  ? `✓ Instance-segmentation manifest detected — ready for RF-DETR-Seg training (no transform needed)${manifestClassNames.length > 0 ? `. Classes: ${manifestClassNames.join(', ')}` : ''}`
                  : isDetection
                    ? DETECTION_GOT_INSTANCE_MANIFEST
                    : LFV_GOT_INSTANCE_MANIFEST}
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
                onChange={({ detail }) => {
                  instanceTouched.current = true;
                  setInstanceType(detail.selectedOption);
                }}
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
              {isDetection && (
                <Box>
                  <Box variant="awsui-key-label">Base model</Box>
                  <Box>{selectedBaseModelOption?.label ?? effectiveBaseModel.ref}</Box>
                </Box>
              )}
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
