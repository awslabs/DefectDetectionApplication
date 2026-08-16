/**
 * Node configuration panel (Requirements 1.7, 1.8, 2.6, 2.7).
 *
 * Right sidebar shown when a canvas node is selected. Renders the node's
 * parameter schema as CloudScape form controls (per declared paramType)
 * with each parameter's current value, and validates edits with the
 * shared parameter constraint predicate (`checkParameterValue`),
 * displaying the violation message as an inline field error.
 *
 * Special parameters:
 *   - `model_ref` (model_inference / llm_inference `modelName`): a
 *     Select populated from the model registry API filtered by the
 *     selected Use_Case (Requirement 2.6) and by node type
 *     (vllm-triton-inference Requirements 6.2, 8.3): `llm_inference`
 *     lists only `model_type === 'vllm'` records, every other
 *     model_ref consumer excludes them; an empty vLLM list renders the
 *     select empty with "No vLLM models are registered for this use
 *     case" (6.11).
 *   - Custom_Python_Node: `code` renders a code editor textarea and the
 *     `input_port_type` / `output_port_type` parameters render port-type
 *     pickers over PORT_TYPES (Requirement 2.7); changes flow into
 *     `node.data.parameters`, so the canvas port handles update via
 *     `resolvedPorts`. For the custom Python node types the `code`
 *     editor additionally gets the Code_Assistant panel below it
 *     (custom-node-code-assist Requirements 1.1, 1.2), rendered only
 *     for workflow-editing roles (6.1, 6.5); accepted code flows
 *     through the same `onParametersChange` path as manual edits (2.5,
 *     2.7). A 750 ms-debounced Import_Analyzer effect watches the
 *     effective `code` value and reconciles the derived pip list into
 *     the `requirements` parameter (custom-node-code-assist
 *     Requirements 3.1, 3.5, 3.10), whose control renders as a
 *     multiline Textarea with a read-only badge annotation list for
 *     derived and needs-review entries (3.6, 3.7).
 *   - bool parameters render as a labeled checkbox (e.g. the
 *     mqtt_publish "AWS IoT support" option); parameters declaring
 *     `dependsOn` are shown only while the named bool parameter's
 *     effective value is true (`isParameterVisible`).
 *
 * Per-parameter help: every catalog parameter carries a `description`
 * (served by the node-catalog endpoint) rendered as the FormField
 * description under the control's label — except bool parameters (the
 * description renders below the checkbox, which carries its own label)
 * and descriptions over LONG_DESCRIPTION_THRESHOLD characters (rendered
 * fully inside a collapsible "Syntax help" section below the control so
 * e.g. the condition expression-language docs don't crowd the panel).
 * Catalog-served `examples` render as a compact "Examples:" row of
 * clickable monospace chips under the control; clicking one fills the
 * field with the full example value (long chips are truncated for
 * display only). The PARAMETER_HELP map (keyed
 * by `typeId.parameterName`, falling back to the bare parameter name)
 * supplies a fallback description plus an optional expandable
 * "Show examples" section with worked examples — e.g. the
 * rule-expression syntax and worked examples for the
 * inference_filter/conditional `condition` parameters and
 * mqtt_publish's `payload_template` placeholders.
 */

import { useEffect, useRef, useState } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Textarea from '@cloudscape-design/components/textarea';
import { apiService, type CodeAssistContract } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import CodeAssistPanel from '../../components/code-assist/CodeAssistPanel';
import type { Device, UserRole } from '../../types';
import type { BuilderNode } from './builderGraph';
import {
  applyAravisCameraSelection,
  applyCameraSelection,
  cameraDeviceValue,
  cameraDisplayName,
  cameraIdValue,
  defaultManualEntry,
  getCameraBindingHint,
  isAravisCompatibleCamera,
  isCameraReferenceParameter,
  isV4l2CompatibleCamera,
  type CameraBindingHint,
  type CameraSourceEntry,
} from './cameraReference';
import {
  deriveRequirements,
  extractImports,
  parseRequirements,
  reconcileRequirements,
} from './importAnalyzer';
import {
  ERROR_MAPPINGS_INVALID,
  MAX_MAPPINGS,
  parseMappings,
  parseStaticJson,
  type MetadataConfigError,
} from './metadataConfig';
import { checkParameterValue } from './parameters';
import { canEditWorkflows } from './WorkflowToolbar';
import {
  PORT_TYPES,
  SOURCE_KIND_TO_SOURCE_TYPE,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
} from './types';

// --------------------------------------------------------------------------
// Parameter value helpers
// --------------------------------------------------------------------------

/**
 * The value the panel displays and validates for a parameter: the
 * explicitly set value when the key is present (an explicit null counts
 * as cleared), else the declared default. Mirrors the inline validator's
 * effective-value semantics so the panel and canvas markers agree.
 */
export function effectiveParameterValue(
  parameters: Record<string, JsonValue>,
  descriptor: ParameterDescriptor
): JsonValue | null | undefined {
  if (Object.prototype.hasOwnProperty.call(parameters, descriptor.name)) {
    return parameters[descriptor.name];
  }
  return descriptor.default;
}

/**
 * Whether a parameter's control is currently visible (catalog
 * `dependsOn` field). Two gating forms (trigger-activation-runtime
 * Requirements 3.1, 3.6):
 *   - bare name (`"aws_iot"`): shown only while the named bool
 *     parameter's effective value is true (existing semantics,
 *     unchanged for all pre-existing descriptors);
 *   - `"name=value"` (`"concurrency_policy=queue"`, `"mode=poll"`):
 *     shown only while the named parameter's effective value (explicit
 *     value when set, else the declared default), rendered as a
 *     string, equals the literal — enum-selection gating.
 * Parameters without `dependsOn` (or referencing an unknown parameter)
 * are always visible. Hidden parameters are optional by catalog
 * convention, so hiding them never suppresses a validation error.
 */
export function isParameterVisible(
  descriptor: ParameterDescriptor,
  allParameters: ParameterDescriptor[],
  parameters: Record<string, JsonValue>
): boolean {
  const dependsOn = descriptor.dependsOn;
  if (dependsOn === undefined || dependsOn === null || dependsOn === '') {
    return true;
  }
  const separator = dependsOn.indexOf('=');
  const controllingName = separator === -1 ? dependsOn : dependsOn.slice(0, separator);
  const controlling = allParameters.find((parameter) => parameter.name === controllingName);
  if (controlling === undefined) {
    return true;
  }
  const effective = effectiveParameterValue(parameters, controlling);
  if (separator === -1) {
    // Bare name: bool-truthy gating, byte-for-byte the pre-feature check.
    return effective === true;
  }
  // "name=value": equality against the effective value's string form.
  return textValue(effective) === dependsOn.slice(separator + 1);
}

// --------------------------------------------------------------------------
// Unified input source-parameter gating (Requirement 5.3)
// --------------------------------------------------------------------------

/**
 * The unified input node type whose visible parameter set is driven by
 * the selected `source_kind` rather than by bool `dependsOn` gating.
 */
export const UNIFIED_INPUT_TYPE_ID = 'unified_input';

/** The unified input node's source-selector (enum) parameter. */
export const SOURCE_KIND_PARAMETER = 'source_kind';

/**
 * The effective `source_kind` of a unified_input node: the explicitly
 * set value when present, else the `source_kind` parameter's declared
 * default (e.g. `"folder"`). Returns null when the node is not a
 * unified_input node or carries no string source_kind value.
 */
