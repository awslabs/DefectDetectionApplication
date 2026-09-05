/**
 * Vitest unit tests for `PromptTuningPreview` (llm-autolabel-prompt-tuning
 * task 12.11, Requirements 2.3, 2.5, 2.6, 2.8, 4.5, 9.8).
 *
 * Covers, by example:
 * - the sample picker: the JPEG/PNG-filtered paged listing, the 5-selection
 *   cap refusing a sixth image, selection retained across pages and across
 *   runs, and the object-key fallback for a thumbnail that fails to load
 *   while the image stays selectable (Req 2.3, 2.7, 2.8);
 * - distinct empty-prefix and inaccessible-prefix messages that name the
 *   prefix and disable the run control, and a refresh that re-lists and
 *   re-enables it (Req 2.5, 2.6);
 * - polling: stopping on `Completed`, on `Failed`, on a `404`, and at the
 *   `sample_count × 120 s + 60 s` bound, rendering results progressively as
 *   individual samples resolve (Req 1.8, 4.5, 4.6, 4.7);
 * - failure rendering with category and reason, and the untruncated raw
 *   model output disclosure for `unusable_model_output` (Req 9.7, 9.8).
 *
 * The universal properties of this component are covered separately by
 * `PromptTuningPreview.property.test.tsx` (Properties 14–21).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
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

const PREFIX = 'datasets/set-a/';
const KEYS = [
  'datasets/set-a/one.jpg',
  'datasets/set-a/two.jpg',
  'datasets/set-a/three.png',
  'datasets/set-a/four.jpg',
  'datasets/set-a/five.jpeg',
  'datasets/set-a/six.png',
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
  results: Entry[]
) => ({
  run_id: 'run-1',
  status,
  sample_count: results.length,
  few_shot: { enabled: false, attached: 0, omitted: 0 },
  results,
});

type PreviewProps = React.ComponentProps<typeof PromptTuningPreview>;

const defaultProps = (): PreviewProps => ({
  usecaseId: 'uc-1',
  datasetPrefix: PREFIX,
  model: 'llm:us.amazon.nova-pro-v1:0',
  detectionPrompt: 'Find scratches on the surface',
  taskType: 'ObjectDetection',
  labelSet: ['scratch'],
  fewShotEnabled: false,
  goodExampleCount: 0,
  badExampleCount: 0,
  ensureExampleImagesUploaded: vi.fn(async () => ({
    good: [] as string[],
    bad: [] as string[],
  })),
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

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getImagePreview.mockResolvedValue(listing(KEYS));
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  apiMocks.getPreviewRun.mockResolvedValue(runStatus('Completed', []));
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  vi.stubGlobal('fetch', fetchMock);
});

/* ------------------------------------------------------------------ */
/* Sample picker                                                       */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview — sample picker', () => {
  it('lists only JPEG and PNG objects under the prefix with keys and thumbnails', async () => {
    renderPreview();

    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    expect(apiMocks.getImagePreview).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prefix: PREFIX,
      limit: 50,
      offset: 0,
      extensions: 'jpg,jpeg,png',
    });
    expect(screen.getAllByTestId('preview-sample-item')).toHaveLength(6);
    expect(
      screen.getByAltText(`Thumbnail of ${KEYS[0]}`)
    ).toHaveAttribute('src', `https://s3.example/${KEYS[0]}?sig=1`);
    expect(
      screen.getAllByTestId('preview-sample-key').map((n) => n.textContent)
    ).toEqual(KEYS);
    expect(runButton()).toBeEnabled();
  });

  it('caps the selection at 5 sample images and refuses a sixth', async () => {
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
    expect(screen.queryByTestId('preview-selection-cap')).not.toBeInTheDocument();

    fireEvent.click(sampleCheckbox(KEYS[5]));

    expect(screen.getByTestId('preview-selection-cap')).toHaveTextContent(
      'At most 5 sample images can be previewed in one run'
    );
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );
    expect(sampleCheckbox(KEYS[5])).not.toBeChecked();

    // Freeing a slot clears the cap message and admits a new selection.
    fireEvent.click(sampleCheckbox(KEYS[0]));
    expect(screen.queryByTestId('preview-selection-cap')).not.toBeInTheDocument();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '4 of 5 sample images selected'
    );
    fireEvent.click(sampleCheckbox(KEYS[5]));
    expect(sampleCheckbox(KEYS[5])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );
  });

  it('retains the selection while paging back and forth', async () => {
    const pageOne = ['datasets/set-a/p1.jpg'];
    const pageTwo = ['datasets/set-a/p2.jpg'];
    apiMocks.getImagePreview.mockImplementation(
      async (params: { offset?: number }) =>
        params.offset === 0 ? listing(pageOne, 60, 0) : listing(pageTwo, 60, 50)
    );

    renderPreview();
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    fireEvent.click(sampleCheckbox(pageOne[0]));
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );

    // 60 listed images at 50 per page: the pager is present (req 2.7).
    expect(screen.getByTestId('preview-sample-pagination')).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'Next page of dataset images' })
    );
    await waitFor(() =>
      expect(document.querySelector(`[data-sample-key="${pageTwo[0]}"]`)).not.toBeNull()
    );

    fireEvent.click(sampleCheckbox(pageTwo[0]));
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '2 of 5 sample images selected'
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Previous page of dataset images' })
    );
    await waitFor(() =>
      expect(document.querySelector(`[data-sample-key="${pageOne[0]}"]`)).not.toBeNull()
    );
    // The page-one selection survived the round trip (req 5.2).
    expect(sampleCheckbox(pageOne[0])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '2 of 5 sample images selected'
    );
  });

  it('falls back to the object key when a thumbnail fails and keeps it selectable', async () => {
    renderPreview();
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    fireEvent.error(screen.getByAltText(`Thumbnail of ${KEYS[0]}`));

    const fallback = screen.getByTestId('preview-thumbnail-fallback');
    expect(fallback).toHaveTextContent(KEYS[0]);
    expect(
      screen.queryByAltText(`Thumbnail of ${KEYS[0]}`)
    ).not.toBeInTheDocument();

    fireEvent.click(sampleCheckbox(KEYS[0]));
    expect(sampleCheckbox(KEYS[0])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );
  });

  it('reports an empty prefix distinctly from an inaccessible one and disables the run', async () => {
    apiMocks.getImagePreview.mockResolvedValue(listing([], 0));
    renderPreview();

    await waitFor(() =>
      expect(screen.getByTestId('preview-prefix-empty')).toBeInTheDocument()
    );
    expect(screen.getByTestId('preview-prefix-empty')).toHaveTextContent(PREFIX);
    expect(
      screen.queryByTestId('preview-prefix-inaccessible')
    ).not.toBeInTheDocument();
    expect(runButton()).toBeDisabled();
  });

  it('reports an inaccessible prefix and disables the run', async () => {
    apiMocks.getImagePreview.mockRejectedValue(new Error('AccessDenied'));
    renderPreview();

    await waitFor(() =>
      expect(
        screen.getByTestId('preview-prefix-inaccessible')
      ).toBeInTheDocument()
    );
    const alert = screen.getByTestId('preview-prefix-inaccessible');
    expect(alert).toHaveTextContent(PREFIX);
    expect(alert).toHaveTextContent('AccessDenied');
    expect(screen.queryByTestId('preview-prefix-empty')).not.toBeInTheDocument();
    expect(runButton()).toBeDisabled();
  });

  it('re-lists on refresh and re-enables the run once the listing succeeds', async () => {
    apiMocks.getImagePreview
      .mockRejectedValueOnce(new Error('AccessDenied'))
      .mockResolvedValue(listing(KEYS));

    renderPreview();
    await waitFor(() =>
      expect(
        screen.getByTestId('preview-prefix-inaccessible')
      ).toBeInTheDocument()
    );
    expect(runButton()).toBeDisabled();

    fireEvent.click(screen.getByTestId('preview-refresh-samples'));

    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    expect(
      screen.queryByTestId('preview-prefix-inaccessible')
    ).not.toBeInTheDocument();
    expect(runButton()).toBeEnabled();
    expect(apiMocks.getImagePreview).toHaveBeenCalledTimes(2);
  });
});

