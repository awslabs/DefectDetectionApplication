/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/**
 * Pure readers of a run's Scene_Analytics_Node outputs
 * (rtsp-rtmp-stream-cameras Requirement 16.3), as the Workflow_Executor
 * merges them into the run metadata:
 *
 * - `counter.<nodeId>`: `{counts: {labelKey: n}, total, labels: {labelKey: label}}`
 * - `association.<nodeId>`: `{subjects, compliant, violations,
 *   missing: {labelKey: n}, violating_ids: [Detection_ID]}`
 * - `event.<nodeId>`: `{state, transition, active_since,
 *   consecutive_true, consecutive_false}`
 *
 * The metadata is arbitrary JSON, so everything here reads it defensively,
 * skips what does not fit, and never throws. Sections keep the metadata's
 * key order, which is the executor's evaluation order.
 */
import type { WorkflowExecutionMetadata } from "api/WorkflowRegistrationAPI";

export interface CountRow {
  /** The Label_Key the counter counted under. */
  key: string;
  /** The label as the model reported it, else the key. */
  label: string;
  count: number;
}

export interface CounterSection {
  nodeId: string;
  total: number;
  rows: CountRow[];
}

export interface AssociationSection {
  nodeId: string;
  subjects: number;
  compliant: number;
  violations: number;
  /** How many subjects lacked each required class. */
  missing: CountRow[];
  /** The Detection_IDs of the non-compliant subjects. */
  violatingIds: string[];
}

export type EventGateState = "active" | "inactive";
export type EventGateTransition = "activated" | "cleared" | "none";

export interface EventGateSection {
  nodeId: string;
  state: EventGateState;
  transition: EventGateTransition;
  /** Epoch milliseconds, while active. */
  activeSince: number | null;
  consecutiveTrue: number;
  consecutiveFalse: number;
}

export interface SceneAnalytics {
  counters: CounterSection[];
  associations: AssociationSection[];
  gates: EventGateSection[];
}

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

/** `[nodeId, record]` of each object entry of `metadata[section]`. */
function nodeEntries(
  metadata: WorkflowExecutionMetadata | undefined | null,
  section: string,
): [string, JsonObject][] {
  if (!isObject(metadata)) {
    return [];
  }
  const bucket = metadata[section];
  if (!isObject(bucket)) {
    return [];
  }
  return Object.entries(bucket).filter((entry): entry is [string, JsonObject] =>
    isObject(entry[1]),
  );
}

function countRows(counts: unknown, labels: unknown): CountRow[] {
  if (!isObject(counts)) {
    return [];
  }
  const names = isObject(labels) ? labels : {};
  return Object.entries(counts).map(([key, value]) => {
    const label = names[key];
    return {
      key,
      label: typeof label === "string" && label.length > 0 ? label : key || "(unlabeled)",
      count: count(value),
    };
  });
}

export function runCounters(
  metadata: WorkflowExecutionMetadata | undefined | null,
): CounterSection[] {
  return nodeEntries(metadata, "counter").map(([nodeId, record]) => ({
    nodeId,
    total: count(record.total),
    rows: countRows(record.counts, record.labels),
  }));
}

export function runAssociations(
  metadata: WorkflowExecutionMetadata | undefined | null,
): AssociationSection[] {
  return nodeEntries(metadata, "association").map(([nodeId, record]) => ({
    nodeId,
    subjects: count(record.subjects),
    compliant: count(record.compliant),
    violations: count(record.violations),
    missing: countRows(record.missing, undefined),
    violatingIds: Array.isArray(record.violating_ids)
      ? record.violating_ids.filter(
          (id): id is string => typeof id === "string" && id.length > 0,
        )
      : [],
  }));
}

export function runEventGates(
  metadata: WorkflowExecutionMetadata | undefined | null,
): EventGateSection[] {
  return nodeEntries(metadata, "event").map(([nodeId, record]) => ({
    nodeId,
    state: record.state === "active" ? "active" : "inactive",
    transition:
      record.transition === "activated" || record.transition === "cleared"
        ? record.transition
        : "none",
    activeSince:
      typeof record.active_since === "number" && Number.isFinite(record.active_since)
        ? record.active_since
        : null,
    consecutiveTrue: count(record.consecutive_true),
    consecutiveFalse: count(record.consecutive_false),
  }));
}

/** Every Scene_Analytics_Node output of the run. */
export function runSceneAnalytics(
  metadata: WorkflowExecutionMetadata | undefined | null,
): SceneAnalytics {
  return {
    counters: runCounters(metadata),
    associations: runAssociations(metadata),
    gates: runEventGates(metadata),
  };
}

export function hasSceneAnalytics(analytics: SceneAnalytics): boolean {
  return (
    analytics.counters.length > 0 ||
    analytics.associations.length > 0 ||
    analytics.gates.length > 0
  );
}

/** The Detection_IDs any association of the run found in violation. */
export function violatingDetectionIds(analytics: SceneAnalytics): Set<string> {
  const ids = new Set<string>();
  for (const association of analytics.associations) {
    for (const id of association.violatingIds) {
      ids.add(id);
    }
  }
  return ids;
}
