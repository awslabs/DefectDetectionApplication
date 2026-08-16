/**
 * Render tests for the build server Fleet page
 * (portal-build-fleet-and-workflow-gates Requirements 6.1, 6.6, 6.12):
 * the fleet table shows every Requirement 6.1 column, the terminate
 * confirmation modal only submits when the typed text matches the server
 * name exactly (Requirement 6.6), and cancelling the confirmation sends
 * no terminate request and leaves the server unchanged (Requirement 6.12).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import FleetPage from './FleetPage';

const {
  listBuildServers,
  launchBuildServer,
  startBuildServer,
  stopBuildServer,
  terminateBuildServer,
  useAuthMock,
  navigateMock,
} = vi.hoisted(() => ({
  listBuildServers: vi.fn(),
  launchBuildServer: vi.fn(),
  startBuildServer: vi.fn(),
  stopBuildServer: vi.fn(),
  terminateBuildServer: vi.fn(),
  useAuthMock: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    listBuildServers,
    launchBuildServer,
    startBuildServer,
    stopBuildServer,
    terminateBuildServer,
  },
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

const LAST_CHANGE_MS = 1700000000000;

const SERVERS = [
  {
    server_id: 'srv-1',
    name: 'arm-builder',
    instance_id: 'i-0123456789abcdef0',
    instance_type: 'm6g.4xlarge',
    cpu_architecture: 'arm64',
    lifecycle_state: 'running',
    running_build_job_id: 'job-42',
    last_state_change_at: LAST_CHANGE_MS,
  },
  {
    server_id: 'srv-2',
    name: 'x86-builder',
    instance_id: 'i-0fedcba9876543210',
    instance_type: 'm6i.4xlarge',
    cpu_architecture: 'x86_64',
    lifecycle_state: 'stopped',
    running_build_job_id: null,
    last_state_change_at: null,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { role: 'PortalAdmin' } });
  listBuildServers.mockResolvedValue({ servers: SERVERS });
  terminateBuildServer.mockResolvedValue({ server: SERVERS[1] });
});

/** Renders the page and waits for the fleet list to load. */
async function renderLoadedFleetPage() {
  render(<FleetPage />);
  await waitFor(() => {
    expect(screen.getByText('arm-builder')).toBeInTheDocument();
  });
}

/** Selects the named server row and opens the terminate modal. */
async function openTerminateModal(serverName: string) {
  await renderLoadedFleetPage();
  fireEvent.click(screen.getByRole('radio', { name: `Select ${serverName}` }));
  fireEvent.click(screen.getByRole('button', { name: 'Terminate' }));
  await waitFor(() => {
    expect(
      screen.getByText(`Terminate build server ${serverName}`)
    ).toBeInTheDocument();
  });
}

describe('FleetPage fleet list (Requirement 6.1)', () => {
  it('shows every server with name, instance id, type, architecture, lifecycle state, running job, and last state change', async () => {
    await renderLoadedFleetPage();

    // Column headers of Requirement 6.1.
    for (const header of [
      'Name',
      'Instance ID',
      'Type',
      'Architecture',
      'Lifecycle state',
      'Running build job',
      'Last state change',
    ]) {
      expect(
        screen.getByRole('columnheader', { name: header })
      ).toBeInTheDocument();
    }

    // Row content: the running ARM64 server with its Build_Job link.
    expect(screen.getByText('arm-builder')).toBeInTheDocument();
    expect(screen.getByText('i-0123456789abcdef0')).toBeInTheDocument();
    expect(screen.getByText('m6g.4xlarge')).toBeInTheDocument();
    expect(screen.getByText('ARM64')).toBeInTheDocument();
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'job-42' })).toBeInTheDocument();
    expect(
      screen.getByText(new Date(LAST_CHANGE_MS).toLocaleString())
    ).toBeInTheDocument();

    // Row content: the stopped x86_64 server with no running job and no
    // recorded state change (both render as '-').
    expect(screen.getByText('x86-builder')).toBeInTheDocument();
    expect(screen.getByText('i-0fedcba9876543210')).toBeInTheDocument();
    expect(screen.getByText('m6i.4xlarge')).toBeInTheDocument();
    expect(screen.getByText('x86_64')).toBeInTheDocument();
    expect(screen.getByText('stopped')).toBeInTheDocument();
    expect(screen.getAllByText('-')).toHaveLength(2);
  });
});

