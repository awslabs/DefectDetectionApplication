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
});
