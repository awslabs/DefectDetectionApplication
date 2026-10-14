/**
 * Unit tests for the deployment screen Plugin_Component helpers
 * (custom-node-designer task 12.6, Requirements 16.2, 16.3, 16.6).
 */
import { describe, expect, it } from 'vitest';
import {
  PLUGIN_COMPONENT_PREFIX,
  architectureLabel,
  describeArchUnsupported,
  describeLifecycleViolation,
  isPluginComponent,
  parsePluginGateRejection,
} from './pluginComponents';

describe('isPluginComponent', () => {
  it('recognizes dda.plugin.* Greengrass component names (16.2)', () => {
    expect(isPluginComponent('dda.plugin.edgefilter')).toBe(true);
    expect(isPluginComponent(`${PLUGIN_COMPONENT_PREFIX}x`)).toBe(true);
  });

  it('rejects other component names', () => {
    expect(isPluginComponent('com.dda.localserver')).toBe(false);
    expect(isPluginComponent('aws.greengrass.Nucleus')).toBe(false);
    expect(isPluginComponent('dda.pluginish.thing')).toBe(false);
    expect(isPluginComponent(null)).toBe(false);
    expect(isPluginComponent(undefined)).toBe(false);
  });
});

describe('architectureLabel', () => {
  it('maps DDA Target_Architectures to human-readable chip labels (16.2)', () => {
    expect(architectureLabel('x86_64')).toBe('x86_64');
    expect(architectureLabel('x86_64_nvidia')).toBe('x86_64 (NVIDIA GPU)');
    expect(architectureLabel('arm64_jp5')).toBe('arm64 JetPack 5');
  });

  it('falls back to the raw value for unknown architectures', () => {
    expect(architectureLabel('riscv')).toBe('riscv');
  });
});

describe('parsePluginGateRejection', () => {
  it('parses a PLUGIN_LIFECYCLE_VIOLATION envelope identifying each component (16.3)', () => {
    const rejection = parsePluginGateRejection(
      'PLUGIN_LIFECYCLE_VIOLATION',
      'One or more depended-on plugin components are not deployable',
      {
        violations: [
          {
            pluginComponent: 'dda.plugin.edgefilter',
            lifecycleState: 'test',
            devices: ['line-a-camera-01'],
            version: '2.0.0',
          },
        ],
      }
    );
    expect(rejection).not.toBeNull();
    expect(rejection!.code).toBe('PLUGIN_LIFECYCLE_VIOLATION');
    expect(rejection!.lifecycleViolations).toEqual([
      {
        pluginComponent: 'dda.plugin.edgefilter',
        lifecycleState: 'test',
        devices: ['line-a-camera-01'],
        version: '2.0.0',
      },
    ]);
    expect(rejection!.archUnsupported).toEqual([]);
  });

  it('parses a PLUGIN_ARCH_UNSUPPORTED envelope identifying component and device arch (16.6)', () => {
    const rejection = parsePluginGateRejection(
      'PLUGIN_ARCH_UNSUPPORTED',
      'One or more target devices have no published Plugin_Artifact',
      {
        unsupported: [
          {
            pluginComponent: 'dda.plugin.gpufilter',
            version: '1.0.0',
            device: 'line-a-camera-01',
            deviceArch: 'x86_64',
          },
        ],
      }
    );
    expect(rejection).not.toBeNull();
    expect(rejection!.code).toBe('PLUGIN_ARCH_UNSUPPORTED');
    expect(rejection!.archUnsupported).toEqual([
      {
        pluginComponent: 'dda.plugin.gpufilter',
        version: '1.0.0',
        device: 'line-a-camera-01',
        deviceArch: 'x86_64',
      },
    ]);
    expect(rejection!.lifecycleViolations).toEqual([]);
  });

  it('returns null for other error codes so generic handling applies', () => {
    expect(parsePluginGateRejection('VALIDATION_ERROR', 'bad input', {})).toBeNull();
    expect(parsePluginGateRejection(undefined, 'plain failure', undefined)).toBeNull();
  });

  it('tolerates missing or malformed details without throwing', () => {
    const lifecycle = parsePluginGateRejection('PLUGIN_LIFECYCLE_VIOLATION', 'rejected', undefined);
    expect(lifecycle!.lifecycleViolations).toEqual([]);

    const arch = parsePluginGateRejection('PLUGIN_ARCH_UNSUPPORTED', 'rejected', {
      unsupported: [null, 'garbage', { pluginComponent: 'dda.plugin.p1' }],
    });
    expect(arch!.archUnsupported).toEqual([
      {
        pluginComponent: 'dda.plugin.p1',
        version: 'unknown',
        device: 'unknown device',
        deviceArch: null,
      },
    ]);
  });

  it('fails closed on a null lifecycleState (unknown state is still a violation)', () => {
    const rejection = parsePluginGateRejection('PLUGIN_LIFECYCLE_VIOLATION', 'rejected', {
      violations: [
        { pluginComponent: 'dda.plugin.p1', lifecycleState: null, devices: ['d1'] },
      ],
    });
    expect(rejection!.lifecycleViolations[0].lifecycleState).toBeNull();
  });
});

describe('describeLifecycleViolation', () => {
  it('identifies the Plugin_Component, its state, and the offending devices (16.3)', () => {
    const text = describeLifecycleViolation({
      pluginComponent: 'dda.plugin.edgefilter',
      lifecycleState: 'test',
      devices: ['line-a', 'line-b'],
      version: '2.0.0',
    });
    expect(text).toContain('dda.plugin.edgefilter');
    expect(text).toContain('v2.0.0');
    expect(text).toContain('test');
    expect(text).toContain('line-a, line-b');
  });

  it('describes dev-state components as not deployable', () => {
    const text = describeLifecycleViolation({
      pluginComponent: 'dda.plugin.p1',
      lifecycleState: 'dev',
      devices: ['bench-01'],
    });
    expect(text).toContain('dda.plugin.p1');
    expect(text).toContain('"dev"');
    expect(text).toContain('bench-01');
  });
});

describe('describeArchUnsupported', () => {
  it('identifies the Plugin_Component version, device, and unsupported architecture (16.6)', () => {
    const text = describeArchUnsupported({
      pluginComponent: 'dda.plugin.gpufilter',
      version: '1.0.0',
      device: 'line-a-camera-01',
      deviceArch: 'x86_64',
    });
    expect(text).toContain('dda.plugin.gpufilter');
    expect(text).toContain('v1.0.0');
    expect(text).toContain('line-a-camera-01');
    expect(text).toContain('x86_64');
  });

  it('handles an unrecorded device architecture', () => {
    const text = describeArchUnsupported({
      pluginComponent: 'dda.plugin.p1',
      version: '2.0.0',
      device: 'd1',
      deviceArch: null,
    });
    expect(text).toContain('unknown architecture');
  });
});
