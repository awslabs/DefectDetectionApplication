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
 * Presentational live-preview for the broadcast-model stream
 * (concurrent-camera-stream-viewing, task 11.2).
 *
 * It drives the lifecycle via {@link useCameraStream} (task 11.1) and renders
 * clear, distinct feedback for each state instead of leaving a frozen image on
 * screen:
 *
 *  Subscription lifecycle (subscriptionStatus):
 *   - "subscribing" -> loading placeholder ("Connecting to camera").
 *   - "limit"       -> "maximum viewers reached" warning (Req 1.6).
 *   - "unavailable" -> "camera unavailable" error (Req 1.7).
 *   - "error"       -> generic error with the server message.
 *
 *  Per-frame status (frameStatus, once subscribed):
 *   - "ok"           -> show the latest frame.
 *   - "stale"        -> show the last frame plus a "reconnecting" indicator
 *                       (Req 4.7).
 *   - "no_frame"     -> loading placeholder ("Waiting for first frame")
 *                       (Req 2.6).
 *   - "disconnected" -> "camera disconnected" error and NO frame (Req 7.5).
 *   - "error"        -> generic error with the server message.
 *
 * The component is self-contained and reusable: pass a `cameraId` (plus an
 * optional `enabled`/`config`) and it manages everything else.
 */
import { ReactNode, useMemo } from "react";
import {
  Alert,
  Box,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import InteractableImage from "components/live-result/InteractableImage";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import { fullWidthStyle } from "components/live-result/styles";
import { StreamSubscribeConfig } from "api/StreamAPI";
import useCameraStream from "components/live-result/useCameraStream";

interface StreamPreviewProps {
  /** Camera/stream id to subscribe to. */
  cameraId: string;
  /** When false, the hook stays idle (no subscribe / poll). Defaults to true. */
  enabled?: boolean;
  /** Optional config forwarded to the broadcaster when starting a new session. */
  config?: StreamSubscribeConfig;
  /** Poll interval in ms; defaults to the hook's PREVIEW_REFRESH_INTERVAL_MS. */
  refreshIntervalMs?: number;
  /** Optional alt text for the rendered frame. */
  alt?: string;
  /** Optional extra actions rendered alongside the image controls. */
  extraActions?: JSX.Element;
}

const SUBSCRIBE_LIMIT_MESSAGE =
  "Maximum viewers reached for this camera. Close another live view and try again.";
const SUBSCRIBE_UNAVAILABLE_MESSAGE =
  "Camera unavailable. It could not be opened for streaming.";
const DISCONNECTED_MESSAGE =
  "Camera disconnected. The live stream has stopped.";

/** Wrap placeholder/feedback content so it occupies the preview area. */
function PreviewFeedback({ content }: { content: ReactNode }): JSX.Element {
  return (
    <div className={fullWidthStyle}>
      <ImagePlaceholder placement="center" content={content} />
    </div>
  );
}

function LoadingFeedback({ label }: { label: string }): JSX.Element {
  return (
    <PreviewFeedback
      content={
        <SpaceBetween direction="vertical" size="m" alignItems="center">
          <Spinner size="big" />
          <p>{label}</p>
        </SpaceBetween>
      }
    />
  );
}

export default function StreamPreview({
  cameraId,
  enabled = true,
  config,
  refreshIntervalMs,
  alt,
  extraActions,
}: StreamPreviewProps): JSX.Element {
  const { frameUrl, frameStatus, subscriptionStatus, message } =
    useCameraStream({ cameraId, enabled, config, refreshIntervalMs });

  // The frame element is reused for "ok" and "stale" so a transient stale
  // window does not unmount/flash the last good image.
  const frame = useMemo(() => {
    if (!frameUrl) {
      return null;
    }
    return (
      <InteractableImage
        imageSrc={frameUrl}
        alt={alt ?? "Live camera frame"}
        extraActions={extraActions}
      />
    );
  }, [frameUrl, alt, extraActions]);

  // 1) Subscription-level outcomes take precedence: until we are subscribed
  //    there is no meaningful per-frame status to show.
  switch (subscriptionStatus) {
    case "idle":
      return (
        <PreviewFeedback
          content={
            <Box variant="p" color="text-status-inactive">
              Live preview is not active.
            </Box>
          }
        />
      );
    case "subscribing":
      return <LoadingFeedback label="Connecting to camera" />;
    case "limit":
      return (
        <PreviewFeedback
          content={
            <Alert type="warning" header="Maximum viewers reached">
              {message ?? SUBSCRIBE_LIMIT_MESSAGE}
            </Alert>
          }
        />
      );
    case "unavailable":
      return (
        <PreviewFeedback
          content={
            <Alert type="error" header="Camera unavailable">
              {message ?? SUBSCRIBE_UNAVAILABLE_MESSAGE}
            </Alert>
          }
        />
      );
    case "error":
      return (
        <PreviewFeedback content={<ImagePreviewError errorMsg={message} />} />
      );
    case "subscribed":
    default:
      break;
  }

  // 2) Subscribed: render based on the latest per-frame status.
  switch (frameStatus) {
    case "ok":
      // Fresh frame available.
      return frame ?? <LoadingFeedback label="Waiting for first frame" />;

    case "stale":
      // Keep showing the last frame, but make staleness explicit.
      if (frame) {
        return (
          <SpaceBetween direction="vertical" size="xs">
            <StatusIndicator type="loading">
              Stream is stale, reconnecting…
            </StatusIndicator>
            {frame}
          </SpaceBetween>
        );
      }
      return <LoadingFeedback label="Waiting for first frame" />;

    case "disconnected":
      // Hard failure: never keep a frozen frame on screen (Req 7.5).
      return (
        <PreviewFeedback
          content={
            <Alert type="error" header="Camera disconnected">
              {message ?? DISCONNECTED_MESSAGE}
            </Alert>
          }
        />
      );

    case "error":
      return (
        <PreviewFeedback content={<ImagePreviewError errorMsg={message} />} />
      );

    case "no_frame":
    case null:
    default:
      // Subscribed but nothing published yet (Req 2.6).
      return <LoadingFeedback label="Waiting for first frame" />;
  }
}
