/**
 * vLLM Package & Publish state module (vllm-package-publish-gui, task 2.1).
 *
 * Pure module — no React, no I/O — holding the shared record types, role
 * gating, timing constants, completion/failure predicates, and error
 * mapping used by the Model Detail page's Package_Publish_Action. The
 * session reducer and panel-state derivation build on these (tasks 2.2,
 * 2.3); everything here is directly unit/property-testable in isolation.
 */

import { ApiError, VllmPublishedComponent } from '../../services/api';
import { UserRole } from '../../types';

// ------------------------------------------------------------ record types

/**
 * One entry of a record's `packaged_components` list as produced by the
 * Packaging_API's vLLM branch (packaging.py): `status` is 'packaged' on
 * success or 'failed' with `error` carrying the recorded failure message
 * (Requirements 3.2, 4.6).
 */
export interface PackagedComponentEntry {
  target: string;
  status: string;
  component_package_s3?: string;
  supported_architectures?: string[];
  error?: string;
}

/**
 * The slice of a model record this module reads. The Model Detail page's
 * full `Model` interface satisfies it structurally; all displayed
 * packaged/published state derives solely from these fields
 * (Requirement 4.1).
 */
export interface VllmPublishRecord {
  model_type: string;
  packaged_components?: PackagedComponentEntry[];
  published_component?: VllmPublishedComponent;
}

// ------------------------------------------------------------- role gating

/**
 * Roles allowed to package/publish, mirroring the backend's
 * `check_user_access(..., 'DataScientist')` hierarchy gate
 * (Viewer < Operator < DataScientist < UseCaseAdmin < PortalAdmin)
 * (Requirement 1.6).
 */
export const VLLM_PACKAGE_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/** True when the role may activate the Package_Publish_Action (Req 1.6). */
export function canPackageVllm(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && VLLM_PACKAGE_ROLES.includes(role);
}

// --------------------------------------------------------------- constants

/** Poll the model record every 10 s — within the 15 s bound (Req 2.2). */
export const POLL_INTERVAL_MS = 10_000;

/** Stop polling 5 minutes after the poll session starts (Req 2.5). */
export const POLL_TIMEOUT_MS = 300_000;

/** Abort a packaging/publish request with no response in 30 s (Req 3.6). */
export const REQUEST_TIMEOUT_MS = 30_000;

/** Error message for aborted or network-failed invocations (Req 3.6). */
export const REQUEST_NOT_COMPLETED_MESSAGE =
  'The request did not complete. Check your connection and try again.';

// ----------------------------------------------------------- session error

/**
 * A failure surfaced on the page: the message plus, when recorded, the
 * failing step (from `ApiError.details.failed_step` or a failed
 * `packaged_components` entry) and which action produced it
 * (Requirements 3.1, 3.2).
 */
export interface SessionError {
  message: string;
  failedStep?: string;
  source: 'package' | 'publish-retry' | 'record';
}

// -------------------------------------------------------------- predicates

/**
 * Baseline captured at invocation time: the published component_version
 * present on the record, or null when the record had none (Req 2.1).
 */
export type Baseline = string | null;

/**
 * Completion predicate for the poll loop: true iff the record carries a
 * `published_component` that is new since invocation (no baseline) or
 * whose component version differs from the baseline — a record still on
 * the baseline version is never complete, which is what makes re-publish
 * detection work (Requirements 2.1, 2.3).
 */
export function isPublishComplete(
  baseline: Baseline,
  record: VllmPublishRecord
): boolean {
  const published = record.published_component;
  if (!published) {
    return false;
  }
  return baseline === null || published.component_version !== baseline;
}

/** True when a packaged entry's status indicates success (packaging.py
 *  writes 'packaged' on success, 'failed' otherwise). */
export function isPackagedEntrySuccess(entry: PackagedComponentEntry): boolean {
  return entry.status === 'packaged';
}

