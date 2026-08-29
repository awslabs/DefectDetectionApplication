/**
 * DOM rendering and kiosk CSS (task 9.3).
 *
 * `createRenderer` builds the static skeleton once — login screen plus the
 * three-band kiosk layout (72 px header / main / 136 px history strip,
 * Requirement 6.1) — and `render(state)` updates it incrementally. Image
 * `src` attributes only change when their URL changes, so the 2-second state
 * churn never reloads or flickers the displayed frames.
 *
 * Covered display criteria:
 *  - Login form with credentials-rejected / login-disabled messages
 *    (Requirements 1.6, 1.7).
 *  - Header: workflow name + displayed run's start time (6.3), connection
 *    badge with last-update time (8.1), in-progress indicator (3.3).
 *  - Verdict panel: icon + word (never color alone, 4.2) at ≥ 48 px via
 *    clamp (6.2), confidence (4.3), truncated generated text with indicator
 *    (4.4), finished time (4.6), failed-run and no-verdict states (4.5, 4.7,
 *    4.8), metadata-unavailable indication (4.9).
 *  - Side-by-side labeled image panels with `object-fit: contain` and equal
 *    heights (5.1, 6.5); captured frame spans the full width when no
 *    reference exists (5.4); more-nodes badge (5.7); `<img>` onerror →
 *    per-panel "image unavailable" placeholder that never substitutes
 *    another port or run (5.6).
 *  - History strip tiles (7.1) with the historical-mode banner, newer-run
 *    notice, and return-to-live control (7.3, 7.4); zero-history message
 *    (7.7); no-runs / workflow-unavailable / no-workflows states (2.8, 6.4,
 *    8.5, 2.5); historical-data-error indication (7.8).
 *
 * The renderer is a thin shell: it dispatches operator intents through
 * `RenderCallbacks` and derives everything else from `AppState`.
 */

import { loadSession } from "../auth/session";
import { nodeImageUrl, outputImageUrl } from "../api/routes";
import type { ResultImage } from "../api/types";
import { formatDateTime, formatTime } from "../logic/format";
import { activeRegistrations, registrationLabel } from "../logic/selection";
import type { VerdictState } from "../logic/verdict";
import type { AppState } from "../app/machine";

// --------------------------------------------------------------------------
// Callbacks (operator intents; wiring lives in main.ts)
// --------------------------------------------------------------------------

export interface RenderCallbacks {
  onLoginSubmit(username: string, password: string): void;
  onSelectRegistration(registrationId: string): void;
  onHistorySelect(executionId: string): void;
  onReturnToLive(): void;
}

export interface Renderer {
  render(state: AppState): void;
}

// --------------------------------------------------------------------------
// Verdict presentation: icon AND word, never color alone (Requirement 4.2)
// --------------------------------------------------------------------------

const VERDICT_PRESENTATION: Record<VerdictState, { icon: string; word: string; className: string }> = {
  pass: { icon: "\u2714", word: "PASS", className: "verdict-pass" },
  fail: { icon: "\u2718", word: "FAIL", className: "verdict-fail" },
  "failed-run": { icon: "\u26A0", word: "ERROR", className: "verdict-error" },
  "no-verdict": { icon: "\u2014", word: "NO VERDICT", className: "verdict-none" },
};

// --------------------------------------------------------------------------
// Kiosk CSS (Requirements 6.1, 6.2, 6.5, 6.6)
// --------------------------------------------------------------------------

