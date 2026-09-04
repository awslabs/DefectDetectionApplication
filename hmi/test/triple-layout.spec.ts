import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Kiosk_Display layout suite for the IMTS Triple Inspection HMI (task 13.1).
 *
 * Everything here is a statement about **rendered geometry**, which is why it
 * runs in a real browser engine instead of jsdom (where `render.test.ts` checks
 * the DOM contents): only a layout engine can decide whether three slots are
 * equally wide, whether an image is cropped, or whether verdict text is 32
 * pixels tall.
 *
 * Covered acceptance criteria:
 *
 *  - **6.1** — at 1920x1080 every piece of primary Live_View content (workflow
 *    identity, run timing, verdicts, three slots with two labeled images each,
 *    history strip) is inside the viewport, non-empty, and free of overlap, and
 *    the document does not scroll in either axis.
 *  - **6.2** — the three Inspection_Slot widths are equal within 2 px, and each
 *    image panel's width-to-height ratio sits inside the 1:1.8 – 1:2.2 band
 *    that matches the inspected plates' 1.5" x 3" form factor.
 *  - **5.3, 6.8** — the two images of a slot render at equal heights, with the
 *    source aspect ratio preserved and uncropped (`object-fit: contain`, the
 *    contained content fitting inside its frame), at ≥ 280 px wide at 1920.
 *  - **6.4** — every verdict state rendered as text (✔ PASS, ✘ FAIL,
 *    — NO VERDICT, ⚠ ERROR — per-slot and run-level) has a rendered text height
 *    of at least 32 px at 1920x1080.
 *  - **6.5** — no horizontal overflow and no overlapping content at both 1280
 *    and 1920 viewport widths.
 *
 * How the page under test is served: the built `dist/` bundle (produced once by
 * the config's `globalSetup`) and every LocalServer response are served through
 * Playwright request interception, so the suite needs no HTTP server, no
 * LocalServer, and no device — while the page itself is the real kiosk entry,
 * auto-started by `triple.html`'s `data-triple-kiosk` body attribute and driven
 * through its real auth, binding, polling, and rendering path.
 *
 * The stubbed device reports local login **disabled**
 * (`GET /local-auth/status`), which is the app's no-credentials entry
 * (Requirement 1.8), so the kiosk reaches its Live_View without a login step.
 */

// --------------------------------------------------------------------------
// Fixture data
// --------------------------------------------------------------------------

/** The built bundle `globalSetup` emits; served straight from disk below. */
const distUrl = new URL("../dist/", import.meta.url);

/** The kiosk page; the origin is only a label for the routing layer. */
const KIOSK_URL = "http://kiosk.test/hmi/triple.html";

const TARGET_NAME = "blue-plate-detection-guided-inspection";

const REGISTRATION = {
  registrationId: "reg-blue-plate",
  workflowId: "wf-blue-plate",
  name: TARGET_NAME,
  version: "1.0.0",
  status: "registered",
  registeredAt: 1_700_000_000,
};

/** The three Bedrock inspection nodes of the target workflow, one per plate. */
const NODE_IDS = ["bedrock_1", "bedrock_2", "bedrock_3"] as const;

/**
 * Intrinsic size of every stubbed frame: a 320x600 plate (ratio 1:1.875).
 *
 * Deliberately *not* the panel's own 1:2 ratio, so `object-fit: contain` has to
 * letterbox — which makes the aspect-preserved and uncropped assertions of
 * Requirements 5.3 and 6.8 non-trivial rather than vacuous.
 */
const IMAGE_NATURAL = { width: 320, height: 600 } as const;

interface ExecutionStub {
  executionId: string;
  registrationId: string;
  status: "pending" | "running" | "completed" | "failed";
  startedAt: number;
  finishedAt: number | null;
  failingNodeId: string | null;
  error: string | null;
  hasImageResults: boolean;
  captureId: string | null;
}

function completedRun(executionId: string, startedAt: number): ExecutionStub {
  return {
    executionId,
    registrationId: REGISTRATION.registrationId,
    status: "completed",
    startedAt,
    finishedAt: startedAt + 4,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: `capture-${executionId}`,
  };
}

function failedRun(executionId: string, startedAt: number): ExecutionStub {
  return {
    executionId,
    registrationId: REGISTRATION.registrationId,
    status: "failed",
    startedAt,
    finishedAt: startedAt + 2,
    failingNodeId: "bedrock_2",
    error: "bedrock_2: inference request failed after 3 attempts",
    hasImageResults: false,
    captureId: `capture-${executionId}`,
  };
}

/** The additive per-Inspection inventory: `original` + `annotated` per node. */
function resultsPayload(): { images: unknown[] } {
  const images: unknown[] = [{ kind: "output", hasOverlay: true }];
  for (const nodeId of NODE_IDS) {
    for (const port of ["annotated", "in", "original"]) {
      images.push({ kind: "node", nodeId, port, hasOverlay: false });
    }
  }
  return { images };
}

/**
 * Metadata exercising three different per-slot verdict states in one run:
 * slot 1 PASS, slot 2 FAIL, slot 3 NO VERDICT (no `bedrock` record), plus the
 * flat run-level verdict rendered once in the header (Requirements 5.5, 5.6,
 * 5.11, 5.12).
 */
const MIXED_METADATA = {
  is_anomalous: false,
  confidence: 0.9312,
  bedrock: {
    bedrock_1: { is_anomalous: false, confidence: 0.97, detection_id: "det-1" },
    bedrock_2: { is_anomalous: true, confidence: 0.88, detection_id: "det-2" },
  },
};

interface Scenario {
  /** Newest-first, as the LocalServer returns them. */
  executions: ExecutionStub[];
  metadata: Record<string, unknown>;
}

/** Latest terminal run completed, six runs of history. */
function completedScenario(): Scenario {
  return {
    executions: [
      completedRun("exec-6", 1_700_000_600),
      completedRun("exec-5", 1_700_000_500),
      failedRun("exec-4", 1_700_000_400),
      completedRun("exec-3", 1_700_000_300),
      completedRun("exec-2", 1_700_000_200),
      completedRun("exec-1", 1_700_000_100),
    ],
    metadata: MIXED_METADATA,
  };
}

/** Latest terminal run failed: the run-level ⚠ ERROR state (Requirement 5.9). */
function failedScenario(): Scenario {
  const scenario = completedScenario();
  scenario.executions.unshift(failedRun("exec-7", 1_700_000_700));
  return scenario;
}

// --------------------------------------------------------------------------
// Stubbed LocalServer + static bundle
// --------------------------------------------------------------------------

/** A plate frame with a known intrinsic size, so cropping is detectable. */
function plateSvg(port: string, nodeId: string): string {
  const { width, height } = IMAGE_NATURAL;
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"`,
    ` viewBox="0 0 ${width} ${height}">`,
    `<rect width="${width}" height="${height}" fill="#123"/>`,
    `<rect x="8" y="8" width="${width - 16}" height="${height - 16}"`,
    ` fill="none" stroke="#7cf" stroke-width="6"/>`,
    `<text x="16" y="48" fill="#fff" font-size="28">${nodeId} ${port}</text>`,
    `</svg>`,
  ].join("");
}

