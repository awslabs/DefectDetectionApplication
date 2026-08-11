/**
 * TypeScript types for the Workflow Manager frontend.
 *
 * Two families of shapes live here:
 *
 * 1. The Workflow_Definition document (generated from the JSON Schema in
 *    `workflow_core.serializer.schema`, schemaVersion 1) — the graph
 *    document exchanged with the portal backend (Requirement 3.1).
 *
 * 2. The node catalog descriptor shapes served by
 *    `GET /workflows/node-catalog` in camelCase wire form, mirroring the
 *    Python dataclasses in `workflow_core.catalog.models`
 *    (Requirement 2.8).
 *
 * Keep these in sync with the Python source of truth in
 * `edge-cv-portal/backend/layers/workflow_core/`.
 */

// --------------------------------------------------------------------------
// Workflow_Definition document (JSON Schema, schemaVersion 1)
// --------------------------------------------------------------------------

/** Current Workflow_Definition schema version. */
export const SCHEMA_VERSION = 1;

/** Canvas position of a node. */
export interface NodePosition {
  x: number;
  y: number;
}

/** JSON value type for node parameter values. */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * A single processing stage: id, catalog type, canvas position, and
 * parameter values. Parameter keys and value types are declared by the
 * node type's catalog descriptor.
 */
export interface WorkflowNode {
  id: string;
  type: string;
  position: NodePosition;
  parameters: Record<string, JsonValue>;
  /**
   * Optional advisory node data (e.g. the `cameraBindingHint` recorded
   * by the Workflow_Builder camera picker, camera-registry-sync
   * Requirements 7.2, 7.5). Preserved through save/load round trips but
   * ignored by validation and compilation, so definitions carrying it
   * stay device-portable.
   */
  data?: Record<string, JsonValue>;
}

/** A typed port endpoint: a port name on a specific node. */
export interface PortEndpoint {
  node: string;
  port: string;
}

/** A directed edge from an output port ('from') to an input port ('to'). */
export interface WorkflowConnection {
  id: string;
  from: PortEndpoint;
  to: PortEndpoint;
}

/**
 * Serializable workflow graph document: all nodes with their
 * configurations and canvas positions, all connections, and a schema
 * version identifier (Requirement 3.1).
 */
export interface WorkflowDefinition {
  schemaVersion: typeof SCHEMA_VERSION;
  nodes: WorkflowNode[];
  connections: WorkflowConnection[];
}

// --------------------------------------------------------------------------
// Port types and node categories (workflow_core.catalog.models)
// --------------------------------------------------------------------------

export const PORT_TYPE_VIDEO_FRAMES = 'VideoFrames';
export const PORT_TYPE_INFERENCE_META = 'InferenceMeta';
export const PORT_TYPE_EVENT_SIGNAL = 'EventSignal';

export const PORT_TYPES = [
  PORT_TYPE_VIDEO_FRAMES,
  PORT_TYPE_INFERENCE_META,
  PORT_TYPE_EVENT_SIGNAL,
] as const;

export type PortType = (typeof PORT_TYPES)[number];

export const CATEGORY_TRIGGER = 'trigger';
export const CATEGORY_INPUT = 'input';
export const CATEGORY_PREPROCESSING = 'preprocessing';
export const CATEGORY_INFERENCE = 'inference';
export const CATEGORY_POST_PROCESSING = 'post_processing';
export const CATEGORY_OUTPUT = 'output';

export const CATEGORIES = [
  CATEGORY_TRIGGER,
  CATEGORY_INPUT,
  CATEGORY_PREPROCESSING,
  CATEGORY_INFERENCE,
  CATEGORY_POST_PROCESSING,
  CATEGORY_OUTPUT,
] as const;

export type NodeCategory = (typeof CATEGORIES)[number];

/**
 * Maps each `unified_input` `source_kind` enum value to the existing
 * source node type it expands to. Mirrors the Python catalog's
 * `SOURCE_KIND_TO_SOURCE_TYPE` (the shared source of truth); used by the
 * configuration panel to gate which source parameters are visible.
 */