const KIOSK_CSS = `
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background: #0f1216; color: #e8eaed;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  overflow: hidden;
}
#app { height: 100vh; }
button { font: inherit; cursor: pointer; }
.hidden { display: none !important; }

/* ---- Login screen (Requirement 1) ---- */
.login-screen {
  height: 100%; display: flex; align-items: center; justify-content: center;
}
.login-box {
  width: 420px; padding: 40px; background: #171b21; border-radius: 12px;
  display: flex; flex-direction: column; gap: 16px;
}
.login-box h1 { font-size: 28px; font-weight: 600; }
.login-box label { display: flex; flex-direction: column; gap: 6px; font-size: 15px; }
.login-box input {
  padding: 12px; font-size: 17px; border-radius: 8px;
  border: 1px solid #3a4048; background: #0f1216; color: inherit;
}
.login-box button {
  padding: 14px; font-size: 18px; font-weight: 600; border: none;
  border-radius: 8px; background: #2f6fed; color: #fff;
}
.login-error { color: #ff8b8b; font-size: 15px; min-height: 20px; }

/* ---- Three-band kiosk layout (Requirement 6.1) ---- */
.kiosk {
  height: 100%; display: grid;
  grid-template-rows: 72px 1fr 136px;
  max-width: 100vw; overflow: hidden;                    /* 6.6 */
}

/* ---- Header (Requirements 6.3, 3.3, 8.1) ---- */
.header {
  display: flex; align-items: center; gap: 24px; padding: 0 24px;
  background: #171b21; border-bottom: 1px solid #262c34;
}
.workflow-name { font-size: 32px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.workflow-select {
  font-size: 16px; padding: 6px 10px; border-radius: 8px;
  background: #0f1216; color: inherit; border: 1px solid #3a4048;
}
.run-started { font-size: 18px; color: #aab2bd; margin-left: auto; white-space: nowrap; }
.in-progress-badge {
  font-size: 16px; font-weight: 600; color: #ffd166; white-space: nowrap;
  padding: 6px 12px; border: 1px solid #ffd166; border-radius: 8px;
}
.connection-badge { font-size: 16px; font-weight: 600; white-space: nowrap; text-align: right; }
.connection-badge.connected { color: #57d98a; }
.connection-badge.disconnected { color: #ff8b8b; }
.connection-last-update { display: block; font-size: 13px; font-weight: 400; color: #aab2bd; }

/* ---- Main band (Requirement 6.1) ---- */
.main {
  display: grid; grid-template-columns: minmax(360px, 440px) 1fr;
  gap: 16px; padding: 16px; min-height: 0;
}
.main-message {
  grid-column: 1 / -1; display: flex; flex-direction: column; gap: 12px;
  align-items: center; justify-content: center; text-align: center;
}
.main-message .message-title { font-size: 36px; font-weight: 600; }
.main-message .message-body { font-size: 20px; color: #aab2bd; }

/* ---- Verdict panel (Requirements 4.2, 6.2) ---- */
.verdict-panel {
  background: #171b21; border-radius: 12px; padding: 24px;
  display: flex; flex-direction: column; gap: 18px; min-height: 0; overflow: hidden;
}
.verdict-headline {
  display: flex; align-items: center; gap: 16px;
  font-size: clamp(48px, 3.75vw, 72px);                  /* >= 48px (6.2) */
  font-weight: 700; line-height: 1.1;
}
.verdict-pass .verdict-headline { color: #57d98a; }
.verdict-fail .verdict-headline { color: #ff6b6b; }
.verdict-error .verdict-headline { color: #ffd166; }
.verdict-none .verdict-headline { color: #aab2bd; }
.verdict-confidence { font-size: 24px; color: #e8eaed; }
.verdict-text {
  font-size: 17px; color: #c7cdd4; white-space: pre-wrap; overflow-y: auto;
  background: #0f1216; border-radius: 8px; padding: 12px; flex: 1 1 auto; min-height: 0;
}
.verdict-truncated { font-size: 14px; color: #aab2bd; font-style: italic; }
.verdict-finished { font-size: 16px; color: #aab2bd; }
.verdict-note { font-size: 15px; color: #ffd166; }

/* ---- Image area (Requirements 5.1, 5.4, 6.5) ---- */
.image-area { display: flex; gap: 16px; min-height: 0; position: relative; }
.image-panel {
  flex: 1 1 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; min-height: 0;
}
.image-frame {
  flex: 1 1 auto; min-height: 0; background: #171b21; border-radius: 12px;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.image-frame img { width: 100%; height: 100%; object-fit: contain; } /* 6.5 */
.image-placeholder { font-size: 22px; color: #aab2bd; text-align: center; padding: 24px; }
.image-label {
  font-size: 24px; font-weight: 600; letter-spacing: 0.08em; text-align: center; color: #aab2bd;
}
.more-nodes-badge {
  position: absolute; right: 12px; bottom: 44px; font-size: 15px;
  background: #262c34; border-radius: 8px; padding: 6px 12px; color: #c7cdd4;
}

/* ---- History strip (Requirements 7.1, 7.3, 7.7) ---- */
.history-strip {
  display: flex; align-items: stretch; gap: 12px; padding: 12px 24px;
  background: #171b21; border-top: 1px solid #262c34; overflow: hidden;
}
.history-tiles { display: flex; gap: 12px; flex: 1 1 auto; overflow: hidden; }
.history-empty { align-self: center; font-size: 18px; color: #aab2bd; }
.history-tile {
  min-width: 132px; border: 1px solid #3a4048; border-radius: 10px;
  background: #0f1216; color: inherit; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px; padding: 8px 12px;
}
.history-tile.selected { border-color: #2f6fed; background: #1a2333; }
.history-tile .tile-verdict { font-size: 18px; font-weight: 700; }
.history-tile .tile-time { font-size: 15px; color: #aab2bd; }
.history-tile .verdict-pass { color: #57d98a; }
.history-tile .verdict-fail { color: #ff6b6b; }
.history-tile .verdict-error { color: #ffd166; }
.history-tile .verdict-none { color: #aab2bd; }
.history-side { display: flex; flex-direction: column; justify-content: center; gap: 6px; min-width: 200px; }
.history-banner {
  font-size: 16px; font-weight: 700; color: #ffd166; text-align: center;
}
.newer-run-notice { font-size: 14px; color: #ffd166; text-align: center; }
.return-to-live {
  padding: 10px 16px; font-size: 16px; font-weight: 600; border: none;
  border-radius: 8px; background: #2f6fed; color: #fff;
}
`;

