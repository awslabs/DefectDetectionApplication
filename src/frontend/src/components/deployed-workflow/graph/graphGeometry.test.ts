/*
 * Unit tests for the run-status graph pure helpers (task 13.1).
 *
 * These cover the status→color mapping (R7.3), the failure/warning detail
 * gating (R7.4), the fully-resolved coloring for unknown/missing status
 * (R7.6), and the node/edge geometry used by the lightweight SVG renderer.
 */

import type {
  NodeStatusMap,
  WorkflowGraphNode,
} from "api/WorkflowRegistrationAPI";
import {
  CATEGORY_COLOR,
  GRAPH_PADDING,
  NEUTRAL_COLOR,
  NODE_HEIGHT,
  NODE_WIDTH,
  STATUS_COLOR,
  connectionEndpoints,
  edgeSegments,
  graphExtent,
  isInProgress,
  nodeCenter,
  nodeVisual,
  normalizeNodePositions,
  shouldShowDetail,
  statusColor,
} from "./graphGeometry";

function node(
  id: string,
  x: number,
  y: number,
  type = "generic",
): WorkflowGraphNode {
  return { id, type, position: { x, y } };
}

describe("statusColor (R7.3)", () => {
  it("maps success to green, failure to red, warning to yellow, running to blue", () => {
    expect(statusColor("success")).toBe(STATUS_COLOR.success);
    expect(statusColor("failure")).toBe(STATUS_COLOR.failure);
    expect(statusColor("warning")).toBe(STATUS_COLOR.warning);
    expect(statusColor("running")).toBe(STATUS_COLOR.running);
  });

  it("maps pending and unknown/undefined to the neutral color (R7.6)", () => {
    expect(statusColor("pending")).toBe(NEUTRAL_COLOR);
    expect(statusColor("bogus")).toBe(NEUTRAL_COLOR);
    expect(statusColor(undefined)).toBe(NEUTRAL_COLOR);
  });

  it("uses distinct colors for each terminal status", () => {
    const colors = new Set([
      STATUS_COLOR.success,
      STATUS_COLOR.failure,
      STATUS_COLOR.warning,
      STATUS_COLOR.running,
      STATUS_COLOR.pending,
    ]);
    expect(colors.size).toBe(5);
  });
});

describe("isInProgress / shouldShowDetail", () => {
  it("only running is in-progress", () => {
    expect(isInProgress("running")).toBe(true);
    expect(isInProgress("success")).toBe(false);
    expect(isInProgress("pending")).toBe(false);
    expect(isInProgress(undefined)).toBe(false);
  });

  it("only failure and warning surface a detail (R7.4)", () => {
    expect(shouldShowDetail("failure")).toBe(true);
    expect(shouldShowDetail("warning")).toBe(true);
    expect(shouldShowDetail("success")).toBe(false);
    expect(shouldShowDetail("running")).toBe(false);
    expect(shouldShowDetail("pending")).toBe(false);
  });
});

describe("nodeVisual", () => {
  const nodes = [node("n1", 0, 0, "input"), node("n2", 100, 50, "output")];
  const statusMap: NodeStatusMap = {
    n1: { status: "failure", detail: "sensor timeout" },
    n2: { status: "success" },
  };

  it("resolves status, color and category accent for a node", () => {
    const v1 = nodeVisual(nodes[0], statusMap);
    expect(v1.status).toBe("failure");
    expect(v1.color).toBe(STATUS_COLOR.failure);
    expect(v1.categoryColor).toBe(CATEGORY_COLOR.input);
    expect(v1.detail).toBe("sensor timeout");
    expect(v1.inProgress).toBe(false);
  });

  it("only exposes detail for failure/warning nodes (R7.4)", () => {
    // success node carries no detail even if the map had one
    const v2 = nodeVisual(nodes[1], {
      n2: { status: "success", detail: "ignored" },
    });
    expect(v2.detail).toBeUndefined();
  });

  it("defaults missing nodes to pending/neutral so the graph is fully colored (R7.6)", () => {
    const v = nodeVisual(node("orphan", 0, 0), {});
    expect(v.status).toBe("pending");
    expect(v.color).toBe(NEUTRAL_COLOR);
  });

  it("flags running nodes as in-progress", () => {
    const v = nodeVisual(node("n3", 0, 0), { n3: { status: "running" } });
    expect(v.inProgress).toBe(true);
    expect(v.color).toBe(STATUS_COLOR.running);
  });
});

