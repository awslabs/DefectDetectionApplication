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

export const CATEGORY_INPUT = 'input';
export const CATEGORY_PREPROCESSING = 'preprocessing';
export const CATEGORY_INFERENCE = 'inference';
export const CATEGORY_POST_PROCESSING = 'post_processing';
export const CATEGORY_OUTPUT = 'output';

export const CATEGORIES = [
  CATEGORY_INPUT,
  CATEGORY_PREPROCESSING,
  CATEGORY_INFERENCE,
  CATEGORY_POST_PROCESSING,
  CATEGORY_OUTPUT,
] as const;

export type NodeCategory = (typeof CATEGORIES)[number];

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
 *   - int/float: `min`, `max`
 *   - string/code: `minLength`, `maxLength`, `regex`
 *   - enum (and discrete value sets on other types): `values`
 */
export interface ParameterConstraints {
  min?: number;
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
   * Conditional visibility: the name of a bool parameter on the same
   * node type. While that parameter's effective value is not true, the
   * configuration panel hides this parameter's control. Absent/null
   * means always visible.
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
 * Successful prompt-based generation result
 * (POST /workflows/generate, workflow_generator.py, Requirements 10.2,
 * 10.3, 10.5). The backend always runs the Workflow_Validator on the
 * generated definition and returns the findings alongside it.
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
