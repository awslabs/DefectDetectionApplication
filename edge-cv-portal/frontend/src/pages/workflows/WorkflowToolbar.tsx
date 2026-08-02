/**
 * Workflow_Builder actions toolbar and Workflow_Store API wiring
 * (Requirements 4.8, 4.9, 5.1, 5.2, 5.7, 11.1, 11.3).
 *
 * Actions:
 *   - New: start a fresh workflow — clears the canvas and resets the
 *     workflow meta to unsaved, with an unsaved-changes confirmation
 *     modal when the canvas is dirty.
 *   - Open: pick a saved workflow of the selected Use_Case and load it
 *     onto the canvas (the parent renders nodes, positions,
 *     configurations, and connections, Requirement 5.4).
 *   - Save: first save prompts for a name and creates version 1 via
 *     POST /workflows (5.1); subsequent saves create a new version via
 *     PUT /workflows/{id}, retaining prior versions (5.2).
 *   - Validate: user-invocable full backend validation (4.8). Unsaved
 *     changes are saved as a new version first so the validated version
 *     is the current canvas definition; the complete findings list
 *     (severity/code/message/node/connection) is displayed (4.9).
 *   - Duplicate: copies the workflow under a new name (5.7).
 *   - Delete: confirm dialog, then DELETE /workflows/{id}; a rejection
 *     for active deployments identifies the referencing deployments (5.6).
 *
 * Role gating (11.1, 11.3): Save/Validate/Duplicate/Delete require a
 * workflow create/edit role (DataScientist, UseCaseAdmin, PortalAdmin)
 * per the design RBAC matrix; Viewer and Operator see the builder
 * read-only (Open stays available since every role holds workflow:read).
 * The role comes from the existing auth context via the parent page.
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Modal from '@cloudscape-design/components/modal';
import Multiselect, { type MultiselectProps } from '@cloudscape-design/components/multiselect';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { ApiError, apiService } from '../../services/api';
import type { UserRole } from '../../types';
import type { WorkflowDefinition, WorkflowValidationRun } from './types';

// --------------------------------------------------------------------------
// Role gating (Requirements 11.1, 11.3, design RBAC matrix)
// --------------------------------------------------------------------------

/** Roles permitted to create, edit, save, and delete workflows (11.1). */
export const WORKFLOW_EDIT_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/** True when the role may create/edit/save/delete workflows (11.1, 11.3). */
export function canEditWorkflows(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && WORKFLOW_EDIT_ROLES.includes(role);
}

// --------------------------------------------------------------------------
// Packaging (workflow_packaging.py, POST /workflows/{id}/package)
// --------------------------------------------------------------------------

/**
 * The DDA target architectures a workflow can be packaged for — the
 * backend DEVICE_ARCHITECTURES set (workflow_core.catalog). Packaging for
 * an architecture registers per-arch artifacts in the
 * dda.workflow.{id} Greengrass component; a device deployment then only
 * offers the component for a device whose recorded Target_Architecture is
 * among the packaged set.
 */
export const PACKAGE_ARCH_OPTIONS: readonly MultiselectProps.Option[] = [
  { value: 'x86_64', label: 'x86_64 (CPU)' },
  { value: 'x86_64_nvidia', label: 'x86_64 (NVIDIA GPU)' },
  { value: 'arm64_jp4', label: 'ARM64 · JetPack 4' },
  { value: 'arm64_jp5', label: 'ARM64 · JetPack 5' },
  { value: 'arm64_jp6', label: 'ARM64 · JetPack 6' },
];

// --------------------------------------------------------------------------
// Props
// --------------------------------------------------------------------------

/** Identity of the workflow currently loaded on the canvas. */
export interface WorkflowMeta {
  workflowId: string;
  name: string;
  description: string;
  version: number;
}

