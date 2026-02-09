import { useState, useEffect } from 'react';
import {
  SpaceBetween,
  Container,
  Header,
  Form,
  FormField,
  Input,
  Select,
  Checkbox,
  Button,
  Box,
  Alert,
  ColumnLayout,
  Multiselect,
  MultiselectProps,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface ComponentParameter {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'select';
  default: any;
  description: string;
  required: boolean;
  validation?: { min?: number; max?: number };
  options?: Array<{ label: string; value: any }>;
  envVar?: string;
}

interface ComponentSchema {
  component_name: string;
  displayName: string;
  description: string;
  parameters: Record<string, ComponentParameter>;
}

interface Props {
  componentName: string;
  usecaseId: string;
  availableDevices: Array<{ device_id: string; device_name: string }>;
  onConfigurationSaved?: (deploymentId: string) => void;
  onCancel?: () => void;
}

export default function ComponentConfigurationForm(props: Props) {
  const { componentName, usecaseId, availableDevices, onConfigurationSaved, onCancel } = props;

  const [schema, setSchema] = useState<ComponentSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Form state
  const [configuration, setConfiguration] = useState<Record<string, any>>({});
  const [selectedDevices, setSelectedDevices] = useState<MultiselectProps.Option[]>([]);
  const [deploymentName, setDeploymentName] = useState('');
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});

  // Load component schema on mount
  useEffect(() => {
    loadSchema();
  }, [componentName]);

  // Initialize form with defaults
  useEffect(() => {
    if (schema) {
      const defaults: Record<string, any> = {};
      Object.entries(schema.parameters).forEach(([key, param]) => {
        defaults[key] = param.default;
      });
      setConfiguration(defaults);

      // Set default deployment name
      const timestamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
      setDeploymentName(`${componentName}-config-${timestamp}`);
    }
  }, [schema, componentName]);

  const loadSchema = async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await apiService.getComponentConfigurationSchema(componentName);
      setSchema(response);
    } catch (err) {
      console.error('Failed to load component schema:', err);
      setError(err instanceof Error ? err.message : 'Failed to load component schema');
    } finally {
      setLoading(false);
    }
  };

  const validateConfiguration = (): boolean => {
    const errors: Record<string, string> = {};

    if (!schema) return false;

    Object.entries(schema.parameters).forEach(([key, param]) => {
      const value = configuration[key];

      // Check required
      if (param.required && (value === undefined || value === null || value === '')) {
        errors[key] = `${param.name} is required`;
        return;
      }

      // Type validation
      if (value !== undefined && value !== null && value !== '') {
        if (param.type === 'number') {
          if (isNaN(Number(value))) {
            errors[key] = `${param.name} must be a number`;
            return;
          }

          const numValue = Number(value);
          if (param.validation?.min !== undefined && numValue < param.validation.min) {
            errors[key] = `${param.name} must be >= ${param.validation.min}`;
            return;
          }
          if (param.validation?.max !== undefined && numValue > param.validation.max) {
            errors[key] = `${param.name} must be <= ${param.validation.max}`;
            return;
          }
        }
      }
    });

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleParameterChange = (key: string, value: any) => {
    setConfiguration((prev) => ({
      ...prev,
      [key]: value,
    }));
    // Clear error for this field
    if (validationErrors[key]) {
      setValidationErrors((prev) => {
        const newErrors = { ...prev };
        delete newErrors[key];
        return newErrors;
      });
    }
  };

  const handleSubmit = async () => {
    if (!validateConfiguration()) {
      setError('Please fix validation errors');
      return;
    }

    if (selectedDevices.length === 0) {
      setError('Please select at least one device');
      return;
    }

    try {
      setSubmitting(true);
      setError(null);

      const response = await apiService.configureComponent({
        component_name: componentName,
        usecase_id: usecaseId,
        configuration,
        target_devices: selectedDevices.map((d) => d.value as string),
        deployment_name: deploymentName,
      });

      if (onConfigurationSaved) {
        onConfigurationSaved(response.deployment_id);
      }
    } catch (err) {
      console.error('Failed to configure component:', err);
      setError(err instanceof Error ? err.message : 'Failed to configure component');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading component configuration schema...
        </Box>
      </Container>
    );
  }

  if (!schema) {
    return (
      <Container>
        <Alert type="error">Failed to load component configuration schema</Alert>
      </Container>
    );
  }

  const renderParameterField = (key: string, param: ComponentParameter) => {
    const value = configuration[key];
    const hasError = !!validationErrors[key];

    switch (param.type) {
      case 'number':
        return (
          <FormField
            key={key}
            label={param.name}
            description={param.description}
            errorText={validationErrors[key]}
          >
            <Input
              type="number"
              value={String(value ?? '')}
              onChange={({ detail }) => handleParameterChange(key, detail.value ? Number(detail.value) : '')}
              placeholder={String(param.default)}
              invalid={hasError}
            />
          </FormField>
        );

      case 'string':
        return (
          <FormField
            key={key}
            label={param.name}
            description={param.description}
            errorText={validationErrors[key]}
          >
            <Input
              type="text"
              value={String(value ?? '')}
              onChange={({ detail }) => handleParameterChange(key, detail.value)}
              placeholder={String(param.default)}
              invalid={hasError}
            />
          </FormField>
        );

      case 'boolean':
        return (
          <FormField key={key} label={param.name} description={param.description}>
            <Checkbox
              checked={value ?? param.default}
              onChange={({ detail }) => handleParameterChange(key, detail.checked)}
            >
              {param.name}
            </Checkbox>
          </FormField>
        );

      case 'select':
        return (
          <FormField
            key={key}
            label={param.name}
            description={param.description}
            errorText={validationErrors[key]}
          >
            <Select
              selectedOption={
                param.options?.find((opt) => opt.value === value) || {
                  label: String(value ?? param.default),
                  value: value ?? param.default,
                }
              }
              onChange={({ detail }) => handleParameterChange(key, detail.selectedOption.value)}
              options={param.options || []}
              invalid={hasError}
            />
          </FormField>
        );

      default:
        return null;
    }
  };

  // Build environment variables preview
  const envVarsPreview: Record<string, string> = {};
  Object.entries(schema.parameters).forEach(([key, param]) => {
    if (param.envVar && configuration[key] !== undefined && configuration[key] !== null) {
      const value = configuration[key];
      if (typeof value === 'boolean') {
        envVarsPreview[param.envVar] = value ? 'true' : 'false';
      } else {
        envVarsPreview[param.envVar] = String(value);
      }
    }
  });

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={schema.description}
          >
            Configure {schema.displayName}
          </Header>
        }
      >
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={onCancel}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSubmit} loading={submitting}>
                Create Deployment
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            {/* Deployment Name */}
            <FormField label="Deployment Name" description="Name for this configuration deployment">
              <Input
                value={deploymentName}
                onChange={({ detail }) => setDeploymentName(detail.value)}
                placeholder="e.g., InferenceUploader-Config-v2"
              />
            </FormField>

            {/* Target Devices */}
            <FormField
              label="Target Devices"
              description="Select devices to deploy this configuration to"
              errorText={selectedDevices.length === 0 ? 'At least one device is required' : undefined}
            >
              <Multiselect
                selectedOptions={selectedDevices}
                onChange={({ detail }) => setSelectedDevices([...detail.selectedOptions])}
                options={availableDevices.map((device) => ({
                  label: device.device_name,
                  value: device.device_id,
                }))}
                placeholder="Select devices..."
                invalid={selectedDevices.length === 0}
              />
            </FormField>

            {/* Component Parameters */}
            <Container header={<Header variant="h3">Component Parameters</Header>}>
              <SpaceBetween size="m">
                {Object.entries(schema.parameters).map(([key, param]) => renderParameterField(key, param))}
              </SpaceBetween>
            </Container>

            {/* Environment Variables Preview */}
            <Container header={<Header variant="h3">Environment Variables Preview</Header>}>
              <Box>
                {Object.keys(envVarsPreview).length > 0 ? (
                  <pre
                    style={{
                      backgroundColor: '#f4f4f4',
                      padding: '12px',
                      borderRadius: '4px',
                      overflow: 'auto',
                      fontSize: '13px',
                      fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {Object.entries(envVarsPreview)
                      .map(([key, value]) => `${key}=${value}`)
                      .join('\n')}
                  </pre>
                ) : (
                  <Box color="text-body-secondary">No environment variables configured</Box>
                )}
              </Box>
            </Container>

            {/* Summary */}
            <Container header={<Header variant="h3">Deployment Summary</Header>}>
              <ColumnLayout columns={2} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Component</Box>
                  <div>{schema.displayName}</div>
                </div>
                <div>
                  <Box variant="awsui-key-label">Target Devices</Box>
                  <div>{selectedDevices.length} device(s) selected</div>
                </div>
                <div>
                  <Box variant="awsui-key-label">Configuration Parameters</Box>
                  <div>{Object.keys(configuration).length} parameters</div>
                </div>
                <div>
                  <Box variant="awsui-key-label">Environment Variables</Box>
                  <div>{Object.keys(envVarsPreview).length} variables</div>
                </div>
              </ColumnLayout>
            </Container>
          </SpaceBetween>
        </Form>
      </Container>
    </SpaceBetween>
  );
}
