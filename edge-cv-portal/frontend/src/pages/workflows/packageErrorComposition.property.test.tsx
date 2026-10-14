/**
 * **Feature: vllm-sizing-and-packaging-errors, Property 7: Package_Dialog
 * message composition**
 *
 * For any ApiError with a message and a string `failing_artifact` (and no
 * findings list), the dialog content contains both the backend message and
 * the failing artifact; when the artifact is `models/{name}` and the message
 * states the model has no published Greengrass component, the content
 * additionally contains the Models-page publish hint; and for any content
 * matching "already exists", the final displayed content is the existing
 * immutability rewrite.
 *
 * **Validates: Requirements 5.1, 5.3, 5.4**
 *
 * The composition lives in `WorkflowToolbar.handleConfirmPackage`'s error
 * path, so the property drives the real component: the package modal is
 * opened once, an architecture is selected, and each generated case rejects
 * `apiService.packageWorkflow` with a fresh ApiError before clicking the
 * confirm button and asserting on the error Alert's rendered text.
 */

import { describe, expect, it, vi } from 'vitest';
import * as fc from 'fast-check';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import WorkflowToolbar, { type WorkflowMeta } from './WorkflowToolbar';
import type { WorkflowDefinition } from './types';

const { listWorkflows, validateWorkflow, packageWorkflow } = vi.hoisted(() => ({
  listWorkflows: vi.fn(),
  validateWorkflow: vi.fn(),
  packageWorkflow: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
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
  return {
    ApiError,
    apiService: {
      listWorkflows,
      createWorkflow: vi.fn(),
      updateWorkflow: vi.fn(),
      deleteWorkflow: vi.fn(),
      duplicateWorkflow: vi.fn(),
      validateWorkflow,
      packageWorkflow,
    },
  };
});

// --------------------------------------------------------------------------
// Fixtures and helpers (mirroring WorkflowToolbar.test.tsx)
// --------------------------------------------------------------------------

const DEFINITION: WorkflowDefinition = {
  schemaVersion: 1,
  nodes: [
    {
      id: 'camera_source_1',
      type: 'camera_source',
      position: { x: 0, y: 0 },
      parameters: { device: '/dev/video0' },
    },
  ],
  connections: [],
};

const WORKFLOW: WorkflowMeta = {
  workflowId: 'wf-1',
  name: 'Line inspection',
  description: '',
  version: 3,
};

const PASSED_VALIDATION = {
  run_id: 'v-1',
  workflow_id: 'wf-1',
  version: 3,
  passed: true,
  validation_status: { status: 'passed' as const },
  findings: [],
  error_count: 0,
  warning_count: 0,
};

function renderToolbar() {
  return render(
    <WorkflowToolbar
      role="DataScientist"
      usecaseId="uc-1"
      workflow={WORKFLOW}
      dirty={false}
      getDefinition={() => DEFINITION}
      onSaved={vi.fn()}
      onOpenWorkflow={vi.fn()}
      onDeleted={vi.fn()}
      onNew={vi.fn()}
    />
  );
}

async function selectArch(optionLabel: string) {
  const picker = screen.getByRole('button', { name: /Select target architectures/ });
  fireEvent.mouseDown(picker);
  fireEvent.click(picker);
  const option = await screen.findByRole('option', { name: new RegExp(optionLabel) });
  fireEvent.mouseDown(option);
  fireEvent.mouseUp(option);
  fireEvent.click(option);
}

function isDisabled(button: HTMLElement): boolean {
  return (
    (button as HTMLButtonElement).disabled === true ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

/** Text of the modal's error Alert (scoped to the Alert when possible). */
function packageAlertText(): string {
  const header = screen.getByText('Package failed');
  const alertRoot = header.closest('[class*="awsui_alert"]');
  return (alertRoot ?? document.body).textContent ?? '';
}

// --------------------------------------------------------------------------
// Generators — controlled vocabulary so no generated fragment collides with
// the modal's static copy or the composition's own marker strings.
// --------------------------------------------------------------------------

const WORDS = [
  'packaging',
  'failed',
  'because',
  'the',
  'registry',
  'entry',
  'was',
  'missing',
  'for',
  'this',
  'target',
  'q1w2e3',
] as const;

const sentenceArb: fc.Arbitrary<string> = fc
  .array(fc.constantFrom(...WORDS), { minLength: 1, maxLength: 6 })
  .map((words) => words.join(' '));

const nameArb: fc.Arbitrary<string> = fc
  .array(fc.constantFrom('vllm', 'qwen', 'yolo', 'demo', '7b', 'x9'), {
    minLength: 1,
    maxLength: 3,
  })
  .map((parts) => parts.join('-'));

/** models/{name} artifacts and non-model artifacts, both exercised. */
const artifactArb: fc.Arbitrary<string> = fc.oneof(
  nameArb.map((name) => `models/${name}`),
  nameArb.map((name) => `plugins/${name}`),
  nameArb.map((name) => `component dda.workflow.${name} v3.0.0`)
);

type MessageKind = 'plain' | 'unpublished' | 'already-exists';

const messageArb: fc.Arbitrary<string> = fc
  .tuple(sentenceArb, fc.constantFrom<MessageKind>('plain', 'unpublished', 'already-exists'), sentenceArb)
  .map(([pre, kind, post]) => {
    switch (kind) {
      case 'unpublished':
        return `${pre} has no published Greengrass component ${post}`;
      case 'already-exists':
        return `${pre} already exists ${post}`;
      default:
        return `${pre} ${post}`;
    }
  });

const caseArb = fc.record({ message: messageArb, artifact: artifactArb });

// Fragments unique to each composition outcome.
const HINT_FRAGMENT = 'open the Models page and use Package & Publish';
const REWRITE_FRAGMENT =
  'already packaged as a Greengrass component (component versions are immutable)';
const ARTIFACT_MARKER = '(failing artifact:';

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Package_Dialog message composition (Property 7)', () => {
  it(
    'composes message + artifact, adds the Models-page hint for unpublished models, ' +
      'and applies the already-exists rewrite last (Req 5.1, 5.3, 5.4)',
    async () => {
      listWorkflows.mockResolvedValue({ workflows: [], count: 0 });
      validateWorkflow.mockResolvedValue(PASSED_VALIDATION);

      const { ApiError } = await import('../../services/api');
      const { container } = renderToolbar();

      // Open the package modal once and pick an architecture; the modal
      // stays open across failures, so every generated case reuses it.
      fireEvent.click(within(container).getByRole('button', { name: 'Package' }));
      await screen.findByText('Package workflow for deployment');
      await selectArch('JetPack 6');
      const confirm = screen.getByRole('button', { name: 'Package workflow' });
      await waitFor(() => expect(isDisabled(confirm)).toBe(false));

      let expectedCalls = 0;
      await fc.assert(
        fc.asyncProperty(caseArb, async ({ message, artifact }) => {
          packageWorkflow.mockRejectedValueOnce(
            new ApiError(message, 502, 'PACKAGING_FAILED', { failing_artifact: artifact })
          );
          expectedCalls += 1;

          await waitFor(() => expect(isDisabled(confirm)).toBe(false));
          fireEvent.click(confirm);
          await waitFor(() => expect(packageWorkflow).toHaveBeenCalledTimes(expectedCalls));
          // The busy flag clears in the same commit that stores the new
          // error content, so once the confirm button re-enables the Alert
          // reflects THIS case (never a stale previous iteration).
          await waitFor(() => expect(isDisabled(confirm)).toBe(false));

          // The property's oracle, phrased from the requirements (not the
          // implementation): compose, hint, then rewrite-last.
          const expectHint =
            artifact.startsWith('models/') &&
            /no published Greengrass component/i.test(message);
          const composed =
            `${message} ${ARTIFACT_MARKER} ${artifact})` +
            (expectHint ? ` ... ${HINT_FRAGMENT} ...` : '');
          const expectRewrite = /already exists/i.test(composed);

          await waitFor(() => {
            const text = packageAlertText();
            if (expectRewrite) {
              // Req 5.4: the rewrite is final and replaces the composition.
              expect(text).toContain(REWRITE_FRAGMENT);
              expect(text).not.toContain(ARTIFACT_MARKER);
            } else {
              // Req 5.1: both the backend message and the artifact are shown.
              expect(text).toContain(message);
              expect(text).toContain(`${ARTIFACT_MARKER} ${artifact})`);
              // Req 5.3: hint present exactly for the unpublished-model case.
              if (expectHint) {
                expect(text).toContain(HINT_FRAGMENT);
              } else {
                expect(text).not.toContain(HINT_FRAGMENT);
              }
            }
          });
        }),
        { numRuns: 30 }
      );
    },
    120_000
  );
});