export function unifiedSourceKind(node: BuilderNode): string | null {
  if (node.data.descriptor.typeId !== UNIFIED_INPUT_TYPE_ID) {
    return null;
  }
  const descriptor = node.data.descriptor.parameters.find(
    (parameter) => parameter.name === SOURCE_KIND_PARAMETER
  );
  const effective =
    descriptor !== undefined
      ? effectiveParameterValue(node.data.parameters, descriptor)
      : node.data.parameters[SOURCE_KIND_PARAMETER];
  return typeof effective === 'string' ? effective : null;
}

/**
 * The parameter names visible on a unified_input node: `source_kind`
 * plus exactly the parameter names of the served catalog descriptor for
 * the source type its `source_kind` expands to
 * (`SOURCE_KIND_TO_SOURCE_TYPE`). Returns null for non-unified nodes so
 * the caller leaves their visibility unchanged. While the served
 * catalog has not yet loaded (or the mapped source descriptor is
 * absent) only `source_kind` is returned, so the selector always shows.
 */
export function unifiedVisibleParameterNames(
  node: BuilderNode,
  catalog: NodeTypeDescriptor[]
): Set<string> | null {
  const sourceKind = unifiedSourceKind(node);
  if (sourceKind === null) {
    return null;
  }
  const visible = new Set<string>([SOURCE_KIND_PARAMETER]);
  const sourceType = (SOURCE_KIND_TO_SOURCE_TYPE as Record<string, string>)[sourceKind];
  const sourceDescriptor = catalog.find((entry) => entry.typeId === sourceType);
  if (sourceDescriptor !== undefined) {
    for (const parameter of sourceDescriptor.parameters) {
      visible.add(parameter.name);
    }
  }
  return visible;
}

/**
 * Parse a numeric field's raw text: empty clears the value (null);
 * non-numeric text is kept as-is so the constraint predicate reports a
 * type violation inline (Requirement 1.8).
 */
export function parseNumericInput(raw: string): JsonValue | null {
  if (raw.trim() === '') {
    return null;
  }
  const parsed = Number(raw);
  return Number.isNaN(parsed) ? raw : parsed;
}

/**
 * Human-friendly control labels for parameters whose raw catalog name
 * would read poorly (the mqtt_publish AWS IoT parameter family). Any
 * parameter not listed here is labeled with its catalog name.
 */
const PARAMETER_DISPLAY_LABELS: Record<string, string> = {
  aws_iot: 'AWS IoT support',
  iot_thing_name: 'IoT thing name',
  iot_ca_cert_path: 'Root CA certificate path (on device)',
  iot_client_cert_path: 'Client certificate path (on device)',
  iot_private_key_path: 'Private key path (on device)',
  // Metadata node (workflow-manager-gaps Requirement 6.2)
  mappings: 'Metadata mappings',
  static_json: 'Static JSON',
};

/** The label shown for a parameter's form control. */
export function parameterLabel(descriptor: ParameterDescriptor): string {
  return PARAMETER_DISPLAY_LABELS[descriptor.name] ?? descriptor.name;
}

// --------------------------------------------------------------------------
// Per-parameter help (description + expandable examples)
// --------------------------------------------------------------------------

/** One worked example rendered inside a parameter's "Examples" section. */
export interface ParameterExample {
  /** The literal value to enter, rendered in a code style. */
  code: string;
  /** What the example does. */
  label: string;
}

/** Help content attached to a parameter's form field. */
export interface ParameterHelp {
  /** Rendered as the FormField description under the label. */
  description: string;
  /** Optional worked examples shown in an expandable section. */
  examples?: ParameterExample[];
}

/**
 * The rule-expression syntax shared by every executor-evaluated
 * `condition` parameter (inference_filter, conditional, digital_output).
 * Mirrors the evaluator in the LocalServer workflow engine and the test
 * sandbox: fields `is_anomalous` (boolean) and `confidence` (number)
 * from the inference metadata; comparisons ==, !=, >=, <=, >, <;
 * logic &&, ||, ! and parentheses; a bare field is tested for truth.
 */
const CONDITION_SYNTAX =
  'An expression over the inference metadata fields is_anomalous ' +
  '(true/false) and confidence (0..1). Supports the comparisons ==, !=, ' +
  '>=, <=, >, <, the logic operators && (and), || (or), ! (not), and ' +
  'parentheses.';

/** The shared examples for condition parameters. */
const CONDITION_EXAMPLES: ParameterExample[] = [
  { code: 'is_anomalous == true', label: 'matches only anomalies' },
  {
    code: 'is_anomalous == true && confidence >= 0.8',
    label: 'matches high-confidence anomalies',
  },
  {
    code: '!(is_anomalous == true)',
    label: 'matches normal (non-anomalous) results',
  },
];

/**
 * Help content per parameter, keyed by `typeId.parameterName` with a
 * fallback lookup on the bare parameter name (for parameters that mean
 * the same thing on every node type, like `condition`).
 */
export const PARAMETER_HELP: Record<string, ParameterHelp> = {
  'inference_filter.condition': {
    description:
      'Inference results continue downstream only while this condition ' +
      'holds. ' +
      CONDITION_SYNTAX,
    examples: CONDITION_EXAMPLES,
  },
  'conditional.condition': {
    description:
      'Routes each inference result to one of the two outputs: the ' +
      '"true" output receives the metadata when this condition holds, ' +
      'the "false" output when it does not. ' +
      CONDITION_SYNTAX,
    examples: [
      {
        code: 'is_anomalous == true',
        label: 'anomalies take the "true" path, normal results the "false" path',
      },
      {
        code: 'is_anomalous == true && confidence >= 0.8',
        label: 'only high-confidence anomalies take the "true" path',
      },
    ],
  },
  // Generic fallback for other condition parameters using the same rule
  // dialect (digital_output's actuation condition).
  condition: {
    description:
      'The node acts only when this condition holds. ' + CONDITION_SYNTAX,
    examples: CONDITION_EXAMPLES.slice(0, 2),
  },
  'mqtt_publish.payload_template': {
    description:
      'The message payload. Placeholders in curly braces are replaced ' +
      'from the inference metadata: {inference_json} (the full metadata ' +
      'as JSON), {is_anomalous}, and {confidence}.',
    examples: [
      { code: '{inference_json}', label: 'publishes the full metadata as JSON' },
      {
        code: 'anomalous={is_anomalous} score={confidence}',
        label: 'publishes a custom text payload',
      },
    ],
  },
};

/**
 * The help content for a parameter of a node type: the
 * `typeId.parameterName` entry when present, else the bare
 * parameter-name fallback, else undefined (no help rendered).
 */
export function parameterHelp(
  typeId: string,
  descriptor: ParameterDescriptor
): ParameterHelp | undefined {
  return (
    PARAMETER_HELP[`${typeId}.${descriptor.name}`] ?? PARAMETER_HELP[descriptor.name]
  );
}

// --------------------------------------------------------------------------
// Catalog-served examples (clickable chips) and long-description handling
// --------------------------------------------------------------------------

/**
 * Descriptions longer than this render inside a collapsible "Syntax
 * help" section instead of the always-visible FormField description, so
 * parameters with extensive documentation (the condition expression
 * language) don't crowd the panel.
 */
export const LONG_DESCRIPTION_THRESHOLD = 200;

/** Maximum characters an example chip displays before truncating. */
export const EXAMPLE_CHIP_MAX_LENGTH = 40;

/** The text a catalog example value renders as (chips and insertion). */
export function exampleText(value: JsonValue): string {
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value);
}

