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
import axios from "axios";
import { Connection } from "config/Interface";
import { MaskBackground } from "api/WorkflowAPI";

// Endpoint constants are built locally from the read-only Connection export
// so config/Interface.tsx stays untouched.
const REGISTRATIONS_ENDPOINT = `${Connection.ENDPOINT}/workflows/registrations`;
const EXECUTIONS_ENDPOINT = `${Connection.ENDPOINT}/workflows/executions`;

export type RegistrationStatus = "registered" | "invalid";
export type ExecutionStatus = "pending" | "running" | "completed" | "failed";

export interface WorkflowRegistration {
  registrationId: string;
  workflowId: string;
  /**
   * Human-friendly workflow name from the deployed manifest. Null for packages
   * built before the packager emitted it; callers fall back to workflowId.
   */
  name?: string | null;
  version: string;
  arch: string;
  artifactPath: string;
  status: RegistrationStatus;
  /** Epoch seconds. */
  registeredAt: number;
  /** Present only when status is not "registered". */
  invalidReason?: string;
}

export interface WorkflowExecution {
  executionId: string;
  registrationId: string;
  status: ExecutionStatus;
  /** Epoch seconds; null until the execution starts. */
  startedAt: number | null;
  /** Epoch seconds; null until the execution finishes. */
  finishedAt: number | null;
  failingNodeId: string | null;
  error: string | null;
  /**
   * Whether this run routed capture artifacts (terminal File_Output_Node),
   * i.e. it has viewable image results. Drives the "View results" link.
   */
  hasImageResults: boolean;
  /** The per-run capture id used to locate artifacts; null when none. */
  captureId: string | null;
  /** The per-run artifact output directory; null when none. */
  outputDir: string | null;
}

export interface WorkflowRegistrationDetails extends WorkflowRegistration {
  executions: WorkflowExecution[];
}

/** A single viewable image produced by a run. */
export interface WorkflowExecutionResultImage {
  kind: "output" | "input";
  hasOverlay: boolean;
}

/** Response shape of the run results-metadata endpoint. */
export interface WorkflowExecutionResults {
  hasImageResults: boolean;
  captureId: string | null;
  images: WorkflowExecutionResultImage[];
}

/**
 * Response shape of the run overlay endpoint, in the exact form the existing
 * on-device overlay pipeline (`getMaskImageProp` / `setupMaskImage`) consumes.
 */
export interface WorkflowExecutionOverlay {
  maskImage: string | null;
  maskBackground: MaskBackground | null;
}

/** Per-node run status for a single node. */
export interface NodeRunStatus {
  status: string;
  detail?: string;
  /** Execution duration in milliseconds; additive (node-execution-timing R2.1). */
  durationMs?: number;
}

/** Map of nodeId to its run status. */
export type NodeStatusMap = Record<string, NodeRunStatus>;

/** A single node in the authored workflow graph, with its layout position. */
export interface WorkflowGraphNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  // Authored nodes carry additional parameters the renderer may ignore.
  [key: string]: unknown;
}

/** A directed connection between two nodes in the workflow graph. */
export interface WorkflowGraphConnection {
  [key: string]: unknown;
}

/** The authored workflow.json graph used to render the run-status graph. */
export interface WorkflowGraph {
  schemaVersion: string;
  nodes: WorkflowGraphNode[];
  connections: WorkflowGraphConnection[];
}

export async function listWorkflowRegistrations(): Promise<
  WorkflowRegistration[]
> {
  const { data } = await axios.get<WorkflowRegistration[]>(
    REGISTRATIONS_ENDPOINT,
  );
  return data;
}

export async function getWorkflowRegistration(
  id: string,
): Promise<WorkflowRegistrationDetails> {
  const { data } = await axios.get<WorkflowRegistrationDetails>(
    `${REGISTRATIONS_ENDPOINT}/${id}`,
  );
  return data;
}

export async function triggerWorkflowRegistration(
  id: string,
): Promise<WorkflowExecution> {
  const { data } = await axios.post<WorkflowExecution>(
    `${REGISTRATIONS_ENDPOINT}/${id}/trigger`,
  );
  return data;
}

export async function getWorkflowExecution(
  id: string,
): Promise<WorkflowExecution> {
  const { data } = await axios.get<WorkflowExecution>(
    `${EXECUTIONS_ENDPOINT}/${id}`,
  );
  return data;
}

/** Results metadata (viewable images + overlay availability) for a run. */
export async function getWorkflowExecutionResults(
  id: string,
): Promise<WorkflowExecutionResults> {
  const { data } = await axios.get<WorkflowExecutionResults>(
    `${EXECUTIONS_ENDPOINT}/${id}/results`,
  );
  return data;
}

/**
 * The per-execution run log as plain text. The backend responds 200 with an
 * empty body when the log is not yet available, so the caller gets "" rather
 * than an error.
 */
export async function getWorkflowExecutionLog(id: string): Promise<string> {
  const { data } = await axios.get<string>(`${EXECUTIONS_ENDPOINT}/${id}/log`, {
    responseType: "text",
    // Keep the raw text; axios otherwise tries to JSON-parse string bodies.
    transformResponse: (value) => value,
  });
  return data ?? "";
}

/** The mask overlay (base64 + background) for a run, or nulls when absent. */
export async function getWorkflowExecutionOverlay(
  id: string,
): Promise<WorkflowExecutionOverlay> {
  const { data } = await axios.get<WorkflowExecutionOverlay>(
    `${EXECUTIONS_ENDPOINT}/${id}/overlay`,
  );
  return data;
}

/** The run's metadata JSON ({capture_id}.json), or {} when unavailable. */
export type WorkflowExecutionMetadata = Record<string, unknown>;

/**
 * The run metadata (final tag values, including each LLM node's
 * `generated_text` and Bedrock's merged fields). The backend responds 200
 * with `{}` when the metadata artifact is unavailable, so the caller never
 * sees an error for a missing file.
 */
export async function getWorkflowExecutionMetadata(
  id: string,
): Promise<WorkflowExecutionMetadata> {
  const { data } = await axios.get<WorkflowExecutionMetadata>(
    `${EXECUTIONS_ENDPOINT}/${id}/metadata`,
  );
  return data;
}

/** Per-node run status map for a run, addressable by nodeId. */
export async function getWorkflowExecutionNodeStatus(
  id: string,
): Promise<NodeStatusMap> {
  const { data } = await axios.get<NodeStatusMap>(
    `${EXECUTIONS_ENDPOINT}/${id}/node-status`,
  );
  return data;
}

/** The authored workflow.json graph (nodes + connections) for a registration. */
export async function getWorkflowRegistrationGraph(
  id: string,
): Promise<WorkflowGraph> {
  const { data } = await axios.get<WorkflowGraph>(
    `${REGISTRATIONS_ENDPOINT}/${id}/graph`,
  );
  return data;
}

/**
 * Build the base output-image URL for a run, appending the auth token as a
 * query parameter when auth is enabled (mirroring how the result-history view
 * builds capture-image URLs, since the image is served on a download route
 * that reads the token from the query string).
 */
export function workflowExecutionOutputImageUrl(
  id: string,
  token?: string,
): string {
  const base = `${EXECUTIONS_ENDPOINT}/${id}/output-image`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}
