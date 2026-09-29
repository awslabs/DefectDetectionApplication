/**
 * Tests for the device page's stream camera support panel
 * (rtsp-rtmp-stream-cameras task 11.7 — Requirement 16.5): the view of a
 * Device_Stream_Capabilities report, its rendering, and the DeviceDetail
 * wiring that shows it only once the device has reported one.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import DeviceStreamCapabilitiesPanel, {
  isStreamCapabilities,
  summarizeStreamCapabilities,
} from './DeviceStreamCapabilitiesPanel';
import type { DeviceStreamCapabilities } from '../pages/workflows/cameraReference';

const { getDevice, getDeviceCameras } = vi.hoisted(() => ({
  getDevice: vi.fn(),
  getDeviceCameras: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice, getDeviceCameras },
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
  useSearchParams: () => [new URLSearchParams('usecase_id=usecase-1'), vi.fn()],
}));

// The device page's other tabs pull heavy dependencies; this file tests
// the Overview only.
vi.mock('./DeviceCamerasTab', () => ({ default: () => null }));
vi.mock('./LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('./RemoteAccessTab', () => ({ default: () => null }));
vi.mock('./ResultsTab', () => ({ default: () => null }));

const JP7_REPORT: DeviceStreamCapabilities = {
  rtsp: true,
  rtmp: true,
  tls: false,
  codecs: {
    h265: { software: 'avdec_h265' },
    h264: { hardware: 'nvv4l2decoder', software: 'avdec_h264' },
  },
  gstreamer: '1.24.2',
  pyav: '14.2.0',
  ffmpeg: '7.1',
  probedAtMs: 1790000000000,
};

afterEach(() => {
  vi.clearAllMocks();
});

// --------------------------------------------------------------------------
// The report's view
// --------------------------------------------------------------------------

describe('summarizeStreamCapabilities', () => {
  it('reads the protocols, the codecs sorted by name with their decoders, and the versions', () => {
    expect(summarizeStreamCapabilities(JP7_REPORT)).toEqual({
      protocols: [
        { label: 'RTSP', supported: true },
        { label: 'RTMP', supported: true },
        { label: 'TLS (rtsps and rtmps)', supported: false },
      ],
      codecs: [
        { name: 'h264', label: 'H.264', hardware: 'nvv4l2decoder', software: 'avdec_h264' },
        { name: 'h265', label: 'H.265', hardware: null, software: 'avdec_h265' },
      ],
      versions: [
        { label: 'GStreamer', value: '1.24.2' },
        { label: 'PyAV', value: '14.2.0' },
        { label: 'FFmpeg', value: '7.1' },
      ],
      probedAtMs: 1790000000000,
    });
  });

  it('reads malformed values as not reported instead of throwing', () => {
    const malformed = {
      rtsp: 'yes',
      rtmp: null,
      codecs: { h264: 'nvv4l2decoder', constructor: { hardware: '', software: 7 } },
      gstreamer: 5,
      pyav: '',
      probedAtMs: -1,
    } as unknown as DeviceStreamCapabilities;
    expect(summarizeStreamCapabilities(malformed)).toEqual({
      protocols: [
        { label: 'RTSP', supported: null },
        { label: 'RTMP', supported: null },
        { label: 'TLS (rtsps and rtmps)', supported: null },
      ],
      codecs: [
        // A codec name is shown as written; a non-object entry has no path.
        { name: 'constructor', label: 'constructor', hardware: null, software: null },
        { name: 'h264', label: 'H.264', hardware: null, software: null },
      ],
      versions: [
        { label: 'GStreamer', value: null },
        { label: 'PyAV', value: null },
        { label: 'FFmpeg', value: null },
      ],
      probedAtMs: null,
    });
    expect(
      summarizeStreamCapabilities({ codecs: ['h264'] } as unknown as DeviceStreamCapabilities)
        .codecs
    ).toEqual([]);
  });

  it('accepts only an object as a report', () => {
    for (const value of [undefined, null, 'rtsp', 3, ['rtsp'], true]) {
      expect(isStreamCapabilities(value)).toBe(false);
    }
    expect(isStreamCapabilities({})).toBe(true);
    expect(isStreamCapabilities(JP7_REPORT)).toBe(true);
  });
});

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

describe('DeviceStreamCapabilitiesPanel', () => {
  it('shows the protocols, the decoder per codec and path, the versions, and the probe time', () => {
    render(<DeviceStreamCapabilitiesPanel capabilities={JP7_REPORT} />);
    const panel = screen.getByTestId('device-stream-capabilities');
    expect(within(panel).getByText('Stream camera support')).toBeInTheDocument();
    expect(within(panel).getAllByText('Supported')).toHaveLength(2);
    expect(within(panel).getByText('Not supported')).toBeInTheDocument();
    expect(within(panel).getByText('nvv4l2decoder')).toBeInTheDocument();
    expect(within(panel).getByText('avdec_h265')).toBeInTheDocument();
    // H.265 has no hardware path on this device.
    expect(within(panel).getByText('Not available')).toBeInTheDocument();
    expect(panel.textContent).toContain('1.24.2');
    expect(panel.textContent).toContain(new Date(1790000000000).toLocaleString());
  });

  it('says stream cameras cannot run when no codec is decodable', () => {
    render(<DeviceStreamCapabilitiesPanel capabilities={{ rtsp: true, rtmp: false, codecs: {} }} />);
    expect(
      screen.getByText(
        'The device reported no codec it can decode, so stream cameras cannot run on it.'
      )
    ).toBeInTheDocument();
    expect(screen.getAllByText('Not reported').length).toBeGreaterThan(0);
  });
});

// --------------------------------------------------------------------------
// DeviceDetail wiring
// --------------------------------------------------------------------------

describe('the device page', () => {
  const device = {
    device_id: 'jetson-thor1',
    usecase_id: 'usecase-1',
    thing_name: 'jetson-thor1',
    status: 'HEALTHY',
    installed_components: [],
  };

  async function renderPage() {
    const { default: DeviceDetail } = await import('../pages/DeviceDetail');
    render(<DeviceDetail />);
    await waitFor(() => expect(screen.getByText('Device Information')).toBeInTheDocument());
    await waitFor(() =>
      expect(getDeviceCameras).toHaveBeenCalledWith('jetson-thor1', 'usecase-1')
    );
  }

  it('shows the panel once the device has reported its stream capabilities', async () => {
    getDevice.mockResolvedValue({ device });
    getDeviceCameras.mockResolvedValue({ cameras: [], stream_capabilities: JP7_REPORT });
    await renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('device-stream-capabilities')).toBeInTheDocument()
    );
  });

  it('shows no panel for a device that has not reported any', async () => {
    getDevice.mockResolvedValue({ device });
    getDeviceCameras.mockResolvedValue({ cameras: [] });
    await renderPage();
    expect(screen.queryByTestId('device-stream-capabilities')).toBeNull();
  });

  it('keeps the rest of the page when the camera registry read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    getDevice.mockResolvedValue({ device });
    getDeviceCameras.mockRejectedValue(new Error('registry unavailable'));
    await renderPage();
    expect(screen.queryByTestId('device-stream-capabilities')).toBeNull();
    expect(screen.getByText('Device Information')).toBeInTheDocument();
    warn.mockRestore();
  });
});
