/**
 * Component tests for the build infrastructure configuration settings
 * section (portal-build-fleet-and-workflow-gates Requirements 9.1, 9.5):
 * the form loads the effective configuration, is editable only for
 * PortalAdmin, and surfaces the per-parameter errors of a CONFIG_INVALID
 * rejection from PUT /build-config on the matching form fields.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import BuildInfrastructureSettings from './BuildInfrastructureSettings';

const { getBuildConfig, updateBuildConfig, useAuthMock } = vi.hoisted(() => ({
  getBuildConfig: vi.fn(),
  updateBuildConfig: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: { getBuildConfig, updateBuildConfig },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

const EFFECTIVE_CONFIG = {
  arm64_instance_type: 'm6g.4xlarge',
  x86_64_instance_type: 'm6i.4xlarge',
  volume_size_gb: 100,
  region: 'us-east-1',
  max_runtime_hours: 4,
  use_spot_for_ephemeral: false,
  source_ref: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  getBuildConfig.mockResolvedValue({ config: EFFECTIVE_CONFIG });
  updateBuildConfig.mockResolvedValue({ config: EFFECTIVE_CONFIG, changes: [] });
});

function setAuthRole(role: string | null) {
  useAuthMock.mockReturnValue({ user: role ? { role } : null });
}

describe('BuildInfrastructureSettings', () => {
  it('shows the effective configuration for PortalAdmin (Requirement 9.1)', async () => {
    setAuthRole('PortalAdmin');
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('ARM64 instance type')).toHaveValue('m6g.4xlarge');
    });
    expect(screen.getByLabelText('x86_64 instance type')).toHaveValue('m6i.4xlarge');
    expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    expect(screen.getByLabelText('Region')).toHaveValue('us-east-1');
    expect(screen.getByLabelText('Max runtime hours')).toHaveValue(4);
    // null source_ref means "repository default branch" and renders blank.
    expect(screen.getByLabelText('Source ref')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save Configuration' })).toBeInTheDocument();
  });

  it.each(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin'])(
    'shows an access notice instead of the form for %s (Requirement 9.6)',
    (role) => {
      setAuthRole(role);
      render(<BuildInfrastructureSettings />);

      expect(screen.getByText('Portal Admin access required')).toBeInTheDocument();
      expect(screen.queryByLabelText('ARM64 instance type')).toBeNull();
      expect(getBuildConfig).not.toHaveBeenCalled();
    },
  );

  it('surfaces CONFIG_INVALID per-parameter errors on the matching fields (Requirement 9.5)', async () => {
    setAuthRole('PortalAdmin');
    const rejection = Object.assign(
      new Error('The configuration update is invalid and was rejected in full.'),
      {
        code: 'CONFIG_INVALID',
        status: 400,
        details: {
          errors: [
            {
              rule: 'config_instance_type_arch_mismatch',
              parameter: 'arm64_instance_type',
              message:
                "Invalid value for arm64_instance_type: instance type 'm6i.4xlarge' " +
                "(family 'm6i') has CPU architecture 'x86_64', but arm64_instance_type " +
                "requires an instance type with CPU architecture 'arm64'.",
            },
            {
              rule: 'config_volume_size_invalid',
              parameter: 'volume_size_gb',
              message: "Invalid value for volume_size_gb: '-5' is not a positive number.",
            },
          ],
        },
      },
    );
    updateBuildConfig.mockRejectedValue(rejection);
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('ARM64 instance type')).toHaveValue('m6g.4xlarge');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    // Each per-parameter error appears on its form field (Req 9.5) and
    // the alert notes the atomic reject (prior values retained).
    await waitFor(() => {
      expect(
        screen.getByText(/instance type 'm6i\.4xlarge' \(family 'm6i'\) has CPU architecture 'x86_64'/),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/'-5' is not a positive number/)).toBeInTheDocument();
    expect(
      screen.getByText(/rejected; the prior values are retained/),
    ).toBeInTheDocument();
  });

  it('saves the edited configuration and reports the applied change count (Requirement 9.1)', async () => {
    setAuthRole('PortalAdmin');
    updateBuildConfig.mockResolvedValue({
      config: { ...EFFECTIVE_CONFIG, volume_size_gb: 200 },
      changes: [{ parameter: 'volume_size_gb', prior_value: 100, new_value: 200 }],
    });
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    });

    fireEvent.change(screen.getByLabelText('Volume size GB'), { target: { value: '200' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Build configuration saved (1 parameter changed)')).toBeInTheDocument();
    });
    expect(updateBuildConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        arm64_instance_type: 'm6g.4xlarge',
        x86_64_instance_type: 'm6i.4xlarge',
        volume_size_gb: 200,
        region: 'us-east-1',
        max_runtime_hours: 4,
        use_spot_for_ephemeral: false,
        // Blank source ref reverts to the repository default branch.
        source_ref: null,
      }),
    );
    // The form reflects the returned effective configuration.
    expect(screen.getByLabelText('Volume size GB')).toHaveValue(200);
  });

  // -------------------------------------------------------------------
  // build-fleet-execution-failures tasks 8.4/8.5 (Req 2.17, 2.19, 2.20,
  // 3.6, 3.12, 3.13): optional runtime budgets and per-target volume
  // sizes, legacy payload preservation, and the explanatory help text.
  // -------------------------------------------------------------------

  it('keeps the legacy payload shape when no optional map is configured (Requirement 3.6)', async () => {
    setAuthRole('PortalAdmin');
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBuildConfig).toHaveBeenCalled();
    });
    const payload = updateBuildConfig.mock.calls[0][0];
    // The optional maps are OMITTED, not sent as null/empty: an
    // unconfigured save is byte-compatible with the legacy payload.
    expect('runtime_budgets' in payload).toBe(false);
    expect('volume_size_gb_by_target' in payload).toBe(false);
    // Every legacy field is still present.
    expect(payload).toMatchObject({
      arm64_instance_type: 'm6g.4xlarge',
      x86_64_instance_type: 'm6i.4xlarge',
      volume_size_gb: 100,
      region: 'us-east-1',
      max_runtime_hours: 4,
      use_spot_for_ephemeral: false,
      source_ref: null,
    });
  });

  it('explains hard ceilings, independent queue/provisioning limits, and snapshot immutability (Requirements 2.17, 3.12, 3.13)', async () => {
    setAuthRole('PortalAdmin');
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    });

    // Hard ceilings are non-extendable.
    expect(
      screen.getByText(/non-extendable safety limit.*can never extend it/),
    ).toBeInTheDocument();
    // Queue/provisioning limits are independent and optional.
    expect(
      screen.getByText(/Queue-wait and provisioning limits are independent and optional/),
    ).toBeInTheDocument();
    // Changing settings does not mutate existing snapshots.
    expect(
      screen.getByText(/does not mutate existing Build_Jobs.*snapshotted when it was submitted/),
    ).toBeInTheDocument();
    // The global volume field stays, documents the raised 200 GB
    // default, and the per-target section names the JP6 minimum.
    expect(screen.getByText(/positive number \(default 200\)/)).toBeInTheDocument();
    expect(screen.getByText(/JP6 requires at least 200 GB/)).toBeInTheDocument();
  });

  it('sends an added runtime budget as the nested target/mode map (Requirement 2.17)', async () => {
    setAuthRole('PortalAdmin');
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add runtime budget' }));
    fireEvent.change(screen.getByLabelText('Runtime budget 1 target'), {
      target: { value: 'JP6' },
    });
    fireEvent.change(screen.getByLabelText('Runtime budget 1 mode'), {
      target: { value: 'ephemeral' },
    });
    fireEvent.change(screen.getByLabelText('Runtime budget 1 hard runtime hours'), {
      target: { value: '3' },
    });
    fireEvent.change(screen.getByLabelText('Runtime budget 1 heartbeat lease minutes'), {
      target: { value: '30' },
    });
    fireEvent.change(screen.getByLabelText('Runtime budget 1 queue wait hours'), {
      target: { value: '6' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBuildConfig).toHaveBeenCalled();
    });
    expect(updateBuildConfig.mock.calls[0][0]).toMatchObject({
      runtime_budgets: {
        JP6: {
          ephemeral: {
            hard_runtime_hours: 3,
            heartbeat_lease_minutes: 30,
            queue_wait_hours: 6,
          },
        },
      },
    });
  });

  it('sends an added per-target volume size while retaining the global field (Requirement 2.20)', async () => {
    setAuthRole('PortalAdmin');
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Volume size GB')).toHaveValue(100);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add per-target volume size' }));
    fireEvent.change(screen.getByLabelText('Per-target volume 1 target'), {
      target: { value: 'JP6' },
    });
    fireEvent.change(screen.getByLabelText('Per-target volume 1 size GB'), {
      target: { value: '400' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBuildConfig).toHaveBeenCalled();
    });
    expect(updateBuildConfig.mock.calls[0][0]).toMatchObject({
      // The global field is retained alongside the per-target map.
      volume_size_gb: 100,
      volume_size_gb_by_target: { JP6: 400 },
    });
  });

  it('loads stored maps into rows and reverts a cleared map with null (Requirement 3.6)', async () => {
    setAuthRole('PortalAdmin');
    getBuildConfig.mockResolvedValue({
      config: {
        ...EFFECTIVE_CONFIG,
        runtime_budgets: { AMD64: { dedicated: { hard_runtime_hours: 8 } } },
        volume_size_gb_by_target: { JP6: 400 },
      },
    });
    render(<BuildInfrastructureSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Runtime budget 1 target')).toHaveValue('AMD64');
    });
    expect(screen.getByLabelText('Runtime budget 1 hard runtime hours')).toHaveValue(8);
    expect(screen.getByLabelText('Per-target volume 1 target')).toHaveValue('JP6');
    expect(screen.getByLabelText('Per-target volume 1 size GB')).toHaveValue(400);

    // Clearing the stored rows reverts the stored maps with null.
    fireEvent.click(screen.getByRole('button', { name: 'Remove runtime budget 1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove per-target volume 1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBuildConfig).toHaveBeenCalled();
    });
    expect(updateBuildConfig.mock.calls[0][0]).toMatchObject({
      runtime_budgets: null,
      volume_size_gb_by_target: null,
    });
  });
});
