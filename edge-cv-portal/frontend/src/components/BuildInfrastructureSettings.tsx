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
 *
 * build-fleet-execution-failures tasks 8.4/8.5 add two OPTIONAL
 * sections while retaining every existing field and the legacy payload
 * shape (Req 2.17, 2.19, 2.20, 3.6, 3.12, 3.13):
 *
 * - Per-target/mode runtime budgets (`runtime_budgets`): hard runtime
 *   ceiling (non-extendable), heartbeat lease, progress-stall lease,
 *   and the independent OPTIONAL queue-wait and provisioning limits.
 * - Per-target ephemeral volume sizes (`volume_size_gb_by_target`);
 *   the global volume size stays (documented default now 200 GB) and
 *   any JP6 entry must be at least 200 GB.
 *
 * Both maps are omitted from the payload while unconfigured (legacy
 * payloads preserved) and sent as null to revert when the stored maps
 * are cleared. Changing any of these settings never mutates existing
 * Build_Jobs: every job keeps the budgets and volume size snapshotted
 * at its submission.
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

/**
 * One editable runtime-budget row (`runtime_budgets` map entry): a
 * build target, an execution mode (or `default` for both modes), and
 * the five optional budget values, all as entered.
 */
interface RuntimeBudgetRow {
  target: string;
  mode: string;
  hard_runtime_hours: string;
  heartbeat_lease_minutes: string;
  progress_stall_minutes: string;
  queue_wait_hours: string;
  provisioning_minutes: string;
}

/** One editable per-target volume-size row. */
interface VolumeByTargetRow {
  target: string;
  volume_size_gb: string;
}

const EMPTY_BUDGET_ROW: RuntimeBudgetRow = {
  target: '',
  mode: '',
  hard_runtime_hours: '',
  heartbeat_lease_minutes: '',
  progress_stall_minutes: '',
  queue_wait_hours: '',
  provisioning_minutes: '',
};

const BUDGET_VALUE_KEYS = [
  'hard_runtime_hours',
  'heartbeat_lease_minutes',
  'progress_stall_minutes',
  'queue_wait_hours',
  'provisioning_minutes',
] as const;

/**
 * The effective configuration may additionally carry the OPTIONAL
 * runtime-budget and per-target volume maps (null while unconfigured).
 * Declared locally so the shared api.ts types stay untouched.
 */
type ExtendedBuildConfig = BuildInfrastructureConfig & {
  runtime_budgets?: Record<string, Record<string, Record<string, unknown>>> | null;
  volume_size_gb_by_target?: Record<string, unknown> | null;
};

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

/** Flatten the stored runtime_budgets map into editable rows. */
export function toBudgetRows(
  budgets: ExtendedBuildConfig['runtime_budgets'],
): RuntimeBudgetRow[] {
  if (!budgets || typeof budgets !== 'object') return [];
  const rows: RuntimeBudgetRow[] = [];
  for (const [target, perMode] of Object.entries(budgets)) {
    if (!perMode || typeof perMode !== 'object') continue;
    for (const [mode, entry] of Object.entries(perMode)) {
      const row: RuntimeBudgetRow = { ...EMPTY_BUDGET_ROW, target, mode };
      if (entry && typeof entry === 'object') {
        for (const key of BUDGET_VALUE_KEYS) {
          const value = (entry as Record<string, unknown>)[key];
          if (value != null) row[key] = String(value);
        }
      }
      rows.push(row);
    }
  }
  return rows;
}

/** Flatten the stored volume_size_gb_by_target map into editable rows. */
export function toVolumeRows(
  byTarget: ExtendedBuildConfig['volume_size_gb_by_target'],
): VolumeByTargetRow[] {
  if (!byTarget || typeof byTarget !== 'object') return [];
  return Object.entries(byTarget).map(([target, size]) => ({
    target,
    volume_size_gb: size == null ? '' : String(size),
  }));
}

/**
 * Assemble the runtime_budgets wire map from the edited rows. Rows that
 * are entirely blank are skipped; a blank mode means `default` (both
 * modes). Values are sent as entered (numericValue) so the backend can
 * name each rejection. Returns null when no row survives (the caller
 * decides between omitting the key and reverting with null).
 */
