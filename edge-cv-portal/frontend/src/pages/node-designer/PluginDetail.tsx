/**
 * Plugin_Record detail (custom-node-designer, Requirements 3.5, 10.2;
 * custom-node-source-lifecycle Requirements 1, 3-6, 8.3, 9.2).
 *
 * One record's latest version: lifecycle and classification badges,
 * per-arch build status with the failing build's log excerpt (stale
 * markers, "Fix with AI", retry), the Source_Editor with save / save-as-
 * new-version / rebuild, the Add-architectures action, the Plugin_Component
 * summary, the Git sync panel, version history, and provenance.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Link,
  Multiselect,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import ConfirmationModal from '../../components/ConfirmationModal';
import type { CodeAssistDiagnosticsState } from '../../components/code-assist/codeAssistState';
import { useAuth } from '../../contexts/AuthContext';
import { ApiError } from '../../services/api';
import { canManageNodeDesigner } from '../../utils/nodeDesignerAccess';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  DEEPSTREAM_ARCHITECTURES,
  DEVICE_ARCHITECTURES,
  PluginBuildsView,
  PluginRecordSummary,
  PluginVersionDetail,
  SyncOperation,
} from './types';
import { BuildStatusIndicator, ClassificationBadge, LifecycleBadge, logExcerpt } from './badges';
import { pickFileForDiagnostics } from './diagnosticsFile';
import GitSyncPanel from './GitSyncPanel';
import {
  adjustRevisionError,
  archRevisionLabel,
  canAdjustRevision,
  GIT_CONNECTIONS_ROUTE,
  importedPluginsSummary,
  importFailureGuidance,
  platformWarningMessage,
} from './importFlow';
import RegistrationPrompt from './RegistrationPrompt';
import SourceEditor from './SourceEditor';
import {
  emptyEditorState,
  fromSourceTree,
  isDirty,
  sourceEditorReducer,
  toSaveRequest,
} from './sourceEditorState';

/** Poll builds every 10 s while any requested build is still running. */
const BUILD_POLL_MS = 10_000;

/** Declared element parameters of a scaffold record (for the hook contract). */
function scaffoldParameters(
  plugin: PluginVersionDetail
): { name: string; param_type: string; description?: string }[] | undefined {
  const raw = plugin.provenance?.scaffoldDeclaration;
  if (typeof raw !== 'string') return undefined;
  try {
    const parameters = JSON.parse(raw)?.parameters;
    if (!Array.isArray(parameters)) return undefined;
    return parameters
      .filter((p: any) => p && typeof p.name === 'string')
      .map((p: any) => ({
        name: p.name,
        param_type: typeof p.paramType === 'string' ? p.paramType : 'unknown',
        ...(typeof p.description === 'string' ? { description: p.description } : {}),
      }));
  } catch {
    return undefined;
  }
}

