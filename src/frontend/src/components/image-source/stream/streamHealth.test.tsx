/*
 * How a stream camera's Stream_Health reads (rtsp-rtmp-stream-cameras
 * Requirements 4.3, 16.1).
 */
import { render, screen } from "@testing-library/react";
import { StreamHealth } from "components/image-source/types";
import {
  StreamHealthSummary,
  StreamStateIndicator,
  categoryLabel,
  frameRate,
  isNotStreaming,
  notStreamingDetail,
  resolution,
  streamStateLabel,
} from "./streamHealth";

const STREAMING: StreamHealth = {
  state: "streaming",
  codec: "h265",
  width: 1920,
  height: 1080,
  sourceFps: 24.96,
  decoder: "hardware",
  decoderFallback: false,
  reconnects: 2,
  leases: 3,
};

describe("the Requirement 4.3 failure categories", () => {
  it.each([
    ["network_error", "Unreachable"],
    ["authentication_failed", "Authentication failed"],
    ["not_found", "Not found"],
    ["unsupported_codec", "Unsupported codec"],
    ["decoder_unavailable", "Decoder unavailable"],
    ["tls_verification_failed", "TLS verification failed"],
    ["timeout", "Timed out"],
  ])("labels %s as %s", (category, label) => {
    expect(categoryLabel(category)).toBe(label);
  });

  it("falls back to a readable form of an unknown category", () => {
    expect(categoryLabel("brand_new_reason")).toBe("brand new reason");
    expect(categoryLabel(null)).toBe("Failed");
  });
});

describe("session states", () => {
  it.each([
    ["streaming", "Streaming"],
    ["connecting", "Connecting"],
    ["reconnecting", "Reconnecting"],
    ["failed", "Failed"],
    ["stopped", "Not connected"],
  ] as const)("labels %s as %s", (state, label) => {
    expect(streamStateLabel(state)).toBe(label);
    render(<StreamStateIndicator state={state} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("reads a camera without health as not connected", () => {
    expect(streamStateLabel(undefined)).toBe("Not connected");
  });

  it("is not streaming only when the health says so", () => {
    expect(isNotStreaming(STREAMING)).toBe(false);
    expect(isNotStreaming({ state: "reconnecting" })).toBe(true);
    expect(isNotStreaming({ state: "stopped" })).toBe(true);
    // Unknown health is not reported as an outage.
    expect(isNotStreaming(undefined)).toBe(false);
    expect(isNotStreaming(null)).toBe(false);
  });
});

describe("health formatting", () => {
  it("formats resolution and frame rate, or a dash", () => {
    expect(resolution(STREAMING)).toBe("1920 × 1080");
    expect(frameRate(STREAMING)).toBe("25.0 fps");
    expect(resolution({ state: "connecting" })).toBe("-");
    expect(frameRate({ state: "connecting", sourceFps: null })).toBe("-");
  });

  it("explains a camera that is not streaming", () => {
    expect(
      notStreamingDetail({
        state: "reconnecting",
        lastError: { category: "timeout", message: "No frame for 10 s" },
        nextAttemptInS: 3.2,
      }),
    ).toBe("No frame for 10 s; next attempt in 4 s");
    expect(notStreamingDetail({ state: "stopped" })).toBe("");
  });

  it("summarizes the health, with the fallback note", () => {
    render(<StreamHealthSummary health={{ ...STREAMING, decoder: "software", decoderFallback: true }} />);
    expect(screen.getByText("Streaming")).toBeInTheDocument();
    expect(screen.getByText("H265")).toBeInTheDocument();
    expect(screen.getByText("1920 × 1080")).toBeInTheDocument();
    expect(screen.getByText(/software \(fell back from hardware\)/)).toBeInTheDocument();
  });

  it("shows why a camera is not streaming in the summary", () => {
    render(
      <StreamHealthSummary
        health={{
          state: "failed",
          lastError: { category: "authentication_failed", message: "401 Unauthorized" },
        }}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("401 Unauthorized")).toBeInTheDocument();
  });
});
