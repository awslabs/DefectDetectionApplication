/**
 * Component tests for the device detail Cameras tab (camera-registry-sync
 * task 8.2): field display of registry entries (Req 1.3), stale badge
 * (Req 4.1), absent badge with its timestamp (Req 4.4), device
 * disconnected indicator (Req 4.2), explicit never-synced state
 * (Req 1.6), discovery-managed edit/delete blocking (Req 5.6), and the
 * conflict re-apply flow (Req 6.4), plus unit tests for the exported
 * pure helpers.
 *
 * Plus the static image camera panel (cloud-static-camera-provisioning
 * task 9.3): each Sync_Status rendering, the no-request state, the
 * failure reason, the connectivity hint while pending (Reqs 1.10, 4.4,
 * 4.5, 4.7); the pin/replace/remove flows calling the Portal_Pin_API
 * routes; and mutation actions hidden without the device-mutation
 * permission (Req 8.1's frontend gate).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import DeviceCamerasTab, {
  canManageDeviceCameras,
  formatEpochMs,
  summarizeRecord,
  summarizeCapabilities,
  isDiscoveryManaged,
  isDeviceDisconnected,
  parseParamsInput,
  summarizeConflictVersion,
} from './DeviceCamerasTab';
import type { UserRole } from '../types';
import type {
  CameraConflictEvent,
  CameraSourceEntry,
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
  StaticImagePinStatusResponse,
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
  authState,
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
  authState: { role: undefined as string | undefined },
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

// The tab reads only `user?.role` from the auth context (permission gate
// for the static-image pin mutations).
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: authState.role ? { role: authState.role } : null,
  }),
}));

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const DEVICE_ID = 'device-1';
const USECASE_ID = 'usecase-1';

const LAST_REPORTED_MS = 1700000000000;
const ABSENT_SINCE_MS = 1700000100000;
const CONFLICT_AT_MS = 1700000200000;

const PORTAL_CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-1',
  name: 'Line 1 inspection cam',
  type: 'Camera',
  params: { devicePath: '/dev/video0', gain: 4 },
  capabilities: {
    formats: [{ pixelFormat: 'YUYV', resolutions: [[1920, 1080], [1280, 720]] }],
  },
  origin: 'portal-created',
  version: 3,
  last_reported_at: LAST_REPORTED_MS,
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const DISCOVERED_CAMERA: CameraSourceEntry = {
  camera_source_id: 'disc-abc123',
  name: 'USB 2.0 Camera',
  type: 'Camera',
  params: { devicePath: '/dev/video2' },
  capabilities: { formats: [{ pixelFormat: 'MJPG', resolutions: [[640, 480]] }] },
  origin: 'edge-discovered',
  version: 1,
  last_reported_at: LAST_REPORTED_MS,
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const FAILED_CAMERA: CameraSourceEntry = {
  camera_source_id: 'cfg-2',
  name: 'RTSP feed',
  type: 'RTSP',
  params: { url: 'rtsp://example/stream' },
  origin: 'edge-configured',
  version: 2,
  last_reported_at: LAST_REPORTED_MS,
  sync_status: 'failed',
  failure_reason: 'schema validation rejected the configuration',
  stale: false,
  absent: false,
};

const CONFLICT: CameraConflictEvent = {
  conflict_id: 'conflict-1',
  camera_source_id: 'cfg-1',
  edge_version: { op: 'update', name: 'Edge name', params: { devicePath: '/dev/video0' } },
  portal_version: { op: 'update', name: 'Portal name', params: { devicePath: '/dev/video9' } },
  resolution: 'edge-retained',
  created_at: CONFLICT_AT_MS,
};

function camerasResponse(overrides: Partial<DeviceCamerasResponse> = {}): DeviceCamerasResponse {
  return {
    device_id: DEVICE_ID,
    usecase_id: USECASE_ID,
    state: 'synced',
    last_report_at: LAST_REPORTED_MS,
    staleness_threshold_hours: 24,
    device_status: 'HEALTHY',
    cameras: [PORTAL_CAMERA],
    ...overrides,
  };
}

function conflictsResponse(
  conflicts: CameraConflictEvent[] = []
): DeviceCameraConflictsResponse {
  return { device_id: DEVICE_ID, usecase_id: USECASE_ID, conflicts, count: conflicts.length };
}

/** Provisioning status response fixture; defaults to the no-request state. */
function pinStatusResponse(
  overrides: Partial<StaticImagePinStatusResponse> = {}
): StaticImagePinStatusResponse {
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

const PIN_CREATED_AT_MS = 1700000300000;

function renderTab() {
  return render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
}

async function waitForLoaded() {
  await waitFor(() => {
    expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument();
  });
  // The static image panel resolves its own status fetch.
  await waitFor(() => {
    expect(getStaticImagePinStatus).toHaveBeenCalled();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.role = undefined;
  getDeviceCameras.mockResolvedValue(camerasResponse());
  getDeviceCameraConflicts.mockResolvedValue(conflictsResponse());
  getStaticImagePinStatus.mockResolvedValue(pinStatusResponse());
});

// --------------------------------------------------------------------------
// Pure helper unit tests
// --------------------------------------------------------------------------

describe('formatEpochMs', () => {
  it('renders a human timestamp for valid epoch milliseconds', () => {
    expect(formatEpochMs(LAST_REPORTED_MS)).toBe(new Date(LAST_REPORTED_MS).toLocaleString());
  });

  it('renders "-" for null, undefined, zero, negative, and non-finite values', () => {
    expect(formatEpochMs(null)).toBe('-');
    expect(formatEpochMs(undefined)).toBe('-');
    expect(formatEpochMs(0)).toBe('-');
    expect(formatEpochMs(-5)).toBe('-');
    expect(formatEpochMs(Number.NaN)).toBe('-');
  });
});

describe('summarizeRecord', () => {
  it('renders "-" for null, undefined, and empty records', () => {
    expect(summarizeRecord(null)).toBe('-');
    expect(summarizeRecord(undefined)).toBe('-');
    expect(summarizeRecord({})).toBe('-');
  });

  it('renders key: value pairs on one line', () => {
    expect(summarizeRecord({ devicePath: '/dev/video0', gain: 4 })).toBe(
      'devicePath: /dev/video0, gain: 4'
    );
  });

  it('JSON-stringifies nested object values', () => {
    expect(summarizeRecord({ nested: { a: 1 } })).toBe('nested: {"a":1}');
  });
});

describe('summarizeCapabilities', () => {
  it('renders "-" for missing or empty capabilities', () => {
    expect(summarizeCapabilities(null)).toBe('-');
    expect(summarizeCapabilities({})).toBe('-');
  });

  it('renders format names with their resolutions', () => {
    expect(
      summarizeCapabilities({
        formats: [{ pixelFormat: 'YUYV', resolutions: [[1920, 1080], [1280, 720]] }],
      })
    ).toBe('YUYV (1920x1080, 1280x720)');
  });

  it('elides beyond three resolutions and marks truncated capability sets', () => {
    expect(
      summarizeCapabilities({
        formats: [
          { pixelFormat: 'MJPG', resolutions: [[1, 1], [2, 2], [3, 3], [4, 4]] },
        ],
        capabilitiesTruncated: true,
      })
    ).toBe('MJPG (1x1, 2x2, 3x3, …) (truncated)');
  });

  it('falls back to generic record rendering without a formats array', () => {
    expect(summarizeCapabilities({ driver: 'uvcvideo' })).toBe('driver: uvcvideo');
  });
});

describe('isDiscoveryManaged', () => {
  it('is true only for origin edge-discovered', () => {
    expect(isDiscoveryManaged(DISCOVERED_CAMERA)).toBe(true);
    expect(isDiscoveryManaged(PORTAL_CAMERA)).toBe(false);
    expect(isDiscoveryManaged(FAILED_CAMERA)).toBe(false);
  });
});

describe('isDeviceDisconnected', () => {
  it('treats DISCONNECTED, OFFLINE, and UNHEALTHY as disconnected, case-insensitively', () => {
    expect(isDeviceDisconnected('DISCONNECTED')).toBe(true);
    expect(isDeviceDisconnected('offline')).toBe(true);
    expect(isDeviceDisconnected('Unhealthy')).toBe(true);
  });

  it('treats healthy and missing statuses as connected', () => {
    expect(isDeviceDisconnected('HEALTHY')).toBe(false);
    expect(isDeviceDisconnected(undefined)).toBe(false);
    expect(isDeviceDisconnected(null)).toBe(false);
    expect(isDeviceDisconnected('')).toBe(false);
  });
});

describe('parseParamsInput', () => {
  it('yields an empty record for empty text', () => {
    expect(parseParamsInput('')).toEqual({ params: {} });
    expect(parseParamsInput('   ')).toEqual({ params: {} });
  });

  it('parses a JSON object', () => {
    expect(parseParamsInput('{"devicePath": "/dev/video0"}')).toEqual({
      params: { devicePath: '/dev/video0' },
    });
  });

  it('rejects non-object JSON and invalid JSON with an error', () => {
    expect(parseParamsInput('[1, 2]').error).toBe('Parameters must be a JSON object');
    expect(parseParamsInput('"text"').error).toBe('Parameters must be a JSON object');
    expect(parseParamsInput('null').error).toBe('Parameters must be a JSON object');
    expect(parseParamsInput('{not json').error).toBe('Parameters must be valid JSON');
  });
});

describe('summarizeConflictVersion', () => {
  it('renders "-" for missing or empty versions', () => {
    expect(summarizeConflictVersion(null)).toBe('-');
    expect(summarizeConflictVersion({})).toBe('-');
  });

  it('summarizes op, name, type, and params', () => {
    expect(
      summarizeConflictVersion({
        op: 'update',
        name: 'Cam A',
        type: 'Camera',
        params: { devicePath: '/dev/video1' },
      })
    ).toBe('op: update, name: Cam A, type: Camera, devicePath: /dev/video1');
  });

  it('falls back to generic record rendering without known fields', () => {
    expect(summarizeConflictVersion({ other: 'value' })).toBe('other: value');
  });
});

// --------------------------------------------------------------------------
// Field display (Req 1.3)
// --------------------------------------------------------------------------

describe('DeviceCamerasTab field display', () => {
  it('displays name, type, params, capabilities, origin, sync status, and last-reported', async () => {
    getDeviceCameras.mockResolvedValue(
      camerasResponse({ cameras: [PORTAL_CAMERA, DISCOVERED_CAMERA, FAILED_CAMERA] })
    );
    renderTab();
    await waitForLoaded();

    // Names
    expect(screen.getByText('Line 1 inspection cam')).toBeInTheDocument();
    expect(screen.getByText('USB 2.0 Camera')).toBeInTheDocument();
    expect(screen.getByText('RTSP feed')).toBeInTheDocument();

    // Params and capabilities summaries
    expect(screen.getByText('devicePath: /dev/video0, gain: 4')).toBeInTheDocument();
    expect(screen.getByText('url: rtsp://example/stream')).toBeInTheDocument();
    expect(screen.getByText('YUYV (1920x1080, 1280x720)')).toBeInTheDocument();

    // Origin badges
    expect(screen.getByText('Portal-created')).toBeInTheDocument();
    expect(screen.getByText('Discovery-managed')).toBeInTheDocument();
    expect(screen.getByText('edge-configured')).toBeInTheDocument();

    // Sync status including the failure reason (Req 5.4 display)
    expect(screen.getAllByText('Synced').length).toBeGreaterThan(0);
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(
      screen.getByText('schema validation rejected the configuration')
    ).toBeInTheDocument();

    // Last-reported timestamp rendered per row
    expect(
      screen.getAllByText(new Date(LAST_REPORTED_MS).toLocaleString()).length
    ).toBeGreaterThan(0);
  });
});

// --------------------------------------------------------------------------
// Stale / absent / disconnected / never-synced rendering (Reqs 4.1, 4.4, 4.2, 1.6)
// --------------------------------------------------------------------------

describe('DeviceCamerasTab state rendering', () => {
  it('shows a stale badge for stale camera sources (Req 4.1)', async () => {
    getDeviceCameras.mockResolvedValue(
      camerasResponse({ cameras: [{ ...PORTAL_CAMERA, stale: true }] })
    );
    renderTab();
    await waitForLoaded();
    expect(screen.getByText('Stale')).toBeInTheDocument();
  });

  it('does not show a stale badge for fresh camera sources', async () => {
    renderTab();
    await waitForLoaded();
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
  });

  it('shows an absent badge with the absence timestamp (Req 4.4)', async () => {
    getDeviceCameras.mockResolvedValue(
      camerasResponse({
        cameras: [{ ...DISCOVERED_CAMERA, absent: true, absent_since: ABSENT_SINCE_MS }],
      })
    );
    renderTab();
    await waitForLoaded();
    expect(
      screen.getByText(`Absent since ${new Date(ABSENT_SINCE_MS).toLocaleString()}`)
    ).toBeInTheDocument();
  });

  it('indicates disconnected device status alongside the inventory (Req 4.2)', async () => {
    getDeviceCameras.mockResolvedValue(camerasResponse({ device_status: 'DISCONNECTED' }));
    renderTab();
    await waitForLoaded();
    expect(screen.getByTestId('device-disconnected-indicator')).toBeInTheDocument();
  });

  it('shows no disconnected indicator for a healthy device', async () => {
    renderTab();
    await waitForLoaded();
    expect(screen.queryByTestId('device-disconnected-indicator')).not.toBeInTheDocument();
  });

  it('renders the explicit never-synced state instead of a bare empty list (Req 1.6)', async () => {
    getDeviceCameras.mockResolvedValue(
      camerasResponse({ state: 'never-synced', never_synced: true, cameras: [], last_report_at: null })
    );
    renderTab();
    await waitForLoaded();
    expect(screen.getByTestId('never-synced-state')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Never synced — no camera inventory has been reported by this device yet'
      )
    ).toBeInTheDocument();
  });

  it('does not render the never-synced state for a synced device', async () => {
    renderTab();
    await waitForLoaded();
    expect(screen.queryByTestId('never-synced-state')).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Discovery-managed edit blocking (Req 5.6)
// --------------------------------------------------------------------------

describe('DeviceCamerasTab discovery-managed edit blocking', () => {
  it('disables Edit and Delete for discovery-managed rows and enables them for portal-managed rows', async () => {
    getDeviceCameras.mockResolvedValue(
      camerasResponse({ cameras: [PORTAL_CAMERA, DISCOVERED_CAMERA] })
    );
    const { container } = renderTab();
    await waitForLoaded();

    const editButton = screen.getByTestId('edit-camera-button');
    const deleteButton = screen.getByTestId('delete-camera-button');

    // No selection: both disabled
    expect(editButton).toBeDisabled();
    expect(deleteButton).toBeDisabled();

    const table = createWrapper(container).findTable('[data-testid="device-cameras-table"]')!;

    // Select the discovery-managed row (row 2): still disabled (Req 5.6)
    table.findRowSelectionArea(2)!.click();
    await waitFor(() => {
      expect(editButton).toBeDisabled();
    });
    expect(deleteButton).toBeDisabled();

    // Select the portal-managed row (row 1): enabled
    table.findRowSelectionArea(1)!.click();
    await waitFor(() => {
      expect(editButton).not.toBeDisabled();
    });
    expect(deleteButton).not.toBeDisabled();
  });
});

// --------------------------------------------------------------------------
// Conflict list and re-apply flow (Reqs 6.3, 6.4)
// --------------------------------------------------------------------------

describe('DeviceCamerasTab conflicts', () => {
  it('renders conflict events with both versions and the resolution (Req 6.3)', async () => {
    getDeviceCameraConflicts.mockResolvedValue(conflictsResponse([CONFLICT]));
    renderTab();
    await waitForLoaded();

    expect(screen.getByTestId('camera-conflicts-table')).toBeInTheDocument();
    expect(screen.getByText('Edge retained')).toBeInTheDocument();
    expect(
      screen.getByText('op: update, name: Edge name, devicePath: /dev/video0')
    ).toBeInTheDocument();
    expect(
      screen.getByText('op: update, name: Portal name, devicePath: /dev/video9')
    ).toBeInTheDocument();
    expect(screen.getByText(new Date(CONFLICT_AT_MS).toLocaleString())).toBeInTheDocument();
  });

  it('re-applies the overridden portal version and reloads (Req 6.4)', async () => {
    getDeviceCameraConflicts.mockResolvedValue(conflictsResponse([CONFLICT]));
    reapplyCameraConflict.mockResolvedValue({
      device_id: DEVICE_ID,
      camera_source_id: 'cfg-1',
      sync_status: 'pending',
      portal_change_id: 'pc-1',
      conflict_id: 'conflict-1',
    });
    renderTab();
    await waitForLoaded();

    fireEvent.click(screen.getByText('Re-apply portal version'));

    await waitFor(() => {
      expect(reapplyCameraConflict).toHaveBeenCalledWith(DEVICE_ID, 'conflict-1', USECASE_ID);
    });
    // The view reloads cameras and conflicts after the re-apply
    await waitFor(() => {
      expect(getDeviceCameras).toHaveBeenCalledTimes(2);
    });
    expect(getDeviceCameraConflicts).toHaveBeenCalledTimes(2);
  });

  it('marks already re-applied conflicts instead of offering the action', async () => {
    getDeviceCameraConflicts.mockResolvedValue(
      conflictsResponse([{ ...CONFLICT, reapplied_as: 'pc-9' }])
    );
    renderTab();
    await waitForLoaded();

    expect(screen.getByText('Re-applied')).toBeInTheDocument();
    expect(screen.queryByText('Re-apply portal version')).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Static image camera panel (cloud-static-camera-provisioning task 9.3)
// --------------------------------------------------------------------------

describe('canManageDeviceCameras', () => {
  it('is true exactly for the roles holding the device-mutation permission', () => {
    // Mirrors the backend RBAC grants (manage_devices: Operator,
    // UseCaseAdmin, plus the PortalAdmin super user).
    expect(canManageDeviceCameras('Operator')).toBe(true);
    expect(canManageDeviceCameras('UseCaseAdmin')).toBe(true);
    expect(canManageDeviceCameras('PortalAdmin')).toBe(true);
    expect(canManageDeviceCameras('DataScientist')).toBe(false);
    expect(canManageDeviceCameras('Viewer')).toBe(false);
    expect(canManageDeviceCameras('DataLabeler')).toBe(false);
    expect(canManageDeviceCameras(undefined)).toBe(false);
    expect(canManageDeviceCameras(null)).toBe(false);
  });
});

describe('DeviceCamerasTab static image panel states', () => {
  it('renders the no-request state for a device with zero pin requests (Reqs 1.10, 4.7)', async () => {
    renderTab();
    await waitForLoaded();

    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
    expect(screen.getByTestId('static-image-no-request')).toBeInTheDocument();
    expect(screen.queryByTestId('static-image-status')).not.toBeInTheDocument();
  });

  it('renders a pending request with the connectivity hint (Reqs 4.4, 4.5)', async () => {
    getStaticImagePinStatus.mockResolvedValue(
      pinStatusResponse({
        latest: {
          pinRequestId: 'req-1',
          op: 'pin',
          status: 'pending',
          createdAt: PIN_CREATED_AT_MS,
        },
        noPinRequest: false,
        connectivity: 'disconnected',
      })
    );
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByText('Pin pending')).toBeInTheDocument();
    });
    const hint = screen.getByTestId('static-image-connectivity-hint');
    expect(hint.textContent).toContain('currently disconnected');
    expect(screen.queryByTestId('static-image-no-request')).not.toBeInTheDocument();
  });

  it('renders an applied pin with the device-reported state and metadata (Reqs 1.7, 4.6)', async () => {
    getStaticImagePinStatus.mockResolvedValue(
      pinStatusResponse({
        latest: {
          pinRequestId: 'req-2',
          op: 'pin',
          status: 'applied',
          createdAt: PIN_CREATED_AT_MS,
          completedAt: PIN_CREATED_AT_MS + 5000,
          deviceMetadata: {
            width: 1920,
            height: 1080,
            format: 'PNG',
            fileName: 'golden-sample.png',
          },
        },
        noPinRequest: false,
        deviceReported: { present: true, absent: false },
        deviceMetadata: {
          width: 1920,
          height: 1080,
          format: 'PNG',
          fileName: 'golden-sample.png',
        },
      })
    );
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByText('Pin applied')).toBeInTheDocument();
    });
    expect(screen.getByText('Device reports a pinned image')).toBeInTheDocument();
    const metadata = screen.getByTestId('static-image-metadata');
    expect(metadata.textContent).toContain('1920 px');
    expect(metadata.textContent).toContain('1080 px');
    expect(metadata.textContent).toContain('PNG');
    expect(metadata.textContent).toContain('golden-sample.png');
    expect(screen.queryByTestId('static-image-connectivity-hint')).not.toBeInTheDocument();
  });

  it('renders a failed request with the device-reported failure reason (Req 4.3 display)', async () => {
    getStaticImagePinStatus.mockResolvedValue(
      pinStatusResponse({
        latest: {
          pinRequestId: 'req-3',
          op: 'pin',
          status: 'failed',
          createdAt: PIN_CREATED_AT_MS,
          failureReason: 'checksum mismatch',
        },
        noPinRequest: false,
      })
    );
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByText('Pin failed')).toBeInTheDocument();
    });
    expect(screen.getByTestId('static-image-failure-reason').textContent).toContain(
      'checksum mismatch'
    );
  });
});

