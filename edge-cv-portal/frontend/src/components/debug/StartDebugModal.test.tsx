/**
 * Unit tests for the "Start debug" modal (workflow-debug-mode task 5.2,
 * Requirements 2.6, 3.4).
 *
 * - defaults: one device, duration 60, level debug, images off;
 * - validation: the duration range 5–1440, a device or "all devices", a
 *   workflow;
 * - the images control disabled with the enforcement note while registry
 *   enforcement is off, enabled when it is on;
 * - the creation body sent for one device and for all devices;
 * - each refusal's remedy rendered: device_not_recorded, no_target_devices,
 *   debug_session_limit, debug_session_exists, debug_images_unavailable.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

const { isRegistryEnforcedMock } = vi.hoisted(() => ({ isRegistryEnforcedMock: vi.fn(() => false) }));

vi.mock('../../config', () => ({
  isRegistryEnforced: isRegistryEnforcedMock,
  getConfig: () => ({ apiUrl: 'http://localhost/api', region: 'us-east-1', branding: {} }),
}));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      listWorkflows: vi.fn(),
      listDevices: vi.fn(),
      createDebugSession: vi.fn(),
    },
  };
});

import { apiService, DebugApiError } from '../../services/api';
import StartDebugModal, {
  DURATION_MESSAGE,
  IMAGES_ENFORCEMENT_NOTE,
  IMAGES_NOTE,
  isIntegerInRange,
  refusalDetails,
} from './StartDebugModal';

const mocked = vi.mocked(apiService);
const body = () => createWrapper(document.body);

const onDismiss = vi.fn();
const onStarted = vi.fn();

async function renderModal(deviceId?: string) {
  render(<StartDebugModal usecaseId="uc-1" deviceId={deviceId} onDismiss={onDismiss} onStarted={onStarted} />);
  await waitFor(() => expect(mocked.listWorkflows).toHaveBeenCalledWith('uc-1'));
  await act(async () => {});
}

function selectOption(testId: string, value: string) {
  const select = body().findSelect(`[data-testid="${testId}"]`)!;
  select.openDropdown();
  select.selectOptionByValue(value);
}

function setInput(testId: string, value: string) {
  body().findInput(`[data-testid="${testId}"]`)!.setInputValue(value);
}

function submitButton(): HTMLButtonElement {
  return screen.getByTestId('start-debug-submit') as HTMLButtonElement;
}

async function submit() {
  await act(async () => {
    fireEvent.click(submitButton());
  });
}

function refusal(status: number, code: string, extra: Record<string, unknown> = {}) {
  const message = `refused with ${code}`;
  return new DebugApiError(message, status, { error: message, error_code: code, ...extra });
}

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  isRegistryEnforcedMock.mockReturnValue(false);
  mocked.listWorkflows.mockResolvedValue({
    workflows: [
      { workflow_id: 'wf-1', name: 'PPE vest check' } as any,
      { workflow_id: 'wf-2', name: 'Casting defects' } as any,
    ],
    count: 2,
  });
  mocked.listDevices.mockResolvedValue({
    devices: [
      { device_id: 'thing-a', target_architecture: 'arm64_jp5' } as any,
      { device_id: 'thing-b', target_architecture: 'x86_64_nvidia' } as any,
    ],
    count: 2,
  });
  mocked.createDebugSession.mockResolvedValue({
    session_id: 'sess-1',
    state: 'active',
    devices: [{ device_id: 'thing-a', shadow: 'ok' }],
    region_source: 'header',
    enabledAt: 1,
    expiresAt: 2,
    logGroup: '/dda/workflow-debug/uc-1',
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('isIntegerInRange', () => {
  it('accepts whole numbers inside the range and nothing else', () => {
    expect(isIntegerInRange('60', 5, 1440)).toBe(true);
    expect(isIntegerInRange('5', 5, 1440)).toBe(true);
    expect(isIntegerInRange('1440', 5, 1440)).toBe(true);
    expect(isIntegerInRange('4', 5, 1440)).toBe(false);
    expect(isIntegerInRange('1441', 5, 1440)).toBe(false);
    expect(isIntegerInRange('60.5', 5, 1440)).toBe(false);
    expect(isIntegerInRange('', 5, 1440)).toBe(false);
    expect(isIntegerInRange('-60', 5, 1440)).toBe(false);
  });
});

describe('StartDebugModal — defaults and validation', () => {
  it('starts with one device, duration 60, level debug and images off', async () => {
    await renderModal();

    const duration = body().findInput('[data-testid="start-debug-duration"]')!;
    expect((duration.findNativeInput().getElement() as HTMLInputElement).value).toBe('60');
    expect(body().findSelect('[data-testid="start-debug-level"]')!.findTrigger().getElement().textContent).toBe(
      'debug'
    );
    const radio = body().findRadioGroup('[data-testid="start-debug-target"]')!;
    expect(radio.findInputByValue('device')!.getElement().checked).toBe(true);
    const images = body().findCheckbox('[data-testid="start-debug-images"]')!;
    expect(images.findNativeInput().getElement().checked).toBe(false);
    // Nothing selected yet: the Start action waits.
    expect(submitButton().disabled).toBe(true);
  });

  it('preselects the device it is opened for', async () => {
    await renderModal('thing-a');

    expect(body().findSelect('[data-testid="start-debug-device"]')!.findTrigger().getElement().textContent).toBe(
      'thing-a'
    );
  });

  it('flags a duration outside 5–1440 and disables Start', async () => {
    await renderModal('thing-a');
    selectOption('start-debug-workflow', 'wf-1');
    await waitFor(() => expect(submitButton().disabled).toBe(false));

    setInput('start-debug-duration', '3');
    expect(screen.getByText(DURATION_MESSAGE)).toBeInTheDocument();
    expect(submitButton().disabled).toBe(true);

    setInput('start-debug-duration', '1441');
    expect(screen.getByText(DURATION_MESSAGE)).toBeInTheDocument();
    expect(submitButton().disabled).toBe(true);

    setInput('start-debug-duration', '1440');
    await waitFor(() => expect(screen.queryByText(DURATION_MESSAGE)).toBeNull());
    expect(submitButton().disabled).toBe(false);
  });

  it('requires a device when "One device" is chosen and none when "All devices" is', async () => {
    await renderModal();
    selectOption('start-debug-workflow', 'wf-1');
    expect(submitButton().disabled).toBe(true);

    body().findRadioGroup('[data-testid="start-debug-target"]')!.findInputByValue('all')!.click();
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    expect(body().findSelect('[data-testid="start-debug-device"]')).toBeNull();
  });
});

describe('StartDebugModal — images and registry enforcement (design §7.2)', () => {
  it('keeps the images control disabled with the enforcement note while enforcement is off', async () => {
    isRegistryEnforcedMock.mockReturnValue(false);
    await renderModal();

    const images = body().findCheckbox('[data-testid="start-debug-images"]')!;
    expect(images.findNativeInput().getElement().disabled).toBe(true);
    expect(images.findNativeInput().getElement().checked).toBe(false);
    expect(screen.getByText(IMAGES_ENFORCEMENT_NOTE)).toBeInTheDocument();
    expect(screen.queryByText(IMAGES_NOTE)).toBeNull();
  });

  it('enables the images control and shows the image size when enforcement is on', async () => {
    isRegistryEnforcedMock.mockReturnValue(true);
    await renderModal('thing-a');
    selectOption('start-debug-workflow', 'wf-1');

    const images = body().findCheckbox('[data-testid="start-debug-images"]')!;
    expect(images.findNativeInput().getElement().disabled).toBe(false);
    expect(screen.getByText(IMAGES_NOTE)).toBeInTheDocument();
    images.findNativeInput().click();
    await waitFor(() => expect(body().findInput('[data-testid="start-debug-image-max-kb"]')).not.toBeNull());
    expect(
      (body().findInput('[data-testid="start-debug-image-max-kb"]')!.findNativeInput().getElement() as HTMLInputElement)
        .value
    ).toBe('150');

    await submit();
    expect(mocked.createDebugSession).toHaveBeenCalledWith('uc-1', {
      workflow_id: 'wf-1',
      device_id: 'thing-a',
      duration_minutes: 60,
      level: 'debug',
      images: true,
      image_max_kb: 150,
    });
  });
});

describe('StartDebugModal — the creation request', () => {
  it('sends one device with the defaults and reports the answer', async () => {
    await renderModal();
    selectOption('start-debug-workflow', 'wf-1');
    selectOption('start-debug-device', 'thing-a');
    await waitFor(() => expect(submitButton().disabled).toBe(false));

    await submit();

    expect(mocked.createDebugSession).toHaveBeenCalledWith('uc-1', {
      workflow_id: 'wf-1',
      device_id: 'thing-a',
      duration_minutes: 60,
      level: 'debug',
      images: false,
    });
    expect(onStarted).toHaveBeenCalledWith(expect.objectContaining({ session_id: 'sess-1', state: 'active' }));
  });

  it('sends all_devices with a chosen duration and level', async () => {
    await renderModal();
    selectOption('start-debug-workflow', 'wf-2');
    body().findRadioGroup('[data-testid="start-debug-target"]')!.findInputByValue('all')!.click();
    setInput('start-debug-duration', '10');
    selectOption('start-debug-level', 'info');
    await waitFor(() => expect(submitButton().disabled).toBe(false));

    await submit();

    expect(mocked.createDebugSession).toHaveBeenCalledWith('uc-1', {
      workflow_id: 'wf-2',
      all_devices: true,
      duration_minutes: 10,
      level: 'info',
      images: false,
    });
  });

  it('Cancel dismisses without a request', async () => {
    await renderModal();
    fireEvent.click(screen.getByTestId('start-debug-cancel'));
    expect(onDismiss).toHaveBeenCalled();
    expect(mocked.createDebugSession).not.toHaveBeenCalled();
  });
});

describe('StartDebugModal — refusals and their remedies (design §7.2)', () => {
  async function refuseWith(error: DebugApiError) {
    mocked.createDebugSession.mockRejectedValue(error);
    await renderModal('thing-a');
    selectOption('start-debug-workflow', 'wf-1');
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    await submit();
    await waitFor(() => expect(screen.getByTestId('start-debug-refusal')).toBeInTheDocument());
    return screen.getByTestId('start-debug-refusal').textContent ?? '';
  }

  it('device_not_recorded: the Device_Record_Remedy', async () => {
    const text = await refuseWith(
      refusal(400, 'device_not_recorded', {
        device_id: 'thing-a',
        remedy: 'A UseCaseAdmin or PortalAdmin sets the device Target architecture on its device page',
      })
    );
    expect(text).toContain('device_not_recorded');
    expect(text).toContain('Device: thing-a.');
    expect(screen.getByTestId('start-debug-remedy').textContent).toContain('A UseCaseAdmin or PortalAdmin sets');
    expect(onStarted).not.toHaveBeenCalled();
  });

  it('no_target_devices: the devices without a deployment or a record', async () => {
    const text = await refuseWith(
      refusal(400, 'no_target_devices', {
        no_deployment: ['thing-c'],
        no_device_record: ['thing-d'],
        remedy: 'Deploy the workflow or record the devices',
      })
    );
    expect(text).toContain('Without a deployment of this workflow: thing-c');
    expect(text).toContain('Without a device record in this use case: thing-d');
    expect(text).toContain('Deploy the workflow or record the devices');
  });

  it('debug_session_limit: the cap and the counts', async () => {
    const text = await refuseWith(
      refusal(409, 'debug_session_limit', { limit: 'usecase', active: 4, max: 4, remedy: 'Stop a session first' })
    );
    expect(text).toContain('Cap reached: sessions per use case (4 of 4).');
    expect(text).toContain('Stop a session first');
  });

  it('debug_session_exists: the live session id', async () => {
    const text = await refuseWith(
      refusal(409, 'debug_session_exists', {
        session_id: 'sess-9',
        device: 'thing-a',
        remedy: 'Stop session sess-9 or wait for it to expire',
      })
    );
    expect(text).toContain('Live session: sess-9 on thing-a.');
    expect(text).toContain('Stop session sess-9 or wait for it to expire');
  });

  it('debug_images_unavailable: the lifecycle script remedy', async () => {
    const text = await refuseWith(
      refusal(409, 'debug_images_unavailable', {
        reason: 'DDATelemetryDebugImages lifecycle rule missing',
        remedy: 'Run scripts/telemetry_lifecycle.py with --apply',
      })
    );
    expect(text).toContain('Reason: DDATelemetryDebugImages lifecycle rule missing.');
    expect(text).toContain('Run scripts/telemetry_lifecycle.py with --apply');
  });

  it('registry_enforcement_required: the enforcement remedy', async () => {
    const text = await refuseWith(
      refusal(409, 'registry_enforcement_required', {
        remedy: 'Turn on registry enforcement once the registry backfill is complete.',
      })
    );
    expect(text).toContain('Turn on registry enforcement once the registry backfill is complete.');
  });

  it('a plain error is shown without a remedy line', async () => {
    mocked.createDebugSession.mockRejectedValue(new Error('network down'));
    await renderModal('thing-a');
    selectOption('start-debug-workflow', 'wf-1');
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    await submit();

    expect(screen.getByTestId('start-debug-refusal').textContent).toContain('network down');
    expect(screen.queryByTestId('start-debug-remedy')).toBeNull();
  });
});

describe('refusalDetails', () => {
  it('returns nothing for a non-debug error or an unknown code', () => {
    expect(refusalDetails(new Error('x'))).toEqual([]);
    expect(refusalDetails(refusal(500, 'internal_error'))).toEqual([]);
  });

  it('names the per-device cap with the device', () => {
    expect(refusalDetails(refusal(409, 'debug_session_limit', { limit: 'device', device: 'thing-a', active: 2, max: 2 }))).toEqual([
      'Cap reached: sessions per device (thing-a) (2 of 2).',
    ]);
    expect(refusalDetails(refusal(409, 'debug_session_limit', { limit: 'devices_per_session' }))).toEqual([
      'Cap reached: devices per session.',
    ]);
  });
});
