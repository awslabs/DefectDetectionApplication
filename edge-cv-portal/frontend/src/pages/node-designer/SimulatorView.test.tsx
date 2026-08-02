/**
 * Component tests for the Plugin_Simulator view (custom-node-designer
 * task 12.7, Requirements 7.1, 7.4): starting a run against a
 * Use_Case-scoped Test_Dataset (7.1), re-running with changed parameter
 * values (7.4), and the missing-x86_64 refusal shown up front (7.5).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import SimulatorView from './SimulatorView';
import { MISSING_X86_64_MESSAGE } from './simulation';

const { navigateMock, getVersion, listTestDatasets, startSimulation, getSimulation } =
  vi.hoisted(() => ({
    navigateMock: vi.fn(),
    getVersion: vi.fn(),
    listTestDatasets: vi.fn(),
    startSimulation: vi.fn(),
    getSimulation: vi.fn(),
  }));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ pluginId: 'p-1', version: '1' }),
}));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  return { ApiError, apiService: {} };
});

vi.mock('./api', () => ({
  nodeDesignerApi: { getVersion, listTestDatasets, startSimulation, getSimulation },
}));

function pluginDetail(artifacts: Record<string, unknown>) {
  return {
    plugin: {
      plugin_id: 'p-1',
      version: 1,
      usecase_id: 'uc-1',
      name: 'blur-regions',
      description: '',
      kind: 'scaffold',
      deepstream: false,
      provenance: {},
      lifecycle_state: 'dev',
      review: { decision: 'pending' },
      artifacts,
      component: {},
      source_s3_prefix: 'plugin-sources/uc-1/p-1/1/',
      created_by: 'user',
      created_at: 1,
      updated_at: 1,
    },
  };
}

const BUILT = pluginDetail({
  x86_64: { buildStatus: 'succeeded', s3Key: 'workflow-plugins/custom/uc-1/x86_64/blur.so' },
});

const COMPLETED_RUN = {
  run_id: 'r-1',
  plugin_id: 'p-1',
  version: 1,
  usecase_id: 'uc-1',
  dataset: { kind: 'dataset', dataset_id: 'd-1' },
  parameters: {},
  element_factory: 'blurregions',
  status: 'completed',
  results_s3_key: null,
  failure: null,
  started_at: 1,
  finished_at: 2,
  created_by: 'user',
};

beforeEach(() => {
  vi.clearAllMocks();
  getVersion.mockResolvedValue(BUILT);
  listTestDatasets.mockResolvedValue({
    datasets: [{ dataset_id: 'd-1', usecase_id: 'uc-1', name: 'Line A frames', file_count: 12 }],
    count: 1,
  });
  startSimulation.mockResolvedValue({ simulation_run: COMPLETED_RUN });
});

describe('SimulatorView', () => {
  it('starts a run against a selected Use_Case Test_Dataset (7.1)', async () => {
    const { container } = render(<SimulatorView />);
    await screen.findByText('Simulate blur-regions v1');
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));

    // Dataset picker offers the Use_Case's Test_Datasets.
    const datasetSelect = createWrapper(container).findSelect()!;
    datasetSelect.openDropdown();
    datasetSelect.selectOptionByValue('d-1');

    fireEvent.click(screen.getByRole('button', { name: 'Run simulation' }));
    await waitFor(() =>
      expect(startSimulation).toHaveBeenCalledWith('p-1', 1, {
        parameters: {},
        dataset_id: 'd-1',
      })
    );
    // The completed run renders its results section.
    await screen.findByText('Results');
  });

  it('re-runs with changed parameter values (7.4)', async () => {
    const { container } = render(<SimulatorView />);
    await screen.findByText('Simulate blur-regions v1');

    const datasetSelect = createWrapper(container).findSelect()!;
    datasetSelect.openDropdown();
    datasetSelect.selectOptionByValue('d-1');
    fireEvent.click(screen.getByRole('button', { name: 'Run simulation' }));
    await waitFor(() => expect(startSimulation).toHaveBeenCalledTimes(1));

    // Edit parameter values and run again.
    fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }));
    fireEvent.change(screen.getByLabelText('Parameter 1 name'), {
      target: { value: 'radius' },
    });
    fireEvent.change(screen.getByLabelText('Parameter 1 value'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Re-run simulation' }));

    await waitFor(() => expect(startSimulation).toHaveBeenCalledTimes(2));
    expect(startSimulation).toHaveBeenLastCalledWith('p-1', 1, {
      parameters: { radius: 5 },
      dataset_id: 'd-1',
    });
  });

  it('shows the refusal and disables the run when no successful x86_64 build exists (7.5)', async () => {
    getVersion.mockResolvedValue(pluginDetail({}));
    render(<SimulatorView />);
    await screen.findByText('Simulation unavailable');
    expect(screen.getByText(MISSING_X86_64_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run simulation' })).toBeDisabled();
  });
});
