/**
 * Unit tests for the dashboards list page (workflow-telemetry-dashboards
 * task 5.5, Requirements 1.2, 11.4, 17.5).
 *
 * - the table from a fixture answer: name (a link to the dashboard), workflow
 *   name, template label and version, devices, author;
 * - "Create dashboard" and "Delete" for an Operator (dashboards:manage),
 *   hidden for a Viewer; "Telemetry settings" for a UseCaseAdmin only;
 * - "Delete" confirms, calls `deleteDashboard` and reloads;
 * - the empty state.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ReactNode } from 'react';
import type { UserRole } from '../../types';
import { NOW, SCHEMAS, dashboard, installResizeObserver } from './__fixtures__/telemetryFixtures';

const { navigateMock, useAuthMock, setSearchParamsMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  setSearchParamsMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useSearchParams: () => [new URLSearchParams('usecase_id=uc-1'), setSearchParamsMock],
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  UsecaseProvider: ({ children }: { children: ReactNode }) => children,
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases: vi.fn(),
      listDashboards: vi.fn(),
      listWorkflows: vi.fn(),
      getTelemetrySchemas: vi.fn(),
      deleteDashboard: vi.fn(),
    },
  };
});

import { apiService, TelemetryApiError } from '../../services/api';
import WorkflowDashboards, { NO_DASHBOARDS } from './WorkflowDashboards';

const mocked = vi.mocked(apiService);

function setRole(role: UserRole) {
  useAuthMock.mockReturnValue({
    user: { user_id: 'user-1', email: 'user@example.com', username: 'user', role, is_super_user: false },
  });
}

async function renderPage() {
  render(<WorkflowDashboards />);
  await waitFor(() => expect(mocked.listDashboards).toHaveBeenCalled());
  await act(async () => {});
}

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(Date, 'now').mockReturnValue(NOW);
  setRole('Operator');
  mocked.listUseCases.mockResolvedValue({ usecases: [{ usecase_id: 'uc-1', name: 'Castings' } as any], count: 1 });
  mocked.listDashboards.mockResolvedValue({
    usecase_id: 'uc-1',
    dashboards: [dashboard(), dashboard({ dashboard_id: 'dash-2', name: 'Line 2', template_id: 'ppe-compliance', device_ids: ['thing-c'] })],
  });
  mocked.listWorkflows.mockResolvedValue({
    workflows: [{ workflow_id: '0f1e2d3c-4b5a-4968-8776-65544332211a', name: 'PPE vest check' } as any],
    count: 1,
  });
  mocked.getTelemetrySchemas.mockResolvedValue(SCHEMAS);
  mocked.deleteDashboard.mockResolvedValue({ dashboard_id: 'dash-1', deleted: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('WorkflowDashboards — the table', () => {
  it('lists the dashboards with workflow name, template, devices and author', async () => {
    await renderPage();

    expect(mocked.listDashboards).toHaveBeenCalledWith('uc-1');
    const table = screen.getByTestId('dashboards-table');
    expect(table.textContent).toContain('Line 1');
    expect(table.textContent).toContain('Line 2');
    expect(table.textContent).toContain('PPE vest check');
    expect(table.textContent).toContain('Generic detector v1');
    expect(table.textContent).toContain('PPE compliance v1');
    expect(table.textContent).toContain('2: thing-a, thing-b');
    expect(table.textContent).toContain('1: thing-c');
    expect(table.textContent).toContain('operator@example.com');
  });

  it('links each name to its dashboard route', async () => {
    await renderPage();
    const link = screen.getByText('Line 1').closest('a')!;
    expect(link.getAttribute('href')).toBe('/workflow-dashboards/dash-1');
    fireEvent.click(link);
    expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards/dash-1');
  });

  it('shows the empty state without dashboards', async () => {
    mocked.listDashboards.mockResolvedValue({ usecase_id: 'uc-1', dashboards: [] });
    await renderPage();
    expect(screen.getByTestId('dashboards-empty').textContent).toBe(NO_DASHBOARDS);
  });

  it('shows a listing failure', async () => {
    mocked.listDashboards.mockRejectedValue(new Error('boom'));
    await renderPage();
    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument());
  });
});

describe('WorkflowDashboards — role gating (design §11.4)', () => {
  it('hides "Create dashboard", "Delete" and "Telemetry settings" for a Viewer', async () => {
    setRole('Viewer');
    await renderPage();
    expect(screen.queryByTestId('dashboards-create-button')).toBeNull();
    expect(screen.queryByTestId('dashboard-delete-dash-1')).toBeNull();
    expect(screen.queryByTestId('dashboards-settings-button')).toBeNull();
  });

  it('shows "Create dashboard" and "Delete" for an Operator, but no "Telemetry settings"', async () => {
    setRole('Operator');
    await renderPage();
    expect(screen.getByTestId('dashboards-create-button')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-delete-dash-1')).toBeInTheDocument();
    expect(screen.queryByTestId('dashboards-settings-button')).toBeNull();
    fireEvent.click(screen.getByTestId('dashboards-create-button'));
    expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards/create?usecase_id=uc-1');
  });

  it.each<UserRole>(['UseCaseAdmin', 'PortalAdmin'])('shows "Telemetry settings" for %s', async (role) => {
    setRole(role);
    await renderPage();
    fireEvent.click(screen.getByTestId('dashboards-settings-button'));
    expect(navigateMock).toHaveBeenCalledWith('/telemetry-settings/uc-1');
  });
});

describe('WorkflowDashboards — delete', () => {
  it('confirms, deletes and reloads', async () => {
    await renderPage();
    expect(mocked.listDashboards).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('dashboard-delete-dash-1'));
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findContent().getElement().textContent).toContain('Delete the dashboard "Line 1"?');
    await act(async () => {
      modal.findFooter()!.findButton('[class*="variant-primary"]')!.click();
    });

    expect(mocked.deleteDashboard).toHaveBeenCalledWith('dash-1');
    await waitFor(() => expect(mocked.listDashboards).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('dashboards-notice').textContent).toContain('Dashboard "Line 1" deleted.');
  });

  it('shows a delete refusal with its remedy', async () => {
    mocked.deleteDashboard.mockRejectedValue(
      new TelemetryApiError('The dashboard could not be deleted; retry', 409, {
        error: 'The dashboard could not be deleted; retry',
        error_code: 'dashboard_conflict',
        remedy: 'Refresh and retry',
      })
    );
    await renderPage();
    fireEvent.click(screen.getByTestId('dashboard-delete-dash-1'));
    const modal = createWrapper(document.body).findModal()!;
    await act(async () => {
      modal.findFooter()!.findButton('[class*="variant-primary"]')!.click();
    });
    await waitFor(() => expect(screen.getByText(/could not be deleted; retry Refresh and retry/)).toBeInTheDocument());
  });
});
