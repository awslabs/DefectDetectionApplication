/**
 * Vitest example tests for the grounded-sam arm of `PromptTuningPreview`
 * (grounded-sam-prompt-tuning-preview task 2.8, Requirements 1.3, 1.6,
 * 3.9, 3.10, 5.1, 5.2, 5.4, 5.5, 6.2, 7.2, 7.3).
 *
 * Covers, by example, with `model === 'grounded-sam'`:
 * - the sample picker offering the same listing/selection surface it
 *   offers `llm:` selections — paged prefix listing with thumbnails and
 *   keys, checkbox selection capped at 5 with a refused sixth (Req 1.3);
 * - the CPU timing expectation rendered for grounded-sam only, absent for
 *   an `llm:` selection (Req 1.6);
 * - polling at the family's 240 s per-sample bound: a grounded-sam run is
 *   still polled past the llm family's 180 s bound and gives up at
 *   `min(n × 240 + 60, 900) + 60` seconds, while an `llm:` run keeps the
 *   pre-feature `n × 120 + 60` bound byte-identically (Req 3.9);
 * - replacement semantics across two sequential runs: the previous run's
 *   results stay displayed until the new run's first result arrives, then
 *   are replaced wholesale (Req 3.10);
 * - result rendering fed from grounded-sam payloads: Segmentation RLE
 *   mask overlay with its class legend and Region_Scores (Req 5.1),
 *   ObjectDetection boxes with adjacent class labels and scores (Req 5.2),
 *   the explicit no-detections success state for an empty Pre_Label
 *   (Req 5.4), and per-sample failure category + reason (Req 5.5);
 * - a start rejection whose ApiError carries
 *   `details.validation_errors: ['Grounded-SAM worker is not deployed']`
 *   surfacing those messages in the existing validation-errors alert with
 *   the panel and selection left operable (Req 6.2);
 * - the tune loop ergonomics: the run control disabled with the
 *   in-progress indication while a run is in flight, re-enabled at the
 *   terminal state, and the Sample_Image selection retained so the next
 *   run re-previews the same samples (Req 7.2, 7.3).
 *
 * The universal properties of the grounded-sam arm are covered separately
 * by `PromptTuningPreview.groundedsam.property.test.tsx` (Property 2) and
 * the wizard-level `CreateLabelingJob.gsampreview.property.test.tsx`
 * (Properties 1, 3); the shipped `PromptTuningPreview.test.tsx` keeps
 * pinning the `llm:` surface byte-identically.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import PromptTuningPreview, { POLL_INTERVAL_MS } from './PromptTuningPreview';
import { ApiError } from '../../services/api';

const { apiMocks, fetchMock } = vi.hoisted(() => ({
  apiMocks: {
    getImagePreview: vi.fn(),
    startPreviewRun: vi.fn(),
    getPreviewRun: vi.fn(),
  },
  fetchMock: vi.fn(),
}));

vi.mock('../../services/api', () => {
  // Mirror of the real ApiError including `details`, which the grounded-sam
  // start-rejection path reads for `validation_errors` (Req 6.2).
  class ApiError extends Error {
    status: number;
    code?: string;
    details?: Record<string, unknown>;
    constructor(
      message: string,
      status = 0,
      code?: string,
      details?: Record<string, unknown>
    ) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PREFIX = 'training-images/';
const KEYS = [
  'training-images/one.jpg',
  'training-images/two.jpg',
  'training-images/three.png',
  'training-images/four.jpg',
  'training-images/five.jpeg',
  'training-images/six.png',
];

const listedImage = (key: string) => ({
  key,
  filename: key.slice(key.lastIndexOf('/') + 1),
  size: 2048,
  last_modified: '2024-05-01T00:00:00Z',
  presigned_url: `https://s3.example/${key}?sig=1`,
});

const listing = (keys: string[], totalFound = keys.length, offset = 0) => ({
  prefix: PREFIX,
  bucket: 'data-bucket',
  total_found: totalFound,
  offset,
  limit: 50,
  has_more: offset + keys.length < totalFound,
  images: keys.map(listedImage),
  expires_in_seconds: 900,
});

type Entry = {
  index: number;
  sample_key: string;
  state: 'Pending' | 'Succeeded' | 'Failed';
  result_url?: string;
  failure_category?: string;
  failure_reason?: string;
};

const runStatus = (
  status: 'Running' | 'Completed' | 'Failed',
  results: Entry[],
  runId = 'run-1'
) => ({
  run_id: runId,
  status,
  sample_count: results.length,
  // Grounded-sam RUN items record few_shot_enabled=false (the family has
  // no Few_Shot_Option), so the status carries the disabled shape.
  few_shot: { enabled: false, attached: 0, omitted: 0 },
  results,
});

type PreviewProps = React.ComponentProps<typeof PromptTuningPreview>;

/** Default props: a grounded-sam Segmentation selection with an override. */
const defaultProps = (): PreviewProps => ({
  usecaseId: 'uc-1',
  datasetPrefix: PREFIX,
  model: 'grounded-sam',
  // The grounded-sam family has no Detection_Prompt input.
  detectionPrompt: '',
  taskType: 'Segmentation',
  labelSet: ['cookie_gap'],
  fewShotEnabled: false,
  promptOverrides: { cookie_gap: 'gap between broken cookie pieces' },
  goodExampleCount: 0,
  badExampleCount: 0,
  ensureExampleImagesUploaded: vi.fn(async () => ({
    good: [] as string[],
    bad: [] as string[],
  })),
});

