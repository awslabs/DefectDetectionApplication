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
 * React hook that drives the broadcast-model live-preview lifecycle for one
 * camera (concurrent-camera-stream-viewing, task 11.1):
 *
 *  - subscribes on mount and stores the server-issued viewer id,
 *  - polls the frame endpoint on {@link PREVIEW_REFRESH_INTERVAL_MS} (the GET
 *    doubles as the viewer's heartbeat, so no separate heartbeat call is needed
 *    while polling is active),
 *  - exposes the latest frame object URL plus a status the UI can render,
 *  - unsubscribes on unmount, and
 *  - registers a `navigator.sendBeacon` unsubscribe on `pagehide` /
 *    `beforeunload` so an abandoned tab releases the device promptly.
 *
 * Object URLs are revoked as they are replaced and on teardown to avoid leaks.
 *
 * NOTE: This hook only *exposes* the frame/subscription status. Rendering the
 * rich frame-state and rejection feedback is task 11.2.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import {
  buildUnsubscribeBeaconUrl,
  getStreamFrame,
  StreamFrameStatus,
  StreamSubscribeConfig,
  subscribeStream,
  unsubscribeStream,
} from "api/StreamAPI";

/**
 * Subscription lifecycle status, distinct from the per-frame status so the UI
 * can tell "still subscribing" / "rejected" apart from "subscribed but no
 * frame yet".
 */
export type StreamSubscriptionStatus =
  | "idle" // not enabled / no camera
  | "subscribing" // subscribe in flight
  | "subscribed" // active viewer, polling frames
  | "limit" // subscribe rejected: viewer limit reached (429)
  | "unavailable" // subscribe rejected: camera unavailable (503)
  | "error"; // unexpected subscribe failure

export interface UseCameraStreamOptions {
  /** Camera/stream id to subscribe to. */
  cameraId: string;
  /** When false, the hook stays idle (no subscribe / poll). Defaults to true. */
  enabled?: boolean;
  /** Optional config forwarded to the broadcaster when starting a new session. */
  config?: StreamSubscribeConfig;
  /** Poll interval in ms; defaults to PREVIEW_REFRESH_INTERVAL_MS. */
  refreshIntervalMs?: number;
}

export interface UseCameraStreamResult {
  /** Object URL for the latest decoded frame, or null when none is available. */
  frameUrl: string | null;
  /** Latest per-frame status (ok/stale/no_frame/disconnected/error). */
  frameStatus: StreamFrameStatus | null;
  /** Subscription lifecycle status. */
  subscriptionStatus: StreamSubscriptionStatus;
  /** Server-issued viewer id while subscribed, else null. */
  viewerId: string | null;
  /** Current active viewer count reported by the backend (best-effort). */
  viewerCount: number | null;
  /** Human-readable detail for rejected/disconnected/error states. */
  message?: string;
}

