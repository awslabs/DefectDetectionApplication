/**
 * GitSyncPanel (custom-node-source-lifecycle, Requirements 2.6, 2.8, 3.1,
 * 3.2, 3.7, 3.8, 3.12, 3.13, 4.1, 4.2, 4.10, 4.11, 10.5).
 *
 * The Detail_Page's Git section for one Plugin_Version:
 * - unlinked: choose a verified Git_Connection of the Use_Case, branch,
 *   and Repository_Path, then link;
 * - linked: connection summary, last sync, Push (message; `force`
 *   revealed after a `diverged` failure; blocked while the editor is
 *   dirty), Pull (ref, mode defaulting per lifecycle with `in_place`
 *   disabled outside `dev`), Unlink, and the Sync_Operation history with
 *   plain-language failure explanations;
 * - disconnected: the linked connection no longer exists.
 *
 * Polls in-flight operations every 4 s until they settle and tells the
 * owner (`onSettled`) so the Detail_Page can refresh the record.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  ColumnLayout,
  FormField,
  Header,
  Input,
  Link,
  Popover,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import ConfirmationModal from '../../components/ConfirmationModal';
import { nodeDesignerApi } from './api';
import { normalizeSourcePath } from './sourcePath';
import { describeSyncFailure } from './syncFailures';
import type {
  GitConnection,
  GitLink,
  PluginVersionDetail,
  PullMode,
  SyncOperation,
} from './types';

/** Poll in-flight operations every 4 s. */
export const SYNC_POLL_MS = 4_000;

export interface GitSyncPanelProps {
  plugin: PluginVersionDetail;
  /** The editor holds unsaved edits: Push asks to save or discard first (3.13). */
  editorDirty: boolean;
  /** Read-only roles see the state, never the actions (9.2). */
  readOnly: boolean;
  /** Called when a Sync_Operation settles so the owner can reload the record. */
  onSettled: (operation: SyncOperation) => void;
}

function shortSha(sha?: string | null): string {
  return sha ? sha.slice(0, 10) : '—';
}

/** Commit URL for the two supported providers; null for unknown hosts. */
export function commitUrl(repoUrl: string | undefined, sha: string | undefined): string | null {
  if (!repoUrl || !sha) return null;
  const base = repoUrl.replace(/\.git$/, '').replace(/\/$/, '');
  if (/github\.com/i.test(base)) return `${base}/commit/${sha}`;
  if (/gitlab/i.test(base)) return `${base}/-/commit/${sha}`;
  return null;
}

function statusType(status: SyncOperation['status']): 'success' | 'error' | 'in-progress' | 'pending' {
  switch (status) {
    case 'succeeded':
      return 'success';
    case 'failed':
      return 'error';
    case 'running':
      return 'in-progress';
    default:
      return 'pending';
  }
}

