import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  ColumnLayout,
  Box,
  StatusIndicator,
  ProgressBar,
  Button,
  ButtonDropdown,
  Tabs,
  KeyValuePairs,
  Alert,
  Link,
  Modal,
  Table,
  FormField,
  Input,
} from '@cloudscape-design/components';
import { useParams, useNavigate } from 'react-router-dom';
import { LabelingJob } from '../types';
import { apiService, LabelingMemberProgress } from '../services/api';
import ManifestTransformer from '../components/ManifestTransformer';
import {
  ALIGNMENT_BREAKING_PATTERN,
  PROMPT_GUIDANCE_CONSTRAINT,
  PromptGuidanceContent,
  findPromptGuardrailViolation,
  promptGuardrailMessage,
} from './promptOverrideGuardrails';

/** Raw `GET /labeling/{id}` job payload, including DDA-only fields. */
type ApiLabelingJob = Awaited<
  ReturnType<typeof apiService.getLabelingJob>
>['job'];

/**
 * Progress display values for a DDA job (dda-data-labeling Requirements
 * 11.1, 11.10). For Skip_Verification_Mode jobs the submitted count the
 * backend reports is the count of completed auto-label attempts
 * (succeeded or failed), so the description and substitution note change
 * accordingly.
 */
export function getDdaProgress(job: {
  submitted_count?: number;
  image_count?: number;
  progress_percent?: number;
  skip_verification?: boolean;
}): { percent: number; description: string; note?: string } {
  const submitted = job.submitted_count ?? 0;
  const total = job.image_count ?? 0;
  const percent =
    job.progress_percent ??
    (total > 0 ? Math.round((submitted * 100) / total) : 0);
  if (job.skip_verification) {
    return {
      percent,
      description: `${submitted} of ${total} auto-label attempts completed`,
      note:
        'Skip-verification job: progress reflects auto-label completion ' +
        '(succeeded or failed attempts), not labeler submissions.',
    };
  }
  return {
    percent,
    description: `${submitted} of ${total} tasks submitted`,
  };
}

/**
 * The Stop action applies only to InProgress DDA jobs (dda-data-labeling
 * Requirements 11.4, 11.9).
 */
export function canStopDdaJob(job: {
  labeling_backend?: string;
  status?: string;
}): boolean {
  return job.labeling_backend === 'DDA' && job.status === 'InProgress';
}

/**
 * Prompt_Override length limit for the re-run dialog's entries, judged on
 * the raw entered value — a local mirror of the wizard's exported
 * `MAX_PROMPT_OVERRIDE_LENGTH` (CreateLabelingJob.tsx) so the detail page
 * does not import the whole wizard module; the error message shape is
 * the wizard's pinned one (grounded-sam-autolabel Requirement 2.6;
 * grounded-sam-prompt-guardrails-and-prelabel-retry Requirement 7.3).
 */
const MAX_PROMPT_OVERRIDE_LENGTH = 256;

/**
 * The Re-run pre-labels action applies only to Retry_Eligible_Jobs: DDA
 * jobs in InProgress status with auto-labeling on (`auto_label.enabled`
 * or Skip_Verification_Mode), review not finalized, and at least one
 * Failed pre-label task
 * (grounded-sam-prompt-guardrails-and-prelabel-retry Requirements 7.1,
 * 7.2; the backend re-checks the same gates).
 */
export function canRerunPrelabels(job: {
  labeling_backend?: string;
  status?: string;
  auto_label?: { enabled?: boolean };
  skip_verification?: boolean;
  review_finalized?: boolean;
  prelabel_failed_count?: number;
}): boolean {
  return (
    job.labeling_backend === 'DDA' &&
    job.status === 'InProgress' &&
    Boolean(job.auto_label?.enabled || job.skip_verification) &&
    !job.review_finalized &&
    (job.prelabel_failed_count ?? 0) >= 1
  );
}

/**
 * Own-keys string-record equality: same key set, character-identical
 * values. Decides whether the re-run dialog's pruned override map equals
 * the job record's persisted map — the `prompt_overrides` body field is
 * omitted exactly when it does, making a no-op edit a pure retry
 * (grounded-sam-prompt-guardrails-and-prelabel-retry Requirement 7.5).
 */
