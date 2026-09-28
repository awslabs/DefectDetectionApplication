/**
 * DeviceDetail reads the `focus` query parameter for both virtual-camera
 * shortcuts (static-camera-video-loop task 9.3, Requirement 9.7):
 * `focus=static-video` sets the Cameras tab's `focusStaticVideo`,
 * `focus=static-image` still sets `focusStaticImage`, and neither sets the
 * other.
 *
 * The harness follows `DeviceDetail.targetArch.test.tsx`; the Cameras tab
 * is replaced by a stub recording the props it receives.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';

import DeviceDetail from './DeviceDetail';

const { getDevice, searchState, cameraTabProps } = vi.hoisted(() => ({
  getDevice: vi.fn(),
  searchState: { query: '' },
  cameraTabProps: [] as Array<Record<string, unknown>>,
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return () => Promise.resolve({});
      },
    }
  );
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ deviceId: 'jetson-thor1' }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(searchState.query), vi.fn()],
}));

vi.mock('../components/DeviceCamerasTab', () => ({
  default: (props: Record<string, unknown>) => {
    cameraTabProps.push(props);
    return null;
  },
}));
vi.mock('../components/LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('../components/RemoteAccessTab', () => ({ default: () => null }));
vi.mock('../components/ResultsTab', () => ({ default: () => null }));

const device = {
  device_id: 'jetson-thor1',
  usecase_id: 'usecase-1',
  thing_name: 'jetson-thor1',
  status: 'HEALTHY',
  installed_components: [],
  test_device: false,
  target_architecture: 'arm64_jp7',
};

async function renderWithQuery(query: string) {
  searchState.query = query;
  getDevice.mockResolvedValue({ device });
  render(<DeviceDetail />);
  await act(async () => {});
  expect(cameraTabProps.length).toBeGreaterThan(0);
  return cameraTabProps[cameraTabProps.length - 1];
}

afterEach(() => {
  vi.clearAllMocks();
  cameraTabProps.length = 0;
});

describe('DeviceDetail focus targets for the Cameras tab', () => {
  it('passes focusStaticVideo for focus=static-video', async () => {
    const props = await renderWithQuery('usecase_id=usecase-1&tab=cameras&focus=static-video');
    expect(props.focusStaticVideo).toBe(true);
    expect(props.focusStaticImage).toBe(false);
    expect(props.deviceId).toBe('jetson-thor1');
    expect(props.usecaseId).toBe('usecase-1');
  });

  it('still passes focusStaticImage for focus=static-image', async () => {
    const props = await renderWithQuery('usecase_id=usecase-1&tab=cameras&focus=static-image');
    expect(props.focusStaticImage).toBe(true);
    expect(props.focusStaticVideo).toBe(false);
  });

  it('passes neither without a focus target', async () => {
    const props = await renderWithQuery('usecase_id=usecase-1&tab=cameras');
    expect(props.focusStaticImage).toBe(false);
    expect(props.focusStaticVideo).toBe(false);
  });
});
