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
 *
 * The sizing controls (llm-model-token-and-image-sizing Requirements 3.1,
 * 3.3, 3.4, 3.11, 5.1-5.4, 5.6, 5.11, 9.8) sit with the run control: a
 * Downscale_Setting select of exactly seven options (Downscale_Off plus each
 * Max_Image_Edge) and a Token_Budget_Selection entry showing its accepted
 * range, both offered for the `llm:` family only and both live input to the
 * *next* run, so changing either after a run keeps the sample selection and
 * every other value with no extra mechanism. Each resolved result carries its
 * Source → Sent dimensions and their ratio, and a failed result additionally
 * names the run's applied Downscale_Setting and Effective_Token_Budget.
 *
 * `PreviewResultCanvas` is deliberately untouched: it positions geometry as
 * percentages of `payload.image_width` / `image_height`, which remain the
 * Source_Dimensions — the space the backend scales the Pre_Label back into —
 * so the new `sent_*` fields are display-only and are never passed to it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Pagination from '@cloudscape-design/components/pagination';
import Select from '@cloudscape-design/components/select';
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

/**
 * Max_Image_Edge_Options — the six selectable Max_Image_Edge values, which
 * with Downscale_Off make up the seven options the Downscale_Setting control
 * offers and the only values it accepts (llm-model-token-and-image-sizing
 * Requirement 5.1).
 */
export const MAX_IMAGE_EDGE_OPTIONS = [512, 768, 1024, 1280, 1536, 2048];
/** Token_Budget_Selection bounds, mirroring the API rule (Req 3.1, 3.5). */
export const TOKEN_BUDGET_MIN = 1;
export const TOKEN_BUDGET_CEILING = 128000;
/** The accepted range, shown beside the control and in the violation (3.1, 3.3). */
export const TOKEN_BUDGET_RANGE_TEXT = `${TOKEN_BUDGET_MIN} to ${TOKEN_BUDGET_CEILING}`;

/**
 * The Token_Budget_Selection an entered value carries, or `null` when the
 * entry is not a whole number in `[1, 128000]`. An empty entry is `undefined`
 * — omitted from the request so the budget resolves from the Model_Token_Limits
 * and the default (Requirement 3.10).
 */
export function parseTokenBudget(
  entered: string | undefined
): number | null | undefined {
  const trimmed = (entered ?? '').trim();
  if (!trimmed) return undefined;
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= TOKEN_BUDGET_MIN && value <= TOKEN_BUDGET_CEILING
    ? value
    : null;
}

/**
 * The ratio of the longer Sent edge to the longer Source edge as a whole
 * percent within 1 to 100 inclusive (Requirement 5.4).
 */
export function sizingRatioPercent(
  sourceWidth: number,
  sourceHeight: number,
  sentWidth: number,
  sentHeight: number
): number {
  const sourceLong = Math.max(sourceWidth, sourceHeight);
  const sentLong = Math.max(sentWidth, sentHeight);
  return Math.min(100, Math.max(1, Math.round((sentLong / sourceLong) * 100)));
}

