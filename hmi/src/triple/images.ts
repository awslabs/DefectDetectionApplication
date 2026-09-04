/**
 * Per-panel image loading for the Triple_HMI (task 10.2).
 *
 * `createPanelImageLoader` returns the `PanelImageLoader` seam that
 * `triple/render.ts` calls whenever a panel's (`executionId`, `nodeId`,
 * `port`) triple changes. One panel, one image request:
 *
 *  - The URL is built by the existing `api/routes.ts` `nodeImageUrl` builder
 *    from the **displayed run's own** `executionId` and the panel's **own**
 *    (`nodeId`, `port`) pair, with the Session_Token in the `token` query
 *    parameter — `<img src>` cannot carry an Authorization header
 *    (Requirements 1.3, 4.5). No other run's or port's identity is reachable
 *    from here: the loader receives exactly one reference and never selects.
 *  - A 10-second timer bounds every request (Requirement 4.5). The `<img>`
 *    element has no timeout of its own, so the timer is the only thing that
 *    turns a stalled request into a visible outcome.
 *  - `error` and timeout both drop the element's `src` and show the
 *    image-unavailable placeholder **in that panel only** — the panel's own
 *    frame is cleared rather than filled with anything else, so no image from
 *    a different Inspection, port, or run can appear in its place, and the
 *    run's other content keeps rendering (Requirements 4.11, 5.8).
 *
 * Late settles cannot corrupt a panel: every outcome is applied only while the
 * element's `src` is still the exact URL that load began with. A panel that
 * has since moved to another reference (new run, new Inspection) or to a
 * placeholder (the no-annotated-image state of Requirement 4.10, a failed run,
 * an unavailable inventory) is therefore left untouched by the older request's
 * `error` event or expiring timer.
 *
 * The module keeps no view state and holds nothing across panels: `loadSession`
 * supplies the token per request (the token may have been re-issued by the 401
 * re-login of `api/client.ts`), and the only retained per-panel value is the
 * in-flight request's canceller.
 */

import { nodeImageUrl } from "../api/routes";
import { loadSession } from "../auth/session";
import type { ImageRef } from "./inspections";
import type { ImagePanelHandle, PanelImageLoader } from "./render";

/** Per-image request timeout in milliseconds (Requirement 4.5). */
export const IMAGE_TIMEOUT_MS = 10_000;

/**
 * Placeholder shown in the affected panel when its own image request fails or
 * times out (Requirements 4.11, 5.8).
 */
export const IMAGE_UNAVAILABLE_TEXT = "Image unavailable";

export interface PanelImageLoaderOptions {
  /**
   * Supplies the current Session_Token; defaults to the stored session's
   * token, or the empty string when no session is stored (the request then
   * fails authentication and the panel shows its placeholder).
   */
  token?: () => string;
  /** Per-image timeout; defaults to `IMAGE_TIMEOUT_MS` (Requirement 4.5). */
  timeoutMs?: number;
}

/** Cancels the pending load of one `<img>`, if any. */
type Canceller = () => void;

/**
 * Builds one panel's image URL (Requirement 4.5).
 *
 * Exported for the wiring tests: the URL is a pure function of the displayed
 * run's `executionId`, the panel's own reference, and the token.
 */
export function panelImageUrl(
  executionId: string,
  ref: ImageRef,
  token: string,
): string {
  return nodeImageUrl(executionId, ref.nodeId, ref.port, token);
}

/**
 * Creates the renderer's `loadImage` seam.
 *
 * The returned loader is called once per panel per (`executionId`, `nodeId`,
 * `port`) change, so a stable panel is never re-requested by the 2-second
 * poll churn.
 */
export function createPanelImageLoader(
  options: PanelImageLoaderOptions = {},
): PanelImageLoader {
  const tokenOf = options.token ?? (() => loadSession()?.token ?? "");
  const timeoutMs = options.timeoutMs ?? IMAGE_TIMEOUT_MS;

  // One in-flight request per element; a new request for the same panel
  // supersedes (and detaches) the previous one.
  const pending = new WeakMap<HTMLImageElement, Canceller>();

  return function loadPanelImage(
    panel: ImagePanelHandle,
    executionId: string,
    ref: ImageRef,
  ): void {
    const img = panel.img;
    pending.get(img)?.();

    const url = panelImageUrl(executionId, ref, tokenOf());

    let settled = false;

    const onLoad = (): void => settle(true);
    const onError = (): void => settle(false);
    const timer = setTimeout(() => settle(false), timeoutMs); // 4.5

    function detach(): void {
      clearTimeout(timer);
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
      if (pending.get(img) === detach) pending.delete(img);
    }

    function settle(loaded: boolean): void {
      if (settled) return;
      settled = true;
      detach();
      // The panel may already have moved on (another run, another Inspection,
      // or a placeholder state): only the request that still owns the element
      // may touch it (Requirements 4.11, 5.8).
      if (img.getAttribute("src") !== url) return;
      if (loaded) {
        panel.showImage();
        return;
      }
      // Clear this panel's own frame instead of substituting anything.
      img.removeAttribute("src");
      panel.showPlaceholder(IMAGE_UNAVAILABLE_TEXT);
    }

    pending.set(img, detach);
    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);
    panel.showImage();
    img.src = url;

    // A cached frame can be complete before any event is dispatched.
    if (img.complete && img.naturalWidth > 0) settle(true);
  };
}
