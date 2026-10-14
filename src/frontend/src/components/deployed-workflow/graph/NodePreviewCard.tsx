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
  Header,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import { Link } from "react-router-dom";

import type { PreviewViewModel } from "./previewModel";
import {
  PREVIEW_DETECTION_LIMIT,
  formatConfidence,
} from "../detections";
import type { RunDetection } from "../detections";

/** In-flight run placeholder message (R3.1). */
export const PENDING_MESSAGE =
  "No output yet — the run is still in progress.";

/** Missing-data fallback message (R3.3). */
export const UNAVAILABLE_MESSAGE = "No preview is available for this node.";

/** Empty Detection_List message (run-detection-visibility R3.2). */
export const NO_DETECTIONS_MESSAGE = "No objects were detected.";

/** Thumbnail box shared by the capture and detection previews. */
const THUMBNAIL_STYLE: React.CSSProperties = {
  maxWidth: 320,
  maxHeight: 200,
  objectFit: "contain",
};

/**
 * The compact detection list of a model_inference preview: the first
 * PREVIEW_DETECTION_LIMIT objects as "label — 93.5%", then how many more the
 * run found (run-detection-visibility R3.2). The list is the run's, so it is
 * labelled as such (design D6).
 */
function DetectionList({
  detections,
}: {
  detections: RunDetection[];
}): JSX.Element {
  const shown = detections.slice(0, PREVIEW_DETECTION_LIMIT);
  const remaining = detections.length - shown.length;
  return (
    <div data-testid="preview-detections">
      <Box variant="awsui-key-label">
        {`Objects detected in this run (${detections.length})`}
      </Box>
      {detections.length === 0 ? (
        <Box variant="p">{NO_DETECTIONS_MESSAGE}</Box>
      ) : (
        <ul style={{ margin: 0, paddingInlineStart: 20 }}>
          {shown.map((detection, index) => (
            <li key={`${index}:${detection.id ?? ""}`}>
              {`${detection.label} — ${formatConfidence(detection.confidence)}`}
            </li>
          ))}
        </ul>
      )}
      {remaining > 0 && (
        <Box
          data-testid="preview-detections-more"
          variant="small"
          color="text-body-secondary"
        >
          {`and ${remaining} more`}
        </Box>
      )}
    </div>
  );
}

export interface NodePreviewCardProps {
  /** The selected output node's id (card header + failure alert header). */
  nodeId: string;
  /** Route params used to build the Run_Results_Page link (R1.3). */
  registrationId: string;
  executionId: string;
  /** The precomputed preview view-model (previewModel.ts). */
  viewModel: PreviewViewModel;
}

/** The fallback message body, reused by `unavailable` and image `onError`. */
function Unavailable(): JSX.Element {
  return (
    <Box data-testid="preview-unavailable" variant="p">
      {UNAVAILABLE_MESSAGE}
    </Box>
  );
}

/**
 * Presentational output preview card (output-node-preview-popover spec,
 * design "NodePreviewCard.tsx"). Renders in the existing selected-node detail
 * area below the graph canvas, replacing the plain `Box`/`Alert` for output
 * nodes. Purely presentational: all state precedence lives in
 * `previewModel.ts`; the only local state is the thumbnail `onError` fallback.
 *
 * The "View full results" link renders in every preview state (R1.3, R3.5).
 */
export default function NodePreviewCard({
  nodeId,
  registrationId,
  executionId,
  viewModel,
}: NodePreviewCardProps): JSX.Element | null {
  // Thumbnail load failures flip the card to the unavailable message (R3.3).
  // Tracked per-src so a new image source gets a fresh attempt.
  const [failedImageSrc, setFailedImageSrc] = React.useState<string | null>(
    null,
  );

  if (viewModel.kind === "none") {
    // Non-output nodes never render the card (R1.4); the caller branches on
    // isOutputNode, so this is defensive only.
    return null;
  }

  let body: JSX.Element;
  switch (viewModel.kind) {
    case "failure":
      // The pre-existing failure alert presentation, kept verbatim inside the
      // card (R3.2, R5.2).
      body = (
        <Alert
          data-testid="node-detail"
          type="error"
          header={`${nodeId} — failure`}
        >
          {viewModel.detail ?? "No additional detail was recorded."}
        </Alert>
      );
      break;
    case "image":
      body =
        failedImageSrc === viewModel.src ? (
          <Unavailable />
        ) : (
          <img
            data-testid="preview-thumbnail"
            src={viewModel.src}
            alt={`Output of ${nodeId}`}
            style={THUMBNAIL_STYLE}
            onError={(): void => setFailedImageSrc(viewModel.src)}
          />
        );
      break;
    case "detections": {
      // A thumbnail that fails to load is hidden; the list stays
      // (run-detection-visibility R3.3).
      const thumbnailSrc = viewModel.imageSrc;
      body = (
        <SpaceBetween size="s">
          {thumbnailSrc !== undefined && failedImageSrc !== thumbnailSrc && (
            <img
              data-testid="preview-overlay-thumbnail"
              src={thumbnailSrc}
              alt={`Objects detected by ${nodeId}, drawn on the captured frame`}
              style={THUMBNAIL_STYLE}
              onError={(): void => setFailedImageSrc(thumbnailSrc)}
            />
          )}
          <DetectionList detections={viewModel.detections} />
        </SpaceBetween>
      );
      break;
    }
    case "text":
      body = (
        <Box data-testid="preview-text" variant="p">
          {viewModel.text}
        </Box>
      );
      break;
    case "fields":
      body = (
        <div data-testid="preview-fields">
          {viewModel.fields.map(([key, value]) => (
            <div key={key} style={{ display: "flex", gap: 8 }}>
              <Box variant="strong">{key}</Box>
              <Box variant="span">{value}</Box>
            </div>
          ))}
        </div>
      );
      break;
    case "status":
      body = (
        <SpaceBetween size="xs" direction="horizontal">
          <StatusIndicator
            type={viewModel.status === "warning" ? "warning" : "success"}
          >
            {viewModel.status}
          </StatusIndicator>
          {viewModel.detail !== undefined && (
            <Box data-testid="preview-status-detail" variant="span">
              {viewModel.detail}
            </Box>
          )}
        </SpaceBetween>
      );
      break;
    case "pending":
      body = (
        <Box data-testid="preview-pending" variant="p">
          {PENDING_MESSAGE}
        </Box>
      );
      break;
    case "loading":
      body = <Spinner data-testid="preview-loading" size="normal" />;
      break;
    default:
      // "unavailable"
      body = <Unavailable />;
      break;
  }

  return (
    <Container
      data-testid="node-preview-card"
      header={<Header variant="h3">{`${nodeId} — output preview`}</Header>}
      footer={
        <Link
          data-testid="preview-results-link"
          to={`/deployed-workflows/${registrationId}/executions/${executionId}/results`}
        >
          View full results
        </Link>
      }
    >
      {body}
    </Container>
  );
}