export interface WorkflowToolbarProps {
  /** The acting user's role from the auth context (11.1, 11.3). */
  role: UserRole | undefined;
  /** The selected Use_Case scoping saves and the Open picker (5.1). */
  usecaseId: string | null;
  /** The loaded workflow, or null while the canvas is unsaved. */
  workflow: WorkflowMeta | null;
  /** True when the canvas differs from the last saved/loaded state. */
  dirty: boolean;
  /** The current canvas as a Workflow_Definition document. */
  getDefinition: () => WorkflowDefinition;
  /** Called after every successful save with the new identity/version. */
  onSaved: (meta: WorkflowMeta) => void;
  /** Called to load a saved workflow onto the canvas (5.4). */
  onOpenWorkflow: (workflowId: string) => Promise<void> | void;
  /** Called after a successful delete so the parent can reset the canvas. */
  onDeleted: () => void;
  /** Called to start a fresh workflow: the parent clears the canvas and
   * resets the workflow meta to unsaved. */
  onNew: () => void;
}

interface Notice {
  type: 'success' | 'error';
  header: string;
  content?: string;
  /** When set, the notice offers a "Deploy this workflow" action that
   * opens Create Deployment with this packaged component pre-selected. */
  deployComponentName?: string;
}

// --------------------------------------------------------------------------
// Toolbar
// --------------------------------------------------------------------------

