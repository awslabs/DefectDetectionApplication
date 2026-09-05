/**
 * PromptTuningPreview — the Prompt_Tuning_Preview surface of the labeling
 * job creation flow (llm-autolabel-prompt-tuning Requirements 1.3, 1.4,
 * 1.7, 1.8, 2.1-2.8, 4.5-4.7, 5.1-5.4, 9.7, 9.8).
 *
 * Three parts, all driven by props so the wizard stays the single source
 * of truth for the job configuration (the component never mutates wizard
 * state, so a rejected run leaves the flow exactly as it was):
 *
 * 1. Sample picker — the existing paged `/datasets/preview` listing with
 *    `extensions=jpg,jpeg,png` (req 2.1) at `limit=50`, rendering each
 *    object key together with its thumbnail (req 2.2). Selection is
 *    capped at 5 (req 2.3) and is retained across pages and across runs
 *    (req 5.2). A thumbnail whose `<img>` fails renders its key instead
 *    and stays selectable (req 2.8). An empty listing and an inaccessible
 *    prefix produce distinct messages that name the prefix and disable the
 *    run control (req 2.5); refreshing re-lists and re-enables it
 *    (req 2.6).
 * 2. Run control — pre-flight validation mirroring the Preview_API's
 *    rules; on rejection every violated rule is listed, no request is
 *    issued and no wizard state is touched (req 1.4, 2.4, 6.2). While a
 *    run is in flight the control is disabled and an in-progress
 *    indication shows (req 1.7, 4.5). A few-shot run uploads the example
 *    images first; an upload failure aborts the start and surfaces the
 *    message naming the failing file.
 * 3. Results — short-polls `GET /labeling-preview/runs/{runId}` every 2 s,
 *    stopping on `Completed`, `Failed`, a `404`, or the overall bound of
 *    `sample_count × 120 s + 60 s` (req 1.8, 4.7). Exactly one entry per
 *    requested Sample_Image, keyed by sample key (req 4.6), rendered
 *    progressively as samples resolve. A new run's results replace the
 *    previous set wholesale once its first result arrives (req 5.3); a run
 *    that fails before producing any result leaves the previous set
 *    displayed unchanged (req 5.4). Failures show their category and
 *    reason beside the sample, and `unusable_model_output` additionally
 *    offers the complete raw model text in a native disclosure with no
 *    truncation (req 9.7, 9.8).
 *
 * Successful results are drawn by `PreviewResultCanvas`, which owns the
 * modality overlay rendering (req 4.1-4.4).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Pagination from '@cloudscape-design/components/pagination';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import {
  ApiError,
  apiService,
  type PreviewFailureCategory,
  type PreviewFewShotExample,
  type PreviewResultEntry,
  type PreviewResultPayload,
  type PreviewRunResponse,
  type PreviewSampleState,
} from '../../services/api';
import PreviewResultCanvas from './PreviewResultCanvas';
import type { LabelingModality } from './AnnotationCanvas';

/** Sample_Limit — the most Sample_Images one Preview_Run may carry (2.3). */
export const SAMPLE_LIMIT = 5;
/** Detection_Prompt length bound, matching the API rule (1.4, 8.4). */
export const MAX_DETECTION_PROMPT_LENGTH = 2000;
/** Label_Set bounds for Segmentation / ObjectDetection (`_validate_label_set`). */
const MAX_LABELS = 10;
const MAX_LABEL_LENGTH = 64;
/** Fixed Label_Set for Binary_Classification. */
const FIXED_CLASSIFICATION_LABEL_SET = ['normal', 'anomaly'];
/** Most Few_Shot_Examples per designation (6.2, 8.4). */
const MAX_EXAMPLES_PER_DESIGNATION = 10;
/** Listing page size — the endpoint's per-page cap, within req 2.7. */
export const SAMPLE_PAGE_SIZE = 50;
/** Only JPEG and PNG objects are selectable (req 2.1). */
const SAMPLE_EXTENSIONS = 'jpg,jpeg,png';
/** Short-poll interval for the run status route. */
export const POLL_INTERVAL_MS = 2000;
/** Per-Sample_Image invocation bound the Preview_API enforces (3.3). */
const PER_SAMPLE_BOUND_MS = 120_000;
/** Slack over the per-sample bounds before the client gives up (1.8). */
const RUN_BOUND_SLACK_MS = 60_000;

