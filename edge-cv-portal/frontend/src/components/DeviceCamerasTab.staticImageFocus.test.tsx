/**
 * Bug condition exploration test for the static-image pin panel
 * discoverability dead end
 * (static-image-camera-binding-and-pin-discoverability task 1, Defect 2).
 *
 * **Property 1: Bug Condition / Fix Checking (portal frontend, part 2)**
 *
 * WHEN the device Cameras tab is reached through the node panel's
 * "Pin a static test image…" shortcut THEN the "Static image camera"
 * panel SHALL be brought into view and visually flagged, so the pin
 * controls are the obvious next action on arrival — instead of sitting
 * off-screen below the 9-row cameras table `jetson-thor1` reports.
 *
 * **Validates: Requirements 2.5, 2.6**
 *
 * **CRITICAL — exploration test**: this file MUST FAIL on the unfixed
 * code. `DeviceCamerasTab` takes no focus prop today, never scrolls, and
 * renders no arrival flag. The failures ARE the reproduction of the bug;
 * the same assertions validate the fix once task 3.2 lands.
 *
 * The mock scaffolding mirrors `DeviceCamerasTab.test.tsx` (mocked
 * `getStaticImagePinStatus` / `getStaticImageUploadUrl` / `pinStaticImage`
 * / `removeStaticImagePin`, the `pinStatusResponse` fixture) with a
 * 9-camera `camerasResponse` mirroring the live `jetson-thor1` inventory
 * — the two duplicate static-image registrations plus the healthy Aravis
 * Fake camera and six other rows.
 *
 * ## Fix contract declared by this test (task 3.2)
 *
 * - `DeviceCamerasTab` takes an OPTIONAL `focusStaticImage?: boolean`
 *   prop (plumbed as a prop, not a router hook, so the existing suite
 *   keeps rendering the component without a Router).
 * - When it is set, the component scrolls the element wrapping the
 *   `static-image-panel` container into view once loading has resolved.
 * - It also renders an arrival flag inside the panel carrying
 *   `data-testid="static-image-focus-flag"`.
 * - `DeviceDetail.tsx` sets the prop from the `focus=static-image` query
 *   parameter the shortcut opens (see
 *   `src/pages/workflows/NodeConfigPanel.pinShortcut.test.tsx`).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ComponentType } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import DeviceCamerasTab, {
  canManageDeviceCameras,
  DEVICE_MUTATION_ROLES,
} from './DeviceCamerasTab';
import type { UserRole } from '../types';
import type {
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

// The tab reads only `user?.role` from the auth context.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: authState.role ? { role: authState.role } : null,
  }),
}));

// --------------------------------------------------------------------------
// Fixtures — the live jetson-thor1 inventory (9 cameras)
// --------------------------------------------------------------------------

const DEVICE_ID = 'jetson-thor1';
const USECASE_ID = 'usecase-1';
const LAST_REPORTED_MS = 1788839000000;
const STATIC_ABSENT_SINCE_MS = 1788839397466;

/**
 * The nine Camera_Registry rows `GET /devices/jetson-thor1/cameras`
 * returns. Rows 1-2 are the duplicate registrations of the one virtual
 * static camera (Defect 3, device-side and out of scope here); row 3 is
 * the healthy Aravis Fake camera; the rest are ordinary discovered and
 * configured sources. Nine rows is what pushes the static-image panel,
 * rendered BELOW the table, off-screen on arrival.
 */
