import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Checkbox,
  ColumnLayout,
  Badge,
  Button,
  Form,
  FormField,
  Input,
  Modal,
  Alert,
  KeyValuePairs,
  Table,
  Link,
  Select,
  SelectProps,
  Spinner,
} from '@cloudscape-design/components';
import {
  apiService,
  ApiError,
  VllmEngineConfiguration,
  VllmEngineSpec,
  VllmFitCheckResult,
  VllmRegistrationFinding,
} from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import CompilationTab from '../components/CompilationTab';
import VllmPackagePublishSection from '../components/vllm-publish/VllmPackagePublishSection';
import { engineDisplayValue, toEngineJsonValue } from './RegisterLlm';
import { TrainingJob } from '../types';

interface Model {
  model_id: string;
  usecase_id: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  source: string;
  training_job_id: string;
  training_job_name?: string;
  model_type: string;
  description?: string;
  metrics: Record<string, number>;
  artifact_s3?: string;
  component_arns: Record<string, string>;
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  updated_at: number;
  completed_at?: number;
  promoted_at?: number;
  promoted_by?: string;
  compilation_status?: string;
  compilation_jobs?: Array<{
    target: string;
    status: string;
    compiled_model_s3?: string;
    // Diagnostic fields preserved by the backend so a failure is never
    // reduced to a bare status token (onnx-compile-error-diagnostics, 2.15).
    failure_reason?: string;
    error?: string;
    poll_error?: string;
    job_started?: boolean;
  }>;
  packaging_status?: string;
  packaged_components?: Array<{
    target: string;
    status: string;
    component_package_s3?: string;
    supported_architectures?: string[];
  }>;
  published_component?: {
    component_name: string;
    component_version: string;
    supported_architectures: string[];
    runtime: string;
    component_arns: Record<string, string>;
    published_at: number;
  };
  // Stored Engine_Configuration, present for vLLM records only
  // (vllm-sizing-and-packaging-errors, Requirement 1.2).
  engine_configuration?: VllmEngineConfiguration | null;
  hyperparameters?: Record<string, unknown>;
  instance_type?: string;
  dataset_manifest_s3?: string;
}

