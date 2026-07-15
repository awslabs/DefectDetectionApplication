/**
 * Component tests for the deployment screen's Plugin_Component UI pieces
 * (custom-node-designer task 12.7, Requirement 16.2): supported
 * Target_Architecture chips on `dda.plugin.*` listing entries, and the
 * pre-submit gate rejection alert identifying each Plugin_Component with
 * its lifecycle violation (16.3) or unsupported architecture (16.6).
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ArchitectureChips, PluginGateRejectionAlert } from './PluginComponentUi';
import type { PluginGateRejection } from './pluginComponents';

describe('ArchitectureChips (16.2)', () => {
  it('renders one labeled chip per supported Target_Architecture', () => {
    render(
      <ArchitectureChips architectures={['x86_64', 'x86_64_nvidia', 'arm64_jp5']} />
    );
    expect(screen.getByText('x86_64')).toBeInTheDocument();
    expect(screen.getByText('x86_64 (NVIDIA GPU)')).toBeInTheDocument();
    expect(screen.getByText('arm64 JetPack 5')).toBeInTheDocument();
  });

  it('renders a placeholder when no architectures are recorded', () => {
    render(<ArchitectureChips architectures={[]} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});

describe('PluginGateRejectionAlert (16.3, 16.6)', () => {
  it('identifies the Plugin_Component and lifecycle violation', () => {
    const rejection: PluginGateRejection = {
      code: 'PLUGIN_LIFECYCLE_VIOLATION',
      message: 'Deployment rejected by the plugin lifecycle gate',
      lifecycleViolations: [
        {
          pluginComponent: 'dda.plugin.edgefilter',
          lifecycleState: 'test',
          devices: ['line-a-camera-01'],
          version: '2.0.0',
        },
      ],
      archUnsupported: [],
    };
    render(<PluginGateRejectionAlert rejection={rejection} onDismiss={vi.fn()} />);

    expect(
      screen.getByText('Deployment rejected: plugin lifecycle violation')
    ).toBeInTheDocument();
    const item = screen.getByRole('listitem');
    expect(item.textContent).toContain('dda.plugin.edgefilter');
    expect(item.textContent).toContain('test');
    expect(item.textContent).toContain('line-a-camera-01');
  });

  it('identifies the Plugin_Component and unsupported device architecture', () => {
    const rejection: PluginGateRejection = {
      code: 'PLUGIN_ARCH_UNSUPPORTED',
      message: 'Deployment rejected by the plugin architecture gate',
      lifecycleViolations: [],
      archUnsupported: [
        {
          pluginComponent: 'dda.plugin.edgefilter',
          version: '2.0.0',
          device: 'bench-01',
          deviceArch: 'arm64_jp6',
        },
      ],
    };
    render(<PluginGateRejectionAlert rejection={rejection} onDismiss={vi.fn()} />);

    expect(
      screen.getByText('Deployment rejected: unsupported device architecture')
    ).toBeInTheDocument();
    const item = screen.getByRole('listitem');
    expect(item.textContent).toContain('dda.plugin.edgefilter v2.0.0');
    expect(item.textContent).toContain('bench-01');
    expect(item.textContent).toContain('arm64 JetPack 6');
  });
});