export const SOURCE_KIND_TO_SOURCE_TYPE = {
  csi_camera: 'csi_camera_source',
  icam: 'icam_source',
  aravis_camera: 'aravis_camera_source',
  folder: 'folder_source',
} as const;

// --------------------------------------------------------------------------
// Target architectures
// --------------------------------------------------------------------------

export const ARCHITECTURES = [
  'x86_64',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'sim',
] as const;

export type Architecture = (typeof ARCHITECTURES)[number];

// --------------------------------------------------------------------------
// Parameter types
// --------------------------------------------------------------------------

export const PARAMETER_TYPES = [
  'string',
  'int',
  'float',
  'bool',
  'enum',
  'code',
  'model_ref',
] as const;

export type ParameterType = (typeof PARAMETER_TYPES)[number];

// --------------------------------------------------------------------------
// Node catalog descriptors (camelCase wire form of the Python dataclasses,
// served by GET /workflows/node-catalog)
// --------------------------------------------------------------------------

/** A typed attachment point on a node where a connection begins or ends. */
export interface PortDescriptor {
  name: string; // e.g. "in", "out"
  portType: string; // one of PORT_TYPES
}

/**
 * Constraints on a parameter value, keyed by parameter type:
 *   - int/float: `min`, `max` (inclusive), `min_exclusive` (strict)
 *   - string/code: `minLength`, `maxLength`, `regex`
 *   - enum (and discrete value sets on other types): `values`
 *
 * `min_exclusive` is a strict lower bound (the bound itself is
 * rejected), e.g. llm_inference's `top_p` in (0.0, 1.0]. It passes
 * through the wire unmapped (snake_case), matching the Python catalog
 * key.
 */
export interface ParameterConstraints {
  min?: number;
  min_exclusive?: number;
  max?: number;
  minLength?: number;
  maxLength?: number;
  regex?: string;
  values?: JsonValue[];
}

/** A configurable node parameter with type, default, and constraints. */
export interface ParameterDescriptor {
  name: string;
  paramType: string; // one of PARAMETER_TYPES
  required: boolean;
  default?: JsonValue | null;
  constraints?: ParameterConstraints;
  /**
   * Conditional visibility, declared in one of two forms (mirroring
   * `ParameterDescriptor.depends_on` in the Python catalog):
   *
   *  - a bare parameter name: the name of a bool parameter on the same
   *    node type. While that parameter's effective value is not true,
   *    the configuration panel hides this parameter's control — the
   *    original bool-truthy semantics, unchanged for every existing
   *    descriptor.
   *  - `"name=value"`: the name of a parameter on the same node type
   *    plus a literal, e.g. `"mode=poll"`. This parameter's control is
   *    visible only while the named parameter's effective value (its
   *    explicit value, else its declared default) equals the literal
   *    when both are rendered as strings — used for enum-selection
   *    gating.
   *
   * Absent/null means always visible.
   */
  dependsOn?: string | null;
  /**
   * Concise human-readable help for the parameter — what it is, the
   * expected format, and a short example value where useful — rendered
   * by the configuration panel as the field description under the
   * label. Absent/null renders no catalog-provided description.
   */
  description?: string | null;
  /**
   * Working example values for the parameter: each entry satisfies the
   * parameter's own type and constraints and can be used verbatim.
   * Absent/null offers no catalog-provided examples.
   */
  examples?: JsonValue[] | null;
}

/** How a node type is realized on one target architecture. */
export interface GstMapping {
  arch: string; // one of ARCHITECTURES
  elementChain: Array<{ factory: string; argsTemplate: Record<string, JsonValue> }>;
  executorBinding: string | null;
  pluginDependencies: string[];
}

