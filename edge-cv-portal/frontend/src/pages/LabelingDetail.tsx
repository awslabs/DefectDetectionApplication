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
} from '@cloudscape-design/components';
import { useParams, useNavigate } from 'react-router-dom';
import { LabelingJob } from '../types';
import { apiService, LabelingMemberProgress } from '../services/api';
import ManifestTransformer from '../components/ManifestTransformer';

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

          {(llmModelId !== null || prelabelResolved) && (
            <Container header={<Header variant="h2">Auto-Labeling</Header>}>
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
