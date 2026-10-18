/**
 * The create-dashboard wizard, `/workflow-dashboards/create`
 * (workflow-telemetry-dashboards design §11.2, §11.4; tasks.md 5.5).
 *
 * Five steps: the use case; 1-20 of its devices (a 400 `device_not_recorded`
 * answers with the Device_Record_Remedy, shown in full); a Workflow of the use
 * case; a template with its availability from `GET …/templates`
 * (`available` / `partial` with the unreported panels / `unavailable`); a
 * name (1-100 characters, unique per use case after NFKC case folding, 409
 * `dashboard_name_taken`). No AWS console step and no deployment: the
 * devices' control shadows are written by the API on create.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  FormField,
  Input,
  KeyValuePairs,
  Multiselect,
  MultiselectProps,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Tiles,
  Wizard,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { getErrorMessage } from '../../utils/errorHandling';
import type { Device } from '../../types';
import type { WorkflowSummary } from '../workflows/types';
import type { TelemetrySchemasResponse, TemplateAvailabilityEntry } from './types';

export const MAX_DEVICES = 20;
export const DEVICES_MESSAGE = `Choose 1 to ${MAX_DEVICES} devices of the use case`;
export const NAME_MESSAGE = 'A name of 1 to 100 characters';
export const NO_RECORDED_DEVICES =
  'The use case has no device records yet. A device joins a dashboard once a UseCaseAdmin or PortalAdmin sets its Target architecture on its device page.';

const WIZARD_I18N = {
  stepNumberLabel: (n: number) => `Step ${n}`,
  collapsedStepsLabel: (n: number, count: number) => `Step ${n} of ${count}`,
  navigationAriaLabel: 'Steps',
  cancelButton: 'Cancel',
  previousButton: 'Previous',
  nextButton: 'Next',
  optional: 'optional',
};

/** The visible state of a template's availability (design §8.2). */
export function availabilityView(entry: TemplateAvailabilityEntry | undefined): { type: 'success' | 'warning' | 'stopped' | 'pending'; text: string } {
  if (!entry) return { type: 'pending', text: 'availability unknown' };
  switch (entry.availability) {
    case 'available':
      return { type: 'success', text: 'available: every panel reported in the last 24 h' };
    case 'partial':
      return { type: 'warning', text: `partial: not reported yet — ${entry.unreported_panels.join(', ')}` };
    default:
      return { type: 'stopped', text: 'unavailable: no summary of the last 24 h reports its panels' };
  }
}

