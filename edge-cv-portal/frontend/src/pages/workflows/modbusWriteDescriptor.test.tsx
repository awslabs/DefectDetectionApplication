/**
 * Frontend example tests for the `modbus_write` catalog mirror
 * (modbus-tcp-output, task 5.2).
 *
 * Covers:
 *  - `types.ts` mirror descriptor identity against the backend
 *    `MODBUS_WRITE` descriptor in `workflow_core/catalog/nodes.py`:
 *    type id, output category, display name, one InferenceMeta `in`
 *    port / zero outputs, all seven parameter shapes
 *    (names/types/defaults/constraints), the `"register_type=coil"`
 *    `dependsOn` gating on `pulse_ms`, the device mappings with zero
 *    plugin dependencies plus the `recording_modbus_write` sim stub,
 *    and `hardwareDependent` — Requirement 3.1
 *  - Node_Palette lists the node under the Outputs section exactly
 *    once — Requirement 3.2
 */

import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import NodePalette from './NodePalette';
import {
  CATEGORY_OUTPUT,
  MODBUS_WRITE_DESCRIPTOR,
  PORT_TYPE_INFERENCE_META,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
} from './types';

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function parameter(descriptor: NodeTypeDescriptor, name: string): ParameterDescriptor {
  const found = descriptor.parameters.find((entry) => entry.name === name);
  expect(found, `${descriptor.typeId} declares parameter '${name}'`).toBeDefined();
  return found!;
}

/**
 * Physical device architectures the backend catalog's
 * `_same_on_device_archs` maps over (`DEVICE_ARCHITECTURES` in
 * `workflow_core/catalog/models.py`).
 */
const DEVICE_ARCHITECTURES = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
];

// --------------------------------------------------------------------------
// Descriptor identity: type id, category, display name, ports
// (Requirement 3.1)
// --------------------------------------------------------------------------

describe('modbus_write descriptor identity (Requirement 3.1)', () => {
  it('mirrors the backend type id, output category, and display name', () => {
    expect(MODBUS_WRITE_DESCRIPTOR.typeId).toBe('modbus_write');
    expect(MODBUS_WRITE_DESCRIPTOR.category).toBe(CATEGORY_OUTPUT);
    expect(MODBUS_WRITE_DESCRIPTOR.displayName).toBe('Modbus TCP Write');
  });

  it('declares exactly one InferenceMeta "in" port and zero outputs', () => {
    expect(MODBUS_WRITE_DESCRIPTOR.inputs).toEqual([
      { name: 'in', portType: PORT_TYPE_INFERENCE_META },
    ]);
    expect(MODBUS_WRITE_DESCRIPTOR.outputs).toEqual([]);
  });

  it('is marked hardware-dependent', () => {
    expect(MODBUS_WRITE_DESCRIPTOR.hardwareDependent).toBe(true);
  });

  it('declares exactly the seven backend parameters in backend order', () => {
    expect(MODBUS_WRITE_DESCRIPTOR.parameters.map((p) => p.name)).toEqual([
      'host',
      'port',
      'unit_id',
      'register_type',
      'address',
      'value_template',
      'pulse_ms',
    ]);
  });
});

// --------------------------------------------------------------------------
// Parameter shapes (Requirement 3.1)
// --------------------------------------------------------------------------

