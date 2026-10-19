/**
 * Unit tests for the create-dashboard wizard (workflow-telemetry-dashboards
 * task 5.5, Requirements 1.2, 6.2, 9.3).
 *
 * - the five steps' validation: a use case, 1-20 devices, a workflow, a
 *   template, a name of 1-100 characters;
 * - the 20-device cap;
 * - the template availability from `getTelemetryTemplates` for the chosen
 *   workflow and devices (`available` / `partial` with the unreported
 *   panels / `unavailable`);
 * - a 400 `device_not_recorded` shows the Device_Record_Remedy and returns to
 *   the devices step; a 409 `dashboard_name_taken` returns to the name step;
 * - a successful create navigates to the dashboard; no AWS console step.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ReactNode } from 'react';
import { SCHEMAS, dashboard, installResizeObserver } from './__fixtures__/telemetryFixtures';

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useSearchParams: () => [new URLSearchParams('usecase_id=uc-1'), vi.fn()],
  };
});

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
      listDevices: vi.fn(),
      listWorkflows: vi.fn(),
      getTelemetrySchemas: vi.fn(),
      getTelemetryTemplates: vi.fn(),
      createDashboard: vi.fn(),
    },
  };
});

import { apiService, TelemetryApiError } from '../../services/api';
import CreateDashboard, { DEVICES_MESSAGE, MAX_DEVICES, NAME_MESSAGE, NO_RECORDED_DEVICES, availabilityView } from './CreateDashboard';

const mocked = vi.mocked(apiService);
const WF = '0f1e2d3c-4b5a-4968-8776-65544332211a';
const REMEDY =
  "A UseCaseAdmin or PortalAdmin sets the device's Target architecture on its device page (Devices > the device), which calls PUT /devices/{id}; until then the device publishes nothing and cannot be added to a dashboard.";

const wizard = () => createWrapper(document.body).findWizard()!;

async function next() {
  await act(async () => {
    wizard().findPrimaryButton().click();
  });
}

async function renderWizard() {
  render(<CreateDashboard />);
  await waitFor(() => expect(mocked.listDevices).toHaveBeenCalledWith('uc-1'));
  await act(async () => {});
}

async function chooseDevices(ids: string[]) {
  for (const id of ids) {
    const select = createWrapper(document.body).findMultiselect('[data-testid="create-dashboard-devices"]')!;
    // The multiselect keeps its dropdown open after a pick; open it only when closed.
    if (!select.findDropdown().findOpenDropdown()) select.openDropdown();
    select.selectOptionByValue(id);
    await act(async () => {});
  }
}

async function chooseWorkflow() {
  const select = createWrapper(document.body).findSelect('[data-testid="create-dashboard-workflow"]')!;
  select.openDropdown();
  select.selectOptionByValue(WF);
  await act(async () => {});
}

async function chooseTemplate(id: string) {
  const tiles = createWrapper(document.body).findTiles('[data-testid="create-dashboard-template"]')!;
  tiles.findInputByValue(id)!.click();
  await act(async () => {});
}

async function typeName(name: string) {
  createWrapper(document.body).findInput('[data-testid="create-dashboard-name"]')!.setInputValue(name);
  await act(async () => {});
}

/** Walks the wizard through the four picks to the name step. */
async function reachNameStep(devices = ['thing-a'], templateId = 'generic-detector') {
  await next(); // use case → devices
  await chooseDevices(devices);
  await next(); // devices → workflow
  await chooseWorkflow();
  await next(); // workflow → template
  await waitFor(() => expect(mocked.getTelemetryTemplates).toHaveBeenCalled());
  await act(async () => {});
  await chooseTemplate(templateId);
  await next(); // template → name
}

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.clearAllMocks();
  mocked.listUseCases.mockResolvedValue({ usecases: [{ usecase_id: 'uc-1', name: 'Castings' } as any], count: 1 });
  mocked.listDevices.mockResolvedValue({
    devices: Array.from({ length: 22 }, (_, i) => ({
      device_id: `thing-${String.fromCharCode(97 + i)}`,
      target_architecture: 'arm64_jp5',
      status: 'HEALTHY',
    })) as any,
    count: 22,
  });
  mocked.listWorkflows.mockResolvedValue({ workflows: [{ workflow_id: WF, name: 'PPE vest check', latest_version: 3 } as any], count: 1 });
  mocked.getTelemetrySchemas.mockResolvedValue(SCHEMAS);
  mocked.getTelemetryTemplates.mockResolvedValue({
    usecase_id: 'uc-1',
    templates: [
      { id: 'generic-detector', version: 1, availability: 'available', reported_panels: ['runs', 'latency', 'detections', 'people', 'model-state', 'model-waits'], unreported_panels: [], producers: ['engine'] },
      { id: 'anomaly', version: 1, availability: 'partial', reported_panels: ['runs', 'latency', 'model-state'], unreported_panels: ['anomalous-share'], producers: ['engine'] },
      { id: 'ppe-compliance', version: 1, availability: 'unavailable', reported_panels: [], unreported_panels: [], producers: [] },
    ],
  });
  mocked.createDashboard.mockResolvedValue({ dashboard: dashboard({ dashboard_id: 'dash-new' }), devices: [{ device_id: 'thing-a', shadow: 'ok' }] });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('availabilityView', () => {
  it('maps the three states and the unknown one', () => {
    expect(availabilityView({ id: 'x', version: 1, availability: 'available', reported_panels: [], unreported_panels: [], producers: [] }).type).toBe('success');
    const partial = availabilityView({ id: 'x', version: 1, availability: 'partial', reported_panels: ['runs'], unreported_panels: ['people', 'clips'], producers: [] });
    expect(partial.type).toBe('warning');
    expect(partial.text).toContain('people, clips');
    expect(availabilityView({ id: 'x', version: 1, availability: 'unavailable', reported_panels: [], unreported_panels: [], producers: [] }).type).toBe('stopped');
    expect(availabilityView(undefined).type).toBe('pending');
  });
});

