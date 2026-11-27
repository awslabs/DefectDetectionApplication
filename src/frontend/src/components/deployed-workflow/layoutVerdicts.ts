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
 * Pure readers of a run's Layout Check verdicts (car-assembly-check
 * Requirement 7.1), as the Workflow_Executor merges them into the run
 * metadata under `layout.<nodeId>`:
 *
 * `{pass, pieces: {label: {status, iou, detections: [{id, class_label,
 * confidence, bounding_box, iou, outline_overlap}]}}, flagged: [id],
 * ignored: [{id, class_label, confidence, bounding_box, outline_overlap}],
 * unexpected_classes, expected_regions: {label: [[x, y], ...]},
 * frame: {width, height} | null, min_iou, min_confidence,
 * min_outline_overlap}`
 *
 * The metadata is arbitrary JSON, so everything here reads it defensively,
 * skips what does not fit, and never throws. Sections and pieces keep the
 * metadata's key order.
 */
import type { WorkflowExecutionMetadata } from "api/WorkflowRegistrationAPI";

export type PieceStatus = "ok" | "misplaced" | "missing" | "duplicate";

export type Box = [number, number, number, number];

export interface PieceDetection {
  id: string | null;
  classLabel: string;
  confidence: number;
  box: Box;
  iou: number | null;
  outlineOverlap: number | null;
}

export interface PieceRow {
  label: string;
  status: PieceStatus;
  iou: number | null;
  detections: PieceDetection[];
}

/** A qualifying instance the check ignored: it lies outside the car outline. */
export interface IgnoredRow {
  id: string | null;
  classLabel: string;
  confidence: number;
  box: Box;
  outlineOverlap: number | null;
}

export interface LayoutSection {
  nodeId: string;
  pass: boolean;
  pieces: PieceRow[];
  flagged: string[];
  ignored: IgnoredRow[];
  unexpectedClasses: string[];
  /** Normalized polygons (0..1) per class. */
  expectedRegions: Record<string, [number, number][]>;
  frame: { width: number; height: number } | null;
}

type JsonObject = Record<string, unknown>;

const STATUSES: ReadonlyArray<PieceStatus> = ["ok", "misplaced", "missing", "duplicate"];

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function numberOrNull(value: unknown): number | null {
  return isFiniteNumber(value) ? value : null;
}

function idOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function readBox(value: unknown): Box | null {
  if (!Array.isArray(value) || value.length !== 4 || !value.every(isFiniteNumber)) {
    return null;
  }
  return [value[0], value[1], value[2], value[3]];
}

function readIgnored(value: unknown): IgnoredRow | null {
  if (!isObject(value) || typeof value.class_label !== "string") {
    return null;
  }
  const box = readBox(value.bounding_box);
  if (box === null || !isFiniteNumber(value.confidence)) {
    return null;
  }
  return {
    id: idOrNull(value.id),
    classLabel: value.class_label,
    confidence: value.confidence,
    box,
    outlineOverlap: numberOrNull(value.outline_overlap),
  };
}

function readDetection(value: unknown): PieceDetection | null {
  const row = readIgnored(value);
  if (row === null) {
    return null;
  }
  return { ...row, iou: numberOrNull((value as JsonObject).iou) };
}

function readPieces(value: unknown): PieceRow[] {
  if (!isObject(value)) {
    return [];
  }
  const rows: PieceRow[] = [];
  for (const [label, piece] of Object.entries(value)) {
    if (!isObject(piece) || !STATUSES.includes(piece.status as PieceStatus)) {
      continue;
    }
    const detections = Array.isArray(piece.detections)
      ? piece.detections
          .map(readDetection)
          .filter((entry): entry is PieceDetection => entry !== null)
      : [];
    rows.push({
      label,
      status: piece.status as PieceStatus,
      iou: numberOrNull(piece.iou),
      detections,
    });
  }
  return rows;
}