/** Human wording for each failure category (req 9.7). */
const FAILURE_CATEGORY_LABELS: Record<PreviewFailureCategory, string> = {
  model_error: 'Model error',
  timeout: 'Timeout',
  unusable_model_output: 'Unusable model output',
  image_access_failure: 'Image access failure',
  unsupported_image_content: 'Unsupported image content',
  unreadable_example_image: 'Unreadable example image',
};

/**
 * The ordered Few_Shot_Example set for uploaded example refs: good in
 * upload order first, then bad, each carrying its designation and its
 * position *within* that designation — the same shape the job record
 * persists, so preview and labeling time attach the identical set
 * (Requirements 6.4, 6.6).
 */
export function previewFewShotExamples(refs: {
  good: string[];
  bad: string[];
}): PreviewFewShotExample[] {
  return [
    ...refs.good.map((ref, position) => ({
      ref,
      designation: 'good' as const,
      position,
    })),
    ...refs.bad.map((ref, position) => ({
      ref,
      designation: 'bad' as const,
      position,
    })),
  ];
}

/**
 * Every violated pre-flight rule for a Preview_Run attempt, mirroring the
 * Preview_API's validation so a rejection never reaches the network
 * (Requirements 1.4, 2.4, 6.2, 8.4). An empty list means the attempt is
 * allowed through.
 */
export function validatePreviewRunInputs(input: {
  model: string;
  detectionPrompt: string;
  taskType: string;
  labelSet: string[];
  selectedCount: number;
  fewShotEnabled: boolean;
  goodExampleCount: number;
  badExampleCount: number;
}): string[] {
  const violations: string[] = [];

  if (!input.model.startsWith('llm:') || input.model.length <= 'llm:'.length) {
    violations.push(
      `Model "${input.model || '(none)'}" is not a prompt-guided LLM model; the preview supports only llm: models`
    );
  }

  if (!input.detectionPrompt.trim()) {
    violations.push('A detection prompt is required');
  } else if (input.detectionPrompt.length > MAX_DETECTION_PROMPT_LENGTH) {
    violations.push(
      `The detection prompt exceeds ${MAX_DETECTION_PROMPT_LENGTH.toLocaleString()} characters`
    );
  }

  const modalities: string[] = [
    'Classification',
    'Segmentation',
    'ObjectDetection',
  ];
  if (!modalities.includes(input.taskType)) {
    violations.push(`Task type "${input.taskType || '(none)'}" is not supported`);
  } else if (input.taskType === 'Classification') {
    const matchesFixed =
      input.labelSet.length === FIXED_CLASSIFICATION_LABEL_SET.length &&
      FIXED_CLASSIFICATION_LABEL_SET.every((l, i) => input.labelSet[i] === l);
    if (!matchesFixed) {
      violations.push(
        `Classification requires the fixed label set ${FIXED_CLASSIFICATION_LABEL_SET.join(', ')}`
      );
    }
  } else {
    const labels = input.labelSet.map((l) => l.trim());
    if (labels.length === 0 || labels.some((l) => !l)) {
      violations.push('Provide at least one non-empty label');
    } else if (labels.length > MAX_LABELS) {
      violations.push(`The label set supports at most ${MAX_LABELS} labels`);
    } else if (labels.some((l) => l.length > MAX_LABEL_LENGTH)) {
      violations.push(
        `Every label must be at most ${MAX_LABEL_LENGTH} characters`
      );
    } else if (new Set(labels).size !== labels.length) {
      violations.push('Label names must be distinct');
    }
  }

  if (input.selectedCount < 1 || input.selectedCount > SAMPLE_LIMIT) {
    violations.push(
      `Select between 1 and ${SAMPLE_LIMIT} sample images (${input.selectedCount} selected)`
    );
  }

  if (input.fewShotEnabled) {
    if (input.goodExampleCount + input.badExampleCount === 0) {
      violations.push(
        'At least one example image is required for the few-shot examples option'
      );
    }
    if (
      input.goodExampleCount > MAX_EXAMPLES_PER_DESIGNATION ||
      input.badExampleCount > MAX_EXAMPLES_PER_DESIGNATION
    ) {
      violations.push(
        `At most ${MAX_EXAMPLES_PER_DESIGNATION} good and ${MAX_EXAMPLES_PER_DESIGNATION} bad example images can be attached`
      );
    }
  }

  return violations;
}

