/**
 * vLLM Package & Publish controller hook (vllm-package-publish-gui,
 * task 5.1).
 *
 * Binds the pure `publishState.ts` session reducer to the API client and
 * the polling timer: every dispatched event runs `publishReducer` and the
 * returned commands are executed as effects (invoke packaging, invoke the
 * publish-only retry, start/stop the 10-second poll loop). All lifecycle
 * rules (single in-flight invocation, confirmation gating, baseline
 * capture, deadline anchoring) live in the reducer; this hook only
 * translates commands into I/O and guards against staleness.
 *
 * Requirements: 1.2, 2.1, 2.2, 2.4, 2.6, 2.7, 3.4, 3.6, 4.4, 5.2.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { apiService } from '../../services/api';
import { UserRole } from '../../types';
import {
  PanelState,
  POLL_INTERVAL_MS,
  PublishCommand,
  PublishEvent,
  PublishSession,
  publishReducer,
  REQUEST_TIMEOUT_MS,
  SessionError,
  toSessionError,
  derivePanelState,
  VllmPublishRecord,
} from './publishState';

/**
 * The slice of the page's model record the controller needs: the
 * record fields the reducer/derivation read (`VllmPublishRecord`) plus
 * the identifiers used to address the APIs. `ModelDetail.tsx`'s `Model`
 * satisfies it structurally.
 */
export interface VllmPublishModel extends VllmPublishRecord {
  model_id: string;
  name: string;
  training_job_id?: string;
}

/** The fresh record shape each successful poll returns (models.py via
 *  `getModel`), handed to `onModelUpdate` so the page's record-derived
 *  sections — including Supported Architectures — refresh live
 *  (Requirements 2.4, 4.4). */
export type PolledModel = Awaited<
  ReturnType<typeof apiService.getModel>
>['model'];

/**
 * Sanitized model name mirroring the backend transform that
 * `_trigger_component_creation` (packaging.py) applies when deriving
 * `model-{safe_model_name}` component names:
 * `re.sub(r'[^a-zA-Z0-9-]', '-', model_name.lower())`.
 *
 * Used only to reproduce the exact placeholder payload that endpoint
 * already sends today — the backend's vLLM branch overrides the name
 * (`derive_vllm_component_name`) and version, so the request contract
 * is unchanged (Requirement 5.2).
 */
export function safeName(name: string): string {
  return name.toLowerCase().replace(/[^a-zA-Z0-9-]/g, '-');
}

/** The controller surface `VllmPackagePublishSection` renders from. */
export interface VllmPublishController {
  panel: PanelState;
  /** Package_Publish_Action activation → `ACTIVATE` (Req 1.2, 1.7). */
  activate(): void;
  /** Re-publish confirmation modal confirm → `CONFIRM` (Req 1.7). */
  confirm(): void;
  /** Re-publish confirmation modal cancel → `CANCEL_CONFIRM`. */
  cancelConfirm(): void;
  /** Publish-only retry activation → `ACTIVATE_PUBLISH_RETRY` (Req 3.4). */
  activatePublishRetry(): void;
}

/**
 * Controller hook for the Model Detail page's vLLM Package & Publish
 * section.
 *
 * - `INVOKE_PACKAGING` → `apiService.startPackaging(trainingId,
 *   undefined, true, { signal })` with `trainingId =
 *   model.training_job_id || model.model_id` (the same derivation
 *   ModelDetail already uses) and `signal =
 *   AbortSignal.timeout(REQUEST_TIMEOUT_MS)` (Req 1.2, 3.6, 5.2).
 * - `INVOKE_PUBLISH` → `apiService.publishGreengrassComponent(
 *   trainingId, 'model-' + safeName(model.name), '1.0.0', model.name,
 *   undefined, { signal })` — the exact `_trigger_component_creation`
 *   payload shape (Req 3.4, 5.2).
 * - `START_POLLING` → `setInterval(POLL_INTERVAL_MS)`; each tick calls
 *   `getModel(model.model_id)` and dispatches `POLL_RESULT` with the
 *   fresh record (also invoking `onModelUpdate(record)`, Req 2.2, 2.4,
 *   4.4) or `POLL_FAILED` on error (Req 2.7).
 * - `STOP_POLLING` and unmount cleanup clear the interval ref, so no
 *   further poll requests are issued after stopping (Req 2.6).
 * - A monotonically increasing session generation counter is captured
 *   by every in-flight request and poll tick; responses whose
 *   generation no longer matches (session stopped, superseded, or
 *   component unmounted) are dropped and can never dispatch.
 */
