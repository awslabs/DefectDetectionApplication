import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  IMAGE_TIMEOUT_MS,
  IMAGE_UNAVAILABLE_TEXT,
  createPanelImageLoader,
  panelImageUrl,
} from "./images";
import type { ImagePanelHandle } from "./render";

/**
 * Unit tests for the per-panel image loader (Requirements 4.5, 4.10, 4.11,
 * 5.8). The loader only touches its own panel's `<img>`, so a minimal
 * element stub is enough and the suite stays in the node environment.
 */

/** The `<img>` surface the loader uses, with test-only dispatch helpers. */
class StubImage {
  private readonly listeners = new Map<string, Set<() => void>>();
  private attribute: string | null = null;
  complete = false;
  naturalWidth = 0;

  get src(): string {
    return this.attribute ?? "";
  }

  set src(value: string) {
    this.attribute = value;
  }

  getAttribute(name: string): string | null {
    return name === "src" ? this.attribute : null;
  }

  removeAttribute(name: string): void {
    if (name === "src") this.attribute = null;
  }

  addEventListener(type: string, listener: () => void): void {
    const set = this.listeners.get(type) ?? new Set<() => void>();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: () => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  dispatch(type: "load" | "error"): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener();
  }

  listenerCount(type: "load" | "error"): number {
    return this.listeners.get(type)?.size ?? 0;
  }
}

interface TestPanel {
  handle: ImagePanelHandle;
  img: StubImage;
  /** The panel's visible state after the loader's last action. */
  shown: { mode: "image" | "placeholder"; text: string };
}

function testPanel(): TestPanel {
  const img = new StubImage();
  const shown: TestPanel["shown"] = { mode: "placeholder", text: "" };
  const handle: ImagePanelHandle = {
    img: img as unknown as HTMLImageElement,
    showImage(): void {
      shown.mode = "image";
      shown.text = "";
    },
    showPlaceholder(text: string): void {
      shown.mode = "placeholder";
      shown.text = text;
    },
  };
  return { handle, img, shown };
}

const REF = { nodeId: "bedrock inspect 1", port: "annotated" };

describe("panelImageUrl", () => {
  it("carries the run, the panel's own pair, and the token in the query (4.5)", () => {
    const url = panelImageUrl("exec 1", REF, "tok/en");
    expect(url).toBe(
      "/workflows/executions/exec%201/node-image" +
        "?nodeId=bedrock%20inspect%201&port=annotated&token=tok%2Fen",
    );
  });
});

describe("createPanelImageLoader", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("points the panel at its own image URL with the session token (4.5)", () => {
    const panel = testPanel();
    const load = createPanelImageLoader({ token: () => "T0" });

    load(panel.handle, "exec-1", REF);

    expect(panel.img.getAttribute("src")).toBe(
      panelImageUrl("exec-1", REF, "T0"),
    );
    expect(panel.shown.mode).toBe("image");
  });

  it("keeps the loaded image visible on success", () => {
    const panel = testPanel();
    createPanelImageLoader({ token: () => "T0" })(panel.handle, "exec-1", REF);

    panel.img.dispatch("load");

    expect(panel.shown.mode).toBe("image");
    expect(panel.img.getAttribute("src")).not.toBeNull();
    // Settled requests leave no listeners or timers behind.
    expect(panel.img.listenerCount("load")).toBe(0);
    expect(panel.img.listenerCount("error")).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("shows the unavailable placeholder in that panel on error (4.11, 5.8)", () => {
    const failing = testPanel();
    const healthy = testPanel();
    const load = createPanelImageLoader({ token: () => "T0" });
    load(failing.handle, "exec-1", REF);
    load(healthy.handle, "exec-1", { nodeId: "other", port: "original" });

    failing.img.dispatch("error");

    expect(failing.shown).toEqual({
      mode: "placeholder",
      text: IMAGE_UNAVAILABLE_TEXT,
    });
    // No image from another Inspection, port, or run takes its place.
    expect(failing.img.getAttribute("src")).toBeNull();
    // The other panel is untouched.
    expect(healthy.shown.mode).toBe("image");
    expect(healthy.img.getAttribute("src")).toBe(
      panelImageUrl("exec-1", { nodeId: "other", port: "original" }, "T0"),
    );
  });

  it("falls back to the placeholder after the 10 s timeout (4.5, 4.11)", () => {
    const panel = testPanel();
    createPanelImageLoader({ token: () => "T0" })(panel.handle, "exec-1", REF);

    vi.advanceTimersByTime(IMAGE_TIMEOUT_MS - 1);
    expect(panel.shown.mode).toBe("image");

    vi.advanceTimersByTime(1);
    expect(panel.shown).toEqual({
      mode: "placeholder",
      text: IMAGE_UNAVAILABLE_TEXT,
    });
    expect(panel.img.getAttribute("src")).toBeNull();
  });

  it("supersedes the previous request when the displayed run changes (3.6, 4.11)", () => {
    const panel = testPanel();
    const load = createPanelImageLoader({ token: () => "T0" });
    load(panel.handle, "exec-1", REF);

    // The displayed run changed: the panel is pointed at the new run's frame.
    load(panel.handle, "exec-2", REF);

    expect(panel.img.getAttribute("src")).toBe(panelImageUrl("exec-2", REF, "T0"));
    // Exactly one request is in flight, so the older one's timeout can never
    // blank the newer frame.
    expect(vi.getTimerCount()).toBe(1);
    expect(panel.img.listenerCount("load")).toBe(1);
    expect(panel.img.listenerCount("error")).toBe(1);

    panel.img.dispatch("load");
    expect(panel.shown.mode).toBe("image");
    vi.advanceTimersByTime(IMAGE_TIMEOUT_MS * 2);
    expect(panel.shown.mode).toBe("image");
  });

  it("leaves a placeholder state alone when a stale request expires (4.10)", () => {
    const panel = testPanel();
    createPanelImageLoader({ token: () => "T0" })(panel.handle, "exec-1", REF);

    // The renderer moved this panel to the no-annotated-image placeholder.
    panel.img.removeAttribute("src");
    panel.handle.showPlaceholder("No annotated image available");

    vi.advanceTimersByTime(IMAGE_TIMEOUT_MS);
    panel.img.dispatch("error");

    expect(panel.shown).toEqual({
      mode: "placeholder",
      text: "No annotated image available",
    });
  });

  it("settles a cached frame that is complete before any event fires", () => {
    const panel = testPanel();
    // A cached frame can be complete before `load` would be dispatched.
    panel.img.complete = true;
    panel.img.naturalWidth = 64;

    createPanelImageLoader({ token: () => "T0" })(panel.handle, "exec-1", REF);

    expect(panel.shown.mode).toBe("image");
    // No pending timeout can later blank an image that is already displayed.
    expect(vi.getTimerCount()).toBe(0);
    expect(panel.img.listenerCount("error")).toBe(0);
  });
});
