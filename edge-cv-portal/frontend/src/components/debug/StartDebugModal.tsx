/**
 * "Start debug" modal (workflow-debug-mode design §7.2, §8; tasks.md 5.2).
 *
 * Collects the creation body of `POST /debug/usecases/{usecase_id}/sessions`:
 * the workflow, one device or every registered device of the workflow, the
 * duration (5–1440 minutes, default 60), the level (default `debug`) and the
 * images switch, which stays off and disabled while registry enforcement is
 * off because the API refuses `images: true` with 409
 * `registry_enforcement_required` in that mode (design §7.2 "two switches").
 *
 * Every refusal is shown with its `remedy`: `device_not_recorded` (who can
 * record the device), `no_target_devices` (the devices without a deployment
 * or a record), `debug_session_limit` (which cap, active/max),
 * `debug_session_exists` (the live session's id), `debug_images_unavailable`
 * (the lifecycle script) and `debug_sink_unavailable`. The server decides;
 * the form only validates what it can (design §13).
 */
import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Input,
  Modal,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
} from '@cloudscape-design/components';
import { apiService, isDebugApiError } from '../../services/api';
import { isRegistryEnforced } from '../../config';
import { getErrorMessage } from '../../utils/errorHandling';
import {
  DEBUG_DEFAULTS,
  type CreateDebugSessionBody,
  type CreateDebugSessionResponse,
  type DebugLevel,
} from '../../pages/debug/types';
import type { Device } from '../../types';
import type { WorkflowSummary } from '../../pages/workflows/types';

export const DURATION_MESSAGE = `Duration must be a whole number of minutes between ${DEBUG_DEFAULTS.minDurationMinutes} and ${DEBUG_DEFAULTS.maxDurationMinutes}`;
export const IMAGE_MAX_KB_MESSAGE = `Image size must be a whole number of KB between ${DEBUG_DEFAULTS.minImageMaxKb} and ${DEBUG_DEFAULTS.maxImageMaxKb}`;
export const IMAGES_ENFORCEMENT_NOTE =
  'Images are unavailable until registry enforcement is on: the API refuses them while PORTAL_REGISTRY_ENFORCED is off, because an image may show people and in that mode every signed-in user can read the log.';
export const IMAGES_NOTE =
  'Up to 2 images per second and 2,000 per session, 960 × 540, from the runs of this session only.';

/** The notice shown after a session started (shadow results and the warning). */
export function startedNotice(result: CreateDebugSessionResponse): string {
  const failed = result.devices.filter((d) => d.shadow !== 'ok');
  const base =
    result.state === 'active'
      ? `Session ${result.session_id} is active on ${result.devices.length} device(s).`
      : `Session ${result.session_id} is pending: ${failed.length} shadow write(s) failed and will be retried every 5 minutes until the session expires.`;
  const failures = failed.length
    ? ` Failed: ${failed.map((d) => `${d.device_id}${d.error ? ` (${d.error})` : ''}`).join(', ')}.`
    : '';
  const warning = result.warning ? ` ${result.warning}.` : '';
  return `${base}${failures}${warning}`;
}

/** True iff `value` is an integer within [min, max]. */
export function isIntegerInRange(value: string, min: number, max: number): boolean {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return false;
  const n = Number(trimmed);
  return Number.isInteger(n) && n >= min && n <= max;
}

/** The refusal's extra detail lines, by error code (design §7.2). */
export function refusalDetails(err: unknown): string[] {
  if (!isDebugApiError(err)) return [];
  const body = err.body;
  const lines: string[] = [];
  switch (err.errorCode) {
    case 'no_target_devices': {
      const noDeployment = Array.isArray(body.no_deployment) ? body.no_deployment : [];
      const noRecord = Array.isArray(body.no_device_record) ? body.no_device_record : [];
      if (noDeployment.length) lines.push(`Without a deployment of this workflow: ${noDeployment.join(', ')}`);
      if (noRecord.length) lines.push(`Without a device record in this use case: ${noRecord.join(', ')}`);
      break;
    }
    case 'debug_session_limit': {
      const which =
        body.limit === 'usecase'
          ? 'sessions per use case'
          : body.limit === 'device'
            ? `sessions per device${body.device ? ` (${body.device})` : ''}`
            : body.limit === 'devices_per_session'
              ? 'devices per session'
              : 'sessions';
      const counts =
        typeof body.active === 'number' && typeof body.max === 'number'
          ? ` (${body.active} of ${body.max})`
          : '';
      lines.push(`Cap reached: ${which}${counts}.`);
      break;
    }
    case 'debug_session_exists':
      if (body.session_id) lines.push(`Live session: ${body.session_id}${body.device ? ` on ${body.device}` : ''}.`);
      break;
    case 'device_not_recorded':
      if (body.device_id) lines.push(`Device: ${body.device_id}.`);
      break;
    case 'debug_images_unavailable':
    case 'debug_sink_unavailable':
      if (typeof body.reason === 'string' && body.reason) lines.push(`Reason: ${body.reason}.`);
      break;
    default:
      break;
  }
  return lines;
}

