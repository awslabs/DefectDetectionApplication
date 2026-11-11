/**
 * Bedrock_Configuration settings section (workflow-manager Requirement 10.6).
 *
 * Lets a PortalAdmin configure the Amazon Bedrock model used by the
 * Workflow_Generator: model identifier, region, inference parameters
 * (max tokens, temperature, top_p), and the invocation timeout
 * (at most 240 seconds, Requirement 10.7). Users without the PortalAdmin
 * role see an access notice instead of the form.
 *
 * The default max tokens depends on the model: choosing another model in
 * the dropdown while max tokens holds the previous model's default
 * replaces it with the new model's default (`default_max_tokens` of the
 * model list); a value the admin entered is kept. When the form lacks the
 * previous or the new model's default (a typed model id, or a model
 * missing from the list), a model change saved with max tokens unchanged
 * leaves max_tokens out of the save, so the backend applies the new
 * model's default when none is stored and keeps a stored max tokens
 * otherwise. After a save the form shows the configuration the save
 * returns.
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

export const MAX_BEDROCK_TIMEOUT_SECONDS = 240;

interface BedrockFormState {
  model_id: string;
  region: string;
  max_tokens: string;
  temperature: string;
  top_p: string;
  timeout_seconds: string;
}

type FieldErrors = Partial<Record<keyof BedrockFormState, string>>;

// The Bedrock_Configuration as GET returns it and PUT echoes it back.
interface BedrockConfiguration {
  model_id: string;
  region: string;
  max_tokens: number;
  temperature: number | null;
  top_p: number | null;
  timeout_seconds: number;
}

function formFromConfiguration(config: BedrockConfiguration): BedrockFormState {
  return {
    model_id: config.model_id,
    region: config.region,
    max_tokens: String(config.max_tokens),
    // A null/undefined sampling parameter is "unset" and renders as
    // a blank field (never the literal string "null").
    temperature: config.temperature == null ? '' : String(config.temperature),
    top_p: config.top_p == null ? '' : String(config.top_p),
    timeout_seconds: String(config.timeout_seconds),
  };
}

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

  // Blank temperature / top_p means "unset": the sampling parameter is
  // omitted from Bedrock invocations so the model uses its own default.
  if (form.temperature.trim() !== '') {
    const temperature = Number(form.temperature);
    if (Number.isNaN(temperature) || temperature < 0 || temperature > 1) {
      errors.temperature = 'Temperature must be between 0 and 1';
    }
  }

  if (form.top_p.trim() !== '') {
    const topP = Number(form.top_p);
    if (Number.isNaN(topP) || topP < 0 || topP > 1) {
      errors.top_p = 'Top P must be between 0 and 1';
    }
  }

  const timeout = Number(form.timeout_seconds);
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > MAX_BEDROCK_TIMEOUT_SECONDS) {
    errors.timeout_seconds = `Timeout must be an integer between 1 and ${MAX_BEDROCK_TIMEOUT_SECONDS} seconds`;
  }

  return errors;
}

// Max tokens after a model change: a value equal to the previous model's
// default becomes the new model's default; any other value is kept, and so
// is every value when either model's default is unknown.
function maxTokensForModelChange(maxTokens: string, previousDefault?: number, nextDefault?: number): string {
  if (previousDefault === undefined || nextDefault === undefined) return maxTokens;
  return Number(maxTokens) === previousDefault ? String(nextDefault) : maxTokens;
}

// Whether a save leaves max_tokens out: the model changed, max tokens
// still holds the loaded value, and the form lacks the loaded or the new
// model's default, so it cannot tell whether that value fits the new
// model. The backend then applies the new model's default when no max
// tokens is stored, and keeps a stored one.
function leavesMaxTokensOut(
  modelId: string,
  maxTokens: number,
  loaded: BedrockConfiguration | null,
  defaults: Record<string, number>,
): boolean {
  if (loaded === null || modelId === loaded.model_id || maxTokens !== loaded.max_tokens) return false;
  return defaults[loaded.model_id] === undefined || defaults[modelId] === undefined;
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
  // Default max tokens per listed model id.
  const [modelDefaults, setModelDefaults] = useState<Record<string, number>>({});
  // The configuration the form was last loaded with: the GET response,
  // then each save's response.
  const [loadedConfig, setLoadedConfig] = useState<BedrockConfiguration | null>(null);

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
          const defaults: Record<string, number> = {};
          for (const m of response.models) {
            if (typeof m.default_max_tokens === 'number') defaults[m.id] = m.default_max_tokens;
          }
          setModelDefaults(defaults);
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
        setLoadedConfig(config);
        setForm(formFromConfiguration(config));
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

    const modelId = form.model_id.trim();
    const maxTokens = Number(form.max_tokens);
    const leaveOutMaxTokens = leavesMaxTokensOut(modelId, maxTokens, loadedConfig, modelDefaults);
    setSaving(true);
    try {
      const response = await apiService.updateBedrockConfiguration({
        model_id: modelId,
        region: form.region.trim(),
        ...(leaveOutMaxTokens ? {} : { max_tokens: maxTokens }),
        // A blank field is sent as an explicit null: the backend merges
        // provided keys over the current configuration, so omitting the
        // key would keep the old value instead of unsetting it.
        temperature: form.temperature.trim() === '' ? null : Number(form.temperature),
        top_p: form.top_p.trim() === '' ? null : Number(form.top_p),
        timeout_seconds: Number(form.timeout_seconds),
      });
      // Show the configuration the save returns: with max_tokens left
      // out, its max tokens can differ from the form's.
      setLoadedConfig(response.bedrock_configuration);
      setForm(formFromConfiguration(response.bedrock_configuration));
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
                  const modelId = detail.selectedOption.value ?? '';
                  setForm((current) => ({
                    ...current,
                    model_id: modelId,
                    max_tokens: maxTokensForModelChange(
                      current.max_tokens,
                      modelDefaults[current.model_id],
                      modelDefaults[modelId],
                    ),
                  }));
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
                placeholder="global.anthropic.claude-opus-5-5"
                ariaLabel="Model identifier"
              />
            )}
          </FormField>

          <FormField label="Region" constraintText="AWS region for Bedrock invocation" errorText={fieldErrors.region}>
            <Input value={form.region} onChange={setField('region')} placeholder="us-east-1" ariaLabel="Region" />
          </FormField>

          <FormField
            label="Max tokens"
            constraintText="Maximum tokens generated per response. The default depends on the model."
            errorText={fieldErrors.max_tokens}
          >
            <Input
              type="number"
              value={form.max_tokens}
              onChange={setField('max_tokens')}
              ariaLabel="Max tokens"
            />
          </FormField>

          <FormField
            label="Temperature"
            constraintText="0 to 1 — leave blank to let the model use its default (parameter is omitted)"
            errorText={fieldErrors.temperature}
          >
            <Input
              type="number"
              step={0.1}
              value={form.temperature}
              onChange={setField('temperature')}
              ariaLabel="Temperature"
            />
          </FormField>

          <FormField
            label="Top P"
            constraintText="0 to 1 — leave blank to let the model use its default (parameter is omitted)"
            errorText={fieldErrors.top_p}
          >
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
