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
 * How a stream camera's Stream_Health reads in the LocalServer UI
 * (rtsp-rtmp-stream-cameras Requirement 16.1): its state, the failure
 * categories of Requirement 4.3, and the health summary.
 */
import {
  Box,
  ColumnLayout,
  StatusIndicator,
  StatusIndicatorProps,
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import { StreamHealth, StreamState } from "components/image-source/types";

/** How often a stream camera's Stream_Health is refreshed. */
export const STREAM_HEALTH_REFRESH_MS = 5000;
/** The refresh while a live preview waits for the camera to stream. */
export const STREAM_HEALTH_FAST_REFRESH_MS = 1000;

const STATE_INDICATORS: Record<StreamState, [StatusIndicatorProps.Type, string]> = {
  streaming: ["success", "Streaming"],
  connecting: ["in-progress", "Connecting"],
  reconnecting: ["pending", "Reconnecting"],
  failed: ["error", "Failed"],
  stopped: ["stopped", "Not connected"],
};

/** The reason categories an operator reads (Requirement 4.3). */
const CATEGORY_LABELS: Record<string, string> = {
  network_error: "Unreachable",
  timeout: "Timed out",
  server_error: "Stream server error",
  stall: "Stream stalled",
  worker_exit: "Stream worker stopped",
  authentication_failed: "Authentication failed",
  not_found: "Not found",
  unsupported_codec: "Unsupported codec",
  decoder_unavailable: "Decoder unavailable",
  tls_verification_failed: "TLS verification failed",
  hardware_decoder_failed: "Hardware decoder failed",
  session_limit: "Too many stream sessions",
};

export function categoryLabel(category: string | null | undefined): string {
  if (!category) {
    return "Failed";
  }
  return CATEGORY_LABELS[category] ?? category.replace(/_/g, " ");
}

function stateIndicator(state: StreamState | undefined): [StatusIndicatorProps.Type, string] {
  return STATE_INDICATORS[state ?? "stopped"] ?? ["stopped", String(state)];
}

/** "Streaming", "Reconnecting", ... for a session state. */
export function streamStateLabel(state: StreamState | undefined): string {
  return stateIndicator(state)[1];
}

export function StreamStateIndicator({
  state,
}: {
  state: StreamState | undefined;
}): JSX.Element {
  const [type, label] = stateIndicator(state);
  return <StatusIndicator type={type}>{label}</StatusIndicator>;
}

/** Whether Stream_Health reports a camera that is not streaming. */
export function isNotStreaming(health: StreamHealth | null | undefined): boolean {
  return !!health && health.state !== "streaming";
}

/** "1920 × 1080" of the stream, or "-". */
export function resolution(health: StreamHealth | null | undefined): string {
  return health?.width && health?.height ? `${health.width} × ${health.height}` : "-";
}

/** The measured source frame rate, or "-". */
export function frameRate(health: StreamHealth | null | undefined): string {
  return typeof health?.sourceFps === "number" ? `${health.sourceFps.toFixed(1)} fps` : "-";
}

/** A one-line explanation of a camera that is not streaming. */
export function notStreamingDetail(health: StreamHealth | null | undefined): string {
  const parts: string[] = [];
  if (health?.lastError?.message) {
    parts.push(health.lastError.message);
  }
  if (typeof health?.nextAttemptInS === "number") {
    parts.push(`next attempt in ${Math.ceil(health.nextAttemptInS)} s`);
  }
  return parts.join("; ");
}

export function StreamHealthSummary({
  health,
}: {
  health: StreamHealth | null | undefined;
}): JSX.Element {
  const detail = health?.state !== "streaming" ? notStreamingDetail(health) : "";
  return (
    <ColumnLayout columns={4} variant="text-grid">
      <ValueWithLabel label="State">
        <StreamStateIndicator state={health?.state} />
        {detail && <Box color="text-body-secondary">{detail}</Box>}
      </ValueWithLabel>
      <ValueWithLabel label="Codec">{health?.codec?.toUpperCase() ?? "-"}</ValueWithLabel>
      <ValueWithLabel label="Resolution">{resolution(health)}</ValueWithLabel>
      <ValueWithLabel label="Frame rate">{frameRate(health)}</ValueWithLabel>
      <ValueWithLabel label="Decoder">
        {health?.decoder ?? "-"}
        {health?.decoderFallback ? " (fell back from hardware)" : ""}
      </ValueWithLabel>
      <ValueWithLabel label="Reconnects">{health?.reconnects ?? 0}</ValueWithLabel>
      <ValueWithLabel label="Consumers">{health?.leases ?? 0}</ValueWithLabel>
    </ColumnLayout>
  );
}