export function budgetRowsToMap(
  rows: RuntimeBudgetRow[],
): Record<string, Record<string, Record<string, number | string>>> | null {
  const budgets: Record<string, Record<string, Record<string, number | string>>> = {};
  for (const row of rows) {
    const target = row.target.trim();
    const blank =
      target === '' &&
      row.mode.trim() === '' &&
      BUDGET_VALUE_KEYS.every((key) => row[key].trim() === '');
    if (blank) continue;
    const mode = row.mode.trim() === '' ? 'default' : row.mode.trim();
    const entry: Record<string, number | string> = {};
    for (const key of BUDGET_VALUE_KEYS) {
      const value = numericValue(row[key]);
      if (value !== null) entry[key] = value;
    }
    if (!budgets[target]) budgets[target] = {};
    budgets[target][mode] = entry;
  }
  return Object.keys(budgets).length > 0 ? budgets : null;
}

/** Assemble the volume_size_gb_by_target wire map from the edited rows. */
export function volumeRowsToMap(
  rows: VolumeByTargetRow[],
): Record<string, number | string> | null {
  const byTarget: Record<string, number | string> = {};
  for (const row of rows) {
    const target = row.target.trim();
    if (target === '' && row.volume_size_gb.trim() === '') continue;
    const value = numericValue(row.volume_size_gb);
    byTarget[target] = value === null ? '' : value;
  }
  return Object.keys(byTarget).length > 0 ? byTarget : null;
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
  const [budgetRows, setBudgetRows] = useState<RuntimeBudgetRow[]>([]);
  const [volumeRows, setVolumeRows] = useState<VolumeByTargetRow[]>([]);
  // Whether the stored configuration carried the optional maps: cleared
  // rows then revert with null; otherwise the keys are omitted so an
  // unconfigured save keeps the exact legacy payload shape.
  const [storedHadBudgets, setStoredHadBudgets] = useState(false);
  const [storedHadVolumeMap, setStoredHadVolumeMap] = useState(false);

  const applyConfig = (config: BuildInfrastructureConfig) => {
    const extended = config as ExtendedBuildConfig;
    setForm(toFormState(config));
    setBudgetRows(toBudgetRows(extended.runtime_budgets));
    setVolumeRows(toVolumeRows(extended.volume_size_gb_by_target));
    setStoredHadBudgets(extended.runtime_budgets != null);
    setStoredHadVolumeMap(extended.volume_size_gb_by_target != null);
  };

  useEffect(() => {
    if (!isPortalAdmin) return;
    let cancelled = false;
    setLoading(true);
    apiService
      .getBuildConfig()
      .then((response) => {
        if (!cancelled) applyConfig(response.config);
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

  const setBudgetRow = (index: number, field: keyof RuntimeBudgetRow, value: string) => {
    setBudgetRows((rows) => rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  };

  const setVolumeRow = (index: number, field: keyof VolumeByTargetRow, value: string) => {
    setVolumeRows((rows) => rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  };

  const handleSave = async () => {
    setError('');
    setSuccess('');
    setFieldErrors({});

    const update: Record<string, unknown> = {
      arm64_instance_type: textValue(form.arm64_instance_type),
      x86_64_instance_type: textValue(form.x86_64_instance_type),
      volume_size_gb: numericValue(form.volume_size_gb),
      region: textValue(form.region),
      max_runtime_hours: numericValue(form.max_runtime_hours),
      use_spot_for_ephemeral: form.use_spot_for_ephemeral,
      source_ref: textValue(form.source_ref),
    };
    // OPTIONAL maps: omitted while unconfigured (legacy payload
    // preserved); null reverts a previously stored map (Req 3.6).
    const budgets = budgetRowsToMap(budgetRows);
    if (budgets !== null || storedHadBudgets) {
      update.runtime_budgets = budgets;
    }
    const volumesByTarget = volumeRowsToMap(volumeRows);
    if (volumesByTarget !== null || storedHadVolumeMap) {
      update.volume_size_gb_by_target = volumesByTarget;
    }

    setSaving(true);
    try {
      const response = await apiService.updateBuildConfig(
        update as BuildInfrastructureConfigUpdate,
      );
      applyConfig(response.config);
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
            constraintText="Root volume size for build instances, a positive number (default 200). Applies to Build_Jobs created after the change: existing jobs keep their snapshotted volume size."
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

          <Header
            variant="h3"
            description="Optional per-target volume sizes for ephemeral build runners. A target without an entry uses the global volume size above. JP6 requires at least 200 GB. Entries apply only to Build_Jobs created after the change: existing jobs keep their snapshotted volume sizes."
          >
            Per-target volume sizes
          </Header>
          {volumeRows.map((row, index) => (
            <SpaceBetween key={`volume-row-${index}`} size="xs" direction="horizontal">
              <FormField label="Build target" constraintText="JP5, JP6, AMD64, or AMD64_NVIDIA">
                <Input
                  value={row.target}
                  onChange={({ detail }) => setVolumeRow(index, 'target', detail.value)}
                  placeholder="JP6"
                  ariaLabel={`Per-target volume ${index + 1} target`}
                />
              </FormField>
              <FormField label="Volume size (GB)" constraintText="JP6 minimum 200">
                <Input
                  type="number"
                  value={row.volume_size_gb}
                  onChange={({ detail }) => setVolumeRow(index, 'volume_size_gb', detail.value)}
                  ariaLabel={`Per-target volume ${index + 1} size GB`}
                />
              </FormField>
              <Button
                ariaLabel={`Remove per-target volume ${index + 1}`}
                onClick={() => setVolumeRows((rows) => rows.filter((_, i) => i !== index))}
              >
                Remove
              </Button>
            </SpaceBetween>
          ))}
          <Button
            ariaLabel="Add per-target volume size"
            onClick={() => setVolumeRows((rows) => [...rows, { target: '', volume_size_gb: '' }])}
          >
            Add per-target volume size
          </Button>

          <Header
            variant="h3"
            description="Optional runtime budgets per build target and execution mode, snapshotted onto each Build_Job at submission. The hard runtime ceiling is a non-extendable safety limit: fresh heartbeats and progress renew the soft leases but can never extend it. Queue-wait and provisioning limits are independent and optional — they stay disabled unless set, and queue or provisioning time is never charged against execution runtime. Changing these settings does not mutate existing Build_Jobs: every job keeps the budgets snapshotted when it was submitted."
          >
            Runtime budgets (per target and mode)
          </Header>
          {budgetRows.map((row, index) => (
            <SpaceBetween key={`budget-row-${index}`} size="xs" direction="horizontal">
              <FormField label="Build target" constraintText="JP5, JP6, AMD64, or AMD64_NVIDIA">
                <Input
                  value={row.target}
                  onChange={({ detail }) => setBudgetRow(index, 'target', detail.value)}
                  placeholder="JP6"
                  ariaLabel={`Runtime budget ${index + 1} target`}
                />
              </FormField>
              <FormField label="Mode" constraintText="ephemeral, dedicated, or blank for both (default)">
                <Input
                  value={row.mode}
                  onChange={({ detail }) => setBudgetRow(index, 'mode', detail.value)}
                  placeholder="default"
                  ariaLabel={`Runtime budget ${index + 1} mode`}
                />
              </FormField>
              <FormField label="Hard runtime (hours)" constraintText="Non-extendable ceiling">
                <Input
                  type="number"
                  value={row.hard_runtime_hours}
                  onChange={({ detail }) => setBudgetRow(index, 'hard_runtime_hours', detail.value)}
                  ariaLabel={`Runtime budget ${index + 1} hard runtime hours`}
                />
              </FormField>
              <FormField label="Heartbeat lease (min)" constraintText="Optional liveness lease">
                <Input
                  type="number"
                  value={row.heartbeat_lease_minutes}
                  onChange={({ detail }) =>
                    setBudgetRow(index, 'heartbeat_lease_minutes', detail.value)
                  }
                  ariaLabel={`Runtime budget ${index + 1} heartbeat lease minutes`}
                />
              </FormField>
              <FormField label="Progress stall (min)" constraintText="Optional progress lease">
                <Input
                  type="number"
                  value={row.progress_stall_minutes}
                  onChange={({ detail }) =>
                    setBudgetRow(index, 'progress_stall_minutes', detail.value)
                  }
                  ariaLabel={`Runtime budget ${index + 1} progress stall minutes`}
                />
              </FormField>
              <FormField label="Queue wait (hours)" constraintText="Optional, independent of runtime">
                <Input
                  type="number"
                  value={row.queue_wait_hours}
                  onChange={({ detail }) => setBudgetRow(index, 'queue_wait_hours', detail.value)}
                  ariaLabel={`Runtime budget ${index + 1} queue wait hours`}
                />
              </FormField>
              <FormField label="Provisioning (min)" constraintText="Optional, independent of runtime">
                <Input
                  type="number"
                  value={row.provisioning_minutes}
                  onChange={({ detail }) =>
                    setBudgetRow(index, 'provisioning_minutes', detail.value)
                  }
                  ariaLabel={`Runtime budget ${index + 1} provisioning minutes`}
                />
              </FormField>
              <Button
                ariaLabel={`Remove runtime budget ${index + 1}`}
                onClick={() => setBudgetRows((rows) => rows.filter((_, i) => i !== index))}
              >
                Remove
              </Button>
            </SpaceBetween>
          ))}
          <Button
            ariaLabel="Add runtime budget"
            onClick={() => setBudgetRows((rows) => [...rows, { ...EMPTY_BUDGET_ROW }])}
          >
            Add runtime budget
          </Button>
        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}