describe('FleetPage launch modal Ubuntu version (jetpack7-support design §10)', () => {
  /** Opens the launch modal from the table header. */
  async function openLaunchModal() {
    await renderLoadedFleetPage();
    fireEvent.click(screen.getByRole('button', { name: 'Launch server' }));
    await waitFor(() => {
      expect(screen.getByText('Launch build server')).toBeInTheDocument();
    });
  }

  /** The modal footer submit (the header button shares the label). */
  function submitButton() {
    const buttons = screen.getAllByRole('button', { name: 'Launch server' });
    return buttons[buttons.length - 1];
  }

  beforeEach(() => {
    launchBuildServer.mockResolvedValue({ server: SERVERS[0] });
  });

  it('defaults ARM64 servers to Ubuntu 22.04', async () => {
    await openLaunchModal();

    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'new-arm' },
    });
    fireEvent.click(screen.getByRole('radio', { name: 'ARM64' }));

    // The Ubuntu version choice appears for ARM64, defaulted to 22.04.
    expect(
      screen.getByRole('radio', { name: 'Ubuntu 22.04 (default)' })
    ).toBeChecked();

    fireEvent.click(submitButton());
    await waitFor(() => {
      expect(launchBuildServer).toHaveBeenCalledWith({
        name: 'new-arm',
        architecture: 'arm64',
        ubuntu_version: '22.04',
      });
    });
  });

  it('launches an Ubuntu 24.04 (JP7) ARM64 server when selected', async () => {
    await openLaunchModal();

    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'jp7-builder' },
    });
    fireEvent.click(screen.getByRole('radio', { name: 'ARM64' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Ubuntu 24.04' }));

    fireEvent.click(submitButton());
    await waitFor(() => {
      expect(launchBuildServer).toHaveBeenCalledWith({
        name: 'jp7-builder',
        architecture: 'arm64',
        ubuntu_version: '24.04',
      });
    });
  });

  it('hides the Ubuntu choice for x86_64 and resets a 24.04 selection to 22.04', async () => {
    await openLaunchModal();

    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'x86-new' },
    });
    // Pick ARM64 + 24.04, then switch to x86_64: 24.04 is arm64-only.
    fireEvent.click(screen.getByRole('radio', { name: 'ARM64' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Ubuntu 24.04' }));
    fireEvent.click(screen.getByRole('radio', { name: 'x86_64' }));

    expect(
      screen.queryByRole('radio', { name: 'Ubuntu 24.04' })
    ).toBeNull();

    fireEvent.click(submitButton());
    await waitFor(() => {
      expect(launchBuildServer).toHaveBeenCalledWith({
        name: 'x86-new',
        architecture: 'x86_64',
        ubuntu_version: '22.04',
      });
    });
  });
});

describe('FleetPage terminate confirmation (Requirements 6.6, 6.12)', () => {
  it('requires the exact typed server name before the terminate request can be sent (Requirement 6.6)', async () => {
    await openTerminateModal('x86-builder');

    const confirmInput = screen.getByLabelText('Confirm server name');
    const terminateButton = screen.getByRole('button', {
      name: 'Terminate server',
    });

    // Nothing typed: submission is not possible.
    expect(terminateButton).toBeDisabled();

    // A non-matching name keeps submission blocked and clicking sends
    // no terminate request.
    fireEvent.change(confirmInput, { target: { value: 'wrong-name' } });
    expect(terminateButton).toBeDisabled();
    fireEvent.click(terminateButton);
    expect(terminateBuildServer).not.toHaveBeenCalled();

    // The exact server name enables submission, and submitting sends
    // the terminate request with the typed confirmation echo.
    fireEvent.change(confirmInput, { target: { value: 'x86-builder' } });
    expect(terminateButton).not.toBeDisabled();
    fireEvent.click(terminateButton);
    await waitFor(() => {
      expect(terminateBuildServer).toHaveBeenCalledWith(
        'srv-2',
        'x86-builder'
      );
    });
  });

  it('cancelling the confirmation sends no terminate request and leaves the server unchanged (Requirement 6.12)', async () => {
    await openTerminateModal('x86-builder');

    // Even with the exact name typed, Cancel submits nothing.
    fireEvent.change(screen.getByLabelText('Confirm server name'), {
      target: { value: 'x86-builder' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(
        screen.queryByText('Terminate build server x86-builder')
      ).toBeNull();
    });
    expect(terminateBuildServer).not.toHaveBeenCalled();
    expect(startBuildServer).not.toHaveBeenCalled();
    expect(stopBuildServer).not.toHaveBeenCalled();
    // The server is still listed unchanged.
    expect(screen.getByText('x86-builder')).toBeInTheDocument();
    expect(screen.getByText('stopped')).toBeInTheDocument();
  });
});
