/**
 * Create-wizard declaration assembly (custom-node-designer).
 *
 * Pure helpers turning the wizard's form state into the
 * Custom_Node_Type declaration wire shape POST /plugins renders the
 * Plugin_Scaffold from (Requirement 1.1). Validation authority stays
 * server-side (workflow_core.scaffold / catalog.custom); these helpers
 * only assemble the shape and provide the light client-side checks the
 * wizard needs for step gating.
 */
import { CATEGORIES } from './types';
import type {
  NodeCategory,
  ParameterDeclaration,
  PortDeclaration,
  ScaffoldDeclaration,
} from './types';
import { CATEGORY_ARRANGEMENTS } from './portGuidance';

// ------------------------------------------------------------- form state

export interface PortForm {
  name: string;
  portType: string;
}

export interface ParameterForm {
  name: string;
  paramType: string;
  required: boolean;
  /** Raw text; converted per paramType (empty string means no default). */
  defaultValue: string;
  description: string;
  /** Raw text; converted per paramType. At least one example is required. */
  example: string;
  /** enum only: comma-separated allowed values. */
  enumValues: string;
  /**
   * Numeric min/max ride-along (gst-parameter-prepopulation, 3.3):
   * carried on the row from a scanned Parameter_Suggestion (or a
   * stored declaration) with no editable UI; parameterFromForm
   * re-emits it so the constraints survive declaration assembly.
   */
  constraints?: { min?: number; max?: number };
}

export interface WizardForm {
  name: string;
  description: string;
  category: string;
  inputs: PortForm[];
  outputs: PortForm[];
  parameters: ParameterForm[];
  architectures: string[];
}

export const emptyPort = (portType = 'VideoFrames'): PortForm => ({
  name: '',
  portType,
});

export const emptyParameter = (): ParameterForm => ({
  name: '',
  paramType: 'string',
  required: false,
  defaultValue: '',
  description: '',
  example: '',
  enumValues: '',
});

// --------------------------------------------- category default ports

/**
 * The default port rows the wizards seed for one palette category,
 * derived from the category's typical arrangement
 * (CATEGORY_ARRANGEMENTS in portGuidance.ts;
 * workflow-designer-bugfixes Bug 2, Requirements 2.4, 2.5):
 *
 * - `input`: no inputs, one VideoFrames "out"
 * - `preprocessing`: one VideoFrames "in", one VideoFrames "out"
 *   (byte-identical to the wizards' historical seeds)
 * - `inference`: one VideoFrames "in", one InferenceMeta "out"
 * - `post_processing`: one InferenceMeta "in", one EventSignal "out"
 * - `output`: one VideoFrames "in" (the seeded representative of
 *   "at least one input of any type"), no outputs
 * - unknown category: the preprocessing shape (today's seeds)
 *
 * Every seeded row carries a non-empty name so `portsStepErrors` stays
 * clean on the untouched defaults, and each concrete arrangement
 * yields `guidanceDivergence === null`.
 */
export function defaultPortsForCategory(category: string): {
  inputs: PortForm[];
  outputs: PortForm[];
} {
  const arrangement = Object.prototype.hasOwnProperty.call(
    CATEGORY_ARRANGEMENTS,
    category
  )
    ? CATEGORY_ARRANGEMENTS[category as NodeCategory]
    : CATEGORY_ARRANGEMENTS.preprocessing;
  const inputs: PortForm[] =
    arrangement.inputs === 'at-least-one'
      ? [{ name: 'in', portType: 'VideoFrames' }]
      : arrangement.inputs.map((portType) => ({ name: 'in', portType }));
  const outputs: PortForm[] = arrangement.outputs.map((portType) => ({
    name: 'out',
    portType,
  }));
  return { inputs, outputs };
}

const samePortRows = (a: readonly PortForm[], b: readonly PortForm[]) =>
  a.length === b.length &&
  a.every(
    (port, i) => port.name === b[i].name && port.portType === b[i].portType
  );

/**
 * Generalized Untouched_Defaults detection (workflow-designer-bugfixes
 * Bug 2): true exactly when the rows deep-equal
 * `defaultPortsForCategory(c)` for some palette category `c`. Any
 * rename, retype, addition, or removal makes the rows user-edited.
 */
export function isDefaultPortArrangement(
  inputs: PortForm[],
  outputs: PortForm[]
): boolean {
  return CATEGORIES.some((category) => {
    const defaults = defaultPortsForCategory(category);
    return (
      samePortRows(defaults.inputs, inputs) &&
      samePortRows(defaults.outputs, outputs)
    );
  });
}

// ------------------------------------------------------------ conversions