function promptOverrideMapsEqual(
  a: Record<string, string>,
  b: Record<string, string>
): boolean {
  const aKeys = Object.keys(a);
  return (
    aKeys.length === Object.keys(b).length &&
    aKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(b, key) && a[key] === b[key]
    )
  );
}

/**
 * Messages of a rejected Retry_Request's `validation_errors` list (the
 * creation-shaped 400 body rides on the thrown ApiError's `details`),
 * empty when the error carries none
 * (grounded-sam-prompt-guardrails-and-prelabel-retry Requirement 7.7).
 * Duck-typed so any Error-shaped rejection is handled.
 */
function extractValidationErrorMessages(error: unknown): string[] {
  const details =
    error && typeof error === 'object' && 'details' in error
      ? (error as { details?: unknown }).details
      : undefined;
  const validationErrors =
    details && typeof details === 'object' && 'validation_errors' in details
      ? (details as { validation_errors?: unknown }).validation_errors
      : undefined;
  if (!Array.isArray(validationErrors)) {
    return [];
  }
  return validationErrors.flatMap((entry) => {
    if (entry && typeof entry === 'object') {
      const message = (entry as { message?: unknown }).message;
      if (typeof message === 'string') {
        return [message];
      }
    }
    return [];
  });
}

export default function LabelingDetail() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [job, setJob] = useState<LabelingJob | null>(null);
  const [rawJob, setRawJob] = useState<ApiLabelingJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showTransformModal, setShowTransformModal] = useState(false);
  // DDA stop flow (dda-data-labeling Requirements 11.4, 11.5).
  const [showStopModal, setShowStopModal] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  // Re-run pre-labels flow
  // (grounded-sam-prompt-guardrails-and-prelabel-retry Requirements
  // 7.3-7.7): the dialog's per-label override entries, seeded from the
  // job record's persisted map when the dialog opens, and its inline
  // error state (message plus the response's validation_errors, when
  // any).
  const [showRerunModal, setShowRerunModal] = useState(false);
  const [rerunOverrides, setRerunOverrides] = useState<
    Record<string, string>
  >({});
  const [rerunSubmitting, setRerunSubmitting] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [rerunErrorDetails, setRerunErrorDetails] = useState<string[]>([]);

  useEffect(() => {
    loadJob();
  }, [jobId]);

  const loadJob = async () => {
    if (!jobId) return;
    
    setLoading(true);
    try {
      const response = await apiService.getLabelingJob(jobId);
      const apiJob = response.job;
      setRawJob(apiJob);
      
      // Map API response to LabelingJob type
      // Convert status from backend format (InProgress, Completed, Failed) to frontend format
      const statusMap: Record<string, LabelingJob['status']> = {
        'InProgress': 'in_progress',
        'Completed': 'completed',
        'Failed': 'failed',
        'Stopped': 'failed',
      };
      
      const mappedJob: LabelingJob = {
        job_id: apiJob.job_id,
        usecase_id: apiJob.usecase_id,
        name: apiJob.job_name,
        manifest_s3: apiJob.manifest_s3_uri,
        output_s3: apiJob.output_s3_uri,
        task_type: apiJob.task_type as LabelingJob['task_type'],
        images_count: apiJob.image_count,
        labeled_count: apiJob.human_labeled || apiJob.labeled_objects || 0,
        status: statusMap[apiJob.status] || 'pending',
        progress_percent: apiJob.progress_percent || 0,
        ground_truth_job_arn: apiJob.sagemaker_job_name,
        workforce_type: 'private',
        created_by: apiJob.created_by,
        created_at: apiJob.created_at,
        completed_at: apiJob.completed_at,
        console_url: apiJob.console_url,
        worker_portal_url: apiJob.worker_portal_url,
      };
      
      setJob(mappedJob);
    } catch (error) {
      console.error('Failed to load labeling job:', error);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIndicator = (status: LabelingJob['status']) => {
    const statusMap = {
      pending: { type: 'pending' as const, label: 'Pending' },
      in_progress: { type: 'in-progress' as const, label: 'In Progress' },
      completed: { type: 'success' as const, label: 'Completed' },
      failed: { type: 'error' as const, label: 'Failed' },
    };
    const config = statusMap[status];
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  // DDA jobs use the portal-managed status values directly
  // (InProgress | Completed | Failed | Stopped, Requirement 11.3).
  const getDdaStatusIndicator = (status: string) => {
    const statusMap: Record<
      string,
      { type: 'in-progress' | 'success' | 'error' | 'stopped'; label: string }
    > = {
      InProgress: { type: 'in-progress', label: 'In Progress' },
      Completed: { type: 'success', label: 'Completed' },
      Failed: { type: 'error', label: 'Failed' },
      Stopped: { type: 'stopped', label: 'Stopped' },
    };
    const config = statusMap[status] || {
      type: 'in-progress' as const,
      label: status,
    };
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  // Stop an InProgress DDA job (Requirements 11.4, 11.5): on failure the
  // job stays InProgress and an explicit not-stopped error is shown.
  const handleStopJob = async () => {
    if (!jobId) return;
    setStopping(true);
    setStopError(null);
    try {
      await apiService.stopLabelingJob(jobId);
      setShowStopModal(false);
      await loadJob();
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : 'Unknown error';
      setStopError(`The job was not stopped: ${reason}`);
    } finally {
      setStopping(false);
    }
  };

  // Open the re-run dialog with the override entries pre-filled from the
  // job record's persisted `auto_label.prompt_overrides`
  // (grounded-sam-prompt-guardrails-and-prelabel-retry Requirement 7.3).
  const openRerunModal = () => {
    setRerunError(null);
    setRerunErrorDetails([]);
    setRerunOverrides({ ...(rawJob?.auto_label?.prompt_overrides ?? {}) });
    setShowRerunModal(true);
  };

  // Submit the Retry_Request
  // (grounded-sam-prompt-guardrails-and-prelabel-retry Requirements
  // 7.5-7.7): grounded-sam jobs assemble the pruned override map with the
  // creation rules (entries non-empty after trimming whose label belongs
  // to the Label_Set, raw values), omitting the body exactly when the
  // pruned map equals the persisted one; a 202 closes the dialog and
  // refreshes the detail; an error renders inline with the entered
  // values retained.
  const handleRerunPrelabels = async () => {
    if (!jobId || !rawJob) return;
    let body: { prompt_overrides: Record<string, string> } | undefined;
    if (rawJob.auto_label?.model === 'grounded-sam') {
      const labels = rawJob.label_set ?? [];
      // The wizard's validation order (Requirement 7.3): over-length
      // first, its pinned message keeping precedence, then the
      // Prompt_Guardrail over the Effective_Prompts.
      const overlongLabel = labels.find(
        (label) =>
          (rerunOverrides[label] || '').length > MAX_PROMPT_OVERRIDE_LENGTH
      );
      if (overlongLabel !== undefined) {
        setRerunError(
          `The text prompt for label "${overlongLabel}" exceeds ${MAX_PROMPT_OVERRIDE_LENGTH} characters`
        );
        setRerunErrorDetails([]);
        return;
      }
      const guardrailViolation = findPromptGuardrailViolation(
        labels,
        rerunOverrides
      );
      if (guardrailViolation !== null) {
        setRerunError(promptGuardrailMessage(guardrailViolation));
        setRerunErrorDetails([]);
        return;
      }
      const pruned: Record<string, string> = {};
      for (const label of labels) {
        const value = rerunOverrides[label];
        if (typeof value === 'string' && value.trim() !== '') {
          pruned[label] = value;
        }
      }
      const persisted = rawJob.auto_label?.prompt_overrides ?? {};
      body = promptOverrideMapsEqual(pruned, persisted)
        ? undefined
        : { prompt_overrides: pruned };
    }
    setRerunSubmitting(true);
    setRerunError(null);
    setRerunErrorDetails([]);
    try {
      await apiService.rerunPrelabels(jobId, body);
      setShowRerunModal(false);
      await loadJob();
    } catch (error) {
      setRerunError(
        error instanceof Error ? error.message : 'Unknown error'
      );
      setRerunErrorDetails(extractValidationErrorMessages(error));
    } finally {
      setRerunSubmitting(false);
    }
  };

  const handleDownloadManifest = () => {
    if (job) {
      console.log('Downloading manifest from:', job.manifest_s3);
      // TODO: Implement actual download
      alert('Manifest download will be implemented with API integration');
    }
  };

  const handleDownloadOutput = () => {
    if (job) {
      console.log('Downloading output from:', job.output_s3);
      // TODO: Implement actual download
      alert('Output download will be implemented with API integration');
    }
  };

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading labeling job details...
        </Box>
      </Container>
    );
  }

  if (!job) {
    return (
      <Container>
        <Alert type="error">Labeling job not found</Alert>
      </Container>
    );
  }

  // DDA jobs render a portal-native detail view (dda-data-labeling
  // Requirements 5.4, 6.4, 6.6, 11.1, 11.2, 11.4, 11.5, 11.10). Ground
  // Truth jobs fall through to the existing rendering unchanged.
  if (rawJob && rawJob.labeling_backend === 'DDA') {
    const progress = getDdaProgress(rawJob);
    const memberProgress: LabelingMemberProgress[] =
      rawJob.member_progress || [];
    const notificationFailures = rawJob.notification_failures || [];
    const unassignedCount = rawJob.unassigned_count || 0;
    // LLM auto-label configuration (llm-auto-labeling Requirement 10.1):
    // the model identifier and the full stored Detection_Prompt render
    // only for `llm:` jobs; non-LLM jobs show neither.
    const autoLabelModel = rawJob.auto_label?.model;
    const llmModelId =
      typeof autoLabelModel === 'string' && autoLabelModel.startsWith('llm:')
        ? autoLabelModel.slice('llm:'.length)
        : null;
    // Pre-label outcome counts (Requirement 10.3): shown once at least
    // one task has resolved (Available or Failed), omitted entirely
    // before that.
    const prelabelAvailable = rawJob.prelabel_available_count ?? 0;
    const prelabelFailed = rawJob.prelabel_failed_count ?? 0;
    const prelabelResolved = prelabelAvailable + prelabelFailed > 0;
    // Failure_Reason_Summary entries and re-run dialog composition
    // (grounded-sam-prompt-guardrails-and-prelabel-retry Requirements
    // 4.2, 7.3, 7.4): grounded-sam jobs edit one override entry per
    // Label_Set label; every other family gets a plain confirmation.
    const prelabelFailureReasons = rawJob.prelabel_failure_reasons || [];
    const isGroundedSamJob = autoLabelModel === 'grounded-sam';
    const rerunLabels = rawJob.label_set ?? [];

    return (
      <>
        <SpaceBetween size="l">
          <Container
            header={
              <Header
                variant="h1"
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button onClick={() => navigate('/labeling')}>
                      Back to List
                    </Button>
                    {rawJob.skip_verification && rawJob.review_ready && (
                      <Button
                        onClick={() =>
                          navigate(`/labeling/${rawJob.job_id}/review`)
                        }
                      >
                        Review Auto-Labels
                      </Button>
                    )}
                    {canStopDdaJob(rawJob) && (
                      <Button
                        variant="primary"
                        onClick={() => {
                          setStopError(null);
                          setShowStopModal(true);
                        }}
                      >
                        Stop Job
                      </Button>
                    )}
                  </SpaceBetween>
                }
              >
                {rawJob.job_name}
              </Header>
            }
          >
            <ColumnLayout columns={4} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Status</Box>
                <div>{getDdaStatusIndicator(rawJob.status)}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Task Type</Box>
                <div>{rawJob.task_type}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Labeling Backend</Box>
                <div>DDA (portal-native)</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Created By</Box>
                <div>{rawJob.created_by}</div>
              </div>
            </ColumnLayout>
          </Container>

          {stopError && (
            <Alert
              type="error"
              header="Stop failed"
              dismissible
              onDismiss={() => setStopError(null)}
            >
              {stopError} The job remains In Progress.
            </Alert>
          )}

          {rawJob.blocked && (
            <Alert type="warning" header="Labeling blocked">
              The last member was removed from this job's labeling team, so
              its unsubmitted tasks are unassigned. Add a member to the team
              to resume labeling.
            </Alert>
          )}

          {rawJob.notifications_skipped && (
            <Alert type="info" header="Notifications skipped">
              Email notifications were skipped for this job because no SES
              sender address is configured for the portal deployment.
            </Alert>
          )}

          {notificationFailures.length > 0 && (
            <Alert type="warning" header="Notification failures">
              <SpaceBetween size="xxs">
                <Box>
                  Notification emails could not be delivered to the
                  following recipients:
                </Box>
                <ul>
                  {notificationFailures.map((failure, index) => (
                    <li key={`${failure.email}-${index}`}>
                      {failure.email}: {failure.reason}
                    </li>
                  ))}
                </ul>
              </SpaceBetween>
            </Alert>
          )}

          {rawJob.status === 'Failed' && rawJob.failure_reason && (
            <Alert type="error" header="Job failed">
              {rawJob.failure_reason}
            </Alert>
          )}

          <Container header={<Header variant="h2">Progress</Header>}>
            <SpaceBetween size="l">
              <ProgressBar
                value={progress.percent}
                label="Labeling Progress"
                description={progress.description}
                additionalInfo={`${progress.percent}% complete`}
              />
              {progress.note && (
                <Box color="text-body-secondary" fontSize="body-s">
                  {progress.note}
                </Box>
              )}

              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Total Images</Box>
                  <Box fontSize="heading-xl" fontWeight="bold">
                    {(rawJob.image_count || 0).toLocaleString()}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    {rawJob.skip_verification
                      ? 'Auto-Label Attempts Completed'
                      : 'Submitted'}
                  </Box>
                  <Box
                    fontSize="heading-xl"
                    fontWeight="bold"
                    color="text-status-success"
                  >
                    {(rawJob.submitted_count || 0).toLocaleString()}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Remaining</Box>
                  <Box
                    fontSize="heading-xl"
                    fontWeight="bold"
                    color="text-status-info"
                  >
                    {Math.max(
                      (rawJob.image_count || 0) -
                        (rawJob.submitted_count || 0),
                      0
                    ).toLocaleString()}
                  </Box>
                </div>
              </ColumnLayout>
            </SpaceBetween>
          </Container>

          {prelabelFailed >= 1 && (
            <Alert
              type="warning"
              header="Pre-labeling failures"
              data-testid="prelabel-failures-alert"
            >
              <SpaceBetween size="xxs">
                <Box>
                  {prelabelFailed.toLocaleString()} pre-label task
                  {prelabelFailed === 1 ? '' : 's'} failed.
                </Box>
                {prelabelFailureReasons.length > 0 && (
                  <ul>
                    {prelabelFailureReasons.map((entry, index) => (
                      <li key={`${entry.reason}-${index}`}>
                        {entry.reason} ({entry.count.toLocaleString()} image
                        {entry.count === 1 ? '' : 's'})
                      </li>
                    ))}
                  </ul>
                )}
              </SpaceBetween>
            </Alert>
          )}

          {(llmModelId !== null || prelabelResolved) && (
            <Container
              header={
                <Header
                  variant="h2"
                  actions={
                    canRerunPrelabels(rawJob) ? (
                      <Button
                        data-testid="rerun-prelabels-button"
                        onClick={openRerunModal}
                      >
                        {`Re-run pre-labels (${prelabelFailed.toLocaleString()} failed)`}
                      </Button>
                    ) : undefined
                  }
                >
                  Auto-Labeling
                </Header>
              }
            >
              <SpaceBetween size="l">
                {llmModelId !== null && (
                  <KeyValuePairs
                    columns={1}
                    items={[
                      { label: 'Model', value: llmModelId },
                      {
                        label: 'Detection Prompt',
                        value: (
                          // Full stored prompt, untruncated, newlines and
                          // whitespace preserved (Requirement 10.1).
                          <Box fontSize="body-s">
                            <span style={{ whiteSpace: 'pre-wrap' }}>
                              {rawJob.auto_label?.detection_prompt ?? ''}
                            </span>
                          </Box>
                        ),
                      },
                    ]}
                  />
                )}
                {prelabelResolved && (
                  <ColumnLayout columns={2} variant="text-grid">
                    <div>
                      <Box variant="awsui-key-label">
                        Pre-Labels Available
                      </Box>
                      <Box
                        fontSize="heading-xl"
                        fontWeight="bold"
                        color="text-status-success"
                      >
                        {prelabelAvailable.toLocaleString()}
                      </Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Pre-Labels Failed</Box>
                      <Box
                        fontSize="heading-xl"
                        fontWeight="bold"
                        color="text-status-error"
                      >
                        {prelabelFailed.toLocaleString()}
                      </Box>
                    </div>
                  </ColumnLayout>
                )}
              </SpaceBetween>
            </Container>
          )}

          {rawJob.team_id && (
            <Container
              header={<Header variant="h2">Team Progress</Header>}
            >
              <SpaceBetween size="m">
                <Table
                  columnDefinitions={[
                    {
                      id: 'labeler',
                      header: 'Labeler',
                      cell: (item: LabelingMemberProgress) =>
                        item.email || item.user_id,
                    },
                    {
                      id: 'submitted',
                      header: 'Submitted',
                      cell: (item: LabelingMemberProgress) => item.submitted,
                    },
                    {
                      id: 'remaining',
                      header: 'Remaining',
                      cell: (item: LabelingMemberProgress) => item.remaining,
                    },
                  ]}
                  items={memberProgress}
                  variant="embedded"
                  empty={
                    <Box textAlign="center" color="text-body-secondary">
                      No team members currently hold tasks in this job.
                    </Box>
                  }
                />
                {unassignedCount > 0 && (
                  <Alert type="warning">
                    {unassignedCount.toLocaleString()} task
                    {unassignedCount === 1 ? ' is' : 's are'} unassigned.
                  </Alert>
                )}
              </SpaceBetween>
            </Container>
          )}

          <Container header={<Header variant="h2">Details</Header>}>
            <KeyValuePairs
              columns={2}
              items={[
                { label: 'Job ID', value: rawJob.job_id },
                {
                  label: 'Label Set',
                  value:
                    rawJob.label_set && rawJob.label_set.length > 0
                      ? rawJob.label_set.join(', ')
                      : '-',
                },
                {
                  label: 'Created',
                  value: rawJob.created_at
                    ? new Date(rawJob.created_at).toLocaleString()
                    : '-',
                },
                {
                  label: 'Completed',
                  value: rawJob.completed_at
                    ? new Date(rawJob.completed_at).toLocaleString()
                    : '-',
                },
                {
                  label: 'Stopped',
                  value: rawJob.stopped_at
                    ? new Date(rawJob.stopped_at).toLocaleString()
                    : '-',
                },
                {
                  label: 'Output Manifest',
                  value: rawJob.output_manifest_s3_uri ? (
                    <Box fontSize="body-s">
                      {rawJob.output_manifest_s3_uri}
                    </Box>
                  ) : (
                    '-'
                  ),
                },
              ]}
            />
          </Container>
        </SpaceBetween>

        <Modal
          visible={showStopModal}
          onDismiss={() => setShowStopModal(false)}
          header="Stop labeling job"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => setShowStopModal(false)}
                  disabled={stopping}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleStopJob}
                  loading={stopping}
                >
                  Stop Job
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Box>
              Are you sure you want to stop "{rawJob.job_name}"? Labelers
              will no longer be able to submit annotations. Annotations
              already submitted are retained.
            </Box>
            {stopError && <Alert type="error">{stopError}</Alert>}
          </SpaceBetween>
        </Modal>

        {/* Re-run pre-labels dialog (the stop-modal precedent,
            grounded-sam-prompt-guardrails-and-prelabel-retry
            Requirements 7.3-7.7): grounded-sam jobs edit one
            Prompt_Override entry per Label_Set label, pre-filled from
            the persisted map, with the shared Prompt_Guidance and the
            wizard's validation order; every other family confirms with
            the failed count only. Mounted only while open so
            zero-failed jobs render exactly as before (Requirement
            4.3). */}
        {showRerunModal && (
          <Modal
            visible
            onDismiss={() => setShowRerunModal(false)}
            header="Re-run pre-labels"
            data-testid="rerun-prelabels-modal"
            footer={
              <Box float="right">
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="link"
                    onClick={() => setShowRerunModal(false)}
                    disabled={rerunSubmitting}
                    data-testid="rerun-prelabels-cancel"
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="primary"
                    onClick={handleRerunPrelabels}
                    loading={rerunSubmitting}
                    data-testid="rerun-prelabels-submit"
                  >
                    Re-run pre-labels
                  </Button>
                </SpaceBetween>
              </Box>
            }
          >
            <SpaceBetween size="s">
              <Box>
                {`Re-run pre-label generation for the ${prelabelFailed.toLocaleString()} failed pre-label task${prelabelFailed === 1 ? '' : 's'} of "${rawJob.job_name}"? The failed tasks are reset and queued again.`}
              </Box>
              {isGroundedSamJob && rerunLabels.length > 0 && (
                <SpaceBetween size="m">
                  <Box>
                    Adjust the per-label text prompts below before
                    re-running, or leave them unchanged to retry with the
                    saved prompts.
                  </Box>
                  {rerunLabels.map((label) => (
                    <FormField
                      key={label}
                      label={label}
                      constraintText={PROMPT_GUIDANCE_CONSTRAINT}
                      info={<PromptGuidanceContent />}
                      errorText={
                        // Over-length first (the wizard's pinned message
                        // keeps precedence), then the Prompt_Guardrail
                        // period variant — the wizard's field-level
                        // convention (Requirements 7.3, 3.5).
                        (rerunOverrides[label] || '').length >
                        MAX_PROMPT_OVERRIDE_LENGTH
                          ? `The text prompt for label "${label}" exceeds ${MAX_PROMPT_OVERRIDE_LENGTH} characters`
                          : ALIGNMENT_BREAKING_PATTERN.test(
                                rerunOverrides[label] || ''
                              )
                            ? promptGuardrailMessage({
                                label,
                                source: 'override',
                              })
                            : undefined
                      }
                    >
                      <Input
                        value={rerunOverrides[label] || ''}
                        placeholder={label}
                        onChange={({ detail }) =>
                          setRerunOverrides((current) => ({
                            ...current,
                            [label]: detail.value,
                          }))
                        }
                        ariaLabel={`Text prompt for ${label}`}
                      />
                    </FormField>
                  ))}
                </SpaceBetween>
              )}
              {rerunError && (
                <Alert type="error" data-testid="rerun-prelabels-error">
                  <SpaceBetween size="xxs">
                    <Box>{rerunError}</Box>
                    {rerunErrorDetails.length > 0 && (
                      <ul>
                        {rerunErrorDetails.map((message, index) => (
                          <li key={`${message}-${index}`}>{message}</li>
                        ))}
                      </ul>
                    )}
                  </SpaceBetween>
                </Alert>
              )}
            </SpaceBetween>
          </Modal>
        )}
      </>
    );
  }

  return (
    <>
      <SpaceBetween size="l">
        <Container
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => navigate('/labeling')}>
                    Back to List
                  </Button>
                  {job.status === 'completed' && (
                    <>
                      <ButtonDropdown
                        items={[
                          {
                            id: 'transform',
                            text: 'Transform Manifest',
                            description: 'Convert to DDA-compatible format',
                          },
                          {
                            id: 'download-manifest',
                            text: 'Download Manifest',
                          },
                          {
                            id: 'view-s3',
                            text: 'View in S3',
                            external: true,
                          },
                        ]}
                        onItemClick={({ detail }) => {
                          if (detail.id === 'transform') {
                            setShowTransformModal(true);
                          } else if (detail.id === 'download-manifest') {
                            handleDownloadManifest();
                          } else if (detail.id === 'view-s3') {
                            window.open(
                              `https://s3.console.aws.amazon.com/s3/buckets/${job.output_s3.replace('s3://', '').split('/')[0]}`,
                              '_blank'
                            );
                          }
                        }}
                      >
                        Actions
                      </ButtonDropdown>
                      <Button variant="primary" onClick={handleDownloadOutput}>
                        Download Labeled Data
                      </Button>
                    </>
                  )}
                </SpaceBetween>
              }
            >
              {job.name}
            </Header>
          }
      >
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <div>{getStatusIndicator(job.status)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Task Type</Box>
            <div>{job.task_type}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Workforce</Box>
            <div>{job.workforce_type}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Created By</Box>
            <div>{job.created_by}</div>
          </div>
        </ColumnLayout>
      </Container>

      <Container header={<Header variant="h2">Progress</Header>}>
        <SpaceBetween size="l">
          <ProgressBar
            value={job.progress_percent}
            label="Labeling Progress"
            description={`${job.labeled_count} of ${job.images_count} images labeled`}
            additionalInfo={`${job.progress_percent}% complete`}
          />

          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Total Images</Box>
              <Box fontSize="heading-xl" fontWeight="bold">
                {job.images_count.toLocaleString()}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Labeled Images</Box>
              <Box fontSize="heading-xl" fontWeight="bold" color="text-status-success">
                {job.labeled_count.toLocaleString()}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Remaining</Box>
              <Box fontSize="heading-xl" fontWeight="bold" color="text-status-info">
                {(job.images_count - job.labeled_count).toLocaleString()}
              </Box>
            </div>
          </ColumnLayout>
        </SpaceBetween>
      </Container>

      <Container>
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'overview',
              label: 'Overview',
              content: (
                <SpaceBetween size="l">
                  <KeyValuePairs
                    columns={2}
                    items={[
                      {
                        label: 'Job ID',
                        value: job.job_id,
                      },
                      {
                        label: 'Ground Truth Job ARN',
                        value: (
                          <Box fontSize="body-s">
                            {job.ground_truth_job_arn}
                          </Box>
                        ),
                      },
                      {
                        label: 'Worker Portal',
                        value: job.worker_portal_url ? (
                          <Link
                            href={job.worker_portal_url}
                            external
                            externalIconAriaLabel="Opens in a new tab"
                          >
                            {job.worker_portal_url}
                          </Link>
                        ) : (
                          <Box fontSize="body-s" color="text-status-inactive">
                            Not available yet (private workforce sign-in URL)
                          </Box>
                        ),
                      },
                      {
                        label: 'AWS Console',
                        value: job.console_url ? (
                          <Link
                            href={job.console_url}
                            external
                            externalIconAriaLabel="Opens in a new tab"
                          >
                            View labeling job in SageMaker Ground Truth
                          </Link>
                        ) : (
                          '-'
                        ),
                      },
                      {
                        label: 'Created',
                        value: new Date(job.created_at).toLocaleString(),
                      },
                      {
                        label: 'Completed',
                        value: job.completed_at
                          ? new Date(job.completed_at).toLocaleString()
                          : '-',
                      },
                      {
                        label: 'Duration',
                        value: job.completed_at
                          ? `${Math.round((job.completed_at - job.created_at) / 3600000)} hours`
                          : `${Math.round((Date.now() - job.created_at) / 3600000)} hours (ongoing)`,
                      },
                    ]}
                  />
                </SpaceBetween>
              ),
            },
            {
              id: 'data',
              label: 'Data Locations',
              content: (
                <SpaceBetween size="l">
                  <KeyValuePairs
                    columns={1}
                    items={[
                      {
                        label: 'Input Manifest',
                        value: (
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box fontSize="body-s">
                              {job.manifest_s3}
                            </Box>
                            <Link onFollow={handleDownloadManifest}>Download</Link>
                          </SpaceBetween>
                        ),
                      },
                      {
                        label: 'Output Location',
                        value: (
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box fontSize="body-s">
                              {job.output_s3}
                            </Box>
                            {job.status === 'completed' && (
                              <Link onFollow={handleDownloadOutput}>Download</Link>
                            )}
                          </SpaceBetween>
                        ),
                      },
                    ]}
                  />

                  {job.status === 'completed' && (
                    <Alert type="success">
                      Labeling job completed successfully. Labeled data is available for download
                      and can be used for training.
                    </Alert>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'workers',
              label: 'Worker Metrics',
              content: (
                <SpaceBetween size="l">
                  <Alert type="info">
                    Worker metrics and quality statistics will be available here once the API
                    integration is complete.
                  </Alert>

                  <Box>
                    <Box variant="h3">Placeholder Metrics</Box>
                    <ColumnLayout columns={3} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Active Workers</Box>
                        <Box fontSize="heading-l">12</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Avg. Time per Image</Box>
                        <Box fontSize="heading-l">45s</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Quality Score</Box>
                        <Box fontSize="heading-l">94%</Box>
                      </div>
                    </ColumnLayout>
                  </Box>
                </SpaceBetween>
              ),
            },
          ]}
        />
      </Container>
    </SpaceBetween>

    <Modal
      visible={showTransformModal}
      onDismiss={() => setShowTransformModal(false)}
      header="Transform Manifest"
      size="large"
      footer={
        <Box float="right">
          <Button variant="link" onClick={() => setShowTransformModal(false)}>
            Close
          </Button>
        </Box>
      }
    >
      <ManifestTransformer usecaseId={job.usecase_id} preSelectedJobId={job.job_id} />
    </Modal>
  </>
  );
}