/** Full declaration of a workflow node type (Requirement 2.8). */
export interface NodeTypeDescriptor {
  typeId: string;
  category: string; // one of CATEGORIES
  displayName: string;
  inputs: PortDescriptor[];
  outputs: PortDescriptor[];
  parameters: ParameterDescriptor[];
  mappings: GstMapping[];
  hardwareDependent: boolean;
  /**
   * Lifecycle marker served by the merged node catalog on
   * Custom_Node_Type entries backed by a test-state Plugin_Record
   * ("test"); absent on built-in node types (custom-node-designer,
   * Requirement 9.6).
   */
  lifecycleState?: string | null;
}

// --------------------------------------------------------------------------
// Subscribe-trigger descriptor mirrors (trigger-activation-runtime
// Requirement 3.5)
//
// Hand-mirrored camelCase wire forms of the `mqtt_subscribe` and
// `opcua_subscribe` descriptors in the Python catalog
// (`workflow_core/catalog/nodes.py`) — identical type ids, category,
// ports, and parameter names, types, defaults, constraints, and
// `dependsOn` gating strings (including the `"name=value"` form). Keep
// byte-for-byte in sync with the backend source of truth; the palette
// lists both under Triggers automatically (category-driven).
// --------------------------------------------------------------------------

/**
 * Architectures that correspond to physical edge devices, mirroring the
 * Python catalog's `DEVICE_ARCHITECTURES`. Used by hardware-dependent
 * node types whose `sim` mapping is a simulation stub rather than the
 * device realization.
 */
const DEVICE_ARCHITECTURES = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
] as const;

/**
 * One identical executor-binding mapping per physical device
 * architecture, mirroring the Python catalog's `_same_on_device_archs`.
 */
function sameOnDeviceArchs(
  executorBinding: string,
  pluginDependencies: string[]
): GstMapping[] {
  return DEVICE_ARCHITECTURES.map((arch) => ({
    arch,
    elementChain: [],
    executorBinding,
    pluginDependencies: [...pluginDependencies],
  }));
}

/**
 * The `ARCH_SIM` appsrc simulation stub shared by the trigger
 * descriptors, mirroring the `digital_input` simulation stub form.
 */
function simAppsrcStub(): GstMapping {
  return {
    arch: 'sim',
    elementChain: [{ factory: 'appsrc', argsTemplate: { name: '{sim_source_name}' } }],
    executorBinding: null,
    pluginDependencies: ['app'],
  };
}

/**
 * The shared per-trigger-node activation policy parameter family,
 * mirroring the Python catalog's `_trigger_policy_parameters()` helper
 * so the two trigger descriptors cannot drift: `concurrency_policy`
 * with its gated `queue_depth` / `debounce_ms` companions (`dependsOn`
 * `"name=value"` form), `retry_limit` (0 = retry forever), and
 * `priority` (lower value = higher priority; ties served FIFO by firing
 * time).
 */
function triggerPolicyParameters(): ParameterDescriptor[] {
  return [
    {
      name: 'concurrency_policy',
      paramType: 'enum',
      required: false,
      default: 'queue',
      constraints: { values: ['queue', 'drop', 'debounce'] },
      dependsOn: null,
      description:
        'What happens when this trigger fires while a run it activated is ' +
        'still in flight or pending: queue the firing (bounded by queue ' +
        'depth), drop it, or debounce — coalesce firings within the ' +
        'debounce interval into one run carrying the most recent trigger ' +
        'context.',
      examples: ['queue', 'drop'],
    },
    {
      name: 'queue_depth',
      paramType: 'int',
      required: false,
      default: 10,
      constraints: { min: 1, max: 1000 },
      dependsOn: 'concurrency_policy=queue',
      description:
        'Maximum pending activations queued for this trigger (1-1000); ' +
        'further firings are discarded and logged, e.g. 10.',
      examples: [10, 100],
    },
    {
      name: 'debounce_ms',
      paramType: 'int',
      required: false,
      default: 500,
      constraints: { min: 1, max: 60000 },
      dependsOn: 'concurrency_policy=debounce',
      description:
        'Trailing debounce interval in milliseconds (1-60000): firings ' +
        'within it coalesce into one activation carrying the most recent ' +
        'trigger context, e.g. 500.',
      examples: [500, 2000],
    },
    {
      name: 'retry_limit',
      paramType: 'int',
      required: false,
      default: 0,
      constraints: { min: 0, max: 1000 },
      dependsOn: null,
      description:
        "Maximum automatic reconnect attempts after the trigger's " +
        'connection drops (0-1000); 0 = retry forever, e.g. 0.',
      examples: [0, 5],
    },
    {
      name: 'priority',
      paramType: 'int',
      required: false,
      default: 100,
      constraints: { min: 0, max: 1000 },
      dependsOn: null,
      description:
        "Activation priority relative to the workflow's other triggers " +
        '(0-1000); lower value = higher priority, ties served in firing ' +
        'order, e.g. 100.',
      examples: [100, 10],
    },
  ];
}

