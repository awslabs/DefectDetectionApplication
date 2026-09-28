/**
 * The Cameras tab with the second virtual camera (static-camera-video-loop
 * task 9.5, Requirements 9.1, 9.7).
 *
 * - Two separate panels: "Static image camera" (unchanged) and "Static
 *   video camera" below it, each loading its own status.
 * - Arrival through the "Pin a test video…" shortcut (`focusStaticVideo`)
 *   scrolls the video panel into view once loading resolves and flags it;
 *   the image panel is neither scrolled to nor flagged, and the image
 *   arrival is unchanged by the video panel.
 * - The create form points at both panels.
 *
 * The mock scaffolding follows `DeviceCamerasTab.staticImageFocus.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import DeviceCamerasTab from './DeviceCamerasTab';
import type {
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
  StaticImagePinStatusResponse,
  StaticVideoPinStatusResponse,
} from '../pages/workflows/cameraReference';

const {
  getDeviceCameras,
  getDeviceCameraConflicts,
  getStaticImagePinStatus,
  getStaticVideoPinStatus,
  authState,
} = vi.hoisted(() => ({
  getDeviceCameras: vi.fn(),
  getDeviceCameraConflicts: vi.fn(),
  getStaticImagePinStatus: vi.fn(),
  getStaticVideoPinStatus: vi.fn(),
  authState: { role: 'Operator' as string | undefined },
}));

vi.mock('../services/api', () => ({
  apiService: {
    getDeviceCameras,
    getDeviceCameraConflicts,
    getStaticImagePinStatus,
    getStaticVideoPinStatus,
  },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: authState.role ? { role: authState.role } : null }),
}));

const DEVICE_ID = 'jetson-thor1';
const USECASE_ID = 'usecase-1';

function camerasResponse(): DeviceCamerasResponse {
  return {
    device_id: DEVICE_ID,
    usecase_id: USECASE_ID,
    state: 'synced',
    last_report_at: 1790000000000,
    staleness_threshold_hours: 24,
    device_status: 'HEALTHY',
    cameras: [
      {
        camera_source_id: 'static-video-camera',
        name: 'Static Video Camera',
        type: 'StaticVideo',
        params: {},
        capabilities: { staticVideo: { id: 'static-video-camera', fps: 29.97 } },
        origin: 'edge-discovered',
        sync_status: 'synced',
        stale: false,
        absent: false,
        last_reported_at: 1790000000000,
      },
    ],
    count: 1,
  };
}

function conflictsResponse(): DeviceCameraConflictsResponse {
  return { device_id: DEVICE_ID, usecase_id: USECASE_ID, conflicts: [], count: 0 };
}

function imageStatus(): StaticImagePinStatusResponse {
  return {
    deviceId: DEVICE_ID,
    usecaseId: USECASE_ID,
    latest: null,
    noPinRequest: true,
    deviceReported: null,
    history: [],
  };
}

function videoStatus(): StaticVideoPinStatusResponse {
  return {
    deviceId: DEVICE_ID,
    usecaseId: USECASE_ID,
    latest: null,
    noPinRequest: true,
    deviceReported: { present: true, absent: false },
    history: [],
  };
}

const scrollTargets: Element[] = [];
const scrollIntoView = vi.fn(function (this: Element) {
  scrollTargets.push(this);
});

beforeEach(() => {
  vi.clearAllMocks();
  scrollTargets.length = 0;
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    value: scrollIntoView,
    writable: true,
    configurable: true,
  });
  authState.role = 'Operator';
  getDeviceCameras.mockResolvedValue(camerasResponse());
  getDeviceCameraConflicts.mockResolvedValue(conflictsResponse());
  getStaticImagePinStatus.mockResolvedValue(imageStatus());
  getStaticVideoPinStatus.mockResolvedValue(videoStatus());
});

afterEach(() => {
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

async function waitForPanels() {
  await waitFor(() => expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument());
  await waitFor(() => expect(screen.getByTestId('static-image-no-request')).toBeInTheDocument());
  await waitFor(() =>
    expect(screen.getByTestId('static-video-device-reported')).toBeInTheDocument()
  );
}

describe('DeviceCamerasTab with the static video camera', () => {
  it('renders a separate video panel below the image panel (Req 9.1)', async () => {
    render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForPanels();

    const imagePanel = screen.getByTestId('static-image-panel');
    const videoPanel = screen.getByTestId('static-video-panel');
    expect(imagePanel.contains(videoPanel)).toBe(false);
    expect(
      imagePanel.compareDocumentPosition(videoPanel) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(getStaticImagePinStatus).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    expect(getStaticVideoPinStatus).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    // Each panel has its own controls.
    expect(screen.getByTestId('static-image-pin-button')).toBeInTheDocument();
    expect(screen.getByTestId('static-video-pin-button').textContent).toContain('Replace video');
    // No arrival: nothing scrolls, nothing is flagged.
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(screen.queryByTestId('static-video-focus-flag')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-focus-flag')).not.toBeInTheDocument();
  });

  it('brings the video panel into view and flags it on a video arrival (Req 9.7)', async () => {
    render(
      <DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} focusStaticVideo />
    );
    await waitForPanels();

    const videoPanel = screen.getByTestId('static-video-panel');
    const imagePanel = screen.getByTestId('static-image-panel');
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1));
    expect(scrollTargets[0].contains(videoPanel)).toBe(true);
    expect(scrollTargets[0].contains(imagePanel)).toBe(false);
    expect(videoPanel.contains(screen.getByTestId('static-video-focus-flag'))).toBe(true);
    expect(screen.queryByTestId('static-image-focus-flag')).not.toBeInTheDocument();
  });

  it('keeps the image arrival on the image panel', async () => {
    render(
      <DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} focusStaticImage />
    );
    await waitForPanels();

    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1));
    expect(scrollTargets[0].contains(screen.getByTestId('static-image-panel'))).toBe(true);
    expect(scrollTargets[0].contains(screen.getByTestId('static-video-panel'))).toBe(false);
    expect(screen.queryByTestId('static-video-focus-flag')).not.toBeInTheDocument();
  });

  it('shows the video panel read-only without the device-mutation permission', async () => {
    authState.role = 'Viewer';
    render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForPanels();
    expect(screen.getByTestId('static-video-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('static-video-pin-button')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-video-remove-button')).not.toBeInTheDocument();
  });

  it('points the create form at both virtual-camera panels', async () => {
    render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForPanels();

    fireEvent.click(screen.getByTestId('create-camera-button'));
    await waitFor(() => expect(screen.getByTestId('camera-form-submit')).toBeInTheDocument());
    expect(screen.getByTestId('camera-form-static-image-note')).toBeInTheDocument();
    expect(screen.getByTestId('camera-form-static-video-note').textContent).toContain(
      'Static video camera'
    );
  });
});
