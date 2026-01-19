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
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

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
}

const COMPILATION_TARGETS: MultiselectProps.Option[] = [
  { label: 'x86_64 CPU', value: 'x86_64-cpu', description: 'Intel/AMD 64-bit processors' },
  { label: 'x86_64 CUDA', value: 'x86_64-cuda', description: 'NVIDIA GPU on x86_64' },
  { label: 'ARM64 CPU', value: 'arm64-cpu', description: 'ARM 64-bit processors' },
  { label: 'Jetson Xavier', value: 'jetson-xavier', description: 'NVIDIA Jetson Xavier' },
];

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
  const [autoCompile, setAutoCompile] = useState(true);
  const [compilationTargets, setCompilationTargets] = useState<MultiselectProps.Option[]>([
    { label: 'x86_64 CPU', value: 'x86_64-cpu' }
  ]);
  
  // Inspection state
  const [inspecting, setInspecting] = useState(false);
  const [inspectionResult, setInspectionResult] = useState<ModelInspectionResult | null>(null);
  
  // Conversion state
  const [converting, setConverting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

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

    setInspecting(true);
    setInspectionResult(null);
    setError(null);

    try {
      const result = await apiService.inspectModel({
        usecase_id: selectedUseCase.value,
        model_s3_uri: modelS3Uri,
      });
      
      setInspectionResult(result.inspection_result);
      
      // Auto-select suggested type if available
      if (result.inspection_result.suggested_type) {
        setModelType(result.inspection_result.suggested_type);
      }
      
      // Auto-fill num_classes if detected
      if (result.inspection_result.num_classes) {
        setNumClasses(result.inspection_result.num_classes.toString());
      }
      
      // Move to step 2
      setCurrentStep(2);
      
    } catch (err) {
      console.error('Inspection error:', err);
      setError(err instanceof Error ? err.message : 'Failed to inspect model');
    } finally {
      setInspecting(false);
    }
  };

  // Convert and import model
  const handleConvert = async () => {
    if (!selectedUseCase?.value || !modelName || !modelType) {
      setError('Please fill in all required fields');
      return;
    }

    const width = useCustomDimensions ? parseInt(customWidth) : parseInt(imageDimension);
    const height = useCustomDimensions ? parseInt(customHeight) : parseInt(imageDimension);

    if (isNaN(width) || isNaN(height) || width <= 0 || height <= 0) {
      setError('Please enter valid image dimensions');
      return;
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
        auto_import: true,
      });

      if (result.training_id) {
        setSuccess(`Model converted and imported successfully! Training ID: ${result.training_id}`);
        
        // If auto-compile is enabled, trigger compilation
        if (autoCompile && compilationTargets.length > 0) {
          try {
            await apiService.startCompilation(
              result.training_id,
              compilationTargets.map(t => t.value!)
            );
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
      setError(err instanceof Error ? err.message : 'Failed to convert model');
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

              <FormField
                label="Model File (S3 URI)"
                description="S3 URI of your PyTorch model file (.pt)"
                constraintText="Just the raw .pt file - no special packaging required!"
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

              {inspecting && (
                <Box textAlign="center" padding="l">
                  <Spinner size="large" />
                  <Box variant="p" color="text-body-secondary">
                    Analyzing model architecture...
                  </Box>
                </Box>
              )}

              {inspectionResult && (
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

                <FormField label="Model Type" description="What does your model do?">
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
                    ]}
                  />
                </FormField>

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
                          options={COMPILATION_TARGETS}
                          placeholder="Select compilation targets"
                        />
                      </FormField>
                    )}
                  </SpaceBetween>
                </ExpandableSection>

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
                      <div>[1, 3, {getImageHeight()}, {getImageWidth()}]</div>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Classes</Box>
                      <div>{numClasses || inspectionResult?.num_classes || 'Auto-detect'}</div>
                    </div>
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
            <li><strong>Upload</strong> - Point to your .pt file in S3 (no special packaging needed)</li>
            <li><strong>Inspect</strong> - We analyze the model to detect architecture and parameters</li>
            <li><strong>Configure</strong> - Confirm or adjust the detected settings</li>
            <li><strong>Convert</strong> - We generate config.yaml, mochi.json, and manifest.json automatically</li>
            <li><strong>Import</strong> - The packaged model is imported and ready for compilation</li>
          </ol>
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