function readRegions(value: unknown): Record<string, [number, number][]> {
  const regions: Record<string, [number, number][]> = {};
  if (!isObject(value)) {
    return regions;
  }
  for (const [label, ring] of Object.entries(value)) {
    if (!Array.isArray(ring) || ring.length < 3) {
      continue;
    }
    const points = ring.filter(
      (point): point is [number, number] =>
        Array.isArray(point) && point.length === 2 && point.every(isFiniteNumber),
    );
    if (points.length === ring.length) {
      regions[label] = points.map(([x, y]) => [x, y]);
    }
  }
  return regions;
}

function readFrame(value: unknown): { width: number; height: number } | null {
  if (!isObject(value)) {
    return null;
  }
  const { width, height } = value;
  return isFiniteNumber(width) && isFiniteNumber(height) && width > 0 && height > 0
    ? { width, height }
    : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry.length > 0)
    : [];
}

/** Every `layout.<nodeId>` section of the run with a boolean `pass`. */
export function readLayoutSections(
  metadata: WorkflowExecutionMetadata | undefined | null,
): LayoutSection[] {
  if (!isObject(metadata) || !isObject(metadata.layout)) {
    return [];
  }
  const sections: LayoutSection[] = [];
  for (const [nodeId, record] of Object.entries(metadata.layout)) {
    if (!isObject(record) || typeof record.pass !== "boolean") {
      continue;
    }
    sections.push({
      nodeId,
      pass: record.pass,
      pieces: readPieces(record.pieces),
      flagged: strings(record.flagged),
      ignored: Array.isArray(record.ignored)
        ? record.ignored.map(readIgnored).filter((row): row is IgnoredRow => row !== null)
        : [],
      unexpectedClasses: strings(record.unexpected_classes),
      expectedRegions: readRegions(record.expected_regions),
      frame: readFrame(record.frame),
    });
  }
  return sections;
}

/**
 * The key that matches a layout detection to a mask-layer entry (the run
 * preview's entries carry no Detection_ID; design DD8):
 * `${classLabel}|${x0}|${y0}|${x1}|${y1}`, each number with one decimal so
 * both sides round identically.
 */
export function instanceKey(entry: { classLabel: string; box: ReadonlyArray<number> }): string {
  return [entry.classLabel, ...entry.box.slice(0, 4).map((value) => Number(value).toFixed(1))].join(
    "|",
  );
}

/** The keys of the flagged detections of every failing section. */
export function flaggedInstanceKeys(sections: LayoutSection[]): Set<string> {
  const keys = new Set<string>();
  for (const section of sections) {
    if (section.pass) {
      continue;
    }
    const flagged = new Set(section.flagged);
    for (const piece of section.pieces) {
      for (const detection of piece.detections) {
        if (detection.id !== null && flagged.has(detection.id)) {
          keys.add(instanceKey(detection));
        }
      }
    }
  }
  return keys;
}

/** The keys of every section's ignored instances, whatever the verdict. */
export function ignoredInstanceKeys(sections: LayoutSection[]): Set<string> {
  const keys = new Set<string>();
  for (const section of sections) {
    for (const row of section.ignored) {
      keys.add(instanceKey(row));
    }
  }
  return keys;
}

/**
 * The outlines of every piece whose status is not `ok` in a failing
 * section, in frame pixels (its Expected_Region scaled by the section's
 * frame). A section without a frame contributes none.
 */
export function failedPieceOutlines(
  sections: LayoutSection[],
): { label: string; points: [number, number][] }[] {
  const outlines: { label: string; points: [number, number][] }[] = [];
  for (const section of sections) {
    const { frame } = section;
    if (section.pass || frame === null) {
      continue;
    }
    for (const piece of section.pieces) {
      const region = section.expectedRegions[piece.label];
      if (piece.status === "ok" || region === undefined) {
        continue;
      }
      outlines.push({
        label: piece.label,
        points: region.map(([x, y]) => [x * frame.width, y * frame.height]),
      });
    }
  }
  return outlines;
}