/** One listed dataset image the picker can select. */
interface SampleListingImage {
  key: string;
  filename: string;
  presigned_url: string;
}

/** One displayed result entry, paired with its Sample_Image (req 4.6). */
interface DisplayedResult {
  index: number;
  sampleKey: string;
  imageUrl?: string;
  state: PreviewSampleState;
  failureCategory?: PreviewFailureCategory;
  failureReason?: string;
  payload?: PreviewResultPayload;
  payloadError?: string;
}

/** The result set currently on screen, always from exactly one run. */
interface DisplayedRun {
  runId: string;
  taskType: LabelingModality;
  labelSet: string[];
  fewShot: PreviewRunResponse['few_shot'];
  status: PreviewRunResponse['status'];
  results: DisplayedResult[];
}

export interface PromptTuningPreviewProps {
  /** Use_Case the dataset prefix and the Preview_Run belong to. */
  usecaseId: string;
  /** Dataset prefix the Sample_Images are listed from and scoped to. */
  datasetPrefix: string;
  /** The configured auto-label model, e.g. `llm:us.amazon.nova-pro-v1:0`. */
  model: string;
  /** Detection_Prompt exactly as entered; sent character-for-character. */
  detectionPrompt: string;
  /** The job's Labeling_Modality. */
  taskType: LabelingModality;
  /** The effective Label_Set (fixed normal/anomaly for Classification). */
  labelSet: string[];
  /** Few_Shot_Option value as it stands in the wizard. */
  fewShotEnabled: boolean;
  /** Count of good example images staged in the wizard. */
  goodExampleCount: number;
  /** Count of bad example images staged in the wizard. */
  badExampleCount: number;
  /**
   * The wizard's memoized example upload helper. Called only for a
   * few-shot run, so a run without the option uploads nothing; a rejected
   * upload aborts the start with the message naming the failing file.
   */
  ensureExampleImagesUploaded: () => Promise<{ good: string[]; bad: string[] }>;
}

