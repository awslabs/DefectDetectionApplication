/**
 * Component tests for the vLLM Package & Publish section
 * (vllm-package-publish-gui, task 6.2).
 *
 * Rendered with `apiService` mocked and driven through
 * `@cloudscape-design/components/test-utils/dom`:
 *
 * - confirmation modal flow: a record with a `published_component`
 *   opens the re-publish modal on activation without invoking any API,
 *   Cancel closes it invoking nothing, Confirm invokes packaging
 *   exactly once (Requirement 1.7);
 * - loading/disabled button states: the action is loading and rejects
 *   further activations while a request is in flight (Requirement 1.5),
 *   and is disabled with the permission message for a role without
 *   packaging permission (Requirement 1.6);
 * - banner rendering for each panel-state variant: error with the
 *   failing step and the action re-enabled (Requirement 3.1),
 *   success + progress while polling, publish-complete success, and
 *   the pending banner after the 5-minute polling deadline
 *   (Requirement 2.5).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import VllmPackagePublishSection from './VllmPackagePublishSection';
import { VllmPublishModel } from './useVllmPublishController';
import {
  PACKAGING_ACCEPTED_MESSAGE,
  PERMISSION_MESSAGE,
  POLL_INTERVAL_MS,
  POLL_TIMEOUT_MS,
  PROGRESS_MESSAGE,
  PUBLISH_PENDING_MESSAGE,
} from './publishState';
import { ApiError, VllmPublishedComponent } from '../../services/api';
import { UserRole } from '../../types';

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
  // Any apiService method other than the three the controller may call
  // is recorded so every test can assert no other endpoint was hit.
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
  name: 'My LLM',
  training_job_id: 'training-456',
  model_type: 'vllm',
};

const publishedComponent: VllmPublishedComponent = {
  component_name: 'model-vllm-my-llm',
  component_version: '2.0.0',
  supported_architectures: ['arm64_jp6'],
  runtime: 'vllm',
  component_arns: {
    'jetson-xavier-jp6': 'arn:aws:greengrass:us-east-1:123:component',
  },
  published_at: 1700000000000,
};

/** Record with a published_component: activation requires the
 *  re-publish confirmation modal (Requirement 1.7). */
const publishedModel: VllmPublishModel = {
  ...baseModel,
  packaged_components: [{ target: 'jetson-xavier-jp6', status: 'packaged' }],
  published_component: publishedComponent,
};

/** Polled record that never completes the session: no published
 *  component, no failed packaged entry — polling continues. */
const pendingPolledRecord = {
  model_id: 'model-123',
  name: 'My LLM',
  model_type: 'vllm',
  packaged_components: [{ target: 'jetson-xavier-jp6', status: 'packaged' }],
};

/** Polled record carrying a fresh published_component (first publish
 *  completion: baseline was null). */
const publishedPolledRecord = {
  ...pendingPolledRecord,
  published_component: {
    ...publishedComponent,
    component_version: '1.0.0',
  },
};

const PACKAGING_ACCEPTED_RESPONSE = {
  training_id: 'training-456',
  packaged_components: pendingPolledRecord.packaged_components,
  message: 'ok',
  component_creation_triggered: true,
};

// --------------------------------------------------------------- helpers

const wrapper = () => createWrapper(document.body);
const actionButton = () =>
  wrapper().findButton('[data-testid="vllm-publish-action"]')!;
const retryButton = () =>
  wrapper().findButton('[data-testid="vllm-publish-retry"]');
const republishModal = () =>
  wrapper().findModal('[data-testid="vllm-republish-modal"]')!;
const confirmButton = () =>
  wrapper().findButton('[data-testid="vllm-republish-confirm"]')!;
const cancelButton = () =>
  wrapper().findButton('[data-testid="vllm-republish-cancel"]')!;

function renderSection(
  model: VllmPublishModel = baseModel,
  role: UserRole | undefined = 'DataScientist'
) {
  const onModelUpdate = vi.fn();
  const rendered = render(
    <VllmPackagePublishSection
      model={model}
      role={role}
      onModelUpdate={onModelUpdate}
    />
  );
  return { ...rendered, onModelUpdate };
}

/** Click a Cloudscape button wrapper and flush resulting async work. */
async function click(button: { click(): void }) {
  await act(async () => {
    button.click();
  });
}