const LLM_MODEL = 'llm:us.amazon.nova-pro-v1:0';

/** Overrides that flip the panel to a pre-feature `llm:` selection. */
const llmOverrides = (): Partial<PreviewProps> => ({
  model: LLM_MODEL,
  detectionPrompt: 'Find scratches on the surface',
  taskType: 'ObjectDetection',
  labelSet: ['scratch'],
  promptOverrides: undefined,
});

/** Overrides for a grounded-sam ObjectDetection selection. */
const odOverrides = (): Partial<PreviewProps> => ({
  taskType: 'ObjectDetection',
  labelSet: ['scratch'],
  promptOverrides: { scratch: 'thin bright scratch' },
});

function renderPreview(overrides: Partial<PreviewProps> = {}) {
  return render(<PromptTuningPreview {...defaultProps()} {...overrides} />);
}

/** The native input of the checkbox belonging to one listed sample. */
function sampleCheckbox(key: string): HTMLInputElement {
  const item = document.querySelector(
    `[data-sample-key="${key}"]`
  ) as HTMLElement | null;
  if (!item) throw new Error(`No listed sample for key ${key}`);
  return createWrapper(item)
    .findCheckbox()!
    .findNativeInput()
    .getElement() as HTMLInputElement;
}

const runButton = () => screen.getByTestId('preview-run-button');

/** Records mask paints so Segmentation rendering is observable in jsdom. */
let putImageDataSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  apiMocks.getImagePreview.mockResolvedValue(listing(KEYS));
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  apiMocks.getPreviewRun.mockResolvedValue(runStatus('Completed', []));
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  vi.stubGlobal('fetch', fetchMock);

  // jsdom has no 2D canvas context: stand a minimal one in so the
  // Segmentation mask overlay paints (and the paint is observable).
  putImageDataSpy = vi.fn();
  const context2d = {
    clearRect: vi.fn(),
    createImageData: (width: number, height: number) => ({
      data: new Uint8ClampedArray(width * height * 4),
      width,
      height,
      colorSpace: 'srgb' as const,
    }),
    putImageData: putImageDataSpy,
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    ((kind: string) =>
      kind === '2d'
        ? (context2d as unknown as CanvasRenderingContext2D)
        : null) as unknown as HTMLCanvasElement['getContext']
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

/* ------------------------------------------------------------------ */
/* Sample picker under grounded-sam (Req 1.3)                          */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — sample picker (Req 1.3)', () => {
  it('offers the llm-identical paged listing with keys and thumbnails and enables the run control (Req 1.3)', async () => {
    renderPreview();

    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    // Same listing surface as the llm: family — the paged dataset-prefix
    // listing filtered to JPEG/PNG.
    expect(apiMocks.getImagePreview).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prefix: PREFIX,
      limit: 50,
      offset: 0,
      extensions: 'jpg,jpeg,png',
    });
    expect(screen.getAllByTestId('preview-sample-item')).toHaveLength(6);
    expect(screen.getByAltText(`Thumbnail of ${KEYS[0]}`)).toHaveAttribute(
      'src',
      `https://s3.example/${KEYS[0]}?sig=1`
    );
    expect(
      screen.getAllByTestId('preview-sample-key').map((n) => n.textContent)
    ).toEqual(KEYS);
    expect(runButton()).toBeEnabled();
  });

  it('selects samples and caps the selection at 5, refusing a sixth (Req 1.3)', async () => {
    renderPreview();
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    for (const key of KEYS.slice(0, 5)) {
      fireEvent.click(sampleCheckbox(key));
    }
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );

    fireEvent.click(sampleCheckbox(KEYS[5]));

    // The sixth selection is refused, exactly as under llm: (Req 1.3).
    expect(screen.getByTestId('preview-selection-cap')).toHaveTextContent(
      'At most 5 sample images can be previewed in one run'
    );
    expect(sampleCheckbox(KEYS[5])).not.toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );
  });
});

