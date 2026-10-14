import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Button,
  Select,
  SelectProps,
  Alert,
  Box,
  Tiles,
  ColumnLayout,
  StatusIndicator,
  Spinner,
  Checkbox,
  Multiselect,
  MultiselectProps,
  ExpandableSection,
  Badge,
  Link,
  SegmentedControl,
  FileUpload,
  ProgressBar,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import type { CheckpointAssessment } from '../services/api';
import { validateS3Uri } from '../utils/s3Validation';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';
import {
  CHECKPOINT_SIZE_CAP_BYTES,
  CONVERTIBLE_VERDICT,
  DEFAULT_IOU_THRESHOLD,
  NOT_CONVERTIBLE_VERDICT,
  UPLOAD_EXTENSIONS,
  checkpointFamilyLabel,
  checkpointLibraryLabel,
  conversionLocksFor,
  formatBytes,
  networkInputProblem,
  putFileWithProgress,
  thresholdProblem,
  uploadFileProblem,
  validateClassNames,
} from '../utils/detectorConversion';
import type { ConversionLocks } from '../utils/detectorConversion';
import { COMPILATION_TARGET_OPTIONS } from '../utils/compilationTargets';

const FILE_UPLOAD_I18N = {
  uploadButtonText: () => 'Choose file',
  dropzoneText: () => 'Drop a model file to upload',
  removeFileAriaLabel: (index: number) => `Remove file ${index + 1}`,
  errorIconAriaLabel: 'Error',
};

/**
 * The Checkpoint panel (detector-checkpoint-import Requirement 10.2): what
 * the probe read from a `.pt` / `.pth` without running it, and whether it
 * can be converted to ONNX.
 */