/**
 * The recorded packaging failure to surface, or null: the first
 * `packaged_components` entry whose status is not a success value,
 * carrying the failing target as the step and the entry's recorded
 * error message (Requirements 3.2, 4.6). Note: a rolled-back vLLM
 * publish writes nothing to the record, so publish failures during
 * auto-chaining are only reachable via the polling-timeout path.
 */
export function recordFailure(record: VllmPublishRecord): SessionError | null {
  const failed = (record.packaged_components ?? []).find(
    (entry) => !isPackagedEntrySuccess(entry)
  );
  if (!failed) {
    return null;
  }
  return {
    message: failed.error || `Packaging failed for target ${failed.target}`,
    failedStep: `packaging (${failed.target})`,
    source: 'record',
  };
}

// ------------------------------------------------------------ error mapping

/** True for AbortSignal.timeout / abort rejections (Req 3.6). */
function isAbortLike(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'name' in err &&
    ((err as { name?: unknown }).name === 'AbortError' ||
      (err as { name?: unknown }).name === 'TimeoutError')
  );
}

/**
 * Map an invocation failure to the SessionError the page displays:
 *
 * - `ApiError` → its message, plus `details.failed_step` when the vLLM
 *   packaging/publish error envelope recorded one (Requirements 3.1, 3.5);
 * - abort/timeout (30 s cap) and network errors (fetch rejects with a
 *   TypeError) → the request-did-not-complete message (Requirement 3.6);
 * - anything else → its message when available, otherwise the
 *   request-did-not-complete fallback.
 */
export function toSessionError(
  err: unknown,
  source: SessionError['source']
): SessionError {
  if (err instanceof ApiError) {
    const failedStep = err.details?.failed_step;
    return {
      message: err.message || REQUEST_NOT_COMPLETED_MESSAGE,
      ...(typeof failedStep === 'string' && failedStep
        ? { failedStep }
        : {}),
      source,
    };
  }
  if (isAbortLike(err) || err instanceof TypeError) {
    // AbortSignal.timeout(REQUEST_TIMEOUT_MS) aborts and fetch network
    // failures both mean the request did not complete (Req 3.6).
    return { message: REQUEST_NOT_COMPLETED_MESSAGE, source };
  }
  if (err instanceof Error && err.message) {
    return { message: err.message, source };
  }
  return { message: REQUEST_NOT_COMPLETED_MESSAGE, source };
}

// --------------------------------------------------------------- versions

/**
 * Next re-publish version hint shown in the confirmation flow: the
 * published major version + 1 with '.0.0' — the same `N.0.0` progression
 * the backend's `next_vllm_component_version` applies (Requirement 1.4).
 * Falls back to '1.0.0' when the current version has no numeric major.
 */
export function nextComponentVersion(current: string): string {
  const major = Number.parseInt(current.split('.')[0], 10);
  if (!Number.isInteger(major) || major < 0) {
    return '1.0.0';
  }
  return `${major + 1}.0.0`;
}

// ---------------------------------------------------- session state machine

/**
 * The transient publish session held per page visit — never persisted;
 * record-derived display always comes from the record itself (Req 4.1).
 */
export type PublishSession =
  // No session activity; the page renders purely from the record.
  | { kind: 'idle' }
  // A packaging or publish request is in flight (Req 1.5 / 3.7).
  | {
      kind: 'requesting';
      action: 'package' | 'publish-retry';
      baseline: Baseline;
    }
  // Confirmation modal open for a re-publish (Req 1.7).
  | { kind: 'confirming'; baseline: Baseline }
  // Poll loop active. deadline = startedAt + POLL_TIMEOUT_MS, fixed at
  // poll start and never moved by poll failures (Req 2.5, 2.7).
  | { kind: 'polling'; baseline: Baseline; startedAt: number; deadline: number }
  // Terminal session states (record state still renders independently).
  | { kind: 'published'; component: VllmPublishedComponent }
  | { kind: 'timed-out' } // Req 2.5 pending message
  | { kind: 'failed'; error: SessionError }; // Req 3.1, 3.2, 3.5, 3.6

