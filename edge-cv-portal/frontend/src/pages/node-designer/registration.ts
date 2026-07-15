/**
 * Registration-wizard declaration assembly (custom-node-designer,
 * task 12.5, Requirements 4.6, 8.1, 8.5).
 *
 * Pure helpers turning the registration wizard's form state into the
 * Custom_Node_Type declaration wire shape POST /custom-node-types
 * validates through workflow_core.catalog.custom
 * .descriptor_from_declaration. Validation authority stays server-side
 * (invalid Port declarations come back as 400 INVALID_DECLARATION with
 * details.field identifying the offense, Requirement 8.5); these
 * helpers assemble the shape and provide the light client-side checks
 * the wizard needs for step gating, plus the "prompt registration after
 * the first successful build" predicate (Requirement 4.6).
 */
import { ApiError } from '../../services/api';
import { buildDeclaration } from './declaration';
import type { ParameterForm, PortForm } from './declaration';
import type {
  MappingDeclaration,
  NodeTypeRegistrationDeclaration,
  PluginArtifactEntry,
} from './types';

// ------------------------------------------------------------- form state

/** One element property -> value template row of a per-arch mapping. */
export interface MappingPropertyForm {
  /** GObject property name on the plugin's element. */
  property: string;
  /** Value template, e.g. "{radius}" to plumb the radius parameter. */
  value: string;
}

/** Element/property mapping for one built Target_Architecture (8.1). */
export interface MappingForm {
  arch: string;
  /** Whether the declaration includes this built architecture. */
  include: boolean;
  /** GStreamer element factory name of the plugin's element. */
  factory: string;
  properties: MappingPropertyForm[];
}

export interface RegistrationForm {
  name: string;
  description: string;
  category: string;
  inputs: PortForm[];
  outputs: PortForm[];
  parameters: ParameterForm[];
  mappings: MappingForm[];
  hardwareDependent: boolean;
  /** Use_Case scoping selected at registration (8.1/8.2). */
  usecaseIds: string[];
}

// ------------------------------------------------------------- predicates

/** Architectures with a successfully built Plugin_Artifact. */
export function successfulBuildArchs(
  artifacts: Record<string, PluginArtifactEntry> | null | undefined
): string[] {
  if (!artifacts) {
    return [];
  }
  return Object.keys(artifacts)
    .filter((arch) => artifacts[arch]?.buildStatus === 'succeeded')
    .sort();
}

/**
 * Whether the Node_Designer should prompt the user to register a
 * Custom_Node_Type for the plugin (Requirement 4.6): at least one
 * Target_Architecture build succeeded.
 */
export function shouldPromptRegistration(
  artifacts: Record<string, PluginArtifactEntry> | null | undefined
): boolean {
  return successfulBuildArchs(artifacts).length > 0;
}

// ------------------------------------------------------------ conversions

/**
 * Default element factory name for the mapping step, mirroring the
 * scaffold's element naming (lower-cased, non-alphanumerics collapsed).
 */