export default function PromptTuningPreview({
  usecaseId,
  datasetPrefix,
  model,
  detectionPrompt,
  taskType,
  labelSet,
  fewShotEnabled,
  goodExampleCount,
  badExampleCount,
  ensureExampleImagesUploaded,
}: PromptTuningPreviewProps) {
  /* ------------------------- sample picker state ------------------------ */
  const [pageIndex, setPageIndex] = useState(0);
  const [reloadToken, setReloadToken] = useState(0);
  const [listingLoading, setListingLoading] = useState(false);
  const [listingError, setListingError] = useState('');
  const [images, setImages] = useState<SampleListingImage[]>([]);
  const [totalFound, setTotalFound] = useState(0);
  const [listingLoaded, setListingLoaded] = useState(false);
  const [thumbnailFailures, setThumbnailFailures] = useState<
    Record<string, boolean>
  >({});

  /** Selected keys in selection order, retained across pages and runs. */
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  /** Presigned thumbnail URL per key seen so far, for result rendering. */
  const sampleUrls = useRef<Record<string, string>>({});
  const [capMessage, setCapMessage] = useState('');

  /* --------------------------- run state -------------------------------- */
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [runError, setRunError] = useState('');
  const [runInFlight, setRunInFlight] = useState(false);
  const [displayed, setDisplayed] = useState<DisplayedRun | null>(null);
  /** Cancellation token for the active poll loop. */
  const pollToken = useRef<{ cancelled: boolean } | null>(null);

  useEffect(
    () => () => {
      if (pollToken.current) pollToken.current.cancelled = true;
    },
    []
  );

  /* --------------------------- listing --------------------------------- */
  useEffect(() => {
    setPageIndex(0);
  }, [usecaseId, datasetPrefix]);

  useEffect(() => {
    if (!usecaseId || !datasetPrefix) {
      setImages([]);
      setTotalFound(0);
      setListingLoaded(false);
      setListingError('');
      return;
    }
    let cancelled = false;
    setListingLoading(true);
    setListingError('');
    apiService
      .getImagePreview({
        usecase_id: usecaseId,
        prefix: datasetPrefix,
        limit: SAMPLE_PAGE_SIZE,
        offset: pageIndex * SAMPLE_PAGE_SIZE,
        extensions: SAMPLE_EXTENSIONS,
      })
      .then((response) => {
        if (cancelled) return;
        const listed = response.images.map((image) => ({
          key: image.key,
          filename: image.filename,
          presigned_url: image.presigned_url,
        }));
        for (const image of listed) {
          sampleUrls.current[image.key] = image.presigned_url;
        }
        setImages(listed);
        setTotalFound(response.total_found);
        setListingLoaded(true);
        setThumbnailFailures({});
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setImages([]);
        setTotalFound(0);
        setListingLoaded(false);
        setListingError(
          err instanceof Error ? err.message : 'Failed to list dataset images'
        );
      })
      .finally(() => {
        if (!cancelled) setListingLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [usecaseId, datasetPrefix, pageIndex, reloadToken]);

  /** Empty prefix and inaccessible prefix are distinct states (req 2.5). */
  const prefixInaccessible = !!listingError;
  const prefixEmpty = listingLoaded && totalFound === 0;
  const canStartRun = listingLoaded && totalFound > 0 && !runInFlight;

  const pagesCount = Math.max(1, Math.ceil(totalFound / SAMPLE_PAGE_SIZE));

  const toggleSample = useCallback(
    (key: string, checked: boolean) => {
      if (!checked) {
        setCapMessage('');
        setSelectedKeys((current) => current.filter((k) => k !== key));
        return;
      }
      if (selectedKeys.includes(key)) return;
      // The cap is enforced here, so a sixth selection is refused rather
      // than silently dropped (req 2.3).
      if (selectedKeys.length >= SAMPLE_LIMIT) {
        setCapMessage(
          `At most ${SAMPLE_LIMIT} sample images can be previewed in one run. Deselect an image first.`
        );
        return;
      }
      setCapMessage('');
      setSelectedKeys((current) => [...current, key]);
    },
    [selectedKeys]
  );

  /* --------------------------- polling --------------------------------- */
  const commitStatus = useCallback(
    (
      runId: string,
      status: PreviewRunResponse,
      payloads: Record<number, PreviewResultPayload>,
      payloadErrors: Record<number, string>,
      snapshot: { taskType: LabelingModality; labelSet: string[] }
    ) => {
      const results: DisplayedResult[] = status.results.map(
        (entry: PreviewResultEntry) => ({
          index: entry.index,
          sampleKey: entry.sample_key,
          imageUrl: sampleUrls.current[entry.sample_key],
          state: entry.state,
          failureCategory: entry.failure_category,
          failureReason: entry.failure_reason,
          payload: payloads[entry.index],
          payloadError: payloadErrors[entry.index],
        })
      );
      const anyResolved = results.some((r) => r.state !== 'Pending');
      setDisplayed((previous) => {
        // The previous run's results stay on screen until this run
        // produces its first result (req 5.3, 5.4).
        if (previous && previous.runId !== runId && !anyResolved) {
          return previous;
        }
        return {
          runId,
          taskType: snapshot.taskType,
          labelSet: snapshot.labelSet,
          fewShot: status.few_shot,
          status: status.status,
          results,
        };
      });
    },
    []
  );

  const pollRun = useCallback(
    async (
      runId: string,
      sampleCount: number,
      snapshot: { taskType: LabelingModality; labelSet: string[] }
    ) => {
      const token = { cancelled: false };
      if (pollToken.current) pollToken.current.cancelled = true;
      pollToken.current = token;

      const deadline =
        Date.now() + sampleCount * PER_SAMPLE_BOUND_MS + RUN_BOUND_SLACK_MS;
      const payloads: Record<number, PreviewResultPayload> = {};
      const payloadErrors: Record<number, string> = {};

      for (;;) {
        if (token.cancelled) return;
        let status: PreviewRunResponse;
        try {
          status = await apiService.getPreviewRun(runId);
        } catch (err: unknown) {
          if (token.cancelled) return;
          const notFound = err instanceof ApiError && err.status === 404;
          setRunError(
            notFound
              ? `The preview run is no longer available. Start a new preview run.`
              : `The preview run failed: ${
                  err instanceof Error ? err.message : 'unknown error'
                }`
          );
          setRunInFlight(false);
          return;
        }
        if (token.cancelled) return;

        // Fetch each newly resolved sample's payload once: successes need
        // the Pre_Label and dimensions, unusable-output failures need the
        // verbatim raw model text (req 9.8).
        for (const entry of status.results) {
          if (
            entry.state === 'Pending' ||
            !entry.result_url ||
            entry.index in payloads ||
            entry.index in payloadErrors
          ) {
            continue;
          }
          try {
            const response = await fetch(entry.result_url);
            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }
            payloads[entry.index] = (await response.json()) as PreviewResultPayload;
          } catch (err: unknown) {
            payloadErrors[entry.index] =
              err instanceof Error ? err.message : 'unknown error';
          }
        }
        if (token.cancelled) return;

        commitStatus(runId, status, payloads, payloadErrors, snapshot);

        if (status.status !== 'Running') {
          if (status.status === 'Failed') {
            setRunError(
              'The preview run failed. Adjust the prompt, model, or samples and run it again.'
            );
          }
          setRunInFlight(false);
          return;
        }
        if (Date.now() >= deadline) {
          setRunError(
            `The preview run did not return results within ${Math.round(
              (sampleCount * PER_SAMPLE_BOUND_MS + RUN_BOUND_SLACK_MS) / 1000
            )} seconds. Start a new preview run.`
          );
          setRunInFlight(false);
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      }
    },
    [commitStatus]
  );

  /* --------------------------- run start ------------------------------- */
  const handleStartRun = useCallback(async () => {
    setValidationErrors([]);
    setRunError('');
    setCapMessage('');

    const violations = validatePreviewRunInputs({
      model,
      detectionPrompt,
      taskType,
      labelSet,
      selectedCount: selectedKeys.length,
      fewShotEnabled,
      goodExampleCount,
      badExampleCount,
    });
    if (violations.length > 0) {
      // No request is issued and no wizard state is touched (req 1.4).
      setValidationErrors(violations);
      return;
    }

    setRunInFlight(true);

    let examples: PreviewFewShotExample[] = [];
    if (fewShotEnabled) {
      try {
        examples = previewFewShotExamples(await ensureExampleImagesUploaded());
      } catch (err: unknown) {
        setRunError(
          `The preview run was not started: ${
            err instanceof Error ? err.message : 'example image upload failed'
          }`
        );
        setRunInFlight(false);
        return;
      }
    }

    const samples = [...selectedKeys];
    try {
      const started = await apiService.startPreviewRun({
        usecase_id: usecaseId,
        dataset_prefix: datasetPrefix,
        model,
        detection_prompt: detectionPrompt,
        task_type: taskType,
        label_set: labelSet,
        sample_images: samples,
        few_shot: { enabled: fewShotEnabled, examples },
      });
      await pollRun(started.run_id, started.sample_count || samples.length, {
        taskType,
        labelSet,
      });
    } catch (err: unknown) {
      // A start rejection leaves the previously displayed results intact
      // (req 5.4) and re-enables the control (req 1.8, 4.7).
      setRunError(
        `The preview run failed to start: ${
          err instanceof Error ? err.message : 'unknown error'
        }`
      );
      setRunInFlight(false);
    }
  }, [
    model,
    detectionPrompt,
    taskType,
    labelSet,
    selectedKeys,
    fewShotEnabled,
    goodExampleCount,
    badExampleCount,
    ensureExampleImagesUploaded,
    usecaseId,
    datasetPrefix,
    pollRun,
  ]);

  const selectionSummary = useMemo(
    () => `${selectedKeys.length} of ${SAMPLE_LIMIT} sample images selected`,
    [selectedKeys.length]
  );

  return (
    <Container
      data-testid="prompt-tuning-preview"
      header={
        <Header
          variant="h3"
          description="Run the configured model and detection prompt against a few dataset images before creating the job. Preview runs create no labeling job, tasks, or notifications."
          actions={
            <Button
              iconName="refresh"
              data-testid="preview-refresh-samples"
              disabled={listingLoading}
              onClick={() => setReloadToken((t) => t + 1)}
            >
              Refresh
            </Button>
          }
        >
          Prompt tuning preview
        </Header>
      }
    >
      <SpaceBetween size="m">
        {/* ------------------------- sample picker ---------------------- */}
        {prefixInaccessible && (
          <Alert
            type="error"
            header="Dataset prefix not accessible"
            data-testid="preview-prefix-inaccessible"
          >
            The dataset prefix "{datasetPrefix}" could not be listed:{' '}
            {listingError}. Refresh to try again.
          </Alert>
        )}

        {!prefixInaccessible && prefixEmpty && (
          <Alert
            type="info"
            header="No images under the dataset prefix"
            data-testid="preview-prefix-empty"
          >
            The dataset prefix "{datasetPrefix}" contains no JPEG or PNG images
            to preview. Refresh after adding images.
          </Alert>
        )}

        {listingLoading && (
          <Box data-testid="preview-samples-loading">
            <Spinner /> Listing images under "{datasetPrefix}"…
          </Box>
        )}

        {images.length > 0 && (
          <SpaceBetween size="xs">
            <Box color="text-status-inactive" data-testid="preview-selection-count">
              {selectionSummary}
            </Box>
            {capMessage && (
              <Box color="text-status-warning" data-testid="preview-selection-cap">
                {capMessage}
              </Box>
            )}
            <div
              data-testid="preview-sample-grid"
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: '12px',
              }}
            >
              {images.map((image) => {
                const selected = selectedKeys.includes(image.key);
                const thumbnailFailed = thumbnailFailures[image.key];
                return (
                  <div
                    key={image.key}
                    data-testid="preview-sample-item"
                    data-sample-key={image.key}
                    style={{
                      border: selected
                        ? '2px solid #0972d3'
                        : '1px solid #e9ebed',
                      borderRadius: '8px',
                      padding: '8px',
                      width: '160px',
                    }}
                  >
                    <SpaceBetween size="xxs">
                      <Checkbox
                        checked={selected}
                        ariaLabel={`Select sample image ${image.key}`}
                        data-testid="preview-sample-checkbox"
                        onChange={({ detail }) =>
                          toggleSample(image.key, detail.checked)
                        }
                      >
                        {image.filename}
                      </Checkbox>
                      {thumbnailFailed ? (
                        // Thumbnail retrieval failed: the object key stands
                        // in and the image stays selectable (req 2.8).
                        <Box
                          data-testid="preview-thumbnail-fallback"
                          fontSize="body-s"
                        >
                          {image.key}
                        </Box>
                      ) : (
                        <img
                          src={image.presigned_url}
                          alt={`Thumbnail of ${image.key}`}
                          data-testid="preview-sample-thumbnail"
                          onError={() =>
                            setThumbnailFailures((current) => ({
                              ...current,
                              [image.key]: true,
                            }))
                          }
                          style={{
                            display: 'block',
                            width: '100%',
                            maxHeight: '110px',
                            objectFit: 'contain',
                          }}
                        />
                      )}
                      <Box
                        fontSize="body-s"
                        color="text-body-secondary"
                        data-testid="preview-sample-key"
                      >
                        {image.key}
                      </Box>
                    </SpaceBetween>
                  </div>
                );
              })}
            </div>
            {pagesCount > 1 && (
              <div data-testid="preview-sample-pagination">
                <Pagination
                  currentPageIndex={pageIndex + 1}
                  pagesCount={pagesCount}
                  onChange={({ detail }) =>
                    setPageIndex(detail.currentPageIndex - 1)
                  }
                  ariaLabels={{
                    nextPageLabel: 'Next page of dataset images',
                    previousPageLabel: 'Previous page of dataset images',
                    pageLabel: (pageNumber) =>
                      `Page ${pageNumber} of dataset images`,
                  }}
                />
              </div>
            )}
          </SpaceBetween>
        )}

        {/* --------------------------- run control ---------------------- */}
        {validationErrors.length > 0 && (
          <Alert
            type="error"
            header="The preview run was not started"
            data-testid="preview-validation-errors"
          >
            <ul>
              {validationErrors.map((violation) => (
                <li key={violation} data-testid="preview-validation-error">
                  {violation}
                </li>
              ))}
            </ul>
          </Alert>
        )}

        {runError && (
          <Alert type="error" data-testid="preview-run-error">
            {runError}
          </Alert>
        )}

        <SpaceBetween size="xs" direction="horizontal">
          <Button
            variant="primary"
            data-testid="preview-run-button"
            disabled={!canStartRun}
            loading={runInFlight}
            onClick={handleStartRun}
          >
            Run preview
          </Button>
        </SpaceBetween>

        {/* Live status so the in-progress and terminal states are announced
            (req 4.5). */}
        <div aria-live="polite" data-testid="preview-run-status">
          {runInFlight ? (
            <StatusIndicator type="in-progress">
              Preview run in progress…
            </StatusIndicator>
          ) : displayed ? (
            <StatusIndicator
              type={displayed.status === 'Failed' ? 'error' : 'success'}
            >
              {displayed.status === 'Failed'
                ? 'Preview run failed'
                : `Preview run ${displayed.status.toLowerCase()} for ${
                    displayed.results.length
                  } sample image(s)`}
            </StatusIndicator>
          ) : null}
        </div>

        {/* --------------------------- results -------------------------- */}
        {displayed && (
          <SpaceBetween size="s">
            {displayed.fewShot?.enabled && (
              <Box
                color="text-status-inactive"
                data-testid="preview-few-shot-counts"
              >
                Few-shot examples: {displayed.fewShot.attached} attached,{' '}
                {displayed.fewShot.omitted} omitted
              </Box>
            )}
            <div data-testid="preview-results">
              {displayed.results.map((result) => (
                <div
                  key={result.sampleKey}
                  data-testid="preview-result-entry"
                  data-sample-key={result.sampleKey}
                  style={{
                    borderTop: '1px solid #e9ebed',
                    padding: '8px 0',
                  }}
                >
                  <SpaceBetween size="xxs">
                    <Box
                      variant="awsui-key-label"
                      data-testid="preview-result-sample-key"
                    >
                      {result.sampleKey}
                    </Box>

                    {result.state === 'Pending' && (
                      <div data-testid="preview-result-pending">
                        <StatusIndicator type="in-progress">
                          Waiting for the model…
                        </StatusIndicator>
                      </div>
                    )}

                    {result.state === 'Failed' && (
                      <div data-testid="preview-result-failure">
                        <SpaceBetween size="xxs">
                          <span data-testid="preview-failure-category">
                            <Badge color="red">
                              {result.failureCategory
                                ? FAILURE_CATEGORY_LABELS[
                                    result.failureCategory
                                  ] ?? result.failureCategory
                                : 'Failed'}
                            </Badge>
                          </span>
                          <Box data-testid="preview-failure-reason">
                            {result.failureReason ||
                              'The model produced no usable result for this image.'}
                          </Box>
                          {result.failureCategory ===
                            'unusable_model_output' && (
                            // Native disclosure, keyboard operable, with the
                            // complete raw text and no truncation (req 9.8).
                            <details data-testid="preview-raw-output">
                              <summary>Show raw model output</summary>
                              <pre
                                data-testid="preview-raw-output-text"
                                style={{
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  margin: '4px 0 0 0',
                                }}
                              >
                                {result.payload?.raw_model_output ?? ''}
                              </pre>
                            </details>
                          )}
                        </SpaceBetween>
                      </div>
                    )}

                    {result.state === 'Succeeded' &&
                      (result.payload?.prelabel && result.imageUrl ? (
                        <PreviewResultCanvas
                          imageUrl={result.imageUrl}
                          taskType={displayed.taskType}
                          labelSet={displayed.labelSet}
                          prelabel={result.payload.prelabel}
                          imageWidth={result.payload.image_width ?? 0}
                          imageHeight={result.payload.image_height ?? 0}
                          alt={`Preview result for ${result.sampleKey}`}
                        />
                      ) : (
                        <div data-testid="preview-result-unavailable">
                          <StatusIndicator type="warning">
                            The result for this image could not be loaded
                            {result.payloadError
                              ? `: ${result.payloadError}`
                              : ''}
                          </StatusIndicator>
                        </div>
                      ))}
                  </SpaceBetween>
                </div>
              ))}
            </div>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}