/**
 * Mirror of the `mqtt_subscribe` trigger descriptor: fires a run
 * activation when a message arrives on the subscribed topic filter over
 * one of three transports (greengrass, aws_iot, plain broker). The
 * connection parameters mirror `mqtt_publish` field-for-field.
 */
export const MQTT_SUBSCRIBE_DESCRIPTOR: NodeTypeDescriptor = {
  typeId: 'mqtt_subscribe',
  category: CATEGORY_TRIGGER,
  displayName: 'MQTT Subscribe',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_EVENT_SIGNAL }],
  parameters: [
    {
      name: 'broker_host',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: null,
      description: 'MQTT broker hostname or IP, e.g. 10.0.0.12 or broker.local.',
      examples: ['10.0.0.12', 'broker.local'],
    },
    {
      name: 'broker_port',
      paramType: 'int',
      required: false,
      default: 1883,
      constraints: { min: 1, max: 65535 },
      dependsOn: null,
      description: 'MQTT broker TCP port, e.g. 1883 (plain MQTT) or 8883 (TLS).',
      examples: [1883, 8883],
    },
    {
      name: 'topic',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: null,
      description:
        'Topic filter the trigger subscribes to; a message arriving on a ' +
        'matching topic starts a workflow run, e.g. factory/line1/trigger ' +
        'or factory/+/trigger.',
      examples: ['factory/line1/trigger', 'factory/+/trigger'],
    },
    {
      name: 'qos',
      paramType: 'enum',
      required: false,
      default: 0,
      constraints: { values: [0, 1, 2] },
      dependsOn: null,
      description:
        'MQTT quality of service: 0 (at most once), 1 (at least once), or ' +
        '2 (exactly once; AWS IoT Core supports up to 1).',
      examples: [0, 1],
    },
    {
      name: 'greengrass',
      paramType: 'bool',
      required: false,
      default: false,
      constraints: {},
      dependsOn: null,
      description:
        "Publish through the device's Greengrass-managed MQTT (the " +
        "Greengrass nucleus's AWS IoT Core connection) instead of a plain " +
        'broker or your own AWS IoT credentials. Zero configuration: only ' +
        'the topic is required — no broker host or port and no certificate ' +
        'paths.',
      examples: [true],
    },
    {
      name: 'aws_iot',
      paramType: 'bool',
      required: false,
      default: false,
      constraints: {},
      dependsOn: null,
      description:
        'Publish to AWS IoT Core over mutual TLS instead of a plain MQTT ' +
        'broker; enables the IoT thing name and certificate path fields.',
      examples: [true],
    },
    {
      name: 'iot_thing_name',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
      description:
        'AWS IoT thing name used as the MQTT client id, e.g. dda-edge-device-01.',
      examples: ['dda-edge-device-01'],
    },
    {
      name: 'iot_ca_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
      description:
        'Path of the Amazon root CA certificate on the device, e.g. ' +
        '/greengrass/v2/rootCA.pem.',
      examples: ['/greengrass/v2/rootCA.pem'],
    },
    {
      name: 'iot_client_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
      description:
        'Path of the device client certificate on the device, e.g. ' +
        '/greengrass/v2/thingCert.crt.',
      examples: ['/greengrass/v2/thingCert.crt'],
    },
    {
      name: 'iot_private_key_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
      description:
        'Path of the device private key on the device, e.g. ' +
        '/greengrass/v2/privKey.key.',
      examples: ['/greengrass/v2/privKey.key'],
    },
    ...triggerPolicyParameters(),
  ],
  mappings: [
    ...sameOnDeviceArchs('mqtt_subscribe', ['python:paho-mqtt', 'python:awsiotsdk']),
    simAppsrcStub(),
  ],
  hardwareDependent: true,
};