/* ------------------------------------------------------------------ */
/* Polling and result rendering                                        */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview — run polling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Advance fake timers inside `act`, flushing the promises they unblock. */
  const advance = async (ms = 0) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };

  /** Render, wait for the listing, select `count` samples and start a run. */
  const startRun = async (count = 1) => {
    renderPreview();
    await advance();
    for (const key of KEYS.slice(0, count)) {
      fireEvent.click(sampleCheckbox(key));
    }
    await act(async () => {
      fireEvent.click(runButton());
    });
  };

  it('stops polling when the run completes and renders its results', async () => {
    apiMocks.startPreviewRun.mockResolvedValue({
      run_id: 'run-1',
      sample_count: 1,
      status: 'Running',
    });
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
        prelabel: {
          modality: 'ObjectDetection',
          boxes: [{ class: 'scratch', left: 10, top: 10, width: 20, height: 20 }],
        },
        image_width: 100,
        image_height: 100,
      }),
    });

    await startRun(1);

    // First poll: the sample is still pending and the run is in progress.
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('preview-result-pending')).toBeInTheDocument();
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run in progress'
    );
    expect(runButton()).toBeDisabled();

    await advance(POLL_INTERVAL_MS);

    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(2);
    expect(screen.getAllByTestId('preview-result-entry')).toHaveLength(1);
    expect(screen.getByTestId('preview-result-sample-key')).toHaveTextContent(
      KEYS[0]
    );
    expect(screen.queryByTestId('preview-result-pending')).not.toBeInTheDocument();
    expect(screen.getByTestId('preview-box')).toBeInTheDocument();
    expect(runButton()).toBeEnabled();
    // The selection is retained for the next run (req 5.2).
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );

    // Terminal status: no further status requests.
    await advance(POLL_INTERVAL_MS * 5);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(2);
  });

  it('renders results progressively as individual samples resolve', async () => {
    apiMocks.startPreviewRun.mockResolvedValue({
      run_id: 'run-1',
      sample_count: 2,
      status: 'Running',
    });
    const payloadFor = (key: string) => ({
      sample_key: key,
      state: 'Succeeded',
      prelabel: { modality: 'ObjectDetection', boxes: [] },
      image_width: 100,
      image_height: 100,
    });
    apiMocks.getPreviewRun
      .mockResolvedValueOnce(
        runStatus('Running', [
          { index: 0, sample_key: KEYS[0], state: 'Pending' },
          { index: 1, sample_key: KEYS[1], state: 'Pending' },
        ])
      )
      .mockResolvedValueOnce(
        runStatus('Running', [
          {
            index: 0,
            sample_key: KEYS[0],
            state: 'Succeeded',
            result_url: 'https://payloads.example/0.json',
          },
          { index: 1, sample_key: KEYS[1], state: 'Pending' },
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
          {
            index: 1,
            sample_key: KEYS[1],
            state: 'Succeeded',
            result_url: 'https://payloads.example/1.json',
          },
        ])
      );
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => payloadFor(url.endsWith('0.json') ? KEYS[0] : KEYS[1]),
    }));

    await startRun(2);

    expect(screen.getAllByTestId('preview-result-pending')).toHaveLength(2);

    await advance(POLL_INTERVAL_MS);
    // One sample resolved, one still pending — both entries on screen.
    expect(screen.getAllByTestId('preview-result-entry')).toHaveLength(2);
    expect(screen.getAllByTestId('preview-result-pending')).toHaveLength(1);
    expect(screen.getAllByTestId('preview-empty-result')).toHaveLength(1);

    await advance(POLL_INTERVAL_MS);
    expect(screen.queryByTestId('preview-result-pending')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('preview-empty-result')).toHaveLength(2);
    expect(
      screen
        .getAllByTestId('preview-result-entry')
        .map((n) => n.getAttribute('data-sample-key'))
    ).toEqual([KEYS[0], KEYS[1]]);
    // Each payload was fetched exactly once.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('stops polling and reports the failure when the run fails', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Failed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Failed',
          failure_category: 'model_error',
          failure_reason: 'bedrock ThrottlingException',
        },
      ])
    );

    await startRun(1);

    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'The preview run failed'
    );
    expect(screen.getByTestId('preview-failure-category')).toHaveTextContent(
      'Model error'
    );
    expect(screen.getByTestId('preview-failure-reason')).toHaveTextContent(
      'bedrock ThrottlingException'
    );
    expect(runButton()).toBeEnabled();

    await advance(POLL_INTERVAL_MS * 4);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
  });

  it('stops polling when the run is no longer available (404)', async () => {
    apiMocks.getPreviewRun.mockRejectedValue(
      new ApiError('Preview run not found', 404)
    );

    await startRun(1);

    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'no longer available'
    );
    expect(runButton()).toBeEnabled();

    await advance(POLL_INTERVAL_MS * 4);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
  });

  it('gives up at the overall run bound while the run stays Running', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Running', [{ index: 0, sample_key: KEYS[0], state: 'Pending' }])
    );

    await startRun(1);
    expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();

    // 1 sample => 1 x 120 s + 60 s = 180 s, i.e. ~90 poll intervals.
    for (let i = 0; i < 120; i++) {
      if (screen.queryByTestId('preview-run-error')) break;
      await advance(POLL_INTERVAL_MS);
    }

    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'did not return results within 180 seconds'
    );
    expect(runButton()).toBeEnabled();

    const callsAtGiveUp = apiMocks.getPreviewRun.mock.calls.length;
    await advance(POLL_INTERVAL_MS * 10);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(callsAtGiveUp);
  });

  it('discloses the complete raw model output for unusable model output', async () => {
    const rawOutput = 'Sure!\n```json\n{"detections": [ }\n```\n  trailing  ';
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [
        {
          index: 0,
          sample_key: KEYS[0],
          state: 'Failed',
          failure_category: 'unusable_model_output',
          failure_reason: 'no parseable JSON object in the model response',
          result_url: 'https://payloads.example/0.json',
        },
      ])
    );
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: KEYS[0],
        state: 'Failed',
        failure_category: 'unusable_model_output',
        failure_reason: 'no parseable JSON object in the model response',
        raw_model_output: rawOutput,
      }),
    });

    await startRun(1);

    expect(screen.getByTestId('preview-failure-category')).toHaveTextContent(
      'Unusable model output'
    );
    expect(screen.getByTestId('preview-failure-reason')).toHaveTextContent(
      'no parseable JSON object in the model response'
    );
    const disclosure = screen.getByTestId('preview-raw-output');
    expect(disclosure.tagName).toBe('DETAILS');
    expect(disclosure).toHaveTextContent('Show raw model output');
    // Character-for-character, untruncated (req 9.8).
    expect(screen.getByTestId('preview-raw-output-text').textContent).toBe(
      rawOutput
    );
  });

  it('marks a resolved sample unavailable when its payload cannot be fetched', async () => {
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
    fetchMock.mockResolvedValue({ ok: false, status: 403, json: async () => ({}) });

    await startRun(1);

    expect(screen.getByTestId('preview-result-unavailable')).toHaveTextContent(
      'HTTP 403'
    );
    expect(runButton()).toBeEnabled();
  });
});
