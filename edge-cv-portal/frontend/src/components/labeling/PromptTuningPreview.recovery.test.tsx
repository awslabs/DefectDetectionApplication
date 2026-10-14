/**
 * Vitest example tests for the session-recovery surface of
 * `PromptTuningPreview` (labeling-setup-session-recovery task 2.2,
 * Requirements 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 7.5).
 *
 * Covers, by example:
 * - `resumeRun` re-enters the existing poll loop once at mount with the
 *   resumed run id and never issues a start request (Req 5.1);
 * - a resumed Running run renders progressively — pending entries and the
 *   in-progress status — then commits the terminal result on a later poll,
 *   exactly as a run the component started itself (Req 5.2);
 * - a resumed run that is terminal on the first poll re-displays its
 *   results (Req 5.3);
 * - a 404 for the resumed run lands on the existing "no longer available"
 *   message with the run control re-enabled (Req 5.4);
 * - keys restored through `initialSelectedKeys` count toward the 5-key cap
 *   (a sixth add is refused with the cap message), show checked on their
 *   listing page, and ride the next run's `sample_images` (Req 5.7);
 * - the clear-selection control empties the entire Sample_Selection and
 *   reports it through `onSelectedKeysChange` (Req 5.8);
 * - rendered without any new prop, the component performs no resume poll,
 *   starts with an empty selection, and operates fully without the new
 *   callbacks (Req 7.5).
 *
 * The mock scaffolding mirrors `PromptTuningPreview.test.tsx`: `apiService`
 * mocks for `getImagePreview` / `startPreviewRun` / `getPreviewRun` and a
 * stubbed `global.fetch` for result payload URLs.
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
// Fixtures (per the PromptTuningPreview.test.tsx scaffolding)
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

/** The Preview_Run_Reference id a Setup_Draft would restore. */
const RESUMED_RUN_ID = 'run-resumed-9';

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
  window.localStorage.clear();
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
/* Preview run resumption (resumeRun)                                  */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview — preview run resumption', () => {
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

  it('polls the resumed run id on mount without issuing a start request (Req 5.1)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus('Completed', [], RESUMED_RUN_ID)
    );

    renderPreview({ resumeRun: { runId: RESUMED_RUN_ID, sampleCount: 1 } });
    await advance();

    // The existing status route was entered with the restored run id...
    expect(apiMocks.getPreviewRun).toHaveBeenCalledWith(RESUMED_RUN_ID);
    // ...and resumption never starts a new run.
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

    // Terminal on the first poll: no further status requests either.
    await advance(POLL_INTERVAL_MS * 3);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();
  });

  it('renders a resumed Running run progressively, then commits the terminal poll (Req 5.2)', async () => {
    const resolvedEntry = (index: number, key: string): Entry => ({
      index,
      sample_key: key,
      state: 'Succeeded',
      result_url: `https://payloads.example/${index}.json`,
    });
    apiMocks.getPreviewRun
      .mockResolvedValueOnce(
        runStatus(
          'Running',
          [
            { index: 0, sample_key: KEYS[0], state: 'Pending' },
            { index: 1, sample_key: KEYS[1], state: 'Pending' },
          ],
          RESUMED_RUN_ID
        )
      )
      .mockResolvedValue(
        runStatus(
          'Completed',
          [resolvedEntry(0, KEYS[0]), resolvedEntry(1, KEYS[1])],
          RESUMED_RUN_ID
        )
      );
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => ({
        sample_key: url.endsWith('0.json') ? KEYS[0] : KEYS[1],
        state: 'Succeeded',
        prelabel: { modality: 'ObjectDetection', boxes: [] },
        image_width: 100,
        image_height: 100,
      }),
    }));

    renderPreview({ resumeRun: { runId: RESUMED_RUN_ID, sampleCount: 2 } });
    await advance();

    // First poll: Running — both samples pending, the in-progress status
    // shows, and the run control is held exactly as for an own-started run.
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledWith(RESUMED_RUN_ID);
    expect(screen.getAllByTestId('preview-result-pending')).toHaveLength(2);
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run in progress'
    );
    expect(runButton()).toBeDisabled();

    await advance(POLL_INTERVAL_MS);

    // Later poll: terminal — both entries resolved, the control re-enabled.
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByTestId('preview-result-pending')
    ).not.toBeInTheDocument();
    expect(screen.getAllByTestId('preview-empty-result')).toHaveLength(2);
    expect(
      screen
        .getAllByTestId('preview-result-entry')
        .map((n) => n.getAttribute('data-sample-key'))
    ).toEqual([KEYS[0], KEYS[1]]);
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run completed for 2 sample image(s)'
    );
    expect(runButton()).toBeEnabled();
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

    // Terminal status: polling stopped.
    await advance(POLL_INTERVAL_MS * 3);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(2);
  });

  it('displays the results when the resumed run is terminal on the first poll (Req 5.3)', async () => {
    apiMocks.getPreviewRun.mockResolvedValue(
      runStatus(
        'Completed',
        [
          {
            index: 0,
            sample_key: KEYS[0],
            state: 'Succeeded',
            result_url: 'https://payloads.example/0.json',
          },
        ],
        RESUMED_RUN_ID
      )
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
            { class: 'scratch', left: 10, top: 10, width: 20, height: 20 },
          ],
        },
        image_width: 100,
        image_height: 100,
      }),
    });

    renderPreview({ resumeRun: { runId: RESUMED_RUN_ID, sampleCount: 1 } });
    await advance();

    // One poll re-displayed the completed run's results.
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(screen.getAllByTestId('preview-result-entry')).toHaveLength(1);
    expect(screen.getByTestId('preview-result-sample-key')).toHaveTextContent(
      KEYS[0]
    );
    expect(screen.getByTestId('preview-box')).toBeInTheDocument();
    expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
      'Preview run completed for 1 sample image(s)'
    );
    expect(runButton()).toBeEnabled();
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

    await advance(POLL_INTERVAL_MS * 3);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
  });

  it('routes a resumed-run 404 to the existing message and re-enables the run control (Req 5.4)', async () => {
    apiMocks.getPreviewRun.mockRejectedValue(
      new ApiError('Preview run not found', 404)
    );

    renderPreview({ resumeRun: { runId: RESUMED_RUN_ID, sampleCount: 1 } });
    await advance();

    expect(apiMocks.getPreviewRun).toHaveBeenCalledWith(RESUMED_RUN_ID);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('preview-run-error')).toHaveTextContent(
      'The preview run is no longer available. Start a new preview run.'
    );
    expect(runButton()).toBeEnabled();
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

    // The 404 ended the loop: no further status requests.
    await advance(POLL_INTERVAL_MS * 3);
    expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1);
  });
});