/**
 * Mirror of the `opcua_subscribe` trigger descriptor: fires a run
 * activation when the monitored OPC UA node's value changes, via a true
 * subscription or the poll fallback. The endpoint/security parameters
 * mirror `opcua_write` field-for-field.
 */
export const OPCUA_SUBSCRIBE_DESCRIPTOR: NodeTypeDescriptor = {
  typeId: 'opcua_subscribe',
  category: CATEGORY_TRIGGER,
  displayName: 'OPC UA Subscribe',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_EVENT_SIGNAL }],
  parameters: [
    {
      name: 'endpoint',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1, regex: '^opc\\.tcp://.+' },
      dependsOn: null,
      description: 'OPC UA server endpoint URL, e.g. opc.tcp://192.168.1.20:4840.',
      examples: ['opc.tcp://192.168.1.20:4840'],
    },
    {
      name: 'node_id',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: null,
      description:
        'OPC UA node id the value is written to, e.g. ns=2;s=Machine1.Reject.',
      examples: ['ns=2;s=Machine1.Reject'],
    },
    {
      name: 'sampling_interval_ms',
      paramType: 'int',
      required: false,
      default: 100,
      constraints: { min: 10, max: 60000 },
      dependsOn: null,
      description:
        'Sampling/publishing interval of the OPC UA subscription in ' +
        'milliseconds (10-60000), e.g. 100.',
      examples: [100, 1000],
    },
    {
      name: 'mode',
      paramType: 'enum',
      required: false,
      default: 'subscribe',
      constraints: { values: ['subscribe', 'poll'] },
      dependsOn: null,
      description:
        'How value changes are detected: subscribe registers a true OPC UA ' +
        'data-change subscription (the default); poll reads the node ' +
        'periodically and fires when the value changes.',
      examples: ['subscribe', 'poll'],
    },
    {
      name: 'poll_interval_ms',
      paramType: 'int',
      required: false,
      default: 500,
      constraints: { min: 10, max: 60000 },
      dependsOn: 'mode=poll',
      description:
        'How often the node is read in poll mode, in milliseconds ' +
        '(10-60000), e.g. 500.',
      examples: [500, 2000],
    },
    {
      name: 'username',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional OPC UA user name for user-token authentication. Leave ' +
        'empty for anonymous access.',
      examples: ['operator'],
    },
    {
      name: 'password',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional password for the OPC UA user. Stored with the workflow ' +
        'definition; treat as a secret.',
      examples: ['changeit'],
    },
    {
      name: 'security_policy',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional OPC UA security policy for an encrypted/signed session, ' +
        'e.g. Basic256Sha256. Requires client_cert_path and client_key_path.',
      examples: ['Basic256Sha256'],
    },
    {
      name: 'security_mode',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional message security mode used with security_policy: Sign or ' +
        'SignAndEncrypt (defaults to SignAndEncrypt when a policy is set).',
      examples: ['SignAndEncrypt', 'Sign'],
    },
    {
      name: 'client_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional path (on the device) to the client application ' +
        'certificate used for certificate-based security.',
      examples: ['/aws_dda/opcua/client-cert.der'],
    },
    {
      name: 'client_key_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        'Optional path (on the device) to the client certificate private key.',
      examples: ['/aws_dda/opcua/client-key.pem'],
    },
    {
      name: 'server_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: {},
      dependsOn: null,
      description:
        "Optional path (on the device) to the server's certificate to " +
        'pin/trust.',
      examples: ['/aws_dda/opcua/server-cert.der'],
    },
    ...triggerPolicyParameters(),
  ],
  mappings: [
    ...sameOnDeviceArchs('opcua_subscribe', ['python:opcua']),
    simAppsrcStub(),
  ],
  hardwareDependent: true,
};