async function fulfillJson(route: Route, body: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/**
 * Routes every request the kiosk makes: the static bundle from `dist/`, and the
 * LocalServer's auth, registrations, executions, results, metadata, and
 * node-image routes from the scenario.
 */
async function installStubs(page: Page, scenario: Scenario): Promise<void> {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;

    // ---- the built static bundle (the LocalServer's /hmi mount, R6.6) ----
    if (pathname.startsWith("/hmi/")) {
      // URL resolution normalizes any `..`, so the prefix check below is a
      // sufficient containment guard for the served directory.
      const fileUrl = new URL(pathname.slice("/hmi/".length), distUrl);
      if (!fileUrl.href.startsWith(distUrl.href)) {
        await route.fulfill({ status: 403, body: "" });
        return;
      }
      try {
        await route.fulfill({ path: decodeURIComponent(fileUrl.pathname) });
      } catch {
        await route.fulfill({ status: 404, body: "" });
      }
      return;
    }

    // ---- LocalServer routes ----
    if (pathname === "/local-auth/status") {
      // No credentials needed on this device (Requirement 1.8).
      await fulfillJson(route, { localLoginEnabled: false });
      return;
    }
    if (pathname === "/workflows/registrations") {
      await fulfillJson(route, [REGISTRATION]);
      return;
    }
    if (/^\/workflows\/registrations\/[^/]+\/executions$/.test(pathname)) {
      await fulfillJson(route, scenario.executions);
      return;
    }
    if (/^\/workflows\/executions\/[^/]+\/results$/.test(pathname)) {
      await fulfillJson(route, resultsPayload());
      return;
    }
    if (/^\/workflows\/executions\/[^/]+\/metadata$/.test(pathname)) {
      await fulfillJson(route, scenario.metadata);
      return;
    }
    if (/^\/workflows\/executions\/[^/]+\/node-image$/.test(pathname)) {
      const nodeId = url.searchParams.get("nodeId") ?? "";
      const port = url.searchParams.get("port") ?? "";
      await route.fulfill({
        status: 200,
        contentType: "image/svg+xml",
        body: plateSvg(port, nodeId),
      });
      return;
    }

    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

// --------------------------------------------------------------------------
// Measurement
// --------------------------------------------------------------------------

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface LabeledRect {
  label: string;
  rect: Rect;
}

interface PanelMeasurement {
  slotNumber: number;
  /** "ANNOTATED" | "ORIGINAL", read from the rendered panel label. */
  label: string;
  frame: Rect;
  imageLoaded: boolean;
  objectFit: string;
  naturalWidth: number;
  naturalHeight: number;
  /** The `<img>` box; with `contain` the content is laid out inside it. */
  imgRect: Rect;
}

interface VerdictMeasurement {
  /** "run" for the header verdict, "slot-N" for a slot's own verdict. */
  scope: string;
  text: string;
  rect: Rect;
}

interface LayoutSnapshot {
  viewport: { width: number; height: number };
  scroll: {
    scrollWidth: number;
    clientWidth: number;
    scrollHeight: number;
    clientHeight: number;
  };
  /** Everything Requirement 6.1 calls primary Live_View content. */
  primary: LabeledRect[];
  /** The three horizontal bands, which must not overlap each other. */
  bands: LabeledRect[];
  /** The three Inspection_Slots (Requirement 6.2). */
  slots: LabeledRect[];
  panels: PanelMeasurement[];
  verdicts: VerdictMeasurement[];
  historyTiles: number;
}

/** Reads the rendered geometry of the kiosk page in one round trip. */
async function measureLayout(page: Page): Promise<LayoutSnapshot> {
  return page.evaluate(() => {
    const rectOf = (element: Element): Rect => {
      const r = element.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const shown = (element: Element): boolean => {
      const r = element.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const all = (selector: string, scope: ParentNode = document): Element[] =>
      Array.from(scope.querySelectorAll(selector)).filter(shown);
    const one = (selector: string, scope: ParentNode = document): Element | null => {
      const found = all(selector, scope);
      return found.length > 0 ? (found[0] as Element) : null;
    };

    const primary: LabeledRect[] = [];
    const push = (label: string, element: Element | null): void => {
      if (element !== null) primary.push({ label, rect: rectOf(element) });
    };

    push("workflow-name", one(".triple-kiosk .workflow-name"));
    push("run-timing", one(".triple-kiosk .run-timing"));
    push("run-verdict", one(".triple-kiosk .run-verdict"));
    push("connection-badge", one(".triple-kiosk .connection-badge"));

    const bands: LabeledRect[] = [];
    for (const [label, selector] of [
      ["header", ".triple-kiosk .header"],
      ["main", ".triple-kiosk .main"],
      ["history-strip", ".triple-kiosk .history-strip"],
    ] as const) {
      const element = one(selector);
      if (element !== null) bands.push({ label, rect: rectOf(element) });
    }

    const slots: LabeledRect[] = [];
    const panels: PanelMeasurement[] = [];
    const verdicts: VerdictMeasurement[] = [];

    const runVerdictLabel = one(".triple-kiosk .run-verdict .verdict-label");
    if (runVerdictLabel !== null) {
      verdicts.push({
        scope: "run",
        text: runVerdictLabel.textContent ?? "",
        rect: rectOf(runVerdictLabel),
      });
    }

    for (const slotNumber of [1, 2, 3]) {
      const slot = one(`.triple-kiosk .slot[data-slot="${slotNumber}"]`);
      if (slot === null) continue;
      slots.push({ label: `slot-${slotNumber}`, rect: rectOf(slot) });
      push(`slot-${slotNumber}-label`, one(".slot-label", slot));

      const slotVerdict = one(".slot-verdict .verdict-label", slot);
      if (slotVerdict !== null) {
        verdicts.push({
          scope: `slot-${slotNumber}`,
          text: slotVerdict.textContent ?? "",
          rect: rectOf(slotVerdict),
        });
        primary.push({ label: `slot-${slotNumber}-verdict`, rect: rectOf(slotVerdict) });
      }

      for (const panel of all(".image-panel", slot)) {
        const frame = one(".image-frame", panel);
        const labelNode = one(".image-label", panel);
        if (frame === null) continue;
        const label = labelNode?.textContent ?? "";
        primary.push({ label: `slot-${slotNumber}-${label}-frame`, rect: rectOf(frame) });
        if (labelNode !== null) {
          primary.push({
            label: `slot-${slotNumber}-${label}-label`,
            rect: rectOf(labelNode),
          });
        }
        const img = frame.querySelector("img");
        const style = img !== null ? getComputedStyle(img) : null;
        panels.push({
          slotNumber,
          label,
          frame: rectOf(frame),
          imageLoaded: img !== null && img.naturalWidth > 0 && !img.classList.contains("hidden"),
          objectFit: style?.objectFit ?? "",
          naturalWidth: img?.naturalWidth ?? 0,
          naturalHeight: img?.naturalHeight ?? 0,
          imgRect: img !== null ? rectOf(img) : { x: 0, y: 0, width: 0, height: 0 },
        });
      }
    }

    const strip = one(".triple-kiosk .history-strip");
    if (strip !== null) primary.push({ label: "history-strip", rect: rectOf(strip) });
    const tiles = strip === null ? [] : all(".history-tile", strip);
    for (const [index, tile] of tiles.entries()) {
      primary.push({ label: `history-tile-${index}`, rect: rectOf(tile) });
    }

    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      scroll: {
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        scrollHeight: document.documentElement.scrollHeight,
        clientHeight: document.documentElement.clientHeight,
      },
      primary,
      bands,
      slots,
      panels,
      verdicts,
      historyTiles: tiles.length,
    };
  });
}

/** Overlap area of two rectangles; 0 when they only touch. */
function overlapArea(a: Rect, b: Rect): number {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return width > 0 && height > 0 ? width * height : 0;
}

/** Asserts that no two rectangles of the group overlap (Requirements 6.1, 6.5). */
function expectNoOverlap(group: LabeledRect[]): void {
  for (let i = 0; i < group.length; i++) {
    for (let j = i + 1; j < group.length; j++) {
      const a = group[i] as LabeledRect;
      const b = group[j] as LabeledRect;
      expect(
        overlapArea(a.rect, b.rect),
        `${a.label} overlaps ${b.label}`,
      ).toBeLessThanOrEqual(1);
    }
  }
}

/** The panels of one slot, in rendered order. */
function panelsOfSlot(snapshot: LayoutSnapshot, slotNumber: number): PanelMeasurement[] {
  return snapshot.panels.filter((panel) => panel.slotNumber === slotNumber);
}

// --------------------------------------------------------------------------
// Page setup
// --------------------------------------------------------------------------

/** Loads the kiosk and waits for the displayed run's six frames to render. */
async function openCompletedRun(page: Page): Promise<void> {
  await installStubs(page, completedScenario());
  await page.goto(KIOSK_URL);
  await expect(page.locator('.triple-kiosk .slot[data-slot="3"]')).toBeVisible();
  await page.waitForFunction(() => {
    const images = Array.from(
      document.querySelectorAll<HTMLImageElement>(".triple-kiosk .image-frame img"),
    );
    return images.length === 6 && images.every((img) => img.naturalWidth > 0);
  });
}

/** Loads the kiosk with a failed latest run: the run-level ⚠ ERROR state. */
async function openFailedRun(page: Page): Promise<void> {
  await installStubs(page, failedScenario());
  await page.goto(KIOSK_URL);
  await expect(page.locator(".triple-kiosk .run-verdict .verdict-label")).toHaveText(
    "⚠ ERROR",
  );
}

// --------------------------------------------------------------------------
// Requirement 6.1 / 6.2 / 5.3 / 6.8 / 6.4 — the 1920x1080 kiosk viewport
// --------------------------------------------------------------------------

test.describe("Kiosk_Display at 1920x1080", () => {
  test.use({ viewport: { width: 1920, height: 1080 } });

  test("shows all primary Live_View content without scrolling or overlap (6.1)", async ({
    page,
  }) => {
    await openCompletedRun(page);
    const snapshot = await measureLayout(page);

    // The bands, the three slots with their two labeled panels each, the
    // header identity/timing/verdict, and the history strip are all present.
    expect(snapshot.bands.map((band) => band.label)).toEqual([
      "header",
      "main",
      "history-strip",
    ]);
    expect(snapshot.slots).toHaveLength(3);
    expect(snapshot.panels).toHaveLength(6);
    expect(snapshot.historyTiles).toBeGreaterThanOrEqual(5);
    for (const label of ["workflow-name", "run-timing", "run-verdict"]) {
      expect(
        snapshot.primary.some((item) => item.label === label),
        `${label} is not rendered`,
      ).toBe(true);
    }

    // Nothing scrolls in either axis at the kiosk's own resolution.
    expect(snapshot.scroll.scrollWidth).toBeLessThanOrEqual(
      snapshot.scroll.clientWidth + 1,
    );
    expect(snapshot.scroll.scrollHeight).toBeLessThanOrEqual(
      snapshot.scroll.clientHeight + 1,
    );

    // Every primary element is non-empty and fully inside the viewport.
    const { width, height } = snapshot.viewport;
    for (const item of snapshot.primary) {
      expect(item.rect.width, `${item.label} has zero width`).toBeGreaterThan(0);
      expect(item.rect.height, `${item.label} has zero height`).toBeGreaterThan(0);
      expect(item.rect.x, `${item.label} starts left of the viewport`).toBeGreaterThan(
        -0.5,
      );
      expect(item.rect.y, `${item.label} starts above the viewport`).toBeGreaterThan(-0.5);
      expect(
        item.rect.x + item.rect.width,
        `${item.label} extends past the right edge`,
      ).toBeLessThanOrEqual(width + 0.5);
      expect(
        item.rect.y + item.rect.height,
        `${item.label} extends past the bottom edge`,
      ).toBeLessThanOrEqual(height + 0.5);
    }

    // No overlap between the bands, between the slots, between the two panels
    // of a slot, or between the header's own items.
    expectNoOverlap(snapshot.bands);
    expectNoOverlap(snapshot.slots);
    for (const slotNumber of [1, 2, 3]) {
      expectNoOverlap(
        panelsOfSlot(snapshot, slotNumber).map((panel) => ({
          label: `slot-${slotNumber}-${panel.label}`,
          rect: panel.frame,
        })),
      );
    }
    expectNoOverlap(
      snapshot.primary.filter((item) =>
        ["workflow-name", "run-timing", "run-verdict", "connection-badge"].includes(
          item.label,
        ),
      ),
    );
  });

  test("sizes the three slots equally and proportions panels for the 1:2 plate (6.2)", async ({
    page,
  }) => {
    await openCompletedRun(page);
    const snapshot = await measureLayout(page);

    const widths = snapshot.slots.map((slot) => slot.rect.width);
    const spread = Math.max(...widths) - Math.min(...widths);
    expect(spread, `slot widths differ: ${widths.join(", ")}`).toBeLessThanOrEqual(2);

    for (const panel of snapshot.panels) {
      const ratio = panel.frame.height / panel.frame.width;
      // 1:1.8 – 1:2.2, the band that matches the 1.5" x 3" plates.
      expect(
        ratio,
        `slot ${panel.slotNumber} ${panel.label} panel ratio 1:${ratio.toFixed(3)}`,
      ).toBeGreaterThanOrEqual(1.8);
      expect(
        ratio,
        `slot ${panel.slotNumber} ${panel.label} panel ratio 1:${ratio.toFixed(3)}`,
      ).toBeLessThanOrEqual(2.2);
    }
  });

  test("renders both images of a slot at equal heights, aspect preserved, uncropped, >=280px wide (5.3, 6.8)", async ({
    page,
  }) => {
    await openCompletedRun(page);
    const snapshot = await measureLayout(page);

    const naturalRatio = IMAGE_NATURAL.width / IMAGE_NATURAL.height;

    for (const slotNumber of [1, 2, 3]) {
      const panels = panelsOfSlot(snapshot, slotNumber);
      // The annotated and original panels, each labeled (Requirement 5.2).
      expect(panels.map((panel) => panel.label)).toEqual(["ANNOTATED", "ORIGINAL"]);

      const contentSizes = panels.map((panel) => {
        expect(panel.imageLoaded, `slot ${slotNumber} ${panel.label} has no image`).toBe(
          true,
        );
        expect(panel.naturalWidth).toBe(IMAGE_NATURAL.width);
        expect(panel.naturalHeight).toBe(IMAGE_NATURAL.height);
        // `contain` is what makes the frame preserve aspect and never crop.
        expect(panel.objectFit).toBe("contain");

        // The minimum rendered width at 1920x1080 (Requirement 6.8).
        expect(
          panel.frame.width,
          `slot ${slotNumber} ${panel.label} is ${panel.frame.width.toFixed(1)}px wide`,
        ).toBeGreaterThanOrEqual(280);

        // The `<img>` box stays inside its frame, so nothing is clipped away.
        expect(panel.imgRect.width).toBeLessThanOrEqual(panel.frame.width + 0.5);
        expect(panel.imgRect.height).toBeLessThanOrEqual(panel.frame.height + 0.5);

        // The contained content: fully inside the box, at the source ratio.
        const scale = Math.min(
          panel.imgRect.width / panel.naturalWidth,
          panel.imgRect.height / panel.naturalHeight,
        );
        const content = {
          width: panel.naturalWidth * scale,
          height: panel.naturalHeight * scale,
        };
        expect(content.width).toBeLessThanOrEqual(panel.imgRect.width + 0.5);
        expect(content.height).toBeLessThanOrEqual(panel.imgRect.height + 0.5);
        expect(
          content.width / content.height,
          `slot ${slotNumber} ${panel.label} distorts the source aspect ratio`,
        ).toBeCloseTo(naturalRatio, 2);
        return content;
      });

      const [annotated, original] = contentSizes;
      expect(annotated).toBeDefined();
      expect(original).toBeDefined();
      // Equal display heights within the slot (Requirement 5.3).
      expect(
        Math.abs((annotated?.height ?? 0) - (original?.height ?? 0)),
        `slot ${slotNumber} image heights differ`,
      ).toBeLessThanOrEqual(1);
      expect(
        Math.abs(
          (panels[0]?.frame.height ?? 0) - (panels[1]?.frame.height ?? 0),
        ),
        `slot ${slotNumber} panel heights differ`,
      ).toBeLessThanOrEqual(1);
    }
  });

  test("renders the per-slot and run-level verdict states at >=32px text height (6.4)", async ({
    page,
  }) => {
    await openCompletedRun(page);
    const snapshot = await measureLayout(page);

    // The stubbed run exercises three states at once: slot 1 PASS, slot 2
    // FAIL, slot 3 NO VERDICT, plus the run-level verdict in the header.
    const texts = snapshot.verdicts.map((verdict) => verdict.text.trim());
    expect(texts).toContain("✔ PASS");
    expect(texts).toContain("✘ FAIL");
    expect(texts).toContain("— NO VERDICT");
    expect(
      snapshot.verdicts.some((verdict) => verdict.scope === "run"),
      "no run-level verdict rendered",
    ).toBe(true);

    for (const verdict of snapshot.verdicts) {
      expect(
        verdict.rect.height,
        `${verdict.scope} verdict "${verdict.text.trim()}" renders ${verdict.rect.height.toFixed(1)}px tall`,
      ).toBeGreaterThanOrEqual(32);
    }
  });

  test("renders the failed-run verdict state at >=32px text height (6.4, 5.9)", async ({
    page,
  }) => {
    await openFailedRun(page);
    const snapshot = await measureLayout(page);

    const runVerdict = snapshot.verdicts.find((verdict) => verdict.scope === "run");
    expect(runVerdict).toBeDefined();
    expect(runVerdict?.text.trim()).toBe("⚠ ERROR");
    expect(
      runVerdict?.rect.height ?? 0,
      `failed-run verdict renders ${(runVerdict?.rect.height ?? 0).toFixed(1)}px tall`,
    ).toBeGreaterThanOrEqual(32);

    // A failed run carries no images at all, so the slots hold placeholders
    // only — nothing from a prior run leaks in (Requirement 5.9).
    for (const panel of snapshot.panels) {
      expect(panel.imageLoaded).toBe(false);
    }
  });
});

// --------------------------------------------------------------------------
// Requirement 6.5 — the 1280-to-1920 viewport width band
// --------------------------------------------------------------------------

for (const width of [1280, 1920]) {
  test.describe(`Kiosk_Display at ${width}px wide`, () => {
    test.use({ viewport: { width, height: 1024 } });

    test("keeps primary content free of horizontal overflow and overlap (6.5)", async ({
      page,
    }) => {
      await openCompletedRun(page);
      const snapshot = await measureLayout(page);

      expect(snapshot.viewport.width).toBe(width);
      // No horizontal scrolling: the document is no wider than its viewport.
      expect(
        snapshot.scroll.scrollWidth,
        `document is ${snapshot.scroll.scrollWidth}px wide at ${width}px`,
      ).toBeLessThanOrEqual(snapshot.scroll.clientWidth + 1);

      for (const item of snapshot.primary) {
        expect(item.rect.x, `${item.label} starts left of the viewport`).toBeGreaterThan(
          -0.5,
        );
        expect(
          item.rect.x + item.rect.width,
          `${item.label} extends past the right edge at ${width}px`,
        ).toBeLessThanOrEqual(snapshot.viewport.width + 0.5);
      }

      // The same non-overlap guarantees hold across the whole width band.
      expectNoOverlap(snapshot.bands);
      expectNoOverlap(snapshot.slots);
      for (const slotNumber of [1, 2, 3]) {
        expectNoOverlap(
          panelsOfSlot(snapshot, slotNumber).map((panel) => ({
            label: `slot-${slotNumber}-${panel.label}`,
            rect: panel.frame,
          })),
        );
      }
    });
  });
}