export default function useCameraStream({
  cameraId,
  enabled = true,
  config,
  refreshIntervalMs = PREVIEW_REFRESH_INTERVAL_MS,
}: UseCameraStreamOptions): UseCameraStreamResult {
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [frameStatus, setFrameStatus] = useState<StreamFrameStatus | null>(null);
  const [subscriptionStatus, setSubscriptionStatus] =
    useState<StreamSubscriptionStatus>("idle");
  const [viewerId, setViewerId] = useState<string | null>(null);
  const [viewerCount, setViewerCount] = useState<number | null>(null);
  const [message, setMessage] = useState<string | undefined>(undefined);

  // Refs let the polling loop and teardown read the latest values without
  // re-binding the interval or the unload listener on every render.
  const viewerIdRef = useRef<string | null>(null);
  const cameraIdRef = useRef<string>(cameraId);
  const currentObjectUrlRef = useRef<string | null>(null);
  const isMountedRef = useRef<boolean>(true);

  cameraIdRef.current = cameraId;

  // Replace the displayed frame, revoking the previously displayed object URL.
  const swapFrameUrl = useCallback((nextUrl: string | null) => {
    const previous = currentObjectUrlRef.current;
    if (previous && previous !== nextUrl) {
      URL.revokeObjectURL(previous);
    }
    currentObjectUrlRef.current = nextUrl;
    setFrameUrl(nextUrl);
  }, []);

  useEffect(() => {
    isMountedRef.current = true;

    if (!enabled || !cameraId) {
      setSubscriptionStatus("idle");
      return;
    }

    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const poll = async (): Promise<void> => {
      const activeViewerId = viewerIdRef.current;
      if (!activeViewerId) {
        return;
      }
      const result = await getStreamFrame(cameraIdRef.current, activeViewerId);
      if (cancelled || !isMountedRef.current) {
        // Component went away mid-flight; drop the freshly created object URL.
        if (result.objectUrl) {
          URL.revokeObjectURL(result.objectUrl);
        }
        return;
      }
      setFrameStatus(result.status);
      setMessage(result.message);
      if (result.objectUrl) {
        swapFrameUrl(result.objectUrl);
      }
      // For no_frame/disconnected/error we keep the last good frame URL (if any)
      // so 11.2 can decide how to present staleness vs. a hard failure.
    };

    const startPolling = (): void => {
      // Kick off an immediate poll, then settle into the interval cadence.
      void poll();
      pollTimer = setInterval(() => void poll(), refreshIntervalMs);
    };

    const subscribe = async (): Promise<void> => {
      setSubscriptionStatus("subscribing");
      setMessage(undefined);
      try {
        const { viewerId: newViewerId, viewerCount: count } =
          await subscribeStream(cameraId, config);
        if (cancelled || !isMountedRef.current) {
          // Subscribed after unmount: immediately release the viewer slot.
          void unsubscribeStream(cameraId, newViewerId).catch(() => undefined);
          return;
        }
        viewerIdRef.current = newViewerId;
        setViewerId(newViewerId);
        setViewerCount(count);
        setSubscriptionStatus("subscribed");
        startPolling();
      } catch (error: any) {
        if (cancelled || !isMountedRef.current) {
          return;
        }
        const httpStatus = error?.response?.status;
        const detail =
          error?.response?.data?.detail ??
          error?.response?.data?.message ??
          error?.message;
        if (httpStatus === 429) {
          setSubscriptionStatus("limit");
        } else if (httpStatus === 503) {
          setSubscriptionStatus("unavailable");
        } else {
          setSubscriptionStatus("error");
        }
        setMessage(detail);
      }
    };

    void subscribe();

    return () => {
      cancelled = true;
      if (pollTimer) {
        clearInterval(pollTimer);
      }
      const activeViewerId = viewerIdRef.current;
      const activeCameraId = cameraIdRef.current;
      if (activeViewerId) {
        // Best-effort prompt teardown; the backend's stale sweep is the backstop.
        void unsubscribeStream(activeCameraId, activeViewerId).catch(
          () => undefined,
        );
      }
      viewerIdRef.current = null;
      const lastUrl = currentObjectUrlRef.current;
      if (lastUrl) {
        URL.revokeObjectURL(lastUrl);
        currentObjectUrlRef.current = null;
      }
    };
    // Re-subscribe when the target camera, enablement, or cadence changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, enabled, refreshIntervalMs]);

  // Release the viewer slot promptly when the tab is closed/hidden. `sendBeacon`
  // survives unload where a normal async request would be cancelled.
  useEffect(() => {
    if (!enabled || !cameraId) {
      return;
    }

    const releaseOnUnload = (): void => {
      const activeViewerId = viewerIdRef.current;
      if (!activeViewerId) {
        return;
      }
      const url = buildUnsubscribeBeaconUrl(cameraIdRef.current, activeViewerId);
      if (typeof navigator !== "undefined" && navigator.sendBeacon) {
        navigator.sendBeacon(url);
      }
    };

    window.addEventListener("pagehide", releaseOnUnload);
    window.addEventListener("beforeunload", releaseOnUnload);
    return () => {
      window.removeEventListener("pagehide", releaseOnUnload);
      window.removeEventListener("beforeunload", releaseOnUnload);
    };
  }, [cameraId, enabled]);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  return {
    frameUrl,
    frameStatus,
    subscriptionStatus,
    viewerId,
    viewerCount,
    message,
  };
}
