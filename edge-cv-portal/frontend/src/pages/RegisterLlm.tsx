/**
 * Register LLM (vLLM) form — vllm-triton-inference task 15.1.
 *
 * Registers a vLLM_Model_Record through POST /api/v1/models/vllm
 * (Requirements 1.1, 1.2):
 * - source radio (Hugging Face ID / S3 artifact) keeps the two source
 *   inputs mutually exclusive in the UI, matching the API's XOR rule;
 * - engine settings render as an optional expandable section pre-filled
 *   with the documented defaults from GET /models/vllm/engine-spec;
 * - a 400 rejection carries a finding list `{field, value, reason}` that
 *   is surfaced per field.
 */
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  ExpandableSection,
  Form,
  FormField,
  Header,
  Input,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
} from '@cloudscape-design/components';
import {
  apiService,
  ApiError,
  VllmEngineSpec,
  VllmRegistrationFinding,
} from '../services/api';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';

type SourceType = 'huggingface' | 's3';

/** The API's XOR finding names both source fields at once. */
const SOURCE_XOR_FIELD = 'huggingface_model_id | s3_model_artifact';

/**
 * Convert one engine form value (kept as a string / boolean in the UI)
 * into the JSON value the API expects. Non-numeric strings are passed
 * through untouched so the backend produces its per-field finding.
 */
export function toEngineJsonValue(
  specType: string,
  value: string | boolean
): string | number | boolean {
  if (typeof value === 'boolean') return value;
  if (specType === 'integer') {
    return /^-?\d+$/.test(value.trim()) ? parseInt(value.trim(), 10) : value;
  }
  if (specType === 'number') {
    const parsed = Number(value.trim());
    return value.trim() !== '' && !Number.isNaN(parsed) ? parsed : value;
  }
  return value;
}

