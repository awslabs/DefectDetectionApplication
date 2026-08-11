/**
 * Builds page (portal-build-fleet-and-workflow-gates, Req 1.1, 2.1,
 * 2.5, 4.2, 4.7).
 *
 * - Submit Form with an ordered Build_Target multi-select (the request
 *   order is the sequential build order, Req 1.3) and an execution-mode
 *   RadioGroup: the dedicated option lists running servers and is
 *   disabled while the fleet has no non-terminated server, making
 *   ephemeral the only selectable mode (Req 2.1, 2.5).
 * - Source selection (build-source-selection Req 1.1-1.4, 2.1-2.4,
 *   2.7): a repository Input pre-filled from the effective build
 *   config's `default_repository`, and a branch Autosuggest populated
 *   by debounced branch discovery against the entered repository. The
 *   Autosuggest accepts a typed value, so manual entry (tags, SHAs, or
 *   discovery failure) always works and discovery failure never blocks
 *   submission. The submitted body carries `repository`/`source_ref`
 *   only when non-default and non-empty, keeping the zero-effort
 *   request byte-identical to the pre-feature shape (Req 1.2, 7.1).
 * - Cloudscape Table of the 90-day Build_Job history, most recent
 *   first: status Badge, target, mode, requester, times, published
 *   version for succeeded jobs (Req 4.7). The list refreshes every
 *   15 s so status changes appear within 30 s (Req 4.2).
 * - Cancel and retry row actions per status.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Autosuggest,
  AutosuggestProps,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  Link,
  Multiselect,
  MultiselectProps,
  Pagination,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { ApiError, apiService } from '../../services/api';
import type { BuildServer } from '../../services/api';
import BuildStatusBadge from './BuildStatusBadge';
import {
  BuildExecutionMode,
  BuildJob,
  BuildTarget,
  CANCELLABLE_BUILD_STATUSES,
  isTerminalBuildStatus,
} from './types';

/** List refresh interval: status changes visible within 30 s (Req 4.2). */
const LIST_POLL_INTERVAL_MS = 15_000;
/** GET /builds page size used while draining the 90-day history. */
const HISTORY_FETCH_LIMIT = 200;
/** Safety cap on history pages fetched per refresh. */
const MAX_HISTORY_PAGES = 25;
/** Client-side table page size. */
const TABLE_PAGE_SIZE = 15;

/** The five supported Build_Targets in display order (Req 1.1; JP7 added by jetpack7-support). */
const TARGET_OPTIONS: MultiselectProps.Option[] = [
  { value: 'JP5', label: 'JP5', description: 'Jetson JetPack 5 (arm64)' },
  { value: 'JP6', label: 'JP6', description: 'Jetson JetPack 6 (arm64)' },
  { value: 'JP7', label: 'JP7', description: 'Jetson JetPack 7 / Thor (arm64)' },
  { value: 'AMD64', label: 'AMD64', description: 'x86_64 (CPU)' },
  {
    value: 'AMD64_NVIDIA',
    label: 'AMD64_NVIDIA',
    description: 'x86_64 with NVIDIA GPU',
  },
];

/**
 * Debounce before branch discovery re-runs after the repository value
 * settles (build-source-selection Req 2.2).
 */
const BRANCH_DISCOVERY_DEBOUNCE_MS = 500;

/**
 * User-facing message per branch-discovery error code (the distinct
 * codes GET /build-branches returns, build-source-selection Req 2.3,
 * 3.3). Every message points at manual ref entry, because discovery
 * failure never blocks submission.
 */
export const DISCOVERY_ERROR_MESSAGES: Record<string, string> = {
  REPOSITORY_NOT_FOUND:
    'Repository not found. Check the URL — you can still type a ref manually.',
  REPOSITORY_FORBIDDEN:
    'Repository is not accessible (it may be private). You can still type a ref manually.',
  DISCOVERY_RATE_LIMITED:
    'Branch discovery is rate-limited right now. Retry shortly, or type a ref manually.',
  DISCOVERY_TIMEOUT:
    'Branch discovery timed out. Retry, or type a ref manually.',
  DISCOVERY_UPSTREAM_ERROR:
    'Branch discovery failed upstream. Retry, or type a ref manually.',
  REPOSITORY_EMPTY:
    'The repository has no branches. You can still type a ref manually.',
};