/**
 * The `custom.<slug>` type id derived from the node name: lower-cased,
 * runs of non-alphanumerics collapsed to underscores. Mirrors the
 * element-name derivability rule in workflow_core.scaffold (at least
 * one usable alphanumeric character required).
 */
export function typeIdFromName(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return slug ? `custom.${slug}` : '';
}

/** Convert one raw text value per parameter type; null when unusable. */
export function convertParameterValue(
  paramType: string,
  raw: string
): string | number | boolean | null {
  const text = raw.trim();
  if (!text) {
    return null;
  }
  switch (paramType) {
    case 'int': {
      const value = Number(text);
      return Number.isInteger(value) ? value : null;
    }
    case 'float': {
      const value = Number(text);
      return Number.isFinite(value) ? value : null;
    }
    case 'bool':
      if (text === 'true') return true;
      if (text === 'false') return false;
      return null;
    default:
      // string, enum, code, model_ref stay strings
      return text;
  }
}

function portFromForm(port: PortForm): PortDeclaration {
  return { name: port.name.trim(), portType: port.portType };
}

function parameterFromForm(parameter: ParameterForm): ParameterDeclaration {
  const example = convertParameterValue(parameter.paramType, parameter.example);
  const defaultValue = convertParameterValue(
    parameter.paramType,
    parameter.defaultValue
  );
  const declaration: ParameterDeclaration = {
    name: parameter.name.trim(),
    paramType: parameter.paramType,
    required: parameter.required,
    description: parameter.description.trim(),
    examples: example === null ? [] : [example],
  };
  if (defaultValue !== null) {
    declaration.default = defaultValue;
  }
  if (parameter.paramType === 'enum') {
    const values = parameter.enumValues
      .split(',')
      .map((value) => value.trim())
      .filter((value) => value.length > 0);
    declaration.constraints = { values };
  } else if (parameter.constraints) {
    // Numeric min/max ride-along re-emitted verbatim (3.3).
    const constraints: Record<string, unknown> = {};
    if (parameter.constraints.min !== undefined) {
      constraints.min = parameter.constraints.min;
    }
    if (parameter.constraints.max !== undefined) {
      constraints.max = parameter.constraints.max;
    }
    if (Object.keys(constraints).length > 0) {
      declaration.constraints = constraints;
    }
  }
  return declaration;
}

/**
 * Assemble the declaration POST /plugins renders the scaffold from.
 * `mappings` stays empty at creation time: the element/property mapping
 * per built architecture is declared later by the registration wizard
 * (task 12.5); the scaffold takes its build configurations from the
 * explicit `architectures` list.
 */
export function buildDeclaration(form: WizardForm): ScaffoldDeclaration {
  return {
    typeId: typeIdFromName(form.name),
    displayName: form.name.trim(),
    description: form.description.trim() || undefined,
    category: form.category,
    inputs: form.inputs.map(portFromForm),
    outputs: form.outputs.map(portFromForm),
    parameters: form.parameters.map(parameterFromForm),
    mappings: [],
    architectures: [...form.architectures],
  };
}

// --------------------------------------------------------- step validation

/** Client-side step gating; the server remains the validation authority. */
export function detailsStepErrors(form: WizardForm): string[] {
  const errors: string[] = [];
  if (!form.name.trim()) {
    errors.push('Name is required.');
  } else if (!typeIdFromName(form.name)) {
    errors.push('Name must contain at least one letter or digit.');
  }
  if (!form.category) {
    errors.push('Category is required.');
  }
  return errors;
}

export function portsStepErrors(form: WizardForm): string[] {
  const errors: string[] = [];
  [...form.inputs, ...form.outputs].forEach((port, index) => {
    if (!port.name.trim()) {
      errors.push(`Port ${index + 1} needs a name.`);
    }
  });
  return errors;
}

export function parametersStepErrors(form: WizardForm): string[] {
  const errors: string[] = [];
  form.parameters.forEach((parameter, index) => {
    const label = parameter.name.trim() || `Parameter ${index + 1}`;
    if (!parameter.name.trim()) {
      errors.push(`Parameter ${index + 1} needs a name.`);
    }
    if (!parameter.description.trim()) {
      errors.push(`${label} needs a description.`);
    }
    if (convertParameterValue(parameter.paramType, parameter.example) === null) {
      errors.push(`${label} needs a valid example value for type ${parameter.paramType}.`);
    }
    if (parameter.paramType === 'enum' && !parameter.enumValues.trim()) {
      errors.push(`${label} needs the allowed enum values (comma-separated).`);
    }
  });
  return errors;
}

export function architecturesStepErrors(form: WizardForm): string[] {
  return form.architectures.length > 0
    ? []
    : ['Select at least one Target_Architecture.'];
}