describe('CreateDashboard — the steps and their validation', () => {
  it('starts at the use case step with the use case from the query', async () => {
    await renderWizard();
    expect(wizard().findMenuNavigationLink(1, 'active')).not.toBeNull();
    const select = createWrapper(document.body).findSelect('[data-testid="create-dashboard-usecase"]')!;
    expect(select.findTrigger().getElement().textContent).toBe('Castings');
  });

  it('refuses to leave the devices step without a device and with more than 20', async () => {
    await renderWizard();
    await next();
    expect(wizard().findMenuNavigationLink(2, 'active')).not.toBeNull();

    await next();
    expect(wizard().findMenuNavigationLink(2, 'active')).not.toBeNull();
    expect(screen.getByText(DEVICES_MESSAGE)).toBeInTheDocument();

    const twentyOne = Array.from({ length: MAX_DEVICES + 1 }, (_, i) => `thing-${String.fromCharCode(97 + i)}`);
    await chooseDevices(twentyOne);
    expect(screen.getByText(`${MAX_DEVICES + 1} of ${MAX_DEVICES} chosen`)).toBeInTheDocument();
    await next();
    expect(wizard().findMenuNavigationLink(2, 'active')).not.toBeNull();
  });

  it('warns when the use case has no device records', async () => {
    mocked.listDevices.mockResolvedValue({ devices: [], count: 0 });
    await renderWizard();
    await next();
    expect(screen.getByTestId('create-dashboard-no-devices').textContent).toBe(NO_RECORDED_DEVICES);
  });

  it('requires a workflow and a template', async () => {
    await renderWizard();
    await next();
    await chooseDevices(['thing-a']);
    await next();
    await next();
    expect(wizard().findMenuNavigationLink(3, 'active')).not.toBeNull();
    expect(screen.getByText('Choose a workflow')).toBeInTheDocument();
    await chooseWorkflow();
    await next();
    await next();
    expect(wizard().findMenuNavigationLink(4, 'active')).not.toBeNull();
    expect(screen.getByText('Choose a template')).toBeInTheDocument();
  });

  it('shows each template with its availability for the chosen workflow and devices', async () => {
    await renderWizard();
    await next();
    await chooseDevices(['thing-a', 'thing-b']);
    await next();
    await chooseWorkflow();
    await next();
    await waitFor(() => expect(mocked.getTelemetryTemplates).toHaveBeenCalledWith('uc-1', { workflow_id: WF, device_ids: ['thing-a', 'thing-b'] }));
    await act(async () => {});
    expect(screen.getByTestId('create-dashboard-availability-generic-detector').textContent).toContain('available');
    expect(screen.getByTestId('create-dashboard-availability-anomaly').textContent).toContain('partial');
    expect(screen.getByTestId('create-dashboard-availability-anomaly').textContent).toContain('anomalous-share');
    expect(screen.getByTestId('create-dashboard-availability-ppe-compliance').textContent).toContain('unavailable');
  });

  it('requires a name of 1 to 100 characters and reviews the picks', async () => {
    await renderWizard();
    await reachNameStep(['thing-a', 'thing-b']);
    expect(wizard().findMenuNavigationLink(5, 'active')).not.toBeNull();
    expect(screen.getByTestId('create-dashboard-review-devices').textContent).toBe('thing-a, thing-b');

    await act(async () => {
      wizard().findPrimaryButton().click();
    });
    expect(screen.getByText(NAME_MESSAGE)).toBeInTheDocument();
    expect(mocked.createDashboard).not.toHaveBeenCalled();

    await typeName('x'.repeat(101));
    await act(async () => {
      wizard().findPrimaryButton().click();
    });
    expect(mocked.createDashboard).not.toHaveBeenCalled();
  });
});

