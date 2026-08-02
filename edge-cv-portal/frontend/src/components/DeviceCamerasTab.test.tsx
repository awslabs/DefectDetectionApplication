/**
 * Component tests for the device detail Cameras tab (camera-registry-sync
 * task 8.2): field display of registry entries (Req 1.3), stale badge
 * (Req 4.1), absent badge with its timestamp (Req 4.4), device
 * disconnected indicator (Req 4.2), explicit never-synced state
 * (Req 1.6), discovery-managed edit/delete blocking (Req 5.6), and the
 * conflict re-apply flow (Req 6.4), plus unit tests for the exported
 * pure helpers.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import DeviceCamerasTab, {
  formatEpochMs,
  summarizeRecord,
  summarizeCapabilities,
  isDiscoveryManaged,
  isDeviceDisconnected,
  parseParamsInput,
  summarizeConflictVersion,
} from './DeviceCamerasTab';
import type {
  CameraConflictEvent,
  CameraSourceEntry,
  DeviceCameraConflictsResponse,
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
} = vi.hoisted(() => ({
  getDeviceCameras: vi.fn(),
  getDeviceCameraConflicts: vi.fn(),
  createDeviceCamera: vi.fn(),
  updateDeviceCamera: vi.fn(),
  deleteDeviceCamera: vi.fn(),
  reapplyCameraConflict: vi.fn(),
  refreshDeviceCameras: vi.fn(),
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
  },
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

function renderTab() {
  return render(<DeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
}

async function waitForLoaded() {
  await waitFor(() => {
    expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  getDeviceCameras.mockResolvedValue(camerasResponse());
  getDeviceCameraConflicts.mockResolvedValue(conflictsResponse());
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
