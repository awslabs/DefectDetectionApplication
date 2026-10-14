/**
 * Regression tests for the Package_Dialog error paths that the task-6.1
 * composition change must NOT have altered (vllm-sizing-and-packaging-errors
 * task 6.3):
 *
 * - a packaging failure carrying a findings list still displays the joined
 *   findings messages (Requirement 5.2), and
 * - the "already exists" rewrite is unchanged and still fires now that the
 *   backend message is included in the composed content (Requirement 5.4).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
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

/** Open the package modal, select JetPack 6, and click the confirm button. */
async function packageOnce(container: HTMLElement) {
  fireEvent.click(within(container).getByRole('button', { name: 'Package' }));
  await screen.findByText('Package workflow for deployment');
  await selectArch('JetPack 6');
  fireEvent.click(screen.getByRole('button', { name: 'Package workflow' }));
}

/** Text content of the modal's "Package failed" error Alert. */
async function packageAlertText(): Promise<string> {
  const header = await screen.findByText('Package failed');
  const alertRoot = header.closest('[class*="awsui_alert"]');
  return (alertRoot ?? document.body).textContent ?? '';
}

beforeEach(() => {
  vi.clearAllMocks();
  listWorkflows.mockResolvedValue({ workflows: [], count: 0 });
  validateWorkflow.mockResolvedValue(PASSED_VALIDATION);
});

// --------------------------------------------------------------------------
// Requirement 5.2: findings list still joined
// --------------------------------------------------------------------------

describe('Package_Dialog findings path (regression, Req 5.2)', () => {
  it('joins the findings messages exactly as before', async () => {
    const { ApiError } = await import('../../services/api');
    packageWorkflow.mockRejectedValue(
      new ApiError('Packaging failed', 502, 'PACKAGING_FAILED', {
        findings: [
          { message: 'Plugin plugin-a is not published for arm64_jp6' },
          { code: 'UNSUPPORTED_NODE', nodeId: 'node_7', arch: 'arm64_jp6' },
        ],
      })
    );

    const { container } = renderToolbar();
    await packageOnce(container);

    await waitFor(async () => {
      const text = await packageAlertText();
      // Joined with '; ', message preferred, code/node/arch fallback shape.
      expect(text).toContain(
        'Plugin plugin-a is not published for arm64_jp6; ' +
          'UNSUPPORTED_NODE (node node_7) [arm64_jp6]'
      );
    });
    // The findings branch wins over the message + failing_artifact branch.
    expect((await packageAlertText())).not.toContain('failing artifact');
  });

  it('prefers findings over failing_artifact when both are present', async () => {
    const { ApiError } = await import('../../services/api');
    packageWorkflow.mockRejectedValue(
      new ApiError('Backend message', 502, 'PACKAGING_FAILED', {
        findings: [{ message: 'finding one' }, { message: 'finding two' }],
        failing_artifact: 'models/my-model',
      })
    );

    const { container } = renderToolbar();
    await packageOnce(container);

    await waitFor(async () => {
      const text = await packageAlertText();
      expect(text).toContain('finding one; finding two');
      expect(text).not.toContain('failing artifact');
      expect(text).not.toContain('models/my-model');
    });
  });
});

// --------------------------------------------------------------------------
// Requirement 5.4: already-exists rewrite unchanged, message now included
// --------------------------------------------------------------------------

describe('Package_Dialog already-exists rewrite (regression, Req 5.4)', () => {
  const REWRITE =
    'This workflow version is already packaged as a Greengrass component ' +
    '(component versions are immutable). Make an edit and Save to create a ' +
    'new version, then package that version.';

  it(
    'still rewrites when the backend message (now included in the composed ' +
      'content) matches "already exists"',
    async () => {
      const { ApiError } = await import('../../services/api');
      packageWorkflow.mockRejectedValue(
        new ApiError(
          'Component version 3.0.0 already exists for dda.workflow.wf-1',
          502,
          'PACKAGING_FAILED',
          { failing_artifact: 'component dda.workflow.wf-1 v3.0.0' }
        )
      );

      const { container } = renderToolbar();
      await packageOnce(container);

      await waitFor(async () => {
        const text = await packageAlertText();
        expect(text).toContain(REWRITE);
        // The rewrite replaces the composed content entirely.
        expect(text).not.toContain('failing artifact');
        expect(text).not.toContain('Component version 3.0.0 already exists');
      });
    }
  );

  it('rewrites a plain "already exists" message without structured details', async () => {
    const { ApiError } = await import('../../services/api');
    packageWorkflow.mockRejectedValue(
      new ApiError('Component version 3.0.0 already exists', 502, 'PACKAGING_FAILED')
    );

    const { container } = renderToolbar();
    await packageOnce(container);

    await waitFor(async () => {
      expect(await packageAlertText()).toContain(REWRITE);
    });
  });
});