describe('modbus_write parameter shapes (Requirement 3.1)', () => {
  it('declares host as a required string with min length 1', () => {
    const host = parameter(MODBUS_WRITE_DESCRIPTOR, 'host');
    expect(host.paramType).toBe('string');
    expect(host.required).toBe(true);
    expect(host.default).toBeNull();
    expect(host.constraints).toEqual({ minLength: 1 });
    expect(host.dependsOn ?? null).toBeNull();
  });

  it('declares port as an optional int defaulting to 502 within 1-65535', () => {
    const port = parameter(MODBUS_WRITE_DESCRIPTOR, 'port');
    expect(port.paramType).toBe('int');
    expect(port.required).toBe(false);
    expect(port.default).toBe(502);
    expect(port.constraints).toEqual({ min: 1, max: 65535 });
    expect(port.dependsOn ?? null).toBeNull();
  });

  it('declares unit_id as an optional int defaulting to 1 within 0-255', () => {
    const unitId = parameter(MODBUS_WRITE_DESCRIPTOR, 'unit_id');
    expect(unitId.paramType).toBe('int');
    expect(unitId.required).toBe(false);
    expect(unitId.default).toBe(1);
    expect(unitId.constraints).toEqual({ min: 0, max: 255 });
    expect(unitId.dependsOn ?? null).toBeNull();
  });

  it('declares register_type as a required enum defaulting to coil with values coil/holding_register', () => {
    const registerType = parameter(MODBUS_WRITE_DESCRIPTOR, 'register_type');
    expect(registerType.paramType).toBe('enum');
    expect(registerType.required).toBe(true);
    expect(registerType.default).toBe('coil');
    expect(registerType.constraints).toEqual({ values: ['coil', 'holding_register'] });
    expect(registerType.dependsOn ?? null).toBeNull();
  });

  it('declares address as a required int with no default within 0-65535', () => {
    const address = parameter(MODBUS_WRITE_DESCRIPTOR, 'address');
    expect(address.paramType).toBe('int');
    expect(address.required).toBe(true);
    expect(address.default).toBeNull();
    expect(address.constraints).toEqual({ min: 0, max: 65535 });
    expect(address.dependsOn ?? null).toBeNull();
  });

  it('declares value_template as an optional string defaulting to {is_anomalous}', () => {
    const valueTemplate = parameter(MODBUS_WRITE_DESCRIPTOR, 'value_template');
    expect(valueTemplate.paramType).toBe('string');
    expect(valueTemplate.required).toBe(false);
    expect(valueTemplate.default).toBe('{is_anomalous}');
    expect(valueTemplate.constraints).toEqual({});
    expect(valueTemplate.dependsOn ?? null).toBeNull();
  });

  it('declares pulse_ms as an optional int defaulting to 0 within 0-60000, gated on register_type=coil', () => {
    const pulseMs = parameter(MODBUS_WRITE_DESCRIPTOR, 'pulse_ms');
    expect(pulseMs.paramType).toBe('int');
    expect(pulseMs.required).toBe(false);
    expect(pulseMs.default).toBe(0);
    expect(pulseMs.constraints).toEqual({ min: 0, max: 60000 });
    expect(pulseMs.dependsOn).toBe('register_type=coil');
  });
});

// --------------------------------------------------------------------------
// Device mappings and the sim recording stub (Requirement 3.1)
// --------------------------------------------------------------------------

describe('modbus_write mappings (Requirement 3.1)', () => {
  it('maps every device architecture to the modbus_write executor binding with zero plugin dependencies', () => {
    const deviceMappings = MODBUS_WRITE_DESCRIPTOR.mappings.filter((m) => m.arch !== 'sim');
    expect(deviceMappings.map((m) => m.arch)).toEqual(DEVICE_ARCHITECTURES);
    for (const mapping of deviceMappings) {
      expect(mapping.executorBinding).toBe('modbus_write');
      expect(mapping.elementChain).toEqual([]);
      expect(mapping.pluginDependencies).toEqual([]);
    }
  });

  it('maps sim to the recording_modbus_write recording stub', () => {
    const simMappings = MODBUS_WRITE_DESCRIPTOR.mappings.filter((m) => m.arch === 'sim');
    expect(simMappings).toEqual([
      {
        arch: 'sim',
        elementChain: [],
        executorBinding: 'recording_modbus_write',
        pluginDependencies: [],
      },
    ]);
  });

  it('declares exactly one mapping per device architecture plus the sim stub', () => {
    expect(MODBUS_WRITE_DESCRIPTOR.mappings).toHaveLength(DEVICE_ARCHITECTURES.length + 1);
  });
});

// --------------------------------------------------------------------------
// Palette grouping (Requirement 3.2)
// --------------------------------------------------------------------------

describe('palette lists modbus_write under Outputs (Requirement 3.2)', () => {
  const CATALOG: NodeTypeDescriptor[] = [MODBUS_WRITE_DESCRIPTOR];

  it('lists Modbus TCP Write in the Output section', () => {
    render(<NodePalette catalog={CATALOG} />);
    const outputSection = screen.getByRole('region', { name: 'Output' });
    expect(
      within(outputSection).getByText(MODBUS_WRITE_DESCRIPTOR.displayName)
    ).toBeInTheDocument();
  });

  it('lists the node exactly once across the whole palette', () => {
    render(<NodePalette catalog={CATALOG} />);
    expect(screen.getAllByText(MODBUS_WRITE_DESCRIPTOR.displayName)).toHaveLength(1);
  });
});
