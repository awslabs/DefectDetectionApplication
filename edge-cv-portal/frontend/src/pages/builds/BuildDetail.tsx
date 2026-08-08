/**
 * Build_Job detail page (portal-build-fleet-and-workflow-gates,
 * Req 4.2, 4.3, 4.4).
 *
 * - Job fields: target, mode, requester, submission/start/end times,
 *   assigned server, published artifact identifiers for succeeded jobs
 *   (Req 4.3).
 * - Built source: repository, ref, and resolved commit, with a
 *   placeholder for legacy jobs that lack them
 *   (build-source-selection Req 2.6).
 * - Status polling every 15 s while the job is not terminal (Req 4.2).
 * - Log viewer polling GET /builds/{id}/logs every 30 s while the job
 *   runs, forward-paginated with the CloudWatch nextToken (Req 4.4).
 * - Cancel and retry actions per status (Req 4.5, 4.6, 3.6).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  KeyValuePairs,
  Link,
  SpaceBetween,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import BuildStatusBadge from './BuildStatusBadge';
import { formatTimestamp } from './BuildsPage';
import {
  BuildJob,
  BuildLogEvent,
  CANCELLABLE_BUILD_STATUSES,
  DiagnosticStreamField,
  ExecutionDiagnostic,
  isTerminalBuildStatus,
} from './types';

/** Status refresh interval: changes visible within 30 s (Req 4.2). */
const STATUS_POLL_INTERVAL_MS = 15_000;
/** Log refresh interval while the build runs (Req 4.4). */
const LOG_POLL_INTERVAL_MS = 30_000;
/** Events per GET /builds/{id}/logs page. */
const LOG_PAGE_LIMIT = 1000;
/** Safety cap on log pages fetched per refresh. */
const MAX_LOG_PAGES_PER_FETCH = 50;

function formatLogEvents(events: BuildLogEvent[]): string {
  return events
    .map((e) => {
      const time = e.timestamp
        ? new Date(e.timestamp).toLocaleTimeString()
        : '';
      return time ? `[${time}] ${e.message}` : e.message;
    })
    .join('\n');
}

/**
 * Human-readable duration for a diagnostic phase/budget value
 * (build-fleet-execution-failures Req 2.18). A null/absent value has no
 * recorded evidence and renders the explicit unavailable placeholder.
 */
function formatDurationMs(ms?: number | null): string {
  if (ms === null || ms === undefined) return 'not recorded';
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  parts.push(`${seconds}s`);
  return parts.join(' ');
}

/**
 * One stdout/stderr excerpt of the execution diagnostic with its
 * explicit unavailable / empty / truncated states (Req 2.2, 2.18,
 * 3.10) — states are written out in text, never signaled by color.
 */
function DiagnosticStreamExcerpt({
  label,
  field,
}: {
  label: string;
  field?: DiagnosticStreamField;
}) {
  let content: JSX.Element;
  if (!field || field.available !== true) {
    content = (
      <Box color="text-status-inactive">
        Not available from the command provider.
      </Box>
    );
  } else if (!field.text) {
    content = (
      <Box color="text-status-inactive">
        Available but empty (the command produced no output on this
        stream).
      </Box>
    );
  } else {
    content = (
      <SpaceBetween size="xxs">
        {field.truncated && (
          <Box color="text-status-inactive">
            Excerpt truncated to the retained byte limit.
          </Box>
        )}
        <Box fontSize="body-s">
          <pre
            style={{
              fontFamily: 'monospace',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              margin: 0,
            }}
          >
            {field.text}
          </pre>
        </Box>
      </SpaceBetween>
    );
  }
  return (
    <SpaceBetween size="xxs">
      <Header variant="h3">{label}</Header>
      {content}
    </SpaceBetween>
  );
}

/**
 * Execution diagnostics panel (build-fleet-execution-failures Req 2.3,
 * 2.10, 2.18, 3.10): safe classification, response/status details,
 * phase durations, timeout kind/budget/source, last heartbeat/progress,
 * disk evidence, and stdout/stderr excerpts with explicit
 * unavailable/truncated states. Rendered only when the Build Log API
 * returned the optional diagnostic; legacy responses show no panel.
 */