export interface StartDebugModalProps {
  usecaseId: string;
  /** Preselects one device (the Device detail "Debug" tab). */
  deviceId?: string;
  onDismiss: () => void;
  onStarted: (result: CreateDebugSessionResponse) => void;
}

export default function StartDebugModal({ usecaseId, deviceId, onDismiss, onStarted }: StartDebugModalProps) {
  const enforced = isRegistryEnforced();

  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [workflow, setWorkflow] = useState<SelectProps.Option | null>(null);
  const [target, setTarget] = useState<'device' | 'all'>('device');
  const [device, setDevice] = useState<SelectProps.Option | null>(
    deviceId ? { label: deviceId, value: deviceId } : null
  );
  const [duration, setDuration] = useState(String(DEBUG_DEFAULTS.durationMinutes));
  const [level, setLevel] = useState<DebugLevel>(DEBUG_DEFAULTS.level);
  const [images, setImages] = useState(false);
  const [imageMaxKb, setImageMaxKb] = useState(String(DEBUG_DEFAULTS.imageMaxKb));

  const [submitting, setSubmitting] = useState(false);
  const [refusal, setRefusal] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([apiService.listWorkflows(usecaseId), apiService.listDevices(usecaseId)])
      .then(([workflowAnswer, deviceAnswer]) => {
        if (cancelled) return;
        setWorkflows(workflowAnswer.workflows ?? []);
        setDevices(deviceAnswer.devices ?? []);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(getErrorMessage(err, 'Failed to load the workflows and devices'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [usecaseId]);

  const workflowOptions: SelectProps.Option[] = useMemo(
    () =>
      workflows.map((wf) => ({
        label: wf.name,
        value: wf.workflow_id,
        description: wf.workflow_id,
      })),
    [workflows]
  );
  const deviceOptions: SelectProps.Option[] = useMemo(
    () =>
      devices.map((d) => ({
        label: d.device_id,
        value: d.device_id,
        description: d.target_architecture ? `Target architecture ${d.target_architecture}` : undefined,
      })),
    [devices]
  );

  const durationInvalid = !isIntegerInRange(
    duration,
    DEBUG_DEFAULTS.minDurationMinutes,
    DEBUG_DEFAULTS.maxDurationMinutes
  );
  const imageMaxKbInvalid =
    images && !isIntegerInRange(imageMaxKb, DEBUG_DEFAULTS.minImageMaxKb, DEBUG_DEFAULTS.maxImageMaxKb);
  const deviceMissing = target === 'device' && !device?.value;
  const canSubmit = !!workflow?.value && !deviceMissing && !durationInvalid && !imageMaxKbInvalid && !submitting;

  const handleStart = async () => {
    if (!workflow?.value || !canSubmit) return;
    const body: CreateDebugSessionBody = {
      workflow_id: workflow.value,
      duration_minutes: Number(duration.trim()),
      level,
      images,
    };
    if (target === 'all') {
      body.all_devices = true;
    } else if (device?.value) {
      body.device_id = device.value;
    }
    if (images) body.image_max_kb = Number(imageMaxKb.trim());
    try {
      setSubmitting(true);
      setRefusal(null);
      const result = await apiService.createDebugSession(usecaseId, body);
      onStarted(result);
    } catch (err) {
      setRefusal(err);
    } finally {
      setSubmitting(false);
    }
  };

  const refusalRemedy = isDebugApiError(refusal) ? refusal.remedy : undefined;
  const refusalCode = isDebugApiError(refusal) ? refusal.errorCode : undefined;
  const details = refusalDetails(refusal);

  return (
    <Modal
      visible
      onDismiss={() => (submitting ? undefined : onDismiss())}
      header="Start debug"
      data-testid="start-debug-modal"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting} data-testid="start-debug-cancel">
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleStart}
              disabled={!canSubmit}
              loading={submitting}
              data-testid="start-debug-submit"
            >
              Start
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="p">
          A debug session sends every run of the workflow on the chosen devices to the use case's debug
          log for the given time, then switches itself off. It is not shown on the dashboards.
        </Box>

        {loadError && <Alert type="error">{loadError}</Alert>}

        {refusal !== null && (
          <Alert type="error" header="The session was not started" data-testid="start-debug-refusal">
            <SpaceBetween size="xxs">
              <Box>
                {getErrorMessage(refusal, 'The request failed')}
                {refusalCode ? ` (${refusalCode})` : ''}
              </Box>
              {details.map((line) => (
                <Box key={line}>{line}</Box>
              ))}
              {refusalRemedy && (
                <Box data-testid="start-debug-remedy">
                  <Box variant="strong">Remedy: </Box>
                  {refusalRemedy}
                </Box>
              )}
            </SpaceBetween>
          </Alert>
        )}

        <FormField label="Workflow" description="A workflow of this use case" stretch>
          <Select
            selectedOption={workflow}
            onChange={({ detail }) => setWorkflow(detail.selectedOption)}
            options={workflowOptions}
            placeholder="Select a workflow"
            selectedAriaLabel="Selected"
            statusType={loading ? 'loading' : 'finished'}
            loadingText="Loading workflows"
            empty="No workflows in this use case"
            data-testid="start-debug-workflow"
          />
        </FormField>

        <FormField label="Devices" stretch>
          <RadioGroup
            value={target}
            onChange={({ detail }) => setTarget(detail.value as 'device' | 'all')}
            items={[
              { value: 'device', label: 'One device' },
              {
                value: 'all',
                label: 'All devices',
                description: `Every device with a deployment of the workflow and a device record in this use case (at most ${DEBUG_DEFAULTS.maxDevicesPerSession} per session)`,
              },
            ]}
            data-testid="start-debug-target"
          />
        </FormField>

        {target === 'device' && (
          <FormField label="Device" description="A device recorded in this use case" stretch>
            <Select
              selectedOption={device}
              onChange={({ detail }) => setDevice(detail.selectedOption)}
              options={deviceOptions}
              placeholder="Select a device"
              selectedAriaLabel="Selected"
              statusType={loading ? 'loading' : 'finished'}
              loadingText="Loading devices"
              empty="No devices in this use case"
              data-testid="start-debug-device"
            />
          </FormField>
        )}

        <FormField
          label="Duration (minutes)"
          description={`${DEBUG_DEFAULTS.minDurationMinutes}–${DEBUG_DEFAULTS.maxDurationMinutes} minutes; the session switches itself off afterwards`}
          errorText={durationInvalid ? DURATION_MESSAGE : undefined}
          stretch
        >
          <Input
            type="number"
            value={duration}
            onChange={({ detail }) => setDuration(detail.value)}
            invalid={durationInvalid}
            ariaLabel="Duration in minutes"
            data-testid="start-debug-duration"
          />
        </FormField>

        <FormField
          label="Level"
          description="debug adds the DEBUG lines of the workflow engine to each run record"
          stretch
        >
          <Select
            selectedOption={{ label: level, value: level }}
            onChange={({ detail }) => setLevel((detail.selectedOption.value as DebugLevel) ?? 'debug')}
            options={[
              { label: 'debug', value: 'debug' },
              { label: 'info', value: 'info' },
            ]}
            selectedAriaLabel="Selected"
            data-testid="start-debug-level"
          />
        </FormField>

        <FormField label="Images" description={enforced ? IMAGES_NOTE : IMAGES_ENFORCEMENT_NOTE} stretch>
          <Checkbox
            checked={images}
            disabled={!enforced}
            onChange={({ detail }) => setImages(detail.checked)}
            data-testid="start-debug-images"
          >
            Upload one image per run
          </Checkbox>
        </FormField>

        {images && (
          <FormField
            label="Image size (KB)"
            description={`${DEBUG_DEFAULTS.minImageMaxKb}–${DEBUG_DEFAULTS.maxImageMaxKb} KB per image`}
            errorText={imageMaxKbInvalid ? IMAGE_MAX_KB_MESSAGE : undefined}
            stretch
          >
            <Input
              type="number"
              value={imageMaxKb}
              onChange={({ detail }) => setImageMaxKb(detail.value)}
              invalid={imageMaxKbInvalid}
              ariaLabel="Image size in KB"
              data-testid="start-debug-image-max-kb"
            />
          </FormField>
        )}
      </SpaceBetween>
    </Modal>
  );
}