/** Events dispatched into the reducer by the controller hook. */
export type PublishEvent =
  | { type: 'ACTIVATE'; record: VllmPublishRecord; now: number } // main action
  | { type: 'CONFIRM'; now: number } // modal confirm
  | { type: 'CANCEL_CONFIRM' }
  | { type: 'ACTIVATE_PUBLISH_RETRY'; record: VllmPublishRecord; now: number }
  | { type: 'REQUEST_SUCCEEDED'; now: number } // API 2xx
  | { type: 'REQUEST_FAILED'; error: SessionError } // API error / 30s timeout
  | { type: 'POLL_RESULT'; record: VllmPublishRecord; now: number } // poll ok
  | { type: 'POLL_FAILED'; now: number }; // failed poll (Req 2.7)

/** Commands the controller executes as effects of a transition. */
export type PublishCommand =
  | { type: 'INVOKE_PACKAGING' } // startPackaging(id, undefined, true)
  | { type: 'INVOKE_PUBLISH' } // publishGreengrassComponent placeholder payload
  | { type: 'START_POLLING' }
  | { type: 'STOP_POLLING' };

/** A reducer step: the next session plus the effects to execute. */
export interface PublishTransition {
  state: PublishSession;
  commands: PublishCommand[];
}

/** Unchanged state, no effects — used to absorb inapplicable events. */
function noTransition(state: PublishSession): PublishTransition {
  return { state, commands: [] };
}

/** True while a session must reject further activations (Req 1.5, 3.7). */
function isInFlight(state: PublishSession): boolean {
  return (
    state.kind === 'requesting' ||
    state.kind === 'confirming' ||
    state.kind === 'polling'
  );
}

/** Baseline capture at activation time (Req 2.1). */
function captureBaseline(record: VllmPublishRecord): Baseline {
  return record.published_component?.component_version ?? null;
}

/**
 * Pure session state machine for the Package_Publish_Action.
 *
 * Rules (each maps to acceptance criteria):
 *
 * - `ACTIVATE` outside an in-flight session captures the baseline and,
 *   for records with a `published_component`, gates on confirmation
 *   (`confirming`, no invoke command — Req 1.7); otherwise it enters
 *   `requesting('package')` and emits `INVOKE_PACKAGING` (Req 1.2).
 *   Entering either state discards any prior `failed` error (Req 3.8).
 * - `ACTIVATE` / `ACTIVATE_PUBLISH_RETRY` while `requesting`,
 *   `confirming`, or `polling` change no state and emit no commands —
 *   at most one invocation in flight per record (Req 1.5, 3.7).
 * - `ACTIVATE_PUBLISH_RETRY` outside an in-flight session enters
 *   `requesting('publish-retry')` and emits `INVOKE_PUBLISH`, resuming
 *   the standard publish session (Req 3.4) and clearing prior failure
 *   state (Req 3.8).
 * - `CONFIRM` in `confirming` → `requesting('package')` +
 *   `INVOKE_PACKAGING` (Req 1.7); `CANCEL_CONFIRM` → `idle`.
 * - `REQUEST_SUCCEEDED` in `requesting` → `polling` anchored at the
 *   success time: `startedAt = now`, `deadline = now + POLL_TIMEOUT_MS`,
 *   emitting `START_POLLING` (Req 2.1).
 * - `REQUEST_FAILED` in `requesting` → `failed(error)`; the derivation
 *   re-enables the initiating action (Req 3.1, 3.5, 3.6).
 * - `POLL_RESULT` in `polling`: completion → `published` + stop
 *   (Req 2.3); recorded failure → `failed` with the recorded step and
 *   message + stop (Req 3.2); at/past deadline → `timed-out` + stop
 *   (Req 2.5); otherwise stay `polling` with the deadline unchanged.
 * - `POLL_FAILED` in `polling`: absorbed without surfacing a failure or
 *   moving the deadline (Req 2.7); at/past the deadline it times out
 *   like any other tick (Req 2.5).
 *
 * Events that do not apply to the current state are absorbed unchanged.
 */
