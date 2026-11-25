/**
 * rfdetr-seg-support task 11.4 (Requirements 11.1, 11.5; design §11 "Two
 * requests from the Portal"): the Results tab's per-capture detail request.
 *
 * The page load is unchanged (`limit: 50`, no `include_masks`). Selecting a
 * `'Detection'` capture with `has_masks` and a `jsonl_key` issues exactly one
 * detail `getCaptures` call with that `key`, `include_masks: true` and the
 * prefix the page was LOADED with (`response.prefix`, not the editable
 * input); re-selecting it issues none (memoised for the page); a capture
 * without `has_masks` issues none; a degraded detail (empty `detections`) and
 * a rejected promise both leave the page rendered and the capture's masks
 * `'unavailable'`; a new page load forgets the previous page's details.
 * `apiService` is mocked; the real `ResultsViewer` renders underneath.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import ResultsTab from './ResultsTab';
import { MASKS_UNAVAILABLE_TEXT, type Capture } from './ResultsViewer';

const { getCaptures } = vi.hoisted(() => ({ getCaptures: vi.fn() }));
vi.mock('../services/api', () => ({ apiService: { getCaptures } }));

const DEVICE_ID = 'thing-1';
const USECASE_ID = 'uc-1';
const PAGE_PREFIX = `${DEVICE_ID}/captures`;
const EXPIRES = 1800;

const INSTANCE_CAPTURE: Capture = {
  capture_id: 'cap-inst',
  inference_result_type: 'Detection',
  detection_count: 1,
  detections: [
    { class_index: '0', class_label: 'scratch', bounding_box: [1, 1, 3, 3], confidence: 0.9, mask_area: 12 },
  ],
  source_url: 'https://example.com/cap-inst.jpg',
  overlay_url: 'https://example.com/cap-inst.overlay.jpg',
  mask_url: 'https://example.com/cap-inst.mask.png',
  detections_truncated: false,
  has_masks: true,
  jsonl_key: `${PAGE_PREFIX}/cap-inst.jsonl`,
};
const DETECTION_CAPTURE: Capture = {
  capture_id: 'cap-det',
  inference_result_type: 'Detection',
  detection_count: 1,
  detections: [{ class_index: '17', class_label: 'dog', bounding_box: [1, 2, 3, 4], confidence: 0.8 }],
  source_url: 'https://example.com/cap-det.jpg',
  overlay_url: null,
  mask_url: null,
  detections_truncated: false,
  has_masks: false,
  jsonl_key: `${PAGE_PREFIX}/cap-det.jsonl`,
};
const SECOND_INSTANCE: Capture = {
  ...INSTANCE_CAPTURE,
  capture_id: 'cap-inst-2',
  jsonl_key: `${PAGE_PREFIX}/cap-inst-2.jsonl`,
};

function pageResponse(captures: Capture[]) {
  return {
    captures,
    bucket: 'dda-inference-results-123456789012',
    prefix: PAGE_PREFIX,
    total_found: captures.length,
    expires_in_seconds: EXPIRES,
  };
}

function detailResponse(capture: Capture, detections = capture.detections) {
  return {
    captures: [
      {
        ...capture,
        detections: detections.map((det) => ({ ...det, mask_rle: '0 5 2 2 2 5' })),
        source_url: null,
        overlay_url: null,
        mask_url: null,
      },
    ],
    bucket: 'dda-inference-results-123456789012',
    prefix: PAGE_PREFIX,
    total_found: 1,
    expires_in_seconds: EXPIRES,
  };
}

const PAGE_CALL = {
  usecase_id: USECASE_ID,
  prefix: DEVICE_ID,
  device_id: DEVICE_ID,
  limit: 50,
};

function detailCall(capture: Capture) {
  return {
    usecase_id: USECASE_ID,
    prefix: PAGE_PREFIX,
    device_id: DEVICE_ID,
    key: capture.jsonl_key,
    include_masks: true,
  };
}

function detailCalls() {
  return getCaptures.mock.calls.filter(([params]) => 'key' in params);
}

async function loadPage() {
  fireEvent.click(screen.getByRole('button', { name: /load captures/i }));
  await waitFor(() => expect(createWrapper(document.body).findTable()).not.toBeNull());
}

function selectRow(index: number) {
  act(() => {
    createWrapper(document.body).findTable()!.findRowSelectionArea(index)!.click();
  });
}

beforeEach(() => {
  getCaptures.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ResultsTab detail request', () => {
  it('loads the page unchanged and issues exactly one detail request for the selected instance capture', async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) =>
      params.key ? detailResponse(INSTANCE_CAPTURE) : pageResponse([INSTANCE_CAPTURE, DETECTION_CAPTURE])
    );
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);

    await loadPage();

    expect(getCaptures.mock.calls[0][0]).toEqual(PAGE_CALL);
    await waitFor(() => expect(detailCalls()).toHaveLength(1));
    expect(detailCalls()[0][0]).toEqual(detailCall(INSTANCE_CAPTURE));
    // The viewer got the detail: the three-way toggle is up and no warning.
    await waitFor(() => expect(createWrapper(document.body).findSegmentedControl()).not.toBeNull());
    expect(screen.queryByText(MASKS_UNAVAILABLE_TEXT)).not.toBeInTheDocument();
    expect(getCaptures).toHaveBeenCalledTimes(2);
  });

  it('sends the prefix the page was loaded with, not the edited input', async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) =>
      params.key ? detailResponse(INSTANCE_CAPTURE) : pageResponse([DETECTION_CAPTURE, INSTANCE_CAPTURE])
    );
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();
    expect(detailCalls()).toHaveLength(0); // the auto-selected first row has no masks

    // Edit the prefix input after the load, then select the instance capture.
    const input = createWrapper(document.body).findInput()!;
    act(() => {
      input.setInputValue('somewhere/else');
    });
    selectRow(2);

    await waitFor(() => expect(detailCalls()).toHaveLength(1));
    expect(detailCalls()[0][0]).toEqual(detailCall(INSTANCE_CAPTURE));
    expect(detailCalls()[0][0].prefix).toBe(PAGE_PREFIX);
  });

  it('issues no detail request for a capture without has_masks and none on re-selection', async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) =>
      params.key ? detailResponse(INSTANCE_CAPTURE) : pageResponse([INSTANCE_CAPTURE, DETECTION_CAPTURE])
    );
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();
    await waitFor(() => expect(detailCalls()).toHaveLength(1));

    selectRow(2); // the detection-only capture
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /show server overlay/i })).toBeInTheDocument()
    );
    expect(detailCalls()).toHaveLength(1);

    selectRow(1); // back to the instance capture: memoised, no second request
    await waitFor(() => expect(createWrapper(document.body).findSegmentedControl()).not.toBeNull());
    expect(detailCalls()).toHaveLength(1);
    expect(screen.queryByText(MASKS_UNAVAILABLE_TEXT)).not.toBeInTheDocument();
  });

  it('requests each selected instance capture once', async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) => {
      if (!params.key) return pageResponse([INSTANCE_CAPTURE, SECOND_INSTANCE]);
      return detailResponse(params.key === SECOND_INSTANCE.jsonl_key ? SECOND_INSTANCE : INSTANCE_CAPTURE);
    });
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();
    await waitFor(() => expect(detailCalls()).toHaveLength(1));

    selectRow(2);
    await waitFor(() => expect(detailCalls()).toHaveLength(2));
    expect(detailCalls()[1][0]).toEqual(detailCall(SECOND_INSTANCE));

    selectRow(1);
    selectRow(2);
    await waitFor(() => expect(createWrapper(document.body).findSegmentedControl()).not.toBeNull());
    expect(detailCalls()).toHaveLength(2);
  });

  it("a degraded detail (empty detections) yields 'unavailable' with the page still rendered", async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) =>
      params.key ? detailResponse(INSTANCE_CAPTURE, []) : pageResponse([INSTANCE_CAPTURE])
    );
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();

    await waitFor(() => expect(screen.getByText(MASKS_UNAVAILABLE_TEXT)).toBeInTheDocument());
    expect(createWrapper(document.body).findTable()).not.toBeNull();
    expect(createWrapper(document.body).findAlert()).toBeNull(); // no page-level error
    expect(detailCalls()).toHaveLength(1);
  });

  it("a rejected detail promise yields 'unavailable' and never touches the page error", async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) => {
      if (params.key) throw new Error('key must be a .jsonl object under the requested prefix');
      return pageResponse([INSTANCE_CAPTURE, DETECTION_CAPTURE]);
    });
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();

    await waitFor(() => expect(screen.getByText(MASKS_UNAVAILABLE_TEXT)).toBeInTheDocument());
    expect(createWrapper(document.body).findAlert()).toBeNull();
    expect(createWrapper(document.body).findTable()!.findRows()).toHaveLength(2);
    expect(screen.getByText(`Capture ${INSTANCE_CAPTURE.capture_id}`)).toBeInTheDocument();
    // The failure is remembered for this page: re-selecting does not retry.
    selectRow(2);
    selectRow(1);
    await waitFor(() => expect(screen.getByText(MASKS_UNAVAILABLE_TEXT)).toBeInTheDocument());
    expect(detailCalls()).toHaveLength(1);
  });

  it('a new page load forgets the previous details and requests again', async () => {
    getCaptures.mockImplementation(async (params: { key?: string }) =>
      params.key ? detailResponse(INSTANCE_CAPTURE) : pageResponse([INSTANCE_CAPTURE])
    );
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await loadPage();
    await waitFor(() => expect(detailCalls()).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: /load captures/i }));
    await waitFor(() => expect(detailCalls()).toHaveLength(2));
    expect(getCaptures.mock.calls.filter(([params]) => !('key' in params))).toHaveLength(2);
  });

  it('shows the page-level error when the page load itself fails', async () => {
    getCaptures.mockRejectedValue(new Error('Insufficient permissions'));
    render(<ResultsTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);

    fireEvent.click(screen.getByRole('button', { name: /load captures/i }));

    await waitFor(() => expect(screen.getByText('Insufficient permissions')).toBeInTheDocument());
    expect(detailCalls()).toHaveLength(0);
  });
});
