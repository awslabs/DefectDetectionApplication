import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Form,
  FormField,
  Input,
  Button,
  Select,
  SelectProps,
  Alert,
  Box,
  ExpandableSection,
  Checkbox,
  Multiselect,
  MultiselectProps,
  StatusIndicator,
  ColumnLayout,
  Spinner,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface ValidationResult {
  valid: boolean;
  model_s3_uri?: string;
  metadata?: {
    image_width: number;
    image_height: number;
    input_shape: number[];
    model_type: string;
    pt_file: string;
    framework: string;
    framework_version: string;
  };
  files_found?: string[];
  warnings?: string[];
  error?: string;
  details?: string[];
}

const COMPILATION_TARGETS: MultiselectProps.Option[] = [
  { label: 'x86_64 CPU', value: 'x86_64-cpu', description: 'Intel/AMD 64-bit processors' },
  { label: 'x86_64 CUDA', value: 'x86_64-cuda', description: 'NVIDIA GPU on x86_64' },
  { label: 'ARM64 CPU', value: 'arm64-cpu', description: 'ARM 64-bit processors' },
  { label: 'Jetson Xavier', value: 'jetson-xavier', description: 'NVIDIA Jetson Xavier' },
];

export default function ImportModel() {
  const navigate = useNavigate();
  
  // Form state
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('1.0.0');
  const [modelS3Uri, setModelS3Uri] = useState('');
  const [description, setDescription] = useState('');
  const [autoCompile, setAutoCompile] = useState(false);
  const [compilationTargets, setCompilationTargets] = useState<MultiselectProps.Option[]>([]);
  
  // Validation state
  const [validating, setValidating] = useState(false);
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  
  // Submission state
  const [submitting, setSubmitting] = useState(false);
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

  // Validate model
  const handleValidate = async () => {
    if (!selectedUseCase?.value || !modelS3Uri) {
      setError('Please select a use case and enter the model S3 URI');
      return;
    }

    setValidating(true);
    setValidationResult(null);
    setError(null);

    try {
      const result = await apiService.validateModel({
        usecase_id: selectedUseCase.value,
        model_s3_uri: modelS3Uri,
      });
      setValidationResult(result);
      
      if (!result.valid) {
        setError(result.error || 'Model validation failed');
      }
    } catch (err) {
      console.error('Validation error:', err);
      setError(err instanceof Error ? err.message : 'Validation failed');
      setValidationResult({
        valid: false,
        error: err instanceof Error ? err.message : 'Validation failed',
      });
    } finally {
      setValidating(false);
    }
  };

  // Import model
  const handleImport = async () => {
    if (!selectedUseCase?.value || !modelName || !modelVersion || !modelS3Uri) {
      setError('Please fill in all required fields');
      return;
    }

    if (!validationResult?.valid) {
      setError('Please validate the model first');
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const result = await apiService.importModel({
        usecase_id: selectedUseCase.value,
        model_name: modelName,
        model_version: modelVersion,
        model_s3_uri: modelS3Uri,
        description: description || undefined,
        auto_compile: autoCompile,
        compilation_targets: autoCompile ? compilationTargets.map(t => t.value!) : undefined,
      });

      setSuccess(`Model imported successfully! Training ID: ${result.training_id}`);
      
      // Navigate to training detail page after short delay
      setTimeout(() => {
        navigate(`/training/${result.training_id}`);
      }, 2000);
    } catch (err) {
      console.error('Import error:', err);
      setError(err instanceof Error ? err.message : 'Failed to import model');
    } finally {
      setSubmitting(false);
    }
  };

  const formatSpecExample = `model.tar.gz
├── config.yaml                    # Image dimensions
│   └── dataset:
│       ├── image_width: 224
│       └── image_height: 224
├── mochi.json                     # Model graph definition
│   └── stages: [{
│       ├── type: "anomaly_detection"
│       └── input_shape: [1, 3, 224, 224]
│   }]
└── export_artifacts/
    ├── manifest.json              # Model metadata
    └── model.pt                   # PyTorch model file`;

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
            description="Import a pre-trained PyTorch model that conforms to the DDA format"
          >
            Import Model (BYOM)
          </Header>
        }
      >
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate('/models')}>Cancel</Button>
              <Button
                onClick={handleValidate}
                loading={validating}
                disabled={!selectedUseCase?.value || !modelS3Uri}
              >
                Validate Model
              </Button>
              <Button
                variant="primary"
                onClick={handleImport}
                loading={submitting}
                disabled={!validationResult?.valid || submitting}
              >
                Import Model
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            {/* Use Case Selection */}
            <FormField label="Use Case" description="Select the use case to import the model into">
              <Select
                selectedOption={selectedUseCase}
                onChange={({ detail }) => {
                  setSelectedUseCase(detail.selectedOption);
                  setValidationResult(null);
                }}
                options={useCases.map((uc) => ({
                  label: uc.name,
                  value: uc.usecase_id,
                  description: `Account: ${uc.account_id}`,
                }))}
                placeholder="Select a use case"
                filteringType="auto"
              />
            </FormField>

            {/* Model S3 URI */}
            <FormField
              label="Model S3 URI"
              description="S3 URI of the model artifact (tar.gz file)"
              constraintText="Format: s3://bucket/path/model.tar.gz"
            >
              <Input
                value={modelS3Uri}
                onChange={({ detail }) => {
                  setModelS3Uri(detail.value);
                  setValidationResult(null);
                }}
                placeholder="s3://my-bucket/models/my-model.tar.gz"
              />
            </FormField>

            {/* Validation Result */}
            {validating && (
              <Box textAlign="center" padding="l">
                <Spinner size="large" />
                <Box variant="p" color="text-body-secondary">
                  Validating model artifact...
                </Box>
              </Box>
            )}

            {validationResult && (
              <Container
                header={
                  <Header variant="h3">
                    Validation Result
                    {validationResult.valid ? (
                      <StatusIndicator type="success">Valid</StatusIndicator>
                    ) : (
                      <StatusIndicator type="error">Invalid</StatusIndicator>
                    )}
                  </Header>
                }
              >
                {validationResult.valid && validationResult.metadata ? (
                  <SpaceBetween size="m">
                    <ColumnLayout columns={3} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Model Type</Box>
                        <div>{validationResult.metadata.model_type}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Framework</Box>
                        <div>{validationResult.metadata.framework} {validationResult.metadata.framework_version}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Model File</Box>
                        <div>{validationResult.metadata.pt_file}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Image Dimensions</Box>
                        <div>{validationResult.metadata.image_width} x {validationResult.metadata.image_height}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Input Shape</Box>
                        <div>[{validationResult.metadata.input_shape.join(', ')}]</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Files Found</Box>
                        <div>{validationResult.files_found?.length || 0} files</div>
                      </div>
                    </ColumnLayout>
                    {validationResult.warnings && validationResult.warnings.length > 0 && (
                      <Alert type="warning">
                        {validationResult.warnings.join(', ')}
                      </Alert>
                    )}
                  </SpaceBetween>
                ) : (
                  <SpaceBetween size="s">
                    <Alert type="error">
                      {validationResult.error}
                    </Alert>
                    {validationResult.details && validationResult.details.length > 0 && (
                      <Box>
                        <Box variant="awsui-key-label">Details:</Box>
                        <ul>
                          {validationResult.details.map((detail, i) => (
                            <li key={i}>{detail}</li>
                          ))}
                        </ul>
                      </Box>
                    )}
                  </SpaceBetween>
                )}
              </Container>
            )}

            {/* Model Details - only show after successful validation */}
            {validationResult?.valid && (
              <>
                <FormField
                  label="Model Name"
                  description="A descriptive name for the model"
                  constraintText="Use alphanumeric characters and hyphens"
                >
                  <Input
                    value={modelName}
                    onChange={({ detail }) => setModelName(detail.value)}
                    placeholder="my-defect-detector"
                  />
                </FormField>

                <FormField
                  label="Model Version"
                  description="Version identifier for the model"
                  constraintText="Format: x.y.z (e.g., 1.0.0)"
                >
                  <Input
                    value={modelVersion}
                    onChange={({ detail }) => setModelVersion(detail.value)}
                    placeholder="1.0.0"
                  />
                </FormField>

                <FormField
                  label="Description"
                  description="Optional description of the model"
                >
                  <Input
                    value={description}
                    onChange={({ detail }) => setDescription(detail.value)}
                    placeholder="Pre-trained model for detecting manufacturing defects"
                  />
                </FormField>

                {/* Auto-compile options */}
                <FormField label="Compilation Options">
                  <SpaceBetween size="s">
                    <Checkbox
                      checked={autoCompile}
                      onChange={({ detail }) => setAutoCompile(detail.checked)}
                    >
                      Automatically compile model after import
                    </Checkbox>
                    {autoCompile && (
                      <FormField
                        label="Compilation Targets"
                        description="Select target platforms for model compilation"
                      >
                        <Multiselect
                          selectedOptions={compilationTargets}
                          onChange={({ detail }) => setCompilationTargets(detail.selectedOptions as MultiselectProps.Option[])}
                          options={COMPILATION_TARGETS}
                          placeholder="Select compilation targets"
                          filteringType="auto"
                        />
                      </FormField>
                    )}
                  </SpaceBetween>
                </FormField>
              </>
            )}
          </SpaceBetween>
        </Form>
      </Container>

      {/* Model Format Specification */}
      <ExpandableSection
        headerText="Model Format Specification"
        variant="container"
      >
        <SpaceBetween size="m">
          <Box variant="p">
            The model artifact must be a <code>.tar.gz</code> file containing the following structure:
          </Box>
          <Box>
            <pre style={{ 
              backgroundColor: '#f4f4f4', 
              padding: '16px', 
              borderRadius: '4px',
              overflow: 'auto',
              fontSize: '13px',
              fontFamily: 'Monaco, Menlo, Consolas, monospace'
            }}>
              {formatSpecExample}
            </pre>
          </Box>
          <SpaceBetween size="s">
            <Box variant="h4">Required Files:</Box>
            <ul>
              <li><strong>config.yaml</strong> - Must contain <code>dataset.image_width</code> and <code>dataset.image_height</code></li>
              <li><strong>mochi.json</strong> - Must contain <code>stages[0].type</code> and <code>stages[0].input_shape</code></li>
              <li><strong>export_artifacts/manifest.json</strong> - Model metadata with <code>model_graph</code> and <code>input_shape</code></li>
              <li><strong>export_artifacts/*.pt</strong> - PyTorch model file (single .pt file)</li>
            </ul>
            <Box variant="h4">Validation Rules:</Box>
            <ul>
              <li>Image dimensions in config.yaml must match input_shape[2] (height) and input_shape[3] (width)</li>
              <li>input_shape must be [batch, channels, height, width] format</li>
              <li>All dimension values must be positive integers</li>
              <li>Model file must be PyTorch 1.8 compatible</li>
            </ul>
          </SpaceBetween>
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
}
