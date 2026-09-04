/**
 * Kiosk_Display rendering for the IMTS Triple Inspection HMI (task 10.1).
 *
 * `createTripleRenderer` builds the static DOM skeleton once — the login
 * screen plus the three-band Kiosk_Display shell (72 px header / main band /
 * 140 px history strip) whose class names are the layout contract of
 * `triple/kiosk.css` — and `render(state)` updates it incrementally from a
 * `TripleAppState`. Nothing is fetched, timed, or decided here: every piece of
 * displayed content is a projection of the state the pure modules produced
 * (`triple/machine.ts`, `triple/verdicts.ts`, `triple/inspections.ts`,
 * `triple/history.ts`), so the renderer holds no verdict, ordering, or
 * selection semantics of its own.
 *
 * Covered display criteria:
 *
 *  - **Header** (6.1, 6.3, 6.9): the Target_Workflow's name, the displayed
 *    run's `startedAt` and — only when present — its `finishedAt`, both
 *    rendered through `logic/format.ts` in local time with seconds precision;
 *    the run-level verdict once at run level (5.6, 5.11) as an icon plus a
 *    distinct word at the stylesheet's ≥32 px size (5.5, 6.4); the
 *    in-progress indicator (3.4); the connection indicator with the
 *    last-successful-update time (8.1, 8.4); and the stale-data indicator
 *    (3.9).
 *  - **Three Inspection_Slots** (5.1, 5.2, 5.3, 5.4): all three rendered
 *    simultaneously, each with its slot identifier, its own per-Inspection
 *    verdict, and the labeled ANNOTATED and ORIGINAL panels side by side —
 *    equal panel heights, aspect preserved and uncropped by the stylesheet's
 *    `aspect-ratio` + `object-fit: contain` frames.
 *  - **History strip** (7.1, 7.3, 7.6): newest-first clickable tiles carrying
 *    each run's verdict state and start time, the selected-tile marker and
 *    historical-view indicator, the newer-run notice (7.4), the
 *    return-to-live control (7.5), and the zero-history message.
 *  - **Empty and error states**, all read from the view models: not deployed
 *    (2.4), no runs recorded (2.6), no completed runs yet (3.7),
 *    no-inspection-data slots (4.6), the more-inspections indicator (4.7),
 *    verdict data unavailable (4.8), inspection data unavailable (4.9), the
 *    failed-run summary (5.9), and the historical-fetch error while the
 *    history strip and the return control stay available (7.7).
 *
 * Image loading itself is deliberately *not* here: each panel exposes a small
 * handle and the renderer calls the `loadImage` seam only when a panel's
 * (`executionId`, `nodeId`, `port`) triple changes, so the 2-second state
 * churn never reloads a frame. That loader is `triple/images.ts` (token URL
 * via `api/routes.ts`, 10 s per-image timeout, per-panel placeholder on error
 * or timeout — Requirements 4.5, 4.11, 5.8); it is used by default and the
 * option exists so tests can substitute their own.
 */

import { formatDateTime, formatTime } from "../logic/format";
import { createPanelImageLoader } from "./images";
import type { HistoryEntry } from "./history";
import type { ImageRef, SlotNumber } from "./inspections";
import { isStaleData, type RunResultVM, type TripleAppState } from "./machine";
import {
  VERDICT_PRESENTATION,
  type InspectionSlotVM,
  type VerdictState,
} from "./verdicts";

// --------------------------------------------------------------------------
// Static copy (exported so tests and callers never duplicate the wording)
// --------------------------------------------------------------------------