// --------------------------------------------------------------------------
// Modbus TCP output descriptor mirror (modbus-tcp-output Requirements
// 3.1, 3.2)
//
// Hand-mirrored camelCase wire form of the `modbus_write` descriptor in
// the Python catalog (`workflow_core/catalog/nodes.py`) — identical type
// id, category, display name, ports, and parameter names, types,
// defaults, constraints, and `dependsOn` gating (the `"name=value"` form
// on `pulse_ms`). Keep byte-for-byte in sync with the backend source of
// truth; the palette lists it under Outputs automatically
// (category-driven).
// --------------------------------------------------------------------------

/**
 * The `ARCH_SIM` recording stub for a hardware output node, mirroring
 * the Python catalog's `_recording_binding`: an executor binding that
 * records would-be actuations to the test run's recording log instead
 * of contacting any endpoint.
 */
function recordingBinding(nodeTypeId: string): GstMapping {
  return {
    arch: 'sim',
    elementChain: [],
    executorBinding: `recording_${nodeTypeId}`,
    pluginDependencies: [],
  };
}

/**
 * Mirror of the `modbus_write` output descriptor: after a workflow run
 * completes, writes one value to one coil or holding register on a
 * Modbus TCP server (typically a PLC), gated by upstream conditional /
 * inference_filter nodes exactly like digital_output / mqtt_publish /
 * opcua_write.
 */
export const MODBUS_WRITE_DESCRIPTOR: NodeTypeDescriptor = {
  typeId: 'modbus_write',
  category: CATEGORY_OUTPUT,
  displayName: 'Modbus TCP Write',
  inputs: [{ name: 'in', portType: PORT_TYPE_INFERENCE_META }],
  outputs: [],
  parameters: [
    {
      name: 'host',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: null,
      description: 'Modbus TCP server (PLC) hostname or IP, e.g. 192.168.1.30.',
      examples: ['192.168.1.30', 'plc.local'],
    },
    {
      name: 'port',
      paramType: 'int',
      required: false,
      default: 502,
      constraints: { min: 1, max: 65535 },
      dependsOn: null,
      description: 'Modbus TCP port, e.g. 502 (the standard Modbus port).',
      examples: [502],
    },
    {
      name: 'unit_id',
      paramType: 'int',
      required: false,
      default: 1,
      constraints: { min: 0, max: 255 },
      dependsOn: null,
      description:
        'Modbus unit (slave) id addressed by the write (0-255), e.g. 1.',
      examples: [1, 0],
    },
    {
      name: 'register_type',
      paramType: 'enum',
      required: true,
      default: 'coil',
      constraints: { values: ['coil', 'holding_register'] },
      dependsOn: null,
      description:
        'Write target kind: coil (a single on/off bit, Write Single Coil ' +
        'function code 0x05) or holding_register (a 16-bit register, Write ' +
        'Single Register function code 0x06).',
      examples: ['coil', 'holding_register'],
    },
    {
      name: 'address',
      paramType: 'int',
      required: true,
      default: null,
      constraints: { min: 0, max: 65535 },
      dependsOn: null,
      description:
        'Address of the coil or holding register written (0-65535), e.g. 12.',
      examples: [12, 40],
    },
    {
      name: 'value_template',
      paramType: 'string',
      required: false,
      default: '{is_anomalous}',
      constraints: {},
      dependsOn: null,
      description:
        'Value written to the target. Placeholders in curly braces are ' +
        'replaced from the inference metadata: {is_anomalous}, ' +
        '{confidence}, or {inference_json}; a single placeholder keeps its ' +
        'native type. Coil writes coerce the rendered value to a boolean; ' +
        'holding-register writes coerce it to an integer 0-65535.',
      examples: ['{is_anomalous}', '{confidence}'],
    },
    {
      name: 'pulse_ms',
      paramType: 'int',
      required: false,
      default: 0,
      constraints: { min: 0, max: 60000 },
      dependsOn: 'register_type=coil',
      description:
        'Coil pulse duration in milliseconds (0-60000): 0 latches the ' +
        'written value (single write); a positive value writes the ' +
        'rendered value, waits pulse_ms milliseconds, then writes the ' +
        'inverse coil value, e.g. 250.',
      examples: [0, 250],
    },
  ],
  mappings: [
    ...sameOnDeviceArchs('modbus_write', []),
    recordingBinding('modbus_write'),
  ],
  hardwareDependent: true,
};