export default function PluginDetail() {
  const navigate = useNavigate();
  const { pluginId } = useParams<{ pluginId: string }>();
  const [plugin, setPlugin] = useState<PluginVersionDetail | null>(null);
  const [versions, setVersions] = useState<PluginRecordSummary[]>([]);
  const [builds, setBuilds] = useState<PluginBuildsView | null>(null);
  const [loading, setLoading] = useState(true);
  // Source_Editor (custom-node-source-lifecycle 1): the editable tree,
  // the active tab, load truncation, save state, and the post-save
  // rebuild offer with the stale architectures the save reported.
  const { user } = useAuth();
  const location = useLocation();
  const canManage = canManageNodeDesigner(user?.role);
  const [editor, dispatchEditor] = useReducer(sourceEditorReducer, undefined, emptyEditorState);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [sourceTruncated, setSourceTruncated] = useState(false);
  const [sourceLoadError, setSourceLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState<'save' | 'new-version' | null>(null);
  const [saveError, setSaveError] = useState<{ header: string; message: string; locked?: boolean; conflict?: boolean } | null>(null);
  const [saveNotice, setSaveNotice] = useState<{ staleArchitectures: string[] } | null>(null);
  const [showLeaveModal, setShowLeaveModal] = useState(false);
  // "Fix with AI" (5.1, 5.2): the Diagnostic_Context seeded into the
  // assistant of the picked source tab.
  const [assistDiagnostics, setAssistDiagnostics] = useState<CodeAssistDiagnosticsState | null>(null);
  const editorRef = useRef<HTMLDivElement | null>(null);
  // Architecture_Addition (6): the picker of not-yet-requested registry
  // architectures and its in-flight/error state.
  const [addArchOpen, setAddArchOpen] = useState(false);
  const [addArchs, setAddArchs] = useState<string[]>([]);
  const [addSubmitting, setAddSubmitting] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Architectures with an in-flight retry request (per-arch buttons and
  // the retry-all action share this so double submission is blocked).
  const [retrying, setRetrying] = useState<string[]>([]);
  const [retryError, setRetryError] = useState<string | null>(null);
  // Manual build submission (the Build header action): a generated or
  // scaffold record accepted without a build round has no artifacts yet,
  // so the page offers an explicit build trigger with an architecture
  // selection (defaulted from the declaration / last build round).
  const [buildPanelOpen, setBuildPanelOpen] = useState(false);
  const [buildArchs, setBuildArchs] = useState<string[]>([]);
  const [buildSubmitting, setBuildSubmitting] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  // Post-import revision adjustment (incompatible platforms carrying a
  // suggestedRevision): which architecture's inline input is open, its
  // editable value, the in-flight flag, and per-arch errors surfaced
  // on the affected platform's entry only.
  const [adjustingArch, setAdjustingArch] = useState<string | null>(null);
  const [adjustValue, setAdjustValue] = useState('');
  const [adjustSubmitting, setAdjustSubmitting] = useState(false);
  const [adjustErrors, setAdjustErrors] = useState<Record<string, string>>({});
  // Record deletion (bad/duplicate imports): confirmation modal state.
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Lifecycle transitions (dev -> test -> prod and back): in-flight
  // flag plus the 409 gate rejection (missing build / missing review).
  const [transitioning, setTransitioning] = useState(false);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!pluginId) return;
    setError(null);
    try {
      const response = await nodeDesignerApi.getPlugin(pluginId);
      setPlugin(response.plugin);
      setVersions(response.versions || []);
      const [buildsView, source] = await Promise.all([
        nodeDesignerApi.getBuilds(pluginId, response.plugin.version).catch(() => null),
        nodeDesignerApi
          .getSourceTree(pluginId, response.plugin.version)
          .catch((err: any) => {
            setSourceLoadError(err?.message || 'The source tree could not be loaded');
            return null;
          }),
      ]);
      setBuilds(buildsView);
      if (source) {
        setSourceLoadError(null);
        const next = fromSourceTree(source.files, source.source_revision);
        dispatchEditor({
          type: 'reset',
          files: next.files,
          binary: next.binary,
          sourceRevision: next.sourceRevision,
        });
        setSourceTruncated(Boolean(source.truncated));
        setActiveFile((current) =>
          current && current in next.files ? current : Object.keys(next.files).sort()[0] ?? null
        );
      }
    } catch (err: any) {
      setError(err.message || 'Failed to load the plugin record');
    } finally {
      setLoading(false);
    }
  }, [pluginId]);

  useEffect(() => {
    load();
  }, [load]);

  // Keep build status fresh while builds are in flight (3.5).
  useEffect(() => {
    if (!pluginId || !plugin || !builds || builds.settled) {
      return;
    }
    const timer = setInterval(async () => {
      try {
        setBuilds(await nodeDesignerApi.getBuilds(pluginId, plugin.version));
      } catch {
        // transient poll failure: keep the last known status
      }
    }, BUILD_POLL_MS);
    return () => clearInterval(timer);
  }, [pluginId, plugin, builds]);

  const editorDirty = isDirty(editor);

  // Unsaved edits: ask before the browser unloads the page (1.11).
  useEffect(() => {
    if (!editorDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [editorDirty]);

  // A simulator failure routed here carries its Diagnostic_Context in the
  // navigation state (5.2).
  useEffect(() => {
    const seeded = (location.state as { assistDiagnostics?: CodeAssistDiagnosticsState } | null)?.assistDiagnostics;
    if (seeded) setAssistDiagnostics(seeded);
  }, [location.state]);

  const leavePage = () => {
    if (editorDirty) {
      setShowLeaveModal(true);
      return;
    }
    navigate('/node-designer');
  };

  // Save in place (dev versions): PUT the reduced request; SOURCE_LOCKED
  // and SOURCE_REVISION_CONFLICT are surfaced with their recovery paths;
  // success rebaselines the editor and offers a rebuild of the stale
  // architectures (1.4, 1.5, 1.8, 1.10, 1.12).
  const saveInPlace = async () => {
    if (!pluginId || !plugin) return;
    const request = toSaveRequest(editor);
    if (!request) return;
    setSaving('save');
    setSaveError(null);
    setSaveNotice(null);
    try {
      const response = await nodeDesignerApi.saveSource(pluginId, plugin.version, request);
      dispatchEditor({ type: 'save-succeeded', sourceRevision: response.source_revision });
      setSaveNotice({ staleArchitectures: response.stale_architectures });
      setPlugin({
        ...plugin,
        source_revision: response.source_revision,
        stale_architectures: response.stale_architectures,
      });
      const view = await nodeDesignerApi.getBuilds(pluginId, plugin.version).catch(() => null);
      if (view) setBuilds(view);
    } catch (err: any) {
      dispatchEditor({ type: 'save-failed' });
      const code = err instanceof ApiError ? err.code : undefined;
      if (code === 'SOURCE_LOCKED') {
        setSaveError({
          header: `This ${plugin.lifecycle_state} version cannot be edited in place`,
          message: 'Save your edits as a new version instead; the new version starts in dev with a pending security review.',
          locked: true,
        });
      } else if (code === 'SOURCE_REVISION_CONFLICT') {
        setSaveError({
          header: 'The source changed since you loaded it',
          message: 'Someone else saved or a pull completed. Reload to see the current source; your edits stay in the editor until you do.',
          conflict: true,
        });
      } else if (code === 'SCAFFOLD_INVALID') {
        const defects = (err.details?.defects as string[] | undefined) ?? [];
        setSaveError({
          header: 'The source does not form a buildable plugin',
          message: defects.length ? defects.join('; ') : err.message,
        });
      } else {
        setSaveError({ header: 'Save failed', message: err?.message || 'The source could not be saved' });
      }
    } finally {
      setSaving(null);
    }
  };

  // Save as new version (any lifecycle state): the tree of this version
  // plus the edits becomes latest+1 in dev (1.6).
  const saveAsNewVersion = async () => {
    if (!pluginId || !plugin) return;
    setSaving('new-version');
    setSaveError(null);
    setSaveNotice(null);
    try {
      const changed: Record<string, string> = {};
      for (const [path, content] of Object.entries(editor.files)) {
        if (!(path in editor.baseline) || editor.baseline[path] !== content) changed[path] = content;
      }
      const response = await nodeDesignerApi.createNewVersion(pluginId, plugin.version, {
        files: changed,
        delete: editor.deleted,
      });
      dispatchEditor({ type: 'save-succeeded', sourceRevision: editor.sourceRevision });
      setPlugin(response.plugin);
      await load();
    } catch (err: any) {
      const defects = err instanceof ApiError && err.code === 'SCAFFOLD_INVALID'
        ? ((err.details?.defects as string[] | undefined) ?? [])
        : [];
      setSaveError({
        header: 'New version not created',
        message: defects.length ? defects.join('; ') : err?.message || 'The new version could not be created',
      });
    } finally {
      setSaving(null);
    }
  };

  // "Fix with AI" on a failed architecture (5.1): pick the most likely
  // file, seed the assistant, and scroll the editor into view.
  const fixWithAi = (arch: string, logTail: string) => {
    const picked = pickFileForDiagnostics(logTail, Object.keys(editor.files), arch);
    if (picked) setActiveFile(picked);
    setAssistDiagnostics({ kind: 'build', architecture: arch, text: logTail });
    editorRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  };

  const onSyncSettled = useCallback(
    (_operation: SyncOperation) => {
      load();
    },
    [load]
  );

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error || !plugin) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{error || 'Plugin record not found'}</Alert>
        <Button onClick={() => navigate('/node-designer')}>Back to Node Designer</Button>
      </SpaceBetween>
    );
  }

  const classification = (plugin.provenance?.classification as string) || null;
  const buildEntries = builds?.builds || plugin.artifacts || {};
  const failedArchs = Object.keys(buildEntries)
    .filter((arch) => (buildEntries[arch] || {}).buildStatus === 'failed')
    .sort();
  // Stale_Artifacts (1.8): architectures built from an older Source_Revision.
  const staleArchs = builds?.stale_architectures ?? plugin.stale_architectures ?? [];
  const requestedArchs = builds?.requested_architectures ?? Object.keys(buildEntries);
  // The Build_Target_Registry (6.7): only architectures with a build
  // project are offered anywhere on this page.
  const registry: string[] = builds?.buildable_architectures
    ?? [...DEVICE_ARCHITECTURES].filter((arch) => arch !== 'arm64_jp7');
  const componentSummary = builds?.component ?? null;

  // Re-submit failed architectures to the Plugin_Build_Service (the
  // build endpoint re-StartBuilds any architecture list; per-arch
  // status flips back to building and the poll picks up the outcome).
  const retryBuilds = async (architectures: string[]) => {
    if (!pluginId || !plugin || architectures.length === 0) return;
    setRetrying(architectures);
    setRetryError(null);
    try {
      const view = await nodeDesignerApi.startBuilds(
        pluginId,
        plugin.version,
        architectures
      );
      setBuilds(view);
    } catch (err: any) {
      setRetryError(err?.message || 'The build retry could not be started');
    } finally {
      setRetrying([]);
    }
  };

  // Architectures the Build panel may target: the Build_Target_Registry,
  // restricted to the JetPack builds for DeepStream-flagged records (the
  // backend enforces the same rules, Requirements 5.1 and 6.7/6.8).
  const buildableArchitectures: string[] = registry.filter((arch) =>
    plugin.deepstream ? (DEEPSTREAM_ARCHITECTURES as readonly string[]).includes(arch) : true
  );
  // Architecture_Addition candidates (6.6): registry minus requested.
  const addableArchitectures = buildableArchitectures.filter((arch) => !requestedArchs.includes(arch));

  const openAddArchPanel = () => {
    setAddArchs([]);
    setAddError(null);
    setAddArchOpen(true);
  };

  const submitAddArchitectures = async () => {
    if (!pluginId || !plugin || addArchs.length === 0) return;
    setAddSubmitting(true);
    setAddError(null);
    try {
      const view = await nodeDesignerApi.addArchitectures(pluginId, plugin.version, addArchs);
      setBuilds(view);
      setAddArchOpen(false);
      if (view.source_revision !== plugin.source_revision) {
        // A scaffold gained a build configuration: reload the source tree.
        await load();
      }
    } catch (err: any) {
      const rejected = err instanceof ApiError ? (err.details?.rejected as Record<string, string> | undefined) : undefined;
      setAddError(
        rejected && Object.keys(rejected).length
          ? Object.entries(rejected).map(([arch, reason]) => `${arch}: ${reason.replace(/_/g, ' ')}`).join('; ')
          : err?.message || 'The architectures could not be added'
      );
    } finally {
      setAddSubmitting(false);
    }
  };

  // Default architecture selection for a new build round: the last
  // round's requested architectures when one exists, else the
  // Target_Architectures of the recorded scaffold declaration (create
  // wizard and generate-and-accept records both carry it in
  // provenance.scaffoldDeclaration), else x86_64.
  const defaultBuildArchitectures = (): string[] => {
    const requested = (builds?.requested_architectures || []).filter((arch) =>
      buildableArchitectures.includes(arch)
    );
    if (requested.length > 0) return requested;
    const raw = plugin.provenance?.scaffoldDeclaration;
    if (typeof raw === 'string') {
      try {
        const declared = JSON.parse(raw)?.architectures;
        if (Array.isArray(declared)) {
          const valid = declared.filter(
            (arch): arch is string =>
              typeof arch === 'string' && buildableArchitectures.includes(arch)
          );
          if (valid.length > 0) return valid;
        }
      } catch {
        // unparseable provenance: fall through to the default
      }
    }
    return buildableArchitectures.includes('x86_64') ? ['x86_64'] : [];
  };

  const openBuildPanel = () => {
    setBuildArchs(defaultBuildArchitectures());
    setBuildError(null);
    setBuildPanelOpen(true);
  };

  // Submit a build round for the selected Target_Architectures via the
  // existing build endpoint; the response's builds view is unsettled,
  // so the status poll resumes automatically.
  const startBuild = async () => {
    if (!pluginId || !plugin || buildArchs.length === 0) return;
    setBuildSubmitting(true);
    setBuildError(null);
    try {
      const view = await nodeDesignerApi.startBuilds(
        pluginId,
        plugin.version,
        buildArchs
      );
      setBuilds(view);
      setBuildPanelOpen(false);
    } catch (err: any) {
      setBuildError(err?.message || 'The build could not be started');
    } finally {
      setBuildSubmitting(false);
    }
  };

  const clearAdjustError = (arch: string) =>
    setAdjustErrors(({ [arch]: _dropped, ...rest }) => rest);

  // Open the inline adjust-revision input for one architecture,
  // pre-filled with the recorded suggestedRevision (editable).
  const openAdjust = (arch: string, suggested: string) => {
    setAdjustingArch(arch);
    setAdjustValue(suggested);
    clearAdjustError(arch);
  };

  // Apply the per-platform revision adjustment: POST .../adjust-revision
  // fetches (or reuses) the adjusted revision's tree and re-runs the
  // platform's build. The response carries the refreshed record and
  // builds view; the build poll resumes because the view is no longer
  // settled. Errors surface on the affected platform's entry only.
  const applyAdjustment = async (arch: string) => {
    if (!pluginId || !plugin) return;
    const validation = adjustRevisionError(adjustValue);
    if (validation) {
      setAdjustErrors((prev) => ({ ...prev, [arch]: validation }));
      return;
    }
    setAdjustSubmitting(true);
    clearAdjustError(arch);
    try {
      const response = await nodeDesignerApi.adjustRevision(
        pluginId,
        plugin.version,
        arch,
        adjustValue.trim()
      );
      setPlugin(response.plugin);
      setBuilds(response.builds);
      setAdjustingArch(null);
    } catch (err: any) {
      setAdjustErrors((prev) => ({
        ...prev,
        [arch]: err?.message || 'The revision adjustment could not be applied',
      }));
    } finally {
      setAdjustSubmitting(false);
    }
  };
  // Which plugins an import covers ('rtsp (1 of 74 found)' for a
  // partial selection, 'All 74 plugins' otherwise); null for
  // non-imports and unsettled fetches.
  const importedPlugins = importedPluginsSummary(
    plugin.selected_plugins,
    plugin.plugins_found?.length
  );

  // Promote (dev -> test -> prod) or demote (prod -> test -> dev) the
  // version. Gate rejections (missing successful build for dev -> test,
  // missing approved security review for test -> prod) come back as 409
  // and surface in the lifecycle alert.
  const changeLifecycle = async (direction: 'promote' | 'demote') => {
    if (!pluginId || !plugin) return;
    setTransitioning(true);
    setLifecycleError(null);
    try {
      const response =
        direction === 'promote'
          ? await nodeDesignerApi.promoteVersion(pluginId, plugin.version)
          : await nodeDesignerApi.demoteVersion(pluginId, plugin.version);
      setPlugin(response.plugin);
    } catch (err: any) {
      setLifecycleError(err?.message || 'The lifecycle transition failed');
    } finally {
      setTransitioning(false);
    }
  };

  // Delete the record (every version plus its source snapshot and
  // built artifacts). Failures (e.g. 409 RECORD_IN_USE for versions
  // promoted beyond dev) surface as an error alert on the page.
  const confirmDelete = async () => {
    if (!pluginId) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await nodeDesignerApi.deletePlugin(pluginId);
      setShowDeleteModal(false);
      navigate('/node-designer');
    } catch (err: any) {
      setShowDeleteModal(false);
      setDeleteError(err?.message || 'The plugin record could not be deleted');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={plugin.description || undefined}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" ariaLabel="Refresh" onClick={load} />
            <Button onClick={leavePage}>Back to library</Button>
            {canManage && plugin.lifecycle_state === 'dev' && (
              <Button
                loading={saving === 'save'}
                disabled={!editorDirty || saving !== null}
                disabledReason={!editorDirty ? 'No unsaved changes.' : undefined}
                onClick={saveInPlace}
              >
                Save
              </Button>
            )}
            {canManage && (
              <Button
                variant={plugin.lifecycle_state === 'dev' ? 'normal' : 'primary'}
                loading={saving === 'new-version'}
                disabled={saving !== null}
                onClick={saveAsNewVersion}
              >
                Save as new version
              </Button>
            )}
            {(plugin.lifecycle_state === 'test' ||
              plugin.lifecycle_state === 'prod') && (
              <Button
                loading={transitioning}
                onClick={() => changeLifecycle('demote')}
              >
                {plugin.lifecycle_state === 'prod'
                  ? 'Demote to test'
                  : 'Demote to dev'}
              </Button>
            )}
            {(plugin.lifecycle_state === 'dev' ||
              plugin.lifecycle_state === 'test') && (
              <Button
                variant="primary"
                loading={transitioning}
                onClick={() => changeLifecycle('promote')}
              >
                {plugin.lifecycle_state === 'dev'
                  ? 'Promote to test'
                  : 'Promote to prod'}
              </Button>
            )}
            <Button
              ariaLabel={`Delete ${plugin.name}`}
              onClick={() => setShowDeleteModal(true)}
            >
              Delete
            </Button>
          </SpaceBetween>
        }
      >
        {plugin.name}
      </Header>

      {deleteError && (
        <Alert
          type="error"
          header="Delete failed"
          dismissible
          onDismiss={() => setDeleteError(null)}
        >
          {deleteError}
        </Alert>
      )}

      {lifecycleError && (
        <Alert
          type="error"
          header="Lifecycle transition rejected"
          dismissible
          onDismiss={() => setLifecycleError(null)}
        >
          {lifecycleError}
        </Alert>
      )}

      {saveError && (
        <Alert
          type={saveError.locked ? 'warning' : 'error'}
          header={saveError.header}
          dismissible
          onDismiss={() => setSaveError(null)}
          action={
            saveError.locked ? (
              <Button loading={saving === 'new-version'} onClick={saveAsNewVersion}>
                Save as new version
              </Button>
            ) : saveError.conflict ? (
              <Button onClick={load}>Reload</Button>
            ) : undefined
          }
        >
          {saveError.message}
        </Alert>
      )}

      {saveNotice && (
        <Alert
          type="success"
          header="Source saved"
          dismissible
          onDismiss={() => setSaveNotice(null)}
          action={
            saveNotice.staleArchitectures.length > 0 || failedArchs.length > 0 ? (
              <Button
                loading={retrying.length > 0}
                onClick={() =>
                  retryBuilds([...new Set([...saveNotice.staleArchitectures, ...failedArchs])].sort())
                }
              >
                Rebuild
              </Button>
            ) : undefined
          }
        >
          {saveNotice.staleArchitectures.length > 0
            ? `${saveNotice.staleArchitectures.length} ${
                saveNotice.staleArchitectures.length === 1 ? 'architecture needs' : 'architectures need'
              } a rebuild: ${saveNotice.staleArchitectures.join(', ')}.`
            : 'No built artifacts are affected.'}
        </Alert>
      )}

      <ConfirmationModal
        visible={showLeaveModal}
        title="Unsaved changes"
        message="You have unsaved source edits. Leave this page and discard them?"
        confirmButtonText="Discard and leave"
        variant="warning"
        onConfirm={() => {
          setShowLeaveModal(false);
          navigate('/node-designer');
        }}
        onCancel={() => setShowLeaveModal(false)}
      />

      <ConfirmationModal
        visible={showDeleteModal}
        title={`Delete ${plugin.name}`}
        message={
          `Delete ${plugin.name}? This removes the record, its source ` +
          'snapshot, and built artifacts. This cannot be undone.'
        }
        confirmButtonText="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setShowDeleteModal(false)}
      />

      {/* Asynchronous import status: the repository fetch is still
          running (refresh to update), or it failed with a finding. */}
      {plugin.import_status === 'fetching' && (
        <Alert type="info" header="Import in progress">
          <StatusIndicator type="in-progress">
            Cloning repository… refresh to update.
          </StatusIndicator>
        </Alert>
      )}
      {plugin.import_status === 'failed' && plugin.import_finding && (
        <Alert
          type="error"
          header={importFailureGuidance(plugin.import_finding_category).header}
        >
          <SpaceBetween size="xs">
            {/* Git_Connection fetch failures carry a Failure_Category
                (private-repo-plugin-import 3.1-3.3): say what to fix,
                and for a rejected token where to re-verify. */}
            {importFailureGuidance(plugin.import_finding_category).guidance && (
              <div>
                {importFailureGuidance(plugin.import_finding_category).guidance}
                {importFailureGuidance(plugin.import_finding_category).linkGitConnections && (
                  <>
                    {' '}
                    <Link
                      href={GIT_CONNECTIONS_ROUTE}
                      onFollow={(event) => {
                        event.preventDefault();
                        navigate(GIT_CONNECTIONS_ROUTE);
                      }}
                    >
                      Open Git connections
                    </Link>
                  </>
                )}
              </div>
            )}
            <div>{plugin.import_finding}</div>
          </SpaceBetween>
        </Alert>
      )}

      <RegistrationPrompt
        pluginId={plugin.plugin_id}
        version={plugin.version}
        artifacts={buildEntries}
      />

      <Container header={<Header variant="h2">Overview</Header>}>
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Version</Box>
            <div>v{plugin.version}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Lifecycle state</Box>
            <LifecycleBadge state={plugin.lifecycle_state} />
          </div>
          <div>
            <Box variant="awsui-key-label">Security review</Box>
            <div>{plugin.review?.decision || 'pending'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Classification</Box>
            <ClassificationBadge classification={classification} />
          </div>
          <div>
            <Box variant="awsui-key-label">Origin</Box>
            <div>{plugin.kind}</div>
          </div>
          {importedPlugins && (
            <div>
              <Box variant="awsui-key-label">Imported plugins</Box>
              <div>{importedPlugins}</div>
            </div>
          )}
          <div>
            <Box variant="awsui-key-label">Created by</Box>
            <div>{plugin.created_by}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Created</Box>
            <div>{plugin.created_at ? new Date(plugin.created_at).toLocaleString() : '—'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Updated</Box>
            <div>{plugin.updated_at ? new Date(plugin.updated_at).toLocaleString() : '—'}</div>
          </div>
        </ColumnLayout>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {failedArchs.length > 1 && (
                  <Button
                    disabled={retrying.length > 0}
                    onClick={() => retryBuilds(failedArchs)}
                  >
                    Retry failed builds
                  </Button>
                )}
                {canManage && addableArchitectures.length > 0 && (
                  <Button
                    disabled={addArchOpen || retrying.length > 0 || plugin.lifecycle_state === 'prod'}
                    disabledReason={
                      plugin.lifecycle_state === 'prod'
                        ? 'Architectures cannot be added to a prod version; create a new version first.'
                        : undefined
                    }
                    onClick={openAddArchPanel}
                  >
                    Add architectures
                  </Button>
                )}
                <Button
                  variant="primary"
                  disabled={buildPanelOpen || retrying.length > 0}
                  onClick={openBuildPanel}
                >
                  Build
                </Button>
              </SpaceBetween>
            }
            description={
              componentSummary?.version
                ? `Deployable component v${componentSummary.version} (${componentSummary.status ?? 'unknown'}): ${
                    componentSummary.architectures.length
                      ? componentSummary.architectures.join(', ')
                      : 'no architectures'
                  }`
                : undefined
            }
          >
            Builds
          </Header>
        }
      >
        <SpaceBetween size="m">
        {addArchOpen && (
          <SpaceBetween size="s">
            {addError && (
              <Alert type="error" dismissible onDismiss={() => setAddError(null)}>
                {addError}
              </Alert>
            )}
            <FormField
              label="Architectures to add"
              description="Only architectures not yet requested for this version and available as build targets are listed. Builds start for the added architectures only."
            >
              <Multiselect
                selectedOptions={addArchs.map((arch) => ({
                  label: ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ?? arch,
                  value: arch,
                }))}
                options={addableArchitectures.map((arch) => ({
                  label: ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ?? arch,
                  value: arch,
                }))}
                onChange={({ detail }) =>
                  setAddArchs(
                    detail.selectedOptions
                      .map((option) => option.value)
                      .filter((value): value is string => Boolean(value))
                  )
                }
                placeholder="Select architectures to add"
                disabled={addSubmitting}
                ariaLabel="Architectures to add"
              />
            </FormField>
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="primary"
                loading={addSubmitting}
                disabled={addArchs.length === 0}
                onClick={submitAddArchitectures}
              >
                Add and build
              </Button>
              <Button disabled={addSubmitting} onClick={() => setAddArchOpen(false)}>
                Cancel
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        )}
        {buildPanelOpen && (
          <SpaceBetween size="s">
            {buildError && (
              <Alert type="error" dismissible onDismiss={() => setBuildError(null)}>
                {buildError}
              </Alert>
            )}
            <FormField
              label="Target architectures"
              description="One build is submitted per selected Target_Architecture."
            >
              <Multiselect
                selectedOptions={buildArchs.map((arch) => ({
                  label:
                    ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ??
                    arch,
                  value: arch,
                }))}
                options={buildableArchitectures.map((arch) => ({
                  label:
                    ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ??
                    arch,
                  value: arch,
                }))}
                onChange={({ detail }) =>
                  setBuildArchs(
                    detail.selectedOptions
                      .map((option) => option.value)
                      .filter((value): value is string => Boolean(value))
                  )
                }
                placeholder="Select target architectures"
                disabled={buildSubmitting}
              />
            </FormField>
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="primary"
                loading={buildSubmitting}
                disabled={buildArchs.length === 0}
                onClick={startBuild}
              >
                Start build
              </Button>
              <Button
                disabled={buildSubmitting}
                onClick={() => setBuildPanelOpen(false)}
              >
                Cancel
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        )}
        {retryError && (
          <Alert type="error" dismissible onDismiss={() => setRetryError(null)}>
            {retryError}
          </Alert>
        )}
        {Object.keys(buildEntries).length === 0 ? (
          <Box color="text-status-inactive">No builds submitted for this version yet.</Box>
        ) : (
          <SpaceBetween size="m">
            {Object.keys(buildEntries)
              .sort()
              .map((arch) => {
                const entry = buildEntries[arch] || {};
                const excerpt = logExcerpt(entry.logTail);
                // Advisory platform requirements check recorded at
                // import time: warn (never block) when the source's
                // GStreamer requirement exceeds what this platform's
                // build image ships, with the working revision to
                // import instead when one is known.
                const compat = plugin.platform_compatibility?.[arch];
                // Multi-revision imports pin architectures to their
                // own source revision (arch_revisions -> fetches):
                // show which revision this architecture builds from.
                const archRevision = archRevisionLabel(plugin, arch);
                return (
                  <SpaceBetween size="xs" key={arch}>
                    <SpaceBetween direction="horizontal" size="xs">
                      <BuildStatusIndicator
                        arch={arch}
                        status={entry.buildStatus}
                        logTail={entry.logTail}
                      />
                      {archRevision && (
                        <Box
                          variant="span"
                          color="text-body-secondary"
                          fontSize="body-s"
                        >
                          revision {archRevision}
                        </Box>
                      )}
                      {staleArchs.includes(arch) && (
                        <Badge color="severity-medium">Rebuild required</Badge>
                      )}
                      {entry.buildStatus === 'failed' && (
                        <Button
                          variant="inline-link"
                          loading={retrying.includes(arch)}
                          disabled={retrying.length > 0 && !retrying.includes(arch)}
                          onClick={() => retryBuilds([arch])}
                        >
                          Retry build
                        </Button>
                      )}
                      {entry.buildStatus === 'failed' && canManage && entry.logTail && (
                        <Button
                          variant="inline-link"
                          ariaLabel={`Fix ${arch} build with AI`}
                          onClick={() => fixWithAi(arch, entry.logTail || '')}
                        >
                          Fix with AI
                        </Button>
                      )}
                    </SpaceBetween>
                    {compat && compat.compatible === false && (
                      <Box padding={{ left: 'l' }}>
                        <SpaceBetween size="xs">
                          <StatusIndicator type="warning">
                            {platformWarningMessage(arch, compat)}
                          </StatusIndicator>
                          {canAdjustRevision(plugin, arch) &&
                            (adjustingArch === arch ? (
                              <SpaceBetween direction="horizontal" size="xs">
                                <Input
                                  value={adjustValue}
                                  onChange={({ detail }) =>
                                    setAdjustValue(detail.value)
                                  }
                                  ariaLabel={`Revision for ${arch}`}
                                  disabled={adjustSubmitting}
                                />
                                <Button
                                  variant="primary"
                                  loading={adjustSubmitting}
                                  disabled={retrying.length > 0}
                                  onClick={() => applyAdjustment(arch)}
                                >
                                  Apply
                                </Button>
                                <Button
                                  disabled={adjustSubmitting}
                                  onClick={() => setAdjustingArch(null)}
                                >
                                  Cancel
                                </Button>
                              </SpaceBetween>
                            ) : (
                              <Button
                                variant="inline-link"
                                disabled={
                                  retrying.length > 0 || adjustSubmitting
                                }
                                onClick={() =>
                                  openAdjust(
                                    arch,
                                    compat.suggestedRevision || ''
                                  )
                                }
                              >
                                Adjust revision for this platform
                              </Button>
                            ))}
                          {adjustErrors[arch] && (
                            <Alert
                              type="error"
                              dismissible
                              onDismiss={() => clearAdjustError(arch)}
                            >
                              {adjustErrors[arch]}
                            </Alert>
                          )}
                        </SpaceBetween>
                      </Box>
                    )}
                    {entry.buildStatus === 'failed' && excerpt && (
                      <Box padding={{ left: 'l' }}>
                        <pre
                          style={{
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            margin: 0,
                            fontSize: '12px',
                            background: '#f2f3f3',
                            padding: '8px',
                            borderRadius: '4px',
                            maxHeight: '200px',
                            overflow: 'auto',
                          }}
                        >
                          {excerpt}
                        </pre>
                      </Box>
                    )}
                  </SpaceBetween>
                );
              })}
          </SpaceBetween>
        )}
        </SpaceBetween>
      </Container>

      <div ref={editorRef}>
        <Container
          header={
            <Header
              variant="h2"
              counter={`(${Object.keys(editor.files).length + editor.binary.length})`}
              description={
                canManage
                  ? plugin.lifecycle_state === 'dev'
                    ? `Source revision ${editor.sourceRevision}. Edits are saved in place on this dev version.`
                    : `Source revision ${editor.sourceRevision}. This ${plugin.lifecycle_state} version is locked; edits are saved as a new version.`
                  : `Source revision ${editor.sourceRevision}. Read-only.`
              }
            >
              Source
            </Header>
          }
        >
          {sourceLoadError ? (
            <Alert type="error">{sourceLoadError}</Alert>
          ) : (
            <SourceEditor
              state={editor}
              dispatch={dispatchEditor}
              activeFile={activeFile}
              onActiveFileChange={setActiveFile}
              readOnly={!canManage}
              truncated={sourceTruncated}
              assist={
                canManage
                  ? {
                      usecaseId: plugin.usecase_id,
                      kind: plugin.kind,
                      parameters: scaffoldParameters(plugin),
                      diagnostics: assistDiagnostics,
                    }
                  : null
              }
            />
          )}
        </Container>
      </div>

      <Container header={<Header variant="h2">Git repository</Header>}>
        <GitSyncPanel
          plugin={plugin}
          editorDirty={editorDirty}
          readOnly={!canManage}
          onSettled={onSyncSettled}
        />
      </Container>

      <Table<PluginRecordSummary>
        items={versions}
        trackBy={(item) => `${item.plugin_id}:${item.version}`}
        columnDefinitions={[
          { id: 'version', header: 'Version', cell: (item) => `v${item.version}` },
          {
            id: 'lifecycle',
            header: 'Lifecycle',
            cell: (item) => <LifecycleBadge state={item.lifecycle_state} />,
          },
          { id: 'review', header: 'Security review', cell: (item) => item.review_decision },
          {
            id: 'updated',
            header: 'Updated',
            cell: (item) =>
              item.updated_at ? new Date(item.updated_at).toLocaleString() : '—',
          },
        ]}
        header={<Header variant="h2">Version history</Header>}
        empty={<Box textAlign="center">No versions</Box>}
      />
    </SpaceBetween>
  );
}
