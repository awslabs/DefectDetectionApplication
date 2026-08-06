/**
 * Build infrastructure configuration settings section
 * (portal-build-fleet-and-workflow-gates Requirements 9.1, 9.5).
 *
 * Lets a PortalAdmin configure the build compute parameters read by the
 * Build_Manager on dispatch and server launch: the dedicated/ephemeral
 * instance type per CPU architecture, volume size, AWS region, maximum
 * Build_Job runtime, spot usage for ephemeral runners, and the source
 * ref built from. Values are validated by the backend on save; a
 * rejected update is discarded in full (atomic reject) and each
 * per-parameter error from `PUT /build-config` (CONFIG_INVALID
 * details.errors) is surfaced on the matching form field.
 *
 * A blank field reverts the parameter to its documented default
 * (Requirement 9.2): the field is sent as null and the backend applies
 * the default on read. Users without the PortalAdmin role see an access
 * notice instead of the form (changes are PortalAdmin-only, Req 9.6).
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Form,
  FormField,
  Header,
  Input,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import type {
  BuildConfigValidationError,
  BuildInfrastructureConfig,
  BuildInfrastructureConfigUpdate,
} from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { getErrorMessage } from '../utils/errorHandling';

/** Text form state: every parameter as entered (booleans excepted). */
interface BuildConfigFormState {
  arm64_instance_type: string;
  x86_64_instance_type: string;
  volume_size_gb: string;
  region: string;
  max_runtime_hours: string;
  use_spot_for_ephemeral: boolean;
  source_ref: string;
}

type FieldErrors = Partial<Record<keyof BuildConfigFormState, string>>;

function toFormState(config: BuildInfrastructureConfig): BuildConfigFormState {
  return {
    arm64_instance_type: config.arm64_instance_type ?? '',
    x86_64_instance_type: config.x86_64_instance_type ?? '',
    volume_size_gb: config.volume_size_gb == null ? '' : String(config.volume_size_gb),
    region: config.region ?? '',
    max_runtime_hours: config.max_runtime_hours == null ? '' : String(config.max_runtime_hours),
    use_spot_for_ephemeral: Boolean(config.use_spot_for_ephemeral),
    // null source_ref means "the repository's default branch" and
    // renders as a blank field (never the literal string "null").
    source_ref: config.source_ref == null ? '' : String(config.source_ref),
  };
}

/**
 * A text field on the wire: blank reverts to the documented default
 * (null); anything else is sent as entered so the backend names the
 * rejection (Requirement 9.5).
 */
function textValue(raw: string): string | null {
  const trimmed = raw.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * A numeric field on the wire: blank reverts to the default (null); a
 * parseable number is sent as a number; anything else is sent verbatim
 * so the backend rejects it with a per-parameter error surfaced on the
 * field (Requirement 9.5) instead of being silently dropped.
 */
function numericValue(raw: string): number | string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : raw;
}

/**
 * Map the CONFIG_INVALID per-parameter errors onto form fields
 * (details.errors: [{parameter, rule, message}]). Errors without a
 * recognizable parameter are returned in `unassigned` for the
 * top-level alert.
 */
export function mapConfigErrors(errors: BuildConfigValidationError[]): {
  fieldErrors: FieldErrors;
  unassigned: string[];
} {
  const fieldErrors: FieldErrors = {};
  const unassigned: string[] = [];
  for (const error of errors) {
    const parameter = error.parameter as keyof BuildConfigFormState | undefined;
    if (parameter && parameter in EMPTY_FORM) {
      // First error per field wins; further ones join the alert.
      if (!fieldErrors[parameter]) {
        fieldErrors[parameter] = error.message;
        continue;
      }
    }
    unassigned.push(error.message);
  }
  return { fieldErrors, unassigned };
}

const EMPTY_FORM: BuildConfigFormState = {
  arm64_instance_type: '',
  x86_64_instance_type: '',
  volume_size_gb: '',
  region: '',
  max_runtime_hours: '',
  use_spot_for_ephemeral: false,
  source_ref: '',
};

