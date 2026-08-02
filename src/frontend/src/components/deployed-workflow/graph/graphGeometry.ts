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
 * Pure presentational logic for the run-status graph (Requirement 7).
 *
 * This module is deliberately free of React/DOM so the status→color mapping
 * and the node/edge geometry are directly unit-testable. The component in
 * `RunStatusGraph.tsx` only wires these helpers to react-query and Cloudscape.
 *
 * The renderer is intentionally lightweight and dependency-free (design §5.3):
 * it positions node cards from the authored `workflow.json` positions and draws
 * SVG lines between node anchor points, so the edge bundle stays lean and the
 * screen renders offline (R7.7) with no graph library.
 */

import type {
  NodeStatusMap,
  WorkflowGraphConnection,
  WorkflowGraphNode,
} from "api/WorkflowRegistrationAPI";

/** The five recognized per-node run statuses (Node_Run_Status, R3). */
export type RunStatus =
  | "pending"
  | "running"
  | "success"
  | "warning"
  | "failure";

// --------------------------------------------------------------------------
// Status → color (R7.3)
// --------------------------------------------------------------------------

/**
 * Status→color map (R7.3): green for `success`, red for `failure`, yellow for
 * `warning`, blue for the in-progress `running` affordance, and a neutral grey
 * for `pending`/unknown. Kept as constants so the mapping is testable.
 */
export const STATUS_COLOR: Record<RunStatus, string> = {
  success: "#037f0c", // green
  failure: "#d13212", // red
  warning: "#f2c200", // yellow
  running: "#0972d3", // blue (in-progress affordance)
  pending: "#5f6b7a", // neutral grey
};

/** Neutral color used for `pending` and any unknown/unrecognized status. */
export const NEUTRAL_COLOR = STATUS_COLOR.pending;

/**
 * Node base-styling palette mirrored from the Portal's `CATEGORY_META`
 * (`edge-cv-portal/frontend/src/pages/workflows/builderGraph.ts`), copied here
 * as local constants so the device bundle never imports across the
 * portal/edge boundary. Used only for a subtle category accent; the node's
 * primary coloring comes from its run status.
 */
export const CATEGORY_COLOR: Record<string, string> = {
  input: "#037f0c",
  preprocessing: "#0972d3",
  inference: "#7d3ac1",
  post_processing: "#8a6116",
  output: "#d13212",
};

/** Fallback category accent for node types with no known category. */
export const UNKNOWN_CATEGORY_COLOR = "#5f6b7a";

/** True when `value` is one of the five recognized run statuses. */
export function isRunStatus(value: string | undefined): value is RunStatus {
  return (
    value === "pending" ||
    value === "running" ||
    value === "success" ||
    value === "warning" ||
    value === "failure"
  );
}

/**
 * The display color for a raw status string. Unknown/undefined statuses fall
 * back to the neutral color (treated like `pending`), so the graph is always
 * fully colored (R7.6).
 */
export function statusColor(status: string | undefined): string {
  return isRunStatus(status) ? STATUS_COLOR[status] : NEUTRAL_COLOR;
}

/** True for the in-progress status that gets an animated/spinner affordance. */
export function isInProgress(status: string | undefined): boolean {
  return status === "running";
}

/**
 * Whether a node's detail (error/warning message) should be surfaced on
 * hover/selection (R7.4): only `failure` and `warning` carry a detail worth
 * showing.
 */
export function shouldShowDetail(status: string | undefined): boolean {
  return status === "failure" || status === "warning";
}

// --------------------------------------------------------------------------
// Node visual model
// --------------------------------------------------------------------------

/** Everything the component needs to render one node card. */
export interface NodeVisual {
  id: string;
  type: string;
  x: number;
  y: number;
  /** The resolved run status (defaults to `pending` when unknown). */
  status: string;
  /** The status color driving the node's border/accent (R7.3). */
  color: string;
  /** Subtle category accent color mirrored from the portal palette. */
  categoryColor: string;
  /** True while the node is running (drives the spinner/pulse affordance). */
  inProgress: boolean;
  /** The error/warning detail, present only for failure/warning nodes (R7.4). */
  detail?: string;
}

/**
 * Resolve the run status and detail for a single node from the node-status
 * map. Nodes absent from the map (or with an unrecognized status) resolve to
 * `pending`/neutral so the graph is always fully colored.
 */
export function nodeVisual(
  node: WorkflowGraphNode,
  statusMap: NodeStatusMap,
): NodeVisual {
  const entry = statusMap[node.id];
  const rawStatus = entry?.status;
  const status = isRunStatus(rawStatus) ? rawStatus : "pending";
  const detail =
    shouldShowDetail(status) && entry?.detail ? entry.detail : undefined;
  return {
    id: node.id,
    type: node.type,
    x: node.position?.x ?? 0,
    y: node.position?.y ?? 0,
    status,
    color: statusColor(status),
    categoryColor: CATEGORY_COLOR[node.type] ?? UNKNOWN_CATEGORY_COLOR,
    inProgress: isInProgress(status),
    ...(detail !== undefined ? { detail } : {}),
  };
}

// --------------------------------------------------------------------------
// Geometry (absolutely-positioned cards + SVG edges)
// --------------------------------------------------------------------------

/** Node card dimensions, in px, used for layout and edge anchoring. */
export const NODE_WIDTH = 180;
export const NODE_HEIGHT = 68;
/** Padding around the graph bounds so cards near the edge are not clipped. */
export const GRAPH_PADDING = 40;

/** The center point of a node card given its top-left position. */
export function nodeCenter(node: { position?: { x: number; y: number } }): {
  cx: number;
  cy: number;
} {
  const x = node.position?.x ?? 0;
  const y = node.position?.y ?? 0;
  return { cx: x + NODE_WIDTH / 2, cy: y + NODE_HEIGHT / 2 };
}

