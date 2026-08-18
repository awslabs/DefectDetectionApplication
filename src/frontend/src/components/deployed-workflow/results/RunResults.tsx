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

import {
  Badge,
  Box,
  ColumnLayout,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import {
  getWorkflowExecutionMetadata,
  getWorkflowExecutionOverlay,
  getWorkflowExecutionResults,
  workflowExecutionNodeImageUrl,
  workflowExecutionOutputImageUrl,
} from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecutionMetadata,
  WorkflowExecutionResultImage,
} from "api/WorkflowRegistrationAPI";
import useAuth from "components/auth/authHook";
import InteractableImage from "components/live-result/InteractableImage";
import RefreshDisplayActions from "components/live-result/RefreshDisplayActions";
import {
  getMaskBackgroundColor,
  getMaskImageProp,
} from "components/live-result/helpers";
import { resultLayoutStyle } from "components/live-result/styles";

// --------------------------------------------------------------------------
// Inference-node sections (vlm-bedrock-parity Requirement 4.3, 4.2)
// --------------------------------------------------------------------------

/**
 * Display label for a node input port. The two shipped ports get the
 * Bedrock-parity wording; any future port falls back to its own name, so the
 * section stays port-generic like the backend listing.
 */
function portLabel(port: string): string {
  if (port === "in") {
    return "Input";
  }
  if (port === "reference") {
    return "Reference";
  }
  return port;
}

/** Node frames grouped by node, preserving the backend's ordering. */
function groupByNode(
  images: WorkflowExecutionResultImage[],
): { nodeId: string; ports: string[] }[] {
  const groups: { nodeId: string; ports: string[] }[] = [];
  for (const image of images) {
    if (image.kind !== "node" || !image.nodeId || !image.port) {
      continue;
    }
    const existing = groups.find((group) => group.nodeId === image.nodeId);
    if (existing) {
      if (!existing.ports.includes(image.port)) {
        existing.ports.push(image.port);
      }
    } else {
      groups.push({ nodeId: image.nodeId, ports: [image.port] });
    }
  }
  return groups;
}

/** Defensive read of `metadata.{section}.{nodeId}` as an object. */
function nodeRecord(
  metadata: WorkflowExecutionMetadata | undefined,
  section: string,
  nodeId: string,
): Record<string, unknown> | undefined {
  const bucket = metadata?.[section];
  if (bucket === null || typeof bucket !== "object" || Array.isArray(bucket)) {
    return undefined;
  }
  const entry = (bucket as Record<string, unknown>)[nodeId];
  if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
    return undefined;
  }
  return entry as Record<string, unknown>;
}

