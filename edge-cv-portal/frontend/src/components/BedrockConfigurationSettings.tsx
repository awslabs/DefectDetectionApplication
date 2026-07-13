/**
 * Bedrock_Configuration settings section (workflow-manager Requirement 10.6).
 *
 * Lets a PortalAdmin configure the Amazon Bedrock model used by the
 * Workflow_Generator: model identifier, region, inference parameters
 * (max tokens, temperature, top_p), and the invocation timeout
 * (at most 60 seconds, Requirement 10.7). Users without the PortalAdmin
 * role see an access notice instead of the form.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Form,
  FormField,
  Header,
  Input,
  Select,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { getErrorMessage } from '../utils/errorHandling';

export const MAX_BEDROCK_TIMEOUT_SECONDS = 60;

interface BedrockFormState {
  model_id: string;
  region: string;
  max_tokens: string;
  temperature: string;
  top_p: string;
  timeout_seconds: string;
}

type FieldErrors = Partial<Record<keyof BedrockFormState, string>>;

function validate(form: BedrockFormState): FieldErrors {
  const errors: FieldErrors = {};

  if (!form.model_id.trim()) {
    errors.model_id = 'Model identifier is required';
  }
  if (!form.region.trim()) {
    errors.region = 'Region is required';
  }

  const maxTokens = Number(form.max_tokens);
  if (!Number.isInteger(maxTokens) || maxTokens < 1) {
    errors.max_tokens = 'Max tokens must be a positive integer';
  }

  const temperature = Number(form.temperature);
  if (form.temperature.trim() === '' || Number.isNaN(temperature) || temperature < 0 || temperature > 1) {
    errors.temperature = 'Temperature must be between 0 and 1';
  }

  const topP = Number(form.top_p);
  if (form.top_p.trim() === '' || Number.isNaN(topP) || topP < 0 || topP > 1) {
    errors.top_p = 'Top P must be between 0 and 1';
  }

  const timeout = Number(form.timeout_seconds);
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > MAX_BEDROCK_TIMEOUT_SECONDS) {
    errors.timeout_seconds = `Timeout must be an integer between 1 and ${MAX_BEDROCK_TIMEOUT_SECONDS} seconds`;
  }

  return errors;
}

export default function BedrockConfigurationSettings() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [form, setForm] = useState<BedrockFormState>({
    model_id: '',
    region: '',
    max_tokens: '',
    temperature: '',
    top_p: '',
    timeout_seconds: '',
  });
  // Invokable model options for the model dropdown. null means the list
  // is unavailable (still loading, fetch failed, or missing backend
  // permissions) and the form falls back to free-text entry.
  const [modelOptions, setModelOptions] = useState<SelectProps.Option[] | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);

  useEffect(() => {
    if (!isPortalAdmin) return;
    let cancelled = false;
    setModelsLoading(true);
    apiService
      .getBedrockModels()
      .then((response) => {
        if (cancelled) return;
        if (response.models.length > 0) {
          setModelOptions(response.models.map((m) => ({ value: m.id, label: m.label })));
        }
      })
      .catch(() => {
        // Fall back silently to the free-text model id input.
        if (!cancelled) setModelOptions(null);
      })
      .finally(() => {
        if (!cancelled) setModelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isPortalAdmin]);

  useEffect(() => {
    if (!isPortalAdmin) return;
    let cancelled = false;
    setLoading(true);
    apiService
      .getBedrockConfiguration()
      .then((response) => {
        if (cancelled) return;
        const config = response.bedrock_configuration;
        setForm({
          model_id: config.model_id,
          region: config.region,
          max_tokens: String(config.max_tokens),
          temperature: String(config.temperature),
          top_p: String(config.top_p),
          timeout_seconds: String(config.timeout_seconds),
        });
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(getErrorMessage(err, 'Failed to load Bedrock configuration'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isPortalAdmin]);

  if (!isPortalAdmin) {
    return (
      <Alert type="info" header="Portal Admin access required">
        Bedrock configuration can only be viewed and changed by Portal Admins.
      </Alert>
    );
  }

  const setField = (field: keyof BedrockFormState) => ({ detail }: { detail: { value: string } }) => {
    setForm((current) => ({ ...current, [field]: detail.value }));
  };

  const handleSave = async () => {
    setError('');
    setSuccess('');
    const errors = validate(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setSaving(true);
    try {
      await apiService.updateBedrockConfiguration({
        model_id: form.model_id.trim(),
        region: form.region.trim(),
        max_tokens: Number(form.max_tokens),
        temperature: Number(form.temperature),
        top_p: Number(form.top_p),
        timeout_seconds: Number(form.timeout_seconds),
      });
      setSuccess('Bedrock configuration saved');
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to save Bedrock configuration'));
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="l">
        <Spinner /> Loading Bedrock configuration...
      </Box>
    );
  }

  // A stored model id that is not in the fetched list (custom id, or a
  // model no longer listed) is surfaced as a selectable option so it is
  // never silently dropped.
  const displayedModelOptions: SelectProps.Option[] =
    modelOptions && form.model_id && !modelOptions.some((option) => option.value === form.model_id)
      ? [{ value: form.model_id, label: form.model_id }, ...modelOptions]
      : (modelOptions ?? []);
  const selectedModelOption =
    displayedModelOptions.find((option) => option.value === form.model_id) ?? null;

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
          {success}
        </Alert>
      )}

      <Form
        header={
          <Header
            variant="h2"
            description="Amazon Bedrock model used for prompt-based workflow generation. Only Portal Admins can change these settings."
          >
            Bedrock Configuration
          </Header>
        }
        actions={
          <Button variant="primary" onClick={handleSave} loading={saving}>
            Save Configuration
          </Button>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Model identifier"
            constraintText="Bedrock model or inference profile id"
            errorText={fieldErrors.model_id}
          >
            {modelOptions ? (
              <Select
                selectedOption={selectedModelOption}
                options={displayedModelOptions}
                onChange={({ detail }) => {
                  setForm((current) => ({ ...current, model_id: detail.selectedOption.value ?? '' }));
                }}
                filteringType="auto"
                statusType={modelsLoading ? 'loading' : 'finished'}
                loadingText="Loading models"
                placeholder="Choose a model"
                ariaLabel="Model identifier"
              />
            ) : (
              <Input
                value={form.model_id}
                onChange={setField('model_id')}
                placeholder="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
                ariaLabel="Model identifier"
              />
            )}
          </FormField>

          <FormField label="Region" constraintText="AWS region for Bedrock invocation" errorText={fieldErrors.region}>
            <Input value={form.region} onChange={setField('region')} placeholder="us-east-1" ariaLabel="Region" />
          </FormField>

          <FormField
            label="Max tokens"
            constraintText="Maximum tokens generated per response"
            errorText={fieldErrors.max_tokens}
          >
            <Input
              type="number"
              value={form.max_tokens}
              onChange={setField('max_tokens')}
              ariaLabel="Max tokens"
            />
          </FormField>

          <FormField label="Temperature" constraintText="0 to 1" errorText={fieldErrors.temperature}>
            <Input
              type="number"
              step={0.1}
              value={form.temperature}
              onChange={setField('temperature')}
              ariaLabel="Temperature"
            />
          </FormField>

          <FormField label="Top P" constraintText="0 to 1" errorText={fieldErrors.top_p}>
            <Input type="number" step={0.1} value={form.top_p} onChange={setField('top_p')} ariaLabel="Top P" />
          </FormField>

          <FormField
            label="Timeout (seconds)"
            constraintText={`Invocation timeout, at most ${MAX_BEDROCK_TIMEOUT_SECONDS} seconds`}
            errorText={fieldErrors.timeout_seconds}
          >
            <Input
              type="number"
              value={form.timeout_seconds}
              onChange={setField('timeout_seconds')}
              ariaLabel="Timeout seconds"
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}
