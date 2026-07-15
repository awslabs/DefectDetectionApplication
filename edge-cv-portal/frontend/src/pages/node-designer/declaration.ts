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
import type {
  ParameterDeclaration,
  PortDeclaration,
  ScaffoldDeclaration,
} from './types';

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
