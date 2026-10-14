/**
 * Component tests for stream cameras in the device Cameras tab
 * (rtsp-rtmp-stream-cameras task 11.7 — Requirements 5.1, 5.7, 16.4).
 *
 * - The table shows a stream camera's URL and settings, a credentials
 *   badge, and the reported health, codec, resolution and decoder.
 * - The typed RTSP/RTMP form never renders a stored credential: the
 *   credential inputs always start empty, and nothing but the URL and the
 *   settings is read from the entry, even when the entry carries
 *   credential-like values (which the registry never serves; they are
 *   planted here to prove the form would not show them).
 * - Saving sends typed credentials only in the write-only `credentials`
 *   object, keeps stored ones when the inputs are left blank, and sends
 *   `clearCredentials` alone when they are removed.
 * - A URL with embedded credentials is rejected in the form with the
 *   Stream_URL message, before anything is sent.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import DeviceCamerasTab from './DeviceCamerasTab';
import type {
  CameraSourceEntry,
  DeviceCamerasResponse,
} from '../pages/workflows/cameraReference';

const {
  getDeviceCameras,
  getDeviceCameraConflicts,
  createDeviceCamera,
  updateDeviceCamera,
  deleteDeviceCamera,
  reapplyCameraConflict,
  refreshDeviceCameras,
  getStaticImagePinStatus,
  getStaticImageUploadUrl,
  pinStaticImage,
  removeStaticImagePin,
} = vi.hoisted(() => ({
  getDeviceCameras: vi.fn(),
  getDeviceCameraConflicts: vi.fn(),
  createDeviceCamera: vi.fn(),
  updateDeviceCamera: vi.fn(),
  deleteDeviceCamera: vi.fn(),
  reapplyCameraConflict: vi.fn(),
  refreshDeviceCameras: vi.fn(),
  getStaticImagePinStatus: vi.fn(),
  getStaticImageUploadUrl: vi.fn(),
  pinStaticImage: vi.fn(),
  removeStaticImagePin: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: {
    getDeviceCameras,
    getDeviceCameraConflicts,
    createDeviceCamera,
    updateDeviceCamera,
    deleteDeviceCamera,
    reapplyCameraConflict,
    refreshDeviceCameras,
    getStaticImagePinStatus,
    getStaticImageUploadUrl,
    pinStaticImage,
    removeStaticImagePin,
  },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: null }),
}));

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const DEVICE_ID = 'device-1';
const USECASE_ID = 'usecase-1';
const DOCK_URL = 'rtsp://10.0.4.21:554/Streaming/Channels/101';
/** Marks every planted credential-like value. */
const SENTINEL = 'SENTINEL-4f1c';

const STREAM_CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-rtsp',
  name: 'Dock 3 overview',
  type: 'RTSP',
  params: {
    url: DOCK_URL,
    transport: 'udp',
    latencyMs: 400,
    credentialsConfigured: true,
    credentialsUpdatedAt: 1790000000000,
    // Never served by the registry (the view omits the reference and
    // masks credential-like keys); planted to prove the tab ignores them.
    credentialRef: `arn:aws:secretsmanager:us-east-1:123456789012:secret:${SENTINEL}-ref`,
    username: `${SENTINEL}-user`,
    password: `${SENTINEL}-password`,
    urlSecret: `${SENTINEL}-key`,
  },
  credentials: { configured: true, updatedAt: 1790000000000 },
  capabilities: {
    stream: { state: 'streaming', codec: 'h265', width: 1920, height: 1080, decoder: 'hardware' },
  },
  origin: 'portal-created',
  version: 2,
  last_reported_at: 1700000000000,
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const PLAIN_STREAM_CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-rtmp',
  name: 'Line 1 encoder',
  type: 'RTMP',
  params: { url: 'rtmp://media.local/live/line1' },
  credentials: { configured: false, updatedAt: null },
  capabilities: { stream: { state: 'failed' } },
  origin: 'portal-created',
  version: 1,
  sync_status: 'synced',
  stale: false,
  absent: false,
};

