/**
 * Synthetic data generation workspace: Generation_Session list plus the
 * create-session wizard (synthetic-defect-data-generation, task 7.2).
 *
 * - Session list with status and creation time (Req 10.4)
 * - Model select with capability flags and empty-catalog guidance (Req 1.1, 1.3)
 * - Object_Type / Defect_Type entry with stored/default Prompt_Template
 *   loading and saving (Req 2.2, 2.3, 2.4)
 * - Dataset browse via the existing dataset discovery endpoints with
 *   presigned Source_Image thumbnails (Req 3.1, 3.5)
 * - Source classification (Defect_Images / Normal_Images) with a required
 *   Defect_Type for Normal_Images (Req 3.2, 3.3, 3.4)
 * - Variation_Count constrained to an integer 1..20 with the valid-range
 *   message (Req 4.1, 4.4)
 * - Seed / guidance-strength controls per model capability flags with
 *   model-appropriate defaults (Req 4.3)
 * - At-least-one-Source_Image validation (Req 3.6)
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  Link,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  StatusIndicatorProps,
  Table,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  SyntheticModel,
  SyntheticSessionStatus,
  SyntheticSessionSummary,
  SyntheticSourceClass,
  SyntheticSourceImage,
} from './types';

/** Valid-range message for Variation_Count (Req 4.1, 4.4). */
export const VARIATION_COUNT_MESSAGE =
  'Variation count must be an integer between 1 and 20';

/** At-least-one-source message (Req 3.6). */
export const NO_SOURCES_MESSAGE = 'At least one Source_Image is required';

/** True iff `value` parses as an integer in 1..20 (Req 4.1). */
export function isValidVariationCount(value: string): boolean {
  if (!/^\d+$/.test(value.trim())) return false;
  const n = Number(value.trim());
  return Number.isInteger(n) && n >= 1 && n <= 20;
}

const STATUS_INDICATOR: Record<
  SyntheticSessionStatus,
  { type: StatusIndicatorProps.Type; label: string }
> = {
  draft: { type: 'pending', label: 'Draft' },
  generating: { type: 'in-progress', label: 'Generating' },
  awaiting_review: { type: 'info', label: 'Awaiting review' },
  approved: { type: 'success', label: 'Approved' },
  integrated: { type: 'success', label: 'Integrated' },
  failed: { type: 'error', label: 'Failed' },
};

interface DatasetRow {
  prefix: string;
  image_count: number;
  last_modified: string | null;
}

interface SourceThumb {
  key: string;
  filename: string;
  presigned_url: string;
}

/** Capability flags of a model rendered as a compact description string. */
function capabilitySummary(model: SyntheticModel): string {
  const flags: string[] = [];
  if (model.capabilities.text_to_image) flags.push('text-to-image');
  if (model.capabilities.inpainting) flags.push('inpainting');
  if (model.capabilities.image_variation) flags.push('image variation');
  if (model.capabilities.seed) flags.push('seed');
  if (model.capabilities.cfg_scale) flags.push('cfg scale');
  return flags.join(', ');
}

