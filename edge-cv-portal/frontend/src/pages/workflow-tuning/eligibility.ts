/**
 * Tunable_Node classification — the frontend half of the one rule
 * (quality-prompt-tuning, Requirement 1.5, Property 1).
 *
 * A node is a Tunable_Node exactly when it is an Inspection_Node — node type
 * `bedrock_inference` or `llm_inference`, matched exactly, with no trimming
 * and no case folding — running in Anomaly_Mode:
 *
 *   - `bedrock_inference`: an absent `anomaly_mode` parameter (and a stored
 *     `null`) defaults to Anomaly_Mode; any other value is coerced and taken
 *     for its truth.
 *   - `llm_inference`: Anomaly_Mode only when the coerced value is truthy —
 *     absent, `null` and `false` all keep the freeform path.
 *   - every other node type: never tunable.
 *
 * This is a faithful port of the shared Python rule
 * (`workflow_core.anomaly_invocation.coerce_parameter_value` /
 * `is_anomaly_mode` / `is_tunable_node`) which the executor, the Portal
 * backend and this module must agree on, so a definition carrying
 * `"false"` as a *string* is read identically on the device, in the Portal
 * and in the designer. The shared oracle for that agreement is the
 * generated fixture
 * `edge-cv-portal/backend/tests/fixtures/anomaly_tuning_eligibility_cases.json`,
 * consumed by `eligibility.property.test.ts` (task 8.5).
 *
 * Consumers: the designer's toolbar action (Requirement 1.3) and node
 * configuration panel link (Requirement 1.4). Hiding an entry point is
 * convenience only — the Portal recomputes tunability server-side on every
 * session create and apply.
 */

/** The Bedrock (cloud VLM) Inspection_Node type id. */
export const NODE_TYPE_BEDROCK_INFERENCE = 'bedrock_inference';

/** The on-device VLM Inspection_Node type id. */
export const NODE_TYPE_LLM_INFERENCE = 'llm_inference';

/** The two Inspection_Node types, the only tunable candidates (Req 1.5). */
export const INSPECTION_NODE_TYPES: readonly string[] = [
  NODE_TYPE_BEDROCK_INFERENCE,
  NODE_TYPE_LLM_INFERENCE,
];

/** The node parameter that selects Anomaly_Mode. */
export const ANOMALY_MODE_PARAMETER = 'anomaly_mode';

/**
 * The executor's parameter coercion (`_coerce` / `coerce_parameter_value`):
 * a string is trimmed and lowercased to recognise `true`/`false`, else read
 * as an integer or a decimal number when it looks like one, else kept
 * unchanged (the ORIGINAL string, not the trimmed one). Non-string values
 * are never coerced.
 *
 * The numeric tests run on the trimmed text because Python's `int()`/
 * `float()` tolerate surrounding whitespace, so `" 0 "` must read as the
 * number 0 here too.
 */
export function coerceParameterValue(value: unknown): unknown {
  if (typeof value !== 'string') {
    return value;
  }
  const trimmed = value.trim();
  const lowered = trimmed.toLowerCase();
  if (lowered === 'true') {
    return true;
  }
  if (lowered === 'false') {
    return false;
  }
  if (/^[+-]?[0-9]+$/.test(trimmed)) {
    return Number(trimmed);
  }
  if (trimmed.includes('.') && /^[+-]?([0-9]+\.[0-9]*|\.[0-9]+)$/.test(trimmed)) {
    return Number(trimmed);
  }
  return value;
}

/**
 * Truth of a coerced value, stated explicitly rather than left to
 * JavaScript's `Boolean` so it matches Python's `bool`: `0`, `-0` and `0.0`
 * are false and every other number is true (`NaN` included, as in Python);
 * the empty string is false and every other string — a whitespace-only
 * string included — is true; `null`/`undefined` are false.
 */
export function isTruthyParameterValue(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return value !== 0;
  }
  if (typeof value === 'string') {
    return value !== '';
  }
  return value !== null && value !== undefined;
}

/**
 * True when the node runs in Anomaly_Mode — the executor's rule, over the
 * node's RAW `anomaly_mode` parameter value (`undefined`/`null` meaning
 * absent).
 */
export function isAnomalyMode(nodeType: unknown, anomalyMode: unknown): boolean {
  const coerced = coerceParameterValue(anomalyMode);
  if (nodeType === NODE_TYPE_BEDROCK_INFERENCE) {
    // Absent (or stored null) defaults to Anomaly_Mode.
    return coerced === null || coerced === undefined ? true : isTruthyParameterValue(coerced);
  }
  if (nodeType === NODE_TYPE_LLM_INFERENCE) {
    return isTruthyParameterValue(coerced);
  }
  return false;
}

/**
 * True when the node is a Tunable_Node (Requirement 1.5) — an
 * Inspection_Node in Anomaly_Mode. Named for the tuning surfaces;
 * `isAnomalyMode` is the same rule under the executor's name, exactly as
 * the shared Python module keeps both names over one implementation.
 */
export function isTunableNode(nodeType: unknown, anomalyMode: unknown): boolean {
  return isAnomalyMode(nodeType, anomalyMode);
}

/** The shape both a Workflow_Definition node and a canvas node satisfy. */
export interface TunableNodeLike {
  id?: string;
  type?: unknown;
  parameters?: Record<string, unknown> | null;
}

/** `isTunableNode` reading the type and `anomaly_mode` off a node. */
export function isTunableWorkflowNode(node: TunableNodeLike | null | undefined): boolean {
  if (node === null || node === undefined) {
    return false;
  }
  return isTunableNode(node.type, node.parameters?.[ANOMALY_MODE_PARAMETER]);
}

/** The ids of a definition's Tunable_Nodes, in document order. */
export function tunableNodeIds(
  definition: { nodes?: readonly TunableNodeLike[] } | null | undefined
): string[] {
  return (definition?.nodes ?? [])
    .filter((node) => isTunableWorkflowNode(node))
    .map((node) => String(node.id ?? ''));
}

/**
 * True when the definition has at least one Tunable_Node — the condition
 * for the designer toolbar's "Tune anomaly prompts" action (Req 1.3).
 */
export function definitionHasTunableNode(
  definition: { nodes?: readonly TunableNodeLike[] } | null | undefined
): boolean {
  return (definition?.nodes ?? []).some((node) => isTunableWorkflowNode(node));
}