// --------------------------------------------------------------------------
// Validation findings (wire form of workflow_core.validator ValidationFinding)
// --------------------------------------------------------------------------

export const SEVERITY_ERROR = 'error';
export const SEVERITY_WARNING = 'warning';

export type FindingSeverity = typeof SEVERITY_ERROR | typeof SEVERITY_WARNING;

/**
 * One validation error or warning (Requirement 4.6), camelCase wire form
 * matching `ValidationFinding.to_dict()` in the Python validator.
 */
export interface ValidationFinding {
  severity: FindingSeverity;
  code: string;
  message: string;
  nodeId: string | null;
  connectionId: string | null;
}

// --------------------------------------------------------------------------
// Workflow_Store API wire shapes (workflows.py / workflow_validation.py)
// --------------------------------------------------------------------------

/** Public shape of a workflow metadata item returned by the Workflow_Store API. */
export interface WorkflowSummary {
  workflow_id: string;
  usecase_id: string;
  account_id?: string | null;
  name: string;
  description: string;
  created_at: number;
  updated_at: number;
  latest_version: number;
  created_by?: string;
}

/**
 * Validation status recorded on a workflow version by the validate
 * endpoint (design: WorkflowVersions.validation_status).
 */
export interface WorkflowValidationStatus {
  status: 'none' | 'passed' | 'failed';
  findings_key?: string;
  validated_at?: number;
}

/**
 * Result of one backend validation run
 * (POST /workflows/{id}/validate, Requirements 4.6, 4.9).
 */
export interface WorkflowValidationRun {
  workflow_id: string;
  version: number;
  passed: boolean;
  validation_status: WorkflowValidationStatus;
  findings: ValidationFinding[];
  error_count: number;
  warning_count: number;
}

/**
 * One affected node or connection of a user-readable Structural_Error
 * (generation_gate.user_readable_errors, portal-build-fleet-and-workflow-gates
 * Requirement 8.8). `displayName` is present only when the definition
 * element carries one; the element is otherwise identified by id alone.
 */
export interface GenerationAffectedElement {
  id: string;
  kind: 'node' | 'connection';
  displayName?: string;
}

/**
 * One user-readable Structural_Error from a `GENERATION_REJECTED` error
 * envelope (`error.details.structural_errors`, Requirement 8.8).
 */
export interface GenerationStructuralError {
  code: string;
  message: string;
  affected: GenerationAffectedElement[];
  explanation: string;
}

/**
 * Generation_Gate metadata attached to accept-path generation responses
 * (workflow_generator.gate_metadata, Requirements 8.3, 8.6). When
 * `repaired` is true, `corrected_errors` lists the original
 * Structural_Errors that the automatic Repair_Pass corrected.
 */
export interface GenerationGate {
  passed: boolean;
  repaired: boolean;
  corrected_errors: ValidationFinding[];
  structural_error_codes: string[];
}

/**
 * Successful prompt-based generation result
 * (POST /workflows/generate, workflow_generator.py, Requirements 10.2,
 * 10.3, 10.5). The backend always runs the Workflow_Validator on the
 * generated definition and returns the findings alongside it, plus the
 * Generation_Gate metadata (Requirement 8.3).
 */