const NINE_CAMERAS: CameraSourceEntry[] = [
  {
    camera_source_id: 'arv-6c84191b7fe6',
    name: 'AWS-DDA Static Image Camera',
    type: 'AravisDiscovered',
    params: {
      cameraId: 'static-image-camera',
      address: 'internal',
      protocol: 'StaticImage',
      serial: 'STATIC-IMAGE-0',
    },
    origin: 'edge-discovered',
    sync_status: 'synced',
    stale: false,
    absent: true,
    absent_since: STATIC_ABSENT_SINCE_MS,
    last_reported_at: LAST_REPORTED_MS,
  },
  {
    camera_source_id: 'static-image-camera',
    name: 'Static Image Camera',
    type: 'StaticImage',
    params: {},
    capabilities: {
      staticImage: {
        id: 'static-image-camera',
        model: 'Static Image Camera',
        address: 'internal',
        physicalId: 'static-image-camera',
        protocol: 'StaticImage',
        serial: 'STATIC-IMAGE-0',
        vendor: 'AWS-DDA',
      },
    },
    origin: 'edge-discovered',
    sync_status: 'synced',
    stale: false,
    absent: true,
    absent_since: STATIC_ABSENT_SINCE_MS,
    last_reported_at: LAST_REPORTED_MS,
  },
  {
    camera_source_id: 'arv-c9dd20f60ee1',
    name: 'Aravis Fake',
    type: 'AravisDiscovered',
    params: { cameraId: 'Fake_1', serial: 'GV01', protocol: 'GigEVision' },
    origin: 'edge-discovered',
    sync_status: 'synced',
    stale: false,
    absent: false,
    last_reported_at: LAST_REPORTED_MS,
  },
  ...Array.from({ length: 6 }, (_, index) => ({
    camera_source_id: `disc-v4l2-${index}`,
    name: `USB camera ${index}`,
    type: 'V4L2Discovered',
    params: { devicePath: `/dev/video${index}` },
    capabilities: { formats: [{ pixelFormat: 'MJPG', resolutions: [[640, 480]] }] },
    origin: 'edge-discovered',
    sync_status: 'synced',
    stale: false,
    absent: false,
    last_reported_at: LAST_REPORTED_MS,
  })),
];

function camerasResponse(overrides: Partial<DeviceCamerasResponse> = {}): DeviceCamerasResponse {
  return {
    device_id: DEVICE_ID,
    usecase_id: USECASE_ID,
    state: 'synced',
    last_report_at: LAST_REPORTED_MS,
    staleness_threshold_hours: 24,
    device_status: 'HEALTHY',
    cameras: NINE_CAMERAS,
    count: NINE_CAMERAS.length,
    ...overrides,
  };
}

function conflictsResponse(): DeviceCameraConflictsResponse {
  return { device_id: DEVICE_ID, usecase_id: USECASE_ID, conflicts: [], count: 0 };
}

/** Provisioning status fixture; defaults to the no-request state. */
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

// --------------------------------------------------------------------------
// Render helper carrying the focus prop the fix introduces
// --------------------------------------------------------------------------

interface FocusableTabProps {
  deviceId: string;
  usecaseId: string;
  /** Set on arrival through the pin shortcut (task 3.2's new prop). */
  focusStaticImage?: boolean;
}

/**
 * The component under test, widened to the props the fix introduces.
 * The cast keeps the type check clean while the prop does not exist yet;
 * it stays valid once task 3.2 declares it.
 */
const FocusableDeviceCamerasTab =
  DeviceCamerasTab as unknown as ComponentType<FocusableTabProps>;

/**
 * jsdom does not implement `scrollIntoView`, so the prototype method is
 * installed as the spy. `this` is recorded per call so the scroll target
 * can be checked against the static-image panel.
 */
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
  getStaticImagePinStatus.mockResolvedValue(pinStatusResponse());
});