export default function ModelDetail() {
  const { modelId } = useParams<{ modelId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  
  const [model, setModel] = useState<Model | null>(null);
  const [trainingJob, setTrainingJob] = useState<TrainingJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [targetStage, setTargetStage] = useState<SelectProps.Option | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  // Engine_Configuration inline edit state (vllm-sizing-and-packaging-errors,
  // Requirements 2.5, 3.5), reusing the RegisterLlm.tsx field-rendering and
  // per-field finding patterns.
  const [editingEngineConfig, setEditingEngineConfig] = useState(false);
  const [engineSpec, setEngineSpec] = useState<VllmEngineSpec | null>(null);
  const [engineValues, setEngineValues] = useState<Record<string, string | boolean>>({});
  const [engineFieldErrors, setEngineFieldErrors] = useState<Record<string, string>>({});
  const [engineEditError, setEngineEditError] = useState<string | null>(null);
  const [engineSubmitting, setEngineSubmitting] = useState(false);
  const [engineNotice, setEngineNotice] = useState<string | null>(null);
  const [engineFitCheck, setEngineFitCheck] = useState<VllmFitCheckResult | null>(null);

  const loadTrainingJob = async (trainingId: string) => {
    try {
      const tj = await apiService.getTrainingJob(trainingId);
      setTrainingJob(tj as TrainingJob);
    } catch (err) {
      console.error('Failed to load training job for compilation tab:', err);
    }
  };

  useEffect(() => {
    const loadModel = async () => {
      if (!modelId) return;
      setLoading(true);
      setError(null);
      try {
        const response = await apiService.getModel(modelId);
        setModel(response.model);
        // Load the underlying training job so the Compilation / Component
        // Actions tab can drive packaging + publishing from here. This applies
        // to both 'trained' models and 'imported' BYO models (incl. ONNX) —
        // imported models are stored in the training_jobs table too, and ONNX
        // models package/publish without compilation.
        const trainingId = response.model.training_job_id || response.model.model_id;
        if (trainingId && (response.model.source === 'trained' || response.model.source === 'imported')) {
          loadTrainingJob(trainingId);
        }
      } catch (err: any) {
        console.error('Failed to load model:', err);
        setError(err.message || 'Failed to load model');
      } finally {
        setLoading(false);
      }
    };
    loadModel();
  }, [modelId]);

  const handlePromote = async () => {
    if (!model || !targetStage?.value) return;
    setActionLoading(true);
    try {
      await apiService.updateModelStage(
        model.model_id, 
        targetStage.value as 'candidate' | 'staging' | 'production'
      );
      const response = await apiService.getModel(model.model_id);
      setModel(response.model);
      setShowPromoteModal(false);
      setTargetStage(null);
    } catch (err: any) {
      setError(err.message || 'Failed to update model stage');
    } finally {
      setActionLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!model) return;
    setActionLoading(true);
    try {
      await apiService.deleteModel(model.model_id);
      navigate('/models');
    } catch (err: any) {
      setError(err.message || 'Failed to delete model');
      setShowDeleteModal(false);
    } finally {
      setActionLoading(false);
    }
  };

  const getStageBadge = (stage: string) => {
    switch (stage) {
      case 'production': return <Badge color="green">Production</Badge>;
      case 'staging': return <Badge color="blue">Staging</Badge>;
      case 'candidate': return <Badge color="grey">Candidate</Badge>;
      default: return <Badge>{stage}</Badge>;
    }
  };

  const getSourceBadge = (source: string) => {
    switch (source) {
      case 'trained': return <Badge color="green">Trained</Badge>;
      case 'imported': return <Badge color="blue">Imported</Badge>;
      case 'marketplace': return <Badge color="grey">Marketplace</Badge>;
      case 'vllm': return <Badge color="blue">Registered</Badge>;
      default: return <Badge>{source}</Badge>;
    }
  };

  // Supported Target_Architectures of a vLLM_Model_Component: preferred
  // from the publish write-back map, falling back to the packaged
  // component entries (Requirement 3.8)
  const getSupportedArchitectures = (m: Model): string[] => {
    if (m.published_component?.supported_architectures?.length) {
      return m.published_component.supported_architectures;
    }
    for (const comp of m.packaged_components || []) {
      if (comp.supported_architectures?.length) {
        return comp.supported_architectures;
      }
    }
    return [];
  };

  const formatTimestamp = (timestamp?: number) => {
    if (!timestamp) return 'N/A';
    return new Date(timestamp).toLocaleString();
  };

  // vLLM_Model_Record detection, mirroring the backend's check
  // (models.py get_model: source === 'vllm' or model_type === 'vllm').
  const isVllmModel = (m: Model): boolean =>
    m.source === 'vllm' || m.model_type === 'vllm';

  // Render a stored engine setting value verbatim (booleans included) so
  // the display shows exactly what the device will load with
  // (Requirement 1.3). Object settings (limit_mm_per_prompt) render as JSON
  // rather than "[object Object]".
  const formatEngineSettingValue = (
    value: string | number | boolean | Record<string, number>
  ): string => engineDisplayValue(value);

  // --- Engine_Configuration inline edit (Requirements 2.5, 3.5) ---

  const engineFieldError = (key: string) =>
    engineFieldErrors[`engine_configuration.${key}`];

  const clearEngineFieldError = (field: string) => {
    setEngineFieldErrors((prev) => {
      if (!(field in prev)) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });
  };

  // Open the inline form pre-filled with the stored values overlaid onto the
  // engine-spec defaults (spec loaded lazily on first edit; stored keys still
  // render as plain inputs if the spec is unavailable).
  const startEngineEdit = async () => {
    setEngineNotice(null);
    setEngineFitCheck(null);
    setEngineEditError(null);
    setEngineFieldErrors({});
    let spec = engineSpec;
    if (!spec) {
      try {
        spec = await apiService.getVllmEngineSpec();
        setEngineSpec(spec);
      } catch (err) {
        console.error('Failed to load vLLM engine spec:', err);
      }
    }
    const values: Record<string, string | boolean> = {};
    if (spec) {
      Object.entries(spec.settings).forEach(([key, setting]) => {
        values[key] =
          setting.type === 'boolean'
            ? Boolean(setting.default)
            : engineDisplayValue(setting.default);
      });
    }
    Object.entries(model?.engine_configuration || {}).forEach(([key, value]) => {
      values[key] = typeof value === 'boolean' ? value : engineDisplayValue(value);
    });
    setEngineValues(values);
    setEditingEngineConfig(true);
  };

  const cancelEngineEdit = () => {
    setEditingEngineConfig(false);
    setEngineEditError(null);
    setEngineFieldErrors({});
  };

  const handleEngineSave = async () => {
    if (!model) return;
    const trainingId = model.training_job_id || model.model_id;
    setEngineSubmitting(true);
    setEngineEditError(null);
    setEngineFieldErrors({});

    // Convert the string/boolean form values into the JSON types the API
    // expects; non-numeric strings pass through so the backend produces its
    // per-field finding (same rule as RegisterLlm).
    const engineConfiguration: VllmEngineConfiguration = {};
    Object.entries(engineValues).forEach(([key, value]) => {
      const stored = model.engine_configuration?.[key];
      // Fall back to the stored value's own shape when the spec is
      // unavailable, so an object setting still round-trips as an object
      // instead of being submitted as its JSON text.
      const specType =
        engineSpec?.settings[key]?.type ??
        (typeof stored === 'number'
          ? 'number'
          : stored !== null && typeof stored === 'object'
            ? 'object'
            : 'string');
      engineConfiguration[key] = toEngineJsonValue(specType, value);
    });

    try {
      const result = await apiService.updateVllmEngineConfiguration(
        trainingId,
        engineConfiguration
      );
      // Reflect the complete updated configuration and surface the
      // re-package/publish notice plus any fit-check result (2.4, 3.5).
      setModel((prev) =>
        prev ? { ...prev, engine_configuration: result.engine_configuration } : prev
      );
      setEngineNotice(
        result.notice ||
          'Engine configuration updated. The change takes effect after the model is packaged and published again.'
      );
      setEngineFitCheck(result.fit_check ?? null);
      setEditingEngineConfig(false);
    } catch (err) {
      console.error('Engine configuration update error:', err);
      // Surface the API's validation findings per field (Requirement 2.5).
      const findings =
        err instanceof ApiError && Array.isArray(err.details?.findings)
          ? (err.details.findings as VllmRegistrationFinding[])
          : [];
      if (findings.length > 0) {
        const perField: Record<string, string> = {};
        const unmatched: string[] = [];
        findings.forEach((finding) => {
          perField[finding.field] = finding.reason;
          const key = finding.field.replace(/^engine_configuration\./, '');
          if (!(key in engineValues)) {
            unmatched.push(`${finding.field}: ${finding.reason}`);
          }
        });
        setEngineFieldErrors(perField);
        setEngineEditError(
          unmatched.length > 0
            ? `The update failed validation: ${unmatched.join('; ')}`
            : 'The update failed validation. Fix the highlighted fields and retry.'
        );
      } else {
        setEngineEditError(
          err instanceof Error ? err.message : 'Failed to update the engine configuration'
        );
      }
    } finally {
      setEngineSubmitting(false);
    }
  };

  // Field rendering adapted from RegisterLlm.renderEngineField (checkbox for
  // booleans, select for enumerated settings, input otherwise).
  const renderEngineEditField = (key: string) => {
    const setting = engineSpec?.settings[key];
    const value = engineValues[key];
    const label = key.replace(/_/g, ' ');
    const onChange = (next: string | boolean) => {
      setEngineValues((prev) => ({ ...prev, [key]: next }));
      clearEngineFieldError(`engine_configuration.${key}`);
    };

    if (setting?.type === 'boolean' || typeof value === 'boolean') {
      return (
        <FormField
          key={key}
          label={label}
          description={setting?.description}
          errorText={engineFieldError(key)}
        >
          <Checkbox
            checked={Boolean(value)}
            onChange={({ detail }) => onChange(detail.checked)}
          >
            {setting ? `Enabled (default: ${String(setting.default)})` : 'Enabled'}
          </Checkbox>
        </FormField>
      );
    }

    if (setting?.accepted_values && setting.accepted_values.length > 0) {
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
        description={setting?.description}
        constraintText={
          setting?.range
            ? `Accepted range: ${setting.range} (default: ${engineDisplayValue(setting.default)})`
            : setting
              ? `Default: ${engineDisplayValue(setting.default)}`
              : undefined
        }
        errorText={engineFieldError(key)}
      >
        <Input
          value={String(value ?? '')}
          onChange={({ detail }) => onChange(detail.value)}
          inputMode={
            setting?.type === 'integer'
              ? 'numeric'
              : setting?.type === 'object'
                ? 'text'
                : 'decimal'
          }
        />
      </FormField>
    );
  };

  const getPromotionOptions = (): SelectProps.Option[] => {
    if (!model) return [];
    const options: SelectProps.Option[] = [];
    if (model.stage === 'candidate') {
      options.push({ label: 'Staging', value: 'staging' });
      options.push({ label: 'Production', value: 'production' });
    } else if (model.stage === 'staging') {
      options.push({ label: 'Production', value: 'production' });
      options.push({ label: 'Candidate (Demote)', value: 'candidate' });
    } else if (model.stage === 'production') {
      options.push({ label: 'Staging (Demote)', value: 'staging' });
      options.push({ label: 'Candidate (Demote)', value: 'candidate' });
    }
    return options;
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 's' }}>Loading model...</Box>
      </Box>
    );
  }

  if (error || !model) {
    return (
      <Alert type="error" header="Error loading model">
        {error || 'Model not found'}
        <Box padding={{ top: 's' }}>
          <Button onClick={() => navigate('/models')}>Back to Models</Button>
        </Box>
      </Alert>
    );
  }

  const canDelete = model.deployed_devices.length === 0;

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/models')}>Back to Models</Button>
            <Button onClick={() => setShowPromoteModal(true)}>Change Stage</Button>
            <Button disabled={!canDelete} onClick={() => setShowDeleteModal(true)}>Delete Model</Button>
          </SpaceBetween>
        }
      >
        {model.name}
      </Header>

      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Stage</Box>
          <Box variant="h2">{getStageBadge(model.stage)}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Source</Box>
          <Box variant="h2">{getSourceBadge(model.source)}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="h3">{model.version}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Deployed Devices</Box>
          <Box variant="h3">{model.deployed_devices.length}</Box>
        </Container>
      </ColumnLayout>

      <Container header={<Header variant="h2">Model Information</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs columns={1} items={[
            { label: 'Model ID', value: model.model_id },
            { label: 'Name', value: model.name },
            { label: 'Version', value: model.version },
            { label: 'Type', value: model.model_type === 'vllm' ? <Badge color="blue">LLM (vLLM)</Badge> : (model.model_type || 'N/A') },
            { label: 'Stage', value: model.stage },
          ]} />
          <KeyValuePairs columns={1} items={[
            { label: 'Use Case', value: model.usecase_id },
            { label: 'Training Job', value: model.training_job_name || model.training_job_id },
            { label: 'Created By', value: model.created_by },
            { label: 'Instance Type', value: model.instance_type || 'N/A' },
            { label: 'Description', value: model.description || 'N/A' },
          ]} />
        </ColumnLayout>
      </Container>

      {isVllmModel(model) && (
        <Container
          header={
            <Header
              variant="h2"
              description="The vLLM engine settings this model is packaged and loaded with on the device"
              actions={
                !editingEngineConfig && (
                  <Button onClick={startEngineEdit} disabled={engineSubmitting}>
                    Edit
                  </Button>
                )
              }
            >
              Engine Configuration
            </Header>
          }
        >
          <SpaceBetween size="m">
            {engineNotice && (
              <Alert
                type="success"
                dismissible
                onDismiss={() => setEngineNotice(null)}
              >
                {engineNotice}
              </Alert>
            )}
            {engineFitCheck?.status === 'warnings' && (
              <Alert
                type="warning"
                header="Fit check warnings"
                dismissible
                onDismiss={() => setEngineFitCheck(null)}
              >
                <SpaceBetween size="xs">
                  {engineFitCheck.findings
                    .filter((finding) => !finding.fits)
                    .map((finding) => (
                      <Box key={finding.arch}>{finding.message}</Box>
                    ))}
                </SpaceBetween>
              </Alert>
            )}
            {engineFitCheck?.status === 'unverified' && (
              <Alert
                type="info"
                dismissible
                onDismiss={() => setEngineFitCheck(null)}
              >
                {engineFitCheck.message ||
                  'The model fit could not be verified: the weight size estimate is unavailable.'}
              </Alert>
            )}
            {editingEngineConfig ? (
              <Form
                errorText={engineEditError ?? undefined}
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button onClick={cancelEngineEdit} disabled={engineSubmitting}>
                      Cancel
                    </Button>
                    <Button
                      variant="primary"
                      onClick={handleEngineSave}
                      loading={engineSubmitting}
                      disabled={engineSubmitting}
                    >
                      Save
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="m">
                  {engineFieldErrors['engine_configuration'] && (
                    <Alert type="error">
                      {engineFieldErrors['engine_configuration']}
                    </Alert>
                  )}
                  {Object.keys(engineValues).map((key) => renderEngineEditField(key))}
                </SpaceBetween>
              </Form>
            ) : model.engine_configuration &&
              Object.keys(model.engine_configuration).length > 0 ? (
              <KeyValuePairs
                columns={3}
                items={Object.entries(model.engine_configuration).map(
                  ([key, value]) => ({
                    label: key,
                    value: formatEngineSettingValue(value),
                  })
                )}
              />
            ) : (
              <Box color="text-status-inactive">
                No engine configuration is stored on this model record.
              </Box>
            )}
          </SpaceBetween>
        </Container>
      )}

      {model.model_type === 'vllm' && (
        <Container
          header={
            <Header
              variant="h2"
              description="Target architectures this vLLM model component can be deployed to"
            >
              Supported Architectures
            </Header>
          }
        >
          {getSupportedArchitectures(model).length > 0 ? (
            <SpaceBetween direction="horizontal" size="xs">
              {getSupportedArchitectures(model).map((arch) => (
                <Badge key={arch} color="blue">{arch}</Badge>
              ))}
            </SpaceBetween>
          ) : (
            <Box color="text-status-inactive">
              Supported architectures are recorded when the model component is packaged and published.
            </Box>
          )}
        </Container>
      )}

      {model.model_type === 'vllm' && (
        <VllmPackagePublishSection
          model={model}
          role={user?.role}
          onModelUpdate={setModel}
        />
      )}

      {model.metrics && Object.keys(model.metrics).length > 0 && (
        <Container header={<Header variant="h2">Model Metrics</Header>}>
          <ColumnLayout columns={5} variant="text-grid">
            {Object.entries(model.metrics).map(([key, value]) => (
              <div key={key}>
                <Box variant="awsui-key-label">{key.replace(/_/g, ' ').toUpperCase()}</Box>
                <Box variant="h3">
                  {typeof value === 'number'
                    ? (key === 'loss'
                        ? value.toFixed(4)
                        // Only ratio metrics (0..1) render as percentages;
                        // raw values like image dimensions render as-is.
                        : (value >= 0 && value <= 1
                            ? `${(value * 100).toFixed(2)}%`
                            : String(value)))
                    : String(value)}
                </Box>
              </div>
            ))}
          </ColumnLayout>
        </Container>
      )}

      {trainingJob ? (
        <CompilationTab
          trainingId={model.training_job_id || model.model_id}
          trainingJob={trainingJob}
          onRefresh={() => {
            const trainingId = model.training_job_id || model.model_id;
            if (trainingId) loadTrainingJob(trainingId);
          }}
        />
      ) : (
        model.compilation_jobs && model.compilation_jobs.length > 0 && (
          <Container header={<Header variant="h2">Compilation Jobs</Header>}>
            <Table
              resizableColumns
              items={model.compilation_jobs}
              columnDefinitions={[
                { id: 'target', header: 'Target', cell: (item) => <Badge>{item.target}</Badge> },
                { id: 'status', header: 'Status', cell: (item) => {
                  // Case-insensitive classification so 'Completed'/'COMPLETED'
                  // etc. all reach their intended arm (2.16, 2.17).
                  const status = String(item.status || '').toUpperCase();
                  if (status === 'COMPLETED') return <Badge color="green">Completed</Badge>;
                  if (status === 'INPROGRESS' || status === 'IN_PROGRESS' || status === 'STARTING') return <Badge color="blue">In Progress</Badge>;
                  if (status === 'FAILED') return <Badge color="red">Failed</Badge>;
                  // Transient poll fault: the job's true status is unknown and
                  // it will be re-polled — not a terminal failure.
                  if (status === 'ERROR') return <Badge color="grey">Status unavailable — retrying</Badge>;
                  return <Badge>{item.status}</Badge>;
                }},
                { id: 'reason', header: 'Reason', cell: (item) => {
                  // Surface the preserved originating reason so this page can
                  // never again show only a status token (2.14, 2.15).
                  const reason = item.failure_reason || item.error;
                  if (!reason && !item.poll_error) {
                    return <Box fontSize="body-s" color="text-status-inactive">N/A</Box>;
                  }
                  return (
                    <SpaceBetween size="xxs">
                      {reason && <Box fontSize="body-s">{reason}</Box>}
                      {item.poll_error && (
                        <Box fontSize="body-s" color="text-status-inactive">
                          Status lookup error: {item.poll_error}
                        </Box>
                      )}
                    </SpaceBetween>
                  );
                }},
                { id: 'output', header: 'Output', cell: (item) => item.compiled_model_s3 ? 
                  <Box fontSize="body-s" color="text-status-info">Available</Box> : 
                  <Box fontSize="body-s" color="text-status-inactive">N/A</Box> },
              ]}
              empty={<Box textAlign="center">No compilation jobs</Box>}
            />
          </Container>
        )
      )}

      {model.component_arns && Object.keys(model.component_arns).length > 0 && (
        <Container header={<Header variant="h2">Greengrass Components</Header>}>
          <Table
            resizableColumns
            items={Object.entries(model.component_arns).map(([platform, arn]) => ({ platform, arn }))}
            columnDefinitions={[
              { id: 'platform', header: 'Platform', cell: (item) => <Badge>{item.platform}</Badge> },
              { id: 'arn', header: 'Component ARN', cell: (item) => (
                <Box fontSize="body-s"><span style={{ fontFamily: 'monospace' }}>{item.arn}</span></Box>
              )},
            ]}
            empty={<Box textAlign="center">No components available</Box>}
          />
        </Container>
      )}

      <Container header={<Header variant="h2" counter={`(${model.deployed_devices.length})`}>Deployed Devices</Header>}>
        {model.deployed_devices.length > 0 ? (
          <Table
            resizableColumns
            items={model.deployed_devices.map((deviceId) => ({ device_id: deviceId }))}
            columnDefinitions={[
              { id: 'device_id', header: 'Device ID', cell: (item) => (
                <Link onFollow={() => navigate(`/devices/${item.device_id}`)}>{item.device_id}</Link>
              )},
            ]}
          />
        ) : (
          <Box textAlign="center" color="inherit" padding="l">This model is not deployed to any devices yet.</Box>
        )}
      </Container>

      <Container header={<Header variant="h2">History</Header>}>
        <KeyValuePairs columns={4} items={[
          { label: 'Created', value: formatTimestamp(model.created_at) },
          { label: 'Completed', value: formatTimestamp(model.completed_at) },
          { label: 'Promoted At', value: formatTimestamp(model.promoted_at) },
          { label: 'Promoted By', value: model.promoted_by || 'N/A' },
        ]} />
      </Container>

      {model.artifact_s3 && (
        <Container header={<Header variant="h2">Artifact Location</Header>}>
          <Box fontSize="body-s"><span style={{ fontFamily: 'monospace' }}>{model.artifact_s3}</span></Box>
        </Container>
      )}

      <Modal visible={showPromoteModal} onDismiss={() => { setShowPromoteModal(false); setTargetStage(null); }}
        header="Change Model Stage"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setShowPromoteModal(false); setTargetStage(null); }}>Cancel</Button>
          <Button variant="primary" disabled={!targetStage || actionLoading} loading={actionLoading} onClick={handlePromote}>Update Stage</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          <Alert type="info">Promoting a model to a higher stage makes it available for deployment. Demoting moves it back.</Alert>
          <Box><Box variant="strong">Current Stage:</Box> {getStageBadge(model.stage)}</Box>
          <Box>
            <Box variant="strong" padding={{ bottom: 's' }}>Change to:</Box>
            <Select selectedOption={targetStage} onChange={({ detail }) => setTargetStage(detail.selectedOption)}
              options={getPromotionOptions()} placeholder="Select target stage" />
          </Box>
          {model.deployed_devices.length > 0 && (
            <Alert type="warning">This model is deployed to {model.deployed_devices.length} device(s). Stage changes won't affect existing deployments.</Alert>
          )}
        </SpaceBetween>
      </Modal>

      <Modal visible={showDeleteModal} onDismiss={() => setShowDeleteModal(false)} header="Delete Model"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setShowDeleteModal(false)}>Cancel</Button>
          <Button variant="primary" disabled={actionLoading || !canDelete} loading={actionLoading} onClick={handleDelete}>Delete</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {!canDelete ? (
            <Alert type="error">Cannot delete - model is deployed to {model.deployed_devices.length} device(s). Undeploy first.</Alert>
          ) : (
            <Alert type="warning">Delete model "{model.name}" (v{model.version})? This cannot be undone.</Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