/** A non-empty string field of a node record, or undefined. */
function stringField(
  record: Record<string, unknown> | undefined,
  field: string,
): string | undefined {
  const value = record?.[field];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * The text the node returned, from either inference node type:
 * `llm.{nodeId}.generated_text` or `bedrock.{nodeId}.text`. A recorded node
 * error is surfaced instead, so a failed node reads as a message rather than
 * an empty section.
 */
export function nodeTextOutcome(
  metadata: WorkflowExecutionMetadata | undefined,
  nodeId: string,
): { text?: string; error?: string } {
  const llm = nodeRecord(metadata, "llm", nodeId);
  const bedrock = nodeRecord(metadata, "bedrock", nodeId);
  const text =
    stringField(llm, "generated_text") ?? stringField(bedrock, "text");
  if (text !== undefined) {
    return { text };
  }
  const error = stringField(llm, "error") ?? stringField(bedrock, "error");
  return error !== undefined ? { error } : {};
}

/** The run's flat anomaly verdict, or null when the run carries none. */
export function runVerdict(
  metadata: WorkflowExecutionMetadata | undefined,
): boolean | null {
  const value = metadata?.is_anomalous;
  return typeof value === "boolean" ? value : null;
}

/** One node frame: labeled image that degrades to a message on a 404. */
function NodeFrame({
  executionId,
  nodeId,
  port,
  token,
}: {
  executionId: string;
  nodeId: string;
  port: string;
  token?: string;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const label = portLabel(port);
  const captionId = `node-frame-${nodeId}-${port}`;

  return (
    <figure style={{ margin: 0 }}>
      <figcaption id={captionId}>
        <Box variant="awsui-key-label">{label}</Box>
      </figcaption>
      {failed ? (
        <StatusIndicator type="info">
          {`The ${label.toLowerCase()} frame is unavailable.`}
        </StatusIndicator>
      ) : (
        <img
          src={workflowExecutionNodeImageUrl(executionId, nodeId, port, token)}
          alt={`${label} frame sent to inference node ${nodeId}`}
          aria-describedby={captionId}
          style={{ maxWidth: "100%" }}
          onError={(): void => setFailed(true)}
        />
      )}
    </figure>
  );
}

/**
 * One section per inference node that persisted frames: its 1-2 sent frames
 * side by side labeled Input/Reference, the run's verdict badge when the run
 * metadata carries `is_anomalous`, and the node's returned text below.
 * Driven only by `(nodeId, port)` entries and the metadata record, so
 * `llm_inference` and `bedrock_inference` render through this same path.
 */
function NodeSection({
  executionId,
  nodeId,
  ports,
  metadata,
  metadataLoading,
  token,
}: {
  executionId: string;
  nodeId: string;
  ports: string[];
  metadata: WorkflowExecutionMetadata | undefined;
  metadataLoading: boolean;
  token?: string;
}): JSX.Element {
  const verdict = runVerdict(metadata);
  const outcome = nodeTextOutcome(metadata, nodeId);

  return (
    <Container
      data-testid={`node-section-${nodeId}`}
      header={
        <Header
          variant="h2"
          actions={
            verdict === null ? undefined : (
              <Badge color={verdict ? "red" : "green"}>
                {verdict ? "Anomalous" : "Not anomalous"}
              </Badge>
            )
          }
        >
          {`Images sent to ${nodeId}`}
        </Header>
      }
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={ports.length > 1 ? 2 : 1}>
          {ports.map((port) => (
            <NodeFrame
              key={port}
              executionId={executionId}
              nodeId={nodeId}
              port={port}
              token={token}
            />
          ))}
        </ColumnLayout>
        {metadataLoading ? (
          <StatusIndicator type="loading">Loading model output</StatusIndicator>
        ) : outcome.text !== undefined ? (
          <Box variant="p" data-testid={`node-text-${nodeId}`}>
            {outcome.text}
          </Box>
        ) : outcome.error !== undefined ? (
          <StatusIndicator type="warning">{outcome.error}</StatusIndicator>
        ) : (
          <StatusIndicator type="info">
            This node returned no text output.
          </StatusIndicator>
        )}
      </SpaceBetween>
    </Container>
  );
}

/**
 * Run results screen (deployed-workflow-run-observability, Requirement 5).
 *
 * Mirrors `result-history/ResultDetailsCardDisplay.tsx`: it renders the run's
 * base output image via the shared `InteractableImage` component and reuses the
 * exact overlay show/hide toggle (`RefreshDisplayActions`) and mask helpers
 * (`getMaskImageProp` / `getMaskBackgroundColor`) the "run inference" results
 * screen uses (R5.4, R5.6). When the run produced a mask overlay the toggle is
 * shown; otherwise the plain base image is displayed with no toggle (R5.5).
 * A load failure / no-results run renders a clear Cloudscape state rather than
 * a broken image or crash (R5.7).
 *
 * vlm-bedrock-parity Requirements 4.3 / 4.2 add, below that untouched output
 * container, one section per inference node that persisted frames: the 1-2
 * frames the node actually sent, side by side labeled Input/Reference, the
 * run's verdict badge when the run metadata carries `is_anomalous`, and the
 * node's returned text. The sections are driven purely by the additive
 * `{kind: "node", nodeId, port}` entries of the `/results` payload, so
 * `llm_inference` and `bedrock_inference` present identically. The `output`
 * entry is conditional now, so a node-image-only run renders node sections
 * with no output container.
 */
export default function RunResults(): JSX.Element {
  const { executionId = "" } = useParams();
  const [showMask, setShowMask] = useState(true);
  const { token, authEnabled } = useAuth();

  const resultsQuery = useQuery({
    queryKey: ["getWorkflowExecutionResults", executionId],
    queryFn: () => getWorkflowExecutionResults(executionId),
  });

  const results = resultsQuery.data;
  // The output image carries the overlay availability; only fetch the overlay
  // (base64 mask + background) when the run actually produced one.
  const hasOverlay = !!results?.images?.some((image) => image.hasOverlay);
  // The `output` entry is now conditional (it appears only when the base
  // artifact exists), so a node-image-only run has none.
  const hasOutputImage = !!results?.images?.some(
    (image) => image.kind === "output",
  );
  const nodeGroups = groupByNode(results?.images ?? []);

  const overlayQuery = useQuery({
    queryKey: ["getWorkflowExecutionOverlay", executionId],
    queryFn: () => getWorkflowExecutionOverlay(executionId),
    enabled: hasOverlay,
  });

  // The node sections show each node's returned text and the run verdict, both
  // of which live in the run metadata; only fetched when node frames exist.
  const metadataQuery = useQuery({
    queryKey: ["getWorkflowExecutionMetadata", executionId],
    queryFn: () => getWorkflowExecutionMetadata(executionId),
    enabled: nodeGroups.length > 0,
  });

  const header = <Header variant="h1">Run results</Header>;

  const renderState = (content: JSX.Element): JSX.Element => (
    <ContentLayout header={header}>
      <Container header={<Header variant="h2">Output images</Header>}>
        {content}
      </Container>
    </ContentLayout>
  );

  if (resultsQuery.isLoading) {
    return renderState(<Spinner size="big" />);
  }

  // Load failure, a run with no viewable image results, or a run that reports
  // results but lists no image at all: clear, non-crashing state (R5.7 / R5.2).
  if (
    resultsQuery.isError ||
    !results ||
    !results.hasImageResults ||
    (!hasOutputImage && nodeGroups.length === 0)
  ) {
    return renderState(
      <StatusIndicator type={resultsQuery.isError ? "error" : "info"}>
        {resultsQuery.isError
          ? "Results unavailable for this run."
          : "This run produced no viewable image results."}
      </StatusIndicator>,
    );
  }

  const imageSrc = workflowExecutionOutputImageUrl(
    executionId,
    authEnabled ? token : undefined,
  );

  // Reuse the exact overlay pipeline: base64 mask + background -> mask prop.
  const maskImage = overlayQuery.data?.maskImage ?? null;
  const backgroundColorProp = getMaskBackgroundColor(
    overlayQuery.data?.maskBackground ?? null,
  );
  const maskImageProp = getMaskImageProp(maskImage, backgroundColorProp);

  const extraActions = (
    <RefreshDisplayActions
      showAnomalyMaskToggle={!!maskImage}
      onClickAnomalyMaskToggle={(checked): void => setShowMask(checked)}
      anomalyMaskToggleChecked={!!showMask}
      // Feedback flag toggle is not applicable to deployed-workflow runs.
      showFlagForReviewToggle={false}
    />
  );

  return (
    <ContentLayout header={header}>
      <SpaceBetween size="l">
        {hasOutputImage && (
          <Container header={<Header variant="h2">Output images</Header>}>
            <SpaceBetween size="l">
              {overlayQuery.isError && (
                <Box variant="p" color="text-status-warning">
                  The overlay could not be loaded; showing the base image.
                </Box>
              )}
              <div className={resultLayoutStyle}>
                <InteractableImage
                  imageSrc={imageSrc}
                  {...maskImageProp}
                  showMask={!!showMask}
                  extraActions={extraActions}
                />
              </div>
            </SpaceBetween>
          </Container>
        )}
        {nodeGroups.map((group) => (
          <NodeSection
            key={group.nodeId}
            executionId={executionId}
            nodeId={group.nodeId}
            ports={group.ports}
            metadata={metadataQuery.data}
            metadataLoading={metadataQuery.isLoading}
            token={authEnabled ? token : undefined}
          />
        ))}
      </SpaceBetween>
    </ContentLayout>
  );
}