/** Chip display text: long example values are truncated with an ellipsis. */
export function exampleChipLabel(value: JsonValue): string {
  const text = exampleText(value);
  if (text.length <= EXAMPLE_CHIP_MAX_LENGTH) {
    return text;
  }
  return `${text.slice(0, EXAMPLE_CHIP_MAX_LENGTH - 1)}\u2026`;
}

/**
 * The catalog-served examples row: an "Examples:" label followed by
 * small monospace chips. Clicking a chip fills the parameter's control
 * with the full example value (truncation is display-only).
 */
function ExampleChips({
  descriptor,
  onChange,
}: {
  descriptor: ParameterDescriptor;
  onChange: (value: JsonValue | null) => void;
}) {
  const examples = descriptor.examples ?? [];
  if (examples.length === 0) {
    return null;
  }
  return (
    <div
      role="group"
      aria-label={`Examples for ${descriptor.name}`}
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 4,
        marginTop: 4,
      }}
    >
      <span style={{ fontSize: 11, color: '#5f6b7a' }}>Examples:</span>
      {examples.map((example, index) => {
        const full = exampleText(example);
        return (
          <button
            key={`${index}-${full}`}
            type="button"
            title={full}
            aria-label={`Use example ${full}`}
            onClick={() => onChange(example)}
            style={{
              fontFamily: 'monospace',
              fontSize: 11,
              lineHeight: '16px',
              padding: '1px 6px',
              border: '1px solid #d1d5db',
              borderRadius: 10,
              background: '#f2f8fd',
              color: '#0972d3',
              cursor: 'pointer',
              maxWidth: '100%',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {exampleChipLabel(example)}
          </button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------
// Code_Assistant integration (custom-node-code-assist Requirements 1.1, 1.2)
// --------------------------------------------------------------------------

/**
 * Node_Contract per custom Python node type: the Code_Assistant panel
 * renders below the `code` parameter editor only for these node types
 * (custom-node-code-assist Requirements 1.1, 1.2), and only for roles
 * that may edit workflows (`canEditWorkflows` — Requirements 6.1, 6.5).
 */
export const CODE_ASSIST_CONTRACTS: Record<string, CodeAssistContract> = {
  custom_python: 'process_frame_or_handle',
  custom_python_preprocess: 'process_frame',
  // custom-python-source Requirement 9.6: the source node's code editor
  // gets the assistant panel, the derived-requirements pipeline, and
  // role gating on the same terms as the other Custom Python node types.
  custom_python_source: 'produce_frame',
};

// --------------------------------------------------------------------------
// Import_Analyzer integration (custom-node-code-assist Requirements
// 3.1, 3.5, 3.6, 3.7, 3.10)
// --------------------------------------------------------------------------

/**
 * Debounce interval for the Import_Analyzer: the derivation runs 750 ms
 * after the last change to the effective `code` value, comfortably
 * inside Requirement 3.1's 2-second bound.
 */
export const IMPORT_ANALYSIS_DEBOUNCE_MS = 750;

/** The parameter holding the node's pip requirements list. */
const REQUIREMENTS_PARAMETER = 'requirements';

/** The effective `code` text of a custom Python node, or null otherwise. */
function effectiveCodeText(node: BuilderNode | null): string | null {
  if (node === null || CODE_ASSIST_CONTRACTS[node.data.descriptor.typeId] === undefined) {
    return null;
  }
  const descriptor = node.data.descriptor.parameters.find(
    (parameter) => parameter.name === 'code'
  );
  if (descriptor === undefined) {
    return null;
  }
  return textValue(effectiveParameterValue(node.data.parameters, descriptor));
}

/** The effective `requirements` text of a node (declared default honored). */
function effectiveRequirementsText(node: BuilderNode): string {
  const descriptor = node.data.descriptor.parameters.find(
    (parameter) => parameter.name === REQUIREMENTS_PARAMETER
  );
  const effective =
    descriptor !== undefined
      ? effectiveParameterValue(node.data.parameters, descriptor)
      : node.data.parameters[REQUIREMENTS_PARAMETER];
  return textValue(effective);
}

/**
 * The 750 ms-debounced Import_Analyzer effect (custom-node-code-assist
 * Requirements 3.1, 3.5, 3.10): whenever the effective `code` value of
 * a custom Python node settles, run `extractImports` →
 * `deriveRequirements` → `reconcileRequirements(currentRequirements,
 * derived)` and write the `requirements` parameter through
 * `onParametersChange` — but only when the reconciled text actually
 * differs from the current text (reconciliation is idempotent, so a
 * clean pass writes nothing). An `{ok: false}` scan (unparseable code,
 * Requirement 3.10) applies nothing. The latest node state and
 * callback are read through a ref at fire time so manual requirements
 * edits made during the debounce window are never clobbered.
 */
function useImportAnalysis(
  node: BuilderNode | null,
  onParametersChange: (nodeId: string, parameters: Record<string, JsonValue>) => void
): void {
  const latest = useRef({ node, onParametersChange });
  latest.current = { node, onParametersChange };

  const nodeId = node?.id ?? null;
  const code = effectiveCodeText(node);

  useEffect(() => {
    if (nodeId === null || code === null) {
      return undefined;
    }
    const timer = setTimeout(() => {
      const current = latest.current.node;
      if (current === null || current.id !== nodeId) {
        return;
      }
      const scan = extractImports(code);
      if (!scan.ok) {
        // Unparseable code changes nothing (Requirement 3.10).
        return;
      }
      const derived = deriveRequirements(scan.imports);
      const currentText = effectiveRequirementsText(current);
      const reconciled = reconcileRequirements(currentText, derived);
      if (reconciled !== currentText) {
        latest.current.onParametersChange(nodeId, {
          ...current.data.parameters,
          [REQUIREMENTS_PARAMETER]: reconciled,
        });
      }
    }, IMPORT_ANALYSIS_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [nodeId, code]);
}

/** The display name of a derived requirements line: text before the marker. */
function derivedEntryLabel(raw: string): string {
  const hash = raw.indexOf('#');
  const name = (hash === -1 ? raw : raw.slice(0, hash)).trim();
  return name === '' ? raw.trim() : name;
}

/**
 * Read-only annotation list under the `requirements` Textarea
 * (custom-node-code-assist Requirements 3.6, 3.7): each derived
 * (marker-carrying) entry renders with a "derived" badge, and entries
 * whose import had no Import_Mapping additionally carry the
 * "verify package name" warning badge — the same Cloudscape Badge
 * styling as the node-designer badges. The raw text above stays the
 * editing surface; this list is purely informational.
 */
export function RequirementsAnnotations({ text }: { text: string }) {
  const derivedEntries = parseRequirements(text).filter((entry) => entry.derived);
  if (derivedEntries.length === 0) {
    return null;
  }
  return (
    <ul
      aria-label="Derived requirements"
      style={{ listStyle: 'none', margin: '4px 0 0', padding: 0 }}
    >
      {derivedEntries.map((entry, index) => (
        <li
          key={`${index}-${entry.raw}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 4,
            marginBottom: 2,
          }}
        >
          <span style={{ fontFamily: 'monospace', fontSize: 12 }}>
            {derivedEntryLabel(entry.raw)}
          </span>
          <Badge color="blue">derived</Badge>
          {entry.needsReview && <Badge color="severity-medium">verify package name</Badge>}
        </li>
      ))}
    </ul>
  );
}

/** Parameter names that declare per-instance port types (Requirement 2.7). */
const PORT_TYPE_PARAMETERS = ['input_port_type', 'output_port_type'];

function isPortTypePicker(descriptor: ParameterDescriptor): boolean {
  return PORT_TYPE_PARAMETERS.includes(descriptor.name);
}

/** The discrete value choices for enum and port-type parameters. */
function selectValues(descriptor: ParameterDescriptor): JsonValue[] {
  const declared = descriptor.constraints?.values;
  if (declared !== undefined && declared.length > 0) {
    return declared;
  }
  if (isPortTypePicker(descriptor)) {
    return [...PORT_TYPES];
  }
  return [];
}

function textValue(value: JsonValue | null | undefined): string {
  if (value === null || value === undefined) {
    return '';
  }
  return typeof value === 'string' ? value : String(value);
}

// --------------------------------------------------------------------------
// Model registry options for model_ref parameters (Requirement 2.6;
// vllm-triton-inference Requirements 6.2, 6.11, 8.3)
// --------------------------------------------------------------------------

/** Load state of the model_ref Select options. */
export interface ModelOptionsState {
  status: 'pending' | 'loading' | 'error' | 'finished';
  options: SelectProps.Option[];
  errorText?: string;
}

/** The node type whose model_ref selects a vLLM model. */
export const LLM_INFERENCE_TYPE_ID = 'llm_inference';

/** The Model_Registry model type of vLLM_Model_Records. */
export const VLLM_MODEL_TYPE = 'vllm';

/**
 * Per-node-type model_ref filter (vllm-triton-inference Requirements
 * 6.2, 8.3): `llm_inference` offers exactly the Use_Case's registered
 * vLLM_Model_Records (`model_type === 'vllm'`); every other model_ref
 * consumer (the vision `model_inference` node) excludes vLLM records,
 * keeping its option list identical to pre-feature behavior (no vLLM
 * records existed before this feature).
 */
export function modelMatchesNodeType(
  typeId: string,
  modelType: string | null | undefined
): boolean {
  if (typeId === LLM_INFERENCE_TYPE_ID) {
    return modelType === VLLM_MODEL_TYPE;
  }
  return modelType !== VLLM_MODEL_TYPE;
}

/**
 * The model_ref Select's empty-list message (vllm-triton-inference
 * Requirement 6.11): a Use_Case with no registered vLLM models renders
 * the llm_inference select empty with an explicit indication.
 */
export function modelSelectEmptyText(typeId: string): string {
  if (typeId === LLM_INFERENCE_TYPE_ID) {
    return 'No vLLM models are registered for this use case';
  }
  return 'No models registered for this use case';
}

/**
 * Fetch the models registered for the selected Use_Case whenever the
 * panel shows a model_ref parameter (Requirement 2.6), filtered per
 * node type (`modelMatchesNodeType`). `typeId` is null when the
 * selected node has no model_ref parameter (no fetch).
 */
function useModelOptions(typeId: string | null): ModelOptionsState {
  const { selectedUsecaseId } = useUsecase();
  const [state, setState] = useState<ModelOptionsState>({ status: 'pending', options: [] });

  useEffect(() => {
    if (typeId === null) {
      return undefined;
    }
    if (!selectedUsecaseId) {
      setState({
        status: 'error',
        options: [],
        errorText: 'Select a use case to list its models',
      });
      return undefined;
    }
    let cancelled = false;
    setState({ status: 'loading', options: [] });
    apiService
      .listModels({ usecase_id: selectedUsecaseId })
      .then((response) => {
        if (cancelled) {
          return;
        }
        const options = (response.models ?? [])
          .filter((model) => modelMatchesNodeType(typeId, model.model_type))
          .map((model) => ({
            label: model.name,
            value: model.name,
            description: `v${model.version} (${model.stage})`,
          }));
        setState({ status: 'finished', options });
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setState({
            status: 'error',
            options: [],
            errorText: error.message || 'Failed to load models',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [typeId, selectedUsecaseId]);

  return state;
}

/**
 * The served node catalog, fetched only while a unified_input node is
 * selected (`enabled`) so its underlying source descriptors are
 * available for source-parameter gating (Requirement 5.3). Reuses the
 * same node-catalog endpoint the Workflow_Builder loads the palette
 * from, keyed by the selected Use_Case so any registered Custom_Node_
 * Types are merged in identically. Returns an empty list until loaded
 * or on failure; the gate then shows only `source_kind`.
 */
function useNodeCatalog(enabled: boolean): NodeTypeDescriptor[] {
  const { selectedUsecaseId } = useUsecase();
  const [catalog, setCatalog] = useState<NodeTypeDescriptor[]>([]);

  useEffect(() => {
    if (!enabled) {
      return undefined;
    }
    let cancelled = false;
    apiService
      .getWorkflowNodeCatalog(selectedUsecaseId || undefined)
      .then((response) => {
        if (!cancelled) {
          setCatalog(response.nodeTypes ?? []);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCatalog([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, selectedUsecaseId]);

  return catalog;
}

// --------------------------------------------------------------------------
// Per-parameter form controls
// --------------------------------------------------------------------------

interface ParameterFieldProps {
  /** The node type owning the parameter (drives the help lookup). */
  typeId: string;
  descriptor: ParameterDescriptor;
  value: JsonValue | null | undefined;
  onChange: (value: JsonValue | null) => void;
  modelOptions: ModelOptionsState;
}

function ParameterControl({ typeId, descriptor, value, onChange, modelOptions }: ParameterFieldProps) {
  const paramType = descriptor.paramType;

  // The pip requirements list of the custom Python node types edits as
  // a multiline Textarea (requirements.txt form, one entry per line —
  // custom-node-code-assist Requirement 3.6); the derived-entry badge
  // annotations render below it in ParameterField.
  if (
    descriptor.name === REQUIREMENTS_PARAMETER &&
    CODE_ASSIST_CONTRACTS[typeId] !== undefined
  ) {
    return (
      <Textarea
        rows={4}
        value={textValue(value)}
        onChange={({ detail }) => onChange(detail.value)}
        spellcheck={false}
        placeholder="one pip package per line"
        ariaLabel={descriptor.name}
      />
    );
  }

  if (paramType === 'bool') {
    // The checkbox carries its own label (e.g. "AWS IoT support"); the
    // enclosing FormField renders no separate label for bool parameters.
    return (
      <Checkbox checked={value === true} onChange={({ detail }) => onChange(detail.checked)}>
        {parameterLabel(descriptor)}
      </Checkbox>
    );
  }

  if (paramType === 'int' || paramType === 'float') {
    return (
      <Input
        type="number"
        step={paramType === 'int' ? 1 : 'any'}
        value={textValue(value)}
        onChange={({ detail }) => onChange(parseNumericInput(detail.value))}
        ariaLabel={descriptor.name}
      />
    );
  }

  if (paramType === 'code') {
    return (
      <Textarea
        rows={12}
        value={textValue(value)}
        onChange={({ detail }) => onChange(detail.value)}
        spellcheck={false}
        placeholder="# Python code"
        ariaLabel={descriptor.name}
      />
    );
  }

  if (paramType === 'model_ref') {
    const current = textValue(value);
    const selected =
      modelOptions.options.find((option) => option.value === current) ??
      (current !== '' ? { label: current, value: current } : null);
    return (
      <Select
        selectedOption={selected}
        onChange={({ detail }) => onChange(detail.selectedOption.value ?? null)}
        options={modelOptions.options}
        statusType={modelOptions.status === 'pending' ? 'finished' : modelOptions.status}
        loadingText="Loading models"
        errorText={modelOptions.errorText}
        placeholder="Choose a model"
        empty={modelSelectEmptyText(typeId)}
        ariaLabel={descriptor.name}
      />
    );
  }

  if (paramType === 'enum' || isPortTypePicker(descriptor)) {
    const values = selectValues(descriptor);
    const options: SelectProps.Option[] = values.map((member, index) => ({
      label: textValue(member),
      value: String(index),
    }));
    const selectedIndex = values.findIndex((member) => member === value);
    return (
      <Select
        selectedOption={selectedIndex >= 0 ? options[selectedIndex] : null}
        onChange={({ detail }) => {
          const index = Number(detail.selectedOption.value);
          onChange(values[index] ?? null);
        }}
        options={options}
        placeholder="Choose a value"
        ariaLabel={descriptor.name}
      />
    );
  }

  // string (and any unknown paramType falls back to free text; the
  // constraint predicate reports unknown declared types inline).
  return (
    <Input
      value={textValue(value)}
      onChange={({ detail }) => onChange(detail.value)}
      ariaLabel={descriptor.name}
    />
  );
}

function ParameterField(props: ParameterFieldProps) {
  const { typeId, descriptor, value, onChange } = props;
  const violation = checkParameterValue(descriptor, value === undefined ? null : value);
  const label = parameterLabel(descriptor);
  const help = parameterHelp(typeId, descriptor);
  // The catalog-served description is authoritative; the PARAMETER_HELP
  // description is a fallback for descriptors without one.
  const description = descriptor.description ?? help?.description;
  const isBool = descriptor.paramType === 'bool';
  // Long descriptions (the condition expression-language docs) render
  // fully — wrapped, small font — inside a collapsible "Syntax help"
  // section below the control so the panel stays usable. Bool
  // descriptions render below the checkbox (the checkbox carries its
  // own label, so the FormField description slot sits awkwardly above).
  const isLongDescription =
    description !== undefined &&
    description !== null &&
    description.length > LONG_DESCRIPTION_THRESHOLD;
  const fieldDescription = isBool || isLongDescription ? undefined : description ?? undefined;
  return (
    <FormField
      label={
        isBool ? undefined : descriptor.required ? (
          label
        ) : (
          <span>
            {label} <i>- optional</i>
          </span>
        )
      }
      description={fieldDescription}
      errorText={violation?.message}
      stretch
    >
      <ParameterControl {...props} />
      {isBool && !isLongDescription && description !== undefined && description !== null && (
        <Box fontSize="body-s" color="text-body-secondary">
          {description}
        </Box>
      )}
      {isLongDescription && (
        <ExpandableSection headerText="Syntax help" variant="footer">
          <div
            style={{
              fontSize: 12,
              lineHeight: '18px',
              whiteSpace: 'pre-wrap',
              overflowWrap: 'break-word',
            }}
          >
            {description}
          </div>
        </ExpandableSection>
      )}
      <ExampleChips descriptor={descriptor} onChange={onChange} />
      {help?.examples !== undefined && help.examples.length > 0 && (
        <ExpandableSection headerText="Show examples" variant="footer">
          <ul
            aria-label={`Examples for ${descriptor.name}`}
            style={{ listStyle: 'none', margin: 0, padding: 0 }}
          >
            {help.examples.map((example) => (
              <li key={example.code}>
                <Box fontSize="body-s" padding={{ bottom: 'xxs' }}>
                  <code>{example.code}</code>
                  <Box variant="span" color="text-body-secondary" fontSize="body-s">
                    {' '}
                    - {example.label}
                  </Box>
                </Box>
              </li>
            ))}
          </ul>
        </ExpandableSection>
      )}
    </FormField>
  );
}

// --------------------------------------------------------------------------
// Camera reference control (camera-registry-sync Requirements 7.1-7.4)
// --------------------------------------------------------------------------

/** Load state of an async Select's options. */
interface AsyncOptionsState<T> {
  status: 'pending' | 'loading' | 'error' | 'finished';
  items: T[];
  errorText?: string;
}

/** Load state of the camera dropdown, including the never-synced flag. */
interface CameraOptionsState extends AsyncOptionsState<CameraSourceEntry> {
  neverSynced?: boolean;
}

/** The Select statusType for an async options state. */
function selectStatus(state: { status: AsyncOptionsState<unknown>['status'] }) {
  return state.status === 'pending' ? ('finished' as const) : state.status;
}

/**
 * The camera dropdown option for one Camera_Source (Requirement 7.4).
 * Aravis options describe the source by its camera id instead of its
 * device path (aravis-camera-input Requirement 3.5); name, type, sync
 * status, and the staleness badge render identically for both flavors.
 */
export function cameraOption(camera: CameraSourceEntry, aravis = false): SelectProps.Option {
  const tags = [camera.type, camera.sync_status, camera.absent ? 'absent' : null].filter(
    (tag): tag is string => typeof tag === 'string' && tag !== ''
  );
  return {
    value: camera.camera_source_id,
    label: cameraDisplayName(camera),
    // Staleness badge on the option label (Requirement 7.4).
    labelTag: camera.stale ? 'Stale' : undefined,
    description: (aravis ? cameraIdValue(camera) : cameraDeviceValue(camera)) ?? undefined,
    tags,
  };
}

interface CameraReferenceFieldProps {
  typeId: string;
  /** The `device` parameter descriptor. */
  descriptor: ParameterDescriptor;
  /** The node's full parameters record. */
  parameters: Record<string, JsonValue>;
  /** The node's current advisory binding hint, when any. */
  hint: CameraBindingHint | null;
  /** Manual-entry edits: the full updated parameters record. */
  onParametersChange: (parameters: Record<string, JsonValue>) => void;
  /** A camera selection: updated parameters plus the binding hint. */
  onCameraSelection: (parameters: Record<string, JsonValue>, hint: CameraBindingHint) => void;
  modelOptions: ModelOptionsState;
}

/**
 * The camera reference control for the `icam_source` node's `device`
 * parameter: a reference-device selector over the current Use_Case's
 * devices and a camera dropdown fed by `GET /devices/{id}/cameras`
 * showing each Camera_Source's name, type, path/URL, sync status, and
 * staleness badge (Requirements 7.1, 7.4). Selecting a camera populates
 * the node's parameters and records the advisory binding hint
 * (Requirement 7.2). The "Manual entry" toggle retains the plain text
 * input (Requirement 7.3).
 */
function CameraReferenceField(props: CameraReferenceFieldProps) {
  const { typeId, descriptor, parameters, hint, onParametersChange, onCameraSelection } = props;
  const { selectedUsecaseId } = useUsecase();
  // The aravis_camera_source flavor of the control (aravis-camera-input
  // Requirements 3.2, 3.3, 3.5): options filtered to Aravis-compatible
  // sources, described by camera id, applied through
  // applyAravisCameraSelection. The icam_source flavor filters options to
  // V4L2-compatible sources and populates the device path through
  // applyCameraSelection (csi-icam-input-nodes Requirement 5.2).
  const isAravis = typeId === 'aravis_camera_source';
  const isIcam = typeId === 'icam_source';

  const [manual, setManual] = useState(() =>
    defaultManualEntry(parameters, descriptor.name, descriptor.default, hint)
  );
  const [devices, setDevices] = useState<AsyncOptionsState<Device>>({
    status: 'pending',
    items: [],
  });
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(
    hint?.sourceDeviceId ?? null
  );
  const [cameras, setCameras] = useState<CameraOptionsState>({ status: 'pending', items: [] });
  const [selectedCameraId, setSelectedCameraId] = useState<string | null>(
    hint?.cameraSourceId ?? null
  );

  // Devices of the current Use_Case (the reference-device selector).
  useEffect(() => {
    if (manual) {
      return undefined;
    }
    if (!selectedUsecaseId) {
      setDevices({
        status: 'error',
        items: [],
        errorText: 'Select a use case to list its devices',
      });
      return undefined;
    }
    let cancelled = false;
    setDevices({ status: 'loading', items: [] });
    apiService
      .listDevices(selectedUsecaseId)
      .then((response) => {
        if (!cancelled) {
          setDevices({ status: 'finished', items: response.devices ?? [] });
        }
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setDevices({
            status: 'error',
            items: [],
            errorText: error.message || 'Failed to load devices',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [manual, selectedUsecaseId]);

  // The selected device's Camera_Registry entries (Requirement 7.1).
  useEffect(() => {
    if (manual || selectedDeviceId === null) {
      return undefined;
    }
    if (!selectedUsecaseId) {
      return undefined;
    }
    let cancelled = false;
    setCameras({ status: 'loading', items: [] });
    apiService
      .getDeviceCameras(selectedDeviceId, selectedUsecaseId)
      .then((response) => {
        if (!cancelled) {
          setCameras({
            status: 'finished',
            items: response.cameras ?? [],
            neverSynced: response.state === 'never-synced',
          });
        }
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setCameras({
            status: 'error',
            items: [],
            errorText: error.message || 'Failed to load the device cameras',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [manual, selectedDeviceId, selectedUsecaseId]);

  // Inline validation of the effective device value stays visible in
  // both modes (the same constraint predicate as every parameter).
  const effective = effectiveParameterValue(parameters, descriptor);
  const violation = checkParameterValue(descriptor, effective === undefined ? null : effective);
  const label = parameterLabel(descriptor);

  const manualToggle = (
    <Checkbox
      checked={manual}
      onChange={({ detail }) => setManual(detail.checked)}
      ariaLabel={`Manual entry for ${descriptor.name}`}
    >
      Manual entry
    </Checkbox>
  );

  // Manual entry keeps the plain text input exactly as before
  // (Requirement 7.3), including description, examples, and validation.
  if (manual) {
    return (
      <SpaceBetween size="xxs">
        {manualToggle}
        <ParameterField
          typeId={typeId}
          descriptor={descriptor}
          value={effective}
          onChange={(value) =>
            onParametersChange({ ...parameters, [descriptor.name]: value })
          }
          modelOptions={props.modelOptions}
        />
      </SpaceBetween>
    );
  }

  const deviceOptions: SelectProps.Option[] = devices.items.map((device) => ({
    value: device.device_id,
    label: device.thing_name || device.device_id,
    description: device.status,
  }));
  const selectedDeviceOption =
    deviceOptions.find((option) => option.value === selectedDeviceId) ??
    (selectedDeviceId !== null ? { value: selectedDeviceId, label: selectedDeviceId } : null);

  // The Aravis node offers only Aravis-compatible sources (Requirement
  // 3.2); the ICAM node offers only V4L2-compatible sources
  // (csi-icam-input-nodes Requirement 5.2).
  const offeredCameras = isAravis
    ? cameras.items.filter(isAravisCompatibleCamera)
    : isIcam
      ? cameras.items.filter(isV4l2CompatibleCamera)
      : cameras.items;
  const cameraOptions = offeredCameras.map((camera) => cameraOption(camera, isAravis));
  const selectedCameraOption =
    cameraOptions.find((option) => option.value === selectedCameraId) ?? null;

  const onCameraChange = (option: SelectProps.Option) => {
    const camera = offeredCameras.find((entry) => entry.camera_source_id === option.value);
    if (camera === undefined || selectedDeviceId === null) {
      return;
    }
    setSelectedCameraId(camera.camera_source_id);
    const result = isAravis
      ? applyAravisCameraSelection(parameters, camera, selectedDeviceId)
      : applyCameraSelection(parameters, camera, selectedDeviceId);
    onCameraSelection(result.parameters, result.hint);
  };

  return (
    <SpaceBetween size="xxs">
      {manualToggle}
      <FormField
        label={
          descriptor.required ? (
            label
          ) : (
            <span>
              {label} <i>- optional</i>
            </span>
          )
        }
        description={descriptor.description ?? undefined}
        errorText={violation?.message}
        stretch
      >
        <SpaceBetween size="xxs">
          <Select
            selectedOption={selectedDeviceOption}
            onChange={({ detail }) => {
              setSelectedCameraId(null);
              setSelectedDeviceId(detail.selectedOption.value ?? null);
            }}
            options={deviceOptions}
            statusType={selectStatus(devices)}
            loadingText="Loading devices"
            errorText={devices.errorText}
            placeholder="Choose a reference device"
            empty="No devices in this use case"
            ariaLabel={`Reference device for ${descriptor.name}`}
          />
          <Select
            selectedOption={selectedCameraOption}
            onChange={({ detail }) => onCameraChange(detail.selectedOption)}
            options={cameraOptions}
            statusType={selectStatus(cameras)}
            loadingText="Loading cameras"
            errorText={cameras.errorText}
            disabled={selectedDeviceId === null}
            placeholder={
              selectedDeviceId === null ? 'Choose a device first' : 'Choose a camera'
            }
            empty={
              cameras.neverSynced === true
                ? 'This device has never synced its cameras'
                : 'No cameras registered for this device'
            }
            triggerVariant="option"
            ariaLabel={`Camera source for ${descriptor.name}`}
          />
          <Box fontSize="body-s" color="text-body-secondary">
            {`Current value: ${textValue(effective) || '(not set)'}`}
            {hint !== null && ` \u2014 linked to ${hint.cameraName} on ${hint.sourceDeviceId}`}
          </Box>
        </SpaceBetween>
        {/* Catalog-served examples stay available as quick manual fills. */}
        <ExampleChips
          descriptor={descriptor}
          onChange={(value) =>
            onParametersChange({ ...parameters, [descriptor.name]: value })
          }
        />
      </FormField>
    </SpaceBetween>
  );
}

// --------------------------------------------------------------------------
// Metadata node configuration (workflow-manager-gaps Requirements 6.2,
// 6.3, 6.7)
// --------------------------------------------------------------------------

/**
 * The metadata node type whose `mappings` and `static_json` parameters
 * render as the mapping rows editor and the static JSON textarea. The
 * palette entry itself appears automatically from the served catalog.
 */
export const METADATA_TYPE_ID = 'metadata';

/** The metadata node's JSON-array mappings parameter. */
const MAPPINGS_PARAMETER = 'mappings';

/** The metadata node's static JSON object parameter. */
const STATIC_JSON_PARAMETER = 'static_json';

/** One editable mapping row: raw (untrimmed) field path and output key. */
export interface MetadataMappingRow {
  path: string;
  key: string;
}

/**
 * The raw mapping rows in a `mappings` parameter value, untrimmed for
 * editing (`parseMappings` trims for validation, which would fight a
 * user typing leading/trailing spaces). Returns null when the value is
 * not parseable as a JSON array of `{path, key}` string pairs
 * (ERROR_MAPPINGS_INVALID) — the editor then falls back to a raw JSON
 * textarea so the invalid text stays visible and repairable.
 */
export function metadataMappingRows(
  raw: JsonValue | null | undefined
): MetadataMappingRow[] | null {
  if (raw === null || raw === undefined) {
    return [];
  }
  if (typeof raw !== 'string') {
    return null;
  }
  if (raw.trim() === '') {
    return [];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) {
    return null;
  }
  const rows: MetadataMappingRow[] = [];
  for (const entry of parsed) {
    const isObject = typeof entry === 'object' && entry !== null && !Array.isArray(entry);
    const path = isObject ? (entry as Record<string, unknown>).path : undefined;
    const key = isObject ? (entry as Record<string, unknown>).key : undefined;
    if (typeof path !== 'string' || typeof key !== 'string') {
      return null;
    }
    rows.push({ path, key });
  }
  return rows;
}

/** Serialize mapping rows to the `mappings` JSON-array parameter value. */
export function serializeMetadataMappingRows(rows: MetadataMappingRow[]): string {
  return JSON.stringify(rows.map(({ path, key }) => ({ path, key })));
}

/** The FormField errorText for a list of metadataConfig errors. */
function metadataErrorText(errors: MetadataConfigError[]): string | undefined {
  if (errors.length === 0) {
    return undefined;
  }
  return errors.map((error) => error.message).join('; ');
}

/** The optional-marker FormField label used by every parameter field. */
function optionalLabel(descriptor: ParameterDescriptor) {
  const label = parameterLabel(descriptor);
  return descriptor.required ? (
    label
  ) : (
    <span>
      {label} <i>- optional</i>
    </span>
  );
}

interface MetadataFieldProps {
  /** The `mappings` or `static_json` parameter descriptor. */
  descriptor: ParameterDescriptor;
  /** The node's full parameters record. */
  parameters: Record<string, JsonValue>;
  /** Edits: the full updated parameters record. */
  onParametersChange: (parameters: Record<string, JsonValue>) => void;
}

/**
 * The mapping rows editor for the metadata node's `mappings` parameter
 * (Requirement 6.2): add/edit/remove up to MAX_MAPPINGS rows of
 * *trigger payload field path* → *output metadata key*, each edit
 * serializing to the JSON-array parameter value. Validation errors from
 * the shared `metadataConfig` rules (empty paths/keys, duplicate keys,
 * row limit — Requirement 6.7) surface as the field error and, through
 * the node's parameters, as the canvas V10 inline marker
 * (`inlineChecks.ts`) that blocks the configuration from validating.
 * A `mappings` value that is not parseable as an array of `{path, key}`
 * string pairs (e.g. from a generated definition) renders as a raw JSON
 * textarea with the parse error so the text stays repairable
 * (Requirement 6.3).
 */
function MetadataMappingsField({ descriptor, parameters, onParametersChange }: MetadataFieldProps) {
  const raw = effectiveParameterValue(parameters, descriptor);
  const [, errors] = parseMappings(raw);
  const errorText = metadataErrorText(errors);
  const rows = metadataMappingRows(raw);

  const commit = (value: JsonValue | null) =>
    onParametersChange({ ...parameters, [descriptor.name]: value });

  // Unparseable/mis-shaped mappings: raw JSON textarea fallback.
  if (rows === null || errors.some((error) => error.code === ERROR_MAPPINGS_INVALID)) {
    return (
      <FormField
        label={optionalLabel(descriptor)}
        description={descriptor.description ?? undefined}
        errorText={errorText}
        stretch
      >
        <Textarea
          rows={4}
          value={textValue(raw)}
          onChange={({ detail }) => commit(detail.value)}
          spellcheck={false}
          placeholder='[{"path": "job_id", "key": "job_id"}]'
          ariaLabel={descriptor.name}
        />
        <ExampleChips descriptor={descriptor} onChange={commit} />
      </FormField>
    );
  }

  const commitRows = (next: MetadataMappingRow[]) => commit(serializeMetadataMappingRows(next));

  const updateRow = (index: number, patch: Partial<MetadataMappingRow>) =>
    commitRows(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  const removeRow = (index: number) => commitRows(rows.filter((_, i) => i !== index));

  const addRow = () => commitRows([...rows, { path: '', key: '' }]);

  return (
    <FormField
      label={optionalLabel(descriptor)}
      description={descriptor.description ?? undefined}
      errorText={errorText}
      stretch
    >
      <SpaceBetween size="xxs">
        {rows.length === 0 && (
          <Box fontSize="body-s" color="text-body-secondary">
            No mappings configured.
          </Box>
        )}
        {rows.map((row, index) => (
          <div
            // Rows are positional (no stable identity beyond index).
            // eslint-disable-next-line react/no-array-index-key
            key={index}
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <Input
                value={row.path}
                onChange={({ detail }) => updateRow(index, { path: detail.value })}
                placeholder="field path (e.g. job_id)"
                ariaLabel={`Mapping ${index + 1} field path`}
              />
            </div>
            <span aria-hidden="true" style={{ color: '#5f6b7a' }}>
              {'\u2192'}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Input
                value={row.key}
                onChange={({ detail }) => updateRow(index, { key: detail.value })}
                placeholder="output key"
                ariaLabel={`Mapping ${index + 1} output key`}
              />
            </div>
            <Button
              iconName="remove"
              variant="icon"
              ariaLabel={`Remove mapping ${index + 1}`}
              onClick={() => removeRow(index)}
            />
          </div>
        ))}
        <Button
          iconName="add-plus"
          disabled={rows.length >= MAX_MAPPINGS}
          onClick={addRow}
          ariaLabel="Add mapping"
        >
          Add mapping
        </Button>
        {rows.length >= MAX_MAPPINGS && (
          <Box fontSize="body-s" color="text-body-secondary">
            {`At most ${MAX_MAPPINGS} mappings are allowed.`}
          </Box>
        )}
      </SpaceBetween>
    </FormField>
  );
}

/**
 * The static JSON textarea for the metadata node's `static_json`
 * parameter (Requirement 6.2). Unparseable or non-object JSON and
 * over-length values surface the shared `metadataConfig` error as the
 * field error (Requirement 6.3) and, through the node's parameters, as
 * the canvas V10 inline marker that blocks the configuration from
 * validating.
 */
function MetadataStaticJsonField({
  descriptor,
  parameters,
  onParametersChange,
}: MetadataFieldProps) {
  const raw = effectiveParameterValue(parameters, descriptor);
  const [, errors] = parseStaticJson(raw);
  const commit = (value: JsonValue | null) =>
    onParametersChange({ ...parameters, [descriptor.name]: value });
  return (
    <FormField
      label={optionalLabel(descriptor)}
      description={descriptor.description ?? undefined}
      errorText={metadataErrorText(errors)}
      stretch
    >
      <Textarea
        rows={5}
        value={textValue(raw)}
        onChange={({ detail }) => commit(detail.value)}
        spellcheck={false}
        placeholder='{"station": "line-1"}'
        ariaLabel={descriptor.name}
      />
      <ExampleChips descriptor={descriptor} onChange={commit} />
    </FormField>
  );
}

// --------------------------------------------------------------------------
// Panel
// --------------------------------------------------------------------------

export interface NodeConfigPanelProps {
  /** The selected canvas node, or null when nothing is selected. */
  node: BuilderNode | null;
  /** Called with the node id and its full updated parameters record. */
  onParametersChange: (nodeId: string, parameters: Record<string, JsonValue>) => void;
  /**
   * Applies a camera reference selection (camera-registry-sync
   * Requirement 7.2): the node id, its full updated parameters record,
   * and the advisory binding hint to store as `data.cameraBindingHint`.
   * When absent, camera selections update parameters only.
   */
  onCameraSelection?: (
    nodeId: string,
    parameters: Record<string, JsonValue>,
    hint: CameraBindingHint
  ) => void;
  /** Closes the panel (deselects the node); omits the close button when absent. */
  onClose?: () => void;
  /**
   * The acting user's role, gating the Code_Assistant panel
   * (custom-node-code-assist Requirements 6.1, 6.5): the assistant
   * renders only when `canEditWorkflows(role)`; Viewer/Operator (or an
   * absent role) see no assistant entry point.
   */
  role?: UserRole | null;
}

export default function NodeConfigPanel({
  node,
  onParametersChange,
  onCameraSelection,
  onClose,
  role,
}: NodeConfigPanelProps) {
  const needsModels =
    node?.data.descriptor.parameters.some((parameter) => parameter.paramType === 'model_ref') ??
    false;
  // The options are filtered per node type (vllm-triton-inference
  // Requirements 6.2, 8.3): llm_inference lists only vLLM records,
  // every other model_ref consumer excludes them.
  const modelOptions = useModelOptions(needsModels && node ? node.data.descriptor.typeId : null);
  // Served catalog for unified_input source-parameter gating (Requirement
  // 5.3): fetched only while a unified_input node is selected so its
  // underlying source descriptor's parameter names can drive visibility.
  const isUnifiedInput = node?.data.descriptor.typeId === UNIFIED_INPUT_TYPE_ID;
  const nodeCatalog = useNodeCatalog(isUnifiedInput);
  const { selectedUsecaseId } = useUsecase();

  // Debounced Import_Analyzer on the effective `code` value of the
  // custom Python node types (custom-node-code-assist Requirements
  // 3.1, 3.5, 3.10); a no-op for every other node type.
  useImportAnalysis(node, onParametersChange);

  if (node === null) {
    return null;
  }

  const { descriptor, parameters } = node.data;

  // Code_Assistant below the `code` editor of the custom Python node
  // types (custom-node-code-assist Requirements 1.1, 1.2), gated to
  // workflow-editing roles (6.1, 6.5).
  const codeAssistContract: CodeAssistContract | undefined =
    CODE_ASSIST_CONTRACTS[descriptor.typeId];

  // For a unified_input node the visible parameters are source_kind plus
  // exactly the served descriptor's parameters for the source type its
  // source_kind expands to (Requirement 5.3); null for every other node
  // type, leaving their dependsOn-based visibility unchanged.
  const unifiedVisibleNames = unifiedVisibleParameterNames(node, nodeCatalog);

  return (
    <aside
      aria-label="Node configuration"
      style={{
        width: 320,
        flexShrink: 0,
        overflowY: 'auto',
        padding: 12,
        background: '#fafafa',
        borderLeft: '1px solid #d1d5db',
      }}
    >
      <SpaceBetween size="m">
        {/* Compact panel header: small bold title (not a heading element)
            with the node identity below and a close button. */}
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 4,
          }}
        >
          <div>
            <Box fontSize="body-m" fontWeight="bold">
              {descriptor.displayName}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              {`${node.id} (${descriptor.typeId})`}
            </Box>
          </div>
          {onClose !== undefined && (
            <Button
              iconName="close"
              variant="icon"
              ariaLabel="Close node configuration"
              onClick={onClose}
            />
          )}
        </div>
        {descriptor.parameters.length === 0 ? (
          <Box color="text-body-secondary">This node has no configurable parameters.</Box>
        ) : (
          descriptor.parameters
            .filter(
              (parameter) =>
                isParameterVisible(parameter, descriptor.parameters, parameters) &&
                (unifiedVisibleNames === null || unifiedVisibleNames.has(parameter.name))
            )
            .map((parameter) =>
              descriptor.typeId === METADATA_TYPE_ID &&
              parameter.name === MAPPINGS_PARAMETER ? (
                // The metadata node's mapping rows editor
                // (workflow-manager-gaps Requirement 6.2), the
                // custom_python/unified_input type-specific pattern;
                // keyed by node id so switching nodes resets its state.
                <MetadataMappingsField
                  key={`${node.id}:${parameter.name}`}
                  descriptor={parameter}
                  parameters={parameters}
                  onParametersChange={(updated) => onParametersChange(node.id, updated)}
                />
              ) : descriptor.typeId === METADATA_TYPE_ID &&
                parameter.name === STATIC_JSON_PARAMETER ? (
                // The metadata node's static JSON textarea with the
                // shared metadataConfig validation (Requirements 6.2, 6.3).
                <MetadataStaticJsonField
                  key={`${node.id}:${parameter.name}`}
                  descriptor={parameter}
                  parameters={parameters}
                  onParametersChange={(updated) => onParametersChange(node.id, updated)}
                />
              ) : isCameraReferenceParameter(descriptor.typeId, parameter.name) ? (
                // The icam_source device parameter and the
                // aravis_camera_source camera_id parameter render as the
                // camera reference control (camera-registry-sync
                // Requirement 7.1, aravis-camera-input Requirement 3.1,
                // csi-icam-input-nodes Requirement 5.2); keyed by node id
                // so switching nodes resets its state.
                <CameraReferenceField
                  key={`${node.id}:${parameter.name}`}
                  typeId={descriptor.typeId}
                  descriptor={parameter}
                  parameters={parameters}
                  hint={getCameraBindingHint(node.data.advisoryData)}
                  onParametersChange={(updated) => onParametersChange(node.id, updated)}
                  onCameraSelection={(updated, hint) =>
                    onCameraSelection !== undefined
                      ? onCameraSelection(node.id, updated, hint)
                      : onParametersChange(node.id, updated)
                  }
                  modelOptions={modelOptions}
                />
              ) : (
                <SpaceBetween key={parameter.name} size="s">
                  <ParameterField
                    typeId={descriptor.typeId}
                    descriptor={parameter}
                    value={effectiveParameterValue(parameters, parameter)}
                    onChange={(value) =>
                      onParametersChange(node.id, { ...parameters, [parameter.name]: value })
                    }
                    modelOptions={modelOptions}
                  />
                  {codeAssistContract !== undefined &&
                    parameter.name === REQUIREMENTS_PARAMETER && (
                      // Read-only badge annotations for derived and
                      // needs-review entries under the editable
                      // requirements Textarea (custom-node-code-assist
                      // Requirements 3.6, 3.7).
                      <RequirementsAnnotations
                        text={textValue(effectiveParameterValue(parameters, parameter))}
                      />
                    )}
                  {codeAssistContract !== undefined &&
                    parameter.name === 'code' &&
                    canEditWorkflows(role) && (
                      // The assistant writes accepted code through the
                      // exact channel manual edits use — the node's
                      // parameters via onParametersChange — so canvas
                      // markers, validation, and save behavior are
                      // untouched, and nothing is persisted by the
                      // panel itself (custom-node-code-assist
                      // Requirements 2.5, 2.7).
                      <CodeAssistPanel
                        usecaseId={selectedUsecaseId}
                        surface="workflow-builder"
                        contract={codeAssistContract}
                        context={{ nodeType: descriptor.typeId }}
                        editorCode={textValue(effectiveParameterValue(parameters, parameter))}
                        onAccept={(code) =>
                          onParametersChange(node.id, { ...parameters, [parameter.name]: code })
                        }
                      />
                    )}
                </SpaceBetween>
              )
            )
        )}
      </SpaceBetween>
    </aside>
  );
}