export function publishReducer(
  state: PublishSession,
  event: PublishEvent
): PublishTransition {
  switch (event.type) {
    case 'ACTIVATE': {
      if (isInFlight(state)) {
        // Single in-flight invocation per record (Req 1.5).
        return noTransition(state);
      }
      const baseline = captureBaseline(event.record);
      if (event.record.published_component) {
        // Re-publish requires explicit confirmation before any
        // invocation (Req 1.7). Prior failure state is discarded by
        // leaving `failed` behind (Req 3.8).
        return { state: { kind: 'confirming', baseline }, commands: [] };
      }
      return {
        state: { kind: 'requesting', action: 'package', baseline },
        commands: [{ type: 'INVOKE_PACKAGING' }],
      };
    }

    case 'ACTIVATE_PUBLISH_RETRY': {
      if (isInFlight(state)) {
        // Single in-flight invocation per record (Req 3.7).
        return noTransition(state);
      }
      // Publish-only retry re-drives publish without re-running
      // packaging and resumes the standard session (Req 3.4); prior
      // failure state is discarded (Req 3.8).
      const baseline = captureBaseline(event.record);
      return {
        state: { kind: 'requesting', action: 'publish-retry', baseline },
        commands: [{ type: 'INVOKE_PUBLISH' }],
      };
    }

    case 'CONFIRM': {
      if (state.kind !== 'confirming') {
        return noTransition(state);
      }
      // Confirmed re-publish invokes packaging exactly once (Req 1.7).
      return {
        state: {
          kind: 'requesting',
          action: 'package',
          baseline: state.baseline,
        },
        commands: [{ type: 'INVOKE_PACKAGING' }],
      };
    }

    case 'CANCEL_CONFIRM': {
      if (state.kind !== 'confirming') {
        return noTransition(state);
      }
      return { state: { kind: 'idle' }, commands: [] };
    }

    case 'REQUEST_SUCCEEDED': {
      if (state.kind !== 'requesting') {
        return noTransition(state);
      }
      // Anchor the poll session at the success time; the deadline is
      // fixed here and never extended (Req 2.1, 2.5).
      return {
        state: {
          kind: 'polling',
          baseline: state.baseline,
          startedAt: event.now,
          deadline: event.now + POLL_TIMEOUT_MS,
        },
        commands: [{ type: 'START_POLLING' }],
      };
    }

    case 'REQUEST_FAILED': {
      if (state.kind !== 'requesting') {
        return noTransition(state);
      }
      // The derivation re-enables the initiating action from `failed`
      // (Req 3.1, 3.5, 3.6).
      return {
        state: { kind: 'failed', error: event.error },
        commands: [],
      };
    }

    case 'POLL_RESULT': {
      if (state.kind !== 'polling') {
        return noTransition(state);
      }
      if (
        isPublishComplete(state.baseline, event.record) &&
        event.record.published_component
      ) {
        // New or version-changed published_component observed (Req 2.3).
        return {
          state: {
            kind: 'published',
            component: event.record.published_component,
          },
          commands: [{ type: 'STOP_POLLING' }],
        };
      }
      const failure = recordFailure(event.record);
      if (failure) {
        // Recorded packaging failure on the record (Req 3.2).
        return {
          state: { kind: 'failed', error: failure },
          commands: [{ type: 'STOP_POLLING' }],
        };
      }
      if (event.now >= state.deadline) {
        // 5-minute deadline reached without completion (Req 2.5).
        return {
          state: { kind: 'timed-out' },
          commands: [{ type: 'STOP_POLLING' }],
        };
      }
      // Keep polling; the deadline never moves (Req 2.5, 2.7).
      return noTransition(state);
    }

    case 'POLL_FAILED': {
      if (state.kind !== 'polling') {
        return noTransition(state);
      }
      if (event.now >= state.deadline) {
        // A failed poll at/past the deadline still times out (Req 2.5).
        return {
          state: { kind: 'timed-out' },
          commands: [{ type: 'STOP_POLLING' }],
        };
      }
      // Absorbed: no failure surfaced, deadline unchanged (Req 2.7).
      return noTransition(state);
    }
  }
}