/** A determined pixel extent: a finite positive number. */
function isExtent(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/** Text shown when a dimension pair could not be determined (Req 5.11). */
export const DIMENSIONS_UNAVAILABLE_TEXT = 'dimensions unavailable';

/** The applied Downscale_Setting in words; absent and `null` are off. */
export function downscaleSettingText(value?: number | null): string {
  return typeof value === 'number' && MAX_IMAGE_EDGE_OPTIONS.includes(value)
    ? `${value} pixels`
    : 'off';
}

/**
 * The per-sample sizing row — `1920 × 1080 → 1024 × 576 (53%)` — or the
 * unavailable indication when either dimension pair is missing, in which case
 * the rest of the result still renders (Requirements 5.4, 5.11).
 */
export function previewSizingText(payload?: PreviewResultPayload): string {
  const sourceWidth = payload?.source_width;
  const sourceHeight = payload?.source_height;
  const sentWidth = payload?.sent_width;
  const sentHeight = payload?.sent_height;
  if (
    !isExtent(sourceWidth) ||
    !isExtent(sourceHeight) ||
    !isExtent(sentWidth) ||
    !isExtent(sentHeight)
  ) {
    return DIMENSIONS_UNAVAILABLE_TEXT;
  }
  const percent = sizingRatioPercent(
    sourceWidth,
    sourceHeight,
    sentWidth,
    sentHeight
  );
  return `${sourceWidth} × ${sourceHeight} → ${sentWidth} × ${sentHeight} (${percent}%)`;
}

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
  /** Token_Budget_Selection as entered; absent or empty means omitted. */
  tokenBudget?: string;
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

  // A non-empty Token_Budget_Selection that is not a whole number in the
  // accepted range is one more violation in this same list, so the run is
  // never started, no request is issued and no wizard state changes
  // (llm-model-token-and-image-sizing Requirement 3.3).
  if (parseTokenBudget(input.tokenBudget) === null) {
    violations.push(
      `The output token budget must be a whole number from ${TOKEN_BUDGET_RANGE_TEXT}`
    );
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
  /** The run's applied Downscale_Setting; `null`/absent is Downscale_Off. */
  downscaleMaxEdge?: number | null;
  /** The run's Effective_Token_Budget, shown beside a failure (Req 9.8). */
  tokenBudget?: number;
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
  /**
   * Downscale_Setting as it stands in the wizard: `null` for Downscale_Off,
   * otherwise one Max_Image_Edge value (llm-model-token-and-image-sizing
   * Requirement 5.1). Absent behaves as Downscale_Off.
   */
  downscaleMaxEdge?: number | null;
  /**
   * Token_Budget_Selection as it stands in the wizard, as entered. Empty
   * omits the value from the run request (Requirement 3.10).
   */
  tokenBudget?: string;
  /**
   * Notified when the Job_Creator changes the Downscale_Setting, so the
   * wizard's value follows the control and is submitted with the job
   * (Requirement 5.7). The control still operates without it.
   */
  onDownscaleMaxEdgeChange?: (value: number | null) => void;
  /**
   * Notified when the Job_Creator changes the Token_Budget_Selection, so the
   * wizard's value follows the control and is submitted with the job
   * (Requirement 3.6).
   */
  onTokenBudgetChange?: (value: string) => void;
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
  downscaleMaxEdge,
  tokenBudget,
  onDownscaleMaxEdgeChange,
  onTokenBudgetChange,
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

  /* ------------------------- sizing controls ---------------------------- */
  /**
   * The Downscale_Setting and Token_Budget_Selection the *next* run will
   * carry. The wizard stays the source of truth — every change is reported
   * through the change callbacks and a replaced prop value (the model
   * compatibility effect replacing the shown budget, Requirement 3.2) is
   * followed here — so the controls work whether or not the wizard is wired.
   * Both are live input to the next run, so changing either after a completed
   * run keeps the sample selection and every other value and leaves the run
   * control enabled (Requirements 3.11, 5.6) with no further mechanism.
   */
  const [downscaleSetting, setDownscaleSetting] = useState<number | null>(
    downscaleMaxEdge ?? null
  );
  const [budgetEntry, setBudgetEntry] = useState<string>(tokenBudget ?? '');

  useEffect(() => {
    setDownscaleSetting(downscaleMaxEdge ?? null);
  }, [downscaleMaxEdge]);

  useEffect(() => {
    setBudgetEntry(tokenBudget ?? '');
  }, [tokenBudget]);

  /**
   * The same gate the wizard's Few_Shot_Option toggle uses
   * (`autoLabelEnabled && isLlmAutoLabelModel`): the component is rendered
   * only while auto-labeling is enabled, so the model prop carries the rest.
   * `sam`, `bedrock:` and no model render neither control and send neither
   * value (Requirement 5.2).
   */
  const isLlmAutoLabelModel =
    model.startsWith('llm:') && model.length > 'llm:'.length;

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
          downscaleMaxEdge: status.downscale_max_edge,
          tokenBudget: status.token_budget,
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
      tokenBudget: isLlmAutoLabelModel ? budgetEntry : '',
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
    // Both sizing values ride the request for the `llm:` family only, and the
    // budget only when the control holds a value (Requirements 3.4, 5.2, 5.3,
    // 3.10). The validation above has already established the entry is a whole
    // number in range, so this parse cannot be `null` here.
    const budgetSelection = isLlmAutoLabelModel
      ? parseTokenBudget(budgetEntry)
      : undefined;
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
        ...(isLlmAutoLabelModel
          ? { downscale_max_edge: downscaleSetting }
          : {}),
        ...(typeof budgetSelection === 'number'
          ? { token_budget: budgetSelection }
          : {}),
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
    isLlmAutoLabelModel,
    downscaleSetting,
    budgetEntry,
  ]);

  const selectionSummary = useMemo(
    () => `${selectedKeys.length} of ${SAMPLE_LIMIT} sample images selected`,
    [selectedKeys.length]
  );

  /* ----------------------- sizing control wiring ------------------------ */
  /** Exactly seven options: Downscale_Off plus each Max_Image_Edge (5.1). */
  const downscaleOptions = useMemo(
    () => [
      { value: '', label: 'Downscale off (send the original image)' },
      ...MAX_IMAGE_EDGE_OPTIONS.map((edge) => ({
        value: String(edge),
        label: `${edge} pixels`,
      })),
    ],
    []
  );

  const selectedDownscaleOption = useMemo(() => {
    const value = downscaleSetting === null ? '' : String(downscaleSetting);
    return (
      downscaleOptions.find((option) => option.value === value) ??
      downscaleOptions[0]
    );
  }, [downscaleOptions, downscaleSetting]);

  const handleDownscaleChange = useCallback(
    (raw: string | undefined) => {
      // No value outside the seven options is accepted: anything else is
      // Downscale_Off (Requirement 5.1).
      const parsed = Number(raw);
      const next = MAX_IMAGE_EDGE_OPTIONS.includes(parsed) ? parsed : null;
      setDownscaleSetting(next);
      onDownscaleMaxEdgeChange?.(next);
    },
    [onDownscaleMaxEdgeChange]
  );

  const handleBudgetChange = useCallback(
    (value: string) => {
      setBudgetEntry(value);
      onTokenBudgetChange?.(value);
    },
    [onTokenBudgetChange]
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

        {/* ------------------------ sizing controls --------------------- */}
        {isLlmAutoLabelModel && (
          <div data-testid="preview-sizing-controls">
            <SpaceBetween size="xs">
              <FormField
                label="Image downscaling"
                description="Resize each image so its longer edge is at most this many pixels before it is sent to the model. Applies to the dataset image and every attached example image."
              >
                <div data-testid="preview-downscale-select">
                  <Select
                    selectedOption={selectedDownscaleOption}
                    options={downscaleOptions}
                    onChange={({ detail }) =>
                      handleDownscaleChange(detail.selectedOption.value)
                    }
                    ariaLabel="Image downscaling"
                    selectedAriaLabel="Selected"
                  />
                </div>
              </FormField>

              <FormField
                label="Output token budget"
                description="The maximum output tokens each model request may use. Leave empty to use the configured limit for this model."
                constraintText={`Whole number from ${TOKEN_BUDGET_RANGE_TEXT}`}
              >
                <div data-testid="preview-token-budget-input">
                  <Input
                    value={budgetEntry}
                    type="number"
                    inputMode="numeric"
                    placeholder={`${TOKEN_BUDGET_MIN} to ${TOKEN_BUDGET_CEILING}`}
                    ariaLabel="Output token budget"
                    onChange={({ detail }) => handleBudgetChange(detail.value)}
                  />
                </div>
              </FormField>
            </SpaceBetween>
          </div>
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

                    {/* Source → Sent dimensions and the ratio, or the
                        unavailable indication with the rest of the result
                        still rendered (req 5.4, 5.11). */}
                    {result.state !== 'Pending' && (
                      <Box
                        fontSize="body-s"
                        color="text-body-secondary"
                        data-testid="preview-result-sizing"
                      >
                        {previewSizingText(result.payload)}
                      </Box>
                    )}

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
                          {/* The run's applied sizing inputs beside the
                              category and reason (req 9.8). */}
                          <Box
                            fontSize="body-s"
                            color="text-body-secondary"
                            data-testid="preview-failure-downscale"
                          >
                            Image downscaling:{' '}
                            {downscaleSettingText(displayed.downscaleMaxEdge)}
                          </Box>
                          <Box
                            fontSize="body-s"
                            color="text-body-secondary"
                            data-testid="preview-failure-token-budget"
                          >
                            Output token budget:{' '}
                            {typeof displayed.tokenBudget === 'number'
                              ? displayed.tokenBudget
                              : 'not reported'}
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