export default function SyntheticData() {
  const navigate = useNavigate();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();

  // Use case selection (same pattern as CreateTraining).
  const [useCaseOptions, setUseCaseOptions] = useState<SelectProps.Option[]>([]);
  const [useCase, setUseCase] = useState<SelectProps.Option | null>(null);

  // Session list (Req 10.4).
  const [sessions, setSessions] = useState<SyntheticSessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // Wizard visibility.
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Model catalog (Req 1.1, 1.3).
  const [models, setModels] = useState<SyntheticModel[]>([]);
  const [modelsGuidance, setModelsGuidance] = useState<string | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);

  // Object / defect type + prompt template (Req 2.2-2.4).
  const [objectType, setObjectType] = useState('');
  const [defectType, setDefectType] = useState('');
  const [promptText, setPromptText] = useState('');
  const [promptIsDefault, setPromptIsDefault] = useState<boolean | null>(null);
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptSaved, setPromptSaved] = useState(false);

  // Dataset browse + source selection (Req 3.1, 3.5, 3.6).
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [datasetPrefix, setDatasetPrefix] = useState<SelectProps.Option | null>(null);
  const [thumbs, setThumbs] = useState<SourceThumb[]>([]);
  const [thumbsLoading, setThumbsLoading] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());

  // Source classification (Req 3.2-3.4).
  const [sourceClass, setSourceClass] = useState<SyntheticSourceClass | ''>('');

  // Generation params (Req 4.1, 4.3).
  const [variationCount, setVariationCount] = useState('5');
  const [seed, setSeed] = useState('');
  const [cfgScale, setCfgScale] = useState('');

  // Target manifest (used at integration time, editable default).
  const [manifestKey, setManifestKey] = useState('');

  const [submitting, setSubmitting] = useState(false);

  const selectedModel = useMemo(
    () => models.find((m) => m.model_id === selectedModelId) ?? null,
    [models, selectedModelId]
  );

  // ----------------------------------------------------------- data loads

  useEffect(() => {
    let cancelled = false;
    apiService
      .listUseCases()
      .then(({ usecases }) => {
        if (cancelled) return;
        const options = usecases.map((uc) => ({
          label: uc.name,
          value: uc.usecase_id,
        }));
        setUseCaseOptions(options);
        const saved = options.find((o) => o.value === selectedUsecaseId);
        const chosen = saved ?? options[0] ?? null;
        setUseCase(chosen);
        if (chosen?.value) setSelectedUsecaseId(chosen.value);
      })
      .catch((err) => console.error('Failed to fetch use cases:', err));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const usecaseId = useCase?.value as string | undefined;

  const loadSessions = useCallback(() => {
    if (!usecaseId) return;
    setSessionsLoading(true);
    apiService
      .listSyntheticSessions(usecaseId)
      .then(({ sessions: rows }) => setSessions(rows))
      .catch((err) => setError(getErrorMessage(err, 'Failed to list sessions')))
      .finally(() => setSessionsLoading(false));
  }, [usecaseId]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Model_Catalog + dataset discovery, loaded when the wizard opens
  // (Req 1.1, 3.1).
  useEffect(() => {
    if (!creating || !usecaseId) return;
    setModelsLoading(true);
    apiService
      .listSyntheticModels(usecaseId)
      .then((response) => {
        setModels(response.models);
        setModelsGuidance(response.models.length === 0 ? response.guidance ?? null : null);
      })
      .catch((err) => setError(getErrorMessage(err, 'Failed to load model catalog')))
      .finally(() => setModelsLoading(false));
    apiService
      .listDatasets({ usecase_id: usecaseId })
      .then(({ datasets: rows }) => setDatasets(rows))
      .catch((err) => setError(getErrorMessage(err, 'Failed to list datasets')));
  }, [creating, usecaseId]);

  // Stored/default Prompt_Template for the Object_Type/Defect_Type pair
  // (Req 2.2, 2.3).
  useEffect(() => {
    if (!creating || !usecaseId || !objectType.trim() || !defectType.trim()) return;
    let cancelled = false;
    apiService
      .getSyntheticPromptTemplate({
        usecase_id: usecaseId,
        object_type: objectType.trim(),
        defect_type: defectType.trim(),
      })
      .then((response) => {
        if (cancelled) return;
        setPromptText(response.template_text);
        setPromptIsDefault(response.is_default);
        setPromptSaved(false);
      })
      .catch((err) => console.error('Failed to load prompt template:', err));
    return () => {
      cancelled = true;
    };
  }, [creating, usecaseId, objectType, defectType]);

  // Presigned Source_Image thumbnails for the browsed prefix (Req 3.5).
  useEffect(() => {
    const prefix = datasetPrefix?.value as string | undefined;
    if (!creating || !usecaseId || !prefix) return;
    setThumbsLoading(true);
    setThumbs([]);
    apiService
      .getImagePreview({ usecase_id: usecaseId, prefix, limit: 50 })
      .then(({ images }) =>
        setThumbs(
          images.map((img) => ({
            key: img.key,
            filename: img.filename,
            presigned_url: img.presigned_url,
          }))
        )
      )
      .catch((err) => setError(getErrorMessage(err, 'Failed to preview images')))
      .finally(() => setThumbsLoading(false));
    if (!manifestKey) {
      setManifestKey(`${prefix}manifests/train.manifest`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating, usecaseId, datasetPrefix]);

  // Model-appropriate randomization defaults on model selection (Req 4.3).
  const handleModelChange = (option: SelectProps.Option) => {
    setSelectedModelId(option.value ?? null);
    const model = models.find((m) => m.model_id === option.value);
    if (model) {
      const defaults = model.randomization_defaults;
      setSeed(defaults.seed === null || defaults.seed === undefined ? '' : String(defaults.seed));
      setCfgScale(
        defaults.cfg_scale === null || defaults.cfg_scale === undefined
          ? ''
          : String(defaults.cfg_scale)
      );
    }
  };

  const toggleSourceImage = (key: string) => {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const savePromptTemplate = async () => {
    if (!usecaseId || !objectType.trim() || !defectType.trim()) return;
    try {
      setPromptSaving(true);
      await apiService.putSyntheticPromptTemplate({
        usecase_id: usecaseId,
        object_type: objectType.trim(),
        defect_type: defectType.trim(),
        template_text: promptText,
      });
      setPromptIsDefault(false);
      setPromptSaved(true);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to save prompt template'));
    } finally {
      setPromptSaving(false);
    }
  };

  // ----------------------------------------------------------- validation

  const variationCountInvalid = !isValidVariationCount(variationCount);
  const defectTypeMissingForNormal =
    sourceClass === 'normal' && !defectType.trim();

  const validationErrors: string[] = [];
  if (creating) {
    if (!selectedModelId) validationErrors.push('A Generation_Model must be selected');
    if (!objectType.trim()) validationErrors.push('Object type is required');
    if (selectedKeys.size === 0) validationErrors.push(NO_SOURCES_MESSAGE);
    if (!sourceClass)
      validationErrors.push(
        'The selection must be classified as defect or normal images'
      );
    if (defectTypeMissingForNormal)
      validationErrors.push(
        'A Defect_Type to synthesize is required for normal source images'
      );
    if (variationCountInvalid) validationErrors.push(VARIATION_COUNT_MESSAGE);
  }

  const handleCreate = async () => {
    if (!usecaseId || validationErrors.length > 0) return;
    const prefix = (datasetPrefix?.value as string) || '';
    const sourceImages: SyntheticSourceImage[] = Array.from(selectedKeys).map(
      (key) => ({ key })
    );
    try {
      setSubmitting(true);
      setError(null);
      const { session } = await apiService.createSyntheticSession({
        usecase_id: usecaseId,
        generation_model_id: selectedModelId ?? undefined,
        object_type: objectType.trim(),
        defect_type: defectType.trim() || undefined,
        prompt_template_text: promptText || undefined,
        source_class: sourceClass || undefined,
        source_images: sourceImages,
        generation_params: {
          variation_count: Number(variationCount),
          ...(seed !== '' && { seed: Number(seed) }),
          ...(cfgScale !== '' && { cfg_scale: Number(cfgScale) }),
        },
        target_dataset_prefix: prefix || undefined,
        target_manifest_key: manifestKey || undefined,
      });
      navigate(`/synthetic/${session.session_id}`);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to create generation session'));
    } finally {
      setSubmitting(false);
    }
  };

  // -------------------------------------------------------------- render

  const modelOptions: SelectProps.Option[] = models.map((model) => ({
    label: model.display_name,
    value: model.model_id,
    description: capabilitySummary(model),
  }));

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={useCase}
                  onChange={({ detail }) => {
                    setUseCase(detail.selectedOption);
                    if (detail.selectedOption.value)
                      setSelectedUsecaseId(detail.selectedOption.value);
                  }}
                  options={useCaseOptions}
                  placeholder="Select a use case"
                  selectedAriaLabel="Selected"
                />
                {!creating && (
                  <Button
                    variant="primary"
                    onClick={() => setCreating(true)}
                    disabled={!usecaseId}
                  >
                    Create generation session
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            Synthetic Data Generation
          </Header>
        }
      >
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {/* Session list: status + creation time (Req 10.4). */}
        <Table
          variant="embedded"
          loading={sessionsLoading}
          loadingText="Loading sessions"
          items={sessions}
          trackBy="session_id"
          columnDefinitions={[
            {
              id: 'session',
              header: 'Session',
              cell: (item) => (
                <Link
                  onFollow={(e) => {
                    e.preventDefault();
                    navigate(`/synthetic/${item.session_id}`);
                  }}
                  href={`/synthetic/${item.session_id}`}
                >
                  {item.session_id.slice(0, 8)}
                </Link>
              ),
            },
            {
              id: 'status',
              header: 'Status',
              cell: (item) => {
                const status = STATUS_INDICATOR[item.status] ?? {
                  type: 'pending' as const,
                  label: item.status,
                };
                return (
                  <StatusIndicator type={status.type}>
                    {status.label}
                  </StatusIndicator>
                );
              },
            },
            {
              id: 'object',
              header: 'Object type',
              cell: (item) => item.object_type || '—',
            },
            {
              id: 'defect',
              header: 'Defect type',
              cell: (item) => item.defect_type || '—',
            },
            {
              id: 'model',
              header: 'Model',
              cell: (item) => item.generation_model_id || '—',
            },
            {
              id: 'created',
              header: 'Created',
              cell: (item) =>
                item.created_at ? new Date(item.created_at).toLocaleString() : '—',
            },
          ]}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No generation sessions</b>
              <Box variant="p" color="inherit">
                Create a session to generate synthetic defect data.
              </Box>
            </Box>
          }
        />
      </Container>

      {creating && (
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setCreating(false)} disabled={submitting}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreate}
                disabled={validationErrors.length > 0 || submitting}
                loading={submitting}
              >
                Create session
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            {validationErrors.length > 0 && (
              <Alert type="warning">
                <ul style={{ margin: 0, paddingLeft: '20px' }}>
                  {validationErrors.map((message) => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              </Alert>
            )}

            {/* Model selection with capability flags (Req 1.1, 1.2). */}
            <Container header={<Header variant="h2">Generation model</Header>}>
              <SpaceBetween size="m">
                {modelsLoading && <Spinner />}
                {modelsGuidance && (
                  <Alert type="warning" header="No generation models available">
                    {modelsGuidance}
                  </Alert>
                )}
                {models.length > 0 && (
                  <FormField
                    label="Generation model"
                    description="Models available in the portal region, with capability flags"
                    stretch
                  >
                    <Select
                      selectedOption={
                        modelOptions.find((o) => o.value === selectedModelId) ?? null
                      }
                      onChange={({ detail }) => handleModelChange(detail.selectedOption)}
                      options={modelOptions}
                      placeholder="Select a generation model"
                      selectedAriaLabel="Selected"
                    />
                  </FormField>
                )}
                {selectedModel && (
                  <SpaceBetween direction="horizontal" size="xs">
                    {Object.entries(selectedModel.capabilities)
                      .filter(([, enabled]) => enabled)
                      .map(([flag]) => (
                        <Badge key={flag} color="blue">
                          {flag.replace(/_/g, ' ')}
                        </Badge>
                      ))}
                  </SpaceBetween>
                )}
              </SpaceBetween>
            </Container>

            {/* Object/defect types + prompt editor (Req 2.2-2.4). */}
            <Container header={<Header variant="h2">Prompt template</Header>}>
              <SpaceBetween size="m">
                <FormField
                  label="Object type"
                  description='The inspected part or product (e.g. "metal casting")'
                  stretch
                >
                  <Input
                    value={objectType}
                    onChange={({ detail }) => setObjectType(detail.value)}
                    placeholder="e.g. metal casting"
                    ariaLabel="Object type"
                  />
                </FormField>
                <FormField
                  label="Defect type"
                  description='The defect to synthesize (e.g. "scratch"). Required for normal source images.'
                  errorText={
                    defectTypeMissingForNormal
                      ? 'A Defect_Type to synthesize is required for normal source images'
                      : undefined
                  }
                  stretch
                >
                  <Input
                    value={defectType}
                    onChange={({ detail }) => setDefectType(detail.value)}
                    placeholder="e.g. scratch"
                    ariaLabel="Defect type"
                  />
                </FormField>
                <FormField
                  label="Prompt template"
                  description={
                    promptIsDefault === null
                      ? 'Enter an object and defect type to load the template'
                      : promptIsDefault
                        ? 'Default template (no stored template for this object/defect type)'
                        : 'Stored template for this object/defect type'
                  }
                  stretch
                >
                  <Textarea
                    value={promptText}
                    onChange={({ detail }) => {
                      setPromptText(detail.value);
                      setPromptSaved(false);
                    }}
                    rows={4}
                    placeholder="Prompt template with {object_type} and {defect_type} placeholders"
                    ariaLabel="Prompt template"
                  />
                </FormField>
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    onClick={savePromptTemplate}
                    loading={promptSaving}
                    disabled={!objectType.trim() || !defectType.trim() || !promptText.trim()}
                  >
                    Save template
                  </Button>
                  {promptSaved && (
                    <StatusIndicator type="success">Template saved</StatusIndicator>
                  )}
                </SpaceBetween>
              </SpaceBetween>
            </Container>

            {/* Source image browse + selection (Req 3.1, 3.5, 3.6). */}
            <Container header={<Header variant="h2">Source images</Header>}>
              <SpaceBetween size="m">
                <FormField
                  label="Dataset"
                  description="Datasets discovered in the use case data bucket"
                  stretch
                >
                  <Select
                    selectedOption={datasetPrefix}
                    onChange={({ detail }) => {
                      setDatasetPrefix(detail.selectedOption);
                      setSelectedKeys(new Set());
                    }}
                    options={datasets.map((dataset) => ({
                      label: dataset.prefix,
                      value: dataset.prefix,
                      description: `${dataset.image_count} images`,
                    }))}
                    placeholder="Select a dataset prefix"
                    empty="No datasets found"
                    selectedAriaLabel="Selected"
                  />
                </FormField>
                {thumbsLoading && <Spinner />}
                {thumbs.length > 0 && (
                  <>
                    <Box variant="small">
                      Click images to select them as Source_Images (
                      {selectedKeys.size} selected)
                    </Box>
                    <div
                      style={{
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: '8px',
                      }}
                    >
                      {thumbs.map((thumb) => {
                        const selected = selectedKeys.has(thumb.key);
                        return (
                          <button
                            key={thumb.key}
                            type="button"
                            data-testid={`source-thumb-${thumb.filename}`}
                            aria-pressed={selected}
                            aria-label={`Select ${thumb.filename}`}
                            onClick={() => toggleSourceImage(thumb.key)}
                            style={{
                              border: selected
                                ? '3px solid #0972d3'
                                : '1px solid #d5dbdb',
                              padding: 2,
                              background: 'none',
                              cursor: 'pointer',
                              borderRadius: 4,
                            }}
                          >
                            <img
                              src={thumb.presigned_url}
                              alt={thumb.filename}
                              style={{
                                width: 96,
                                height: 96,
                                objectFit: 'cover',
                                display: 'block',
                              }}
                            />
                          </button>
                        );
                      })}
                    </div>
                  </>
                )}
                {/* Classification of the selection (Req 3.2-3.4). */}
                <FormField
                  label="Source classification"
                  description="Classify the selected images"
                  stretch
                >
                  <RadioGroup
                    value={sourceClass || null}
                    onChange={({ detail }) =>
                      setSourceClass(detail.value as SyntheticSourceClass)
                    }
                    items={[
                      {
                        value: 'defect',
                        label: 'Defect images',
                        description:
                          'The images already contain defects; the defect type may be specified',
                      },
                      {
                        value: 'normal',
                        label: 'Normal images',
                        description:
                          'Non-defective images; the defect type to synthesize is required',
                      },
                    ]}
                  />
                </FormField>
              </SpaceBetween>
            </Container>

            {/* Generation controls (Req 4.1, 4.3, 4.4). */}
            <Container header={<Header variant="h2">Generation controls</Header>}>
              <SpaceBetween size="m">
                <FormField
                  label="Variations per source image"
                  description="Number of variations generated per Source_Image (1-20)"
                  errorText={variationCountInvalid ? VARIATION_COUNT_MESSAGE : undefined}
                  stretch
                >
                  <Input
                    type="number"
                    value={variationCount}
                    onChange={({ detail }) => setVariationCount(detail.value)}
                    invalid={variationCountInvalid}
                    ariaLabel="Variation count"
                  />
                </FormField>
                {selectedModel?.capabilities.seed && (
                  <FormField
                    label="Seed"
                    description="Randomization seed (leave blank for random)"
                    stretch
                  >
                    <Input
                      type="number"
                      value={seed}
                      onChange={({ detail }) => setSeed(detail.value)}
                      ariaLabel="Seed"
                    />
                  </FormField>
                )}
                {selectedModel?.capabilities.cfg_scale && (
                  <FormField
                    label="Guidance strength (cfgScale)"
                    description="How strongly the prompt guides generation"
                    stretch
                  >
                    <Input
                      type="number"
                      value={cfgScale}
                      onChange={({ detail }) => setCfgScale(detail.value)}
                      ariaLabel="Guidance strength"
                    />
                  </FormField>
                )}
                <FormField
                  label="Target manifest key"
                  description="Data_Manifest the approved images will be appended to at integration"
                  stretch
                >
                  <Input
                    value={manifestKey}
                    onChange={({ detail }) => setManifestKey(detail.value)}
                    placeholder="datasets/.../manifests/train.manifest"
                    ariaLabel="Target manifest key"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
          </SpaceBetween>
        </Form>
      )}
    </SpaceBetween>
  );
}