// ------------------------------------------------------- panel derivation

/** Action label variants (Req 1.1, 1.4). */
export type PublishActionLabel = 'Package & Publish' | 'Re-publish Component';

/** Secondary text for users lacking packaging permission (Req 1.6). */
export const PERMISSION_MESSAGE =
  'Packaging permission in the owning use case is required.';

/** Progress indicator shown while the poll loop is active (Req 2.2). */
export const PROGRESS_MESSAGE =
  'Publishing component… checking every 10 seconds.';

/** Confirmation that packaging completed and publish chained (Req 2.1). */
export const PACKAGING_ACCEPTED_MESSAGE =
  'Packaging completed — component publish was triggered.';

/** Pending message shown when the 5-minute poll deadline lapses (Req 2.5). */
export const PUBLISH_PENDING_MESSAGE =
  'Publish is still pending. Refresh the page to check again.';

/**
 * Everything `VllmPackagePublishSection` renders, derived from
 * `(record, role, session)`. Record-derived parts (`packagedSection`,
 * `publishedSection`, `publishRetry` presence) depend ONLY on the record
 * (Requirement 4.1); the action and banners fold in the transient session.
 */
export interface PanelState {
  /** Section renders only for vLLM records (Req 1.3, 5.1, 5.4). */
  visible: boolean;
  action: {
    label: PublishActionLabel; // Req 1.1, 1.4
    /** False when the role lacks permission or a session is in flight
     *  (Req 1.5, 1.6); true again after failure/timeout (Req 3.1). */
    enabled: boolean;
    /** True while the packaging request is in flight (Req 1.5). */
    loading: boolean;
    permissionMessage?: string; // Req 1.6
    /** "registers the next component version" note (Req 1.4). */
    republishNote?: string;
    /** Activation must confirm before invoking (Req 1.7). */
    requiresConfirmation: boolean;
  };
  /** Present iff the record has a successfully packaged entry and no
   *  published_component (Req 3.3, 4.2). */
  publishRetry?: {
    enabled: boolean;
    loading: boolean; // requesting('publish-retry') (Req 3.7)
  };
  /** Present iff the record has packaged entries (Req 4.1, 4.2, 4.6). */
  packagedSection?: Array<{ target: string; status: string; error?: string }>;
  /** Present iff the record has a published_component (Req 4.1, 4.3). */
  publishedSection?: {
    componentName: string;
    componentVersion: string;
    publishedAt: number;
    componentArns: Record<string, string>;
    supportedArchitectures: string[];
  };
  progress?: { message: string }; // polling in progress (Req 2.2)
  success?: { message: string }; // accepted / complete (Req 2.1, 2.3)
  pending?: { message: string }; // timeout message (Req 2.5)
  error?: SessionError; // Req 3.1, 3.2, 3.5, 3.6
}

/**
 * Derive the full panel state for the Model Detail section.
 *
 * - Visibility: only `model_type === 'vllm'` records yield a visible
 *   panel; all other records get an inert, invisible panel with no
 *   sections, no retry action, and no banner output (Req 1.3, 5.1, 5.4).
 * - Action: labeled as a re-publish (with the next-version note and
 *   confirmation flag) iff the record has a `published_component`
 *   (Req 1.4, 1.7); disabled with the permission message when the role
 *   is not in `VLLM_PACKAGE_ROLES` (Req 1.6); disabled while a session
 *   is in flight and loading during the packaging request (Req 1.5);
 *   enabled again from `failed`/`timed-out`/`published` states so the
 *   user can retry (Req 3.1).
 * - Publish-only retry: offered iff the record has at least one
 *   successfully packaged entry and no `published_component`
 *   (Req 3.3, 4.2), loading during its own request (Req 3.7).
 * - Packaged/published sections: derived solely from the record's
 *   `packaged_components` and `published_component` (Req 4.1, 4.2,
 *   4.3, 4.5, 4.6).
 * - Banners: progress + packaging-accepted while polling (Req 2.1,
 *   2.2), publish-complete success from the `published` session
 *   (Req 2.3), pending message after timeout (Req 2.5), session error
 *   from `failed` (Req 3.1, 3.5, 3.6), and — with no active session —
 *   the failure recorded on the record itself when it has a failed
 *   entry and no `published_component`, so page loads surface it
 *   without polling (Req 4.6). Activation leaves `failed`/`idle`
 *   behind, so prior failure output disappears on retry (Req 3.8).
 */