/* ------------------------------------------------------------------ */
/* Restored selection (initialSelectedKeys) and clear-selection        */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview — restored sample selection', () => {
  it('counts restored keys toward the cap, shows them checked, and rides them on the next run (Req 5.7)', async () => {
    const onSelectedKeysChange = vi.fn();
    renderPreview({
      initialSelectedKeys: [KEYS[0], KEYS[1]],
      onSelectedKeysChange,
    });
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    // Restored keys show as selected on their listing page.
    expect(sampleCheckbox(KEYS[0])).toBeChecked();
    expect(sampleCheckbox(KEYS[1])).toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '2 of 5 sample images selected'
    );

    // They count toward the cap: three more selections fill it...
    for (const key of KEYS.slice(2, 5)) {
      fireEvent.click(sampleCheckbox(key));
    }
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );
    expect(onSelectedKeysChange).toHaveBeenLastCalledWith([
      KEYS[0],
      KEYS[1],
      KEYS[2],
      KEYS[3],
      KEYS[4],
    ]);

    // ...and a sixth is refused with the cap message.
    fireEvent.click(sampleCheckbox(KEYS[5]));
    expect(screen.getByTestId('preview-selection-cap')).toHaveTextContent(
      'At most 5 sample images can be previewed in one run'
    );
    expect(sampleCheckbox(KEYS[5])).not.toBeChecked();
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '5 of 5 sample images selected'
    );
    // The refused add changed nothing and therefore reported nothing.
    expect(onSelectedKeysChange).toHaveBeenCalledTimes(3);

    // Restored keys ride the next run's sample_images, restored-first.
    await act(async () => {
      fireEvent.click(runButton());
    });
    await waitFor(() =>
      expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1)
    );
    expect(apiMocks.startPreviewRun.mock.calls[0][0].sample_images).toEqual([
      KEYS[0],
      KEYS[1],
      KEYS[2],
      KEYS[3],
      KEYS[4],
    ]);
  });

  it('clears the entire selection through the clear-selection control and reports it (Req 5.8)', async () => {
    const onSelectedKeysChange = vi.fn();
    renderPreview({
      initialSelectedKeys: [KEYS[0], KEYS[2]],
      onSelectedKeysChange,
    });
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '2 of 5 sample images selected'
    );

    fireEvent.click(screen.getByTestId('preview-clear-selection'));

    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '0 of 5 sample images selected'
    );
    expect(sampleCheckbox(KEYS[0])).not.toBeChecked();
    expect(sampleCheckbox(KEYS[2])).not.toBeChecked();
    // The emptied selection was reported in full.
    expect(onSelectedKeysChange).toHaveBeenCalledWith([]);
    // The control renders only while the selection is non-empty.
    expect(
      screen.queryByTestId('preview-clear-selection')
    ).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Prop absence (Req 7.5)                                              */
/* ------------------------------------------------------------------ */

describe('PromptTuningPreview — without the restore props', () => {
  it('performs no resume poll, starts empty, and operates fully without the new callbacks (Req 7.5)', async () => {
    renderPreview(); // none of the four new props

    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );

    // No resume poll at mount and no run started on its own.
    expect(apiMocks.getPreviewRun).not.toHaveBeenCalled();
    expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

    // The initial selection is empty; no clear-selection control shows.
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '0 of 5 sample images selected'
    );
    expect(
      screen.queryByTestId('preview-clear-selection')
    ).not.toBeInTheDocument();

    // Selecting, deselecting, and a full run all operate without
    // onSelectedKeysChange / onRunStarted wired.
    fireEvent.click(sampleCheckbox(KEYS[0]));
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );
    fireEvent.click(sampleCheckbox(KEYS[0]));
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '0 of 5 sample images selected'
    );

    fireEvent.click(sampleCheckbox(KEYS[1]));
    await act(async () => {
      fireEvent.click(runButton());
    });
    await waitFor(() =>
      expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1)
    );
    await waitFor(() =>
      expect(screen.getByTestId('preview-run-status')).toHaveTextContent(
        'Preview run completed'
      )
    );
    // The only status polling belongs to the run this component started.
    expect(apiMocks.getPreviewRun).toHaveBeenCalledWith('run-1');
  });
});