export default function GitSyncPanel({ plugin, editorDirty, readOnly, onSettled }: GitSyncPanelProps) {
  const link: GitLink | null | undefined = plugin.git;
  const isDev = plugin.lifecycle_state === 'dev';

  const [connections, setConnections] = useState<GitConnection[]>([]);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [operations, setOperations] = useState<SyncOperation[]>([]);

  // Link form
  const [selectedConnection, setSelectedConnection] = useState<SelectProps.Option | null>(null);
  const [branch, setBranch] = useState('');
  const [path, setPath] = useState('');
  const [linking, setLinking] = useState(false);
  const [linkError, setLinkError] = useState<string | null>(null);

  // Push / pull
  const [message, setMessage] = useState('');
  const [force, setForce] = useState(false);
  const [pullRef, setPullRef] = useState('');
  const [pullMode, setPullMode] = useState<PullMode>(isDev ? 'in_place' : 'new_version');
  const [busy, setBusy] = useState<'push' | 'pull' | 'unlink' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showDirtyModal, setShowDirtyModal] = useState(false);
  const [showUnlinkModal, setShowUnlinkModal] = useState(false);

  const connection = useMemo(
    () => connections.find((c) => c.connection_id === link?.connection_id) ?? null,
    [connections, link]
  );
  const linkedButMissing = Boolean(link) && connections.length >= 0 && !connection && !connectionsError;
  const inFlight = operations.find((o) => o.status === 'queued' || o.status === 'running') ?? null;
  const lastFailure = operations.find((o) => o.status === 'failed') ?? null;
  const divergedRecently = lastFailure?.kind === 'push' && lastFailure.failure?.category === 'diverged';

  const loadConnections = useCallback(async () => {
    try {
      const response = await nodeDesignerApi.listGitConnections(plugin.usecase_id);
      setConnections(response.connections);
      setConnectionsError(null);
    } catch (err: any) {
      setConnectionsError(err?.message || 'Git connections could not be loaded');
    }
  }, [plugin.usecase_id]);

  const loadOperations = useCallback(async () => {
    try {
      const response = await nodeDesignerApi.listSyncOperations(plugin.plugin_id, plugin.version);
      setOperations(response.operations);
    } catch {
      // history is best-effort
    }
  }, [plugin.plugin_id, plugin.version]);

  useEffect(() => {
    loadConnections();
    loadOperations();
  }, [loadConnections, loadOperations]);

  useEffect(() => {
    setPullMode(isDev ? 'in_place' : 'new_version');
  }, [isDev]);

  // Poll the in-flight operation until it settles (4.10).
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(async () => {
      try {
        const { operation } = await nodeDesignerApi.getSyncOperation(inFlight.operation_id);
        if (operation.status === 'succeeded' || operation.status === 'failed') {
          setOperations((prev) => prev.map((o) => (o.operation_id === operation.operation_id ? operation : o)));
          onSettled(operation);
        }
      } catch {
        // transient poll failure: keep the last known status
      }
    }, SYNC_POLL_MS);
    return () => clearInterval(timer);
  }, [inFlight, onSettled]);

  const verifiedOptions: SelectProps.Option[] = connections
    .filter((c) => c.status === 'verified')
    .map((c) => ({ label: c.name, value: c.connection_id, description: c.repo_url }));

  const linkPathError = path.trim() && normalizeSourcePath(path) === null
    ? 'Use a relative repository path without ".." segments.'
    : null;

  const submitLink = async () => {
    if (!selectedConnection?.value || linkPathError) return;
    setLinking(true);
    setLinkError(null);
    try {
      await nodeDesignerApi.setGitLink(plugin.plugin_id, plugin.version, {
        connection_id: selectedConnection.value,
        ...(branch.trim() ? { branch: branch.trim() } : {}),
        ...(path.trim() ? { path: path.trim() } : {}),
      });
      onSettled({ kind: 'verify', status: 'succeeded' } as SyncOperation);
    } catch (err: any) {
      setLinkError(err?.message || 'The Git link could not be saved');
    } finally {
      setLinking(false);
    }
  };

  const startPush = async () => {
    setBusy('push');
    setActionError(null);
    try {
      const { operation } = await nodeDesignerApi.pushGit(plugin.plugin_id, plugin.version, {
        ...(message.trim() ? { message: message.trim() } : {}),
        ...(force ? { force: true } : {}),
      });
      setOperations((prev) => [operation, ...prev]);
      setForce(false);
      if (operation.status === 'failed') onSettled(operation);
    } catch (err: any) {
      setActionError(err?.message || 'The push could not be started');
    } finally {
      setBusy(null);
    }
  };

  const requestPush = () => {
    if (editorDirty) {
      setShowDirtyModal(true);
      return;
    }
    startPush();
  };

  const startPull = async () => {
    setBusy('pull');
    setActionError(null);
    try {
      const { operation } = await nodeDesignerApi.pullGit(plugin.plugin_id, plugin.version, {
        mode: pullMode,
        ...(pullRef.trim() ? { ref: pullRef.trim() } : {}),
      });
      setOperations((prev) => [operation, ...prev]);
      if (operation.status === 'failed') onSettled(operation);
    } catch (err: any) {
      setActionError(err?.message || 'The pull could not be started');
    } finally {
      setBusy(null);
    }
  };

  const unlink = async () => {
    setBusy('unlink');
    setActionError(null);
    try {
      await nodeDesignerApi.removeGitLink(plugin.plugin_id, plugin.version);
      setShowUnlinkModal(false);
      onSettled({ kind: 'verify', status: 'succeeded' } as SyncOperation);
    } catch (err: any) {
      setActionError(err?.message || 'The Git link could not be removed');
    } finally {
      setBusy(null);
    }
  };

  const lastSync = link?.last_sync;
  const lastSyncUrl = commitUrl(connection?.repo_url, lastSync?.commit);

  return (
    <SpaceBetween size="m">
      {connectionsError && <Alert type="error">{connectionsError}</Alert>}
      {actionError && (
        <Alert type="error" dismissible onDismiss={() => setActionError(null)}>
          {actionError}
        </Alert>
      )}

      {!link && (
        <SpaceBetween size="s">
          <Box color="text-body-secondary">
            Link this version to a repository to push its source or pull changes back.
          </Box>
          {!readOnly && (
            <SpaceBetween size="s">
              {linkError && (
                <Alert type="error" dismissible onDismiss={() => setLinkError(null)}>
                  {linkError}
                </Alert>
              )}
              <FormField
                label="Git connection"
                description="Only verified connections of this use case can be linked."
              >
                <Select
                  selectedOption={selectedConnection}
                  options={verifiedOptions}
                  onChange={({ detail }) => setSelectedConnection(detail.selectedOption)}
                  placeholder="Select a verified connection"
                  empty="No verified Git connections — create one under Node Designer › Git connections."
                  ariaLabel="Git connection"
                />
              </FormField>
              <ColumnLayout columns={2}>
                <FormField label="Branch" description="Defaults to the connection's default branch.">
                  <Input value={branch} onChange={({ detail }) => setBranch(detail.value)} ariaLabel="Branch" />
                </FormField>
                <FormField
                  label="Repository path"
                  description="Directory mirroring the plugin source; defaults to the plugin name."
                  errorText={linkPathError ?? undefined}
                >
                  <Input value={path} onChange={({ detail }) => setPath(detail.value)} ariaLabel="Repository path" />
                </FormField>
              </ColumnLayout>
              <Button
                variant="primary"
                loading={linking}
                disabled={!selectedConnection?.value || Boolean(linkPathError)}
                onClick={submitLink}
              >
                Link repository
              </Button>
            </SpaceBetween>
          )}
        </SpaceBetween>
      )}

      {link && (
        <SpaceBetween size="m">
          {linkedButMissing && (
            <Alert type="warning" header="Disconnected">
              The linked Git connection no longer exists. Push and pull are unavailable until this
              version is linked to another connection.
            </Alert>
          )}
          {connection && connection.status !== 'verified' && (
            <Alert type="warning" header="Connection not verified">
              {`The connection "${connection.name}" is ${connection.status}; push and pull are unavailable until it verifies.`}
            </Alert>
          )}
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Connection</Box>
              <div>{connection?.name ?? link.connection_id}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Branch</Box>
              <div>{link.branch}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Repository path</Box>
              <div>{link.path}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Last sync</Box>
              {lastSync ? (
                <div>
                  {lastSync.kind}{' '}
                  {lastSyncUrl ? (
                    <Link external href={lastSyncUrl}>
                      {shortSha(lastSync.commit)}
                    </Link>
                  ) : (
                    <code>{shortSha(lastSync.commit)}</code>
                  )}{' '}
                  by {lastSync.by} · {new Date(lastSync.at).toLocaleString()}
                </div>
              ) : (
                <div>never</div>
              )}
            </div>
          </ColumnLayout>

          {!readOnly && !linkedButMissing && (
            <ColumnLayout columns={2}>
              <SpaceBetween size="xs">
                <Header variant="h3">Push</Header>
                <FormField label="Commit message" description="Optional; a default message names the plugin, version, and revision.">
                  <Input value={message} onChange={({ detail }) => setMessage(detail.value)} ariaLabel="Commit message" />
                </FormField>
                {divergedRecently && (
                  <Checkbox checked={force} onChange={({ detail }) => setForce(detail.checked)}>
                    Overwrite repository changes under {link.path}
                  </Checkbox>
                )}
                <Button
                  variant="primary"
                  loading={busy === 'push'}
                  disabled={Boolean(inFlight) || busy !== null || connection?.status !== 'verified'}
                  onClick={requestPush}
                >
                  Push
                </Button>
              </SpaceBetween>
              <SpaceBetween size="xs">
                <Header variant="h3">Pull</Header>
                <FormField label="Ref" description={`Branch, tag, or commit; defaults to ${link.branch}.`}>
                  <Input value={pullRef} onChange={({ detail }) => setPullRef(detail.value)} ariaLabel="Ref" />
                </FormField>
                <RadioGroup
                  value={pullMode}
                  onChange={({ detail }) => setPullMode(detail.value as PullMode)}
                  ariaLabel="Pull mode"
                  items={[
                    {
                      value: 'in_place',
                      label: 'Replace this version’s source',
                      description: isDev
                        ? 'Existing builds become stale.'
                        : 'Only dev versions can be replaced in place.',
                      disabled: !isDev,
                    },
                    {
                      value: 'new_version',
                      label: 'Create a new version',
                      description: 'The pulled tree becomes a new dev version.',
                    },
                  ]}
                />
                <Button
                  loading={busy === 'pull'}
                  disabled={Boolean(inFlight) || busy !== null || connection?.status !== 'verified'}
                  onClick={startPull}
                >
                  Pull
                </Button>
              </SpaceBetween>
            </ColumnLayout>
          )}

          {!readOnly && (
            <Button
              variant="inline-link"
              disabled={Boolean(inFlight) || busy !== null}
              onClick={() => setShowUnlinkModal(true)}
            >
              Unlink repository
            </Button>
          )}

          <Table<SyncOperation>
            items={operations}
            trackBy="operation_id"
            variant="embedded"
            header={<Header variant="h3" counter={`(${operations.length})`}>Sync operations</Header>}
            empty={<Box textAlign="center" color="text-status-inactive">No sync operations yet.</Box>}
            columnDefinitions={[
              { id: 'kind', header: 'Kind', cell: (op) => op.kind },
              {
                id: 'status',
                header: 'Status',
                cell: (op) => (
                  <SpaceBetween direction="horizontal" size="xs">
                    <StatusIndicator type={statusType(op.status)}>{op.status}</StatusIndicator>
                    {op.failure && (
                      <Popover
                        size="large"
                        header={describeSyncFailure(op.failure.category, op.failure, op.target?.path).header}
                        content={
                          <SpaceBetween size="xs">
                            <div>{describeSyncFailure(op.failure.category, op.failure, op.target?.path).message}</div>
                            {describeSyncFailure(op.failure.category, op.failure, op.target?.path).action && (
                              <Box color="text-body-secondary">
                                {describeSyncFailure(op.failure.category, op.failure, op.target?.path).action}
                              </Box>
                            )}
                            {op.failure.log_excerpt && (
                              <pre style={{ whiteSpace: 'pre-wrap', fontSize: '12px', margin: 0 }}>
                                {op.failure.log_excerpt}
                              </pre>
                            )}
                          </SpaceBetween>
                        }
                      >
                        <Badge color="red">{op.failure.category}</Badge>
                      </Popover>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: 'commit',
                header: 'Commit',
                cell: (op) => {
                  const sha = op.result?.commit;
                  const url = commitUrl(connection?.repo_url, sha);
                  if (!sha) return '—';
                  return url ? (
                    <Link external href={url}>
                      {shortSha(sha)}
                    </Link>
                  ) : (
                    <code>{shortSha(sha)}</code>
                  );
                },
              },
              {
                id: 'detail',
                header: 'Detail',
                cell: (op) =>
                  op.result?.no_changes
                    ? 'no changes'
                    : op.result?.version && op.kind === 'pull'
                      ? `v${op.result.version} (${op.result.mode === 'new_version' ? 'new version' : 'in place'})`
                      : op.target?.force
                        ? 'overwrite'
                        : '—',
              },
              { id: 'by', header: 'By', cell: (op) => op.started_by },
              {
                id: 'started',
                header: 'Started',
                cell: (op) => (op.started_at ? new Date(op.started_at).toLocaleString() : '—'),
              },
            ]}
          />
        </SpaceBetween>
      )}

      <ConfirmationModal
        visible={showDirtyModal}
        title="Unsaved changes"
        message="Save or discard your source edits before pushing; a push sends the saved source tree, not the editor contents."
        confirmButtonText="OK"
        variant="info"
        onConfirm={() => setShowDirtyModal(false)}
        onCancel={() => setShowDirtyModal(false)}
      />
      <ConfirmationModal
        visible={showUnlinkModal}
        title="Unlink repository"
        message="Remove the Git link from this version? Recorded sync history is kept; push and pull become unavailable until it is linked again."
        confirmButtonText="Unlink"
        variant="warning"
        loading={busy === 'unlink'}
        onConfirm={unlink}
        onCancel={() => setShowUnlinkModal(false)}
      />
    </SpaceBetween>
  );
}