function camerasResponse(cameras: CameraSourceEntry[]): DeviceCamerasResponse {
  return {
    device_id: DEVICE_ID,
    usecase_id: USECASE_ID,
    state: 'synced',
    last_report_at: 1700000000000,
    staleness_threshold_hours: 24,
    device_status: 'HEALTHY',
    cameras,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  getDeviceCameras.mockResolvedValue(camerasResponse([STREAM_CAMERA, PLAIN_STREAM_CAMERA]));
  getDeviceCameraConflicts.mockResolvedValue({
    device_id: DEVICE_ID,
    usecase_id: USECASE_ID,
    conflicts: [],
    count: 0,
  });
  getStaticImagePinStatus.mockResolvedValue({
    deviceId: DEVICE_ID,
    usecaseId: USECASE_ID,
    latest: null,
    noPinRequest: true,
    deviceReported: null,
    history: [],
  });
  createDeviceCamera.mockResolvedValue({});
  updateDeviceCamera.mockResolvedValue({});
});

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

async function renderLoaded() {
  const view = render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
  await waitFor(() => expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument());
  await waitFor(() => expect(getStaticImagePinStatus).toHaveBeenCalled());
  return view;
}

/** The modal renders in a portal, so everything is found from the body. */
const body = () => createWrapper(document.body);

function input(testId: string) {
  return body().findInput(`[data-testid="${testId}"]`);
}

function inputValue(testId: string): string {
  return (input(testId)!.findNativeInput().getElement() as HTMLInputElement).value;
}

/** Every rendered input's value and the whole document's markup. */
function renderedText(): string {
  const values = Array.from(document.body.querySelectorAll('input, textarea')).map(
    (element) => (element as HTMLInputElement).value
  );
  return `${document.body.innerHTML}\n${values.join('\n')}`;
}

async function openEditForm(container: HTMLElement, row: number) {
  const table = createWrapper(container).findTable('[data-testid="device-cameras-table"]')!;
  table.findRowSelectionArea(row)!.click();
  const edit = screen.getByTestId('edit-camera-button');
  await waitFor(() => expect(edit).not.toBeDisabled());
  fireEvent.click(edit);
  await waitFor(() => expect(input('stream-form-url')).not.toBeNull());
}

function submit() {
  fireEvent.click(screen.getByTestId('camera-form-submit'));
}

// --------------------------------------------------------------------------
// Table display (Requirements 5.7, 16.4)
// --------------------------------------------------------------------------

describe('stream camera rows', () => {
  it('show the URL and settings, the credential state, and the reported stream details', async () => {
    const { container } = await renderLoaded();
    const table = createWrapper(container).findTable('[data-testid="device-cameras-table"]')!;
    const dock = table.findRows()[0].getElement();
    expect(dock.textContent).toContain(`url: ${DOCK_URL}, transport: udp, latencyMs: 400`);
    expect(within(dock).getByText('Credentials configured')).toBeInTheDocument();
    expect(within(dock).getByText('Streaming')).toBeInTheDocument();
    expect(dock.textContent).toContain('H.265 \u00b7 1920\u00d71080 \u00b7 hardware');

    const encoder = table.findRows()[1].getElement();
    expect(within(encoder).getByText('No credentials')).toBeInTheDocument();
    expect(within(encoder).getByText('Failed')).toBeInTheDocument();

    // No credential value, reference, or flag is rendered as text.
    const text = renderedText();
    expect(text).not.toContain(SENTINEL);
    expect(dock.textContent).not.toContain('credentialsConfigured');
    expect(dock.textContent).not.toContain('credentialRef');
  });
});

// --------------------------------------------------------------------------
// The edit form never renders stored credentials (Requirements 5.1, 5.7)
// --------------------------------------------------------------------------