/* ------------------------------------------------------------------ */
/* Timing expectation (Req 1.6)                                        */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — timing expectation (Req 1.6)', () => {
  it('renders the CPU timing note under grounded-sam and not under an llm: model (Req 1.6)', async () => {
    const view = renderPreview();
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    const note = screen.getByTestId('preview-gsam-timing-note');
    expect(note).toHaveTextContent(
      'Inference runs on CPU at roughly 5 seconds per image once warm.'
    );
    expect(note).toHaveTextContent(
      'The first run after idle can take a few minutes while the worker starts.'
    );
    view.unmount();

    renderPreview(llmOverrides());
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    expect(
      screen.queryByTestId('preview-gsam-timing-note')
    ).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Fake-timer helpers shared by the polling suites                     */
/* ------------------------------------------------------------------ */

/** Advance fake timers inside `act`, flushing the promises they unblock. */
const advance = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

/** Render, wait for the listing, select `count` samples and start a run. */
const startRun = async (overrides: Partial<PreviewProps> = {}, count = 1) => {
  renderPreview(overrides);
  await advance();
  for (const key of KEYS.slice(0, count)) {
    fireEvent.click(sampleCheckbox(key));
  }
  await act(async () => {
    fireEvent.click(runButton());
  });
};

/* ------------------------------------------------------------------ */
/* Poll bound (Req 3.9)                                                */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — poll bound (Req 3.9)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls a grounded-sam run at 240 s per sample and gives up at min(n × 240 + 60, 900) + 60 seconds (Req 3.9)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Running', [{ index: 0, sample_key: KEYS[0], state: 'Pending' }])
    );

    await startRun({}, 1);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);

    // Past the llm family's 1 × 120 s + 60 s = 180 s bound (92 × 2 s =
    // 184 s): the grounded-sam run is still being polled, one status
    // request per interval, with the control still disabled.
    for (let i = 0; i < 92; i++) {
      await advance(POLL_INTERVAL_MS);
    }
    expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(93);
    expect(runButton()).toBeDisabled();

    // The family bound for 1 sample: min(1 × 240 + 60, 900) + 60 = 360 s.
    for (let i = 0; i < 120; i++) {
      if (screen.queryByTestId('preview-run-error')) break;
      await advance(POLL_INTERVAL_MS);
    }
    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'did not return results within 360 seconds'
    );
    expect(runButton()).toBeEnabled();

    // Past the give-up, no further status requests.
    const callsAtGiveUp = apiMocks.getPreviewRun.mock.calls.length;
    await advance(POLL_INTERVAL_MS * 10);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(callsAtGiveUp);
  });

  it('keeps the llm: bound unchanged at sample_count × 120 + 60 seconds (Req 3.9)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Running', [{ index: 0, sample_key: KEYS[0], state: 'Pending' }])
    );

    await startRun(llmOverrides(), 1);

    // 1 sample => 1 × 120 s + 60 s = 180 s — the pre-feature expression.
    for (let i = 0; i < 120; i++) {
      if (screen.queryByTestId('preview-run-error')) break;
      await advance(POLL_INTERVAL_MS);
    }
    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'did not return results within 180 seconds'
    );
  });
});

