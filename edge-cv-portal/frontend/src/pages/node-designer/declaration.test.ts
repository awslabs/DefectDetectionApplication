/**
 * Unit tests for the create-wizard declaration assembly
 * (custom-node-designer task 12.1, Requirement 1.1).
 */
import { describe, expect, it } from 'vitest';
import {
  WizardForm,
  architecturesStepErrors,
  buildDeclaration,
  convertParameterValue,
  defaultPortsForCategory,
  detailsStepErrors,
  emptyParameter,
  emptyPort,
  isDefaultPortArrangement,
  parametersStepErrors,
  portsStepErrors,
  typeIdFromName,
} from './declaration';

const baseForm = (): WizardForm => ({
  name: 'Blur Regions',
  description: 'Blurs configured regions',
  category: 'preprocessing',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [],
  architectures: ['x86_64', 'arm64_jp5'],
});

describe('typeIdFromName', () => {
  it('derives a custom.<slug> id from the display name', () => {
    expect(typeIdFromName('Blur Regions')).toBe('custom.blur_regions');
    expect(typeIdFromName('  Edge-Detect 2 ')).toBe('custom.edge_detect_2');
  });

  it('returns empty when no usable characters remain', () => {
    expect(typeIdFromName('!!!')).toBe('');
    expect(typeIdFromName('')).toBe('');
  });
});

describe('convertParameterValue', () => {
  it('converts by parameter type', () => {
    expect(convertParameterValue('int', '5')).toBe(5);
    expect(convertParameterValue('float', '0.25')).toBe(0.25);
    expect(convertParameterValue('bool', 'true')).toBe(true);
    expect(convertParameterValue('bool', 'false')).toBe(false);
    expect(convertParameterValue('string', 'hello')).toBe('hello');
  });

  it('rejects unusable raw values', () => {
    expect(convertParameterValue('int', '1.5')).toBeNull();
    expect(convertParameterValue('int', 'abc')).toBeNull();
    expect(convertParameterValue('bool', 'yes')).toBeNull();
    expect(convertParameterValue('string', '   ')).toBeNull();
  });
});

describe('buildDeclaration', () => {
  it('assembles the wire declaration collected by the wizard (1.1)', () => {
    const form = baseForm();
    form.parameters = [
      {
        ...emptyParameter(),
        name: 'radius',
        paramType: 'int',
        required: true,
        defaultValue: '5',
        description: 'Blur radius in pixels',
        example: '8',
      },
    ];
    const declaration = buildDeclaration(form);

    expect(declaration.typeId).toBe('custom.blur_regions');
    expect(declaration.displayName).toBe('Blur Regions');
    expect(declaration.category).toBe('preprocessing');
    expect(declaration.inputs).toEqual([{ name: 'in', portType: 'VideoFrames' }]);
    expect(declaration.outputs).toEqual([{ name: 'out', portType: 'VideoFrames' }]);
    expect(declaration.architectures).toEqual(['x86_64', 'arm64_jp5']);
    expect(declaration.mappings).toEqual([]);
    expect(declaration.parameters).toEqual([
      {
        name: 'radius',
        paramType: 'int',
        required: true,
        default: 5,
        description: 'Blur radius in pixels',
        examples: [8],
      },
    ]);
  });

  it('records enum allowed values as a values constraint', () => {
    const form = baseForm();
    form.parameters = [
      {
        ...emptyParameter(),
        name: 'mode',
        paramType: 'enum',
        description: 'Blur mode',
        example: 'low',
        enumValues: 'low, medium, high',
      },
    ];
    const declaration = buildDeclaration(form);
    expect(declaration.parameters[0].constraints).toEqual({
      values: ['low', 'medium', 'high'],
    });
    expect(declaration.parameters[0].examples).toEqual(['low']);
  });

  it('omits the default when none is provided', () => {
    const form = baseForm();
    form.parameters = [
      {
        ...emptyParameter(),
        name: 'label',
        paramType: 'string',
        description: 'Overlay label',
        example: 'defect',
      },
    ];
    expect('default' in buildDeclaration(form).parameters[0]).toBe(false);
  });
});