export default function WorkflowToolbar({
  role,
  usecaseId,
  workflow,
  dirty,
  getDefinition,
  onSaved,
  onOpenWorkflow,
  onDeleted,
  onNew,
}: WorkflowToolbarProps) {
  const canEdit = canEditWorkflows(role);
  const navigate = useNavigate();

  // One in-flight action at a time; every button disables while busy.
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [validation, setValidation] = useState<WorkflowValidationRun | null>(null);

  // Save-as modal (first save prompts for a name, Requirement 5.1)
  const [saveModalVisible, setSaveModalVisible] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saveDescription, setSaveDescription] = useState('');

  // Open picker modal (5.4)
  const [openModalVisible, setOpenModalVisible] = useState(false);
  const [openOptions, setOpenOptions] = useState<SelectProps.Option[] | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const [openSelection, setOpenSelection] = useState<SelectProps.Option | null>(null);

  // Delete confirmation modal (5.5, 5.6)
  const [deleteModalVisible, setDeleteModalVisible] = useState(false);

  // New-workflow unsaved-changes confirmation modal
  const [newModalVisible, setNewModalVisible] = useState(false);

  // Package modal (select target architectures, POST /workflows/{id}/package)
  const [packageModalVisible, setPackageModalVisible] = useState(false);
  const [packageArchs, setPackageArchs] = useState<readonly MultiselectProps.Option[]>([]);
  // In-modal error (shown inside the Package modal so it is never hidden
  // behind the modal overlay the way a toolbar-level notice would be).
  const [packageError, setPackageError] = useState<{ header: string; content?: string } | null>(null);

  const failNotice = useCallback((header: string, err: unknown) => {
    let content = err instanceof Error ? err.message : String(err);
    if (err instanceof ApiError && Array.isArray(err.details?.deployment_ids)) {
      content += ` (deployments: ${(err.details.deployment_ids as string[]).join(', ')})`;
    }
    setNotice({ type: 'error', header, content });
  }, []);

  // ------------------------------------------------------------------
  // New: start a fresh workflow
  // ------------------------------------------------------------------

  /** Reset the toolbar's transient state and hand off to the parent. */
  const startNewWorkflow = useCallback(() => {
    setNewModalVisible(false);
    setNotice(null);
    setValidation(null);
    onNew();
  }, [onNew]);

  const handleNew = useCallback(() => {
    if (dirty) {
      // Unsaved changes on the canvas: confirm before discarding them.
      setNewModalVisible(true);
      return;
    }
    startNewWorkflow();
  }, [dirty, startNewWorkflow]);

  // ------------------------------------------------------------------
  // Save (5.1, 5.2)
  // ------------------------------------------------------------------

  /** PUT the current canvas as a new version of the loaded workflow. */
  const saveNewVersion = useCallback(async (): Promise<WorkflowMeta> => {
    if (workflow === null) {
      throw new Error('No workflow loaded');
    }
    const response = await apiService.updateWorkflow(workflow.workflowId, {
      definition: getDefinition(),
    });
    const meta = { ...workflow, version: response.version };
    onSaved(meta);
    return meta;
  }, [workflow, getDefinition, onSaved]);

  const handleSave = useCallback(async () => {
    if (workflow === null) {
      // First save: prompt for a name (5.1)
      setSaveName('');
      setSaveDescription('');
      setSaveModalVisible(true);
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const meta = await saveNewVersion();
      setNotice({ type: 'success', header: `Saved '${meta.name}' as version ${meta.version}` });
    } catch (err) {
      failNotice('Save failed', err);
    } finally {
      setBusy(false);
    }
  }, [workflow, saveNewVersion, failNotice]);

  const handleCreate = useCallback(async () => {
    if (!usecaseId) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const response = await apiService.createWorkflow({
        usecase_id: usecaseId,
        name: saveName.trim(),
        description: saveDescription.trim() || undefined,
        definition: getDefinition(),
      });
      setSaveModalVisible(false);
      onSaved({
        workflowId: response.workflow.workflow_id,
        name: response.workflow.name,
        description: response.workflow.description ?? '',
        version: response.version,
      });
      setNotice({
        type: 'success',
        header: `Saved '${response.workflow.name}' as version ${response.version}`,
      });
    } catch (err) {
      failNotice('Save failed', err);
    } finally {
      setBusy(false);
    }
  }, [usecaseId, saveName, saveDescription, getDefinition, onSaved, failNotice]);

  // ------------------------------------------------------------------
  // Validate (4.8, 4.9)
  // ------------------------------------------------------------------

  const handleValidate = useCallback(async () => {
    if (workflow === null) {
      return;
    }
    setBusy(true);
    setNotice(null);
    setValidation(null);
    try {
      // Validate the current canvas definition: unsaved changes are
      // saved as a new version first (5.2), then that version is
      // validated by the backend Workflow_Validator (4.9).
      const meta = dirty ? await saveNewVersion() : workflow;
      const result = await apiService.validateWorkflow(meta.workflowId, meta.version);
      setValidation(result);
    } catch (err) {
      failNotice('Validation failed', err);
    } finally {
      setBusy(false);
    }
  }, [workflow, dirty, saveNewVersion, failNotice]);

  // ------------------------------------------------------------------
  // Package (workflow_packaging.py — makes the workflow deployable)
  // ------------------------------------------------------------------

  const handleConfirmPackage = useCallback(async () => {
    if (workflow === null || packageArchs.length === 0) {
      return;
    }
    setBusy(true);
    setNotice(null);
    setPackageError(null);
    try {
      // Package the current canvas definition: save unsaved changes as a
      // new version first (5.2) so the packaged component matches the
      // canvas.
      const meta = dirty ? await saveNewVersion() : workflow;

      // Pre-flight validation gate: the backend Component_Packager rejects
      // packaging unless the version has a recorded PASSED validation
      // (workflow_packaging.validation_guard — VALIDATION_REQUIRED /
      // VALIDATION_FAILED). Validate here first so (a) the required passed
      // record exists, and (b) any validation errors are shown IN THIS
      // MODAL rather than surfacing as an opaque background failure.
      const validationRun = await apiService.validateWorkflow(meta.workflowId, meta.version);
      if (!validationRun.passed) {
        setValidation(validationRun); // also show the full findings list in the toolbar
        setPackageError({
          header:
            `Cannot package: validation found ${validationRun.error_count} ` +
            `error${validationRun.error_count === 1 ? '' : 's'}`,
          content:
            validationRun.findings
              .filter((f) => f.severity === 'error')
              .map((f) => `${f.code}: ${f.message}${f.nodeId ? ` (node ${f.nodeId})` : ''}`)
              .join('; ') || 'Resolve the validation errors, then package again.',
        });
        return;
      }

      // Register the dda.workflow.{id} component for the selected arches.
      const result = await apiService.packageWorkflow(meta.workflowId, {
        architectures: packageArchs.map((option) => option.value as string),
        version: meta.version,
      });
      setPackageModalVisible(false);
      setNotice({
        type: 'success',
        header: `Packaged '${meta.name}' v${meta.version} as ${result.component_name} ${result.component_version}`,
        content:
          `Registered for ${result.architectures.join(', ')}. ` +
          'It is now deployable from Create Deployment — select a target ' +
          'device whose architecture is among the packaged set.',
        deployComponentName: result.component_name,
      });
    } catch (err) {
      // Any failure (save, validation call, or the packaging gates:
      // VALIDATION_REQUIRED/FAILED, UNSUPPORTED/LLM architecture, plugin
      // lifecycle/architecture, packaging failure) is shown IN THE MODAL.
      let content = err instanceof Error ? err.message : String(err);
      if (err instanceof ApiError) {
        const findings = err.details?.findings;
        if (Array.isArray(findings) && findings.length > 0) {
          content = findings
            .map((f: { message?: string; code?: string; nodeId?: string; arch?: string }) =>
              f.message ?? `${f.code ?? 'error'}${f.nodeId ? ` (node ${f.nodeId})` : ''}${f.arch ? ` [${f.arch}]` : ''}`)
            .join('; ');
        } else if (typeof err.details?.failing_artifact === 'string') {
          content = `Failing artifact: ${err.details.failing_artifact}`;
        }
      }
      // Greengrass component versions are immutable and the component
      // version is derived from the workflow version, so re-packaging an
      // already-packaged version conflicts. Make that actionable rather
      // than leaving the raw "already exists" message.
      if (/already exists/i.test(content)) {
        content =
          'This workflow version is already packaged as a Greengrass component ' +
          '(component versions are immutable). Make an edit and Save to create a ' +
          'new version, then package that version.';
      }
      setPackageError({ header: 'Package failed', content });
    } finally {
      setBusy(false);
    }
  }, [workflow, packageArchs, dirty, saveNewVersion]);

  // ------------------------------------------------------------------
  // Duplicate (5.7)
  // ------------------------------------------------------------------

  const handleDuplicate = useCallback(async () => {
    if (workflow === null) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const response = await apiService.duplicateWorkflow(workflow.workflowId);
      setNotice({
        type: 'success',
        header: `Duplicated as '${response.workflow.name}'`,
        content: 'The copy was created from the latest saved version.',
      });
    } catch (err) {
      failNotice('Duplicate failed', err);
    } finally {
      setBusy(false);
    }
  }, [workflow, failNotice]);

  // ------------------------------------------------------------------
  // Delete (5.5, 5.6)
  // ------------------------------------------------------------------

  const handleDelete = useCallback(async () => {
    if (workflow === null) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await apiService.deleteWorkflow(workflow.workflowId);
      setDeleteModalVisible(false);
      setValidation(null);
      setNotice({ type: 'success', header: `Deleted '${workflow.name}'` });
      onDeleted();
    } catch (err) {
      setDeleteModalVisible(false);
      failNotice('Delete failed', err);
    } finally {
      setBusy(false);
    }
  }, [workflow, onDeleted, failNotice]);

  // ------------------------------------------------------------------
  // Open picker (5.4)
  // ------------------------------------------------------------------

  useEffect(() => {
    if (!openModalVisible || !usecaseId) {
      return;
    }
    let cancelled = false;
    setOpenOptions(null);
    setOpenError(null);
    setOpenSelection(null);
    apiService
      .listWorkflows(usecaseId)
      .then((response) => {
        if (!cancelled) {
          setOpenOptions(
            response.workflows.map((wf) => ({
              label: wf.name,
              value: wf.workflow_id,
              description: `v${wf.latest_version}`,
            }))
          );
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setOpenError(err.message || 'Failed to list workflows');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [openModalVisible, usecaseId]);

  const handleOpen = useCallback(async () => {
    const workflowId = openSelection?.value;
    if (!workflowId) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await onOpenWorkflow(workflowId);
      setOpenModalVisible(false);
      setValidation(null);
    } catch (err) {
      failNotice('Open failed', err);
    } finally {
      setBusy(false);
    }
  }, [openSelection, onOpenWorkflow, failNotice]);

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  const editDisabledReason = !canEdit
    ? 'Your role does not permit workflow changes'
    : undefined;
  const needsSaveReason =
    canEdit && workflow === null ? 'Save the workflow first' : undefined;

  return (
    <div role="toolbar" aria-label="Workflow actions">
      <SpaceBetween size="xs">
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Button onClick={handleNew} disabled={busy}>
            New
          </Button>
          <Button onClick={() => setOpenModalVisible(true)} disabled={busy || !usecaseId}>
            Open
          </Button>
          <Button
            variant="primary"
            onClick={handleSave}
            disabled={busy || !canEdit || !usecaseId}
            disabledReason={editDisabledReason}
          >
            Save
          </Button>
          <Button
            onClick={handleValidate}
            disabled={busy || !canEdit || workflow === null}
            disabledReason={editDisabledReason ?? needsSaveReason}
          >
            Validate
          </Button>
          <Button
            onClick={() => {
              setNotice(null);
              setPackageError(null);
              setPackageArchs([]);
              setPackageModalVisible(true);
            }}
            disabled={busy || !canEdit || workflow === null}
            disabledReason={editDisabledReason ?? needsSaveReason}
          >
            Package
          </Button>
          <Button
            onClick={handleDuplicate}
            disabled={busy || !canEdit || workflow === null}
            disabledReason={editDisabledReason ?? needsSaveReason}
          >
            Duplicate
          </Button>
          <Button
            onClick={() => setDeleteModalVisible(true)}
            disabled={busy || !canEdit || workflow === null}
            disabledReason={editDisabledReason ?? needsSaveReason}
          >
            Delete
          </Button>
          <Box color="text-body-secondary" fontSize="body-s">
            {workflow === null
              ? 'Unsaved workflow'
              : `${workflow.name} (v${workflow.version})${dirty ? ' — unsaved changes' : ''}`}
          </Box>
        </SpaceBetween>

        {notice !== null && (
          <Alert
            type={notice.type}
            header={notice.header}
            dismissible
            onDismiss={() => setNotice(null)}
            action={
              notice.deployComponentName && usecaseId ? (
                <Button
                  onClick={() =>
                    navigate(
                      `/deployments/create?usecase_id=${encodeURIComponent(usecaseId)}` +
                        `&clone_components=${encodeURIComponent(notice.deployComponentName as string)}`
                    )
                  }
                >
                  Deploy this workflow
                </Button>
              ) : undefined
            }
          >
            {notice.content}
          </Alert>
        )}

        {validation !== null && (
          <Alert
            type={validation.passed ? 'success' : 'error'}
            dismissible
            onDismiss={() => setValidation(null)}
            header={
              validation.passed
                ? `Validation passed (${validation.warning_count} warning${
                    validation.warning_count === 1 ? '' : 's'
                  })`
                : `Validation failed: ${validation.error_count} error${
                    validation.error_count === 1 ? '' : 's'
                  }, ${validation.warning_count} warning${
                    validation.warning_count === 1 ? '' : 's'
                  }`
            }
          >
            {validation.findings.length === 0 ? (
              'No findings.'
            ) : (
              <ul aria-label="Validation findings" style={{ margin: 0, paddingLeft: 18 }}>
                {validation.findings.map((finding, index) => (
                  <li key={`${finding.code}-${index}`}>
                    [{finding.severity}] {finding.code}: {finding.message}
                    {finding.nodeId !== null && ` (node: ${finding.nodeId})`}
                    {finding.connectionId !== null && ` (connection: ${finding.connectionId})`}
                  </li>
                ))}
              </ul>
            )}
          </Alert>
        )}
      </SpaceBetween>

      {/* First-save name prompt (5.1) */}
      <Modal
        visible={saveModalVisible}
        onDismiss={() => setSaveModalVisible(false)}
        header="Save workflow"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setSaveModalVisible(false)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreate}
                disabled={busy || saveName.trim() === ''}
              >
                Save workflow
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Name">
            <Input
              value={saveName}
              onChange={({ detail }) => setSaveName(detail.value)}
              ariaLabel="Workflow name"
              autoFocus
            />
          </FormField>
          <FormField label={<span>Description <i>- optional</i></span>}>
            <Input
              value={saveDescription}
              onChange={({ detail }) => setSaveDescription(detail.value)}
              ariaLabel="Workflow description"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Open picker (5.4) */}
      <Modal
        visible={openModalVisible}
        onDismiss={() => setOpenModalVisible(false)}
        header="Open workflow"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setOpenModalVisible(false)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleOpen}
                disabled={busy || openSelection === null}
              >
                Open workflow
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField label="Workflow" errorText={openError ?? undefined}>
          <Select
            selectedOption={openSelection}
            onChange={({ detail }) => setOpenSelection(detail.selectedOption)}
            options={openOptions ?? []}
            statusType={openError ? 'error' : openOptions === null ? 'loading' : 'finished'}
            loadingText="Loading workflows"
            placeholder="Choose a workflow"
            empty="No saved workflows for this use case"
            ariaLabel="Workflow to open"
          />
        </FormField>
      </Modal>

      {/* Package: select target architectures, then register the
          dda.workflow.{id} component so the workflow is deployable. */}
      <Modal
        visible={packageModalVisible}
        onDismiss={() => setPackageModalVisible(false)}
        header="Package workflow for deployment"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setPackageModalVisible(false)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleConfirmPackage}
                disabled={busy || packageArchs.length === 0}
                loading={busy}
              >
                Package workflow
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p" color="text-body-secondary">
            Packaging validates this workflow and compiles it into a
            deployable Greengrass component for the selected device
            architectures. The workflow must pass validation first. Only
            devices whose recorded architecture is among the packaged set
            will be offered this workflow on the Create Deployment screen.
            {dirty && ' Unsaved canvas changes will be saved as a new version first.'}
          </Box>
          {packageError !== null && (
            <Alert type="error" header={packageError.header}>
              {packageError.content}
            </Alert>
          )}
          <FormField
            label="Target architectures"
            description="Choose every architecture you intend to deploy this workflow to."
          >
            <Multiselect
              selectedOptions={packageArchs}
              onChange={({ detail }) => setPackageArchs(detail.selectedOptions)}
              options={PACKAGE_ARCH_OPTIONS as MultiselectProps.Option[]}
              placeholder="Select target architectures"
              ariaLabel="Target architectures"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* New-workflow unsaved-changes confirmation */}
      <Modal
        visible={newModalVisible}
        onDismiss={() => setNewModalVisible(false)}
        header="New workflow"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setNewModalVisible(false)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={startNewWorkflow} disabled={busy}>
                Discard and start new
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        The canvas has unsaved changes. Start a new workflow and discard them?
      </Modal>

      {/* Delete confirmation (5.5) */}
      <Modal
        visible={deleteModalVisible}
        onDismiss={() => setDeleteModalVisible(false)}
        header="Delete workflow"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteModalVisible(false)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} disabled={busy}>
                Delete workflow
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {workflow !== null &&
          `Delete '${workflow.name}' and all its versions? This cannot be undone.`}
      </Modal>
    </div>
  );
}
