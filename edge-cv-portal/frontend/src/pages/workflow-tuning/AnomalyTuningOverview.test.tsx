/**
 * Unit tests for the Anomaly Tuning overview page
 * (quality-prompt-tuning, task 8.6 — Requirements 1.2, 1.6).
 *
 * The listing: one container per workflow with its version and tunable-node
 * count, and a row per Tunable_Node carrying the node id, a human type
 * label, the model, the Sample_Store count and a Session badge, with the
 * per-node "Open session" action making the create-or-get call and
 * navigating to the returned session (Requirement 1.2).
 *
 * The Requirement 1.6 explanation: shown exactly while the Use_Case has
 * Sample_Export disabled, with a link to the Use_Case settings.
 *
 * Plus the `?workflowId=` preselection (the toolbar entry point's landing
 * spot) and its "Show all workflows" escape, the sample-store warning and
 * the empty state.
 *
 * No AWS and no network: `apiService`, `UsecaseContext` and `useNavigate`
 * are mocked.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import AnomalyTuningOverview, {
  EXPORT_DISABLED_MESSAGE,
  NO_TUNABLE_WORKFLOWS_MESSAGE,
  USECASE_SETTINGS_HREF,
  nodeTypeLabel,
} from './AnomalyTuningOverview';
import type { TuningOverviewResponse } from './types';

const {
  navigateMock,
  searchParamsRef,
  setSearchParamsMock,
  listUseCases,
  listTuningWorkflows,
  createTuningSession,
  useUsecaseMock,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  searchParamsRef: { current: new URLSearchParams() },
  setSearchParamsMock: vi.fn(),
  listUseCases: vi.fn(),
  listTuningWorkflows: vi.fn(),
  createTuningSession: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useSearchParams: () =>
      [searchParamsRef.current, setSearchParamsMock] as const,
  };
});

vi.mock('../../services/api', () => ({
  apiService: { listUseCases, listTuningWorkflows, createTuningSession },
}));

vi.mock('../../contexts/UsecaseContext', () => ({ useUsecase: useUsecaseMock }));

// ------------------------------------------------------------------ fixtures

function overview(
  patch: Partial<TuningOverviewResponse> = {}
): TuningOverviewResponse {
  return {
    usecaseId: 'uc-1',
    sampleExportEnabled: true,
    sampleRetentionDays: 30,
    count: 1,
    sampleStoreError: null,
    workflows: [
      {
        workflowId: 'wf-1',
        name: 'Line inspection',
        latestVersion: 4,
        updatedAt: 1_700_000_000,
        nodes: [
          {
            nodeId: 'bedrock_1',
            nodeType: 'bedrock_inference',
            model: 'anthropic.claude-3-5-sonnet',
            sampleCount: 42,
            sessionId: 'ts-1',
          },
          {
            nodeId: 'vlm_1',
            nodeType: 'llm_inference',
            model: null,
            sampleCount: 0,
            sessionId: null,
          },
        ],
      },
    ],
    ...patch,
  };
}

async function renderOverview(response = overview()) {
  listTuningWorkflows.mockResolvedValue(response);
  const view = render(<AnomalyTuningOverview />);
  await waitFor(() => expect(listTuningWorkflows).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByTestId('tuning-workflow-wf-1')).toBeTruthy());
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  searchParamsRef.current = new URLSearchParams();
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Cookies' }],
  });
  createTuningSession.mockResolvedValue({
    session: { sessionId: 'ts-new' },
    created: true,
  });
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------------- Requirement 1.2

describe('Requirement 1.2: the tunable-node listing', () => {
  it('requests the overview for the selected use case', async () => {
    await renderOverview();
    expect(listTuningWorkflows).toHaveBeenCalledWith('uc-1', undefined);
  });

  it('names the workflow, its version and its tunable-node count', async () => {
    await renderOverview();
    const container = screen.getByTestId('tuning-workflow-wf-1');
    expect(within(container).getByText('Line inspection')).toBeTruthy();
    expect(
      within(container).getByText('Version 4 · 2 tunable nodes')
    ).toBeTruthy();
  });

  it('falls back to the workflow id when the workflow has no name', async () => {
    const response = overview();
    response.workflows[0].name = null;
    await renderOverview(response);
    expect(
      within(screen.getByTestId('tuning-workflow-wf-1')).getByText('wf-1')
    ).toBeTruthy();
  });

  it('lists Node / Type / Model / Samples per tunable node', async () => {
    await renderOverview();
    const table = screen.getByTestId('tuning-nodes-wf-1');
    for (const header of ['Node', 'Type', 'Model', 'Samples']) {
      expect(within(table).getByText(header)).toBeTruthy();
    }
    expect(within(table).getByText('bedrock_1')).toBeTruthy();
    expect(within(table).getByText('Bedrock (cloud VLM)')).toBeTruthy();
    expect(within(table).getByText('anthropic.claude-3-5-sonnet')).toBeTruthy();
    expect(within(table).getByText('42')).toBeTruthy();

    expect(within(table).getByText('vlm_1')).toBeTruthy();
    expect(within(table).getByText('VLM on device')).toBeTruthy();
    // No model configured, and no sample exported yet.
    expect(within(table).getByText('—')).toBeTruthy();
    expect(within(table).getByText('0')).toBeTruthy();
  });

  it('labels the two tunable node types and passes anything else through', () => {
    expect(nodeTypeLabel('bedrock_inference')).toBe('Bedrock (cloud VLM)');
    expect(nodeTypeLabel('llm_inference')).toBe('VLM on device');
    expect(nodeTypeLabel('model_inference')).toBe('model_inference');
  });

  it('badges the node that already has a session and not the one without', async () => {
    await renderOverview();
    const rows = within(screen.getByTestId('tuning-nodes-wf-1')).getAllByRole('row');
    const withSession = rows.find((row) => row.textContent?.includes('bedrock_1'))!;
    const withoutSession = rows.find((row) => row.textContent?.includes('vlm_1'))!;
    expect(withSession.textContent).toContain('Session');
    expect(withoutSession.textContent).not.toContain('Session');
  });

  it('create-or-gets the node\'s session and opens its workspace', async () => {
    await renderOverview();
    const rows = within(screen.getByTestId('tuning-nodes-wf-1')).getAllByRole('row');
    const vlmRow = rows.find((row) => row.textContent?.includes('vlm_1'))!;
    fireEvent.click(within(vlmRow).getByRole('button', { name: 'Open session' }));
    await waitFor(() =>
      expect(createTuningSession).toHaveBeenCalledWith({
        workflow_id: 'wf-1',
        node_id: 'vlm_1',
      })
    );
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith(
        '/workflow-tuning/anomaly/sessions/ts-new'
      )
    );
  });

  it('surfaces a failed open without navigating anywhere', async () => {
    await renderOverview();
    createTuningSession.mockRejectedValue(new Error('NODE_NOT_TUNABLE'));
    fireEvent.click(screen.getAllByRole('button', { name: 'Open session' })[0]);
    await waitFor(() => expect(screen.getByText(/NODE_NOT_TUNABLE/)).toBeTruthy());
    expect(navigateMock).not.toHaveBeenCalled();
  });
});

describe('the empty listing', () => {
  it('explains that no workflow has an anomaly-mode node', async () => {
    listTuningWorkflows.mockResolvedValue(overview({ workflows: [], count: 0 }));
    render(<AnomalyTuningOverview />);
    await waitFor(() =>
      expect(screen.getByTestId('no-tunable-workflows').textContent).toBe(
        NO_TUNABLE_WORKFLOWS_MESSAGE
      )
    );
  });
});

// ------------------------------------------------------------- Requirement 1.6

describe('Requirement 1.6: the export-disabled explanation', () => {
  it('explains the export toggle and links to the use case settings', async () => {
    await renderOverview(overview({ sampleExportEnabled: false }));
    const alert = screen.getByTestId('export-disabled-explanation');
    expect(alert.textContent).toContain(EXPORT_DISABLED_MESSAGE);
    expect(alert.textContent).toContain('redeployed');
    const link = within(alert).getByRole('link', { name: 'Use case settings' });
    expect(link.getAttribute('href')).toBe(USECASE_SETTINGS_HREF);
    expect(USECASE_SETTINGS_HREF).toBe('/usecases');
    fireEvent.click(link);
    expect(navigateMock).toHaveBeenCalledWith(USECASE_SETTINGS_HREF);
  });

  it('is absent while export is enabled', async () => {
    await renderOverview();
    expect(screen.queryByTestId('export-disabled-explanation')).toBeNull();
  });

  it('warns separately that the counts read 0 when the store cannot be listed', async () => {
    await renderOverview(overview({ sampleStoreError: 'AccessDenied' }));
    expect(screen.getByText(/counts below read 0: AccessDenied/)).toBeTruthy();
  });
});

// ------------------------------------------- Requirement 1.3's landing spot

describe('the ?workflowId= preselection', () => {
  it('narrows the listing to the preselected workflow and says so', async () => {
    searchParamsRef.current = new URLSearchParams({ workflowId: 'wf-1' });
    await renderOverview();
    expect(listTuningWorkflows).toHaveBeenCalledWith('uc-1', 'wf-1');
    expect(screen.getByTestId('workflow-preselection').textContent).toContain(
      'wf-1'
    );
  });

  it('clears the preselection on "Show all workflows"', async () => {
    searchParamsRef.current = new URLSearchParams({ workflowId: 'wf-1' });
    await renderOverview();
    fireEvent.click(screen.getByRole('button', { name: 'Show all workflows' }));
    expect(setSearchParamsMock).toHaveBeenCalledTimes(1);
    const [next, options] = setSearchParamsMock.mock.calls[0];
    expect((next as URLSearchParams).get('workflowId')).toBeNull();
    expect(options).toEqual({ replace: true });
  });

  it('says the preselected workflow has no tunable node when it matches nothing', async () => {
    searchParamsRef.current = new URLSearchParams({ workflowId: 'wf-9' });
    listTuningWorkflows.mockResolvedValue(overview({ workflows: [], count: 0 }));
    render(<AnomalyTuningOverview />);
    await waitFor(() =>
      expect(screen.getByTestId('no-tunable-workflows').textContent).toContain(
        'Workflow wf-9 has no anomaly-mode'
      )
    );
  });
});