export const TRIPLE_MESSAGES = {
  /** Panel label of an Inspection's Annotated_Image (Requirement 5.2). */
  annotatedLabel: "ANNOTATED",
  /** Panel label of an Inspection's Original_Image (Requirement 5.2). */
  originalLabel: "ORIGINAL",
  /** No registrations payload has been bound yet. */
  bindingPendingTitle: "Looking for the workflow",
  /** No active registration matches the target name (Requirement 2.4). */
  notDeployedTitle: "Workflow not deployed",
  /** The Target_Workflow exists but has no runs at all (Requirement 2.6). */
  noRunsTitle: "No runs have been recorded",
  noRunsBody: "Waiting for the first run of this workflow\u2026",
  /** Runs exist but none has reached a terminal status (Requirement 3.7). */
  noCompletedRuns: "No completed runs yet",
  /** A slot the inventory yielded no Inspection for (Requirement 4.6). */
  noInspectionData: "No inspection data for this slot",
  /** The inventory yielded more than three Inspections (Requirement 4.7). */
  moreInspections: "Additional inspection images exist",
  /** `/metadata` failed after its single retry (Requirement 4.8). */
  verdictDataUnavailable: "Verdict data unavailable",
  /** `/results` failed after its single retry (Requirement 4.9). */
  inspectionDataUnavailable: "Inspection data unavailable",
  /** A failed run carries no Inspection images at all (Requirement 5.9). */
  failedRunSlot: "No inspection images for a failed run",
  /** The Inspection has no `annotated` entry (Requirement 4.10). */
  noAnnotatedImage: "No annotated image available",
  /** The Inspection has no `original` entry either. */
  noOriginalImage: "No original image available",
  /** The displayed run's data is still being fetched. */
  loading: "Loading\u2026",
  /** A selected historical run's data could not be fetched (Req. 7.7). */
  historicalDataError: "Selected run data unavailable",
  /** Zero runs exist, so the strip has no tiles (Requirement 7.6). */
  historyEmpty: "No run history available",
  inProgress: "\u27F3 RUN IN PROGRESS",
  connected: "\u25CF CONNECTED",
  disconnected: "\u25B2 DISCONNECTED",
  /** Five or more consecutive failed poll cycles (Requirement 3.9). */
  stale: "DATA MAY BE STALE",
  viewingHistory: "\u25C9 VIEWING HISTORY",
  newerRunAvailable: "Newer run available",
  returnToLive: "RETURN TO LIVE",
  loginTitle: "IMTS Triple Inspection",
  loginSubmit: "Sign in",
  loginDisabled: "Local login is disabled on this device.",
  loginRejected: "Username or password is incorrect.",
  loginUnreachable: "Could not reach the device. Check the connection and try again.",
} as const;

/** CSS class per verdict state; the icon + word carry the state (R5.5). */
const VERDICT_CLASS: Readonly<Record<VerdictState, string>> = {
  pass: "verdict-pass",
  fail: "verdict-fail",
  "no-verdict": "verdict-none",
  "failed-run": "verdict-error",
};

const VERDICT_CLASSES = Object.values(VERDICT_CLASS);

// --------------------------------------------------------------------------
// Public interfaces
// --------------------------------------------------------------------------

/** Operator intents; the wiring lives in `triple/main.ts` (task 11.3). */
export interface TripleRenderCallbacks {
  onLoginSubmit(username: string, password: string): void;
  /** A history tile was clicked (Requirement 7.3). */
  onHistorySelect(executionId: string): void;
  /** The return-to-live control was activated (Requirement 7.5). */
  onReturnToLive(): void;
}

/**
 * One image panel's frame, handed to the image loader (task 10.2). The loader
 * owns the `<img>` element's `src` and its load/error/timeout handling; the
 * renderer owns which of the two children is visible.
 */
export interface ImagePanelHandle {
  readonly img: HTMLImageElement;
  /** Reveals the `<img>` and hides the placeholder. */
  showImage(): void;
  /** Hides the `<img>` and shows `text` in its place (never substituting). */
  showPlaceholder(text: string): void;
}

/**
 * Loads one panel's image (task 10.2). Called only when the panel's
 * (`executionId`, `nodeId`, `port`) triple changes, so a stable panel is never
 * reloaded. `ref` always belongs to the panel's own Inspection and port, and
 * `executionId` to the displayed run, so no substitution across inspections,
 * ports, or runs is possible here (Requirements 4.11, 5.8).
 */