export function useVllmPublishController(
  model: VllmPublishModel,
  role: UserRole | undefined,
  onModelUpdate: (model: PolledModel) => void
): VllmPublishController {
  const [session, setSession] = useState<PublishSession>({ kind: 'idle' });

  // The reducer runs against a ref so dispatches from async callbacks
  // (request settlement, poll ticks) always see the latest session.
  const sessionRef = useRef<PublishSession>(session);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Session generation: bumped when a new request starts, when polling
  // stops, and on unmount. Async callbacks capture the generation at
  // start time and drop their response if it no longer matches.
  const generationRef = useRef(0);

  // Latest props, readable from timer callbacks without re-binding the
  // interval on every render.
  const modelRef = useRef(model);
  modelRef.current = model;
  const onModelUpdateRef = useRef(onModelUpdate);
  onModelUpdateRef.current = onModelUpdate;

  const clearPolling = useCallback(() => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const dispatchRef = useRef<(event: PublishEvent) => void>(() => {});

  /** Start a packaging/publish request under the current generation;
   *  settle into REQUEST_SUCCEEDED / REQUEST_FAILED unless stale
   *  (Req 3.6 abort mapping via toSessionError). */
  const runRequest = useCallback(
    (invoke: () => Promise<unknown>, source: SessionError['source']) => {
      const generation = ++generationRef.current;
      invoke().then(
        () => {
          if (generation === generationRef.current) {
            dispatchRef.current({ type: 'REQUEST_SUCCEEDED', now: Date.now() });
          }
        },
        (err: unknown) => {
          if (generation === generationRef.current) {
            dispatchRef.current({
              type: 'REQUEST_FAILED',
              error: toSessionError(err, source),
            });
          }
        }
      );
    },
    []
  );

  const executeCommand = useCallback(
    (command: PublishCommand) => {
      switch (command.type) {
        case 'INVOKE_PACKAGING': {
          const current = modelRef.current;
          const trainingId = current.training_job_id || current.model_id;
          runRequest(
            () =>
              apiService.startPackaging(trainingId, undefined, true, {
                signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
              }),
            'package'
          );
          break;
        }

        case 'INVOKE_PUBLISH': {
          const current = modelRef.current;
          const trainingId = current.training_job_id || current.model_id;
          // Placeholder name/version mirror _trigger_component_creation's
          // payload; the backend vLLM branch overrides both (Req 5.2).
          runRequest(
            () =>
              apiService.publishGreengrassComponent(
                trainingId,
                `model-${safeName(current.name)}`,
                '1.0.0',
                current.name,
                undefined,
                { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }
              ),
            'publish-retry'
          );
          break;
        }

        case 'START_POLLING': {
          clearPolling();
          const generation = ++generationRef.current;
          intervalRef.current = setInterval(() => {
            if (generation !== generationRef.current) {
              return;
            }
            apiService.getModel(modelRef.current.model_id).then(
              (response) => {
                if (generation !== generationRef.current) {
                  return; // stale: session stopped or unmounted (Req 2.6)
                }
                // Refresh the page's record-derived sections live
                // (Req 2.4, 4.4), then advance the session (Req 2.2).
                onModelUpdateRef.current(response.model);
                dispatchRef.current({
                  type: 'POLL_RESULT',
                  record: response.model,
                  now: Date.now(),
                });
              },
              () => {
                if (generation !== generationRef.current) {
                  return;
                }
                // Individual poll failures are absorbed by the reducer
                // without moving the deadline (Req 2.7).
                dispatchRef.current({ type: 'POLL_FAILED', now: Date.now() });
              }
            );
          }, POLL_INTERVAL_MS);
          break;
        }

        case 'STOP_POLLING': {
          // Invalidate any in-flight poll response, then clear the
          // timer so no further requests are issued (Req 2.6).
          generationRef.current += 1;
          clearPolling();
          break;
        }
      }
    },
    [clearPolling, runRequest]
  );

  const dispatch = useCallback(
    (event: PublishEvent) => {
      const { state: next, commands } = publishReducer(
        sessionRef.current,
        event
      );
      sessionRef.current = next;
      setSession(next);
      for (const command of commands) {
        executeCommand(command);
      }
    },
    [executeCommand]
  );
  dispatchRef.current = dispatch;

  // Unmount cleanup: drop pending responses and stop the poll loop so
  // no further requests dispatch after the page is left (Req 2.6).
  useEffect(() => {
    return () => {
      generationRef.current += 1;
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, []);

  const activate = useCallback(() => {
    dispatch({ type: 'ACTIVATE', record: modelRef.current, now: Date.now() });
  }, [dispatch]);

  const confirm = useCallback(() => {
    dispatch({ type: 'CONFIRM', now: Date.now() });
  }, [dispatch]);

  const cancelConfirm = useCallback(() => {
    dispatch({ type: 'CANCEL_CONFIRM' });
  }, [dispatch]);

  const activatePublishRetry = useCallback(() => {
    dispatch({
      type: 'ACTIVATE_PUBLISH_RETRY',
      record: modelRef.current,
      now: Date.now(),
    });
  }, [dispatch]);

  const panel = useMemo(
    () => derivePanelState(model, role, session),
    [model, role, session]
  );

  return { panel, activate, confirm, cancelConfirm, activatePublishRetry };
}
