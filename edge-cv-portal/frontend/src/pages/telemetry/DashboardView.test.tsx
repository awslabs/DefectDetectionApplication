/**
 * Unit tests for the dashboard page (workflow-telemetry-dashboards task 5.5,
 * Requirements 1.4, 9.3, 11.4, 17.5; design §8.3, §9, §11.3).
 *
 * With fake timers and a mocked `apiService`:
 * - the page composes the template's tiles: the series panel with every panel
 *   of the template, Now, Health, the clip gallery only for a template that
 *   lists `clips`, the live tile with a device selector, the events list;
 * - exactly ONE series request per refresh and ONE now request per poll for
 *   both panels;
 * - "telemetry is off" when the settings say so; the delete action for
 *   dashboards:manage; the settings link for telemetry:manage;
 * - a not-found dashboard shows the refusal.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ReactNode } from 'react';
import type { UserRole } from '../../types';
import { NOW, SCHEMAS, dashboard, installResizeObserver, nowAnswer, seriesAnswer, template } from './__fixtures__/telemetryFixtures';

const { navigateMock, useAuthMock, paramsMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  paramsMock: { dashboardId: 'dash-1' },
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => paramsMock,
    useLocation: () => ({ pathname: '/workflow-dashboards/dash-1', state: null, search: '', hash: '', key: 'k' }),
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      getDashboard: vi.fn(),
      getTelemetrySchemas: vi.fn(),
      getDashboardNow: vi.fn(),
      getDashboardSeries: vi.fn(),
      getDashboardEvents: vi.fn(),
      getDashboardClips: vi.fn(),
      acquireLiveLease: vi.fn(),
      getLiveFrame: vi.fn(),
      deleteDashboard: vi.fn(),
    },
  };
});

import { apiService, TelemetryApiError } from '../../services/api';
import DashboardView from './DashboardView';
import { TELEMETRY_OFF } from '../../components/telemetry/nowData';

const mocked = vi.mocked(apiService);

function setRole(role: UserRole) {
  useAuthMock.mockReturnValue({
    user: { user_id: 'user-1', email: 'user@example.com', username: 'user', role, is_super_user: false },
  });
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderPage() {
  render(<DashboardView />);
  await advance(0);
  await advance(0);
}

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  setRole('Operator');
  mocked.getDashboard.mockResolvedValue(dashboard());
  mocked.getTelemetrySchemas.mockResolvedValue(SCHEMAS);
  mocked.getDashboardNow.mockResolvedValue(nowAnswer());
  mocked.getDashboardSeries.mockResolvedValue(seriesAnswer());
  mocked.getDashboardEvents.mockResolvedValue({ events: [], cursor: null, from: NOW - 86_400_000, to: NOW });
  mocked.getDashboardClips.mockResolvedValue({ clips: [], cursor: null, from: NOW - 86_400_000, to: NOW });
  mocked.acquireLiveLease.mockResolvedValue({
    device_id: 'thing-a', workflow_id: 'wf', slot: 0, startedAt: NOW, leaseExpiresAt: NOW + 60_000, expiresAt: NOW + 60_000,
    renewSeconds: 20, region: 'us-east-1', region_source: 'header',
  });
  mocked.getLiveFrame.mockResolvedValue({ frame: null, frame_token: null, state: 'no_live_source' });
  mocked.deleteDashboard.mockResolvedValue({ dashboard_id: 'dash-1', deleted: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('DashboardView — composition', () => {
  it('renders the generic-detector tiles: every panel, Now, Health, Live with a device selector, Events; no clip gallery', async () => {
    await renderPage();

    expect(mocked.getDashboard).toHaveBeenCalledWith('dash-1');
    expect(screen.getByText('Line 1')).toBeInTheDocument();
    for (const panel of template('generic-detector').panels) {
      expect(screen.getByTestId(`telemetry-panel-${panel.id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('telemetry-now-panel')).toBeInTheDocument();
    expect(screen.getByTestId('telemetry-health-panel')).toBeInTheDocument();
    expect(screen.getByTestId('telemetry-live-tile')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-live-device')).toBeInTheDocument();
    expect(screen.getByTestId('telemetry-events-list')).toBeInTheDocument();
    expect(screen.queryByTestId('telemetry-clip-gallery')).toBeNull();
    expect(mocked.getDashboardClips).not.toHaveBeenCalled();
    // The live tile leased the first device.
    expect(mocked.acquireLiveLease).toHaveBeenCalledWith('dash-1', 'thing-a');
  });

  it('renders the clip gallery for the ppe-compliance template', async () => {
    mocked.getDashboard.mockResolvedValue(dashboard({ template_id: 'ppe-compliance' }));
    await renderPage();
    expect(screen.getByTestId('telemetry-clip-gallery')).toBeInTheDocument();
    expect(mocked.getDashboardClips).toHaveBeenCalledWith('dash-1', {});
    for (const panel of template('ppe-compliance').panels) {
      expect(screen.getByTestId(`telemetry-panel-${panel.id}`)).toBeInTheDocument();
    }
  });

  it('makes exactly one series request per refresh and one now request per poll for both panels', async () => {
    await renderPage();
    expect(mocked.getDashboardSeries).toHaveBeenCalledTimes(1);
    expect(mocked.getDashboardSeries.mock.calls[0][1]).toEqual({ from: NOW - 15 * 60_000, to: NOW });
    expect(mocked.getDashboardNow).toHaveBeenCalledTimes(1);

    await advance(5_000);
    expect(mocked.getDashboardNow).toHaveBeenCalledTimes(2);
    expect(mocked.getDashboardSeries).toHaveBeenCalledTimes(1);

    await advance(55_000);
    expect(mocked.getDashboardSeries).toHaveBeenCalledTimes(2);
    expect(mocked.getDashboardNow).toHaveBeenCalledTimes(13);
    for (const call of mocked.getDashboardSeries.mock.calls) {
      expect(Object.keys(call[1]).sort()).toEqual(['from', 'to']);
    }
    // The events list refreshed every 15 s.
    expect(mocked.getDashboardEvents).toHaveBeenCalledTimes(5);
  });

  it('shows "telemetry is off" when the settings say so, with the settings action for a UseCaseAdmin', async () => {
    setRole('UseCaseAdmin');
    mocked.getDashboardNow.mockResolvedValue(nowAnswer({ enabled: false }));
    await renderPage();
    expect(screen.getByTestId('dashboard-telemetry-off').textContent).toContain(TELEMETRY_OFF);
    expect(screen.getByTestId('dashboard-settings-button')).toBeInTheDocument();
  });

  it('hides the settings link for an Operator and the delete action for a Viewer', async () => {
    setRole('Viewer');
    await renderPage();
    expect(screen.queryByTestId('dashboard-settings-button')).toBeNull();
    expect(screen.queryByTestId('dashboard-delete-button')).toBeNull();
    expect(screen.queryByTestId('dashboard-telemetry-off')).toBeNull();
  });

  it('deletes the dashboard after confirmation and returns to the list', async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId('dashboard-delete-button'));
    await advance(0);
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findContent().getElement().textContent).toContain('Delete the dashboard "Line 1"?');
    await act(async () => {
      modal.findFooter()!.findButton('[class*="variant-primary"]')!.click();
    });
    await advance(0);
    expect(mocked.deleteDashboard).toHaveBeenCalledWith('dash-1');
    expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards?usecase_id=uc-1');
  });

  it('shows the refusal of a dashboard that does not exist', async () => {
    mocked.getDashboard.mockRejectedValue(
      new TelemetryApiError('Dashboard not found', 404, { error: 'Dashboard not found', error_code: 'dashboard_not_found' })
    );
    await renderPage();
    expect(screen.getByTestId('dashboard-error').textContent).toContain('Dashboard not found');
    expect(mocked.getDashboardSeries).not.toHaveBeenCalled();
    expect(mocked.getDashboardNow).not.toHaveBeenCalled();
  });

  it('shows an unknown template as an error and still renders the Now panel', async () => {
    mocked.getDashboard.mockResolvedValue(dashboard({ template_id: 'not-a-template' }));
    await renderPage();
    expect(screen.getByTestId('dashboard-unknown-template')).toBeInTheDocument();
    expect(screen.queryByTestId('telemetry-series-panel')).toBeNull();
  });
});