export type PanelImageLoader = (
  panel: ImagePanelHandle,
  executionId: string,
  ref: ImageRef,
) => void;

export interface TripleRendererOptions {
  /** Omitted → the default `triple/images.ts` loader (task 10.2). */
  loadImage?: PanelImageLoader;
}

export interface TripleRenderer {
  render(state: TripleAppState): void;
}

// --------------------------------------------------------------------------
// Skeleton helpers
// --------------------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className !== undefined) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toggleHidden(node: HTMLElement, hidden: boolean): void {
  node.classList.toggle("hidden", hidden);
}

/** A verdict line: icon + word, plus an optional confidence (R5.5, 5.7). */
interface VerdictView {
  root: HTMLElement;
  label: HTMLElement;
  confidence: HTMLElement;
}

function buildVerdict(extraClass: string): VerdictView {
  const root = el("div", `${extraClass} verdict hidden`);
  root.setAttribute("aria-live", "polite");
  const label = el("span", "verdict-label");
  const confidence = el("span", "verdict-confidence");
  root.append(label, confidence);
  return { root, label, confidence };
}

/**
 * Renders a verdict state, or hides the line when there is none — a run whose
 * metadata carries no verdict fields shows no verdict content rather than an
 * error (Requirement 5.10), and run-level verdicts are never duplicated into
 * the slots (Requirement 5.6).
 */
function setVerdict(
  view: VerdictView,
  state: VerdictState | null,
  confidenceText?: string | undefined,
): void {
  view.root.classList.remove(...VERDICT_CLASSES);
  toggleHidden(view.root, state === null);
  if (state === null) {
    view.label.textContent = "";
    view.confidence.textContent = "";
    return;
  }
  const presentation = VERDICT_PRESENTATION[state];
  view.root.classList.add(VERDICT_CLASS[state]);
  // Icon *and* distinct word: no state is distinguished by color alone (5.5).
  view.label.textContent = `${presentation.icon} ${presentation.word}`;
  view.confidence.textContent =
    confidenceText !== undefined ? `conf ${confidenceText}` : "";
}

/** One labeled image panel of a slot (Requirement 5.2). */
interface PanelView {
  root: HTMLElement;
  handle: ImagePanelHandle;
  /** The (executionId, nodeId, port) triple currently loaded, if any. */
  key: string | null;
}

function buildPanel(labelText: string): PanelView {
  const root = el("div", "image-panel");
  const frame = el("div", "image-frame");
  const img = el("img");
  img.alt = labelText;
  const placeholder = el("div", "image-placeholder");
  frame.append(img, placeholder);
  // The label identifies which panel is annotated and which is original (5.2).
  root.append(frame, el("div", "image-label", labelText));

  const handle: ImagePanelHandle = {
    img,
    showImage(): void {
      img.classList.remove("hidden");
      placeholder.classList.add("hidden");
    },
    showPlaceholder(text: string): void {
      placeholder.textContent = text;
      placeholder.classList.remove("hidden");
      img.classList.add("hidden");
    },
  };
  handle.showPlaceholder("");
  return { root, handle, key: null };
}

/** One Inspection_Slot's elements. */
interface SlotView {
  slotNumber: SlotNumber;
  root: HTMLElement;
  label: HTMLElement;
  verdict: VerdictView;
  note: HTMLElement;
  annotated: PanelView;
  original: PanelView;
}

function buildSlot(slotNumber: SlotNumber): SlotView {
  const root = el("section", "slot");
  root.dataset["slot"] = String(slotNumber);

  const heading = el("div", "slot-heading");
  // Slot identifier derived from the inventory-key slot assignment (R5.4).
  const label = el("div", "slot-label", `SLOT ${slotNumber}`);
  const verdict = buildVerdict("slot-verdict");
  heading.append(label, verdict.root);

  const note = el("div", "slot-note hidden");
  const panels = el("div", "slot-panels");
  // Annotated first, original beside it, at equal heights (5.2, 5.3).
  const annotated = buildPanel(TRIPLE_MESSAGES.annotatedLabel);
  const original = buildPanel(TRIPLE_MESSAGES.originalLabel);
  panels.append(annotated.root, original.root);

  root.append(heading, note, panels);
  return { slotNumber, root, label, verdict, note, annotated, original };
}

