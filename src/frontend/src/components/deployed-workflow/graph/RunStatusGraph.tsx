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

import * as React from "react";
import {
  Alert,
  Box,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import {
  getWorkflowExecution,
  getWorkflowExecutionNodeStatus,
  getWorkflowRegistrationGraph,
} from "api/WorkflowRegistrationAPI";
import { isExecutionActive } from "../presentation";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  edgeSegments,
  graphExtent,
  nodeVisual,
  normalizeNodePositions,
  shouldShowDetail,
} from "./graphGeometry";

/** Poll interval for the node-status query while the run is active (R7.5). */
export const NODE_STATUS_POLL_INTERVAL_MS = 2000;

/**
 * Run status graph screen (deployed-workflow-run-observability, Requirement 7).
 *
 * A lightweight, read-only, dependency-free renderer (design §5.3): it mirrors
 * the authored `workflow.json` graph by absolutely positioning node cards from
 * each node's `position {x,y}` and drawing connections as SVG lines behind the
 * cards. Each node is colored by its live Node_Run_Status (R7.3); failure and
 * warning nodes surface their detail on selection (R7.4). The node-status query
 * polls while the execution is active and stops at a terminal state (R7.5), at
 * which point the coloring is fully resolved (R7.6, backed by backend R3.6).
 * It renders purely from two device API calls, with no cloud access (R7.7).
 */
