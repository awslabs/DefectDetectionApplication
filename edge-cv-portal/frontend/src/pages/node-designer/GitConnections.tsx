/**
 * Git connections page (custom-node-source-lifecycle task 12.1,
 * Requirements 2.1, 2.2, 2.5, 2.7, 2.8, 2.9).
 *
 * Use_Case-scoped table of Git_Connections — name, provider, repository
 * URL, default branch, and verification status with a plain-language
 * failure explanation — plus create/edit (token in a password field that
 * is never pre-filled; blank on edit keeps the stored token), Verify, and
 * Delete behind a confirmation. Mutations are limited to UseCaseAdmins and
 * PortalAdmins (`canManageNodeDesigner`); every role holding read access
 * sees the table. While any connection is `verifying`, the list is polled
 * until it settles. The token is sent exactly once, on create or on a
 * token change, and is never part of any response this page renders.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  FormField,
  Header,
  Input,
  Link,
  Modal,
  Popover,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import ConfirmationModal from '../../components/ConfirmationModal';
import { useAuth } from '../../contexts/AuthContext';
import { useUsecase } from '../../contexts/UsecaseContext';
import { apiService } from '../../services/api';
import { UseCase } from '../../types';
import { canManageNodeDesigner } from '../../utils/nodeDesignerAccess';
import { nodeDesignerApi } from './api';
import { describeSyncFailure } from './syncFailures';
import type { GitConnection, GitProvider, UpdateGitConnectionRequest } from './types';

/** Poll the list every 4 s while a connection is verifying (2.5). */
export const CONNECTIONS_POLL_MS = 4_000;

const PROVIDER_LABELS: Record<GitProvider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

/**
 * Client-side mirror of the backend URL rule (2.2): a syntactically valid
 * URL using the https scheme. Returns the error text or null.
 */
export function repoUrlError(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return 'Enter the repository URL.';
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return 'Enter a valid URL, for example https://github.com/org/repo.git.';
  }
  if (parsed.protocol !== 'https:') return 'The repository URL must use https.';
  if (!parsed.hostname) return 'Enter a valid URL, for example https://github.com/org/repo.git.';
  return null;
}

interface ConnectionForm {
  name: string;
  provider: GitProvider;
  repo_url: string;
  default_branch: string;
  token: string;
}

const EMPTY_FORM: ConnectionForm = {
  name: '',
  provider: 'github',
  repo_url: '',
  default_branch: 'main',
  token: '',
};

function formFor(connection: GitConnection): ConnectionForm {
  return {
    name: connection.name,
    provider: connection.provider,
    repo_url: connection.repo_url,
    default_branch: connection.default_branch,
    // The token is never pre-filled (2.4); blank keeps the stored one.
    token: '',
  };
}

/** The PUT body: only the fields that changed, token only when entered. */
export function changedFields(connection: GitConnection, form: ConnectionForm): UpdateGitConnectionRequest {
  const body: UpdateGitConnectionRequest = {};
  if (form.name.trim() !== connection.name) body.name = form.name.trim();
  if (form.provider !== connection.provider) body.provider = form.provider;
  if (form.repo_url.trim() !== connection.repo_url) body.repo_url = form.repo_url.trim();
  if (form.default_branch.trim() !== connection.default_branch) body.default_branch = form.default_branch.trim();
  if (form.token) body.token = form.token;
  return body;
}

function VerificationStatus({ connection }: { connection: GitConnection }) {
  switch (connection.status) {
    case 'verifying':
      return <StatusIndicator type="in-progress">Verifying</StatusIndicator>;
    case 'verified':
      return <StatusIndicator type="success">Verified</StatusIndicator>;
    case 'failed': {
      const view = describeSyncFailure(connection.verification?.category, {
        category: connection.verification?.category ?? 'internal',
        message: connection.verification?.message ?? '',
      });
      return (
        <Popover
          size="medium"
          header={view.header}
          content={
            <SpaceBetween size="xxs">
              <div>{view.message}</div>
              {view.action && <Box color="text-body-secondary">{view.action}</Box>}
            </SpaceBetween>
          }
        >
          <StatusIndicator type="error">Failed</StatusIndicator>
        </Popover>
      );
    }
    default:
      return <StatusIndicator type="pending">{connection.status}</StatusIndicator>;
  }
}