function CheckpointPanel({ assessment, fineTunable }: { assessment: CheckpointAssessment; fineTunable: boolean }) {
  const names = Array.isArray(assessment.class_names) ? assessment.class_names : [];
  const library = checkpointLibraryLabel(assessment);
  return (
    <Container header={<Header variant="h3">Checkpoint</Header>} data-testid="checkpoint-panel">
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Family</Box>
            <div data-testid="checkpoint-family">{checkpointFamilyLabel(assessment)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Task</Box>
            <div>{assessment.task || '—'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Saved by</Box>
            <div data-testid="checkpoint-library">{library || '—'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Classes</Box>
            <div data-testid="checkpoint-class-count">{assessment.num_classes ?? '—'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Training input size</Box>
            <div data-testid="checkpoint-input-size">
              {assessment.train_input_size ? `${assessment.train_input_size} px` : 'Not recorded'}
            </div>
          </div>
          <div>
            <Box variant="awsui-key-label">Fine-tunable</Box>
            <div>{fineTunable ? 'Yes (the checkpoint is kept as a base model)' : 'No'}</div>
          </div>
        </ColumnLayout>
        <div>
          <Box variant="awsui-key-label">Class names (index order)</Box>
          <div data-testid="checkpoint-class-names">
            {names.length > 0 ? names.join(', ') : 'Not stored in the checkpoint'}
          </div>
        </div>
        <div data-testid="checkpoint-verdict">
          {assessment.convertible ? (
            <StatusIndicator type="success">{CONVERTIBLE_VERDICT}</StatusIndicator>
          ) : (
            <SpaceBetween size="xxs">
              <StatusIndicator type="error">{NOT_CONVERTIBLE_VERDICT}</StatusIndicator>
              <ul data-testid="checkpoint-reasons">
                {(assessment.reasons || []).map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            </SpaceBetween>
          )}
        </div>
      </SpaceBetween>
    </Container>
  );
}

/**
 * One name per class index; the count is the checkpoint's head and cannot
 * change, only the names can (Requirement 10.3).
 */
function ClassNameEditor({ names, onChange }: { names: string[]; onChange: (names: string[]) => void }) {
  return (
    <ColumnLayout columns={names.length > 8 ? 4 : 2}>
      {names.map((name, i) => (
        <FormField key={i} label={`Class ${i}`}>
          <Input
            value={name}
            ariaLabel={`Class ${i} name`}
            invalid={!name.trim()}
            onChange={({ detail }) => onChange(names.map((n, j) => (j === i ? detail.value : n)))}
          />
        </FormField>
      ))}
    </ColumnLayout>
  );
}

interface ModelInspectionResult {
  type: string;
  is_state_dict?: boolean;
  is_jit?: boolean;
  is_full_model?: boolean;
  layers?: string[];
  total_layers?: number;
  input_channels?: number;
  num_classes?: number;
  architecture_hints: string[];
  suggested_type?: string;
  error?: string;
  // ONNX auto-detected attributes (from graph input/output shapes).
  detection_arch?: string;      // 'yolo' | 'rf_detr'
  input_width?: number | null;
  input_height?: number | null;
  num_outputs?: number;
  input_shapes?: (number | null)[][];
  output_shapes?: (number | null)[][];
  // .pt / .pth sources: the Checkpoint_Probe's pre-flight (Requirement 2).
  class_names?: string[] | null;
  checkpoint?: CheckpointAssessment;
  fine_tunable?: boolean;
}

const COMMON_DIMENSIONS: Record<string, { label: string; value: string }[]> = {
  classification: [
    { label: '224x224 (ResNet, VGG)', value: '224' },
    { label: '256x256', value: '256' },
    { label: '299x299 (Inception)', value: '299' },
    { label: '384x384 (EfficientNet)', value: '384' },
    { label: '512x512', value: '512' },
  ],
  object_detection: [
    { label: '320x320', value: '320' },
    { label: '416x416 (YOLOv3)', value: '416' },
    { label: '512x512', value: '512' },
    { label: '640x640 (YOLOv5/v8/v10)', value: '640' },
    { label: '1280x1280', value: '1280' },
  ],
  segmentation: [
    { label: '256x256', value: '256' },
    { label: '512x512', value: '512' },
    { label: '768x768', value: '768' },
    { label: '1024x1024', value: '1024' },
  ],
  anomaly_detection: [
    { label: '224x224', value: '224' },
    { label: '256x256', value: '256' },
    { label: '512x512', value: '512' },
  ],
};

export default function SmartImport() {
  const navigate = useNavigate();
  
  // Step tracking
  const [currentStep, setCurrentStep] = useState(1);
  
  // Form state
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [modelS3Uri, setModelS3Uri] = useState('');
  const [modelName, setModelName] = useState('');
  const [modelType, setModelType] = useState<string>('classification');
  const [imageDimension, setImageDimension] = useState<string>('224');
  const [customWidth, setCustomWidth] = useState('');
  const [customHeight, setCustomHeight] = useState('');
  const [useCustomDimensions, setUseCustomDimensions] = useState(false);
  const [numClasses, setNumClasses] = useState('');
  // Export/runtime format: 'pytorch' (legacy .pt/DLR) or 'onnx' (ONNX Runtime
  // engine; required for the object-detection task path).
  const [exportFormat, setExportFormat] = useState<string>('pytorch');
  // Object-detection decoder family: 'yolo' (single tensor + NMS) or 'rf_detr'
  // (DETR-family, two tensors, NMS-free top-k). Only relevant for detection.
  const [detectionArch, setDetectionArch] = useState<string>('yolo');
  // Letterbox by default: detectors exported from ultralytics (including this
  // repo's datasets/detection_training/train.py) are trained letterboxed, and
  // serving them squashed silently costs confidence. Untick for a model that
  // was genuinely trained on squashed input.
  const [preserveAspect, setPreserveAspect] = useState(true);
  // Detection decode settings. Without class names the on-device postprocessor
  // labels every box with its numeric class id ("0"), which downstream
  // consumers matching on a label string will never match. The thresholds
  // default to the same values the backend applies.
  const [classNames, setClassNames] = useState('');
  const [scoreThreshold, setScoreThreshold] = useState('0.25');
  const [iouThreshold, setIouThreshold] = useState('0.45');
  const [autoCompile, setAutoCompile] = useState(true);
  const [compilationTargets, setCompilationTargets] = useState<MultiselectProps.Option[]>([
    { label: 'x86_64 CPU', value: 'x86_64-cpu' }
  ]);
  
  // Model source: an S3 URI, or a file uploaded from this machine to a
  // server-issued key in the use case's bucket (Requirement 10.1).
  const [sourceMode, setSourceMode] = useState<'s3' | 'upload'>('s3');
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadedUri, setUploadedUri] = useState<string | null>(null);

  // Inspection state
  const [inspecting, setInspecting] = useState(false);
  const [inspectionResult, setInspectionResult] = useState<ModelInspectionResult | null>(null);

  // Checkpoint conversion (a Convertible_Checkpoint): one name per class
  // index, count locked, and the square network input.
  const [conversionClassNames, setConversionClassNames] = useState<string[]>([]);
  const [networkInput, setNetworkInput] = useState('');
  
  // Conversion state
  const [converting, setConverting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // A .pt / .pth source carries the probe's pre-flight. Convertible: the form
  // is locked to an ONNX detector conversion. Not convertible: ONNX output is
  // unavailable (the server would reject it) and PyTorch / Neo stays.
  const checkpoint = inspectionResult?.checkpoint ?? null;
  const locks: ConversionLocks | null = conversionLocksFor(checkpoint);
  const onnxBlocked = !!checkpoint && !locks;

  // Segmentation is supported on-device only via the RF-DETR ONNX decoder
  // (instance masks -> semantic overlay). Lock the architecture to RF-DETR and
  // the runtime to ONNX when the user selects Segmentation -- unless the
  // source is a checkpoint that cannot produce ONNX, which keeps PyTorch/Neo.
  useEffect(() => {
    if (modelType === 'segmentation' && !onnxBlocked) {
      setDetectionArch('rf_detr');
      setExportFormat('onnx');
    }
  }, [modelType, onnxBlocked]);

  // Load use cases
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        setUseCases(response.usecases || []);
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError('Failed to load use cases');
      }
    };
    loadUseCases();
  }, []);

  // Inspect model
  const handleInspect = async () => {
    if (!selectedUseCase?.value || !modelS3Uri) {
      setError('Please select a use case and enter the model S3 URI');
      return;
    }
    await inspectSource(selectedUseCase.value, modelS3Uri);
  };

  // Upload the chosen file to a server-issued key, then inspect it
  // (Requirement 10.1). The returned model_s3_uri is what inspect and
  // convert use from here on.
  const handleUploadAndInspect = async () => {
    const file = uploadFiles[0];
    if (!selectedUseCase?.value || !file) {
      setError('Please select a use case and choose a model file');
      return;
    }
    const problem = uploadFileProblem(file);
    if (problem) {
      setError(problem);
      scrollToTop();
      return;
    }
    const usecaseId = selectedUseCase.value;
    setUploading(true);
    setUploadProgress(0);
    setUploadedUri(null);
    setInspectionResult(null);
    setError(null);
    let uploaded: string;
    try {
      const upload = await apiService.getModelUploadUrl({
        usecase_id: usecaseId,
        file_name: file.name,
        size_bytes: file.size,
      });
      await putFileWithProgress(upload.upload_url, file, setUploadProgress);
      uploaded = upload.model_s3_uri;
      setModelS3Uri(uploaded);
      setUploadedUri(uploaded);
    } catch (err) {
      console.error('Upload error:', err);
      setError(getErrorMessage(err, 'Failed to upload the model file'));
      scrollToTop();
      return;
    } finally {
      setUploading(false);
    }
    await inspectSource(usecaseId, uploaded);
  };

  const inspectSource = async (usecaseId: string, sourceUri: string) => {
    setInspecting(true);
    setInspectionResult(null);
    setError(null);

    try {
      const result = await apiService.inspectModel({
        usecase_id: usecaseId,
        model_s3_uri: sourceUri,
      });
      
      const ir = result.inspection_result;
      setInspectionResult(ir);

      // A .pt / .pth: the probe's Checkpoint block decides the form.
      if (ir.checkpoint) {
        const ckptLocks = conversionLocksFor(ir.checkpoint);
        if (ckptLocks) {
          // Convertible_Checkpoint: lock and pre-fill (Requirement 10.3).
          setModelType(ckptLocks.modelType);
          setExportFormat(ckptLocks.exportFormat);
          setDetectionArch(ckptLocks.arch);
          setNumClasses(String(ckptLocks.numClasses));
          setConversionClassNames(ckptLocks.classNames);
          setNetworkInput(ckptLocks.networkInput !== null ? String(ckptLocks.networkInput) : '');
          setScoreThreshold(String(ckptLocks.scoreThreshold));
          setIouThreshold(String(ckptLocks.iouThreshold ?? DEFAULT_IOU_THRESHOLD));
          setPreserveAspect(ckptLocks.preserveAspect);
        } else {
          // Not convertible: ONNX output is disabled (Requirement 10.4).
          setExportFormat('pytorch');
          if (ir.num_classes) {
            setNumClasses(ir.num_classes.toString());
          }
        }
        setCurrentStep(2);
        return;
      }

      // ONNX models run on the pluggable ONNX Runtime engine — pre-select it.
      if (ir.type === 'onnx') {
        setExportFormat('onnx');
      }

      // Auto-select suggested type if available
      if (ir.suggested_type) {
        setModelType(ir.suggested_type);
      }

      // Auto-select the detection architecture (YOLO / RF-DETR) when detected.
      if (ir.detection_arch) {
        setDetectionArch(ir.detection_arch);
      }

      // Auto-fill num_classes if detected
      if (ir.num_classes) {
        setNumClasses(ir.num_classes.toString());
      }

      // Auto-fill the input size from the model's declared input shape. ONNX
      // exports carry a fixed square input (e.g. RF-DETR base 560, nano 384),
      // which is rarely in the preset dropdown — populate the custom W/H boxes.
      if (ir.input_width && ir.input_height) {
        setUseCustomDimensions(true);
        setCustomWidth(String(ir.input_width));
        setCustomHeight(String(ir.input_height));
      }

      // Move to step 2
      setCurrentStep(2);
      
    } catch (err) {
      console.error('Inspection error:', err);
      setError(getErrorMessage(err, 'Failed to inspect model'));
      scrollToTop();
    } finally {
      setInspecting(false);
    }
  };

  // Convert a Convertible_Checkpoint to ONNX (Requirements 4, 10.3, 10.5).
  // The server re-classifies the source, starts the isolated Conversion_Job
  // and writes the record; when the job finishes it validates, packages and
  // publishes by itself, so this page never calls packaging.
  const handleConvertCheckpoint = async (usecaseId: string, conv: ConversionLocks) => {
    const problems = [
      validateClassNames(conversionClassNames, conv.numClasses),
      networkInputProblem(conv, networkInput),
      thresholdProblem('Score threshold', scoreThreshold),
      conv.arch === 'yolo' ? thresholdProblem('IoU threshold', iouThreshold) : null,
    ].filter((p): p is string => !!p);
    if (problems.length > 0) {
      setError(problems.join('. '));
      scrollToTop();
      return;
    }
    const size = parseInt(networkInput, 10);

    setConverting(true);
    setError(null);
    try {
      const result = await apiService.convertModel({
        usecase_id: usecaseId,
        model_s3_uri: modelS3Uri,
        model_name: modelName.trim(),
        model_type: conv.modelType,
        image_width: size,
        image_height: size,
        num_classes: conv.numClasses,
        export_format: conv.exportFormat,
        detection_arch: conv.arch,
        preserve_aspect: conv.preserveAspect,
        class_names: conversionClassNames.map(n => n.trim()),
        score_threshold: parseFloat(scoreThreshold),
        // RF-DETR is NMS-free: the field must be absent, not null.
        iou_threshold: conv.arch === 'yolo' ? parseFloat(iouThreshold) : undefined,
        auto_import: true,
      });
      if (result.training_id) {
        navigate(`/training/${result.training_id}`);
      }
    } catch (err) {
      console.error('Checkpoint conversion error:', err);
      // 400 / 503 reasons verbatim (Requirement 10.6).
      setError(getErrorMessage(err, 'Failed to start the checkpoint conversion'));
      scrollToTop();
    } finally {
      setConverting(false);
    }
  };

  // Convert and import model
  const handleConvert = async () => {
    if (!selectedUseCase?.value || !modelName || !modelType) {
      setError('Please fill in all required fields');
      return;
    }

    if (locks) {
      await handleConvertCheckpoint(selectedUseCase.value, locks);
      return;
    }

    const width = useCustomDimensions ? parseInt(customWidth) : parseInt(imageDimension);
    const height = useCustomDimensions ? parseInt(customHeight) : parseInt(imageDimension);

    if (isNaN(width) || isNaN(height) || width <= 0 || height <= 0) {
      setError('Please enter valid image dimensions');
      return;
    }

    // Detection decode settings apply only to object detection; everything else
    // sends undefined so the backend keeps its own defaults.
    const isDetection = modelType === 'object_detection';
    const parsedClassNames = classNames
      .split(',')
      .map(n => n.trim())
      .filter(n => n.length > 0);
    const detectionClassNames =
      isDetection && parsedClassNames.length > 0 ? parsedClassNames : undefined;

    // A class-name list shorter than num_classes silently falls back to numeric
    // labels for the missing ids on device, so catch the mismatch here.
    const declaredClasses = numClasses ? parseInt(numClasses) : undefined;
    if (
      detectionClassNames &&
      declaredClasses &&
      detectionClassNames.length !== declaredClasses
    ) {
      setError(
        `Class names (${detectionClassNames.length}) must match number of classes (${declaredClasses})`
      );
      return;
    }

    // Thresholds: blank means "let the backend default apply", so only a
    // present-but-unusable value is an error. Kept as plain locals rather than a
    // helper returning a sentinel, so the type stays number | undefined.
    let detectionScoreThreshold: number | undefined;
    let detectionIouThreshold: number | undefined;
    if (isDetection) {
      if (scoreThreshold.trim() !== '') {
        const value = parseFloat(scoreThreshold);
        if (isNaN(value) || value < 0 || value > 1) {
          setError('Score threshold must be a number between 0 and 1');
          return;
        }
        detectionScoreThreshold = value;
      }
      // NMS is YOLO-only; RF-DETR is set-based top-k, so it has no IoU knob.
      if (detectionArch === 'yolo' && iouThreshold.trim() !== '') {
        const value = parseFloat(iouThreshold);
        if (isNaN(value) || value < 0 || value > 1) {
          setError('IoU threshold must be a number between 0 and 1');
          return;
        }
        detectionIouThreshold = value;
      }
    }

    setConverting(true);
    setError(null);

    try {
      const result = await apiService.convertModel({
        usecase_id: selectedUseCase.value,
        model_s3_uri: modelS3Uri,
        model_name: modelName,
        model_type: modelType,
        image_width: width,
        image_height: height,
        num_classes: numClasses ? parseInt(numClasses) : undefined,
        export_format: exportFormat,
        detection_arch:
          modelType === 'object_detection' || modelType === 'segmentation'
            ? detectionArch
            : undefined,
        preserve_aspect:
          modelType === 'object_detection' ? preserveAspect : undefined,
        class_names: detectionClassNames,
        score_threshold: detectionScoreThreshold,
        iou_threshold: detectionIouThreshold,
        auto_import: true,
      });

      if (result.conversion) {
        // A conversion was started server-side after all (the server
        // re-classifies every source): it finalizes itself, never package.
        navigate(`/training/${result.training_id}`);
        return;
      }

      if (result.training_id) {
        setSuccess(`Model converted and imported successfully! Training ID: ${result.training_id}`);

        const targets = compilationTargets.map(t => t.value!);
        if (exportFormat === 'onnx') {
          // ONNX runs on the ONNX Runtime engine — no Neo compilation. Go
          // straight to packaging (architecture-agnostic) with auto-publish so
          // a deployable component is created in one step.
          //
          // Deliberately NOT forwarding `targets`: the ONNX artifact is
          // portable, one package serves every platform, and this screen offers
          // no platform picker for ONNX. Passing the Neo compilation-target
          // selection here silently narrowed the fan-out instead — and since
          // the compilation-target list has no jetson-xavier-jp7 entry at all, a JP7
          // device could never receive an ONNX model imported from this screen.
          // Omitting it lets packaging.py apply its full portable target set
          // (jp5, jp6, jp7, x86_64-cpu).
          try {
            await apiService.startPackaging(result.training_id, undefined, true);
            setSuccess(`Model imported and packaging started (ONNX, no compilation needed)! Training ID: ${result.training_id}`);
          } catch (pkgErr) {
            console.error('Packaging trigger failed:', pkgErr);
          }
        } else if (autoCompile && targets.length > 0) {
          // Legacy PyTorch/Neo path: compile first.
          try {
            await apiService.startCompilation(result.training_id, targets);
            setSuccess(`Model converted, imported, and compilation started! Training ID: ${result.training_id}`);
          } catch (compileErr) {
            console.error('Compilation trigger failed:', compileErr);
            // Don't fail the whole operation
          }
        }
        
        // Navigate to training detail page
        setTimeout(() => {
          navigate(`/training/${result.training_id}`);
        }, 2000);
      } else {
        setSuccess(`Model converted successfully! Output: ${result.converted_model_s3_uri}`);
      }
      
    } catch (err) {
      console.error('Conversion error:', err);
      setError(getErrorMessage(err, 'Failed to convert model'));
      scrollToTop();
    } finally {
      setConverting(false);
    }
  };

  const getImageWidth = () => useCustomDimensions ? parseInt(customWidth) || 0 : parseInt(imageDimension);
  const getImageHeight = () => useCustomDimensions ? parseInt(customHeight) || 0 : parseInt(imageDimension);

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success">
          {success}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h1"
            description="Import any PyTorch model - we'll auto-generate the required metadata"
            info={<Link variant="info" onFollow={() => navigate('/models/import')}>Use manual import instead</Link>}
          >
            Smart Import (BYOM)
          </Header>
        }
      >
        <SpaceBetween size="l">
          {/* Step 1: Upload and Inspect */}
          <Container
            header={
              <Header variant="h2">
                <SpaceBetween direction="horizontal" size="xs">
                  <Badge color={currentStep >= 1 ? 'blue' : 'grey'}>Step 1</Badge>
                  Upload Model
                </SpaceBetween>
              </Header>
            }
          >
            <SpaceBetween size="m">
              <FormField label="Use Case" description="Select the use case to import the model into">
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                    description: `Account: ${uc.account_id}`,
                  }))}
                  placeholder="Select a use case"
                  filteringType="auto"
                />
              </FormField>

              <FormField label="Model source">
                <SegmentedControl
                  selectedId={sourceMode}
                  onChange={({ detail }) => setSourceMode(detail.selectedId === 'upload' ? 'upload' : 's3')}
                  label="Model source"
                  options={[
                    { id: 's3', text: 'S3 URI' },
                    { id: 'upload', text: 'Upload a file' },
                  ]}
                />
              </FormField>

              {sourceMode === 's3' ? (
                <>
                  <FormField
                    label="Model File (S3 URI)"
                    description="S3 URI of your model file: a PyTorch .pt / .pth, or an .onnx graph"
                    constraintText="Just the raw file - no special packaging required!"
                    errorText={validateS3Uri(modelS3Uri)}
                  >
                    <Input
                      value={modelS3Uri}
                      onChange={({ detail }) => setModelS3Uri(detail.value)}
                      placeholder="s3://my-bucket/models/yolov10.pt"
                    />
                  </FormField>

                  <Button
                    onClick={handleInspect}
                    loading={inspecting}
                    disabled={!selectedUseCase?.value || !modelS3Uri}
                  >
                    Inspect Model
                  </Button>
                </>
              ) : (
                <>
                  <FormField
                    label="Model file"
                    description="Uploaded to the use case's own bucket, then inspected. Detector checkpoints (ultralytics YOLO .pt, RF-DETR .pth) can be converted to ONNX."
                    constraintText={`${UPLOAD_EXTENSIONS.join(', ')}, up to ${formatBytes(CHECKPOINT_SIZE_CAP_BYTES)}`}
                    errorText={uploadFiles[0] ? uploadFileProblem(uploadFiles[0]) : undefined}
                  >
                    <FileUpload
                      value={uploadFiles}
                      onChange={({ detail }) => {
                        setUploadFiles(detail.value);
                        setUploadedUri(null);
                        setUploadProgress(0);
                      }}
                      accept={UPLOAD_EXTENSIONS.join(',')}
                      showFileSize
                      i18nStrings={FILE_UPLOAD_I18N}
                    />
                  </FormField>

                  {(uploading || uploadedUri) && (
                    <ProgressBar
                      value={uploadProgress}
                      label={uploadedUri ? 'Uploaded' : `Uploading ${uploadFiles[0]?.name ?? ''}`}
                      description={uploadedUri ?? undefined}
                      status={uploadedUri ? 'success' : 'in-progress'}
                      resultText="Upload complete"
                      data-testid="model-upload-progress"
                    />
                  )}

                  <Button
                    onClick={handleUploadAndInspect}
                    loading={uploading || inspecting}
                    disabled={
                      !selectedUseCase?.value ||
                      uploadFiles.length === 0 ||
                      !!uploadFileProblem(uploadFiles[0]) ||
                      uploading ||
                      inspecting
                    }
                  >
                    Upload and inspect
                  </Button>
                </>
              )}

              {inspecting && (
                <Box textAlign="center" padding="l">
                  <Spinner size="large" />
                  <Box variant="p" color="text-body-secondary">
                    Analyzing model architecture...
                  </Box>
                </Box>
              )}

              {checkpoint && (
                <CheckpointPanel assessment={checkpoint} fineTunable={!!inspectionResult?.fine_tunable} />
              )}

              {inspectionResult && !checkpoint && (
                <Container
                  header={<Header variant="h3">Model Analysis</Header>}
                >
                  <ColumnLayout columns={2} variant="text-grid">
                    <div>
                      <Box variant="awsui-key-label">Detected Type</Box>
                      <div>
                        {inspectionResult.suggested_type ? (
                          <StatusIndicator type="success">
                            {inspectionResult.suggested_type}
                          </StatusIndicator>
                        ) : inspectionResult.type === 'onnx' ? (
                          <StatusIndicator type="success">ONNX</StatusIndicator>
                        ) : (
                          <StatusIndicator type="info">Unknown</StatusIndicator>
                        )}
                      </div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Total Layers</Box>
                      <div>{inspectionResult.total_layers || 'N/A'}</div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Input Channels</Box>
                      <div>{inspectionResult.input_channels || 'N/A'}</div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Detected Classes</Box>
                      <div>{inspectionResult.num_classes || 'N/A'}</div>
                    </div>
                  </ColumnLayout>
                  {inspectionResult.architecture_hints.length > 0 && (
                    <Box margin={{ top: 's' }}>
                      <Box variant="awsui-key-label">Architecture Hints</Box>
                      <ul>
                        {inspectionResult.architecture_hints.map((hint, i) => (
                          <li key={i}>{hint}</li>
                        ))}
                      </ul>
                    </Box>
                  )}
                </Container>
              )}
            </SpaceBetween>
          </Container>

          {/* Step 2: Configure */}
          {currentStep >= 2 && (
            <Container
              header={
                <Header variant="h2">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Badge color={currentStep >= 2 ? 'blue' : 'grey'}>Step 2</Badge>
                    Configure Model
                  </SpaceBetween>
                </Header>
              }
            >
              <SpaceBetween size="m">
                <FormField label="Model Name" constraintText="A descriptive name for your model">
                  <Input
                    value={modelName}
                    onChange={({ detail }) => setModelName(detail.value)}
                    placeholder="my-defect-detector"
                  />
                </FormField>

                <FormField
                  label="Model Type"
                  description={
                    locks
                      ? 'Locked to Object Detection: the checkpoint is a detector.'
                      : 'What does your model do?'
                  }
                >
                  <Tiles
                    value={modelType}
                    onChange={({ detail }) => setModelType(detail.value)}
                    items={[
                      {
                        value: 'classification',
                        label: 'Classification',
                        description: 'Classify images into categories',
                      },
                      {
                        value: 'object_detection',
                        label: 'Object Detection',
                        description: 'Detect and locate objects (YOLO, SSD)',
                      },
                      {
                        value: 'segmentation',
                        label: 'Segmentation',
                        description: 'Pixel-level classification',
                      },
                      {
                        value: 'anomaly_detection',
                        label: 'Anomaly Detection',
                        description: 'Detect anomalies/defects',
                      },
                    ].map(item => ({ ...item, disabled: !!locks && item.value !== locks.modelType }))}
                  />
                </FormField>

                <FormField
                  label="Runtime / export format"
                  description={
                    locks
                      ? 'Locked to ONNX: the checkpoint is converted to ONNX in an isolated job, then validated, packaged and published automatically. The checkpoint itself is kept as a fine-tunable base model.'
                      : onnxBlocked
                      ? 'ONNX is unavailable for this checkpoint (see the reasons in the Checkpoint panel). PyTorch / Neo remains available.'
                      : 'ONNX runs on the pluggable ONNX Runtime engine (GPU on JetPack 5/6). Object detection requires ONNX. PyTorch/Neo uses the legacy DLR path.'
                  }
                >
                  <Tiles
                    value={exportFormat}
                    onChange={({ detail }) => setExportFormat(detail.value)}
                    items={[
                      {
                        value: 'pytorch',
                        label: 'PyTorch / Neo (DLR)',
                        description: checkpoint?.kind === 'ultralytics_checkpoint' || checkpoint?.kind === 'rfdetr_checkpoint'
                          ? 'Import as a base model only (kept for fine-tuning); compiled with SageMaker Neo to DLR'
                          : 'Legacy path — compiled with SageMaker Neo to DLR',
                        disabled: !!locks,
                      },
                      {
                        value: 'onnx',
                        label: locks ? 'ONNX Runtime (convert)' : 'ONNX Runtime',
                        description: onnxBlocked
                          ? `Unavailable: ${(checkpoint?.reasons || []).join('; ') || 'this checkpoint cannot be converted'}`
                          : 'Portable ONNX engine; required for object detection',
                        disabled: onnxBlocked,
                      },
                    ]}
                  />
                </FormField>

                {modelType === 'object_detection' && (
                  <FormField
                    label="Detection architecture"
                    description={
                      locks
                        ? `Locked to ${locks.arch === 'yolo' ? 'YOLO' : 'RF-DETR'}: the family the checkpoint was saved by.`
                        : 'Decoder family for the on-device postprocessor. YOLO = single output tensor with NMS. RF-DETR = DETR-family with two tensors (boxes + logits), NMS-free top-k.'
                    }
                  >
                    <Tiles
                      value={detectionArch}
                      onChange={({ detail }) => setDetectionArch(detail.value)}
                      items={[
                        {
                          value: 'yolo',
                          label: 'YOLO',
                          description: 'YOLOv5/v8-style single-tensor output, NMS decode',
                        },
                        {
                          value: 'rf_detr',
                          label: 'RF-DETR',
                          description: 'DETR-family, boxes + logits tensors, NMS-free top-k',
                        },
                      ].map(item => ({ ...item, disabled: !!locks && item.value !== locks.arch }))}
                    />
                  </FormField>
                )}

                {locks && (
                  <FormField label="Input resize geometry" description={locks.geometryReason}>
                    <Box data-testid="conversion-geometry">
                      <StatusIndicator type="info">{locks.geometry}</StatusIndicator>
                    </Box>
                  </FormField>
                )}

                {!locks && modelType === 'object_detection' && (
                  <FormField
                    label="Input resize geometry"
                    description="Must match how the model was trained. Letterbox scales by a single ratio and centre-pads; squash stretches the frame to the network input. A mismatch does not error — it just loses detections (~1.35x mean confidence, up to 5.7x on high-resolution frames)."
                  >
                    <Checkbox
                      checked={preserveAspect}
                      onChange={({ detail }) => setPreserveAspect(detail.checked)}
                    >
                      Preserve aspect ratio (letterbox) — correct for
                      ultralytics/YOLO-trained models
                    </Checkbox>
                  </FormField>
                )}

                {modelType === 'segmentation' && (
                  <FormField
                    label="Segmentation architecture"
                    description="On-device decoder for the ONNX segmentation model. RF-DETR (ONNX Runtime) instance masks are composited into a colored, per-class semantic overlay you can toggle over the source image."
                  >
                    <Tiles
                      value={detectionArch}
                      onChange={({ detail }) => setDetectionArch(detail.value)}
                      items={[
                        {
                          value: 'rf_detr',
                          label: 'RF-DETR',
                          description: 'DETR-family instance segmentation → semantic mask overlay',
                        },
                      ]}
                    />
                  </FormField>
                )}

                {locks ? (
                  <>
                    {/* Convertible_Checkpoint (Requirement 10.3): the network
                        input is pre-filled from the training size within the
                        arch's bounds, the class count is the checkpoint's
                        head, and no Neo compilation targets are offered. */}
                    <FormField
                      label="Network input"
                      description={
                        locks.allowedInputs
                          ? `RF-DETR converts only at its size's native resolution (${locks.allowedInputs.join(', ')} px).`
                          : 'The square input the ONNX graph is exported at. Pre-filled from the training size.'
                      }
                      constraintText={
                        locks.allowedInputs
                          ? undefined
                          : `A multiple of ${locks.inputBounds.step} between ${locks.inputBounds.min} and ${locks.inputBounds.max} px`
                      }
                      errorText={networkInput.trim() ? networkInputProblem(locks, networkInput) : undefined}
                    >
                      {locks.allowedInputs ? (
                        <Select
                          selectedOption={
                            networkInput
                              ? { label: `${networkInput} x ${networkInput}`, value: networkInput }
                              : null
                          }
                          onChange={({ detail }) => setNetworkInput(detail.selectedOption?.value || '')}
                          options={locks.allowedInputs.map(n => ({ label: `${n} x ${n}`, value: String(n) }))}
                          disabled={locks.allowedInputs.length === 1}
                          placeholder="Select the checkpoint's native resolution"
                        />
                      ) : (
                        <Input
                          type="number"
                          value={networkInput}
                          onChange={({ detail }) => setNetworkInput(detail.value)}
                          placeholder={String(locks.networkInput ?? 640)}
                        />
                      )}
                    </FormField>

                    <FormField
                      label="Number of Classes"
                      description="Locked to the checkpoint's detection head. Classes can be renamed but not added or removed."
                    >
                      <Input type="number" value={String(locks.numClasses)} disabled onChange={() => undefined} />
                    </FormField>

                    <ExpandableSection headerText="Detection decode settings" defaultExpanded>
                      <SpaceBetween size="s">
                        <FormField
                          label="Class names"
                          description="In class-index order, as stored in the checkpoint. Rename a class by editing its name; the device labels detections with these names."
                          errorText={validateClassNames(conversionClassNames, locks.numClasses) ?? undefined}
                        >
                          <ClassNameEditor names={conversionClassNames} onChange={setConversionClassNames} />
                        </FormField>

                        <FormField
                          label="Score threshold"
                          description="Minimum confidence for a detection to be kept."
                          errorText={thresholdProblem('Score threshold', scoreThreshold) ?? undefined}
                        >
                          <Input
                            type="number"
                            value={scoreThreshold}
                            onChange={({ detail }) => setScoreThreshold(detail.value)}
                            placeholder={String(locks.scoreThreshold)}
                          />
                        </FormField>

                        {locks.arch === 'yolo' && (
                          <FormField
                            label="IoU threshold"
                            description="Overlap above which NMS suppresses the weaker of two boxes. YOLO only — RF-DETR is NMS-free."
                            errorText={thresholdProblem('IoU threshold', iouThreshold) ?? undefined}
                          >
                            <Input
                              type="number"
                              value={iouThreshold}
                              onChange={({ detail }) => setIouThreshold(detail.value)}
                              placeholder={String(DEFAULT_IOU_THRESHOLD)}
                            />
                          </FormField>
                        )}
                      </SpaceBetween>
                    </ExpandableSection>
                  </>
                ) : (
                  <>
                    <FormField label="Input Image Size" description="The image dimensions your model expects">
                      <SpaceBetween size="s">
                        {!useCustomDimensions && (
                          <Select
                            selectedOption={
                              COMMON_DIMENSIONS[modelType]?.find(d => d.value === imageDimension) || null
                            }
                            onChange={({ detail }) => setImageDimension(detail.selectedOption?.value || '224')}
                            options={COMMON_DIMENSIONS[modelType] || COMMON_DIMENSIONS.classification}
                            placeholder="Select image size"
                          />
                        )}
                        <Checkbox
                          checked={useCustomDimensions}
                          onChange={({ detail }) => setUseCustomDimensions(detail.checked)}
                        >
                          Use custom dimensions
                        </Checkbox>
                        {useCustomDimensions && (
                          <SpaceBetween direction="horizontal" size="xs">
                            <FormField label="Width">
                              <Input
                                type="number"
                                value={customWidth}
                                onChange={({ detail }) => setCustomWidth(detail.value)}
                                placeholder="640"
                              />
                            </FormField>
                            <FormField label="Height">
                              <Input
                                type="number"
                                value={customHeight}
                                onChange={({ detail }) => setCustomHeight(detail.value)}
                                placeholder="640"
                              />
                            </FormField>
                          </SpaceBetween>
                        )}
                      </SpaceBetween>
                    </FormField>

                    <FormField
                      label="Number of Classes"
                      description="How many output classes does your model have?"
                      constraintText="Optional - will use detected value if available"
                    >
                      <Input
                        type="number"
                        value={numClasses}
                        onChange={({ detail }) => setNumClasses(detail.value)}
                        placeholder={inspectionResult?.num_classes?.toString() || '10'}
                      />
                    </FormField>

                    {modelType === 'object_detection' && (
                      <ExpandableSection headerText="Detection decode settings" defaultExpanded>
                        <SpaceBetween size="s">
                          <FormField
                            label="Class names"
                            description="Comma-separated, in class-id order. Without these the device labels every detection with its numeric class id (class 0 becomes the label &quot;0&quot;), so anything matching on a label string will not match."
                            constraintText="Optional, e.g. blue_plate"
                          >
                            <Input
                              value={classNames}
                              onChange={({ detail }) => setClassNames(detail.value)}
                              placeholder="blue_plate"
                            />
                          </FormField>

                          <FormField
                            label="Score threshold"
                            description="Minimum confidence for a detection to be kept."
                          >
                            <Input
                              type="number"
                              value={scoreThreshold}
                              onChange={({ detail }) => setScoreThreshold(detail.value)}
                              placeholder="0.25"
                            />
                          </FormField>

                          {detectionArch === 'yolo' && (
                            <FormField
                              label="IoU threshold"
                              description="Overlap above which NMS suppresses the weaker of two boxes. YOLO only — RF-DETR is NMS-free."
                            >
                              <Input
                                type="number"
                                value={iouThreshold}
                                onChange={({ detail }) => setIouThreshold(detail.value)}
                                placeholder="0.45"
                              />
                            </FormField>
                          )}
                        </SpaceBetween>
                      </ExpandableSection>
                    )}

                    <ExpandableSection headerText="Compilation Options" defaultExpanded>
                      <SpaceBetween size="s">
                        <Checkbox
                          checked={autoCompile}
                          onChange={({ detail }) => setAutoCompile(detail.checked)}
                        >
                          Automatically compile model after import
                        </Checkbox>
                        {autoCompile && (
                          <FormField label="Compilation Targets">
                            <Multiselect
                              selectedOptions={compilationTargets}
                              onChange={({ detail }) => setCompilationTargets(detail.selectedOptions as MultiselectProps.Option[])}
                              options={COMPILATION_TARGET_OPTIONS}
                              placeholder="Select compilation targets"
                            />
                          </FormField>
                        )}
                      </SpaceBetween>
                    </ExpandableSection>
                  </>
                )}

                {/* Summary */}
                <Container header={<Header variant="h3">Summary</Header>}>
                  <ColumnLayout columns={2} variant="text-grid">
                    <div>
                      <Box variant="awsui-key-label">Model Name</Box>
                      <div>{modelName || '-'}</div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Model Type</Box>
                      <div>{modelType}</div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Input Shape</Box>
                      <div>
                        {locks
                          ? `[1, 3, ${networkInput || '?'}, ${networkInput || '?'}]`
                          : `[1, 3, ${getImageHeight()}, ${getImageWidth()}]`}
                      </div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Classes</Box>
                      <div>
                        {locks
                          ? locks.numClasses
                          : numClasses || inspectionResult?.num_classes || 'Auto-detect'}
                      </div>
                    </div>
                    {locks && (
                      <div>
                        <Box variant="awsui-key-label">Output</Box>
                        <div>
                          ONNX, converted in an isolated job, then validated, packaged and published for
                          JetPack 5/6/7 and x86 automatically
                        </div>
                      </div>
                    )}
                  </ColumnLayout>
                </Container>

                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => setCurrentStep(1)}>Back</Button>
                  <Button
                    variant="primary"
                    onClick={handleConvert}
                    loading={converting}
                    disabled={!modelName || converting}
                  >
                    Convert & Import Model
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            </Container>
          )}
        </SpaceBetween>
      </Container>

      {/* Help Section */}
      <ExpandableSection headerText="How Smart Import Works" variant="container">
        <SpaceBetween size="m">
          <Box variant="p">
            Smart Import automatically generates the required DDA metadata files from your raw PyTorch model:
          </Box>
          <ol>
            <li><strong>Upload</strong> - Upload your .pt / .pth / .onnx file, or point to it in S3 (no special packaging needed)</li>
            <li><strong>Inspect</strong> - We analyze the model to detect architecture and parameters</li>
            <li><strong>Configure</strong> - Confirm or adjust the detected settings</li>
            <li><strong>Convert</strong> - We generate config.yaml, mochi.json, and manifest.json automatically</li>
            <li><strong>Import</strong> - The packaged model is imported and ready for compilation</li>
          </ol>
          <Box variant="p">
            Detector checkpoints (an ultralytics YOLO <code>best.pt</code> or an RF-DETR <code>.pth</code>) are
            converted to ONNX instead: the checkpoint is loaded only inside a network-isolated SageMaker job, and
            the portal validates the resulting ONNX, then packages and publishes it for JetPack 5/6/7 and x86.
            The checkpoint is also kept, so the imported model can be fine-tuned later.
          </Box>
          <Alert type="info">
            For models that don't work with Smart Import, use the{' '}
            <Link onFollow={() => navigate('/models/import')}>Manual Import</Link>{' '}
            option with a pre-packaged tar.gz file.
          </Alert>
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
}