export interface WorkflowGenerationResult {
  session_id: string;
  usecase_id: string;
  definition: WorkflowDefinition;
  findings: ValidationFinding[];
  error_count: number;
  warning_count: number;
  validation_passed: boolean;
  assistant_text: string | null;
  model_id?: string;
  gate?: GenerationGate;
}

// --------------------------------------------------------------------------
// Workflow_Test_Runner API wire shapes (workflow_testing.py, Requirement 12)
// --------------------------------------------------------------------------

/**
 * Public shape of a Test_Dataset record scoped to a Use_Case
 * (design: TestDatasets table, Requirements 12.2, 12.3).
 */
export interface TestDataset {
  dataset_id: string;
  usecase_id: string;
  account_id?: string | null;
  name: string;
  description?: string;
  s3_prefix?: string;
  total_bytes?: number;
  file_count?: number;
  format?: string;
  created_at?: number;
  created_by?: string;
}

/** One presigned part URL of a multipart dataset upload. */
export interface TestDatasetUploadPart {
  part_number: number;
  url: string;
}

/** Presigned multipart upload of a single dataset file. */
export interface TestDatasetUploadFile {
  name: string;
  key: string;
  upload_id: string;
  part_size: number;
  parts: TestDatasetUploadPart[];
}

/**
 * Result of POST /test-datasets (action=initiate): presigned multipart
 * uploads for the declared file set. No dataset record exists until the
 * finalize step verifies the uploaded content (Requirement 12.3).
 */
export interface TestDatasetUploadInitiation {
  dataset_id: string;
  usecase_id: string;
  name: string;
  s3_prefix: string;
  upload: {
    files: TestDatasetUploadFile[];
    expires_in: number;
  };
  message?: string;
}

/** A completed part of a multipart upload sent to the finalize step. */
export interface TestDatasetCompletedPart {
  part_number: number;
  etag: string;
}

/** A completed file upload sent to the finalize step. */
export interface TestDatasetCompletedFile {
  key: string;
  upload_id: string;
  parts: TestDatasetCompletedPart[];
}

/** Test run lifecycle status (TestRuns table). */
export type TestRunStatus = 'pending' | 'running' | 'completed' | 'failed';

/** Failure record of a failed test run (12.10, 12.13). */
export interface TestRunFailure {
  nodeId: string | null;
  message: string;
  timeout: boolean;
}

/**
 * One human-readable progress entry appended by a state-machine step
 * while a test run executes: when it was recorded (epoch ms) and what
 * the run was doing (e.g. "Validating the workflow definition").
 */
export interface TestRunProgressEntry {
  at: number;
  message: string;
}

/** Public shape of one test run (TestRuns table). */
export interface WorkflowTestRun {
  test_run_id: string;
  workflow_id: string;
  version: number;
  usecase_id: string;
  dataset_id: string;
  status: TestRunStatus;
  /**
   * Human-readable entries describing what the run has been doing
   * (appended by the state-machine steps, most recent last), shown as
   * live progress while the run is polled and in the final report.
   */
  progress?: TestRunProgressEntry[] | null;
  failure: TestRunFailure | null;
  started_at: number | null;
  finished_at: number | null;
  created_by?: string;
}

/** Error record on a per-node test result. */
export interface TestRunNodeError {
  code?: string;
  message?: string;
  [key: string]: JsonValue | undefined;
}

/**
 * Per-node result of a test run: produced outputs, recorded stub
 * activity, and any error, keyed by the node identifier (12.7). A
 * non-empty `stubActivity` marks the node as executed with a stub —
 * simulated rather than actuated (12.6, 12.8).
 */
export interface TestRunNodeResult {
  nodeId: string;
  status: string;
  outputs: JsonValue[];
  stubActivity: JsonValue[];
  error: TestRunNodeError | null;
}

/** GET /test-runs/{id}: status plus per-node results produced so far. */
export interface WorkflowTestRunDetail {
  test_run: WorkflowTestRun;
  node_results: TestRunNodeResult[];
}