export default function BuildInfrastructureSettings() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [form, setForm] = useState<BuildConfigFormState>(EMPTY_FORM);

  useEffect(() => {
    if (!isPortalAdmin) return;
    let cancelled = false;
    setLoading(true);
    apiService
      .getBuildConfig()
      .then((response) => {
        if (!cancelled) setForm(toFormState(response.config));
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(getErrorMessage(err, 'Failed to load the build configuration'));
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
        The build infrastructure configuration can only be changed by Portal Admins.
      </Alert>
    );
  }

  const setField = (field: keyof BuildConfigFormState) => ({ detail }: { detail: { value: string } }) => {
    setForm((current) => ({ ...current, [field]: detail.value }));
  };

  const handleSave = async () => {
    setError('');
    setSuccess('');
    setFieldErrors({});

    const update: BuildInfrastructureConfigUpdate = {
      arm64_instance_type: textValue(form.arm64_instance_type),
      x86_64_instance_type: textValue(form.x86_64_instance_type),
      volume_size_gb: numericValue(form.volume_size_gb),
      region: textValue(form.region),
      max_runtime_hours: numericValue(form.max_runtime_hours),
      use_spot_for_ephemeral: form.use_spot_for_ephemeral,
      source_ref: textValue(form.source_ref),
    };

    setSaving(true);
    try {
      const response = await apiService.updateBuildConfig(update);
      setForm(toFormState(response.config));
      setSuccess(
        response.changes.length > 0
          ? `Build configuration saved (${response.changes.length} ` +
            `parameter${response.changes.length === 1 ? '' : 's'} changed)`
          : 'Build configuration saved (no parameter changed)'
      );
    } catch (err: unknown) {
      // PUT /build-config rejects an invalid update atomically with
      // CONFIG_INVALID and per-parameter errors in details.errors
      // (Requirement 9.5): surface each error on its form field.
      const anyErr = err as { code?: string; details?: { errors?: BuildConfigValidationError[] } };
      if (anyErr?.code === 'CONFIG_INVALID' && Array.isArray(anyErr.details?.errors)) {
        const { fieldErrors: mapped, unassigned } = mapConfigErrors(anyErr.details!.errors!);
        setFieldErrors(mapped);
        setError(
          [
            'The configuration update was rejected; the prior values are retained.',
            ...unassigned,
          ].join(' ')
        );
      } else {
        setError(getErrorMessage(err, 'Failed to save the build configuration'));
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="l">
        <Spinner /> Loading build configuration...
      </Box>
    );
  }

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
            description="Compute parameters for portal component builds: dedicated server launches and ephemeral build runners. Leave a field blank to use its documented default. Only Portal Admins can change these settings."
          >
            Build Infrastructure
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
            label="ARM64 instance type"
            constraintText="Graviton EC2 instance type for ARM64 builds (default m6g.4xlarge)"
            errorText={fieldErrors.arm64_instance_type}
          >
            <Input
              value={form.arm64_instance_type}
              onChange={setField('arm64_instance_type')}
              placeholder="m6g.4xlarge"
              ariaLabel="ARM64 instance type"
            />
          </FormField>

          <FormField
            label="x86_64 instance type"
            constraintText="Intel/AMD EC2 instance type for x86_64 builds (default m6i.4xlarge)"
            errorText={fieldErrors.x86_64_instance_type}
          >
            <Input
              value={form.x86_64_instance_type}
              onChange={setField('x86_64_instance_type')}
              placeholder="m6i.4xlarge"
              ariaLabel="x86_64 instance type"
            />
          </FormField>

          <FormField
            label="Volume size (GB)"
            constraintText="Root volume size for build instances, a positive number (default 100)"
            errorText={fieldErrors.volume_size_gb}
          >
            <Input
              type="number"
              value={form.volume_size_gb}
              onChange={setField('volume_size_gb')}
              ariaLabel="Volume size GB"
            />
          </FormField>

          <FormField
            label="Region"
            constraintText="AWS region for build compute (default us-east-1)"
            errorText={fieldErrors.region}
          >
            <Input
              value={form.region}
              onChange={setField('region')}
              placeholder="us-east-1"
              ariaLabel="Region"
            />
          </FormField>

          <FormField
            label="Max runtime (hours)"
            constraintText="Maximum Build_Job runtime before the watchdog fails it, a positive duration (default 4)"
            errorText={fieldErrors.max_runtime_hours}
          >
            <Input
              type="number"
              value={form.max_runtime_hours}
              onChange={setField('max_runtime_hours')}
              ariaLabel="Max runtime hours"
            />
          </FormField>

          <FormField
            label="Ephemeral runners"
            constraintText="Spot capacity can be interrupted; interrupted Build_Jobs can be retried"
            errorText={fieldErrors.use_spot_for_ephemeral}
          >
            <Checkbox
              checked={form.use_spot_for_ephemeral}
              onChange={({ detail }) =>
                setForm((current) => ({ ...current, use_spot_for_ephemeral: detail.checked }))
              }
            >
              Use Spot instances for ephemeral build runners
            </Checkbox>
          </FormField>

          <FormField
            label="Source ref"
            constraintText="Git branch, tag, or commit built from — leave blank for the repository's default branch"
            errorText={fieldErrors.source_ref}
          >
            <Input
              value={form.source_ref}
              onChange={setField('source_ref')}
              placeholder="Repository default branch"
              ariaLabel="Source ref"
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}
