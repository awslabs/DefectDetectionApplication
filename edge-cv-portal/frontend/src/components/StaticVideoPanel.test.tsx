/**
 * Tests for the "Static video camera" panel (static-camera-video-loop
 * task 9.5, Requirements 8.2-8.4 and 9.1-9.7).
 *
 * - Pure helpers: the 100 MB pre-check wording, duration/fps formatting,
 *   the metadata rows, which validation to surface, and the poll interval.
 * - The panel: the loop note and the limit, role gating, the upload flow
 *   (pre-check, presigned PUT with progress, then the pin submit that
 *   starts the Portal's asynchronous validation), the validating and
 *   rejected states and their polling, the request states with the
 *   failure reason and the connectivity hint, the applied metadata, the
 *   removal confirmation, the arrival flag, and the load error.
 *
 * `apiService` and the XHR upload helper are mocked; the panel takes the
 * mutation permission as a prop, so no auth context is needed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import StaticVideoPanel, {
  VIDEO_PIN_STATUS_POLL_MS,
  VIDEO_VALIDATION_POLL_MS,
  formatVideoDuration,
  formatVideoFps,
  outstandingValidation,
  videoMetadataItems,
  videoOversizeMessage,
  videoStatusPollInterval,
} from './StaticVideoPanel';
import {
  MAX_PIN_VIDEO_BYTES,
  type StaticVideoPinStatusResponse,
} from '../pages/workflows/cameraReference';

const {
  getStaticVideoPinStatus,
  getStaticVideoUploadUrl,
  pinStaticVideo,
  removeStaticVideoPin,
  putFileWithProgress,
} = vi.hoisted(() => ({
  getStaticVideoPinStatus: vi.fn(),
  getStaticVideoUploadUrl: vi.fn(),
  pinStaticVideo: vi.fn(),
  removeStaticVideoPin: vi.fn(),
  putFileWithProgress: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: {
    getStaticVideoPinStatus,
    getStaticVideoUploadUrl,
    pinStaticVideo,
    removeStaticVideoPin,
  },
}));

vi.mock('../utils/detectorConversion', () => ({ putFileWithProgress }));

const DEVICE_ID = 'jetson-thor1';
const USECASE_ID = 'usecase-1';
const CREATED_AT_MS = 1790000000000;

function statusResponse(
  overrides: Partial<StaticVideoPinStatusResponse> = {}
): StaticVideoPinStatusResponse {
  return {
    deviceId: DEVICE_ID,
    usecaseId: USECASE_ID,
    latest: null,
    noPinRequest: true,
    deviceReported: null,
    history: [],
    ...overrides,
  };
}

const APPLIED_METADATA = {
  fileName: 'scene.mp4',
  format: 'MP4',
  codec: 'H264',
  width: 64,
  height: 48,
  fps: 29.97002997002997,
  frameCount: 30,
  durationMs: 1001,
  fileSizeBytes: 5 * 1024 * 1024,
  pinnedAtEpochMs: CREATED_AT_MS,
};

function renderPanel(props: Partial<Parameters<typeof StaticVideoPanel>[0]> = {}) {
  return render(
    <StaticVideoPanel deviceId={DEVICE_ID} usecaseId={USECASE_ID} canMutate {...props} />
  );
}

async function waitForLoaded() {
  await waitFor(() => expect(getStaticVideoPinStatus).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByTestId('static-video-panel')).toBeInTheDocument());
}

function chooseFile(container: HTMLElement, file: File) {
  const upload = createWrapper(container).findFileUpload()!;
  fireEvent.change(upload.findNativeInput().getElement(), { target: { files: [file] } });
}

function videoFile(name = 'scene.mp4', sizeBytes?: number): File {
  const file = new File(['mp4-bytes'], name, { type: 'video/mp4' });
  if (sizeBytes !== undefined) {
    Object.defineProperty(file, 'size', { value: sizeBytes });
  }
  return file;
}

beforeEach(() => {
  getStaticVideoPinStatus.mockReset();
  getStaticVideoPinStatus.mockResolvedValue(statusResponse());
  getStaticVideoUploadUrl.mockReset();
  getStaticVideoUploadUrl.mockResolvedValue({
    deviceId: DEVICE_ID,
    uploadUrl: 'https://upload.example/staging-put',
    stagingKey: 'static-image-pins/staging/v-1',
    bucket: 'dda-component-bucket',
    expiresInSeconds: 900,
  });
  pinStaticVideo.mockReset();
  pinStaticVideo.mockResolvedValue({
    validationId: '01790000000000#abcdef12',
    deviceId: DEVICE_ID,
    status: 'validating',
  });
  removeStaticVideoPin.mockReset();
  removeStaticVideoPin.mockResolvedValue({
    pinRequestId: '01790000000100#fedcba98',
    deviceId: DEVICE_ID,
    status: 'pending',
  });
  putFileWithProgress.mockReset();
  putFileWithProgress.mockResolvedValue(undefined);
});

afterEach(() => {
  // Restores the setInterval/clearInterval spies of the polling test.
  vi.restoreAllMocks();
});

// --------------------------------------------------------------------------
// Pure helpers
// --------------------------------------------------------------------------

describe('StaticVideoPanel helpers', () => {
  it('pre-checks the 100 MB limit, naming it (Req 9.3)', () => {
    expect(MAX_PIN_VIDEO_BYTES).toBe(100 * 1024 * 1024);
    expect(videoOversizeMessage(MAX_PIN_VIDEO_BYTES)).toBeNull();
    expect(videoOversizeMessage(0)).toBeNull();
    expect(videoOversizeMessage(MAX_PIN_VIDEO_BYTES + 1)).toBe(
      'The video is 100.0 MB; videos can be at most 100 MB.'
    );
    expect(videoOversizeMessage(150 * 1024 * 1024)).toContain('150.0 MB');
  });

  it('formats durations and frame rates', () => {
    expect(formatVideoDuration(1001)).toBe('1.0 s');
    expect(formatVideoDuration(59_949)).toBe('59.9 s');
    expect(formatVideoDuration(125_000)).toBe('2 min 5.0 s');
    expect(formatVideoDuration(null)).toBe('-');
    expect(formatVideoDuration(-1)).toBe('-');
    expect(formatVideoFps(29.97002997002997)).toBe('29.97 fps');
    expect(formatVideoFps(30)).toBe('30 fps');
    expect(formatVideoFps(0)).toBe('-');
    expect(formatVideoFps(undefined)).toBe('-');
  });

  it('lists every Video_Metadata row the panel shows (Req 9.5)', () => {
    const items = videoMetadataItems(APPLIED_METADATA);
    expect(items.map((item) => item.label)).toEqual([
      'Duration',
      'Frame rate',
      'Frame count',
      'Codec',
      'Dimensions',
      'Container',
      'File name',
      'File size',
    ]);
    expect(items.map((item) => item.value)).toEqual([
      '1.0 s',
      '29.97 fps',
      '30',
      'H264',
      '64 × 48 px',
      'MP4',
      'scene.mp4',
      '5.0 MB',
    ]);
    // Validated metadata carries no file name or size: the name falls back
    // to the one passed in, and there is no size row.
    const validated = videoMetadataItems(
      { format: 'WEBM', codec: 'VP9', width: 32, height: 24, fps: 12, frameCount: 24, durationMs: 2000 },
      'loop.webm'
    );
    expect(validated.find((item) => item.label === 'File name')?.value).toBe('loop.webm');
    expect(validated.some((item) => item.label === 'File size')).toBe(false);
  });

  it('surfaces a validation only while it is outstanding and newer than the latest request', () => {
    const validating = { validationId: 'v-2', status: 'validating', createdAt: 2000, fileName: 'a.mp4' };
    const rejected = { ...validating, status: 'rejected', error: 'not a video' };
    const latest = { pinRequestId: 'p-1', op: 'pin', status: 'applied', createdAt: 1000 };

    expect(outstandingValidation(statusResponse({ validation: validating }))).toEqual(validating);
    expect(outstandingValidation(statusResponse({ validation: rejected, latest }))).toEqual(rejected);
    expect(
      outstandingValidation(statusResponse({ validation: { ...rejected, status: 'expired' } }))
    ).not.toBeNull();
    // Older than the latest request (a later removal, or the request it
    // created): history, not shown.
    expect(
      outstandingValidation(
        statusResponse({ validation: rejected, latest: { ...latest, createdAt: 3000 } })
      )
    ).toBeNull();
    for (const status of ['accepted', 'superseded']) {
      expect(
        outstandingValidation(statusResponse({ validation: { ...validating, status } }))
      ).toBeNull();
    }
    expect(outstandingValidation(statusResponse())).toBeNull();
    expect(outstandingValidation(null)).toBeNull();
  });

  it('polls fast while validating, slower while pending, not otherwise', () => {
    const validating = { validationId: 'v-2', status: 'validating', createdAt: 2000 };
    const pendingLatest = { pinRequestId: 'p-1', op: 'pin', status: 'pending', createdAt: 1000 };
    expect(videoStatusPollInterval(statusResponse({ validation: validating }))).toBe(
      VIDEO_VALIDATION_POLL_MS
    );
    expect(
      videoStatusPollInterval(statusResponse({ validation: validating, latest: pendingLatest }))
    ).toBe(VIDEO_VALIDATION_POLL_MS);
    expect(videoStatusPollInterval(statusResponse({ latest: pendingLatest }))).toBe(
      VIDEO_PIN_STATUS_POLL_MS
    );
    expect(
      videoStatusPollInterval(
        statusResponse({ latest: { ...pendingLatest, status: 'applied' } })
      )
    ).toBeNull();
    expect(videoStatusPollInterval(statusResponse())).toBeNull();
  });
});

// --------------------------------------------------------------------------
// The panel
// --------------------------------------------------------------------------

describe('StaticVideoPanel', () => {
  it('states the loop and the 100 MB limit, with no request yet (Reqs 9.1, 9.2)', async () => {
    const { container } = renderPanel();
    await waitForLoaded();

    const panel = screen.getByTestId('static-video-panel');
    expect(panel.textContent).toContain('Static video camera');
    expect(panel.textContent).toContain('plays it in a loop');
    expect(panel.textContent).toContain('at most 100 MB');
    expect(screen.getByTestId('static-video-no-request')).toBeInTheDocument();
    expect(screen.queryByTestId('static-video-status')).not.toBeInTheDocument();
    expect(getStaticVideoPinStatus).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);

    // The picker accepts the supported containers.
    const input = createWrapper(container).findFileUpload()!.findNativeInput().getElement();
    for (const extension of ['.mp4', '.m4v', '.mov', '.avi', '.mkv', '.webm']) {
      expect(input.getAttribute('accept')).toContain(extension);
    }
    expect(screen.getByTestId('static-video-pin-button').textContent).toContain('Pin video');
  });

  it('hides the mutations without the device-mutation permission (Req 9.6)', async () => {
    renderPanel({ canMutate: false });
    await waitForLoaded();
    expect(screen.getByTestId('static-video-no-request')).toBeInTheDocument();
    expect(screen.queryByTestId('static-video-pin-button')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-video-remove-button')).not.toBeInTheDocument();
  });

  it('refuses a video over 100 MB before any upload (Req 9.3)', async () => {
    const { container } = renderPanel();
    await waitForLoaded();

    chooseFile(container, videoFile('huge.mp4', MAX_PIN_VIDEO_BYTES + 1));

    // The selection re-renders synchronously (fireEvent runs in act), so
    // this is asserted directly. Cloudscape test-utils lookups are kept out
    // of waitFor callbacks throughout this file: a lookup that finds
    // nothing keeps re-triggering waitFor's MutationObserver, and a failing
    // check then hangs the run instead of failing it.
    const field = createWrapper(container).findFormField()!;
    expect(field.findError()?.getElement().textContent ?? '').toContain(
      'The video is 100.0 MB; videos can be at most 100 MB.'
    );
    expect(screen.getByTestId('static-video-pin-button')).toBeDisabled();
    expect(getStaticVideoUploadUrl).not.toHaveBeenCalled();
    expect(putFileWithProgress).not.toHaveBeenCalled();
  });

  it('uploads with progress, then submits the pin for validation (Reqs 8.2, 9.4)', async () => {
    let finishUpload: () => void = () => {};
    let reportProgress: (percent: number) => void = () => {};
    putFileWithProgress.mockImplementation(
      (_url: string, _file: Blob, onProgress: (percent: number) => void) => {
        reportProgress = onProgress;
        return new Promise<void>((resolve) => {
          finishUpload = resolve;
        });
      }
    );
    const { container } = renderPanel();
    await waitForLoaded();
    const file = videoFile('scene.mp4');

    chooseFile(container, file);
    const pinButton = screen.getByTestId('static-video-pin-button');
    expect(pinButton).not.toBeDisabled();
    fireEvent.click(pinButton);

    await waitFor(() => expect(putFileWithProgress).toHaveBeenCalledTimes(1));
    expect(getStaticVideoUploadUrl).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    const [url, uploaded] = putFileWithProgress.mock.calls[0];
    expect(url).toBe('https://upload.example/staging-put');
    expect(uploaded).toBe(file);

    // The progress bar follows the upload.
    act(() => reportProgress(42));
    const bar = createWrapper(container).findProgressBar();
    expect(bar?.findPercentageText()?.getElement().textContent ?? '').toContain('42');
    expect(screen.getByTestId('static-video-upload-progress').textContent).toContain(
      'Uploading scene.mp4'
    );
    expect(pinStaticVideo).not.toHaveBeenCalled();

    await act(async () => finishUpload());
    await waitFor(() => {
      expect(pinStaticVideo).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID, {
        stagingKey: 'static-image-pins/staging/v-1',
        fileName: 'scene.mp4',
      });
    });
    // The status is reloaded after the submit, and the progress bar goes.
    await waitFor(() => expect(getStaticVideoPinStatus.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() =>
      expect(screen.queryByTestId('static-video-upload-progress')).not.toBeInTheDocument()
    );
  });

  it('shows an upload failure and never submits', async () => {
    putFileWithProgress.mockRejectedValue(new Error('Upload failed (HTTP 403 AccessDenied)'));
    const { container } = renderPanel();
    await waitForLoaded();

    chooseFile(container, videoFile());
    const pinButton = screen.getByTestId('static-video-pin-button');
    expect(pinButton).not.toBeDisabled();
    fireEvent.click(pinButton);

    await waitFor(() => {
      expect(screen.getByTestId('static-video-action-error').textContent).toContain(
        'Upload failed (HTTP 403 AccessDenied)'
      );
    });
    expect(pinStaticVideo).not.toHaveBeenCalled();
  });

  it('shows the validation in progress and polls until it ends (Req 8.2)', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        noPinRequest: true,
        validation: {
          validationId: 'v-1',
          status: 'validating',
          createdAt: CREATED_AT_MS,
          fileName: 'scene.mp4',
        },
      })
    );
    renderPanel();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-validation').textContent).toContain(
        'Validating scene.mp4'
      );
    });
    expect(screen.getByTestId('static-video-panel').textContent).toContain(
      'can take up to a minute'
    );
    // Validating is not "no request": the empty-state line is hidden.
    expect(screen.queryByTestId('static-video-no-request')).not.toBeInTheDocument();

    // The panel polls at the validation interval; one tick reloads.
    const poll = setIntervalSpy.mock.calls.find(
      ([, delay]) => delay === VIDEO_VALIDATION_POLL_MS
    );
    expect(poll).toBeDefined();
    const before = getStaticVideoPinStatus.mock.calls.length;
    await act(async () => {
      (poll![0] as () => void)();
    });
    await waitFor(() =>
      expect(getStaticVideoPinStatus.mock.calls.length).toBeGreaterThan(before)
    );

    // Once the validation ends (here: rejected), polling stops.
    const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        validation: {
          validationId: 'v-1',
          status: 'rejected',
          createdAt: CREATED_AT_MS,
          fileName: 'scene.mp4',
          error: 'not a video',
        },
      })
    );
    await act(async () => {
      (poll![0] as () => void)();
    });
    await waitFor(() =>
      expect(screen.getByTestId('static-video-validation-error').textContent).toBe(
        'not a video'
      )
    );
    expect(clearIntervalSpy).toHaveBeenCalled();
    setIntervalSpy.mockRestore();
    clearIntervalSpy.mockRestore();
  });

  it('shows why the Portal rejected a video (Reqs 8.3, 8.4)', async () => {
    const message =
      'The video took too long to validate (over 60 seconds); use a lower resolution or more frequent keyframes.';
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        validation: {
          validationId: 'v-1',
          status: 'rejected',
          createdAt: CREATED_AT_MS,
          completedAt: CREATED_AT_MS + 61_000,
          fileName: 'huge-4k.mp4',
          error: message,
        },
      })
    );
    renderPanel();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-validation').textContent).toContain(
        'Video rejected: huge-4k.mp4'
      );
    });
    expect(screen.getByTestId('static-video-validation-error').textContent).toBe(message);
  });

  it('shows the pending request, its validated metadata, and the connectivity hint', async () => {
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        noPinRequest: false,
        latest: {
          pinRequestId: 'p-1',
          op: 'pin',
          status: 'pending',
          createdAt: CREATED_AT_MS + 5_000,
          validatedMetadata: {
            format: 'MP4', codec: 'HEVC', width: 1920, height: 1080,
            fps: 25, frameCount: 250, durationMs: 10_000,
          },
        },
        validation: {
          validationId: 'v-1',
          status: 'accepted',
          createdAt: CREATED_AT_MS,
          pinRequestId: 'p-1',
          fileName: 'line.mp4',
        },
        connectivity: 'disconnected',
      })
    );
    renderPanel();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-status').textContent).toContain(
        'Video pin pending'
      );
    });
    expect(screen.queryByTestId('static-video-validation')).not.toBeInTheDocument();
    expect(screen.getByTestId('static-video-connectivity-hint').textContent).toContain(
      'currently disconnected'
    );
    const validated = screen.getByTestId('static-video-validated-metadata');
    expect(validated.textContent).toContain('HEVC');
    expect(validated.textContent).toContain('1920 × 1080 px');
    expect(validated.textContent).toContain('10.0 s');
    expect(screen.queryByTestId('static-video-metadata')).not.toBeInTheDocument();
  });

  it('shows the applied video with its metadata and offers a replace (Req 9.5)', async () => {
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        noPinRequest: false,
        latest: {
          pinRequestId: 'p-1',
          op: 'pin',
          status: 'applied',
          createdAt: CREATED_AT_MS,
          completedAt: CREATED_AT_MS + 30_000,
          deviceMetadata: APPLIED_METADATA,
        },
        deviceReported: { present: true, absent: false },
        deviceMetadata: APPLIED_METADATA,
      })
    );
    const { container } = renderPanel();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-status').textContent).toContain(
        'Video pin applied'
      );
    });
    expect(screen.getByTestId('static-video-device-reported').textContent).toContain(
      'Device reports a pinned video'
    );
    const pairs = createWrapper(screen.getByTestId('static-video-metadata'))
      .findKeyValuePairs()!
      .findItems()
      .map((item) => [
        item.findLabel()?.getElement().textContent,
        item.findValue()?.getElement().textContent,
      ]);
    expect(pairs).toEqual([
      ['Duration', '1.0 s'],
      ['Frame rate', '29.97 fps'],
      ['Frame count', '30'],
      ['Codec', 'H264'],
      ['Dimensions', '64 × 48 px'],
      ['Container', 'MP4'],
      ['File name', 'scene.mp4'],
      ['File size', '5.0 MB'],
    ]);
    expect(screen.getByTestId('static-video-pin-button').textContent).toContain('Replace video');
    expect(
      createWrapper(container).findFormField()!.findLabel()?.getElement().textContent
    ).toContain('Replace the pinned video');
  });

  it('shows a failed request with the device-reported reason and the absent camera', async () => {
    getStaticVideoPinStatus.mockResolvedValue(
      statusResponse({
        noPinRequest: false,
        latest: {
          pinRequestId: 'p-1',
          op: 'pin',
          status: 'failed',
          createdAt: CREATED_AT_MS,
          failureReason: 'The video could not be decoded (codec: AV1).',
        },
        deviceReported: { present: false, absent: true, absentSince: CREATED_AT_MS - 1_000 },
      })
    );
    renderPanel();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-status').textContent).toContain(
        'Video pin failed'
      );
    });
    expect(screen.getByTestId('static-video-failure-reason').textContent).toContain(
      'codec: AV1'
    );
    expect(screen.getByTestId('static-video-device-reported').textContent).toContain(
      'Device reports the static video camera absent since'
    );
  });

  it('removes the pinned video after confirmation', async () => {
    renderPanel();
    await waitForLoaded();

    fireEvent.click(screen.getByTestId('static-video-remove-button'));
    fireEvent.click(screen.getByTestId('static-video-remove-confirm'));

    await waitFor(() => {
      expect(removeStaticVideoPin).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    });
    await waitFor(() => expect(getStaticVideoPinStatus.mock.calls.length).toBeGreaterThan(1));
  });

  it('flags the panel as the shortcut arrival target only when focused (Req 9.7)', async () => {
    const { unmount } = renderPanel({ focused: true });
    await waitForLoaded();
    const flag = screen.getByTestId('static-video-focus-flag');
    expect(screen.getByTestId('static-video-panel').contains(flag)).toBe(true);
    unmount();

    renderPanel();
    await waitForLoaded();
    expect(screen.queryByTestId('static-video-focus-flag')).not.toBeInTheDocument();
  });

  it('shows a load error with a retry', async () => {
    getStaticVideoPinStatus.mockRejectedValueOnce(new Error('status read failed'));
    renderPanel();

    await waitFor(() => {
      expect(screen.getByTestId('static-video-load-error').textContent).toContain(
        'status read failed'
      );
    });
    fireEvent.click(screen.getByTestId('static-video-retry'));
    await waitFor(() => expect(screen.getByTestId('static-video-no-request')).toBeInTheDocument());
  });
});