/* ------------------------------------------------------------------ */
/* Replacement semantics (Req 3.10)                                    */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — replacement semantics (Req 3.10)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the first run's results until the second run's first result arrives, then replaces them wholesale (Req 3.10)", async () => {
    apiMocks.startPreviewRun
      .mockResolvedValueOnce({ run_id: 'run-1', sample_count: 1, status: 'Running' })
      .mockResolvedValueOnce({ run_id: 'run-2', sample_count: 1, status: 'Running' });
    apiMocks.getPreviewRun
      // Run 1, first poll: completed with one box.
      .mockResolvedValueOnce(
        runStatus(
          'Completed',
          [
            {
              index: 0,
              sample_key: KEYS[0],
              state: 'Succeeded',
              result_url: 'https://payloads.example/r1-0.json',
            },
          ],
          'run-1'
        )
      )
      // Run 2, first poll: nothing resolved yet.
      .mockResolvedValueOnce(
        runStatus(
          'Running',
          [{ index: 0, sample_key: KEYS[0], state: 'Pending' }],
          'run-2'
        )
      )
      // Run 2, second poll: resolved with an empty Pre_Label.
      .mockResolvedValue(
        runStatus(
          'Completed',
          [
            {
              index: 0,
              sample_key: KEYS[0],
              state: 'Succeeded',
              result_url: 'https://payloads.example/r2-0.json',
            },
          ],
          'run-2'
        )
      );
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        url.includes('r1-0')
          ? {
              sample_key: KEYS[0],
              state: 'Succeeded',
              prelabel: {
                modality: 'ObjectDetection',
                boxes: [
                  { class: 'scratch', left: 10, top: 10, width: 20, height: 20 },
                ],
              },
              image_width: 100,
              image_height: 100,
            }
          : {
              sample_key: KEYS[0],
              state: 'Succeeded',
              prelabel: { modality: 'ObjectDetection', boxes: [] },
              image_width: 100,
              image_height: 100,
            },
    }));

    await startRun(odOverrides(), 1);

    // Run 1's result set is displayed: one entry carrying one box.
    expect(screen.getByTestId('preview-box')).toBeInTheDocument();
    expect(screen.queryByTestId('preview-empty-result')).not.toBeInTheDocument();
    expect(runButton()).toBeEnabled();

    // Second run over the retained selection.
    await act(async () => {
      fireEvent.click(runButton());
    });

    // The new run has produced no result yet: run 1's results stay
    // displayed unchanged (the existing replacement semantics).
    expect(screen.getByTestId('preview-box')).toBeInTheDocument();
    expect(screen.getAllByTestId('preview-result-entry')).toHaveLength(1);

    await advance(POLL_INTERVAL_MS);

    // Run 2's first result replaces the previous set wholesale: the box
    // is gone, the empty result stands in its place.
    expect(screen.queryByTestId('preview-box')).not.toBeInTheDocument();
    expect(screen.getByTestId('preview-empty-result')).toBeInTheDocument();
    const entries = screen.getAllByTestId('preview-result-entry');
    expect(entries).toHaveLength(1);
    expect(entries[0].getAttribute('data-sample-key')).toBe(KEYS[0]);
  });
});