export default function GitConnections() {
  const { user } = useAuth();
  const canManage = canManageNodeDesigner(user?.role);
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [connections, setConnections] = useState<GitConnection[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Create / edit modal
  const [editing, setEditing] = useState<GitConnection | 'new' | null>(null);
  const [form, setForm] = useState<ConnectionForm>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  // Verify / delete
  const [verifying, setVerifying] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<GitConnection | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Load use cases on mount; restore the context selection or default to
  // the first use case (same pattern as the other Node_Designer pages).
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        const saved = selectedUsecaseId
          ? useCaseList.find((uc) => uc.usecase_id === selectedUsecaseId)
          : undefined;
        const chosen = saved || useCaseList[0];
        if (chosen) {
          setSelectedUseCase({ label: chosen.name, value: chosen.usecase_id });
          setSelectedUsecaseId(chosen.usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadConnections = useCallback(async (usecaseId: string, quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await nodeDesignerApi.listGitConnections(usecaseId);
      setConnections(response.connections || []);
      setError(null);
    } catch (err: any) {
      if (!quiet) {
        setError(err?.message || 'Git connections could not be loaded');
        setConnections([]);
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedUseCase?.value) {
      loadConnections(selectedUseCase.value);
    } else {
      setConnections([]);
    }
  }, [selectedUseCase, loadConnections]);

  // Poll while any connection is verifying (2.5).
  const anyVerifying = connections.some((c) => c.status === 'verifying');
  useEffect(() => {
    if (!anyVerifying || !selectedUseCase?.value) return;
    const usecaseId = selectedUseCase.value;
    const timer = setInterval(() => loadConnections(usecaseId, true), CONNECTIONS_POLL_MS);
    return () => clearInterval(timer);
  }, [anyVerifying, selectedUseCase, loadConnections]);

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setFormError(null);
    setTouched(false);
    setEditing('new');
  };

  const openEdit = (connection: GitConnection) => {
    setForm(formFor(connection));
    setFormError(null);
    setTouched(false);
    setEditing(connection);
  };

  const closeModal = () => {
    if (submitting) return;
    setEditing(null);
    // Drop the token from memory as soon as the modal closes.
    setForm(EMPTY_FORM);
  };

  const urlError = repoUrlError(form.repo_url);
  const nameError = form.name.trim() ? null : 'Enter a display name.';
  const branchError = form.default_branch.trim() ? null : 'Enter the default branch.';
  const tokenError = editing === 'new' && !form.token ? 'Enter an access token.' : null;
  const formValid = !urlError && !nameError && !branchError && !tokenError;

  const submitForm = async () => {
    if (!editing || !selectedUseCase?.value) return;
    setTouched(true);
    if (!formValid) return;
    setSubmitting(true);
    setFormError(null);
    try {
      if (editing === 'new') {
        const { connection } = await nodeDesignerApi.createGitConnection({
          usecase_id: selectedUseCase.value,
          name: form.name.trim(),
          provider: form.provider,
          repo_url: form.repo_url.trim(),
          default_branch: form.default_branch.trim(),
          token: form.token,
        });
        setConnections((prev) => [connection, ...prev.filter((c) => c.connection_id !== connection.connection_id)]);
      } else {
        const body = changedFields(editing, form);
        if (Object.keys(body).length > 0) {
          const { connection } = await nodeDesignerApi.updateGitConnection(editing.connection_id, body);
          setConnections((prev) => prev.map((c) => (c.connection_id === connection.connection_id ? connection : c)));
        }
      }
      setEditing(null);
      setForm(EMPTY_FORM);
    } catch (err: any) {
      setFormError(err?.message || 'The Git connection could not be saved');
    } finally {
      setSubmitting(false);
    }
  };

  const verify = async (connection: GitConnection) => {
    setVerifying(connection.connection_id);
    setError(null);
    try {
      const response = await nodeDesignerApi.verifyGitConnection(connection.connection_id);
      setConnections((prev) =>
        prev.map((c) => (c.connection_id === response.connection.connection_id ? response.connection : c))
      );
    } catch (err: any) {
      setError(err?.message || 'The verification could not be started');
    } finally {
      setVerifying(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setError(null);
    try {
      await nodeDesignerApi.deleteGitConnection(deleteTarget.connection_id);
      setConnections((prev) => prev.filter((c) => c.connection_id !== deleteTarget.connection_id));
      setDeleteTarget(null);
    } catch (err: any) {
      setDeleteTarget(null);
      setError(err?.message || 'The Git connection could not be deleted');
    } finally {
      setDeleting(false);
    }
  };

  const modalTitle = editing === 'new' ? 'Create Git connection' : `Edit ${editing?.name ?? 'connection'}`;
  const tokenDescription = useMemo(
    () =>
      editing === 'new'
        ? 'A personal access token with read and write access to the repository. Stored in AWS Secrets Manager; never shown again.'
        : 'Leave blank to keep the stored token. Entering a token replaces it and re-verifies the connection.',
    [editing]
  );

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Repositories plugin source can be pushed to and pulled from. Tokens are stored in AWS Secrets Manager and never displayed."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Select
              placeholder="Select use case"
              selectedOption={selectedUseCase}
              options={useCases.map((uc) => ({ label: uc.name, value: uc.usecase_id }))}
              onChange={({ detail }) => {
                setSelectedUseCase(detail.selectedOption);
                if (detail.selectedOption.value) {
                  setSelectedUsecaseId(detail.selectedOption.value);
                }
              }}
            />
            <Button
              iconName="refresh"
              ariaLabel="Refresh Git connections"
              onClick={() => selectedUseCase?.value && loadConnections(selectedUseCase.value)}
            />
            {canManage && (
              <Button variant="primary" disabled={!selectedUseCase?.value} onClick={openCreate}>
                Create connection
              </Button>
            )}
          </SpaceBetween>
        }
      >
        Git connections
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Table<GitConnection>
        items={connections}
        loading={loading}
        loadingText="Loading Git connections"
        trackBy="connection_id"
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: (item) => item.name },
          {
            id: 'provider',
            header: 'Provider',
            cell: (item) => <Badge color="grey">{PROVIDER_LABELS[item.provider] ?? item.provider}</Badge>,
          },
          {
            id: 'repo',
            header: 'Repository',
            cell: (item) => (
              <Link external href={item.repo_url}>
                {item.repo_url}
              </Link>
            ),
          },
          { id: 'branch', header: 'Default branch', cell: (item) => item.default_branch },
          { id: 'status', header: 'Verification', cell: (item) => <VerificationStatus connection={item} /> },
          ...(canManage
            ? [
                {
                  id: 'actions',
                  header: 'Actions',
                  cell: (item: GitConnection) => (
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button variant="inline-link" ariaLabel={`Edit ${item.name}`} onClick={() => openEdit(item)}>
                        Edit
                      </Button>
                      <Button
                        variant="inline-link"
                        ariaLabel={`Verify ${item.name}`}
                        loading={verifying === item.connection_id}
                        disabled={item.status === 'verifying' || verifying !== null}
                        onClick={() => verify(item)}
                      >
                        Verify
                      </Button>
                      <Button
                        variant="inline-link"
                        ariaLabel={`Delete ${item.name}`}
                        onClick={() => setDeleteTarget(item)}
                      >
                        Delete
                      </Button>
                    </SpaceBetween>
                  ),
                },
              ]
            : []),
        ]}
        header={<Header variant="h2" counter={`(${connections.length})`}>Connections</Header>}
        empty={
          <Box textAlign="center" color="text-status-inactive">
            {selectedUseCase
              ? canManage
                ? 'No Git connections yet. Create one to push plugin source to a repository.'
                : 'No Git connections for this use case.'
              : 'Select a use case.'}
          </Box>
        }
      />

      <Modal
        visible={editing !== null}
        header={modalTitle}
        onDismiss={closeModal}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button disabled={submitting} onClick={closeModal}>
                Cancel
              </Button>
              <Button variant="primary" loading={submitting} onClick={submitForm}>
                {editing === 'new' ? 'Create' : 'Save'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {formError && <Alert type="error">{formError}</Alert>}
          <FormField label="Name" errorText={touched ? nameError ?? undefined : undefined}>
            <Input
              value={form.name}
              onChange={({ detail }) => setForm((f) => ({ ...f, name: detail.value }))}
              placeholder="team-plugins"
              ariaLabel="Connection name"
            />
          </FormField>
          <FormField label="Provider">
            <RadioGroup
              value={form.provider}
              onChange={({ detail }) => setForm((f) => ({ ...f, provider: detail.value as GitProvider }))}
              ariaLabel="Provider"
              items={[
                { value: 'github', label: 'GitHub' },
                { value: 'gitlab', label: 'GitLab' },
              ]}
            />
          </FormField>
          <FormField
            label="Repository URL"
            description="HTTPS clone URL of the repository."
            errorText={touched || form.repo_url ? urlError ?? undefined : undefined}
          >
            <Input
              value={form.repo_url}
              onChange={({ detail }) => setForm((f) => ({ ...f, repo_url: detail.value }))}
              placeholder="https://github.com/org/repo.git"
              ariaLabel="Repository URL"
              inputMode="url"
            />
          </FormField>
          <FormField label="Default branch" errorText={touched ? branchError ?? undefined : undefined}>
            <Input
              value={form.default_branch}
              onChange={({ detail }) => setForm((f) => ({ ...f, default_branch: detail.value }))}
              ariaLabel="Default branch"
            />
          </FormField>
          <FormField
            label={editing === 'new' ? 'Access token' : 'New access token'}
            description={tokenDescription}
            errorText={touched ? tokenError ?? undefined : undefined}
          >
            <Input
              type="password"
              value={form.token}
              onChange={({ detail }) => setForm((f) => ({ ...f, token: detail.value }))}
              ariaLabel="Access token"
              autoComplete={false}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      <ConfirmationModal
        visible={deleteTarget !== null}
        title={`Delete ${deleteTarget?.name ?? 'connection'}`}
        message={
          `Delete the Git connection "${deleteTarget?.name ?? ''}"? Its token is scheduled for deletion. ` +
          'Plugin versions linked to it keep their sync history but show as disconnected until relinked.'
        }
        confirmButtonText="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </SpaceBetween>
  );
}
