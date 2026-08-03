/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

/**
 * Pure view-model logic for the output-node preview card
 * (output-node-preview-popover spec).
 *
 * All preview logic — which node types get a preview, state precedence,
 * content extraction, and snippet truncation — lives here, free of React/DOM,
 * mirroring the `graphGeometry.ts` pattern. `RunStatusGraph.tsx` only wires
 * the selector to react-query and Cloudscape.
 */

import type {
  NodeRunStatus,
  WorkflowExecutionMetadata,
} from "api/WorkflowRegistrationAPI";

// --------------------------------------------------------------------------
// Output-node classification (D2 scope; R1.1, R1.4)
// --------------------------------------------------------------------------

/** Node types that get an output preview (Requirements D2 scope). */
export const OUTPUT_NODE_TYPES = new Set([
  "capture",
  "llm_inference",
  "bedrock_inference",
  "mqtt_publish",
  "opcua_write",
  "digital_output",
]);

/** True when the node type is one of the output-node types (R1.1, R1.4). */
export function isOutputNode(type: string): boolean {
  return OUTPUT_NODE_TYPES.has(type);
}

// --------------------------------------------------------------------------
// Snippet truncation (D5; R2.2, R2.5)
// --------------------------------------------------------------------------

/** Max snippet length before ellipsis truncation (D5). */
export const SNIPPET_MAX_LENGTH = 280;

/** Truncate to SNIPPET_MAX_LENGTH chars, appending "…" when truncated. */
export function snippet(text: string): string {
  if (text.length <= SNIPPET_MAX_LENGTH) {
    return text;
  }
  return `${text.slice(0, SNIPPET_MAX_LENGTH)}…`;
}

// --------------------------------------------------------------------------
// Preview view-model
// --------------------------------------------------------------------------

export type PreviewViewModel =
  | { kind: "none" } // not an output node
  | { kind: "pending" } // non-terminal status (R3.1)
  | { kind: "failure"; detail?: string } // failure status (R3.2)
  | { kind: "loading" } // metadata query in flight (R3.4)
  | { kind: "image"; src: string } // capture (R2.1)
  | { kind: "text"; text: string } // llm_inference (R2.2, snippet applied)
  | { kind: "fields"; fields: [string, string][] } // bedrock_inference (R2.3)
  | { kind: "status"; status: string; detail?: string } // publish types (R2.4)
  | { kind: "unavailable" }; // missing data (R3.3)

/** The three publish-style output types whose preview is the run status. */
const STATUS_PREVIEW_TYPES = new Set([
  "mqtt_publish",
  "opcua_write",
  "digital_output",
]);

/** True for the terminal statuses (`success`, `warning`, `failure`). */
function isTerminalStatus(status: string | undefined): boolean {
  return status === "success" || status === "warning" || status === "failure";
}

/**
 * Defensive read of `metadata.llm?.[nodeId]`, returning the entry object or
 * undefined. Arbitrary tag values can appear anywhere in the metadata, so
 * every path access type-checks before descending.
 */
function llmEntry(
  metadata: WorkflowExecutionMetadata | undefined,
  nodeId: string,
): Record<string, unknown> | undefined {
  if (metadata === undefined) {
    return undefined;
  }
  const llm = metadata.llm;
  if (llm === null || typeof llm !== "object" || Array.isArray(llm)) {
    return undefined;
  }
  const entry = (llm as Record<string, unknown>)[nodeId];
  if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
    return undefined;
  }
  return entry as Record<string, unknown>;
}

/** Render an arbitrary metadata value as a display string for fields rows. */
function fieldValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (
    typeof value === "number" ||
    typeof value === "boolean" ||
    value === null
  ) {
    return String(value);
  }
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * Compute the preview view-model for the selected node.
 *
 * State precedence (evaluated in order; design "State precedence"):
 * 1. Not an output type → `none` (R1.4).
 * 2. Status absent or non-terminal (`pending`/`running`) → `pending` (R3.1).
 * 3. Status `failure` → `failure` with the node's detail (R3.2).
 * 4. Type-specific extraction (status is `success` or `warning`):
 *    - `capture`: `hasImageResults` → `image`; else `unavailable` (R2.1, R3.3).
 *    - `llm_inference`: loading → `loading`; `llm[nodeId].generated_text`
 *      (non-empty string) → `text` with `snippet(...)`; else `unavailable`
 *      (R2.2, R3.3, R3.4).
 *    - `bedrock_inference`: loading → `loading`; `is_anomalous`/`confidence`
 *      present → `fields`; else `unavailable` (R2.3, R3.3, R3.4).
 *    - publish types → `status` with the node's status and detail (R2.4).
 */
export function previewViewModel(args: {
  nodeType: string;
  nodeId: string;
  statusEntry?: NodeRunStatus;
  hasImageResults: boolean;
  imageSrc: string;
  metadata?: WorkflowExecutionMetadata;
  metadataLoading: boolean;
  metadataError: boolean;
}): PreviewViewModel {
  const {
    nodeType,
    nodeId,
    statusEntry,
    hasImageResults,
    imageSrc,
    metadata,
    metadataLoading,
    metadataError,
  } = args;

  // 1. Not an output node → no preview (R1.4).
  if (!isOutputNode(nodeType)) {
    return { kind: "none" };
  }

  // 2. Missing or non-terminal status → in-flight placeholder (R3.1).
  const status = statusEntry?.status;
  if (!isTerminalStatus(status)) {
    return { kind: "pending" };
  }

  // 3. Failure keeps the existing failure-alert content (R3.2).
  if (status === "failure") {
    return statusEntry?.detail !== undefined
      ? { kind: "failure", detail: statusEntry.detail }
      : { kind: "failure" };
  }

  // 4. Terminal success/warning → type-specific extraction.
  if (nodeType === "capture") {
    return hasImageResults
      ? { kind: "image", src: imageSrc }
      : { kind: "unavailable" };
  }

  if (STATUS_PREVIEW_TYPES.has(nodeType)) {
    // Publish bindings do not persist payloads; the status + detail is the
    // available output evidence (R2.4). No fetch needed.
    return statusEntry?.detail !== undefined
      ? { kind: "status", status: status as string, detail: statusEntry.detail }
      : { kind: "status", status: status as string };
  }

  // Metadata-backed types (llm_inference / bedrock_inference).
  if (metadataLoading) {
    return { kind: "loading" };
  }
  if (metadataError) {
    return { kind: "unavailable" };
  }

  if (nodeType === "llm_inference") {
    const entry = llmEntry(metadata, nodeId);
    const generated = entry?.generated_text;
    if (typeof generated === "string" && generated.length > 0) {
      return { kind: "text", text: snippet(generated) };
    }
    // An llm `error` entry (or a missing entry) yields the fallback; the
    // error itself also surfaces via node status (R3.3).
    return { kind: "unavailable" };
  }

  // bedrock_inference: top-level merged fields (R2.3).
  const fields: [string, string][] = [];
  if (metadata !== undefined && "is_anomalous" in metadata) {
    fields.push(["is_anomalous", fieldValue(metadata.is_anomalous)]);
  }
  if (metadata !== undefined && "confidence" in metadata) {
    fields.push(["confidence", fieldValue(metadata.confidence)]);
  }
  return fields.length > 0 ? { kind: "fields", fields } : { kind: "unavailable" };
}
