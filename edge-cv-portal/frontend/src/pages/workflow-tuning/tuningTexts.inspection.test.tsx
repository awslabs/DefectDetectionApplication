/**
 * The workflow tuning texts name inspection-format Bedrock nodes (AC-17a,
 * FR-13.4).
 *
 * Inspection-format Bedrock nodes are tunable whatever their
 * `anomaly_mode`, so every text that says which nodes tuning covers names
 * them too: the apply refusal, the overview's empty states and header, the
 * landing page's tool description, the Samples tab's empty state and the
 * reason the Synthetic negatives toggle is off. The existing tests that pin
 * the exported constants and the overview's "Workflow {id} has no
 * anomaly-mode" prefix keep passing.
 *
 * At c299a6e1 the exported texts do not contain "inspection-format".
 *
 * No AWS and no network: `apiService`, `UsecaseContext` and the router
 * hooks are mocked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { NODE_NOT_TUNABLE_MESSAGE } from './ApplyTab';
import AnomalyTuningOverview, {
  NO_TUNABLE_WORKFLOWS_MESSAGE,
} from './AnomalyTuningOverview';
import SamplesTab from './SamplesTab';
import { WORKFLOW_TUNING_TOOLS } from './WorkflowTuningLanding';

const {
  searchParamsRef,
  listUseCases,
  listTuningWorkflows,
  createTuningSession,
  listTuningSamples,
  useUsecaseMock,
} = vi.hoisted(() => ({
  searchParamsRef: { current: new URLSearchParams() },
  listUseCases: vi.fn(),
  listTuningWorkflows: vi.fn(),
  createTuningSession: vi.fn(),
  listTuningSamples: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => vi.fn(),
    useSearchParams: () => [searchParamsRef.current, vi.fn()] as const,
  };
});

vi.mock('../../services/api', () => ({
  apiService: {
    listUseCases,
    listTuningWorkflows,
    createTuningSession,
    listTuningSamples,
    setTuningSampleLabels: vi.fn(),
    setTuningSyntheticNegatives: vi.fn(),
  },
}));

vi.mock('../../contexts/UsecaseContext', () => ({ useUsecase: useUsecaseMock }));

const TEXTS = {
  apply:
    'The target node no longer exists in the latest workflow version, or is '
    + 'no longer an anomaly-mode Bedrock or VLM inspection node or an '
    + 'inspection-format Bedrock node, so the prompt set cannot be applied.',
  noTunableWorkflows:
    'No workflow in this use case has an anomaly-mode Bedrock or VLM '
    + 'inspection node, or an inspection-format Bedrock node, in its latest '
    + 'version.',
  preselected:
    'Workflow wf-9 has no anomaly-mode Bedrock or VLM inspection node, or '
    + 'inspection-format Bedrock node, in its latest version.',
  header:
    'Tune the prompts of anomaly-mode Bedrock and VLM inspection nodes, and '
    + 'of inspection-format Bedrock nodes, against the images your devices '
    + 'really sent',
  landing:
    'Collect the images your anomaly-mode Bedrock and VLM inspection nodes '
    + 'and your inspection-format Bedrock nodes really sent, label them, '
    + 'score candidate prompts against them, and apply the winner as a new '
    + 'workflow version.',
  noSamples:
    'No sample matches these filters. Devices export samples after every '
    + 'anomaly-mode or inspection-format invocation that returns an answer; '
    + 'use "Refresh samples" to index newly exported ones.',
  syntheticUnsupported:
    'Not available for inspection-format nodes: their reference image (for '
    + 'example a legend) is not a per-part golden sample, so cross-paired '
    + 'images are not defects.',
};

function normalized(text: string | null | undefined): string {
  return (text ?? '').replace(/\s+/g, ' ').trim();
}

beforeEach(() => {
  vi.clearAllMocks();
  searchParamsRef.current = new URLSearchParams();
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line inspection' }],
  });
});

afterEach(() => {
  cleanup();
});

describe('the exported tuning texts', () => {
  it('name inspection-format Bedrock nodes', async () => {
    const { SYNTHETIC_UNSUPPORTED_REASON } = await import('./SamplesTab');
    expect(NODE_NOT_TUNABLE_MESSAGE).toBe(TEXTS.apply);
    expect(NO_TUNABLE_WORKFLOWS_MESSAGE).toBe(TEXTS.noTunableWorkflows);
    expect(WORKFLOW_TUNING_TOOLS[0].description).toBe(TEXTS.landing);
    expect(SYNTHETIC_UNSUPPORTED_REASON).toBe(TEXTS.syntheticUnsupported);
    for (const text of [
      NODE_NOT_TUNABLE_MESSAGE,
      NO_TUNABLE_WORKFLOWS_MESSAGE,
      WORKFLOW_TUNING_TOOLS[0].description,
    ]) {
      expect(text).toContain('inspection-format');
    }
  });
});

describe('the rendered tuning texts', () => {
  it('the overview header and its preselected empty state', async () => {
    searchParamsRef.current = new URLSearchParams({ workflowId: 'wf-9' });
    listTuningWorkflows.mockResolvedValue({
      usecaseId: 'uc-1', sampleExportEnabled: true, sampleRetentionDays: 30,
      workflows: [], count: 0, sampleStoreError: null,
    });
    render(<AnomalyTuningOverview />);
    await waitFor(() =>
      expect(normalized(screen.getByTestId('no-tunable-workflows').textContent)).toBe(
        TEXTS.preselected
      )
    );
    expect(screen.getByText(TEXTS.header)).toBeTruthy();
  });

  it('the Samples tab empty state', async () => {
    listTuningSamples.mockResolvedValue({
      sessionId: 'ts-1', samples: [], count: 0, matched: 0, nextCursor: null,
      expiresInSeconds: 1800,
      labelCounts: {
        OK: 0, NOK: 0, EXCLUDE: 0, unlabelled: 0, synthetic: 0,
        duplicates: 0, total: 0,
      },
    });
    render(
      <SamplesTab
        sessionId="ts-1"
        syntheticEnabled={false}
        labelCounts={null}
        onCountsChange={vi.fn()}
        onDevicesSeen={vi.fn()}
        onSyntheticChange={vi.fn()}
      />
    );
    await waitFor(() =>
      expect(normalized(screen.getByTestId('no-samples').textContent)).toBe(
        TEXTS.noSamples
      )
    );
  });
});
