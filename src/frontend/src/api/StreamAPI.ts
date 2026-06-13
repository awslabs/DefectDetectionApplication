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
 * Client for the broadcast-model live-preview stream API
 * (`/streams/{camera_id}/...`). These are thin wrappers over the backend
 * endpoints in `src/backend/endpoints/streams.py`: a viewer subscribes to a
 * camera (start-on-first-viewer), polls the frame endpoint (the GET doubles as
 * a heartbeat), and unsubscribes on teardown (stop-on-last-viewer).
 */
import axios from "axios";
import { ImageSourceConfiguration } from "components/image-source/types";
import { APIList } from "config/Interface";

/** Fill the `{camera_id}` template in an APIList stream URL. */
function streamUrl(template: string, cameraId: string): string {
  return template.replace("{camera_id}", encodeURIComponent(cameraId));
}

/**
 * Optional config forwarded verbatim to the broadcaster on subscribe. It is
 * only used when starting a brand-new session; an existing session reuses its
 * claim and ignores the config.
 */
export interface StreamSubscribeConfig {
  type?: string;
  imageSourceConfiguration?: ImageSourceConfiguration;
  [key: string]: unknown;
}

/** Accepted-subscription payload: the server-issued viewer id + viewer count. */
export interface SubscribeStreamResponse {
  viewerId: string;
  viewerCount: number;
}

/**
 * Frame freshness/availability as exposed to the UI. `ok`/`stale` carry a
 * decoded object URL; the remaining states carry no image and let the consumer
 * render appropriate feedback (handled by task 11.2).
 *
 * - `ok` / `stale`     — frame served (200); `stale` flags an aged frame.
 * - `no_frame`         — session up, nothing published yet (204).
 * - `disconnected`     — camera dropped / no active session (503).
 * - `limit`            — subscribe rejected, viewer limit reached (429).
 * - `unavailable`      — subscribe rejected, camera unavailable (503).
 * - `error`            — any other/unexpected failure.
 */
export type StreamFrameStatus =
  | "ok"
  | "stale"
  | "no_frame"
  | "disconnected"
  | "limit"
  | "unavailable"
  | "error";

/**
 * Result of a single frame poll. When `objectUrl` is set the caller owns it and
 * must `URL.revokeObjectURL` it once the image is no longer displayed.
 */
export interface StreamFrameResult {
  status: StreamFrameStatus;
  /** Object URL for the decoded frame bytes, or null when no image is available. */
  objectUrl: string | null;
  /** Monotonic per-session sequence number, when provided by the backend. */
  seq?: number;
  width?: number;
  height?: number;
  /** Epoch seconds when the frame was grabbed, when provided by the backend. */
  acquiredAt?: number;
  /** Human-readable detail for non-image states (e.g. disconnect reason). */
  message?: string;
}

export interface UnsubscribeStreamResponse {
  viewerCount: number;
}

/**
 * Register a new viewer for a camera, starting the session on the first
 * subscriber. Rejections surface as thrown errors via axios:
 *   - 429 -> viewer limit reached
 *   - 503 -> camera unavailable
 * Callers can inspect `error.response?.status` to distinguish them.
 */
export async function subscribeStream(
  cameraId: string,
  config?: StreamSubscribeConfig,
): Promise<SubscribeStreamResponse> {
  const endpoint = streamUrl(APIList.streamSubscribeAPI, cameraId);
  const { data } = await axios.post<SubscribeStreamResponse>(endpoint, {
    config: config ?? null,
  });
  return data;
}

/** Map the backend `X-Frame-Status` header to the UI status enum. */
function normalizeFrameStatus(header: string | undefined): StreamFrameStatus {
  const value = (header ?? "").toLowerCase();
  if (value === "ok" || value === "stale" || value === "no_frame") {
    return value;
  }
  return "ok";
}

function toNumber(value: string | undefined): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

/**
 * Fetch the latest frame for a viewer. The GET doubles as the viewer's
 * heartbeat on the backend, so polling this keeps the subscription alive.
 *
 * Distinct outcomes are encoded in {@link StreamFrameResult.status}:
 *   - 200 -> `ok` / `stale` (per the `X-Frame-Status` header) with an object URL
 *   - 204 -> `no_frame` (session up, nothing published yet)
 *   - 503 -> `disconnected`
 *   - anything else -> `error`
 *
 * The frame bytes are read as an ArrayBuffer and converted to a Blob object URL
 * suitable for an `<img>` element. The caller is responsible for revoking the
 * returned object URL to avoid leaks.
 */