export default function RegisterLlm() {
  const navigate = useNavigate();

  // Form state
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('1.0');
  const [description, setDescription] = useState('');

  // Source XOR state: one radio choice, one visible input (1.1)
  const [sourceType, setSourceType] = useState<SourceType>('huggingface');
  const [hfModelId, setHfModelId] = useState('');
  const [s3Artifact, setS3Artifact] = useState('');

  // Engine settings, pre-filled from the engine-spec endpoint (1.2)
  const [engineSpec, setEngineSpec] = useState<VllmEngineSpec | null>(null);
  const [engineValues, setEngineValues] = useState<Record<string, string | boolean>>({});
  const [engineExpanded, setEngineExpanded] = useState(false);

  // Submission state
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const response = await apiService.listUseCases();
        setUseCases(response.usecases || []);
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError('Failed to load use cases');
      }
      try {
        const spec = await apiService.getVllmEngineSpec();
        setEngineSpec(spec);
        // Pre-fill every engine setting with its documented default
        const defaults: Record<string, string | boolean> = {};
        Object.entries(spec.settings).forEach(([key, setting]) => {
          defaults[key] =
            setting.type === 'boolean'
              ? Boolean(setting.default)
              : String(setting.default);
        });
        setEngineValues(defaults);
      } catch (err) {
        console.error('Failed to load vLLM engine spec:', err);
        setError('Failed to load the vLLM engine settings specification');
      }
    };
    load();
  }, []);

  const clearFieldError = (field: string) => {
    setFieldErrors((prev) => {
      if (!(field in prev)) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });
  };

  // Switching the source clears the other input and both source errors,
  // keeping the two inputs mutually exclusive in the UI (API XOR, 1.1)
  const handleSourceChange = (value: SourceType) => {
    setSourceType(value);
    if (value === 'huggingface') {
      setS3Artifact('');
    } else {
      setHfModelId('');
    }
    clearFieldError('huggingface_model_id');
    clearFieldError('s3_model_artifact');
    clearFieldError(SOURCE_XOR_FIELD);
  };

  const handleSubmit = async () => {
    if (!selectedUseCase?.value || !modelName.trim() || !modelVersion.trim()) {
      setError('Please select a use case and fill in the model name and version');
      scrollToTop();
      return;
    }

    setSubmitting(true);
    setError(null);
    setFieldErrors({});

    const engineConfiguration: Record<string, string | number | boolean> = {};
    if (engineSpec) {
      Object.entries(engineValues).forEach(([key, value]) => {
        const setting = engineSpec.settings[key];
        if (setting) {
          engineConfiguration[key] = toEngineJsonValue(setting.type, value);
        }
      });
    }

    try {
      const result = await apiService.registerVllmModel({
        usecase_id: selectedUseCase.value,
        model_name: modelName.trim(),
        model_version: modelVersion.trim(),
        // Exactly one source, matching the API's XOR (1.1)
        ...(sourceType === 'huggingface' && hfModelId.trim()
          ? { huggingface_model_id: hfModelId.trim() }
          : {}),
        ...(sourceType === 's3' && s3Artifact.trim()
          ? { s3_model_artifact: s3Artifact.trim() }
          : {}),
        ...(Object.keys(engineConfiguration).length > 0
          ? { engine_configuration: engineConfiguration }
          : {}),
        ...(description.trim() ? { description: description.trim() } : {}),
      });

      setSuccess(
        `LLM registered successfully and is eligible for publish. Model ID: ${result.training_id}`
      );
      setTimeout(() => navigate(`/models/${result.training_id}`), 2000);
    } catch (err) {
      console.error('vLLM registration error:', err);
      // Surface the API's validation findings per field (1.1, 1.2)
      const findings =
        err instanceof ApiError && Array.isArray(err.details?.findings)
          ? (err.details.findings as VllmRegistrationFinding[])
          : [];
      if (findings.length > 0) {
        const perField: Record<string, string> = {};
        findings.forEach((finding) => {
          perField[finding.field] = finding.reason;
        });
        setFieldErrors(perField);
        if (
          Object.keys(perField).some((field) =>
            field.startsWith('engine_configuration')
          )
        ) {
          setEngineExpanded(true);
        }
        setError('Registration failed validation. Fix the highlighted fields and retry.');
      } else {
        setError(getErrorMessage(err, 'Failed to register the LLM'));
      }
      scrollToTop();
    } finally {
      setSubmitting(false);
    }
  };

  const engineFieldError = (key: string) => fieldErrors[`engine_configuration.${key}`];

  const renderEngineField = (key: string) => {
    if (!engineSpec) return null;
    const setting = engineSpec.settings[key];
    const value = engineValues[key];
    const label = key.replace(/_/g, ' ');
    const onChange = (next: string | boolean) => {
      setEngineValues((prev) => ({ ...prev, [key]: next }));
      clearFieldError(`engine_configuration.${key}`);
    };

    if (setting.type === 'boolean') {
      return (
        <FormField
          key={key}
          label={label}
          description={setting.description}
          errorText={engineFieldError(key)}
        >
          <Checkbox
            checked={Boolean(value)}
            onChange={({ detail }) => onChange(detail.checked)}
          >
            Enabled (default: {String(setting.default)})
          </Checkbox>
        </FormField>
      );
    }

    if (setting.accepted_values && setting.accepted_values.length > 0) {
      return (
        <FormField
          key={key}
          label={label}
          description={setting.description}
          constraintText={`Accepted values: ${setting.accepted_values.join(', ')} (default: ${setting.default})`}
          errorText={engineFieldError(key)}
        >
          <Select
            selectedOption={{ label: String(value), value: String(value) }}
            onChange={({ detail }) => onChange(detail.selectedOption.value || '')}
            options={setting.accepted_values.map((v) => ({ label: v, value: v }))}
          />
        </FormField>
      );
    }

    return (
      <FormField
        key={key}
        label={label}
        description={setting.description}
        constraintText={
          setting.range
            ? `Accepted range: ${setting.range} (default: ${setting.default})`
            : `Default: ${setting.default}`
        }
        errorText={engineFieldError(key)}
      >
        <Input
          value={String(value ?? '')}
          onChange={({ detail }) => onChange(detail.value)}
          inputMode={setting.type === 'integer' ? 'numeric' : 'decimal'}
        />
      </FormField>
    );
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      {success && <Alert type="success">{success}</Alert>}

      <Container
        header={
          <Header
            variant="h1"
            description="Register a large language model served on-device through the Triton vLLM backend. Registered LLMs skip labeling and training and are publish-eligible immediately."
          >
            Register LLM (vLLM)
          </Header>
        }
      >
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate('/models')}>Cancel</Button>
              <Button
                variant="primary"
                onClick={handleSubmit}
                loading={submitting}
                disabled={
                  submitting ||
                  !selectedUseCase?.value ||
                  !modelName.trim() ||
                  !modelVersion.trim()
                }
              >
                Register LLM
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            <FormField label="Use Case" description="Select the use case to register the LLM into">
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
              label="Model Name"
              description="A descriptive name for the LLM"
              constraintText="Use alphanumeric characters and hyphens"
            >
              <Input
                value={modelName}
                onChange={({ detail }) => setModelName(detail.value)}
                placeholder="plant-qa-llm"
              />
            </FormField>

            <FormField label="Model Version" description="Version identifier for the model">
              <Input
                value={modelVersion}
                onChange={({ detail }) => setModelVersion(detail.value)}
                placeholder="1.0"
              />
            </FormField>

            <FormField
              label="Model Source"
              description="Exactly one source must be provided"
              errorText={fieldErrors[SOURCE_XOR_FIELD]}
            >
              <RadioGroup
                value={sourceType}
                onChange={({ detail }) => handleSourceChange(detail.value as SourceType)}
                items={[
                  {
                    value: 'huggingface',
                    label: 'Hugging Face model ID',
                    description: 'A model identifier on the Hugging Face Hub',
                  },
                  {
                    value: 's3',
                    label: 'S3 model artifact',
                    description: 'An archive of LLM weights and tokenizer files in S3',
                  },
                ]}
              />
            </FormField>

            {sourceType === 'huggingface' ? (
              <FormField
                label="Hugging Face Model ID"
                constraintText="Format: {organization}/{model_name} (e.g. facebook/opt-125m)"
                errorText={fieldErrors['huggingface_model_id']}
              >
                <Input
                  value={hfModelId}
                  onChange={({ detail }) => {
                    setHfModelId(detail.value);
                    clearFieldError('huggingface_model_id');
                    clearFieldError(SOURCE_XOR_FIELD);
                  }}
                  placeholder="facebook/opt-125m"
                />
              </FormField>
            ) : (
              <FormField
                label="S3 Model Artifact"
                constraintText="Format: s3:// URI ending in .tar.gz, readable from the use case account"
                errorText={fieldErrors['s3_model_artifact']}
              >
                <Input
                  value={s3Artifact}
                  onChange={({ detail }) => {
                    setS3Artifact(detail.value);
                    clearFieldError('s3_model_artifact');
                    clearFieldError(SOURCE_XOR_FIELD);
                  }}
                  placeholder="s3://my-bucket/models/llm.tar.gz"
                />
              </FormField>
            )}

            <FormField label="Description" description="Optional description of the model">
              <Input
                value={description}
                onChange={({ detail }) => setDescription(detail.value)}
                placeholder="On-device LLM for defect triage"
              />
            </FormField>

            <ExpandableSection
              headerText="Engine settings (optional)"
              headerDescription="Pre-filled with the documented defaults; omitted settings receive their defaults at registration time"
              expanded={engineExpanded}
              onChange={({ detail }) => setEngineExpanded(detail.expanded)}
            >
              {engineSpec ? (
                <SpaceBetween size="m">
                  {fieldErrors['engine_configuration'] && (
                    <Alert type="error">{fieldErrors['engine_configuration']}</Alert>
                  )}
                  {Object.keys(engineSpec.settings).map((key) => renderEngineField(key))}
                </SpaceBetween>
              ) : (
                <Box color="text-status-inactive">
                  Engine settings specification is unavailable; the documented defaults
                  will be applied at registration time.
                </Box>
              )}
            </ExpandableSection>
          </SpaceBetween>
        </Form>
      </Container>
    </SpaceBetween>
  );
}