afterEach(() => {
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

// ------------------------------------------------------------------ tests

describe('VllmPackagePublishSection — confirmation modal flow (Req 1.7)', () => {
  it('activation on a record with a published_component opens the modal without invoking any API', async () => {
    renderSection(publishedModel);

    expect(republishModal().isVisible()).toBe(false);
    expect(actionButton().getElement().textContent).toContain(
      'Re-publish Component'
    );

    await click(actionButton());

    expect(republishModal().isVisible()).toBe(true);
    // Confirmation gates the invocation: nothing called yet.
    expect(startPackaging).not.toHaveBeenCalled();
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(getModel).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });

  it('Cancel closes the modal and invokes nothing, leaving the action enabled', async () => {
    renderSection(publishedModel);

    await click(actionButton());
    expect(republishModal().isVisible()).toBe(true);

    await click(cancelButton());

    expect(republishModal().isVisible()).toBe(false);
    expect(startPackaging).not.toHaveBeenCalled();
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
    expect(actionButton().isDisabled()).toBe(false);
  });

  it('Confirm closes the modal and invokes packaging exactly once', async () => {
    // Keep the request in flight so the post-confirm state is observable.
    startPackaging.mockReturnValueOnce(new Promise(() => {}));
    renderSection(publishedModel);

    await click(actionButton());
    await click(confirmButton());

    expect(republishModal().isVisible()).toBe(false);
    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(startPackaging).toHaveBeenCalledWith(
      'training-456',
      undefined,
      true,
      { signal: expect.any(AbortSignal) }
    );
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
    // In flight after confirm: the action rejects further activations.
    expect(actionButton().isDisabled()).toBe(true);
  });
});

describe('VllmPackagePublishSection — loading/disabled button states (Req 1.5, 1.6)', () => {
  it('renders the action loading and rejects further activations while packaging is in flight (Req 1.5)', async () => {
    startPackaging.mockReturnValueOnce(new Promise(() => {}));
    renderSection(baseModel);

    expect(actionButton().isDisabled()).toBe(false);

    await click(actionButton());

    expect(actionButton().findLoadingIndicator()).not.toBeNull();
    expect(actionButton().isDisabled()).toBe(true);

    // A second activation while in flight must not re-invoke.
    await click(actionButton());
    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(otherApiCalls).toEqual([]);
  });

  it('disables the action with the permission message for a role without packaging permission (Req 1.6)', async () => {
    renderSection(baseModel, 'Viewer');

    expect(actionButton().isDisabled()).toBe(true);
    expect(
      screen.getByTestId('vllm-publish-permission-message').textContent
    ).toBe(PERMISSION_MESSAGE);

    await click(actionButton());
    expect(startPackaging).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });

  it('renders the publish-only retry loading and rejects further activations while its request is in flight', async () => {
    publishGreengrassComponent.mockReturnValueOnce(new Promise(() => {}));
    renderSection({
      ...baseModel,
      packaged_components: [
        { target: 'jetson-xavier-jp6', status: 'packaged' },
      ],
    });

    expect(retryButton()).not.toBeNull();
    await click(retryButton()!);

    expect(retryButton()!.findLoadingIndicator()).not.toBeNull();
    expect(retryButton()!.isDisabled()).toBe(true);
    // The main action is also blocked while a session is in flight.
    expect(actionButton().isDisabled()).toBe(true);

    await click(retryButton()!);
    expect(publishGreengrassComponent).toHaveBeenCalledTimes(1);
    expect(startPackaging).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });
});

describe('VllmPackagePublishSection — banner rendering per panel-state variant', () => {
  it('error banner shows the message and failing step and re-enables the action (Req 3.1)', async () => {
    startPackaging.mockRejectedValueOnce(
      new ApiError('vLLM packaging failed: artifact upload error', 502, undefined, {
        failed_step: 'artifact_upload',
      })
    );
    renderSection(baseModel);

    await click(actionButton());

    const error = screen.getByTestId('vllm-publish-error');
    expect(error.textContent).toContain(
      'vLLM packaging failed: artifact upload error'
    );
    expect(error.textContent).toContain('Failed step: artifact_upload');
    // Re-enabled for retry (Req 3.1); no other banner variants shown.
    expect(actionButton().isDisabled()).toBe(false);
    expect(actionButton().findLoadingIndicator()).toBeNull();
    expect(screen.queryByTestId('vllm-publish-success')).toBeNull();
    expect(screen.queryByTestId('vllm-publish-pending')).toBeNull();
    expect(screen.queryByTestId('vllm-publish-progress')).toBeNull();
  });

  it('success and progress banners render while the polling session is active', async () => {
    startPackaging.mockResolvedValueOnce(PACKAGING_ACCEPTED_RESPONSE);
    getModel.mockResolvedValue({ model: pendingPolledRecord });
    renderSection(baseModel);

    await click(actionButton());

    expect(screen.getByTestId('vllm-publish-success').textContent).toBe(
      PACKAGING_ACCEPTED_MESSAGE
    );
    expect(
      screen.getByTestId('vllm-publish-progress').textContent
    ).toContain(PROGRESS_MESSAGE);
    expect(screen.queryByTestId('vllm-publish-error')).toBeNull();
    expect(screen.queryByTestId('vllm-publish-pending')).toBeNull();
  });

  it('publish-complete success banner renders when a poll observes the new published component', async () => {
    vi.useFakeTimers();
    try {
      startPackaging.mockResolvedValueOnce(PACKAGING_ACCEPTED_RESPONSE);
      getModel.mockResolvedValue({ model: publishedPolledRecord });
      renderSection(baseModel);

      await click(actionButton());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      });

      const success = screen.getByTestId('vllm-publish-success');
      expect(success.textContent).toContain('model-vllm-my-llm');
      expect(success.textContent).toContain('1.0.0');
      expect(success.textContent).toContain('published successfully');
      expect(screen.queryByTestId('vllm-publish-progress')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-error')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-pending')).toBeNull();
      expect(actionButton().isDisabled()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('pending banner renders after the 5-minute polling deadline (Req 2.5)', async () => {
    vi.useFakeTimers();
    try {
      startPackaging.mockResolvedValueOnce(PACKAGING_ACCEPTED_RESPONSE);
      getModel.mockResolvedValue({ model: pendingPolledRecord });
      renderSection(baseModel);

      await click(actionButton());
      expect(screen.getByTestId('vllm-publish-progress')).not.toBeNull();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_TIMEOUT_MS);
      });

      expect(screen.getByTestId('vllm-publish-pending').textContent).toBe(
        PUBLISH_PENDING_MESSAGE
      );
      expect(screen.queryByTestId('vllm-publish-progress')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-success')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-error')).toBeNull();
      // The action is re-enabled so the user can retry after refresh.
      expect(actionButton().isDisabled()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