/* ------------------------------------------------------------------ */
/* Result rendering from grounded-sam payloads (Req 5.1, 5.2, 5.4, 5.5)*/
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — result rendering (Req 5.1, 5.2, 5.4, 5.5)', () => {
  /** Render, wait for the listing, select `count` samples, run (real timers). */
  const startRunReal = async (
    overrides: Partial<PreviewProps> = {},
    count = 1
  ) => {
    renderPreview(overrides);
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    for (const key of KEYS.slice(0, count)) {
      fireEvent.click(sampleCheckbox(key));
    }
    await act(async () => {
      fireEvent.click(runButton());
    });
  };

  it('renders Segmentation regions as a mask overlay with a class legend and region scores (Req 5.1)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Succeeded',
          result_url: 'https://payloads.example/0.json',
        },
      ])
    );
    // The worker's Segmentation shape: regions with class, RLE and an
    // optional Region_Score.
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: KEYS[0],
        state: 'Succeeded',
        prelabel: {
          modality: 'Segmentation',
          regions: [
            { class: 'cookie_gap', rle: '1 1 1 1', score: 0.87 },
            { class: 'crumb', rle: '0 1 3' },
          ],
        },
        image_width: 2,
        image_height: 2,
      }),
    });

    await startRunReal(
      {
        labelSet: ['cookie_gap', 'crumb'],
        promptOverrides: { cookie_gap: 'gap between broken cookie pieces' },
      },
      1
    );

    // The RLE masks were decoded and painted on the overlay canvas through
    // the existing client-side path (Req 5.1).
    expect(screen.getByTestId('preview-mask-overlay')).toBeInTheDocument();
    expect(putImageDataSpy).toHaveBeenCalled();

    // The legend associates each region's class name; the region carrying
    // a Region_Score shows it beside the class name.
    expect(screen.getByTestId('preview-region-legend')).toBeInTheDocument();
    expect(
      screen.getAllByTestId('preview-region-class').map((n) => n.textContent)
    ).toEqual(['cookie_gap (0.87)', 'crumb']);
    expect(
      screen.getAllByTestId('preview-region-score').map((n) => n.textContent)
    ).toEqual([' (0.87)']);
    expect(screen.queryByTestId('preview-empty-result')).not.toBeInTheDocument();
  });

  it('renders ObjectDetection boxes proportionally with adjacent class labels and box scores (Req 5.2)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Succeeded',
          result_url: 'https://payloads.example/0.json',
        },
      ])
    );
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: KEYS[0],
        state: 'Succeeded',
        prelabel: {
          modality: 'ObjectDetection',
          boxes: [
            {
              class: 'scratch',
              left: 25,
              top: 25,
              width: 50,
              height: 25,
              score: 0.42,
            },
            { class: 'scratch', left: 0, top: 0, width: 10, height: 10 },
          ],
        },
        image_width: 100,
        image_height: 100,
      }),
    });

    await startRunReal(odOverrides(), 1);

    const boxes = screen.getAllByTestId('preview-box');
    expect(boxes).toHaveLength(2);
    // Proportionally positioned over the sample image (Req 5.2).
    expect(boxes[0].style.left).toBe('25%');
    expect(boxes[0].style.top).toBe('25%');
    expect(boxes[0].style.width).toBe('50%');
    expect(boxes[0].style.height).toBe('25%');

    // Class names adjacent; the box carrying a Region_Score shows it.
    expect(
      screen.getAllByTestId('preview-box-class').map((n) => n.textContent)
    ).toEqual(['scratch (0.42)', 'scratch']);
    expect(
      screen.getAllByTestId('preview-box-score').map((n) => n.textContent)
    ).toEqual([' (0.42)']);
  });

  it('renders the explicit no-detections success state for an empty grounded-sam result (Req 5.4)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Succeeded',
          result_url: 'https://payloads.example/0.json',
        },
      ])
    );
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: KEYS[0],
        state: 'Succeeded',
        prelabel: { modality: 'Segmentation', regions: [] },
        image_width: 100,
        image_height: 100,
      }),
    });

    await startRunReal({}, 1);

    // A success state, visually distinct from a failure (Req 5.4).
    expect(screen.getByTestId('preview-empty-result')).toHaveTextContent(
      'No detections'
    );
    expect(
      screen.queryByTestId('preview-result-failure')
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run completed for 1 sample image(s)'
    );
  });

  it('renders the failure category and reason beside each failed sample (Req 5.5)', async () => {
    apiMocks.startPreviewRun.mockResolvedValue({
      run_id: 'run-1',
      sample_count: 2,
      status: 'Running',
    });
    // The executor's grounded-sam categories: a deadline-guard timeout and
    // an invalid-response model_error, reasons carried verbatim.
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Failed',
          failure_category: 'timeout',
          failure_reason:
            'the preview run deadline was reached before this sample could be invoked',
        },
        {
          index: 1,
          sample_key: KEYS[1],
          state: 'Failed',
          failure_category: 'model_error',
          failure_reason:
            "the worker returned a region whose class 'blob' is not in the label set",
        },
      ])
    );

    await startRunReal({}, 2);

    expect(
      screen.getAllByTestId('preview-failure-category').map((n) => n.textContent)
    ).toEqual(['Timeout', 'Model error']);
    const reasons = screen
      .getAllByTestId('preview-failure-reason')
      .map((n) => n.textContent);
    expect(reasons[0]).toContain(
      'the preview run deadline was reached before this sample could be invoked'
    );
    expect(reasons[1]).toContain(
      "the worker returned a region whose class 'blob' is not in the label set"
    );
    // The run itself completed; the per-sample failures are not a run error.
    expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Worker not deployed (Req 6.2)                                       */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — worker not deployed (Req 6.2)', () => {
  it("surfaces a start rejection's validation_errors in the validation alert and keeps the panel operable (Req 6.2)", async () => {
    apiMocks.startPreviewRun.mockRejectedValueOnce(
      new ApiError('Validation failed', 400, 'VALIDATION_ERROR', {
        validation_errors: ['Grounded-SAM worker is not deployed'],
      })
    );

    renderPreview();
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    fireEvent.click(sampleCheckbox(KEYS[0]));

    await act(async () => {
      fireEvent.click(runButton());
    });

    // The Not_Deployed_Message lands in the existing validation-errors
    // alert, not the generic run-error path (Req 6.2).
    const alert = screen.getByTestId('preview-validation-errors');
    expect(
      within(alert)
        .getAllByTestId('preview-validation-error')
        .map((n) => n.textContent)
    ).toEqual(['Grounded-SAM worker is not deployed']);
    expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();

    // The panel and the sample selection stay intact and operable.
    expect(runButton()).toBeEnabled();
    expect(sampleCheckbox(KEYS[0])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );

    // A subsequent attempt goes through (the default start mock resolves)
    // and clears the rejection's messages.
    await act(async () => {
      fireEvent.click(runButton());
    });
    expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByTestId('preview-validation-errors')
    ).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Tune loop ergonomics (Req 7.2, 7.3)                                 */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview grounded-sam — tune loop (Req 7.2, 7.3)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('disables the run control in flight, re-enables at the terminal state and retains the selection across runs (Req 7.2, 7.3)', async () => {
    apiMocks.getPreviewRun
      .mockResolvedValueOnce(
        runStatus('Running', [
          { index: 0, sample_key: KEYS[0], state: 'Pending' },
        ])
      )
      .mockResolvedValue(
        runStatus('Completed', [
          {
            index: 0,
            sample_key: KEYS[0],
            state: 'Succeeded',
            result_url: 'https://payloads.example/0.json',
          },
        ])
      );
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: KEYS[0],
        state: 'Succeeded',
        prelabel: { modality: 'Segmentation', regions: [] },
        image_width: 2,
        image_height: 2,
      }),
    });

    await startRun({}, 1);

    // In flight: the run control is disabled with the in-progress
    // indication (Req 7.2).
    expect(runButton()).toBeDisabled();
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run in progress'
    );

    await advance(POLL_INTERVAL_MS);

    // Terminal state: the control is re-enabled (Req 7.2) and the
    // Sample_Image selection is retained (Req 7.3).
    expect(runButton()).toBeEnabled();
    expect(sampleCheckbox(KEYS[0])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );

    // The next run re-previews the same samples without re-selection
    // (Req 7.3).
    await act(async () => {
      fireEvent.click(runButton());
    });
    expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(2);
    expect(apiMocks.startPreviewRun.mock.calls[1][0].sample_images).toEqual([
      KEYS[0],
    ]);
    expect(apiMocks.startPreviewRun.mock.calls[1][0].sample_images).toEqual(
      apiMocks.startPreviewRun.mock.calls[0][0].sample_images
    );
  });
});
