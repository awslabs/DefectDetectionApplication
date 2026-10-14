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
 * Pure helpers for a run's Detection_List (run-detection-visibility spec).
 *
 * The executor merges the run's detections into the run metadata
 * (`workflow_engine.detections.merge_detections`) as
 * `detections: [{id, label, confidence, x_min, y_min, x_max, y_max}]`, in the
 * order downstream nodes see them. The metadata is served verbatim by
 * `/metadata`, and its values are arbitrary JSON, so everything here reads it
 * defensively and never throws.
 */

import type { WorkflowExecutionMetadata } from "api/WorkflowRegistrationAPI";

/** One detected object, as shown in the run views. */
export interface RunDetection {
  /** The executor's Detection_ID, when recorded. */
  id?: string;
  /** The class label; "object" when the entry carries none. */
  label: string;
  /** The model's confidence, as recorded (0..1). */
  confidence: number;
  /** `[x_min, y_min, x_max, y_max]` in source-frame pixels, when complete. */
  box?: [number, number, number, number];
}

/** How many detections the graph's model preview lists before "and N more". */
export const PREVIEW_DETECTION_LIMIT = 10;

/** Label shown for an entry that carries no label. */
export const UNLABELLED_DETECTION = "object";

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * The run's Detection_List, or `null` when the metadata carries none.
 *
 * `null` means "no Detection_List" (a non-detection model, or metadata that
 * is unavailable); an empty array means the detection model ran and found
 * nothing. Entries that are not objects or lack a finite numeric confidence
 * are skipped; every other entry keeps its position relative to the rest.
 */
export function runDetections(
  metadata: WorkflowExecutionMetadata | undefined | null,
): RunDetection[] | null {
  if (metadata === undefined || metadata === null) {
    return null;
  }
  if (typeof metadata !== "object" || Array.isArray(metadata)) {
    return null;
  }
  const raw = (metadata as Record<string, unknown>).detections;
  if (!Array.isArray(raw)) {
    return null;
  }
  const detections: RunDetection[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      continue;
    }
    const record = entry as Record<string, unknown>;
    if (!isFiniteNumber(record.confidence)) {
      continue;
    }
    const detection: RunDetection = {
      label:
        typeof record.label === "string" && record.label.length > 0
          ? record.label
          : UNLABELLED_DETECTION,
      confidence: record.confidence,
    };
    const { x_min: xMin, y_min: yMin, x_max: xMax, y_max: yMax } = record;
    if (
      isFiniteNumber(xMin) &&
      isFiniteNumber(yMin) &&
      isFiniteNumber(xMax) &&
      isFiniteNumber(yMax)
    ) {
      detection.box = [xMin, yMin, xMax, yMax];
    }
    if (typeof record.id === "string" && record.id.length > 0) {
      detection.id = record.id;
    }
    detections.push(detection);
  }
  return detections;
}

/** Confidence as a percentage with one decimal place, e.g. `93.5%`. */
export function formatConfidence(confidence: number): string {
  return `${(confidence * 100).toFixed(1)}%`;
}

/**
 * A box as integer source-frame pixel ranges, e.g. `x 92–170, y 202–255`, or
 * `-` when the entry has no complete box.
 */
export function formatBox(box?: [number, number, number, number]): string {
  if (!box) {
    return "-";
  }
  const [xMin, yMin, xMax, yMax] = box.map((value) => Math.round(value));
  return `x ${xMin}–${xMax}, y ${yMin}–${yMax}`;
}

/**
 * Per-label counts, ordered by label so the summary reads the same for every
 * run, e.g. `helmet 2 · human 5 · vest 2`. Empty for an empty list.
 */
export function detectionLabelSummary(detections: RunDetection[]): string {
  const counts = new Map<string, number>();
  for (const detection of detections) {
    counts.set(detection.label, (counts.get(detection.label) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([label, count]) => `${label} ${count}`)
    .join(" · ");
}
