/**
 * Build_Job detail page (portal-build-fleet-and-workflow-gates,
 * Req 4.2, 4.3, 4.4).
 *
 * - Job fields: target, mode, requester, submission/start/end times,
 *   assigned server, published artifact identifiers for succeeded jobs
 *   (Req 4.3).
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
                : 'No log output was recorded for this build job.')
            }
            rows={25}
            readOnly
          />
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