export function derivePanelState(
  record: VllmPublishRecord,
  role: UserRole | undefined,
  session: PublishSession
): PanelState {
  if (record.model_type !== 'vllm') {
    // Non-vLLM records: no action, no state sections, no banners, and
    // nothing that could drive polling (Req 1.3, 5.1, 5.4).
    return {
      visible: false,
      action: {
        label: 'Package & Publish',
        enabled: false,
        loading: false,
        requiresConfirmation: false,
      },
    };
  }

  const permitted = canPackageVllm(role);
  const inFlight = isInFlight(session);
  const published = record.published_component;
  const packagedEntries = record.packaged_components ?? [];
  const hasPackagedSuccess = packagedEntries.some(isPackagedEntrySuccess);

  const panel: PanelState = {
    visible: true,
    action: {
      label: published ? 'Re-publish Component' : 'Package & Publish',
      enabled: permitted && !inFlight,
      loading: session.kind === 'requesting' && session.action === 'package',
      ...(permitted ? {} : { permissionMessage: PERMISSION_MESSAGE }),
      ...(published
        ? {
            republishNote: `Re-publishing registers the next component version (${nextComponentVersion(
              published.component_version
            )}).`,
          }
        : {}),
      requiresConfirmation: Boolean(published),
    },
  };

  // Publish-only retry: packaged successfully but never published
  // (Req 3.3, 4.2). Presence derives solely from the record.
  if (hasPackagedSuccess && !published) {
    panel.publishRetry = {
      enabled: permitted && !inFlight,
      loading:
        session.kind === 'requesting' && session.action === 'publish-retry',
    };
  }

  // Packaged state section: one row per recorded entry (Req 4.1, 4.2,
  // 4.6); absent when the record has none (Req 4.5).
  if (packagedEntries.length > 0) {
    panel.packagedSection = packagedEntries.map((entry) => ({
      target: entry.target,
      status: entry.status,
      ...(entry.error ? { error: entry.error } : {}),
    }));
  }

  // Published state section straight off the record (Req 4.1, 4.3);
  // absent when the record has no published_component (Req 4.5).
  if (published) {
    panel.publishedSection = {
      componentName: published.component_name,
      componentVersion: published.component_version,
      publishedAt: published.published_at,
      componentArns: published.component_arns,
      supportedArchitectures: published.supported_architectures,
    };
  }

  // Session-driven banners.
  switch (session.kind) {
    case 'polling':
      panel.success = { message: PACKAGING_ACCEPTED_MESSAGE }; // Req 2.1
      panel.progress = { message: PROGRESS_MESSAGE }; // Req 2.2
      break;
    case 'published':
      panel.success = {
        message: `Component ${session.component.component_name} version ${session.component.component_version} published successfully.`,
      }; // Req 2.3
      break;
    case 'timed-out':
      panel.pending = { message: PUBLISH_PENDING_MESSAGE }; // Req 2.5
      break;
    case 'failed':
      panel.error = session.error; // Req 3.1, 3.2, 3.5, 3.6
      break;
    case 'idle': {
      // Recorded failure surfaced on page load without an active
      // session (Req 4.6); suppressed once activation starts so prior
      // failure output clears on retry (Req 3.8).
      if (!published) {
        const recorded = recordFailure(record);
        if (recorded) {
          panel.error = recorded;
        }
      }
      break;
    }
    default:
      break;
  }

  return panel;
}
