/**
 * Unit tests for the registration-wizard declaration assembly
 * (custom-node-designer task 12.5, Requirements 4.6, 8.1, 8.5).
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../services/api';
import {
  RegistrationForm,
  buildRegistrationDeclaration,
  defaultElementFactory,
  formFromDeclaration,
  initialMappings,
  mappingsStepErrors,
  registrationErrorView,
  scopeStepErrors,
  shouldPromptRegistration,
  successfulBuildArchs,
} from './registration';
import type { PluginArtifactEntry } from './types';

const baseForm = (): RegistrationForm => ({
  name: 'Blur Regions',
  description: 'Blurs configured regions',
  category: 'preprocessing',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    {
      name: 'radius',
      paramType: 'int',
      required: true,
      defaultValue: '3',
      description: 'Blur radius in pixels',
      example: '5',
      enumValues: '',
    },
  ],
  mappings: [
    {
      arch: 'x86_64',
      include: true,
      factory: 'blur_regions',
      properties: [{ property: 'radius', value: '{radius}' }],
    },
    {
      arch: 'arm64_jp5',
      include: false,
      factory: 'blur_regions',
      properties: [],
    },
  ],
  hardwareDependent: false,
  usecaseIds: ['uc-1'],
});

const artifacts = (
  entries: Record<string, string | null>
): Record<string, PluginArtifactEntry> =>
  Object.fromEntries(
    Object.entries(entries).map(([arch, status]) => [
      arch,
      { buildStatus: status as PluginArtifactEntry['buildStatus'] },
    ])
  );

describe('successfulBuildArchs / shouldPromptRegistration (4.6)', () => {
  it('returns exactly the architectures with a succeeded build', () => {
    expect(
      successfulBuildArchs(
        artifacts({ x86_64: 'succeeded', arm64_jp5: 'failed', arm64_jp6: 'building' })
      )
    ).toEqual(['x86_64']);
    expect(successfulBuildArchs(null)).toEqual([]);
    expect(successfulBuildArchs({})).toEqual([]);
  });

  it('prompts registration once at least one build succeeded', () => {
    expect(shouldPromptRegistration(artifacts({ x86_64: 'succeeded' }))).toBe(true);
    expect(
      shouldPromptRegistration(artifacts({ x86_64: 'failed', arm64_jp5: 'building' }))
    ).toBe(false);
    expect(shouldPromptRegistration(undefined)).toBe(false);
  });
});

describe('defaultElementFactory / initialMappings', () => {
  it('derives the element factory the scaffold registers (element_name_for over the typeId)', () => {
    // typeId "custom.blur_regions" -> element "customblurregions": the
    // scaffold strips every non-alphanumeric, so the underscored slug
    // "blur_regions" would NOT match the built plugin's element and the
    // device pipeline would fail with `no element "blur_regions"`.
    expect(defaultElementFactory('Blur Regions')).toBe('customblurregions');
    expect(defaultElementFactory('  Edge-Detect 2 ')).toBe('customedgedetect2');
  });

  it('creates one included mapping row per built architecture', () => {
    const mappings = initialMappings(['arm64_jp5', 'x86_64'], 'blur');
    expect(mappings).toHaveLength(2);
    expect(mappings.every((m) => m.include && m.factory === 'blur')).toBe(true);
    expect(mappings.map((m) => m.arch)).toEqual(['arm64_jp5', 'x86_64']);
  });
});

describe('buildRegistrationDeclaration (8.1)', () => {
  it('assembles the wire declaration with mappings and hardware flag', () => {
    const declaration = buildRegistrationDeclaration(baseForm());
    expect(declaration.typeId).toBe('custom.blur_regions');
    expect(declaration.displayName).toBe('Blur Regions');
    expect(declaration.category).toBe('preprocessing');
    expect(declaration.inputs).toEqual([{ name: 'in', portType: 'VideoFrames' }]);
    expect(declaration.outputs).toEqual([{ name: 'out', portType: 'VideoFrames' }]);
    expect(declaration.parameters).toEqual([
      {
        name: 'radius',
        paramType: 'int',
        required: true,
        default: 3,
        description: 'Blur radius in pixels',
        examples: [5],
      },
    ]);
    expect(declaration.hardwareDependent).toBe(false);
    // Only the included mapping is declared, with the property template.
    expect(declaration.mappings).toEqual([
      {
        arch: 'x86_64',
        elementChain: [
          { factory: 'blur_regions', argsTemplate: { radius: '{radius}' } },
          // VideoFrames-output nodes gain a trailing videoconvert: the
          // scaffold bridge emits fixed caps that strict downstream
          // encoders (capture's jpegenc) reject without a converter
          // (custom-node-plugin-runtime-fixes, verified on JP6).
          { factory: 'videoconvert', argsTemplate: {} },
        ],
        pluginDependencies: [],
      },
    ]);
  });

  it('drops property rows without a name and trims the factory', () => {
    const form = baseForm();
    form.mappings[0].factory = '  blur_regions  ';
    form.mappings[0].properties = [
      { property: '  ', value: '{radius}' },
      { property: 'radius', value: '{radius}' },
    ];
    const declaration = buildRegistrationDeclaration(form);
    expect(declaration.mappings[0].elementChain[0]).toEqual({
      factory: 'blur_regions',
      argsTemplate: { radius: '{radius}' },
    });
  });

  it('carries the hardware-dependence flag', () => {
    const form = baseForm();
    form.hardwareDependent = true;
    expect(buildRegistrationDeclaration(form).hardwareDependent).toBe(true);
  });
});

describe('step validation', () => {
  it('requires at least one included mapping', () => {
    const form = baseForm();
    form.mappings = form.mappings.map((m) => ({ ...m, include: false }));
    expect(mappingsStepErrors(form)).toHaveLength(1);
  });

  it('requires the element factory on included mappings', () => {
    const form = baseForm();
    form.mappings[0].factory = '   ';
    expect(mappingsStepErrors(form).join(' ')).toContain('factory');
  });

  it('flags value-only property rows', () => {
    const form = baseForm();
    form.mappings[0].properties = [{ property: '', value: '{radius}' }];
    expect(mappingsStepErrors(form)).toHaveLength(1);
  });

  it('accepts a complete mapping step', () => {
    expect(mappingsStepErrors(baseForm())).toEqual([]);
  });

  it('requires at least one use case in the scoping step', () => {
    const form = baseForm();
    form.usecaseIds = [];
    expect(scopeStepErrors(form)).toHaveLength(1);
    expect(scopeStepErrors(baseForm())).toEqual([]);
  });
});

describe('registrationErrorView (8.5)', () => {
  it('surfaces the offending field from the structured error envelope', () => {
    const err = new ApiError(
      "inputs[0].portType: unknown port type 'Bogus'",
      400,
      'INVALID_DECLARATION',
      { field: 'inputs[0].portType' }
    );
    expect(registrationErrorView(err)).toEqual({
      message: "inputs[0].portType: unknown port type 'Bogus'",
      field: 'inputs[0].portType',
    });
  });

  it('falls back to the message when no field is identified', () => {
    expect(registrationErrorView(new Error('network down'))).toEqual({
      message: 'network down',
    });
    expect(registrationErrorView('boom')).toEqual({ message: 'Registration failed' });
  });
});

describe('formFromDeclaration (update mode)', () => {
  const declaration = {
    typeId: 'custom.rtsp_source',
    displayName: 'RTSP Source',
    description: 'Pulls frames from an RTSP stream',
    category: 'input',
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
    parameters: [
      {
        name: 'latency',
        paramType: 'int',
        required: true,
        default: 200,
        description: 'Jitterbuffer latency (ms)',
        examples: [200],
      },
      {
        name: 'protocol',
        paramType: 'enum',
        required: false,
        description: 'Transport',
        examples: ['tcp'],
        constraints: { values: ['tcp', 'udp'] },
      },
    ],
    mappings: [
      {
        arch: 'x86_64',
        elementChain: [
          { factory: 'rtspsrc', argsTemplate: { latency: '{latency}' } },
        ],
        pluginDependencies: ['custom:uc-1/rtsp'],
      },
    ],
    hardwareDependent: false,
  };

  it('rebuilds the wizard form from a stored declaration', () => {
    const form = formFromDeclaration(
      declaration,
      ['arm64_jp5', 'x86_64'],
      ['uc-1'],
      'default_factory'
    );
    expect(form.name).toBe('RTSP Source');
    expect(form.category).toBe('input');
    expect(form.inputs).toEqual([{ name: 'in', portType: 'VideoFrames' }]);
    expect(form.outputs).toEqual([{ name: 'out', portType: 'VideoFrames' }]);
    expect(form.parameters).toHaveLength(2);
    expect(form.parameters[0]).toMatchObject({
      name: 'latency',
      paramType: 'int',
      required: true,
      defaultValue: '200',
      example: '200',
    });
    expect(form.parameters[1]).toMatchObject({
      name: 'protocol',
      paramType: 'enum',
      enumValues: 'tcp, udp',
      defaultValue: '',
    });
    expect(form.hardwareDependent).toBe(false);
    expect(form.usecaseIds).toEqual(['uc-1']);
  });

  it('includes declared architectures and offers new builds as opt-in rows', () => {
    const form = formFromDeclaration(
      declaration,
      ['arm64_jp5', 'x86_64'],
      ['uc-1'],
      'default_factory'
    );
    // Declared arch: included with its stored factory and properties.
    const x86 = form.mappings.find((m) => m.arch === 'x86_64');
    expect(x86).toMatchObject({ include: true, factory: 'rtspsrc' });
    expect(x86?.properties).toEqual([{ property: 'latency', value: '{latency}' }]);
    // Built since registration: present but excluded until opted in.
    const jp5 = form.mappings.find((m) => m.arch === 'arm64_jp5');
    expect(jp5).toMatchObject({ include: false, factory: 'default_factory' });
  });

  it('round-trips through buildRegistrationDeclaration', () => {
    const form = formFromDeclaration(
      declaration,
      ['x86_64'],
      ['uc-1'],
      'default_factory'
    );
    const rebuilt = buildRegistrationDeclaration(form);
    expect(rebuilt.displayName).toBe('RTSP Source');
    expect(rebuilt.category).toBe('input');
    expect(rebuilt.mappings).toEqual([
      {
        arch: 'x86_64',
        elementChain: [
          { factory: 'rtspsrc', argsTemplate: { latency: '{latency}' } },
          // Trailing videoconvert appended on save for VideoFrames
          // outputs (custom-node-plugin-runtime-fixes).
          { factory: 'videoconvert', argsTemplate: {} },
        ],
        pluginDependencies: [],
      },
    ]);
  });
});
