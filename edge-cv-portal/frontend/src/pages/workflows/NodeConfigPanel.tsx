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
 *   - `model_ref` (model_inference `modelName`): a Select populated from
 *     the model registry API filtered by the selected Use_Case
 *     (Requirement 2.6).
 *   - Custom_Python_Node: `code` renders a code editor textarea and the
 *     `input_port_type` / `output_port_type` parameters render port-type
 *     pickers over PORT_TYPES (Requirement 2.7); changes flow into
 *     `node.data.parameters`, so the canvas port handles update via
 *     `resolvedPorts`.
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

import { useEffect, useState } from 'react';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Textarea from '@cloudscape-design/components/textarea';
import { apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import type { Device } from '../../types';
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
  type CameraBindingHint,
  type CameraSourceEntry,
} from './cameraReference';
import { checkParameterValue } from './parameters';
import { PORT_TYPES, type JsonValue, type ParameterDescriptor } from './types';

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
 * `dependsOn` field): a parameter that depends on a bool parameter is
 * shown only while that parameter's effective value is true. Parameters
 * without `dependsOn` (or referencing an unknown parameter) are always
 * visible. Hidden parameters are optional by catalog convention, so
 * hiding them never suppresses a validation error.
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
  const controlling = allParameters.find((parameter) => parameter.name === dependsOn);
  if (controlling === undefined) {
    return true;
  }
  return effectiveParameterValue(parameters, controlling) === true;
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
// Model registry options for model_ref parameters (Requirement 2.6)
// --------------------------------------------------------------------------

/** Load state of the model_ref Select options. */
export interface ModelOptionsState {
  status: 'pending' | 'loading' | 'error' | 'finished';
  options: SelectProps.Option[];
  errorText?: string;
}

/**
 * Fetch the models registered for the selected Use_Case whenever the
 * panel shows a model_ref parameter (Requirement 2.6).
 */
function useModelOptions(needed: boolean): ModelOptionsState {
  const { selectedUsecaseId } = useUsecase();
  const [state, setState] = useState<ModelOptionsState>({ status: 'pending', options: [] });

  useEffect(() => {
    if (!needed) {
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
        const options = (response.models ?? []).map((model) => ({
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
  }, [needed, selectedUsecaseId]);

  return state;
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

function ParameterControl({ descriptor, value, onChange, modelOptions }: ParameterFieldProps) {
  const paramType = descriptor.paramType;

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
        empty="No models registered for this use case"
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
 * The camera reference control for the `camera_source` node's `device`
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
  // applyAravisCameraSelection. camera_source's path is untouched.
  const isAravis = typeId === 'aravis_camera_source';

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
  // 3.2); camera_source keeps the full registry list.
  const offeredCameras = isAravis
    ? cameras.items.filter(isAravisCompatibleCamera)
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
}

export default function NodeConfigPanel({
  node,
  onParametersChange,
  onCameraSelection,
  onClose,
}: NodeConfigPanelProps) {
  const needsModels =
    node?.data.descriptor.parameters.some((parameter) => parameter.paramType === 'model_ref') ??
    false;
  const modelOptions = useModelOptions(needsModels);

  if (node === null) {
    return null;
  }

  const { descriptor, parameters } = node.data;

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
            .filter((parameter) => isParameterVisible(parameter, descriptor.parameters, parameters))
            .map((parameter) =>
              isCameraReferenceParameter(descriptor.typeId, parameter.name) ? (
                // The camera_source device parameter and the
                // aravis_camera_source camera_id parameter render as the
                // camera reference control (camera-registry-sync
                // Requirement 7.1, aravis-camera-input Requirement 3.1);
                // keyed by node id so switching nodes resets its state.
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
                <ParameterField
                  key={parameter.name}
                  typeId={descriptor.typeId}
                  descriptor={parameter}
                  value={effectiveParameterValue(parameters, parameter)}
                  onChange={(value) =>
                    onParametersChange(node.id, { ...parameters, [parameter.name]: value })
                  }
                  modelOptions={modelOptions}
                />
              )
            )
        )}
      </SpaceBetween>
    </aside>
  );
}