/** Derive the Autosuggest error text from a discovery failure (Req 2.3). */
export function discoveryErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.code && DISCOVERY_ERROR_MESSAGES[err.code]) {
    return DISCOVERY_ERROR_MESSAGES[err.code];
  }
  if (err instanceof Error && err.message) {
    return `Branch discovery failed: ${err.message}. You can still type a ref manually.`;
  }
  return 'Branch discovery failed. You can still type a ref manually.';
}

export function formatTimestamp(ms?: number | null): string {
  if (!ms) return '-';
  return new Date(ms).toLocaleString();
}

export default function BuildsPage() {
  const navigate = useNavigate();

  // History + fleet state.
  const [jobs, setJobs] = useState<BuildJob[]>([]);
  const [servers, setServers] = useState<BuildServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  // Submit form state. Target selection order is preserved: it defines
  // the sequential build order of the request (Req 1.3).
  const [selectedTargets, setSelectedTargets] = useState<
    MultiselectProps.Option[]
  >([]);
  const [executionMode, setExecutionMode] =
    useState<BuildExecutionMode>('ephemeral');
  const [selectedServer, setSelectedServer] =
    useState<SelectProps.Option | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [submittedRequest, setSubmittedRequest] = useState<string | null>(null);

  // Source selection state (build-source-selection Req 1.1-1.4, 2.1-2.4).
  // The repository pre-fills from the effective build config's
  // default_repository (Req 1.1, 1.5); the branch Autosuggest is fed by
  // debounced discovery against the entered repository (Req 2.1, 2.2).
  const [defaultRepository, setDefaultRepository] = useState('');
  const [repository, setRepository] = useState('');
  const [sourceRef, setSourceRef] = useState('');
  const [branchOptions, setBranchOptions] = useState<AutosuggestProps.Option[]>(
    []
  );
  const [branchStatus, setBranchStatus] = useState<
    'pending' | 'loading' | 'error' | 'finished'
  >('pending');
  const [branchError, setBranchError] = useState<string | null>(null);
  const [discoveryRetry, setDiscoveryRetry] = useState(0);
  /** Guards against out-of-order discovery responses. */
  const discoveryRunRef = useRef(0);

  // Row action state (cancel / retry in flight).
  const [actionJobId, setActionJobId] = useState<string | null>(null);

  const loadedOnce = useRef(false);

  const loadData = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try {
      const [serversResponse, firstPage] = await Promise.all([
        apiService.listBuildServers(),
        apiService.listBuilds({ limit: HISTORY_FETCH_LIMIT }),
      ]);

      // Drain the paginated 90-day history (already most recent first).
      const history = [...(firstPage.jobs || [])];
      let token = firstPage.nextToken;
      let pages = 1;
      while (token && pages < MAX_HISTORY_PAGES) {
        const page = await apiService.listBuilds({
          limit: HISTORY_FETCH_LIMIT,
          nextToken: token,
        });
        history.push(...(page.jobs || []));
        token = page.nextToken;
        pages += 1;
      }

      setServers(serversResponse.servers || []);
      setJobs(history);
      setError(null);
      loadedOnce.current = true;
    } catch (err) {
      console.error('Failed to load builds:', err);
      // Keep stale data on background refresh failures.
      if (!background || !loadedOnce.current) {
        setError(err instanceof Error ? err.message : 'Failed to load builds');
      }
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  // Initial load + 15 s refresh (Req 4.2).
  useEffect(() => {
    loadData();
    const interval = setInterval(() => loadData(true), LIST_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadData]);

  // Pre-fill the repository from the effective build config's
  // default_repository (Req 1.1, 1.5). A config read failure leaves the
  // field editable and empty — it never blocks the form.
  useEffect(() => {
    let cancelled = false;
    apiService
      .getBuildConfig()
      .then(({ config }) => {
        if (cancelled) return;
        const configured = config.default_repository || '';
        setDefaultRepository(configured);
        setRepository((current) => current || configured);
      })
      .catch((err) => {
        console.error('Failed to load build config default repository:', err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Branch discovery, re-run debounced whenever the repository value
  // settles (Req 2.1, 2.2). Failures surface on the Autosuggest via
  // statusType/errorText and never block submission (Req 2.3).
  useEffect(() => {
    const repo = repository.trim();
    if (!repo) {
      discoveryRunRef.current += 1;
      setBranchOptions([]);
      setBranchStatus('pending');
      setBranchError(null);
      return;
    }
    const runId = ++discoveryRunRef.current;
    setBranchStatus('loading');
    setBranchError(null);
    const timer = setTimeout(async () => {
      try {
        const response = await apiService.listBuildBranches(repo);
        if (discoveryRunRef.current !== runId) return;
        setBranchOptions(
          (response.branches || []).map((branch) => ({
            value: branch,
            ...(branch === response.default_branch
              ? { description: 'default branch' }
              : {}),
          }))
        );
        setBranchStatus('finished');
      } catch (err) {
        if (discoveryRunRef.current !== runId) return;
        setBranchOptions([]);
        setBranchStatus('error');
        setBranchError(discoveryErrorMessage(err));
      }
    }, BRANCH_DISCOVERY_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [repository, discoveryRetry]);

  // Fleet-derived form facts. Ephemeral is the only selectable mode
  // while the fleet has no non-terminated server (Req 2.5); the
  // dedicated server selection lists running servers only (Req 2.1).
  const nonTerminatedServers = useMemo(
    () => servers.filter((s) => s.lifecycle_state !== 'terminated'),
    [servers]
  );
  const runningServers = useMemo(
    () => servers.filter((s) => s.lifecycle_state === 'running'),
    [servers]
  );
  const dedicatedAvailable = nonTerminatedServers.length > 0;

  // If the fleet loses its last non-terminated server while dedicated
  // is selected, fall back to ephemeral (Req 2.5).
  useEffect(() => {
    if (!dedicatedAvailable && executionMode === 'dedicated') {
      setExecutionMode('ephemeral');
      setSelectedServer(null);
    }
  }, [dedicatedAvailable, executionMode]);

  const serverOptions: SelectProps.Option[] = useMemo(
    () =>
      runningServers.map((s) => ({
        value: s.server_id,
        label: s.name,
        description: `${s.instance_type} · ${s.cpu_architecture}${
          s.running_build_job_id ? ' · build running (will queue)' : ''
        }`,
      })),
    [runningServers]
  );

  /**
   * Reconcile a Multiselect change while preserving selection order:
   * previously selected targets keep their position, newly selected
   * ones are appended (the request order drives the sequential build
   * order, Req 1.3).
   */
  const handleTargetsChange = (
    newSelection: readonly MultiselectProps.Option[]
  ) => {
    const selectedValues = new Set(newSelection.map((o) => o.value));
    const kept = selectedTargets.filter((o) => selectedValues.has(o.value));
    const keptValues = new Set(kept.map((o) => o.value));
    const appended = newSelection.filter((o) => !keptValues.has(o.value));
    setSelectedTargets([...kept, ...appended]);
    setTargetsError(null);
  };

  const handleSubmit = async () => {
    // Client-side checks mirroring the backend validation (Req 1.8, 2.6).
    let valid = true;
    if (selectedTargets.length === 0) {
      setTargetsError('Select at least one build target.');
      valid = false;
    }
    if (executionMode === 'dedicated' && !selectedServer?.value) {
      setServerError('Select the dedicated build server to use.');
      valid = false;
    }
    if (!valid) return;

    setSubmitting(true);
    setSubmitError(null);
    setSubmittedRequest(null);
    try {
      // repository/source_ref ride only when non-default and non-empty,
      // so the zero-effort request body stays byte-identical to the
      // pre-feature shape (Req 1.2, 2.4, 7.1). The backend validates
      // and rejects malformed values with the field named (Req 1.4).
      const trimmedRepository = repository.trim();
      const trimmedRef = sourceRef.trim();
      const response = await apiService.submitBuild({
        targets: selectedTargets.map((o) => o.value as BuildTarget),
        execution_mode: executionMode,
        ...(executionMode === 'dedicated' && selectedServer?.value
          ? { server_id: selectedServer.value }
          : {}),
        ...(trimmedRepository && trimmedRepository !== defaultRepository
          ? { repository: trimmedRepository }
          : {}),
        ...(trimmedRef ? { source_ref: trimmedRef } : {}),
      });
      setSubmittedRequest(
        `Build request submitted: ${response.jobs.length} job${
          response.jobs.length === 1 ? '' : 's'
        } created.`
      );
      setSelectedTargets([]);
      await loadData(true);
    } catch (err) {
      console.error('Failed to submit build request:', err);
      setSubmitError(
        err instanceof Error ? err.message : 'Failed to submit build request'
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (job: BuildJob) => {
    setActionJobId(job.build_job_id);
    setError(null);
    try {
      await apiService.cancelBuild(job.build_job_id);
      await loadData(true);
    } catch (err) {
      console.error('Failed to cancel build:', err);
      setError(err instanceof Error ? err.message : 'Failed to cancel build');
    } finally {
      setActionJobId(null);
    }
  };

  const handleRetry = async (job: BuildJob) => {
    setActionJobId(job.build_job_id);
    setError(null);
    try {
      const response = await apiService.retryBuild(job.build_job_id);
      navigate(`/builds/${response.job.build_job_id}`);
    } catch (err) {
      console.error('Failed to retry build:', err);
      setError(err instanceof Error ? err.message : 'Failed to retry build');
      setActionJobId(null);
    }
  };

  const serverName = (serverId?: string | null) => {
    if (!serverId) return null;
    return servers.find((s) => s.server_id === serverId)?.name || serverId;
  };

  const paginatedJobs = jobs.slice(
    (currentPage - 1) * TABLE_PAGE_SIZE,
    currentPage * TABLE_PAGE_SIZE
  );

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Submit form (Req 1.1, 2.1, 2.5) */}
      <Container
        header={
          <Header
            variant="h2"
            description="Build and publish edge components for the selected targets. Multiple targets build sequentially in the order selected."
          >
            Submit build
          </Header>
        }
      >
        <Form
          actions={
            <Button
              variant="primary"
              onClick={handleSubmit}
              loading={submitting}
            >
              Submit build request
            </Button>
          }
          errorText={submitError ?? undefined}
        >
          <SpaceBetween size="l">
            {submittedRequest && (
              <Alert
                type="success"
                dismissible
                onDismiss={() => setSubmittedRequest(null)}
              >
                {submittedRequest}
              </Alert>
            )}
            <FormField
              label="Build targets"
              description="Selection order defines the build order."
              errorText={targetsError ?? undefined}
            >
              <Multiselect
                selectedOptions={selectedTargets}
                onChange={({ detail }) =>
                  handleTargetsChange(detail.selectedOptions)
                }
                options={TARGET_OPTIONS}
                placeholder="Select one or more build targets"
                keepOpen
              />
            </FormField>
            <FormField
              label="Repository"
              description="HTTPS Git repository to build from. Defaults to the configured repository; enter a fork to build your own."
            >
              <Input
                value={repository}
                onChange={({ detail }) => setRepository(detail.value)}
                placeholder={
                  defaultRepository || 'https://github.com/owner/repository'
                }
                ariaLabel="Repository"
              />
            </FormField>
            <FormField
              label={
                <span>
                  Branch or ref <i>- optional</i>
                </span>
              }
              description="Pick a discovered branch or type any branch, tag, or commit SHA. Leave empty to build the repository's default branch."
            >
              <Autosuggest
                value={sourceRef}
                onChange={({ detail }) => setSourceRef(detail.value)}
                options={branchOptions}
                statusType={branchStatus}
                loadingText="Discovering branches"
                errorText={branchError ?? undefined}
                recoveryText="Retry discovery"
                onLoadItems={() => {
                  // Recovery click re-runs discovery; failure never
                  // blocks manual entry or submission (Req 2.3).
                  if (branchStatus === 'error') {
                    setDiscoveryRetry((n) => n + 1);
                  }
                }}
                empty="No branches discovered. Type a branch, tag, or commit SHA."
                enteredTextLabel={(value) => `Use ref: "${value}"`}
                placeholder="Repository default branch"
                filteringType="auto"
                ariaLabel="Branch or ref"
              />
            </FormField>
            <FormField
              label="Execution mode"
              description={
                dedicatedAvailable
                  ? 'Ephemeral compute is provisioned per build and terminated afterwards; dedicated builds run on a fleet server.'
                  : 'No dedicated build servers exist in the fleet, so ephemeral compute is the only available mode.'
              }
            >
              <RadioGroup
                value={executionMode}
                onChange={({ detail }) => {
                  setExecutionMode(detail.value as BuildExecutionMode);
                  setServerError(null);
                }}
                items={[
                  {
                    value: 'ephemeral',
                    label: 'Ephemeral compute',
                    description:
                      'Provisioned on demand for this build, terminated when it finishes (no idle cost).',
                  },
                  {
                    value: 'dedicated',
                    label: 'Dedicated build server',
                    description: dedicatedAvailable
                      ? 'Run on a running fleet server.'
                      : 'Unavailable: the fleet has no build server.',
                    disabled: !dedicatedAvailable,
                  },
                ]}
              />
            </FormField>
            {executionMode === 'dedicated' && (
              <FormField
                label="Build server"
                description="Running servers in the fleet. The server's CPU architecture must match every selected target."
                errorText={serverError ?? undefined}
              >
                <Select
                  selectedOption={selectedServer}
                  onChange={({ detail }) => {
                    setSelectedServer(detail.selectedOption);
                    setServerError(null);
                  }}
                  options={serverOptions}
                  placeholder="Select a running build server"
                  empty="No running build servers. Start one from the Fleet page."
                />
              </FormField>
            )}
          </SpaceBetween>
        </Form>
      </Container>

      {/* 90-day history, most recent first (Req 4.7) */}
      <Table
        resizableColumns
        header={
          <Header
            variant="h1"
            description="Build jobs from the last 90 days, most recent first. Refreshes every 15 seconds."
            counter={`(${jobs.length})`}
            actions={
              <Button
                iconName="refresh"
                onClick={() => loadData()}
                loading={loading}
              >
                Refresh
              </Button>
            }
          >
            Builds
          </Header>
        }
        pagination={
          <Pagination
            currentPageIndex={currentPage}
            pagesCount={Math.max(1, Math.ceil(jobs.length / TABLE_PAGE_SIZE))}
            onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
          />
        }
        loading={loading}
        loadingText="Loading builds"
        items={paginatedJobs}
        columnDefinitions={[
          {
            id: 'job',
            header: 'Build job',
            cell: (item) => (
              <Link onFollow={() => navigate(`/builds/${item.build_job_id}`)}>
                {item.build_job_id.substring(0, 8)}
              </Link>
            ),
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item) => <BuildStatusBadge status={item.status} />,
          },
          {
            id: 'target',
            header: 'Target',
            cell: (item) => item.build_target,
          },
          {
            id: 'mode',
            header: 'Mode',
            cell: (item) =>
              item.execution_mode === 'dedicated'
                ? `dedicated (${serverName(item.server_id) || '-'})`
                : 'ephemeral',
          },
          {
            id: 'requester',
            header: 'Requested by',
            cell: (item) => item.requested_by || '-',
          },
          {
            id: 'submitted',
            header: 'Submitted',
            cell: (item) => formatTimestamp(item.created_at),
          },
          {
            id: 'started',
            header: 'Started',
            cell: (item) => formatTimestamp(item.started_at),
          },
          {
            id: 'ended',
            header: 'Ended',
            cell: (item) => formatTimestamp(item.ended_at),
          },
          {
            id: 'published',
            header: 'Published version',
            cell: (item) =>
              item.status === 'succeeded'
                ? item.result?.published_version || '-'
                : '-',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xxs">
                {CANCELLABLE_BUILD_STATUSES.has(item.status) && (
                  <Button
                    variant="inline-link"
                    onClick={() => handleCancel(item)}
                    loading={actionJobId === item.build_job_id}
                  >
                    Cancel
                  </Button>
                )}
                {item.status === 'interrupted' && (
                  <Button
                    variant="inline-link"
                    onClick={() => handleRetry(item)}
                    loading={actionJobId === item.build_job_id}
                  >
                    Retry
                  </Button>
                )}
                {isTerminalBuildStatus(item.status) &&
                  item.status !== 'interrupted' && (
                    <Box color="text-body-secondary">-</Box>
                  )}
              </SpaceBetween>
            ),
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No builds</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No build jobs in the last 90 days. Submit one above.
            </Box>
          </Box>
        }
      />
    </SpaceBetween>
  );
}
