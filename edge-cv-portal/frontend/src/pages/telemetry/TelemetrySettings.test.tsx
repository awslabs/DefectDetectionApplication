/**
 * Unit tests for the telemetry settings page (workflow-telemetry-dashboards
 * task 5.5, Requirements 6.2, 6.4; design §10.1, §10.2, §11.3).
 *
 * - the form from a fixture answer: enabled, the two intervals, the read-only
 *   media location with its region and region_source;
 * - the checklist: each item's status, detail and remedy, the
 *   Device_Record_Remedy in full for `device_records`, the cross-region and the
 *   enforcement warnings;
 * - the save answer: per-device shadow results and `unrecorded_devices`; the
 *   202 notice; the refusals with their remedy and field;
 * - the range validation of the two intervals.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { NOW, installResizeObserver } from './__fixtures__/telemetryFixtures';
import type { TelemetryChecklistItem, TelemetrySettingsResponse, TelemetrySettingsUpdateResponse } from './types';

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => ({ usecaseId: 'uc-1' }),
  };
});

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      getTelemetrySettings: vi.fn(),
      putTelemetrySettings: vi.fn(),
    },
  };
});

import { apiService, TelemetryApiError } from '../../services/api';
import TelemetrySettings, { HEARTBEAT_MESSAGE, PENDING_NOTICE, SUMMARY_MESSAGE, checklistDetail, isIntegerInRange } from './TelemetrySettings';

const mocked = vi.mocked(apiService);

const DEVICE_RECORD_REMEDY =
  "A UseCaseAdmin or PortalAdmin sets the device's Target architecture on its device page (Devices > the device), " +
  'which calls PUT /devices/{id} with {"usecase_id": "<this use case>", "target_architecture": "<arch>"}; ' +
  '{"usecase_id": ..., "test_device": false} also works; or re-run quick setup on the device. The route needs ' +
  'node_designer:manage, which Operators do not hold. Until then the device publishes nothing and cannot be added ' +
  'to a dashboard.';
const ENFORCEMENT_WARNING =
  'Registry enforcement is off: every signed-in user holds dashboards:read on this use case, so clip playback and live frames are refused until it is on (Q3).';
const CROSS_REGION_WARNING = 'the bucket is in eu-west-1 while the use case is registered in us-east-1: devices PUT across regions (transfer cost and latency)';

function checklist(): TelemetryChecklistItem[] {
  return [
    { id: 'ingestion_resources', ready: true, topology: 'cross' },
    { id: 'region', ready: true, usecase_region: 'us-east-1', portal_region: 'us-east-1' },
    { id: 'role_form', ready: true },
    { id: 'device_records', ready: false, remedy: DEVICE_RECORD_REMEDY, unrecorded: ['thing-c'], other_usecase: { 'thing-c': 'uc-9' }, recorded: ['thing-a'], managed: ['thing-a', 'thing-c'] },
    { id: 'telemetry_source', ready: false, remedy: 'Deploy the LocalServer release that carries the Telemetry_Agent to a device of this use case; summaries appear within a minute of enabling.', lastSeenMs: null },
    { id: 'media_location', ready: true, warning: CROSS_REGION_WARNING, media_location: { bucket: 'dda-inference-results-fixture', prefix: 'dda-telemetry/', source: 'default', region: 'eu-west-1', region_source: 'header' } },
    { id: 'media_retention', ready: false, remedy: 'Run scripts/telemetry_lifecycle.py --bucket <name> --clip-days <n> --apply with operator credentials.', reason: 'media_retention_unset', versioning: 'never', clipRuleDays: null },
    { id: 'registry_enforcement', ready: true, warning: ENFORCEMENT_WARNING, enforced: false },
    { id: 'device_shadows', ready: false, remedy: 'The listed devices did not accept the telemetry shadow: check that they are online.', pending: [], failed: { 'thing-a': 'thing_not_found' }, devices: 1 },
  ];
}

function settingsAnswer(overrides: Partial<TelemetrySettingsResponse['settings']> = {}): TelemetrySettingsResponse {
  return {
    settings: {
      usecase_id: 'uc-1',
      enabled: false,
      summaryIntervalS: 10,
      idleHeartbeatS: 60,
      personClasses: ['person', 'human'],
      media_location: { bucket: 'dda-inference-results-fixture', prefix: 'dda-telemetry/', source: 'default', region: 'eu-west-1', region_source: 'header' },
      topology: 'cross',
      updatedAt: NOW - 3_600_000,
      updatedBy: 'admin@example.com',
      shadowSyncPending: false,
      ...overrides,
    },
    checklist: checklist(),
  };
}

function saveAnswer(status: number, overrides: Partial<TelemetrySettingsUpdateResponse> = {}): TelemetrySettingsUpdateResponse {
  const base = settingsAnswer({ enabled: true });
  return {
    ...base,
    devices: [
      { device_id: 'thing-a', shadow: 'ok' },
      { device_id: 'thing-b', shadow: 'failed', error: 'thing_not_found' },
    ],
    unrecorded_devices: ['thing-c'],
    status,
    ...overrides,
  };
}

async function renderPage() {
  render(<TelemetrySettings />);
  await waitFor(() => expect(mocked.getTelemetrySettings).toHaveBeenCalledWith('uc-1'));
  await act(async () => {});
}

function setInput(testId: string, value: string) {
  createWrapper(document.body).findInput(`[data-testid="${testId}"]`)!.setInputValue(value);
}

async function save() {
  await act(async () => {
    fireEvent.click(screen.getByTestId('telemetry-settings-save'));
  });
  await act(async () => {});
}

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.clearAllMocks();
  mocked.getTelemetrySettings.mockResolvedValue(settingsAnswer());
  mocked.putTelemetrySettings.mockResolvedValue(saveAnswer(200));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('isIntegerInRange and checklistDetail', () => {
  it('accepts whole numbers inside the range only', () => {
    expect(isIntegerInRange('10', 5, 60)).toBe(true);
    expect(isIntegerInRange('5', 5, 60)).toBe(true);
    expect(isIntegerInRange('60', 5, 60)).toBe(true);
    expect(isIntegerInRange('4', 5, 60)).toBe(false);
    expect(isIntegerInRange('61', 5, 60)).toBe(false);
    expect(isIntegerInRange('10.5', 5, 60)).toBe(false);
    expect(isIntegerInRange('', 5, 60)).toBe(false);
  });

  it('describes the device records, media location and shadows items', () => {
    const items = checklist();
    expect(checklistDetail(items[3])).toContain('without a record: thing-c');
    expect(checklistDetail(items[3])).toContain('thing-c is recorded for another use case (uc-9)');
    expect(checklistDetail(items[5])).toContain('region eu-west-1 from header');
    expect(checklistDetail(items[7])).toBe('enforcement off');
    expect(checklistDetail(items[8])).toContain('thing-a failed (thing_not_found)');
  });
});

describe('TelemetrySettings — the form', () => {
  it('renders the settings and the read-only media location with region and region_source', async () => {
    await renderPage();
    expect(createWrapper(document.body).findToggle('[data-testid="telemetry-settings-enabled"]')!.findNativeInput().getElement().checked).toBe(false);
    expect(createWrapper(document.body).findInput('[data-testid="telemetry-settings-summary"]')!.findNativeInput().getElement().value).toBe('10');
    expect(createWrapper(document.body).findInput('[data-testid="telemetry-settings-heartbeat"]')!.findNativeInput().getElement().value).toBe('60');
    expect(screen.getByTestId('telemetry-settings-media-bucket').textContent).toBe('dda-inference-results-fixture/dda-telemetry/');
    expect(screen.getByTestId('telemetry-settings-media-region').textContent).toBe('eu-west-1 (header)');
    // The media location is read-only: no input carries the bucket.
    expect(createWrapper(document.body).findAllInputs().some((i) => i.findNativeInput().getElement().value.includes('dda-inference-results'))).toBe(false);
  });

  it('validates the two intervals before saving', async () => {
    await renderPage();
    setInput('telemetry-settings-summary', '4');
    setInput('telemetry-settings-heartbeat', '301');
    await save();
    expect(mocked.putTelemetrySettings).not.toHaveBeenCalled();
    expect(screen.getByText(SUMMARY_MESSAGE)).toBeInTheDocument();
    expect(screen.getByText(HEARTBEAT_MESSAGE)).toBeInTheDocument();
  });
});

describe('TelemetrySettings — the readiness checklist (design §10.1)', () => {
  it('shows each item with its status, detail and remedy', async () => {
    await renderPage();
    expect(screen.getByTestId('telemetry-checklist-ingestion_resources-status').textContent).toBe('ready');
    expect(screen.getByTestId('telemetry-checklist-region-status').textContent).toBe('ready');
    expect(screen.getByTestId('telemetry-checklist-device_records-status').textContent).toBe('not ready');
    expect(screen.getByTestId('telemetry-checklist-device_records-remedy').textContent).toBe(DEVICE_RECORD_REMEDY);
    expect(screen.getByTestId('telemetry-checklist-device_records-detail').textContent).toContain('thing-c is recorded for another use case (uc-9)');
    expect(screen.getByTestId('telemetry-checklist-telemetry_source-status').textContent).toBe('not ready');
    expect(screen.getByTestId('telemetry-checklist-telemetry_source-remedy').textContent).toContain('Deploy the LocalServer release');
    expect(screen.getByTestId('telemetry-checklist-media_retention-remedy').textContent).toContain('telemetry_lifecycle.py');
    expect(screen.getByTestId('telemetry-checklist-device_shadows-remedy').textContent).toContain('check that they are online');
    expect(screen.getByTestId('telemetry-checklist-media_location-status').textContent).toBe('ready with a warning');
    expect(screen.getByTestId('telemetry-checklist-registry_enforcement-status').textContent).toBe('ready with a warning');
    // Ready items carry no remedy.
    expect(screen.queryByTestId('telemetry-checklist-region-remedy')).toBeNull();
  });

  it('shows the cross-region and the enforcement warnings', async () => {
    await renderPage();
    expect(screen.getByTestId('telemetry-checklist-warning-media_location').textContent).toContain(CROSS_REGION_WARNING);
    expect(screen.getByTestId('telemetry-checklist-warning-registry_enforcement').textContent).toContain(ENFORCEMENT_WARNING);
  });

  it('omits the role_form item for a portal-topology use case', async () => {
    const answer = settingsAnswer({ topology: 'portal' });
    answer.checklist = answer.checklist.filter((i) => i.id !== 'role_form');
    mocked.getTelemetrySettings.mockResolvedValue(answer);
    await renderPage();
    expect(screen.queryByTestId('telemetry-checklist-role_form')).toBeNull();
    expect(screen.getByTestId('telemetry-checklist-ingestion_resources')).toBeInTheDocument();
  });
});

describe('TelemetrySettings — saving (design §10.2)', () => {
  it('saves the three settings and shows the per-device shadow results and the unrecorded devices', async () => {
    await renderPage();
    createWrapper(document.body).findToggle('[data-testid="telemetry-settings-enabled"]')!.findNativeInput().click();
    setInput('telemetry-settings-summary', '15');
    setInput('telemetry-settings-heartbeat', '120');
    await save();

    expect(mocked.putTelemetrySettings).toHaveBeenCalledWith('uc-1', { enabled: true, summaryIntervalS: 15, idleHeartbeatS: 120 });
    expect(screen.getByTestId('telemetry-settings-saved').textContent).toContain('Telemetry is on');
    expect(screen.getByTestId('telemetry-settings-shadows').textContent).toBe('Telemetry shadows: thing-a ok, thing-b failed (thing_not_found).');
    expect(screen.getByTestId('telemetry-settings-unrecorded').textContent).toContain('thing-c');
    expect(screen.queryByTestId('telemetry-settings-pending')).toBeNull();
  });

  it('shows the 202 notice when the shadow writes are pending', async () => {
    mocked.putTelemetrySettings.mockResolvedValue(
      saveAnswer(202, { devices: [], pending_devices: 41, message: PENDING_NOTICE })
    );
    await renderPage();
    await save();
    expect(screen.getByTestId('telemetry-settings-pending').textContent).toBe(`${PENDING_NOTICE} (41 devices)`);
    expect(screen.queryByTestId('telemetry-settings-saved')).toBeNull();
  });

  it.each([
    ['region_not_supported', "Telemetry is not supported for use cases outside the Portal's region.", undefined],
    ['usecase_role_invalid', "Register the use case with its account's DDAPortalAccessRole ARN.", undefined],
    ['media_location_not_writable', "Create the use case's inference-results bucket, or set its override.", undefined],
    ['invalid_settings', undefined, 'summaryIntervalS'],
  ] as const)('shows a %s refusal with its remedy or field', async (code, remedy, field) => {
    mocked.putTelemetrySettings.mockRejectedValue(
      new TelemetryApiError(`refused with ${code}`, 400, { error: `refused with ${code}`, error_code: code, remedy, field })
    );
    await renderPage();
    await save();
    expect(screen.getByTestId('telemetry-settings-error').textContent).toContain(code);
    if (remedy) expect(screen.getByTestId('telemetry-settings-remedy').textContent).toBe(remedy);
    if (field) expect(screen.getAllByText(`refused with ${code} (${code})`).length).toBeGreaterThan(0);
  });

  it('shows a read failure', async () => {
    mocked.getTelemetrySettings.mockRejectedValue(new Error('boom'));
    await renderPage();
    expect(screen.getByTestId('telemetry-settings-error').textContent).toContain('boom');
  });

  it('"Dashboards" returns to the list of the use case', async () => {
    await renderPage();
    fireEvent.click(screen.getByText('Dashboards'));
    expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards?usecase_id=uc-1');
  });
});