export function defaultElementFactory(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

/** One editable mapping row per successfully built architecture. */
export function initialMappings(builtArchs: string[], factory: string): MappingForm[] {
  return builtArchs.map((arch) => ({
    arch,
    include: true,
    factory,
    properties: [],
  }));
}

/**
 * Rebuild the wizard form from a registered node type's stored
 * declaration (update mode): the wizard pre-fills from the existing
 * registration and submits PUT /custom-node-types/{id} as a new
 * version instead of registering a duplicate. Architectures built
 * since the original registration appear as excluded mapping rows the
 * user can opt into.
 */
export function formFromDeclaration(
  declaration: Record<string, unknown>,
  builtArchs: string[],
  usecaseIds: string[],
  defaultFactory: string
): RegistrationForm {
  const ports = (value: unknown): PortForm[] =>
    Array.isArray(value)
      ? value.map((port: any) => ({
          name: String(port?.name ?? ''),
          portType: String(port?.portType ?? 'VideoFrames'),
        }))
      : [];

  const parameters: ParameterForm[] = Array.isArray(declaration.parameters)
    ? (declaration.parameters as any[]).map((parameter) => ({
        name: String(parameter?.name ?? ''),
        paramType: String(parameter?.paramType ?? 'string'),
        required: Boolean(parameter?.required),
        defaultValue:
          parameter?.default === undefined || parameter?.default === null
            ? ''
            : String(parameter.default),
        description: String(parameter?.description ?? ''),
        example:
          Array.isArray(parameter?.examples) && parameter.examples.length > 0
            ? String(parameter.examples[0])
            : '',
        enumValues: Array.isArray(parameter?.constraints?.values)
          ? (parameter.constraints.values as unknown[]).map(String).join(', ')
          : '',
      }))
    : [];

  const declared = new Map<string, any>();
  if (Array.isArray(declaration.mappings)) {
    for (const mapping of declaration.mappings as any[]) {
      if (mapping?.arch) {
        declared.set(String(mapping.arch), mapping);
      }
    }
  }
  const mappings: MappingForm[] = builtArchs.map((arch) => {
    const mapping = declared.get(arch);
    const element = Array.isArray(mapping?.elementChain)
      ? mapping.elementChain[0]
      : undefined;
    return {
      arch,
      include: Boolean(mapping),
      factory: String(element?.factory ?? defaultFactory),
      properties: Object.entries(
        (element?.argsTemplate ?? {}) as Record<string, unknown>
      ).map(([property, value]) => ({ property, value: String(value) })),
    };
  });

  return {
    name: String(declaration.displayName ?? ''),
    description: String(declaration.description ?? ''),
    category: String(declaration.category ?? 'preprocessing'),
    inputs: ports(declaration.inputs),
    outputs: ports(declaration.outputs),
    parameters,
    mappings,
    hardwareDependent: Boolean(declaration.hardwareDependent),
    usecaseIds,
  };
}

function mappingFromForm(mapping: MappingForm): MappingDeclaration {
  const argsTemplate: Record<string, string> = {};
  for (const row of mapping.properties) {
    const property = row.property.trim();
    if (property) {
      argsTemplate[property] = row.value;
    }
  }
  return {
    arch: mapping.arch,
    elementChain: [{ factory: mapping.factory.trim(), argsTemplate }],
    pluginDependencies: [],
  };
}

/**
 * Assemble the registration declaration POST /custom-node-types
 * validates (8.1): ports from PORT_TYPES, parameters with descriptions
 * and examples, the element/property mapping per included built
 * architecture, and the hardware-dependence flag. The plugin dependency
 * is injected server-side (custom:{usecase}/{plugin}).
 */
export function buildRegistrationDeclaration(
  form: RegistrationForm
): NodeTypeRegistrationDeclaration {
  const base = buildDeclaration({
    name: form.name,
    description: form.description,
    category: form.category,
    inputs: form.inputs,
    outputs: form.outputs,
    parameters: form.parameters,
    architectures: [],
  });
  return {
    typeId: base.typeId,
    displayName: base.displayName,
    description: base.description,
    category: base.category,
    inputs: base.inputs,
    outputs: base.outputs,
    parameters: base.parameters,
    mappings: form.mappings.filter((m) => m.include).map(mappingFromForm),
    hardwareDependent: form.hardwareDependent,
  };
}

// --------------------------------------------------------- step validation

/** Client-side mapping step gating; the server remains the authority. */
export function mappingsStepErrors(form: RegistrationForm): string[] {
  const errors: string[] = [];
  const included = form.mappings.filter((m) => m.include);
  if (included.length === 0) {
    errors.push(
      'Include the element mapping for at least one built Target_Architecture.'
    );
  }
  included.forEach((mapping) => {
    if (!mapping.factory.trim()) {
      errors.push(`Mapping for ${mapping.arch} needs the element factory name.`);
    }
    mapping.properties.forEach((row, index) => {
      if (!row.property.trim() && row.value.trim()) {
        errors.push(
          `Mapping for ${mapping.arch}: property ${index + 1} needs a name.`
        );
      }
    });
  });
  return errors;
}

export function scopeStepErrors(form: RegistrationForm): string[] {
  return form.usecaseIds.length > 0
    ? []
    : ['Select at least one Use_Case the node type is scoped to.'];
}

// ----------------------------------------------------------- error surface

/**
 * Surface a registration rejection with the offending field identified
 * (Requirement 8.5: invalid Port declarations rejected identifying the
 * invalid declaration; the backend reports details.field for every
 * DeclarationError and unbuilt-architecture mapping).
 */
export function registrationErrorView(err: unknown): {
  message: string;
  field?: string;
} {
  if (err instanceof ApiError) {
    const field =
      err.details && typeof (err.details as Record<string, unknown>).field === 'string'
        ? ((err.details as Record<string, unknown>).field as string)
        : undefined;
    return { message: err.message, field };
  }
  if (err instanceof Error) {
    return { message: err.message };
  }
  return { message: 'Registration failed' };
}
