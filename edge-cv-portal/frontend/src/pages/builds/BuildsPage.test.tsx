/**
 * Render tests for the Builds page submit form
 * (portal-build-fleet-and-workflow-gates Requirements 1.1, 2.1, 2.5):
 *
 * - the four supported Build_Targets are selectable and the submit
 *   control posts the selection in order (Req 1.1),
 * - the execution-mode RadioGroup offers ephemeral and dedicated, with
 *   the dedicated option listing only running fleet servers (Req 2.1),
 * - ephemeral is the only selectable mode while the fleet has no
 *   non-terminated server (Req 2.5).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import BuildsPage from './BuildsPage';
import type { BuildJob } from './types';
import type { BuildServer } from '../../services/api';

const { listBuilds, listBuildServers, submitBuild, cancelBuild, retryBuild, navigateMock } =
  vi.hoisted(() => ({
    listBuilds: vi.fn(),
    listBuildServers: vi.fn(),
    submitBuild: vi.fn(),
    cancelBuild: vi.fn(),
    retryBuild: vi.fn(),
    navigateMock: vi.fn(),
  }));

vi.mock('../../services/api', () => ({
  apiService: { listBuilds, listBuildServers, submitBuild, cancelBuild, retryBuild },
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

function server(overrides: Partial<BuildServer>): BuildServer {
  return {
    server_id: 's-1',
    name: 'build-server-1',
    instance_id: 'i-0abc',
    instance_type: 'm6g.4xlarge',
    cpu_architecture: 'arm64',
    lifecycle_state: 'running',
    running_build_job_id: null,
    ...overrides,
  } as BuildServer;
}

const CREATED_JOB: BuildJob = {
  build_job_id: 'job-new-1',
  request_id: 'r-1',
  request_order: 0,
  predecessor_job_id: null,
  build_target: 'JP5',
  component_name: 'aws.edgeml.dda.LocalServer.arm64JP5',
  required_arch: 'arm64',
  execution_mode: 'ephemeral',
  server_id: null,
  status: 'queued',
  requested_by: 'alice',
  created_at: 1_700_000_000_000,
};

beforeEach(() => {
  vi.clearAllMocks();
  listBuilds.mockResolvedValue({ jobs: [], nextToken: null, total: 0 });
  listBuildServers.mockResolvedValue({ servers: [] });
  submitBuild.mockResolvedValue({ request_id: 'r-1', jobs: [CREATED_JOB] });
});

async function renderPage() {
  const utils = render(<BuildsPage />);
  await waitFor(() => expect(listBuilds).toHaveBeenCalled());
  // Initial load finished once the empty-history state renders.
  await screen.findByText('No builds');
  return utils;
}

describe('BuildsPage submit form', () => {
  it('offers the four supported Build_Targets and submits the selection in order (Requirement 1.1)', async () => {
    const { container } = await renderPage();

    const targets = createWrapper(container).findMultiselect()!;
    targets.openDropdown();

    // All four supported Build_Targets are selectable (Req 1.1).
    const optionLabels = targets
      .findDropdown()
      .findOptions()
      .map((o) => o.findLabel().getElement().textContent);
    expect(optionLabels).toEqual(['JP5', 'JP6', 'AMD64', 'AMD64_NVIDIA']);

    // Select two targets; selection order defines the request order.
    targets.selectOptionByValue('JP6');
    targets.selectOptionByValue('AMD64_NVIDIA');

    fireEvent.click(screen.getByRole('button', { name: 'Submit build request' }));

    await waitFor(() => expect(submitBuild).toHaveBeenCalledTimes(1));
    expect(submitBuild).toHaveBeenCalledWith({
      targets: ['JP6', 'AMD64_NVIDIA'],
      execution_mode: 'ephemeral',
    });
    await screen.findByText('Build request submitted: 1 job created.');
  });

  it('rejects an empty target selection client-side without calling the API (Requirement 1.1)', async () => {
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Submit build request' }));

    expect(await screen.findByText('Select at least one build target.')).toBeInTheDocument();
    expect(submitBuild).not.toHaveBeenCalled();
  });

  it('lets the user pick the dedicated mode and a running server (Requirement 2.1)', async () => {
    listBuildServers.mockResolvedValue({
      servers: [
        server({ server_id: 's-run', name: 'arm-builder', lifecycle_state: 'running' }),
        server({ server_id: 's-stop', name: 'stopped-builder', lifecycle_state: 'stopped' }),
      ],
    });
    const { container } = await renderPage();

    // Both execution modes are offered and enabled (Req 2.1).
    const radioGroup = createWrapper(container).findRadioGroup()!;
    const ephemeralInput = radioGroup.findInputByValue('ephemeral')!.getElement();
    const dedicatedInput = radioGroup.findInputByValue('dedicated')!.getElement();
    expect(ephemeralInput).toBeChecked();
    expect(dedicatedInput).not.toBeDisabled();

    fireEvent.click(dedicatedInput);

    // The dedicated server picker lists only running servers (Req 2.1).
    const serverSelect = createWrapper(container).findSelect()!;
    serverSelect.openDropdown();
    const serverOptions = serverSelect.findDropdown().findOptions();
    expect(serverOptions).toHaveLength(1);
    expect(serverOptions[0].findLabel().getElement()).toHaveTextContent('arm-builder');
    serverSelect.selectOptionByValue('s-run');

    const targets = createWrapper(container).findMultiselect()!;
    targets.openDropdown();
    targets.selectOptionByValue('JP5');

    fireEvent.click(screen.getByRole('button', { name: 'Submit build request' }));

    await waitFor(() => expect(submitBuild).toHaveBeenCalledTimes(1));
    expect(submitBuild).toHaveBeenCalledWith({
      targets: ['JP5'],
      execution_mode: 'dedicated',
      server_id: 's-run',
    });
  });

  it('requires a server selection before submitting a dedicated build (Requirement 2.1)', async () => {
    listBuildServers.mockResolvedValue({
      servers: [server({ server_id: 's-run', name: 'arm-builder', lifecycle_state: 'running' })],
    });
    const { container } = await renderPage();

    const radioGroup = createWrapper(container).findRadioGroup()!;
    fireEvent.click(radioGroup.findInputByValue('dedicated')!.getElement());

    const targets = createWrapper(container).findMultiselect()!;
    targets.openDropdown();
    targets.selectOptionByValue('JP5');

    fireEvent.click(screen.getByRole('button', { name: 'Submit build request' }));

    expect(
      await screen.findByText('Select the dedicated build server to use.'),
    ).toBeInTheDocument();
    expect(submitBuild).not.toHaveBeenCalled();
  });

  it('presents ephemeral as the only selectable mode when the fleet has no non-terminated server (Requirement 2.5)', async () => {
    listBuildServers.mockResolvedValue({
      servers: [
        server({ server_id: 's-dead', name: 'old-builder', lifecycle_state: 'terminated' }),
      ],
    });
    const { container } = await renderPage();

    const radioGroup = createWrapper(container).findRadioGroup()!;
    expect(radioGroup.findInputByValue('ephemeral')!.getElement()).toBeChecked();
    expect(radioGroup.findInputByValue('dedicated')!.getElement()).toBeDisabled();
    expect(
      screen.getByText(
        'No dedicated build servers exist in the fleet, so ephemeral compute is the only available mode.',
      ),
    ).toBeInTheDocument();
  });
});
