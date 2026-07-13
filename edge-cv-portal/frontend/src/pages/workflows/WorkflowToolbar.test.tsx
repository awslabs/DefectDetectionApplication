/**
 * Component tests for the Workflow_Builder actions toolbar (task 8.5):
 * role gating of Save/Validate/Duplicate/Delete (Requirements 11.1,
 * 11.3), first-save naming and versioned saves (5.1, 5.2), the validate
 * action invoking the backend and displaying the complete findings list
 * (4.8, 4.9), duplication (5.7), and delete confirmation (5.5, 5.6).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import WorkflowToolbar, { canEditWorkflows, type WorkflowMeta } from './WorkflowToolbar';
import type { UserRole } from '../../types';
import type { ValidationFinding, WorkflowDefinition } from './types';

const {
  listWorkflows,
  createWorkflow,
  updateWorkflow,
  deleteWorkflow,
  duplicateWorkflow,
  validateWorkflow,
} = vi.hoisted(() => ({
  listWorkflows: vi.fn(),
  createWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
  deleteWorkflow: vi.fn(),
  duplicateWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
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
      createWorkflow,
      updateWorkflow,
      deleteWorkflow,
      duplicateWorkflow,
      validateWorkflow,
    },
  };
});

// --------------------------------------------------------------------------
// Fixtures and helpers
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

function renderToolbar(overrides: {
  role?: UserRole;
  workflow?: WorkflowMeta | null;
  dirty?: boolean;
  onSaved?: (meta: WorkflowMeta) => void;
  onOpenWorkflow?: (workflowId: string) => Promise<void>;
  onDeleted?: () => void;
  onNew?: () => void;
} = {}) {
  return render(
    <WorkflowToolbar
      role={overrides.role ?? 'DataScientist'}
      usecaseId="uc-1"
      workflow={overrides.workflow === undefined ? WORKFLOW : overrides.workflow}
      dirty={overrides.dirty ?? false}
      getDefinition={() => DEFINITION}
      onSaved={overrides.onSaved ?? vi.fn()}
      onOpenWorkflow={overrides.onOpenWorkflow ?? vi.fn()}
      onDeleted={overrides.onDeleted ?? vi.fn()}
      onNew={overrides.onNew ?? vi.fn()}
    />
  );
}

function actionButton(name: string): HTMLElement {
  return screen.getByRole('button', { name });
}

/**
 * CloudScape disables buttons either natively or via aria-disabled (when
 * a disabledReason tooltip is attached), so accept both.
 */
function isDisabled(button: HTMLElement): boolean {
  return (
    (button as HTMLButtonElement).disabled === true ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listWorkflows.mockResolvedValue({ workflows: [], count: 0 });
});

// --------------------------------------------------------------------------
// Role gating (Requirements 11.1, 11.3)
// --------------------------------------------------------------------------

describe('WorkflowToolbar role gating', () => {
  const MUTATING_ACTIONS = ['Save', 'Validate', 'Duplicate', 'Delete'];

  it.each<UserRole>(['Viewer', 'Operator'])(
    'disables Save/Validate/Duplicate/Delete for the %s role (11.3)',
    (role) => {
      renderToolbar({ role });
      for (const action of MUTATING_ACTIONS) {
        expect(isDisabled(actionButton(action)), `${action} should be disabled`).toBe(true);
      }
      // Every role holds workflow:read, so Open stays available.
      expect(isDisabled(actionButton('Open'))).toBe(false);
    }
  );

  it.each<UserRole>(['DataScientist', 'UseCaseAdmin', 'PortalAdmin'])(
    'enables Save/Validate/Duplicate/Delete for the %s role (11.1)',
    (role) => {
      renderToolbar({ role });
      for (const action of MUTATING_ACTIONS) {
        expect(isDisabled(actionButton(action)), `${action} should be enabled`).toBe(false);
      }
    }
  );

  it('disables Validate/Duplicate/Delete until the workflow is saved', () => {
    renderToolbar({ workflow: null });
    expect(isDisabled(actionButton('Save'))).toBe(false);
    for (const action of ['Validate', 'Duplicate', 'Delete']) {
      expect(isDisabled(actionButton(action)), `${action} should be disabled`).toBe(true);
    }
  });

  it('exposes the role predicate used for gating', () => {
    expect(canEditWorkflows('DataScientist')).toBe(true);
    expect(canEditWorkflows('UseCaseAdmin')).toBe(true);
    expect(canEditWorkflows('PortalAdmin')).toBe(true);
    expect(canEditWorkflows('Operator')).toBe(false);
    expect(canEditWorkflows('Viewer')).toBe(false);
    expect(canEditWorkflows(undefined)).toBe(false);
  });
});

