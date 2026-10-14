/**
 * Tests for the DeviceDetail "Deployed models" panel — the portal display
 * leg of model-gpu-fallback-visibility (Property 4 fix checking, design
 * fix-check case 8), driven by the additive `model_status` field the
 * backend reads from the device's `dda-model-status` shadow.
 *
 * Covers:
 * - degraded shadow -> warning alert + red "CPU fallback" badge;
 * - healthy GPU model -> green "GPU" badge (no alert);
 * - CPU-by-design entry -> neutral "CPU" badge;
 * - `model_status` absent/null -> panel NOT rendered, today's DOM (the
 *   task-2 deferred absence-DOM leg lands here).
 *
 * Validates: Requirements 2.5, 3.4
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import DeviceDetail from './DeviceDetail';

const { getDevice } = vi.hoisted(() => ({
  getDevice: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => Promise.resolve({});
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

// The camera/logs/remote-access/results tabs pull heavy dependencies; stub
// them — this test exercises only the Overview tab.
vi.mock('../components/DeviceCamerasTab', () => ({ default: () => null }));
vi.mock('../components/LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('../components/RemoteAccessTab', () => ({ default: () => null }));
vi.mock('../components/ResultsTab', () => ({ default: () => null }));

const baseDevice = {
  device_id: 'jetson-thor1',
  usecase_id: 'usecase-1',
  thing_name: 'jetson-thor1',
  status: 'HEALTHY',
  installed_components: [
    {
      componentName: 'aws.edgeml.dda.LocalServer.arm64JP7',
      componentVersion: '1.0.34',
      lifecycleState: 'RUNNING',
      isRoot: true,
    },
  ],
  test_device: false,
  target_architecture: 'arm64_jp7',
};

// The Aug 14-15 incident shape: every GPU-chain model fell back to CPU.
const degradedStatus = {
  models: {
    yolo_test: {
      status: 'READY',
      runtime: 'onnx',
      gpuRequested: true,
      gpuActive: false,
    },
  },
  gpuDegraded: true,
  gpuChainModels: 1,
  gpuActiveModels: 0,
  updatedAt: '2026-08-16T12:00:00Z',
};

// Healthy GPU model plus a CPU-by-design model — not degraded.
const healthyStatus = {
  models: {
    yolo_test: {
      status: 'READY',
      runtime: 'onnx',
      gpuRequested: true,
      gpuActive: true,
    },
    ocr_cpu: {
      status: 'READY',
      runtime: 'onnx',
      gpuRequested: false,
      gpuActive: false,
    },
  },
  gpuDegraded: false,
  gpuChainModels: 1,
  gpuActiveModels: 1,
  updatedAt: '2026-08-16T12:00:00Z',
};

const wrapper = () => createWrapper(document.body);

async function renderPage(device: Record<string, unknown> = baseDevice) {
  getDevice.mockResolvedValue({ device });
  const rendered = render(<DeviceDetail />);
  await act(async () => {});
  return rendered;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('DeviceDetail — Deployed models panel (model_status)', () => {
  it('renders the degraded warning alert and a red "CPU fallback" badge', async () => {
    await renderPage({ ...baseDevice, model_status: degradedStatus });

    // Panel present with the model row.
    expect(screen.getByText('Deployed models')).not.toBeNull();
    expect(screen.getByText('yolo_test')).not.toBeNull();

    // Device-level degraded alert, type warning, with the spec text. The
    // page also carries an Alert inside the (closed) restart-Greengrass
    // modal, so scope to the alert carrying the degraded header.
    const alert = wrapper()
      .findAllAlerts()
      .find((a) =>
        (a.getElement().textContent ?? '').includes('GPU inference degraded')
      );
    expect(alert).not.toBeUndefined();
    const alertText = alert!.getElement().textContent ?? '';
    expect(alertText).toContain('GPU inference degraded');
    expect(alertText).toContain(
      '1 GPU-chain models loaded, none has an active GPU provider'
    );
    expect(alertText).toContain('models are serving on CPU fallback');
    expect(alert!.getElement().innerHTML).toContain('warning');

    // Red "CPU fallback" badge on the fallen-back model.
    const badge = screen.getByText('CPU fallback');
    expect(badge.className).toContain('red');
  });

  it('renders a green "GPU" badge for a healthy model and a neutral "CPU" badge for a CPU-by-design model, without the alert', async () => {
    await renderPage({ ...baseDevice, model_status: healthyStatus });

    expect(screen.getByText('Deployed models')).not.toBeNull();

    // Healthy GPU-chain model: green "GPU" badge.
    const gpuBadge = screen.getByText('GPU');
    expect(gpuBadge.className).toContain('green');

    // CPU-by-design model: neutral (grey) "CPU" badge — never flagged.
    const cpuBadge = screen.getByText('CPU');
    expect(cpuBadge.className).toContain('grey');
    expect(screen.queryByText('CPU fallback')).toBeNull();

    // No degraded alert on a healthy device (the restart-modal alert is
    // unrelated; assert no alert carries the degraded header).
    expect(screen.queryByText('GPU inference degraded')).toBeNull();
    expect(
      wrapper()
        .findAllAlerts()
        .some((a) =>
          (a.getElement().textContent ?? '').includes('GPU inference degraded')
        )
    ).toBe(false);
  });

  it('does not render the panel when model_status is absent (today\'s DOM)', async () => {
    await renderPage(baseDevice);

    // No panel, no alert, no badges — the deferred task-2 absence leg.
    expect(screen.queryByText('Deployed models')).toBeNull();
    expect(screen.queryByText('GPU inference degraded')).toBeNull();
    expect(screen.queryByText('CPU fallback')).toBeNull();
    // The rest of the Overview tab renders exactly as today.
    expect(screen.getByText('Device Information')).not.toBeNull();
    expect(screen.getAllByText('jetson-thor1').length).toBeGreaterThan(0);
  });

  it('does not render the panel when model_status is null (the backend no-shadow response)', async () => {
    await renderPage({ ...baseDevice, model_status: null });

    expect(screen.queryByText('Deployed models')).toBeNull();
    expect(screen.getByText('Device Information')).not.toBeNull();
  });

  it('does not render the panel when the models map is empty', async () => {
    await renderPage({
      ...baseDevice,
      model_status: { ...healthyStatus, models: {} },
    });

    expect(screen.queryByText('Deployed models')).toBeNull();
    expect(screen.getByText('Device Information')).not.toBeNull();
  });
});