function ExecutionDiagnosticsPanel({
  diagnostic,
}: {
  diagnostic: ExecutionDiagnostic;
}) {
  const timing = diagnostic.timing || {};
  const timeout = diagnostic.timeout;
  const disk = diagnostic.disk;
  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Retained evidence recorded for this build job's command execution."
        >
          Execution diagnostics
        </Header>
      }
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs
            columns={1}
            items={[
              {
                label: 'Classification',
                value: diagnostic.classification || 'not recorded',
              },
              {
                label: 'Command status',
                value: diagnostic.status || 'not recorded',
              },
              {
                label: 'Status details',
                value: diagnostic.statusDetails || 'not recorded',
              },
              {
                label: 'Response code',
                value:
                  diagnostic.responseCode !== null &&
                  diagnostic.responseCode !== undefined
                    ? String(diagnostic.responseCode)
                    : 'not recorded',
              },
              {
                label: 'Observed',
                value: diagnostic.observedAt
                  ? formatTimestamp(diagnostic.observedAt)
                  : 'not recorded',
              },
            ]}
          />
          <KeyValuePairs
            columns={1}
            items={[
              {
                label: 'Queue wait',
                value: formatDurationMs(timing.queueMs),
              },
              {
                label: 'Provisioning',
                value: formatDurationMs(timing.provisioningMs),
              },
              {
                label: 'Execution',
                value: formatDurationMs(timing.executionMs),
              },
              ...(timeout
                ? [
                    {
                      label: 'Timeout',
                      value: `${timeout.kind || 'not recorded'} (budget ${formatDurationMs(
                        timeout.budgetMs
                      )}${timeout.budgetSource ? ` from ${timeout.budgetSource}` : ''})`,
                    },
                    {
                      label: 'Last heartbeat',
                      value: timeout.lastHeartbeatAt
                        ? formatTimestamp(timeout.lastHeartbeatAt)
                        : 'not recorded',
                    },
                    {
                      label: 'Last progress',
                      value: timeout.lastProgressAt
                        ? formatTimestamp(timeout.lastProgressAt)
                        : 'not recorded',
                    },
                  ]
                : []),
              ...(disk
                ? [
                    {
                      label: 'Runner disk',
                      value: disk.available
                        ? `${disk.available_gb ?? '?'} GB free of ${disk.total_gb ?? '?'} GB${
                            disk.docker_storage_path
                              ? ` at ${disk.docker_storage_path}`
                              : ''
                          }`
                        : 'not measured',
                    },
                  ]
                : []),
            ]}
          />
        </ColumnLayout>
        <DiagnosticStreamExcerpt
          label="Command stdout"
          field={diagnostic.stdout}
        />
        <DiagnosticStreamExcerpt
          label="Command stderr"
          field={diagnostic.stderr}
        />
      </SpaceBetween>
    </Container>
  );
}