/**
 * The point on the border of the node card centered at `center` in the
 * direction of `toward`. Used to trim edge segments so they start/end at the
 * card boundary rather than the (card-occluded) center — which keeps the
 * directional arrowhead visible in the gap between two cards. Falls back to the
 * center for coincident points (avoids a divide-by-zero on overlapping nodes).
 */
export function borderPoint(
  center: { cx: number; cy: number },
  toward: { cx: number; cy: number },
): { x: number; y: number } {
  const dx = toward.cx - center.cx;
  const dy = toward.cy - center.cy;
  if (dx === 0 && dy === 0) {
    return { x: center.cx, y: center.cy };
  }
  const halfWidth = NODE_WIDTH / 2;
  const halfHeight = NODE_HEIGHT / 2;
  const scale = 1 / Math.max(Math.abs(dx) / halfWidth, Math.abs(dy) / halfHeight);
  return { x: center.cx + dx * scale, y: center.cy + dy * scale };
}

/**
 * Resolve a connection endpoint to its node id. DDA's authored `workflow.json`
 * expresses endpoints as objects (`{ node, port }`), which is the canonical
 * shape the device serves; a bare string (`"n1"`) is also accepted so simpler
 * authored graphs and fixtures keep working. Anything else yields `null`.
 */
export function endpointNodeId(value: unknown): string | null {
  if (typeof value === "string") {
    return value;
  }
  if (value !== null && typeof value === "object") {
    const node = (value as { node?: unknown }).node;
    if (typeof node === "string") {
      return node;
    }
  }
  return null;
}

/** The two endpoint node ids of a connection, or null when malformed. */
export function connectionEndpoints(
  connection: WorkflowGraphConnection,
): { from: string; to: string } | null {
  const from = endpointNodeId((connection as { from?: unknown }).from);
  const to = endpointNodeId((connection as { to?: unknown }).to);
  if (from !== null && to !== null) {
    return { from, to };
  }
  return null;
}

/** A single SVG edge segment between two node anchor points. */
export interface EdgeSegment {
  id: string;
  fromId: string;
  toId: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/**
 * Compute the SVG line segments for every connection whose endpoints both
 * resolve to a node on the graph. Each segment is trimmed to the two cards'
 * borders (via {@link borderPoint}) so a directional arrowhead drawn at the
 * `to` end is visible in the gap between cards and communicates run order.
 * Connections referencing a missing node (or with a malformed shape) are
 * skipped rather than drawn to (0,0), so a partial graph never renders
 * spurious edges.
 */
export function edgeSegments(
  nodes: WorkflowGraphNode[],
  connections: WorkflowGraphConnection[],
): EdgeSegment[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const segments: EdgeSegment[] = [];
  connections.forEach((connection, index) => {
    const endpoints = connectionEndpoints(connection);
    if (endpoints === null) {
      return;
    }
    const fromNode = byId.get(endpoints.from);
    const toNode = byId.get(endpoints.to);
    if (fromNode === undefined || toNode === undefined) {
      return;
    }
    const from = nodeCenter(fromNode);
    const to = nodeCenter(toNode);
    // Trim both ends to the card borders so the segment (and its arrowhead)
    // is drawn in the visible gap between the two cards, not under them.
    const start = borderPoint(from, to);
    const end = borderPoint(to, from);
    segments.push({
      id: `${endpoints.from}->${endpoints.to}#${index}`,
      fromId: endpoints.from,
      toId: endpoints.to,
      x1: start.x,
      y1: start.y,
      x2: end.x,
      y2: end.y,
    });
  });
  return segments;
}

/**
 * Shift every node so the minimum x/y across the graph maps to the padding
 * origin. Authored `workflow.json` positions can be negative (the Portal
 * builder canvas places nodes anywhere), and the renderer positions cards with
 * absolute `left/top` in a container whose origin is (0,0) — so a node at a
 * negative coordinate renders above/left of the visible area and is clipped
 * (the "missing node" bug). Normalizing translates the whole graph into the
 * positive quadrant with symmetric padding, preserving relative layout and
 * every connection. Returns new node objects (input is not mutated); an empty
 * input yields an empty array.
 */
export function normalizeNodePositions(
  nodes: WorkflowGraphNode[],
): WorkflowGraphNode[] {
  if (nodes.length === 0) {
    return [];
  }
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  for (const node of nodes) {
    minX = Math.min(minX, node.position?.x ?? 0);
    minY = Math.min(minY, node.position?.y ?? 0);
  }
  const offsetX = GRAPH_PADDING - minX;
  const offsetY = GRAPH_PADDING - minY;
  return nodes.map((node) => ({
    ...node,
    position: {
      x: (node.position?.x ?? 0) + offsetX,
      y: (node.position?.y ?? 0) + offsetY,
    },
  }));
}

/** The overall SVG/container extent needed to fit every node card. */
export function graphExtent(nodes: WorkflowGraphNode[]): {
  width: number;
  height: number;
} {
  if (nodes.length === 0) {
    return { width: NODE_WIDTH + GRAPH_PADDING, height: NODE_HEIGHT + GRAPH_PADDING };
  }
  let maxRight = 0;
  let maxBottom = 0;
  for (const node of nodes) {
    const x = node.position?.x ?? 0;
    const y = node.position?.y ?? 0;
    maxRight = Math.max(maxRight, x + NODE_WIDTH);
    maxBottom = Math.max(maxBottom, y + NODE_HEIGHT);
  }
  return {
    width: maxRight + GRAPH_PADDING,
    height: maxBottom + GRAPH_PADDING,
  };
}
