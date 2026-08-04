/**
 * Builds page (portal-build-fleet-and-workflow-gates, Req 1.1, 2.1,
 * 2.5, 4.2, 4.7).
 *
 * - Submit Form with an ordered Build_Target multi-select (the request
 *   order is the sequential build order, Req 1.3) and an execution-mode
 *   RadioGroup: the dedicated option lists running servers and is
 *   disabled while the fleet has no non-terminated server, making
 *   ephemeral the only selectable mode (Req 2.1, 2.5).
 * - Cloudscape Table of the 90-day Build_Job history, most recent
 *   first: status Badge, target, mode, requester, times, published
 *   version for succeeded jobs (Req 4.7). The list refreshes every
 *   15 s so status changes appear within 30 s (Req 4.2).
 * - Cancel and retry row actions per status.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
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
import { apiService } from '../../services/api';
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

/** The four supported Build_Targets in display order (Req 1.1). */
const TARGET_OPTIONS: MultiselectProps.Option[] = [
  { value: 'JP5', label: 'JP5', description: 'Jetson JetPack 5 (arm64)' },
  { value: 'JP6', label: 'JP6', description: 'Jetson JetPack 6 (arm64)' },
  { value: 'AMD64', label: 'AMD64', description: 'x86_64 (CPU)' },
  {
    value: 'AMD64_NVIDIA',
    label: 'AMD64_NVIDIA',
    description: 'x86_64 with NVIDIA GPU',
  },
];

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
      const response = await apiService.submitBuild({
        targets: selectedTargets.map((o) => o.value as BuildTarget),
        execution_mode: executionMode,
        ...(executionMode === 'dedicated' && selectedServer?.value
          ? { server_id: selectedServer.value }
          : {}),
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