export async function getStreamFrame(
  cameraId: string,
  viewerId: string,
): Promise<StreamFrameResult> {
  const endpoint = streamUrl(APIList.streamFrameAPI, cameraId);
  try {
    const response = await axios.get<ArrayBuffer>(endpoint, {
      params: { viewerId },
      responseType: "arraybuffer",
      // Treat 2xx and 204 as resolved; 4xx/5xx fall through to the catch so we
      // can classify disconnects vs. unexpected errors.
      validateStatus: (status) => status >= 200 && status < 300,
    });

    if (response.status === 204) {
      return { status: "no_frame", objectUrl: null };
    }

    const headers = response.headers ?? {};
    const status = normalizeFrameStatus(headers["x-frame-status"]);
    const contentType =
      (headers["content-type"] as string | undefined) ??
      "application/octet-stream";
    const blob = new Blob([response.data], { type: contentType });
    const objectUrl = URL.createObjectURL(blob);

    return {
      status,
      objectUrl,
      seq: toNumber(headers["x-frame-seq"]),
      width: toNumber(headers["x-frame-width"]),
      height: toNumber(headers["x-frame-height"]),
      acquiredAt: toNumber(headers["x-frame-acquired-at"]),
    };
  } catch (error: any) {
    const httpStatus = error?.response?.status;
    if (httpStatus === 503) {
      return {
        status: "disconnected",
        objectUrl: null,
        message: extractErrorMessage(error),
      };
    }
    return {
      status: "error",
      objectUrl: null,
      message: extractErrorMessage(error),
    };
  }
}

/**
 * Best-effort decode of a FastAPI error body that arrived as an ArrayBuffer
 * (because the request used `responseType: 'arraybuffer'`).
 */
function extractErrorMessage(error: any): string | undefined {
  const data = error?.response?.data;
  if (!data) {
    return error?.message;
  }
  try {
    const text =
      data instanceof ArrayBuffer
        ? new TextDecoder().decode(new Uint8Array(data))
        : typeof data === "string"
          ? data
          : JSON.stringify(data);
    try {
      const parsed = JSON.parse(text);
      return parsed?.detail ?? parsed?.message ?? text;
    } catch {
      return text;
    }
  } catch {
    return error?.message;
  }
}

/**
 * Explicit keep-alive for a viewer. Resolves to `true` when the viewer was
 * refreshed and `false` when the backend reports the viewer is unknown/expired
 * (404), signalling the caller to re-subscribe.
 */
export async function heartbeatStream(
  cameraId: string,
  viewerId: string,
): Promise<boolean> {
  const endpoint = streamUrl(APIList.streamHeartbeatAPI, cameraId);
  try {
    await axios.post(endpoint, { viewerId });
    return true;
  } catch (error: any) {
    if (error?.response?.status === 404) {
      return false;
    }
    throw error;
  }
}

/**
 * Deregister a viewer; the backend stops the session if it was the last viewer.
 * Idempotent and always safe to call on teardown.
 */
export async function unsubscribeStream(
  cameraId: string,
  viewerId: string,
): Promise<UnsubscribeStreamResponse> {
  const endpoint = streamUrl(APIList.streamUnsubscribeAPI, cameraId);
  const { data } = await axios.post<UnsubscribeStreamResponse>(endpoint, {
    viewerId,
  });
  return data;
}

/**
 * Build the unsubscribe URL (with the viewer id as a query parameter) for use
 * with `navigator.sendBeacon` on `pagehide` / `beforeunload`. `sendBeacon`
 * cannot set a JSON body reliably, so the viewer id is passed as a query param,
 * which the backend's unsubscribe endpoint also accepts.
 */
export function buildUnsubscribeBeaconUrl(
  cameraId: string,
  viewerId: string,
): string {
  const endpoint = streamUrl(APIList.streamUnsubscribeAPI, cameraId);
  return `${endpoint}?viewerId=${encodeURIComponent(viewerId)}`;
}
