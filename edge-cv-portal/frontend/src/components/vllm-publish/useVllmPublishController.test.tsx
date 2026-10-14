/**
 * Unit tests for the vLLM Package & Publish controller hook
 * (vllm-package-publish-gui, task 5.2).
 *
 * With `apiService` mocked and `vi.useFakeTimers()`:
 * - exact API payloads called exactly once and no other endpoints hit
 *   (Requirements 1.2, 3.4, 5.2);
 * - first poll within 15 s of the success response and subsequent polls
 *   at the 10 s interval (Requirements 2.1, 2.2);
 * - unmount during polling issues zero further `getModel` calls
 *   (Requirement 2.6);
 * - a never-resolving `startPackaging` plus a 30 s timer advance
 *   surfaces the request-did-not-complete error and re-enables the
 *   action (Requirement 3.6).
 *
 * `AbortSignal.timeout` is replaced with a `setTimeout`-based
 * implementation so the 30-second request cap runs on the fake clock
 * (the native implementation uses internal timers vitest cannot fake).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import {
  useVllmPublishController,
  VllmPublishModel,
} from './useVllmPublishController';
import {
  POLL_INTERVAL_MS,
  REQUEST_NOT_COMPLETED_MESSAGE,
  REQUEST_TIMEOUT_MS,
} from './publishState';

const { startPackaging, publishGreengrassComponent, getModel, otherApiCalls } =
  vi.hoisted(() => ({
    startPackaging: vi.fn(),
    publishGreengrassComponent: vi.fn(),
    getModel: vi.fn(),
    otherApiCalls: [] as string[],
  }));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  // Any apiService method other than the three the hook may call is
  // recorded so every test can assert no other endpoint was hit
  // (Requirement 5.2).
  const apiService = new Proxy(
    { startPackaging, publishGreengrassComponent, getModel },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => {
          otherApiCalls.push(String(prop));
          return Promise.resolve({});
        };
      },
    }
  );
  return { ApiError, apiService };
});

// -------------------------------------------------------------- fixtures

/** vLLM record with no packaged/published state (first publish path). */
const baseModel: VllmPublishModel = {
  model_id: 'model-123',
  // Name exercising the safeName transform: lowercase + non-alphanumeric
  // characters collapse to '-' (payload check, Requirement 5.2).
  name: 'My LLM_Model v1.2',
  training_job_id: 'training-456',
  model_type: 'vllm',
};

/** Record with a successfully packaged entry and no published component,
 *  which offers the publish-only retry (Requirement 3.4). */
const packagedModel: VllmPublishModel = {
  ...baseModel,
  packaged_components: [
    { target: 'jetson-xavier-jp6', status: 'packaged' },
  ],
};

/** Polled record that never completes the session: no published
 *  component, no failed packaged entry — polling continues. */
const pendingPolledRecord = {
  model_id: 'model-123',
  name: 'My LLM_Model v1.2',
  model_type: 'vllm',
  packaged_components: [
    { target: 'jetson-xavier-jp6', status: 'packaged' },
  ],
};

const EXPECTED_TRAINING_ID = 'training-456';
const EXPECTED_COMPONENT_NAME = 'model-my-llm-model-v1-2';

// ------------------------------------------------------------------ setup

const realAbortSignalTimeout = AbortSignal.timeout;

beforeEach(() => {
  vi.useFakeTimers();
  // setTimeout-based stand-in driven by the fake clock; aborts with the
  // same TimeoutError shape the native implementation produces.
  AbortSignal.timeout = (ms: number) => {
    const controller = new AbortController();
    setTimeout(() => {
      controller.abort(new DOMException('signal timed out', 'TimeoutError'));
    }, ms);
    return controller.signal;
  };
});