export default function BuildDetail() {
  const { buildJobId } = useParams<{ buildJobId: string }>();
  const navigate = useNavigate();

  const [job, setJob] = useState<BuildJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const [logs, setLogs] = useState('');
  const [logsLoading, setLogsLoading] = useState(false);
  // Optional execution diagnostic returned with Build Log pages
  // (build-fleet-execution-failures Req 2.3): immutable metadata,
  // identical on every page; absent from legacy responses.
  const [diagnostic, setDiagnostic] = useState<ExecutionDiagnostic | null>(
    null
  );
  // Accumulated log events and the CloudWatch forward token; the same
  // token is re-polled for new output of a running build (Req 4.4).
  const logEventsRef = useRef<BuildLogEvent[]>([]);
  const logTokenRef = useRef<string | null>(null);

  const fetchJob = useCallback(async () => {
    if (!buildJobId) return null;
    try {
      const response = await apiService.getBuild(buildJobId);
      setJob(response.job);
      setError(null);
      return response.job;
    } catch (err) {
      console.error('Failed to fetch build job:', err);
      setError(
        err instanceof Error ? err.message : 'Failed to load build job'
      );
      return null;
    } finally {
      setLoading(false);
    }
  }, [buildJobId]);

  const fetchLogs = useCallback(async () => {
    if (!buildJobId) return;
    setLogsLoading(true);
    try {
      // Follow the forward token until CloudWatch reports no further
      // page (the token repeats when the stream is exhausted).
      for (let page = 0; page < MAX_LOG_PAGES_PER_FETCH; page += 1) {
        const sentToken = logTokenRef.current;
        const response = await apiService.getBuildLogs(buildJobId, {
          limit: LOG_PAGE_LIMIT,
          ...(sentToken ? { nextToken: sentToken } : {}),
        });
        if (response.events.length > 0) {
          logEventsRef.current = [...logEventsRef.current, ...response.events];
        }
        if (response.diagnostic) {
          setDiagnostic(response.diagnostic);
        }
        logTokenRef.current = response.nextToken;
        if (!response.nextToken || response.nextToken === sentToken) {
          break;
        }
      }
      setLogs(formatLogEvents(logEventsRef.current));
    } catch (err) {
      console.error('Failed to fetch build logs:', err);
      // Keep the accumulated log on transient failures.
      if (logEventsRef.current.length === 0) {
        setLogs('Failed to load logs. Use Refresh to try again.');
      }
    } finally {
      setLogsLoading(false);
    }
  }, [buildJobId]);

  // Job status: initial load + 15 s polling while not terminal (Req 4.2).
  useEffect(() => {
    fetchJob();
    const interval = setInterval(async () => {
      const current = await fetchJob();
      if (current && isTerminalBuildStatus(current.status)) {
        clearInterval(interval);
        // One final log fetch so the tail of the build output lands.
        fetchLogs();
      }
    }, STATUS_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchJob, fetchLogs]);

  // Logs: initial load + 30 s polling while the job is not terminal
  // (Req 4.4). The effect re-evaluates when the job's status changes.
  const running = job !== null && !isTerminalBuildStatus(job.status);
  useEffect(() => {
    fetchLogs();
    if (!running) return;
    const interval = setInterval(fetchLogs, LOG_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchLogs, running]);

  const handleCancel = async () => {
    if (!buildJobId) return;
    setActionLoading(true);
    setActionError(null);
    try {
      const response = await apiService.cancelBuild(buildJobId);
      setJob(response.job);
    } catch (err) {
      console.error('Failed to cancel build:', err);
      setActionError(
        err instanceof Error ? err.message : 'Failed to cancel build'
      );
    } finally {
      setActionLoading(false);
    }
  };

  const handleRetry = async () => {
    if (!buildJobId) return;
    setActionLoading(true);
    setActionError(null);
    try {
      const response = await apiService.retryBuild(buildJobId);
      navigate(`/builds/${response.job.build_job_id}`);
    } catch (err) {
      console.error('Failed to retry build:', err);
      setActionError(
        err instanceof Error ? err.message : 'Failed to retry build'
      );
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        Loading build job...
      </Box>
    );
  }

  if (error || !job) {
    return (
      <SpaceBetween size="l">
        <Alert type="error" header="Error loading build job">
          {error || 'Build job not found'}
        </Alert>
        <Button onClick={() => navigate('/builds')}>Back to builds</Button>
      </SpaceBetween>
    );
  }

  const cancellable = CANCELLABLE_BUILD_STATUSES.has(job.status);
  const retryable = job.status === 'interrupted';

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/builds')}>Back to builds</Button>
            {cancellable && (
              <Button onClick={handleCancel} loading={actionLoading}>
                Cancel build
              </Button>
            )}
            {retryable && (
              <Button
                variant="primary"
                onClick={handleRetry}
                loading={actionLoading}
              >
                Retry build
              </Button>
            )}
          </SpaceBetween>
        }
        description={job.component_name}
      >
        <SpaceBetween direction="horizontal" size="xs">
          {`Build ${job.build_target}`}
          <BuildStatusBadge status={job.status} />
        </SpaceBetween>
      </Header>

      {actionError && (
        <Alert type="error" dismissible onDismiss={() => setActionError(null)}>
          {actionError}
        </Alert>
      )}

      {job.status === 'failed' && job.error && (
        <Alert type="error" header="Build failed">
          <SpaceBetween size="xs">
            <div>{job.error.message || job.error.code || 'Build failed'}</div>
            {job.error.kind === 'publishing' && (
              <div>
                Published before the failure:{' '}
                {(job.error.published || []).join(', ') || 'none'}. Not
                published: {(job.error.unpublished || []).join(', ') || 'none'}
                .
              </div>
            )}
          </SpaceBetween>
        </Alert>
      )}

      {job.status === 'interrupted' && (
        <Alert type="warning" header="Build interrupted">
          The build compute was interrupted before the build finished. Logs up
          to the interruption are retained; use Retry to run the same target
          and execution mode again.
        </Alert>
      )}

      {/* Job fields (Req 4.3) */}
      <Container header={<Header variant="h2">Job information</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs
            columns={1}
            items={[
              { label: 'Build job ID', value: job.build_job_id },
              { label: 'Target', value: job.build_target },
              {
                label: 'Execution mode',
                value:
                  job.execution_mode === 'dedicated'
                    ? `dedicated (${job.server_id || '-'})`
                    : 'ephemeral',
              },
              { label: 'Requested by', value: job.requested_by },
              // Built source (build-source-selection Req 2.6): legacy
              // jobs lack these snapshot fields and show '-'. A null or
              // absent source_ref on a source-selection-era job means
              // the repository's default branch.
              {
                label: 'Repository',
                value: job.config_snapshot?.repository || '-',
              },
              {
                label: 'Source ref',
                value: job.config_snapshot?.repository
                  ? job.config_snapshot.source_ref ?? 'default branch'
                  : '-',
              },
              {
                label: 'Resolved commit',
                value: job.source_commit ? (
                  <span style={{ fontFamily: 'monospace' }}>
                    {job.source_commit}
                  </span>
                ) : (
                  '-'
                ),
              },
              ...(job.retry_of
                ? [
                    {
                      label: 'Retry of',
                      value: (
                        <Link
                          onFollow={() => navigate(`/builds/${job.retry_of}`)}
                        >
                          {job.retry_of}
                        </Link>
                      ),
                    },
                  ]
                : []),
            ]}
          />
          <KeyValuePairs
            columns={1}
            items={[
              {
                label: 'Status',
                value: <BuildStatusBadge status={job.status} />,
              },
              { label: 'Submitted', value: formatTimestamp(job.created_at) },
              { label: 'Started', value: formatTimestamp(job.started_at) },
              { label: 'Ended', value: formatTimestamp(job.ended_at) },
            ]}
          />
        </ColumnLayout>
      </Container>

      {/* Published artifact identifiers for succeeded jobs (Req 4.3, 4.7) */}
      {job.status === 'succeeded' && job.result && (
        <Container
          header={<Header variant="h2">Published artifacts</Header>}
        >
          <KeyValuePairs
            columns={1}
            items={[
              {
                label: 'Component',
                value: job.result.component_name || job.component_name,
              },
              {
                label: 'Published version',
                value: job.result.published_version || '-',
              },
              {
                label: 'Pushed images',
                value:
                  job.result.pushed_image_refs &&
                  job.result.pushed_image_refs.length > 0 ? (
                    <SpaceBetween size="xxs">
                      {job.result.pushed_image_refs.map((ref) => (
                        <Box key={ref} fontSize="body-s">
                          <span style={{ fontFamily: 'monospace' }}>{ref}</span>
                        </Box>
                      ))}
                    </SpaceBetween>
                  ) : (
                    '-'
                  ),
              },
            ]}
          />
        </Container>
      )}

      {/* Log viewer (Req 4.4) */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <Button
                iconName="refresh"
                onClick={fetchLogs}
                loading={logsLoading}
              >
                Refresh
              </Button>
            }
          >
            Build log
          </Header>
        }
      >
        <SpaceBetween size="m">
          {running && (
            <Alert type="info">
              The log refreshes every 30 seconds while the build runs. Output
              may take a few minutes to appear after the job starts.
            </Alert>
          )}
          <Textarea
            value={
              logs ||
              (running
                ? 'No log output yet.'
                : diagnostic
                  ? 'No CloudWatch log output was recorded for this build ' +
                    'job. The Execution diagnostics section below shows ' +
                    'the evidence retained for this job.'
                  : 'No log output was recorded for this build job. No ' +
                    'execution diagnostics were retained either; the ' +
                    'evidence is unavailable or may have expired.')
            }
            rows={25}
            readOnly
          />
        </SpaceBetween>
      </Container>

      {/* Execution diagnostics (build-fleet-execution-failures Req 2.3,
          2.18, 3.10): rendered whenever the Build Log API returned the
          optional diagnostic — independently of CloudWatch events — so
          retained SSM/timeout evidence is never hidden behind an
          empty-log message. Legacy responses omit the diagnostic and
          render exactly as before. */}
      {diagnostic && <ExecutionDiagnosticsPanel diagnostic={diagnostic} />}
    </SpaceBetween>
  );
}