afterEach(() => {
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

async function waitForLoaded() {
  await waitFor(() => {
    expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument();
  });
  // The static image panel resolves its own status fetch.
  await waitFor(() => {
    expect(getStaticImagePinStatus).toHaveBeenCalled();
  });
  await waitFor(() => {
    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
  });
}

// --------------------------------------------------------------------------
// Property 1 (part 2): the pin panel is in view and flagged on arrival
// --------------------------------------------------------------------------

describe('Property 1 (Bug Condition, Defect 2): arriving through the pin shortcut', () => {
  it('reproduces the landing the shortcut lands on: nine camera rows above the pin panel', async () => {
    render(<FocusableDeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForLoaded();

    // The live inventory shape that makes the panel unreachable: nine
    // rows, two of them the one virtual static camera, and the panel
    // rendered after the table in document order.
    const table = screen.getByTestId('device-cameras-table');
    const panel = screen.getByTestId('static-image-panel');
    expect(screen.getByText('AWS-DDA Static Image Camera')).toBeInTheDocument();
    expect(screen.getByText('Static Image Camera')).toBeInTheDocument();
    expect(screen.getByText('Aravis Fake')).toBeInTheDocument();
    expect(
      table.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it('brings the static image panel into view on arrival (Requirement 2.5)', async () => {
    render(
      <FocusableDeviceCamerasTab
        deviceId={DEVICE_ID}
        usecaseId={USECASE_ID}
        focusStaticImage
      />
    );
    await waitForLoaded();

    const panel = screen.getByTestId('static-image-panel');

    // The panel (or the wrapper holding it) is scrolled into view once
    // loading has resolved.
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalled();
    });
    expect(
      scrollTargets.some((target) => target === panel || target.contains(panel))
    ).toBe(true);
  });

  it('flags the panel as the arrival target (Requirements 2.5, 2.6)', async () => {
    render(
      <FocusableDeviceCamerasTab
        deviceId={DEVICE_ID}
        usecaseId={USECASE_ID}
        focusStaticImage
      />
    );
    await waitForLoaded();

    // A visual flag inside the panel makes the pin controls the obvious
    // next action instead of the prominent "Create camera source" button,
    // whose type options exclude StaticImage by design.
    const flag = await waitFor(() => screen.getByTestId('static-image-focus-flag'));
    expect(screen.getByTestId('static-image-panel').contains(flag)).toBe(true);

    // The pin controls themselves are reachable for a role holding the
    // device-mutation permission (Requirement 3.9 unchanged).
    expect(screen.getByTestId('static-image-pin-button')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Property 2 (Preservation): the Cameras tab without the focus flag
// (static-image-camera-binding-and-pin-discoverability task 2, Defect 2)
// --------------------------------------------------------------------------

/**
 * **Property 2: Preservation Checking (portal frontend, part 2)**
 *
 * ```pascal
 * FOR ALL X WHERE NOT isBugCondition(X) DO
 *   ASSERT F(X) = F'(X)
 * END FOR
 * ```
 *
 * **Validates: Requirements 3.8, 3.9, 3.10**
 *
 * Every arrival at the Cameras tab that did NOT come through the pin
 * shortcut renders exactly as it does today: no scroll, no arrival flag,
 * the same panel, the same role gate, the same five create-form type
 * options, and the same loading and load-error early returns. These
 * assertions PASS on the UNFIXED code — they are the baseline the focus
 * behavior must not regress, so a fix that scrolled unconditionally or
 * added `StaticImage` to the creatable types fails here.
 *
 * ## Observation-first baseline (recorded against the UNFIXED code)
 *
 * Arriving without the focus flag (`Operator`, nine-camera inventory):
 * - `Element.prototype.scrollIntoView` calls: `0`
 * - `static-image-focus-flag`: absent
 * - `static-image-panel`, `static-image-no-request`, `static-image-pin-button`,
 *   `static-image-remove-button`, `camera-conflicts-table`, `create-camera-button`,
 *   `device-cameras-table`: all present
 *
 * Create-form type options: exactly
 * `["Camera (V4L2)", "NVIDIA CSI", "RTSP", "Folder", "ICam"]`, with
 * `Camera (V4L2)` preselected — no `StaticImage` entry.
 *
 * Role gate: `DEVICE_MUTATION_ROLES` is `["Operator", "UseCaseAdmin", "PortalAdmin"]`;
 * `canManageDeviceCameras` is `true` for those three and `false` for
 * `DataScientist`, `Viewer`, `DataLabeler`, `undefined`, and `null`.
 *
 * Loading early return: `"Loading camera registry..."` present; table,
 * panel absent; `0` scrolls. Load-error early return: the error message
 * and `Retry` present; table, panel, flag absent; `0` scrolls.
 */
describe('Property 2 (Preservation, Defect 2): arriving without the focus flag', () => {
  it('renders the tab as today — no scroll and no arrival flag (Requirement 3.10)', async () => {
    render(<FocusableDeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForLoaded();

    // Recorded baseline: zero scrolls and no arrival flag without the prop.
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(scrollTargets).toHaveLength(0);
    expect(screen.queryByTestId('static-image-focus-flag')).not.toBeInTheDocument();

    // The normal synced render is intact: cameras table, conflicts table,
    // and the static-image panel in its no-request state.
    expect(screen.getByTestId('device-cameras-table')).toBeInTheDocument();
    expect(screen.getByTestId('camera-conflicts-table')).toBeInTheDocument();
    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
    expect(screen.getByTestId('static-image-no-request')).toBeInTheDocument();
    expect(screen.getByTestId('create-camera-button')).toBeInTheDocument();
  });

  it('keeps the explicitly-false flag inert, exactly like an absent one', async () => {
    render(
      <FocusableDeviceCamerasTab
        deviceId={DEVICE_ID}
        usecaseId={USECASE_ID}
        focusStaticImage={false}
      />
    );
    await waitForLoaded();

    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(screen.queryByTestId('static-image-focus-flag')).not.toBeInTheDocument();
    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
  });

  it('offers exactly the five existing create-form types, without StaticImage (Requirement 3.8)', async () => {
    render(<FocusableDeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForLoaded();

    fireEvent.click(screen.getByTestId('create-camera-button'));
    await waitFor(() => {
      expect(screen.getByTestId('camera-form-submit')).toBeInTheDocument();
    });

    // The modal renders in a portal, so wrap the document body.
    const select = createWrapper(document.body).findSelect()!;
    expect(select.findTrigger().getElement().textContent).toBe('Camera (V4L2)');

    select.openDropdown();
    const options = select
      .findDropdown()
      .findOptions()
      .map((option) => option.getElement().textContent);

    // The recorded option list, in order. StaticImage stays absent: the
    // backend rejects manual creation of discovery-managed sources.
    expect(options).toEqual(['Camera (V4L2)', 'NVIDIA CSI', 'RTSP', 'Folder', 'ICam']);
    expect(options).not.toContain('StaticImage');
    expect(options).not.toContain('Static Image Camera');
  });

  it('gates the pin controls on the unchanged device-mutation role set (Requirement 3.9)', () => {
    // Exhaustive over the role domain (`UserRole` plus the two absent
    // cases), so the gate cannot drift without failing here.
    expect(DEVICE_MUTATION_ROLES).toEqual(['Operator', 'UseCaseAdmin', 'PortalAdmin']);

    const granted: UserRole[] = ['Operator', 'UseCaseAdmin', 'PortalAdmin'];
    const denied: UserRole[] = ['DataScientist', 'Viewer', 'DataLabeler'];
    for (const role of granted) {
      expect(canManageDeviceCameras(role)).toBe(true);
      expect(DEVICE_MUTATION_ROLES).toContain(role);
    }
    for (const role of denied) {
      expect(canManageDeviceCameras(role)).toBe(false);
      expect(DEVICE_MUTATION_ROLES).not.toContain(role);
    }
    expect(canManageDeviceCameras(undefined)).toBe(false);
    expect(canManageDeviceCameras(null)).toBe(false);
  });

  it('hides the pin mutations for a role without the permission and shows them with it (Requirement 3.9)', async () => {
    authState.role = 'Viewer' satisfies UserRole;
    const { unmount } = render(
      <FocusableDeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />
    );
    await waitForLoaded();

    // Status stays visible (view permission); the mutations do not.
    expect(screen.getByTestId('static-image-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('static-image-pin-button')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-remove-button')).not.toBeInTheDocument();
    unmount();

    authState.role = 'Operator' satisfies UserRole;
    render(<FocusableDeviceCamerasTab deviceId={DEVICE_ID} usecaseId={USECASE_ID} />);
    await waitForLoaded();

    expect(screen.getByTestId('static-image-pin-button')).toBeInTheDocument();
    expect(screen.getByTestId('static-image-remove-button')).toBeInTheDocument();
  });

  it('keeps the loading early return unchanged (Requirement 3.10)', async () => {
    let resolveCameras: (value: DeviceCamerasResponse) => void = () => {};
    getDeviceCameras.mockReturnValue(
      new Promise<DeviceCamerasResponse>((resolve) => {
        resolveCameras = resolve;
      })
    );

    render(
      <FocusableDeviceCamerasTab
        deviceId={DEVICE_ID}
        usecaseId={USECASE_ID}
        focusStaticImage
      />
    );

    // The spinner block renders and nothing else does — so even the focus
    // arrival has nothing to scroll to until loading resolves.
    expect(screen.getByText('Loading camera registry...')).toBeInTheDocument();
    expect(screen.queryByTestId('device-cameras-table')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-panel')).not.toBeInTheDocument();
    expect(scrollIntoView).not.toHaveBeenCalled();

    resolveCameras(camerasResponse());
    await waitForLoaded();
  });

  it('keeps the load-error early return unchanged (Requirement 3.10)', async () => {
    getDeviceCameras.mockRejectedValue(new Error('registry read failed'));

    render(
      <FocusableDeviceCamerasTab
        deviceId={DEVICE_ID}
        usecaseId={USECASE_ID}
        focusStaticImage
      />
    );

    await waitFor(() => {
      expect(screen.getByText('registry read failed')).toBeInTheDocument();
    });
    expect(screen.getByText('Retry')).toBeInTheDocument();
    expect(screen.queryByTestId('device-cameras-table')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-panel')).not.toBeInTheDocument();
    expect(screen.queryByTestId('static-image-focus-flag')).not.toBeInTheDocument();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
