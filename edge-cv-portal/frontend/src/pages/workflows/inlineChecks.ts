/**
 * Lightweight TypeScript mirror of Workflow_Validator checks V4 and V5
 * (Requirements 1.9, 4.4, 4.5), run on every canvas graph mutation for
 * inline validation markers. Full validation (all checks) is performed
 * by the backend via `workflow_core.validator.checks` — the source of
 * truth these mirrors must stay in sync with.
 *
 * | Check | Rule                                                   |
 * |-------|--------------------------------------------------------|
 * | V4    | required parameters have values satisfying constraints |
 * | V5    | every node reachable from some input node (forward BFS)|
 * | V7    | no connection targets a trigger node (stage ordering)  |
 * | V7-co | frame-feed source coexistence (one fed source per run) |
 * | V8    | mqtt_subscribe declares a connection target            |
 * | V9    | one activation model per workflow (mixed-model rule)   |
 * | V10   | metadata node mappings/static_json configuration valid |
 * | V11   | a stream node's url is a Stream_URL for its type       |
 * | V12   | a continuous stream node has no trigger activation     |
 * | V13   | scene analytics zone / label parameters parse          |
 * | W3    | a counter / association node has a detector upstream   |
 */

import {
  MAX_CLASS_LIST_ITEMS,
  MAX_REQUIRED_CLASSES,
  parseLabelList,
  parseZone,
} from './sceneConfig';
import { checkStreamUrl, schemesForNodeType, STREAM_SOURCE_TYPES } from './streamUrl';
import {
  ERROR_DUPLICATE_KEY,
  ERROR_EMPTY_FIELD_PATH,
  ERROR_EMPTY_KEY,
  ERROR_MAPPINGS_INVALID,
  ERROR_STATIC_JSON_INVALID,
  ERROR_TOO_MANY_MAPPINGS,
  parseMappings,
  parseStaticJson,
} from './metadataConfig';
import { checkParameterValue, VIOLATION_REQUIRED } from './parameters';
import {
  CATEGORY_INPUT,
  CATEGORY_TRIGGER,
  PORT_TYPES,
  SEVERITY_ERROR,
  SEVERITY_WARNING,
  SOURCE_KIND_TO_SOURCE_TYPE,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type ValidationFinding,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Finding codes (stable identifiers shared with the Python validator)
// --------------------------------------------------------------------------

export const CODE_V4_MISSING_REQUIRED_PARAMETER = 'V4_MISSING_REQUIRED_PARAMETER';
export const CODE_V4_INVALID_PARAMETER_VALUE = 'V4_INVALID_PARAMETER_VALUE';
export const CODE_V5_UNREACHABLE_NODE = 'V5_UNREACHABLE_NODE';
export const CODE_V7_STAGE_ORDER = 'V7_STAGE_ORDER';
export const CODE_V7_COEXISTENCE_CONFLICT = 'V7_COEXISTENCE_CONFLICT';
export const CODE_V8_MQTT_SUB_NO_TARGET = 'V8_MQTT_SUB_NO_TARGET';
export const CODE_V9_MIXED_ACTIVATION_MODEL = 'V9_MIXED_ACTIVATION_MODEL';
export const CODE_V10_METADATA_MAPPINGS_INVALID = 'V10_METADATA_MAPPINGS_INVALID';
export const CODE_V10_METADATA_EMPTY_FIELD_PATH = 'V10_METADATA_EMPTY_FIELD_PATH';
export const CODE_V10_METADATA_EMPTY_KEY = 'V10_METADATA_EMPTY_KEY';
export const CODE_V10_METADATA_DUPLICATE_KEY = 'V10_METADATA_DUPLICATE_KEY';
export const CODE_V10_METADATA_TOO_MANY_MAPPINGS = 'V10_METADATA_TOO_MANY_MAPPINGS';
export const CODE_V10_METADATA_STATIC_JSON_INVALID = 'V10_METADATA_STATIC_JSON_INVALID';
export const CODE_V11_STREAM_URL = 'V11_STREAM_URL';
export const CODE_V12_CONTINUOUS_ACTIVATION = 'V12_CONTINUOUS_ACTIVATION';
export const CODE_V13_ANALYTICS_CONFIG_INVALID = 'V13_ANALYTICS_CONFIG_INVALID';
export const CODE_W3_ANALYTICS_NO_DETECTOR = 'W3_ANALYTICS_NO_DETECTOR';

/** The graph slice the inline checks operate on. */
export interface GraphLike {
  nodes: WorkflowNode[];
  connections: WorkflowConnection[];
}

/**
 * Map node id -> catalog descriptor for nodes whose type is known;
 * nodes with unknown types are excluded (they cannot be checked for
 * parameters and never count as input roots).
 */
function typedNodes(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): Map<string, NodeTypeDescriptor> {
  const descriptors = new Map(catalog.map((d) => [d.typeId, d]));
  const result = new Map<string, NodeTypeDescriptor>();
  for (const node of graph.nodes) {
    const descriptor = descriptors.get(node.type);
    if (descriptor !== undefined) {
      result.set(node.id, descriptor);
    }
  }
  return result;
}

/**
 * Resolve a node's input and output port types as `{portName: portType}`
 * maps, mirroring `_resolved_ports` in the Python validator.
 *
 * Custom Python nodes declare their input and output port types per node
 * instance (Requirement 2.7): when the descriptor exposes
 * `input_port_type` / `output_port_type` parameters and the node carries
 * a known port type value, that value overrides the declared default
 * type of the node's ports.
 */
export function resolvedPorts(
  node: WorkflowNode,
  descriptor: NodeTypeDescriptor
): { inputs: Record<string, string>; outputs: Record<string, string> } {
  const inputs: Record<string, string> = {};
  for (const port of descriptor.inputs) {
    inputs[port.name] = port.portType;
  }
  const outputs: Record<string, string> = {};
  for (const port of descriptor.outputs) {
    outputs[port.name] = port.portType;
  }

  const parameterNames = new Set(descriptor.parameters.map((p) => p.name));

  if (parameterNames.has('input_port_type')) {
    const override = node.parameters['input_port_type'];
    if (typeof override === 'string' && (PORT_TYPES as readonly string[]).includes(override)) {
      for (const name of Object.keys(inputs)) {
        inputs[name] = override;
      }
    }
  }
  if (parameterNames.has('output_port_type')) {
    const override = node.parameters['output_port_type'];
    if (typeof override === 'string' && (PORT_TYPES as readonly string[]).includes(override)) {
      for (const name of Object.keys(outputs)) {
        outputs[name] = override;
      }
    }
  }

  return { inputs, outputs };
}

/**
 * The value V4 validates: the explicitly set value when the key is
 * present (an explicit null counts as cleared), else the declared
 * default (a default is a value, so required parameters with defaults
 * are satisfied when omitted).
 */
function effectiveValue(
  node: WorkflowNode,
  parameter: ParameterDescriptor
): JsonValue | null | undefined {
  if (Object.prototype.hasOwnProperty.call(node.parameters, parameter.name)) {
    return node.parameters[parameter.name];
  }
  return parameter.default;
}

// --------------------------------------------------------------------------
// V4: required parameters satisfy constraints (Requirement 4.4)
// --------------------------------------------------------------------------

/** Mirror of validator check V4 (missing/invalid required parameters). */
export function checkV4(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const node of graph.nodes) {
    const descriptor = typed.get(node.id);
    if (descriptor === undefined) {
      continue;
    }
    for (const parameter of descriptor.parameters) {
      const violation = checkParameterValue(parameter, effectiveValue(node, parameter));
      if (violation === null) {
        continue;
      }
      const code =
        violation.code === VIOLATION_REQUIRED
          ? CODE_V4_MISSING_REQUIRED_PARAMETER
          : CODE_V4_INVALID_PARAMETER_VALUE;
      findings.push({
        severity: SEVERITY_ERROR,
        code,
        message: `Node '${node.id}': ${violation.message}`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V5: reachability from input nodes via forward BFS (Requirement 4.5)
// --------------------------------------------------------------------------

/** Mirror of validator check V5 (nodes unreachable from any input node). */
export function checkV5(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const known = new Set(graph.nodes.map((node) => node.id));

  const successors = new Map<string, string[]>();
  for (const id of known) {
    successors.set(id, []);
  }
  for (const connection of graph.connections) {
    const source = connection.from.node;
    const target = connection.to.node;
    if (known.has(source) && known.has(target)) {
      successors.get(source)!.push(target);
    }
  }

  const roots = graph.nodes
    .filter((node) => {
      const category = typed.get(node.id)?.category;
      return category === CATEGORY_INPUT || category === CATEGORY_TRIGGER;
    })
    .map((node) => node.id);

  const visited = new Set(roots);
  const frontier = [...roots];
  while (frontier.length > 0) {
    const current = frontier.pop()!;
    for (const child of successors.get(current) ?? []) {
      if (!visited.has(child)) {
        visited.add(child);
        frontier.push(child);
      }
    }
  }

  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    if (!visited.has(node.id)) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V5_UNREACHABLE_NODE,
        message: `Node '${node.id}' is not reachable from any input node`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V7: trigger stage ordering (Requirement 5.5, mirrors backend V7)
// --------------------------------------------------------------------------

/**
 * Mirror of validator check V7 (illegal stage ordering). A
 * `CATEGORY_TRIGGER` node has no input ports and may only feed an Input's
 * activation port, so any connection whose target resolves to a trigger
 * node is an illegal ordering — the only way to place a trigger
 * downstream of another node. The check is target-category based, which
 * also makes the legal `Trigger → Unified activation-port` case pass
 * automatically (its target is the `CATEGORY_INPUT` unified node).
 */
export function checkV7(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const connection of graph.connections) {
    const target = typed.get(connection.to.node);
    if (target !== undefined && target.category === CATEGORY_TRIGGER) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V7_STAGE_ORDER,
        message:
          `Connection '${connection.id}' targets trigger node ` +
          `'${connection.to.node}': a trigger may not be downstream of any ` +
          `node (Trigger -> Input ordering)`,
        nodeId: null,
        connectionId: connection.id,
      });
    }
  }
  return findings;
}

/**
 * `map[key]` when `key` is an own property of `map`, else undefined: the
 * backend's `dict.get(key)`. Node types and parameter values come from
 * the graph, which is external input, so a type named `constructor` or
 * `__proto__` must miss rather than resolve an Object.prototype member.
 */
function ownEntry<T>(map: Readonly<Record<string, T>>, key: unknown): T | undefined {
  return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key)
    ? map[key]
    : undefined;
}

// --------------------------------------------------------------------------
// V7-coexistence: frame-feed source coexistence
// (custom-python-source Requirement 8.4, mirrors backend V7-coexistence)
// --------------------------------------------------------------------------

/**
 * Node types allowed at most once per workflow, mapped to the
 * plain-language reason — the TypeScript mirror of the backend's
 * `COEXISTENCE_SINGLETON_TYPES` table in
 * `workflow_core.validator.checks` (the source of truth; keep the
 * entries and reason strings in sync).
 */
const COEXISTENCE_SINGLETON_TYPES: Record<string, string> = {
  aravis_camera_source:
    'the single-frame appsrc feed supports exactly one Aravis camera source per workflow',
  custom_python_source:
    'the single-frame appsrc feed serves exactly one frame-feed source per workflow',
  // rtsp-rtmp-stream-cameras Requirement 2.4: both stream types feed the
  // runtime through the same single-frame appsrc Frame_Feed.
  rtsp_camera_source:
    'the single-frame appsrc feed serves exactly one frame-feed source per workflow',
  rtmp_stream_source:
    'the single-frame appsrc feed serves exactly one frame-feed source per workflow',
};

/**
 * Node types that all bind the runtime's single frame feed; at most one
 * node across the whole group may exist per workflow (custom-python-source
 * Requirements 8.1, 8.2; rtsp-rtmp-stream-cameras Requirement 2.4).
 * Mirror of the backend's `FRAME_FEED_SOURCE_TYPES`.
 */
const FRAME_FEED_SOURCE_TYPES = new Set([
  'aravis_camera_source',
  'custom_python_source',
  'rtsp_camera_source',
  'rtmp_stream_source',
]);

/**
 * Mirror of the backend `_check_v7_coexistence` frame-feed rule
 * (custom-python-source Requirement 8.4, rtsp-rtmp-stream-cameras
 * Requirement 2.4): when a workflow contains TWO OR MORE of the
 * frame-feed source types, every frame-feed node gets one error finding
 * naming the full conflicting membership across those types and stating
 * that the runtime serves one frame-feed source per workflow; when two
 * or more nodes of one singleton type are present (and the mixed rule
 * did not already report them), every one of them gets an error finding
 * naming the full same-type membership. Like the backend, the check
 * keys on `node.type` directly (not the catalog), and each offending
 * node is reported exactly once.
 */
export function checkV7Coexistence(graph: GraphLike): ValidationFinding[] {
  const findings: ValidationFinding[] = [];
  const byType = new Map<string, string[]>();
  for (const node of graph.nodes) {
    // Own keys only, like the backend's dict membership: two nodes typed
    // 'constructor' are not a singleton conflict.
    if (ownEntry(COEXISTENCE_SINGLETON_TYPES, node.type) !== undefined) {
      const ids = byType.get(node.type) ?? [];
      ids.push(node.id);
      byType.set(node.type, ids);
    }
  }

  // Two or more distinct frame-feed types present (with the two
  // pre-feature members this was "both present", so every pre-feature
  // graph keeps its findings).
  const frameFeedTypesPresent = [...FRAME_FEED_SOURCE_TYPES].filter((type) => byType.has(type));
  const mixedFrameFeed = frameFeedTypesPresent.length >= 2;
  if (mixedFrameFeed) {
    const memberIds = frameFeedTypesPresent
      .flatMap((type) => byType.get(type) ?? [])
      .sort();
    const members = memberIds.map((id) => `'${id}'`).join(', ');
    for (const nodeId of memberIds) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V7_COEXISTENCE_CONFLICT,
        message:
          `Node '${nodeId}': frame-feed source nodes (${members}) cannot ` +
          `coexist in one workflow: the runtime serves one frame-feed ` +
          `source per workflow`,
        nodeId,
        connectionId: null,
      });
    }
  }

  for (const [nodeType, nodeIds] of [...byType.entries()].sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0
  )) {
    if (mixedFrameFeed && FRAME_FEED_SOURCE_TYPES.has(nodeType)) {
      // Already reported by the mixed frame-feed rule above; a
      // singleton finding here would double-report these nodes.
      continue;
    }
    if (nodeIds.length < 2) {
      continue;
    }
    const reason = COEXISTENCE_SINGLETON_TYPES[nodeType];
    const sortedIds = [...nodeIds].sort();
    const members = sortedIds.map((id) => `'${id}'`).join(', ');
    for (const nodeId of sortedIds) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V7_COEXISTENCE_CONFLICT,
        message:
          `Node '${nodeId}': ${nodeIds.length} nodes of type '${nodeType}' ` +
          `cannot coexist in one workflow (${members}): ${reason}`,
        nodeId,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V8: mqtt_subscribe must declare a connection target
// (trigger-activation-runtime Requirement 4.5, mirrors backend V8)
// --------------------------------------------------------------------------

/**
 * Trigger node type that subscribes over MQTT; V8 requires it to declare
 * a connection target (Greengrass, AWS IoT Core, or a plain broker host).
 */
const TYPE_MQTT_SUBSCRIBE = 'mqtt_subscribe';

/** Trigger node type that subscribes to an OPC UA monitored node. */
const TYPE_OPCUA_SUBSCRIBE = 'opcua_subscribe';

/**
 * The subscription trigger node types whose presence engages the V9
 * mixed-activation-model rule (`digital_input` is deliberately absent:
 * its activation behavior is unchanged by trigger-activation-runtime).
 */
const SUBSCRIPTION_TRIGGER_TYPES = new Set([TYPE_MQTT_SUBSCRIBE, TYPE_OPCUA_SUBSCRIBE]);

/**
 * The activation input port name on CATEGORY_INPUT nodes (the unified
 * input node and the four legacy sources all declare it).
 */
const ACTIVATION_PORT = 'activation';

/**
 * Mirror of validator check V8: every `mqtt_subscribe` node must name a
 * connection target — an error when the node enables neither the
 * Greengrass path (`greengrass`) nor the AWS IoT Core path (`aws_iot`)
 * and supplies no non-empty `broker_host` (effective values: explicit,
 * else declared default). Kept as a code separate from V6 so V6's
 * publish-specific behavior is untouched.
 */
export function checkV8(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const node of graph.nodes) {
    if (node.type !== TYPE_MQTT_SUBSCRIBE) {
      continue;
    }
    const descriptor = typed.get(node.id);
    if (descriptor === undefined) {
      continue;
    }
    const values = new Map<string, JsonValue | null | undefined>(
      descriptor.parameters.map((parameter) => [parameter.name, effectiveValue(node, parameter)])
    );
    const greengrass = Boolean(values.get('greengrass'));
    const awsIot = Boolean(values.get('aws_iot'));
    const brokerHost = values.get('broker_host');
    const hasBrokerHost = typeof brokerHost === 'string' && brokerHost.trim() !== '';
    if (!(greengrass || awsIot || hasBrokerHost)) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V8_MQTT_SUB_NO_TARGET,
        message:
          `Node '${node.id}': mqtt_subscribe requires a connection target — ` +
          `enable 'greengrass', enable 'aws_iot', or set 'broker_host'`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V9: one activation model per workflow
// (trigger-activation-runtime Requirement 4.5, mirrors backend V9)
// --------------------------------------------------------------------------

/**
 * Mirror of validator check V9: a workflow has exactly one activation
 * model — when the graph contains at least one subscription trigger node
 * (`mqtt_subscribe` or `opcua_subscribe`), every `CATEGORY_INPUT` node
 * must have at least one connection targeting its `activation` input
 * port; one error finding per unconnected input node. Graphs with zero
 * subscription trigger nodes produce zero V9 findings (`digital_input`
 * presence alone does not engage V9: its activation behavior is
 * unchanged).
 */
export function checkV9(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const hasSubscriptionTrigger = graph.nodes.some((node) =>
    SUBSCRIPTION_TRIGGER_TYPES.has(node.type)
  );
  if (!hasSubscriptionTrigger) {
    return [];
  }

  const activationConnected = new Set(
    graph.connections
      .filter((connection) => connection.to.port === ACTIVATION_PORT)
      .map((connection) => connection.to.node)
  );

  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    const descriptor = typed.get(node.id);
    if (descriptor === undefined || descriptor.category !== CATEGORY_INPUT) {
      continue;
    }
    if (isContinuousStreamNode(node, descriptor)) {
      // Reported by V12 instead (rtsp-rtmp-stream-cameras Requirement 2.3).
      continue;
    }
    if (!activationConnected.has(node.id)) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V9_MIXED_ACTIVATION_MODEL,
        message:
          `Input node '${node.id}' has no trigger connected to its ` +
          `'activation' port: a workflow with subscription triggers must ` +
          `drive every input from a trigger`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V10: Metadata_Node configuration validity
// (workflow-manager-gaps Requirements 6.3, 6.7, mirrors backend V10)
// --------------------------------------------------------------------------

/** The node type carrying the `mappings`/`static_json` parameters. */
export const TYPE_METADATA = 'metadata';

/**
 * Shared metadata_config error class -> V10 finding code, the
 * TypeScript mirror of the backend's `_METADATA_ERROR_CODES` table in
 * `workflow_core.validator.checks` (keep in sync).
 */
const METADATA_ERROR_CODES: Record<string, string> = {
  [ERROR_MAPPINGS_INVALID]: CODE_V10_METADATA_MAPPINGS_INVALID,
  [ERROR_EMPTY_FIELD_PATH]: CODE_V10_METADATA_EMPTY_FIELD_PATH,
  [ERROR_EMPTY_KEY]: CODE_V10_METADATA_EMPTY_KEY,
  [ERROR_DUPLICATE_KEY]: CODE_V10_METADATA_DUPLICATE_KEY,
  [ERROR_TOO_MANY_MAPPINGS]: CODE_V10_METADATA_TOO_MANY_MAPPINGS,
  [ERROR_STATIC_JSON_INVALID]: CODE_V10_METADATA_STATIC_JSON_INVALID,
};

/**
 * Mirror of validator check V10 (`_check_v10_metadata`): every
 * `metadata`-typed node's `mappings` and `static_json` parameters are
 * parsed through the shared `metadataConfig` helpers, and each reported
 * error class becomes one SEVERITY_ERROR finding under its dedicated
 * V10 code (workflow-manager-gaps Requirements 6.3, 6.7). A valid
 * configuration produces zero findings, and the check fires only on
 * `metadata`-typed nodes so metadata-free graphs produce identical
 * findings (Requirement 8.1). Like the backend, the raw parameter
 * values are parsed (absent values fall back to the descriptor
 * defaults inside the shared parsers).
 */
export function checkV10Metadata(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const node of graph.nodes) {
    if (node.type !== TYPE_METADATA) {
      continue;
    }
    const descriptor = typed.get(node.id);
    if (descriptor === undefined) {
      continue;
    }
    const [, mappingErrors] = parseMappings(node.parameters['mappings']);
    const [, staticErrors] = parseStaticJson(node.parameters['static_json']);
    for (const configError of [...mappingErrors, ...staticErrors]) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: METADATA_ERROR_CODES[configError.code] ?? CODE_V10_METADATA_MAPPINGS_INVALID,
        message: `Node '${node.id}': ${configError.message}`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// Stream camera and scene analytics rules (rtsp-rtmp-stream-cameras
// Requirements 2.1-2.6, 13.7, 13.8, 14.6; mirrors backend V11, V12, V13,
// W3 and the V9 skip)
// --------------------------------------------------------------------------

/** The unified input node type and its effective-type parameter. */
const TYPE_UNIFIED_INPUT = 'unified_input';
const SOURCE_KIND_PARAMETER = 'source_kind';

/** The stream node parameters the rules read. */
const PROCESSING_MODE_PARAMETER = 'processing_mode';
const PROCESSING_MODE_CONTINUOUS = 'continuous';
const STREAM_URL_PARAMETER = 'url';

/** The inference node type whose presence populates the Detection_List. */
const TYPE_MODEL_INFERENCE = 'model_inference';

/** Scene_Analytics_Node types that consume a Detection_List (W3). */
const DETECTION_CONSUMER_TYPES = new Set(['detection_counter', 'object_association']);

/**
 * Scene_Analytics_Node type -> the label parameters V13 parses and the
 * item cap of each (backend `_ANALYTICS_LABEL_PARAMETERS`).
 */
const ANALYTICS_LABEL_PARAMETERS: Record<string, Record<string, number>> = {
  detection_counter: { classes: MAX_CLASS_LIST_ITEMS },
  object_association: { subject_class: 1, required_classes: MAX_REQUIRED_CLASSES },
};

/** Scene_Analytics_Node type -> its Zone parameters. */
const ANALYTICS_ZONE_PARAMETERS: Record<string, string[]> = {
  detection_counter: ['zone'],
  object_association: ['zone'],
};

/**
 * The type the stream rules evaluate `node` as: a `unified_input` node
 * through its explicitly set `source_kind` (backend
 * `_effective_node_type`), every other node as its own type.
 */
export function effectiveNodeType(node: WorkflowNode): string {
  if (node.type !== TYPE_UNIFIED_INPUT) {
    return node.type;
  }
  // Own-property lookup, like the backend's dict.get: a source_kind such
  // as 'constructor' falls back to the node's own type.
  return (
    ownEntry(
      SOURCE_KIND_TO_SOURCE_TYPE as Record<string, string>,
      node.parameters[SOURCE_KIND_PARAMETER]
    ) ?? node.type
  );
}

/**
 * The effective value of parameter `name` (backend `_parameter_value`):
 * through the declared default when the descriptor declares it, else the
 * raw parameter map.
 */
function parameterValue(
  node: WorkflowNode,
  descriptor: NodeTypeDescriptor | undefined,
  name: string
): JsonValue | null | undefined {
  const parameter = descriptor?.parameters.find((p) => p.name === name);
  if (parameter !== undefined) {
    return effectiveValue(node, parameter);
  }
  return node.parameters[name];
}

/** Whether `node` is a Stream_Camera_Source_Node in continuous mode. */
function isContinuousStreamNode(
  node: WorkflowNode,
  descriptor: NodeTypeDescriptor | undefined
): boolean {
  if (!STREAM_SOURCE_TYPES.has(effectiveNodeType(node))) {
    return false;
  }
  const mode = parameterValue(node, descriptor, PROCESSING_MODE_PARAMETER);
  if (mode === null || mode === undefined) {
    return true; // the descriptor default
  }
  return mode === PROCESSING_MODE_CONTINUOUS;
}

/**
 * Mirror of validator check V11: every Stream_Camera_Source_Node's `url`
 * (unified nodes through their `source_kind`) must be a Stream_URL for
 * its node type, judged by the same `checkStreamUrl` port.
 */
export function checkV11StreamUrl(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    const allowedSchemes = schemesForNodeType(effectiveNodeType(node));
    if (allowedSchemes === undefined) {
      continue;
    }
    const url = parameterValue(node, typed.get(node.id), STREAM_URL_PARAMETER);
    const problem = checkStreamUrl(url, allowedSchemes);
    if (problem === null) {
      continue;
    }
    findings.push({
      severity: SEVERITY_ERROR,
      code: CODE_V11_STREAM_URL,
      message: `Node '${node.id}': parameter '${STREAM_URL_PARAMETER}': ${problem.message}`,
      nodeId: node.id,
      connectionId: null,
    });
  }
  return findings;
}

/**
 * Mirror of validator check V12: a continuous stream node may neither
 * have a connection into its `activation` port nor share the workflow
 * with a subscription trigger; one finding per such node, naming every
 * reason.
 */
export function checkV12ContinuousActivation(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const activationConnected = new Set(
    graph.connections
      .filter((connection) => connection.to.port === ACTIVATION_PORT)
      .map((connection) => connection.to.node)
  );
  const subscriptionTriggerIds = graph.nodes
    .filter((node) => SUBSCRIPTION_TRIGGER_TYPES.has(node.type))
    .map((node) => node.id)
    .sort();

  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    if (!isContinuousStreamNode(node, typed.get(node.id))) {
      continue;
    }
    const reasons: string[] = [];
    if (activationConnected.has(node.id)) {
      reasons.push(`a connection into its '${ACTIVATION_PORT}' port`);
    }
    if (subscriptionTriggerIds.length > 0) {
      reasons.push(
        `the subscription trigger node(s) ${subscriptionTriggerIds
          .map((id) => `'${id}'`)
          .join(', ')} in the same workflow`
      );
    }
    if (reasons.length === 0) {
      continue;
    }
    findings.push({
      severity: SEVERITY_ERROR,
      code: CODE_V12_CONTINUOUS_ACTIVATION,
      message:
        `Node '${node.id}': continuous processing is its own activation model, ` +
        `but the workflow has ${reasons.join(' and ')}. Set ` +
        `'${PROCESSING_MODE_PARAMETER}' to 'on_trigger' to drive this node from ` +
        'a trigger, or remove the trigger',
      nodeId: node.id,
      connectionId: null,
    });
  }
  return findings;
}

/**
 * Mirror of validator check V13: each Scene_Analytics_Node label or Zone
 * parameter that fails to parse becomes one finding carrying every
 * problem; a blank value is not malformed.
 */
export function checkV13AnalyticsConfig(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];
  const finding = (nodeId: string, name: string, problems: string[]): ValidationFinding => ({
    severity: SEVERITY_ERROR,
    code: CODE_V13_ANALYTICS_CONFIG_INVALID,
    message: `Node '${nodeId}': parameter '${name}': ${problems.join(' ')}`,
    nodeId,
    connectionId: null,
  });
  for (const node of graph.nodes) {
    const labelParameters = ownEntry(ANALYTICS_LABEL_PARAMETERS, node.type) ?? {};
    const zoneParameters = ownEntry(ANALYTICS_ZONE_PARAMETERS, node.type) ?? [];
    const labelNames = Object.keys(labelParameters).sort();
    if (labelNames.length === 0 && zoneParameters.length === 0) {
      continue;
    }
    const descriptor = typed.get(node.id);
    for (const name of labelNames) {
      const [, problems] = parseLabelList(
        parameterValue(node, descriptor, name),
        labelParameters[name]
      );
      if (problems.length > 0) {
        findings.push(finding(node.id, name, problems));
      }
    }
    for (const name of zoneParameters) {
      const [, problems] = parseZone(parameterValue(node, descriptor, name));
      if (problems.length > 0) {
        findings.push(finding(node.id, name, problems));
      }
    }
  }
  return findings;
}

/**
 * Mirror of validator check W3: a `detection_counter` or
 * `object_association` node with no `model_inference` node transitively
 * upstream gets one warning. Keys on `node.type`, like V7-coexistence.
 */
export function checkW3AnalyticsNoDetector(graph: GraphLike): ValidationFinding[] {
  const known = new Set(graph.nodes.map((node) => node.id));
  const predecessors = new Map<string, string[]>();
  for (const id of known) {
    predecessors.set(id, []);
  }
  for (const connection of graph.connections) {
    if (known.has(connection.from.node) && known.has(connection.to.node)) {
      predecessors.get(connection.to.node)!.push(connection.from.node);
    }
  }
  const nodeTypes = new Map(graph.nodes.map((node) => [node.id, node.type]));

  const hasDetectorUpstream = (nodeId: string): boolean => {
    const visited = new Set([nodeId]);
    const frontier = [...(predecessors.get(nodeId) ?? [])];
    while (frontier.length > 0) {
      const current = frontier.pop()!;
      if (visited.has(current)) {
        continue;
      }
      visited.add(current);
      if (nodeTypes.get(current) === TYPE_MODEL_INFERENCE) {
        return true;
      }
      frontier.push(...(predecessors.get(current) ?? []));
    }
    return false;
  };

  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    if (!DETECTION_CONSUMER_TYPES.has(node.type) || hasDetectorUpstream(node.id)) {
      continue;
    }
    findings.push({
      severity: SEVERITY_WARNING,
      code: CODE_W3_ANALYTICS_NO_DETECTOR,
      message:
        `Node '${node.id}': no '${TYPE_MODEL_INFERENCE}' node is upstream of this ` +
        `'${node.type}' node, so no detections will exist for it to analyze at runtime`,
      nodeId: node.id,
      connectionId: null,
    });
  }
  return findings;
}

/**
 * Run the inline mirror checks (V4 + V5 + V7 + V7-coexistence + V8 + V9
 * + V10 + V11 + V12 + V13 + W3) and return every
 * finding, each with the associated node or connection identifier. The
 * canvas turns these into inline validation markers (Requirements 1.9,
 * 1.10, 5.5; trigger-activation-runtime Requirement 4.5;
 * workflow-manager-gaps Requirements 6.3, 6.7; rtsp-rtmp-stream-cameras
 * Requirement 2.6).
 */
export function runInlineChecks(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  return [
    ...checkV4(graph, catalog),
    ...checkV5(graph, catalog),
    ...checkV7(graph, catalog),
    ...checkV7Coexistence(graph),
    ...checkV8(graph, catalog),
    ...checkV9(graph, catalog),
    ...checkV10Metadata(graph, catalog),
    ...checkV11StreamUrl(graph, catalog),
    ...checkV12ContinuousActivation(graph, catalog),
    ...checkV13AnalyticsConfig(graph, catalog),
    ...checkW3AnalyticsNoDetector(graph),
  ];
}