describe('step validation', () => {
  it('accepts a complete form', () => {
    const form = baseForm();
    expect(detailsStepErrors(form)).toEqual([]);
    expect(portsStepErrors(form)).toEqual([]);
    expect(parametersStepErrors(form)).toEqual([]);
    expect(architecturesStepErrors(form)).toEqual([]);
  });

  it('flags missing name, unnamed ports, and empty architecture selection', () => {
    const form = baseForm();
    form.name = '  ';
    form.inputs = [emptyPort()];
    form.architectures = [];
    expect(detailsStepErrors(form).length).toBeGreaterThan(0);
    expect(portsStepErrors(form).length).toBeGreaterThan(0);
    expect(architecturesStepErrors(form).length).toBeGreaterThan(0);
  });

  it('requires parameter description and a type-valid example', () => {
    const form = baseForm();
    form.parameters = [
      { ...emptyParameter(), name: 'radius', paramType: 'int', example: 'oops' },
    ];
    const errors = parametersStepErrors(form);
    expect(errors.some((e) => e.includes('description'))).toBe(true);
    expect(errors.some((e) => e.includes('example'))).toBe(true);
  });
});

/**
 * Category-driven default port seeds (workflow-designer-bugfixes
 * Bug 2, Requirements 2.4, 2.5, 3.6, 3.10).
 */
describe('defaultPortsForCategory', () => {
  it('seeds each palette category with its typical arrangement (2.4, 2.5)', () => {
    expect(defaultPortsForCategory('input')).toEqual({
      inputs: [],
      outputs: [{ name: 'out', portType: 'VideoFrames' }],
    });
    expect(defaultPortsForCategory('preprocessing')).toEqual({
      inputs: [{ name: 'in', portType: 'VideoFrames' }],
      outputs: [{ name: 'out', portType: 'VideoFrames' }],
    });
    expect(defaultPortsForCategory('inference')).toEqual({
      inputs: [{ name: 'in', portType: 'VideoFrames' }],
      outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    });
    expect(defaultPortsForCategory('post_processing')).toEqual({
      inputs: [{ name: 'in', portType: 'InferenceMeta' }],
      outputs: [{ name: 'out', portType: 'EventSignal' }],
    });
    expect(defaultPortsForCategory('output')).toEqual({
      inputs: [{ name: 'in', portType: 'VideoFrames' }],
      outputs: [],
    });
  });

  it("falls back to the preprocessing shape (today's seeds) for unknown categories", () => {
    expect(defaultPortsForCategory('unknown')).toEqual(
      defaultPortsForCategory('preprocessing')
    );
    expect(defaultPortsForCategory('')).toEqual(
      defaultPortsForCategory('preprocessing')
    );
  });
});

describe('isDefaultPortArrangement', () => {
  it("answers true for every category's default arrangement", () => {
    for (const category of [
      'input',
      'preprocessing',
      'inference',
      'post_processing',
      'output',
    ]) {
      const { inputs, outputs } = defaultPortsForCategory(category);
      expect(isDefaultPortArrangement(inputs, outputs)).toBe(true);
    }
  });

  it('answers false once the rows are edited (rename, retype, add, remove)', () => {
    const { inputs, outputs } = defaultPortsForCategory('preprocessing');
    expect(
      isDefaultPortArrangement(
        [{ ...inputs[0], name: 'video' }],
        outputs
      )
    ).toBe(false);
    expect(
      isDefaultPortArrangement(
        [{ ...inputs[0], portType: 'EventSignal' }],
        outputs
      )
    ).toBe(false);
    expect(
      isDefaultPortArrangement([...inputs, { name: 'aux', portType: 'VideoFrames' }], outputs)
    ).toBe(false);
    expect(isDefaultPortArrangement([], [])).toBe(false);
  });
});