function setNote(node: HTMLElement, text: string): void {
  node.textContent = text;
  toggleHidden(node, text === "");
}

// --------------------------------------------------------------------------
// Renderer
// --------------------------------------------------------------------------

export function createTripleRenderer(
  root: HTMLElement,
  callbacks: TripleRenderCallbacks,
  options: TripleRendererOptions = {},
): TripleRenderer {
  // Every panel URL carries the Session_Token in its query and every request
  // is bounded by a 10 s timeout (Requirement 4.5).
  const loadImage: PanelImageLoader = options.loadImage ?? createPanelImageLoader();

  // ---- login screen (Requirement 1) ----
  const loginScreen = el("div", "login-screen");
  const loginForm = el("form", "login-box");
  const userInput = el("input");
  userInput.name = "username";
  userInput.autocomplete = "username";
  const userLabel = el("label", undefined, "Username");
  userLabel.append(userInput);
  const passInput = el("input");
  passInput.type = "password";
  passInput.name = "password";
  passInput.autocomplete = "current-password";
  const passLabel = el("label", undefined, "Password");
  passLabel.append(passInput);
  const loginError = el("div", "login-error");
  loginError.setAttribute("role", "alert");
  const loginButton = el("button", undefined, TRIPLE_MESSAGES.loginSubmit);
  loginButton.type = "submit";
  loginForm.append(
    el("h1", undefined, TRIPLE_MESSAGES.loginTitle),
    userLabel,
    passLabel,
    loginError,
    loginButton,
  );
  loginForm.addEventListener("submit", (event) => {
    event.preventDefault();
    callbacks.onLoginSubmit(userInput.value, passInput.value);
  });
  loginScreen.append(loginForm);

  // ---- kiosk shell: header ----
  const kiosk = el("div", "triple-kiosk");
  const header = el("header", "header");
  const workflowName = el("h1", "workflow-name");
  const runTiming = el("div", "run-timing");
  const runNote = el("div", "run-note hidden");
  const runVerdict = buildVerdict("run-verdict");
  const inProgressBadge = el("div", "in-progress-badge hidden", TRIPLE_MESSAGES.inProgress);
  const connectionBadge = el("div", "connection-badge connected");
  const connectionWord = el("span", "connection-word", TRIPLE_MESSAGES.connected);
  const connectionLastUpdate = el("span", "connection-last-update");
  const staleBadge = el("span", "stale-badge hidden", TRIPLE_MESSAGES.stale);
  connectionBadge.append(connectionWord, connectionLastUpdate, staleBadge);
  header.append(
    workflowName,
    runTiming,
    runNote,
    runVerdict.root,
    inProgressBadge,
    connectionBadge,
  );

  // ---- kiosk shell: main band with the three Inspection_Slots ----
  const main = el("main", "main");
  const mainMessage = el("div", "main-message hidden");
  const messageTitle = el("div", "message-title");
  const messageBody = el("div", "message-body");
  mainMessage.append(messageTitle, messageBody);
  const slotViews: SlotView[] = [1, 2, 3].map((slotNumber) =>
    buildSlot(slotNumber as SlotNumber),
  );
  const moreInspectionsBadge = el(
    "div",
    "more-inspections-badge hidden",
    TRIPLE_MESSAGES.moreInspections,
  );
  main.append(
    mainMessage,
    ...slotViews.map((view) => view.root),
    moreInspectionsBadge,
  );

  // ---- kiosk shell: history strip ----
  const historyStrip = el("footer", "history-strip");
  const historyTiles = el("div", "history-tiles");
  const historyEmpty = el("div", "history-empty hidden", TRIPLE_MESSAGES.historyEmpty);
  const historySide = el("div", "history-side");
  const historyBanner = el("div", "history-banner hidden", TRIPLE_MESSAGES.viewingHistory);
  const newerRunNotice = el(
    "div",
    "newer-run-notice hidden",
    TRIPLE_MESSAGES.newerRunAvailable,
  );
  const returnToLive = el("button", "return-to-live hidden", TRIPLE_MESSAGES.returnToLive);
  returnToLive.type = "button";
  returnToLive.addEventListener("click", () => callbacks.onReturnToLive());
  historySide.append(historyBanner, newerRunNotice, returnToLive);
  historyStrip.append(historyTiles, historyEmpty, historySide);

  kiosk.append(header, main, historyStrip);
  root.replaceChildren(loginScreen, kiosk);

  // ---- panels -----------------------------------------------------------

  /**
   * Points a panel at one image reference, or at a placeholder.
   *
   * A placeholder never carries another Inspection's, port's, or run's image:
   * the `<img>` source is dropped with it. A reference is handed to the loader
   * only when its (`executionId`, `nodeId`, `port`) triple changes, so the
   * displayed frame is stable across polls and replaced exactly when the
   * displayed run changes (Requirement 3.6).
   */
  function updatePanel(
    panel: PanelView,
    executionId: string,
    ref: ImageRef | undefined,
    placeholderText: string,
  ): void {
    if (ref === undefined) {
      if (panel.key !== null) {
        panel.key = null;
        // Dropping the src also disowns any in-flight request for this panel,
        // so a late failure cannot overwrite this placeholder and a late
        // success cannot reveal a previous run's frame (Requirements 4.11, 5.8).
        panel.handle.img.removeAttribute("src");
      }
      panel.handle.showPlaceholder(placeholderText);
      return;
    }
    const key = `${executionId}\u0000${ref.nodeId}\u0000${ref.port}`;
    if (key === panel.key) return;
    panel.key = key;
    panel.handle.showImage();
    loadImage(panel.handle, executionId, ref);
  }

  /** Both panels of a slot show the same placeholder (no run content). */
  function placeholderSlot(view: SlotView, note: string, panelText: string): void {
    setVerdict(view.verdict, null);
    setNote(view.note, note);
    updatePanel(view.annotated, "", undefined, panelText);
    updatePanel(view.original, "", undefined, panelText);
  }

  /** Renders one Inspection_Slot of the displayed run (5.1, 5.2, 5.4, 5.5). */
  function renderSlot(view: SlotView, run: RunResultVM | null): void {
    if (run === null) {
      // Runs exist but none is terminal yet (Requirement 3.7).
      placeholderSlot(view, TRIPLE_MESSAGES.noCompletedRuns, TRIPLE_MESSAGES.noCompletedRuns);
      return;
    }
    if (run.resultsUnavailable) {
      // `/results` failed after its retry: the run's status stays on screen
      // while every slot reports the inventory as unavailable (4.9).
      placeholderSlot(
        view,
        TRIPLE_MESSAGES.inspectionDataUnavailable,
        TRIPLE_MESSAGES.inspectionDataUnavailable,
      );
      return;
    }
    if (run.failedRun !== undefined) {
      // Placeholders in all three slots, and no image from any prior run
      // (Requirement 5.9).
      placeholderSlot(view, TRIPLE_MESSAGES.failedRunSlot, TRIPLE_MESSAGES.failedRunSlot);
      return;
    }
    if (run.dataPending) {
      placeholderSlot(view, "", TRIPLE_MESSAGES.loading);
      return;
    }

    const slot: InspectionSlotVM =
      run.slots[view.slotNumber - 1] ?? { slotNumber: view.slotNumber };
    const inspection = slot.inspection;
    if (inspection === undefined) {
      // Fewer Inspections than slots (Requirement 4.6).
      placeholderSlot(view, TRIPLE_MESSAGES.noInspectionData, TRIPLE_MESSAGES.noInspectionData);
      return;
    }

    const verdict = slot.verdict;
    setVerdict(
      view.verdict,
      verdict === undefined ? null : verdict.state,
      verdict !== undefined && verdict.state !== "no-verdict"
        ? verdict.confidenceText
        : undefined,
    );
    setNote(view.note, "");

    const executionId = run.execution.executionId;
    // The annotated panel has no fallback image of any kind (4.10).
    updatePanel(
      view.annotated,
      executionId,
      inspection.annotated,
      TRIPLE_MESSAGES.noAnnotatedImage,
    );
    updatePanel(
      view.original,
      executionId,
      inspection.original,
      TRIPLE_MESSAGES.noOriginalImage,
    );
  }

  // ---- header -----------------------------------------------------------

  function renderHeader(state: TripleAppState): void {
    const bound = state.binding.state === "bound" ? state.binding.registration : null;
    // The workflow identity is shown even when no run content exists (2.6, 6.3).
    workflowName.textContent =
      bound !== null && bound.name !== null && bound.name !== ""
        ? bound.name
        : state.targetName;

    const run = state.live.displayed;
    if (run === null) {
      runTiming.textContent = "";
    } else {
      const started = `Started ${formatDateTime(run.execution.startedAt)}`;
      // The finish time is omitted exactly when `finishedAt` is absent (6.9).
      const finished =
        run.execution.finishedAt !== null
          ? `  Finished ${formatTime(run.execution.finishedAt)}`
          : "";
      runTiming.textContent = `${started}${finished}`;
    }

    // Run-level verdict, rendered once at run level (5.6, 5.11); a failed run
    // shows the failure state here instead (5.9).
    if (run !== null && run.failedRun !== undefined) {
      setVerdict(runVerdict, "failed-run");
    } else if (run !== null && run.runLevelVerdict !== undefined) {
      setVerdict(runVerdict, run.runLevelVerdict.state, run.runLevelVerdict.confidenceText);
    } else {
      setVerdict(runVerdict, null);
    }

    setNote(runNote, runNotes(state).join("  \u00B7  "));
    toggleHidden(inProgressBadge, !state.live.inProgress); // 3.4

    const connected = state.connection.state === "connected";
    connectionBadge.classList.toggle("connected", connected);
    connectionBadge.classList.toggle("disconnected", !connected);
    connectionWord.textContent = connected
      ? TRIPLE_MESSAGES.connected
      : TRIPLE_MESSAGES.disconnected;
    // The last successful update is retained across a disconnect (8.1).
    connectionLastUpdate.textContent =
      state.connection.lastSuccessfulUpdate !== null
        ? `last update ${formatTime(state.connection.lastSuccessfulUpdate / 1000)}`
        : "";
    toggleHidden(staleBadge, !isStaleData(state)); // 3.9
  }

  /** Run-level indications that accompany, rather than replace, the run. */
  function runNotes(state: TripleAppState): string[] {
    const notes: string[] = [];
    const run = state.live.displayed;
    if (run === null) return notes;
    if (run.failedRun !== undefined) notes.push(run.failedRun.errorSummary); // 5.9
    if (run.resultsUnavailable) notes.push(TRIPLE_MESSAGES.inspectionDataUnavailable); // 4.9
    if (run.metadataUnavailable) notes.push(TRIPLE_MESSAGES.verdictDataUnavailable); // 4.8
    // The historical-fetch error is indicated in the Live_View while the
    // history strip and the return-to-live control stay available (7.7).
    if (state.live.mode === "historical" && state.live.historicalDataError) {
      notes.push(TRIPLE_MESSAGES.historicalDataError);
    }
    return notes;
  }

  // ---- main band --------------------------------------------------------

  /**
   * Full-band message states that replace the Run_Result content: the
   * not-deployed message (2.4) and the no-runs-recorded message (2.6).
   * Returns true when a message is shown.
   */
  function renderMessageState(state: TripleAppState): boolean {
    let title: string | null = null;
    let body = "";
    if (state.binding.state === "not-deployed") {
      title = TRIPLE_MESSAGES.notDeployedTitle;
      body = `The ${state.targetName} workflow is not deployed on this device.`;
    } else if (state.binding.state === "pending") {
      title = TRIPLE_MESSAGES.bindingPendingTitle;
      body = `Waiting for ${state.targetName}\u2026`;
    } else if (
      state.live.displayed === null &&
      state.live.latestExecutions.length === 0
    ) {
      title = TRIPLE_MESSAGES.noRunsTitle;
      body = TRIPLE_MESSAGES.noRunsBody;
    }

    toggleHidden(mainMessage, title === null);
    for (const view of slotViews) toggleHidden(view.root, title !== null);
    if (title === null) return false;

    messageTitle.textContent = title;
    messageBody.textContent = body;
    return true;
  }

  // ---- history strip ---------------------------------------------------

  let renderedHistoryKey: string | null = null;

  function historyKey(history: readonly HistoryEntry[], selectedId: string): string {
    const tiles = history
      .map((entry) => `${entry.executionId}:${entry.verdict}:${entry.startedAt}`)
      .join("|");
    return `${tiles}@${selectedId}`;
  }

  function buildTile(entry: HistoryEntry, selected: boolean): HTMLButtonElement {
    const tile = el("button", "history-tile");
    tile.type = "button";
    tile.dataset["executionId"] = entry.executionId;
    if (selected) tile.classList.add("selected"); // 7.3
    const presentation = VERDICT_PRESENTATION[entry.verdict];
    tile.append(
      el(
        "div",
        `tile-verdict ${VERDICT_CLASS[entry.verdict]}`,
        `${presentation.icon} ${presentation.word}`,
      ),
      el("div", "tile-time", formatTime(entry.startedAt)),
    );
    tile.addEventListener("click", () => callbacks.onHistorySelect(entry.executionId));
    return tile;
  }

  function renderHistory(state: TripleAppState): void {
    const { history, mode, displayed, newerRunAvailable } = state.live;
    const historical = mode === "historical";
    const selectedId = historical ? displayed?.execution.executionId ?? "" : "";

    // Tiles are rebuilt only when the strip's content or selection changes.
    const key = historyKey(history, selectedId);
    if (key !== renderedHistoryKey) {
      renderedHistoryKey = key;
      // Newest first, exactly the runs the history view model holds (7.1).
      historyTiles.replaceChildren(
        ...history.map((entry) => buildTile(entry, entry.executionId === selectedId)),
      );
      toggleHidden(historyEmpty, history.length > 0); // 7.6
    }

    toggleHidden(historyBanner, !historical); // 7.3
    toggleHidden(returnToLive, !historical); // 7.3, 7.5, 7.7
    toggleHidden(newerRunNotice, !(historical && newerRunAvailable)); // 7.4
  }

  // ---- render ----------------------------------------------------------

  function render(state: TripleAppState): void {
    const onLogin = state.auth.screen === "login";
    toggleHidden(loginScreen, !onLogin);
    toggleHidden(kiosk, onLogin);
    if (onLogin) {
      loginError.textContent =
        state.auth.loginError === "disabled"
          ? TRIPLE_MESSAGES.loginDisabled
          : state.auth.loginError === "rejected"
            ? TRIPLE_MESSAGES.loginRejected
            : state.auth.loginError === "unreachable"
              ? TRIPLE_MESSAGES.loginUnreachable
              : "";
      return;
    }

    renderHeader(state);

    if (!renderMessageState(state)) {
      const run = state.live.displayed;
      // All three slots are rendered simultaneously, every run (5.1).
      for (const view of slotViews) renderSlot(view, run);
      toggleHidden(moreInspectionsBadge, !(run?.moreInspections ?? false)); // 4.7
    } else {
      toggleHidden(moreInspectionsBadge, true);
    }

    renderHistory(state);
  }

  return { render };
}