export default function CreateDashboard() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();

  const [step, setStep] = useState(0);
  const [useCases, setUseCases] = useState<SelectProps.Option[]>([]);
  const [usecaseId, setUsecaseId] = useState<string | null>(searchParams.get('usecase_id') ?? selectedUsecaseId ?? null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [devicesLoaded, setDevicesLoaded] = useState(false);
  const [chosenDevices, setChosenDevices] = useState<readonly MultiselectProps.Option[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [schemas, setSchemas] = useState<TelemetrySchemasResponse | null>(null);
  const [availability, setAvailability] = useState<Record<string, TemplateAvailabilityEntry>>({});
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ text: string; remedy?: string; field?: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    apiService
      .listUseCases()
      .then(({ usecases }) => {
        const options = usecases.map((uc) => ({ label: uc.name, value: uc.usecase_id }));
        setUseCases(options);
        if (!usecaseId && options[0]?.value) setUsecaseId(options[0].value);
      })
      .catch((err) => setLoadError(getErrorMessage(err, 'Failed to list use cases')));
    apiService
      .getTelemetrySchemas()
      .then(setSchemas)
      .catch((err) => setLoadError(getErrorMessage(err, 'Failed to read the templates')));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The use case's devices and workflows.
  useEffect(() => {
    if (!usecaseId) return;
    let cancelled = false;
    setDevicesLoaded(false);
    setChosenDevices([]);
    setWorkflowId(null);
    setTemplateId(null);
    setAvailability({});
    apiService
      .listDevices(usecaseId)
      .then(({ devices: list }) => {
        if (cancelled) return;
        setDevices(list ?? []);
        setDevicesLoaded(true);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(getErrorMessage(err, 'Failed to list devices'));
      });
    apiService
      .listWorkflows(usecaseId)
      .then(({ workflows: list }) => {
        if (!cancelled) setWorkflows(list ?? []);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(getErrorMessage(err, 'Failed to list workflows'));
      });
    return () => {
      cancelled = true;
    };
  }, [usecaseId]);

  const deviceIds = useMemo(() => chosenDevices.map((o) => o.value!).filter(Boolean), [chosenDevices]);

  // The templates' availability for the chosen workflow on the chosen devices.
  useEffect(() => {
    if (!usecaseId || !workflowId || deviceIds.length === 0) return;
    let cancelled = false;
    apiService
      .getTelemetryTemplates(usecaseId, { workflow_id: workflowId, device_ids: deviceIds })
      .then((answer) => {
        if (cancelled) return;
        const byId: Record<string, TemplateAvailabilityEntry> = {};
        for (const entry of answer.templates ?? []) byId[entry.id] = entry;
        setAvailability(byId);
      })
      .catch(() => {
        if (!cancelled) setAvailability({});
      });
    return () => {
      cancelled = true;
    };
  }, [usecaseId, workflowId, deviceIds]);

  const deviceOptions: MultiselectProps.Option[] = useMemo(
    () =>
      devices.map((d) => ({
        label: d.device_id,
        value: d.device_id,
        description: d.target_architecture ? `${d.target_architecture}${d.status ? ` · ${d.status}` : ''}` : d.status,
      })),
    [devices]
  );
  const workflowOptions: SelectProps.Option[] = useMemo(
    () => workflows.map((wf) => ({ label: wf.name, value: wf.workflow_id, description: `v${wf.latest_version}` })),
    [workflows]
  );
  const templates = useMemo(() => Object.values(schemas?.templates ?? {}), [schemas]);

  const devicesValid = deviceIds.length >= 1 && deviceIds.length <= MAX_DEVICES;
  const nameTrimmed = name.normalize('NFKC').trim();
  const nameValid = nameTrimmed.length >= 1 && nameTrimmed.length <= 100;

  const stepValid = (index: number): boolean => {
    switch (index) {
      case 0:
        return !!usecaseId;
      case 1:
        return devicesValid;
      case 2:
        return !!workflowId;
      case 3:
        return !!templateId;
      default:
        return nameValid;
    }
  };

  const onNavigate = (requested: number) => {
    if (requested > step && !stepValid(step)) {
      setAttempted(true);
      return;
    }
    setAttempted(false);
    setStep(requested);
  };

  const submit = async () => {
    setAttempted(true);
    if (!usecaseId || !workflowId || !templateId || !devicesValid || !nameValid) return;
    try {
      setSubmitting(true);
      setError(null);
      const answer = await apiService.createDashboard({
        usecase_id: usecaseId,
        name: nameTrimmed,
        workflow_id: workflowId,
        device_ids: deviceIds,
        template_id: templateId,
      });
      setSelectedUsecaseId(usecaseId);
      navigate(`/workflow-dashboards/${encodeURIComponent(answer.dashboard.dashboard_id)}`, {
        state: { created: true, devices: answer.devices },
      });
    } catch (err) {
      if (isTelemetryApiError(err)) {
        setError({ text: `${err.message}${err.errorCode ? ` (${err.errorCode})` : ''}`, remedy: err.remedy, field: err.field });
        if (err.errorCode === 'device_not_recorded') setStep(1);
        else if (err.errorCode === 'dashboard_name_taken' || err.field === 'name') setStep(4);
      } else {
        setError({ text: getErrorMessage(err, 'The dashboard could not be created') });
      }
    } finally {
      setSubmitting(false);
    }
  };

  const chosenTemplate = templateId ? schemas?.templates?.[templateId] : undefined;

  return (
    <SpaceBetween size="m">
      {loadError && (
        <Alert type="error" dismissible onDismiss={() => setLoadError(null)}>
          {loadError}
        </Alert>
      )}
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} data-testid="create-dashboard-error">
          <SpaceBetween size="xxs">
            <span>{error.text}</span>
            {error.remedy && <span data-testid="create-dashboard-remedy">{error.remedy}</span>}
          </SpaceBetween>
        </Alert>
      )}
      <Wizard
        i18nStrings={WIZARD_I18N}
        submitButtonText="Create dashboard"
        activeStepIndex={step}
        onNavigate={({ detail }) => onNavigate(detail.requestedStepIndex)}
        onCancel={() => navigate('/workflow-dashboards')}
        onSubmit={() => void submit()}
        isLoadingNextStep={submitting}
        data-testid="create-dashboard-wizard"
        steps={[
          {
            title: 'Use case',
            description: 'The dashboard lives in one use case; its readers are the use case\'s dashboards:read holders.',
            content: (
              <FormField label="Use case" errorText={attempted && !usecaseId ? 'Choose a use case' : undefined}>
                <Select
                  selectedOption={useCases.find((o) => o.value === usecaseId) ?? null}
                  onChange={({ detail }) => setUsecaseId(detail.selectedOption.value ?? null)}
                  options={useCases}
                  placeholder="Select a use case"
                  selectedAriaLabel="Selected"
                  data-testid="create-dashboard-usecase"
                />
              </FormField>
            ),
          },
          {
            title: 'Devices',
            description: `${DEVICES_MESSAGE}. Only devices with a record for this use case publish telemetry.`,
            content: (
              <SpaceBetween size="s">
                {devicesLoaded && devices.length === 0 && (
                  <Alert type="warning" data-testid="create-dashboard-no-devices">
                    {NO_RECORDED_DEVICES}
                  </Alert>
                )}
                <FormField
                  label="Devices"
                  description={`${deviceIds.length} of ${MAX_DEVICES} chosen`}
                  errorText={attempted && !devicesValid ? DEVICES_MESSAGE : undefined}
                >
                  <Multiselect
                    selectedOptions={chosenDevices}
                    onChange={({ detail }) => setChosenDevices(detail.selectedOptions)}
                    options={deviceOptions}
                    placeholder="Choose devices"
                    selectedAriaLabel="Selected"
                    statusType={devicesLoaded ? 'finished' : 'loading'}
                    loadingText="Loading devices"
                    data-testid="create-dashboard-devices"
                  />
                </FormField>
              </SpaceBetween>
            ),
          },
          {
            title: 'Workflow',
            description: 'A workflow of the use case; the dashboard shows the runs of this workflow on the chosen devices.',
            content: (
              <FormField label="Workflow" errorText={attempted && !workflowId ? 'Choose a workflow' : undefined}>
                <Select
                  selectedOption={workflowOptions.find((o) => o.value === workflowId) ?? null}
                  onChange={({ detail }) => setWorkflowId(detail.selectedOption.value ?? null)}
                  options={workflowOptions}
                  placeholder="Select a workflow"
                  selectedAriaLabel="Selected"
                  empty="No workflows in this use case"
                  data-testid="create-dashboard-workflow"
                />
              </FormField>
            ),
          },
          {
            title: 'Template',
            description: 'The panels and tiles. Availability comes from the newest summaries of the chosen devices.',
            content: (
              <FormField label="Template" errorText={attempted && !templateId ? 'Choose a template' : undefined}>
                <Tiles
                  value={templateId}
                  onChange={({ detail }) => setTemplateId(detail.value)}
                  columns={1}
                  data-testid="create-dashboard-template"
                  items={templates.map((tpl) => {
                    const view = availabilityView(availability[tpl.id]);
                    return {
                      value: tpl.id,
                      label: `${tpl.label} v${tpl.version}`,
                      description: (
                        <SpaceBetween size="xxs">
                          <span>{tpl.description}</span>
                          <StatusIndicator type={view.type}>
                            <span data-testid={`create-dashboard-availability-${tpl.id}`}>{view.text}</span>
                          </StatusIndicator>
                          <Box variant="small" color="text-body-secondary">
                            panels: {tpl.panels.map((p) => p.title).join(', ')} · tiles: {tpl.tiles.join(', ')}
                          </Box>
                        </SpaceBetween>
                      ),
                    };
                  })}
                />
              </FormField>
            ),
          },
          {
            title: 'Name',
            description: 'Unique within the use case. Creating the dashboard writes the devices\' telemetry shadows; nothing is deployed.',
            content: (
              <SpaceBetween size="m">
                <FormField label="Name" errorText={attempted && !nameValid ? NAME_MESSAGE : error?.field === 'name' ? error.text : undefined}>
                  <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="Line 1 PPE" data-testid="create-dashboard-name" />
                </FormField>
                <KeyValuePairs
                  columns={2}
                  items={[
                    { label: 'Use case', value: useCases.find((o) => o.value === usecaseId)?.label ?? usecaseId ?? '—' },
                    { label: 'Devices', value: <span data-testid="create-dashboard-review-devices">{deviceIds.join(', ') || '—'}</span> },
                    { label: 'Workflow', value: workflowOptions.find((o) => o.value === workflowId)?.label ?? workflowId ?? '—' },
                    {
                      label: 'Template',
                      value: chosenTemplate ? (
                        <SpaceBetween direction="horizontal" size="xs">
                          <span>{chosenTemplate.label}</span>
                          <Badge color={availability[chosenTemplate.id]?.availability === 'available' ? 'green' : 'grey'}>
                            {availability[chosenTemplate.id]?.availability ?? 'unknown'}
                          </Badge>
                        </SpaceBetween>
                      ) : (
                        '—'
                      ),
                    },
                  ]}
                />
              </SpaceBetween>
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