// --------------------------------------------------------------------------
// Skeleton construction helpers
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

/** One image panel: label + frame holding an <img> and its placeholder. */
interface ImagePanel {
  root: HTMLElement;
  img: HTMLImageElement;
  placeholder: HTMLElement;
  label: HTMLElement;
  currentUrl: string | null;
}

function buildImagePanel(labelText: string): ImagePanel {
  const root = el("div", "image-panel");
  const frame = el("div", "image-frame");
  const img = el("img");
  img.alt = labelText;
  const placeholder = el("div", "image-placeholder", "Image unavailable");
  const label = el("div", "image-label", labelText);
  frame.append(img, placeholder);
  root.append(frame, label);

  const panel: ImagePanel = { root, img, placeholder, label, currentUrl: null };
  // Per-panel placeholder on load failure; never substitute another port or
  // run (Requirement 5.6).
  img.addEventListener("error", () => {
    img.classList.add("hidden");
    placeholder.classList.remove("hidden");
  });
  img.addEventListener("load", () => {
    img.classList.remove("hidden");
    placeholder.classList.add("hidden");
  });
  return panel;
}

/** Updates a panel to show a URL (stable src → no reload), or a placeholder. */
function updateImagePanel(panel: ImagePanel, url: string | null, placeholderText: string): void {
  if (url === null) {
    panel.currentUrl = null;
    panel.img.removeAttribute("src");
    panel.img.classList.add("hidden");
    panel.placeholder.textContent = placeholderText;
    panel.placeholder.classList.remove("hidden");
    return;
  }
  if (url !== panel.currentUrl) {
    panel.currentUrl = url;
    panel.placeholder.textContent = "Image unavailable";
    // Optimistically show the image; onerror flips back to the placeholder.
    panel.img.classList.remove("hidden");
    panel.placeholder.classList.add("hidden");
    panel.img.src = url;
  }
}

/** Builds the image URL for one selected results entry (1.3, 5.5). */
function imageUrlFor(executionId: string, image: ResultImage, token: string): string | null {
  if (image.kind === "output") return outputImageUrl(executionId, token);
  if (image.nodeId === undefined || image.port === undefined) return null;
  return nodeImageUrl(executionId, image.nodeId, image.port, token);
}