describe('DeviceCamerasTab static image panel permission gating', () => {
  it('hides the pin/replace/remove actions without the device-mutation permission', async () => {
    authState.role = 'Viewer' satisfies UserRole;
    renderTab();
    await waitForLoaded();

    // Status is still visible (view permission), actions are not.
    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('static-image-pin-button')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-remove-button')).not.toBeInTheDocument();
  });

  it('shows the actions for a role holding the device-mutation permission', async () => {
    authState.role = 'Operator' satisfies UserRole;
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-image-pin-button')).toBeInTheDocument();
    });
    expect(screen.getByTestId('static-image-remove-button')).toBeInTheDocument();
  });
});

describe('DeviceCamerasTab static image pin/replace/remove flows', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    authState.role = 'Operator';
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({ ok: true, status: 200 });
    getStaticImageUploadUrl.mockResolvedValue({
      deviceId: DEVICE_ID,
      uploadUrl: 'https://upload.example/staging-put',
      stagingKey: 'static-image-pins/staging/u-1',
      bucket: 'dda-component-bucket',
      expiresInSeconds: 900,
    });
    pinStaticImage.mockResolvedValue({
      pinRequestId: 'req-9',
      deviceId: DEVICE_ID,
      status: 'pending',
    });
    removeStaticImagePin.mockResolvedValue({
      pinRequestId: 'req-10',
      deviceId: DEVICE_ID,
      status: 'pending',
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('pins an image: upload-url, presigned PUT, then the pin submit (Reqs 1.1, 1.5)', async () => {
    const { container } = renderTab();
    await waitForLoaded();

    const file = new File(['png-bytes'], 'sample.png', { type: 'image/png' });
    const upload = createWrapper(container).findFileUpload()!;
    fireEvent.change(upload.findNativeInput().getElement(), {
      target: { files: [file] },
    });

    const pinButton = screen.getByTestId('static-image-pin-button');
    await waitFor(() => expect(pinButton).not.toBeDisabled());
    fireEvent.click(pinButton);

    await waitFor(() => {
      expect(pinStaticImage).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID, {
        stagingKey: 'static-image-pins/staging/u-1',
        fileName: 'sample.png',
      });
    });
    expect(getStaticImageUploadUrl).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    // The image bytes went to the presigned URL, not the API.
    expect(fetchMock).toHaveBeenCalledWith('https://upload.example/staging-put', {
      method: 'PUT',
      body: file,
    });
    // The panel refreshes the provisioning status after the submit.
    await waitFor(() => {
      expect(getStaticImagePinStatus.mock.calls.length).toBeGreaterThan(1);
    });
  });

  it('labels the pin action as replace while the device reports a pinned image (Req 7.1)', async () => {
    getStaticImagePinStatus.mockResolvedValue(
      pinStatusResponse({
        latest: {
          pinRequestId: 'req-2',
          op: 'pin',
          status: 'applied',
          createdAt: PIN_CREATED_AT_MS,
        },
        noPinRequest: false,
        deviceReported: { present: true, absent: false },
      })
    );
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-image-pin-button').textContent).toContain(
        'Replace image'
      );
    });
  });

  it('removes the pinned image after confirmation (Req 7.2)', async () => {
    renderTab();
    await waitForLoaded();

    await waitFor(() => {
      expect(screen.getByTestId('static-image-remove-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('static-image-remove-button'));
    fireEvent.click(screen.getByTestId('static-image-remove-confirm'));

    await waitFor(() => {
      expect(removeStaticImagePin).toHaveBeenCalledWith(DEVICE_ID, USECASE_ID);
    });
    await waitFor(() => {
      expect(getStaticImagePinStatus.mock.calls.length).toBeGreaterThan(1);
    });
  });
});