// --------------------------------------------------------------------------
// New: start a fresh workflow
// --------------------------------------------------------------------------

describe('WorkflowToolbar new workflow', () => {
  it('places the New action left of Open', () => {
    renderToolbar();
    const toolbar = screen.getByRole('toolbar', { name: 'Workflow actions' });
    const labels = Array.from(toolbar.querySelectorAll('button')).map(
      (button) => button.textContent
    );
    expect(labels.indexOf('New')).toBeGreaterThanOrEqual(0);
    expect(labels.indexOf('New')).toBeLessThan(labels.indexOf('Open'));
  });

  it('starts a fresh workflow immediately when the canvas has no unsaved changes', () => {
    const onNew = vi.fn();
    renderToolbar({ dirty: false, onNew });

    fireEvent.click(actionButton('New'));

    // No confirmation step: the parent resets right away.
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it('asks for confirmation when the canvas is dirty and discards only on confirm', () => {
    const onNew = vi.fn();
    renderToolbar({ dirty: true, onNew });

    fireEvent.click(actionButton('New'));

    // The unsaved-changes confirmation modal appears; nothing resets yet.
    expect(onNew).not.toHaveBeenCalled();
    expect(
      screen.getByText('The canvas has unsaved changes. Start a new workflow and discard them?')
    ).toBeInTheDocument();

    fireEvent.click(actionButton('Discard and start new'));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it('keeps the canvas untouched when the confirmation is cancelled', () => {
    const onNew = vi.fn();
    renderToolbar({ dirty: true, onNew });

    fireEvent.click(actionButton('New'));

    // Scope Cancel to the confirmation dialog: other (hidden) toolbar
    // modals also render a Cancel button into the DOM.
    const dialog = screen
      .getByText('The canvas has unsaved changes. Start a new workflow and discard them?')
      .closest('[class*="awsui_dialog"]') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(onNew).not.toHaveBeenCalled();
  });

  it('stays available regardless of role (clearing the canvas is a local action)', () => {
    renderToolbar({ role: 'Viewer' });
    expect(isDisabled(actionButton('New'))).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Save (Requirements 5.1, 5.2)
// --------------------------------------------------------------------------

describe('WorkflowToolbar save', () => {
  it('prompts for a name on first save and creates the workflow (5.1)', async () => {
    createWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-new',
        usecase_id: 'uc-1',
        name: 'My workflow',
        description: '',
        created_at: 1,
        updated_at: 1,
        latest_version: 1,
      },
      version: 1,
    });
    const onSaved = vi.fn();
    renderToolbar({ workflow: null, onSaved });

    fireEvent.click(actionButton('Save'));
    const nameInput = document.querySelector('input[aria-label="Workflow name"]');
    expect(nameInput).not.toBeNull();
    fireEvent.change(nameInput!, { target: { value: 'My workflow' } });
    fireEvent.click(actionButton('Save workflow'));

    await waitFor(() =>
      expect(createWorkflow).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        name: 'My workflow',
        description: undefined,
        definition: DEFINITION,
      })
    );
    expect(onSaved).toHaveBeenCalledWith({
      workflowId: 'wf-new',
      name: 'My workflow',
      description: '',
      version: 1,
    });
  });

  it('saves an existing workflow as a new version (5.2)', async () => {
    updateWorkflow.mockResolvedValue({
      workflow: { ...WORKFLOW, workflow_id: 'wf-1', latest_version: 4 },
      version: 4,
    });
    const onSaved = vi.fn();
    renderToolbar({ dirty: true, onSaved });

    fireEvent.click(actionButton('Save'));

    await waitFor(() =>
      expect(updateWorkflow).toHaveBeenCalledWith('wf-1', { definition: DEFINITION })
    );
    expect(onSaved).toHaveBeenCalledWith({ ...WORKFLOW, version: 4 });
    expect(await screen.findByText("Saved 'Line inspection' as version 4")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Validate (Requirements 4.8, 4.9)
// --------------------------------------------------------------------------

describe('WorkflowToolbar validate', () => {
  const FINDINGS: ValidationFinding[] = [
    {
      severity: 'error',
      code: 'V1_NO_OUTPUT_NODE',
      message: 'Workflow has no output node',
      nodeId: null,
      connectionId: null,
    },
    {
      severity: 'error',
      code: 'V4_MISSING_REQUIRED_PARAMETER',
      message: "Required parameter 'modelName' has no value",
      nodeId: 'model_inference_1',
      connectionId: null,
    },
    {
      severity: 'warning',
      code: 'W1_UNUSED_OUTPUT_PORT',
      message: "Output port 'out' is unused",
      nodeId: 'crop_1',
      connectionId: null,
    },
  ];

  it('invokes backend validation and displays the complete findings list (4.8, 4.9)', async () => {
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      passed: false,
      validation_status: { status: 'failed' },
      findings: FINDINGS,
      error_count: 2,
      warning_count: 1,
    });
    renderToolbar();

    fireEvent.click(actionButton('Validate'));

    await waitFor(() => expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 3));
    // No unsaved changes: nothing is re-saved before validating.
    expect(updateWorkflow).not.toHaveBeenCalled();

    expect(
      await screen.findByText('Validation failed: 2 errors, 1 warning')
    ).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Validation findings' });
    const items = list.querySelectorAll('li');
    expect(items).toHaveLength(FINDINGS.length);
    expect(items[0].textContent).toContain('[error] V1_NO_OUTPUT_NODE: Workflow has no output node');
    expect(items[1].textContent).toContain(
      "[error] V4_MISSING_REQUIRED_PARAMETER: Required parameter 'modelName' has no value (node: model_inference_1)"
    );
    expect(items[2].textContent).toContain(
      "[warning] W1_UNUSED_OUTPUT_PORT: Output port 'out' is unused (node: crop_1)"
    );
  });

  it('saves unsaved changes as a new version before validating', async () => {
    updateWorkflow.mockResolvedValue({
      workflow: { ...WORKFLOW, workflow_id: 'wf-1', latest_version: 4 },
      version: 4,
    });
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 4,
      passed: true,
      validation_status: { status: 'passed' },
      findings: [],
      error_count: 0,
      warning_count: 0,
    });
    renderToolbar({ dirty: true });

    fireEvent.click(actionButton('Validate'));

    await waitFor(() => expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 4));
    expect(updateWorkflow).toHaveBeenCalledWith('wf-1', { definition: DEFINITION });
    expect(await screen.findByText('Validation passed (0 warnings)')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Duplicate (Requirement 5.7) and Delete (Requirements 5.5, 5.6)
// --------------------------------------------------------------------------

describe('WorkflowToolbar duplicate and delete', () => {
  it('duplicates the workflow under a new name (5.7)', async () => {
    duplicateWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-copy',
        usecase_id: 'uc-1',
        name: 'Line inspection (copy)',
        description: '',
        created_at: 2,
        updated_at: 2,
        latest_version: 1,
      },
      version: 1,
    });
    renderToolbar();

    fireEvent.click(actionButton('Duplicate'));

    await waitFor(() => expect(duplicateWorkflow).toHaveBeenCalledWith('wf-1'));
    expect(
      await screen.findByText("Duplicated as 'Line inspection (copy)'")
    ).toBeInTheDocument();
  });

  it('deletes only after confirmation (5.5)', async () => {
    deleteWorkflow.mockResolvedValue({ workflow_id: 'wf-1', message: 'deleted' });
    const onDeleted = vi.fn();
    renderToolbar({ onDeleted });

    fireEvent.click(actionButton('Delete'));
    expect(deleteWorkflow).not.toHaveBeenCalled();
    expect(
      screen.getByText("Delete 'Line inspection' and all its versions? This cannot be undone.")
    ).toBeInTheDocument();

    fireEvent.click(actionButton('Delete workflow'));

    await waitFor(() => expect(deleteWorkflow).toHaveBeenCalledWith('wf-1'));
    expect(onDeleted).toHaveBeenCalled();
  });

  it('surfaces the referencing deployments when delete is rejected (5.6)', async () => {
    const { ApiError } = await import('../../services/api');
    deleteWorkflow.mockRejectedValue(
      new ApiError(
        'Workflow cannot be deleted while active deployments reference it',
        409,
        'WORKFLOW_HAS_ACTIVE_DEPLOYMENTS',
        { deployment_ids: ['dep-1', 'dep-2'] }
      )
    );
    const onDeleted = vi.fn();
    renderToolbar({ onDeleted });

    fireEvent.click(actionButton('Delete'));
    fireEvent.click(actionButton('Delete workflow'));

    expect(
      await screen.findByText(
        'Workflow cannot be deleted while active deployments reference it (deployments: dep-1, dep-2)'
      )
    ).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