describe("normalizeNodePositions (missing-node / negative-coordinate fix)", () => {
  it("shifts negative coordinates into view at the padding origin", () => {
    // Mirrors the real failing workflow: n1/n3 have negative coordinates and
    // were clipped out of the visible canvas.
    const nodes = [
      node("n1", -17.9, -112.5, "folder_source"),
      node("n2", 250, 0, "model_inference"),
      node("n3", 500, -80, "capture"),
      node("n4", 500, 100, "opcua_write"),
    ];
    const out = normalizeNodePositions(nodes);
    // Every normalized position is within the visible (>= padding) quadrant.
    for (const n of out) {
      expect(n.position.x).toBeGreaterThanOrEqual(GRAPH_PADDING);
      expect(n.position.y).toBeGreaterThanOrEqual(GRAPH_PADDING);
    }
    // n1 holds both the min x (-17.9) and the min y (-112.5), so it lands
    // exactly at the padding origin; the others shift by the same offset.
    const byId = Object.fromEntries(out.map((n) => [n.id, n.position]));
    expect(byId["n1"].x).toBeCloseTo(GRAPH_PADDING);
    expect(byId["n1"].y).toBeCloseTo(GRAPH_PADDING);
    // n3's y (-80) shifts by offsetY = 40 - (-112.5) = 152.5 -> 72.5.
    expect(byId["n3"].y).toBeCloseTo(72.5);
  });

  it("preserves relative layout (all nodes shift by the same offset)", () => {
    const nodes = [node("a", -10, -20), node("b", 90, 30)];
    const out = normalizeNodePositions(nodes);
    // Relative distances are unchanged.
    expect(out[1].position.x - out[0].position.x).toBe(100);
    expect(out[1].position.y - out[0].position.y).toBe(50);
  });

  it("does not mutate the input nodes", () => {
    const nodes = [node("a", -5, -5)];
    normalizeNodePositions(nodes);
    expect(nodes[0].position).toEqual({ x: -5, y: -5 });
  });

  it("leaves an already-positive graph offset only by padding", () => {
    const nodes = [node("a", 100, 200)];
    const out = normalizeNodePositions(nodes);
    // min is (100,200) -> maps to (padding,padding).
    expect(out[0].position).toEqual({ x: GRAPH_PADDING, y: GRAPH_PADDING });
  });

  it("returns an empty array for no nodes", () => {
    expect(normalizeNodePositions([])).toEqual([]);
  });

  it("treats missing position as origin (0,0)", () => {
    const out = normalizeNodePositions([
      { id: "a", type: "generic" } as WorkflowGraphNode,
    ]);
    expect(out[0].position).toEqual({ x: GRAPH_PADDING, y: GRAPH_PADDING });
  });
});

describe("geometry", () => {
  it("computes the node center from its top-left position", () => {
    expect(nodeCenter(node("n", 10, 20))).toEqual({
      cx: 10 + NODE_WIDTH / 2,
      cy: 20 + NODE_HEIGHT / 2,
    });
  });

  it("parses well-formed connection endpoints and rejects malformed ones", () => {
    // Bare-string form (simple authored graphs / fixtures).
    expect(connectionEndpoints({ from: "a", to: "b" })).toEqual({
      from: "a",
      to: "b",
    });
    // Canonical DDA object form ({ node, port }) served by the device.
    expect(
      connectionEndpoints({
        from: { node: "n1", port: "out" },
        to: { node: "n2", port: "in" },
      }),
    ).toEqual({ from: "n1", to: "n2" });
    // Mixed / partial / malformed shapes are rejected.
    expect(connectionEndpoints({ from: "a" })).toBeNull();
    expect(connectionEndpoints({ from: { port: "out" }, to: "b" })).toBeNull();
    expect(connectionEndpoints({})).toBeNull();
  });

  it("builds edge segments from object-form endpoints ({node, port})", () => {
    const nodes = [node("n1", 0, 0), node("n2", 200, 0)];
    const segments = edgeSegments(nodes, [
      { from: { node: "n1", port: "out" }, to: { node: "n2", port: "in" } },
    ]);
    expect(segments).toHaveLength(1);
    expect(segments[0].fromId).toBe("n1");
    expect(segments[0].toId).toBe("n2");
  });

  it("builds edge segments between node centers, skipping dangling refs", () => {
    const nodes = [node("n1", 0, 0), node("n2", 200, 0)];
    const segments = edgeSegments(nodes, [
      { from: "n1", to: "n2" },
      { from: "n1", to: "missing" },
      { from: "n1" },
    ]);
    expect(segments).toHaveLength(1);
    const [seg] = segments;
    expect(seg.fromId).toBe("n1");
    expect(seg.toId).toBe("n2");
    // Endpoints are trimmed to the card borders (not the centers) so the
    // arrowhead is visible between cards. For two horizontally-aligned nodes
    // the source end sits on n1's right edge and the target end on n2's left.
    expect(seg.x1).toBe(NODE_WIDTH);
    expect(seg.x2).toBe(200);
    expect(seg.y1).toBe(NODE_HEIGHT / 2);
    expect(seg.y2).toBe(NODE_HEIGHT / 2);
  });

  it("sizes the graph extent to fit every node card plus padding", () => {
    const nodes = [node("n1", 0, 0), node("n2", 300, 120)];
    expect(graphExtent(nodes)).toEqual({
      width: 300 + NODE_WIDTH + GRAPH_PADDING,
      height: 120 + NODE_HEIGHT + GRAPH_PADDING,
    });
  });

  it("returns a minimal extent for an empty graph", () => {
    expect(graphExtent([])).toEqual({
      width: NODE_WIDTH + GRAPH_PADDING,
      height: NODE_HEIGHT + GRAPH_PADDING,
    });
  });
});