describe('CreateDashboard — submit', () => {
  it('creates the dashboard with the four picks and the name, then opens it', async () => {
    await renderWizard();
    await reachNameStep(['thing-a'], 'generic-detector');
    await typeName('  Line 1  ');
    await act(async () => {
      wizard().findPrimaryButton().click();
    });

    await waitFor(() => expect(mocked.createDashboard).toHaveBeenCalledTimes(1));
    expect(mocked.createDashboard).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      name: 'Line 1',
      workflow_id: WF,
      device_ids: ['thing-a'],
      template_id: 'generic-detector',
    });
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards/dash-new', {
        state: { created: true, devices: [{ device_id: 'thing-a', shadow: 'ok' }] },
      })
    );
    // No AWS console step anywhere in the wizard.
    expect(document.body.textContent).not.toMatch(/console/i);
  });

  it('a 400 device_not_recorded shows the Device_Record_Remedy and returns to the devices step', async () => {
    mocked.createDashboard.mockRejectedValue(
      new TelemetryApiError('thing-a has no device record for this use case', 400, {
        error: 'thing-a has no device record for this use case',
        error_code: 'device_not_recorded',
        field: 'device_ids',
        remedy: REMEDY,
      })
    );
    await renderWizard();
    await reachNameStep();
    await typeName('Line 1');
    await act(async () => {
      wizard().findPrimaryButton().click();
    });
    await waitFor(() => expect(screen.getByTestId('create-dashboard-error')).toBeInTheDocument());
    expect(screen.getByTestId('create-dashboard-error').textContent).toContain('device_not_recorded');
    expect(screen.getByTestId('create-dashboard-remedy').textContent).toBe(REMEDY);
    expect(wizard().findMenuNavigationLink(2, 'active')).not.toBeNull();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('a 409 dashboard_name_taken stays on the name step with the message', async () => {
    mocked.createDashboard.mockRejectedValue(
      new TelemetryApiError('A dashboard of this use case already has that name', 409, {
        error: 'A dashboard of this use case already has that name',
        error_code: 'dashboard_name_taken',
        name: 'Line 1',
      })
    );
    await renderWizard();
    await reachNameStep();
    await typeName('Line 1');
    await act(async () => {
      wizard().findPrimaryButton().click();
    });
    await waitFor(() => expect(screen.getByTestId('create-dashboard-error').textContent).toContain('dashboard_name_taken'));
    expect(wizard().findMenuNavigationLink(5, 'active')).not.toBeNull();
  });

  it('Cancel returns to the list', async () => {
    await renderWizard();
    await act(async () => {
      wizard().findCancelButton().click();
    });
    expect(navigateMock).toHaveBeenCalledWith('/workflow-dashboards');
  });
});