// --------------------------------------------------------------------------
// Renderer
// --------------------------------------------------------------------------

export function createRenderer(root: HTMLElement, callbacks: RenderCallbacks): Renderer {
  // ---- one-time style injection ----
  if (document.getElementById("hmi-kiosk-style") === null) {
    const style = el("style");
    style.id = "hmi-kiosk-style";
    style.textContent = KIOSK_CSS;
    document.head.append(style);
  }

  // ---- login screen ----
  const loginScreen = el("div", "login-screen");
  const loginBox = el("form", "login-box");
  const loginTitle = el("h1", undefined, "Quality Station HMI");
  const userLabel = el("label", undefined, "Username");
  const userInput = el("input");
  userInput.name = "username";
  userInput.autocomplete = "username";
  userLabel.append(userInput);
  const passLabel = el("label", undefined, "Password");
  const passInput = el("input");
  passInput.type = "password";
  passInput.name = "password";
  passInput.autocomplete = "current-password";
  passLabel.append(passInput);
  const loginError = el("div", "login-error");
  loginError.setAttribute("role", "alert");
  const loginButton = el("button", undefined, "Sign in");
  loginButton.type = "submit";
  loginBox.append(loginTitle, userLabel, passLabel, loginError, loginButton);
  loginScreen.append(loginBox);
  loginBox.addEventListener("submit", (event) => {
    event.preventDefault();
    callbacks.onLoginSubmit(userInput.value, passInput.value);
  });

  // ---- kiosk: header ----
  const kiosk = el("div", "kiosk");
  const header = el("header", "header");
  const workflowName = el("h1", "workflow-name");
  const workflowSelect = el("select", "workflow-select");
  workflowSelect.setAttribute("aria-label", "Workflow");
  workflowSelect.addEventListener("change", () => {
    callbacks.onSelectRegistration(workflowSelect.value);
  });
  const runStarted = el("div", "run-started");
  const inProgressBadge = el("div", "in-progress-badge hidden", "\u27F3 RUN IN PROGRESS");
  const connectionBadge = el("div", "connection-badge connected");
  const connectionWord = el("span", undefined, "\u25CF CONNECTED");
  const connectionLastUpdate = el("span", "connection-last-update");
  connectionBadge.append(connectionWord, connectionLastUpdate);
  header.append(workflowName, workflowSelect, runStarted, inProgressBadge, connectionBadge);

  // ---- kiosk: main band ----
  const main = el("main", "main");
  const mainMessage = el("div", "main-message hidden");
  const messageTitle = el("div", "message-title");
  const messageBody = el("div", "message-body");
  mainMessage.append(messageTitle, messageBody);

  const verdictPanel = el("section", "verdict-panel verdict-none");
  verdictPanel.setAttribute("aria-live", "polite");
  const verdictHeadline = el("div", "verdict-headline");
  const verdictIcon = el("span", undefined);
  const verdictWord = el("span", undefined);
  verdictHeadline.append(verdictIcon, verdictWord);
  const verdictConfidence = el("div", "verdict-confidence");
  const verdictText = el("div", "verdict-text");
  const verdictTruncated = el("div", "verdict-truncated hidden", "Output truncated to 500 characters");
  const verdictNote = el("div", "verdict-note hidden");
  const verdictFinished = el("div", "verdict-finished");
  verdictPanel.append(verdictHeadline, verdictConfidence, verdictText, verdictTruncated, verdictNote, verdictFinished);

  const imageArea = el("section", "image-area");
  const referencePanel = buildImagePanel("REFERENCE");
  const capturedPanel = buildImagePanel("CAPTURED FRAME");
  const moreNodesBadge = el("div", "more-nodes-badge hidden", "+ more nodes");
  imageArea.append(referencePanel.root, capturedPanel.root, moreNodesBadge);

  main.append(mainMessage, verdictPanel, imageArea);

  // ---- kiosk: history strip ----
  const historyStrip = el("footer", "history-strip");
  const historyTiles = el("div", "history-tiles");
  const historyEmpty = el("div", "history-empty hidden", "No run history");
  const historySide = el("div", "history-side");
  const historyBanner = el("div", "history-banner hidden", "\u25C9 VIEWING HISTORY");
  const newerRunNotice = el("div", "newer-run-notice hidden", "Newer run available");
  const returnToLive = el("button", "return-to-live hidden", "RETURN TO LIVE");
  returnToLive.type = "button";
  returnToLive.addEventListener("click", () => callbacks.onReturnToLive());
  historySide.append(historyBanner, newerRunNotice, returnToLive);
  historyStrip.append(historyTiles, historyEmpty, historySide);

  kiosk.append(header, main, historyStrip);
  root.replaceChildren(loginScreen, kiosk);

  // ---- incremental render ----
  let renderedHistoryKey = "";

  function renderHistory(state: AppState): void {
    const { history, mode, displayedRun } = state.live;
    const selectedId = mode === "historical" ? displayedRun?.executionId ?? "" : "";
    const key = history.map((h) => `${h.executionId}:${h.verdict}`).join("|") + "@" + selectedId;
    if (key === renderedHistoryKey) return;
    renderedHistoryKey = key;

    historyTiles.replaceChildren(
      ...history.map((entry) => {
        const tile = el("button", "history-tile");
        tile.type = "button";
        if (entry.executionId === selectedId) tile.classList.add("selected");
        const presentation = VERDICT_PRESENTATION[entry.verdict];
        const verdict = el(
          "div",
          `tile-verdict ${presentation.className}`,
          `${presentation.icon} ${presentation.word}`,
        );
        const time = el("div", "tile-time", formatTime(entry.startedAt));
        tile.append(verdict, time);
        tile.addEventListener("click", () => callbacks.onHistorySelect(entry.executionId));
        return tile;
      }),
    );
    historyEmpty.classList.toggle("hidden", history.length > 0); // 7.7
  }

  function renderWorkflowSelect(state: AppState): void {
    const actives = activeRegistrations(state.registrations);
    const optionsKey = actives.map((r) => `${r.registrationId}:${registrationLabel(r)}`).join("|");
    if (workflowSelect.dataset.key !== optionsKey) {
      workflowSelect.dataset.key = optionsKey;
      workflowSelect.replaceChildren(
        ...actives.map((r) => {
          const option = el("option", undefined, registrationLabel(r));
          option.value = r.registrationId;
          return option;
        }),
      );
    }
    if (state.selectedRegistrationId !== null) {
      workflowSelect.value = state.selectedRegistrationId;
    }
  }

  function renderMessageState(state: AppState): boolean {
    // Full-band message states replace the Run_Result content (2.8, 6.4,
    // 8.5, 2.5). Returns true when a message is shown.
    let title: string | null = null;
    let body = "";
    if (state.availability === "no-workflows") {
      title = "No workflows available";
      body = "No active workflows are deployed on this station.";
    } else if (state.availability === "unavailable") {
      title = "Workflow no longer available";
      body = "The displayed workflow is no longer deployed. Select another workflow above.";
    } else if (state.live.historicalDataError) {
      title = "Run data unavailable";
      body = "The selected historical run's data could not be retrieved.";
    } else if (state.live.displayedRun === null) {
      title = "No runs recorded yet";
      body = "Waiting for the first workflow run\u2026";
    }
    mainMessage.classList.toggle("hidden", title === null);
    verdictPanel.classList.toggle("hidden", title !== null);
    imageArea.classList.toggle("hidden", title !== null);
    if (title !== null) {
      messageTitle.textContent = title;
      messageBody.textContent = body;
    }
    return title !== null;
  }

  function renderVerdict(state: AppState): void {
    const { verdict, displayedRun } = state.live;
    verdictPanel.classList.remove("verdict-pass", "verdict-fail", "verdict-error", "verdict-none");
    if (verdict === null) {
      verdictPanel.classList.add("verdict-none");
      verdictIcon.textContent = "";
      verdictWord.textContent = "Loading\u2026";
      verdictConfidence.textContent = "";
      verdictText.textContent = "";
      verdictText.classList.add("hidden");
      verdictTruncated.classList.add("hidden");
      verdictNote.classList.add("hidden");
      verdictFinished.textContent = "";
      return;
    }
    const presentation = VERDICT_PRESENTATION[verdict.state];
    verdictPanel.classList.add(presentation.className);
    verdictIcon.textContent = presentation.icon;
    verdictWord.textContent = presentation.word; // icon + word, 4.2

    verdictConfidence.textContent =
      verdict.confidenceText !== undefined ? `Confidence ${verdict.confidenceText}` : "";

    const text = verdict.state === "failed-run" ? verdict.errorSummary ?? "" : verdict.generatedText ?? "";
    verdictText.textContent = text;
    verdictText.classList.toggle("hidden", text === "");
    verdictTruncated.classList.toggle("hidden", !verdict.generatedTextTruncated); // 4.4

    verdictNote.textContent = "Verdict data unavailable";
    verdictNote.classList.toggle("hidden", !verdict.metadataUnavailable); // 4.9

    verdictFinished.textContent =
      displayedRun?.finishedAt != null ? `Finished ${formatTime(displayedRun.finishedAt)}` : "";
  }

  function renderImages(state: AppState): void {
    const { images, displayedRun } = state.live;
    const token = loadSession()?.token ?? "";
    const executionId = displayedRun?.executionId ?? "";

    const referenceImage = images?.reference ?? null;
    const capturedImage = images?.captured ?? null;

    // No reference entry → single-panel layout, captured frame takes the
    // combined width (5.4).
    referencePanel.root.classList.toggle("hidden", referenceImage === null);
    updateImagePanel(
      referencePanel,
      referenceImage !== null ? imageUrlFor(executionId, referenceImage, token) : null,
      "Image unavailable",
    );
    updateImagePanel(
      capturedPanel,
      capturedImage !== null ? imageUrlFor(executionId, capturedImage, token) : null,
      images === null ? "Loading\u2026" : "No viewable image", // 5.6
    );
    moreNodesBadge.classList.toggle("hidden", !(images?.hasMoreNodes ?? false)); // 5.7
  }

  function render(state: AppState): void {
    // ---- screen switch (Requirement 1) ----
    const onLogin = state.auth.screen === "login";
    loginScreen.classList.toggle("hidden", !onLogin);
    kiosk.classList.toggle("hidden", onLogin);
    if (onLogin) {
      loginError.textContent =
        state.auth.loginError === "disabled"
          ? "Local login is disabled on this device."
          : state.auth.loginError === "rejected"
            ? "Username or password is incorrect."
            : state.auth.loginError === "unreachable"
              ? "Could not reach the device. Check the connection and try again."
              : "";
      return;
    }

    // ---- header (6.3, 3.3, 8.1) ----
    renderWorkflowSelect(state);
    const selected = state.registrations.find(
      (r) => r.registrationId === state.selectedRegistrationId,
    );
    workflowName.textContent = selected !== undefined ? registrationLabel(selected) : "";
    runStarted.textContent =
      state.live.displayedRun !== null
        ? `Run started ${formatDateTime(state.live.displayedRun.startedAt)}`
        : "";
    inProgressBadge.classList.toggle("hidden", !state.live.inProgress);

    const connected = state.connection.state === "connected";
    connectionBadge.classList.toggle("connected", connected);
    connectionBadge.classList.toggle("disconnected", !connected);
    connectionWord.textContent = connected ? "\u25CF CONNECTED" : "\u25B2 DISCONNECTED";
    connectionLastUpdate.textContent =
      state.connection.lastSuccessfulUpdate !== null
        ? `last update ${formatTime(state.connection.lastSuccessfulUpdate / 1000)}`
        : "";

    // ---- main band ----
    if (!renderMessageState(state)) {
      renderVerdict(state);
      renderImages(state);
    }

    // ---- history strip (7.1, 7.3, 7.4) ----
    renderHistory(state);
    const historical = state.live.mode === "historical";
    historyBanner.classList.toggle("hidden", !historical);
    returnToLive.classList.toggle("hidden", !historical);
    newerRunNotice.classList.toggle("hidden", !(historical && state.live.newerRunAvailable));
  }

  return { render };
}