describe('the stream camera edit form', () => {
  it('starts with empty, masked credential inputs and never renders stored values', async () => {
    const { container } = await renderLoaded();
    await openEditForm(container, 1);

    expect(inputValue('stream-form-url')).toBe(DOCK_URL);
    expect(inputValue('stream-form-latency')).toBe('400');
    for (const testId of ['stream-form-username', 'stream-form-password', 'stream-form-url-secret']) {
      expect(inputValue(testId)).toBe('');
    }
    for (const testId of ['stream-form-password', 'stream-form-url-secret']) {
      expect(input(testId)!.findNativeInput().getElement()).toHaveAttribute('type', 'password');
    }
    expect(screen.getByText('Credentials are configured. Leave these blank to keep them.')).toBeInTheDocument();
    // The typed form replaces the raw parameters JSON for stream types.
    expect(screen.queryByTestId('camera-form-params')).toBeNull();
    expect(renderedText()).not.toContain(SENTINEL);
  });

  it('keeps stored credentials when the inputs are left blank', async () => {
    const { container } = await renderLoaded();
    await openEditForm(container, 1);
    submit();
    await waitFor(() => expect(updateDeviceCamera).toHaveBeenCalledTimes(1));
    expect(updateDeviceCamera).toHaveBeenCalledWith(DEVICE_ID, 'cfg-rtsp', USECASE_ID, {
      name: 'Dock 3 overview',
      type: 'RTSP',
      params: { url: DOCK_URL, transport: 'udp', latencyMs: 400 },
    });
  });

  it('sends a typed credential only in the write-only credentials object', async () => {
    const { container } = await renderLoaded();
    await openEditForm(container, 1);
    input('stream-form-password')!.setInputValue('n3w-pass-7d');
    submit();
    await waitFor(() => expect(updateDeviceCamera).toHaveBeenCalledTimes(1));
    const [, , , sent] = updateDeviceCamera.mock.calls[0];
    expect(sent.credentials).toEqual({ password: 'n3w-pass-7d' });
    expect(sent.clearCredentials).toBeUndefined();
    expect(JSON.stringify(sent.params)).not.toContain('n3w-pass-7d');
  });

  it('sends clearCredentials alone when the stored credentials are removed', async () => {
    const { container } = await renderLoaded();
    await openEditForm(container, 1);
    input('stream-form-username')!.setInputValue('someone');
    body()
      .findCheckbox('[data-testid="stream-form-clear-credentials"]')!
      .findNativeInput()
      .click();
    // Removing clears and disables the inputs.
    expect(inputValue('stream-form-username')).toBe('');
    expect(input('stream-form-password')!.findNativeInput().getElement()).toBeDisabled();
    submit();
    await waitFor(() => expect(updateDeviceCamera).toHaveBeenCalledTimes(1));
    const [, , , sent] = updateDeviceCamera.mock.calls[0];
    expect(sent.clearCredentials).toBe(true);
    expect(sent.credentials).toBeUndefined();
  });

  it('does not keep a typed credential after the form is cancelled', async () => {
    const { container } = await renderLoaded();
    await openEditForm(container, 1);
    input('stream-form-password')!.setInputValue('typed-then-cancelled');
    // The delete confirmation modal has a Cancel of its own.
    const dialog = screen.getByTestId('camera-form-submit').closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(input('stream-form-url')).toBeNull());
    fireEvent.click(screen.getByTestId('edit-camera-button'));
    await waitFor(() => expect(input('stream-form-url')).not.toBeNull());
    expect(inputValue('stream-form-password')).toBe('');
    expect(renderedText()).not.toContain('typed-then-cancelled');
  });
});

// --------------------------------------------------------------------------
// Creating an RTMP camera (Requirement 5.1)
// --------------------------------------------------------------------------

describe('creating an RTMP camera', () => {
  it('rejects a URL with credentials in the form, then sends the stream key write-only', async () => {
    await renderLoaded();
    fireEvent.click(screen.getByTestId('create-camera-button'));
    await waitFor(() => expect(screen.getByTestId('camera-form-submit')).toBeInTheDocument());
    input('camera-form-name')!.setInputValue('Line 2 encoder');
    const typeSelect = body().findSelect()!;
    typeSelect.openDropdown();
    typeSelect.selectOptionByValue('RTMP');
    await waitFor(() => expect(input('stream-form-url')).not.toBeNull());

    // RTMP has no transport or latency, and names its URL secret a stream key.
    expect(body().findSelect('[data-testid="stream-form-transport"]')).toBeNull();
    expect(input('stream-form-latency')).toBeNull();
    expect(screen.getByText('Stream key')).toBeInTheDocument();

    input('stream-form-url')!.setInputValue('rtmp://encoder:hunter2@media.local/live/line2');
    submit();
    await waitFor(() =>
      expect(document.body.textContent).toContain(
        "credentials belong in the camera's configuration"
      )
    );
    expect(createDeviceCamera).not.toHaveBeenCalled();

    input('stream-form-url')!.setInputValue('rtmp://media.local/live/line2');
    input('stream-form-url-secret')!.setInputValue('line2-key');
    submit();
    await waitFor(() => expect(createDeviceCamera).toHaveBeenCalledTimes(1));
    expect(createDeviceCamera).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID, {
      name: 'Line 2 encoder',
      type: 'RTMP',
      params: { url: 'rtmp://media.local/live/line2' },
      credentials: { urlSecret: 'line2-key' },
    });
  });
});