afterEach(() => {
  vi.useRealTimers();
  AbortSignal.timeout = realAbortSignalTimeout;
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

function renderController(model: VllmPublishModel = baseModel) {
  const onModelUpdate = vi.fn();
  const rendered = renderHook(() =>
    useVllmPublishController(model, 'DataScientist', onModelUpdate)
  );
  return { ...rendered, onModelUpdate };
}

/** Drive the hook from idle into an active polling session. */
async function activateToPolling(model: VllmPublishModel = baseModel) {
  startPackaging.mockResolvedValueOnce({
    training_id: EXPECTED_TRAINING_ID,
    packaged_components: pendingPolledRecord.packaged_components,
    message: 'ok',
    component_creation_triggered: true,
  });
  getModel.mockResolvedValue({ model: pendingPolledRecord });
  const rendered = renderController(model);
  // Flushes the resolved startPackaging promise so REQUEST_SUCCEEDED
  // dispatches and the poll interval starts.
  await act(async () => {
    rendered.result.current.activate();
  });
  return rendered;
}

// ------------------------------------------------------------------ tests

describe('useVllmPublishController — API payloads (Req 1.2, 5.2)', () => {
  it('activation invokes startPackaging exactly once with the exact payload and hits no other endpoint', async () => {
    // Keep the request in flight so repeat activations are observable.
    startPackaging.mockReturnValueOnce(new Promise(() => {}));
    const { result } = renderController();

    act(() => {
      result.current.activate();
    });
    // Repeat activations while in flight must not re-invoke (Req 1.2).
    act(() => {
      result.current.activate();
    });

    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(startPackaging).toHaveBeenCalledWith(
      EXPECTED_TRAINING_ID,
      undefined,
      true,
      { signal: expect.any(AbortSignal) }
    );
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(getModel).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });

  it('falls back to model_id as the training identifier when training_job_id is absent', () => {
    startPackaging.mockReturnValueOnce(new Promise(() => {}));
    const { result } = renderController({
      ...baseModel,
      training_job_id: undefined,
    });

    act(() => {
      result.current.activate();
    });

    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(startPackaging).toHaveBeenCalledWith('model-123', undefined, true, {
      signal: expect.any(AbortSignal),
    });
  });

  it('publish-only retry invokes publishGreengrassComponent exactly once with the _trigger_component_creation payload shape (Req 3.4, 5.2)', () => {
    publishGreengrassComponent.mockReturnValueOnce(new Promise(() => {}));
    const { result } = renderController(packagedModel);

    act(() => {
      result.current.activatePublishRetry();
    });
    // Repeat activations while in flight must not re-invoke (Req 3.4).
    act(() => {
      result.current.activatePublishRetry();
    });

    expect(publishGreengrassComponent).toHaveBeenCalledTimes(1);
    expect(publishGreengrassComponent).toHaveBeenCalledWith(
      EXPECTED_TRAINING_ID,
      EXPECTED_COMPONENT_NAME,
      '1.0.0',
      'My LLM_Model v1.2',
      undefined,
      { signal: expect.any(AbortSignal) }
    );
    expect(startPackaging).not.toHaveBeenCalled();
    expect(getModel).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });
});

describe('useVllmPublishController — polling cadence (Req 2.1, 2.2)', () => {
  it('polls getModel first within 15 s of success and then at the 10 s interval', async () => {
    const { onModelUpdate } = await activateToPolling();

    // No poll before the first 10 s tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS - 1);
    });
    expect(getModel).not.toHaveBeenCalled();

    // First poll lands at 10 s — within the 15 s bound (Req 2.1).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(getModel).toHaveBeenCalledTimes(1);
    expect(getModel).toHaveBeenCalledWith('model-123');

    // Subsequent polls arrive exactly one 10 s interval apart (Req 2.2).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS - 1);
    });
    expect(getModel).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(getModel).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(getModel).toHaveBeenCalledTimes(3);

    // Each successful poll hands the fresh record to the page (Req 2.2).
    expect(onModelUpdate).toHaveBeenCalledTimes(3);
    expect(onModelUpdate).toHaveBeenCalledWith(pendingPolledRecord);
    expect(otherApiCalls).toEqual([]);
  });
});

describe('useVllmPublishController — unmount during polling (Req 2.6)', () => {
  it('issues zero further getModel calls after unmount', async () => {
    const { result, unmount } = await activateToPolling();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(getModel).toHaveBeenCalledTimes(1);
    expect(result.current.panel.progress).toBeDefined();

    unmount();

    // Well past several would-be poll intervals: no further requests.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 12);
    expect(getModel).toHaveBeenCalledTimes(1);
    expect(otherApiCalls).toEqual([]);
  });
});

describe('useVllmPublishController — 30 s request timeout (Req 3.6)', () => {
  it('surfaces the request-did-not-complete error and re-enables the action when startPackaging never resolves', async () => {
    // Never resolves on its own; rejects only when the 30 s signal aborts.
    startPackaging.mockImplementationOnce(
      (
        _trainingId: string,
        _targets: unknown,
        _autoTriggered: unknown,
        options?: { signal?: AbortSignal }
      ) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener('abort', () => {
            reject(
              options.signal?.reason ??
                new DOMException('aborted', 'AbortError')
            );
          });
        })
    );
    const { result } = renderController();

    act(() => {
      result.current.activate();
    });
    expect(result.current.panel.action.loading).toBe(true);
    expect(result.current.panel.action.enabled).toBe(false);

    // Just before the 30 s cap the request is still in flight.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1);
    });
    expect(result.current.panel.error).toBeUndefined();

    // At 30 s the abort fires and the failure surfaces (Req 3.6).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.panel.error?.message).toBe(
      REQUEST_NOT_COMPLETED_MESSAGE
    );
    expect(result.current.panel.action.enabled).toBe(true);
    expect(result.current.panel.action.loading).toBe(false);

    // The failed session never starts polling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 6);
    });
    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(getModel).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });
});