export default function RunStatusGraph(): JSX.Element {
  const { registrationId = "", executionId = "" } = useParams();
  const [selectedNodeId, setSelectedNodeId] = React.useState<string | null>(
    null,
  );

  // Fetch the execution so we know whether the run is still active and should
  // keep polling node status. Poll the execution itself while active so the
  // graph learns when the run reaches a terminal state.
  const executionQuery = useQuery({
    queryKey: ["getWorkflowExecution", executionId],
    queryFn: () => getWorkflowExecution(executionId),
    refetchInterval: (data) =>
      data && isExecutionActive(data) ? NODE_STATUS_POLL_INTERVAL_MS : false,
  });

  const active = executionQuery.data
    ? isExecutionActive(executionQuery.data)
    : false;

  // The authored graph (nodes + connections + positions). Static per run.
  const graphQuery = useQuery({
    queryKey: ["getWorkflowRegistrationGraph", registrationId],
    queryFn: () => getWorkflowRegistrationGraph(registrationId),
  });

  // The per-node status map, polled while the execution is active and stopped
  // once it reaches a terminal state (R7.5).
  const nodeStatusQuery = useQuery({
    queryKey: ["getWorkflowExecutionNodeStatus", executionId],
    queryFn: () => getWorkflowExecutionNodeStatus(executionId),
    refetchInterval: () => (active ? NODE_STATUS_POLL_INTERVAL_MS : false),
  });

  const graph = graphQuery.data;
  const statusMap = nodeStatusQuery.data ?? {};

  if (graphQuery.isError) {
    return (
      <ContentLayout header={<Header variant="h1">Run status</Header>}>
        <Container header={<Header variant="h2">Workflow graph</Header>}>
          <Alert type="error" header="Failed to load workflow graph">
            The workflow graph for this run could not be loaded.
          </Alert>
        </Container>
      </ContentLayout>
    );
  }

  if (!graph) {
    return (
      <ContentLayout header={<Header variant="h1">Run status</Header>}>
        <Container header={<Header variant="h2">Workflow graph</Header>}>
          <StatusIndicator type="loading">
            Loading run status for execution {executionId}.
          </StatusIndicator>
        </Container>
      </ContentLayout>
    );
  }

  // Normalize authored positions into the positive quadrant so nodes with
  // negative coordinates are not clipped out of view (the "missing node" bug).
  const nodes = normalizeNodePositions(graph.nodes ?? []);
  const connections = graph.connections ?? [];
  const visuals = nodes.map((node) => nodeVisual(node, statusMap));
  const segments = edgeSegments(nodes, connections);
  const extent = graphExtent(nodes);

  const selectedVisual = visuals.find((visual) => visual.id === selectedNodeId);

  const runIndicator = active ? (
    <StatusIndicator type="in-progress">Run in progress</StatusIndicator>
  ) : (
    <StatusIndicator type="success">Run finished</StatusIndicator>
  );

  return (
    <ContentLayout
      header={<Header variant="h1">Run status</Header>}
    >
      <SpaceBetween size="l">
        <Container
          header={
            <Header variant="h2" description="Each node is colored by its run status.">
              Workflow graph
            </Header>
          }
        >
          <SpaceBetween size="m">
            {runIndicator}
            <div
              data-testid="run-status-graph-canvas"
              style={{
                position: "relative",
                width: extent.width,
                height: extent.height,
                overflow: "auto",
                maxWidth: "100%",
              }}
            >
              {/* SVG edge layer sits behind the node cards. */}
              <svg
                data-testid="run-status-graph-edges"
                width={extent.width}
                height={extent.height}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  pointerEvents: "none",
                }}
              >
                {/* Arrowhead marker: points along each edge toward the "to"
                    node so the run order reads unambiguously. */}
                <defs>
                  <marker
                    id="run-status-arrow"
                    markerWidth={10}
                    markerHeight={8}
                    refX={8}
                    refY={3}
                    orient="auto"
                    markerUnits="strokeWidth"
                  >
                    <path d="M0,0 L8,3 L0,6 Z" fill="#879596" />
                  </marker>
                </defs>
                {segments.map((segment) => (
                  <line
                    key={segment.id}
                    data-testid={`edge-${segment.fromId}-${segment.toId}`}
                    x1={segment.x1}
                    y1={segment.y1}
                    x2={segment.x2}
                    y2={segment.y2}
                    stroke="#879596"
                    strokeWidth={2}
                    markerEnd="url(#run-status-arrow)"
                  />
                ))}
              </svg>

              {/* Absolutely-positioned node cards. */}
              {visuals.map((visual) => {
                const isSelected = visual.id === selectedNodeId;
                return (
                  <button
                    key={visual.id}
                    type="button"
                    data-testid={`node-${visual.id}`}
                    data-status={visual.status}
                    data-color={visual.color}
                    aria-pressed={isSelected}
                    title={
                      visual.detail
                        ? `${visual.id}: ${visual.detail}`
                        : `${visual.id} (${visual.status})`
                    }
                    onClick={(): void =>
                      setSelectedNodeId(isSelected ? null : visual.id)
                    }
                    style={{
                      position: "absolute",
                      left: visual.x,
                      top: visual.y,
                      width: NODE_WIDTH,
                      height: NODE_HEIGHT,
                      boxSizing: "border-box",
                      textAlign: "left",
                      cursor: "pointer",
                      background: "#ffffff",
                      border: `3px solid ${visual.color}`,
                      borderLeft: `8px solid ${visual.categoryColor}`,
                      borderRadius: 8,
                      boxShadow: isSelected
                        ? "0 0 0 2px #0972d3"
                        : "0 1px 2px rgba(0,0,0,0.15)",
                      padding: "6px 10px",
                      // A simple pulsing affordance for in-progress nodes.
                      animation: visual.inProgress
                        ? "runStatusPulse 1.2s ease-in-out infinite"
                        : undefined,
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 6,
                      }}
                    >
                      <span
                        style={{
                          fontWeight: 700,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {visual.id}
                      </span>
                      {visual.inProgress && <Spinner size="normal" />}
                    </div>
                    <div style={{ fontSize: 12, color: "#5f6b7a" }}>
                      {visual.type}
                    </div>
                    <div
                      data-testid={`node-status-${visual.id}`}
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: visual.color,
                      }}
                    >
                      {visual.status}
                    </div>
                  </button>
                );
              })}
              {/* Keyframes for the in-progress pulse; scoped, no global CSS. */}
              <style>
                {
                  "@keyframes runStatusPulse { 0% { opacity: 1; } 50% { opacity: 0.55; } 100% { opacity: 1; } }"
                }
              </style>
            </div>
          </SpaceBetween>
        </Container>

        {/* Detail panel: shows the error/warning detail for a selected
            failure/warning node (R7.4). */}
        {selectedVisual && shouldShowDetail(selectedVisual.status) && (
          <Alert
            data-testid="node-detail"
            type={selectedVisual.status === "failure" ? "error" : "warning"}
            header={`${selectedVisual.id} — ${selectedVisual.status}`}
          >
            {selectedVisual.detail ?? "No additional detail was recorded."}
          </Alert>
        )}
        {selectedVisual && !shouldShowDetail(selectedVisual.status) && (
          <Box data-testid="node-detail" variant="p">
            {selectedVisual.id} — {selectedVisual.status}
          </Box>
        )}
      </SpaceBetween>
    </ContentLayout>
  );
}
